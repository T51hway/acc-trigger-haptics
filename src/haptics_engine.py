# -*- coding: utf-8 -*-
"""判断与混音层：把遥测快照变成四个马达的强度。

输入：acc_telemetry.Snapshot
输出：Motors(left_grip, right_grip, left_trigger, right_trigger)，0.0~1.0
      顺序和 PadDriver.set_vibration 完全一致，可以直接：

          driver.set_vibration(*engine.update(snapshot))

分工（设计说明.md D4）：
    扳机 ← 短促、突然、要立刻反应的事（锁死、打滑、ABS 介入）
    握把 ← 持续、厚重、提供方位感的事（路面、路肩）

两层（D5）：
    底层：刹车压力 × 20%。不看屏幕也知道自己踩了几成，上限压得很低，
          免得盖住上层信息。
    上层：临界事件，60~80ms，最高 70%。

混音（D11）：
    加权求和 + 单次软限幅。同一马达同一时刻最多 1 个主效果 + 1 个辅效果
    （辅效果封顶 25%），其余直接丢掉。不用"平滑或"层层叠加，
    否则几个效果一叠就饱和，八件事糊成"在震"一件事。

一条必须接受的物理限制（阶段 0 实测）：
    前轮抱死不是渐进的，实测 -0.312 → -0.908 不到 20 毫秒，中间没有过渡。
    所以做不出"逼近极限时逐渐增强"，只能报两种状态：
        已经锁死（ABS 没介入时）        → 60~80ms 的急促脉冲
        ABS 正在救你（有介入信号时）    → 跟着 absVibrations 断续的嗒嗒

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
"""

import math
import time
from typing import NamedTuple

from acc_telemetry import Snapshot

# ── 参数初值（设计说明.md 第六节）────────────────────────────────
# 这些是"从数据分布推出来的候选值"，不是手感验证过的值。
# 阶段 3 的闭眼测试要重新定，先集中放在这里，方便一处改。
PRESSURE_FLOOR_MAX = 0.20        # 底层压力反馈上限 20%
EVENT_AMPLITUDE = 0.70           # 临界事件幅度 70%
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
ABS_AMPLITUDE = 0.70             # ABS 嗒嗒的幅度

GRIP_ROAD_WEIGHT = 0.80          # 路面纹理在握把上的权重
GRIP_KERB_WEIGHT = 0.90          # 路肩在握把上的权重
G_VIBRATION_DEADZONE = 0.05      # gVibrations 不设死区就是永远在震（实测 91.5% 的帧 >0.02）
G_VIBRATION_FULL = 7.5           # 实测上限
KERB_DEADZONE = 0.005
KERB_FULL = 0.10                 # kerbVibration 被截断在 ±0.1

SECONDARY_CAP = 0.25             # 辅效果封顶 25%（D11）
SOFT_KNEE = 0.70                 # 软限幅的拐点，拐点以下不动
MIX_FLOOR = 0.002                # 小于这个的贡献当作没有（报文本来就是 1% 一档）


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


def _load_bias(loads):
    """左右轮荷差，-1~1。用来给路肩分个左右。

    注意：这只是个便宜的启发式，不是真正的"路肩在哪一侧"。
    压路肩时那侧轮荷会抬起来，方向一般对，但弯道里的外侧也会更重。
    阶段 3 有了手感数据再改，现在先保证"有左右差别"。
    """
    values = [_num(v) for v in loads]
    total = sum(values)
    if total <= 1.0:
        return 0.0
    left = (values[0] + values[2]) / total
    return max(-1.0, min(1.0, (left - 0.5) * 4.0))


class HapticsEngine:
    """把一帧遥测变成四个马达值。有状态：事件包络、不应期、打滑收放。"""

    def __init__(self):
        self._event_start = -1e9
        self._rear_level = 0.0
        self._last_update = None
        self.last_trace = {}
        self.last_metrics = {}

    def reset(self):
        """清掉事件和包络。遥测中断、回到维修区、换车时调一下。"""
        self._event_start = -1e9
        self._rear_level = 0.0
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

        bias = _load_bias(snap.wheel_load)
        road = _scale(_num(snap.g_vibrations),
                      G_VIBRATION_DEADZONE, G_VIBRATION_FULL)
        kerb = _scale(abs(_num(snap.kerb_vibration)), KERB_DEADZONE, KERB_FULL)

        left_grip, left_grip_trace = _mix([
            ("路面", road * GRIP_ROAD_WEIGHT),
            ("左路肩", kerb * (0.5 + 0.5 * bias) * GRIP_KERB_WEIGHT),
        ])
        right_grip, right_grip_trace = _mix([
            ("路面", road * GRIP_ROAD_WEIGHT),
            ("右路肩", kerb * (0.5 - 0.5 * bias) * GRIP_KERB_WEIGHT),
        ])

        # ── 左扳机：刹车压力打底，锁死 / ABS 覆盖上去 ────────────
        brake = _clamp01(_num(snap.brake))
        front_slip = min(_num(snap.slip_ratio[0]), _num(snap.slip_ratio[1]))
        abs_flag = _clamp01(_num(snap.abs_level))
        abs_pulse = _clamp01(abs(_num(snap.abs_vibrations)))
        abs_active = max(abs_flag, abs_pulse) > ABS_GATE

        left_trigger_parts = [("刹车压力", brake * PRESSURE_FLOOR_MAX)]

        if abs_active:
            # 节奏用 absVibrations 这个方波，abs 只当"ABS 在工作"的开关。
            # 实测 abs 只有 0 和 1（不是强度），拿它当幅度会变成一直满着；
            # 方波归零的那几拍就让它静音，这才有"嗒嗒"的断续感。
            if abs_pulse > 0.0:
                left_trigger_parts.append(("ABS 介入", ABS_AMPLITUDE * abs_pulse))
        elif front_slip <= FRONT_LOCK_THRESHOLD:
            if now >= self._event_start + EVENT_REFRACTORY:
                self._event_start = now
            age = now - self._event_start
            if age < EVENT_DURATION:
                amplitude = EVENT_AMPLITUDE
                remaining = EVENT_DURATION - age
                if remaining < EVENT_RELEASE:
                    amplitude *= remaining / EVENT_RELEASE
                left_trigger_parts.append(("前轮锁死", amplitude))

        left_trigger, left_trigger_trace = _mix(left_trigger_parts)

        # ── 右扳机：后轮打滑，连续的，起振快、收振慢 ──────────────
        rear_slip = max(_num(snap.slip_ratio[2]), _num(snap.slip_ratio[3]))
        target = 0.0
        if rear_slip > REAR_SLIP_THRESHOLD:
            severity = _scale(rear_slip, REAR_SLIP_THRESHOLD, REAR_SLIP_FULL)
            target = EVENT_AMPLITUDE * severity ** REAR_CURVE

        if first_tick or dt <= 0.0:
            self._rear_level = target
        elif target >= self._rear_level:
            self._rear_level += (target - self._rear_level) * min(1.0, dt / REAR_ATTACK)
        else:
            self._rear_level += (target - self._rear_level) * min(1.0, dt / REAR_RELEASE)

        right_trigger, right_trigger_trace = _mix([("后轮打滑", self._rear_level)])

        self.last_trace = {
            "left_grip": left_grip_trace,
            "right_grip": right_grip_trace,
            "left_trigger": left_trigger_trace,
            "right_trigger": right_trigger_trace,
        }
        self.last_metrics = {
            "brake": brake,
            "front_slip": front_slip,
            "rear_slip": rear_slip,
            "abs_active": abs_active,
            "abs_flag": abs_flag,
            "abs_pulse": abs_pulse,
            "road": road,
            "kerb": kerb,
            "load_bias": bias,
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
    print("=" * 62)
    print("判断与混音层自检（src/haptics_engine.py）")
    print("=" * 62)
    print("喂几段写死的遥测，看四个马达的输出。不需要 ACC，也不需要手柄。")

    engine = HapticsEngine()
    clock = 0.0

    cases = [
        ("滑行：什么都没踩", {}),
        ("轻刹 30%", dict(brake=0.30)),
        ("重刹到底，但没锁死", dict(brake=1.0, slip_ratio=(-0.06, -0.05, 0.02, 0.02))),
        ("前轮锁死（ABS 没介入）", dict(brake=1.0, slip_ratio=(-0.90, -0.88, 0.02, 0.02))),
        ("ABS 正在动作", dict(brake=1.0, abs_level=1.0, abs_vibrations=1.0,
                             slip_ratio=(-0.05, -0.05, 0.02, 0.02))),
        ("后轮打滑 0.50", dict(throttle=1.0, slip_ratio=(-0.02, -0.02, 0.50, 0.45))),
        ("压左路肩", dict(g_vibrations=2.0, kerb_vibration=0.06,
                         wheel_load=(4000.0, 2600.0, 3600.0, 2400.0))),
        ("压右路肩", dict(g_vibrations=2.0, kerb_vibration=-0.06,
                         wheel_load=(2600.0, 4000.0, 2400.0, 3600.0))),
    ]

    for label, changes in cases:
        engine.reset()
        clock = _run(engine, label, _snapshot(**changes), clock)

    print()
    print("=" * 62)
    print("看这几个地方")
    print("=" * 62)
    print("  滑行        四个都应该是 0（不能有底噪）")
    print("  轻刹 30%    左扳机约 6%（20% × 0.3）")
    print("  前轮锁死    左扳机 80% 上下，来源是“前轮锁死 + 刹车压力”")
    print("  ABS 动作    左扳机 80% 上下，来源是“ABS 介入 + 刹车压力”")
    print("  后轮打滑    右扳机跟着滑移量走，滑得越多越强")
    print("  压路肩      两侧握把都有，压到的那一侧更强")
    print()
    print("参数都在文件开头，阶段 3 按手感改那几个数就行。")


if __name__ == "__main__":
    main()
