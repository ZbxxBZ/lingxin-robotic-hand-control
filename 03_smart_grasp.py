"""第 3 步：力反馈智能抓取（对应附件里 L6 的 SmartGrasp，改为 L10 版）。

流程：张开 → 预成形（拇指/侧摆就位）→ 弯曲关节逐步闭合，每步读指尖力，
达到目标力就停在当前位置 → 保持 → 释放。

和附件做法的区别：附件先一次性发送最终角度、再看力，力只能用来“报告”；
这里是边闭合边读力，达到目标力时立即停下，力才真正起到控制作用。

    python 03_smart_grasp.py
    python 03_smart_grasp.py --side right --interface pcan --channel PCAN_USBBUS1

抓取参数（姿态、目标力、监控手指）在 l10_config.OBJECTS 中修改。
"""

from __future__ import annotations

import argparse
import time

from linkerbot_hand import HandError

import l10_config as cfg
from l10_hand import (
    L10Hand,
    add_connection_args,
    check_pose,
    contact_force,
    format_forces,
    lerp_pose,
    peak_force,
    preshape_of,
)


class SmartGrasp:
    def __init__(self, hand: L10Hand) -> None:
        self.hand = hand

    def grasp(self, name: str) -> bool:
        params = cfg.OBJECTS.get(name)
        if params is None:
            print(f"未找到物体配置：{name}")
            return False

        target = check_pose(params["target"], name=f"{name}.target")
        preshape = preshape_of(target)
        target_force = params["target_force"]
        fingers = tuple(params["monitor_fingers"])
        steps = max(1, round(params["close_time_s"] / cfg.STEP_DT_S))

        print(f"\n{'=' * 60}")
        print(f"抓取物体：{params['label']}（{name}）")
        print(f"描述：{params['description']}")
        print(f"目标力值：{target_force}   力上限：{cfg.FORCE_HARD_LIMIT}")
        print(f"监控手指：{[cfg.FINGER_LABELS[f] for f in fingers]}")
        print(f"{'=' * 60}")

        print("步骤1：张开手")
        self.hand.set_speeds(cfg.MOVE_SPEED)
        self.hand.move_to("open")

        print("步骤2：预成形（拇指翻滚/旋转、侧摆就位，手指保持伸直）")
        self.hand.set_pose(preshape, wait_s=cfg.SETTLE_S)

        print(f"步骤3：逐步闭合（{steps} 步，每步 {cfg.STEP_DT_S}s），实时监测力")
        self.hand.set_speeds(cfg.GRASP_SPEED)
        reached = False
        for i in range(1, steps + 1):
            self.hand.set_pose(lerp_pose(preshape, target, i / steps))
            time.sleep(cfg.STEP_DT_S)
            try:
                forces = self.hand.read_forces()
            except HandError as error:
                # 读不到力就无法判断接触，停止闭合比继续闭合安全
                print(f"力读取失败，停止闭合并保持当前位置：{error}")
                break
            current = contact_force(forces, fingers)
            peak_finger, peak = peak_force(forces, fingers)
            print(f"  [{i:>3}/{steps}] 力值 {current:6.1f} / 目标 {target_force}   {format_forces(forces)}")
            if peak >= cfg.FORCE_HARD_LIMIT:
                print(f"{cfg.FINGER_LABELS[peak_finger]}力值 {peak:.0f} 超过上限，停止闭合")
                break
            if current >= target_force:
                print("达到目标力值！停在当前位置")
                reached = True
                break
        else:
            print("已到闭合终点，未达到目标力值（物体没放好 / 偏小，或目标力偏高）")

        print(f"步骤4：保持抓取 {params['hold_s']} 秒")
        time.sleep(params["hold_s"])
        try:
            print(f"  保持时力值：{format_forces(self.hand.read_forces())}")
        except HandError:
            pass

        print("抓取完成！")
        return reached

    def release(self) -> None:
        print("\n释放物体")
        self.hand.set_speeds(cfg.MOVE_SPEED)
        self.hand.move_to("open")
        print("释放完成")


def ask_release(smart: SmartGrasp) -> None:
    """中断后由用户决定是否张开（张开可能让物体掉落）。"""
    if not smart.hand.is_open:
        return
    try:
        answer = input("是否张开手释放物体？[y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if answer == "y":
        smart.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="L10 力反馈智能抓取")
    add_connection_args(parser)
    args = parser.parse_args()

    keys = list(cfg.OBJECTS)
    hand = L10Hand.from_args(args)
    smart = SmartGrasp(hand)
    try:
        hand.open()
        print(f"指尖力数据来源：{hand.detect_force_source()}")
        while True:
            print("\n请选择要抓取的物体：")
            for i, key in enumerate(keys, start=1):
                p = cfg.OBJECTS[key]
                print(f"  {i}-{p['label']}（{p['description']}，目标力 {p['target_force']}）")
            print("  q-退出")
            choice = input(f"\n请输入（1-{len(keys)} 或 q）：").strip().lower()
            if choice == "q":
                print("退出程序")
                break
            if not (choice.isdigit() and 1 <= int(choice) <= len(keys)):
                print("无效输入")
                continue
            smart.grasp(keys[int(choice) - 1])
            input("按回车释放物体...")
            smart.release()
    except KeyboardInterrupt:
        print("\n\n程序被用户中断，手保持当前姿态")
        ask_release(smart)
    except (HandError, RuntimeError, ValueError) as error:
        print(f"\n发生错误：{error}")
    finally:
        try:
            hand.close()
        except HandError as error:
            print(f"断开连接失败：{error}")
        print("\n程序结束")


if __name__ == "__main__":
    main()
