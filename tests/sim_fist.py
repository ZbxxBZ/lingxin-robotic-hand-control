"""离线测握拳辅助：握拳时拇指带到握拳预设，竖拇指 / 捏取 / 张开不受影响。不碰真机。"""

import contextlib
import io
import json
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: E402,F401  仓库根目录加进 sys.path；数据目录换成临时目录
from l10ctl import config as cfg  # noqa: E402
from l10ctl import mirror as lm  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):
    sys.stdout.reconfigure = lambda **k: None  # type: ignore[attr-defined]
    import sim_mirror as tm

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def mappers():
    """默认映射，以及用合成手做完三项标定的映射（对应你已经标定过的情况）。"""
    default = lm.MirrorMapper()
    calibrated = lm.MirrorMapper()
    calibrated.calibrate("open", lm.hand_features(tm.make_hand()))
    calibrated.calibrate("fist", lm.hand_features(tm.make_hand(curl=(80, 80, 80, 80), thumb="fist")))
    calibrated.calibrate("thumb", lm.hand_features(tm.make_hand(thumb="pinky")))
    return {"默认映射": default, "标定后": calibrated}


hands = {
    "握拳": tm.make_hand(curl=(80, 80, 80, 80), thumb="fist"),
    "半握拳": tm.make_hand(curl=(60, 60, 60, 60), thumb="fist"),
    "竖大拇指": tm.make_hand(curl=(80, 80, 80, 80), thumb="open"),
    "张开": tm.make_hand(),
    "剪刀": tm.make_hand(curl=(0, 0, 80, 80), thumb="fist"),
}
thumb = cfg.MIRROR_FIST_THUMB_JOINTS
target = [lm.FIST_THUMB[j] for j in thumb]
print(f"握拳预设的拇指（弯曲, 翻滚, 旋转）= {target}")
for label, mapper in mappers().items():
    print(f"== {label}")
    for name, hand in hands.items():
        feats = lm.hand_features(hand)
        prog = mapper.progress(feats)
        cfg.MIRROR_FIST_ASSIST, guard_curl = False, cfg.MIRROR_THUMB_GUARD_CURL
        cfg.MIRROR_THUMB_GUARD_CURL = (1.5, 2.0)  # “改前”：也关掉四指弯着时的拇指限位
        before = mapper.map(feats)
        cfg.MIRROR_FIST_ASSIST, cfg.MIRROR_THUMB_GUARD_CURL = True, guard_curl
        after = mapper.map(feats)
        amount = lm.fist_amount(prog)
        print(f"  {name:6s} 握拳程度 {amount:.2f}  拇指 改前 {[round(before[j]) for j in thumb]} → 改后 {[round(after[j]) for j in thumb]}")
        if name == "握拳":
            check(all(abs(after[j] - lm.FIST_THUMB[j]) <= 3 for j in thumb), f"{label}：握拳时拇指带到握拳预设位置")
            check(before[9] < 160, f"{label}：改前握拳的拇指旋转偏小（{before[9]:.0f}），就是插进手指卡住的原因")
            check(all(after[j] < 5 for j in (2, 3, 4, 5)), f"{label}：四指照常握紧")
        if name in ("竖大拇指", "张开"):
            check(amount == 0 and all(abs(after[j] - before[j]) < 1e-9 for j in range(10)), f"{label}：{name}不受影响")
        if name == "剪刀":
            check(all(abs(after[j] - before[j]) < 1e-9 for j in range(2, 9)), f"{label}：剪刀的四指不受影响")
            check(all(after[j] >= before[j] - 1e-9 for j in thumb), f"{label}：剪刀的拇指只会更靠外、不会更深")
        if name == "半握拳":
            check(0 <= amount <= 1, f"{label}：半握拳时平滑过渡（{amount:.2f}）")

print("== 捏取不受影响")
mapper = lm.MirrorMapper()
p = tm.make_hand(curl=(35, 0, 0, 0), thumb="fist")
p[4] = p[8] + np.array([0.0, 0.0, 0.008])
p[3] = p[2] + 0.6 * (p[4] - p[2])
f = lm.hand_features(p)
check(lm.fist_amount(mapper.progress(f)) == 0, "捏食指时握拳辅助为 0")


class Sim:
    def __init__(self, pose, rate=14):
        self.m = np.array(pose, float)
        self.rate = rate

    def step(self, cmd):
        self.m = self.m + np.clip(np.array(cmd, float) - self.m, -self.rate, self.rate)
        return tuple(int(round(v)) for v in self.m)


print("== 跟随：张开 → 握拳 → 张开（模拟电机）")
fist_target = lm.MirrorMapper().map(lm.hand_features(hands["握拳"]))
open_target = lm.MirrorMapper().map(lm.hand_features(hands["张开"]))
guard, sim = lm.MirrorGuard(), Sim(cfg.POSES["open"])
measured, t, seq = tuple(int(v) for v in sim.m), 0.0, []
for target_pose, seconds in ((fist_target, 3.0), (open_target, 3.0)):
    end = t + seconds
    while t < end:
        cmd = guard.step(target_pose, measured, t)
        if not seq or seq[-1] != guard.phase:
            seq.append(guard.phase)
        measured = sim.step(cmd)
        t = round(t + cfg.MIRROR_LOOP_DT_S, 6)
    if target_pose is fist_target:
        print(f"  握拳到位：{list(measured)}")
        check(all(abs(measured[j] - lm.FIST_THUMB[j]) <= 2 for j in thumb), "握拳时拇指到达握拳预设位置")
print(f"  张开到位：{list(measured)}；阶段 {' → '.join(seq)}")
check(min(measured[:6]) >= 250, "能正常张开")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
