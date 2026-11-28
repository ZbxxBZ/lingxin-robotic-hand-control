"""离线验证到位判断：慢速 / 角度刷新慢时不再误判停住；真被挡住仍能判出；正计时 1→9 每个数字都到位。不碰真机。"""

import sys
import threading
import time
from types import SimpleNamespace

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: E402,F401  仓库根目录加进 sys.path；数据目录换成临时目录
from l10ctl import actions  # noqa: E402
from l10ctl import config as cfg  # noqa: E402
from l10ctl.hand import ArrivalCheck  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class OldArrivalCheck:
    """改之前的判断：和上一次读数比，0.25 s 内每次变化都 ≤ 2 就算停住。"""

    def __init__(self, target, now, timeout_s=2.0):
        self.target, self.start, self.timeout_s = tuple(target), now, timeout_s
        self.last, self.still_since, self.outcome = None, None, None

    def update(self, measured, now):
        error = max(abs(a - b) for a, b in zip(measured, self.target))
        if error <= cfg.ARRIVE_TOL:
            self.outcome = "reached"
            return True
        moving = self.last is None or max(abs(a - b) for a, b in zip(measured, self.last)) > 2
        self.last = tuple(measured)
        if moving:
            self.still_since = None
        elif self.still_since is None:
            self.still_since = now
        elapsed = now - self.start
        if self.still_since is not None and elapsed >= 0.3 and now - self.still_since >= 0.25:
            self.outcome = "stalled"
            return True
        if elapsed >= self.timeout_s:
            self.outcome = "timeout"
            return True
        return False


FIVE = actions.merge((255,) * 10, *actions.preset_parts("num5"))
SIX = actions.merge((255,) * 10, *actions.preset_parts("num6"))


def simulate(check_cls, rate, refresh_ticks=1, block=None):
    """rate：每 50 ms 走多少；refresh_ticks：角度每几个周期才刷新一次；block：(关节, 最低到的值)。"""
    true = np.array(FIVE, float)
    seen = tuple(FIVE)
    chk = check_cls(SIX, 0.0) if check_cls is OldArrivalCheck else check_cls(SIX, 0.0)
    t, k = 0.0, 0
    while True:
        true = true + np.clip(np.array(SIX, float) - true, -rate, rate)
        if block:
            true[block[0]] = max(true[block[0]], block[1])
        if k % refresh_ticks == 0:
            seen = tuple(int(round(v)) for v in true)
        k += 1
        t = round(t + 0.05, 6)
        if chk.update(seen, t):
            return chk.outcome, t, seen


print("== 5 → 6，三根手指从 255 弯到 0")
cases = [
    ("正常速度（每 50 ms 走 12）", dict(rate=12)),
    ("慢速（每 50 ms 走 1.5，约 30/秒）", dict(rate=1.5)),
    ("角度每 0.25 秒才刷新一次", dict(rate=8, refresh_ticks=5)),
    ("真被挡住（中指停在 90）", dict(rate=12, block=(3, 90))),
]
for name, kw in cases:
    old = simulate(OldArrivalCheck, **kw)
    new = simulate(ArrivalCheck, **kw)
    print(f"  {name}：改前 {old[0]} @ {old[1]:.2f}s（实测 {old[2][2:5]}） → 改后 {new[0]} @ {new[1]:.2f}s（实测 {new[2][2:5]}）")
    if "挡住" in name:
        check(new[0] == "stalled", f"{name}：仍能判出停住（停下后 {cfg.ARRIVE_STILL_S} 秒内）")
    elif "慢速" in name:
        check(new[0] != "stalled", f"{name}：不再误判为停住（{new[0]}）")
    else:
        check(new[0] == "reached", f"{name}：到位")


# ---------------------------------------------------------------- 整体：网页控制台跑正计时 1→9
class SlowHand:
    """电机较慢（约 120/秒），角度每 0.2 秒才刷新一次。"""

    side = "left"

    def __init__(self):
        self.m = np.array(cfg.POSES["open"], float)
        self.cmd = self.m.copy()
        self.t = time.monotonic()
        self.lock = threading.Lock()
        self.seen = tuple(int(v) for v in self.m)
        self.seen_at = 0.0

    def _advance(self):
        now = time.monotonic()
        dt, self.t = now - self.t, now
        self.m = self.m + np.clip(self.cmd - self.m, -120 * dt, 120 * dt)
        if now - self.seen_at >= 0.2:
            self.seen, self.seen_at = tuple(int(round(v)) for v in self.m), now

    def device_info(self):
        return SimpleNamespace(model="L20Lite", serial_number="SIM", firmware_version="sim", product_code="LHT10",
                               sensor_variant="matrix", side="left")

    def detect_force_source(self):
        return "fn_max"

    def set_speeds(self, speeds):
        pass

    def get_pose(self):
        with self.lock:
            self._advance()
            return self.seen

    def set_pose(self, pose):
        with self.lock:
            self._advance()
            self.cmd = np.array(pose, float)
        return tuple(pose)

    def read_force_detail(self, timeout=None):
        return dict.fromkeys(cfg.FINGERS, 0.0), None

    def temperatures(self):
        return (35,) * 10

    def faults(self):
        return []


from l10ctl import dashboard as web  # noqa: E402
web.HandService._log = lambda self, level, text: self.log.appendleft({"t": "", "level": level, "text": text})

print("== 网页控制台：慢速手 + 角度刷新慢，正计时 1→9（间隔 700 ms）")
hand = SlowHand()
service = web.HandService(hand)
service.prepare()
service.start()
try:
    service.submit("animation", {"key": "count_up", "interval_ms": 700, "cycles": 1})
    targets = [actions.merge((255,) * 10, *actions.preset_parts(f"num{i}")) for i in range(1, 10)]
    shown = {}  # 数字 → 到位后保持了多久
    done_at = None
    start = time.monotonic()
    while time.monotonic() - start < 60:
        snap = service.snapshot()
        true = tuple(int(round(v)) for v in hand.m)
        for i, target in enumerate(targets, 1):
            if max(abs(a - b) for a, b in zip(true, target)) <= cfg.ARRIVE_TOL:
                shown.setdefault(i, [time.monotonic(), time.monotonic()])[1] = time.monotonic()
        if snap.get("animation") is None and time.monotonic() - start > 2:
            if done_at is None:
                done_at = time.monotonic()
            elif time.monotonic() - done_at > 7:  # 最后一个数字（9）分三段走，多等一会儿
                break
        time.sleep(0.02)
    stalls = [e["text"] for e in service.log if "卸力" in e["text"] or "超时" in e["text"]]
finally:
    service.stop()
held = {i: round(v[1] - v[0], 2) for i, v in shown.items()}
print("  每个数字真正到位后保持（秒）：", held)
print("  卸力 / 超时日志：", stalls[:5] or "无")
check(sorted(held) == list(range(1, 10)), "1～9 每个数字都真正到位")
check(all(v >= 0.5 for i, v in held.items() if i < 9), "1～8 每个数字到位后都保持了至少 0.5 秒（9 是最后一个，停在那里）")
check(not any("卸力" in s for s in stalls), "没有把慢速运动误判为堵转去卸力")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
