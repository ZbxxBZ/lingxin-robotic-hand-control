"""第 2 步：基本动作。从菜单选择预设手势（张开、握拳、点赞、OK、数字……）。

    python examples/02_gestures.py
    python examples/02_gestures.py --side right

姿态定义在 config.POSES，可用 04_joint_jog.py 调好后写进 l10_settings.toml 的 [POSES]。
"""

from __future__ import annotations

import argparse
import time

from l10ctl import config as cfg
from l10ctl.hand import L10Hand, add_connection_args, format_pose


def print_menu(keys: list[str]) -> None:
    print("\n可选动作：")
    for i, key in enumerate(keys, start=1):
        print(f"  {i}. {cfg.POSE_LABELS.get(key, key):<8} {format_pose(cfg.POSES[key])}")
    print("  d. 演示序列：" + " → ".join(cfg.POSE_LABELS[k] for k in cfg.DEMO_SEQUENCE))
    print("  r. 读取当前角度")
    print("  q. 张开并退出")


def main() -> None:
    parser = argparse.ArgumentParser(description="L10 预设手势")
    add_connection_args(parser)
    args = parser.parse_args()

    keys = list(cfg.POSES)
    try:
        with L10Hand.from_args(args) as hand:
            hand.set_speeds(cfg.MOVE_SPEED)
            print("先张开手")
            hand.move_to("open")
            while True:
                print_menu(keys)
                choice = input("\n请输入：").strip().lower()
                if choice == "q":
                    hand.move_to("open")
                    break
                if choice == "r":
                    print(f"当前角度：{format_pose(hand.get_pose())}")
                elif choice == "d":
                    for key in cfg.DEMO_SEQUENCE:
                        print(f"→ {cfg.POSE_LABELS[key]}")
                        hand.move_to(key)
                elif choice.isdigit() and 1 <= int(choice) <= len(keys):
                    key = keys[int(choice) - 1]
                    print(f"→ {cfg.POSE_LABELS.get(key, key)}")
                    hand.move_to(key)
                    time.sleep(0.2)
                    print(f"到位后角度：{format_pose(hand.get_pose())}")
                else:
                    print("无效输入")
    except KeyboardInterrupt:
        print("\n程序被用户中断，手保持当前姿态")


if __name__ == "__main__":
    main()
