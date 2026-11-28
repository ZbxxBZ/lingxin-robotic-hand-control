"""握手（v2）：按触觉合拢、按触觉松手。本模块只做判断，不直接访问真机。

流程（网页“握手”页）：
  准备   手走到准备姿态（握手姿态再张开一些），到位后记录各指尖的零点（这时别碰指尖）
  等待   把手伸进来：任一指尖碰到（力超过 HANDSHAKE_TRIGGER_FORCE），或点“握” / 按空格，就开始
  合拢   五根手指各自从准备姿态往最紧姿态慢慢弯，哪根手指停下由它自己决定：
           - 指尖力到了握力 → 碰到了，停在这里；
           - 某个关节的指令比实测更合拢、却不动了（碰到的是你手的侧面，指尖读不到力）→ 被挡住，
             整根手指停在实测位置，只往里留一点点；
           - 弯到最紧也没碰到 → 到头了。
         手大的人碰得早、停得早，手小的人多弯一些，握力都差不多。
         拇指：等四指里有一根碰到或被挡住（确认手在里面）才动；先把翻滚转到握手姿态的位置
         （搭到你手背上方，不横扫），再往下弯；握力、上限、速度都比四指小。
  握住   你用力捏、某根手指的力超过上限时，它往回让一让（有上限，你放松后慢慢回来）；某个关节又开始往里顶，就退回到实测位置
  松开   每根手指单独判断是否失去接触：碰到指尖的看指尖力掉到它握稳时的一小部分，
         被挡住的（轻轻贴着）看是否往里走了；一半以上失去接触就松开。
         也可以点“松开”；握满 HANDSHAKE_MAX_HOLD_S 自动松开 → 回到准备姿态
  再来   指尖力回落、过了 HANDSHAKE_REARM_S 后，自动回到“等待”

合拢中的手指用“合拢程度” c（0 = 准备姿态，1 = 最紧）算关节值；停下后改为记住具体的关节值。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from . import config as cfg

Pose = tuple[int, ...]

# 每根手指合拢时动哪些关节（拇指：弯曲 + 翻滚）
FINGER_JOINTS = {"thumb": (0, 1), "index": (2,), "middle": (3,), "ring": (4,), "pinky": (5,)}
STATE_TEXT = {
    "ready": "准备中：手在摆准备姿态",
    "taring": "记录指尖零点：先别碰指尖",
    "waiting": "把手伸进来握手（碰到指尖就开始，或点“握” / 按空格）",
    "armed": "碰到了，马上握…",
    "closing": "正在握…",
    "holding": "握住了：把手往外抽就会松开",
    "releasing": "松开中…",
    "rearm": "松开了：把手拿开，稍后可以再握",
}
STOP_TEXT = {"contact": "碰到", "blocked": "挡住", "limit": "到头", "skip": "不参与"}


class Mover(Protocol):
    def goto(self, pose: Pose) -> None: ...  # 分段过渡到 pose（防撞）

    def set_target(self, pose: Pose) -> None: ...  # 直接把目标设为 pose（合拢时的小步）

    def motion_busy(self) -> bool: ...  # 分段过渡是否还在进行


def _clip(v: float) -> int:
    return int(round(min(255.0, max(0.0, v))))


def ready_pose() -> Pose:
    return tuple(_clip(p + o) for p, o in zip(cfg.HANDSHAKE_POSE, cfg.HANDSHAKE_READY_OFFSET, strict=True))


def tight_pose() -> Pose:
    return tuple(_clip(p - o) for p, o in zip(cfg.HANDSHAKE_POSE, cfg.HANDSHAKE_TIGHT_OFFSET, strict=True))


class HandshakeController:
    def __init__(self, mover: Mover, now: float) -> None:
        self.mover = mover
        self.ready = ready_pose()
        self.tight = tight_pose()
        self.grip_force = float(cfg.HANDSHAKE_GRIP_FORCE)
        self.auto_start = True
        self.auto_release = True
        self.use_thumb = bool(cfg.HANDSHAKE_USE_THUMB)
        self.count = 0  # 握手次数
        self.closure = dict.fromkeys(FINGER_JOINTS, 0.0)
        self.stopped: dict[str, str] = {}  # 手指 → "contact" / "blocked" / "limit" / "skip"
        self.held: dict[str, dict[int, int]] = {}  # 停下的手指 → 记住的关节值
        self.stop_at: dict[str, dict[int, int]] = {}  # 被挡住时的实测（判断你抽手）
        self.baseline = dict.fromkeys(FINGER_JOINTS, 0.0)
        self.samples: list[Mapping[str, float | None]] = []
        self.forces: dict[str, float | None] = dict.fromkeys(FINGER_JOINTS)
        self.still: dict[int, tuple[int, float]] = {}  # 关节 → (参考实测, 开始不动的时间)
        self.lagging: dict[str, tuple[tuple[int, ...], float]] = {}  # 合拢时跟不上的手指 → (实测, 开始时间)
        self.hold_force: float | None = None
        self.low_frames = 0
        self.ref: dict[str, float] = {}  # 握稳时每根碰到的手指的指尖力（平均）：判断“力掉下去了”的基准
        self.settle_samples: dict[str, list[float]] = {}
        self.settled = False
        self.give: dict[str, int] = {}  # 你用力捏时各手指临时往回让的量
        self.trigger_frames = 0
        self.last_close = now
        self.events: list[str] = []
        self.requests: list[str] = []
        self._enter("ready", now)
        self.mover.goto(self.ready)

    # ------------------------------------------------------------ 网页操作
    def request(self, action: str) -> None:
        if action not in ("grip", "release"):
            raise ValueError(f"未知握手操作 {action}")
        self.requests.append(action)

    def set_grip_force(self, value: float) -> float:
        self.grip_force = min(float(cfg.HANDSHAKE_FORCE_LIMIT) - 5, max(5.0, float(value)))
        return self.grip_force

    @property
    def gripping(self) -> bool:
        return self.state in ("armed", "closing", "holding")

    def _grip_of(self, finger: str) -> float:
        return self.grip_force * (cfg.HANDSHAKE_THUMB_FORCE_RATIO if finger == "thumb" else 1.0)

    def _limit_of(self, finger: str) -> float:
        return cfg.HANDSHAKE_FORCE_LIMIT * (cfg.HANDSHAKE_THUMB_FORCE_RATIO if finger == "thumb" else 1.0)

    # ------------------------------------------------------------ 每个控制周期调用
    def update(self, now: float, forces: Mapping[str, float | None] | None, measured: Sequence[int]) -> None:
        """forces：本周期新读到的去皮指尖力（没读则 None）；measured：实测 10 关节角度。"""
        fresh = forces is not None
        if forces is not None:
            self.forces = {f: self._net(f, forces.get(f)) for f in FINGER_JOINTS}
        requests, self.requests = self.requests, []
        if "release" in requests and self.state in ("armed", "closing", "holding"):
            self._release(now, "点了“松开”")
        elif "grip" in requests and self.state in ("waiting", "rearm", "armed"):
            self._start_closing(now)

        if self.state == "ready":
            if not self.mover.motion_busy():
                self.samples = []
                self._enter("taring", now)
        elif self.state == "taring":
            if forces is not None:
                self.samples.append(forces)
            if now - self.since >= cfg.HANDSHAKE_TARE_S and self.samples:
                for f in FINGER_JOINTS:
                    values = [v for s in self.samples if (v := s.get(f)) is not None]
                    self.baseline[f] = sum(values) / len(values) if values else 0.0
                self.forces = dict.fromkeys(FINGER_JOINTS, 0.0)
                self.trigger_frames = 0
                self._enter("waiting", now)
        elif self.state == "waiting":
            if fresh and self.auto_start:
                touched = any((v or 0.0) > cfg.HANDSHAKE_TRIGGER_FORCE for v in self.forces.values())
                self.trigger_frames = self.trigger_frames + 1 if touched else 0
                if self.trigger_frames >= cfg.HANDSHAKE_TRIGGER_FRAMES:
                    self._enter("armed", now)
        elif self.state == "armed":
            if now - self.since >= cfg.HANDSHAKE_TRIGGER_DELAY_S:
                self._start_closing(now)
        elif self.state == "closing":
            self._step_closing(now, measured)
        elif self.state == "holding":
            self._step_holding(now, fresh, measured)
        elif self.state == "releasing":
            if not self.mover.motion_busy():
                self._enter("rearm", now)
        elif self.state == "rearm":
            calm = all((v or 0.0) < cfg.HANDSHAKE_TRIGGER_FORCE for v in self.forces.values())
            if calm and now - self.since >= cfg.HANDSHAKE_REARM_S:
                self.trigger_frames = 0
                self._enter("waiting", now)

    # ------------------------------------------------------------ 关节值
    def _net(self, finger: str, value: float | None) -> float | None:
        return None if value is None else max(0.0, value - self.baseline[finger])

    def _enter(self, state: str, now: float) -> None:
        self.state, self.since = state, now

    def _joint_value(self, finger: str, j: int, c: float) -> int:
        """合拢程度 c → 关节值。拇指先转翻滚（c < ROLL_FIRST），再弯曲。"""
        if finger == "thumb":
            first = cfg.HANDSHAKE_THUMB_ROLL_FIRST
            c = min(1.0, c / first) if j == 1 else max(0.0, (c - first) / (1.0 - first))
        return _clip(self.ready[j] + (self.tight[j] - self.ready[j]) * c)

    def _pose(self) -> Pose:
        pose = list(self.ready)
        for finger, joints in FINGER_JOINTS.items():
            held = self.held.get(finger)
            for j in joints:
                pose[j] = held[j] if held is not None else self._joint_value(finger, j, self.closure[finger])
            give = self.give.get(finger, 0)
            if give and held is not None:  # 你用力捏时让一让：主关节往准备姿态方向退 give
                j = joints[0]
                toward = 1 if self.ready[j] > pose[j] else -1 if self.ready[j] < pose[j] else 0
                pose[j] = _clip(pose[j] + toward * give)
        return tuple(pose)

    def _command(self, j: int) -> int:
        return self._pose()[j]

    # ------------------------------------------------------------ 合拢
    def _start_closing(self, now: float) -> None:
        self.closure = dict.fromkeys(FINGER_JOINTS, 0.0)
        self.stopped, self.held, self.stop_at, self.still, self.lagging = {}, {}, {}, {}, {}
        if not self.use_thumb:
            self.stopped["thumb"] = "skip"
            self.held["thumb"] = {j: self.ready[j] for j in FINGER_JOINTS["thumb"]}
        self.hold_force, self.low_frames, self.settled, self.give = None, 0, False, {}
        self.ref, self.settle_samples = {}, {}
        self.last_close = now
        self._enter("closing", now)

    def _pushing_joints(self, finger: str, measured: Sequence[int], now: float) -> list[int]:
        """这根手指里“指令比实测更合拢一大截、却不再动”的关节（被挡住 / 顶住了）。"""
        err = self._lead_of(finger) - 4  # 指令领先已接近上限
        result = []
        for j in FINGER_JOINTS[finger]:
            closing_down = self.tight[j] < self.ready[j]  # 合拢方向：原始值变小
            gap = (measured[j] - self._command(j)) if closing_down else (self._command(j) - measured[j])
            ref, since = self.still.get(j, (measured[j], now))
            if gap <= err or abs(measured[j] - ref) > cfg.HANDSHAKE_BLOCK_MOVE:
                self.still[j] = (measured[j], now)
                continue
            self.still[j] = (ref, since)
            if now - since >= cfg.HANDSHAKE_BLOCK_S:
                result.append(j)
        return result

    def _gap(self, j: int, measured: Sequence[int]) -> int:
        """指令比实测更合拢多少（正数 = 指令在前面）。"""
        cmd = self._command(j)
        return (measured[j] - cmd) if self.tight[j] < self.ready[j] else (cmd - measured[j])

    def _lead_of(self, finger: str) -> int:
        return cfg.HANDSHAKE_THUMB_LEAD if finger == "thumb" else cfg.HANDSHAKE_LEAD

    def _within_lead(self, finger: str, c: float, measured: Sequence[int]) -> bool:
        """合拢程度到 c 时，每个关节的指令都不超过实测 LEAD：不跑在实际位置前面太多。"""
        for j in FINGER_JOINTS[finger]:
            cmd = self._joint_value(finger, j, c)
            gap = (measured[j] - cmd) if self.tight[j] < self.ready[j] else (cmd - measured[j])
            if gap > self._lead_of(finger):
                return False
        return True

    def _freeze(self, finger: str, measured: Sequence[int], pushing: Sequence[int]) -> None:
        """整根手指停在实测位置；顶住的关节只往里留一点点（保持轻轻接触）。"""
        margin = cfg.HANDSHAKE_THUMB_BLOCK_MARGIN if finger == "thumb" else cfg.HANDSHAKE_BLOCK_MARGIN
        held = {}
        for j in FINGER_JOINTS[finger]:
            if j in pushing:
                step = -margin if self.tight[j] < self.ready[j] else margin
                held[j] = _clip(measured[j] + step)
            else:
                held[j] = int(measured[j])
        self.held[finger] = held
        self.stop_at[finger] = {j: int(measured[j]) for j in pushing}

    def _hand_found(self) -> bool:
        """四指里至少一根碰到或被挡住：手确实在里面。"""
        return any(why in ("contact", "blocked") for f, why in self.stopped.items() if f != "thumb")

    def _step_closing(self, now: float, measured: Sequence[int]) -> None:
        dt, self.last_close = now - self.last_close, now
        hand_found = self._hand_found()
        for finger in FINGER_JOINTS:
            if finger in self.stopped:
                continue
            force = self.forces.get(finger)
            grip = self._grip_of(finger)
            if finger == "thumb" and not hand_found and (force or 0.0) < grip:
                continue  # 拇指等四指碰到手再动：没有手时拇指不往里走，不会撞进弯下的四指
            if force is not None and force >= grip:
                self.stopped[finger] = "contact"
                self.held[finger] = {j: self._command(j) for j in FINGER_JOINTS[finger]}
                continue
            rate = cfg.HANDSHAKE_CLOSE_RATE * (cfg.HANDSHAKE_THUMB_RATE_RATIO if finger == "thumb" else 1.0)
            if force is not None and force >= cfg.HANDSHAKE_SLOW_RATIO * grip:
                rate *= cfg.HANDSHAKE_SLOW_FACTOR  # 已经碰到一点了：放慢，停下时更接近握力
            new_c = min(1.0, self.closure[finger] + rate * dt)
            if self._within_lead(finger, new_c, measured):
                self.closure[finger] = new_c
                self.lagging.pop(finger, None)
            else:
                # 指令已经领先实测到上限、再往前就是硬压：先不往里走。实际位置也不动了 → 被挡住
                pos = tuple(measured[j] for j in FINGER_JOINTS[finger])
                ref = self.lagging.get(finger)
                moved = ref is not None and max(abs(a - b) for a, b in zip(pos, ref[0], strict=True))
                if ref is None or moved > cfg.HANDSHAKE_BLOCK_MOVE:
                    self.lagging[finger] = (pos, now)
                elif now - ref[1] >= cfg.HANDSHAKE_BLOCK_S:
                    lag = [j for j in FINGER_JOINTS[finger] if self._gap(j, measured) > self._lead_of(finger) / 2]
                    self._freeze(finger, measured, lag or list(FINGER_JOINTS[finger]))
                    self.stopped[finger] = "blocked"
                    continue
            if self.closure[finger] >= 1.0:
                self.stopped[finger] = "limit"
                self.held[finger] = {j: self._joint_value(finger, j, 1.0) for j in FINGER_JOINTS[finger]}
        self.mover.set_target(self._pose())
        four_done = all(f in self.stopped for f in FINGER_JOINTS if f != "thumb")
        if four_done and not self._hand_found() and self.stopped.get("thumb") != "contact":
            self.events.append("四指都弯到最紧也没碰到手，松开（拇指没有往里走）")
            self._release(now, None)
            return
        if len(self.stopped) < len(FINGER_JOINTS):
            return
        self.count += 1
        parts = [f"{cfg.FINGER_LABELS[f]}{STOP_TEXT[self.stopped[f]]}" for f in FINGER_JOINTS]
        self.events.append("握住了（" + "、".join(parts) + "）")
        self.still = {}
        self._enter("holding", now)

    # ------------------------------------------------------------ 握住
    def _margin_of(self, finger: str) -> int:
        return cfg.HANDSHAKE_THUMB_BLOCK_MARGIN if finger == "thumb" else cfg.HANDSHAKE_BLOCK_MARGIN

    def _settle(self) -> None:
        """握稳：每根碰到的手指取握稳这段时间的平均指尖力，作为判断“力掉下去了”的基准。
        （用平均而不是最大：你中途用力捏一下再放松，不会被当成抽手。）"""
        for finger, values in self.settle_samples.items():
            if values:
                self.ref[finger] = sum(values) / len(values)
        refs = list(self.ref.values())
        self.hold_force = sum(refs) / len(refs) if refs else None
        self.settled = True

    def _advance(self, finger: str, measured: Sequence[int]) -> int:
        """停下（轻贴）后这根手指往里走了多少：你的手不在那儿了它才会往里走。"""
        best = 0
        for j, stop in self.stop_at.get(finger, {}).items():
            best = max(best, (stop - measured[j]) if self.tight[j] < self.ready[j] else (measured[j] - stop))
        return best

    def _lost(self, finger: str, measured: Sequence[int]) -> bool:
        """这根手指是否失去了和你手的接触。
        被挡住的手指（轻轻贴着）：往里走了 → 你的手不在那儿了；
        碰到指尖的手指：指尖力掉到基准的一小部分（握稳前的基准用握力）。"""
        if self.give.get(finger, 0) > 0:
            return False  # 正在让一让（你在用力捏），力变小是正常的
        if self.stopped.get(finger) == "blocked":
            limit = cfg.HANDSHAKE_THUMB_RELEASE_ADVANCE if finger == "thumb" else cfg.HANDSHAKE_RELEASE_ADVANCE
            return self._advance(finger, measured) > limit
        force = self.forces.get(finger)
        if force is None:
            return False
        ref = self.ref.get(finger, self._grip_of(finger))
        return force < max(cfg.HANDSHAKE_RELEASE_ABS, cfg.HANDSHAKE_RELEASE_RATIO * ref)

    def _step_holding(self, now: float, fresh: bool, measured: Sequence[int]) -> None:
        held_s = now - self.since
        changed = False
        if fresh and not self.settled:  # 握稳这段时间：记下每根碰到的手指的指尖力
            for finger, why in self.stopped.items():
                force = self.forces.get(finger)
                if why == "contact" and force is not None:
                    self.settle_samples.setdefault(finger, []).append(force)
        if not self.settled and held_s >= cfg.HANDSHAKE_SETTLE_S:
            self._settle()
        # 某个关节又开始往里顶（例如你的手动了一下）：退回到实测位置，不越压越紧
        for finger in FINGER_JOINTS:
            if self.stopped.get(finger) not in ("contact", "blocked", "limit"):
                continue
            pushing = self._pushing_joints(finger, measured, now)
            if pushing:
                self._freeze(finger, measured, pushing)
                if self.stopped[finger] != "blocked":
                    self.events.append(f"{cfg.FINGER_LABELS[finger]}顶住了，退回到实测位置")
                self.stopped[finger] = "blocked"
                changed = True
        if fresh:
            for finger in FINGER_JOINTS:  # 捏得太紧：让一让（有上限）；你放松后慢慢回到原来握住的位置
                force = self.forces.get(finger)
                if force is None or finger not in self.held:
                    continue
                give = self.give.get(finger, 0)
                if force > self._limit_of(finger):
                    new = min(cfg.HANDSHAKE_RELAX_MAX, give + cfg.HANDSHAKE_RELAX_RAW)
                elif give and force < 0.6 * self._limit_of(finger):
                    new = max(0, give - cfg.HANDSHAKE_RELAX_BACK)
                else:
                    new = give
                if new != give:
                    self.give[finger] = new
                    changed = True
        if changed:
            self.mover.set_target(self._pose())
        if not fresh or not self.auto_release:
            return
        if held_s >= cfg.HANDSHAKE_MAX_HOLD_S:
            self._release(now, f"已经握了 {cfg.HANDSHAKE_MAX_HOLD_S:.0f} 秒")
            return
        # 抽手：一半以上碰到 / 挡住的手指失去接触，连续 RELEASE_FRAMES 次
        touched = [f for f, why in self.stopped.items() if why in ("contact", "blocked")]
        if not touched:
            return
        lost = [f for f in touched if self._lost(f, measured)]
        need = max(1, (len(touched) + 1) // 2)
        self.low_frames = self.low_frames + 1 if len(lost) >= need else 0
        if self.low_frames >= cfg.HANDSHAKE_RELEASE_FRAMES:
            names = "、".join(cfg.FINGER_LABELS[f] for f in lost)
            self._release(now, f"{names}和你的手没接触了，你在抽手")

    def _release(self, now: float, reason: str | None) -> None:
        if reason:
            self.events.append(f"松开：{reason}")
        self.closure = dict.fromkeys(FINGER_JOINTS, 0.0)
        self.stopped, self.held, self.stop_at = {}, {}, {}
        self.mover.goto(self.ready)
        self._enter("releasing", now)

    # ------------------------------------------------------------ 给网页
    def take_events(self) -> list[str]:
        events, self.events = self.events, []
        return events

    def _shown_closure(self, finger: str, pose: Pose) -> float:
        """按当前指令换算的合拢程度（停下后往回松 / 退回实测，网页上也能看出来）。拇指按弯曲算。"""
        j = FINGER_JOINTS[finger][0]
        span = self.tight[j] - self.ready[j]
        return 0.0 if span == 0 else min(1.0, max(0.0, (pose[j] - self.ready[j]) / span))

    def status(self) -> dict:
        pose = self._pose()
        return {
            "state": self.state,
            "text": STATE_TEXT[self.state],
            "count": self.count,
            "grip_force": self.grip_force,
            "force_limit": cfg.HANDSHAKE_FORCE_LIMIT,
            "thumb_ratio": cfg.HANDSHAKE_THUMB_FORCE_RATIO,
            "hold_force": None if self.hold_force is None else round(self.hold_force, 1),
            "auto_start": self.auto_start,
            "auto_release": self.auto_release,
            "use_thumb": self.use_thumb,
            "fingers": [
                {
                    "name": f,
                    "label": cfg.FINGER_LABELS[f],
                    "closure": round(self._shown_closure(f, pose), 2),
                    "force": None if self.forces.get(f) is None else round(self.forces[f], 1),
                    "grip": round(self._grip_of(f), 1),
                    "stopped": STOP_TEXT.get(self.stopped.get(f, ""), ""),
                }
                for f in FINGER_JOINTS
            ],
        }
