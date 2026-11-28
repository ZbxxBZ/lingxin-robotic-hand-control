"""离线测试共用：每个 sim_*.py 第一件事就是 import 这个文件。

- 把仓库根目录加进 sys.path：没有 pip install 也能跑；
- 数据目录和当前目录换成临时目录：不读你的 l10_settings.toml、不动你的镜像标定，全部用默认参数。

这些测试只用模拟的手和合成的人手关键点，不连真机、不开摄像头。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="l10ctl-test-"))
os.environ["L10_DATA_DIR"] = str(TMP)
os.environ.pop("L10_SETTINGS", None)
os.chdir(TMP)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:  # 被重定向成不支持 reconfigure 的对象时（例如测试里临时屏蔽输出）
    pass
