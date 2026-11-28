"""文件放在哪：不写死任何绝对路径。

程序自带的资源跟着包走：
    l10ctl/web/            网页控制台的页面和官方 3D 模型
    l10ctl/bundled/        只在打包的 exe 里有：一起打进去的手部识别模型

运行时要读写的数据（镜像标定、下载的手部识别模型、设置文件）放在“数据目录”：
    1. 环境变量 L10_DATA_DIR 指定的目录
    2. 打包的 exe：exe 所在的文件夹（拷走 exe 时连标定一起拷）
    3. Windows：%LOCALAPPDATA%\\l10ctl
       macOS：  ~/Library/Application Support/l10ctl
       Linux：  $XDG_DATA_HOME/l10ctl（默认 ~/.local/share/l10ctl）

    python -m l10ctl.settings     # 打印数据目录和正在用的设置文件
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "l10ctl"
FROZEN = bool(getattr(sys, "frozen", False))  # 是否是打包后的 exe
PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
BUNDLED_DIR = PACKAGE_DIR / "bundled"


def data_dir() -> Path:
    env = os.environ.get("L10_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if FROZEN:
        return Path(sys.executable).resolve().parent
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def data_file(name: str) -> Path:
    """name 是绝对路径就直接用，否则放在数据目录里。"""
    path = Path(name).expanduser()
    return path if path.is_absolute() else data_dir() / path
