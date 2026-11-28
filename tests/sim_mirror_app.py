"""离线测试：用模拟手跑 HandWorker（跟随 / 分段姿态 / 按力卸载）、猜拳流程，并把界面画成 PNG。不碰真机和摄像头。"""

import sys
from pathlib import Path
import threading
import time

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
import _common  # noqa: E402,F401  仓库根目录加进 sys.path；数据目录换成临时目录
OUT = str(_common.TMP)  # 截图存到临时目录

from l10ctl import config as cfg  # noqa: E402
from l10ctl.mirror import TrackedHand, thumb_folded  # noqa: E402

from l10ctl import mirror_app as app_mod  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class FakeHand:
    """按真实时间推进的一阶电机模型；拇指压着时四指运动记为卡阻。"""

    def __init__(self, pose):
        self.m = np.array(pose, float)
        self.cmd = self.m.copy()
        self.t = time.monotonic()
        self.lock = threading.Lock()
        self.jam = 0
        self.sent = 0

    def _advance(self):
        now = time.monotonic()
        dt, self.t = now - self.t, now
        folded = thumb_folded(self.m)
        new = self.m + np.clip(self.cmd - self.m, -400 * dt, 400 * dt)
        if folded and np.abs(new[2:6] - self.m[2:6]).max() > 0.5:
            self.jam += 1
        self.m = new

    def get_pose(self):
        with self.lock:
            self._advance()
            return tuple(int(round(v)) for v in self.m)

    def set_pose(self, pose):
        with self.lock:
            self._advance()
            self.cmd = np.array(pose, float)
            self.sent += 1

    def read_force_detail(self, timeout=None):
        with self.lock:
            thumb = max(0.0, (60 - self.m[0]) * 2.5) + 2.0  # 拇指弯曲低于 60 开始受力；+2 是零点
        return {"thumb": thumb, "index": 2.0, "middle": 2.0, "ring": 2.0, "pinky": 2.0}, None


def wait_until(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


print("== 控制线程：模拟手")
hand = FakeHand(cfg.POSES["open"])
worker = app_mod.HandWorker(hand, dict.fromkeys(cfg.FINGERS, 2.0))
worker.start()
try:
    worker.follow(cfg.POSES["fist"])
    ok = wait_until(lambda: max(abs(a - b) for a, b in zip(hand.get_pose(), cfg.POSES["fist"])) <= 2, 4.0)
    check(ok, "跟随：到达握拳")
    check(hand.jam == 0, "跟随张开→握拳：没有卡阻")
    snap = worker.snapshot()
    check(snap["hz"] > 15 and snap["error"] is None, f"控制循环 {snap['hz']:.0f} Hz，无错误")
    worker.follow(cfg.POSES["open"])
    ok = wait_until(lambda: max(abs(a - b) for a, b in zip(hand.get_pose(), cfg.POSES["open"])) <= 2, 4.0)
    check(ok and hand.jam == 0, "跟随握拳→张开：到达且没有卡阻")

    sent_before = hand.sent
    worker.hold()
    time.sleep(0.5)
    check(hand.sent == sent_before, "暂停后不再发送指令")

    worker.goto(cfg.GAME_POSES["scissors"])
    wait_until(lambda: worker.snapshot()["phase"] == "pose", 2.0)
    ok = wait_until(lambda: worker.snapshot()["phase"] == "hold", 6.0)
    events = worker.take_events()
    print("  事件：", events)
    check(any("分 " in e for e in events), "出剪刀：分段过渡")
    check(hand.jam == 0, "分段过渡：没有卡阻")
    time.sleep(cfg.FORCE_RELIEF_WINDOW_S + 0.3)
    final = hand.get_pose()
    raw, _ = hand.read_force_detail()
    print(f"  剪刀最终 {final}，拇指力（去皮）{raw['thumb'] - 2:.0f}")
    check(final[0] > 0 and raw["thumb"] - 2 <= cfg.PRESET_FORCE_LIMIT + 15, "到位后按力卸载：拇指退开、力降到限值附近")
finally:
    worker.stop()
check(not worker.is_alive(), "控制线程正常退出")


print("== 猜拳流程")


class RecWorker:
    def __init__(self):
        self.calls = []

    def goto(self, pose):
        self.calls.append(pose)


game = app_mod.RpsGame()
rec = RecWorker()
from l10ctl import mirror as l10_mirror
random_choice = l10_mirror.random.choice
l10_mirror.random.choice = lambda seq: "scissors"
t0 = 100.0
game.start(t0)
texts = []
t = t0
while t < t0 + 2 * cfg.GAME_BEAT_S + cfg.GAME_CAPTURE_DELAY_S + cfg.GAME_CAPTURE_S + cfg.GAME_RESULT_S + 0.5:
    gesture = "rock" if t < t0 + 2 * cfg.GAME_BEAT_S + 0.1 else "paper"
    big, small = game.update(t, gesture, rec)
    if not texts or texts[-1] != (big, small):
        texts.append((big, small))
    t += 1 / 30
l10_mirror.random.choice = random_choice
for item in texts:
    print("  ", item)
names = {cfg.GAME_PUMP: "PUMP", cfg.GAME_READY: "READY", cfg.GAME_POSES["scissors"]: "剪刀"}
print("  发给手的姿态：", [names.get(c, c) for c in rec.calls])
check([names.get(c) for c in rec.calls] == ["PUMP", "READY", "PUMP", "READY", "剪刀", "READY"], "口令节拍 → 出拳 → 回准备")
check(game.human == "paper" and game.outcome == "robot" and game.score["robot"] == 1, "你出布、L10 出剪刀 → L10 赢，记分")
check(game.state == "idle", "结果显示后回到等待")


print("== 界面截图")



def synthetic_hand():
    import sim_mirror as tm  # 复用合成手（会把那边的测试也跑一遍）

    return tm.make_hand(curl=(0, 0, 80, 80), thumb="fist")


class SnapWorker:
    def snapshot(self):
        return {
            "measured": (200, 230, 250, 248, 40, 35, 128, 67, 89, 230),
            "cmd": (180, 225, 255, 255, 30, 30, 128, 67, 89, 225),
            "forces": {"thumb": 62.0, "index": 5.0, "middle": 3.0, "ring": 18.0, "pinky": 0.0},
            "force_age": 0.1,
            "phase": "fingers",
            "floors": {0: 180},
            "error": None,
            "hz": 24.6,
        }

    def take_events(self):
        return []

    def goto(self, pose):
        pass

    def follow(self, d):
        pass

    def hold(self):
        pass



world = synthetic_hand()
img_pts = np.stack([0.5 + world[:, 0] * 3.0, 0.85 - world[:, 1] * 3.0], axis=1)
tracked = TrackedHand(world=world, image=img_pts, label="Left", score=0.97)
frame = np.zeros((480, 640, 3), np.uint8)
frame[:] = np.linspace(60, 120, 640, dtype=np.uint8)[None, :, None]

app = app_mod.MirrorApp(tracker=None, cap=None, worker=SnapWorker(), game_mode=False)
from l10ctl.mirror import classify_rps, hand_features  # noqa: E402

feats = hand_features(world)
app.mapped = np.array(app.mapper.map(feats))
app.gesture = classify_rps(app.mapper.progress(feats))
app.following = True
app.hand_seen_at = time.monotonic()
app.fps = 29.8
app.notice("压力卸载：拇指力 62，拇指弯曲 往回退", app_mod.AMBER)
img = app.draw(frame, tracked, ("", ""), time.monotonic())
cv2.imwrite(str(Path(OUT) / "ui_mirror.png"), img)

app.mode = "game"
app.game.score = {"human": 1, "robot": 2, "draw": 0}
img = app.draw(frame, tracked, ("布！", "L10 出：剪刀"), time.monotonic())
cv2.imwrite(str(Path(OUT) / "ui_game.png"), img)

app2 = app_mod.MirrorApp(tracker=None, cap=None, worker=None, game_mode=False)
app2.calib = ("fist", time.monotonic() - 0.5, [])
img = app2.draw(frame, None, ("", ""), time.monotonic() + 1)
cv2.imwrite(str(Path(OUT) / "ui_preview.png"), img)
print("  已保存 ui_mirror.png / ui_game.png / ui_preview.png")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
