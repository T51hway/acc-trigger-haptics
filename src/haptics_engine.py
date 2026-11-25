# -*- coding: utf-8 -*-
"""判断与混音层：把遥测快照变成四个马达的强度。

输入：acc_telemetry.Snapshot
输出：Motors(left_grip, right_grip, left_trigger, right_trigger)，0.0~1.0
      顺序和 PadDriver.set_vibration 完全一致，可以直接：

          driver.set_vibration(*engine.update(snapshot))

分工（设计说明.md D4）：
    扳机 ← 短促、突然、要立刻反应的事（锁死、打滑、ABS 介入、推头）
    握把 ← 持续、厚重、提供方位感的事（路面纹理）

推头（2026-11-25 实测标定）：
    判据是"前轮侧滑角比后轮大多少"，不是绝对角度——前轮被方向盘拧着走，
    天生就比后轮大；真正推头时才会明显超过后轮。
    110 秒实测：正常过弯差值 0.01~0.05 弧度，推头时 0.07~0.13。
    所以 0.05 起、0.13 满，用 40ms 起振 / 200ms 收振跟随（推头是持续状态）。

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

# ── 参数初值（设计说明.md 第六节）────────────────────────────────
# 这些是"从数据分布推出来的候选值"，不是手感验证过的值。
# 阶段 3 的闭眼测试要重新定，先集中放在这里，方便一处改。
PRESSURE_FLOOR_MAX = 0.20        # 底层压力反馈上限 20%（D5）
THROTTLE_FLOOR_MAX = 0.15        # 右扳机·油门底层；比刹车低，避免长时间给油一直嗡嗡

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
UNDERSTEER_START = 0.05          # 约 2.9°，开始有感觉
UNDERSTEER_FULL = 0.13           # 约 7.4°，到满幅
UNDERSTEER_CURVE = 0.7           # 温和抬升，别让刚推头就满
UNDERSTEER_ATTACK = 0.040        # 40ms 起振
UNDERSTEER_RELEASE = 0.200       # 200ms 收振：推头是持续状态，不要抖
UNDERSTEER_SPEED_MIN = 25.0      # km/h 以下不判（停车、倒车、维修区）
UNDERSTEER_SLIP_MAX = 0.5        # 弧度，超过这个算打转/出界，不判推头
UNDERSTEER_MIN_REAR = 0.003      # 后轮几乎为 0 时没有参考，跳过
UNDERSTEER_GAIN = 1.00           # 推头强度（独立于事件强度，见 手感设置.toml）

GRIP_ROAD_WEIGHT = 0.21          # 路面纹理基准（2026-11-25 从 1.00 降下来：原标度整体偏大）
GRIP_ROAD_CURVE = 0.50           # 轻纹理抬升（越小越明显）
G_VIBRATION_DEADZONE = 0.05      # gVibrations 不设死区就是永远在震（实测 91.5% 的帧 >0.02）
G_VIBRATION_FULL = 7.5           # 实测上限

SECONDARY_CAP = 0.25             # 辅效果封顶 25%（D11）
SOFT_KNEE = 0.70                 # 软限幅的拐点，拐点以下不动
MIX_FLOOR = 0.002                # 小于这个的贡献当作没有（报文本来就是 1% 一档）


# ══════════════════════════════════════════════════════════════════
# 手感设置（外部文件）
# ══════════════════════════════════════════════════════════════════
# 只把最常调的四项放出去。其余（阈值、曲线、混音预算）等阶段 3 的
# 闭眼测试有结论了再往外放，免得一次给太多旋钮反而不知道动哪个。
DEFAULT_TUNING = {
    "grip_road": GRIP_ROAD_WEIGHT,
    "trigger_floor": PRESSURE_FLOOR_MAX,
    "throttle_floor": THROTTLE_FLOOR_MAX,
    "trigger_event": EVENT_AMPLITUDE,
    "understeer_gain": UNDERSTEER_GAIN,
}
TUNING_RANGES = {
    "grip_road": (0.0, 0.80),
    "trigger_floor": (0.0, 0.50),
    "throttle_floor": (0.0, 0.35),
    "trigger_event": (0.0, 1.0),
    "understeer_gain": (0.0, 1.50),
}

CONFIG_NAME = "手感设置.toml"
CONFIG_TEMPLATE = """# ACC 手柄震动 · 手感设置
#
# 只改等号 = 右边的数字。# 后面是说明，改了不会生效。
# 改完保存：程序每秒检查一次，1 秒内生效，不用重启。
# 强度类：数字越大越强。路面纹理 0.21 左右是基准（实测手感）。
# 注意：超过 0.6 容易把马达顶满，差别会被抹平。

grip_road = %(grip_road).2f      # 握把·路面纹理

# 左扳机：刹车压力底层（0 ~ 0.35 比较合理，代码上限 0.50）。D5 基准 0.20；
# 调高更有实感，但锁死 / ABS 的对比会变弱。
trigger_floor = %(trigger_floor).2f

# 右扳机：油门压力底层（0 ~ 0.35 比较合理）。踩多少震多少，
# 调高更有实感，但后轮打滑 / 推头的对比会变弱。
throttle_floor = %(throttle_floor).2f

trigger_event = %(trigger_event).2f   # 扳机事件：锁死 / ABS / 后轮打滑（0 ~ 1.0）

# 右扳机：推头强度（前轮比后轮多滑时震）。1.0 是满幅，0 = 关掉推头。
understeer_gain = %(understeer_gain).2f
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


def load_tuning(path=None):
    """读手感设置。没有文件、格式坏了、值超范围，都退回默认值。"""
    tuning = dict(DEFAULT_TUNING)
    path = Path(path) if path is not None else config_path()
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return tuning
    except Exception as exc:
        print("[手感] 读取 %s 失败：%s（先用默认值）" % (path.name, exc))
        return tuning

    for key in data:
        if key not in TUNING_RANGES:
            print("[手感] 不认识的设置项 %s，已忽略。" % key)

    for key, (low, high) in TUNING_RANGES.items():
        if key not in data:
            continue
        try:
            value = float(data[key])
        except (TypeError, ValueError):
            print("[手感] %s 不是数字，忽略。" % key)
            continue
        if not math.isfinite(value):
            print("[手感] %s 不是有效数字，忽略。" % key)
            continue
        clamped = max(low, min(high, value))
        if clamped != value:
            print("[手感] %s = %.2f 超出 %.2f~%.2f，按 %.2f 处理。"
                  % (key, value, low, high, clamped))
        tuning[key] = clamped
    return tuning


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
        self._grip_peak = [0.0, 0.0]
        self._last_update = None
        self.last_trace = {}
        self.last_metrics = {}

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
        speed = _num(snap.speed_kmh)
        # 停车时把"压力底层"关掉：停着踩满刹车也一直震，像故障
        floor_factor = _scale(speed, FLOOR_SPEED_OFF, FLOOR_SPEED_FULL)

        # 握把只剩路面纹理（路肩通道 2026-11-25 移除：用户用不上，
        # 而且它会和路面抢同一个马达的预算）
        left_grip, left_grip_trace = _mix([("路面", road * self.tuning["grip_road"])])
        right_grip, right_grip_trace = _mix([("路面", road * self.tuning["grip_road"])])
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
            ("刹车压力", brake * self.tuning["trigger_floor"] * floor_factor),
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
        front_angle = max(abs(_num(snap.slip_angle[0])),
                          abs(_num(snap.slip_angle[1])))
        rear_angle = max(abs(_num(snap.slip_angle[2])),
                         abs(_num(snap.slip_angle[3])))
        understeer_target = 0.0
        if (speed > UNDERSTEER_SPEED_MIN
                and front_angle < UNDERSTEER_SLIP_MAX
                and rear_angle < UNDERSTEER_SLIP_MAX
                and rear_angle >= UNDERSTEER_MIN_REAR):
            severity = _scale(front_angle - rear_angle,
                              UNDERSTEER_START, UNDERSTEER_FULL)
            understeer_target = (self.tuning["understeer_gain"]
                                 * severity ** UNDERSTEER_CURVE)

        if first_tick or dt <= 0.0:
            self._understeer_level = understeer_target
        elif understeer_target >= self._understeer_level:
            self._understeer_level += ((understeer_target - self._understeer_level)
                                       * min(1.0, dt / UNDERSTEER_ATTACK))
        else:
            self._understeer_level += ((understeer_target - self._understeer_level)
                                       * min(1.0, dt / UNDERSTEER_RELEASE))

        right_trigger, right_trigger_trace = _mix([
            ("油门压力", throttle * self.tuning["throttle_floor"] * floor_factor),
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
        throttle=0.0, brake=0.0, gear_raw=2, rpm=1200, speed_kmh=100.0,
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
    return clock


def main():
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
        ("推头（前轮比后轮多滑）", dict(speed_kmh=130.0, throttle=1.0,
                                    slip_angle=(0.15, 0.15, 0.06, 0.06))),
        ("打转（四轮都大，不算推头）", dict(speed_kmh=80.0,
                                     slip_angle=(0.9, 0.9, 0.85, 0.85))),
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
    print("  轻刹 30%    左扳机跟着 trigger_floor 走（默认约 6%，个人配置会更高）")
    print("  前轮锁死    左扳机 90% 上下，来源是“前轮锁死 + 刹车压力”")
    print("  ABS 动作    左扳机 90% 上下，来源是“ABS 介入 + 刹车压力”")
    print("  后轮打滑    右扳机跟着滑移量走，滑得越多越强")
    print("  停着踩踏板  左右扳机都应该是 0（压力底层有车速门限）")
    print("  全油门直道  右扳机约 15%（油门底层），滑起来会更强")
    print("  推头        右扳机跟着“前轮比后轮多滑”的程度走")
    print("  打转        四个角一起失真，不算推头，右扳机应该是 0")
    print("  路面纹理重  两侧握把一起震，强度跟路面基准走")
    print()
    print("想调强度：双击 调手感.bat，改完保存，1 秒内生效。")


if __name__ == "__main__":
    main()
