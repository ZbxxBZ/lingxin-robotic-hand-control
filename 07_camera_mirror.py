"""摄像头镜像演示：L10 跟着你的手动，还能和你玩石头剪刀布。

普通 USB 摄像头 + MediaPipe 识别你的手（左右手都行），实时换算成 L10 的 10 个关节：
四指弯曲、三个侧摆、拇指弯曲 / 翻滚 / 旋转。跟随时有三层保护：
  - 防撞：四指要大幅运动时拇指不进掌心；拇指折着时四指先等拇指让开
  - 堵转卸力：关节被挡住不动时，不再继续往里顶
  - 按力卸载：指尖力超过 MIRROR_FORCE_LIMIT，那根手指往伸直方向退
没看到手时，L10 保持当前姿态，不会乱动。

第一次使用：
    python 07_camera_mirror.py --download-model   # 下载 MediaPipe 手部模型（约 7.5 MB，只需一次）
    python 07_camera_mirror.py --no-hand          # 先不连手：只看识别效果和映射出的关节值
      在窗口里标定（每个姿势保持 1 秒）：张开五指按 O，握拳按 F，拇指尖碰小指根按 T
然后连上手：
    python 07_camera_mirror.py                    # 启动后先暂停，按空格开始跟随
    python 07_camera_mirror.py --game             # 直接进入石头剪刀布

窗口按键（先用鼠标点一下窗口）：
  空格    开始 / 暂停跟随；猜拳模式下开始一局
  G       切换 镜像跟随 / 石头剪刀布
  O F T   标定：张开五指 / 握拳 / 拇指尖碰小指根      X  清除标定
  P       在终端打印当前特征和映射值（映射不准时用来调参数）
  R       暂停跟随并张开手
  Q / Esc 退出（手会先张开）

参数在 l10_config.py 的“摄像头镜像”和“石头剪刀布”两节。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from linkerbot_hand import HandError
from PIL import Image, ImageDraw, ImageFont

import l10_config as cfg
from l10_hand import (
    ArrivalCheck,
    L10Hand,
    add_connection_args,
    check_pose,
    describe_relief,
    force_relief_step,
    plan_transition,
    relieve_stall,
)
from l10_camera import MODEL_PATH, download_model, draw_skeleton, open_camera
from l10_mirror import (
    CALIBRATIONS,
    FEATURE_LABELS,
    Deadband,
    PHASE_TEXT,
    PINCH_LABELS,
    RPS_LABELS,
    HandTracker,
    MirrorGuard,
    MirrorMapper,
    OneEuroFilter,
    RpsGame,
    average_features,
    classify_rps,
    hand_features,
    load_calibration,
    save_calibration,
)

PROJECT = Path(__file__).resolve().parent
WINDOW = "L10 Camera Mirror"
CAM_W, CAM_H = 640, 480
PANEL_W = 430
CANVAS_H = 570
FAIL_LIMIT = 10  # 连续这么多次读写失败才算通信中断

# 颜色（BGR）
BG = (34, 30, 28)
TEXT = (235, 235, 235)
MUTED = (150, 150, 150)
GREEN = (110, 200, 90)
AMBER = (40, 180, 245)
RED = (80, 80, 235)
BLUE = (220, 150, 70)
ORANGE = (60, 140, 255)


# ---------------------------------------------------------------- 准备工作
def tare_forces(hand: L10Hand) -> dict[str, float] | None:
    """手张开、指尖不接触时记录零点；读不到力返回 None（按力卸载随之关闭）。"""
    sums = dict.fromkeys(cfg.FINGERS, 0.0)
    counts = dict.fromkeys(cfg.FINGERS, 0)
    try:
        for _ in range(cfg.DEMO_TARE_SAMPLES):
            forces, _ = hand.read_force_detail(timeout=cfg.TIMEOUT_S)
            for finger, value in forces.items():
                if value is not None:
                    sums[finger] += value
                    counts[finger] += 1
            time.sleep(0.02)
    except (HandError, RuntimeError) as error:
        print(f"[警告] 读不到指尖力，按力卸载关闭（堵转卸力仍然有效）：{error}")
        return None
    tare = {f: (sums[f] / counts[f] if counts[f] else 0.0) for f in cfg.FINGERS}
    print("去皮零点：" + "  ".join(f"{cfg.FINGER_LABELS[f]} {v:.1f}" for f, v in tare.items()))
    return tare


# ---------------------------------------------------------------- 控制线程
class HandWorker(threading.Thread):
    """唯一访问真机的线程：周期性读角度 / 触觉，按模式发指令。

    模式：hold 不发新指令；follow 跟随人手（经 MirrorGuard 加工）；
          pose 按 plan_transition 分段走到一个姿态，到位后堵转卸力 + 按力卸载（同网页控制台预设）。
    """

    def __init__(self, hand: L10Hand, tare: dict[str, float] | None) -> None:
        super().__init__(name="l10-worker", daemon=True)
        self.hand = hand
        self.tare = tare
        self.guard = MirrorGuard()
        self._lock = threading.Lock()
        self._running = True
        self._mode = "hold"
        self._desired: tuple[float, ...] | None = None
        self._pinch, self._pinch_finger = 0.0, None
        self._goto: tuple[int, ...] | None = None
        self._reset_guard = False
        self._events: deque[str] = deque(maxlen=20)
        self._state: dict = {"measured": None, "cmd": None, "forces": {}, "error": None, "hz": 0.0}
        self._last_sent: tuple[int, ...] | None = None
        self._last_send_t = 0.0

    # ------------------------------------------------------------ 主线程调用
    def follow(self, desired: tuple[float, ...], pinch: float = 0.0, pinch_finger: str | None = None) -> None:
        with self._lock:
            if self._mode != "follow":
                self._mode, self._reset_guard = "follow", True
            self._desired, self._pinch, self._pinch_finger = tuple(desired), float(pinch), pinch_finger

    def hold(self) -> None:
        with self._lock:
            self._mode, self._desired, self._goto = "hold", None, None

    def goto(self, pose: tuple[int, ...]) -> None:
        with self._lock:
            self._mode, self._goto = "pose", check_pose(pose)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def take_events(self) -> list[str]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def stop(self) -> None:
        self._running = False
        self.join(timeout=3.0)

    # ------------------------------------------------------------ 线程内部
    def _event(self, text: str) -> None:
        with self._lock:
            self._events.append(text)

    def _send(self, pose: tuple[int, ...]) -> None:
        self.hand.set_pose(pose)
        self._last_sent, self._last_send_t = tuple(pose), time.monotonic()

    def run(self) -> None:
        tick, failures, hz = 0, 0, 0.0
        measured: tuple[int, ...] | None = None
        forces: dict[str, float | None] = {}
        force_at = 0.0
        plan: list[tuple[int, ...]] = []
        check: ArrivalCheck | None = None
        target: tuple[int, ...] | None = None
        relief_base: tuple[int, ...] | None = None
        relief_until = 0.0
        error: str | None = None
        last = time.monotonic()
        while self._running:
            start = time.monotonic()
            with self._lock:
                mode, desired, goto = self._mode, self._desired, self._goto
                pinch, pinch_finger = self._pinch, self._pinch_finger
                reset, self._goto, self._reset_guard = self._reset_guard, None, False
            try:
                measured = self.hand.get_pose()
                fresh = None
                if self.tare is not None and tick % cfg.MIRROR_FORCE_EVERY == 0:
                    try:
                        raw, _ = self.hand.read_force_detail()
                        fresh = {
                            f: None if raw.get(f) is None else max(0.0, raw[f] - self.tare[f])
                            for f in cfg.FINGERS
                        }
                        forces, force_at = fresh, time.monotonic()
                    except HandError:
                        pass  # 偶尔一帧读不全：沿用上一帧，不影响跟随
                now = time.monotonic()
                if mode != "pose":
                    plan, check, relief_until = [], None, 0.0
                if goto is not None:
                    plan, check, relief_until = plan_transition(measured, goto), None, 0.0
                    if len(plan) > 1:
                        self._event(f"分 {len(plan)} 段过渡，避免拇指与四指相撞")

                if mode == "follow" and desired is not None:
                    if reset:
                        self.guard.reset()
                    cmd = self.guard.step(desired, measured, now, fresh, pinch=pinch, pinch_finger=pinch_finger)
                    for text in self.guard.take_events():
                        self._event(text)
                    changed = self._last_sent is None or max(
                        abs(a - b) for a, b in zip(cmd, self._last_sent, strict=True)
                    ) >= cfg.MIRROR_SEND_MIN
                    if changed or now - self._last_send_t > 0.5:
                        self._send(cmd)
                elif mode == "pose" and plan:
                    stage = plan[0]
                    if check is None:
                        self._send(stage)
                        check = ArrivalCheck(stage, now)
                    elif check.update(measured, now):
                        plan.pop(0)
                        if not plan:  # 最后一段结束
                            target = stage
                            if cfg.STALL_RELIEF and check.outcome == "stalled":
                                relieved, changes = relieve_stall(stage, measured)
                                if changes:
                                    self._send(relieved)
                                    target = relieved
                                    self._event(f"卸力：{describe_relief(changes)}")
                            if cfg.FORCE_RELIEF and self.tare is not None:
                                relief_base, relief_until = target, now + cfg.FORCE_RELIEF_WINDOW_S
                        check = None
                elif mode == "pose" and relief_until > now and fresh is not None and target is not None:
                    assert relief_base is not None
                    new, changes = force_relief_step(target, relief_base, fresh)
                    if changes:
                        self._send(new)
                        target = new
                failures, error = 0, None
            except Exception as exc:  # 兜底：记录后继续，避免控制线程静默退出
                failures += 1
                if failures >= FAIL_LIMIT:
                    error = f"通信中断：{exc}"
            end = time.monotonic()
            hz = 0.9 * hz + 0.1 / max(end - last, 1e-3)
            last = end
            with self._lock:
                following = mode == "follow" and desired is not None
                self._state = {
                    "measured": measured,
                    "cmd": self._last_sent,
                    "forces": forces,
                    "force_age": end - force_at if force_at else None,
                    "phase": self.guard.phase if following else ("pose" if plan else "hold"),
                    "floors": self.guard.floors if following else {},
                    "error": error,
                    "hz": hz,
                }
            tick += 1
            time.sleep(max(0.0, cfg.MIRROR_LOOP_DT_S - (time.monotonic() - start)))


# ---------------------------------------------------------------- 文字绘制（中文用 PIL）
class TextLayer:
    FONT_PATHS = (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    )

    def __init__(self) -> None:
        self.path = next((p for p in self.FONT_PATHS if Path(p).exists()), None)
        if self.path is None:
            print("[提示] 没找到中文字体，界面文字会显示成 ASCII")
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}
        self.items: list[tuple] = []

    def add(self, xy: tuple[int, int], text: str, size: int = 15, color=TEXT, anchor: str = "la", stroke: int = 0):
        self.items.append((xy, text, size, color, anchor, stroke))

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self._fonts:
            self._fonts[size] = ImageFont.truetype(self.path, size)
        return self._fonts[size]

    def render(self, canvas: np.ndarray) -> None:
        items, self.items = self.items, []
        if self.path is None:
            for (x, y), text, size, color, _anchor, _stroke in items:
                ascii_text = text.encode("ascii", "replace").decode()
                cv2.putText(canvas, ascii_text, (x, y + size), cv2.FONT_HERSHEY_SIMPLEX, size / 30, color, 1)
            return
        image = Image.fromarray(canvas[:, :, ::-1])
        draw = ImageDraw.Draw(image)
        for xy, text, size, color, anchor, stroke in items:
            rgb = (color[2], color[1], color[0])
            draw.text(xy, text, font=self._font(size), fill=rgb, anchor=anchor,
                      stroke_width=stroke, stroke_fill=(0, 0, 0))  # fmt: skip
        canvas[:] = np.asarray(image)[:, :, ::-1]


# ---------------------------------------------------------------- 界面与主循环
class MirrorApp:
    def __init__(self, tracker: HandTracker, cap: cv2.VideoCapture, worker: HandWorker | None, game_mode: bool):
        self.tracker, self.cap, self.worker = tracker, cap, worker
        self.calibration_path = PROJECT / cfg.MIRROR_CALIBRATION_FILE
        self.mapper = MirrorMapper(load_calibration(self.calibration_path))
        self.filter = OneEuroFilter(cfg.MIRROR_FILTER_MIN_CUTOFF, cfg.MIRROR_FILTER_BETA)
        self.deadband = Deadband(cfg.MIRROR_DEADBAND)
        self.text = TextLayer()
        self.game = RpsGame()
        self.mode = "game" if game_mode else "mirror"
        self.following = False
        self.calib: tuple[str, float, list] | None = None
        self.notices: deque[tuple[float, str, tuple]] = deque(maxlen=3)
        self.hand_seen_at = 0.0
        self.mapped: np.ndarray | None = None
        self.feats: dict[str, float] | None = None
        self.gesture: str | None = None
        self.pinch, self.pinch_finger = 0.0, None
        self.fps = 0.0
        if self.mode == "game" and worker:
            worker.goto(cfg.GAME_READY)

    def notice(self, text: str, color=TEXT) -> None:
        self.notices.append((time.monotonic(), text, color))
        print(text)

    # ------------------------------------------------------------ 按键
    def on_key(self, key: int, now: float) -> bool:
        """处理按键；返回 False 表示退出。"""
        if key in (ord("q"), ord("Q"), 27):
            return False
        if key == ord(" "):
            if self.mode == "game":
                if self.game.state in ("idle", "result"):
                    self.game.start(now)
            else:
                self.set_following(not self.following)
        elif key in (ord("g"), ord("G")):
            self.set_following(False)
            self.mode = "game" if self.mode == "mirror" else "mirror"
            self.game.state = "idle"
            if self.worker:
                self.worker.goto(cfg.GAME_READY if self.mode == "game" else cfg.POSES["open"])
            self.notice("进入石头剪刀布：按空格开始一局" if self.mode == "game" else "回到镜像跟随：按空格开始")
        elif key in (ord("r"), ord("R")):
            self.set_following(False)
            if self.worker:
                self.worker.goto(cfg.POSES["open"])
            self.notice("暂停跟随，张开手")
        elif key in (ord("p"), ord("P")):
            self.print_features()
        elif key in (ord("x"), ord("X")):
            self.mapper.reset()
            self.calibration_path.unlink(missing_ok=True)
            self.notice("已清除标定，恢复默认映射")
        else:
            for kind, spec in CALIBRATIONS.items():
                if key in (ord(spec["key"].lower()), ord(spec["key"])):
                    self.calib = (kind, now, [])
                    self.notice(f"标定“{spec['label']}”：保持这个姿势 {cfg.MIRROR_CALIBRATION_S:.0f} 秒")
        return True

    def print_features(self) -> None:
        """在终端打印当前特征和映射值，方便调 MIRROR_FEATURE_DEFAULTS。"""
        if self.feats is None or self.mapped is None:
            self.notice("没看到手，无法打印")
            return
        print("---- 当前特征（括号里是 A→B 标定值）")
        for name, value in self.feats.items():
            a, b = self.mapper.points[name]
            print(f"  {FEATURE_LABELS[name]:<6s} {value:8.2f}   ({a:.2f} → {b:.2f})")
        print("  映射关节值：" + ", ".join(f"{v:.0f}" for v in self.mapped))
        self.notice("已在终端打印当前特征和映射值")

    def set_following(self, on: bool) -> None:
        if on and self.worker is None:
            self.notice("预览模式（--no-hand）没有连接手，只显示映射值")
            return
        if on == self.following:
            return
        self.following = on
        if not on and self.worker:
            self.worker.hold()
        if on:
            self.notice("开始跟随", GREEN)

    # ------------------------------------------------------------ 标定
    def update_calibration(self, feats: dict | None, now: float) -> None:
        if self.calib is None:
            return
        kind, start, samples = self.calib
        if feats is not None:
            samples.append(feats)
        if now - start < cfg.MIRROR_CALIBRATION_S:
            return
        label = CALIBRATIONS[kind]["label"]
        if len(samples) < 5:
            if now - start > cfg.MIRROR_CALIBRATION_S + 2.0:
                self.calib = None
                self.notice(f"标定“{label}”失败：没看清手，请把手放进画面再按一次", RED)
            return
        self.calib = None
        try:
            accepted, rejected = self.mapper.calibrate(kind, average_features(samples))
        except ValueError as error:
            self.notice(f"{error}，没有采用；请摆好姿势再按 {CALIBRATIONS[kind]['key']}", RED)
            return
        save_calibration(self.calibration_path, self.mapper)
        if rejected:
            names = "、".join(FEATURE_LABELS[n] for n in rejected)
            self.notice(f"“{label}”标定完成；{names} 和默认差别太大没有采用（姿势对吗？）", AMBER)
        else:
            self.notice(f"“{label}”标定完成", GREEN)

    # ------------------------------------------------------------ 主循环
    def run(self) -> None:
        cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
        last = time.monotonic()
        read_failures = 0
        while True:
            ok, frame = self.cap.read()
            now = time.monotonic()
            if not ok or frame is None:
                read_failures += 1
                if read_failures > 30:
                    raise RuntimeError("摄像头连续读不到画面")
                time.sleep(0.03)
                continue
            read_failures = 0
            self.fps = 0.9 * self.fps + 0.1 / max(now - last, 1e-3)
            last = now
            frame = cv2.flip(frame, 1)  # 镜像显示：像照镜子一样
            tracked = self.tracker.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), int(now * 1000))

            feats = None
            if tracked is not None:
                try:
                    feats = hand_features(tracked.world)
                except ValueError:
                    tracked = None
            self.feats = feats
            if feats is not None:
                self.hand_seen_at = now
                self.mapped = self.deadband(self.filter(self.mapper.map(feats), now))
                self.gesture = classify_rps(self.mapper.progress(feats))
                self.pinch_finger, self.pinch = self.mapper.pinch(feats)
            else:
                self.gesture = None
            self.update_calibration(feats, now)

            if self.worker:
                for text in self.worker.take_events():
                    self.notice(text, AMBER)
                if self.mode == "mirror" and self.following and feats is not None and self.mapped is not None:
                    self.worker.follow(tuple(self.mapped), self.pinch, self.pinch_finger)

            overlay = ("", "")
            if self.mode == "game":
                overlay = self.game.update(now, self.gesture, self.worker)

            cv2.imshow(WINDOW, self.draw(frame, tracked, overlay, now))
            key = cv2.waitKey(1) & 0xFF
            if key != 0xFF and not self.on_key(key, now):
                break
            if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break

    # ------------------------------------------------------------ 绘制
    def draw(self, frame: np.ndarray, tracked, overlay: tuple[str, str], now: float) -> np.ndarray:
        canvas = np.full((CANVAS_H, CAM_W + PANEL_W, 3), BG, dtype=np.uint8)
        h, w = frame.shape[:2]
        scale = min(CAM_W / w, CAM_H / h)
        dw, dh = int(w * scale), int(h * scale)
        ox, oy = (CAM_W - dw) // 2, (CAM_H - dh) // 2
        canvas[oy : oy + dh, ox : ox + dw] = cv2.resize(frame, (dw, dh))
        if tracked is not None:
            draw_skeleton(canvas, tracked.image, (ox, oy, dw, dh))
        snap = self.worker.snapshot() if self.worker else None
        self._draw_panel(canvas, snap, tracked, now)
        self._draw_bottom(canvas, now)
        if self.mode == "game":
            self._draw_game(canvas, overlay)
        if self.calib is not None:
            kind, start, _ = self.calib
            frac = min(1.0, (now - start) / cfg.MIRROR_CALIBRATION_S)
            cv2.rectangle(canvas, (20, CAM_H - 40), (20 + int(600 * frac), CAM_H - 30), GREEN, -1)
            self.text.add((20, CAM_H - 70), f"标定：{CALIBRATIONS[kind]['label']}，请保持", 20, GREEN, stroke=2)
        self.text.render(canvas)
        return canvas

    def _draw_panel(self, canvas: np.ndarray, snap: dict | None, tracked, now: float) -> None:
        x0 = CAM_W + 14
        t = self.text
        title = "L10 摄像头镜像" if self.mode == "mirror" else "L10 石头剪刀布"
        t.add((x0, 10), title, 22)
        if self.worker is None:
            t.add((x0, 44), "预览模式：没有连接手（--no-hand）", 15, AMBER)
        elif snap and snap.get("error"):
            t.add((x0, 44), snap["error"][:40], 15, RED)
        elif self.mode == "game":
            t.add((x0, 44), "猜拳中：L10 不跟随你的手", 15, MUTED)
        elif self.following:
            t.add((x0, 44), "● 跟随中（空格暂停）", 15, GREEN)
        else:
            t.add((x0, 44), "○ 已暂停（空格开始跟随）", 15, MUTED)
        if tracked is not None:
            side = {"Left": "左手", "Right": "右手"}.get(tracked.label, "手")
            gesture = f" · 手势 {RPS_LABELS[self.gesture]}" if self.gesture else ""
            if self.pinch >= 0.05:
                gesture += f" · 捏取（{PINCH_LABELS.get(self.pinch_finger, '')}）{self.pinch:.0%}"
            t.add((x0, 68), f"看到{side} {tracked.score:.2f}{gesture} · {self.fps:.0f} fps", 15)
        elif now - self.hand_seen_at > cfg.MIRROR_LOST_S:
            hold = "，L10 保持当前姿态" if self.following else ""
            t.add((x0, 68), f"没看到手{hold} · {self.fps:.0f} fps", 15, AMBER)
        phase = PHASE_TEXT.get(snap.get("phase", ""), "") if snap else ""
        if phase:
            t.add((x0, 92), phase, 15, AMBER)

        # 关节条
        top, row = 146, 24
        bx0, bx1 = x0 + 108, x0 + 300
        t.add((x0, top - 20), "关节（0~255）", 13, MUTED)
        t.add((bx0, top - 20), "白=人手  蓝=指令  橙=实测", 13, MUTED)
        mapped = self.mapped
        cmd = snap.get("cmd") if snap else None
        measured = snap.get("measured") if snap else None
        floors = snap.get("floors", {}) if snap else {}

        def px(v: float) -> int:
            return int(bx0 + (bx1 - bx0) * min(255.0, max(0.0, float(v))) / 255.0)

        for j, label in enumerate(cfg.JOINT_LABELS):
            y = top + j * row
            t.add((x0, y), label.replace("(侧摆)", ""), 14)
            cv2.rectangle(canvas, (bx0, y + 3), (bx1, y + 15), (70, 66, 62), -1)
            fill = cmd[j] if cmd is not None else (mapped[j] if mapped is not None else None)
            if fill is not None:
                cv2.rectangle(canvas, (bx0, y + 3), (px(fill), y + 15), AMBER if j in floors else BLUE, -1)
            if mapped is not None:
                cv2.line(canvas, (px(mapped[j]), y), (px(mapped[j]), y + 18), (255, 255, 255), 2)
            if measured is not None:
                cv2.line(canvas, (px(measured[j]), y), (px(measured[j]), y + 18), ORANGE, 3)
            values = []
            if mapped is not None:
                values.append(f"{mapped[j]:.0f}")
            if measured is not None:
                values.append(f"{measured[j]}")
            t.add((bx1 + 8, y), "/".join(values), 13, MUTED)

        # 指尖力
        fy = top + 10 * row + 10
        forces = snap.get("forces", {}) if snap else {}
        if self.worker is not None:
            stale = snap.get("force_age") if snap else None
            note = "" if stale is None or stale < 1.0 else "（数据过旧）"
            t.add((x0, fy), f"指尖力（去皮，超过 {cfg.MIRROR_FORCE_LIMIT} 会卸力）{note}", 13, MUTED)
            for i, finger in enumerate(cfg.FINGERS):
                cx = x0 + i * 80
                value = forces.get(finger)
                t.add((cx, fy + 20), cfg.FINGER_LABELS[finger], 13)
                cv2.rectangle(canvas, (cx, fy + 40), (cx + 70, fy + 50), (70, 66, 62), -1)
                if value is not None:
                    frac = min(1.0, value / max(1, cfg.DEMO_DISPLAY_MAX))
                    color = RED if value > cfg.MIRROR_FORCE_LIMIT else GREEN
                    cv2.rectangle(canvas, (cx, fy + 40), (cx + int(70 * frac), fy + 50), color, -1)
                    t.add((cx + 70, fy + 20), f"{value:.0f}", 13, MUTED, anchor="ra")

        # 标定 / 通信
        cy = fy + 66
        marks = "   ".join(
            f"{spec['key']} {spec['label']}（{'已标' if kind in self.mapper.done else '未标'}）"
            for kind, spec in CALIBRATIONS.items()
        )
        t.add((x0, cy), f"标定：{marks}", 13, GREEN if len(self.mapper.done) == len(CALIBRATIONS) else MUTED)
        if snap:
            t.add((x0, cy + 22), f"控制循环 {snap.get('hz', 0):.0f} Hz", 13, MUTED)

    def _draw_bottom(self, canvas: np.ndarray, now: float) -> None:
        y = CAM_H + 8
        for stamp, text, color in list(self.notices):
            if now - stamp < 6.0:
                self.text.add((14, y), text[:60], 14, color)
                y += 20
        self.text.add(
            (14, CANVAS_H - 22),
            "空格 跟随/开局   G 猜拳   O/F/T 标定   X 清除标定   P 打印特征   R 张开   Q 退出",
            13,
            MUTED,
        )

    def _draw_game(self, canvas: np.ndarray, overlay: tuple[str, str]) -> None:
        big, small = overlay
        s = self.game.score
        self.text.add((16, 12), f"你 {s['human']} : {s['robot']} L10    平局 {s['draw']}", 22, TEXT, stroke=2)
        if big:
            self.text.add((CAM_W // 2, CAM_H // 2 - 20), big, 72, (255, 255, 255), anchor="mm", stroke=4)
        if small:
            self.text.add((CAM_W // 2, CAM_H // 2 + 50), small, 24, (255, 255, 255), anchor="mm", stroke=3)


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_connection_args(parser)
    parser.add_argument("--camera", type=int, default=cfg.MIRROR_CAMERA_INDEX, help="摄像头编号")
    parser.add_argument("--no-hand", action="store_true", help="不连接 L10，只看识别和映射")
    parser.add_argument("--game", action="store_true", help="启动后直接进入石头剪刀布")
    parser.add_argument("--download-model", action="store_true", help="下载 MediaPipe 手部模型后退出")
    args = parser.parse_args()

    model_path = MODEL_PATH
    if args.download_model:
        download_model(model_path)
        print("下一步：python 07_camera_mirror.py --no-hand")
        return 0
    if not model_path.exists():
        print(
            f"缺少 MediaPipe 手部模型 {model_path}\n"
            f"  运行 python 07_camera_mirror.py --download-model 自动下载，\n"
            f"  或用浏览器下载 {cfg.MIRROR_MODEL_URL}\n"
            f"  放到 {model_path}"
        )
        return 1

    tracker = HandTracker(model_path)
    cap = None
    hand: L10Hand | None = None
    worker: HandWorker | None = None
    try:
        cap = open_camera(args.camera)
        if not args.no_hand:
            hand = L10Hand.from_args(args)
            hand.open()
            hand.set_speeds(cfg.MIRROR_SPEED)
            print("张开手...")
            hand.move_safely(cfg.POSES["open"])
            print("去皮：请确认指尖没有接触任何东西")
            worker = HandWorker(hand, tare_forces(hand))
            worker.start()
        print("窗口已打开：先用鼠标点一下窗口，再按空格开始（Q 退出）")
        MirrorApp(tracker, cap, worker, args.game).run()
    except KeyboardInterrupt:
        print("\n已中断")
    finally:
        if worker is not None:
            worker.stop()
        if hand is not None and hand.is_open:
            try:
                print("张开手...")
                hand.move_safely(cfg.POSES["open"])
            except Exception as error:  # 退出时尽力张开，失败也要断开
                print(f"[警告] 退出时张开手失败：{error}")
            hand.close()
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()
        tracker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
