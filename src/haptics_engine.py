# -*- coding: utf-8 -*-
"""判断与混音层：把遥测快照变成四个马达的强度。

输入：acc_telemetry.Snapshot
输出：Motors(left_grip, right_grip, left_trigger, right_trigger)，0.0~1.0
      顺序和 PadDriver.set_vibration 完全一致，可以直接：

          driver.set_vibration(*engine.update(snapshot))

分工（设计说明.md D4）：
    扳机 ← 短促、突然、要立刻反应的事（锁死、打滑、ABS 介入、推头）
    握把 ← 持续、厚重、提供方位感的事（路面纹理、失控）

失控（2026-11-25，设计说明.md D13）：
    握把新增"后轮横滑 / 甩尾 / 打转"。判据思路来自 MIT 项目
    ACC-DualSense-Feedback 的 rear drag，但只依赖已经实测过的 slipAngle
    和车速：后轮平均侧滑角超过随车速变化的门槛（高速 0.18 / 低速 0.30），
    且两个后轮都滑，才报。速度门限 14→35 km/h 渐入，所以低速打转也报。
    方向盘 / 车身速度 / 横摆不参与，旧采集就能验证。
    两个握把同强度。四轮抱死继续只走左扳机；冲出赛道但没打滑，不报。

推头（2026-11-25 实测标定，v0.2.2 参考项目加固）：
    主判据仍是"前轮侧滑角比后轮大多少"——前轮被方向盘拧着走，天生就比后轮大；
    真正推头时才会明显超过后轮。
    110 秒实测：正常过弯差值 0.01~0.05 弧度，推头时 0.07~0.13，所以 0.05 起、
    0.13 满。参考项目的数据里正常前轮绝对侧滑最高 0.15、推头中位数 0.19，
    正常 `slipVibrations` 中位数 0.03、推头 0.64，所以再加两层：
    前轮绝对侧滑 0.12→0.16 的门槛，以及 `slipVibrations` 0.16→0.85 的置信
    （最低保留 50%，轻推头不会被抹掉）。两个前轮都要滑 ≥0.05；速度 20→40 km/h
    渐入；12ms 起振 / 100ms 收振，比原来 40/200 更跟手。

两层（D5）：
    底层：刹车压力 × 20%（左扳机）、油门压力 × 15%（右扳机）。
          不看屏幕也知道自己踩了几成，上限压得很低，免得盖住上层信息。
          车速低于 5 km/h 时这层淡出到 0（停着踩刹车不该一直震），
          20 km/h 以上恢复全量。
    上层：临界事件，60~80ms，最高 75%（可调，见 手感设置.toml）。

混音（D11）：
    加权求和 + 单次软限幅。同一马达同一时刻最多 1 个主效果 + 1 个辅效果
    （辅效果封顶 25%），其余直接丢掉。不用"平滑或"层层叠加，
    否则几个效果一叠就饱和，八件事糊成"在震"一件事。

一条必须接受的物理限制（阶段 0 实测）：
    前轮抱死不是渐进的，实测 -0.312 → -0.908 不到 20 毫秒，中间没有过渡。
    所以做不出"逼近极限时逐渐增强"，只能报两种状态：
        已经锁死（ABS 没介入时）        → 60~80ms 的急促脉冲
        ABS 正在救你（有介入信号时）    → 同样 70ms 的脉冲，120ms 一次（约 8Hz）

    判 ABS 用 abs / absVibrations，但**节奏由我们自己出**：游戏的方波频率没保证，
    太快时偏心马达只会把它抹成一条连续震动，听不出"嗒嗒"。

ABS 开 / 关（D10）：
    遥测里没有"ABS 是否启用"这个字段，所以这里不猜设置，直接看信号：
    只要 abs / absVibrations 说明 ABS 正在动作，就打 ABS 的嗒嗒，
    并且压掉"锁死"警报（这时候轮子没真锁死，是 ABS 在救）。
    没有介入信号、但前轮滑移已经越过阈值，才打"锁死"警报。
    两套逻辑自动切换，不需要用户配。

为什么打滑不用 4 次方（D8 的边角）：
    D8 的 4 次方是给"没有硬阈值的连续抓地力使用率"用的——平时安静，
    用到八成才有感觉。打滑这边已经有 0.15 这个硬阈值（实测后轮 p90 才 0.14），
    阈值以上已经是真滑了，再压 4 次方会让刚打滑时几乎没有反馈。
    所以这里用较温和的 0.6 次方，阈值负责"平时安静"，曲线负责"滑得越多越强"。

调用方式：引擎自己不起线程，谁需要就在自己的循环里每帧调一次 update()。
60Hz 左右就行，和 pad_driver 的写入频率一致。没有新遥测时不要喂旧快照，
直接让马达归零（或者调 reset()）。

命令行自检（不需要 ACC，也不需要手柄）：
    python src/haptics_engine.py
    python src/haptics_engine.py --replay docs/captures/某份采集.csv

手感设置：
    仓库根目录的 `手感设置.toml`（双击 `调手感.bat` 编辑）。
    改完保存，正在跑的程序 1 秒内自动生效。没有这个文件就用内置默认值。
"""

import math
import sys
import time
import tomllib
from pathlib import Path
from typing import NamedTuple

from acc_telemetry import Snapshot
from i18n import t

# ── 参数初值（设计说明.md 第六节）────────────────────────────────
# 这些是"从数据分布推出来的候选值"，不是手感验证过的值。
# 阶段 3 的闭眼测试要重新定，先集中放在这里，方便一处改。
#
# 2026-11-26 起，路面 / 刹车 / 油门三个强度改成"倍率"：设置文件里写 1.00，
# 就乘以下面对应的基准振幅。数值语义统一了，手感一点没变。
PRESSURE_FLOOR_REFERENCE = 0.28  # 刹车底层基准振幅（1.00 对应它）
THROTTLE_FLOOR_REFERENCE = 0.15  # 油门底层基准振幅；比刹车低，避免长时间给油一直嗡嗡

# 压力底层的车速门限（2026-11-25 用户要求）：
# 停着踩满刹车也一直震，像故障；车速低于 OFF 时这一层归零，到 FULL 恢复全量。
# 只影响"压力底层"，锁死 / ABS / 打滑 / 推头不受影响。
FLOOR_SPEED_OFF = 5.0            # km/h，低于它压力层完全关掉
FLOOR_SPEED_FULL = 20.0          # km/h，到这里恢复全量
EVENT_AMPLITUDE = 0.75           # 临界 / ABS 事件幅度（第一版手感，从 70% 提上来）
EVENT_DURATION = 0.070           # 事件持续 70ms（设计区间 60~80ms）
EVENT_RELEASE = 0.015            # 最后 15ms 渐落，避免硬切
EVENT_REFRACTORY = 0.120         # 不应期 120ms，防止连成持续震动

FRONT_LOCK_THRESHOLD = -0.10     # 前轮滑移比阈值（实测候选）
REAR_SLIP_THRESHOLD = 0.15       # 后轮打滑阈值（实测候选）
REAR_SLIP_FULL = 0.80            # 到这个滑移比就满幅（实测后轮能到 11，很早就是真滑了）
REAR_CURVE = 0.6                 # 阈值以上用温和曲线，见文件头说明
REAR_ATTACK = 0.015              # 打滑起振时间 15ms
REAR_RELEASE = 0.100             # 收振时间 100ms

ABS_GATE = 0.05                  # abs / absVibrations 超过它才算"ABS 在动作"

# 推头：前轮比后轮多滑多少（弧度）。阈值来自 2026-11-25 的 110 秒实测：
# 正常过弯差值 0.01~0.05，推头时 0.07~0.13。
UNDERSTEER_START = 0.05          # 前轮−后轮，约 2.9°，开始有感觉
UNDERSTEER_FULL = 0.13           # 约 7.4°，到满幅
UNDERSTEER_CURVE = 0.7           # 温和抬升，别让刚推头就满
UNDERSTEER_ATTACK = 0.012        # 12ms 起振：比原来跟手
UNDERSTEER_RELEASE = 0.100       # 100ms 收振
UNDERSTEER_SPEED_START = 20.0    # km/h，低于它淡出
UNDERSTEER_SPEED_FULL = 40.0     # km/h，到这里全量
UNDERSTEER_SLIP_MAX = 0.5        # 弧度，超过这个算打转/出界，不判推头
UNDERSTEER_MIN_REAR = 0.003      # 后轮几乎为 0 时没有参考，跳过
UNDERSTEER_SUPPORT_MIN = 0.05    # 两个前轮都要滑到这个程度；单轮尖峰不算
UNDERSTEER_FRONT_START = 0.12    # 前轮绝对侧滑角，开始算"真的在推"
UNDERSTEER_FRONT_FULL = 0.16     # 到这里绝对门槛拉满
UNDERSTEER_CONF_START = 0.16     # |slipVibrations| 置信门限
UNDERSTEER_CONF_FULL = 0.85
UNDERSTEER_CONF_FLOOR = 0.50     # 置信最低保留一半，别把轻推头全抹掉
UNDERSTEER_GAIN = 1.00           # 推头强度（独立于事件强度，见 手感设置.toml）

# ── 失控：后轮横滑 / 甩尾 / 打转（2026-11-25）──────────────────
# 判据思路来自 MIT 项目 ACC-DualSense-Feedback 的 rear drag，但**只依赖
# 已经实测过的 slipAngle 和车速**。方向盘 / 车身速度 / 横摆不参与，
# 所以 2026-11-25 那份 110 秒采集就能完整标定，不用再跑新数据。
#
# 后轮平均侧滑角的门槛随车速变：
#   50 km/h 以上 0.18（正常 120 km/h 高速弯实测最高 0.16）
#   20 km/h 以下 0.30（低速正常大角度弯容易到 0.2，抬高门槛防误报）
#   中间平滑过渡；28 km/h 的真打转后轮还有 0.40，照样能报。
LOSS_SPEED_START = 14.0          # km/h，低于它完全淡出
LOSS_SPEED_FULL = 35.0           # km/h，到这里恢复全量

LOSS_REAR_START = 0.18           # 高速段门槛
LOSS_REAR_START_LOW = 0.30       # 低速段门槛
LOSS_REAR_SPEED_LOW = 20.0       # 低于这个车速用低速门槛
LOSS_REAR_SPEED_HIGH = 50.0      # 高于这个车速用高速门槛
LOSS_REAR_SPAN = 0.16            # 资格满幅 = 门槛 + 0.16

LOSS_DIFF_START = 0.025          # 后轮比前轮多滑多少，开始有资格
LOSS_DIFF_FULL = 0.075
LOSS_QUALIFY_MIN = 0.055         # 资格超过它才算"确认"
LOSS_EVIDENCE = 0.012            # 连续确认 12ms 才开，防单帧毛刺
LOSS_SUPPORT_PAIR = 0.075        # 两个后轮都滑到这个程度才认
LOSS_STRENGTH_SPAN = 0.14        # 强度满幅 = 门槛 + 0.14
LOSS_STRENGTH_DIFF_START = 0.018
LOSS_STRENGTH_DIFF_FULL = 0.065
LOSS_ATTACK = 0.012              # 12ms 起振
LOSS_RELEASE = 0.150             # 150ms 收振：滑掉了就持续，救回来就落
LOSS_GAIN = 1.00                 # 失控强度（两个握把同强度）

GRIP_ROAD_REFERENCE = 0.05       # 路面纹理基准振幅（1.00 对应它）
GRIP_ROAD_CURVE = 0.50           # 轻纹理抬升（越小越明显）
G_VIBRATION_DEADZONE = 0.05      # gVibrations 不设死区就是永远在震（实测 91.5% 的帧 >0.02）
G_VIBRATION_FULL = 7.5           # 实测上限

SECONDARY_CAP = 0.25             # 辅效果封顶 25%（D11）
SOFT_KNEE = 0.70                 # 软限幅的拐点，拐点以下不动
MIX_FLOOR = 0.002                # 小于这个的贡献当作没有（报文本来就是 1% 一档）


# ══════════════════════════════════════════════════════════════════
# 手感设置（外部文件）
# ══════════════════════════════════════════════════════════════════
# 只把最常调的几项放出去。其余（阈值、曲线、混音预算）等闭眼测试
# 有结论了再往外放，免得一次给太多旋钮反而不知道动哪个。
DEFAULT_TUNING = {
    "grip_road": 1.00,
    "trigger_floor": 1.00,
    "throttle_floor": 1.00,
    "trigger_event": EVENT_AMPLITUDE,
    "understeer_gain": UNDERSTEER_GAIN,
    "loss_gain": LOSS_GAIN,
}
TUNING_RANGES = {
    "grip_road": (0.0, 4.0),
    "trigger_floor": (0.0, 1.8),
    "throttle_floor": (0.0, 2.3),
    "trigger_event": (0.0, 1.0),
    "understeer_gain": (0.0, 1.50),
    "loss_gain": (0.0, 1.50),
}

CONFIG_NAME = "手感设置.toml"
CONFIG_TEMPLATE = """# ACC Trigger Haptics · 手感设置
#
# 只改等号 = 右边的数字。# 后面是说明，改了不会生效。
# 改完保存：程序每秒检查一次，1 秒内生效，不用重启。
# 强度类：数字越大越强。路面 / 刹车 / 油门 / 推头 / 失控是倍率，1.00 = 标准手感。
# 事件强度是 0~1 的振幅，1.00 = 满幅。

grip_road = %(grip_road).2f      # 握把·路面纹理（倍率，1.00 = 标准）

# 左扳机：刹车压力底层（0 ~ 1.8，1.00 = 标准）。
# 调高更有实感，但超过 1.25 左右，锁死 / ABS 的对比会变弱。
trigger_floor = %(trigger_floor).2f

# 右扳机：油门压力底层（0 ~ 2.3，1.00 = 标准）。踩多少震多少，
# 调高更有实感，但后轮打滑 / 推头的对比会变弱。
throttle_floor = %(throttle_floor).2f

trigger_event = %(trigger_event).2f   # 扳机事件：锁死 / ABS / 后轮打滑（0 ~ 1.0）

# 右扳机：推头强度（前轮比后轮多滑时震）。1.0 是标准强度，0 = 关掉推头。
understeer_gain = %(understeer_gain).2f

# 两个握把：失控强度（车尾横滑 / 甩尾 / 打转时震，两个握把同强度）。
# 1.0 是标准强度，0 = 关掉失控通道。
loss_gain = %(loss_gain).2f
""" % DEFAULT_TUNING


def application_dir():
    """程序"自己的"目录：源码运行时是仓库根目录；打包成 exe 后是 exe 所在目录。

    打包后 __file__ 指向 PyInstaller 的临时解压目录，不能再用它找设置文件，
    否则 exe 会去临时目录读一个不存在的配置。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_path():
    """手感设置放在程序目录，和 bat / exe 同级，方便找。"""
    return application_dir() / CONFIG_NAME


def load_tuning_result(path=None, fallback=None):
    """读手感设置，并告诉调用方这次到底读成功了没有。

    返回 (ok, tuning)：
        ok=True   文件读到了、TOML 也合法（缺文件且没有 fallback 也算首次启动的正常情况）
        ok=False  读取/解析失败。给了 fallback 就原样返回它，让热更新继续用上次成功的值。

    首次启动没有配置文件时 fallback 是 None，仍然返回内置默认值，行为和以前一致。
    """
    tuning = dict(DEFAULT_TUNING)
    path = Path(path) if path is not None else config_path()
    fallback = None if fallback is None else dict(fallback)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        if fallback is not None:
            print(t("cfg_reload_missing", path.name))
            return False, fallback
        return True, tuning
    except Exception as exc:
        if fallback is not None:
            print(t("cfg_reload_fail", path.name, exc))
            return False, fallback
        print(t("cfg_read_fail", path.name, exc))
        return False, tuning

    for key in data:
        if key not in TUNING_RANGES:
            print(t("cfg_unknown_key", key))

    for key, (low, high) in TUNING_RANGES.items():
        if key not in data:
            continue
        try:
            value = float(data[key])
        except (TypeError, ValueError):
            print(t("cfg_not_number", key))
            continue
        if not math.isfinite(value):
            print(t("cfg_not_finite", key))
            continue
        clamped = max(low, min(high, value))
        if clamped != value:
            print(t("cfg_out_of_range", key, value, low, high, clamped))
        tuning[key] = clamped
    return True, tuning


def load_tuning(path=None, fallback=None):
    """读手感设置。启动时没有文件、格式坏了、值超范围，都退回默认值。

    fallback 只给热更新用：读取失败时返回上一次成功加载的设置，
    避免文件写到一半或暂时损坏时把档位跳回默认值。
    """
    return load_tuning_result(path, fallback=fallback)[1]


def write_default_config(path=None):
    """生成一份带注释的默认设置。已存在就不覆盖。"""
    path = Path(path) if path is not None else config_path()
    if path.exists():
        return False
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return True


class Motors(NamedTuple):
    """四个马达的最终强度，顺序与 PadDriver.set_vibration 一致。"""
    left_grip: float
    right_grip: float
    left_trigger: float
    right_trigger: float


def _num(value):
    """挡住 NaN / inf。野生值进了马达会变成非法整数，直接把写入线程搞挂。"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return value


def _clamp01(value):
    if not (value > 0.0):
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _scale(value, deadzone, full):
    """把 [deadzone, full] 映射到 0~1，低于死区算 0。"""
    if full <= deadzone:
        return 0.0
    return _clamp01((value - deadzone) / (full - deadzone))


def _smoothstep(low, high, value):
    """平滑过渡：low 以下 0，high 以上 1。用于失控判据，避免硬阈值抖动。"""
    if high <= low:
        return 1.0 if value >= high else 0.0
    x = (value - low) / (high - low)
    x = 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)
    return x * x * (3.0 - 2.0 * x)


def _soft_clip(value):
    """单次软限幅：拐点以下原样通过，以上用 tanh 压回去。

    不用 tanh 直接把整段都压：0.2 的压力反馈会被抬成 0.3，
    底层就没法"压到两成"了。
    """
    if value <= SOFT_KNEE:
        return value
    span = 1.0 - SOFT_KNEE
    return SOFT_KNEE + span * math.tanh((value - SOFT_KNEE) / span)


def _mix(contributions):
    """主辅预算 + 加权求和 + 单次软限幅。

    contributions 是 [(名字, 已经加过权的幅度), ...]。
    返回 (输出值, 实际参与混音的效果列表)，后者给调试和日志看。
    """
    active = [(name, amp) for name, amp in contributions if amp > MIX_FLOOR]
    if not active:
        return 0.0, []

    active.sort(key=lambda item: item[1], reverse=True)
    used = [active[0]]
    total = active[0][1]

    if len(active) > 1:
        secondary = min(active[1][1], SECONDARY_CAP)
        if secondary > MIX_FLOOR:
            used.append((active[1][0], secondary))
            total += secondary

    return _soft_clip(total), used


class HapticsEngine:
    """把一帧遥测变成四个马达值。有状态：事件包络、不应期、打滑收放。"""

    def __init__(self, tuning=None):
        self.tuning = dict(DEFAULT_TUNING)
        if tuning:
            self.set_tuning(tuning)
        self._event_start = -1e9
        self._abs_start = -1e9
        self._rear_level = 0.0
        self._understeer_level = 0.0
        self._loss_level = 0.0
        self._loss_evidence = 0.0
        self._loss_metrics = {}
        self._grip_peak = [0.0, 0.0]
        self._last_update = None
        self.last_trace = {}
        self.last_metrics = {}

    def set_tuning(self, tuning):
        """更新手感参数（配置文件热重载用）。只认已知的键，越界会夹住。"""
        if not tuning:
            return
        for key, (low, high) in TUNING_RANGES.items():
            if key in tuning:
                value = _num(tuning[key])
                self.tuning[key] = max(low, min(high, value))

    def reset(self):
        """清掉事件和包络。遥测中断、回到维修区、换车时调一下。"""
        self._event_start = -1e9
        self._abs_start = -1e9
        self._rear_level = 0.0
        self._understeer_level = 0.0
        self._loss_level = 0.0
        self._loss_evidence = 0.0
        self._loss_metrics = {}
        self._grip_peak = [0.0, 0.0]
        self._last_update = None
        self.last_trace = {}
        self.last_metrics = {}

    def _update_loss(self, snap, dt, first_tick, speed):
        """后轮横滑 / 甩尾 / 打转 → 0~1。两个握把共用这个值。

        只依赖 slipAngle 和车速；方向盘、车身速度、横摆不参与，
        所以 2026-11-25 那份旧采集就能完整验证。判据思路来自参考项目
        ACC-DualSense-Feedback 的 rear drag（MIT），见文件头。
        """
        rear_l = abs(_num(snap.slip_angle[2]))
        rear_r = abs(_num(snap.slip_angle[3]))
        front_l = abs(_num(snap.slip_angle[0]))
        front_r = abs(_num(snap.slip_angle[1]))
        rear = (rear_l + rear_r) * 0.5
        front = (front_l + front_r) * 0.5

        speed_fade = _smoothstep(LOSS_SPEED_START, LOSS_SPEED_FULL, abs(speed))
        # 低速时正常大角度弯也会把后轮侧滑推到 0.2，所以低速门槛抬高；
        # 高速正常弯实测最高 0.16，所以高速门槛 0.18 足够安静。
        low_speed = 1.0 - _smoothstep(
            LOSS_REAR_SPEED_LOW, LOSS_REAR_SPEED_HIGH, abs(speed))
        start = (LOSS_REAR_START
                 + (LOSS_REAR_START_LOW - LOSS_REAR_START) * low_speed)
        full = start + LOSS_REAR_SPAN

        qualification = (
            _smoothstep(start, full, rear)
            * speed_fade
            * max(
                _smoothstep(LOSS_DIFF_START, LOSS_DIFF_FULL, rear - front),
                _smoothstep(start, full, rear),
            )
        )
        # 两个后轮都滑才认：单个轮子压路肩/上草地的尖峰过不了这一关。
        supported = min(rear_l, rear_r) >= LOSS_SUPPORT_PAIR
        confirmed = qualification > LOSS_QUALIFY_MIN and supported

        # 卡帧时别让一次大 dt 把证据/包络一步顶满；参考项目也夹在 50ms。
        step = min(max(dt, 0.0), 0.050)
        if confirmed:
            self._loss_evidence = min(0.100, self._loss_evidence + step)
        else:
            self._loss_evidence = 0.0

        target = 0.0
        if self._loss_evidence >= LOSS_EVIDENCE:
            strength = (
                _smoothstep(start, start + LOSS_STRENGTH_SPAN, rear)
                * max(
                    _smoothstep(LOSS_STRENGTH_DIFF_START,
                                LOSS_STRENGTH_DIFF_FULL, rear - front),
                    _smoothstep(start, start + LOSS_STRENGTH_SPAN, rear),
                )
                * speed_fade
            )
            target = strength * self.tuning["loss_gain"]

        if first_tick or step <= 0.0:
            self._loss_level = target
        elif target >= self._loss_level:
            self._loss_level += ((target - self._loss_level)
                                 * min(1.0, step / LOSS_ATTACK))
        else:
            self._loss_level += ((target - self._loss_level)
                                 * min(1.0, step / LOSS_RELEASE))
        self._loss_level = _clamp01(self._loss_level)

        self._loss_metrics = {
            "loss": self._loss_level,
            "loss_confirmed": confirmed,
            "loss_qualification": qualification,
            "loss_rear": rear,
            "loss_front": front,
            "loss_threshold": start,
            "loss_supported": supported,
        }
        return self._loss_level

    def update(self, snap, now=None):
        """输入一帧 Snapshot，返回四个马达值。

        now 只是为了让自测能喂虚拟时间，正常调用不用传。
        """
        now = time.perf_counter() if now is None else float(now)
        if self._last_update is None:
            dt = 0.0
        else:
            dt = max(0.0, now - self._last_update)
        first_tick = self._last_update is None
        self._last_update = now

        road = _scale(_num(snap.g_vibrations),
                      G_VIBRATION_DEADZONE, G_VIBRATION_FULL) ** GRIP_ROAD_CURVE
        road_amplitude = (road * self.tuning["grip_road"]
                          * GRIP_ROAD_REFERENCE)
        speed = _num(snap.speed_kmh)
        # 停车时把"压力底层"关掉：停着踩满刹车也一直震，像故障
        floor_factor = _scale(speed, FLOOR_SPEED_OFF, FLOOR_SPEED_FULL)

        # 握把：路面纹理 + 失控（2026-11-25 新增，两个握把同强度）。
        # 失控是主效果时，路面纹理按 D11 的预算降为辅助（封顶 25%），
        # 不会完全消失，也不会和失控抢成一片糊。
        loss = self._update_loss(snap, dt, first_tick, speed)
        left_grip, left_grip_trace = _mix([
            ("路面", road_amplitude),
            ("失控", loss),
        ])
        right_grip, right_grip_trace = _mix([
            ("路面", road_amplitude),
            ("失控", loss),
        ])
        # 最近 1 秒的握把峰值，只给 --status 看：1Hz 打一行很容易错过短事件
        self._grip_peak[0] = max(left_grip, self._grip_peak[0] - dt / 1.0)
        self._grip_peak[1] = max(right_grip, self._grip_peak[1] - dt / 1.0)

        # ── 左扳机：刹车压力打底，锁死 / ABS 覆盖上去 ────────────
        brake = _clamp01(_num(snap.brake))
        front_slip = min(_num(snap.slip_ratio[0]), _num(snap.slip_ratio[1]))
        abs_flag = _clamp01(_num(snap.abs_level))
        abs_pulse = _clamp01(abs(_num(snap.abs_vibrations)))
        abs_active = max(abs_flag, abs_pulse) > ABS_GATE

        left_trigger_parts = [
            ("刹车压力", brake * self.tuning["trigger_floor"]
             * PRESSURE_FLOOR_REFERENCE * floor_factor),
        ]

        if abs_active:
            # 自己出节奏：70ms 脉冲 + 120ms 不应期，约 8Hz。
            # 不跟游戏的 absVibrations 方波：它的频率没保证，太快时 ERM
            # 只会把它抹成一条连续的震动，听不出"嗒嗒"。
            if now >= self._abs_start + EVENT_REFRACTORY:
                self._abs_start = now
            age = now - self._abs_start
            if age < EVENT_DURATION:
                amplitude = self.tuning["trigger_event"]
                remaining = EVENT_DURATION - age
                if remaining < EVENT_RELEASE:
                    amplitude *= remaining / EVENT_RELEASE
                left_trigger_parts.append(("ABS 介入", amplitude))
        elif front_slip <= FRONT_LOCK_THRESHOLD:
            if now >= self._event_start + EVENT_REFRACTORY:
                self._event_start = now
            age = now - self._event_start
            if age < EVENT_DURATION:
                amplitude = self.tuning["trigger_event"]
                remaining = EVENT_DURATION - age
                if remaining < EVENT_RELEASE:
                    amplitude *= remaining / EVENT_RELEASE
                left_trigger_parts.append(("前轮锁死", amplitude))

        left_trigger, left_trigger_trace = _mix(left_trigger_parts)

        # ── 右扳机：油门底层打底，后轮打滑覆盖上去 ────────────────
        # 右扳机以前只报"后轮打滑"，正常给油时永远是 0；加一层油门底层，
        # 让它踩多少震多少（2026-11-25）。推头等标定后也接在这里。
        throttle = _clamp01(_num(snap.throttle))
        rear_slip = max(_num(snap.slip_ratio[2]), _num(snap.slip_ratio[3]))
        target = 0.0
        if rear_slip > REAR_SLIP_THRESHOLD:
            severity = _scale(rear_slip, REAR_SLIP_THRESHOLD, REAR_SLIP_FULL)
            target = self.tuning["trigger_event"] * severity ** REAR_CURVE

        if first_tick or dt <= 0.0:
            self._rear_level = target
        elif target >= self._rear_level:
            self._rear_level += (target - self._rear_level) * min(1.0, dt / REAR_ATTACK)
        else:
            self._rear_level += (target - self._rear_level) * min(1.0, dt / REAR_RELEASE)

        # ── 推头：前轮比后轮多滑（2026-11-25 按实测标定）──────────
        # 只用侧滑角：前轮被方向盘拧着走，前轮侧滑角明显大于后轮，说明前轮在推。
        # 打转/出界时四个角一起失真，这种帧跳过，别误报成推头。
        front_l = abs(_num(snap.slip_angle[0]))
        front_r = abs(_num(snap.slip_angle[1]))
        rear_l = abs(_num(snap.slip_angle[2]))
        rear_r = abs(_num(snap.slip_angle[3]))
        front_angle = max(front_l, front_r)
        rear_angle = max(rear_l, rear_r)

        understeer_target = 0.0
        if (front_angle < UNDERSTEER_SLIP_MAX
                and rear_angle < UNDERSTEER_SLIP_MAX
                and rear_angle >= UNDERSTEER_MIN_REAR
                and min(front_l, front_r) >= UNDERSTEER_SUPPORT_MIN):
            # 速度渐入，不再 25 km/h 一刀切；低速起步时淡出。
            speed_gate = _smoothstep(UNDERSTEER_SPEED_START,
                                     UNDERSTEER_SPEED_FULL, speed)
            # 前轮绝对侧滑门槛：防止前轮 0.06、后轮 0.003 这种"差值够了但
            # 根本没在推"的帧也报。
            front_gate = _smoothstep(UNDERSTEER_FRONT_START,
                                     UNDERSTEER_FRONT_FULL, front_angle)
            # 参考项目用 slipVibrations 当置信：正常弯中位数 0.03，推头 0.64。
            # 保留 0.5 下限，轻推头不会被完全抹掉。
            confidence = (UNDERSTEER_CONF_FLOOR
                          + (1.0 - UNDERSTEER_CONF_FLOOR)
                          * _smoothstep(UNDERSTEER_CONF_START,
                                        UNDERSTEER_CONF_FULL,
                                        abs(_num(snap.slip_vibrations))))
            severity = _scale(front_angle - rear_angle,
                              UNDERSTEER_START, UNDERSTEER_FULL)
            understeer_target = (self.tuning["understeer_gain"]
                                 * severity ** UNDERSTEER_CURVE
                                 * speed_gate * front_gate * confidence)

        if first_tick or dt <= 0.0:
            self._understeer_level = understeer_target
        elif understeer_target >= self._understeer_level:
            self._understeer_level += ((understeer_target - self._understeer_level)
                                       * min(1.0, dt / UNDERSTEER_ATTACK))
        else:
            self._understeer_level += ((understeer_target - self._understeer_level)
                                       * min(1.0, dt / UNDERSTEER_RELEASE))

        right_trigger, right_trigger_trace = _mix([
            ("油门压力", throttle * self.tuning["throttle_floor"]
             * THROTTLE_FLOOR_REFERENCE * floor_factor),
            ("后轮打滑", self._rear_level),
            ("推头", self._understeer_level),
        ])

        self.last_trace = {
            "left_grip": left_grip_trace,
            "right_grip": right_grip_trace,
            "left_trigger": left_trigger_trace,
            "right_trigger": right_trigger_trace,
        }
        self.last_metrics = {
            "brake": brake,
            "throttle": throttle,
            "front_slip": front_slip,
            "rear_slip": rear_slip,
            "abs_active": abs_active,
            "abs_flag": abs_flag,
            "abs_pulse": abs_pulse,
            "road": road,
            "speed": speed,
            "front_angle": front_angle,
            "rear_angle": rear_angle,
            "understeer": self._understeer_level,
            "grip_peak_left": self._grip_peak[0],
            "grip_peak_right": self._grip_peak[1],
        }
        self.last_metrics.update(self._loss_metrics)

        return Motors(
            left_grip=_clamp01(left_grip),
            right_grip=_clamp01(right_grip),
            left_trigger=_clamp01(left_trigger),
            right_trigger=_clamp01(right_trigger),
        )


# ══════════════════════════════════════════════════════════════════
# 命令行自检：喂几段假的遥测，看四个马达会输出什么
# ══════════════════════════════════════════════════════════════════
def _snapshot(**changes):
    """造一帧静止的数据，再按需改几个字段。"""
    base = dict(
        packet_id=0, received_at=0.0,
        throttle=0.0, brake=0.0, gear_raw=2, rpm=1200, steer_angle=0.0,
        speed_kmh=100.0,
        tc_level=0.0, heading=0.0, abs_level=0.0,
        kerb_vibration=0.0, slip_vibrations=0.0,
        g_vibrations=0.0, abs_vibrations=0.0,
        wheel_load=(3000.0, 3000.0, 3000.0, 3000.0),
        wheel_angular_speed=(0.0, 0.0, 0.0, 0.0),
        suspension_travel=(0.0, 0.0, 0.0, 0.0),
        slip_ratio=(0.0, 0.0, 0.0, 0.0),
        slip_angle=(0.0, 0.0, 0.0, 0.0),
        brake_pressure=(0.0, 0.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0), acc_g=(0.0, 0.0, 0.0),
        local_velocity=(0.0, 0.0, 0.0),
        local_angular_velocity=(0.0, 0.0, 0.0),
    )
    base.update(changes)
    return Snapshot(**base)


def _describe(trace):
    if not trace:
        return "无"
    return " + ".join("%s %.0f%%" % (name, amp * 100.0) for name, amp in trace)


def _run(engine, label, snap, clock, seconds=0.05):
    """按 60Hz 跑一小段，返回这一段结束时的输出。"""
    motors = None
    steps = max(1, int(round(seconds * 60)))
    for _ in range(steps):
        clock += 1.0 / 60.0
        motors = engine.update(snap, now=clock)
    print()
    print("%s" % label)
    print("    左握把 %5.2f   右握把 %5.2f   左扳机 %5.2f   右扳机 %5.2f"
          % (motors.left_grip, motors.right_grip,
             motors.left_trigger, motors.right_trigger))
    print("    左扳机来源：%s" % _describe(engine.last_trace["left_trigger"]))
    print("    右扳机来源：%s" % _describe(engine.last_trace["right_trigger"]))
    print("    握把来源：  左 %s ｜ 右 %s"
          % (_describe(engine.last_trace["left_grip"]),
             _describe(engine.last_trace["right_grip"])))
    metrics = engine.last_metrics or {}
    print("    失控 %5.2f  （后轮侧滑 %.2f，本帧门槛 %.2f）"
          % (metrics.get("loss", 0.0), metrics.get("loss_rear", 0.0),
             metrics.get("loss_threshold", 0.0)))
    return clock


def _replay(path):
    """把 check_acc.py 采到的 CSV 按 50Hz 喂进引擎，看失控通道会怎么走。

    失控只用 slipAngle 和车速，所以旧采集（没有方向盘/车身速度列）也能
    完整验证，不需要重新跑采集。
    """
    import csv

    def num(row, name, default=0.0):
        try:
            return float(row.get(name) or default)
        except (TypeError, ValueError):
            return default

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)

        print("=" * 62)
        print("失控回放：%s" % path)
        print("=" * 62)
        print()

        engine = HapticsEngine(load_tuning())
        clock = 0.0
        frames = 0
        hits = 0
        peak = 0.0
        last_print = -1.0

        for row in reader:
            speed = num(row, "speedKmh")
            snap = _snapshot(
                packet_id=int(num(row, "packetId")),
                throttle=num(row, "gas"), brake=num(row, "brake"),
                gear_raw=int(num(row, "gear", 2)), rpm=int(num(row, "rpms")),
                speed_kmh=speed,
                abs_level=num(row, "abs"), heading=num(row, "heading"),
                kerb_vibration=num(row, "kerbVibration"),
                slip_vibrations=num(row, "slipVibrations"),
                g_vibrations=num(row, "gVibrations"),
                abs_vibrations=num(row, "absVibrations"),
                slip_ratio=tuple(num(row, "slipRatio_" + w) for w in
                                 ("LF", "RF", "LR", "RR")),
                slip_angle=tuple(num(row, "slipAngle_" + w) for w in
                                 ("LF", "RF", "LR", "RR")),
            )
            clock += 1.0 / 50.0
            motors = engine.update(snap, now=clock)
            frames += 1
            loss = engine.last_metrics.get("loss", 0.0)
            peak = max(peak, loss)
            if loss > 0.05:
                hits += 1
                if clock - last_print >= 0.10:
                    last_print = clock
                    print("t=%6.2fs  %5.1f km/h  后轮 %5.2f  门槛 %5.2f  "
                          "→  失控 %5.2f  握把 %4.2f/%4.2f"
                          % (clock, speed,
                             engine.last_metrics.get("loss_rear", 0.0),
                             engine.last_metrics.get("loss_threshold", 0.0),
                             loss, motors.left_grip, motors.right_grip))

    print()
    print("回放结束：%d 帧，失控帧 %d，最大失控 %.2f" % (frames, hits, peak))
    print("如果正常弯也大量触发，把 LOSS_* 阈值往上调；")
    print("如果真甩尾不触发，往下调。阈值在 haptics_engine.py 开头。")


def main():
    if "--replay" in sys.argv:
        index = sys.argv.index("--replay")
        if index + 1 >= len(sys.argv):
            print("用法：python src/haptics_engine.py --replay 某份采集.csv")
            return
        _replay(sys.argv[index + 1])
        return

    if "--write-config" in sys.argv:
        path = config_path()
        created = write_default_config(path)
        print(("已生成" if created else "已存在") + "：" + str(path))
        return

    print("=" * 62)
    print("判断与混音层自检（src/haptics_engine.py）")
    print("=" * 62)
    print("喂几段写死的遥测，看四个马达的输出。不需要 ACC，也不需要手柄。")

    engine = HapticsEngine(load_tuning())
    clock = 0.0

    cases = [
        ("滑行：什么都没踩", {}),
        ("轻刹 30%", dict(brake=0.30)),
        ("重刹到底，但没锁死", dict(brake=1.0, slip_ratio=(-0.06, -0.05, 0.02, 0.02))),
        ("前轮锁死（ABS 没介入）", dict(brake=1.0, slip_ratio=(-0.90, -0.88, 0.02, 0.02))),
        ("ABS 正在动作", dict(brake=1.0, abs_level=1.0, abs_vibrations=1.0,
                             slip_ratio=(-0.05, -0.05, 0.02, 0.02))),
        ("后轮打滑 0.50", dict(throttle=1.0, slip_ratio=(-0.02, -0.02, 0.50, 0.45))),
        ("停着踩满刹车（应该不震）", dict(brake=1.0, speed_kmh=0.0)),
        ("停着踩满油门（应该不震）", dict(throttle=1.0, speed_kmh=0.0)),
        ("全油门直道，不打滑", dict(throttle=1.0, slip_ratio=(0.01, 0.01, 0.02, 0.02))),
        ("轻推头（前 0.15 / 后 0.06）", dict(
            speed_kmh=130.0, throttle=1.0, slip_vibrations=0.7,
            slip_angle=(0.15, 0.15, 0.06, 0.06))),
        ("推头（前 0.21 / 后 0.06）", dict(
            speed_kmh=120.0, throttle=1.0, slip_vibrations=0.7,
            slip_angle=(0.21, 0.21, 0.06, 0.06))),
        ("单前轮压路肩（前 0.55 / 0.04，不该报推头）", dict(
            speed_kmh=100.0, slip_vibrations=0.8,
            slip_angle=(0.55, 0.04, 0.06, 0.06))),
        ("打转（四轮都大，不算推头）", dict(speed_kmh=80.0,
                                     slip_angle=(0.9, 0.9, 0.85, 0.85))),
        ("正常高速弯（后轮 0.09，不该报失控）", dict(
            speed_kmh=120.0, slip_angle=(0.05, 0.05, 0.09, 0.09))),
        ("边界高速弯（后轮 0.16，不该报失控）", dict(
            speed_kmh=120.0, slip_angle=(0.07, 0.07, 0.16, 0.16))),
        ("高速甩尾（后轮 0.55，报失控）", dict(
            speed_kmh=110.0, slip_angle=(0.20, 0.20, 0.55, 0.55))),
        ("四轮漂移（前后都 0.35，报失控）", dict(
            speed_kmh=90.0, slip_angle=(0.35, 0.35, 0.35, 0.35))),
        ("低速打转 25 km/h（后轮 0.45，报失控）", dict(
            speed_kmh=25.0, slip_angle=(0.15, 0.15, 0.45, 0.45))),
        ("低速正常大角度弯（25 km/h，后轮 0.22，不该报）", dict(
            speed_kmh=25.0, slip_angle=(0.05, 0.05, 0.22, 0.22))),
        ("单轮压路肩（后 0.55 / 0.04，不该报失控）", dict(
            speed_kmh=100.0, slip_angle=(0.04, 0.04, 0.55, 0.04))),
        ("纯纵向空转（侧滑角小，不该报失控）", dict(
            speed_kmh=60.0, throttle=1.0,
            slip_ratio=(-0.02, -0.02, 0.80, 0.80),
            slip_angle=(0.03, 0.03, 0.06, 0.06))),
        ("路面纹理较重", dict(g_vibrations=2.0)),
    ]

    for label, changes in cases:
        engine.reset()
        clock = _run(engine, label, _snapshot(**changes), clock)

    print()
    print("=" * 62)
    print("看这几个地方")
    print("=" * 62)
    print("  滑行        四个都应该是 0（不能有底噪）")
    print("  轻刹 30%    左扳机跟着 trigger_floor 走（默认约 8%，即 1.00 × 0.28）")
    print("  前轮锁死    左扳机 90% 上下，来源是“前轮锁死 + 刹车压力”")
    print("  ABS 动作    左扳机 90% 上下，来源是“ABS 介入 + 刹车压力”")
    print("  后轮打滑    右扳机跟着滑移量走，滑得越多越强")
    print("  停着踩踏板  左右扳机都应该是 0（压力底层有车速门限）")
    print("  全油门直道  右扳机约 15%（油门底层），滑起来会更强")
    print("  轻推头      前 0.15 / 后 0.06，右扳机给中等强度")
    print("  推头        前 0.21 / 后 0.06，右扳机明显加强")
    print("  单前轮路肩  只有一只前轮尖峰，不该报推头")
    print("  打转        四个角一起失真，不算推头；两个握把报失控，右扳机是 0")
    print("  正常高速弯  握把只有路面纹理，失控应该是 0")
    print("  边界高速弯  后轮 0.16 也不该报（高速门槛 0.18）")
    print("  高速甩尾    两个握把一起震，滑得越多越强")
    print("  四轮漂移    前后都滑、后轮也大，照样报")
    print("  低速打转    25 km/h 也要报（门槛随车速降到约 0.29）")
    print("  低速正常弯  25 km/h 后轮 0.22 不该报（低速门槛 0.30）")
    print("  单轮路肩    两个后轮都要滑，单个尖峰不算")
    print("  纯纵向空转  只走右扳机打滑，握把不报失控")
    print("  路面纹理重  两侧握把一起震，强度跟路面基准走")
    print()
    print("想调强度：双击 调手感.bat，改完保存，1 秒内生效。")


if __name__ == "__main__":
    main()
