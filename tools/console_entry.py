"""打包 exe 用的入口：启动网页控制台。设置文件写错时把原因显示出来再关窗口（双击运行时看得到）。"""

import sys

from l10ctl.settings import SettingsError

try:
    from l10ctl.dashboard import main
except SettingsError as error:
    print(f"设置文件有问题：{error}")
    try:
        input("按回车键关闭窗口…")
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(1)

main()
