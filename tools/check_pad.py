# -*- coding: utf-8 -*-
"""阶段 0 验证脚本 ②：四个马达能不能一个个震起来。

依次驱动：左握把 → 右握把 → 左扳机 → 右扳机
每步满强度 0.6 秒，中间静音 0.4 秒，并打印这一次走的是哪条驱动通道。

为什么要分通道：
    老的 XInput 只能控制两个握把，够不到扳机。想驱动扳机必须走
    GameInput / Windows.Gaming.Input / 蓝牙 HID 三者之一。
    本脚本按这个顺序试，哪个能用就用哪个：
      GameInput  → 四马达，一次写四个值
      HID 0x03   → 四马达，只在蓝牙连接下有效（直接往手柄发报文）
      XInput     → 只有两个握把，够不到扳机
    三条都试完还不行，才算手柄有问题。

用法：
    python tools/check_pad.py
    python tools/check_pad.py --pad 0     # 指定第几号手柄（默认 0）

只用 Python 标准库。
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import sys
import time

XINPUT_CAPS_WIRELESS = 0x0002
ERROR_DEVICE_NOT_CONNECTED = 1167

GameInputKindGamepad = 0x00040000

STRENGTH = 1.0     # 满强度
HOLD = 0.6         # 每步持续时间（秒）
GAP = 0.4          # 步间静音（秒）


# ══════════════════════════════════════════════════════════════════
# 通道 A：XInput —— 只能控制两个握把
# ══════════════════════════════════════════════════════════════════
class XINPUT_GAMEPAD_STRUCT(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", wintypes.WORD),
                ("wRightMotorSpeed", wintypes.WORD)]


class XINPUT_CAPABILITIES(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_ubyte),
                ("SubType", ctypes.c_ubyte),
                ("Flags", wintypes.WORD),
                ("Gamepad", XINPUT_GAMEPAD_STRUCT),
                ("Vibration", XINPUT_VIBRATION)]


class XInputChannel:
    name = "XInput"
    detail = "XInputSetState，只有两个握把通道"
    can_drive_triggers = False

    def __init__(self, lib, slot):
        self.lib = lib
        self.slot = slot

    def open(self):
        return True

    def run_motors(self, left_grip=0.0, right_grip=0.0,
                   left_trigger=0.0, right_trigger=0.0):
        """扳机参数在这里是被忽略的 —— 这就是老接口的天花板。"""
        v = XINPUT_VIBRATION(int(max(0.0, min(1.0, left_grip)) * 65535),
                             int(max(0.0, min(1.0, right_grip)) * 65535))
        ret = self.lib.XInputSetState(self.slot, ctypes.byref(v))
        return ret == 0

    def close(self):
        self.run_motors()


# ══════════════════════════════════════════════════════════════════
# 通道 B：Windows.Gaming.Input —— 四马达，微软另一套接口
# ══════════════════════════════════════════════════════════════════
class GAMEPAD_VIBRATION(ctypes.Structure):
    _fields_ = [("LeftMotor", ctypes.c_double),
                ("RightMotor", ctypes.c_double),
                ("LeftTrigger", ctypes.c_double),
                ("RightTrigger", ctypes.c_double)]


IID_IGAMEPAD_STATICS = "8BBCE529-D49C-39E9-9560-E47DDE96B7C8"

# 各接口的虚表槽位（据官方 ABI 头文件 windows.gaming.input.h 核对）
IDX_STATICS_GET_GAMEPADS = 10   # IGamepadStatics：前四个是事件方法，不是 get_Gamepads
IDX_VIEW_GET_AT = 6             # IVectorView<T>::GetAt
IDX_VIEW_SIZE = 7               # IVectorView<T>::get_Size
IDX_PAD_GET_VIBRATION = 6       # IGamepad::get_Vibration
IDX_PAD_PUT_VIBRATION = 7       # IGamepad::put_Vibration


class WgiChannel:
    name = "WGI"
    detail = "Windows.Gaming.Input，四马达（仅前台有效）"
    can_drive_triggers = True

    def __init__(self):
        self.pad = None
        self.statics = None

    def open(self):
        try:
            combase = ctypes.WinDLL("combase")
        except OSError:
            return "加载 combase 失败，这不像正常的 Windows。"
        combase.RoInitialize.argtypes = [ctypes.c_int]
        combase.RoInitialize.restype = ctypes.c_long
        combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, wintypes.UINT,
                                                ctypes.POINTER(ctypes.c_void_p)]
        combase.WindowsCreateString.restype = ctypes.c_long
        combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID),
                                                   ctypes.POINTER(ctypes.c_void_p)]
        combase.RoGetActivationFactory.restype = ctypes.c_long

        hr = combase.RoInitialize(0)          # 多线程单元
        if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:   # 已初始化过就不算错
            return "RoInitialize 失败（0x%08X）。" % (hr & 0xFFFFFFFF)

        name = "Windows.Gaming.Input.Gamepad"
        hstr = ctypes.c_void_p()
        if combase.WindowsCreateString(name, len(name), ctypes.byref(hstr)) < 0:
            return "创建类名字符串失败。"

        iid = guid_of(IID_IGAMEPAD_STATICS)
        statics = ctypes.c_void_p()
        hr = combase.RoGetActivationFactory(hstr, ctypes.byref(iid), ctypes.byref(statics))
        if hr < 0 or not statics:
            return "拿不到 Gamepad 接口（0x%08X）。" % (hr & 0xFFFFFFFF)
        self.statics = statics

        view = ctypes.c_void_p()
        hr = vtable(statics, IDX_STATICS_GET_GAMEPADS, ctypes.c_long,
                    ctypes.POINTER(ctypes.c_void_p))(statics, ctypes.byref(view))
        if hr < 0 or not view:
            return "取手柄列表失败（0x%08X）。" % (hr & 0xFFFFFFFF)

        size = ctypes.c_uint32()
        vtable(view, IDX_VIEW_SIZE, ctypes.c_long,
               ctypes.POINTER(ctypes.c_uint32))(view, ctypes.byref(size))
        if size.value == 0:
            return ("WGI 接口正常，但手柄列表这一刻是空的。"
                    "刚启动时常见，稍等一会儿会自己出现，按一下手柄也能催它。")

        pad = ctypes.c_void_p()
        hr = vtable(view, IDX_VIEW_GET_AT, ctypes.c_long, ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_void_p))(view, 0, ctypes.byref(pad))
        if hr < 0 or not pad:
            return "手柄列表里有 %d 个，但取不出来（0x%08X）。" % (size.value, hr & 0xFFFFFFFF)
        self.pad = pad
        return None

    def run_motors(self, left_grip=0.0, right_grip=0.0,
                   left_trigger=0.0, right_trigger=0.0):
        vib = GAMEPAD_VIBRATION(float(left_grip), float(right_grip),
                                float(left_trigger), float(right_trigger))
        hr = vtable(self.pad, IDX_PAD_PUT_VIBRATION, ctypes.c_long,
                    GAMEPAD_VIBRATION)(self.pad, vib)
        return hr >= 0

    def close(self):
        if self.pad:
            try:
                self.run_motors()
            except Exception:
                pass
            try:
                vtable(self.pad, IDX_RELEASE, ctypes.c_ulong)(self.pad)
            except Exception:
                pass
            self.pad = None


# ══════════════════════════════════════════════════════════════════
# 通道 C：GameInput —— 四马达
# ══════════════════════════════════════════════════════════════════
class GAMEINPUT_RUMBLE(ctypes.Structure):
    _fields_ = [("lowFrequency", ctypes.c_float),     # 左握把（大马达，低频）
                ("highFrequency", ctypes.c_float),    # 右握把（小马达，高频）
                ("leftTrigger", ctypes.c_float),
                ("rightTrigger", ctypes.c_float)]


def vtable(obj, index, restype, *argtypes):
    """按序号取 COM 接口的虚函数。obj 是指向接口的地址。"""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    addr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(addr)


# 官方头文件 GameInput.h 里的顺序（IUnknown 占 0~2）
#
# ⚠ 这一套是「旧版（v0）接口」的排法。微软后来把接口重做了一版（v4），
#   IGameInputDevice 变成另一个 IID，SetRumbleState 挪到了第 44 位。
#   同一个 GameInput.dll 会同时提供两套，用 QueryInterface 选。
#   所以下面这几步必须先主动索取 v0，否则槽位全错（表现是直接闪退）。
IDX_RELEASE = 2
IDX_GI_TIMESTAMP = 3          # IGameInput::GetCurrentTimestamp
IDX_GI_CURRENT_READING = 4    # IGameInput::GetCurrentReading
IDX_READING_GET_DEVICE = 6    # IGameInputReading::GetDevice
IDX_DEVICE_SET_RUMBLE = 10    # IGameInputDevice::SetRumbleState

IID_IGAMEINPUT_V0 = "11BE2A7E-4254-445A-9C09-FFC40F006918"


class GUID(ctypes.Structure):
    _fields_ = [("b", ctypes.c_ubyte * 16)]


def guid_of(text):
    import uuid
    g = GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
    return g


class GameInputChannel:
    name = "GameInput"
    detail = "IGameInputDevice::SetRumbleState，四马达"
    can_drive_triggers = True

    def __init__(self):
        self.lib = None
        self.gi = None
        self.device = None
        self.reading = None

    def open(self):
        # 系统自带的不行就试可再发行版
        last = None
        for dll in ("GameInput.dll", "GameInputRedist.dll"):
            try:
                self.lib = ctypes.WinDLL(dll)
                break
            except OSError as exc:
                last = exc
        if self.lib is None:
            return "两个 GameInput 动态库都加载不了（%s）。系统可能太旧，缺 GameInput 组件。" % last

        try:
            create = ctypes.WINFUNCTYPE(ctypes.c_long,
                                        ctypes.POINTER(ctypes.c_void_p))(
                ("GameInputCreate", self.lib))
        except OSError:
            return "动态库里找不到 GameInputCreate 导出函数，装的可能是残缺版本。"

        gi = ctypes.c_void_p()
        hr = create(ctypes.byref(gi))
        if hr < 0 or not gi:
            return "GameInputCreate 调用失败（HRESULT 0x%08X）。" % (hr & 0xFFFFFFFF)

        # 主动索取旧版接口。拿不到就别猜槽位，直接说清楚。
        qi = vtable(gi, 0, ctypes.c_long, ctypes.POINTER(GUID),
                    ctypes.POINTER(ctypes.c_void_p))
        iid = guid_of(IID_IGAMEINPUT_V0)
        gi_v0 = ctypes.c_void_p()
        hr = qi(gi, ctypes.byref(iid), ctypes.byref(gi_v0))
        if hr != 0 or not gi_v0:
            vtable(gi, IDX_RELEASE, ctypes.c_ulong)(gi)
            return ("这个 GameInput 不提供旧版（v0）接口，HRESULT 0x%08X。"
                    "本脚本按旧版槽位写的，换用新版需要改实现，现有代码不能直接跑。"
                    % (hr & 0xFFFFFFFF))
        vtable(gi, IDX_RELEASE, ctypes.c_ulong)(gi)
        self.gi = gi_v0

        # 自检：时间戳是「1601 年起的微秒数」，看绝对值得不出结论，
        # 要看两次读数之间涨了多少。涨得跟睡眠时长对得上，才说明接口地址没错。
        # 地址错的话这里读到的会是野值，宁可停下，也不要带着错误的虚表去调震动。
        get_ts = vtable(self.gi, IDX_GI_TIMESTAMP, ctypes.c_uint64)
        t1 = get_ts(self.gi)
        time.sleep(0.02)
        t2 = get_ts(self.gi)
        delta = t2 - t1
        if not (1000 <= delta <= 5000000):
            return ("GameInput 接口自检没过：20 毫秒内时间戳涨了 %d（正常应接近 20000）。"
                    % delta)

        # 拿一个手柄读数
        get_reading = vtable(self.gi, IDX_GI_CURRENT_READING, ctypes.c_long,
                             ctypes.c_int, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_void_p))
        reading = ctypes.c_void_p()
        hr = get_reading(self.gi, GameInputKindGamepad, None, ctypes.byref(reading))
        if hr < 0 or not reading:
            # 先分清是哪一种情况：手柄没被认到，还是 GameInput 压根没在工作。
            # 用键盘做对照——如果连键盘都读不到，那跟手柄没关系。
            probe = ctypes.c_void_p()
            get_reading(self.gi, 0x10, None, ctypes.byref(probe))   # Keyboard
            any_device = bool(probe)
            if probe:
                vtable(probe, IDX_RELEASE, ctypes.c_ulong)(probe)
            if not any_device:
                return ("GameInput 一个设备都没分发出来（连键盘鼠标都读不到），"
                        "所以这不是手柄的问题。常见原因：GameInputSvc 服务没启动"
                        "（手动启动需要管理员权限），或者当前程序运行的环境不完整。")
            return ("GameInput 通道本身正常（自检已过），但没有拿到手柄读数"
                    "（HRESULT 0x%08X）。它能读到键盘却读不到手柄，"
                    "说明设备被别的程序抓走了（Steam 输入最常见），"
                    "或者蓝牙连接下没被识别。"
                    % (hr & 0xFFFFFFFF))
        self.reading = reading

        get_device = vtable(reading, IDX_READING_GET_DEVICE, None,
                            ctypes.POINTER(ctypes.c_void_p))
        device = ctypes.c_void_p()
        get_device(reading, ctypes.byref(device))
        if not device:
            return "拿到读数了，但取设备句柄为空。"
        self.device = device
        return None

    def run_motors(self, left_grip=0.0, right_grip=0.0,
                   left_trigger=0.0, right_trigger=0.0):
        params = GAMEINPUT_RUMBLE(float(left_grip), float(right_grip),
                                  float(left_trigger), float(right_trigger))
        vtable(self.device, IDX_DEVICE_SET_RUMBLE, None,
               ctypes.POINTER(GAMEINPUT_RUMBLE))(self.device, ctypes.byref(params))
        return True

    def close(self):
        try:
            if self.device:
                self.run_motors()
        except Exception:
            pass
        for obj in (self.device, self.reading, self.gi):
            if obj:
                try:
                    vtable(obj, IDX_RELEASE, ctypes.c_ulong)(obj)
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════
# 通道 C：蓝牙 HID 报文 —— 四马达，只在蓝牙连接下有效
# ══════════════════════════════════════════════════════════════════
# 报文格式（9 字节），来自 Xbox 蓝牙手柄的 HID 报告描述符：
#   [0] 0x03          报告 ID
#   [1] 0x0F          启用四个执行器的掩码
#   [2] 左扳机        0~100
#   [3] 右扳机        0~100
#   [4] 左握把（低频）
#   [5] 右握把（高频）
#   [6] 0xFF          持续时间参数
#   [7] 0x00
#   [8] 0xEB
# 这是公开的系统协议格式，按描述符自行实现。
HID_REPORT_ID = 0x03
HID_ENABLE_ALL_MOTORS = 0x0F


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


def hid_candidates():
    """列出所有微软（VID 045E）的 HID 接口路径。"""
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    guid = guid_of("4D1E55B2-F16F-11CF-88CB-001111000030")  # GUID_DEVINTERFACE_HID

    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                              ctypes.c_void_p, wintypes.DWORD]
    flags = 0x02 | 0x10  # PRESENT | DEVICEINTERFACE
    devinfo = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, flags)
    if devinfo in (None, ctypes.c_void_p(-1).value):
        return []

    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
        wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

    out = []
    index = 0
    while True:
        did = SP_DEVICE_INTERFACE_DATA()
        did.cbSize = ctypes.sizeof(did)
        if not setupapi.SetupDiEnumDeviceInterfaces(devinfo, None, ctypes.byref(guid),
                                                    index, ctypes.byref(did)):
            break
        index += 1

        detail = SP_DEVICE_INTERFACE_DETAIL_DATA()
        detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                devinfo, ctypes.byref(did), ctypes.byref(detail),
                ctypes.sizeof(detail), None, None):
            continue
        path = detail.DevicePath

        # 打开看看是谁，只看不写
        handle = ctypes.windll.kernel32.CreateFileW(
            path, 0, 0x03, None, 3, 0, None)   # 0 访问权限：只查属性
        if handle in (None, ctypes.c_void_p(-1).value):
            continue
        try:
            attrs = HIDD_ATTRIBUTES()
            attrs.Size = ctypes.sizeof(attrs)
            hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)]
            if not hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
                continue
            if attrs.VendorID != 0x045E:       # 只认微软（Xbox）设备
                continue
            if attrs.ProductID == 0x0B00:      # 045E:0B00 是 Xbox 无线适配器，不是手柄
                continue
            out.append((path, attrs.ProductID))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    setupapi.SetupDiDestroyDeviceInfoList(ctypes.c_void_p(devinfo))
    return out


class HidChannel:
    name = "蓝牙HID"
    detail = "HID 输出报文 0x03（9 字节，四马达）"
    can_drive_triggers = True

    def __init__(self):
        self.handle = None
        self.path = None
        self.product_id = None
        self._last_vals = None
        self._last_send = 0.0

    def open(self):
        try:
            cands = hid_candidates()
        except Exception as exc:
            return "枚举 HID 设备时出错：%s" % exc
        if not cands:
            return ("没找到微软手柄的 HID 接口。若手柄是用 USB 线连着，"
                    "这条通道不适用（它只在蓝牙连接下有效）。")

        k32 = ctypes.windll.kernel32
        tried = []
        for path, pid in cands:
            handle = k32.CreateFileW(path, 0x80000000 | 0x40000000, 0x03, None, 3, 0, None)
            if not handle or handle == ctypes.c_void_p(-1).value:
                tried.append("PID %04X 打不开" % pid)
                continue

            # 直接发一个全零报文来试探，能写进去才算这条通道能用。
            # 不能靠 HidP_GetCaps 判断：它只报第一个集合（手柄输入）的能力，
            # 震动报文在同一个设备的另一个集合里，查出来是 0 但实际能写。
            self.handle = handle
            if self._send(0, 0, 0, 0):
                self.path = path
                self.product_id = pid
                self._last_vals = None
                self._last_send = 0.0
                return None
            k32.CloseHandle(handle)
            self.handle = None
            tried.append("PID %04X 写不进去" % pid)

        return ("找到微软设备，但都发不出震动报文（%s）。"
                "有线连接时这属于正常（有线要用另一种报文格式）。"
                % "；".join(tried[:4]))

    def _send(self, left_grip, right_grip, left_trigger, right_trigger,
              mask=HID_ENABLE_ALL_MOTORS):
        """mask 是四个马达的使能位：0x0F 全开，0x0C 只开两个扳机。"""
        def level(x):
            return int(max(0.0, min(1.0, float(x))) * 100)
        packet = bytes([HID_REPORT_ID, mask,
                        level(left_trigger), level(right_trigger),
                        level(left_grip), level(right_grip),
                        0xFF, 0x00, 0xEB])
        written = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.WriteFile(
            self.handle, packet, len(packet), ctypes.byref(written), None)
        return bool(ok) and written.value == len(packet)

    def run_motors(self, left_grip=0.0, right_grip=0.0,
                   left_trigger=0.0, right_trigger=0.0):
        """只在值真的变化时才发报文。

        原因：这个报文里带着脉冲参数，每发一次等于重新起一个脉冲，
        发得太密会让马达被打断，听感上功率不足。所以：
          - 值没变 → 不发（最多每 0.5 秒补发一次，防止脉冲到期）
          - 值变了 → 发，但最快 20Hz（BLE 撑不住更密）
          - 回零 → 立即发，不受限制（安全相关，绝不能漏）
        """
        vals = (float(left_grip), float(right_grip),
                float(left_trigger), float(right_trigger))
        now = time.perf_counter()

        if self.handle and self._last_vals is not None and any(vals):
            biggest = max(abs(a - b) for a, b in zip(vals, self._last_vals))
            unchanged = biggest < 0.02
            if unchanged and (now - self._last_send) < 0.5:
                return True          # 什么都没变，别发
            if (now - self._last_send) < 0.05:
                return True          # 太密了，等下一轮

        ok = self._send(*vals)
        if ok:
            self._last_vals = vals
            self._last_send = now
        return ok

    def close(self):
        if self.handle:
            try:
                self._send(0, 0, 0, 0)
            except Exception:
                pass
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
            self._last_vals = None


# ══════════════════════════════════════════════════════════════════
# 探测与测试流程
# ══════════════════════════════════════════════════════════════════
def probe_xinput(slot):
    """先确认手柄到底在不在 —— 这一层过了，后面失败才好定位。"""
    for dll in ("xinput1_4", "xinput1_3", "xinput1_2", "xinput9_1_0"):
        try:
            lib = ctypes.WinDLL(dll)
            break
        except OSError:
            lib = None
    if lib is None:
        return None, None, "系统里找不到任何 XInput 动态库，这不正常。"

    lib.XInputGetState.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    lib.XInputGetState.restype = wintypes.DWORD
    lib.XInputSetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_VIBRATION)]
    lib.XInputSetState.restype = wintypes.DWORD

    state = (ctypes.c_ubyte * 16)()
    ret = lib.XInputGetState(slot, ctypes.byref(state))
    if ret == ERROR_DEVICE_NOT_CONNECTED:
        return lib, None, ("XInput 没看到 %d 号位有手柄。\n"
                           "     请检查：① 线插好没有（USB-C 数据线，不是只能充电的）；\n"
                           "             ② 手柄有没有开机（按一下中间的大圆按钮，灯常亮）；\n"
                           "             ③ 换一个 USB 口试试。" % slot)
    if ret != 0:
        return lib, None, "XInputGetState 返回错误码 %d。" % ret

    caps = XINPUT_CAPABILITIES()
    lib.XInputGetCapabilities.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                          ctypes.POINTER(XINPUT_CAPABILITIES)]
    lib.XInputGetCapabilities.restype = wintypes.DWORD
    wired = "有线"
    if lib.XInputGetCapabilities(slot, 0, ctypes.byref(caps)) == 0:
        if caps.Flags & XINPUT_CAPS_WIRELESS:
            wired = "无线（蓝牙或专用适配器）"

    return lib, (slot, wired), None


def boost_priority():
    """把自己提到「高于正常」优先级。

    全屏游戏会抢走大量 CPU，后台的写报文循环如果被压到很低频，
    表现出来就是震动断续。
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p   # 不声明会被截成 32 位
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass.restype = ctypes.c_int
        handle = k32.GetCurrentProcess()
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        ok = bool(k32.SetPriorityClass(handle, ABOVE_NORMAL_PRIORITY_CLASS))
        if not ok:
            print("    （优先级设置被拒绝，错误码 %d）" % ctypes.get_last_error())
        return ok
    except Exception as exc:
        print("    （优先级设置失败：%s）" % exc)
        return False


def rate_test(driver):
    """同一个扳机、同样满值，只改发送频率，看断续是不是"写得不够勤"造成的。"""
    if not hasattr(driver, "_send"):
        print("这个通道不支持频率测试（需要 HID 通道）。")
        return

    got = boost_priority()
    print("=" * 62)
    print("发送频率测试：两个扳机满值，依次用不同频率写")
    print("=" * 62)
    print("进程优先级已提升：%s" % ("是" if got else "否（被系统拒绝）"))
    print()
    print("ACC 开着跑这个测试（就在赛道上停着也行）。")
    print("每段 6 秒，中间停 2 秒。注意扳机是「稳」还是「断续」。")
    print()
    print("3 秒后开始。")
    for n in (3, 2, 1):
        print("    %d" % n, end="\r")
        sys.stdout.flush()
        time.sleep(1.0)
    print("                    ")

    try:
        for hz in (60, 120, 200):
            interval = 1.0 / hz
            print()
            print(">>> %d Hz —— 6 秒" % hz)
            sys.stdout.flush()
            end = time.time() + 6.0
            sends = 0
            while time.time() < end:
                driver._send(0.0, 0.0, 1.0, 1.0)
                sends += 1
                time.sleep(interval)
            driver._send(0.0, 0.0, 0.0, 0.0)
            print("    结束（这 6 秒里实际发出 %d 次，目标 %d 次）"
                  % (sends, int(6 * hz)))
            time.sleep(2.0)
    finally:
        try:
            driver._send(0.0, 0.0, 0.0, 0.0)
        except Exception:
            pass

    print()
    print("哪一段最稳？60 / 120 / 200？")


def run_steps(channel):
    """四步走完，或者走到接口的天花板为止。"""
    steps = [
        ("左握把", "left_grip", dict(left_grip=STRENGTH)),
        ("右握把", "right_grip", dict(right_grip=STRENGTH)),
        ("左扳机", "left_trigger", dict(left_trigger=STRENGTH)),
        ("右扳机", "right_trigger", dict(right_trigger=STRENGTH)),
    ]

    for i, (label, motor, args) in enumerate(steps, 1):
        if not channel.can_drive_triggers and "trigger" in motor:
            print()
            print("第 %d/4 步 %s —— 跳过" % (i, label))
            print("    原因：%s 够不到扳机马达，这是接口本身的天花板，不是手柄的问题。"
                  % channel.name)
            continue

        print()
        print("第 %d/4 步  %s   强度 %.1f   持续 %.1f 秒" % (i, label, STRENGTH, HOLD))
        print("    通道：%s（%s）" % (channel.name, channel.detail))
        if channel.name == "GameInput":
            mapped = {"左握把": "lowFrequency", "右握把": "highFrequency",
                      "左扳机": "leftTrigger", "右扳机": "rightTrigger"}[label]
            print("    实际写入：%s = %.1f" % (mapped, STRENGTH))

        ok = channel.run_motors(**args)
        if not ok:
            print("    !! 写入失败，这一步没有生效。")
            continue
        time.sleep(HOLD)
        channel.run_motors()
        time.sleep(GAP)


def main():
    ap = argparse.ArgumentParser(description="验证手柄四个马达能不能分别震")
    ap.add_argument("--pad", type=int, default=0, help="手柄序号，默认 0")
    ap.add_argument("--wait", type=float, default=20.0,
                    help="等手柄被激活的秒数，默认 20（期间按一下手柄即可）")
    ap.add_argument("--rate", action="store_true",
                    help="发送频率测试：60 / 120 / 200 Hz 各 6 秒，看断续")
    args = ap.parse_args()

    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("手柄四马达验证")
    print("=" * 62)
    print("开始前请确认两件事：")
    print("  1. 手柄已开机并连上（蓝牙或有线都行）")
    print("  2. Steam 已经关掉（Steam 设置 → 控制器 → 关闭 Steam 输入）")
    print("     没关的话，震动指令会被 Steam 截走，表现为「代码没反应」。")
    print()

    lib, info, xi_err = probe_xinput(args.pad)
    if xi_err:
        print("XInput 这一层：没看到手柄")
        print("    " + xi_err)
    else:
        slot, wired = info
        print("XInput 这一层：%d 号位有手柄，连接方式 %s。" % (slot, wired))
    print()

    # 三条能驱动扳机的路。按顺序试，并且允许等一会——
    # 手柄闲放着的时候，WGI/GameInput 可能不把它当成可用设备，
    # 按一下才认（参考项目里也有"请按手柄任意键激活"的提示）。
    order = [("① 蓝牙HID", HidChannel),
             ("② WGI", WgiChannel),
             ("③ GameInput", GameInputChannel)]
    deadline = time.time() + max(0.0, args.wait)
    chosen = None
    errors = {}
    attempt = 0

    while True:
        attempt += 1
        errors = {}
        for label, cls in order:
            ch = cls()
            err = ch.open()
            if err is None:
                chosen = (label, ch)
                break
            errors[label] = err
            try:
                ch.close()
            except Exception:
                pass
        if chosen is not None or time.time() >= deadline:
            break
        if attempt == 1:
            print("现在看不到手柄。")
            print(">>> 请按一下手柄上的按键，或推一下左摇杆，我在这里等 <<<")
        print("    等待中…… 还剩 %.0f 秒" % max(0.0, deadline - time.time()))
        sys.stdout.flush()
        time.sleep(1.0)
    print()

    if chosen is not None:
        label, driver = chosen
        print("%s：通道已打开，四个马达都能控。" % label)
        for other, msg in errors.items():
            print("    （%s 没走通：%s）" % (other, msg))
        # 其余通道的实例在 open 失败时已关闭，这里无需再处理
    elif info is not None:
        print("三条路都没走通，退回 XInput，只能验证两个握把。")
        for label, msg in errors.items():
            print("    %s：%s" % (label, msg))
        print()
        driver = XInputChannel(lib, info[0])
        driver.open()
        slot = info[0]
    else:
        print("=" * 62)
        print("三条路都不通，而且 XInput 也看不到手柄，测不了。")
        for label, msg in errors.items():
            print("    %s：%s" % (label, msg))
        print("=" * 62)
        sys.exit(1)
    print()

    # --rate：发送频率测试
    if args.rate:
        try:
            rate_test(driver)
        finally:
            driver.close()
        return

    try:
        run_steps(driver)
    except KeyboardInterrupt:
        print()
        print("被中断，先清零。")
    finally:
        driver.close()

    print()
    print("=" * 62)
    print("怎么判断通过")
    print("=" * 62)
    if driver.can_drive_triggers:
        print("四步都要有感：")
        print("  第 1 步 左手握把震  → 只有左边在动")
        print("  第 2 步 右手握把震  → 只有右边在动")
        print("  第 3 步 左扳机震    → 手指搭在左扳机上能感觉到细碎的高频抖动")
        print("  第 4 步 右扳机震    → 同上，右边")
        print()
        print("扳机马达很小，震感是「麻」而不是「晃」，手指要主动搭上去感受。")
        print("如果第 1、2 步左右手感觉反了，说明 lowFrequency/highFrequency")
        print("和左右握把的对应关系要调换，记下来告诉我。")
    else:
        print("只验证了两个握把（第 1、2 步）。")
        print("扳机没测到 —— 这是 XInput 接口的限制，不代表手柄不能震扳机。")
        print("下一步要先把 GameInput 通道打通，才能验证扳机。")


if __name__ == "__main__":
    main()
