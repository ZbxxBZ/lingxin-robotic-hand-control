# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：把 06 网页控制台打成一个 exe（单文件，双击运行）。
#
# 在 l10_hand 环境里、项目根目录运行：
#   C:\Users\23116\.conda\envs\l10_hand\python.exe -m PyInstaller tools\l10_console.spec --noconfirm
# 输出：dist\L10_Console_v<版本>.exe（版本号取自 l10_config.APP_VERSION）
#
# exe 里包含：网页文件、官方 3D 模型、MediaPipe 手部识别模型、Python 和所有依赖。
# PCAN 驱动（PCANBasic.dll）不打包，用电脑上安装的 PEAK 驱动。
# 参数（l10_config.py）是打包时的值；改了参数要重新打包。

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
SDK = r"D:\project\linkerbot_sdk"
with open(os.path.join(ROOT, "l10_config.py"), encoding="utf-8") as f:
    VERSION = re.search(r'^APP_VERSION = "([^"]+)"', f.read(), re.M).group(1)

datas = [
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "models", "hand_landmarker.task"), "models"),
]
binaries = []
hiddenimports = (
    collect_submodules("linkerbot_hand")
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
    [os.path.join(ROOT, "06_web_dashboard.py")],
    pathex=[
        ROOT,
        os.path.join(SDK, "linkerbot-hand-python-sdk", "src"),
        os.path.join(SDK, "linkerbot-transport", "src"),
    ],
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
