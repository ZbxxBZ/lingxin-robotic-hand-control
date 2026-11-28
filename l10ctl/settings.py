"""用户设置：在 l10_settings.toml 里覆盖 config.py 的默认值，不用改源码。

查找顺序（只用找到的第一个文件）：
    1. 环境变量 L10_SETTINGS 指定的文件
    2. 当前目录下的 l10_settings.toml
    3. 数据目录下的 l10_settings.toml（打包的 exe：exe 旁边；数据目录见 paths.py）

文件里写 config.py 中的大写名字，例如：

    SIDE = "right"
    INTERFACE = "socketcan"
    CHANNEL = "can0"

    [POSES]                # 表（字典）只覆盖写到的项，其余保持默认
    open = [255, 255, 255, 255, 255, 255, 128, 67, 89, 255]

    [OBJECTS.cup]
    target_force = 20

名字写错、类型不对（比如姿态不是 10 个 0~255 的整数）会直接报错，不会悄悄用默认值。
仓库里的 l10_settings.example.toml 列了常用的项。

    python -m l10ctl.settings     # 看用的是哪个设置文件、改了哪些项
"""

from __future__ import annotations

import difflib
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import paths

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

FILE_NAME = "l10_settings.toml"
FIXED = frozenset({"APP_VERSION", "SDK_MODEL", "JOINT_NAMES", "FLEX_JOINTS", "FINGERS"})  # 和协议 / 代码结构绑定，不能改


class SettingsError(ValueError):
    """设置文件有问题（找不到、格式错、名字或类型不对）。"""


def candidates() -> list[Path]:
    env = os.environ.get("L10_SETTINGS")
    if env:
        return [Path(env).expanduser()]
    return [Path.cwd() / FILE_NAME, paths.data_dir() / FILE_NAME]


def find_settings_file() -> Path | None:
    env = os.environ.get("L10_SETTINGS")
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise SettingsError(f"环境变量 L10_SETTINGS 指向的设置文件不存在：{path}")
        return path
    return next((p for p in candidates() if p.is_file()), None)


def load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"设置文件 {path} 格式错误：{error}") from None


def apply_overrides(namespace: dict[str, Any]) -> tuple[Path | None, tuple[str, ...]]:
    """用设置文件覆盖 namespace（config 模块的 globals()）。返回 (设置文件, 改了哪些名字)。"""
    path = find_settings_file()
    if path is None:
        return None, ()
    names = [k for k in namespace if k.isupper()]
    changed = []
    for key, value in load(path).items():
        if key in FIXED:
            raise SettingsError(f"{path}：{key} 和协议绑定，不能改")
        if key not in names:
            close = difflib.get_close_matches(key, names, 1)
            hint = f"（是不是 {close[0]}？）" if close else ""
            raise SettingsError(f"{path}：没有叫 {key} 的设置{hint}")
        try:
            namespace[key] = coerce(key, namespace[key], value)
        except SettingsError as error:
            raise SettingsError(f"{path}：{error}") from None
        changed.append(key)
    return path, tuple(changed)


# ---------------------------------------------------------------- 类型检查
def _is_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_joint_vector(x: object) -> bool:
    """10 个关节的姿态 / 速度。"""
    return isinstance(x, tuple) and len(x) == 10 and all(_is_number(v) for v in x)


def _native(value: Any) -> Any:
    """TOML 的列表 → 元组（代码里的姿态、序列都是元组）。"""
    if isinstance(value, list):
        return tuple(_native(v) for v in value)
    if isinstance(value, Mapping):
        return {k: _native(v) for k, v in value.items()}
    return value


def _same_shape(values: list[Any]) -> bool:
    first = values[0]
    if _is_joint_vector(first):
        return all(_is_joint_vector(v) for v in values)
    return all(type(v) is type(first) for v in values) and not isinstance(first, (dict, tuple))


def coerce(name: str, default: Any, value: Any, merge: bool = True) -> Any:
    """把设置文件里的 value 换成和 default 同样的类型；对不上就报错。"""
    if default is None:
        return _native(value)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise SettingsError(f"{name} 应为 true 或 false")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{name} 应为整数")
        return value
    if isinstance(default, float):
        if not _is_number(value):
            raise SettingsError(f"{name} 应为数字")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise SettingsError(f"{name} 应为字符串（要加引号）")
        return value
    if isinstance(default, tuple):
        if not isinstance(value, list):
            raise SettingsError(f"{name} 应为列表 [...]")
        if _is_joint_vector(default):
            if len(value) != 10 or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in value):
                raise SettingsError(f"{name} 应为 10 个 0~255 的整数（顺序见 JOINT_NAMES）")
            return tuple(value)
        if len(value) == len(default):
            return tuple(coerce(f"{name}[{i}]", d, v) for i, (d, v) in enumerate(zip(default, value, strict=True)))
        return _native(value)
    if isinstance(default, dict):
        if not isinstance(value, Mapping):
            raise SettingsError(f"{name} 应为表（[{name}] 或 {{ ... }}）")
        values = list(default.values())
        template = values[0] if values and _same_shape(values) else None
        result = dict(default) if merge else {}
        for key, item in value.items():
            if key in default:
                result[key] = coerce(f"{name}.{key}", default[key], item, merge)
            else:  # 新加的项（例如新的姿态）：和同类的项同样检查；新加的物体等要写全
                result[key] = coerce(f"{name}.{key}", template, item, merge=False)
        return result
    return _native(value)


# ---------------------------------------------------------------- python -m l10ctl.settings
def main() -> None:
    from . import config as cfg

    print(f"l10ctl {cfg.APP_VERSION}")
    print(f"数据目录（标定、手部识别模型）：{paths.data_dir()}")
    print("设置文件查找顺序：")
    for path in candidates():
        print(f"  {'→' if path.is_file() else ' '} {path}")
    if cfg.SETTINGS_FILE is None:
        print("没有找到设置文件，全部用默认值（config.py）。")
    else:
        print(f"正在用：{cfg.SETTINGS_FILE}")
        for key in cfg.SETTINGS_CHANGED:
            print(f"  {key} = {getattr(cfg, key)!r}")
    print(f"连接：{cfg.SIDE} 手，{cfg.INTERFACE} {cfg.CHANNEL}，{cfg.BITRATE} bit/s")


if __name__ == "__main__":
    try:
        main()
    except SettingsError as error:
        sys.exit(f"设置文件有问题：{error}")
