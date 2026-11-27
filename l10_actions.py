"""从官方 L10 控制台（l10-dashboard_2025.08.06.exe）迁移来的预设动作和动画。

控制台把一次姿态拆成两帧发送，和 SDK 的 10 个关节一一对应：
  手指帧 0x01 的 6 个值 → SDK 关节 1~6（拇指弯曲、拇指翻滚、食指、中指、无名指、小指）
  掌部帧 0x04 的 4 个值 → SDK 关节 7~10（食指侧摆、无名指侧摆、小指侧摆、拇指旋转）
控制台手指数组的第 7 个值只给 O7 用，这里已去掉。

控制台里有些预设只发手指帧、不发掌部帧（palm 为 None），这时掌部 4 个关节
保持当前值不变；动画里的 None 同理。本模块保持这个行为。

所有数值原样照搬控制台，未做修改。控制台把掌部预设标注为“原 L10”，
如果你的手是齿轮拇指版本，个别动作的拇指/侧摆效果可能和控制台演示不同。
"""

from __future__ import annotations

from collections.abc import Sequence

Pose = tuple[int, ...]

# ---------------------------------------------------------------- 控制台原始数据
# 手指帧（6 值）：拇指弯曲、拇指翻滚、食指、中指、无名指、小指
DASHBOARD_FINGER = {
    "FIST": (64, 64, 64, 64, 64, 64),
    "OPEN": (192, 192, 192, 192, 192, 192),
    "THUMBSUP": (255, 255, 0, 0, 0, 0),
    "POINT": (0, 0, 255, 0, 0, 0),
    "YO": (255, 255, 255, 0, 0, 255),
    "GUN": (255, 255, 255, 255, 0, 0),
    "WAVE": (40, 60, 80, 100, 120, 140),
    "PINCH": (114, 63, 136, 0, 0, 0),
    "OK": (124, 31, 132, 255, 255, 255),
    "BIG_FIST": (0, 85, 0, 0, 0, 0),
    "BIG_OPEN": (255, 255, 255, 255, 255, 255),
    "YEAH": (0, 103, 255, 255, 0, 0),
    "ONE": (0, 180, 255, 0, 0, 0),
    "TWO": (0, 180, 255, 255, 0, 0),
    "THREE": (0, 180, 255, 255, 255, 0),
    "FOUR": (0, 180, 255, 255, 255, 255),
    "FIVE": (255, 255, 255, 255, 255, 255),
    "SIX": (255, 255, 0, 0, 0, 255),
    "SEVEN": (120, 137, 130, 109, 0, 0),
    "EIGHT": (216, 240, 255, 36, 41, 46),
    "NINE": (0, 255, 159, 0, 0, 0),
}

# 掌部帧（4 值）：食指侧摆、无名指侧摆、小指侧摆、拇指旋转
DASHBOARD_PALM = {
    "PALM_LEFT": (48, 48, 48, 48),
    "PALM_RIGHT": (208, 208, 208, 208),
    "PALM_NEUTRAL": (128, 128, 128, 128),
    "PALM_GUN": (0, 0, 0, 128),
    "PALM_PINCH": (255, 163, 255, 127),
    "PALM_OK": (255, 163, 255, 127),
    "PALM_BIG_FIST": (255, 235, 128, 128),
    "PALM_BIG_OPEN": (128, 128, 128, 128),
    "PALM_YEAH": (255, 235, 128, 128),
    "PALM_ONE": (255, 109, 255, 118),
    "PALM_TWO": (255, 109, 255, 118),
    "PALM_THREE": (255, 109, 255, 118),
    "PALM_FOUR": (255, 109, 255, 118),
    "PALM_FIVE": (255, 109, 255, 118),
    "PALM_SIX": (255, 255, 255, 255),
    "PALM_SEVEN": (255, 200, 199, 76),
    "PALM_EIGHT": (106, 200, 199, 76),
    "PALM_NINE": (255, 38, 195, 51),
}

# ---------------------------------------------------------------- 预设动作
# (键, 名称, 分组, 手指预设, 掌部预设或 None)；按钮与掌部的搭配照搬控制台 setupPresetButtons
PRESET_TABLE = (
    ("fist", "握拳", "常用", "FIST", None),
    ("open", "张开", "常用", "OPEN", None),
    ("pinch", "捏取", "常用", "PINCH", "PALM_PINCH"),
    ("point", "食指指点", "常用", "POINT", None),
    ("yeah", "Yeah", "常用", "YEAH", "PALM_YEAH"),
    ("thumbs_up", "竖起大拇指", "常用", "THUMBSUP", None),
    ("wave_pose", "手指波浪", "常用", "WAVE", None),
    ("big_fist", "紧握拳", "常用", "BIG_FIST", "PALM_BIG_FIST"),
    ("big_open", "完全张开", "常用", "BIG_OPEN", "PALM_BIG_OPEN"),
    ("yo", "Yo!", "常用", "YO", None),
    ("gun", "PONG", "常用", "GUN", "PALM_GUN"),
    ("ok", "OK!", "常用", "OK", "PALM_OK"),
    ("reset", "重置", "常用", "OPEN", "PALM_NEUTRAL"),  # 控制台“重置所有手部”
    ("num1", "1", "数字", "ONE", "PALM_ONE"),
    ("num2", "2", "数字", "TWO", "PALM_TWO"),
    ("num3", "3", "数字", "THREE", "PALM_THREE"),
    ("num4", "4", "数字", "FOUR", "PALM_FOUR"),
    ("num5", "5", "数字", "FIVE", "PALM_FIVE"),
    ("num6", "6", "数字", "SIX", "PALM_SIX"),
    ("num7", "7", "数字", "SEVEN", "PALM_SEVEN"),
    ("num8", "8", "数字", "EIGHT", "PALM_EIGHT"),
    ("num9", "9", "数字", "NINE", "PALM_NINE"),
    ("palm_left", "掌部向左", "掌部", None, "PALM_LEFT"),
    ("palm_neutral", "掌部中立", "掌部", None, "PALM_NEUTRAL"),
    ("palm_right", "掌部向右", "掌部", None, "PALM_RIGHT"),
)

# ---------------------------------------------------------------- 真机调整
# 在这只手上实际调好的完整 10 关节姿态，覆盖同名预设（上面的控制台原始数据保持不变）。
# 顺序：拇指弯曲、拇指翻滚、食指、中指、无名指、小指、食指侧摆、无名指侧摆、小指侧摆、拇指旋转
USER_OVERRIDES = {
    # 2026-11-26 按网页控制台截图调整；控制台原值为手指 PINCH + 掌部 PALM_PINCH
    "pinch": (136, 106, 121, 0, 0, 0, 255, 163, 255, 220),
    # 2026-11-26 按截图调整（第二次：中指/无名指/小指改为 0）；
    # 控制台原值为 EIGHT + PALM_EIGHT（拇指翻滚 240、中/无名/小指 36/41/46、拇指旋转 76）
    "num8": (216, 255, 255, 0, 0, 0, 106, 200, 199, 255),
    # 2026-11-26 按截图调整；控制台原值为 SEVEN + PALM_SEVEN（拇指弯曲 120、拇指旋转 76）
    "num7": (159, 137, 130, 109, 0, 0, 255, 200, 199, 152),
}

# ---------------------------------------------------------------- 动画
# 每一步是 (手指 6 值或 None, 掌部 4 值或 None)。interval_ms 为控制台里对应按钮的间隔。
_NEUTRAL = DASHBOARD_PALM["PALM_NEUTRAL"]
_OPEN6 = (255, 255, 255, 255, 255, 255)
_FIST6 = (64, 64, 64, 64, 64, 64)
_MID6 = (128, 128, 128, 128, 128, 128)

ANIMATION_TABLE = {
    "numbers": {
        "label": "数字倒计时 9→0",
        "source": "同步循环数字动画（循环数字动画为 200 ms）",
        "interval_ms": 700,
        # 每步都是一个数字手势：使用 USER_OVERRIDES 里的真机调整值，并按防干涉过渡分段运动
        "staged": True,
        "step_presets": ("num9", "num8", "num7", "num6", "num5", "num4", "num3", "num2", "num1", None),
        "steps": (
            ((0, 255, 159, 0, 0, 0), (255, 38, 195, 51)),
            ((216, 240, 255, 36, 41, 46), (106, 200, 199, 76)),
            ((120, 137, 130, 109, 0, 0), (255, 200, 199, 76)),
            ((255, 255, 0, 0, 0, 255), (255, 255, 255, 255)),
            ((255, 255, 255, 255, 255, 255), (255, 109, 255, 118)),
            ((0, 180, 255, 255, 255, 255), (255, 109, 255, 118)),
            ((0, 180, 255, 255, 255, 0), (255, 109, 255, 118)),
            ((0, 180, 255, 255, 0, 0), (255, 109, 255, 118)),
            ((0, 180, 255, 0, 0, 0), (255, 109, 255, 118)),
            ((64, 64, 64, 64, 64, 64), (128, 128, 128, 128)),
        ),
    },
    "count_up": {
        "label": "数字正计时 1→9",
        "source": "本项目新增：数字 1 到 9 依次比出（与倒计时同一组手势）",
        "interval_ms": 700,
        # 同倒计时：使用 USER_OVERRIDES 里的真机调整值（数字 7、8），并按防干涉过渡分段运动
        "staged": True,
        "step_presets": ("num1", "num2", "num3", "num4", "num5", "num6", "num7", "num8", "num9"),
        "steps": tuple(
            (DASHBOARD_FINGER[finger], DASHBOARD_PALM[f"PALM_{finger}"])
            for finger in ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE")
        ),
    },
    "mexican": {
        "label": "墨西哥波浪",
        "source": "循环墨西哥波浪动画",
        "interval_ms": 100,
        "steps": tuple(
            (fingers, _NEUTRAL)
            for fingers in (
                (64, 64, 64, 64, 64, 64),
                (255, 255, 64, 64, 64, 64),
                (255, 255, 128, 64, 64, 64),
                (255, 255, 255, 128, 64, 64),
                (255, 255, 255, 255, 128, 64),
                (255, 255, 255, 255, 255, 128),
                (255, 255, 255, 255, 255, 255),
                (255, 255, 255, 255, 255, 255),
                (255, 255, 255, 255, 255, 255),
                (255, 255, 255, 255, 255, 128),
                (255, 255, 255, 255, 128, 64),
                (255, 255, 255, 128, 64, 64),
                (255, 255, 128, 64, 64, 64),
                (255, 255, 128, 64, 64, 64),
                (128, 128, 64, 64, 64, 64),
            )
        ),
    },
    "finger_wave": {
        "label": "手指波浪（张开-握拳）",
        "source": "循环手指波浪",
        "interval_ms": 300,
        "steps": ((_OPEN6, _NEUTRAL), (_FIST6, _NEUTRAL), (_OPEN6, _NEUTRAL)),
    },
    "wave": {
        "label": "波浪（张开-中间-握拳）",
        "source": "同步循环波浪",
        "interval_ms": 300,
        "steps": ((_OPEN6, _NEUTRAL), (_MID6, _NEUTRAL), (_FIST6, _NEUTRAL), (_MID6, _NEUTRAL)),
    },
    "ripple": {
        "label": "逐指波浪",
        "source": "波浪动画（只发手指帧）",
        "interval_ms": 500,
        # 先逐指张开：其余 192、当前指 64；再逐指握拳：其余 64、当前指 192
        "steps": tuple(
            (tuple(64 if i == idx else 192 for i in range(6)), None) for idx in range(6)
        )
        + tuple((tuple(192 if i == idx else 64 for i in range(6)), None) for idx in range(6)),
    },
    "sway": {
        "label": "横向摆动",
        "source": "横向摆动（只发掌部帧）",
        "interval_ms": 500,
        "steps": ((None, DASHBOARD_PALM["PALM_LEFT"]), (None, DASHBOARD_PALM["PALM_RIGHT"])),
    },
    "refill_core": {
        "label": "Refill Core（拇指依次对指）",
        "source": "Refill Core 按钮（L10 序列：食→中→无名→小→无名→中）",
        "interval_ms": 100,
        "steps": (
            ((149, 30, 145, 36, 41, 46), (246, 188, 128, 128)),
            ((138, 80, 0, 154, 41, 46), (246, 155, 154, 66)),
            ((140, 80, 0, 15, 155, 46), (246, 155, 154, 40)),
            ((140, 62, 0, 15, 29, 143), (246, 155, 154, 25)),
            ((140, 80, 0, 15, 155, 46), (246, 155, 154, 40)),
            ((138, 80, 0, 154, 41, 46), (246, 155, 154, 66)),
        ),
    },
    "thumbs_up": {
        "label": "竖拇指",
        "source": "控制台内置（多手依次演示）",
        "interval_ms": 400,
        "steps": (((255, 255, 0, 0, 0, 0), _NEUTRAL), (_MID6, _NEUTRAL)),
    },
    "point": {
        "label": "食指指点",
        "source": "控制台内置（多手依次演示）",
        "interval_ms": 350,
        "steps": (((0, 0, 255, 0, 0, 0), _NEUTRAL), (_MID6, _NEUTRAL)),
    },
    "fist_open": {
        "label": "握拳-张开",
        "source": "控制台内置（多手依次演示）",
        "interval_ms": 400,
        "steps": ((_FIST6, _NEUTRAL), (_OPEN6, _NEUTRAL), (_MID6, _NEUTRAL)),
    },
}


# ---------------------------------------------------------------- 工具函数
def merge(current: Sequence[int], fingers: Sequence[int] | None, palm: Sequence[int] | None) -> Pose:
    """把手指 6 值 / 掌部 4 值合并进当前 10 关节姿态；None 表示该部分保持不变。"""
    pose = [int(v) for v in current]
    if len(pose) != 10:
        raise ValueError(f"当前姿态需要 10 个值，实际 {len(pose)} 个")
    if fingers is not None:
        if len(fingers) != 6:
            raise ValueError("手指帧需要 6 个值")
        pose[0:6] = [int(v) for v in fingers]
    if palm is not None:
        if len(palm) != 4:
            raise ValueError("掌部帧需要 4 个值")
        pose[6:10] = [int(v) for v in palm]
    return tuple(pose)


def preset_parts(key: str) -> tuple[Pose | None, Pose | None]:
    """返回预设的 (手指 6 值或 None, 掌部 4 值或 None)；有真机调整值时优先用它。"""
    if key in USER_OVERRIDES:
        pose = tuple(int(v) for v in USER_OVERRIDES[key])
        if len(pose) != 10:
            raise ValueError(f"USER_OVERRIDES[{key!r}] 需要 10 个值")
        return pose[:6], pose[6:]
    for preset_key, _label, _group, finger_key, palm_key in PRESET_TABLE:
        if preset_key == key:
            fingers = DASHBOARD_FINGER[finger_key] if finger_key else None
            palm = DASHBOARD_PALM[palm_key] if palm_key else None
            return fingers, palm
    raise KeyError(f"没有这个预设：{key}")


def preset_pose(key: str, current: Sequence[int]) -> Pose:
    """把预设应用到当前姿态，得到要发送的 10 关节目标。"""
    fingers, palm = preset_parts(key)
    return merge(current, fingers, palm)


def animation_steps(key: str) -> tuple[tuple[Pose | None, Pose | None], ...]:
    """动画的实际步骤：对应预设有真机调整值（USER_OVERRIDES）时用调整值替换该步。"""
    spec = ANIMATION_TABLE[key]
    steps = list(spec["steps"])
    for index, preset_key in enumerate(spec.get("step_presets", ())):
        if preset_key in USER_OVERRIDES:
            steps[index] = preset_parts(preset_key)
    return tuple(steps)


def catalog() -> dict:
    """给网页用的动作清单。"""
    presets = []
    for key, label, group, _finger_key, _palm_key in PRESET_TABLE:
        fingers, palm = preset_parts(key)
        presets.append(
            {
                "key": key,
                "label": label,
                "group": group,
                "fingers": fingers,
                "palm": palm,
                "custom": key in USER_OVERRIDES,
            }
        )
    animations = [
        {
            "key": key,
            "label": spec["label"],
            "source": spec["source"],
            "interval_ms": spec["interval_ms"],
            "steps": len(spec["steps"]),
            "staged": bool(spec.get("staged")),
        }
        for key, spec in ANIMATION_TABLE.items()
    ]
    return {"presets": presets, "animations": animations}
