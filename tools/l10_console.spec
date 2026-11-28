# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：把网页控制台打成一个 exe（单文件，双击运行）。
#
# 在装好本项目的环境里（pip install -e ".[camera,build]"），项目根目录运行：
#   python -m PyInstaller tools/l10_console.spec --noconfirm
# 输出：dist/L10_Console_v<版本>.exe（版本号取自 l10ctl/__init__.py）
#
# 打包前先下载手部识别模型（l10-mirror --download-model），它会被打进 exe。
# exe 里包含：网页文件、官方 3D 模型、MediaPipe 手部识别模型、Python 和所有依赖。
# PCAN 驱动（PCANBasic.dll）不打包，用电脑上安装的 PEAK 驱动。
# 参数用打包时的默认值；exe 旁边放一个 l10_settings.toml 就能覆盖，不用重新打包。

import os
import re
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

# conda 环境把 ffi-8.dll、liblzma.dll 等放在 Library\bin。不激活环境、直接用环境里的 python 打包时
# 这个目录不在 PATH 上，PyInstaller 找不到这些依赖，exe 一启动就报“DLL load failed while importing _ctypes”。
LIB_BIN = os.path.join(sys.prefix, "Library", "bin")
if os.path.isdir(LIB_BIN):
    os.environ["PATH"] = LIB_BIN + os.pathsep + os.environ.get("PATH", "")

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)  # 没有 pip install 时也能找到 l10ctl

with open(os.path.join(ROOT, "l10ctl", "__init__.py"), encoding="utf-8") as f:
    VERSION = re.search(r'^__version__ = "([^"]+)"', f.read(), re.M).group(1)

from l10ctl import config as cfg  # noqa: E402
from l10ctl import paths  # noqa: E402

MODEL = paths.data_file(cfg.MIRROR_MODEL_PATH)
if not MODEL.is_file():
    sys.exit(f"缺少手部识别模型 {MODEL}，先运行 l10-mirror --download-model")

datas = [
    (os.path.join(ROOT, "l10ctl", "web"), os.path.join("l10ctl", "web")),
    (str(MODEL), os.path.join("l10ctl", "bundled")),
]
binaries = []
hiddenimports = (
    collect_submodules("l10ctl")
    + collect_submodules("linkerbot_hand")
    + collect_submodules("linkerbot_transport")
    + ["can.interfaces.pcan", "can.interfaces.pcan.pcan", "can.interfaces.pcan.basic"]
)
for package in ("mediapipe",):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
datas += copy_metadata("python-can")

a = Analysis(
    [os.path.join(ROOT, "tools", "console_entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pytest", "sounddevice"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"L10_Console_v{VERSION}",
    console=True,  # 保留命令行窗口：显示连接状态和日志，关掉窗口即退出
    upx=False,
    debug=False,
    strip=False,
    runtime_tmpdir=None,
)
