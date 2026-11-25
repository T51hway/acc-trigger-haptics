# -*- coding: utf-8 -*-
"""阶段 0 验证脚本 ①：能不能读到 ACC 的物理数据。

只做一件事：把 Local\\acpmf_physics 里的几个字段原样打印出来，
顺便统计它们的最小值/最大值，用来回答设计说明第五节的三个问题：
  - wheelSlip 的量纲是什么，能不能直接当 0~1 用
  - slipRatio 到底有没有值，是整数还是小数（官方文档写 int，很可疑）
  - kerbVibration 等四个震动通道的实际范围
  - slipAngle 的单位和推头时的分布（2026-11-25 加，用于标定推头通道）
  - wheelLoad 行驶时是否非零（2026-11-25 加，顺带回答交接文档里的老问题）

用法：
    python tools/check_acc.py            # 实时刷新，Ctrl+C 结束并打印统计
    python tools/check_acc.py --once     # 只打印一帧就退出
    python tools/check_acc.py --hz 20    # 刷新频率（默认 10）
    python tools/check_acc.py --seconds 180 --csv x.csv
                                         # 倒计时采集；开始/剩30秒/剩10秒/结束都有提示音
    python tools/check_acc.py --no-beep  # 不出声（自动化测试用）

只用 Python 标准库。
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import struct
import sys
import time

MAPPING_NAME = "Local\\acpmf_physics"
PHYSICS_PAGE_SIZE = 800          # ACC 物理页固定 800 字节

# ── 字段表：全部按官方文档的字节偏移硬写，不建大结构体 ──────────────────
# 这样一旦某个偏移错了，打印出来的数值会明显不合常理，一眼能发现。
#
# 注意：下面「震动通道」四个字段，官方文档标的是 int，实测是 float。
# 按 int 读出来是 ±11 亿这种位数，其实是浮点的位模式（1.0f = 1065353216）。
# 已按实测改正。文档在这里是错的。
SCALARS = [
    # 名字,              类型, 偏移
    ("packetId",         "i",  0),
    ("gas",              "f",  4),
    ("brake",            "f",  8),
    ("gear",             "i",  16),
    ("rpms",             "i",  20),
    ("speedKmh",         "f",  28),
    ("heading",          "f",  208),
    ("abs",              "f",  252),
    ("kerbVibration",    "f",  784),
    ("slipVibrations",   "f",  788),
    ("gVibrations",      "f",  792),
    ("absVibrations",    "f",  796),
]

VECTORS = [
    # 名字,              偏移
    ("wheelSlip",        56),
    ("wheelLoad",        72),        # 2026-11-25 加：验证行驶时是否非零
    ("suspensionTravel", 184),
    ("slipRatio",        640),
    ("slipAngle",        656),       # 2026-11-25 加：推头标定用
    ("brakePressure",    716),
]

VEC3 = [
    # 名字,              偏移（三轴）
    ("accG",             44),
]

WHEELS = ("左前", "右前", "左后", "右后")
AXES = ("X", "Y", "Z")


# ── 错误说明 ────────────────────────────────────────────────────────
def enable_vt():
    """让老式控制台也能识别光标控制码（否则屏幕上会是乱码）。

    双击 .bat 打开的窗口未必支持，所以先试着开，开不了就退回朴素模式。
    """
    try:
        k32 = ctypes.WinDLL("kernel32")
        handle = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def die(title, detail):
    print()
    print("读取失败：" + title)
    for line in detail:
        print("  " + line)
    print()
    sys.exit(1)


def _beep(freq, ms):
    """响一声。没有声卡或控制台也能跑，出错就算了。"""
    try:
        import winsound
        winsound.Beep(int(freq), int(ms))
    except Exception:
        pass


def _set_title(text):
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(str(text))
    except Exception:
        pass


def _mmss(seconds):
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def open_mapping():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]

    handle = k32.OpenFileMappingW(0x0004, False, MAPPING_NAME)  # FILE_MAP_READ
    if not handle:
        err = ctypes.get_last_error()
        if err == 2:
            die("找不到共享内存 Local\\acpmf_physics",
                ["说明 ACC 没在运行，或者还在主菜单没进赛道。",
                 "共享内存是 ACC 进赛道后才建起来的。",
                 "排查顺序：ACC 是否已启动 → 是否已进入实际驾驶（不是菜单/回放）"])
        elif err == 5:
            die("找到了但没权限打开（拒绝访问）",
                ["ACC 可能以管理员身份运行，权限比本脚本高。",
                 "解决办法：用管理员身份运行本脚本再试。"])
        else:
            die("打开共享内存失败，系统错误码 %d" % err,
                ["错误码不是 2 也不是 5，属于少见情况，把这一行原样贴出来即可。"])

    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    addr = k32.MapViewOfFile(handle, 0x0004, 0, 0, 0)  # 映射整块
    if not addr:
        die("共享内存存在，但映射到本进程失败（系统错误码 %d）" % ctypes.get_last_error(),
            ["通常是权限问题，先试管理员运行。"])

    # 问系统这块区域到底多大，防止读越界
    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p),
                    ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD),
                    ("__align1", wintypes.DWORD),
                    ("RegionSize", ctypes.c_size_t),
                    ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD),
                    ("Type", wintypes.DWORD),
                    ("__align2", wintypes.DWORD)]

    k32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    mbi = MBI()
    if not k32.VirtualQuery(addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
        die("取共享内存大小失败", ["无法确定页面尺寸，不敢继续读，怕越界。"])
    return addr, int(mbi.RegionSize)


# ── 取值 ────────────────────────────────────────────────────────────
def read_all(buf):
    """把整页按字段表解成字典。buf 是 bytes。"""
    out = {}
    for name, kind, off in SCALARS:
        if kind == "i":
            out[name] = struct.unpack_from("<i", buf, off)[0]
        else:
            out[name] = struct.unpack_from("<f", buf, off)[0]
    for name, off in VECTORS:
        out[name] = list(struct.unpack_from("<4f", buf, off))
    for name, off in VEC3:
        out[name] = list(struct.unpack_from("<3f", buf, off))
    return out


class Stats:
    """记录每个数值出现过的最小值/最大值，用来摸清量纲。"""

    def __init__(self):
        self.data = {}
        self.samples = 0

    def feed(self, label, value):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            self.data.setdefault(label, [float("nan"), float("nan")])
            return
        lo_hi = self.data.get(label)
        if lo_hi is None:
            self.data[label] = [value, value]
        else:
            if value < lo_hi[0]:
                lo_hi[0] = value
            if value > lo_hi[1]:
                lo_hi[1] = value

    def report(self):
        print()
        print("=" * 62)
        print("统计结果（本次运行期间出现的范围）")
        print("=" * 62)
        print("%-26s %12s %12s" % ("字段", "最小", "最大"))
        print("-" * 62)
        for label, (lo, hi) in self.data.items():
            print("%-26s %12.4f %12.4f" % (label, lo, hi))
        print("-" * 62)
        print("共 %d 帧。" % self.samples)
        print()
        print("看这几个地方：")
        print("  四个震动通道   应该能看到明显峰值（压路肩、抱死、ABS 介入时）")
        print("  打滑量         抱死瞬间前轮 slipRatio 会逼近 -1.0，这是最有用的判据")
        print("  数值一直很平   可能是采样太稀漏掉了峰，把 --hz 调到 50 再跑一次")
        print()


def sanity_warnings(d, region_size):
    """偏移写错时，数值会明显不合常理。这里主动挑出来提醒。"""
    warn = []
    if region_size < PHYSICS_PAGE_SIZE:
        warn.append("映射区域只有 %d 字节，小于物理页需要的 %d 字节。"
                    % (region_size, PHYSICS_PAGE_SIZE))
    if not (0.0 <= d["gas"] <= 1.001):
        warn.append("gas = %.3f 不在 0~1，偏移可能不对。" % d["gas"])
    if not (0.0 <= d["brake"] <= 1.001):
        warn.append("brake = %.3f 不在 0~1，偏移可能不对。" % d["brake"])
    if not (-2 <= d["gear"] <= 12):
        warn.append("gear = %d 超出 -1~8 的常识范围，偏移可能不对。" % d["gear"])
    if not (0.0 <= d["speedKmh"] < 600):
        warn.append("speedKmh = %.1f 超出常识范围，偏移可能不对。" % d["speedKmh"])
    for i, v in enumerate(d["wheelSlip"]):
        if not (-50.0 < v < 50.0):
            warn.append("wheelSlip[%s] = %.3f 数值离谱，偏移可能不对。" % (WHEELS[i], v))
            break
    return warn


def main():
    ap = argparse.ArgumentParser(description="读 ACC 物理页，验证数据能不能拿到")
    ap.add_argument("--once", action="store_true", help="只打印一帧就退出")
    ap.add_argument("--hz", type=float, default=10.0, help="刷新频率，默认 10")
    ap.add_argument("--csv", metavar="文件路径",
                    help="把每帧原始数据另存为 csv，便于离线分析")
    ap.add_argument("--seconds", type=float, default=0,
                    help="跑满这么多秒自动停下（默认 0 = 一直跑，按 Ctrl+C 结束）")
    ap.add_argument("--no-beep", action="store_true",
                    help="不响提示音（自动化测试用）")
    args = ap.parse_args()

    # 管道输出时统一成 UTF-8；真控制台保持系统默认（Python 会走宽字符接口）
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    addr, region_size = open_mapping()
    print("已连上 Local\\acpmf_physics，映射区域 %d 字节（系统按页对齐，真实长度就是 %d）。"
          % (region_size, PHYSICS_PAGE_SIZE))

    readable = min(region_size, PHYSICS_PAGE_SIZE)
    raw = (ctypes.c_ubyte * readable).from_address(addr)
    stats = Stats()
    interval = 1.0 / max(args.hz, 0.5)

    # CSV：一帧一行，方便事后分析分布和相关关系（min/max 表看不出来的东西）
    csv_file = None
    csv_writer = None
    if args.csv:
        import csv
        csv_file = open(args.csv, "w", newline="", encoding="utf-8-sig")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(
            ["packetId", "speedKmh", "gas", "brake", "gear", "rpms", "abs", "heading",
             "accGx", "accGy", "accGz",
             "kerbVibration", "slipVibrations", "gVibrations", "absVibrations"]
            + ["wheelSlip_" + w for w in ("LF", "RF", "LR", "RR")]
            + ["slipRatio_" + w for w in ("LF", "RF", "LR", "RR")]
            + ["slipAngle_" + w for w in ("LF", "RF", "LR", "RR")]
            + ["wheelLoad_" + w for w in ("LF", "RF", "LR", "RR")]
            + ["suspTravel_" + w for w in ("LF", "RF", "LR", "RR")]
            + ["brakePressure_" + w for w in ("LF", "RF", "LR", "RR")])

    if args.seconds:
        print()
        print("开始记录，共 %d 秒（约 %.0f 分钟）。" % (args.seconds, args.seconds / 60.0))
        print("现在切到 ACC 开始开车，时间到了会自动停下。")
        print("听声音就行：开始 1 声、剩 30 秒 2 声、剩 10 秒 3 声、结束 1 长声。")
        print()
        if not args.no_beep:
            _beep(880, 120)
        _set_title("采集中…")

    # 刷新率：用包号的绝对增量算，采样慢也不会低估
    win_start = time.perf_counter()
    win_packet = None
    packet_rate = 0.0
    last_packet = None
    last_change = time.perf_counter()
    frozen_hint_shown = False
    frames = 0
    prev_lines = 0
    d = None
    interactive = sys.stdout.isatty()
    vt_ok = interactive and enable_vt()
    plain_console = interactive and not vt_ok
    last_plain = 0.0
    t_start = time.perf_counter()
    timed_out = False
    cue_30 = False
    cue_10 = False
    last_title = 0.0

    try:
        while True:
            buf = bytes(raw)          # 一次拷出来，避免读到一半数据被覆盖
            d = read_all(buf)
            frames += 1

            # 刷新率估算
            now = time.perf_counter()

            # 倒计时提示：全屏开车看不见窗口，用声音和标题栏报进度
            if args.seconds:
                left = max(0.0, args.seconds - (now - t_start))
                if not args.no_beep:
                    if left <= 30 and not cue_30 and args.seconds > 32:
                        cue_30 = True
                        _beep(700, 100)
                        time.sleep(0.06)
                        _beep(700, 100)
                    if left <= 10 and not cue_10:
                        cue_10 = True
                        for _ in range(3):
                            _beep(700, 80)
                            time.sleep(0.05)
                if now - last_title >= 1.0:
                    last_title = now
                    _set_title("采集中 · 剩 %s · 已 %d 帧"
                               % (_mmss(left), frames))

            if win_packet is None:
                win_packet = d["packetId"]
                win_start = now
            elif now - win_start >= 1.0:
                packet_rate = (d["packetId"] - win_packet) / (now - win_start)
                win_packet = d["packetId"]
                win_start = now

            if last_packet is not None and d["packetId"] != last_packet:
                last_change = now
            elif now - last_change > 1.0 and not frozen_hint_shown:
                print()
                print("注意：包号已经 1 秒没变了。游戏可能停在菜单、暂停，或者已经退出。")
                print("      数据是死的，不是脚本读不到。")
                frozen_hint_shown = True
            last_packet = d["packetId"]

            # 统计
            for name, _kind, _off in SCALARS:
                stats.feed(name, float(d[name]))
            for name, _off in VECTORS:
                for i in range(4):
                    stats.feed("%s[%s]" % (name, WHEELS[i]), d[name][i])
            for name, _off in VEC3:
                for i in range(3):
                    stats.feed("%s[%s]" % (name, AXES[i]), d[name][i])
            stats.samples = frames

            # 写 CSV（用于离线分析，一帧一行）
            if csv_writer is not None:
                csv_writer.writerow(
                    [d["packetId"], d["speedKmh"], d["gas"], d["brake"], d["gear"],
                     d["rpms"], d["abs"], d["heading"],
                     d["accG"][0], d["accG"][1], d["accG"][2],
                     d["kerbVibration"], d["slipVibrations"],
                     d["gVibrations"], d["absVibrations"]]
                    + d["wheelSlip"] + d["slipRatio"] + d["slipAngle"]
                    + d["wheelLoad"] + d["suspensionTravel"] + d["brakePressure"])
                csv_file.flush()

            # 打印
            warn = sanity_warnings(d, region_size)
            lines = []
            lines.append("包号 %-10d 刷新 %5.0f Hz   速度 %6.1f km/h   油门 %.2f   刹车 %.2f   档位 %2d   ABS %.2f"
                         % (d["packetId"], packet_rate, d["speedKmh"], d["gas"],
                            d["brake"], d["gear"], d["abs"]))
            lines.append("  wheelSlip     " + "  ".join(
                "%s %7.3f" % (WHEELS[i], d["wheelSlip"][i]) for i in range(4)))
            lines.append("  slipRatio     " + "  ".join(
                "%s %7.3f" % (WHEELS[i], d["slipRatio"][i]) for i in range(4)))
            lines.append("  slipAngle     " + "  ".join(
                "%s %7.3f" % (WHEELS[i], d["slipAngle"][i]) for i in range(4)))
            lines.append("  wheelLoad     " + "  ".join(
                "%s %6.0f" % (WHEELS[i], d["wheelLoad"][i]) for i in range(4)))
            lines.append("  accG          " + "  ".join(
                "%s %7.3f" % (AXES[i], d["accG"][i]) for i in range(3)))
            lines.append("  震动通道      路肩 %6.3f   打滑 %6.3f   纵向G %6.3f   ABS %6.3f"
                         % (d["kerbVibration"], d["slipVibrations"],
                            d["gVibrations"], d["absVibrations"]))
            if csv_writer is not None:
                left = ""
                if args.seconds:
                    left = "   还剩 %d 秒" % max(0, int(args.seconds - (now - t_start)))
                lines.append("  已记录 %d 帧 → %s%s" % (frames, args.csv, left))
            for msg in warn:
                lines.append("  [可疑] " + msg)

            if args.once:
                for ln in lines:
                    print(ln)
                stats.report()
                return

            # 覆盖上一次的显示
            block = "\n".join(lines)
            if vt_ok:
                # 正常情况：原地刷新
                if prev_lines:
                    sys.stdout.write("\033[%dA\033[J" % prev_lines)
                sys.stdout.write(block + "\n")
                prev_lines = len(lines)
            elif plain_console:
                # 老式控制台：每秒打一行，不刷屏
                if now - last_plain >= 1.0:
                    left = ""
                    if args.seconds:
                        left = "还剩 %3d 秒  " % max(0, int(args.seconds - (now - t_start)))
                    print("%s速度 %5.1f km/h  油门 %.2f  刹车 %.2f  档位 %2d  "
                          "路肩 %.3f 打滑 %.3f 纵向G %.3f ABS %.3f"
                          % (left, d["speedKmh"], d["gas"], d["brake"], d["gear"],
                             d["kerbVibration"], d["slipVibrations"],
                             d["gVibrations"], d["absVibrations"]))
                    last_plain = now
            else:
                # 输出被重定向（比如存日志）：逐帧打全量
                sys.stdout.write(block + "\n")
            sys.stdout.flush()

            if args.seconds and (time.perf_counter() - t_start) >= args.seconds:
                timed_out = True
                break

            time.sleep(interval)

    except KeyboardInterrupt:
        print()
        stats.report()
        if d is not None:
            for msg in sanity_warnings(d, region_size):
                print("  [可疑] " + msg)
    finally:
        if csv_file is not None:
            csv_file.close()
            print()
            print("CSV 已保存：%s（共 %d 帧）" % (args.csv, frames))

    if timed_out:
        print()
        print("时间到，自动结束了。")
        if not args.no_beep:
            _beep(600, 500)
        _set_title("采集完成（%d 帧）" % frames)
        stats.report()
    elif args.seconds:
        _set_title("采集结束（%d 帧）" % frames)
    else:
        _set_title("ACC 物理页")


if __name__ == "__main__":
    main()
