"""离线复现 / 验证“松拳时卡住”。不碰真机。

模拟的卡阻（按真机现象设定）：拇指折着（弯曲或翻滚 < 200）并压在弯曲的食指 / 中指上（< 150）时，
  - 拇指旋转往小（往小指方向横扫）超过 170 以下就被手指挡住，同时把食指 / 中指卡住不能伸直；
  - 四指伸直时顶到还折着的拇指（拇指翻滚 < 120）也会卡住。
人手：1 秒握拳 → 保持 1 秒 → 1 秒松开 → 保持 2 秒，关键点在握拳和张开之间线性插值。
"""

import contextlib
import io
import sys

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


class JamSim:
    def __init__(self, pose, rate=10):
        self.m = np.array(pose, float)
        self.rate = rate  # 每周期最多走多少（约 250/秒，偏慢一些，更容易暴露问题）
        self.jam_ticks = 0

    def step(self, cmd):
        cmd = np.array(cmd, float)
        new = self.m + np.clip(cmd - self.m, -self.rate, self.rate)
        folded = self.m[0] < 200 or self.m[1] < 200
        on_fingers = min(self.m[2], self.m[3]) < 150
        jammed = False
        if folded and on_fingers and new[9] < 170 and new[9] < self.m[9]:
            new[9] = max(new[9], min(self.m[9], 170))  # 拇指被弯曲的手指挡住
            jammed = True
        if folded and self.m[1] < 120 and on_fingers:
            for j in (2, 3):
                if new[j] > self.m[j]:  # 手指伸直顶到折着的拇指
                    new[j] = self.m[j]
                    jammed = True
        if folded and self.m[1] < 120 and min(self.m[2], self.m[3]) > 60:
            for j in (2, 3):
                if new[j] < self.m[j] - 0.5:  # 拇指已折在掌前，四指还在往里握：拇指被包进拳头
                    jammed = True
        if jammed:
            self.jam_ticks += 1
        self.m = new
        return tuple(int(round(v)) for v in self.m)


def human_track(close_s=1.0, open_s=1.0, cycles=1):
    open_p, fist_p = tm.make_hand(), tm.make_hand(curl=(80, 80, 80, 80), thumb="fist")
    dt = cfg.MIRROR_LOOP_DT_S
    period = close_s + 1.5 + open_s + 2.0
    t = 0.0
    while t < period * cycles:
        u = t % period
        if u < close_s:
            s = u / close_s
        elif u < close_s + 1.5:
            s = 1.0
        elif u < close_s + 1.5 + open_s:
            s = 1.0 - (u - close_s - 1.5) / open_s
        else:
            s = 0.0
        yield t, (1 - s) * open_p + s * fist_p
        t = round(t + dt, 6)


def run(label, **track):
    mapper = lm.MirrorMapper()
    mapper.calibrate("open", lm.hand_features(tm.make_hand()))
    mapper.calibrate("fist", lm.hand_features(tm.make_hand(curl=(80, 80, 80, 80), thumb="fist")))
    mapper.calibrate("thumb", lm.hand_features(tm.make_hand(thumb="pinky")))
    guard, sim = lm.MirrorGuard(), JamSim(cfg.POSES["open"])
    measured = tuple(int(v) for v in sim.m)
    min_rot_while_on_fingers, phases = 255, []
    for t, hand in human_track(**track):
        want = mapper.map(lm.hand_features(hand))
        cmd = guard.step(want, measured, t)
        if not phases or phases[-1] != guard.phase:
            phases.append(guard.phase)
        measured = sim.step(cmd)
        if (measured[0] < 200 or measured[1] < 200) and min(measured[2], measured[3]) < 150:
            min_rot_while_on_fingers = min(min_rot_while_on_fingers, measured[9])
    print(f"  {label}：卡阻 {sim.jam_ticks} 个周期；拇指压在手指上时旋转最低 {min_rot_while_on_fingers}；"
          f"阶段 {' → '.join(phases)}；最终 {list(measured)}")
    return sim.jam_ticks, measured


if __name__ == "__main__":
    print("== 握拳 → 松开（带卡阻的模拟）")
    scenarios = {
        "1 秒握 / 1 秒松": dict(close_s=1.0, open_s=1.0),
        "慢握 3 秒 / 慢松 3 秒": dict(close_s=3.0, open_s=3.0),
        "快握 0.3 秒 / 快松 0.3 秒": dict(close_s=0.3, open_s=0.3),
        "连续 3 次": dict(close_s=0.8, open_s=0.8, cycles=3),
    }
    for name, track in scenarios.items():
        jam, final = run(name, **track)
        if "--report-only" not in sys.argv:
            check(jam == 0, f"{name}：全程没有卡阻")
            check(min(final[:6]) >= 250, f"{name}：最终完全张开")
        print()
        print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
        sys.exit(1 if FAILS else 0)
