"""离线测试握手逻辑（l10ctl.handshake）：模拟电机 + 模拟人手。不碰真机。

模拟人手：blocks = {关节: (接触位置, 指尖力记到哪根手指 或 None)}
  - 记到手指（指尖碰到）：关节可以再往里压最多 MAX_DEPTH（手是软的），指尖力 = K × 压入深度；
  - None（侧面挡住）：关节停在接触位置，没有指尖力，但会记下“顶住的力度”（压入的指令差）用来看疼不疼。
"""

import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: E402,F401  仓库根目录加进 sys.path；数据目录换成临时目录
from l10ctl import config as cfg  # noqa: E402
from l10ctl import handshake as hs  # noqa: E402

FAILS = []
DT = 0.05
K = 2.5
MAX_DEPTH = 30
OFFSET = {"thumb": 3.0, "index": 1.5, "middle": 2.0, "ring": 0.5, "pinky": 4.0}


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class World:
    def __init__(self, rng=0):
        self.m = np.array(cfg.POSES["open"], float)
        self.cmd = self.m.copy()
        self.goal = None
        self.blocks = {}
        self.rng = np.random.default_rng(rng)
        self.extra = dict.fromkeys(hs.FINGER_JOINTS, 0.0)
        self.touch = dict.fromkeys(hs.FINGER_JOINTS, 0.0)
        self.max_push = {}  # 关节 → 侧面顶住时指令比实测更往里的最大量（越大越疼）

    def goto(self, pose):
        self.cmd = np.array(pose, float)
        self.goal = tuple(pose)

    def set_target(self, pose):
        self.cmd = np.array(pose, float)
        self.goal = None

    def motion_busy(self):
        return self.goal is not None and np.abs(self.m - np.array(self.goal)).max() > 3

    def step(self):
        new = self.m + np.clip(self.cmd - self.m, -8, 8)
        for j, (contact, finger) in self.blocks.items():
            floor = contact - (MAX_DEPTH if finger else 0)
            if new[j] < floor:
                new[j] = floor
            if finger is None and self.m[j] <= contact + 1:
                self.max_push[j] = max(self.max_push.get(j, 0.0), contact - self.cmd[j])
        self.m = new

    def measured(self):
        return tuple(int(round(v)) for v in self.m)

    def forces(self):
        out = {}
        for f in hs.FINGER_JOINTS:
            out[f] = OFFSET[f] + self.rng.normal(0, 0.8) + self.touch[f]
        for j, (contact, finger) in self.blocks.items():
            if finger:
                depth = contact - self.m[j]
                if depth > 0:
                    out[finger] += K * depth + self.extra[finger]
        return {f: max(0.0, v) for f, v in out.items()}


def run(world, ctrl, seconds, t0=0.0, on_tick=None):
    t = t0
    for k in range(int(seconds / DT)):
        forces = world.forces() if k % 2 == 0 else None
        ctrl.update(t, forces, world.measured())
        if on_tick:
            on_tick(t)
        world.step()
        t = round(t + DT, 6)
    return t


def net_forces(world):
    return {k: round(max(0.0, v - OFFSET[k]), 1) for k, v in world.forces().items()}


def hand(thumb, index, middle, ring, pinky, thumb_tip=True, tips=True, thumb_roll=None):
    """手的大小：各手指主关节碰到手的位置。thumb_roll：拇指翻滚被手挡住的位置（侧面，无触觉）。"""
    blocks = {0: (thumb, "thumb" if thumb_tip else None)}
    for j, (name, v) in zip((2, 3, 4, 5), (("index", index), ("middle", middle), ("ring", ring), ("pinky", pinky))):
        blocks[j] = (v, name if tips else None)
    if thumb_roll is not None:
        blocks[1] = (thumb_roll, None)
    return blocks


BIG = hand(205, 205, 180, 165, 140)
SMALL = hand(150, 130, 100, 85, 60)
pose = cfg.HANDSHAKE_POSE

print("== 准备与去皮")
world = World()
ctrl = hs.HandshakeController(world, 0.0)
t = run(world, ctrl, 3.0)
check(ctrl.state == "waiting", f"准备到位、记录零点后进入等待（{ctrl.state}）")
check(all(abs(ctrl.baseline[f] - OFFSET[f]) < 1.0 for f in hs.FINGER_JOINTS), "零点记下了各指的偏移")

print("== 大手：碰指尖自动开始；拇指翻滚只动一点，主要靠弯曲")
world.touch["pinky"] = 12.0
t = run(world, ctrl, 0.3, t)
world.touch["pinky"] = 0.0
world.blocks = BIG
track = []
t = run(world, ctrl, 5.0, t, on_tick=lambda tt: track.append((world.cmd[0], world.cmd[1])))
check(ctrl.state == "holding", f"握住了（{ctrl.state}）")
rolls = [r for _f, r in track]
check(min(rolls) >= pose[1] - 1, f"拇指翻滚最低只到握手姿态的 {pose[1]}（实际最低 {min(rolls):.0f}），不往里横扫")
flexes = [f for f, _r in track]
check(max(rolls) - min(rolls) <= 35 and max(flexes) - min(flexes) >= 25,
      f"拇指翻滚只动了 {max(rolls) - min(rolls):.0f}，弯曲动了 {max(flexes) - min(flexes):.0f}（主要靠弯曲）")
flex_started = next((i for i, (f, _r) in enumerate(track) if f < hs.ready_pose()[0] - 1), None)
roll_done = next((i for i, (_f, r) in enumerate(track) if r <= pose[1] + 1), None)
check(flex_started is None or (roll_done is not None and flex_started >= roll_done - 1),
      "拇指翻滚转到位后才开始往下弯")
forces = net_forces(world)
print("  握住时指尖力：", forces, " 停下原因：", ctrl.stopped)
check(forces["thumb"] <= cfg.HANDSHAKE_GRIP_FORCE * cfg.HANDSHAKE_THUMB_FORCE_RATIO + 10,
      f"拇指力 {forces['thumb']} 比四指轻（拇指握力 = {cfg.HANDSHAKE_GRIP_FORCE * cfg.HANDSHAKE_THUMB_FORCE_RATIO:.0f}）")
check(all(forces[f] <= cfg.HANDSHAKE_GRIP_FORCE + 12 for f in ("index", "middle", "ring", "pinky")), "四指力在握力附近")
big_meas = world.measured()

print("== 抽手 → 自动松开 → 再来")
t_pull = t
world.blocks = {}
rel = []
t = run(world, ctrl, 3.0, t, on_tick=lambda tt: rel.append(tt) if ctrl.state == "releasing" and not rel else None)
check(bool(rel) and rel[0] - t_pull < 1.0, "抽手后 1 秒内松开")
t = run(world, ctrl, 3.0, t)
check(ctrl.state == "waiting", f"回到等待（{ctrl.state}）")

print("== 拇指侧面被你的手顶住（翻滚关节被挡，拇指尖没力）")
world.blocks = hand(205, 205, 180, 165, 140, thumb_roll=95)
ctrl.request("grip")
t = run(world, ctrl, 5.0, t)
print("  停下原因：", ctrl.stopped, " 侧面顶住的最大指令差：", {j: round(v) for j, v in world.max_push.items()})
check(ctrl.stopped.get("thumb") == "blocked", "拇指判为挡住，停下")
check(world.max_push.get(1, 0) <= cfg.HANDSHAKE_THUMB_LEAD + 6,
      f"拇指翻滚最多比实测往里 {world.max_push.get(1, 0):.0f}，不会越压越深（领先上限 {cfg.HANDSHAKE_THUMB_LEAD}）")
check("thumb" in ctrl.held and abs(ctrl.held["thumb"][1] - (95 - cfg.HANDSHAKE_THUMB_BLOCK_MARGIN)) <= 2, "停住后只往里留一点点")
check("thumb" in ctrl.held and ctrl.held["thumb"][0] >= hs.ready_pose()[0] - 3, "翻滚被挡时拇指还没往下弯")
ctrl.request("release")
world.blocks = {}
world.max_push = {}
t = run(world, ctrl, 4.0, t)

print("== 握住后你的手动了一下，某根手指又顶住 → 退回实测位置")
world.blocks = BIG
ctrl.request("grip")
t = run(world, ctrl, 5.0, t)
check(ctrl.state == "holding", "握住了")
# 你把手往食指那边挪：食指被挡在更外面（侧面，无指尖力）
world.blocks = dict(BIG)
world.blocks[2] = (225, None)
world.m[2] = max(world.m[2], 225)
t = run(world, ctrl, 1.0, t)
check(ctrl.stopped.get("index") == "blocked" and ctrl.held["index"][2] >= 225 - cfg.HANDSHAKE_BLOCK_MARGIN - 1,
      f"食指改判为挡住，退到实测位置（{ctrl.held['index'][2]}）")
ctrl.request("release")
world.blocks = {}
t = run(world, ctrl, 4.0, t)

print("== 小手：多弯一些，力度差不多")
world.blocks = SMALL
ctrl.request("grip")
t = run(world, ctrl, 6.0, t)
check(ctrl.state == "holding", f"握住了（{ctrl.state}）")
small_meas = world.measured()
print("  小手实测：", small_meas[:6], " 大手实测：", big_meas[:6])
check(all(small_meas[j] < big_meas[j] for j in (2, 3, 4, 5)), "小手时四指弯得更深")
forces = net_forces(world)
check(all(forces[f] <= cfg.HANDSHAKE_GRIP_FORCE + 12 for f in ("index", "middle", "ring", "pinky")), "小手时四指力也在握力附近")

print("== 用力捏食指 → 往回松")
before = int(world.cmd[2])
world.extra["index"] = 60.0
t = run(world, ctrl, 1.0, t)
during = int(world.cmd[2])
world.extra["index"] = 0.0
check(during > before and ctrl.state == "holding", f"食指往回让（指令 {before} → {during}），仍然握着")
check(during - before <= cfg.HANDSHAKE_RELAX_MAX, f"最多让 {cfg.HANDSHAKE_RELAX_MAX}")
t = run(world, ctrl, 3.0, t)
check(abs(int(world.cmd[2]) - before) <= 3 and ctrl.state == "holding", f"放松后回到原来握住的位置（{int(world.cmd[2])}），仍然握着")
ctrl.request("release")
world.blocks = {}
t = run(world, ctrl, 4.0, t)

print("== 没有手：四指到头就松开，拇指不往里走")
ctrl.request("grip")
thumb_cmd = []
t = run(world, ctrl, 5.0, t, on_tick=lambda tt: thumb_cmd.append((world.cmd[0], world.cmd[1])))
ev = ctrl.take_events()
check(any("没碰到手" in e for e in ev), "判断为没有手并松开")
ready = hs.ready_pose()
check(all(abs(a - ready[0]) <= 1 and abs(b - ready[1]) <= 1 for a, b in thumb_cmd), "拇指始终没有往里走")
t = run(world, ctrl, 3.0, t)

print("== 侧面挡住（四指指尖都没力）：抽手时靠位置判断松开")
world.blocks = hand(205, 205, 180, 165, 140, thumb_tip=False, tips=False)
ctrl.request("grip")
t = run(world, ctrl, 5.0, t)
check(ctrl.state == "holding" and all(v == "blocked" for v in ctrl.stopped.values()), "全部判为挡住并握住")
t_pull = t
world.blocks = {}
rel = []
t = run(world, ctrl, 3.0, t, on_tick=lambda tt: rel.append(tt) if ctrl.state == "releasing" and not rel else None)
check(bool(rel) and rel[0] - t_pull < 1.0, "抽手后手指往里走，判断为抽手并松开")
t = run(world, ctrl, 3.0, t)

print("== 关掉“拇指一起握”")
ctrl.use_thumb = False
world.blocks = BIG
ctrl.request("grip")
thumb_cmd = []
t = run(world, ctrl, 5.0, t, on_tick=lambda tt: thumb_cmd.append((world.cmd[0], world.cmd[1])))
check(ctrl.state == "holding" and ctrl.stopped.get("thumb") == "skip", "只用四指握住")
check(all(abs(a - ready[0]) <= 1 and abs(b - ready[1]) <= 1 for a, b in thumb_cmd), "拇指一直停在准备姿态")
ctrl.use_thumb = True
ctrl.request("release")
world.blocks = {}
t = run(world, ctrl, 4.0, t)

print("== 握满时间自动松开")
ctrl.request("grip")
world.blocks = BIG
t = run(world, ctrl, cfg.HANDSHAKE_MAX_HOLD_S + 5.0, t)
ev = ctrl.take_events()
check(any(f"握了 {cfg.HANDSHAKE_MAX_HOLD_S:.0f} 秒" in e for e in ev), "握满时间自动松开")

print("== 网页状态")
status = ctrl.status()
check({"state", "text", "count", "fingers", "grip_force", "use_thumb"} <= set(status) and len(status["fingers"]) == 5,
      "状态字段齐全")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
