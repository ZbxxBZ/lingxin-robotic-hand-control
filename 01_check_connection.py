"""第 1 步：只读检查。连接 L10，打印身份、当前角度、温度、故障和指尖力。

本脚本不发送任何运动指令，手不会动。

    python 01_check_connection.py                  # 一次性检查
    python 01_check_connection.py --watch-force    # 持续打印指尖力，用手指按压指尖来标定阈值
    python 01_check_connection.py --side right --interface socketcan --channel can0
"""

from __future__ import annotations

import argparse
import time

from linkerbot_hand import HandError

import l10_config as cfg
from l10_hand import L10Hand, add_connection_args, format_forces, format_pose


def print_state(hand: L10Hand) -> None:
    print("\n设备信息：")
    print(f"  {hand.device_info()}")

    pose = hand.get_pose()
    print("\n当前角度（原始值 0~255，弯曲关节 0=弯曲 255=伸直）：")
    for i, (label, value) in enumerate(zip(cfg.JOINT_LABELS, pose), start=1):
        print(f"  {i:>2}. {label:<10} {value:>3}")
    print(f"  可复制到 l10_config.py：{format_pose(pose)}")

    try:
        temps = hand.temperatures()
        print(f"\n电机温度（℃）：{list(temps)}")
    except HandError as error:
        print(f"\n电机温度读取失败：{error}")

    try:
        faults = hand.faults()
        print("故障：" + ("无" if not faults else "；".join(faults)))
    except HandError as error:
        print(f"故障读取失败：{error}")

    print("\n指尖力（两种来源各读一次；指尖不受力时应接近 0）：")
    for source, result in hand.probe_force_sources().items():
        text = result if isinstance(result, str) else format_forces(result)
        print(f"  {source:<7} {text}")
    try:
        chosen = hand.detect_force_source()
        print(f"程序将使用：{chosen}（FORCE_SOURCE={cfg.FORCE_SOURCE!r}）")
        print("  若按压指尖时该来源的数值不变、另一种在变，请在 l10_config.py 中改 FORCE_SOURCE")
    except RuntimeError as error:
        print(f"没有可用的指尖力数据：{error}")


def watch_force(hand: L10Hand, rate_hz: float) -> None:
    source = hand.detect_force_source()
    print(f"\n持续读取指尖力（来源 {source}），Ctrl+C 停止。")
    print("用手指分别按压各指尖，记下轻 / 中 / 重按时的数值，用于设置 target_force。\n")
    period = 1.0 / rate_hz
    while True:
        start = time.monotonic()
        try:
            print(f"\r{format_forces(hand.read_forces())}   ", end="", flush=True)
        except HandError as error:
            print(f"\n读取失败：{error}")
        time.sleep(max(0.0, period - (time.monotonic() - start)))


def main() -> None:
    parser = argparse.ArgumentParser(description="L10 只读连接检查")
    add_connection_args(parser)
    parser.add_argument("--watch-force", action="store_true", help="持续打印指尖力")
    parser.add_argument("--rate", type=float, default=10.0, help="--watch-force 的刷新频率 Hz")
    args = parser.parse_args()

    try:
        with L10Hand.from_args(args) as hand:
            print_state(hand)
            if args.watch_force:
                watch_force(hand, args.rate)
    except KeyboardInterrupt:
        print("\n已停止")
    except (HandError, RuntimeError) as error:
        print(f"\n发生错误：{error}")


if __name__ == "__main__":
    main()
