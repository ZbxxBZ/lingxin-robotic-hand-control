"""离线测试：l10_settings.toml 覆盖默认值、写错时报错、数据目录。不碰真机。"""

import os
import sys
from pathlib import Path

import _common  # noqa: F401  仓库根目录加进 sys.path；数据目录和当前目录换成临时目录

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# 当前目录（临时目录）下的 l10_settings.toml 会被 config 读到
Path("l10_settings.toml").write_text(
    """
SIDE = "right"
INTERFACE = "socketcan"
CHANNEL = "can0"
MIRROR_DEADBAND = 9
MIRROR_FILTER_MIN_CUTOFF = 2          # 整数写给小数项：自动变成 2.0
HANDSHAKE_POSE = [190, 77, 165, 132, 114, 84, 128, 128, 128, 128]

[POSES]
open = [250, 255, 255, 255, 255, 255, 128, 67, 89, 255]
my_pose = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

[OBJECTS.cup]
target_force = 20

[MIRROR_FEATURE_DEFAULTS]
thumb_bend = [10, 80]
""",
    encoding="utf-8",
)

from l10ctl import config as cfg  # noqa: E402
from l10ctl import paths, settings  # noqa: E402

print("== 设置文件覆盖默认值")
check(cfg.SETTINGS_FILE == Path.cwd() / "l10_settings.toml", f"读到当前目录的设置文件（{cfg.SETTINGS_FILE}）")
check((cfg.SIDE, cfg.INTERFACE, cfg.CHANNEL) == ("right", "socketcan", "can0"), "连接参数")
check(cfg.MIRROR_DEADBAND == 9 and isinstance(cfg.MIRROR_FILTER_MIN_CUTOFF, float), "整数 / 小数类型")
check(cfg.HANDSHAKE_POSE == (190, 77, 165, 132, 114, 84, 128, 128, 128, 128), "姿态是元组")
check(cfg.POSES["open"][0] == 250 and cfg.POSES["fist"] == (90, 0, 0, 0, 0, 0, 128, 67, 89, 197), "表里只改写到的项")
check(cfg.POSES["my_pose"] == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10), "可以新加姿态")
check(cfg.OBJECTS["cup"]["target_force"] == 20 and cfg.OBJECTS["cup"]["monitor_fingers"] == ("thumb", "index", "middle"),
      "嵌套表只改写到的项")
check(cfg.MIRROR_FEATURE_DEFAULTS["thumb_bend"] == (10.0, 80.0), "小数元组")
check("SIDE" in cfg.SETTINGS_CHANGED and len(cfg.SETTINGS_CHANGED) == 9, f"记下改了哪些项（{len(cfg.SETTINGS_CHANGED)}）")


def error_of(text: str) -> str | None:
    path = _common.TMP / "bad.toml"
    path.write_text(text, encoding="utf-8")
    os.environ["L10_SETTINGS"] = str(path)
    namespace = {"SIDE": "left", "MOVE_SPEED": (100,) * 10, "FORCE_HARD_LIMIT": 150, "SAFE_TRANSITION": True,
                 "SDK_MODEL": "L20Lite", "POSES": {"open": (255,) * 10}}
    try:
        settings.apply_overrides(namespace)
        return None
    except settings.SettingsError as error:
        return str(error)
    finally:
        del os.environ["L10_SETTINGS"]


print("== 写错就报错，不悄悄用默认值")
cases = {
    "名字写错（给出提示）": ('SIED = "right"', "是不是 SIDE"),
    "类型不对": ("FORCE_HARD_LIMIT = 1.5", "应为整数"),
    "布尔写成数字": ("SAFE_TRANSITION = 1", "true 或 false"),
    "速度不是 10 个": ("MOVE_SPEED = [100, 100]", "10 个 0~255"),
    "姿态超出 0~255": ("[POSES]\nopen = [300, 255, 255, 255, 255, 255, 255, 255, 255, 255]", "10 个 0~255"),
    "新姿态也检查": ("[POSES]\nmine = [1, 2, 3]", "10 个 0~255"),
    "协议相关的不能改": ('SDK_MODEL = "L10"', "不能改"),
    "TOML 格式错": ("SIDE = right", "格式错误"),
}
for label, (text, expect) in cases.items():
    error = error_of(text)
    check(error is not None and expect in error, f"{label}：{error}")

os.environ["L10_SETTINGS"] = str(_common.TMP / "missing.toml")
try:
    settings.find_settings_file()
    check(False, "L10_SETTINGS 指向不存在的文件要报错")
except settings.SettingsError as error:
    check("不存在" in str(error), f"L10_SETTINGS 指向不存在的文件：{error}")
del os.environ["L10_SETTINGS"]

example = _common.ROOT / "l10_settings.example.toml"
os.environ["L10_SETTINGS"] = str(example)
try:
    namespace = {k: v for k, v in vars(cfg).items() if k.isupper()}
    _path, changed = settings.apply_overrides(namespace)
    check(set(changed) == {"SIDE", "INTERFACE", "CHANNEL"}, f"示例设置文件能直接用（改了 {', '.join(changed)}）")
except settings.SettingsError as error:
    check(False, f"示例设置文件有错：{error}")
del os.environ["L10_SETTINGS"]

print("== 数据目录")
check(paths.data_dir() == _common.TMP.resolve(), f"L10_DATA_DIR 生效（{paths.data_dir()}）")
check(paths.data_file("a.json") == paths.data_dir() / "a.json", "相对路径放在数据目录")
absolute = Path(_common.TMP / "elsewhere" / "b.json").resolve()
check(paths.data_file(str(absolute)) == absolute, "绝对路径原样使用")
check((paths.WEB_DIR / "index.html").is_file(), "网页文件跟着包走")

from l10ctl import camera  # noqa: E402

check(camera.CALIBRATION_PATH == paths.data_dir() / cfg.MIRROR_CALIBRATION_FILE, "镜像标定在数据目录")
check(camera.MODEL_PATH == paths.data_dir() / cfg.MIRROR_MODEL_PATH, "手部识别模型在数据目录")

print()
print("全部通过" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
