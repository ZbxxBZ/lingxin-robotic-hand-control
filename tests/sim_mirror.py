"""离线测试：合成手部关键点 → 特征 / 映射 / 手势；模拟电机 → MirrorGuard 防撞与卸力。不碰真机。"""

import math
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: E402,F401  仓库根目录加进 sys.path；数据目录换成临时目录
from l10ctl import config as cfg  # noqa: E402
from l10ctl.mirror import (  # noqa: E402
    MirrorGuard,
    MirrorMapper,
    OneEuroFilter,
    classify_rps,
    hand_features,
    judge,
    thumb_folded,
)

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------- 合成手
def finger_chain(mcp, spread_deg, mcp_deg, pip_deg, dip_deg, lengths=(0.045, 0.027, 0.02)):
    """手指在掌面（x 横向、y 向前）内，绕掌面法线 z 旋转 spread，向 +z（掌心侧）弯曲。"""
    phi = math.radians(spread_deg)
    d0 = np.array([math.sin(phi), math.cos(phi), 0.0])
    n = np.array([0.0, 0.0, 1.0])
    pts = [np.array(mcp, float)]
    alpha = 0.0
    for ang, length in zip((mcp_deg, pip_deg, dip_deg), lengths):
        alpha += math.radians(ang)
        d = math.cos(alpha) * d0 + math.sin(alpha) * n
        pts.append(pts[-1] + length * d)
    return pts  # mcp, pip, dip, tip


def thumb_chain(cmc, meta_dir, bend_dir, mcp_deg, ip_deg, lengths=(0.04, 0.032, 0.025)):
    meta = np.array(meta_dir, float)
    meta /= np.linalg.norm(meta)
    w = np.array(bend_dir, float)
    w = w - np.dot(w, meta) * meta
    w /= np.linalg.norm(w)
    cmc = np.array(cmc, float)
    mcp = cmc + lengths[0] * meta
    a1 = math.radians(mcp_deg)
    d1 = math.cos(a1) * meta + math.sin(a1) * w
    ip = mcp + lengths[1] * d1
    a2 = a1 + math.radians(ip_deg)
    d2 = math.cos(a2) * meta + math.sin(a2) * w
    tip = ip + lengths[2] * d2
    return [cmc, mcp, ip, tip]


MCPS = {"index": (0.03, 0.09, 0), "middle": (0.008, 0.095, 0), "ring": (-0.012, 0.09, 0), "pinky": (-0.03, 0.08, 0)}


def make_hand(curl=(0, 0, 0, 0), spread=(8, 0, -6, -14), thumb="open"):
    """curl：四指 (MCP 角) ，PIP = 1.1×MCP，DIP = 0.6×MCP。spread：各指在掌面内朝 +x 的角度。"""
    p = np.zeros((21, 3))
    if thumb == "open":
        th = thumb_chain((0.022, 0.02, 0.005), (0.8, 0.6, 0.15), (0, 0, 1), 8, 8)
    elif thumb == "fist":  # 拇指压在食指中指外面
        th = thumb_chain((0.022, 0.02, 0.01), (0.35, 0.6, 0.7), (-1, 0.2, 0.3), 35, 30)
    elif thumb == "pinky":  # 拇指尖到小指根
        th = thumb_chain((0.022, 0.02, 0.01), (-0.2, 0.55, 0.8), (-1, 0.3, -0.2), 30, 25)
    else:
        raise ValueError(thumb)
    p[1:5] = th
    for i, name in enumerate(("index", "middle", "ring", "pinky")):
        m = curl[i]
        pts = finger_chain(MCPS[name], spread[i], m, 1.1 * m, 0.6 * m)
        p[5 + 4 * i : 9 + 4 * i] = pts
    return p


def random_rotation(rng):
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    a, b, c, d = q
    return np.array(
        [
            [a * a + b * b - c * c - d * d, 2 * (b * c - a * d), 2 * (b * d + a * c)],
            [2 * (b * c + a * d), a * a - b * b + c * c - d * d, 2 * (c * d - a * b)],
            [2 * (b * d - a * c), 2 * (c * d + a * b), a * a - b * b - c * c + d * d],
        ]
    )


print("== 特征")
open_hand = make_hand()
fist_hand = make_hand(curl=(80, 80, 80, 80), thumb="fist")
f_open, f_fist = hand_features(open_hand), hand_features(fist_hand)
for k in f_open:
    print(f"  {k:14s} 张开 {f_open[k]:8.2f}   握拳 {f_fist[k]:8.2f}")
check(f_open["index_curl"] < 15 and abs(f_fist["index_curl"] - 168) < 1.5, "四指弯曲角：张开 < 15°，握拳 ≈ MCP + PIP（80 + 88）")
check(abs(f_open["index_spread"] - 8) < 0.5 and abs(f_open["ring_spread"] - 6) < 0.5 and abs(f_open["pinky_spread"] - 8) < 0.5,
      "侧摆角恢复（食指 8°、无名指 6°、小指相对无名指 8°）")
check(abs(f_open["thumb_bend"] - 16) < 1 and abs(f_fist["thumb_bend"] - 65) < 1, "拇指弯曲角 = MCP + IP")
check(f_fist["thumb_front"] > f_open["thumb_front"] + 30, "握拳时拇指掌骨转到掌心前方（角度明显变大）")
f_pinky = hand_features(make_hand(thumb="pinky"))
check(f_pinky["thumb_across"] < f_fist["thumb_across"] < f_open["thumb_across"], "拇指横移：张开 > 握拳 > 碰小指根")
print(f"  拇指碰小指根：across {f_pinky['thumb_across']:.2f} front {f_pinky['thumb_front']:.2f}")

rng = np.random.default_rng(1)
mirror = open_hand * np.array([-1, 1, 1])
same = all(abs(hand_features(mirror)[k] - f_open[k]) < 1e-6 for k in f_open)
check(same, "镜像（另一只手）特征完全相同")
R = random_rotation(rng)
rot = (fist_hand - fist_hand[0]) @ R.T + np.array([0.1, -0.2, 0.3])
check(all(abs(hand_features(rot)[k] - f_fist[k]) < 1e-6 for k in f_fist), "整体旋转平移后特征不变")

print("== 映射")
mapper = MirrorMapper()
m_open, m_fist = mapper.map(f_open), mapper.map(f_fist)
print("  张开 →", [round(v) for v in m_open])
print("  握拳 →", [round(v) for v in m_fist])
check(all(m_open[j] > 240 for j in (0, 2, 3, 4, 5)), "张开：五指弯曲关节接近 255")
check(all(m_fist[j] < 25 for j in (2, 3, 4, 5)), "握拳：四指弯曲接近 0")
check(all(abs(m_fist[j] - mapper_neutral) < 8 for j, mapper_neutral in ((6, 128), (7, 67), (8, 89))),
      "握拳：侧摆回到中位（随弯曲回中）")
acc, rej = mapper.calibrate("open", f_open)
acc2, rej2 = mapper.calibrate("fist", f_fist)
acc3, rej3 = mapper.calibrate("thumb", f_pinky)
print(f"  标定 张开 采用 {len(acc)} 拒绝 {rej}；握拳 采用 {len(acc2)} 拒绝 {rej2}；对掌 采用 {len(acc3)} 拒绝 {rej3}")
check(not rej2 and not rej3, "握拳 / 对掌标定全部采用")
m_open2, m_fist2 = mapper.map(f_open), mapper.map(f_fist)
check(all(abs(m_open2[j] - 255) < 1 for j in (0, 1, 2, 3, 4, 5, 9)), "标定后：张开精确映射到 255")
check(all(abs(m_fist2[j] - 0) < 1 for j in (2, 3, 4, 5, 1)), "标定后：握拳四指和拇指翻滚精确映射到 0")
check(abs(m_fist2[0] - cfg.POSES["fist"][0]) < 1 and abs(m_fist2[9] - cfg.POSES["fist"][9]) < 1, "标定后：握拳时拇指按握拳辅助带到握拳预设（弯曲 90、旋转 197）")
bad = MirrorMapper()
try:
    bad.calibrate("fist", f_open)  # 摆错姿势：张开时按了握拳
    check(False, "姿势摆错时拒绝采用")
except ValueError as e:
    check(bad.points == MirrorMapper().points, f"姿势摆错时整体拒绝且不改动（{e}）")
only_open = MirrorMapper()
only_open.calibrate("open", f_open)
mo = only_open.map(f_open)
check(all(mo[j] > 250 for j in (0, 1, 2, 3, 4, 5, 9)), "只标定张开：张开仍映射到 255（另一端被推开）")
import json
re = MirrorMapper(json.loads(json.dumps(mapper.to_json())))
check(re.points == mapper.points and re.marked == mapper.marked and re.done == mapper.done, "标定保存 / 读取一致")

print("== 手势")
mapper = MirrorMapper()
g = lambda hand: classify_rps(mapper.progress(hand_features(hand)))
check(g(open_hand) == "paper", "张开 = 布")
check(g(fist_hand) == "rock", "握拳 = 石头")
check(g(make_hand(curl=(0, 0, 80, 80), thumb="fist")) == "scissors", "食指中指伸直、其余弯曲 = 剪刀")
check(g(make_hand(curl=(0, 80, 80, 80))) is None, "只伸食指：不算三种手势")
check(judge("rock", "scissors") == "human" and judge("rock", "paper") == "robot" and judge("paper", "paper") == "draw", "胜负判定")

print("== 滤波")
flt = OneEuroFilter(cfg.MIRROR_FILTER_MIN_CUTOFF, cfg.MIRROR_FILTER_BETA)
noise = rng.normal(0, 6, size=(150, 1))
out = [flt([128 + n[0]], t * 0.033)[0] for t, n in enumerate(noise)]
check(np.std(out[30:]) < np.std(noise[30:]) * 0.5, f"静止抖动从 ±{np.std(noise):.1f} 降到 ±{np.std(out[30:]):.1f}")
flt.reset()
step = [flt([0 if t < 10 else 255], t * 0.033)[0] for t in range(40)]
t90 = next(i for i, v in enumerate(step) if v > 230) - 10
check(t90 * 33 < 400, f"阶跃到 90% 用时 {t90 * 33} ms")


# ---------------------------------------------------------------- 模拟电机
class SimHand:
    """一阶追踪 + 可选卡阻：拇指折着时被压的手指不能动；被挡关节停在某处。"""

    def __init__(self, pose, rate=14, jam=True):
        self.m = np.array(pose, float)
        self.cmd = np.array(pose, float)
        self.rate = rate
        self.jam = jam
        self.blocks = {}  # 关节 → 最低能到的值（被挡住）
        self.jam_ticks = 0

    def step(self):
        folded = thumb_folded(self.m)
        new = self.m + np.clip(self.cmd - self.m, -self.rate, self.rate)
        for j, lo in self.blocks.items():
            new[j] = max(new[j], lo) if self.cmd[j] < self.m[j] else new[j]
        if self.jam and folded:
            moved = np.abs(new[2:6] - self.m[2:6]) > 0.5
            if moved.any():
                self.jam_ticks += 1  # 拇指折着时四指在动：真机上会被卡或相撞
        self.m = new
        return tuple(int(round(v)) for v in self.m)


def run(guard, sim, desired, seconds, forces_fn=None, log=None):
    dt = cfg.MIRROR_LOOP_DT_S
    t = 0.0
    thumb_fold_while_busy = 0
    for k in range(int(seconds / dt)):
        measured = tuple(int(round(v)) for v in sim.m)
        forces = forces_fn(sim.m) if forces_fn and k % 3 == 0 else None
        cmd = guard.step(desired, measured, t, forces)
        sim.cmd = np.array(cmd, float)
        # 拇指指令进掌心时，四指必须已经到位
        if thumb_folded(cmd) and max(abs(desired[j] - measured[j]) for j in range(2, 9)) > 40:
            thumb_fold_while_busy += 1
        sim.step()
        if log is not None:
            log.append((round(t, 2), guard.phase, cmd, tuple(int(v) for v in sim.m)))
        t += dt
    return thumb_fold_while_busy


OPEN = cfg.POSES["open"]
FIST = cfg.POSES["fist"]
print("== 防撞：张开 → 握拳")
guard, sim = MirrorGuard(), SimHand(OPEN)
guard.step(OPEN, OPEN, 0.0)
log = []
bad = run(guard, sim, FIST, 3.0, log=log)
phases = [p for _, p, _, _ in log]
first_free = next(i for i, p in enumerate(phases) if p == "free" and i > 0)
print(f"  阶段序列：{phases[0]} ×{phases.index('free', 1)} → free（第 {first_free * 40} ms 拇指才进掌心）")
check(bad == 0, "四指没到位时拇指指令从不进掌心")
check(sim.jam_ticks == 0, "拇指折着时四指从不运动")
check(max(abs(a - b) for a, b in zip(log[-1][3], FIST)) <= 1, "最终到达握拳")

print("== 静止 1 秒后突然握拳（电机起步慢 0.12 s）：不误判堵转")
guard, sim = MirrorGuard(), SimHand(OPEN, jam=False)
t, start_delay = 0.0, 0
for k in range(100):
    target = OPEN if t < 1.0 else FIST
    measured = tuple(int(round(v)) for v in sim.m)
    cmd = guard.step(target, measured, t)
    if abs(t - 1.0) < 1e-6 or (t > 1.0 and start_delay == 0 and not hasattr(sim, "started")):
        start_delay, sim.started = 3, True
    sim.cmd = np.array(cmd, float)
    if start_delay > 0:
        start_delay -= 1
    else:
        sim.step()
    t = round(t + cfg.MIRROR_LOOP_DT_S, 6)
check(not guard.stall_floor, f"没有误判堵转（下限 {guard.stall_floor}）")
check(max(abs(a - b) for a, b in zip(sim.m, FIST)) <= 1, "到达握拳")

print("== 防撞：握拳 → 张开")
guard, sim = MirrorGuard(), SimHand(FIST)
guard.step(FIST, FIST, 0.0)
log = []
run(guard, sim, OPEN, 3.0, log=log)
seq = []
for _, p, _, _ in log:
    if not seq or seq[-1] != p:
        seq.append(p)
print("  阶段：" + " → ".join(seq))
check(seq[0] == "clear", "先让拇指让开（clear），四指原地等")
check(sim.jam_ticks == 0, "拇指折着时四指从不运动")
check(max(abs(a - b) for a, b in zip(log[-1][3], OPEN)) <= 1, "最终到达张开")

print("== 防撞：数字 1 → 2（拇指压着中指，中指要伸直）")
ONE = (0, 180, 255, 0, 0, 0, 255, 109, 255, 118)
TWO = (0, 180, 255, 255, 0, 0, 255, 109, 255, 118)
guard, sim = MirrorGuard(), SimHand(ONE)
guard.step(ONE, ONE, 0.0)
log = []
run(guard, sim, TWO, 4.0, log=log)
check(sim.jam_ticks == 0, "中指在拇指让开后才伸直")
check(max(abs(a - b) for a, b in zip(log[-1][3], TWO)) <= 1, "最终到达数字 2")

print("== 拇指被压住让不开 → 超时后四指直接运动")
guard, sim = MirrorGuard(), SimHand(FIST, jam=False)
sim.rate_thumb_blocked = True
guard.step(FIST, FIST, 0.0)
sim.blocks = {}
orig_step = sim.step


def stuck_step():
    sim.cmd[0], sim.cmd[1] = sim.m[0], sim.m[1]  # 拇指完全动不了
    return orig_step()


sim.step = stuck_step
log = []
run(guard, sim, OPEN, 3.0, log=log)
phases = [p for _, p, _, _ in log]
check("forced" in phases, "出现 forced（不再无限等待）")
t_forced = next(t for t, p, _, _ in log if p == "forced")
check(t_forced <= cfg.MIRROR_CLEAR_STALL_S + 0.3, f"拇指不动 {cfg.MIRROR_CLEAR_STALL_S} s 就放行（实际 {t_forced:.2f} s）")
check(log[-1][3][2] > 240, "四指最终张开（拇指放出来）")

print("== 堵转卸力：握拳时拇指被手指挡在 75，指令 40")
guard, sim = MirrorGuard(), SimHand(OPEN, jam=False)
sim.blocks = {0: 75}
guard.step(OPEN, OPEN, 0.0)
target = (40, 0, 0, 0, 0, 0, 128, 67, 89, 197)
log = []
run(guard, sim, target, 4.0, log=log)
events = guard.take_events()
print("  事件：", events)
final_cmd = log[-1][2]
check(0 in guard.floors and final_cmd[0] >= 75 - cfg.RELIEF_MARGIN, f"拇指指令停在 {final_cmd[0]}（实测 75），不再往 40 顶")
check(all(final_cmd[j] <= 1 for j in (2, 3, 4, 5)), "其他手指照常握紧")
cmd = guard.step((255,) * 6 + (128, 67, 89, 255), log[-1][3], 10.0)
check(0 not in guard.floors, "人手张开后自动解除下限")

print("== 按力卸载：拇指力 120 → 每次读力退 5，力降下来后停止")
guard, sim = MirrorGuard(), SimHand(OPEN, jam=False)
guard.step(OPEN, OPEN, 0.0)
target = (40, 0, 0, 0, 0, 0, 128, 67, 89, 197)


def forces_fn(m):
    # 拇指弯曲低于 90 开始受力，越弯越大
    return {"thumb": max(0.0, (90 - m[0]) * 3.0), "index": 0, "middle": 0, "ring": 0, "pinky": 0}


log = []
run(guard, sim, target, 6.0, forces_fn=forces_fn, log=log)
final = log[-1][3]
force = forces_fn(np.array(final, float))["thumb"]
print(f"  最终拇指弯曲 {final[0]}，拇指力 {force:.0f}；下限 {guard.floors}")
check(force <= cfg.MIRROR_FORCE_LIMIT + 3 * cfg.MIRROR_FORCE_STEP, "拇指力降到限值附近")
check(final[2] <= 1, "其他手指不受影响")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
if __name__ == "__main__":  # 其他测试会 import 这个文件复用合成手
    sys.exit(1 if FAILS else 0)
