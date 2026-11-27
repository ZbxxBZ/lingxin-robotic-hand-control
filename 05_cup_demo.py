"""拿杯子演示：L10 抓纸杯，同时在窗口里实时显示指尖力。

窗口内容：
  - 五指力条（去皮后），带目标力线和触发阈值线
  - 最近 10 秒的力曲线（五指 + 接触平均力）
  - 大字状态：等待杯子 / 准备抓取 / 正在合手 / 握持中 / 松手
  - 如果是触觉点阵版本，再显示五个指尖的 12×6 热力图

操作（鼠标先点一下窗口，让窗口获得键盘焦点）：
  用杯子轻碰任一指尖，或按空格 → 稍等片刻后合手抓杯
  握住后把杯子往外拽，或按 R   → 松手
  Z                           → 重新去皮（手张开、指尖没接触时按）
  Q 或关闭窗口                  → 松手并退出

流程和 03_smart_grasp.py 一样：预成形 → 弯曲关节逐步闭合，每步读力，
达到目标力就停在当前位置。参数在 l10_config.py 的 OBJECTS["cup"] 和
“拿杯子演示”一节。

运行前需要：pip install matplotlib
    python 05_cup_demo.py
    python 05_cup_demo.py --log cup_run.csv    # 同时记录力值，便于调阈值
"""

from __future__ import annotations

import argparse
import csv
import threading
import time
from collections import deque
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from linkerbot_hand import HandError
from matplotlib.animation import FuncAnimation

import l10_config as cfg
from l10_hand import (
    L10Hand,
    add_connection_args,
    check_pose,
    contact_force,
    lerp_pose,
    peak_force,
    preshape_of,
)

WAIT, ARMED, CLOSING, HOLD, RELEASING, ERROR = (
    "wait",
    "armed",
    "closing",
    "hold",
    "releasing",
    "error",
)
STATE_TEXT = {
    WAIT: "等待杯子",
    ARMED: "准备抓取…",
    CLOSING: "正在合手",
    HOLD: "握持中",
    RELEASING: "松手",
    ERROR: "出错，已停止运动",
}
STATE_COLOR = {
    WAIT: "#2563eb",
    ARMED: "#d97706",
    CLOSING: "#ea580c",
    HOLD: "#16a34a",
    RELEASING: "#6b7280",
    ERROR: "#dc2626",
}
KEY_HINT = "空格 抓取    R 松手    Z 去皮    Q 退出"


# ---------------------------------------------------------------- 线程间共享数据
class Shared:
    """控制线程写、界面线程读；界面的按键请求反向传给控制线程。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.quit = threading.Event()
        self.state = WAIT
        self.detail = "用杯子轻碰指尖，或按空格"
        self.forces: dict[str, float | None] = dict.fromkeys(cfg.FINGERS, 0.0)
        self.matrices: dict[str, tuple[tuple[float, ...], ...]] | None = None
        self.contact = 0.0
        self.history: deque[tuple[float, ...]] = deque(
            maxlen=int(cfg.DEMO_HISTORY_S / cfg.STEP_DT_S) + 10
        )
        self._requests: set[str] = set()

    def request(self, kind: str) -> None:
        with self.lock:
            self._requests.add(kind)

    def take_requests(self) -> set[str]:
        with self.lock:
            requests, self._requests = self._requests, set()
        return requests

    def publish(self, **values: object) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "state": self.state,
                "detail": self.detail,
                "forces": dict(self.forces),
                "matrices": self.matrices,
                "contact": self.contact,
                "history": list(self.history),
            }


# ---------------------------------------------------------------- 控制线程
class CupDemo:
    """抓杯子的状态机。只有这个线程在演示过程中访问真机。"""

    def __init__(self, hand: L10Hand, shared: Shared, log_path: str | None) -> None:
        params = cfg.OBJECTS[cfg.DEMO_OBJECT]
        self.hand = hand
        self.shared = shared
        self.log_path = log_path
        self.label = params["label"]
        self.target = check_pose(params["target"], name=f"{cfg.DEMO_OBJECT}.target")
        self.preshape = preshape_of(self.target)
        self.target_force = float(params["target_force"])
        self.fingers = tuple(params["monitor_fingers"])
        self.steps = max(1, round(params["close_time_s"] / cfg.STEP_DT_S))
        self.tare = dict.fromkeys(cfg.FINGERS, 0.0)

    # ------------------------------------------------------------ 传感器
    def tare_sensors(self) -> None:
        """手张开、指尖不接触时调用：记录各指读数作为零点。"""
        sums = dict.fromkeys(cfg.FINGERS, 0.0)
        counts = dict.fromkeys(cfg.FINGERS, 0)
        for _ in range(cfg.DEMO_TARE_SAMPLES):
            forces, _ = self.hand.read_force_detail(timeout=cfg.TIMEOUT_S)
            for finger, value in forces.items():
                if value is not None:
                    sums[finger] += value
                    counts[finger] += 1
            time.sleep(0.02)
        self.tare = {
            f: (sums[f] / counts[f] if counts[f] else 0.0) for f in cfg.FINGERS
        }
        print("去皮零点：" + "  ".join(f"{cfg.FINGER_LABELS[f]} {v:.1f}" for f, v in self.tare.items()))

    def read(self) -> tuple[dict[str, float | None], dict | None]:
        raw, matrices = self.hand.read_force_detail()
        forces = {
            f: None if raw.get(f) is None else max(0.0, raw[f] - self.tare[f])
            for f in cfg.FINGERS
        }
        return forces, matrices

    # ------------------------------------------------------------ 主循环
    def run(self) -> None:
        log_file = open(self.log_path, "w", newline="", encoding="utf-8") if self.log_path else None
        try:
            writer = csv.writer(log_file) if log_file else None
            if writer:
                writer.writerow(["t", "state", "contact", *cfg.FINGERS])
            self._loop(writer)
        except Exception as error:  # 任何异常都停止运动，并显示在画面上
            print(f"[控制线程] 出错：{type(error).__name__}: {error}")
            self.shared.publish(state=ERROR, detail=f"{type(error).__name__}: {error}")
        finally:
            if log_file:
                log_file.close()

    def _loop(self, writer: Any) -> None:  # writer: csv.writer 对象或 None
        state = WAIT
        t_state = time.monotonic()
        detail = "用杯子轻碰指尖，或按空格"
        step = 0
        trigger_frames = 0
        auto_ready = True  # 自动触发是否已就绪（松手后要等力回落）
        rearm_at = 0.0
        quiet_since: float | None = None
        settle: list[float] = []
        baseline: float | None = None
        tug_frames = 0
        tug_enabled = True
        read_failures = 0
        t0 = time.monotonic()

        def enter(new_state: str, new_detail: str) -> None:
            nonlocal state, t_state, detail
            state, t_state, detail = new_state, time.monotonic(), new_detail
            print(f"[{STATE_TEXT[new_state]}] {new_detail}")

        def start_release(reason: str) -> None:
            self.hand.set_speeds(cfg.MOVE_SPEED)
            self.hand.set_pose(self.preshape)
            enter(RELEASING, reason)

        while not self.shared.quit.is_set():
            loop_start = time.monotonic()
            requests = self.shared.take_requests()

            if "tare" in requests:
                if state == WAIT:
                    self.shared.publish(detail="去皮中，请不要碰手指…")
                    self.tare_sensors()
                    detail = "已去皮。用杯子轻碰指尖，或按空格"
                else:
                    print("去皮只能在“等待杯子”状态下进行")

            # ---- 读力
            try:
                forces, matrices = self.read()
                read_failures = 0
            except HandError as error:
                read_failures += 1
                forces, matrices = None, None
                if read_failures >= 10:
                    raise RuntimeError(f"连续 10 次读不到指尖力：{error}") from error

            now = time.monotonic()
            contact = contact_force(forces, self.fingers) if forces else 0.0

            # ---- 状态机
            if state == WAIT:
                max_force = max((v for v in (forces or {}).values() if v is not None), default=0.0)
                if not auto_ready:
                    # 松手后：冷却时间已过，且指尖力持续回落 0.5 s，才重新允许自动触发
                    if max_force < cfg.DEMO_TRIGGER_FORCE * 0.5:
                        quiet_since = quiet_since or now
                    else:
                        quiet_since = None
                    if now >= rearm_at and quiet_since and now - quiet_since >= 0.5:
                        auto_ready = True
                touching = forces is not None and max_force >= cfg.DEMO_TRIGGER_FORCE
                trigger_frames = trigger_frames + 1 if touching else 0
                auto_hit = (
                    cfg.DEMO_AUTO_GRASP
                    and auto_ready
                    and trigger_frames >= cfg.DEMO_TRIGGER_FRAMES
                )
                if "grasp" in requests or auto_hit:
                    enter(ARMED, "把杯子放进掌心")

            elif state == ARMED:
                if "release" in requests:
                    enter(WAIT, "已取消。用杯子轻碰指尖，或按空格")
                elif now - t_state >= cfg.DEMO_TRIGGER_DELAY_S:
                    self.hand.set_speeds(cfg.GRASP_SPEED)
                    step = 0
                    enter(CLOSING, f"目标力 {self.target_force:.0f}")

            elif state == CLOSING:
                finger, peak = peak_force(forces or {}, self.fingers)
                hold_reason = None
                if "release" in requests:
                    start_release("按 R 松手")
                elif forces is None:
                    hold_reason, tug_enabled = "读力失败，停止合手并保持", False
                elif peak >= cfg.FORCE_HARD_LIMIT:
                    hold_reason = f"{cfg.FINGER_LABELS[finger]}力值超过上限，停止合手"
                elif contact >= self.target_force:
                    hold_reason = f"达到目标力 {self.target_force:.0f}，停在当前位置"
                elif step >= self.steps:
                    hold_reason = "已到闭合终点，未达到目标力（杯子没放好或目标力偏高）"
                else:
                    step += 1
                    self.hand.set_pose(lerp_pose(self.preshape, self.target, step / self.steps))
                if hold_reason:
                    settle, baseline, tug_frames = [], None, 0
                    tug_enabled = tug_enabled and cfg.DEMO_AUTO_RELEASE
                    enter(HOLD, hold_reason + ("。往外拽即松手" if tug_enabled else "。按 R 松手"))

            elif state == HOLD:
                if "release" in requests:
                    start_release("按 R 松手")
                elif tug_enabled and forces is not None:
                    if now - t_state < cfg.DEMO_HOLD_SETTLE_S:
                        settle.append(contact)
                    else:
                        if baseline is None:
                            baseline = sum(settle) / len(settle) if settle else contact
                            print(f"握持稳态力：{baseline:.1f}")
                        limit = max(cfg.DEMO_TUG_ABS, cfg.DEMO_TUG_REL * baseline)
                        tug_frames = tug_frames + 1 if abs(contact - baseline) > limit else 0
                        if tug_frames >= cfg.DEMO_TUG_FRAMES:
                            start_release("检测到拽拉，松手")

            elif state == RELEASING:
                if now - t_state >= cfg.SETTLE_S:
                    auto_ready, quiet_since, trigger_frames = False, None, 0
                    rearm_at = now + cfg.DEMO_REARM_S
                    tug_enabled = True
                    enter(WAIT, "用杯子轻碰指尖，或按空格")

            # ---- 发布给界面 / 写日志
            row_forces = [0.0 if not forces or forces[f] is None else forces[f] for f in cfg.FINGERS]
            with self.shared.lock:
                self.shared.state = state
                self.shared.detail = detail
                if forces is not None:
                    self.shared.forces = forces
                    self.shared.contact = contact
                if matrices is not None:
                    self.shared.matrices = matrices
                self.shared.history.append((now, contact, *row_forces))
            if writer:
                writer.writerow([f"{now - t0:.3f}", state, f"{contact:.2f}", *(f"{v:.2f}" for v in row_forces)])

            time.sleep(max(0.0, cfg.STEP_DT_S - (time.monotonic() - loop_start)))


# ---------------------------------------------------------------- 界面（主线程）
class DemoView:
    def __init__(self, shared: Shared, demo: CupDemo, source: str, with_tactile: bool) -> None:
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        plt.rcParams["keymap.home"] = ["h", "home"]  # 让出 R 键给“松手”
        self.shared = shared
        self.demo = demo
        self.with_tactile = with_tactile
        self.top = float(cfg.DEMO_DISPLAY_MAX)

        rows = 3 if with_tactile else 2
        ratios = [3, 2, 2] if with_tactile else [3, 2]
        self.fig = plt.figure(figsize=(13, 9 if with_tactile else 7))
        if self.fig.canvas.manager is not None:
            self.fig.canvas.manager.set_window_title("L10 拿杯子演示")
        grid = self.fig.add_gridspec(rows, 5, height_ratios=ratios, hspace=0.45, wspace=0.3)

        # 力条
        self.ax_bar = self.fig.add_subplot(grid[0, :3])
        labels = [cfg.FINGER_LABELS[f] for f in cfg.FINGERS]
        self.bars = self.ax_bar.bar(labels, [0.0] * len(labels), color=STATE_COLOR[WAIT])
        self.bar_text = [
            self.ax_bar.text(rect.get_x() + rect.get_width() / 2, 0, "", ha="center", va="bottom", fontsize=13)
            for rect in self.bars
        ]
        self.ax_bar.axhline(demo.target_force, ls="--", color="#16a34a", label=f"目标力 {demo.target_force:.0f}")
        self.ax_bar.axhline(cfg.DEMO_TRIGGER_FORCE, ls=":", color="#d97706", label=f"触发阈值 {cfg.DEMO_TRIGGER_FORCE}")
        self.ax_bar.set_ylim(0, self.top)
        self.ax_bar.set_title(f"指尖力（去皮后，来源 {source}）", fontsize=13)
        self.ax_bar.legend(loc="upper right")

        # 状态面板
        self.ax_status = self.fig.add_subplot(grid[0, 3:])
        self.ax_status.axis("off")
        self.status_title = self.ax_status.text(0.5, 0.78, "", ha="center", va="center", fontsize=30, weight="bold")
        self.status_detail = self.ax_status.text(0.5, 0.55, "", ha="center", va="center", fontsize=12, wrap=True)
        self.status_force = self.ax_status.text(0.5, 0.35, "", ha="center", va="center", fontsize=16)
        self.ax_status.text(0.5, 0.18, f"物体：{demo.label}    监控：{'、'.join(cfg.FINGER_LABELS[f] for f in demo.fingers)}",
                            ha="center", va="center", fontsize=11, color="#4b5563")
        self.ax_status.text(0.5, 0.04, KEY_HINT, ha="center", va="center", fontsize=11, color="#4b5563")

        # 力曲线
        self.ax_hist = self.fig.add_subplot(grid[1, :])
        self.lines = {f: self.ax_hist.plot([], [], lw=1.5, label=cfg.FINGER_LABELS[f])[0] for f in cfg.FINGERS}
        self.contact_line = self.ax_hist.plot([], [], color="black", lw=2.5, label="接触平均力")[0]
        self.ax_hist.axhline(demo.target_force, ls="--", color="#16a34a")
        self.ax_hist.set_xlim(-cfg.DEMO_HISTORY_S, 0)
        self.ax_hist.set_ylim(0, self.top)
        self.ax_hist.set_xlabel("时间 (s)")
        self.ax_hist.legend(loc="upper left", ncol=6, fontsize=9)

        # 触觉点阵热力图
        self.images = {}
        if with_tactile:
            for i, finger in enumerate(cfg.FINGERS):
                ax = self.fig.add_subplot(grid[2, i])
                self.images[finger] = ax.imshow(
                    np.zeros((12, 6)), vmin=0, vmax=self.top, cmap="inferno", interpolation="nearest"
                )
                ax.set_title(cfg.FINGER_LABELS[finger], fontsize=11)
                ax.set_xticks([])
                ax.set_yticks([])

        self.fig.canvas.mpl_connect("key_press_event", self._on_key)
        self.fig.canvas.mpl_connect("close_event", lambda _event: shared.quit.set())
        self.anim = FuncAnimation(self.fig, self._update, interval=66, cache_frame_data=False)

    def _on_key(self, event: object) -> None:
        key = (getattr(event, "key", None) or "").lower()
        if key in (" ", "enter"):
            self.shared.request("grasp")
        elif key == "r":
            self.shared.request("release")
        elif key == "z":
            self.shared.request("tare")
        # Q：matplotlib 默认关闭窗口，触发 close_event

    def _bar_color(self, value: float) -> str:
        if value >= cfg.FORCE_HARD_LIMIT:
            return "#dc2626"
        if value >= self.demo.target_force:
            return "#16a34a"
        if value >= cfg.DEMO_TRIGGER_FORCE:
            return "#f59e0b"
        return "#93c5fd"

    def _update(self, _frame: int) -> list:
        snap = self.shared.snapshot()
        state = snap["state"]
        forces = snap["forces"]

        for rect, text, finger in zip(self.bars, self.bar_text, cfg.FINGERS, strict=True):
            value = forces.get(finger)
            height = 0.0 if value is None else min(value, self.top)
            rect.set_height(height)
            rect.set_color(self._bar_color(0.0 if value is None else value))
            text.set_y(height)
            text.set_text("--" if value is None else f"{value:.0f}")

        self.status_title.set_text(STATE_TEXT[state])
        self.status_title.set_color(STATE_COLOR[state])
        self.status_detail.set_text(snap["detail"])
        self.status_force.set_text(f"接触平均力 {snap['contact']:.0f} / 目标 {self.demo.target_force:.0f}")

        history = snap["history"]
        if history:
            data = np.asarray(history, dtype=float)
            x = data[:, 0] - time.monotonic()
            self.contact_line.set_data(x, data[:, 1])
            for i, finger in enumerate(cfg.FINGERS):
                self.lines[finger].set_data(x, data[:, 2 + i])

        matrices = snap["matrices"]
        if self.images and matrices:
            for finger, image in self.images.items():
                rows = matrices.get(finger)
                if rows:
                    grid = np.asarray(rows, dtype=float)
                    image.set_data(grid)
                    image.set_extent((-0.5, grid.shape[1] - 0.5, grid.shape[0] - 0.5, -0.5))
        return []

    def show(self) -> None:
        plt.show()


# ---------------------------------------------------------------- 入口
def main() -> None:
    parser = argparse.ArgumentParser(description="L10 拿杯子演示（实时指尖力显示）")
    add_connection_args(parser)
    parser.add_argument("--log", default=None, help="把每帧力值写入该 CSV 文件")
    args = parser.parse_args()

    hand = L10Hand.from_args(args)
    shared = Shared()
    worker: threading.Thread | None = None
    try:
        hand.open()
        source = hand.detect_force_source()
        print(f"指尖力数据来源：{source}")
        demo = CupDemo(hand, shared, args.log)

        print("张开并预成形…")
        hand.set_speeds(cfg.MOVE_SPEED)
        hand.move_to("open")
        hand.set_pose(demo.preshape, wait_s=cfg.SETTLE_S)

        print("去皮：请保持指尖不接触任何东西")
        demo.tare_sensors()
        _, matrices = hand.read_force_detail(timeout=cfg.TIMEOUT_S)

        view = DemoView(shared, demo, source, with_tactile=matrices is not None)
        worker = threading.Thread(target=demo.run, name="l10-cup-demo", daemon=True)
        worker.start()
        print("窗口已打开。用杯子轻碰指尖或按空格开始；Q 或关闭窗口退出。")
        view.show()  # 阻塞，直到窗口关闭
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except (HandError, RuntimeError, ValueError) as error:
        print(f"\n发生错误：{error}")
    finally:
        shared.quit.set()
        if worker is not None:
            worker.join(timeout=3)
        if hand.is_open:
            if worker is None or not worker.is_alive():
                try:
                    print("退出前松手…")
                    hand.set_speeds(cfg.MOVE_SPEED)
                    hand.move_to("open")
                except HandError as error:
                    print(f"松手失败：{error}")
            try:
                hand.close()
            except HandError as error:
                print(f"断开连接失败：{error}")
        print("程序结束")


if __name__ == "__main__":
    main()
