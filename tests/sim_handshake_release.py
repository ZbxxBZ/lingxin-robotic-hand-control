"""离线测试：拇指慢 / 角度刷新慢时不误判挡住；松手灵敏但不误松。不碰真机。"""

import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: E402,F401  仓库根目录加进 sys.path；数据目录换成临时目录
from l10ctl import config as cfg  # noqa: E402
from l10ctl import handshake as hs  # noqa: E402

src = Path(__file__).with_name("sim_handshake.py").read_text(encoding="utf-8")
exec(src.split('print("== 准备与去皮")')[0])  # 复用 World / run / hand / BIG 等

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class SlowWorld(World):
    """拇指电机慢（每 50 ms 只走 rate_thumb），角度每 refresh 个周期才刷新一次。"""

    def __init__(self, rate_thumb=1.5, refresh=3):
        super().__init__()
        self.rate = np.full(10, 8.0)
        self.rate[0] = self.rate[1] = rate_thumb
        self.refresh, self.k = refresh, 0
        self.seen = tuple(int(round(v)) for v in self.m)

    def step(self):
        new = self.m + np.clip(self.cmd - self.m, -self.rate, self.rate)
        for j, (contact, finger) in self.blocks.items():
            floor = contact - (MAX_DEPTH if finger else 0)
            new[j] = max(new[j], floor)
        self.m = new
        self.k += 1
        if self.k % self.refresh == 0:
            self.seen = tuple(int(round(v)) for v in self.m)

    def measured(self):
        return self.seen


def ready(world):
    ctrl = hs.HandshakeController(world, 0.0)
    t = run(world, ctrl, 12.0)
    assert ctrl.state == "waiting", ctrl.state
    return ctrl, t


print("== 拇指走得慢（约 30/秒）、角度每 0.15 秒才刷新：不误判为挡住")
world = SlowWorld()
ctrl, t = ready(world)
world.blocks = dict(BIG)
ctrl.request("grip")
t = run(world, ctrl, 15.0, t)
print("  停下原因：", ctrl.stopped, " 拇指实测：", world.measured()[:2])
check(ctrl.stopped.get("thumb") == "contact", f"拇指是碰到你的手才停（{ctrl.stopped.get('thumb')}），不是误判挡住")
check(ctrl.state == "holding", "握住了")


def released_after(world, ctrl, t, change, seconds=3.0):
    """做 change()（例如抽手），返回多少秒后开始松开（没松开返回 None）。"""
    change()
    t0, out = t, []
    t = run(world, ctrl, seconds, t, on_tick=lambda tt: out.append(tt) if ctrl.state == "releasing" and not out else None)
    return (out[0] - t0 if out else None), t


def regrip(world, ctrl, t, blocks=BIG, wait=4.0):
    world.blocks = {}
    t = run(world, ctrl, 4.0, t)
    world.blocks = dict(blocks)  # 用副本：抽手时会清空它
    ctrl.request("grip")
    return run(world, ctrl, wait, t)


print("== 抽手松开的速度")
world = World()
ctrl, t = ready(world)
t = regrip(world, ctrl, t)
check(ctrl.state == "holding", "握住了")
t = run(world, ctrl, 1.0, t)
dt, t = released_after(world, ctrl, t, lambda: world.blocks.clear())
print(f"  握稳后抽手：{dt:.2f} 秒开始松开" if dt is not None else "  没有松开")
check(dt is not None and dt <= 0.4, "握稳后抽手 0.4 秒内松开")

t = regrip(world, ctrl, t)
t = run(world, ctrl, 0.15, t)  # 刚握住（还没握稳）就抽手
dt, t = released_after(world, ctrl, t, lambda: world.blocks.clear())
check(dt is not None and dt <= 0.5, f"刚握住就抽手也能松开（{dt}）")

print("== 抽手时小指还被拖着、带着力：照样松开")
t = regrip(world, ctrl, t)
t = run(world, ctrl, 1.0, t)
dt, t = released_after(world, ctrl, t, lambda: [world.blocks.pop(j) for j in (0, 2, 3, 4)])
check(dt is not None and dt <= 0.5, f"一半以上手指失去接触就松开（{dt}）")

print("== 不误松：用力捏一下再放松")
t = regrip(world, ctrl, t)
t = run(world, ctrl, 1.0, t)
for f in hs.FINGER_JOINTS:
    world.extra[f] = 40.0
t = run(world, ctrl, 0.8, t)
for f in hs.FINGER_JOINTS:
    world.extra[f] = 0.0
t = run(world, ctrl, 1.5, t)
check(ctrl.state == "holding", f"捏一下再放松，仍然握着（{ctrl.state}）")

print("== 不误松：手动了一下，只有一根手指离开")
dt, t = released_after(world, ctrl, t, lambda: world.blocks.pop(5), seconds=1.5)
check(dt is None and ctrl.state == "holding", "只有一根手指离开，仍然握着")

print("== 侧面挡住的手（没有指尖力）：抽手也能松开")
side = hand(205, 205, 180, 165, 140, thumb_tip=False, tips=False)
t = regrip(world, ctrl, t, blocks=side, wait=6.0)
check(ctrl.state == "holding", "握住了（全部挡住）")
t = run(world, ctrl, 1.0, t)
dt, t = released_after(world, ctrl, t, lambda: world.blocks.clear())
check(dt is not None and dt <= 0.5, f"抽手后靠位置判断松开（{dt}）")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
