"""离线测防抖：静止时的识别噪声还会不会发给电机；动起来时跟随慢了多少。不碰真机。"""

import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: E402,F401  仓库根目录加进 sys.path；数据目录换成临时目录
from l10ctl import config as cfg  # noqa: E402
from l10ctl.mirror import Deadband, OneEuroFilter  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def pipeline(min_cutoff, band):
    flt = OneEuroFilter(min_cutoff, cfg.MIRROR_FILTER_BETA)
    dead = Deadband(band)
    return lambda x, t: dead(flt(x, t))


def sends_at_rest(min_cutoff, band, noise, seconds=10.0, fps=30, seed=0):
    """手静止（真值 150）+ 识别噪声：统计会触发重发（变化 ≥ MIRROR_SEND_MIN）的次数。"""
    rng = np.random.default_rng(seed)
    run = pipeline(min_cutoff, band)
    last, sends, outs = None, 0, []
    for k in range(int(seconds * fps)):
        out = run([150 + rng.normal(0, noise)], k / fps)[0]
        outs.append(out)
        if k < fps:  # 前 1 秒是起步，不计
            last = out
            continue
        if abs(out - last) >= cfg.MIRROR_SEND_MIN:
            sends += 1
            last = out
    return sends, float(np.ptp(outs[fps:]))


print("== 手静止 10 秒，识别噪声 ±4（标准差）")
old_sends, old_span = sends_at_rest(1.5, 0, 4)
new_sends, new_span = sends_at_rest(cfg.MIRROR_FILTER_MIN_CUTOFF, cfg.MIRROR_DEADBAND, 4)
print(f"  旧设置（滤波 1.5 Hz，无回差）：重发 {old_sends} 次，指令摆动范围 {old_span:.1f}")
print(f"  新设置（滤波 {cfg.MIRROR_FILTER_MIN_CUTOFF} Hz，回差 {cfg.MIRROR_DEADBAND}）：重发 {new_sends} 次，摆动范围 {new_span:.1f}")
check(new_sends == 0, "新设置下手静止时不再给电机发新指令")
for noise in (6, 8):
    n, span = sends_at_rest(cfg.MIRROR_FILTER_MIN_CUTOFF, cfg.MIRROR_DEADBAND, noise)
    print(f"  噪声 ±{noise}：重发 {n} 次，摆动范围 {span:.1f}")
for band in (10, 14):
    n, span = sends_at_rest(cfg.MIRROR_FILTER_MIN_CUTOFF, band, 8)
    print(f"  噪声 ±8、回差 {band}：重发 {n} 次，摆动范围 {span:.1f}")

print("== 动起来：0.5 秒内从 255 握到 0，再停住")
for label, min_cutoff, band in (("旧设置", 1.5, 0), ("新设置", cfg.MIRROR_FILTER_MIN_CUTOFF, cfg.MIRROR_DEADBAND)):
    run = pipeline(min_cutoff, band)
    fps, outs = 30, []
    for k in range(60):
        t = k / fps
        truth = 255 - 255 * min(1.0, max(0.0, (t - 0.5) / 0.5))
        outs.append((t, truth, run([truth], t)[0]))
    lag = max(abs(o - tr) for t, tr, o in outs if 0.5 <= t <= 1.0)
    t90 = next(t for t, tr, o in outs if o <= 25.5) - 0.5
    print(f"  {label}：运动中最大落后 {lag:.0f}，到 90% 用时 {t90 * 1000:.0f} ms，最终 {outs[-1][2]:.0f}")
    if label == "新设置":
        check(round(outs[-1][2]) == 0, "握到底时发给电机的指令是 0（端点不受回差影响）")
        check(t90 < 0.8, "到 90% 的时间在 0.8 秒以内")

print("== 小动作：从 150 慢慢移到 160")
run = pipeline(cfg.MIRROR_FILTER_MIN_CUTOFF, cfg.MIRROR_DEADBAND)
out = None
for k in range(90):
    out = run([150 + min(10.0, k / 3)], k / 30)[0]
print(f"  最终指令 {out:.0f}（真值 160，回差 {cfg.MIRROR_DEADBAND}）")
check(abs(out - 160) <= cfg.MIRROR_DEADBAND + 0.5, "小动作也会跟，只差不超过回差")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
