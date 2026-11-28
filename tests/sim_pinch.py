"""离线测捏取辅助（食指 / 中指 / 三指捏）：映射是否对准、握拳等不误触发、防撞放宽是否按被捏的手指。不碰真机。"""

import contextlib
import io
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: E402,F401  仓库根目录加进 sys.path；数据目录换成临时目录
from l10ctl import config as cfg  # noqa: E402
from l10ctl.mirror import PINCH_TARGETS, MirrorGuard, MirrorMapper, classify_rps, hand_features  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):  # test_mirror 导入时会跑它自己的测试，这里不看输出
    sys.stdout.reconfigure = lambda **k: None  # type: ignore[attr-defined]
    import sim_mirror as tm

FAILS = []
TIP = {"index": 8, "middle": 12}


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def pinch_hand(finger="index", gap=0.008, mcp=35):
    """被捏的手指半弯、其余伸直，拇指尖放到它的指尖旁边（gap 米）。"""
    curl = [0, 0, 0, 0]
    curl[("index", "middle").index(finger)] = mcp
    p = tm.make_hand(curl=tuple(curl), thumb="fist")
    tip = p[TIP[finger]] + np.array([0.0, 0.0, gap])
    p[4] = tip
    p[3] = p[2] + 0.6 * (tip - p[2])
    return p


def tripod_hand():
    """食指、中指都半弯，拇指尖放在两个指尖中间。"""
    p = tm.make_hand(curl=(35, 35, 0, 0), thumb="fist")
    tip = (p[8] + p[12]) / 2 + np.array([0.0, 0.0, 0.008])
    p[4] = tip
    p[3] = p[2] + 0.6 * (tip - p[2])
    return p


mapper = MirrorMapper()
print("== 捏取识别")
cases = {
    "拇指捏食指": pinch_hand("index"),
    "拇指捏中指": pinch_hand("middle"),
    "快捏上食指（相距 25 mm）": pinch_hand("index", 0.025),
    "三指捏": tripod_hand(),
    "张开": tm.make_hand(),
    "握拳": tm.make_hand(curl=(80, 80, 80, 80), thumb="fist"),
    "剪刀（拇指压无名指小指）": tm.make_hand(curl=(0, 0, 80, 80), thumb="fist"),
    "拇指碰小指根": tm.make_hand(thumb="pinky"),
}
result = {}
for name, hand in cases.items():
    f = hand_features(hand)
    result[name] = mapper.pinch(f)
    finger, amount = result[name]
    print(f"  {name:16s} 到食指尖 {f['pinch_index']:.2f} 到中指尖 {f['pinch_middle']:.2f}（掌宽）→ {finger} {amount:.2f}")
check(result["拇指捏食指"][0] == "index" and result["拇指捏食指"][1] > 0.95, "捏食指：识别为食指，程度接近 1")
check(result["拇指捏中指"][0] == "middle" and result["拇指捏中指"][1] > 0.95, "捏中指：识别为中指，程度接近 1")
check(0 < result["快捏上食指（相距 25 mm）"][1] < result["拇指捏食指"][1], "距离越近程度越大（平滑过渡）")
for name in ("张开", "握拳", "剪刀（拇指压无名指小指）", "拇指碰小指根"):
    check(result[name] == (None, 0.0), f"{name}：不触发捏取")

print("== 映射")
for name, finger in (("拇指捏食指", "index"), ("拇指捏中指", "middle")):
    raw = mapper.map(hand_features(cases[name]))
    pose, joints = PINCH_TARGETS[finger]
    print(f"  {name}：映射 {[round(v) for v in raw]}")
    check(all(abs(raw[j] - pose[j]) <= 3 for j in joints), f"{name}：拇指和{finger}对准调好的姿态 {[pose[j] for j in joints]}")
    others = [j for j in (2, 3, 4, 5) if j not in joints]
    check(all(raw[j] > 240 for j in others), f"{name}：其余手指照常跟随（伸直）")
raw = mapper.map(hand_features(cases["三指捏"]))
ip, mp = PINCH_TARGETS["index"][0], PINCH_TARGETS["middle"][0]
print(f"  三指捏：映射 {[round(v) for v in raw]}")
check(abs(raw[2] - ip[2]) <= 3 and abs(raw[3] - mp[3]) <= 3, "三指捏：食指、中指都对准各自的捏取弯曲")
check(min(ip[0], mp[0]) - 1 <= raw[0] <= max(ip[0], mp[0]) + 1, f"三指捏：拇指弯曲在两组之间（{raw[0]:.0f}）")
fist_raw = mapper.map(hand_features(cases["握拳"]))
check(all(fist_raw[j] < 5 for j in (2, 3, 4, 5)), "握拳映射不受影响")
check(classify_rps(mapper.progress(hand_features(cases["握拳"]))) == "rock", "握拳仍然识别成石头")


class Sim:
    def __init__(self, pose, rate=14):
        self.m = np.array(pose, float)
        self.rate = rate

    def step(self, cmd):
        self.m = self.m + np.clip(np.array(cmd, float) - self.m, -self.rate, self.rate)
        return tuple(int(round(v)) for v in self.m)


def time_to_reach(target, finger, pinch, seconds=3.0):
    guard, sim = MirrorGuard(), Sim(cfg.POSES["open"])
    measured = tuple(int(v) for v in sim.m)
    for k in range(10):  # 实际使用时控制循环一直在跑：先在张开状态静止一会儿
        guard.step(cfg.POSES["open"], measured, -0.4 + k * 0.04)
    flex = {"index": 2, "middle": 3}[finger]
    t, reached, phases = 0.0, None, set()
    while t < seconds:
        cmd = guard.step(target, measured, t, None, pinch=pinch, pinch_finger=finger if pinch else None)
        phases.add(guard.phase)
        measured = sim.step(cmd)
        if reached is None and abs(measured[0] - target[0]) <= 20 and abs(measured[flex] - target[flex]) <= 20:
            reached = t
        t = round(t + cfg.MIRROR_LOOP_DT_S, 6)
    return reached, phases, measured


print("== 跟随（模拟电机，从张开开始）")
for name, finger in (("拇指捏食指", "index"), ("拇指捏中指", "middle")):
    target = tuple(mapper.map(hand_features(cases[name])))
    old_t, _, _ = time_to_reach(target, finger, 0.0)
    new_t, phases, final = time_to_reach(target, finger, 1.0)
    print(f"  {name}：不放宽 {old_t * 1000:.0f} ms → 放宽 {new_t * 1000:.0f} ms，阶段 {sorted(phases)}")
    check(new_t < old_t and "fingers" not in phases, f"{name}：拇指和被捏的手指同时动，更快到位")
    check(max(abs(a - b) for a, b in zip(final, target)) <= 1, f"{name}：最终到达")

print("== 捏中指时食指要大幅弯下 → 照样拦住拇指")
guard = MirrorGuard()
target = list(mapper.map(hand_features(cases["拇指捏中指"])))
target[2] = 0
guard.step(target, cfg.POSES["open"], 0.0, None, pinch=1.0, pinch_finger="middle")
check(guard.phase == "fingers", "没被捏的手指大幅运动时，拇指仍先不进掌心")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
