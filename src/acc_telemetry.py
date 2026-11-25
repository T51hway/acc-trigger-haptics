# -*- coding: utf-8 -*-
"""ACC 遥测层：读共享内存，交出一份"最新快照"。

这一层只做两件事：
    1. 读出 ACC 的物理页（Local\\acpmf_physics）
    2. 按实测偏移解析成一份固定结构的快照

它不判断"该不该震"，也不回调、不起线程。谁需要数据，谁就在自己的循环里
调一次 latest()。这样不会因为上层慢而堵住下层，延迟也最低——
共享内存是本机内存直读，比 UDP 少一次打包和排队（见 设计说明.md D2）。

为什么按显式字节偏移读，不建大结构体：
    一处偏移写错，后面的字段会一起错位，而且很容易"看起来有数"。
    显式写出每个字段的偏移，出错时打印出来的值会明显不合常理，一眼能发现。
    这张表另有实测数据兜底（见下方"偏移可信度"）。

偏移可信度：
    有实测锚点的字段（阶段 0 的 14555 帧 CSV 和 check_acc.py 都验证过）：
        packetId / gas / brake / gear / rpms / speedKmh / heading / abs
        wheelSlip / slipRatio / brakePressure / accG
        kerbVibration / slipVibrations / gVibrations / absVibrations
    其余字段（wheelLoad、suspensionTravel、wheelAngularSpeed、slipAngle、
    velocity、localVelocity、tc）按 ACC 物理页的公开布局写，未单独实测。
    物理页总长 800 字节；系统按页对齐给 4096 是正常的，只读前 800。

官方文档不能照抄，已知三处错误（设计说明.md 第二、五节）：
    - slipRatio 标成 int，实际是 float
    - 四个震动通道标成 int，实际是 float
    - accG 的轴序不是字面的 x纵/y横/z垂，而是 x横/y垂/z纵
      按字面理解会把重刹读成横向加速度

刻意不暴露的字段：
    wheelSlip[4] —— 实测数值到 222，与"滑移比×车速"对不齐，含义不明，
    判据一律用 slipRatio（设计说明.md 第五节第 2 条）。

用法：
    from acc_telemetry import AccTelemetry

    telemetry = AccTelemetry()
    while True:
        snap = telemetry.latest()      # 没数据时返回 None，不抛异常
        if snap is not None and snap.brake > 0:
            print(snap.slip_ratio[0], snap.brake_pressure[0])
        time.sleep(1 / 60)

    也可以 with AccTelemetry() as telemetry: ...

命令行自检（实时看数据，确认偏移和量纲还对）：
    python src/acc_telemetry.py
    python src/acc_telemetry.py --once
    python src/acc_telemetry.py --hz 5

只用 Python 标准库。
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import struct
import sys
import time

from i18n import t
from dataclasses import dataclass

MAPPING_NAME = "Local\\acpmf_physics"
PHYSICS_PAGE_SIZE = 800          # ACC 物理页固定 800 字节
FILE_MAP_READ = 0x0004

# 震动通道的归一化常数，来自阶段 0 实测（见 设计说明.md 第五节）
KERB_SCALE = 10.0                # kerbVibration 被截断在 ±0.1，×10 才是 0~1
G_VIBRATION_MAX = 7.5            # gVibrations 实测上限约 7.5，不截断

WHEELS = ("前左", "前右", "后左", "后右")   # ACC 的 [FL, FR, RL, RR]

# ── 字段表：名字, 类型, 字节偏移 ────────────────────────────────────
# 名字是 Python 侧的名字，注释里给 ACC 原名。
SCALARS = [
    ("packet_id", "i", 0),            # packetId，物理页每刷新一次 +1
    ("throttle", "f", 4),             # gas
    ("brake", "f", 8),                # brake
    ("gear_raw", "i", 16),            # gear，原始值 0=R、1=N、2=1 档、3=2 档……
    ("rpm", "i", 20),                 # rpms
    ("speed_kmh", "f", 28),           # speedKmh
    ("tc_level", "f", 204),           # tc，0~1
    ("heading", "f", 208),            # heading，-π~π 弧度（实测）
    ("abs_level", "f", 252),          # abs，0~1
    ("kerb_vibration", "f", 784),     # kerbVibration，±0.1
    ("slip_vibrations", "f", 788),    # slipVibrations，±1
    ("g_vibrations", "f", 792),       # gVibrations，0~7.5
    ("abs_vibrations", "f", 796),     # absVibrations，只有 -1/0/1
]

VECTORS = [
    # 名字, 偏移（四个 float，顺序固定为 前左、前右、后左、后右）
    ("wheel_load", 72),               # wheelLoad
    ("wheel_angular_speed", 104),     # wheelAngularSpeed
    ("suspension_travel", 184),       # suspensionTravel
    ("slip_ratio", 640),              # slipRatio，前轮 -1.0 = 完全抱死
    ("slip_angle", 656),              # slipAngle
    ("brake_pressure", 716),          # brakePressure，刹车输入 × 前后分配比
]

VEC3 = [
    # 名字, 偏移（三个 float）
    ("velocity", 32),                 # 世界坐标速度
    ("acc_g", 44),                    # ⚠ x=横向, y=垂直, z=纵向
    ("local_velocity", 568),          # 车身坐标速度
]

_I = struct.Struct("<i")
_F = struct.Struct("<f")
_F4 = struct.Struct("<4f")
_F3 = struct.Struct("<3f")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k32.OpenFileMappingW.restype = wintypes.HANDLE
_k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_size_t]
_k32.MapViewOfFile.restype = ctypes.c_void_p
_k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_k32.UnmapViewOfFile.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL


def _clamp01(value):
    # 写成这样顺带能挡住 NaN：NaN 的任何比较都是 False
    if not (value > 0.0):
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _console_log(text):
    print(text, flush=True)


@dataclass(frozen=True)
class Snapshot:
    """某一帧的 ACC 物理数据。

    四轮的量都是元组，顺序固定为 (前左, 前右, 后左, 后右)。
    时间戳 received_at 是读到的时刻（perf_counter），不是游戏的帧时间。

    字段对照（Python 名 ← ACC 原名）：
        throttle ← gas          rpm ← rpms        abs_level ← abs
        tc_level ← tc           speed_kmh ← speedKmh
        acc_g[3] 的轴序是 x=横向、y=垂直、z=纵向，别按字面用。

    ⚠ 档位：ACC 原始值是从 0 起算的——
        0=R、1=N、2=1 档、3=2 档……（2026-11-25 实测确认）
      为了不让人踩坑，这里拆成两个：
        gear_raw    ACC 原值，方便对照 CSV / 其他工具
        gear        直观值：-1=R、0=N、1=1 档……
        gear_label  直接显示的字符串："R" / "N" / "3"
      另外，换挡的那几十毫秒里 ACC 会短暂报 1（空档），这是正常的，
      以后如果要用档位做换挡震动，记得先去抖。

    几个容易误会的量纲（全部来自实测，详见 设计说明.md 第五节）：
        slip_ratio     无量纲比值，前轮 -1.0 = 完全抱死
        brake_pressure 不是 bar，是"刹车输入 × 前后分配比"。
                       实测前轴 0.742、后轴 0.258，前后相加为 1；
                       同一轴左右两轮的值相同。
        kerb_vibration ±0.1（被截断）
        slip_vibrations ±1（被截断）
        g_vibrations   0~7.5（不截断）
        abs_vibrations 只有 -1/0/1，本质是"ABS 正在动作"的方波
    """

    packet_id: int
    received_at: float
    throttle: float
    brake: float
    gear_raw: int
    rpm: int
    speed_kmh: float
    tc_level: float
    heading: float
    abs_level: float
    kerb_vibration: float
    slip_vibrations: float
    g_vibrations: float
    abs_vibrations: float
    wheel_load: tuple
    wheel_angular_speed: tuple
    suspension_travel: tuple
    slip_ratio: tuple
    slip_angle: tuple
    brake_pressure: tuple
    velocity: tuple
    acc_g: tuple
    local_velocity: tuple

    # ── 档位：ACC 原始值从 0 起算，这里换成直观值 ────────────────
    @property
    def gear(self):
        """-1=R、0=N、1=1 档……（ACC 原始值统一减 1）。"""
        return self.gear_raw - 1

    @property
    def gear_label(self):
        """直接显示的档位："R" / "N" / "1"……"""
        if self.gear < 0:
            return "R"
        if self.gear == 0:
            return "N"
        return str(self.gear)

    # ── 归一化后的震动通道，直接给混音层用 ──────────────────────
    @property
    def kerb_level(self):
        """路肩震动 0~1（实测被截断在 ±0.1，所以乘 10）。"""
        return _clamp01(self.kerb_vibration * KERB_SCALE)

    @property
    def slip_vibration_level(self):
        """打滑纹理 0~1（实测在 ±1，取绝对值）。"""
        return _clamp01(abs(self.slip_vibrations))

    @property
    def g_vibration_level(self):
        """纵向 G 纹理 0~1（实测上限约 7.5）。"""
        return _clamp01(self.g_vibrations / G_VIBRATION_MAX)

    @property
    def abs_vibration_level(self):
        """ABS 是否在动作，0 或 1（实测只有 -1/0/1 三种值）。"""
        return _clamp01(abs(self.abs_vibrations))


def _parse(raw, received_at):
    values = {"received_at": received_at}
    for name, kind, offset in SCALARS:
        values[name] = (_I if kind == "i" else _F).unpack_from(raw, offset)[0]
    for name, offset in VECTORS:
        values[name] = _F4.unpack_from(raw, offset)
    for name, offset in VEC3:
        values[name] = _F3.unpack_from(raw, offset)
    return Snapshot(**values)


def _explain_open_error(code):
    if code == 2:
        return t("tel_open_missing")
    if code == 5:
        return t("tel_open_denied")
    return t("tel_open_fail", code)


class AccTelemetry:
    """共享内存的持有者，负责连接、解析、丢了再连。

    不是线程安全的：请让同一个线程调 latest()（引擎循环那一个就够了）。
    ACC 没开、没进赛道、中途退出，latest() 都只返回 None，不抛异常。
    """

    def __init__(self, log=_console_log, retry_interval=1.0,
                 stale_after=0.5, close_after=10.0):
        """
        retry_interval：没连上时，多久重试一次（秒）
        stale_after：   包号多久不变就算"没有新数据"，返回 None（秒）
        close_after：   包号多久不变就主动断开共享内存（秒）。
                        断开是必要的：一直握着映射，ACC 下次启动时
                        会复用我们手里这块旧区域，容易出怪事。
        """
        self._log = log
        self._retry_interval = retry_interval
        self._stale_after = stale_after
        self._close_after = close_after

        self._handle = None
        self._addr = None
        self._last_try = -1e9
        self._last_packet_id = None
        self._last_change_at = 0.0
        self._need_fresh = False      # 超时断开过：重连后要看到包号推进才交数据
        self._state = None            # None / "live" / "stale" / "down"
        self.last_error = t("tel_never")

    # ── 对外接口 ──────────────────────────────────────────────────
    @property
    def connected(self):
        return self._addr is not None

    def attach(self):
        """尝试连上共享内存。成功返回 True；失败返回 False，原因在 last_error。"""
        if self._addr is not None:
            return True

        ctypes.set_last_error(0)
        handle = _k32.OpenFileMappingW(FILE_MAP_READ, False, MAPPING_NAME)
        if not handle:
            self.last_error = _explain_open_error(ctypes.get_last_error())
            self._note("down")
            return False

        addr = _k32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
        if not addr:
            error = ctypes.get_last_error()
            _k32.CloseHandle(handle)
            self.last_error = t("tel_map_fail", error)
            self._note("down")
            return False

        self._handle = handle
        self._addr = addr
        self._last_packet_id = None
        self._last_change_at = time.perf_counter()
        self.last_error = ""
        return True

    def latest(self):
        """返回最新的一帧 Snapshot；没有可用数据时返回 None。

        每调一次只做一次 800 字节拷贝，60Hz 调也毫无压力。
        """
        if self._addr is None:
            now = time.perf_counter()
            if now - self._last_try < self._retry_interval:
                return None
            self._last_try = now
            if not self.attach():
                return None

        snap = _parse(self._read_page(), time.perf_counter())

        # 包号往前走了 = 新数据。
        if (self._last_packet_id is not None
                and snap.packet_id != self._last_packet_id):
            self._last_packet_id = snap.packet_id
            self._last_change_at = snap.received_at
            self._need_fresh = False
            self._note("live")
            return snap

        if self._last_packet_id is None:
            self._last_packet_id = snap.packet_id
            self._last_change_at = snap.received_at
            # 超时断开后重连的第一帧可能是冻结的旧数据，先记下来，等包号推进
            if self._need_fresh:
                return None
            self._note("live")
            return snap

        idle = snap.received_at - self._last_change_at
        if idle > self._close_after:
            self.last_error = t("tel_stale_long")
            self._need_fresh = True
            self.detach()
            self._note("down")
            return None
        if idle > self._stale_after:
            self._note("stale")
            return None
        if self._need_fresh:
            return None

        # 包号没变但还在阈值内：正常，同一个物理帧被读了两次而已
        return snap

    def close(self):
        """断开共享内存。可重复调用。"""
        self.detach()
        self._state = None
        self._need_fresh = False

    def detach(self):
        addr, self._addr = self._addr, None
        handle, self._handle = self._handle, None
        if addr:
            _k32.UnmapViewOfFile(addr)
        if handle:
            _k32.CloseHandle(handle)
        self._last_packet_id = None

    def __enter__(self):
        self.attach()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ── 内部 ──────────────────────────────────────────────────────
    def _read_page(self):
        """拷出 800 字节，并尽量避开"读到一半被覆盖"的撕裂帧。

        物理页 333Hz 在写，我们 60Hz 在读，撞上的概率不高，
        但撕裂帧会出现一个野生 slipRatio，足以误触发一次震动。
        做法：拷完一遍，再读一次包号；两次一致就认为这一遍是完整的。
        """
        raw = b""
        for _ in range(4):
            raw = ctypes.string_at(self._addr, PHYSICS_PAGE_SIZE)
            first = _I.unpack_from(raw, 0)[0]
            again = _I.unpack_from(ctypes.string_at(self._addr, 4), 0)[0]
            if first == again:
                break
        return raw

    def _note(self, state):
        """只在状态变化时说话，避免每秒刷一行。"""
        if state == self._state:
            return
        previous, self._state = self._state, state
        if state == "live":
            if previous == "stale":
                self._say(t("tel_recovered"))
            else:
                self._say(t("tel_connected"))
        elif state == "stale":
            self._say(t("tel_stale_short", self._stale_after))
        elif state == "down":
            self._say(self.last_error)

    def _say(self, text):
        if self._log is None:
            return
        try:
            self._log(t("log_prefix_telemetry", time.strftime("%H:%M:%S"), text))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# 命令行自检
# ══════════════════════════════════════════════════════════════════
def _summary(snap):
    return ("[%07d] %6.1f km/h  档 %-2s %5d rpm  油门 %.2f  刹车 %.2f  |  "
            "滑移比 前 %6.3f/%6.3f  后 %6.3f/%6.3f  |  "
            "纹理 路肩 %.2f 滑移 %.2f G %.2f ABS %.2f"
            % (snap.packet_id, snap.speed_kmh, snap.gear_label, snap.rpm,
               snap.throttle, snap.brake,
               snap.slip_ratio[0], snap.slip_ratio[1],
               snap.slip_ratio[2], snap.slip_ratio[3],
               snap.kerb_level, snap.slip_vibration_level,
               snap.g_vibration_level, snap.abs_vibration_level))


def _details(snap):
    return [
        "包号 %-8d 速度 %6.1f km/h  档位 %-2s %d rpm   （ACC 原始档位值 %d）"
        % (snap.packet_id, snap.speed_kmh, snap.gear_label, snap.rpm, snap.gear_raw),
        "  油门 %.2f   刹车 %.2f   ABS %.2f   TC %.2f"
        % (snap.throttle, snap.brake, snap.abs_level, snap.tc_level),
        "  滑移比    " + "  ".join("%s %7.3f" % (WHEELS[i], snap.slip_ratio[i])
                                  for i in range(4)),
        "  打滑角    " + "  ".join("%s %7.3f" % (WHEELS[i], snap.slip_angle[i])
                                  for i in range(4)),
        "  刹车分配  前 %.3f  后 %.3f   （不是 bar，是前后分配比）"
        % (snap.brake_pressure[0], snap.brake_pressure[2]),
        "  震动原始  路肩 %.4f  滑移 %.4f  纵向G %.4f  ABS %.4f"
        % (snap.kerb_vibration, snap.slip_vibrations,
           snap.g_vibrations, snap.abs_vibrations),
    ]


def _enable_vt():
    """让老式控制台也能识别光标控制码（否则屏幕上会是乱码）。"""
    try:
        k32 = ctypes.WinDLL("kernel32")
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _wait_for_snapshot(telemetry, seconds):
    deadline = time.perf_counter() + seconds
    announced = False
    while time.perf_counter() < deadline:
        snap = telemetry.latest()
        if snap is not None:
            return snap
        if not announced:
            print("还没读到数据。请确认 ACC 已经进入赛道（不是主菜单、不是回放）。")
            announced = True
        time.sleep(0.2)
    return None


def main():
    parser = argparse.ArgumentParser(description="读 ACC 物理页，检查遥测层")
    parser.add_argument("--once", action="store_true", help="只打印一帧就退出")
    parser.add_argument("--hz", type=float, default=5.0, help="刷新频率，默认 5")
    parser.add_argument("--wait", type=float, default=20.0,
                        help="等 ACC 数据的秒数，默认 20")
    args = parser.parse_args()

    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("ACC 遥测层自检（src/acc_telemetry.py）")
    print("=" * 62)
    print("这一步需要 ACC 在跑，并且已经进入赛道。")
    print()

    telemetry = AccTelemetry()
    try:
        if args.once:
            snap = _wait_for_snapshot(telemetry, args.wait)
            if snap is None:
                print()
                print("等了 %.0f 秒还是没数据，先不测了。" % args.wait)
                print("  - ACC 是否已经进入赛道？（菜单里没有共享内存）")
                print("  - 如果是管理员权限开的 ACC，这个窗口也要用管理员打开")
                return 1
            for line in _details(snap):
                print(line)
            return 0

        snap = _wait_for_snapshot(telemetry, args.wait)
        if snap is None:
            print()
            print("等了 %.0f 秒还是没数据，先不测了。" % args.wait)
            print("  - ACC 是否已经进入赛道？（菜单里没有共享内存）")
            print("  - 如果是管理员权限开的 ACC，这个窗口也要用管理员打开")
            return 1

        interactive = sys.stdout.isatty()
        vt_ok = interactive and _enable_vt()
        prev_lines = 0
        last_plain = 0.0
        last_packet = None
        interval = 1.0 / max(args.hz, 0.5)

        while True:
            snap = telemetry.latest()
            now = time.perf_counter()
            if snap is None:
                if prev_lines:
                    sys.stdout.write("\n")
                    prev_lines = 0
                if now - last_plain >= 2.0:
                    print("（暂时没有新数据，等 ACC 回来……）")
                    last_plain = now
            elif snap.packet_id != last_packet:
                last_packet = snap.packet_id
                if vt_ok:
                    if prev_lines:
                        sys.stdout.write("\033[%dA\033[J" % prev_lines)
                    sys.stdout.write(_summary(snap) + "\n")
                    prev_lines = 1
                elif interactive:
                    if now - last_plain >= 1.0:
                        print(_summary(snap))
                        last_plain = now
                else:
                    print(_summary(snap))
                sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        print("已停止。")
        return 0
    finally:
        telemetry.close()


if __name__ == "__main__":
    sys.exit(main())
