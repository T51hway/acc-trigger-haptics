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
from haptics_engine import (
    HapticsEngine, config_path, load_tuning_result,
)
from i18n import set_language, system_language, t
from pad_driver import PadBusy, PadDriver, PadError
from version import VERSION


def _file_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_tuning(path, fallback):
    """读手感设置，返回 (ok, tuning, 提示文本)。

    提示交给 load_tuning_result 的 report 回调收集，不去重定向 stdout。
    热更新失败后会每秒重试，提示由调用方只在状态变化时说一次；
    否则一个一直写坏的文件会每秒刷一行同样的错误。
    """
    notes = []
    ok, tuning = load_tuning_result(
        path, fallback=fallback, report=notes.append)
    return ok, tuning, "\n".join(notes)


def _source_names(trace, key):
    items = trace.get(key) or []
    if not items:
        return t("cli_none")
    return "+".join(name for name, _amplitude in items)


def _tuning_text(tuning):
    return t("cfg_tuning_text", tuning["grip_road"], tuning["trigger_floor"],
             tuning["throttle_floor"], tuning["trigger_event"],
             tuning["understeer_gain"], tuning["loss_gain"])


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
    # 先记时间戳再读文件：读的瞬间文件可能刚被改过，宁可下一轮多读一次，
    # 也不能把没读到的版本当成已加载。
    config_mtime = _file_mtime(tuning_file)
    initial_ok, tuning, initial_detail = _read_tuning(tuning_file, None)
    if initial_detail:
        print(initial_detail)
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
    reload_failed = not initial_ok
    last_config_check = 0.0

    try:
        while True:
            snap = telemetry.latest()
            now = time.perf_counter()

            if now - last_config_check >= 1.0:
                last_config_check = now
                mtime = _file_mtime(tuning_file)
                # 上次读失败就不看 mtime，下一轮接着重试：用户可能原地修好
                # 文件但 mtime 没变（同秒写入、复制保留时间戳）。
                if reload_failed or mtime != config_mtime:
                    ok, tuning, detail = _read_tuning(
                        tuning_file, engine.tuning)
                    if ok:
                        config_mtime = mtime
                        reload_failed = False
                        engine.set_tuning(tuning)
                        if detail:
                            print(detail, flush=True)
                        print(t("cfg_updated", _tuning_text(engine.tuning)),
                              flush=True)
                    elif not reload_failed:
                        # 一直坏着的文件只提示一次，重试过程保持安静。
                        reload_failed = True
                        if detail:
                            print(detail, flush=True)

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
