/*
 * xinput1_3.dll 代理（60 多行，只做一件事）
 *
 * 目的：ACC 每一帧都会调 XInputSetState 把震动写到手柄上，
 *       顺手把四个马达（包括两个扳机）一起清零，导致我们自己发的扳机震动被冲掉。
 *
 * 做法：把这个 DLL 放到 AC2\Binaries\Win64\ 下，游戏会优先加载它。
 *       读手柄的调用（GetState / GetCapabilities / GetKeystroke ...）原样转发给系统的真 DLL，
 *       只有写震动的 XInputSetState 直接返回成功、不往下传。
 *
 *       结果：ACC 以为自己震了，实际一个字节都没发出去。读输入完全不受影响。
 *
 * 卸载：删掉这个文件（或改名），立刻恢复原生震动。不改注册表、不装驱动、不动任何原有文件。
 *
 * 编译（需要 MinGW-w64 的 gcc）：
 *   gcc -shared -O2 -static -o xinput1_3.dll proxy.c proxy.def
 */

#include <windows.h>

static HMODULE g_real = NULL;

/* 加载系统目录里的真 DLL。必须用完整路径，否则会加载到自己，无限递归。 */
static void load_real(void)
{
    if (g_real) return;

    WCHAR path[MAX_PATH];
    UINT n = GetSystemDirectoryW(path, MAX_PATH);
    if (n == 0 || n >= MAX_PATH - 32) return;

    lstrcatW(path, L"\\xinput1_3.dll");
    g_real = LoadLibraryW(path);
}

static FARPROC real_proc(const char *name)
{
    load_real();
    if (!g_real) return NULL;
    return GetProcAddress(g_real, name);
}

/* ── 读输入：全部原样转发 ─────────────────────────────────────── */

void WINAPI XInputEnable(BOOL enable)
{
    typedef void (WINAPI *FN)(BOOL);
    FN f = (FN)real_proc("XInputEnable");
    if (f) f(enable);
}

DWORD WINAPI XInputGetState(DWORD index, void *state)
{
    typedef DWORD (WINAPI *FN)(DWORD, void *);
    FN f = (FN)real_proc("XInputGetState");
    return f ? f(index, state) : ERROR_DEVICE_NOT_CONNECTED;
}

DWORD WINAPI XInputGetCapabilities(DWORD index, DWORD flags, void *caps)
{
    typedef DWORD (WINAPI *FN)(DWORD, DWORD, void *);
    FN f = (FN)real_proc("XInputGetCapabilities");
    return f ? f(index, flags, caps) : ERROR_DEVICE_NOT_CONNECTED;
}

DWORD WINAPI XInputGetKeystroke(DWORD index, DWORD reserved, void *keystroke)
{
    typedef DWORD (WINAPI *FN)(DWORD, DWORD, void *);
    FN f = (FN)real_proc("XInputGetKeystroke");
    return f ? f(index, reserved, keystroke) : ERROR_DEVICE_NOT_CONNECTED;
}

DWORD WINAPI XInputGetBatteryInformation(DWORD index, BYTE dev_type, void *battery)
{
    typedef DWORD (WINAPI *FN)(DWORD, BYTE, void *);
    FN f = (FN)real_proc("XInputGetBatteryInformation");
    return f ? f(index, dev_type, battery) : ERROR_DEVICE_NOT_CONNECTED;
}

DWORD WINAPI XInputGetDSoundAudioDeviceGuids(DWORD index, void *render, void *capture)
{
    typedef DWORD (WINAPI *FN)(DWORD, void *, void *);
    FN f = (FN)real_proc("XInputGetDSoundAudioDeviceGuids");
    return f ? f(index, render, capture) : ERROR_DEVICE_NOT_CONNECTED;
}

/* ── 写震动：只这一个被吃掉 ──────────────────────────────────── */

DWORD WINAPI XInputSetState(DWORD index, void *vibration)
{
    (void)index;
    (void)vibration;
    return ERROR_SUCCESS;   /* 假装写成功了，实际不发 */
}
