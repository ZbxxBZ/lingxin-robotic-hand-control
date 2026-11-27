"""灵心巧手 L10 配置：连接参数、关节顺序、预设姿态、抓取参数。

L10 在新版 linkerbot-hand SDK（2.x）里的型号名是 "L20Lite"：
SDK 用出厂产品码 LHT10 校验身份，10 个关节和 CAN 帧（0x01+0x04 角度、
0x05/0x06 速度、0x20~0x23 指尖力）都与 L10 协议一致。

所有角度都是原始值 0~255：
- 弯曲关节（*_flex）：0 = 完全弯曲，255 = 完全伸直
- 侧摆 / 拇指翻滚 / 拇指旋转：0 和 255 是两个行程端点，方向以真机为准

注意：下面的姿态和力阈值是起始值，第一次用之前请用
01_check_connection.py 和 04_joint_jog.py 在真机上核对、标定。
"""

# ---------------------------------------------------------------- 版本
APP_VERSION = "2.0"  # 网页控制台 / 打包的 exe 显示的版本号（v2：加了握手）

# ---------------------------------------------------------------- 连接
SDK_MODEL = "L20Lite"  # 新 SDK 中 L10 的型号名，不要改

SIDE = "left"  # "left" 或 "right"
INTERFACE = "pcan"  # "pcan"（Windows PCAN）| "socketcan"（Linux can0）| "auto"（自动发现）
CHANNEL = "PCAN_USBBUS1"  # pcan 填 PCAN_USBBUS1；socketcan 填 can0；auto 时忽略
BITRATE = 1_000_000

TIMEOUT_S = 1.0  # 普通读取 / 指令确认的超时
# 连接时身份校验（C0/C1/C2/C4 查询）每一阶段的等待时间；SDK 默认 0.2 s，这里放宽
IDENTITY_TIMEOUT_S = 1.0
FORCE_TIMEOUT_S = 0.5  # 单次读力的总超时（需大于下面的应答窗口）
# 读取 / 指令的协议应答窗口。SDK 默认各自不同，触觉点阵（60 帧）只有 0.15 s，
# 这只手偶尔来不及全部回完会超时；放宽到 0.3 s。只在应答丢失时才会多等。
TRANSACTION_TIMEOUT_S = 0.3

# ---------------------------------------------------------------- 关节
# 顺序与 SDK 一致（见 SDK 文档 tutorials/l20lite.md）
JOINT_NAMES = (
    "thumb_flex",
    "thumb_roll",
    "index_flex",
    "middle_flex",
    "ring_flex",
    "pinky_flex",
    "index_abd",
    "ring_abd",
    "pinky_abd",
    "thumb_rotation",
)
JOINT_LABELS = (
    "拇指弯曲",
    "拇指翻滚(侧摆)",
    "食指弯曲",
    "中指弯曲",
    "无名指弯曲",
    "小指弯曲",
    "食指侧摆",
    "无名指侧摆",
    "小指侧摆",
    "拇指旋转",
)
FLEX_JOINTS = (0, 2, 3, 4, 5)  # 五根手指的弯曲关节下标
FINGERS = ("thumb", "index", "middle", "ring", "pinky")
FINGER_LABELS = {
    "thumb": "拇指",
    "index": "食指",
    "middle": "中指",
    "ring": "无名指",
    "pinky": "小指",
}

# ---------------------------------------------------------------- 速度
# 原始值 0~255，10 个关节各一个。第一次运行建议保持偏慢。
MOVE_SPEED = (100,) * 10  # 张开、手势、预成形
GRASP_SPEED = (150,) * 10  # 逐步闭合时（步长很小，速度要能跟上）
JOG_SPEED = (60,) * 10  # 单关节调试
SETTLE_S = 1.5  # 大动作后的等待时间

# ---------------------------------------------------------------- 防干涉过渡
# 两类情况会卡住或相撞：
#   - 拇指和四指同时大幅运动（例如数字 6 → 7），拇指扫过还没就位的手指；
#   - 拇指折在掌心压着手指时，被压的手指要伸直（例如数字 1 → 2、2 → 3）。
# 开启后，只要四指要动、并且拇指也要大幅运动或起止姿态里拇指是折起的，就分三段：
#   拇指先抬到 SAFE_THUMB → 四指到位 → 拇指到目标。和上一段相同的阶段会自动省略。
SAFE_TRANSITION = True
STAGE_MIN_DELTA = 40  # 关节变化超过该值才算“要动”
SAFE_THUMB = (255, 255, 255)  # 抬开拇指时的（拇指弯曲, 拇指翻滚, 拇指旋转）：完全张开
THUMB_CLEAR_FLEX = 200  # 拇指弯曲低于该值视为折起，可能压住或挡住手指
THUMB_CLEAR_ROLL = 200  # 拇指翻滚低于该值视为横在掌前
ARRIVE_TOL = 20  # 实测与目标相差在此以内视为到位

# ---------------------------------------------------------------- 堵转卸力
# 预设动作到位后，如果某个关节被挡住停下（例如数字 1、9 里拇指压在弯曲的手指上，目标 0 却停在 40 左右），
# 电机会一直全力顶着，压力很大。开启后：停住且离目标还差 RELIEF_MIN_ERROR 以上的关节，
# 目标改为“实测停住的位置再往原目标方向多 RELIEF_MARGIN”，保持轻轻接触而不再全力顶。
STALL_RELIEF = True
RELIEF_MIN_ERROR = 15
RELIEF_MARGIN = 6

# ---------------------------------------------------------------- 压力卸载（按指尖力）
# 位置卸力只看“有没有被挡住停下”；有时关节已接近目标（误差不到 ARRIVE_TOL）却仍压得很紧，
# 比如数字 1 里拇指折在手指上，拇指点阵读数 110 以上。开启后，网页控制台的预设动作到位后
# FORCE_RELIEF_WINDOW_S 秒内：哪根手指的指尖力（去皮后的点阵最大值）超过 PRESET_FORCE_LIMIT，
# 就把它的弯曲关节往伸直方向每次退 FORCE_RELIEF_STEP，直到不超限，最多退 FORCE_RELIEF_MAX。
# 只用于手势预设；03 / 05 的抓取需要用力，不受影响。
FORCE_RELIEF = True
PRESET_FORCE_LIMIT = 50
FORCE_RELIEF_STEP = 5
FORCE_RELIEF_MAX = 60
FORCE_RELIEF_WINDOW_S = 3.0
STAGE_TIMEOUT_S = 4.0  # 每段最长等待；停住不动（被挡住或到限位）也会提前进入下一段
# “停住”的判断：ARRIVE_STILL_S 秒内所有关节累计变化都不超过 ARRIVE_MOVE_EPS。
# 用一段时间的累计变化而不是相邻两次读数，慢速运动不会被误判为停住。
ARRIVE_MOVE_EPS = 3
ARRIVE_STILL_S = 0.5

# ---------------------------------------------------------------- 预设姿态
# 来自官方 linkerhand-python-sdk 的 GUI 预设（"大拇指关节齿轮版 L10"）。
# 侧摆中位：食指 128、无名指 67、小指 89。
#
# 如果你的 L10 是早期（非齿轮拇指）版本，官方 example/L10 用的是另一组值，例如：
#   张开     (255, 70, 255, 255, 255, 255, 255, 255, 255, 255)
#   拇指对食指 (135, 128, 146, 255, 255, 255, 255, 255, 255, 80)
# 两组值差别主要在拇指翻滚/旋转和侧摆，请用 04_joint_jog.py 确认后替换。
POSES = {
    "open": (255, 255, 255, 255, 255, 255, 128, 67, 89, 255),
    "fist": (90, 0, 0, 0, 0, 0, 128, 67, 89, 197),
    "thumbs_up": (255, 255, 0, 0, 0, 0, 128, 67, 89, 255),
    "ok": (84, 39, 122, 255, 255, 255, 128, 67, 89, 255),
    "one": (55, 0, 255, 0, 0, 0, 128, 67, 89, 124),
    "two": (55, 0, 255, 255, 0, 0, 128, 67, 89, 124),
    "four": (0, 0, 255, 255, 255, 255, 128, 67, 89, 255),
    "six": (255, 255, 0, 0, 0, 255, 128, 67, 89, 255),
}
POSE_LABELS = {
    "open": "张开",
    "fist": "握拳",
    "thumbs_up": "点赞",
    "ok": "OK",
    "one": "数字 1",
    "two": "数字 2",
    "four": "数字 4",
    "six": "数字 6",
}
DEMO_SEQUENCE = ("open", "fist", "open", "thumbs_up", "open", "ok", "open")

# ---------------------------------------------------------------- 智能抓取
# 对应附件 L6 程序里的 INSTRUMENT_PARAMS，改成 L10 的 10 关节原始值。
#
# target：闭合终点姿态（应略紧于“刚好接触物体”）。程序先把非弯曲关节
#         （拇指翻滚/旋转、侧摆）摆到 target 的值、手指保持伸直（预成形），
#         再把弯曲关节从伸直逐步插值到 target，每一步读一次指尖力，
#         达到 target_force 就停在当前位置。
# target_force：monitor_fingers 中已接触手指（力 > 0）的平均力（原始计数，
#         标度未经厂商标定），用 `01_check_connection.py --watch-force`
#         按压指尖观察数值后再定。
OBJECTS = {
    "glass_probe": {
        "label": "玻璃探针",
        "description": "捏取模式，适合细长器械；玻璃器械需轻拿轻放",
        "target": (84, 39, 122, 255, 255, 255, 128, 67, 89, 255),
        "target_force": 20,
        "monitor_fingers": ("thumb", "index"),
        "close_time_s": 2.5,
        "hold_s": 1.5,
    },
    "bottle": {
        "label": "瓶子",
        "description": "半握瓶子",
        "target": (110, 20, 70, 70, 70, 70, 128, 67, 89, 197),
        "target_force": 50,
        "monitor_fingers": ("thumb", "index", "middle", "ring", "pinky"),
        "close_time_s": 2.0,
        "hold_s": 1.5,
    },
    "tissue": {
        "label": "纸巾",
        "description": "纸巾拿取需较大力度才能夹住",
        "target": (70, 39, 100, 255, 255, 255, 128, 67, 89, 255),
        "target_force": 85,
        "monitor_fingers": ("thumb", "index"),
        "close_time_s": 2.0,
        "hold_s": 1.5,
    },
    "plate": {
        "label": "托盘",
        "description": "握紧托盘",
        "target": (100, 39, 110, 110, 110, 110, 128, 67, 89, 255),
        "target_force": 50,
        "monitor_fingers": ("thumb", "index", "middle", "ring", "pinky"),
        "close_time_s": 2.0,
        "hold_s": 1.5,
    },
    "cup": {
        "label": "纸杯",
        "description": "侧面包握纸杯，轻力握住不捏扁",
        "target": (110, 20, 80, 80, 80, 80, 128, 67, 89, 197),
        "target_force": 15,
        "monitor_fingers": ("thumb", "index", "middle"),
        "close_time_s": 2.0,
        "hold_s": 1.5,
    },
}

STEP_DT_S = 0.05  # 逐步闭合的步长时间（20 Hz）
# 任一被监控手指超过该值立即停止闭合（保护物体和手指），与目标力无关
FORCE_HARD_LIMIT = 150

# 力数据来源："auto" 先试 fns（每指法向汇总值），失败再用 fn 点阵最大值
#             "fns"    只用每指法向汇总值（0x20）
#             "fn_max" 只用 12×6 触觉点阵中每指的最大值
# 这只左手会应答 0xB1~0xB5（五指触觉点阵）查询，是点阵式压感版本，所以直接用 fn_max。
# 01_check_connection.py 仍会把两种来源各读一次，便于核对。
FORCE_SOURCE = "fn_max"

# ---------------------------------------------------------------- 拿杯子演示（05_cup_demo.py）
# 力值都是“去皮后”的值：启动时手张开、指尖不接触任何东西，程序会记下各指读数作为零点。
DEMO_OBJECT = "cup"  # 用 OBJECTS 里的哪一项（姿态、目标力、监控手指都从那里读）

DEMO_AUTO_GRASP = True  # True：用杯子轻碰任一指尖就开始抓；空格键始终可用
DEMO_TRIGGER_FORCE = 8  # 任一手指超过该值视为“碰到”
DEMO_TRIGGER_FRAMES = 2  # 连续多少帧超过才触发（防抖）
DEMO_TRIGGER_DELAY_S = 0.8  # 碰到后等多久再合手，留时间把杯子放进掌心

DEMO_AUTO_RELEASE = True  # True：握持中检测到被拽就松手；R 键始终可用
DEMO_HOLD_SETTLE_S = 1.0  # 握住后用这段时间的平均力作为“稳态”
DEMO_TUG_ABS = 15  # 握持力偏离稳态超过 max(TUG_ABS, TUG_REL×稳态) 视为被拽
DEMO_TUG_REL = 0.5
DEMO_TUG_FRAMES = 3  # 连续多少帧偏离才松手
DEMO_REARM_S = 1.5  # 松手后至少过多久、且指尖力回落后，才允许再次自动抓取

DEMO_DISPLAY_MAX = 100  # 力条、曲线和热力图的满刻度
DEMO_HISTORY_S = 10  # 曲线显示最近多少秒
DEMO_TARE_SAMPLES = 20  # 去皮时取多少次读数求平均

# ---------------------------------------------------------------- 摄像头镜像（07_camera_mirror.py）
# 普通 USB 摄像头 + MediaPipe 手部关键点模型。模型第一次用前下载一次：
#   python 07_camera_mirror.py --download-model
MIRROR_CAMERA_INDEX = 0  # 摄像头编号；笔记本自带摄像头一般是 0，外接 USB 摄像头常是 1
MIRROR_CAMERA_SIZE = (640, 480)
MIRROR_MODEL_PATH = "models/hand_landmarker.task"  # 相对项目目录
MIRROR_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)
MIRROR_DETECT_CONFIDENCE = 0.6  # 识别到手的最低置信度
MIRROR_TRACK_CONFIDENCE = 0.5

MIRROR_SPEED = (150,) * 10  # 跟随时的关节速度；觉得跟得慢可以加到 200，第一次建议保持
MIRROR_LOOP_DT_S = 0.04  # 控制循环周期（25 Hz）：发角度、读角度
MIRROR_FORCE_EVERY = 3  # 每几个周期读一次触觉（点阵 60 帧，读得太勤会拖慢循环）
MIRROR_SEND_MIN = 2  # 目标变化小于该值不重发，减少总线负担

# 抖动滤波（One Euro 滤波器）：MIN_CUTOFF 越小越稳但越拖；BETA 越大，快速动作时跟得越紧
MIRROR_FILTER_MIN_CUTOFF = 1.0
MIRROR_FILTER_BETA = 0.01
# 防抖回差：关节值在当前指令 ±MIRROR_DEADBAND 以内的变化不发给电机，手静止时就不会跟着识别噪声抖；
# 手一动，指令被“拖着”走，只多这么一点滞后、不会跳变。越大越稳，但细小动作跟得越少。
# 网页“镜像跟随”页的“防抖”滑块可以实时调（0~MIRROR_DEADBAND_MAX），调好后把数值写回这里才会保存。
MIRROR_DEADBAND = 6
MIRROR_DEADBAND_MAX = 20
MIRROR_LOST_S = 0.5  # 超过这么久没看到手，界面提示“保持当前姿态”（手不会乱动）

# 人手特征 → L10 关节。每个关节用两个点做线性映射：
#   特征值 A → 原始值 A，特征值 B → 原始值 B，中间线性插值，超出范围取端点。
# 特征（都与左右手无关，左右手都能用）：
#   *_curl        四指弯曲角：掌指关节 + 近端指间关节的夹角之和（度）
#   thumb_bend    拇指弯曲角：拇指掌指关节 + 指间关节（度）
#   thumb_front   拇指掌骨绕手掌长轴的转角（度）：0 指向侧方，越大越转到掌心前方
#   thumb_across  拇指尖相对食指根、沿“食指根→小指根”反方向的位置（掌宽倍数）：越小越横到小指那边
#   *_spread      侧摆：近节手指在掌面内和相邻手指的夹角（度），向外张为正
# 关节方向来自官方模型（linker-sim-assets l20lite）：原始值 255 = 关节零位，0 = 行程另一端。
#   拇指翻滚(侧摆) 0 = 拇指转到掌心前方；拇指旋转 0 = 拇指在掌面内摆向小指；侧摆 0 = 张得最开
MIRROR_JOINT_MAP = {
    # 关节: (特征, 原始值 A, 原始值 B)
    "thumb_flex": ("thumb_bend", 255, 40),
    "thumb_roll": ("thumb_front", 255, 0),
    "index_flex": ("index_curl", 255, 0),
    "middle_flex": ("middle_curl", 255, 0),
    "ring_flex": ("ring_curl", 255, 0),
    "pinky_flex": ("pinky_curl", 255, 0),
    "index_abd": ("index_spread", 255, 0),
    "ring_abd": ("ring_spread", 255, 0),
    "pinky_abd": ("pinky_spread", 255, 0),
    "thumb_rotation": ("thumb_across", 255, 30),
}
# 各特征的默认 (A, B) 值；窗口里按 O / F / T 标定后会改成你的手的实测值，
# 保存在 MIRROR_CALIBRATION_FILE（删掉该文件或按 X 恢复默认）。
#   O 张开五指（拇指伸向外侧）→ 记录各特征的 A 端，以及侧摆的 B 端（张开时的夹角）
#   F 握拳（拇指压在食指、中指外面）→ 记录弯曲、拇指弯曲、拇指前伸的 B 端
#   T 拇指尖碰小指根 → 记录拇指横移的 B 端
MIRROR_FEATURE_DEFAULTS = {
    "thumb_bend": (15.0, 85.0),
    "thumb_front": (20.0, 75.0),
    "thumb_across": (0.7, -0.9),
    "index_curl": (15.0, 160.0),
    "middle_curl": (15.0, 160.0),
    "ring_curl": (15.0, 160.0),
    "pinky_curl": (15.0, 160.0),
    "index_spread": (0.0, 20.0),
    "ring_spread": (0.0, 15.0),
    "pinky_spread": (0.0, 20.0),
}
MIRROR_CALIBRATION_FILE = "mirror_calibration.json"  # 相对项目目录
MIRROR_CALIBRATION_S = 1.0  # 标定时取多长时间的平均
MIRROR_ABDUCTION = True  # False：三个侧摆固定在 POSES["open"] 的中位，不跟随
# 手指越弯，侧摆越回到中位（弯着的手指左右摆容易互相蹭，人手识别也不准）
MIRROR_SPREAD_WITH_CURL = True

# 握拳辅助：握拳时人的拇指横压在手指外面，按位置换算出的拇指旋转偏小（约 120），
# L10 拇指会横着插进已经弯下的手指里卡住。开启后，四指越接近握拳、同时你的拇指也收在掌前，
# L10 拇指（弯曲、翻滚、旋转）就越接近 POSES["fist"] 里的拇指位置（真机验证过能包住手指）。
# 竖大拇指（四指弯、拇指伸直在侧面）不触发，拇指照常跟随。
MIRROR_FIST_ASSIST = True
MIRROR_FIST_THUMB_JOINTS = (0, 1, 9)  # 拇指弯曲、拇指翻滚、拇指旋转
# 四指平均弯曲程度（0 张开 ~ 1 握拳）从第一个值到第二个值，辅助从 0 渐变到满
MIRROR_FIST_CURL = (0.6, 0.85)
# 你的拇指“收在掌前”的程度（拇指前伸特征，0 侧方 ~ 1 掌前）从第一个值到第二个值，辅助从 0 渐变到满
MIRROR_FIST_THUMB_FRONT = (0.3, 0.6)

# 捏取辅助：逐个关节换算时，人手和 L10 结构不同，拇指尖和其他指尖不一定能对上。
# 开启后，你的拇指尖越靠近哪根手指的指尖，L10 的拇指 + 那根手指就越接近下面对应的捏取姿态，
# 保证指尖对上；只改“用到的关节”，其余手指照常跟随。拇指同时靠近两根手指（三指捏）时两组姿态平滑混合。
# 要加无名指 / 小指：在网页控制台调好姿态，照下面的格式加一行（"ring" / "pinky"）。
MIRROR_PINCH_ASSIST = True
MIRROR_PINCH_TARGETS = {
    # 手指: (网页控制台调好的完整 10 关节姿态, 用到的关节)
    # 关节号：0 拇指弯曲 1 拇指翻滚 2 食指 3 中指 4 无名指 5 小指 6 食指侧摆 7 无名指侧摆 8 小指侧摆 9 拇指旋转
    "index": ((172, 175, 125, 192, 192, 192, 128, 128, 128, 128), (0, 1, 2, 6, 9)),  # 2026-11-26 真机调好
    "middle": ((129, 112, 192, 125, 192, 192, 128, 128, 128, 128), (0, 1, 3, 9)),  # 2026-11-26 真机调好
}
# 拇指尖到指尖的距离（掌宽的倍数）：≤ 第一个值算捏上（完全用捏取姿态），≥ 第二个值不辅助
MIRROR_PINCH_DIST = (0.35, 0.8)
# 被捏的手指弯得很深时（握拳）不算捏取：它的弯曲程度（0 张开 ~ 1 握拳）从第一个值到第二个值，辅助逐渐关掉
MIRROR_PINCH_CURL_GATE = (0.65, 0.85)
# 捏取时放宽防撞：拇指和被捏的手指可以同时动（像人捏东西一样），只看其余手指是否在大幅运动。
# 捏取程度 ≥ 第一个值进入、≤ 第二个值退出（两个值不同，避免来回切换）
MIRROR_PINCH_RELAX = (0.5, 0.3)

# 跟随时的防撞（相当于预设动作“分段过渡”的实时版本，按状态判断，慢动作也不会漏掉）
# 拇指折着（实测弯曲 < THUMB_CLEAR_FLEX 或翻滚 < THUMB_CLEAR_ROLL）时，记下四指当时的位置：
#   - 四指只能往合拢方向微调（最多 MIRROR_FOLDED_HOLD），伸直方向一点也不走（不会一点点伸到拇指底下）；
#   - 超过 MIRROR_FOLDED_MOVE：拇指先往外退开（只退不进），四指原地等，拇指不折了再放开四指。
MIRROR_FOLDED_HOLD = 8
MIRROR_FOLDED_MOVE = 20
# 拇指要往里折时，四指必须已经停稳：实测 MIRROR_SETTLE_S 内变化不超过 MIRROR_SETTLE_MOVE，
# 且离目标不超过 MIRROR_SETTLE_TOL。慢慢握拳时拇指会一直在外面等，最后才折进来。
MIRROR_SETTLE_TOL = 25
MIRROR_SETTLE_MOVE = 4
MIRROR_SETTLE_S = 0.2
# 等拇指退开时，拇指（弯曲, 翻滚, 旋转）至少退到这里；前两个要比 THUMB_CLEAR_FLEX / THUMB_CLEAR_ROLL 大一些
MIRROR_THUMB_CLEAR = (225, 225, 160)
# 拇指退不开（被压住）：拇指 MIRROR_CLEAR_STALL_S 内不再动、或总共等了 MIRROR_CLEAR_TIMEOUT_S，就不再等，四指直接动
MIRROR_CLEAR_STALL_S = 0.4
MIRROR_CLEAR_TIMEOUT_S = 3.0
# 四指弯着时，拇指不折得比握拳预设更深（尤其旋转不低于 197，不横扫进弯着的手指）：
# 四指平均弯曲程度从第一个值到第二个值，这个限制从 0 渐变到满
MIRROR_THUMB_GUARD_CURL = (0.3, 0.6)
# 堵转卸力：关节往“合拢”方向还差 MIRROR_STALL_ERR 以上、却 MIRROR_STALL_S 秒不动，
# 就不再往里顶（停在实测位置再多 RELIEF_MARGIN）；人手往回张开后自动解除
MIRROR_STALL_ERR = 20
MIRROR_STALL_S = 0.4
# 按力卸载：指尖力（去皮后）超过该值，那根手指的弯曲关节每次往伸直方向退 STEP，最多退 MAX
MIRROR_FORCE_LIMIT = 50
MIRROR_FORCE_STEP = 5
MIRROR_FORCE_MAX = 60

# ---------------------------------------------------------------- 石头剪刀布（07_camera_mirror.py 按 G）
# 四指弯曲程度（0 = 标定的张开，1 = 标定的握拳）低于 EXTENDED 算伸直，高于 BENT 算弯曲
GESTURE_EXTENDED = 0.4
GESTURE_BENT = 0.6
GAME_POSES = {
    "rock": POSES["fist"],
    "scissors": (0, 180, 255, 255, 0, 0, 255, 109, 255, 118),  # 与网页控制台“数字 2”相同
    "paper": POSES["open"],
}
GAME_READY = (255, 255, 128, 128, 128, 128, 128, 67, 89, 255)  # 准备：拇指抬开、四指半握
GAME_PUMP = (255, 255, 60, 60, 60, 60, 128, 67, 89, 255)  # 喊口令时四指一握一松
GAME_BEAT_S = 0.7  # “石头”“剪刀”“布”每拍间隔
GAME_CAPTURE_DELAY_S = 0.15  # 喊“布”后多久开始认你的手势
GAME_CAPTURE_S = 0.7  # 认手势的时间窗口（取这段时间里出现最多的手势）
GAME_RESULT_S = 2.5  # 结果显示多久后回到准备姿态

# ---------------------------------------------------------------- 握手（v2，网页“握手”页）
# 握手姿态：2026-11-26 在真机网页控制台上调好（拇指在上、四指自然弯成握手的弧度）
HANDSHAKE_POSE = (192, 77, 165, 132, 114, 84, 128, 128, 128, 128)
# 准备姿态 = 握手姿态 + READY_OFFSET（更张开，方便把手伸进来）；
# 最紧姿态 = 握手姿态 − TIGHT_OFFSET（手很小时最多弯到这里）。值为 0 的关节握手时不动。
# 顺序：拇指弯曲、拇指翻滚、食指、中指、无名指、小指、食指侧摆、无名指侧摆、小指侧摆、拇指旋转
HANDSHAKE_READY_OFFSET = (40, 30, 70, 70, 70, 70, 0, 0, 0, 0)  # 拇指翻滚只多张开 30：握手时拇指主要靠弯曲
HANDSHAKE_TIGHT_OFFSET = (100, 0, 70, 70, 70, 70, 0, 0, 0, 0)  # 拇指多弯一些；翻滚最多转到握手姿态的值，不往里横扫
HANDSHAKE_SPEED = (120,) * 10  # 握手时的关节速度
# 合拢：每根手指从准备姿态往最紧姿态走（合拢程度 0→1），每秒走 CLOSE_RATE；
# 指尖力（相对准备时记的零点）到 GRIP_FORCE 就停在那里。网页上的“握力”滑块可以实时调。
HANDSHAKE_CLOSE_RATE = 0.6
# 碰到了就放慢：指尖力到握力的 SLOW_RATIO 后，这根手指改用 SLOW_FACTOR 倍的速度合拢，停下时更接近握力
HANDSHAKE_SLOW_RATIO = 0.4
HANDSHAKE_SLOW_FACTOR = 0.3
HANDSHAKE_GRIP_FORCE = 25
# 你用力捏时：某根手指的指尖力超过 FORCE_LIMIT，它每次往回让 RELAX_RAW，最多让 RELAX_MAX；
# 力降到上限的 60% 以下后，每次回来 RELAX_BACK，回到原来握住的位置（不会越让越松、被当成抽手）
HANDSHAKE_FORCE_LIMIT = 60
HANDSHAKE_RELAX_RAW = 6
HANDSHAKE_RELAX_MAX = 24
HANDSHAKE_RELAX_BACK = 3
# 不跑在实际位置前面：合拢时指令最多比实测领先 LEAD（原始值），被挡住时不会越压越深
# 挡住：指令领先到了上限、实际位置 BLOCK_S 秒内累计变化不超过 BLOCK_MOVE（手指碰到你手的侧面、
# 指尖读不到力），判断为被挡住，停在实测位置再往里 BLOCK_MARGIN（轻轻贴着）。
# 用一段时间的累计变化判断：真机上手指走得慢、角度刷新也不快，不会把正在走的手指误判为挡住。
HANDSHAKE_LEAD = 20
HANDSHAKE_BLOCK_S = 0.5
HANDSHAKE_BLOCK_MOVE = 3
HANDSHAKE_BLOCK_MARGIN = 10
HANDSHAKE_TARE_S = 0.6  # 准备姿态到位后用多长时间记录指尖零点（这时别碰指尖）
HANDSHAKE_TRIGGER_FORCE = 8  # 等待时任一指尖力超过它（你的手碰到了）就开始握
HANDSHAKE_TRIGGER_FRAMES = 2  # 连续这么多次读数才算（防误触）
HANDSHAKE_TRIGGER_DELAY_S = 0.4  # 碰到后等这么久再握，留时间把手放好
# 握稳：握住后 SETTLE_S 内每根碰到的手指的平均指尖力，作为判断“力掉下去了”的基准
HANDSHAKE_SETTLE_S = 0.5
# 自动松手（每根手指单独判断“失去接触”）：
#   - 碰到指尖的手指：指尖力 < max(RELEASE_ABS, RELEASE_RATIO × 它握稳时的力)（握稳前用握力当基准）；
#   - 被挡住的手指（轻轻贴着）：往里走了 RELEASE_ADVANCE 以上（拇指 THUMB_RELEASE_ADVANCE；要小于各自的 BLOCK_MARGIN）。
# 一半以上的接触手指失去接触、连续 RELEASE_FRAMES 次读数，就松开。只有一根手指离开（手动了一下）不会松。
HANDSHAKE_RELEASE_RATIO = 0.4
HANDSHAKE_RELEASE_ABS = 6
HANDSHAKE_RELEASE_FRAMES = 2
HANDSHAKE_RELEASE_ADVANCE = 6
HANDSHAKE_MAX_HOLD_S = 15  # 最长握这么久就自动松开
HANDSHAKE_REARM_S = 1.5  # 松开后至少过这么久、并且指尖力回落，才回到“等待”
# 拇指（力气大、侧面没有触觉，最容易按疼人）：
#   - 主要靠弯曲：翻滚只动一点（READY_OFFSET 的 30），合拢程度 < THUMB_ROLL_FIRST 时先转完这一点，之后只弯曲；
#   - 握力和“捏太紧”上限都按四指的 THUMB_FORCE_RATIO 倍算，合拢速度按 THUMB_RATE_RATIO 倍；
#   - 指令最多领先实测 THUMB_LEAD（比四指小），挡住后只往里留 THUMB_BLOCK_MARGIN。
# 网页上的“拇指一起握”可以临时关掉拇指（停在准备姿态）。
HANDSHAKE_USE_THUMB = True
HANDSHAKE_THUMB_ROLL_FIRST = 0.3
HANDSHAKE_THUMB_FORCE_RATIO = 0.6
HANDSHAKE_THUMB_RATE_RATIO = 0.6
HANDSHAKE_THUMB_LEAD = 12
HANDSHAKE_THUMB_BLOCK_MARGIN = 6
HANDSHAKE_THUMB_RELEASE_ADVANCE = 4
