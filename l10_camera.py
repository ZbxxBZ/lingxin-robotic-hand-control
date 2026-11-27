"""摄像头 + 手部识别的公共部分：07_camera_mirror.py 和 06 网页控制台共用。本模块不访问真机。

- download_model / open_camera / draw_skeleton：模型下载、打开摄像头、画手部骨架
- CameraService：后台线程不停地取最新一帧 → 识别 → 换算成 L10 关节值（含滤波、标定、手势），
  同时把带骨架的画面编码成 JPEG，供网页以 MJPEG 实时显示。控制线程随时取“最新结果”，
  不会被摄像头帧率或识别耗时拖慢。
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np

import l10_config as cfg
from l10_mirror import (
    CALIBRATIONS,
    FEATURE_LABELS,
    PINCH_LABELS,
    RPS_LABELS,
    Deadband,
    HandTracker,
    MirrorMapper,
    OneEuroFilter,
    average_features,
    classify_rps,
    hand_features,
    load_calibration,
    save_calibration,
)

PROJECT = Path(__file__).resolve().parent
# 打包成 exe 后，程序文件解压在临时目录里（每次退出都会删掉）；标定这类要保存的文件放在 exe 旁边
DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT
MODEL_PATH = PROJECT / cfg.MIRROR_MODEL_PATH  # 打包时一起放进 exe
CALIBRATION_PATH = DATA_DIR / cfg.MIRROR_CALIBRATION_FILE
HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
)  # fmt: skip
TIP_POINTS = (4, 8, 12, 16, 20)
JPEG_QUALITY = 75
GESTURE_FRESH_S = 0.25  # 猜拳只认这么新的手势


def download_model(path: Path = MODEL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    print(f"下载 MediaPipe 手部模型：\n  {cfg.MIRROR_MODEL_URL}\n  → {path}")
    with urllib.request.urlopen(cfg.MIRROR_MODEL_URL, timeout=60) as response, part.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 16):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()
    if done < 1_000_000:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"只下载到 {done} 字节，可能被网络拦截了；可以用浏览器下载上面的地址")
    part.replace(path)
    print(f"完成（{done / 1e6:.1f} MB）")


def open_camera(index: int) -> cv2.VideoCapture:
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"打不开摄像头 {index}（外接摄像头一般是 1）")
    width, height = cfg.MIRROR_CAMERA_SIZE
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 只留最新一帧，减少延迟（部分驱动不支持，忽略即可）
    return cap


def draw_skeleton(image: np.ndarray, points: np.ndarray, box: tuple[int, int, int, int] | None = None) -> None:
    """在 BGR 图上画 21 点骨架。points 为归一化坐标；box=(x, y, w, h) 是画面在 image 中的位置。"""
    x0, y0, w, h = box or (0, 0, image.shape[1], image.shape[0])
    pts = [(int(x0 + x * w), int(y0 + y * h)) for x, y in points]
    for a, b in HAND_CONNECTIONS:
        cv2.line(image, pts[a], pts[b], (230, 230, 230), 2, cv2.LINE_AA)
    for i, p in enumerate(pts):
        color = (60, 140, 255) if i in TIP_POINTS else (110, 200, 90)
        cv2.circle(image, p, 4 if i % 4 == 0 else 3, color, -1, cv2.LINE_AA)


class CameraService:
    """后台摄像头线程。start() 立即返回，打开失败的原因放在 status()["error"]，并通过 on_log 报告。"""

    def __init__(self, on_log: Callable[[str, str], None] | None = None) -> None:
        self.on_log = on_log or (lambda level, text: print(text))
        self.mapper = MirrorMapper(load_calibration(CALIBRATION_PATH))
        self.deadband = Deadband(cfg.MIRROR_DEADBAND)  # 放在这里：重开摄像头后保留网页上调过的值
        self._lock = threading.Lock()
        self._frame_ready = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._opened = False
        self._starting = False
        self._error: str | None = None
        self._index = cfg.MIRROR_CAMERA_INDEX
        self._jpeg: bytes | None = None
        self._seq = 0
        self._calib: list | None = None  # [kind, 开始时间或 None, 样本]
        self._reset_latest()

    def _reset_latest(self) -> None:
        self._latest = {
            "mapped": None, "pinch": 0.0, "pinch_finger": None, "seen_at": 0.0, "gesture": None, "label": "", "score": 0.0, "fps": 0.0,
        }  # fmt: skip

    # ------------------------------------------------------------ 控制
    @property
    def running(self) -> bool:
        return self._opened and self._thread is not None and self._thread.is_alive()

    def start(self, index: int | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._index = cfg.MIRROR_CAMERA_INDEX if index is None else int(index)
        self._stop.clear()
        self._error, self._starting = None, True
        self._thread = threading.Thread(target=self._run, name="l10-camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._frame_ready:
            self._frame_ready.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def calibrate(self, kind: str) -> None:
        if kind not in CALIBRATIONS:
            raise ValueError(f"未知标定 {kind}")
        if not self.running:
            raise ValueError("请先开启摄像头")
        with self._lock:
            self._calib = [kind, None, []]
        self.on_log("info", f"标定“{CALIBRATIONS[kind]['label']}”：保持这个姿势 {cfg.MIRROR_CALIBRATION_S:.0f} 秒")

    def set_deadband(self, value: float) -> float:
        value = min(float(cfg.MIRROR_DEADBAND_MAX), max(0.0, float(value)))
        with self._lock:
            self.deadband.band = value
        return value

    def reset_calibration(self) -> None:
        with self._lock:
            self.mapper.reset()
        CALIBRATION_PATH.unlink(missing_ok=True)
        self.on_log("info", "已清除标定，恢复默认映射")

    # ------------------------------------------------------------ 读取
    def latest(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def fresh_gesture(self, now: float) -> str | None:
        with self._lock:
            latest = self._latest
            return latest["gesture"] if now - latest["seen_at"] < GESTURE_FRESH_S else None

    def wait_frame(self, last_seq: int, timeout: float = 1.0) -> tuple[int, bytes | None]:
        """等到有比 last_seq 新的画面（或超时 / 停止），返回 (序号, JPEG)。"""
        with self._frame_ready:
            self._frame_ready.wait_for(lambda: self._seq != last_seq or self._stop.is_set(), timeout)
            return self._seq, self._jpeg

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            latest = dict(self._latest)
            calib = self._calib
            done = list(self.mapper.done)
        seen = now - latest["seen_at"] < cfg.MIRROR_LOST_S
        progress = 0.0
        if calib is not None and calib[1] is not None:
            progress = min(1.0, (now - calib[1]) / cfg.MIRROR_CALIBRATION_S)
        mapped = latest["mapped"]
        return {
            "on": self.running,
            "starting": self._starting,
            "error": self._error,
            "index": self._index,
            "fps": round(latest["fps"], 1),
            "seen": seen,
            "label": {"Left": "左手", "Right": "右手"}.get(latest["label"], "手") if seen else "",
            "score": round(latest["score"], 2),
            "gesture": latest["gesture"] if seen else None,
            "gesture_label": RPS_LABELS.get(latest["gesture"], "") if seen else "",
            "pinch": round(latest["pinch"], 2) if seen else 0.0,
            "pinch_label": PINCH_LABELS.get(latest["pinch_finger"], "") if seen else "",
            "mapped": [round(v) for v in mapped] if mapped is not None and seen else None,
            "calibrated": done,
            "calibrating": calib[0] if calib is not None else None,
            "calib_progress": round(progress, 2),
            "deadband": self.deadband.band,
        }

    # ------------------------------------------------------------ 线程
    def _run(self) -> None:
        tracker = cap = None
        try:
            if not MODEL_PATH.exists():
                raise RuntimeError(f"缺少手部模型 {MODEL_PATH}，先运行 python 07_camera_mirror.py --download-model")
            tracker = HandTracker(MODEL_PATH)
            cap = open_camera(self._index)
            with self._lock:
                self._reset_latest()
            self._opened, self._starting = True, False
            self.on_log("info", f"摄像头 {self._index} 已开启")
            self._loop(tracker, cap)
        except Exception as error:  # 打开失败或运行中出错：记录原因，线程结束
            self._error = str(error)
            self.on_log("error", f"摄像头：{error}")
        finally:
            self._opened, self._starting = False, False
            if cap is not None:
                cap.release()
            if tracker is not None:
                tracker.close()
            with self._frame_ready:
                self._jpeg = None
                self._seq += 1
                self._frame_ready.notify_all()
            if self._stop.is_set() and self._error is None:
                self.on_log("info", "摄像头已关闭")

    def _loop(self, tracker: HandTracker, cap: cv2.VideoCapture) -> None:
        smooth = OneEuroFilter(cfg.MIRROR_FILTER_MIN_CUTOFF, cfg.MIRROR_FILTER_BETA)
        with self._lock:
            self.deadband.reset()
        failures, fps, last = 0, 0.0, time.monotonic()
        while not self._stop.is_set():
            ok, frame = cap.read()
            now = time.monotonic()
            if not ok or frame is None:
                failures += 1
                if failures > 30:
                    raise RuntimeError("摄像头连续读不到画面")
                time.sleep(0.03)
                continue
            failures = 0
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-3)
            last = now
            frame = cv2.flip(frame, 1)  # 镜像：像照镜子一样
            tracked = tracker.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), int(now * 1000))
            feats = None
            if tracked is not None:
                try:
                    feats = hand_features(tracked.world)
                except ValueError:
                    tracked = None
            with self._lock:
                self._latest["fps"] = fps
                if feats is not None:
                    pinch_finger, pinch = self.mapper.pinch(feats)
                    self._latest.update(
                        pinch=pinch,
                        pinch_finger=pinch_finger,
                        mapped=tuple(self.deadband(smooth(self.mapper.map(feats), now))),
                        gesture=classify_rps(self.mapper.progress(feats)),
                        seen_at=now,
                        label=tracked.label,
                        score=tracked.score,
                    )
            self._update_calibration(feats, now)
            if tracked is not None:
                draw_skeleton(frame, tracked.image)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                with self._frame_ready:
                    self._jpeg = buf.tobytes()
                    self._seq += 1
                    self._frame_ready.notify_all()

    def _update_calibration(self, feats: dict | None, now: float) -> None:
        with self._lock:
            calib = self._calib
            if calib is None:
                return
            if calib[1] is None:
                calib[1] = now
            if feats is not None:
                calib[2].append(feats)
            kind, start, samples = calib
            if now - start < cfg.MIRROR_CALIBRATION_S:
                return
            label = CALIBRATIONS[kind]["label"]
            if len(samples) < 5:
                if now - start > cfg.MIRROR_CALIBRATION_S + 2.0:
                    self._calib = None
                    message = ("error", f"标定“{label}”失败：没看清手，请把手放进画面再试")
                else:
                    return
            else:
                self._calib = None
                try:
                    accepted, rejected = self.mapper.calibrate(kind, average_features(samples))
                except ValueError as error:
                    message = ("warning", f"{error}，没有采用；请摆好姿势再标定")
                else:
                    save_calibration(CALIBRATION_PATH, self.mapper)
                    if rejected:
                        names = "、".join(FEATURE_LABELS[n] for n in rejected)
                        message = ("warning", f"“{label}”标定完成；{names} 和默认差别太大没有采用")
                    else:
                        message = ("info", f"“{label}”标定完成")
        self.on_log(*message)
