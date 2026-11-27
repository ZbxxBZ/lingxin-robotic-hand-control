"""摄像头镜像用的手部识别、关节映射和跟随保护。本模块不直接访问真机。

- HandTracker     MediaPipe HandLandmarker（Tasks 接口），每帧识别一只手的 21 个关键点
- hand_features   从 3D 关键点算出与左右手无关的特征（弯曲角、侧摆角、拇指位置）
- MirrorMapper    按标定把特征线性映射成 L10 的 10 个关节原始值（0~255），含捏取辅助
- OneEuroFilter   抑制抖动，又不明显拖慢跟随
- classify_rps    根据四指弯曲程度判断石头 / 剪刀 / 布
- MirrorGuard     实时跟随时的防撞、堵转卸力和按力卸载
                  （对应预设动作里的“分段过渡”和“卸力”，改成每个控制周期判断一次）

MediaPipe 关键点编号：0 手腕；拇指 1~4（CMC、MCP、IP、指尖）；
食指 5~8、中指 9~12、无名指 13~16、小指 17~20（MCP、PIP、DIP、指尖）。
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

import l10_config as cfg
from l10_hand import FINGER_FLEX_JOINT, FINGER_JOINTS, THUMB_JOINTS

Pose = tuple[int, ...]

FEATURES = tuple(cfg.MIRROR_FEATURE_DEFAULTS)
PINCH_TIPS = {"index": 8, "middle": 12, "ring": 16, "pinky": 20}  # MediaPipe 指尖关键点
CURL_FEATURES = ("index_curl", "middle_curl", "ring_curl", "pinky_curl")
FEATURE_LABELS = {
    "thumb_bend": "拇指弯曲",
    "thumb_front": "拇指前伸",
    "thumb_across": "拇指横移",
    "index_curl": "食指弯曲",
    "middle_curl": "中指弯曲",
    "ring_curl": "无名指弯曲",
    "pinky_curl": "小指弯曲",
    "index_spread": "食指侧摆",
    "ring_spread": "无名指侧摆",
    "pinky_spread": "小指侧摆",
}
# 侧摆关节 → 决定它“回中位”程度的那根手指的弯曲关节
ABD_FOLLOWS = {6: 2, 7: 4, 8: 5}
NEUTRAL_ABD = {j: cfg.POSES["open"][j] for j in ABD_FOLLOWS}

# 标定姿势 → 它记录哪些特征的哪一端（0 = A 端，1 = B 端）
CALIBRATIONS = {
    "open": {
        "label": "张开五指",
        "key": "O",
        "points": {
            "thumb_bend": 0,
            "thumb_front": 0,
            "thumb_across": 0,
            **dict.fromkeys(CURL_FEATURES, 0),
            "index_spread": 1,
            "ring_spread": 1,
            "pinky_spread": 1,
        },
    },
    "fist": {
        "label": "握拳",
        "key": "F",
        "points": {"thumb_bend": 1, "thumb_front": 1, **dict.fromkeys(CURL_FEATURES, 1)},
    },
    "thumb": {
        "label": "拇指碰小指根",
        "key": "T",
        "points": {"thumb_across": 1},
    },
}
MIN_SPAN_RATIO = 0.3  # 标定后 A、B 两端至少要相差默认跨度的这么多


def pose_matches(kind: str, feats: Mapping[str, float]) -> bool:
    """按默认映射粗判标定姿势对不对（只看最可靠的特征），防止摆错姿势按错键。"""
    prog = {}
    for name in FEATURES:
        a, b = cfg.MIRROR_FEATURE_DEFAULTS[name]
        prog[name] = (feats[name] - a) / (b - a)
    curl = sum(prog[c] for c in CURL_FEATURES) / len(CURL_FEATURES)
    if kind == "open":
        return curl < 0.35
    if kind == "fist":
        return curl > 0.65
    return prog["thumb_across"] > 0.5  # 拇指尖横到了掌心另一侧


# ---------------------------------------------------------------- 识别
@dataclass
class TrackedHand:
    world: np.ndarray  # (21, 3) 以手为中心的 3D 坐标（米）
    image: np.ndarray  # (21, 2) 图像归一化坐标 0~1，用于画骨架
    label: str  # "Left" / "Right"（输入是镜像画面时，就是真实的左右手）
    score: float


class HandTracker:
    """MediaPipe HandLandmarker 的视频模式封装，只识别一只手。"""

    def __init__(self, model_path: str | Path) -> None:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=cfg.MIRROR_DETECT_CONFIDENCE,
            min_hand_presence_confidence=cfg.MIRROR_DETECT_CONFIDENCE,
            min_tracking_confidence=cfg.MIRROR_TRACK_CONFIDENCE,
        )
        self._mp = mp
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._last_ts = -1

    def detect(self, frame_rgb: np.ndarray, timestamp_ms: int) -> TrackedHand | None:
        """frame_rgb：H×W×3 的 RGB uint8 图像。时间戳必须递增（这里自动保证）。"""
        ts = max(int(timestamp_ms), self._last_ts + 1)
        self._last_ts = ts
        image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame_rgb)
        )
        result = self._landmarker.detect_for_video(image, ts)
        if not result.hand_world_landmarks or not result.hand_landmarks:
            return None
        world = np.array([[p.x, p.y, p.z] for p in result.hand_world_landmarks[0]], dtype=float)
        points = np.array([[p.x, p.y] for p in result.hand_landmarks[0]], dtype=float)
        label, score = "", 0.0
        if result.handedness and result.handedness[0]:
            best = result.handedness[0][0]
            label, score = best.category_name or "", float(best.score or 0.0)
        return TrackedHand(world=world, image=points, label=label, score=score)

    def close(self) -> None:
        self._landmarker.close()


# ---------------------------------------------------------------- 特征
def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else np.zeros_like(v)


def _angle(u: np.ndarray, v: np.ndarray) -> float:
    cos = float(np.clip(np.dot(_unit(u), _unit(v)), -1.0, 1.0))
    return math.degrees(math.acos(cos))


def hand_features(points: np.ndarray) -> dict[str, float]:
    """21 个 3D 关键点 → 特征字典（含义见 l10_config.MIRROR_JOINT_MAP 上方的说明）。

    只用夹角、距离和由关键点自身定义的方向，与左右手、画面是否镜像无关。
    """
    p = np.asarray(points, dtype=float).reshape(21, 3)
    lateral = p[5] - p[17]  # 小指根 → 食指根
    width = float(np.linalg.norm(lateral))
    if width < 1e-6:
        raise ValueError("关键点异常：食指根和小指根重合")
    lat = lateral / width
    fwd_raw = p[9] - p[0]  # 手腕 → 中指根
    fwd = _unit(fwd_raw - np.dot(fwd_raw, lat) * lat)
    normal = np.cross(fwd, lat)  # 掌面法线（正负随左右手变化，只取绝对值用）

    feats: dict[str, float] = {}
    for name, (mcp, pip, dip) in (
        ("index", (5, 6, 7)),
        ("middle", (9, 10, 11)),
        ("ring", (13, 14, 15)),
        ("pinky", (17, 18, 19)),
    ):
        feats[f"{name}_curl"] = _angle(p[mcp] - p[0], p[pip] - p[mcp]) + _angle(
            p[pip] - p[mcp], p[dip] - p[pip]
        )
    feats["thumb_bend"] = _angle(p[2] - p[1], p[3] - p[2]) + _angle(p[3] - p[2], p[4] - p[3])
    # 拇指掌骨绕手掌长轴的转角：0° 指向掌面侧方，90° 垂直掌面（转到掌心前方）；
    # 对应 L10 拇指翻滚关节（转轴沿手掌长轴）
    meta = p[2] - p[1]
    feats["thumb_front"] = math.degrees(
        math.atan2(abs(float(np.dot(meta, normal))), float(np.dot(meta, lat)))
    )
    feats["thumb_across"] = float(np.dot(p[4] - p[5], lat)) / width

    def azimuth(mcp: int, pip: int) -> float:
        d = p[pip] - p[mcp]
        return math.degrees(math.atan2(float(np.dot(d, lat)), float(np.dot(d, fwd))))

    a_index, a_middle, a_ring, a_pinky = (azimuth(5, 6), azimuth(9, 10), azimuth(13, 14), azimuth(17, 18))
    feats["index_spread"] = a_index - a_middle  # 食指往拇指侧张开为正
    feats["ring_spread"] = a_middle - a_ring  # 无名指往小指侧张开为正
    feats["pinky_spread"] = a_ring - a_pinky  # 小指相对无名指往外张开为正
    for finger, tip in PINCH_TIPS.items():  # 拇指尖到各指尖的距离（捏取辅助用）
        feats[f"pinch_{finger}"] = float(np.linalg.norm(p[4] - p[tip])) / width
    return feats


def average_features(samples: Sequence[Mapping[str, float]]) -> dict[str, float]:
    if not samples:
        raise ValueError("没有样本")
    return {name: float(np.mean([s[name] for s in samples])) for name in FEATURES}


# ---------------------------------------------------------------- 映射
PINCH_TARGETS = {
    finger: (tuple(int(v) for v in pose), tuple(int(j) for j in joints))
    for finger, (pose, joints) in cfg.MIRROR_PINCH_TARGETS.items()
    if finger in PINCH_TIPS
}
PINCH_LABELS = {"index": "食指", "middle": "中指", "ring": "无名指", "pinky": "小指"}


def pinch_amounts(feats: Mapping[str, float], progress: Mapping[str, float]) -> dict[str, float]:
    """每根配置了捏取姿态的手指的捏取程度 0~1：拇指尖离它的指尖越近越大；这根手指弯得很深（握拳）时为 0。"""
    if not cfg.MIRROR_PINCH_ASSIST:
        return {}
    near, far = cfg.MIRROR_PINCH_DIST
    lo, hi = cfg.MIRROR_PINCH_CURL_GATE
    amounts = {}
    for finger in PINCH_TARGETS:
        dist = feats.get(f"pinch_{finger}")
        if dist is None:
            continue
        closeness = min(1.0, max(0.0, (far - dist) / (far - near)))
        gate = min(1.0, max(0.0, (hi - progress[f"{finger}_curl"]) / (hi - lo)))
        s = closeness * gate
        amounts[finger] = s * s * (3.0 - 2.0 * s)  # 平滑过渡，两端变化慢，不会突然跳到捏取姿态
    return amounts


FIST_THUMB = {j: int(cfg.POSES["fist"][j]) for j in cfg.MIRROR_FIST_THUMB_JOINTS}


def _ramp(value: float, lo: float, hi: float) -> float:
    """value 从 lo 到 hi 时平滑地从 0 变到 1（两端变化慢）。"""
    s = min(1.0, max(0.0, (value - lo) / (hi - lo)))
    return s * s * (3.0 - 2.0 * s)


def fist_amount(progress: Mapping[str, float]) -> float:
    """握拳程度 0~1：四指都弯、且拇指收在掌前时接近 1；竖大拇指（拇指在侧面）时为 0。"""
    if not cfg.MIRROR_FIST_ASSIST:
        return 0.0
    curl = sum(progress[c] for c in CURL_FEATURES) / len(CURL_FEATURES)
    return _ramp(curl, *cfg.MIRROR_FIST_CURL) * _ramp(progress["thumb_front"], *cfg.MIRROR_FIST_THUMB_FRONT)


def blend_pinch(raw: list[float], amounts: Mapping[str, float]) -> None:
    """把各捏取姿态按捏取程度混进 raw：每个关节取“用到它的捏取姿态”的加权平均，混合比例取其中最大的程度。"""
    for j in range(len(raw)):
        weights = [(amounts.get(f, 0.0), pose[j]) for f, (pose, joints) in PINCH_TARGETS.items() if j in joints]
        weights = [(w, v) for w, v in weights if w > 0]
        if not weights:
            continue
        total = sum(w for w, _ in weights)
        target = sum(w * v for w, v in weights) / total
        raw[j] += (target - raw[j]) * max(w for w, _ in weights)


class MirrorMapper:
    """特征 → 10 个关节原始值。points[特征] = [A 端特征值, B 端特征值]。"""

    def __init__(self, calibration: Mapping | None = None) -> None:
        self.points: dict[str, list[float]] = {}
        self.done: list[str] = []
        self.marked: set[tuple[str, int]] = set()  # 哪些 (特征, 端) 是实测标定的
        self.reset()
        if calibration:
            for name, pair in dict(calibration.get("points", {})).items():
                if name in self.points and len(pair) == 2:
                    self.points[name] = [float(pair[0]), float(pair[1])]
            self.done = [k for k in calibration.get("done", []) if k in CALIBRATIONS]
            self.marked = {
                (str(n), int(e)) for n, e in calibration.get("marked", []) if n in self.points and e in (0, 1)
            }

    def reset(self) -> None:
        self.points = {name: [float(a), float(b)] for name, (a, b) in cfg.MIRROR_FEATURE_DEFAULTS.items()}
        self.done = []
        self.marked = set()

    def progress(self, feats: Mapping[str, float]) -> dict[str, float]:
        """每个特征在 A→B 之间的位置，0 = A 端，1 = B 端（已截断到 0~1）。"""
        result = {}
        for name in FEATURES:
            a, b = self.points[name]
            t = (feats[name] - a) / (b - a) if b != a else 0.0
            result[name] = min(1.0, max(0.0, t))
        return result

    def map(self, feats: Mapping[str, float]) -> tuple[float, ...]:
        """返回 10 个关节的原始值（浮点，0~255，未取整，便于滤波）。"""
        prog = self.progress(feats)
        raw = []
        for joint in cfg.JOINT_NAMES:
            feature, raw_a, raw_b = cfg.MIRROR_JOINT_MAP[joint]
            raw.append(raw_a + (raw_b - raw_a) * prog[feature])
        for abd, flex in ABD_FOLLOWS.items():
            neutral = NEUTRAL_ABD[abd]
            if not cfg.MIRROR_ABDUCTION:
                raw[abd] = neutral
            elif cfg.MIRROR_SPREAD_WITH_CURL:
                straight = raw[flex] / 255.0
                raw[abd] = neutral + (raw[abd] - neutral) * straight
        fist = fist_amount(prog)
        if fist > 0:  # 握拳：拇指带到握拳预设的位置，避免横插进弯曲的手指里卡住
            for j, value in FIST_THUMB.items():
                raw[j] += (value - raw[j]) * fist
        # 四指弯着时拇指不折得比握拳预设更深（松拳途中握拳辅助减弱，拇指旋转也不会往里横扫）
        curl = sum(prog[c] for c in CURL_FEATURES) / len(CURL_FEATURES)
        limit = _ramp(curl, *cfg.MIRROR_THUMB_GUARD_CURL)
        if limit > 0:
            for j, value in FIST_THUMB.items():
                raw[j] = max(raw[j], value * limit)
        blend_pinch(raw, pinch_amounts(feats, prog))
        return tuple(min(255.0, max(0.0, v)) for v in raw)

    def pinch(self, feats: Mapping[str, float]) -> tuple[str | None, float]:
        """捏取程度最大的那根手指和它的程度；没有捏取时为 (None, 0.0)。"""
        amounts = pinch_amounts(feats, self.progress(feats))
        if not amounts:
            return None, 0.0
        finger = max(amounts, key=amounts.get)
        return (finger, amounts[finger]) if amounts[finger] > 0 else (None, 0.0)

    def calibrate(self, kind: str, feats: Mapping[str, float]) -> tuple[list[str], list[str]]:
        """用一个标定姿势的平均特征更新对应端点。返回 (已采用的特征, 被拒绝的特征)。

        姿势明显不对（例如张开时按了握拳）抛出 ValueError，什么都不改。
        某项和另一端太接近或方向反了：另一端已实测标定 → 这一项不采用；
        另一端还是默认值 → 把另一端推开默认跨度的一半，保证映射可用，等标定另一端时再改准。
        """
        if not pose_matches(kind, feats):
            raise ValueError(f"姿势不像“{CALIBRATIONS[kind]['label']}”")
        accepted, rejected = [], []
        for name, end in CALIBRATIONS[kind]["points"].items():
            value = float(feats[name])
            default_a, default_b = cfg.MIRROR_FEATURE_DEFAULTS[name]
            default_span = default_b - default_a
            pair = list(self.points[name])
            pair[end] = value
            other = 1 - end
            span = pair[1] - pair[0]
            if span * default_span <= 0 or abs(span) < MIN_SPAN_RATIO * abs(default_span):
                if (name, other) in self.marked:
                    rejected.append(name)
                    continue
                half = 0.5 * default_span
                pair[other] = value + half if other == 1 else value - half
            self.points[name] = pair
            self.marked.add((name, end))
            accepted.append(name)
        if accepted and kind not in self.done:
            self.done.append(kind)
        return accepted, rejected

    def to_json(self) -> dict:
        return {
            "version": 1,
            "points": self.points,
            "done": self.done,
            "marked": sorted([name, end] for name, end in self.marked),
        }


def load_calibration(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        print(f"[警告] 标定文件 {path} 读取失败，改用默认值：{error}")
        return None


def save_calibration(path: Path, mapper: MirrorMapper) -> None:
    path.write_text(json.dumps(mapper.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 滤波
class OneEuroFilter:
    """One Euro 滤波器（Casiez 等，2012），对每个通道独立滤波。"""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float = 1.0) -> None:
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self) -> None:
        self.x: np.ndarray | None = None
        self.dx: np.ndarray | None = None
        self.t: float | None = None

    @staticmethod
    def _alpha(cutoff: np.ndarray | float, dt: float) -> np.ndarray | float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, values: Sequence[float], t: float) -> np.ndarray:
        x = np.asarray(values, dtype=float)
        if self.x is None or self.t is None or self.dx is None:
            self.x, self.dx, self.t = x.copy(), np.zeros_like(x), t
            return x.copy()
        dt = max(t - self.t, 1e-3)
        self.t = t
        self.dx = self.dx + self._alpha(self.d_cutoff, dt) * ((x - self.x) / dt - self.dx)
        cutoff = self.min_cutoff + self.beta * np.abs(self.dx)
        self.x = self.x + self._alpha(cutoff, dt) * (x - self.x)
        return self.x.copy()


class Deadband:
    """回差（backlash）滤波：输入在当前输出 ±band 以内变化时输出不动；超出时输出被“拖着”走，
    始终离输入不超过 band。手静止时识别噪声传不到电机；手一动立即跟上，没有跳变。
    输入到达 0 或 255（完全弯曲 / 伸直）时输出直接到端点，保证能完全握紧和张开。"""

    def __init__(self, band: float) -> None:
        self.band = max(0.0, float(band))
        self.out: np.ndarray | None = None

    def reset(self) -> None:
        self.out = None

    def __call__(self, values: Sequence[float]) -> np.ndarray:
        x = np.asarray(values, dtype=float)
        if self.out is None or self.out.shape != x.shape or self.band <= 0:
            self.out = x.copy()
            return x.copy()
        self.out = np.clip(self.out, x - self.band, x + self.band)
        ends = (x <= 0.5) | (x >= 254.5)
        self.out[ends] = x[ends]
        return self.out.copy()


# ---------------------------------------------------------------- 手势
RPS_LABELS = {"rock": "石头", "scissors": "剪刀", "paper": "布"}
RPS_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}  # 键 赢 值


def classify_rps(progress: Mapping[str, float]) -> str | None:
    """四指弯曲程度 → "rock" / "scissors" / "paper"；看不出来返回 None。拇指不参与判断。"""
    curl = [progress[name] for name in CURL_FEATURES]
    straight = [c < cfg.GESTURE_EXTENDED for c in curl]
    bent = [c > cfg.GESTURE_BENT for c in curl]
    if all(bent):
        return "rock"
    if all(straight):
        return "paper"
    if straight[0] and straight[1] and bent[2] and bent[3]:
        return "scissors"
    return None


def judge(human: str, robot: str) -> str:
    """返回 "human" / "robot" / "draw"。"""
    if human == robot:
        return "draw"
    return "human" if RPS_BEATS[human] == robot else "robot"


class PoseMover(Protocol):
    def goto(self, pose: tuple[int, ...]) -> None: ...


class RpsGame:
    """石头剪刀布流程。worker 是任何带 goto(姿态) 的对象（07 的控制线程或网页的服务线程）。"""

    def __init__(self) -> None:
        self.state = "idle"  # idle 等待开始 / count 喊口令 / reveal 出拳 / result 显示结果
        self.score = {"human": 0, "robot": 0, "draw": 0}
        self.robot: str | None = None
        self.human: str | None = None
        self.outcome: str | None = None
        self.votes: Counter[str] = Counter()
        self.t0 = 0.0
        self.schedule: list[tuple[float, tuple[int, ...]]] = []

    def start(self, now: float) -> None:
        self.state, self.t0 = "count", now
        self.robot = random.choice(tuple(cfg.GAME_POSES))  # 开局前就定好，不看你的手
        self.human, self.outcome = None, None
        self.votes.clear()
        beat = cfg.GAME_BEAT_S
        self.schedule = [
            (now, cfg.GAME_PUMP),
            (now + beat / 2, cfg.GAME_READY),
            (now + beat, cfg.GAME_PUMP),
            (now + beat * 1.5, cfg.GAME_READY),
        ]

    def update(self, now: float, gesture: str | None, worker: PoseMover | None) -> tuple[str, str]:
        """推进一帧，返回 (大字, 小字)。"""
        beat = cfg.GAME_BEAT_S
        reveal_at = self.t0 + 2 * beat
        if self.state == "count":
            while self.schedule and now >= self.schedule[0][0]:
                _, pose = self.schedule.pop(0)
                if worker:
                    worker.goto(pose)
            if now >= reveal_at:
                self.state = "reveal"
                if worker:
                    worker.goto(cfg.GAME_POSES[self.robot])
            else:
                return ("石头…", "") if now < self.t0 + beat else ("剪刀…", "")
        if self.state == "reveal":
            if gesture and now >= reveal_at + cfg.GAME_CAPTURE_DELAY_S:
                self.votes[gesture] += 1
            if now < reveal_at + cfg.GAME_CAPTURE_DELAY_S + cfg.GAME_CAPTURE_S:
                return "布！", f"L10 出：{RPS_LABELS[self.robot]}"
            self.human = self.votes.most_common(1)[0][0] if self.votes else None
            if self.human:
                self.outcome = judge(self.human, self.robot)
                self.score[self.outcome] += 1
            self.state, self.t0 = "result", now
        if self.state == "result":
            if now - self.t0 >= cfg.GAME_RESULT_S:
                self.state = "idle"
                if worker:
                    worker.goto(cfg.GAME_READY)
            else:
                if self.human is None:
                    return "没看清", "这局不算，按空格再来"
                text = {"human": "你赢了！", "robot": "L10 赢了！", "draw": "平局"}[self.outcome]
                return text, f"你：{RPS_LABELS[self.human]}    L10：{RPS_LABELS[self.robot]}"
        return "", "按空格开始一局"


# ---------------------------------------------------------------- 跟随保护
def thumb_folded(pose: Sequence[float]) -> bool:
    return pose[0] < cfg.THUMB_CLEAR_FLEX or pose[1] < cfg.THUMB_CLEAR_ROLL


# 捏取时判断“四指在大幅运动”只看没被捏的手指的弯曲（侧摆幅度小、挡不到拇指），拇指和被捏的手指可以一起动
PINCH_BUSY_BASE = (2, 3, 4, 5)
# 会“往里压”的关节：数值减小 = 合拢（弯曲关节、拇指翻滚 / 旋转）。侧摆力气小，不限制。
PRESSING_JOINTS = (0, 1, 2, 3, 4, 5, 9)


class MirrorGuard:
    """每个控制周期把“人手映射出的目标”加工成安全的指令。

    1. 下限：堵转或指尖力超限的关节，记一个“最多合拢到这里”的下限，人手往回张开超过下限后解除。
    2. 防撞顺序（按状态判断，人手动得再慢也不会漏掉）：
       - 拇指折着时记下四指当时的位置（锚点）。四指只能往合拢方向微调（最多 MIRROR_FOLDED_HOLD），不往伸直方向走；
         人手要四指离开锚点超过 MIRROR_FOLDED_MOVE，拇指先往外退开（只退不进），四指原地等；
         拇指 MIRROR_CLEAR_STALL_S 不动（被压住）或等满 MIRROR_CLEAR_TIMEOUT_S 才不再等。
       - 拇指没折着、但要往里折时，四指必须已经停稳，否则拇指在 MIRROR_THUMB_CLEAR 外面等。
       - 捏取时被捏的那根手指不参与上面的判断，可以和拇指一起动。
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.settled_since: float | None = None
        self.anchor: dict[int, int] | None = None  # 拇指折上时四指的位置（判断往合拢方向走了多远）
        self.lowest: dict[int, int] | None = None  # 拇指折着期间四指合得最紧的位置（不许再往伸直方向走）
        self.clearing = False  # 正在等拇指退开
        self.clear_since = 0.0
        self.thumb_moved_at = 0.0
        self.last_thumb: tuple[int, ...] | None = None
        self.history: list[tuple[float, Pose]] = []  # 最近的实测，用来判断四指是否停稳
        self.phase = "free"  # free 直接跟随 / fingers 四指先动 / clear 等拇指让开 / forced 不再等
        self.pinch_mode = False  # 捏取时防撞放宽
        self.pinch_finger: str | None = None
        self.stall_floor: dict[int, int] = {}
        self.force_floor: dict[int, tuple[int, int]] = {}  # 关节 → (下限, 最多退到)
        self._still: dict[int, tuple[int, float]] = {}  # 关节 → (上次实测, 开始不动的时间)
        self.last_cmd: Pose | None = None
        self.events: list[str] = []

    # ------------------------------------------------------------ 下限
    def _floors(self) -> dict[int, int]:
        floors = dict(self.stall_floor)
        for j, (floor, _cap) in self.force_floor.items():
            floors[j] = max(floors.get(j, 0), floor)
        return floors

    def _release_floors(self, desired: Sequence[float]) -> None:
        """人手已经张开到下限以上：解除该关节的下限。"""
        for table in (self.stall_floor, self.force_floor):
            for j in list(table):
                floor = table[j] if isinstance(table[j], int) else table[j][0]
                if desired[j] >= floor:
                    del table[j]

    def _update_stall(self, measured: Pose, now: float) -> None:
        if self.last_cmd is None:
            return
        for j in PRESSING_JOINTS:
            pushing = measured[j] - self.last_cmd[j] > cfg.MIRROR_STALL_ERR  # 指令比实测更合拢
            last, since = self._still.get(j, (measured[j], now))
            # 只在“正往里压”期间计时：没在压、或者还在动，都从现在重新计时。
            # （否则手静止一会儿后人手突然握拳，电机还没起步就会被误判为堵转）
            if not pushing or abs(measured[j] - last) > 2:
                self._still[j] = (measured[j], now)
                continue
            self._still[j] = (last, since)
            if now - since >= cfg.MIRROR_STALL_S and j not in self.stall_floor:
                floor = min(255, measured[j] - cfg.RELIEF_MARGIN)
                self.stall_floor[j] = max(0, floor)
                self.events.append(f"堵转卸力：{cfg.JOINT_LABELS[j]} 停在 {measured[j]}，不再往里顶")

    def _update_force(self, measured: Pose, forces: Mapping[str, float | None]) -> None:
        for finger, j in FINGER_FLEX_JOINT.items():
            value = forces.get(finger)
            if value is None or value <= cfg.MIRROR_FORCE_LIMIT:
                continue
            first = j not in self.force_floor
            floor, cap = self.force_floor.get(j, (measured[j], min(255, measured[j] + cfg.MIRROR_FORCE_MAX)))
            new = min(cap, max(floor, measured[j]) + cfg.MIRROR_FORCE_STEP)
            self.force_floor[j] = (max(new, floor), cap)
            if first:  # 之后每次读力超限再退一步，不重复记日志
                self.events.append(
                    f"压力卸载：{cfg.FINGER_LABELS[finger]}力 {value:.0f}，{cfg.JOINT_LABELS[j]} 往回退"
                )

    # ------------------------------------------------------------ 主函数
    def step(
        self,
        desired: Sequence[float],
        measured: Sequence[int],
        now: float,
        forces: Mapping[str, float | None] | None = None,
        pinch: float = 0.0,
        pinch_finger: str | None = None,
    ) -> Pose:
        """desired：人手映射值；measured：实测角度；forces：本周期新读到的去皮指尖力（没读则 None）；
        pinch / pinch_finger：捏取程度和被捏的手指（MirrorMapper.pinch），够大时拇指和这根手指可以同时动。"""
        want = [int(round(min(255.0, max(0.0, v)))) for v in desired]
        meas = tuple(int(v) for v in measured)
        self._update_stall(meas, now)
        if forces is not None:
            self._update_force(meas, forces)
        self._release_floors(want)
        for j, floor in self._floors().items():
            want[j] = max(want[j], floor)

        enter, leave = cfg.MIRROR_PINCH_RELAX
        if pinch >= enter:
            self.pinch_mode = True
        elif pinch <= leave:
            self.pinch_mode = False
        if self.pinch_mode and pinch_finger in FINGER_FLEX_JOINT:
            pinched = FINGER_FLEX_JOINT[pinch_finger]
            self.pinch_finger = pinch_finger
            busy_joints = tuple(j for j in PINCH_BUSY_BASE if j != pinched)
        else:
            busy_joints = FINGER_JOINTS
        settled = self._fingers_settled(want, meas, now, busy_joints)
        cmd = list(want)
        clear = dict(zip(THUMB_JOINTS, cfg.MIRROR_THUMB_CLEAR, strict=True))
        if thumb_folded(meas):
            if self.anchor is None:
                self.anchor = {j: meas[j] for j in FINGER_JOINTS}
                self.lowest = dict(self.anchor)
            assert self.lowest is not None
            for j in FINGER_JOINTS:
                self.lowest[j] = min(self.lowest[j], meas[j])
            drift = max(
                max(want[j] - self.lowest[j], self.anchor[j] - want[j]) for j in busy_joints
            )  # 想伸直（相对合得最紧处）或想继续往里握（相对拇指折上时）
            if not self.clearing and drift > cfg.MIRROR_FOLDED_MOVE:
                self.clearing, self.clear_since, self.thumb_moved_at = True, now, now
                self.last_thumb = tuple(meas[j] for j in THUMB_JOINTS)
            if self.clearing:
                thumb_now = tuple(meas[j] for j in THUMB_JOINTS)
                if self.last_thumb is None or max(abs(a - b) for a, b in zip(thumb_now, self.last_thumb)) > 2:
                    self.last_thumb, self.thumb_moved_at = thumb_now, now
                for j in THUMB_JOINTS:  # 只往外退：不低于目标、退开位置和当前实测
                    cmd[j] = max(want[j], clear[j], meas[j])
                stuck = now - self.thumb_moved_at >= cfg.MIRROR_CLEAR_STALL_S
                if stuck or now - self.clear_since >= cfg.MIRROR_CLEAR_TIMEOUT_S:
                    if self.phase != "forced":
                        self.events.append("拇指没能让开（可能被压住），四指直接运动")
                    self.phase = "forced"
                else:
                    self.phase = "clear"
                    for j in busy_joints:
                        cmd[j] = self.lowest[j]
            else:
                self.phase = "free"
                for j in busy_joints:  # 四指只能往合拢方向微调，不往伸直方向走（不会一点点伸到拇指底下）
                    low = min(self.anchor[j] - cfg.MIRROR_FOLDED_HOLD, self.lowest[j])
                    cmd[j] = min(self.lowest[j], max(low, want[j]))
        else:
            self.anchor, self.lowest, self.clearing = None, None, False
            if thumb_folded(want) and not settled:
                self.phase = "fingers"  # 四指还在动：拇指在外面等
                for j in THUMB_JOINTS:
                    cmd[j] = max(want[j], clear[j])
            else:
                self.phase = "free"
        self.last_cmd = tuple(cmd)
        return self.last_cmd

    def _fingers_settled(self, want: Sequence[int], meas: Pose, now: float, joints: Sequence[int]) -> bool:
        """四指是否停稳：最近 MIRROR_SETTLE_S 内实测变化很小，且离目标不远，并保持了 MIRROR_SETTLE_S。"""
        self.history.append((now, meas))
        while len(self.history) > 2 and now - self.history[1][0] >= cfg.MIRROR_SETTLE_S:
            self.history.pop(0)
        old = self.history[0][1]
        moving = max(abs(meas[j] - old[j]) for j in joints) > cfg.MIRROR_SETTLE_MOVE
        close = max(abs(want[j] - meas[j]) for j in joints) <= cfg.MIRROR_SETTLE_TOL
        if moving or not close:
            self.settled_since = None
            return False
        self.settled_since = now if self.settled_since is None else self.settled_since
        return now - self.settled_since >= cfg.MIRROR_SETTLE_S

    def take_events(self) -> list[str]:
        events, self.events = self.events, []
        return events

    @property
    def floors(self) -> dict[int, int]:
        return self._floors()


PHASE_TEXT = {
    "free": "",
    "fingers": "防撞：四指先到位，拇指暂不进掌心",
    "clear": "防撞：拇指先让开，四指等待",
    "forced": "拇指没让开，四指直接运动",
}
