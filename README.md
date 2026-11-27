# 灵心巧手 L10 控制代码

基于官方新版 SDK [linkerbot-hand-python-sdk](https://github.com/linker-bot/linkerbot-hand-python-sdk)（2.x，包名 `linkerbot_hand`）。

> **L10 在新 SDK 里叫 `L20Lite`。** SDK 用出厂产品码 `LHT10` 校验身份，10 个关节和 CAN 协议都与 L10 一致，所以代码里写的是 `model="L20Lite"`（已在 `l10_config.py` 里固定，不用改）。

## 文件

| 文件 | 作用 | 会不会动 |
| --- | --- | --- |
| `l10_config.py` | 连接参数、关节顺序、预设姿态、抓取参数（**改参数只改这里**） | — |
| `l10_hand.py` | `L10Hand` 封装：连接、发姿态、设速度、读角度/力/温度/故障 | — |
| `01_check_connection.py` | 只读检查：身份、当前角度、温度、故障、指尖力；`--watch-force` 持续看力 | **不动** |
| `04_joint_jog.py` | 单关节微调，用于标定姿态（从当前角度起步，速度慢） | 动 |
| `02_gestures.py` | 预设手势菜单：张开、握拳、点赞、OK、数字、演示序列 | 动 |
| `03_smart_grasp.py` | 力反馈抓取（附件 L6 `SmartGrasp` 的 L10 版） | 动 |
| `05_cup_demo.py` | 拿杯子演示：碰指尖自动抓杯、拽一下松手，窗口实时显示五指力条、力曲线（点阵版还有指尖热力图） | 动 |
| `l10_actions.py` | 从官方控制台 `l10-dashboard_2025.08.06` 迁移来的预设动作和动画（数值逐个核对过） | — |
| `06_web_dashboard.py` + `web/` | 网页控制台：关节滑块、迁移来的预设和动画、官方模型 3D 视图、触觉热力图、力曲线、温度、故障 | 动 |
| `07_camera_mirror.py` | 摄像头镜像：L10 实时模仿你的手；按 G 和 L10 玩石头剪刀布 | 动 |
| `l10_mirror.py` | 手部识别（MediaPipe）、关节映射、标定、手势判断、石头剪刀布流程和跟随保护（06、07 共用） | — |
| `l10_camera.py` | 摄像头后台线程：取最新一帧 → 识别 → 映射，并输出带骨架的画面（06、07 共用） | — |
| `l10_handshake.py` | v2 握手：按触觉合拢、挡住检测、按触觉 / 位置松手（网页“握手”页用） | — |
| `tools/convert_l10_model.py` | 把官方 L10 模型（URDF + OBJ）转换成网页用的 `web/model/` 文件 | — |
| `tools/l10_console.spec` | 把 06 网页控制台打包成一个 exe 的配置（PyInstaller） | — |
| `releases/` | 打包好的版本：exe、标定文件、对应的源码快照 | — |

## 环境（conda）

- conda 环境：`l10_hand`（Python 3.12）
- SDK 源码在 `D:\project\linkerbot_sdk\` 下的 `linkerbot-transport` 和 `linkerbot-hand-python-sdk`，以可编辑方式安装。更新 SDK 时在这两个目录里 `git pull` 即可。

每次使用前（本机 PowerShell 没有做 `conda init`，所以普通 PowerShell 里 `conda activate` 用不了，二选一）：

```powershell
# 方式一：直接用环境里的 python（任何终端都行）
cd D:\project\hand_project
C:\Users\23116\.conda\envs\l10_hand\python.exe 01_check_connection.py
```

```powershell
# 方式二：开始菜单打开 “Miniforge Prompt”，再激活
conda activate l10_hand
cd /d D:\project\hand_project
python 01_check_connection.py
```

从头搭建（按 SDK quick-start，用 conda 代替 venv）：

```powershell
conda create -n l10_hand python=3.12 pip -y --override-channels -c https://conda.anaconda.org/conda-forge
conda activate l10_hand
New-Item -ItemType Directory -Path D:\project\linkerbot_sdk -Force
cd D:\project\linkerbot_sdk
git clone --branch main https://github.com/linker-bot/linkerbot-transport.git
git clone --branch main https://github.com/linker-bot/linkerbot-hand-python-sdk.git
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e "./linkerbot-transport[all]"
python -m pip install -e ./linkerbot-hand-python-sdk
python -m pip install matplotlib
python -m pip install mediapipe   # 07 摄像头镜像用，会一起装 OpenCV
```

> 本机开着 Clash 代理（127.0.0.1:7897）时，清华、中科大镜像会报 SSL 错误，所以上面直接用官方 conda-forge 源，pip 也走官方 PyPI。

使用 PCAN 时还需安装 PEAK 驱动（`桌面\机械手\PeakOemDrv.exe`），其中包含 PCANBasic 库。Linux 用 SocketCAN 时先执行：

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 1000000
sudo ip link set can0 up
```

## 配置

打开 `l10_config.py`，按实际情况修改：

```python
SIDE = "left"              # 左手 / 右手
INTERFACE = "pcan"         # "pcan" | "socketcan" | "auto"
CHANNEL = "PCAN_USBBUS1"   # pcan: PCAN_USBBUS1；socketcan: can0
```

也可以用命令行临时覆盖：`--side right --interface socketcan --channel can0`。

## 建议的运行顺序

在激活了 `l10_hand` 环境的终端里，进入本目录：

1. `python 01_check_connection.py`：确认能连上、身份是 L20Lite，并看看当前角度、温度和故障。手不会动。
2. `python 01_check_connection.py --watch-force`：用手指按压各指尖，记下轻 / 中 / 重按时的读数，用来设置 `OBJECTS[...]["target_force"]` 和 `FORCE_HARD_LIMIT`。
3. `python 04_joint_jog.py`：逐个关节核对方向，调出适合你这只手的 `open` 等姿态和每个物体的 `target`。输入 `s` 打印姿态，复制回 `l10_config.py`。
4. `python 02_gestures.py`：试预设手势。
5. `python 03_smart_grasp.py`：力反馈抓取。
6. `python 05_cup_demo.py`：拿杯子演示（需先 `python -m pip install matplotlib`）。
7. `python 06_web_dashboard.py`：网页控制台，会自动打开 http://127.0.0.1:8765 。
8. `python 07_camera_mirror.py --no-hand`：摄像头镜像，先不连手标定，再连手跟随或玩石头剪刀布（见下文）。

## 网页控制台（06_web_dashboard.py）

直接用 SDK 控制手，取代官方的 can_bridge + 控制台组合，所以同样不能和它们同时运行。不需要额外安装依赖。

- **启动不会动**：程序先读当前角度作为目标，之后只在你操作时才发指令；退出时手保持当前姿态。
- **关节控制**：10 个滑块，拖动即发送；绿色竖线是实测角度。底部可以设置全局速度。
- **预设动作**：官方控制台的 12 个常用动作、数字 1–9、掌部左/中/右、“重置”，外加 `l10_config.POSES` 里的齿轮版 GUI 预设。鼠标悬停可以看到具体数值。只发手指帧的预设（例如“握拳”）会保持掌部 4 个关节不变，和官方控制台一致。
- **动画**：数字倒计时 9→0、数字正计时 1→9（本项目新增，同样用真机调整过的数字 7、8 并分段过渡）、墨西哥波浪、手指波浪、波浪、逐指波浪、横向摆动、Refill Core、竖拇指、食指指点、握拳-张开。可以改间隔（最小 50 ms）和循环次数（0 = 无限）。拖滑块或点预设会打断动画。
- **可视化**：手部 3D 视图（拖动旋转、双击复位，可切换掌心 / 侧面 / 手背 / 斜视；实体为实测姿态，半透明蓝色为目标，指尖颜色表示压力）、五指 12×6 触觉热力图、指尖力条和最近 10 秒曲线、电机温度、故障和日志。
- **官方 3D 模型**：3D 视图用的是灵心巧手官方的 L10 模型（`linker-bot/linker-sim-assets` 仓库的 `hands/l20lite`，Apache-2.0，源码放在 `D:\project\linkerbot_sdk\linker-sim-assets`），按官方给的 `sdk_channels.yaml` 把 10 个原始值换算成关节角（原始值 255 = 关节下限，0 = 上限），指节联动按 URDF。`web/model/` 下的文件由 `tools/convert_l10_model.py` 生成；官方模型更新后，在 `linker-sim-assets` 里 `git pull` 再运行一次这个脚本即可。浏览器不支持 WebGL2 或模型文件缺失时，自动退回简化示意图。
- **压力卸载（按指尖力）**：网页上的手势预设到位后 3 秒内，如果某根手指的指尖力（点阵最大值）超过 50，就把它的弯曲关节一点点往伸直方向退，直到不超限（最多退 60）。日志会显示“拇指力 115 → 45（拇指弯曲 0 → 20）”这样的结果。阈值等在 `l10_config.py` 的“压力卸载”一节；只用于手势预设，`03`/`05` 的抓取不受影响。
- **堵转卸力**：预设到位后，如果某个关节被挡住停下（例如数字 1、9 里拇指压在弯曲的手指上），会把它的目标改为“停住的位置再多一点”，保持轻轻接触而不再全力顶着；日志里会显示实际停住的角度，觉得合适可以写进 `USER_OVERRIDES`。开关和参数在 `l10_config.py` 的“堵转卸力”一节。
- **停止并保持**（顶栏红色按钮或 Esc）：停止动画，并把目标设为当前实测角度。
- **镜像跟随 / 游戏（石头剪刀布）**：页面顶部的“镜像跟随”和“游戏”两个选项卡，功能和 `07_camera_mirror.py` 一样（同一套识别、标定文件和保护），但不用另开程序，也就不会和网页抢 PCAN。用法见下一节“在网页里用”。
- **握手（v2）**：页面顶部的“握手”选项卡，用法见下一节“握手（v2）”。
- **安全**：只监听 127.0.0.1，并拒绝其他网页发来的跨站请求。

- **防干涉过渡**：切换预设（以及数字倒计时动画）时，只要四指要动，并且拇指也要大幅运动（如数字 6 → 7）或拇指折在掌心（如数字 1 → 2、2 → 3，被压的手指要伸直），就会自动分段：拇指先抬到完全张开，四指到位后拇指再回到目标。开关和阈值在 `l10_config.py` 的“防干涉过渡”一节；`02`/`03`/`05` 里的 `move_to` 也用同样的逻辑。
- **真机调整值**：在真机上调好的姿态写在 `l10_actions.py` 的 `USER_OVERRIDES`，会覆盖同名预设（目前有捏取、数字 7、数字 8），数字倒计时动画也会用这些值。

迁移的数值与官方控制台完全一致（用脚本对照 exe 里提取的页面代码逐项核对过）。官方把掌部预设标为“原 L10”，在齿轮拇指版本上，个别动作的拇指和侧摆效果可能和官方演示不同；第一次试动画时请先把速度和间隔调慢。

## 拿杯子演示（05_cup_demo.py）

启动后手会张开、摆好预成形姿态，然后去皮：这几秒内不要碰指尖。之后弹出显示窗口，先用鼠标点一下窗口，让它接收键盘。

| 操作 | 效果 |
| --- | --- |
| 用杯子轻碰任一指尖，或按空格 | 0.8 秒后合手，接触平均力到目标力就停 |
| 握住后把杯子往外拽，或按 R | 松手，回到等待状态 |
| Z | 重新去皮（手张开、没接触时按） |
| Q 或关闭窗口 | 张开后退出 |

调参都在 `l10_config.py`：
- 握杯姿态、目标力、监控哪几根手指：`OBJECTS["cup"]`。先用 `04_joint_jog.py` 或官方控制台调出能包住杯子的 `target`。
- 触发和拽拉的灵敏度：`DEMO_TRIGGER_FORCE`、`DEMO_TUG_ABS`、`DEMO_TUG_REL` 等。可以加 `--log run.csv` 运行一次，看记录下的力值再调。
- 不想自动触发或自动松手：把 `DEMO_AUTO_GRASP` 或 `DEMO_AUTO_RELEASE` 设为 `False`，只用按键控制。

建议用空纸杯：轻力握住不变形，就能说明力控在起作用。

## 握手（v2，网页“握手”页）

v2.0 在网页控制台里加了握手。还是运行 `06_web_dashboard.py`（或以后打包的 v2 exe），切到“握手”选项卡：

1. 点“开始握手”：L10 摆到准备姿态（你调好的握手姿态再张开一些），然后记录各指尖的零点。**这时别碰指尖。**
2. 状态变成“把手伸进来握手”后，把手伸进来。碰到任一指尖就自动开始握；也可以点“握”或按空格。
3. 五根手指各自慢慢合拢，**每根手指按自己的指尖力决定停在哪**：
   - 指尖力到了“握力”（默认 25）→ 碰到了，停下。手大的人碰得早、停得早，手小的人多弯一些，力度差不多。
   - 碰到你手的侧面、指尖读不到力时 → 指令已经领先到上限、实际位置 0.5 秒内几乎不动（累计不到 3），判断为挡住，停在挡住的位置轻轻贴着。用一段时间的累计变化判断，手指走得慢也不会被误判。
   - 快碰到时会放慢，停下时更接近设定的握力。
   - 拇指等四指里有一根碰到或挡住（确认手在里面）才动。没有手时四指到头就松开，拇指不会撞进弯下的四指。
   - 拇指主要靠弯曲包住你的手：翻滚只动一点（准备时 107，最多到 77），不横扫。拇指的握力和“捏太紧”上限都是四指的 60%，合拢也更慢。
   - 合拢时指令最多比实际位置领先一点（拇指 12、四指 20），被挡住时不会越压越深；握住后哪个关节又开始往里顶，就退回到实测位置。
4. 握住后把手往外抽就自动松开，回到准备姿态，可以再握。每根手指单独判断有没有失去接触：碰到指尖的看指尖力是否掉到它握稳时的 40% 以下，被挡住的看是否往里走了。一半以上失去接触、连续 0.2 秒就松开（模拟里约 0.1～0.2 秒）。握住后马上就开始判断，刚握上就抽手也能松开。
5. 点“松开”或按 **Esc** 会立即松开。握着时按 Esc 是松开，不是保持。

- **握力滑块**：调完马上生效，越大握得越紧（最大不超过“捏太紧”的上限）。满意的值写进 `l10_config.HANDSHAKE_GRIP_FORCE` 才会保存。
- **你用力回握时**：某根手指的指尖力超过 `HANDSHAKE_FORCE_LIMIT`（60，拇指 36），这根手指往回让一让，最多让 24；你放松后它慢慢回到原来握住的位置。让着的手指不参与抽手判断，所以捏一下再放松不会被误当成抽手；只有一根手指离开（手动了一下）也不会松。
- **三个开关**：“碰到指尖自动开始握”和“抽手自动松开”可以关掉，改成全靠按钮；“拇指一起握”关掉后只用四指，拇指停在准备姿态（拇指按得不舒服时先这样用）。
- 最长握 `HANDSHAKE_MAX_HOLD_S`（15 秒）后自动松开。
- 握手姿态、准备 / 最紧的张开量、速度、各种阈值都在 `l10_config.py` 的“握手”一节。

和镜像跟随、游戏一样，握手进行时拖滑块、点预设或动画会自动结束握手。握手期间速度临时用 `HANDSHAKE_SPEED`，结束后恢复。

## 摄像头镜像 + 石头剪刀布（07_camera_mirror.py）

用普通 USB 摄像头（笔记本自带的也行）识别你的手，L10 实时跟着做；按 G 切换成和 L10 玩石头剪刀布。需要 `mediapipe`，已经装进 `l10_hand` 环境，OpenCV 也会一起装上。

### 在网页里用（推荐）

同样的功能已经放进 06 网页控制台。只开网页这一个程序，PCAN 就不会冲突：

```powershell
C:\Users\23116\.conda\envs\l10_hand\python.exe D:\project\hand_project\06_web_dashboard.py
```

页面顶部有“手部控制 | 镜像跟随 | 游戏”三个选项卡。后两个共用同一个摄像头画面，右侧分别是跟随和游戏的按钮。跟随或游戏进行时，选项卡上会显示“跟随中 / 进行中”，切到别的页也照常运行：

1. 点“开启摄像头”。外接摄像头先把编号改成 1。画面是镜像显示的，上面画好了手部骨架。
2. 标定：摆好姿势再点对应按钮（张开五指 / 握拳 / 拇指碰小指根），每个姿势保持 1 秒。标定结果和 07 共用同一个文件，已经在 07 里标过就不用再标。
3. 在“镜像跟随”页点“开始跟随”，L10 就跟着你的手动。3D 视图里蓝色是发出的目标，实体是实测姿态。
4. 游戏（石头剪刀布）：切到“游戏”页，点“进入游戏”，再按空格或点“开一局”。比分和口令直接显示在画面上。

- 按 **Esc**（或“停止并保持”）会立即停止跟随或猜拳。拖滑块、点预设或动画也会自动停止。
- 跟随和游戏时，速度自动切到 `MIRROR_SPEED`；结束后恢复成网页上设置的速度。
- **手不动时还在抖**：调“镜像跟随”页的“防抖”滑块。默认 6，越大越稳，但细小动作跟得越少，调完马上生效。觉得合适就把数值写进 `l10_config.py` 的 `MIRROR_DEADBAND`，不然重启后会恢复默认。
- 防撞、堵转卸力和按力卸载的提示会写在日志里。卡片上也会显示当前在防撞的哪个阶段，以及哪些关节正在卸力。

### 单独运行（07_camera_mirror.py）

不想开网页时，也可以单独运行 07。它自己有显示窗口，但不能和网页控制台同时运行。

第一次按下面的顺序来：

```powershell
python 07_camera_mirror.py --download-model
```

```powershell
python 07_camera_mirror.py --no-hand
```

- `--download-model`：把 MediaPipe 手部模型下载到 `models\hand_landmarker.task`。文件约 7.5 MB，只需要下一次，要求能访问 storage.googleapis.com。
- `--no-hand`：不连手，只看识别效果和右侧映射出的关节值。

在 `--no-hand` 窗口里做三次标定。每个姿势保持 1 秒，结果存在 `mirror_calibration.json`：

1. 五指张开，拇指伸向外侧，按 O。
2. 握拳，拇指压在食指和中指外面，按 F。
3. 拇指尖碰小指根，按 T。

标完后动一动手，看右侧的白色竖线（人手映射值）：张开时应接近 255，握拳时应接近 0。如果某个关节不对，按 P 会在终端打印各项特征值，照着调 `MIRROR_FEATURE_DEFAULTS`，或者把输出发给我。

确认无误后再连手：

```powershell
python 07_camera_mirror.py
```

```powershell
python 07_camera_mirror.py --game
```

- 不带参数时，手会先张开、去皮，然后处于暂停状态，按空格才开始跟随。
- `--game` 启动后直接进入石头剪刀布。

| 按键（先用鼠标点一下窗口） | 作用 |
| --- | --- |
| 空格 | 开始 / 暂停跟随；猜拳模式下开始一局 |
| G | 在镜像跟随和石头剪刀布之间切换 |
| O / F / T | 标定：张开五指 / 握拳 / 拇指尖碰小指根 |
| X | 清除标定，恢复默认映射 |
| P | 在终端打印当前特征和映射值 |
| R | 暂停跟随并张开手 |
| Q / Esc | 张开后退出 |

**怎么映射**：
- 四指弯曲：人手掌指关节和近端指间关节的弯曲角。
- 侧摆：相邻手指之间的夹角。手指越弯，侧摆越回到中位。
- 拇指弯曲：拇指弯曲角。
- 拇指翻滚：拇指掌骨转到掌心前方的角度。
- 拇指旋转：拇指尖横过掌心的位置。
- **握拳辅助**：握拳时按位置换算出的拇指旋转偏小，L10 拇指会横着插进弯下的手指里卡住。所以四指都弯、你的拇指也收在掌前时，L10 拇指会逐渐带到握拳预设的位置（弯曲 90、翻滚 0、旋转 197）。竖大拇指不触发。开关和范围在 `MIRROR_FIST_*`。
- **捏取辅助**：逐个关节换算时，拇指尖和其他指尖不一定能对上。所以你的拇指尖越靠近哪根手指的指尖，L10 的拇指和那根手指就越接近调好的捏取姿态，其余手指照常跟随。目前有食指和中指两组（`MIRROR_PINCH_TARGETS`，都取自真机网页控制台）；拇指同时靠近两根手指（三指捏）时两组姿态平滑混合。被捏的手指弯得很深（握拳）时不触发。网页状态栏会显示“捏取（食指）xx%”。要加无名指或小指，在网页控制台调好姿态，照格式往 `MIRROR_PINCH_TARGETS` 加一行。

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

**常用调参**（都在 `l10_config.py` 的“摄像头镜像”和“石头剪刀布”两节）：
- 跟得慢：`MIRROR_SPEED` 调到 200。手静止时抖：`MIRROR_DEADBAND`（防抖回差，网页上可实时调）调大；动作时发飘：`MIRROR_FILTER_MIN_CUTOFF` 调小。
- 用外接摄像头：加 `--camera 1`。
- 侧摆乱动：设 `MIRROR_ABDUCTION = False`，侧摆就固定在中位。
- 喊口令的节奏：`GAME_BEAT_S`。

和网页控制台、官方 can_bridge 一样，这个脚本也要占用 PCAN，不能同时运行。第一次跟随时保持默认速度，手慢慢动，确认每个关节方向都对了再加快。

## 打包好的程序（releases/）

`releases\` 里是打包好的网页控制台，不装 Python 也能用。每个版本一个文件夹：

| 版本 | 内容 |
| --- | --- |
| `v1.0\` | 手部控制（含数字正计时 1→9）、镜像跟随、游戏（石头剪刀布） |
| `v2.0\` | v1.0 的全部功能 + **握手**（按触觉合拢、抽手自动松开） |

每个文件夹里：

| 文件 | 说明 |
| --- | --- |
| `L10_Console_v<版本>.exe` | 双击运行：连接 L10、启动网页控制台并自动打开浏览器。功能和打包时的 `06_web_dashboard.py` 一样 |
| `mirror_calibration.json` | 镜像跟随的标定。exe 读写的是它旁边的这个文件，换电脑时和 exe 放在一起 |
| `L10_Console_v<版本>_source.zip` | 打这个包时的全部源码（含网页、模型、打包配置），以后要改这个版本或重新打包时用 |

- 运行前同样要先关掉 can_bridge、官方控制台和其他占用 PCAN 的程序。电脑上要装 PEAK 驱动，exe 里不含 PCANBasic.dll。
- 命令行窗口会显示连接状态和日志。关掉窗口就退出，手保持当前姿态。启动出错时窗口会停住，按回车再关闭。
- exe 每次启动要先把自身解压到临时目录，比直接运行 Python 慢几秒，这是正常的。
- 自检：`L10_Console_v2.0.exe --self-test`。不连手、不开摄像头，只检查程序文件是否完整。
- 命令行参数和 06 相同，例如 `L10_Console_v2.0.exe --port 8766 --no-browser`。
- 参数（`l10_config.py`）在打包时就固定了，改了参数要重新打包：

```powershell
cd D:\project\hand_project
C:\Users\23116\.conda\envs\l10_hand\python.exe -m PyInstaller tools\l10_console.spec --noconfirm
```

输出在 `dist\`，文件名里的版本号取自 `l10_config.APP_VERSION`。

## 需要在真机上确认的地方

- **预设姿态**：取自官方 `linkerhand-python-sdk` GUI 的“大拇指关节齿轮版 L10”预设。早期非齿轮版 L10 的拇指翻滚、旋转和侧摆数值不同（`l10_config.py` 注释里列了官方示例用的那组）。第一次运行 02/03 前，先用 04 核对 `open` 姿态。
- **抓取姿态和力阈值**：附件里 L6 的百分比角度和力值不能直接换算到 L10，`OBJECTS` 里的数值只是起点，需要实际标定。SDK 也注明指尖力原始值的物理标度未经验证。
- **摄像头镜像的拇指映射**：拇指翻滚、旋转的方向是按官方模型推出来的，标定后先用 `--no-hand` 看映射值，再慢慢连手确认；方向反了就把 `MIRROR_JOINT_MAP` 里对应关节的两个原始值对调。
- **力数据来源**：L10 有两种传感器版本。`FORCE_SOURCE = "auto"` 时先用每指法向汇总值 `fns`，读不到再用 12×6 触觉点阵每指的最大值 `fn_max`。`01_check_connection.py` 会把两种来源各读一次；如果按压指尖时 `fns` 一直不变而 `fn_max` 在变，就把 `FORCE_SOURCE` 改成 `"fn_max"`。

## 抓取逻辑（03_smart_grasp.py）

1. 张开；
2. 预成形：拇指翻滚 / 旋转和侧摆先摆到目标值，五个弯曲关节保持伸直；
3. 五个弯曲关节以 `STEP_DT_S`（默认 50 ms）为步长，从伸直逐步插值到 `target`，每步读一次指尖力：
   - 已接触手指的平均力 ≥ `target_force`：停在当前位置；
   - 任一被监控手指 ≥ `FORCE_HARD_LIMIT`：立即停止；
   - 读力失败：停止闭合（看不到力时继续闭合不安全）；
   - 走到 `target` 仍未达到目标力：停在 `target`，不会无限加力；
4. 保持 `hold_s` 秒，按回车后释放。

按 Ctrl+C 中断时，手保持当前姿态（避免直接张开导致物体掉落），程序会询问是否张开。

## 和附件 L6 代码的差异

附件用的是旧包 `linkerbot`（`from linkerbot import L6`、`hand.angle.set_angles`、`hand.stream()`）。该命名空间已在 SDK 2.x 中移除，新 API 统一为：

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

## 与官方可视化控制台（can_bridge + l10_dashboard）的关系

- **不能同时运行。** `can_bridge_*.exe` 会通过 PCANBasic 独占 PCAN 适配器（本机端口 8899），`l10*-dashboard*.exe` 是浏览器控制页（http://localhost:9090），通过 can_bridge 发 CAN 帧。运行本目录脚本前，先关掉 can_bridge 和 dashboard；反过来也一样。
- **可以代替 `04_joint_jog.py` 调姿态。** 控制台的“关节控制”滑块关节 1–6（指令 0x01）加上“掌部控制”关节 7–10（指令 0x04），顺序与 `l10_config.py` 的 10 个值完全一致。按顺序抄下 10 个数即可；标着“O7 专用”的关节 7 滑块对 L10 无效，忽略。
- 建议用新版 `l10-dashboard_2025.08.06.exe`：只有它能显示五指压力（每 0.5 秒刷新一次，0–255），适合在跑本目录脚本前先确认指尖传感器有读数。
- 控制台自带的预设（掌部预设标注为“原 L10”）与本目录 `POSES` 用的齿轮版 GUI 预设数值不同，哪一组适合你的手，以真机效果为准。

## 安全

- 第一次运行前把手固定好，清空周围，随时准备断电。
- `l10_config.py` 里的速度默认偏慢（`MOVE_SPEED=100`、`JOG_SPEED=60`），确认无误后再调快。
- 不要同时运行其他控制程序（ROS 节点、GUI、动捕等），以免指令冲突。
