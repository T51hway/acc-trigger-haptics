# -*- coding: utf-8 -*-
"""马达驱动层：把四个马达的强度稳稳地写进 Xbox 手柄。

这一层只管"写"，不管"该不该震"：
    输入：四个 0.0~1.0 的强度（左握把、右握把、左扳机、右扳机）
    输出：以 60Hz 持续写进手柄的蓝牙 HID 报文
    至于什么时候该震什么，那是 haptics_engine.py 的事。

为什么走蓝牙 HID（依据见 设计说明.md 的硬事实 6）：
    - 老 XInput 只有两个握把，根本够不到扳机
    - Windows.Gaming.Input 本机能写入、读回值也对，但马达不响
    - GameInput 本机连键盘都读不到设备
    - HID 输出报文 0x03（9 字节）实测四个马达全可控，且不受前台焦点限制

报文格式（9 字节，每个马达值 0~100）：
    [0] 0x03   报告 ID
    [1] 0x0F   四个马达全开
    [2] 左扳机
    [3] 右扳机
    [4] 左握把
    [5] 右握把
    [6] 0xFF   固定尾部参数（阶段 0 用的就是这三个字节，语义未完全确认）
    [7] 0x00
    [8] 0xEB

几条不是"优化"而是"必须"的规矩：
    1. 通道能不能用，靠试写判断，绝不查 HidP_GetCaps。
       它只报第一个顶层集合（手柄输入）的能力，震动报文在另一个集合里，
       查出来 OutputReportByteLength = 0，实际却能写——信它会筛掉能用的设备。
    2. 60Hz 持续写，与遥测彻底解耦。
       完全静止在 0 的时候不重复写；只要有一个马达不是 0，就每个节拍都写。
       不做"值不变就少发"的优化：报文尾部带着脉冲参数，多久会失效没确认过，
       而 60Hz 连续写是实测"又稳又满"的那条路，不拿它做实验。
    3. 回零必须立刻发，不等下一个节拍；stop() / 异常退出一定清零。
    4. 蓝牙会掉。断开后每秒重试，连上后自动接着写。
    5. 同一时间只允许一个写入者（两个会互相打断），用命名互斥体挡住。
    6. 点窗口 ×、注销、关机时 atexit 不会跑，用 SetConsoleCtrlHandler 抢一包零。

用法：
    from pad_driver import PadDriver

    driver = PadDriver()
    driver.start()                            # 起一个 60Hz 的写入线程
    driver.set_vibration(left_trigger=0.6)    # 每次调用都是"完整状态"
    driver.stop()                             # 保证清零，可重复调用

    with PadDriver() as driver:
        driver.set_vibration(left_grip=0.2, right_grip=0.2)

    注意 set_vibration 一次写四个值，没写的按 0 算——这跟硬件的行为一致，
    不要分两次调用来分别改两个马达，否则中间会闪一下零。

命令行自检（四个马达依次震一下，用来确认这条链路还通）：
    python src/pad_driver.py
    python src/pad_driver.py --wait 30

只用 Python 标准库。
"""

import argparse
import atexit
import ctypes
import ctypes.wintypes as wintypes
import sys
import threading
import time
import uuid

from i18n import t

# ── 协议常量 ────────────────────────────────────────────────────────
REPORT_ID = 0x03
MASK_ALL_MOTORS = 0x0F
PACKET_TAIL = b"\xFF\x00\xEB"
LEVELS = 100                     # 报文里每个马达是 0~100 的整数

# ── 时序 ────────────────────────────────────────────────────────────
DEFAULT_HZ = 60.0                # 实测 60Hz 就够，120/200 没有更好
RECONNECT_INTERVAL = 1.0         # 断开后多久试一次重连（秒）
STOP_JOIN_TIMEOUT = 2.0          # 等写入线程收尾的上限（秒）

# ── 设备筛选 ────────────────────────────────────────────────────────
XBOX_VENDOR_ID = 0x045E
WIRELESS_ADAPTER_PID = 0x0B00    # 045E:0B00 是无线适配器，不是手柄

MUTEX_NAME = "Local\\acc-trigger-haptics.motor-writer"
ERROR_ALREADY_EXISTS = 183

# 控制台退出事件（SetConsoleCtrlHandler）
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6
CONSOLE_CLEANUP_EVENTS = (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT)

GUID_DEVINTERFACE_HID = "4D1E55B2-F16F-11CF-88CB-001111000030"
DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ_WRITE = 0x03
OPEN_EXISTING = 3
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


# ══════════════════════════════════════════════════════════════════
# Win32 结构与绑定
# ══════════════════════════════════════════════════════════════════
class GUID(ctypes.Structure):
    _fields_ = [("b", ctypes.c_ubyte * 16)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


class SP_DEVICE_INTERFACE_DETAIL_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("DevicePath", ctypes.c_wchar * 512)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", ctypes.c_ulong),
                ("VendorID", ctypes.c_ushort),
                ("ProductID", ctypes.c_ushort),
                ("VersionNumber", ctypes.c_ushort)]


def _guid_of(text):
    guid = GUID()
    ctypes.memmove(ctypes.byref(guid), uuid.UUID(text).bytes_le, 16)
    return guid


_GUID_HID_INTERFACE = _guid_of(GUID_DEVINTERFACE_HID)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.WriteFile.restype = wintypes.BOOL
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.GetCurrentProcess.argtypes = []
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.SetPriorityClass.restype = wintypes.BOOL

_PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
_k32.SetConsoleCtrlHandler.argtypes = [_PHANDLER_ROUTINE, wintypes.BOOL]
_k32.SetConsoleCtrlHandler.restype = wintypes.BOOL

_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                           ctypes.c_void_p, wintypes.DWORD]
_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
    wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
_setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

_hid = ctypes.WinDLL("hid", use_last_error=True)
_hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
_hid.HidD_GetAttributes.restype = wintypes.BOOL


# ══════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════
def _levels(values):
    """四个 0.0~1.0 的浮点 → 报文里的 0~100 整数。越界一律夹住。"""
    out = []
    for value in values:
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        if value < 0.0:
            value = 0.0
        elif value > 1.0:
            value = 1.0
        out.append(int(value * LEVELS + 0.5))
    return tuple(out)


def _packet(levels):
    """levels 的顺序是 (左握把, 右握把, 左扳机, 右扳机)，报文里扳机在前。"""
    left_grip, right_grip, left_trigger, right_trigger = levels
    head = bytes((REPORT_ID, MASK_ALL_MOTORS,
                  left_trigger, right_trigger, left_grip, right_grip))
    return head + PACKET_TAIL


def _open_hid(path, writable):
    access = (GENERIC_READ | GENERIC_WRITE) if writable else 0
    handle = _k32.CreateFileW(path, access, FILE_SHARE_READ_WRITE, None,
                              OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return None
    return handle


def _close_handle(handle):
    try:
        _k32.CloseHandle(handle)
    except Exception:
        pass


def _console_log(text):
    print(text, flush=True)


def raise_process_priority():
    """把自己提到"高于正常"优先级。

    全屏游戏会把后台线程压到很低频，表现出来就是震动断续。
    阶段 0 那条"60Hz 就够"的结论，是在这个优先级下测出来的。
    """
    try:
        if _k32.SetPriorityClass(_k32.GetCurrentProcess(),
                                 ABOVE_NORMAL_PRIORITY_CLASS):
            return True
    except Exception:
        pass
    return False


def _enumerate_hid_paths():
    """列出微软（VID 045E）的 HID 接口，返回 [(设备路径, PID), ...]。

    只按厂商号筛，再排除无线适配器。到底哪一个能发振动，
    交给后面的试写去判断，不在这里猜。
    """
    flags = DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    devinfo = _setupapi.SetupDiGetClassDevsW(
        ctypes.byref(_GUID_HID_INTERFACE), None, None, flags)
    if not devinfo or devinfo == INVALID_HANDLE_VALUE:
        return []

    found = []
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = ctypes.sizeof(interface)
            if not _setupapi.SetupDiEnumDeviceInterfaces(
                    devinfo, None, ctypes.byref(_GUID_HID_INTERFACE),
                    index, ctypes.byref(interface)):
                break
            index += 1

            detail = SP_DEVICE_INTERFACE_DETAIL_DATA()
            detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not _setupapi.SetupDiGetDeviceInterfaceDetailW(
                    devinfo, ctypes.byref(interface), ctypes.byref(detail),
                    ctypes.sizeof(detail), None, None):
                continue

            path = detail.DevicePath
            handle = _open_hid(path, writable=False)   # 只看属性
            if handle is None:
                continue
            try:
                attributes = HIDD_ATTRIBUTES()
                attributes.Size = ctypes.sizeof(attributes)
                if not _hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
                    continue
                if attributes.VendorID != XBOX_VENDOR_ID:
                    continue
                if attributes.ProductID == WIRELESS_ADAPTER_PID:
                    continue
                found.append((path, attributes.ProductID))
            finally:
                _close_handle(handle)
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(devinfo)
    return found


# ══════════════════════════════════════════════════════════════════
# 异常
# ══════════════════════════════════════════════════════════════════
class PadError(RuntimeError):
    """马达层能说清楚的错误。"""


class PadBusy(PadError):
    """已经有一个写入者在跑。"""


# ══════════════════════════════════════════════════════════════════
# 驱动
# ══════════════════════════════════════════════════════════════════
class PadDriver:
    """60Hz 的后台写入线程 + 一条蓝牙 HID 通道。

    线程安全：set_vibration() 可以从任何线程调用，最新一次的值会覆盖上一次
    （这是有意的——马达只关心"现在该多强"，不关心历史）。

    典型生命周期：start() → 反复 set_vibration() → stop()。
    stop() 一定会清零，而且可以重复调用；进程正常退出时由 atexit 兜底。
    """

    def __init__(self, hz=DEFAULT_HZ, log=_console_log):
        hz = float(hz)
        if hz <= 0:
            raise ValueError("hz 必须大于 0")
        self._hz = hz
        self._interval = 1.0 / hz
        self._log = log

        self._handle = None
        self._path = None
        self._product_id = None
        self._mutex = None

        self._target = (0.0, 0.0, 0.0, 0.0)
        self._last_sent = None          # 上次成功写出的整数档位
        self._last_send_at = 0.0

        self._cond = threading.Condition()
        self._stopping = False
        self._wake = False              # 回零时把写入线程提前叫醒
        self._thread = None
        self._next_connect_at = 0.0
        self._next_error_report = 0.0
        self._atexit_registered = False
        self._ctrl_handler = None

    # ── 对外接口 ──────────────────────────────────────────────────
    def start(self, wait=0.0):
        """起写入线程。

        wait：启动阶段等手柄出现的秒数。0 表示只试一次，
        之后交给后台每秒重连——所以"先开程序、再开手柄"也能用。
        """
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = None
            self._stopping = False
            self._wake = False
            self._next_connect_at = 0.0

        self._acquire_instance_lock()
        try:
            if not raise_process_priority():
                self._say(t("pad_priority"))

            if wait and wait > 0:
                deadline = time.perf_counter() + wait
                announced = False
                while not self._connect():
                    if time.perf_counter() >= deadline:
                        break
                    if not announced:
                        self._say(t("pad_wait_device"))
                        announced = True
                    time.sleep(0.5)
            else:
                self._connect()

            if self._handle is None:
                self._say(t("pad_no_device", RECONNECT_INTERVAL))

            thread = threading.Thread(target=self._writer_loop,
                                      name="pad-writer", daemon=True)
            self._thread = thread
            thread.start()
            if not self._atexit_registered:
                atexit.register(self.stop)
                self._atexit_registered = True
            self._install_close_handler()
        except BaseException:
            self._release_instance_lock()
            raise

    def set_vibration(self, left_grip=0.0, right_grip=0.0,
                      left_trigger=0.0, right_trigger=0.0):
        """设置四个马达的目标强度（0.0~1.0）。

        每次调用都是完整状态：没传的参数按 0 处理。这跟硬件的写入方式一致
        （一次报文写四个值），分两次改会闪一下零。
        """
        values = (left_grip, right_grip, left_trigger, right_trigger)
        with self._cond:
            was_active = any(self._target)
            self._target = values
            # 从"在震"回到"全零"：立刻叫醒写入线程，不等下一个节拍。
            if was_active and not any(values):
                self._wake = True
                self._cond.notify()

    def stop(self):
        """停线程、清零、释放设备和单实例锁。可以重复调用。"""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()

        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_JOIN_TIMEOUT)
            if thread.is_alive():
                self._say(t("pad_stop_slow"))

        # 不管线程是怎么没的，最后自己清一次零。
        if self._handle is not None:
            try:
                self._write_levels((0, 0, 0, 0))
            except Exception:
                pass
            _close_handle(self._handle)
            self._handle = None
        self._last_sent = None
        with self._cond:
            self._target = (0.0, 0.0, 0.0, 0.0)
        self._uninstall_close_handler()
        self._release_instance_lock()

        if self._atexit_registered:
            try:
                atexit.unregister(self.stop)
            except Exception:
                pass
            self._atexit_registered = False

    def status(self):
        """给界面/日志看的一小块状态。"""
        with self._cond:
            target = self._target
            last_sent = self._last_sent
        return {
            "connected": self._handle is not None,
            "channel": "蓝牙HID 0x03" if self._handle is not None else None,
            "product_id": self._product_id,
            "hz": self._hz,
            "target": target,
            "last_sent": last_sent,
        }

    @property
    def connected(self):
        return self._handle is not None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    # ── 写入线程 ──────────────────────────────────────────────────
    def _writer_loop(self):
        next_tick = time.perf_counter()
        while True:
            with self._cond:
                while not self._stopping:
                    remaining = next_tick - time.perf_counter()
                    if remaining <= 0.0 or self._wake:
                        break
                    self._cond.wait(remaining)
                if self._stopping:
                    return
                self._wake = False
                target = self._target

            try:
                self._tick(target)
            except Exception as exc:
                now = time.perf_counter()
                if now >= self._next_error_report:
                    self._next_error_report = now + 5.0
                    self._say(t("pad_loop_error", type(exc).__name__, exc))

            next_tick += self._interval
            now = time.perf_counter()
            if next_tick < now - self._interval:
                next_tick = now       # 落后太多就不追帧，从现在重新排

    def _tick(self, target):
        if self._handle is None:
            now = time.perf_counter()
            if now < self._next_connect_at:
                return
            self._next_connect_at = now + RECONNECT_INTERVAL
            if not self._connect():
                return

        levels = _levels(target)
        # 完全静止在 0 就没什么可写的；只要不是全零，每个节拍都写。
        if levels == (0, 0, 0, 0) and self._last_sent == (0, 0, 0, 0):
            return
        if not self._write_levels(levels):
            self._drop_link(t("pad_write_fail", ctypes.get_last_error()))
            return
        self._last_sent = levels
        self._last_send_at = time.perf_counter()

    # ── 连接维护 ──────────────────────────────────────────────────
    def _connect(self):
        """枚举 → 打开 → 试写全零报文。写进去了才算这条通道可用。"""
        try:
            candidates = _enumerate_hid_paths()
        except Exception as exc:
            self._say(t("pad_enum_error", exc))
            return False

        for path, product_id in candidates:
            handle = _open_hid(path, writable=True)
            if handle is None:
                continue
            self._handle = handle
            # 试写。不能靠 HidP_GetCaps：它只报第一个集合，会假阴性。
            try:
                writable = self._write_levels((0, 0, 0, 0))
            except Exception:
                writable = False
            if writable:
                self._path = path
                self._product_id = product_id
                self._last_sent = (0, 0, 0, 0)
                self._say(t("pad_connected", product_id))
                return True
            _close_handle(handle)
            self._handle = None
        return False

    def _drop_link(self, reason):
        handle, self._handle = self._handle, None
        self._last_sent = None
        if handle is not None:
            _close_handle(handle)
        self._say(t("pad_dropped", reason, RECONNECT_INTERVAL))

    def _write_levels(self, levels):
        packet = _packet(levels)
        written = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        ok = _k32.WriteFile(self._handle, packet, len(packet),
                            ctypes.byref(written), None)
        return bool(ok) and written.value == len(packet)

    # ── 单实例 ────────────────────────────────────────────────────
    def _acquire_instance_lock(self):
        if self._mutex is not None:
            return
        ctypes.set_last_error(0)
        handle = _k32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle or handle == INVALID_HANDLE_VALUE:
            raise PadError(t("pad_lock_fail", ctypes.get_last_error()))
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            _close_handle(handle)
            raise PadBusy(t("pad_busy"))
        self._mutex = handle

    def _release_instance_lock(self):
        if self._mutex is not None:
            _close_handle(self._mutex)
            self._mutex = None

    # ── 控制台退出 ────────────────────────────────────────────────
    def _install_close_handler(self):
        """点窗口 ×、注销、关机时，atexit 不会跑，这里抢一包零。

        注册失败不影响主功能（例如 pythonw 启动时没有控制台）。
        """
        if self._ctrl_handler is not None:
            return
        try:
            self._ctrl_handler = _PHANDLER_ROUTINE(self._on_console_event)
            if not _k32.SetConsoleCtrlHandler(self._ctrl_handler, True):
                self._ctrl_handler = None
        except Exception:
            self._ctrl_handler = None

    def _uninstall_close_handler(self):
        if self._ctrl_handler is None:
            return
        try:
            _k32.SetConsoleCtrlHandler(self._ctrl_handler, False)
        except Exception:
            pass
        self._ctrl_handler = None

    def _on_console_event(self, event):
        """系统线程里跑：只做最少的事，别跟主线程抢着收尾。"""
        if event not in CONSOLE_CLEANUP_EVENTS:
            return False
        # 先叫停写入循环，等可能正在写的那一拍走完，再补一包零。
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        time.sleep(0.05)
        if self._handle is not None:
            try:
                self._write_levels((0, 0, 0, 0))
            except Exception:
                pass
        return False

    # ── 日志 ──────────────────────────────────────────────────────
    def _say(self, text):
        if self._log is None:
            return
        try:
            self._log(t("log_prefix_pad", time.strftime("%H:%M:%S"), text))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# 命令行自检
# ══════════════════════════════════════════════════════════════════
def _demo(driver, hold=0.6, gap=0.35, strength=0.7):
    steps = [
        ("左握把", dict(left_grip=strength)),
        ("右握把", dict(right_grip=strength)),
        ("左扳机", dict(left_trigger=strength)),
        ("右扳机", dict(right_trigger=strength)),
    ]
    for index, (label, kwargs) in enumerate(steps, 1):
        print()
        print("第 %d/4 步  %s  强度 %.1f  %.1f 秒"
              % (index, label, strength, hold))
        driver.set_vibration(**kwargs)
        time.sleep(hold)
        driver.set_vibration()
        time.sleep(gap)


def main():
    parser = argparse.ArgumentParser(description="马达驱动层自检：四个马达依次震一下")
    parser.add_argument("--wait", type=float, default=20.0,
                        help="等手柄出现的秒数，默认 20")
    parser.add_argument("--hz", type=float, default=DEFAULT_HZ,
                        help="写入频率，默认 60（实测 60 就够，调高没有收益）")
    args = parser.parse_args()

    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("马达驱动层自检（src/pad_driver.py）")
    print("=" * 62)
    print("开始前请确认：")
    print("  1. 手柄已开机，蓝牙连上（这台机器只验证过蓝牙）")
    print("  2. Steam 输入已关闭，否则指令会被 Steam 截走")
    print("  3. 这一步不用开 ACC，只测“能不能震”")
    print()

    driver = PadDriver(hz=args.hz)
    try:
        driver.start(wait=args.wait)
    except PadBusy as exc:
        print()
        print(exc)
        return 1
    except PadError as exc:
        print()
        print("启动失败：%s" % exc)
        return 1

    try:
        if not driver.connected:
            print()
            print("等了 %.0f 秒还是没看到手柄，先不测了。" % args.wait)
            print("  - 按一下手柄中间的圆按钮，看灯是不是常亮")
            print("  - Windows 设置 → 蓝牙和其他设备，确认手柄在“已连接”里")
            print("  - 有线连接目前不支持：这条报文只对蓝牙有效")
            return 1
        _demo(driver)
    except KeyboardInterrupt:
        print()
        print("被中断，下面先清零再退出。")
    finally:
        driver.stop()

    print()
    print("=" * 62)
    print("判断标准")
    print("=" * 62)
    print("  第 1 步 左握把 → 只有左手在震")
    print("  第 2 步 右握把 → 只有右手在震")
    print("  第 3 步 左扳机 → 手指搭在左扳机上有细碎的麻感")
    print("  第 4 步 右扳机 → 同上，右边")
    print()
    print("扳机马达很小，是“麻”不是“晃”，手指要主动搭上去。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
