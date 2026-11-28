"""标定工具：逐个关节微调，得到适合你这只 L10 和你的物体的姿态。

启动时先读取真机当前角度作为起点，所以不会突然大幅运动。
调好后输入 s，把打印的 10 个数写进 l10_settings.toml 的 [POSES] / [OBJECTS.xxx] 里（不用改源码）。

    python examples/04_joint_jog.py

命令：
  <关节号> <值>       设置单个关节，例如 3 128（关节号 1~10，见启动时的列表）
  <关节号> +N / -N    相对调整，例如 3 -20
  p v1 v2 ... v10     直接设置完整姿态
  o                   张开（POSES["open"]）
  r                   重新读取真机角度
  f                   读取一次指尖力
  s                   打印当前目标姿态
  q                   退出（手保持当前姿态）
"""

from __future__ import annotations

import argparse

from linkerbot_hand import HandError

from l10ctl import config as cfg
from l10ctl.hand import L10Hand, add_connection_args, check_pose, format_forces, format_pose, format_toml_pose


def print_joints(target: tuple[int, ...]) -> None:
    for i, (label, value) in enumerate(zip(cfg.JOINT_LABELS, target), start=1):
        print(f"  {i:>2}. {label:<10} {value:>3}")


def main() -> None:
    parser = argparse.ArgumentParser(description="L10 单关节调试 / 姿态标定")
    add_connection_args(parser)
    args = parser.parse_args()

    try:
        with L10Hand.from_args(args) as hand:
            hand.set_speeds(cfg.JOG_SPEED)
            target = hand.get_pose()
            print("当前角度（作为起点）：")
            print_joints(target)
            print(__doc__.split("命令：", 1)[1])
            while True:
                line = input("jog> ").strip()
                if not line:
                    continue
                parts = line.split()
                cmd = parts[0].lower()
                try:
                    if cmd == "q":
                        break
                    if cmd == "s":
                        print(f"  {format_pose(target)}")
                        print(f"  写进 l10_settings.toml，例如 [POSES] 下：open = {format_toml_pose(target)}")
                        continue
                    if cmd == "r":
                        target = hand.get_pose()
                        print_joints(target)
                        continue
                    if cmd == "f":
                        print(f"  {format_forces(hand.read_forces(timeout=cfg.TIMEOUT_S))}")
                        continue
                    if cmd == "o":
                        new = cfg.POSES["open"]
                    elif cmd == "p":
                        new = tuple(int(v) for v in parts[1:])
                    elif cmd.isdigit() and len(parts) == 2:
                        joint = int(cmd) - 1
                        if not 0 <= joint < len(target):
                            print(f"关节号应为 1~{len(target)}")
                            continue
                        value = parts[1]
                        if value[0] in "+-":
                            value = target[joint] + int(value)
                        else:
                            value = int(value)
                        new = list(target)
                        new[joint] = value
                    else:
                        print("无法识别的命令")
                        continue
                    target = hand.set_pose(check_pose(new))
                    print(f"  目标 {format_pose(target)}")
                except ValueError as error:
                    print(f"输入错误：{error}")
                except (HandError, RuntimeError) as error:
                    print(f"执行失败：{error}")
    except KeyboardInterrupt:
        print("\n已退出，手保持当前姿态")


if __name__ == "__main__":
    main()
