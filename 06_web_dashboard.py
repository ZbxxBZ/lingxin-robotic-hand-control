"""网页控制台：直接用 linkerbot-hand SDK 控制 L10，在浏览器里实时查看状态。

    python 06_web_dashboard.py                # 自动打开 http://127.0.0.1:8765
    python 06_web_dashboard.py --no-browser   # 不自动打开浏览器
    python 06_web_dashboard.py --side left --interface pcan --channel PCAN_USBBUS1

页面功能：
  - 10 个关节滑块（显示目标值和实测值）、全局速度
  - 从官方控制台 l10-dashboard_2025.08.06 迁移来的预设动作、数字手势和 10 个动画
  - l10_config.POSES 里的官方 GUI 预设
  - 实时可视化：手部示意图、五指 12×6 触觉热力图、指尖力条和曲线、电机温度、故障、日志
  - “停止并保持”（Esc）：停止动画 / 镜像跟随 / 猜拳，并把目标设为当前实测角度
  - 摄像头镜像：L10 实时模仿你的手；石头剪刀布（与 07_camera_mirror.py 同一套识别、标定和保护）
  - 握手（v2）：每根手指按自己的指尖力合拢到合适的松紧，你抽手时按触觉自动松开

注意：
  - 不能和 can_bridge / 官方控制台同时运行（它们都要独占 PCAN）。
  - 启动时只读取当前角度作为目标，不会主动运动；关闭程序时手保持当前姿态。
  - 只监听本机 127.0.0.1，并拒绝来自其他网页的跨站请求。
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from linkerbot_hand import HandError

import l10_actions as actions
import l10_config as cfg
from l10_hand import (
    ArrivalCheck,
    L10Hand,
    add_connection_args,
    check_pose,
    check_speeds,
    describe_relief,
    force_relief_step,
    plan_transition,
    relieve_stall,
)
from l10_camera import MODEL_PATH, CameraService
from l10_handshake import HandshakeController, ready_pose
from l10_mirror import CALIBRATIONS, PHASE_TEXT, PINCH_LABELS, MirrorGuard, RpsGame

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MODEL_FILES = {  # 官方 L10 模型（tools/convert_l10_model.py 生成）
    f"/model/l20lite_{side}.{ext}": (f"model/l20lite_{side}.{ext}", ctype)
    for side in ("left", "right")
    for ext, ctype in (("json", "application/json; charset=utf-8"), ("bin", "application/octet-stream"))
}
LOOP_DT_S = 0.05  # 服务循环周期（20 Hz）：发目标、读角度
FORCE_EVERY = 2  # 每 2 个循环读一次触觉点阵（10 Hz），减轻总线负担
SLOW_READ_S = 1.0  # 温度、故障的读取周期
CORE_FAIL_LIMIT = 10  # 角度/指令连续失败多少次算通信中断
MISS_REPORT_S = 10.0  # 偶发超时最多每隔多久汇总一条日志
MISS_REPORT_MIN = 5  # 统计窗口内某类超时达到这么多次才写日志（偶尔一两次不提示）
FORCE_STALE_S = 1.0  # 点阵数据超过多久没更新算“数据延迟”
MIN_ANIM_INTERVAL_MS = 50
MAX_BODY_BYTES = 64 * 1024
FOLLOW_RESEND_S = 0.5  # 跟随时目标没变化也每隔这么久重发一次
MANUAL_COMMANDS = {"joint", "pose", "preset", "animation", "stop"}  # 会打断镜像跟随 / 猜拳的操作


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


# ---------------------------------------------------------------- 真机服务线程
class HandService:
    """唯一访问真机的线程：执行网页指令、播放动画、周期读取状态。"""

    def __init__(self, hand: L10Hand) -> None:
        self.hand = hand
        self.commands: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="l10-web-service", daemon=True)
        self.log: deque[dict] = deque(maxlen=80)
        self.tare = dict.fromkeys(cfg.FINGERS, 0.0)
        self.target: tuple[int, ...] = ()
        self.measured: tuple[int, ...] = ()
        self.dirty = False
        self.stages: list[tuple[int, ...]] = []  # 分段过渡中剩余的阶段（含当前段）
        self.stage_index = 0
        self.stage_check: ArrivalCheck | None = None
        self.motion_relieve = False  # 当前运动结束后是否做按力卸载（只用于手势预设）
        self.force_relief: dict | None = None  # 进行中的按力卸载
        self.anim: dict | None = None
        self.speed = list(cfg.MOVE_SPEED)
        self.device: dict = {}
        self.source = ""
        self.state: dict = {"connected": False, "error": None}
        # 摄像头镜像 / 猜拳
        self.camera = CameraService(on_log=self._log)
        self.guard = MirrorGuard()
        self.following = False
        self.game: RpsGame | None = None
        self.game_overlay = ("", "")
        self.game_poses = {tuple(int(v) for v in pose) for pose in cfg.GAME_POSES.values()}
        self.fresh_forces: dict | None = None  # 本循环新读到的指尖力，交给跟随保护
        self.mode_speed: tuple[int, ...] | None = None  # 跟随 / 游戏 / 握手时临时用的速度
        self.handshake: HandshakeController | None = None
        self.last_follow_send = 0.0

    # ------------------------------------------------------------ 启动 / 停止
    def prepare(self) -> None:
        """在主线程里、服务线程启动前调用：读身份、确定力数据来源、设速度、读当前角度。"""
        info = self.hand.device_info()
        self.device = {
            name: _text(getattr(info, name, None))
            for name in ("model", "serial_number", "firmware_version", "product_code", "sensor_variant", "side")
        }
        self.source = self.hand.detect_force_source()
        self.hand.set_speeds(self.speed)
        self.target = self.hand.get_pose()
        self.measured = self.target
        self._log("info", f"已连接，当前角度 {list(self.target)}，力数据来源 {self.source}")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
        self.camera.stop()

    def submit(self, kind: str, data: dict) -> None:
        self.commands.put((kind, data))

    # ------------------------------------------------------------ 给网页的数据
    def snapshot(self) -> dict:
        with self.lock:
            snap = dict(self.state)
            snap["log"] = list(self.log)
        return snap

    def catalog(self) -> dict:
        data = actions.catalog()
        data["gui_poses"] = [
            {"key": f"gui:{key}", "label": cfg.POSE_LABELS.get(key, key), "pose": list(pose)}
            for key, pose in cfg.POSES.items()
        ]
        data["joints"] = [{"name": n, "label": l} for n, l in zip(cfg.JOINT_NAMES, cfg.JOINT_LABELS, strict=True)]
        data["fingers"] = [{"name": f, "label": cfg.FINGER_LABELS[f]} for f in cfg.FINGERS]
        data["side"] = self.hand.side
        data["display_max"] = cfg.DEMO_DISPLAY_MAX
        data["force_hard_limit"] = cfg.FORCE_HARD_LIMIT
        data["min_anim_interval_ms"] = MIN_ANIM_INTERVAL_MS
        data["version"] = cfg.APP_VERSION
        data["calibrations"] = [{"kind": k, "label": v["label"], "key": v["key"]} for k, v in CALIBRATIONS.items()]
        data["mirror_model"] = MODEL_PATH.exists()
        data["camera_index"] = cfg.MIRROR_CAMERA_INDEX
        data["handshake"] = {"grip_force": cfg.HANDSHAKE_GRIP_FORCE, "force_limit": cfg.HANDSHAKE_FORCE_LIMIT}
        data["deadband"] = cfg.MIRROR_DEADBAND
        data["deadband_max"] = cfg.MIRROR_DEADBAND_MAX
        return data

    def _log(self, level: str, text: str) -> None:
        entry = {"t": time.strftime("%H:%M:%S"), "level": level, "text": text}
        with self.lock:
            self.log.appendleft(entry)
        print(f"[{entry['t']}] {text}")

    # ------------------------------------------------------------ 主循环
    def _run(self) -> None:
        loops = 0
        failures = 0  # 连续的角度 / 指令通信失败次数
        last_slow = 0.0
        last_loop = time.monotonic()
        temps: list | None = None
        faults: list[str] = []
        forces: dict = dict.fromkeys(cfg.FINGERS)
        matrices = None
        force_ok_at = 0.0
        misses = {"角度/指令": 0, "指尖点阵": 0, "温度/故障": 0}  # 本统计窗口内的偶发超时
        last_report = time.monotonic()
        while not self.stop_event.is_set():
            start = time.monotonic()
            loops += 1
            self._drain_commands()
            try:
                # ---- 核心：发目标、读角度。连续失败 10 次才算断开
                try:
                    self._step_animation(start)
                    self._step_mirror(start)
                    if self.dirty:
                        self.dirty = False
                        self.hand.set_pose(self.target)
                    self.measured = self.hand.get_pose()
                    self._update_motion(time.monotonic())
                except HandError as error:
                    failures += 1
                    misses["角度/指令"] += 1
                    if failures == CORE_FAIL_LIMIT:
                        self._log("error", f"通信中断（连续 {failures} 次失败）：{type(error).__name__}: {error}")
                        if self.anim is not None:
                            self.anim = None
                            self._log("warning", "通信中断，已停止动画")
                    with self.lock:
                        self.state["connected"] = failures < CORE_FAIL_LIMIT
                        self.state["error"] = f"{type(error).__name__}: {error}"
                        self.state["animation"] = self._anim_status()
                    if failures >= CORE_FAIL_LIMIT:
                        time.sleep(0.5)
                    continue
                if failures >= CORE_FAIL_LIMIT:
                    self._log("info", "通信恢复")
                failures = 0

                # ---- 触觉点阵：每 FORCE_EVERY 个循环读一次；偶发超时就沿用上一帧
                if loops % FORCE_EVERY == 0:
                    try:
                        raw, new_matrices = self.hand.read_force_detail()
                        forces = {
                            f: None if raw.get(f) is None else max(0.0, raw[f] - self.tare[f])
                            for f in cfg.FINGERS
                        }
                        matrices = new_matrices
                        force_ok_at = time.monotonic()
                        self.fresh_forces = forces
                        self._update_force_relief(force_ok_at, forces)
                    except HandError:
                        misses["指尖点阵"] += 1

                # ---- 温度 / 故障：每秒一次；偶发超时下一秒再读
                if start - last_slow >= SLOW_READ_S:
                    last_slow = start
                    try:
                        temps = list(self.hand.temperatures())
                        faults = self.hand.faults()
                    except HandError:
                        misses["温度/故障"] += 1

                # ---- 超时汇总：最多每 MISS_REPORT_S 秒一条，且只在某类超时较频繁时提示
                if start - last_report >= MISS_REPORT_S:
                    parts = [f"{name} {count} 次" for name, count in misses.items() if count >= MISS_REPORT_MIN]
                    if parts:
                        self._log(
                            "warning",
                            f"最近 {start - last_report:.0f} 秒读取超时较多：{'，'.join(parts)}"
                            "（已沿用上一帧数据；持续出现请检查 CAN 连线，并确认 can_bridge 已关闭）",
                        )
                    misses = dict.fromkeys(misses, 0)
                    last_report = start

                now = time.monotonic()
                hz = 1.0 / max(1e-3, now - last_loop)
                last_loop = now
                force_age = (now - force_ok_at) if force_ok_at else None
                with self.lock:
                    self.state = {
                        "connected": True,
                        "error": None,
                        "device": self.device,
                        "force_source": self.source,
                        "target": list(self.target),
                        "measured": list(self.measured),
                        "speed": self.speed,
                        "forces": forces,
                        "matrices": matrices,
                        "force_stale": force_age is None or force_age > FORCE_STALE_S,
                        "temps": temps,
                        "faults": faults,
                        "animation": self._anim_status(),
                        "transition": (
                            {"stage": self.stage_index + 1, "stages": len(self.stages)}
                            if len(self.stages) > 1
                            else None
                        ),
                        "relief": self.force_relief is not None,
                        "loop_hz": round(hz, 1),
                        "mirror": self._mirror_status(),
                        "handshake": self.handshake.status() if self.handshake is not None else None,
                    }
            except Exception as error:  # 兜底：记录后继续，避免服务线程静默退出
                self._log("error", f"内部错误：{type(error).__name__}: {error}")
                time.sleep(0.5)
            finally:
                time.sleep(max(0.0, LOOP_DT_S - (time.monotonic() - start)))

    # ------------------------------------------------------------ 指令
    def _drain_commands(self) -> None:
        while True:
            try:
                kind, data = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle(kind, data)
            except (ValueError, KeyError, TypeError) as error:
                self._log("warning", f"指令 {kind} 无效：{error}")
            except HandError as error:
                self._log("error", f"指令 {kind} 执行失败：{type(error).__name__}: {error}")

    def _set_target(self, pose: tuple[int, ...]) -> None:
        self.target = check_pose(pose)
        self.dirty = True

    def _stop_anim(self, reason: str | None = None) -> None:
        if self.anim is not None:
            if reason:
                self._log("info", f"动画「{self.anim['label']}」{reason}")
            self.anim = None

    # ------------------------------------------------------------ 分段过渡
    def _start_motion(self, target: tuple[int, ...], staged: bool, relieve: bool = False) -> None:
        """移动到 target 并跟踪到位情况：staged 时分段防干涉；结束时按需堵转卸力，relieve 时再按指尖力卸载。"""
        self.motion_relieve = relieve
        self.force_relief = None
        stages = plan_transition(self.measured or self.target, target) if staged else [check_pose(target)]
        self._set_target(stages[0])
        self.stages, self.stage_index = stages, 0
        self.stage_check = ArrivalCheck(stages[0], time.monotonic())
        if len(stages) > 1:
            self._log("info", f"分 {len(stages)} 段过渡，避免拇指与四指相撞")

    def _cancel_motion(self) -> None:
        """结束分段跟踪，并停止进行中的按力卸载（手动操作、停止、新动作时调用）。"""
        self.stages, self.stage_index, self.stage_check = [], 0, None
        self.force_relief = None

    def _update_motion(self, now: float) -> None:
        """每次读到实测角度后调用：当前段到位（或停住 / 超时）就发下一段；最后一段结束时卸力。"""
        if not self.stages or self.stage_check is None:
            return
        if not self.stage_check.update(self.measured, now):
            return
        last_stage = self.stage_index == len(self.stages) - 1
        if not last_stage and not self.stage_check.reached:
            self._log(
                "warning",
                f"过渡第 {self.stage_index + 1}/{len(self.stages)} 段未完全到位"
                f"（{self.stage_check.describe_gap()}），继续下一段",
            )
        if last_stage:
            final, outcome = self.stages[-1], self.stage_check.outcome
            if outcome == "timeout":
                self._log("warning", f"动作超时仍未到位：{self.stage_check.describe_gap()}")
            self._cancel_motion()
            if self.anim is not None and self.anim["staged"]:
                # 分段动画：每个手势到位后完整停留一个间隔再做下一个（分段过渡常常比间隔还长，
                # 否则刚摆好就被切走，例如正计时 5→6 时 6 几乎看不到）
                self.anim["next"] = max(self.anim["next"], now + self.anim["interval"])
            if cfg.STALL_RELIEF and outcome == "stalled":
                relieved, changes = relieve_stall(final, self.measured)
                if changes:
                    self._set_target(relieved)
                    self._log("info", f"卸力：{describe_relief(changes)}")
            if self.motion_relieve and cfg.FORCE_RELIEF:
                # 从“堵转卸力之后的目标”开始往回退；若以预设原目标为基准，
                # 堵转卸力已把拇指改到 75 时，“最多退 60”的上限（0+60）会让按力卸载一步也退不了
                self.force_relief = {
                    "base": tuple(self.target),
                    "until": now + cfg.FORCE_RELIEF_WINDOW_S,
                    "first": {},
                    "last": {},
                }
            return
        self.stage_index += 1
        pose = self.stages[self.stage_index]
        self._set_target(pose)
        self.stage_check = ArrivalCheck(pose, now)

    def _update_force_relief(self, now: float, forces: dict) -> None:
        """每读到一次新的指尖力调用：超限的手指退一步；不再超限、退到上限或超时就结束并汇总一条日志。"""
        relief = self.force_relief
        if relief is None:
            return
        pose, changes = force_relief_step(self.target, relief["base"], forces)
        for finger, joint, old, new, value in changes:
            relief["first"].setdefault(finger, (value, old, joint))
            relief["last"][finger] = new
        if changes:
            self._set_target(pose)
        if changes and now < relief["until"]:
            return
        if relief["first"]:
            parts = []
            for finger, (force0, old, joint) in relief["first"].items():
                force1 = forces.get(finger)
                now_text = "--" if force1 is None else f"{force1:.0f}"
                parts.append(
                    f"{cfg.FINGER_LABELS[finger]}力 {force0:.0f} → {now_text}"
                    f"（{cfg.JOINT_LABELS[joint]} {old} → {relief['last'][finger]}）"
                )
            self._log("info", "压力卸载：" + "；".join(parts))
        self.force_relief = None

    def _handle(self, kind: str, data: dict) -> None:
        if kind == "stop" and self.handshake is not None and self.handshake.gripping:
            # 握着人手时按停止：松开回到准备姿态，而不是保持握着
            self._stop_mirror("已停止，松开")
            self._stop_anim()
            self.goto(ready_pose())
            return
        if kind in MANUAL_COMMANDS:
            self._stop_mirror("已停止" if kind == "stop" else "已被手动操作打断")
        if kind == "joint":
            index, value = int(data["index"]), int(data["value"])
            if not 0 <= index < len(cfg.JOINT_NAMES):
                raise ValueError(f"关节号 {index} 超出范围")
            self._stop_anim("已被手动操作打断")
            self._cancel_motion()
            pose = list(self.target)
            pose[index] = value
            self._set_target(tuple(pose))
        elif kind == "pose":
            self._stop_anim("已被手动操作打断")
            self._cancel_motion()
            self._set_target(tuple(int(v) for v in data["values"]))
        elif kind == "preset":
            key = str(data["key"])
            self._stop_anim("已被预设动作打断")
            if key.startswith("gui:"):
                name = key[4:]
                pose, label = cfg.POSES[name], cfg.POSE_LABELS.get(name, name)
            else:
                pose = actions.preset_pose(key, self.target)
                label = next(p[1] for p in actions.PRESET_TABLE if p[0] == key)
            pose = check_pose(pose)
            self._start_motion(pose, staged=cfg.SAFE_TRANSITION, relieve=True)
            self._log("info", f"预设：{label} → {list(pose)}")
        elif kind == "speed":
            value = data["value"]
            speeds = check_speeds(int(value) if isinstance(value, (int, float)) else [int(v) for v in value])
            self.hand.set_speeds(speeds)
            self.speed = list(speeds)
            self._log("info", f"速度设为 {self.speed[0] if len(set(speeds)) == 1 else self.speed}")
        elif kind == "animation":
            key = str(data["key"])
            spec = actions.ANIMATION_TABLE[key]
            interval_ms = int(data.get("interval_ms") or spec["interval_ms"])
            interval_ms = max(MIN_ANIM_INTERVAL_MS, interval_ms)
            cycles = max(0, int(data.get("cycles", 1)))
            self._stop_anim("已被新动画替换")
            self.anim = {
                "key": key,
                "label": spec["label"],
                "steps": actions.animation_steps(key),  # 含 USER_OVERRIDES 的真机调整值
                "staged": bool(spec.get("staged")) and cfg.SAFE_TRANSITION,
                "interval": interval_ms / 1000.0,
                "interval_ms": interval_ms,
                "cycles": cycles,
                "index": 0,
                "cycle": 0,
                "next": time.monotonic(),
            }
            loops = "无限循环" if cycles == 0 else f"{cycles} 轮"
            self._log("info", f"开始动画「{spec['label']}」，间隔 {interval_ms} ms，{loops}")
        elif kind == "stop":
            self._stop_anim("已停止")
            self._cancel_motion()
            self._set_target(self.measured or self.target)
            self._log("info", f"停止并保持：{list(self.target)}")
        elif kind == "tare":
            self._tare()
        elif kind == "camera":
            if data.get("on"):
                index = int(data.get("index", cfg.MIRROR_CAMERA_INDEX))
                if not 0 <= index <= 9:
                    raise ValueError(f"摄像头编号 {index} 超出范围")
                self._log("info", f"正在开启摄像头 {index}…")
                self.camera.start(index)
            else:
                self._stop_mirror("已停止：摄像头关闭")
                self.camera.stop()
        elif kind == "follow":
            if data.get("on"):
                if not self.camera.running:
                    raise ValueError("请先开启摄像头")
                self._stop_anim("已被镜像跟随打断")
                self._cancel_motion()
                self.game, self.game_overlay = None, ("", "")
                self._mode_speed(cfg.MIRROR_SPEED)
                self.guard.reset()
                self.fresh_forces = None
                self.following = True
                self._log("info", f"开始镜像跟随（速度 {cfg.MIRROR_SPEED[0]}）")
            else:
                self._stop_mirror("已暂停")
        elif kind == "calibrate":
            self.camera.calibrate(str(data["pose"]))
        elif kind == "calibration_reset":
            self.camera.reset_calibration()
        elif kind == "deadband":
            value = self.camera.set_deadband(float(data["value"]))
            self._log("info", f"防抖回差设为 {value:g}（写进 l10_config.MIRROR_DEADBAND 才会保存）")
        elif kind == "game":
            action = str(data["action"])
            if action == "enter":
                if not self.camera.running:
                    raise ValueError("请先开启摄像头")
                self._stop_anim("已被游戏打断")
                self._cancel_motion()
                self.following = False
                self._mode_speed(cfg.MIRROR_SPEED)
                self.game, self.game_overlay = RpsGame(), ("", "按空格或“开一局”开始")
                self.goto(cfg.GAME_READY)
                self._log("info", "进入游戏：石头剪刀布")
            elif action == "start":
                if self.game is None:
                    raise ValueError("请先进入游戏")
                if self.game.state in ("idle", "result"):
                    self.game.start(time.monotonic())
            elif action == "exit":
                if self.game is not None:
                    self._stop_mirror("已退出")
                    self.goto(cfg.POSES["open"])
            else:
                raise ValueError(f"未知游戏操作 {action}")
        elif kind == "handshake":
            action = str(data["action"])
            if action == "enter":
                if self.handshake is not None:
                    return
                self._stop_mirror("已被握手打断")
                self._stop_anim("已被握手打断")
                self._cancel_motion()
                self._mode_speed(cfg.HANDSHAKE_SPEED)
                self.handshake = HandshakeController(self, time.monotonic())
                self._log("info", "进入握手：手先摆到准备姿态并记录指尖零点，这时别碰指尖")
            elif action == "exit":
                if self.handshake is not None:
                    self._stop_mirror("已退出")
                    self.goto(cfg.POSES["open"])
            elif action in ("grip", "release"):
                if self.handshake is None:
                    raise ValueError("请先进入握手")
                self.handshake.request(action)
            elif action == "config":
                if self.handshake is None:
                    raise ValueError("请先进入握手")
                if "grip_force" in data:
                    value = self.handshake.set_grip_force(float(data["grip_force"]))
                    self._log("info", f"握力设为 {value:g}（写进 l10_config.HANDSHAKE_GRIP_FORCE 才会保存）")
                if "auto_start" in data:
                    self.handshake.auto_start = bool(data["auto_start"])
                if "auto_release" in data:
                    self.handshake.auto_release = bool(data["auto_release"])
                if "use_thumb" in data:
                    self.handshake.use_thumb = bool(data["use_thumb"])
                    self._log("info", "拇指一起握" if self.handshake.use_thumb else "拇指不参与握手（停在准备姿态），下一次握时生效")
            else:
                raise ValueError(f"未知握手操作 {action}")
        else:
            raise ValueError(f"未知指令 {kind}")

    # ------------------------------------------------------------ 镜像跟随 / 猜拳
    def goto(self, pose: tuple[int, ...]) -> None:
        """猜拳用：分段过渡到 pose；出拳姿态到位后和手势预设一样按指尖力卸载。"""
        pose = check_pose(pose)
        self._start_motion(pose, staged=cfg.SAFE_TRANSITION, relieve=pose in self.game_poses)

    def set_target(self, pose: tuple[int, ...]) -> None:
        """握手合拢时用：直接把目标设为 pose（小步，不分段）。"""
        self._cancel_motion()
        self._set_target(pose)

    def motion_busy(self) -> bool:
        return bool(self.stages)

    def _mode_speed(self, speeds: tuple[int, ...] | None) -> None:
        """跟随 / 游戏 / 握手时临时换速度；传 None 恢复网页上设置的速度。"""
        if speeds == self.mode_speed:
            return
        self.hand.set_speeds(check_speeds(speeds if speeds is not None else self.speed))
        self.mode_speed = speeds

    def _stop_mirror(self, reason: str | None = None) -> None:
        """结束镜像跟随 / 游戏 / 握手（哪个在进行就结束哪个），恢复速度。"""
        if not self.following and self.game is None and self.handshake is None:
            return
        what = "游戏" if self.game is not None else "握手" if self.handshake is not None else "镜像跟随"
        self.following, self.game, self.game_overlay, self.handshake = False, None, ("", ""), None
        if reason:
            self._log("info", f"{what}{reason}")
        self._mode_speed(None)

    def _step_mirror(self, now: float) -> None:
        """每个循环调用：握手 / 猜拳推进流程；跟随时把人手映射值经 MirrorGuard 加工后设为目标。"""
        if self.handshake is not None:
            if self.measured:
                self.handshake.update(now, self.fresh_forces, self.measured)
            self.fresh_forces = None
            for text in self.handshake.take_events():
                self._log("info", text)
            return
        if (self.following or self.game is not None) and not self.camera.running:
            self._stop_mirror("已停止：摄像头已关闭")
            return
        if self.game is not None:
            self.game_overlay = self.game.update(now, self.camera.fresh_gesture(now), self)
            return
        if not self.following or not self.measured:
            return
        latest = self.camera.latest()
        mapped = latest["mapped"]
        if mapped is None:
            return
        # 没看到手时 mapped 停在最后一次的值：照常交给保护逻辑（卸力继续有效），手保持不动
        cmd = self.guard.step(
            mapped, self.measured, now, self.fresh_forces, pinch=latest["pinch"], pinch_finger=latest["pinch_finger"]
        )
        self.fresh_forces = None
        for text in self.guard.take_events():
            self._log("info", text)
        changed = not self.target or max(abs(a - b) for a, b in zip(cmd, self.target, strict=True)) >= cfg.MIRROR_SEND_MIN
        if changed or now - self.last_follow_send > FOLLOW_RESEND_S:
            self._set_target(cmd)
            self.last_follow_send = now

    def _mirror_status(self) -> dict:
        game = None
        if self.game is not None:
            big, small = self.game_overlay
            game = {"state": self.game.state, "big": big, "small": small, "score": dict(self.game.score)}
        phase = self.guard.phase if self.following else None
        return {
            "camera": self.camera.status(),
            "following": self.following,
            "phase_text": PHASE_TEXT.get(phase or "", ""),
            "pinch_mode": self.following and self.guard.pinch_mode,
            "pinch_label": PINCH_LABELS.get(self.guard.pinch_finger, "") if self.following else "",
            "floors": [cfg.JOINT_LABELS[j] for j in sorted(self.guard.floors)] if self.following else [],
            "game": game,
        }

    def _tare(self) -> None:
        sums = dict.fromkeys(cfg.FINGERS, 0.0)
        counts = dict.fromkeys(cfg.FINGERS, 0)
        for _ in range(cfg.DEMO_TARE_SAMPLES):
            raw, _ = self.hand.read_force_detail(timeout=cfg.TIMEOUT_S)
            for finger, value in raw.items():
                if finger in sums and value is not None:
                    sums[finger] += value
                    counts[finger] += 1
            time.sleep(0.02)
        self.tare = {f: (sums[f] / counts[f] if counts[f] else 0.0) for f in cfg.FINGERS}
        self._log("info", "去皮完成：" + "  ".join(f"{cfg.FINGER_LABELS[f]} {v:.1f}" for f, v in self.tare.items()))

    # ------------------------------------------------------------ 动画
    def _step_animation(self, now: float) -> None:
        anim = self.anim
        if anim is None or now < anim["next"] or self.stages:  # 分段过渡未完成时先不走下一步
            return
        fingers, palm = anim["steps"][anim["index"]]
        merged = actions.merge(self.target, fingers, palm)
        if anim["staged"]:
            self._start_motion(merged, staged=True)
        else:
            self._cancel_motion()
            self._set_target(merged)
        anim["index"] += 1
        if anim["index"] >= len(anim["steps"]):
            anim["index"] = 0
            anim["cycle"] += 1
            if anim["cycles"] and anim["cycle"] >= anim["cycles"]:
                self._log("info", f"动画「{anim['label']}」播放完毕")
                self.anim = None
                return
        anim["next"] = max(now, anim["next"]) + anim["interval"]

    def _anim_status(self) -> dict | None:
        anim = self.anim
        if anim is None:
            return None
        return {
            "key": anim["key"],
            "label": anim["label"],
            "cycle": anim["cycle"] + 1,
            "cycles": anim["cycles"],
            "step": anim["index"],
            "steps": len(anim["steps"]),
            "interval_ms": anim["interval_ms"],
        }


# ---------------------------------------------------------------- HTTP
def make_handler(service: HandService, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "L10Web/1.0"

        def log_message(self, fmt: str, *args: object) -> None:  # 不刷屏
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: object, status: int = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            # 防 DNS 重绑定：只接受用本机地址访问
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                return self._json(service.snapshot())
            if path == "/api/catalog":
                return self._json(service.catalog())
            if path == "/api/camera.mjpg":
                return self._stream_camera()
            entry = STATIC_FILES.get(path) or MODEL_FILES.get(path)
            if entry is None or not (WEB_DIR / entry[0]).is_file():
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            name, content_type = entry
            self._send(HTTPStatus.OK, (WEB_DIR / name).read_bytes(), content_type)

        def _stream_camera(self) -> None:
            """MJPEG：一直推送最新画面，直到摄像头关闭或浏览器断开。"""
            camera = service.camera
            if not camera.running:
                return self._json({"error": "camera off"}, HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = -1
            try:
                while camera.running and not service.stop_event.is_set():
                    seq, jpeg = camera.wait_frame(seq, timeout=1.0)
                    if jpeg is None:
                        continue
                    head = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
                    self.wfile.write(head.encode("ascii") + jpeg + b"\r\n")
                    self.wfile.flush()
            except OSError:  # 浏览器关闭或刷新页面
                pass
            self.close_connection = True

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
            origin = self.headers.get("Origin")
            if origin is not None and origin.removeprefix("http://") not in allowed_hosts:
                return self._json({"error": "cross-site request rejected"}, HTTPStatus.FORBIDDEN)
            # 要求 JSON：其他网页无法在不经过 CORS 预检的情况下发出这种请求
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json({"error": "Content-Type must be application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            if self.path != "/api/command":
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self._json({"error": "body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                kind = str(payload.pop("kind"))
            except (ValueError, KeyError, AttributeError):
                return self._json({"error": "bad request"}, HTTPStatus.BAD_REQUEST)
            service.submit(kind, payload)
            self._json({"ok": True})

    return Handler


# ---------------------------------------------------------------- 入口
FROZEN = getattr(sys, "frozen", False)  # 是否是打包后的 exe


def self_test() -> int:
    """检查程序文件是否完整（网页、3D 模型、手部识别模型、PCAN 接口）。不连接手，不开摄像头。"""
    import numpy as np

    from l10_mirror import HandTracker

    problems = []
    for name, _ctype in {**STATIC_FILES, **MODEL_FILES}.values():
        if not (WEB_DIR / name).is_file():
            problems.append(f"缺少网页文件 {name}")
    try:
        tracker = HandTracker(MODEL_PATH)
        tracker.detect(np.zeros((480, 640, 3), np.uint8), 0)
        tracker.close()
        print("  手部识别模型：正常")
    except Exception as error:  # 自检要把所有问题都报出来
        problems.append(f"手部识别模型加载失败：{error}")
    try:
        import can.interfaces.pcan.pcan  # noqa: F401  只导入接口，不打开 PCAN

        print("  PCAN 接口：正常")
    except Exception as error:
        problems.append(f"PCAN 接口导入失败：{error}")
    try:
        CameraService(on_log=lambda level, text: None)
        print("  摄像头服务：正常（未打开摄像头）")
    except Exception as error:
        problems.append(f"摄像头服务创建失败：{error}")
    print(f"  网页文件：{'正常' if not any('网页' in p for p in problems) else '有缺失'}")
    if problems:
        print("自检失败：\n  " + "\n  ".join(problems))
        return 1
    print(f"自检通过（L10 网页控制台 v{cfg.APP_VERSION}）")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="L10 网页控制台（基于 linkerbot-hand SDK）")
    add_connection_args(parser)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--self-test", action="store_true", help="只检查程序文件是否完整后退出（不连手、不开摄像头）")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(self_test())
    print(f"L10 网页控制台 v{cfg.APP_VERSION}")
    try:
        run(args)
    finally:
        if FROZEN:  # 双击 exe 运行时，出错也让窗口停住，方便看提示
            try:
                input("按回车键关闭窗口…")
            except (EOFError, KeyboardInterrupt):
                pass


def run(args: argparse.Namespace) -> None:
    hand = L10Hand.from_args(args)
    service = HandService(hand)
    server: ThreadingHTTPServer | None = None
    try:
        hand.open()
        service.prepare()
        service.start()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(service, args.port))
        url = f"http://127.0.0.1:{args.port}"
        print(f"网页控制台已启动：{url}   （Ctrl+C 退出，手会保持当前姿态）")
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在退出…")
    except (HandError, RuntimeError, OSError) as error:
        print(f"\n发生错误：{error}")
    finally:
        if server is not None:
            server.server_close()
        service.stop()
        if hand.is_open:
            try:
                hand.close()
            except HandError as error:
                print(f"断开连接失败：{error}")
        print("程序结束")


if __name__ == "__main__":
    main()
