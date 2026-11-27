# -*- coding: utf-8 -*-
"""控制面板：状态灯 + 五个滑块 + 起停 + 代理 DLL 装卸。

为什么面板自带一份循环，而不是复用 main.py：
    `main.py` 是目前唯一实车验证过的链路，也是出问题时的退路。
    这里单独放一份循环，改面板就不会波及退路。
    代价是这段循环逻辑有两份，**改的时候要一起改**（main.py 里也有注释提醒）。

线程分工（很重要）：
    - 主线程：tkinter 的界面，只做界面，绝不碰手柄和共享内存
    - 后台线程 Runner：60Hz 读遥测 → 判断 → 写马达，结果放在一把锁保护的字典里
    界面每 200ms 取一次状态，所以界面卡一下也不会影响震动。

启动方式（日常）：
    双击 启动面板.bat（本机专用，写死了带 tkinter 的 Python 路径）

自检（不需要 ACC，也不需要手柄）：
    带 tkinter 的 Python 跑 src/panel.py --selftest
    会建出窗口、跑 1.5 秒、自己关掉，退出码 0 表示界面和循环都没炸。
"""

import ctypes
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:                     # 源码启动脚本只需要代理装卸，不建窗口
    tk = None
    messagebox = ttk = None

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from acc_telemetry import AccTelemetry                     # noqa: E402
from haptics_engine import (                               # noqa: E402
    HapticsEngine, config_path, load_tuning, load_tuning_result,
    write_default_config,
)
from pad_driver import PadBusy, PadDriver                  # noqa: E402
from version import VERSION                                # noqa: E402
from i18n import (                                          # noqa: E402
    get_language, set_language, system_language, t,
)


def _app_dir():
    """exe 所在目录（源码运行时是仓库根目录）。设置、日志都放这儿。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT


def _bundle_dir():
    """打包后资源所在的临时目录；源码运行时就是仓库根目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else ROOT


APP_DIR = _app_dir()
BUNDLE_DIR = _bundle_dir()
LOG_PATH = APP_DIR / "panel.log"
SETTINGS_PATH = APP_DIR / "panel-settings.json"
PROXY_SOURCE = BUNDLE_DIR / "tools" / "xinput_proxy" / "xinput1_3.dll"
PROXY_LOCAL = APP_DIR / "tools" / "xinput_proxy" / "xinput1_3.dll"   # 兜底
PROXY_NAME = "xinput1_3.dll"
PROXY_BACKUP = PROXY_NAME + ".off"

# 已知代理 DLL 的 SHA-256。当前仓库历史上的代理都是同一份；
# 这里的哈希只是快速通道，真正判断归属还要看文件内容里的标记（见 _looks_like_our_proxy）。
KNOWN_PROXY_HASHES = {
    "8e284775850c9cba345b2b9912a71a45050f84c9186a0387387f5105cd4afe6c",
}
PROXY_EXPORT_NAMES = (
    "XInputGetState",
    "XInputSetState",
    "XInputGetCapabilities",
    "XInputEnable",
    "XInputGetDSoundAudioDeviceGuids",
    "XInputGetBatteryInformation",
    "XInputGetKeystroke",
)

# ACC 目录探测：先试下面这些常见位置（本机就是第一个），
# 再读 Steam 自己的库配置，兼容装在别的盘 / 自定义库的人。
ACC_CANDIDATES = (
    r"E:\SteamLibrary\steamapps\common\Assetto Corsa Competizione\AC2\Binaries\Win64",
    r"D:\SteamLibrary\steamapps\common\Assetto Corsa Competizione\AC2\Binaries\Win64",
    r"C:\SteamLibrary\steamapps\common\Assetto Corsa Competizione\AC2\Binaries\Win64",
    r"C:\Program Files (x86)\Steam\steamapps\common\Assetto Corsa Competizione"
    r"\AC2\Binaries\Win64",
)

_ACC_RELATIVE = Path("steamapps") / "common" / "Assetto Corsa Competizione" \
    / "AC2" / "Binaries" / "Win64"
_ACC_CACHE = {"at": 0.0, "path": None}

# 滑块：(键, 文案键, 上限)。上限是滑块最右端对应的实际数值。
SLIDERS = (
    ("grip_road", "sl_road", 4.00),
    ("trigger_floor", "sl_brake", 1.80),
    ("throttle_floor", "sl_throttle", 2.30),
    ("trigger_event", "sl_event", 1.00),
    ("understeer_gain", "sl_understeer", 1.50),
    ("loss_gain", "sl_loss", 1.50),
)

# 这五项是倍率：1.00 = 标准手感，界面和日志里数值后面加 ×。
GAIN_KEYS = ("grip_road", "trigger_floor", "throttle_floor",
             "understeer_gain", "loss_gain")

TOPS = {key: top for key, _label, top in SLIDERS}
LABEL_KEYS = {key: label for key, label, _top in SLIDERS}

# 倍率滑块的位置范围固定为 0~2：中点 1.00 是标准值，
# 左半段 0~1 线性，右半段 1~上限线性。这样 1.00 永远在正中间，
# 同时保留各自的最大倍率（路面 4×，刹车 1.8× ……）。
SLIDER_SPAN = 2.0
SMALL_FONT = ("Microsoft YaHei UI", 8)


def value_to_position(key, value):
    """实际倍率 → 滑块位置（0~2）。非倍率项原样返回。"""
    if key not in GAIN_KEYS:
        return value
    top = max(1.0, TOPS[key])
    value = max(0.0, min(top, value))
    if value <= 1.0:
        return value
    return 1.0 + (value - 1.0) / (top - 1.0)


def position_to_value(key, position):
    """滑块位置（0~2）→ 实际倍率。非倍率项原样返回。"""
    if key not in GAIN_KEYS:
        return position
    top = max(1.0, TOPS[key])
    position = max(0.0, min(SLIDER_SPAN, position))
    if position <= 1.0:
        return position
    return 1.0 + (position - 1.0) * (top - 1.0)


def format_value(key, value):
    suffix = "×" if key in GAIN_KEYS else ""
    return "%.2f%s" % (value, suffix)


def load_settings():
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data):
    try:
        SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

C_GREEN = "#2e7d32"
C_RED = "#c62828"
C_GRAY = "#757575"
C_AMBER = "#b26a00"

NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW，跑 tasklist 时不闪黑框


# ══════════════════════════════════════════════════════════════════
# 小工具：ACC 目录 / 游戏是否在跑 / 手感文件读写
# ══════════════════════════════════════════════════════════════════
def _steam_roots():
    """从注册表拿 Steam 安装目录（可能有多个）。"""
    roots = []
    try:
        import winreg
    except Exception:
        return roots
    targets = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, key, name in targets:
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, name)
        except OSError:
            continue
        if not value:
            continue
        path = Path(str(value).replace("\\", "/"))
        if path not in roots:
            roots.append(path)
    return roots


def _steam_libraries():
    """Steam 根目录 + libraryfolders.vdf 里登记的其它库。"""
    libraries = []
    for root in _steam_roots():
        libraries.append(root)
        config = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            path = Path(match.group(1).replace("\\\\", "\\"))
            if path not in libraries:
                libraries.append(path)
    return libraries


def acc_bin():
    """返回 ACC 的 Win64 目录，找不到返回 None。

    先试写死的常见路径（本机的情况），再读 Steam 库配置——
    别人的游戏装在自定义库时也能找到。
    """
    for text in ACC_CANDIDATES:
        path = Path(text)
        if (path / "AC2-Win64-Shipping.exe").exists():
            return path

    now = time.monotonic()
    if now - _ACC_CACHE["at"] < 10.0:
        return _ACC_CACHE["path"]

    found = None
    for library in _steam_libraries():
        candidate = library / _ACC_RELATIVE
        if (candidate / "AC2-Win64-Shipping.exe").exists():
            found = candidate
            break
    _ACC_CACHE["at"] = now
    _ACC_CACHE["path"] = found
    return found


def acc_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AC2-Win64-Shipping.exe"],
            capture_output=True, creationflags=NO_WINDOW, timeout=5,
        )
        return b"AC2-Win64-Shipping.exe" in out.stdout
    except Exception:
        return False


class ProxyError(Exception):
    """带双语文案键的代理装卸错误，界面层直接拿它弹窗。"""

    def __init__(self, title_key, body_key, *body_args):
        super().__init__(body_key)
        self.title_key = title_key
        self.body_key = body_key
        self.body_args = body_args


def _looks_like_our_proxy(data):
    """按文件内容判断是不是本项目的代理 DLL。

    只认两个特征：
      1. 导出表里有完整的 XInput 函数名（旧版代理也一样）；
      2. 二进制里带编译源文件名 `proxy.c`（本项目代理的编译产物特征）。
    另外排除微软自带转发 DLL 的特征串，避免把系统文件误判成自己的。
    """
    if not data or not data.startswith(b"MZ"):
        return False
    lowered = data.lower()
    if b"xinputforwarder" in lowered or b"xinput1_3.pdb" in lowered:
        return False
    if b"proxy.c\x00" not in data and b"proxy.c" not in data:
        return False
    return all(name.encode("ascii") + b"\x00" in data
               for name in PROXY_EXPORT_NAMES)


def proxy_kind(path):
    """返回 none / ours / unknown。只读内容，不看文件名。"""
    path = Path(path)
    if not path.exists():
        return "none"
    try:
        data = path.read_bytes()
    except OSError:
        return "unknown"
    if hashlib.sha256(data).hexdigest() in KNOWN_PROXY_HASHES:
        return "ours"
    return "ours" if _looks_like_our_proxy(data) else "unknown"


def proxy_state(folder=None):
    """返回 (状态, 文件路径)。状态为 no_acc / none / ours / unknown。"""
    if folder is None:
        folder = acc_bin()
    if not folder:
        return "no_acc", None
    path = Path(folder) / PROXY_NAME
    return proxy_kind(path), path


def proxy_installed():
    """只有内容确认是本项目的代理才算“已安装”。"""
    return proxy_state()[0] == "ours"


def _unique_backup_path(folder):
    """给改名后的代理找一个不冲突的名字，绝不覆盖已有文件。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "%s.%s" % (PROXY_BACKUP, stamp)
    candidate = Path(folder) / base
    index = 1
    while candidate.exists():
        candidate = Path(folder) / ("%s-%d" % (base, index))
        index += 1
    return candidate


def install_proxy(folder, source):
    """把代理复制进 ACC 目录，返回一些给界面用的提示信息。

    遇到无法确认归属的同名 DLL 时直接拒绝，原文件和任何 .off 备份都不动。
    复制先写临时文件再整体替换，复制/替换失败时原文件保持不变。
    """
    folder = Path(folder)
    source = Path(source)
    active = folder / PROXY_NAME
    backup = folder / PROXY_BACKUP

    if proxy_kind(source) != "ours":
        raise ProxyError("dlg_missing_title", "dlg_missing_body")

    existing = proxy_kind(active)
    if existing == "unknown":
        raise ProxyError("dlg_unknown_title", "dlg_unknown_install_body",
                         str(active))

    tmp = folder / ("%s.tmp-%d-%d" % (PROXY_NAME, os.getpid(),
                                      time.time_ns()))
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, active)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

    return {
        "upgraded": existing == "ours",
        "backup_preserved": backup.exists() and proxy_kind(backup) != "ours",
    }


def restore_proxy(folder):
    """撤掉代理，恢复原生震动。

    只处理内容确认属于本项目的代理；无法确认的文件一律不动。
    如果已有 .off 备份：是代理就正常覆盖它；不是代理就保留原备份，
    把当前代理改成一个带时间戳的新名字，两个文件都留给用户。
    """
    folder = Path(folder)
    active = folder / PROXY_NAME
    backup = folder / PROXY_BACKUP
    kind = proxy_kind(active)
    if kind == "none":
        return {"action": "nothing"}
    if kind == "unknown":
        raise ProxyError("dlg_unknown_title", "dlg_unknown_restore_body",
                         str(active))

    try:
        if backup.exists() and proxy_kind(backup) != "ours":
            target = _unique_backup_path(folder)
            os.replace(active, target)
            return {
                "action": "moved",
                "target": str(target),
                "kept_backup": str(backup),
            }
        os.replace(active, backup)
    except PermissionError:
        raise ProxyError("dlg_rename_denied_title", "dlg_rename_denied_body")
    return {"action": "renamed"}


def mtime_of(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# TOML 合法的裸数字写法：普通整数/小数、指数、下划线分隔、十六/八/二进制、inf/nan。
# 面板只写小数，但用户手改过的文件里可能是 1e0 或 1_0.0，必须原地替换而不是追加新键。
_TOML_NUMBER = r"""
    [+-]?
    (?:
        0[xX][0-9A-Fa-f](?:_?[0-9A-Fa-f])*
      | 0[oO][0-7](?:_?[0-7])*
      | 0[bB][01](?:_?[01])*
      | (?:[0-9](?:_?[0-9])*)
        (?:\.[0-9](?:_?[0-9])*)?
        (?:[eE][+-]?[0-9](?:_?[0-9])*)?
      | inf
      | nan
    )
"""


def _insert_top_level_key(text, key, value):
    """把缺失的键放到第一张表之前，避免被写成表内键。"""
    line = "%s = %.2f" % (key, value)
    match = re.search(r"(?m)^[ \t]*\[", text)
    if match is not None:
        before = text[:match.start()]
        if before and not before.endswith("\n"):
            before += "\n"
        return before + line + "\n" + text[match.start():]
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def save_values(path, values):
    """只改等号右边的数字，行内注释原样保留。

    先写同目录的临时文件再整体替换：主程序每秒读一次这个文件，
    直接原地改可能让它读到半截，数值会闪回默认值。
    替换前先用 tomllib 解析候选内容；解析失败就抛错，原文件一个字节都不动。
    """
    path = Path(path)
    if not path.exists():
        write_default_config(path)
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        pattern = re.compile(
            r"^(?P<head>[ \t]*%s[ \t]*=[ \t]*)(?P<num>%s)"
            r"(?P<tail>[ \t]*(?:[#].*)?)$"
            % (re.escape(key), _TOML_NUMBER),
            re.MULTILINE | re.VERBOSE,
        )

        def _replace(match, value=value):
            return "%s%.2f%s" % (match.group("head"), value, match.group("tail"))

        text, count = pattern.subn(_replace, text, count=1)
        if count == 0:
            # 已有这个键但写法不是裸数字（例如加引号/数组）时不要追加，
            # 否则会变成重复键。让用户先把那一行改回来。
            if re.search(r"(?m)^[ \t]*%s[ \t]*=" % re.escape(key), text):
                raise ValueError(t("save_not_applied",
                                   "%s (existing value is not a bare number)"
                                   % key))
            text = _insert_top_level_key(text, key, value)

    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(t("save_invalid_toml", exc)) from exc
    for key, value in values.items():
        actual = parsed.get(key)
        if not isinstance(actual, (int, float)) \
                or abs(float(actual) - float(value)) > 1e-9:
            raise ValueError(t("save_not_applied",
                               "%s (got %r)" % (key, actual)))

    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        last_error = None
        for _ in range(15):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:      # 主程序正在读，等它一下
                last_error = exc
                time.sleep(0.02)
        raise last_error
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════
# 后台循环（和 main.py 同一套逻辑，两份分开维护）
# ══════════════════════════════════════════════════════════════════
class Runner(threading.Thread):
    """60Hz 循环。界面只通过 state() 看结果，通过 set_drive() 起停。"""

    def __init__(self, log):
        super().__init__(name="haptics-loop", daemon=True)
        self._log = log
        self._guard = threading.RLock()
        self._stop_event = threading.Event()
        self._want_drive = False
        self._driver = None
        self._telemetry = None
        self._state = {
            "acc": False,       # 遥测有没有数据
            "pad": False,       # 手柄连没连
            "driving": False,   # 震动开没开
            "message": "",
        }

    # ── 给界面用 ─────────────────────────────────────────────────
    def state(self):
        with self._guard:
            return dict(self._state)

    def set_drive(self, wanted):
        with self._guard:
            self._want_drive = bool(wanted)
            self._state["message"] = (t("log_starting") if wanted
                                      else t("log_stopping"))

    def shutdown(self):
        self._stop_event.set()
        self.join(timeout=3.0)

    def _note(self, text):
        with self._guard:
            self._state["message"] = text
        self._log(text)

    # ── 主循环 ───────────────────────────────────────────────────
    def run(self):
        loop_error = None
        try:
            try:
                self._telemetry = AccTelemetry(log=self._log)
            except Exception as exc:
                self._note(t("log_telemetry_open_fail", exc))
                return

            engine = HapticsEngine(load_tuning())
            tuning_file = config_path()
            config_mtime = mtime_of(tuning_file)
            last_config_check = 0.0
            have_signal = False
            interval = 1.0 / 60.0
            next_tick = time.perf_counter()

            while not self._stop_event.is_set():
                now = time.perf_counter()
                driver = self._sync_driver(engine)

                try:
                    snap = self._telemetry.latest()
                except Exception as exc:
                    self._note(t("log_telemetry_error", exc))
                    snap = None

                if snap is None:
                    # 没数据就不要喂旧快照：清零一次，引擎的包络也一起清掉
                    if have_signal:
                        have_signal = False
                        engine.reset()
                        if driver is not None:
                            driver.set_vibration()
                else:
                    have_signal = True
                    if driver is not None:
                        try:
                            motors = engine.update(snap, now=now)
                        except Exception as exc:
                            # 这一帧算不出来：先让四个马达立刻归零，再清掉引擎状态。
                            # 任何一步失败都不能挡住后面几步。
                            have_signal = False
                            self._note(t("log_master_error", exc))
                            try:
                                driver.set_vibration()
                            except Exception as zero_exc:
                                self._log(t("log_zero_fail", zero_exc))
                            try:
                                engine.reset()
                            except Exception as reset_exc:
                                self._log(t("log_reset_fail", reset_exc))
                        else:
                            try:
                                driver.set_vibration(*motors)
                            except Exception as exc:
                                self._note(t("log_master_error", exc))

                with self._guard:
                    self._state["acc"] = snap is not None
                    self._state["pad"] = bool(
                        driver is not None and driver.connected)
                    self._state["driving"] = driver is not None

                # 手感文件热更新：面板自己写的也算，值一样，重载无害。
                # 读取失败时继续用上次成功的设置，不跳回默认值。
                if now - last_config_check >= 1.0:
                    last_config_check = now
                    mtime = mtime_of(tuning_file)
                    if mtime is not None and mtime != config_mtime:
                        config_mtime = mtime
                        ok, tuning = load_tuning_result(
                            tuning_file, fallback=engine.tuning)
                        if ok:
                            engine.set_tuning(tuning)
                            self._log(t("log_reloaded"))
                        else:
                            self._note(t("log_reload_kept"))

                next_tick += interval
                delay = next_tick - time.perf_counter()
                if delay > 0.0:
                    time.sleep(delay)
                else:
                    next_tick = time.perf_counter()
        except Exception as exc:
            # 循环本身（不是单帧计算）炸了：记下来，交给 finally 统一收尾。
            loop_error = exc
            self._note(t("log_loop_exit", type(exc).__name__, exc))
        finally:
            self._cleanup(loop_error)

    def _cleanup(self, loop_error=None):
        """线程退出时一定走这里：停止写入、清零、关遥测、更新面板状态。

        每一步单独 try：某一步失败也要把剩下的步骤做完。
        """
        with self._guard:
            driver = self._driver
            self._driver = None
            self._want_drive = False

        if driver is not None:
            try:
                driver.set_vibration()
            except Exception as exc:
                self._log(t("log_cleanup_zero_fail", exc))
            try:
                driver.stop()
            except Exception as exc:
                self._log(t("log_cleanup_driver_fail", exc))

        telemetry, self._telemetry = self._telemetry, None
        if telemetry is not None:
            try:
                telemetry.close()
            except Exception as exc:
                self._log(t("log_cleanup_telemetry_fail", exc))

        with self._guard:
            self._state["driving"] = False
            self._state["pad"] = False
            self._state["acc"] = False
            if loop_error is not None:
                self._state["message"] = t(
                    "log_loop_exit", type(loop_error).__name__, loop_error)

    def _sync_driver(self, engine):
        """起停都放在这个线程里做：start() 可能卡几百毫秒，不能放进界面线程。"""
        with self._guard:
            wanted = self._want_drive
            driver = self._driver

        if wanted and driver is None:
            try:
                new = PadDriver(log=self._log)
                new.start(wait=0.0)
            except PadBusy as exc:
                with self._guard:
                    self._want_drive = False
                self._note(str(exc))
                return None
            except Exception as exc:
                with self._guard:
                    self._want_drive = False
                self._note(t("log_driver_fail", exc))
                return None
            with self._guard:
                self._driver = new
            engine.reset()
            self._note(t("log_drive_started"))
            return new

        if not wanted and driver is not None:
            with self._guard:
                self._driver = None
            try:
                driver.stop()
            except Exception:
                pass
            self._note(t("log_drive_stopped"))
            return None

        return driver


# ══════════════════════════════════════════════════════════════════
# 界面
# ══════════════════════════════════════════════════════════════════
class Panel:
    def __init__(self, root):
        self.root = root
        self._guard = threading.Lock()
        self._last_line = ""
        self._write_job = None
        self._flash_jobs = {}
        self._syncing = False
        self._closing = False

        root.title("ACC Trigger Haptics")
        root.minsize(620, 1)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)

        self._setup_log()

        # 语言：上次的选择优先，没有就跟随 Windows 显示语言
        settings = load_settings()
        set_language(settings.get("language") or system_language())

        self._build()

        self.runner = Runner(self.log)
        self.runner.start()
        self.log(t("log_started", VERSION))

        self._tick()

    # ── 日志 ─────────────────────────────────────────────────────
    def _setup_log(self):
        self._logger = logging.getLogger("panel")
        self._logger.setLevel(logging.INFO)
        if not self._logger.handlers:
            handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
            self._logger.addHandler(handler)

    def log(self, text):
        try:
            self._logger.info(text)
        except Exception:
            pass
        text = str(text)
        if len(text) > 100:
            text = text[:99] + "…"
        with self._guard:
            self._last_line = text

    # ── 布局 ─────────────────────────────────────────────────────
    def _build(self):
        font = ("Microsoft YaHei UI", 10)
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TButton", font=font)
        style.configure("TLabel", font=font)

        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        self.frame = frame

        # 状态灯：2x2
        lamps = ttk.LabelFrame(frame, text=t("group_status"), padding=8)
        lamps.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.lamps = {}
        for index, key in enumerate(("proxy", "pad", "acc", "drive")):
            label = ttk.Label(lamps, text="○", font=font)
            label.grid(row=index // 2, column=(index % 2) * 2, sticky="w",
                       padx=(0, 4), pady=2)
            self.lamps[key] = label
        lamps.columnconfigure(1, weight=1)
        lamps.columnconfigure(3, weight=1)

        # 滑块
        tuning = load_tuning()
        self.vars = {}
        self.value_vars = {}
        self.value_entries = {}
        sliders = ttk.LabelFrame(frame, text=t("group_tuning"), padding=8)
        sliders.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        for row, (key, label_key, top) in enumerate(SLIDERS):
            is_gain = key in GAIN_KEYS
            ttk.Label(sliders, text=t(label_key), width=13).grid(
                row=row, column=0, sticky="w", pady=3)
            ttk.Label(sliders, text="0", width=2, anchor="e",
                      foreground=C_GRAY, font=SMALL_FONT).grid(
                row=row, column=1, sticky="e")

            var = tk.DoubleVar(
                value=value_to_position(key, float(tuning[key])))
            self.vars[key] = var
            span = SLIDER_SPAN if is_gain else top
            ttk.Scale(sliders, from_=0.0, to=span, variable=var,
                      orient="horizontal", length=170,
                      command=lambda _v, k=key: self._on_slide(k)).grid(
                row=row, column=2, sticky="ew", padx=4)

            ttk.Label(sliders, text=format_value(key, top), width=6,
                      anchor="w", foreground=C_GRAY, font=SMALL_FONT).grid(
                row=row, column=3, sticky="w")

            text = tk.StringVar(value="%.2f" % position_to_value(
                key, float(var.get())))
            self.value_vars[key] = text
            entry = ttk.Entry(sliders, textvariable=text, width=6,
                              justify="right", font=font)
            entry.grid(row=row, column=4, sticky="e", padx=(4, 0))
            entry.bind("<Return>", lambda _e, k=key: self._commit_entry(k))
            entry.bind("<KP_Enter>", lambda _e, k=key: self._commit_entry(k))
            entry.bind("<FocusOut>", lambda _e, k=key: self._commit_entry(k))
            entry.bind("<Escape>", lambda _e, k=key: self._cancel_entry(k))
            entry.bind("<FocusIn>",
                       lambda _e, k=key, w=entry: self._focus_entry(k, w))
            self.value_entries[key] = entry

            ttk.Label(sliders, text="×" if is_gain else "",
                      foreground=C_GRAY).grid(
                row=row, column=5, sticky="w", padx=(2, 0))
        sliders.columnconfigure(2, weight=1)

        # 按钮
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.start_button = ttk.Button(buttons, text=t("btn_start"),
                                       command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_button = ttk.Button(buttons, text=t("btn_stop"),
                                      command=self._stop)
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

        proxy = ttk.Frame(frame)
        proxy.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.install_button = ttk.Button(proxy, text=t("btn_install"),
                                         command=self._install)
        self.install_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.restore_button = ttk.Button(proxy, text=t("btn_restore"),
                                         command=self._restore)
        self.restore_button.grid(row=0, column=1, sticky="ew")
        proxy.columnconfigure(0, weight=1)
        proxy.columnconfigure(1, weight=1)

        # 底部一行提示
        self.status_label = ttk.Label(frame, text="", wraplength=470,
                                      justify="left", foreground=C_GRAY)
        self.status_label.grid(row=4, column=0, columnspan=3, sticky="w",
                               pady=(12, 0))

        # 底部：语言选择 + 版本号
        bottom = ttk.Frame(frame)
        bottom.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.lang_box = ttk.Combobox(bottom, values=("中文", "English"),
                                     state="readonly", width=8)
        self.lang_box.set("English" if get_language() == "en" else "中文")
        self.lang_box.grid(row=0, column=0, sticky="w")
        self.lang_box.bind("<<ComboboxSelected>>", self._on_language)
        ttk.Label(bottom, text="v%s" % VERSION, foreground=C_GRAY).grid(
            row=0, column=1, sticky="e")
        bottom.columnconfigure(1, weight=1)

        self._refresh_proxy_lamp()

    def _on_language(self, _event=None):
        code = "en" if self.lang_box.get() == "English" else "zh"
        if code == get_language():
            return
        if self._write_job is not None:
            self.root.after_cancel(self._write_job)
            self._flush_values()
        set_language(code)
        settings = load_settings()
        settings["language"] = code
        save_settings(settings)
        for job in self._flash_jobs.values():
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._flash_jobs.clear()
        for child in self.root.winfo_children():
            child.destroy()
        self._build()
        # 状态栏保存的是翻译好的成品字符串，切语言后要写一条新的，
        # 否则会一直显示切换前那种语言的最后一条消息。
        self.log(t("log_language", "English" if code == "en" else "中文"))

    # ── 交互 ─────────────────────────────────────────────────────
    def _current_value(self, key):
        return position_to_value(key, float(self.vars[key].get()))

    def _show_value(self, key):
        self.value_vars[key].set("%.2f" % self._current_value(key))

    def _on_slide(self, key):
        if self._syncing:
            return
        self._show_value(key)
        self._flash_entry(key, None)
        if self._write_job is not None:
            self.root.after_cancel(self._write_job)
        self._write_job = self.root.after(250, self._flush_values)

    def _focus_entry(self, key, widget):
        try:
            widget.selection_range(0, "end")
        except tk.TclError:
            pass

    def _cancel_entry(self, key):
        self._show_value(key)
        self._flash_entry(key, None)
        self.root.focus_set()

    def _commit_entry(self, key):
        label = t(LABEL_KEYS[key])
        raw = self.value_vars[key].get().strip()
        text = raw.replace("×", "").replace("x", "").replace("X", "")
        text = text.replace(",", ".").strip()

        # 没改过就只是失焦，不重复写文件。
        if text == "%.2f" % self._current_value(key):
            self._show_value(key)
            return

        try:
            value = float(text)
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError):
            self._show_value(key)
            self._flash_entry(key, C_RED)
            self.log(t("log_value_bad", label))
            return

        top = TOPS[key]
        clamped = max(0.0, min(top, value))

        self._syncing = True
        try:
            self.vars[key].set(value_to_position(key, clamped))
        finally:
            self._syncing = False
        self._show_value(key)

        if self._write_job is not None:
            self.root.after_cancel(self._write_job)
            self._write_job = None
        self._flush_values()

        # 保存日志先写，再把范围提示放到最后，保证底部先看到的是提示。
        if clamped != value:
            self._flash_entry(key, C_AMBER)
            if value > top:
                self.log(t("log_value_max", label, format_value(key, top)))
            else:
                self.log(t("log_value_min", label))
        else:
            self._flash_entry(key, None)

    def _flash_entry(self, key, color):
        """输入框闪一下颜色：红色=格式错误，琥珀=超出范围。"""
        entry = self.value_entries.get(key)
        if entry is None:
            return
        job = self._flash_jobs.pop(key, None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        try:
            entry.configure(foreground=color or "black")
        except tk.TclError:
            return
        if color is None:
            return

        def reset():
            self._flash_jobs.pop(key, None)
            widget = self.value_entries.get(key)
            if widget is not None:
                try:
                    widget.configure(foreground="black")
                except tk.TclError:
                    pass

        self._flash_jobs[key] = self.root.after(1800, reset)

    def _flush_values(self):
        self._write_job = None
        values = {key: round(position_to_value(key, float(var.get())), 2)
                  for key, var in self.vars.items()}
        try:
            save_values(config_path(), values)
        except Exception as exc:
            self.log(t("log_save_fail", exc))
            return
        self.log(t("log_saved", values["grip_road"], values["trigger_floor"],
                   values["throttle_floor"], values["trigger_event"],
                   values["understeer_gain"], values["loss_gain"]))

    def _start(self):
        kind, _path = proxy_state()
        if kind == "unknown":
            self.log(t("log_proxy_unknown_warn"))
        elif kind != "ours":
            self.log(t("log_proxy_missing"))
        self.runner.set_drive(True)
        self.log(t("log_starting"))

    def _stop(self):
        self.runner.set_drive(False)

    def _show_proxy_error(self, exc):
        messagebox.showerror(t(exc.title_key), t(exc.body_key, *exc.body_args))

    def _install(self):
        folder = acc_bin()
        if folder is None:
            messagebox.showerror(t("dlg_no_acc_title"), t("dlg_no_acc_short"))
            return
        if acc_running():
            messagebox.showwarning(t("dlg_running_title"), t("dlg_running_body"))
            return
        if not PROXY_SOURCE.exists() and not PROXY_LOCAL.exists():
            messagebox.showerror(t("dlg_missing_title"), t("dlg_missing_body"))
            return
        source = PROXY_SOURCE if PROXY_SOURCE.exists() else PROXY_LOCAL
        try:
            result = install_proxy(folder, source)
        except ProxyError as exc:
            self._show_proxy_error(exc)
            return
        except PermissionError:
            messagebox.showerror(t("dlg_denied_title"), t("dlg_denied_body"))
            return
        except Exception as exc:
            messagebox.showerror(t("dlg_copy_fail_title"), str(exc))
            return
        if result.get("backup_preserved"):
            self.log(t("log_backup_preserved",
                       str(Path(folder) / PROXY_BACKUP)))
        self.log(t("log_installed"))
        self._refresh_proxy_lamp()

    def _restore(self):
        folder = acc_bin()
        if folder is None:
            messagebox.showerror(t("dlg_no_acc_title"), t("dlg_no_acc_short"))
            return
        if acc_running():
            messagebox.showwarning(t("dlg_running_title"), t("dlg_running_body"))
            return
        try:
            result = restore_proxy(folder)
        except ProxyError as exc:
            self._show_proxy_error(exc)
            return
        except Exception as exc:
            messagebox.showerror(t("dlg_restore_fail_title"), str(exc))
            return
        if result["action"] == "nothing":
            self.log(t("log_nothing_to_restore"))
            self._refresh_proxy_lamp()
            return
        if result["action"] == "moved":
            self.log(t("log_restored_moved", result["target"],
                       result["kept_backup"]))
            messagebox.showinfo(
                t("dlg_backup_kept_title"),
                t("dlg_backup_kept_body", result["target"],
                  result["kept_backup"]))
        else:
            self.log(t("log_restored"))
        self._refresh_proxy_lamp()

    # ── 刷新 ─────────────────────────────────────────────────────
    def _refresh_proxy_lamp(self):
        kind, _path = proxy_state()
        if kind == "ours":
            self.lamps["proxy"].configure(text="●  " + t("lamp_proxy_on"),
                                          foreground=C_GREEN)
        elif kind == "unknown":
            self.lamps["proxy"].configure(text="?  " + t("lamp_proxy_unknown"),
                                          foreground=C_AMBER)
        else:
            self.lamps["proxy"].configure(text="○  " + t("lamp_proxy_off"),
                                          foreground=C_RED)

    def _tick(self):
        if self._closing:
            return
        state = self.runner.state()

        if state["driving"]:
            if state["pad"]:
                self.lamps["pad"].configure(text="●  " + t("lamp_pad_on"),
                                            foreground=C_GREEN)
            else:
                self.lamps["pad"].configure(text="○  " + t("lamp_pad_wait"),
                                            foreground=C_AMBER)
            self.lamps["drive"].configure(text="●  " + t("lamp_drive_on"),
                                          foreground=C_GREEN)
        else:
            self.lamps["pad"].configure(text="○  " + t("lamp_pad_off"),
                                        foreground=C_GRAY)
            self.lamps["drive"].configure(text="○  " + t("lamp_drive_off"),
                                          foreground=C_GRAY)

        if state["acc"]:
            self.lamps["acc"].configure(text="●  " + t("lamp_acc_on"),
                                        foreground=C_GREEN)
        else:
            self.lamps["acc"].configure(text="○  " + t("lamp_acc_off"),
                                        foreground=C_AMBER)

        self.start_button.configure(state="disabled" if state["driving"] else "normal")
        self.stop_button.configure(state="normal" if state["driving"] else "disabled")
        self._refresh_proxy_lamp()

        with self._guard:
            line = self._last_line
        self.status_label.configure(text=line)

        self.root.after(200, self._tick)

    # ── 退出 ─────────────────────────────────────────────────────
    def close(self):
        if self._closing:
            return
        self._closing = True
        try:
            self.runner.set_drive(False)
            self.runner.shutdown()
        except Exception:
            pass
        self.log(t("log_exit"))
        self.root.destroy()


def proxy_cli(argv):
    """给源码启动脚本用的小桥：复用面板同一套守卫，不创建窗口。

    只接收一个已定位的游戏目录；ACC 是否在跑、目录是否有效仍由
    .bat 和面板界面负责检查。这里只管代理文件和备份。
    """
    if len(argv) != 2 or argv[0] not in ("--install", "--restore"):
        print("Usage: python src/panel.py --install|--restore <ACC Win64 folder>")
        return 2

    action, folder = argv[0], Path(argv[1])
    if not folder.is_dir():
        print("Game folder not found: %s" % folder)
        return 1

    if action == "--install":
        source = PROXY_SOURCE if PROXY_SOURCE.exists() else PROXY_LOCAL
        try:
            result = install_proxy(folder, source)
        except ProxyError as exc:
            if exc.body_key == "dlg_missing_body":
                print("Proxy source DLL is missing: %s" % source)
            else:
                print("Refusing to install: the existing xinput1_3.dll is "
                      "not recognized as this project's proxy. Nothing was "
                      "changed.")
            return 1
        except Exception as exc:
            print("Install failed: %s" % exc)
            return 1
        if result.get("backup_preserved"):
            print("Existing backup kept untouched: %s"
                  % (folder / PROXY_BACKUP))
        print("Takeover installed: ACC rumble is now blocked.")
        return 0

    try:
        result = restore_proxy(folder)
    except ProxyError as exc:
        if exc.body_key == "dlg_rename_denied_body":
            print("Restore failed: the file is in use; make sure ACC is closed.")
        else:
            print("Refusing to restore: the xinput1_3.dll is not recognized "
                  "as this project's proxy. Nothing was changed.")
        return 1
    except Exception as exc:
        print("Restore failed: %s" % exc)
        return 1

    if result["action"] == "nothing":
        print("Nothing to restore.")
    elif result["action"] == "moved":
        print("Restored; current proxy moved to: %s" % result["target"])
        print("Existing backup kept untouched: %s" % result["kept_backup"])
    else:
        print("Restored: ACC's own rumble is back.")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--install", "--restore"):
        return proxy_cli(sys.argv[1:])

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    panel = Panel(root)
    if "--selftest" in sys.argv:
        root.after(1500, panel.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
