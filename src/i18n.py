# -*- coding: utf-8 -*-
"""界面与日志的双语表。

只做一件事：按当前语言返回一句话。找不到键时把键名原样返回，
这样漏翻的地方会直接暴露出来，而不是崩溃。

语言默认跟随 Windows 显示语言（中文系统 → 中文，其它 → 英文）。
面板会在启动时读出用户上次的选择，再调用 set_language()。

约定：
    - 中文在前，英文在后。
    - 带参数的句子用 % 格式化，两种语言参数顺序必须一致。
    - 面板界面、运行时日志、命令行输出都走这里；
      开发用自检脚本（check_acc / check_pad / --selftest）不翻。
"""

import ctypes

LANGUAGES = ("zh", "en")

# 键 → (中文, English)
MESSAGES = {
    # ── 面板：状态灯 ──────────────────────────────────────────────
    "lamp_proxy_on": ("代理 DLL：已安装", "Proxy DLL: installed"),
    "lamp_proxy_off": ("代理 DLL：未安装", "Proxy DLL: not installed"),
    "lamp_pad_on": ("手柄：已连接", "Controller: connected"),
    "lamp_pad_wait": ("手柄：等待连接", "Controller: waiting"),
    "lamp_pad_off": ("手柄：未启动", "Controller: not started"),
    "lamp_acc_on": ("ACC 遥测：更新中", "ACC telemetry: live"),
    "lamp_acc_off": ("ACC 遥测：没有数据", "ACC telemetry: no data"),
    "lamp_drive_on": ("震动：运行中", "Haptics: running"),
    "lamp_drive_off": ("震动：已停止", "Haptics: stopped"),

    # ── 面板：分组与控件 ──────────────────────────────────────────
    "group_status": ("状态", "Status"),
    "group_tuning": ("手感（拖动即保存）", "Feel (drag to save)"),
    "sl_road": ("路面纹理", "Road texture"),
    "sl_brake": ("刹车底层", "Brake floor"),
    "sl_throttle": ("油门底层", "Throttle floor"),
    "sl_event": ("事件强度", "Event strength"),
    "sl_understeer": ("推头强度", "Understeer gain"),
    "sl_loss": ("失控强度", "Loss of control"),
    "btn_start": ("开始震动", "Start haptics"),
    "btn_stop": ("停止震动", "Stop haptics"),
    "btn_install": ("安装接管", "Install takeover"),
    "btn_restore": ("恢复原生", "Restore native"),
    "btn_language": ("语言", "Language"),

    # ── 面板：提示与日志 ──────────────────────────────────────────
    "log_started": ("面板已启动 v%s", "Panel started v%s"),
    "log_exit": ("面板退出", "Panel closed"),
    "log_saved": ("手感已保存：路面 %.2f× ｜ 刹车 %.2f× ｜ 油门 %.2f× ｜ 事件 %.2f ｜ 推头 %.2f ｜ 失控 %.2f",
                  "Feel saved: road %.2f× | brake %.2f× | throttle %.2f× | event %.2f | understeer %.2f | loss of control %.2f"),
    "log_save_fail": ("保存手感失败：%s", "Saving feel settings failed: %s"),
    "log_starting": ("正在启动震动……", "Starting haptics…"),
    "log_stopping": ("正在停止……", "Stopping…"),
    "log_proxy_missing": ("警告：代理 DLL 不在 ACC 目录，扳机会被 ACC 每帧冲掉",
                          "Warning: proxy DLL is not in the ACC folder; ACC will overwrite the triggers every frame"),
    "log_installed": ("已安装接管：ACC 的震动写入已被屏蔽（下次启动 ACC 生效）",
                      "Takeover installed: ACC rumble is blocked (takes effect next launch)"),
    "log_restored": ("已恢复原生：ACC 自己的震动回来了（下次启动 ACC 生效）",
                     "Restored: ACC's own rumble is back (takes effect next launch)"),
    "log_nothing_to_restore": ("本来就没有接管，无需恢复", "Nothing to restore"),
    "log_reloaded": ("手感设置已重新读取", "Feel settings reloaded"),
    "log_drive_started": ("震动已开始（手柄没连上时会每秒自动重连）",
                          "Haptics started (reconnecting once per second until the controller shows up)"),
    "log_drive_stopped": ("震动已停止，四个马达已清零", "Haptics stopped, all motors zeroed"),
    "log_master_error": ("主循环出错：%s", "Main loop error: %s"),
    "log_telemetry_error": ("读遥测出错：%s", "Telemetry read error: %s"),
    "log_telemetry_open_fail": ("遥测打不开：%s", "Could not open telemetry: %s"),
    "log_driver_fail": ("马达驱动启动失败：%s", "Could not start the motor driver: %s"),

    # ── 面板：弹窗 ────────────────────────────────────────────────
    "dlg_no_acc_title": ("找不到 ACC", "ACC not found"),
    "dlg_no_acc_body": ("没在常见路径和 Steam 库里找到 ACC。\n请确认游戏已经安装。",
                        "Could not find ACC in the usual paths or Steam libraries.\nMake sure the game is installed."),
    "dlg_running_title": ("ACC 正在运行", "ACC is running"),
    "dlg_running_body": ("游戏里加载着这个 DLL，改不动。\n请先完全退出 ACC，再点一次。",
                         "The DLL is in use by the game.\nPlease quit ACC completely and try again."),
    "dlg_missing_title": ("文件缺失", "File missing"),
    "dlg_missing_body": ("找不到 tools\\xinput_proxy\\xinput1_3.dll。",
                         "tools\\xinput_proxy\\xinput1_3.dll was not found."),
    "dlg_denied_title": ("写入被拒绝", "Access denied"),
    "dlg_denied_body": ("复制失败，文件可能被占用。\n请确认 ACC 已完全退出；仍失败就右键以管理员身份运行。",
                        "Copy failed; the file may be in use.\nMake sure ACC is closed. If it still fails, run as administrator."),
    "dlg_copy_fail_title": ("复制失败", "Copy failed"),
    "dlg_restore_fail_title": ("恢复失败", "Restore failed"),
    "dlg_rename_denied_title": ("改名被拒绝", "Rename denied"),
    "dlg_rename_denied_body": ("文件可能被占用，请确认 ACC 已退出。",
                              "The file may be in use; make sure ACC is closed."),
    "dlg_no_acc_short": ("没在预设路径里找到 ACC。", "ACC was not found in the usual paths."),

    # ── 面板：语言 ────────────────────────────────────────────────
    "lang_zh": ("中文", "中文"),
    "lang_en": ("English", "English"),

    # ── 驱动层日志 ────────────────────────────────────────────────
    "pad_priority": ("优先级没能提升（不影响功能，但全屏游戏里可能更抖）。",
                     "Could not raise process priority (harmless, but may feel less steady in fullscreen)."),
    "pad_wait_device": ("没看到手柄。按一下手柄让它连上，我在这里等……",
                        "No controller yet. Press a button on it and I'll keep waiting…"),
    "pad_no_device": ("暂时没有手柄，后台每 %.0f 秒重试一次。",
                      "No controller right now; retrying every %.0f s in the background."),
    "pad_stop_slow": ("写入线程没有按时停下，直接收尾。",
                      "Writer thread did not stop in time; finishing anyway."),
    "pad_loop_error": ("写入循环出错（%s: %s），继续重试。",
                       "Writer loop error (%s: %s); still retrying."),
    "pad_enum_error": ("枚举 HID 设备时出错：%s", "Failed to enumerate HID devices: %s"),
    "pad_connected": ("已连上手柄（PID %04X，通道：HID 报文 0x03）",
                      "Controller connected (PID %04X, channel: HID report 0x03)"),
    "pad_dropped": ("%s。每 %.0f 秒重试一次。", "%s. Retrying every %.0f s."),
    "pad_write_fail": ("写入失败，系统错误码 %d", "Write failed, system error %d"),
    "pad_lock_fail": ("创建单实例锁失败（错误码 %d）。",
                      "Could not create the single-instance lock (error %d)."),
    "pad_busy": ("已经有一个马达驱动在运行了。\n两个程序同时写手柄会互相打断，所以这里不重复启动。\n请先关掉那个窗口，再试一次。",
                 "A motor driver is already running.\nTwo writers would fight over the controller, so this one will not start.\nClose the other window and try again."),

    "log_prefix_telemetry": ("[遥测 %s] %s", "[Telemetry %s] %s"),
    "log_prefix_pad": ("[马达 %s] %s", "[Motor %s] %s"),

    # ── 遥测层日志 ────────────────────────────────────────────────
    "tel_never": ("还没连过。", "Not connected yet."),
    "tel_open_missing": ("找不到 Local\\acpmf_physics：ACC 没在运行，或者还停在主菜单、没进赛道。",
                         "Local\\acpmf_physics not found: ACC is not running, or still in the menu instead of on track."),
    "tel_open_denied": ("共享内存找到了，但拒绝访问：ACC 可能以管理员身份运行，本程序也要用管理员身份打开。",
                        "Shared memory found but access was denied: ACC may be running as administrator, so this program must run as administrator too."),
    "tel_open_fail": ("打开共享内存失败，系统错误码 %d。",
                      "Could not open shared memory, system error %d."),
    "tel_map_fail": ("共享内存能打开，但映射进本进程失败（错误码 %d），通常还是权限问题。",
                     "Shared memory opened but could not be mapped (error %d); usually a permissions issue."),
    "tel_stale_long": ("ACC 遥测长时间没有更新：可能退出了、暂停了，或者还停在菜单里。",
                       "ACC telemetry has not updated for a long time: the game may have exited, paused, or still be in a menu."),
    "tel_recovered": ("ACC 遥测恢复了。", "ACC telemetry is back."),
    "tel_connected": ("已连上 ACC 遥测（Local\\acpmf_physics）。",
                      "Connected to ACC telemetry (Local\\acpmf_physics)."),
    "tel_stale_short": ("ACC 遥测超过 %.1f 秒没有更新，先按没有数据处理。",
                        "ACC telemetry has not updated for %.1f s; treating it as no data."),

    # ── 手感设置读取 ──────────────────────────────────────────────
    "cfg_read_fail": ("[手感] 读取 %s 失败：%s（先用默认值）",
                      "[Settings] Could not read %s: %s (using defaults)"),
    "cfg_unknown_key": ("[手感] 不认识的设置项 %s，已忽略。",
                        "[Settings] Unknown key %s ignored."),
    "cfg_not_number": ("[手感] %s 不是数字，忽略。", "[Settings] %s is not a number; ignored."),
    "cfg_not_finite": ("[手感] %s 不是有效数字，忽略。", "[Settings] %s is not a finite number; ignored."),
    "cfg_out_of_range": ("[手感] %s = %.2f 超出 %.2f~%.2f，按 %.2f 处理。",
                         "[Settings] %s = %.2f is outside %.2f~%.2f; using %.2f."),
    "cfg_updated": ("[手感] 已更新：%s", "[Settings] Updated: %s"),
    "cfg_tuning_text": ("路面 %.2f× ｜ 刹车底层 %.2f× ｜ 油门底层 %.2f× ｜ 事件 %.2f ｜ 推头 %.2f ｜ 失控 %.2f",
                        "Road %.2f× | Brake floor %.2f× | Throttle floor %.2f× | Event %.2f | Understeer %.2f | Loss of control %.2f"),

    # ── 命令行版 ──────────────────────────────────────────────────
    "cli_preflight": ("开始前确认：", "Before you start:"),
    "cli_preflight_1": ("  1. 已经运行过 开启接管.bat，否则 ACC 会每帧冲掉扳机",
                        "  1. You have run the takeover installer, otherwise ACC wipes the triggers every frame"),
    "cli_preflight_2": ("  2. Steam 输入已关闭", "  2. Steam Input is off"),
    "cli_preflight_3": ("  3. 手柄已连接（蓝牙或 USB-C），ACC 已进入赛道",
                        "  3. The controller is connected (Bluetooth or USB-C) and ACC is on track"),
    "cli_ctrl_c": ("按 Ctrl+C 退出，退出时四个马达一定会清零。",
                   "Press Ctrl+C to quit; all four motors are always zeroed on exit."),
    "cli_settings": ("手感设置：%s", "Feel settings: %s"),
    "cli_settings_default": ("手感设置：默认值（双击 调手感.bat 可以改）",
                             "Feel settings: defaults (run the settings editor to change)"),
    "cli_driver_fail": ("马达驱动启动失败：%s", "Could not start the motor driver: %s"),
    "cli_driver_fail_detail": ("马达驱动启动失败：%s: %s", "Could not start the motor driver: %s: %s"),
    "cli_selftest_done": ("自测时间到，准备退出。", "Self-test finished, exiting."),
    "cli_interrupted": ("收到 Ctrl+C，正在清零……", "Ctrl+C received, zeroing motors…"),
    "cli_loop_error": ("主循环出错：%s: %s", "Main loop error: %s: %s"),
    "cli_report": ("（把上面这行发给开发者）", "(Send the line above to the developer)"),
    "cli_exit": ("已退出。", "Exited."),
    "cli_status": ("[%07d] %6.1f km/h  档 %-2s  刹车 %.2f  油门 %.2f  ABS %d  |  "
                   "扳机 %3.0f/%3.0f  握把峰 %3.0f/%3.0f  |  左 %s ｜ 右 %s",
                   "[%07d] %6.1f km/h  gear %-2s  brake %.2f  throttle %.2f  ABS %d  |  "
                   "triggers %3.0f/%3.0f  grip peak %3.0f/%3.0f  |  L %s | R %s"),
    "cli_none": ("-", "-"),
    "cli_description": ("ACC Trigger Haptics · 主循环", "ACC Trigger Haptics · main loop"),
    "cli_hz_help": ("主循环频率，默认 60（和马达写入频率一致）",
                    "Loop rate, default 60 (matches the motor write rate)"),
    "cli_status_help": ("每秒打印一行状态（联调时用）",
                        "Print one status line per second (for debugging)"),
    "cli_seconds_help": ("跑满这么多秒自动停（自测用，默认 0 = 一直跑）",
                         "Stop after this many seconds (0 = run forever)"),
}

_language = "zh"


def system_language():
    """Windows 显示语言：中文（含繁体）返回 zh，其它返回 en。"""
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "zh" if (lang_id & 0x3FF) == 0x04 else "en"
    except Exception:
        return "zh"


def set_language(code):
    """code 只接受 zh / en，其它值忽略（保持原样）。"""
    global _language
    if code in LANGUAGES:
        _language = code


def get_language():
    return _language


def t(key, *args):
    pair = MESSAGES.get(key)
    if pair is None:
        return key
    text = pair[1] if _language == "en" else pair[0]
    if args:
        try:
            text = text % args
        except TypeError:
            pass
    return text
