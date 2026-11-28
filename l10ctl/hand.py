"""灵心巧手 L10 的薄封装，基于 linkerbot-hand 2.x SDK。

提供：连接 / 断开、发送 10 关节姿态、设置速度、读角度、读指尖力、
读温度和故障。其他脚本都通过这里访问真机。

    from l10ctl.hand import L10Hand
    with L10Hand() as hand:
        print(hand.get_pose())
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Iterable, Sequence

from linkerbot_hand import (
    CommandStage,
    DeviceConfig,
    ForceSample,
    HandDevice,
    HandError,
    discover_hands,
)
from linkerbot_transport.can import CANTransport

from . import config as cfg

Pose = tuple[int, ...]


# ---------------------------------------------------------------- 工具函数
def check_pose(values: Iterable[float], name: str = "姿态") -> Pose:
    """检查并规范化 10 个 0~255 的关节值。"""
    pose = tuple(int(round(v)) for v in values)
    if len(pose) != len(cfg.JOINT_NAMES):
        raise ValueError(f"{name}需要 {len(cfg.JOINT_NAMES)} 个值，实际 {len(pose)} 个")
    bad = [
        f"{cfg.JOINT_LABELS[i]}={v}" for i, v in enumerate(pose) if not 0 <= v <= 255
    ]
    if bad:
        raise ValueError(f"{name}超出 0~255：{', '.join(bad)}")
    return pose


def check_speeds(values: int | Iterable[int]) -> Pose:
    if isinstance(values, int):
        values = (values,) * len(cfg.JOINT_NAMES)
    return check_pose(values, name="速度")


def lerp_pose(start: Sequence[int], end: Sequence[int], t: float) -> Pose:
    """在两个姿态之间线性插值，t ∈ [0, 1]。"""
    t = min(max(t, 0.0), 1.0)
    return tuple(round(a + (b - a) * t) for a, b in zip(start, end, strict=True))


def preshape_of(target: Sequence[int], open_pose: Sequence[int] | None = None) -> Pose:
    """预成形：非弯曲关节取 target 的值，五个弯曲关节取张开姿态的值。"""
    open_pose = cfg.POSES["open"] if open_pose is None else open_pose
    return tuple(
        open_pose[i] if i in cfg.FLEX_JOINTS else target[i]
        for i in range(len(cfg.JOINT_NAMES))
    )


THUMB_JOINTS = (0, 1, 9)  # 拇指弯曲、拇指翻滚、拇指旋转：数值越大越张开、越远离掌心
FINGER_JOINTS = (2, 3, 4, 5, 6, 7, 8)  # 四指弯曲和三个侧摆


def plan_transition(current: Sequence[int], target: Sequence[int]) -> list[Pose]:
    """把可能相撞 / 卡住的切换拆成几段，返回依次要发送的姿态（最后一段就是 target）。

    四指（含侧摆）有关节要变化超过 STAGE_MIN_DELTA，并且满足下面任一条件时分段：
      - 拇指自身也要变化超过 STAGE_MIN_DELTA（例如数字 6 → 7）；
      - 起止姿态里拇指是折起的（弯曲 < THUMB_CLEAR_FLEX 或翻滚 < THUMB_CLEAR_ROLL），
        可能压住要伸直的手指（例如数字 1 → 2、2 → 3）。
    分段：
      1. 拇指抬到 SAFE_THUMB（逐关节不低于起止姿态），四指不动；
      2. 四指和侧摆到目标，拇指保持抬开；
      3. 拇指到目标。
    和上一段几乎相同的阶段会被省略。
    """
    cur, tgt = check_pose(current, "当前姿态"), check_pose(target, "目标姿态")
    thumb_delta = max(abs(cur[j] - tgt[j]) for j in THUMB_JOINTS)
    finger_delta = max(abs(cur[j] - tgt[j]) for j in FINGER_JOINTS)

    def thumb_folded(pose: Pose) -> bool:
        return pose[0] < cfg.THUMB_CLEAR_FLEX or pose[1] < cfg.THUMB_CLEAR_ROLL

    needs_stages = finger_delta >= cfg.STAGE_MIN_DELTA and (
        thumb_delta >= cfg.STAGE_MIN_DELTA or thumb_folded(cur) or thumb_folded(tgt)
    )
    if not cfg.SAFE_TRANSITION or not needs_stages:
        return [tgt]
    retracted = {
        j: max(cur[j], tgt[j], safe) for j, safe in zip(THUMB_JOINTS, cfg.SAFE_THUMB, strict=True)
    }

    def with_thumb(pose: Pose, thumb: dict[int, int]) -> Pose:
        return tuple(thumb.get(j, v) for j, v in enumerate(pose))

    stages: list[Pose] = []
    previous = cur
    for stage in (with_thumb(cur, retracted), with_thumb(tgt, retracted), tgt):
        if max(abs(a - b) for a, b in zip(stage, previous, strict=True)) >= 3:
            stages.append(stage)
            previous = stage
    return stages or [tgt]


class ArrivalCheck:
    """判断一段运动是否结束：到位、停住不动（可能被挡住或到限位）或超时。"""

    def __init__(self, target: Sequence[int], now: float, timeout_s: float = cfg.STAGE_TIMEOUT_S) -> None:
        self.target = tuple(int(v) for v in target)
        self.start = now
        self.timeout_s = timeout_s
        self.ref: Pose | None = None  # 判断“停住”的参考实测
        self.ref_time = now
        self.measured: Pose | None = None
        self.error: int | None = None
        self.outcome: str | None = None  # "reached" 到位 / "stalled" 停住未到位 / "timeout" 超时

    @property
    def reached(self) -> bool:
        return self.outcome == "reached"

    def update(self, measured: Sequence[int], now: float) -> bool:
        measured = tuple(int(v) for v in measured)
        self.measured = measured
        self.error = max(abs(a - b) for a, b in zip(measured, self.target, strict=True))
        if self.error <= cfg.ARRIVE_TOL:
            self.outcome = "reached"
            return True
        # 和参考实测比：任何关节累计变化超过 ARRIVE_MOVE_EPS 就算还在动，并更新参考。
        # 不和上一次读数比——慢速运动或角度刷新慢时，相邻两次读数几乎一样，会被误判为“停住”，
        # 进而触发堵转卸力把手指停在半路（例如正计时 5→6 时 6 摆不到位就切到 7）。
        if self.ref is None or max(abs(a - b) for a, b in zip(measured, self.ref, strict=True)) > cfg.ARRIVE_MOVE_EPS:
            self.ref, self.ref_time = measured, now
        elapsed = now - self.start
        if elapsed >= 0.3 and now - self.ref_time >= cfg.ARRIVE_STILL_S:
            self.outcome = "stalled"
            return True
        if elapsed >= self.timeout_s:
            self.outcome = "timeout"
            return True
        return False

    def describe_gap(self, limit: int = 3) -> str:
        """离目标最远的几个关节，例如“食指弯曲 目标 0 实测 120”（日志里定位哪根手指没到位）。"""
        if self.measured is None:
            return ""
        gaps = sorted(
            ((abs(m - t), j, t, m) for j, (m, t) in enumerate(zip(self.measured, self.target, strict=True))),
            reverse=True,
        )
        return "；".join(f"{cfg.JOINT_LABELS[j]} 目标 {t} 实测 {m}" for gap, j, t, m in gaps[:limit] if gap > cfg.ARRIVE_TOL)


def relieve_stall(target: Sequence[int], measured: Sequence[int]) -> tuple[Pose, list[tuple[int, int, int, int]]]:
    """堵转卸力：被挡住的关节改为“停住位置再往目标多 RELIEF_MARGIN”。

    返回 (新目标, [(关节下标, 原目标, 实测, 新目标), ...])；没有需要卸力的关节时列表为空。
    只应在运动已停住（ArrivalCheck.outcome == "stalled"）时调用，否则会截断还在进行的运动。
    """
    pose = list(check_pose(target))
    changes = []
    for j, (goal, actual) in enumerate(zip(target, measured, strict=True)):
        error = int(goal) - int(actual)
        if abs(error) <= cfg.RELIEF_MIN_ERROR:
            continue
        new = int(actual) + (cfg.RELIEF_MARGIN if error > 0 else -cfg.RELIEF_MARGIN)
        new = min(255, max(0, new))
        pose[j] = new
        changes.append((j, int(goal), int(actual), new))
    return tuple(pose), changes


FINGER_FLEX_JOINT = {"thumb": 0, "index": 2, "middle": 3, "ring": 4, "pinky": 5}


def force_relief_step(
    target: Sequence[int], base: Sequence[int], forces: dict[str, float | None]
) -> tuple[Pose, list[tuple[str, int, int, int, float]]]:
    """压力卸载的一步：指尖力超过 PRESET_FORCE_LIMIT 的手指，弯曲关节往伸直方向退 FORCE_RELIEF_STEP。

    相对 base（开始按力卸载时的目标，即堵转卸力之后的目标）最多退 FORCE_RELIEF_MAX。
    返回 (新目标, [(手指, 关节下标, 原值, 新值, 当前力), ...])；没有超限或已退到上限时列表为空。
    """
    pose = list(check_pose(target))
    changes = []
    for finger, joint in FINGER_FLEX_JOINT.items():
        value = forces.get(finger)
        if value is None or value <= cfg.PRESET_FORCE_LIMIT:
            continue
        new = min(255, pose[joint] + cfg.FORCE_RELIEF_STEP, int(base[joint]) + cfg.FORCE_RELIEF_MAX)
        if new > pose[joint]:
            changes.append((finger, joint, pose[joint], new, float(value)))
            pose[joint] = new
    return tuple(pose), changes


def describe_relief(changes: list[tuple[int, int, int, int]]) -> str:
    return "；".join(f"{cfg.JOINT_LABELS[j]} 目标 {goal} 停在 {actual} → 保持 {new}" for j, goal, actual, new in changes)


def contact_force(forces: dict[str, float | None], fingers: Sequence[str]) -> float:
    """监控手指中已接触（力 > 0）的手指的平均力；都没接触则为 0。"""
    values = [v for f in fingers if (v := forces.get(f)) is not None and v > 0]
    return sum(values) / len(values) if values else 0.0


def peak_force(forces: dict[str, float | None], fingers: Sequence[str]) -> tuple[str, float]:
    """监控手指中力最大的那根手指及其力值。"""
    best = ("", 0.0)
    for finger in fingers:
        value = forces.get(finger)
        if value is not None and value > best[1]:
            best = (finger, value)
    return best


def format_pose(pose: Sequence[int]) -> str:
    return "(" + ", ".join(str(int(v)) for v in pose) + ")"


def format_toml_pose(pose: Sequence[int]) -> str:
    """l10_settings.toml 里的写法，例如 [POSES] 下的 open = [255, ...]。"""
    return "[" + ", ".join(str(int(v)) for v in pose) + "]"


def format_forces(forces: dict[str, float | None]) -> str:
    parts = []
    for finger in cfg.FINGERS:
        value = forces.get(finger)
        text = "  --" if value is None else f"{value:4.0f}"
        parts.append(f"{cfg.FINGER_LABELS[finger]} {text}")
    return "  ".join(parts)


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    """命令行覆盖连接参数（默认值来自 config / l10_settings.toml）。"""
    parser.add_argument("--side", choices=("left", "right"), default=cfg.SIDE)
    parser.add_argument(
        "--interface", choices=("pcan", "socketcan", "auto"), default=cfg.INTERFACE
    )
    parser.add_argument(
        "--channel", default=cfg.CHANNEL, help="PCAN_USBBUS1 / can0（auto 时忽略）"
    )


# ---------------------------------------------------------------- 封装类
class L10Hand:
    """一只 L10 灵巧手。用 with 语句或手动 open()/close()。"""

    def __init__(
        self,
        side: str = cfg.SIDE,
        interface: str = cfg.INTERFACE,
        channel: str = cfg.CHANNEL,
        bitrate: int = cfg.BITRATE,
        timeout: float = cfg.TIMEOUT_S,
    ) -> None:
        self.side = side
        self.interface = interface
        self.channel = channel
        self.bitrate = bitrate
        self.timeout = timeout
        self.device: HandDevice | None = None
        self.force_source: str | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> L10Hand:
        return cls(side=args.side, interface=args.interface, channel=args.channel)

    # ------------------------------------------------------------ 生命周期
    def __enter__(self) -> L10Hand:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self.device is not None

    def open(self) -> None:
        if self.device is not None:
            return
        config = DeviceConfig(
            identity_timeout_s=cfg.IDENTITY_TIMEOUT_S,
            transaction_timeout_s=cfg.TRANSACTION_TIMEOUT_S,
        )
        if self.interface == "auto":
            print("自动发现设备中（请只接入一只手）...")
            device = discover_hands().require_one().create_hand_device(config=config)
        else:
            transport = CANTransport(self.channel, self.interface, bitrate=self.bitrate)
            device = HandDevice(model=cfg.SDK_MODEL, side=self.side, transport=transport, config=config)
        try:
            device.open()  # 打开通道、校验身份（LHT10）、读取基线，不发送运动指令
            model = device.get_capabilities().model
            if model != cfg.SDK_MODEL:
                raise RuntimeError(
                    f"连接到的是 {model}，不是 L10（SDK 型号名 {cfg.SDK_MODEL}）"
                )
        except BaseException as error:
            if isinstance(error, HandError) and "identity" in str(error):
                print(
                    "[提示] 身份校验没有完成：手没有在时限内应答全部身份查询（C0/C1/C2/C4）。\n"
                    "       请先确认 can_bridge / 官方控制台已关闭、左右手设置正确，\n"
                    "       再运行 python examples/00_probe_identity.py 看固件应答了哪些帧。"
                )
            try:
                device.close()
            except Exception as close_error:  # 清理失败不覆盖原始错误
                print(f"[警告] 打开失败后的清理也失败：{close_error}")
            raise
        self.device = device
        print(f"已连接 L10（{self.interface} {self.channel if self.interface != 'auto' else ''}）")

    def close(self) -> None:
        device = self.device
        if device is None:
            return
        device.close()  # 失败时保留对象，可再次调用 close() 重试
        self.device = None
        print("已断开 L10")

    @property
    def _dev(self) -> HandDevice:
        if self.device is None:
            raise RuntimeError("L10 未连接，请先调用 open()")
        return self.device

    # ------------------------------------------------------------ 运动
    def set_pose(self, pose: Iterable[float], wait_s: float = 0.0) -> Pose:
        """发送 10 关节目标角度（0~255），确认指令已送达；可选再等待 wait_s 秒。"""
        pose = check_pose(pose)
        handle = self._dev.set_angles(pose)
        stage = handle.wait(timeout=self.timeout)  # 只等协议确认，不等机械到位
        if stage is CommandStage.FAILED:
            raise RuntimeError(f"角度指令失败：{handle.error}")
        if wait_s > 0:
            time.sleep(wait_s)
        return pose

    def move_to(self, name: str, timeout_s: float = cfg.STAGE_TIMEOUT_S) -> Pose:
        """移动到 config.POSES 中的预设姿态（必要时分段，避免拇指与四指相撞）。"""
        return self.move_safely(cfg.POSES[name], timeout_s=timeout_s)

    def move_safely(self, target: Iterable[float], timeout_s: float = cfg.STAGE_TIMEOUT_S) -> Pose:
        """按 plan_transition 分段移动到 target，每段等到位（或停住 / 超时）再走下一段。"""
        stages = plan_transition(self.get_pose(), tuple(target))
        if len(stages) > 1:
            print(f"  分 {len(stages)} 段过渡，避免拇指与四指相撞")
        check = None
        for stage in stages:
            self.set_pose(stage)
            check = self.wait_arrival(stage, timeout_s)
        final = stages[-1]
        if cfg.STALL_RELIEF and check is not None and check.outcome == "stalled":
            relieved, changes = relieve_stall(final, self.get_pose())
            if changes:
                self.set_pose(relieved)
                print(f"  卸力：{describe_relief(changes)}")
                return relieved
        return final

    def wait_arrival(self, target: Sequence[int], timeout_s: float = cfg.STAGE_TIMEOUT_S) -> ArrivalCheck:
        """阻塞等待到位、停住或超时；返回 ArrivalCheck（outcome 说明是哪一种）。"""
        check = ArrivalCheck(target, time.monotonic(), timeout_s)
        while True:
            time.sleep(0.05)
            if check.update(self.get_pose(), time.monotonic()):
                return check

    def set_speeds(self, speeds: int | Iterable[int]) -> None:
        speeds = check_speeds(speeds)
        handle = self._dev.set_speeds(speeds)
        stage = handle.wait(timeout=self.timeout)
        if stage is CommandStage.FAILED:
            raise RuntimeError(f"速度指令失败：{handle.error}")

    # ------------------------------------------------------------ 读取
    def get_pose(self) -> Pose:
        """主动读取一次当前 10 关节原始角度。"""
        return tuple(int(v) for v in self._dev.get_angles(timeout=self.timeout).raw)

    def device_info(self) -> object:
        return self._dev.get_device_info(timeout=self.timeout)

    def temperatures(self) -> Pose:
        return tuple(int(v) for v in self._dev.get_temperatures(timeout=self.timeout).raw)

    def faults(self) -> list[str]:
        """返回有故障的关节说明；空列表表示无故障。"""
        sample = self._dev.get_faults(timeout=self.timeout)
        messages = []
        for index, raw in enumerate(sample.raw):
            if not raw:
                continue
            status = sample.statuses[index] if index < len(sample.statuses) else None
            detail = ", ".join(getattr(status, "descriptions", ())) or f"0x{raw:02X}"
            messages.append(f"{cfg.JOINT_LABELS[index]}：{detail}")
        return messages

    # ------------------------------------------------------------ 指尖力
    def detect_force_source(self) -> str:
        """确定用哪种力数据（fns 汇总值 / fn 点阵最大值），结果缓存。"""
        if cfg.FORCE_SOURCE == "auto":
            candidates = ("fns", "fn_max")
        elif cfg.FORCE_SOURCE in ("fns", "fn_max"):
            candidates = (cfg.FORCE_SOURCE,)
        else:
            raise ValueError(f"FORCE_SOURCE 配置错误：{cfg.FORCE_SOURCE!r}")
        problems = []
        for source in candidates:
            try:
                forces = self._read_forces(source, timeout=self.timeout)
            except HandError as error:  # 超时 / 不支持
                problems.append(f"{source}：{error}")
                continue
            if any(value is not None for value in forces.values()):
                self.force_source = source
                return source
            problems.append(f"{source}：没有有效数据")
        raise RuntimeError("读不到指尖力数据 → " + "；".join(problems))

    def probe_force_sources(self) -> dict[str, dict[str, float | None] | str]:
        """分别试读 fns 和 fn_max，返回 {来源: 力值 或 错误说明}，用于诊断。"""
        results: dict[str, dict[str, float | None] | str] = {}
        for source in ("fns", "fn_max"):
            try:
                results[source] = self._read_forces(source, timeout=self.timeout)
            except HandError as error:
                results[source] = f"读取失败：{error}"
        return results

    def read_forces(self, timeout: float = cfg.FORCE_TIMEOUT_S) -> dict[str, float | None]:
        """读取五指力值 {finger: 值或 None}。None 表示该指没有有效数据。"""
        if self.force_source is None:
            self.detect_force_source()
        assert self.force_source is not None
        return self._read_forces(self.force_source, timeout=timeout)

    def read_force_detail(
        self, timeout: float = cfg.FORCE_TIMEOUT_S
    ) -> tuple[dict[str, float | None], dict[str, tuple[tuple[float, ...], ...]] | None]:
        """一次读取：五指力值，以及（触觉点阵版本时）每指的 12×6 原始点阵。

        返回 (forces, matrices)。数据来源是 fns 时 matrices 为 None。
        """
        if self.force_source is None:
            self.detect_force_source()
        field_name = "fns" if self.force_source == "fns" else "fn"
        sample = self._dev.get_force(field_name, timeout=timeout)
        forces = self._forces_from_sample(sample, field_name)
        if field_name != "fn" or not sample.tactile:
            return forces, None
        matrices = {}
        for index, matrix in enumerate(sample.tactile):
            if matrix.name in cfg.FINGERS:
                name = matrix.name
            elif index < len(cfg.FINGERS):
                name = cfg.FINGERS[index]
            else:
                continue
            matrices[name] = matrix.raw
        return forces, matrices

    def _read_forces(self, source: str, timeout: float) -> dict[str, float | None]:
        field_name = "fns" if source == "fns" else "fn"
        sample = self._dev.get_force(field_name, timeout=timeout)
        return self._forces_from_sample(sample, field_name)

    @staticmethod
    def _forces_from_sample(sample: ForceSample, field_name: str) -> dict[str, float | None]:
        field = sample.field(field_name)
        result: dict[str, float | None] = dict.fromkeys(cfg.FINGERS)
        if field is None:
            return result
        for name, raw, valid in zip(
            field.sensor_names, field.raw, field.valid_values, strict=True
        ):
            if raw is None or valid is None:
                continue
            good = [float(v) for v, ok in zip(raw, valid, strict=True) if ok]
            # fns 每指只有 1 个值；fn 是 12×6=72 个点，取最大值（与 fns 同为 0~255 量级）
            result[name] = max(good) if good else None
        return result
