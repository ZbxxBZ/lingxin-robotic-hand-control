# 灵心巧手 L10 控制代码（l10ctl）

基于官方新版 SDK [linkerbot-hand-python-sdk](https://github.com/linker-bot/linkerbot-hand-python-sdk)（2.x，包名 `linkerbot_hand`）：预设动作、力反馈抓取、网页控制台、摄像头镜像跟随、石头剪刀布、按触觉握手。

> **L10 在新 SDK 里叫 `L20Lite`。** SDK 用出厂产品码 `LHT10` 校验身份，10 个关节和 CAN 协议都与 L10 一致，所以代码里写的是 `model="L20Lite"`（固定在 `l10ctl/config.py`，不用改）。

## 目录

| 路径 | 作用 | 会不会动 |
| --- | --- | --- |
| `l10ctl/` | Python 包，所有功能都在这里（`pip install -e .` 安装） | — |
| `l10ctl/config.py` | 全部参数的默认值和说明：连接、关节、预设姿态、抓取、镜像、握手 | — |
| `l10ctl/settings.py` | 读 `l10_settings.toml`，覆盖 `config.py` 的默认值（**改参数不用改源码**） | — |
| `l10ctl/paths.py` | 文件放在哪：网页资源跟着包走，标定和模型放在数据目录 | — |
| `l10ctl/hand.py` | `L10Hand` 封装：连接、发姿态、设速度、读角度/力/温度/故障、防干涉过渡 | — |
| `l10ctl/actions.py` | 从官方控制台 `l10-dashboard_2025.08.06` 迁移来的预设动作和动画（数值逐个核对过） | — |
| `l10ctl/dashboard.py` + `l10ctl/web/` | 网页控制台（`l10-dashboard`） | 动 |
| `l10ctl/mirror_app.py` | 摄像头镜像的独立窗口版（`l10-mirror`） | 动 |
| `l10ctl/mirror.py` | 手部识别（MediaPipe）、关节映射、标定、手势、石头剪刀布、跟随保护 | — |
| `l10ctl/camera.py` | 摄像头后台线程：取最新一帧 → 识别 → 映射，并输出带骨架的画面 | — |
| `l10ctl/handshake.py` | 握手：按触觉合拢、挡住检测、按触觉 / 位置松手 | — |
| `examples/00_probe_identity.py` | 底层身份查询（身份校验失败时排查用） | **不动** |
| `examples/01_check_connection.py` | 只读检查：身份、当前角度、温度、故障、指尖力；`--watch-force` 持续看力 | **不动** |
| `examples/02_gestures.py` | 预设手势菜单：张开、握拳、点赞、OK、数字、演示序列 | 动 |
| `examples/03_smart_grasp.py` | 力反馈抓取 | 动 |
| `examples/04_joint_jog.py` | 单关节微调，用来调姿态（从当前角度起步，速度慢） | 动 |
| `examples/05_cup_demo.py` | 拿杯子演示：碰指尖自动抓杯、拽一下松手，窗口实时显示五指力条和力曲线 | 动 |
| `tests/` | 离线测试（模拟的手和合成的人手，不连真机、不开摄像头） | — |
| `tools/convert_l10_model.py` | 把官方 L10 模型（URDF + OBJ）转换成网页用的 `l10ctl/web/model/` | — |
| `tools/l10_console.spec` | 把网页控制台打包成一个 exe（PyInstaller） | — |
| `l10_settings.example.toml` | 设置文件示例 | — |

## 安装

需要 Python 3.10 以上（在 3.12 上测试过）。venv 和 conda 都可以，下面以 venv 为例（Windows PowerShell；Linux / macOS 把 `.venv\Scripts\activate` 换成 `source .venv/bin/activate`）：

```powershell
git clone https://github.com/ZbxxBZ/lingxin-robotic-hand-control.git
cd lingxin-robotic-hand-control
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip

# 官方 SDK 不在 PyPI 上，直接从 GitHub 装
python -m pip install "linkerbot-transport[all] @ git+https://github.com/linker-bot/linkerbot-transport.git"
python -m pip install "linkerbot-hand @ git+https://github.com/linker-bot/linkerbot-hand-python-sdk.git"

# 本项目（-e：改代码后不用重装），会一起装上 MediaPipe、OpenCV 等依赖
python -m pip install -e .
```

方括号里可以加可选功能，例如 `pip install -e ".[plot,test]"`：`plot` = `05_cup_demo.py` 的图表（matplotlib），`build` = 打包 exe（PyInstaller），`test` = pytest。

装好后有两个命令（在激活了环境的终端里，任何目录都能用）：

| 命令 | 等同于 | 作用 |
| --- | --- | --- |
| `l10-dashboard` | `python -m l10ctl` | 网页控制台 |
| `l10-mirror` | `python -m l10ctl.mirror_app` | 摄像头镜像（独立窗口） |

**CAN 适配器：**
- Windows + PEAK PCAN-USB：安装 PEAK 官方驱动（包含 PCANBasic 库），`INTERFACE = "pcan"`、`CHANNEL = "PCAN_USBBUS1"`（默认）。
- Linux + SocketCAN：`INTERFACE = "socketcan"`、`CHANNEL = "can0"`，先把接口拉起来：

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 1000000
sudo ip link set can0 up
```

## 设置（不用改源码）

所有参数的默认值和说明都在 [`l10ctl/config.py`](l10ctl/config.py)。要改哪项，就把它写进 `l10_settings.toml`，例如右手 + Linux：

```toml
SIDE = "right"
INTERFACE = "socketcan"
CHANNEL = "can0"

[POSES]                # 表只覆盖写到的项，其余保持默认
open = [255, 255, 255, 255, 255, 255, 128, 67, 89, 255]
```

从 [`l10_settings.example.toml`](l10_settings.example.toml) 复制一份开始改。程序按下面的顺序找设置文件，只用找到的第一个：

1. 环境变量 `L10_SETTINGS` 指定的文件
2. 运行命令时的当前目录下的 `l10_settings.toml`
3. 数据目录下的 `l10_settings.toml`（打包的 exe：exe 旁边）

- 名字写错、类型不对（比如姿态不是 10 个 0~255 的整数）会直接报错并指出是哪一项，不会悄悄用默认值。
- `python -m l10ctl.settings` 打印正在用哪个设置文件、改了哪些项、数据目录在哪。
- 连接参数也可以用命令行临时覆盖：`--side right --interface socketcan --channel can0`。
- `l10_settings.toml` 已经在 `.gitignore` 里，每个人、每只手用自己的。

**数据目录**放运行时生成的文件：镜像标定 `mirror_calibration.json`、下载的手部识别模型 `hand_landmarker.task`。

| 情况 | 位置 |
| --- | --- |
| 设了环境变量 `L10_DATA_DIR` | 那个目录 |
| 打包的 exe | exe 所在的文件夹 |
| Windows | `%LOCALAPPDATA%\l10ctl` |
| macOS | `~/Library/Application Support/l10ctl` |
| Linux | `$XDG_DATA_HOME/l10ctl`（默认 `~/.local/share/l10ctl`） |

> 从旧版本升级：旧版把这两个文件放在项目目录（`mirror_calibration.json`、`models/hand_landmarker.task`），拷到数据目录就能继续用。

## 建议的运行顺序

在激活了环境的终端里、仓库根目录下：

1. `python examples/01_check_connection.py`：确认能连上、身份是 L20Lite，并看看当前角度、温度和故障。手不会动。
2. `python examples/01_check_connection.py --watch-force`：用手指按压各指尖，记下轻 / 中 / 重按时的读数，用来设置 `OBJECTS` 的 `target_force` 和 `FORCE_HARD_LIMIT`。它也会告诉你这只手该用哪种力数据来源（`FORCE_SOURCE`）。
3. `python examples/04_joint_jog.py`：逐个关节核对方向，调出适合你这只手的 `open` 等姿态和每个物体的 `target`。输入 `s` 会打印成可以直接写进设置文件的格式。
4. `python examples/02_gestures.py`：试预设手势。
5. `python examples/03_smart_grasp.py`：力反馈抓取。
6. `python examples/05_cup_demo.py`：拿杯子演示（要装 `plot`）。
7. `l10-dashboard`：网页控制台，会自动打开 http://127.0.0.1:8765 。
8. `l10-mirror --no-hand`：摄像头镜像，先不连手标定，再连手跟随或玩石头剪刀布（见下文）。

身份校验失败时运行 `python examples/00_probe_identity.py`，看固件应答了哪些查询帧。

## 网页控制台（l10-dashboard）

直接用 SDK 控制手，取代官方的 can_bridge + 控制台组合，所以同样不能和它们同时运行。

```powershell
l10-dashboard                    # 自动打开浏览器
l10-dashboard --no-browser --port 8766
l10-dashboard --self-test        # 只检查程序文件是否完整（不连手、不开摄像头）
```

- **启动不会动**：程序先读当前角度作为目标，之后只在你操作时才发指令；退出时手保持当前姿态。
- **关节控制**：10 个滑块，拖动即发送；绿色竖线是实测角度。底部可以设置全局速度。
- **预设动作**：官方控制台的 12 个常用动作、数字 1–9、掌部左/中/右、“重置”，外加 `POSES` 里的齿轮版 GUI 预设。鼠标悬停可以看到具体数值。只发手指帧的预设（例如“握拳”）会保持掌部 4 个关节不变，和官方控制台一致。
- **动画**：数字倒计时 9→0、数字正计时 1→9（同样用真机调整过的数字 7、8 并分段过渡）、墨西哥波浪、手指波浪、波浪、逐指波浪、横向摆动、Refill Core、竖拇指、食指指点、握拳-张开。可以改间隔（最小 50 ms）和循环次数（0 = 无限）。拖滑块或点预设会打断动画。
- **可视化**：手部 3D 视图（拖动旋转、双击复位，可切换掌心 / 侧面 / 手背 / 斜视；实体为实测姿态，半透明蓝色为目标，指尖颜色表示压力）、五指 12×6 触觉热力图、指尖力条和最近 10 秒曲线、电机温度、故障和日志。
- **官方 3D 模型**：3D 视图用的是灵心巧手官方的 L10 模型（[linker-bot/linker-sim-assets](https://github.com/linker-bot/linker-sim-assets) 的 `hands/l20lite`，Apache-2.0），按官方给的 `sdk_channels.yaml` 把 10 个原始值换算成关节角（原始值 255 = 关节下限，0 = 上限），指节联动按 URDF。`l10ctl/web/model/` 下的文件由 `tools/convert_l10_model.py` 生成，仓库里已经带了；官方模型更新后：`git clone` 那个仓库，再运行 `python tools/convert_l10_model.py --assets <linker-sim-assets>/hands/l20lite`。浏览器不支持 WebGL2 或模型文件缺失时，自动退回简化示意图。
- **压力卸载（按指尖力）**：网页上的手势预设到位后 3 秒内，如果某根手指的指尖力（点阵最大值）超过 50，就把它的弯曲关节一点点往伸直方向退，直到不超限（最多退 60）。日志会显示“拇指力 115 → 45（拇指弯曲 0 → 20）”这样的结果。参数在 `config.py` 的“压力卸载”一节；只用于手势预设，`03`/`05` 的抓取不受影响。
- **堵转卸力**：预设到位后，如果某个关节被挡住停下（例如数字 1、9 里拇指压在弯曲的手指上），会把它的目标改为“停住的位置再多一点”，保持轻轻接触而不再全力顶着；日志里会显示实际停住的角度，觉得合适可以写进设置文件的 `[USER_OVERRIDES]`。参数在 `config.py` 的“堵转卸力”一节。
- **防干涉过渡**：切换预设（以及数字动画）时，只要四指要动，并且拇指也要大幅运动（如数字 6 → 7）或拇指折在掌心（如数字 1 → 2、2 → 3，被压的手指要伸直），就会自动分段：拇指先抬到完全张开，四指到位后拇指再回到目标。`examples/` 里的 `move_to` 也用同样的逻辑。
- **真机调整值**：在真机上调好的姿态写在 `config.USER_OVERRIDES`（目前有捏取、数字 7、数字 8），会覆盖同名预设，数字动画也用这些值。你的手不一样时，在设置文件的 `[USER_OVERRIDES]` 里改。
- **停止并保持**（顶栏红色按钮或 Esc）：停止动画 / 跟随 / 游戏，并把目标设为当前实测角度。
- **镜像跟随 / 游戏 / 握手**：页面顶部的选项卡，用法见下面几节。
- **安全**：只监听 127.0.0.1，并拒绝其他网页发来的跨站请求。

迁移的数值与官方控制台完全一致（用脚本对照 exe 里提取的页面代码逐项核对过）。官方把掌部预设标为“原 L10”，在齿轮拇指版本上，个别动作的拇指和侧摆效果可能和官方演示不同；第一次试动画时请先把速度和间隔调慢。

## 摄像头镜像 + 石头剪刀布

用普通 USB 摄像头（笔记本自带的也行）识别你的手，L10 实时跟着做；也可以和 L10 玩石头剪刀布。第一次用前下载手部识别模型（约 7.5 MB，放到数据目录，只需一次，要能访问 storage.googleapis.com）：

```powershell
l10-mirror --download-model
```

### 在网页里用（推荐）

只开 `l10-dashboard` 这一个程序，PCAN 就不会冲突。页面顶部“镜像跟随”和“游戏”两个选项卡共用同一个摄像头画面。跟随或游戏进行时，选项卡上会显示“跟随中 / 进行中”，切到别的页也照常运行：

1. 点“开启摄像头”。外接摄像头先把编号改成 1。画面是镜像显示的，上面画好了手部骨架。
2. 标定：摆好姿势再点对应按钮（张开五指 / 握拳 / 拇指碰小指根），每个姿势保持 1 秒。标定存在数据目录，和 `l10-mirror` 共用。
3. 在“镜像跟随”页点“开始跟随”，L10 就跟着你的手动。3D 视图里蓝色是发出的目标，实体是实测姿态。
4. 游戏：切到“游戏”页，点“进入游戏”，再按空格或点“开一局”。比分和口令直接显示在画面上。

- 按 **Esc**（或“停止并保持”）会立即停止跟随或猜拳。拖滑块、点预设或动画也会自动停止。
- 跟随和游戏时，速度自动切到 `MIRROR_SPEED`；结束后恢复成网页上设置的速度。
- **手不动时还在抖**：调“镜像跟随”页的“防抖”滑块。默认 6，越大越稳，但细小动作跟得越少，调完马上生效。觉得合适就写进设置文件的 `MIRROR_DEADBAND`，不然重启后会恢复默认。
- 防撞、堵转卸力和按力卸载的提示会写在日志里。卡片上也会显示当前在防撞的哪个阶段，以及哪些关节正在卸力。

### 单独运行（l10-mirror）

不想开网页时用。它自己有显示窗口，但不能和网页控制台同时运行。

```powershell
l10-mirror --no-hand     # 不连手：只看识别效果和右侧映射出的关节值，先在这里标定
l10-mirror               # 连手：先张开、去皮，然后暂停，按空格开始跟随
l10-mirror --game        # 直接进入石头剪刀布
l10-mirror --camera 1    # 外接摄像头
```

在 `--no-hand` 窗口里做三次标定，每个姿势保持 1 秒：

1. 五指张开，拇指伸向外侧，按 O。
2. 握拳，拇指压在食指和中指外面，按 F。
3. 拇指尖碰小指根，按 T。

标完后动一动手，看右侧的白色竖线（人手映射值）：张开时应接近 255，握拳时应接近 0。如果某个关节不对，按 P 会在终端打印各项特征值，照着调 `MIRROR_FEATURE_DEFAULTS`。

| 按键（先用鼠标点一下窗口） | 作用 |
| --- | --- |
| 空格 | 开始 / 暂停跟随；猜拳模式下开始一局 |
| G | 在镜像跟随和石头剪刀布之间切换 |
| O / F / T | 标定：张开五指 / 握拳 / 拇指尖碰小指根 |
| X | 清除标定，恢复默认映射 |
| P | 在终端打印当前特征和映射值 |
| R | 暂停跟随并张开手 |
| Q / Esc | 张开后退出 |

窗口里的中文用系统自带字体（Windows 雅黑 / 黑体、macOS 苹方、Linux Noto CJK / 文泉驿）；都没有时在设置文件里写 `MIRROR_FONT = "字体文件路径"`。

**怎么映射**：
- 四指弯曲：人手掌指关节和近端指间关节的弯曲角。
- 侧摆：相邻手指之间的夹角。手指越弯，侧摆越回到中位。
- 拇指弯曲：拇指弯曲角。
- 拇指翻滚：拇指掌骨转到掌心前方的角度。
- 拇指旋转：拇指尖横过掌心的位置。
- **握拳辅助**：握拳时按位置换算出的拇指旋转偏小，L10 拇指会横着插进弯下的手指里卡住。所以四指都弯、你的拇指也收在掌前时，L10 拇指会逐渐带到握拳预设的位置（弯曲 90、翻滚 0、旋转 197）。竖大拇指不触发。开关和范围在 `MIRROR_FIST_*`。
- **捏取辅助**：逐个关节换算时，拇指尖和其他指尖不一定能对上。所以你的拇指尖越靠近哪根手指的指尖，L10 的拇指和那根手指就越接近调好的捏取姿态，其余手指照常跟随。目前有食指和中指两组（`MIRROR_PINCH_TARGETS`，在真机网页控制台上调的）；拇指同时靠近两根手指（三指捏）时两组姿态平滑混合。被捏的手指弯得很深（握拳）时不触发。网页状态栏会显示“捏取（食指）xx%”。要加无名指或小指，在网页控制台调好姿态，照格式往 `MIRROR_PINCH_TARGETS` 加一项。

左手、右手都能用。画面是镜像显示的，像照镜子。

**跟随时的保护**（相当于网页预设里防干涉过渡和卸力的实时版本）：
- **防撞**（按状态判断，人手动得再慢也不会漏掉）：拇指要往里折时，四指必须已经停稳，否则拇指在外面等。拇指折着时记下四指的位置，四指只能往合拢方向微调、不往伸直方向走；人手要四指明显移动（超过 `MIRROR_FOLDED_MOVE`），拇指先往外退开（只退不进），四指原地等。拇指被压住退不开时，停住 0.4 秒或等满 3 秒就不再等。四指弯着时，拇指不会折得比握拳预设更深（旋转不低于 197），松拳途中不会横扫进手指。
- **堵转卸力**：关节往合拢方向被挡住不动时，不再继续往里顶。等人手张开回来，限制自动解除。
- **按力卸载**：指尖力超过 `MIRROR_FORCE_LIMIT`（默认 50）时，那根手指往伸直方向一点点退。
- **捏取时放宽防撞**：捏取程度超过一半时，被捏的那根手指不参与上面的判断，拇指和它可以同时动，不用等它停稳。
- **识别中断**：没看到手时，L10 保持当前姿态，不会乱动。

界面里关节条变成黄色，表示这个关节正在卸力。蓝色是发出的指令，橙色是实测值。

**石头剪刀布**：
- 按空格开一局。L10 四指一握一松，打两拍（“石头…”“剪刀…”），第三拍“布！”时出拳。
- L10 出什么在开局时就随机定好了，不看你的手。你的手势在“布！”之后 0.15–0.85 秒内识别，取出现次数最多的那个。
- 判断手势只看四指：四指都弯是石头，都伸直是布，食指和中指伸直、无名指和小指弯曲是剪刀。
- L10 的出拳姿态在 `GAME_POSES` 里：石头 = 握拳，布 = 张开，剪刀 = 网页控制台的数字 2。出拳同样走分段过渡、堵转卸力和压力卸载。

**常用调参**（`config.py` 的“摄像头镜像”和“石头剪刀布”两节，写进设置文件覆盖）：
- 跟得慢：`MIRROR_SPEED` 调到 200。手静止时抖：`MIRROR_DEADBAND` 调大；动作时发飘：`MIRROR_FILTER_MIN_CUTOFF` 调小。
- 外接摄像头：`MIRROR_CAMERA_INDEX = 1`（或 `--camera 1`）。
- 侧摆乱动：`MIRROR_ABDUCTION = false`，侧摆就固定在中位。
- 喊口令的节奏：`GAME_BEAT_S`。

第一次跟随时保持默认速度，手慢慢动，确认每个关节方向都对了再加快。方向反了就把 `MIRROR_JOINT_MAP` 里对应关节的两个原始值对调。

## 握手（网页“握手”页）

1. 点“开始握手”：L10 摆到准备姿态（调好的握手姿态再张开一些），然后记录各指尖的零点。**这时别碰指尖。**
2. 状态变成“把手伸进来握手”后，把手伸进来。碰到任一指尖就自动开始握；也可以点“握”或按空格。
3. 五根手指各自慢慢合拢，**每根手指按自己的指尖力决定停在哪**：
   - 指尖力到了“握力”（默认 25）→ 碰到了，停下。手大的人碰得早、停得早，手小的人多弯一些，力度差不多。
   - 碰到你手的侧面、指尖读不到力时 → 指令已经领先到上限、实际位置 0.5 秒内几乎不动（累计不到 3），判断为挡住，停在挡住的位置轻轻贴着。
   - 快碰到时会放慢，停下时更接近设定的握力。
   - 拇指等四指里有一根碰到或挡住（确认手在里面）才动。没有手时四指到头就松开，拇指不会撞进弯下的四指。
   - 拇指主要靠弯曲包住你的手：翻滚只动一点，不横扫。拇指的握力和“捏太紧”上限都是四指的 60%，合拢也更慢。
   - 合拢时指令最多比实际位置领先一点（拇指 12、四指 20），被挡住时不会越压越深；握住后哪个关节又开始往里顶，就退回到实测位置。
4. 握住后把手往外抽就自动松开，回到准备姿态，可以再握。每根手指单独判断有没有失去接触：碰到指尖的看指尖力是否掉到它握稳时的 40% 以下，被挡住的看是否往里走了。一半以上失去接触、连续 0.2 秒就松开。
5. 点“松开”或按 **Esc** 会立即松开。握着时按 Esc 是松开，不是保持。

- **握力滑块**：调完马上生效，越大握得越紧（最大不超过“捏太紧”的上限）。满意的值写进设置文件的 `HANDSHAKE_GRIP_FORCE`。
- **你用力回握时**：某根手指的指尖力超过 `HANDSHAKE_FORCE_LIMIT`（60，拇指 36），这根手指往回让一让，最多让 24；你放松后它慢慢回到原来握住的位置。捏一下再放松、或只有一根手指离开，都不会被当成抽手。
- **三个开关**：“碰到指尖自动开始握”和“抽手自动松开”可以关掉，改成全靠按钮；“拇指一起握”关掉后只用四指。
- 最长握 `HANDSHAKE_MAX_HOLD_S`（15 秒）后自动松开。
- 握手姿态（`HANDSHAKE_POSE`，按你的手在网页控制台上调）、准备 / 最紧的张开量、速度、各种阈值都在 `config.py` 的“握手”一节。

和镜像跟随、游戏一样，握手进行时拖滑块、点预设或动画会自动结束握手。握手期间速度临时用 `HANDSHAKE_SPEED`，结束后恢复。

## 拿杯子演示（examples/05_cup_demo.py）

启动后手会张开、摆好预成形姿态，然后去皮：这几秒内不要碰指尖。之后弹出显示窗口，先用鼠标点一下窗口，让它接收键盘。

| 操作 | 效果 |
| --- | --- |
| 用杯子轻碰任一指尖，或按空格 | 0.8 秒后合手，接触平均力到目标力就停 |
| 握住后把杯子往外拽，或按 R | 松手，回到等待状态 |
| Z | 重新去皮（手张开、没接触时按） |
| Q 或关闭窗口 | 张开后退出 |

- 握杯姿态、目标力、监控哪几根手指：`OBJECTS["cup"]`（设置文件里写 `[OBJECTS.cup]`）。先用 `04_joint_jog.py` 或网页控制台调出能包住杯子的 `target`。
- 触发和拽拉的灵敏度：`DEMO_TRIGGER_FORCE`、`DEMO_TUG_ABS`、`DEMO_TUG_REL` 等。可以加 `--log run.csv` 运行一次，看记录下的力值再调。
- 不想自动触发或自动松手：`DEMO_AUTO_GRASP = false` 或 `DEMO_AUTO_RELEASE = false`，只用按键控制。

建议用空纸杯：轻力握住不变形，就能说明力控在起作用。

## 抓取逻辑（examples/03_smart_grasp.py）

1. 张开；
2. 预成形：拇指翻滚 / 旋转和侧摆先摆到目标值，五个弯曲关节保持伸直；
3. 五个弯曲关节以 `STEP_DT_S`（默认 50 ms）为步长，从伸直逐步插值到 `target`，每步读一次指尖力：
   - 已接触手指的平均力 ≥ `target_force`：停在当前位置；
   - 任一被监控手指 ≥ `FORCE_HARD_LIMIT`：立即停止；
   - 读力失败：停止闭合（看不到力时继续闭合不安全）；
   - 走到 `target` 仍未达到目标力：停在 `target`，不会无限加力；
4. 保持 `hold_s` 秒，按回车后释放。

按 Ctrl+C 中断时，手保持当前姿态（避免直接张开导致物体掉落），程序会询问是否张开。

## 打包成 exe（Windows）

```powershell
python -m pip install -e ".[build]"
l10-mirror --download-model          # 手部识别模型会被打进 exe
python -m PyInstaller tools/l10_console.spec --noconfirm
```

输出 `dist/L10_Console_v<版本>.exe`（版本号在 `l10ctl/__init__.py`）。双击运行：连接 L10、启动网页控制台并自动打开浏览器，不用装 Python。

- exe 读写的是它旁边的文件：`mirror_calibration.json`（标定）、`l10_settings.toml`（设置）。拷到别的电脑时一起拷；改参数写进 exe 旁边的设置文件，不用重新打包。
- 电脑上要装 PEAK 驱动，exe 里不含 PCANBasic.dll。运行前同样要先关掉 can_bridge、官方控制台和其他占用 PCAN 的程序。
- 命令行窗口会显示连接状态和日志。关掉窗口就退出，手保持当前姿态。启动出错时窗口会停住，按回车再关闭。
- exe 每次启动要先把自身解压到临时目录，比直接运行 Python 慢几秒，这是正常的。
- 自检：`L10_Console_v<版本>.exe --self-test`；其他命令行参数和 `l10-dashboard` 相同。
- 用 conda 环境打包时，spec 会自动把环境的 `Library\bin` 加进 PATH，避免 exe 启动时报 `DLL load failed while importing _ctypes`。

## 测试

```powershell
python tests/run_all.py        # 或 pytest（要装 test）
```

只用模拟的手和合成的人手关键点，不连真机、不开摄像头；数据目录换成临时目录，不会读你的设置文件或改你的标定。覆盖：镜像映射与标定、捏取 / 握拳辅助、防撞和松拳不卡、防抖、到位判断（数字正计时）、握手合拢与松手、设置文件。

## 需要在真机上确认的地方

- **预设姿态**：取自官方 `linkerhand-python-sdk` GUI 的“大拇指关节齿轮版 L10”预设。早期非齿轮版 L10 的拇指翻滚、旋转和侧摆数值不同（`config.py` 注释里列了官方示例用的那组）。第一次运行 02/03 前，先用 04 核对 `open` 姿态。
- **抓取姿态和力阈值**：`OBJECTS` 里的数值只是起点，需要实际标定。SDK 也注明指尖力原始值的物理标度未经验证。
- **力数据来源**：L10 有两种传感器版本。默认 `FORCE_SOURCE = "fn_max"`（12×6 触觉点阵每指的最大值，点阵版用）；`"fns"` 是每指法向汇总值，`"auto"` 先试 `fns` 读不到再用 `fn_max`。`01_check_connection.py` 会把两种来源各读一次；按压指尖时哪种在变就用哪种。
- **摄像头镜像的拇指映射**：拇指翻滚、旋转的方向是按官方模型推出来的，标定后先用 `--no-hand` 看映射值，再慢慢连手确认。

## SDK 用法（和旧包 `linkerbot` 的差异）

旧包 `linkerbot`（`from linkerbot import L6`、`hand.angle.set_angles`、`hand.stream()`）的命名空间已在 SDK 2.x 中移除，新 API 统一为：

```python
from linkerbot_hand import HandDevice
from linkerbot_transport.can import CANTransport

hand = HandDevice(model="L20Lite", side="left",
                  transport=CANTransport("PCAN_USBBUS1", "pcan", bitrate=1_000_000))
try:
    hand.open()
    hand.set_angles([255] * 10).wait(timeout=1.0)   # 10 个原始值 0~255
    print(hand.get_angles(timeout=1.0).raw)
    print(hand.get_force("fns", timeout=1.0).fns)   # 五指法向力
finally:
    hand.close()
```

本项目的封装用起来更简单（连接参数来自设置文件）：

```python
from l10ctl.hand import L10Hand

with L10Hand() as hand:
    print(hand.get_pose())
    hand.move_safely((255, 255, 255, 255, 255, 255, 128, 67, 89, 255))   # 必要时分段，避免拇指和四指相撞
```

## 与官方可视化控制台（can_bridge + l10_dashboard）的关系

- **不能同时运行。** `can_bridge_*.exe` 会通过 PCANBasic 独占 PCAN 适配器，`l10*-dashboard*.exe` 是浏览器控制页，通过 can_bridge 发 CAN 帧。运行本项目前，先关掉 can_bridge 和官方控制台；反过来也一样。
- **可以代替 `04_joint_jog.py` 调姿态。** 控制台的“关节控制”滑块关节 1–6（指令 0x01）加上“掌部控制”关节 7–10（指令 0x04），顺序与 `config.JOINT_NAMES` 的 10 个值完全一致。按顺序抄下 10 个数即可；标着“O7 专用”的关节 7 滑块对 L10 无效，忽略。
- 控制台自带的预设（掌部预设标注为“原 L10”）与 `POSES` 用的齿轮版 GUI 预设数值不同，哪一组适合你的手，以真机效果为准。

## 安全

- 第一次运行前把手固定好，清空周围，随时准备断电。
- 默认速度偏慢（`MOVE_SPEED=100`、`JOG_SPEED=60`），确认无误后再调快。
- 不要同时运行其他控制程序（ROS 节点、GUI、动捕等），以免指令冲突。
