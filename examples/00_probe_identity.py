"""诊断：直接用 python-can 向 L10 发身份查询，看固件到底应答了哪些帧。

新 SDK 连接时要求手应答 C0（产品码/序列号）、C1（PCB 版本）、C2（固件版本）、
C4（机械版本）四种查询，缺任何一种都会报
“protocol response did not complete before timeout (stage=('identity', ...))”。
本脚本逐个发送这些查询，再加上旧 SDK 用的 0x64 版本查询和一次角度读取，
把收到的应答原样打印出来。

只发送查询帧，不发送任何运动指令，手不会动。
运行前请关闭 can_bridge、官方控制台和其他控制脚本（它们会占用 PCAN）。

    python examples/00_probe_identity.py
    python examples/00_probe_identity.py --side right
    python examples/00_probe_identity.py --interface socketcan --channel can0
"""

from __future__ import annotations

import argparse
import time

import can

from l10ctl import config as cfg

CAN_IDS = {"left": 0x28, "right": 0x27}
QUERIES = (
    (0xC0, "产品码 / 序列号", True),
    (0xC1, "PCB 版本", True),
    (0xC2, "固件版本", True),
    (0xC4, "机械版本", True),
    (0x64, "旧版本号（旧 SDK 使用）", False),
    (0x01, "关节 1–6 角度（只读）", False),
)


def ascii_of(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def probe(bus: can.BusABC, can_id: int, opcode: int, wait_s: float) -> list[bytes]:
    """发一个只含操作码的查询帧，收集 wait_s 内同 ID、同操作码的应答。"""
    bus.send(can.Message(arbitration_id=can_id, data=[opcode], is_extended_id=False))
    replies: list[bytes] = []
    deadline = time.monotonic() + wait_s
    while (remaining := deadline - time.monotonic()) > 0:
        msg = bus.recv(timeout=remaining)
        if msg is None:
            break
        data = bytes(msg.data)
        if msg.arbitration_id == can_id and not msg.is_extended_id and len(data) > 1 and data[0] == opcode:
            replies.append(data)
    return replies


def main() -> None:
    parser = argparse.ArgumentParser(description="L10 身份查询诊断（只读）")
    parser.add_argument("--side", choices=("left", "right"), default=cfg.SIDE)
    parser.add_argument("--interface", choices=("pcan", "socketcan"), default=cfg.INTERFACE if cfg.INTERFACE != "auto" else "pcan")
    parser.add_argument("--channel", default=cfg.CHANNEL)
    parser.add_argument("--wait", type=float, default=0.5, help="每个查询等待应答的秒数")
    args = parser.parse_args()

    can_id = CAN_IDS[args.side]
    print(f"打开 {args.interface} {args.channel}，查询 {args.side} 手（CAN ID 0x{can_id:02X}）…\n")
    bus = can.Bus(interface=args.interface, channel=args.channel, bitrate=cfg.BITRATE)
    missing = []
    try:
        # 先清掉缓冲区里的旧帧
        while bus.recv(timeout=0.05) is not None:
            pass
        for opcode, label, required in QUERIES:
            replies = probe(bus, can_id, opcode, args.wait)
            tag = "（新 SDK 必需）" if required else ""
            if not replies:
                print(f"0x{opcode:02X} {label}{tag}：无应答")
                if required:
                    missing.append(f"0x{opcode:02X}")
                continue
            print(f"0x{opcode:02X} {label}{tag}：{len(replies)} 帧")
            for data in replies:
                print(f"    {data.hex(' ').upper():<24} | {ascii_of(data[1:])}")
            time.sleep(0.05)
    finally:
        bus.shutdown()

    print()
    if missing:
        print(f"结论：新 SDK 需要的身份帧里，{'、'.join(missing)} 没有应答，所以 SDK 的身份校验无法通过。")
        print("      把上面的完整输出发给我，我据此决定怎么适配这版固件。")
    else:
        print("结论：四种身份查询都有应答。如果 SDK 仍然超时，可能是应答太慢或格式不同，")
        print("      请把上面的完整输出发给我。")


if __name__ == "__main__":
    main()
