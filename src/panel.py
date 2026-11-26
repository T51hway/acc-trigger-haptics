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
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from acc_telemetry import AccTelemetry                     # noqa: E402
from haptics_engine import (                               # noqa: E402
    HapticsEngine, config_path, load_tuning, write_default_config,
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

# 滑块：(键, 文案键, 上限)。上限只是"拖到头"的位置，不改变程序里的合法范围。
SLIDERS = (
    ("grip_road", "sl_road", 4.00),
    ("trigger_floor", "sl_brake", 1.80),
    ("throttle_floor", "sl_throttle", 2.30),
    ("trigger_event", "sl_event", 1.00),
    ("understeer_gain", "sl_understeer", 1.50),
    ("loss_gain", "sl_loss", 1.50),
)

# 这三项是倍率：1.00 = 标准手感。界面和日志里数值后面加 ×，免得被当成绝对强度。
GAIN_KEYS = ("grip_road", "trigger_floor", "throttle_floor")


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


def proxy_installed():
    folder = acc_bin()
    return bool(folder and (folder / "xinput1_3.dll").exists())


def mtime_of(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def save_values(path, values):
    """只改等号右边的数字，行内注释原样保留。

    先写同目录的临时文件再整体替换：主程序每秒读一次这个文件，
    直接原地改可能让它读到半截，数值会闪回默认值。
    """
    if not path.exists():
        write_default_config(path)
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        pattern = re.compile(
            r"^(?P<head>[ \t]*%s[ \t]*=[ \t]*)(?P<num>[-+]?\d*\.?\d+)"
            r"(?P<tail>[ \t]*(?:#.*)?)$" % re.escape(key),
            re.MULTILINE,
        )

        def _replace(match, value=value):
            return "%s%.2f%s" % (match.group("head"), value, match.group("tail"))

        text, count = pattern.subn(_replace, text, count=1)
        if count == 0:
            text = text.rstrip("\n") + "\n%s = %.2f\n" % (key, value)

    tmp = path.with_name(path.name + ".tmp")
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
                        driver.set_vibration(*engine.update(snap, now=now))
                    except Exception as exc:
                        self._note(t("log_master_error", exc))

            with self._guard:
                self._state["acc"] = snap is not None
                self._state["pad"] = bool(driver is not None and driver.connected)
                self._state["driving"] = driver is not None

            # 手感文件热更新：面板自己写的也算，值一样，重载无害
            if now - last_config_check >= 1.0:
                last_config_check = now
                mtime = mtime_of(tuning_file)
                if mtime is not None and mtime != config_mtime:
                    config_mtime = mtime
                    engine.set_tuning(load_tuning(tuning_file))
                    self._log(t("log_reloaded"))

            next_tick += interval
            delay = next_tick - time.perf_counter()
            if delay > 0.0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()

        with self._guard:
            driver = self._driver
            self._driver = None
        if driver is not None:
            driver.stop()
        if self._telemetry is not None:
            self._telemetry.close()

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
        self.value_labels = {}
        sliders = ttk.LabelFrame(frame, text=t("group_tuning"), padding=8)
        sliders.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        for row, (key, label_key, top) in enumerate(SLIDERS):
            ttk.Label(sliders, text=t(label_key), width=13).grid(
                row=row, column=0, sticky="w", pady=3)
            var = tk.DoubleVar(value=float(tuning[key]))
            self.vars[key] = var
            ttk.Scale(sliders, from_=0.0, to=top, variable=var,
                      orient="horizontal", length=230,
                      command=lambda _v, k=key: self._on_slide(k)).grid(
                row=row, column=1, sticky="ew", padx=6)
            value = ttk.Label(sliders, text=format_value(key, float(tuning[key])),
                              width=6, anchor="e")
            value.grid(row=row, column=2, sticky="e")
            self.value_labels[key] = value
        sliders.columnconfigure(1, weight=1)

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
        for child in self.root.winfo_children():
            child.destroy()
        self._build()

    # ── 交互 ─────────────────────────────────────────────────────
    def _on_slide(self, key):
        value = self.vars[key].get()
        self.value_labels[key].configure(text=format_value(key, value))
        if self._write_job is not None:
            self.root.after_cancel(self._write_job)
        self._write_job = self.root.after(250, self._flush_values)

    def _flush_values(self):
        self._write_job = None
        values = {key: round(float(var.get()), 2) for key, var in self.vars.items()}
        try:
            save_values(config_path(), values)
        except Exception as exc:
            self.log(t("log_save_fail", exc))
            return
        self.log(t("log_saved", values["grip_road"], values["trigger_floor"],
                   values["throttle_floor"], values["trigger_event"],
                   values["understeer_gain"], values["loss_gain"]))

    def _start(self):
        if not proxy_installed():
            self.log(t("log_proxy_missing"))
        self.runner.set_drive(True)
        self.log(t("log_starting"))

    def _stop(self):
        self.runner.set_drive(False)

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
            shutil.copy2(source, folder / "xinput1_3.dll")
        except PermissionError:
            messagebox.showerror(t("dlg_denied_title"), t("dlg_denied_body"))
            return
        except Exception as exc:
            messagebox.showerror(t("dlg_copy_fail_title"), str(exc))
            return
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
        current = folder / "xinput1_3.dll"
        if not current.exists():
            self.log(t("log_nothing_to_restore"))
            self._refresh_proxy_lamp()
            return
        try:
            backup = folder / "xinput1_3.dll.off"
            if backup.exists():
                backup.unlink()
            current.rename(backup)
        except PermissionError:
            messagebox.showerror(t("dlg_rename_denied_title"),
                                 t("dlg_rename_denied_body"))
            return
        except Exception as exc:
            messagebox.showerror(t("dlg_restore_fail_title"), str(exc))
            return
        self.log(t("log_restored"))
        self._refresh_proxy_lamp()

    # ── 刷新 ─────────────────────────────────────────────────────
    def _refresh_proxy_lamp(self):
        if proxy_installed():
            self.lamps["proxy"].configure(text="●  " + t("lamp_proxy_on"),
                                          foreground=C_GREEN)
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


def main():
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
