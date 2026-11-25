# -*- coding: utf-8 -*-
"""主循环：把遥测、判断、马达三层接起来。

一层一层看：
    acc_telemetry.latest()   读一块共享内存，给一份最新快照
    haptics_engine.update()  把快照算成四个马达值
    pad_driver.set_vibration 交给 60Hz 的写入线程

这个文件只做"接"和"兜底"，不掺任何判断逻辑：
    - 有数据：正常算、正常写
    - 没数据（ACC 没开、在菜单、退出了）：立刻清零，并把引擎的状态清掉
    - Ctrl+C / 异常：退出时清零（pad_driver 里还有 atexit 和控制台关闭兜底）

前置条件（都满足才有意义）：
    1. 已经运行过 开启接管.bat（ACC 的震动写入被代理 DLL 挡住）
    2. Steam 输入已关闭
    3. 手柄蓝牙已连接，ACC 已进入赛道

用法：
    python src/main.py
    python src/main.py --status        # 每秒打一行状态，联调时用
    python src/main.py --seconds 30    # 跑 30 秒自动停，自测用

只用 Python 标准库。
"""

import argparse
import sys
import time

from acc_telemetry import AccTelemetry
from haptics_engine import HapticsEngine, config_path, load_tuning
from i18n import set_language, system_language, t
from pad_driver import PadBusy, PadDriver, PadError
from version import VERSION


def _file_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _source_names(trace, key):
    items = trace.get(key) or []
    if not items:
        return t("cli_none")
    return "+".join(name for name, _amplitude in items)


def _tuning_text(tuning):
    return t("cfg_tuning_text", tuning["grip_road"], tuning["trigger_floor"],
             tuning["throttle_floor"], tuning["trigger_event"],
             tuning["understeer_gain"])


def _status_line(snap, motors, engine):
    trace = engine.last_trace
    metrics = engine.last_metrics or {}
    abs_on = 1 if (snap.abs_level > 0.05 or abs(snap.abs_vibrations) > 0.05) else 0
    return t("cli_status", snap.packet_id, snap.speed_kmh, snap.gear_label,
             snap.brake, snap.throttle, abs_on,
             motors.left_trigger * 100.0, motors.right_trigger * 100.0,
             metrics.get("grip_peak_left", 0.0) * 100.0,
             metrics.get("grip_peak_right", 0.0) * 100.0,
             _source_names(trace, "left_trigger"),
             _source_names(trace, "right_trigger"))


def main():
    set_language(system_language())
    parser = argparse.ArgumentParser(description=t("cli_description"))
    parser.add_argument("--hz", type=float, default=60.0,
                        help=t("cli_hz_help"))
    parser.add_argument("--status", action="store_true",
                        help=t("cli_status_help"))
    parser.add_argument("--seconds", type=float, default=0.0,
                        help=t("cli_seconds_help"))
    args = parser.parse_args()

    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("ACC Trigger Haptics · v%s" % VERSION)
    print("=" * 62)
    print(t("cli_preflight"))
    print(t("cli_preflight_1"))
    print(t("cli_preflight_2"))
    print(t("cli_preflight_3"))
    print(t("cli_ctrl_c"))
    print()

    telemetry = AccTelemetry()
    tuning_file = config_path()
    tuning = load_tuning(tuning_file)
    engine = HapticsEngine(tuning)
    if tuning_file.exists():
        print(t("cli_settings", tuning_file.name))
    else:
        print(t("cli_settings_default"))
    print("           %s" % _tuning_text(engine.tuning))
    print()

    driver = PadDriver()

    try:
        driver.start()
    except PadBusy as exc:
        print(exc)
        return 1
    except PadError as exc:
        print(t("cli_driver_fail", exc))
        return 1
    except Exception as exc:
        print(t("cli_driver_fail_detail", type(exc).__name__, exc))
        return 1

    interval = 1.0 / max(args.hz, 1.0)
    next_tick = time.perf_counter()
    started = next_tick
    have_signal = False
    last_status = 0.0
    exit_code = 0
    config_mtime = _file_mtime(tuning_file)
    last_config_check = 0.0

    try:
        while True:
            snap = telemetry.latest()
            now = time.perf_counter()

            if now - last_config_check >= 1.0:
                last_config_check = now
                mtime = _file_mtime(tuning_file)
                if mtime is not None and mtime != config_mtime:
                    config_mtime = mtime
                    engine.set_tuning(load_tuning(tuning_file))
                    print(t("cfg_updated", _tuning_text(engine.tuning)),
                          flush=True)

            if snap is None:
                # 没数据就不要喂旧快照。清零一次，引擎的包络也一起清掉。
                if have_signal:
                    have_signal = False
                    engine.reset()
                    driver.set_vibration()
            else:
                have_signal = True
                motors = engine.update(snap, now=now)
                driver.set_vibration(*motors)
                if args.status and now - last_status >= 1.0:
                    last_status = now
                    print(_status_line(snap, motors, engine), flush=True)

            if args.seconds and now - started >= args.seconds:
                print()
                print(t("cli_selftest_done"))
                break

            next_tick += interval
            delay = next_tick - time.perf_counter()
            if delay > 0.0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()   # 落后了就不追帧
    except KeyboardInterrupt:
        print()
        print(t("cli_interrupted"))
    except Exception as exc:
        print()
        print(t("cli_loop_error", type(exc).__name__, exc))
        print(t("cli_report"))
        exit_code = 1
    finally:
        driver.stop()
        telemetry.close()

    print(t("cli_exit"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
