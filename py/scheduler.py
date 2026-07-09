#!/usr/bin/env python3
"""内置任务调度器：替代外部 systemd/cron timer，在 agent_daemon 进程内长驻运行."""

from __future__ import annotations

import datetime
import hashlib
import threading

from agent_spawn import spawn_py_script
from config import load_config, save_config_keys
from log_util import log

_RUNNER_INTERVAL_SEC = 1200  # 20 分钟
_DAILY_CHECK_SEC = 30         # 每 30 秒扫描一次日历任务窗口


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _slog(cfg: dict, level: str, msg: str) -> None:
    log(cfg, "Sched", level, msg)


def _resolve_updater_time(cfg: dict) -> tuple[int, int]:
    """读取 updater 每日触发 UTC 时刻；若未配置则按节点名派生确定性随机值并持久化."""
    try:
        uh = int(cfg["UPDATER_UTC_HOUR"])
        um = int(cfg["UPDATER_UTC_MIN"])
        return uh % 24, um % 60
    except (KeyError, ValueError, TypeError):
        pass

    seed = int(hashlib.md5(str(cfg.get("NODE_NAME", "node")).encode()).hexdigest()[:8], 16)
    uh = seed % 24
    um = (seed >> 8) % 60
    save_config_keys({"UPDATER_UTC_HOUR": str(uh), "UPDATER_UTC_MIN": str(um)})
    cfg["UPDATER_UTC_HOUR"] = str(uh)
    cfg["UPDATER_UTC_MIN"] = str(um)
    return uh, um


class InternalScheduler:
    """长驻调度器：管理 runner / updater / report / quality 的周期性触发."""

    def __init__(self) -> None:
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(
            target=self._runner_loop, daemon=True, name="sched-runner"
        ).start()
        threading.Thread(
            target=self._daily_loop, daemon=True, name="sched-daily"
        ).start()

    def stop(self) -> None:
        self._stop.set()

    # ── runner：每 20 分钟触发一次 ──────────────────────────────────────────
    def _runner_loop(self) -> None:
        # 启动后等待一个完整周期，避免重启后立即重复执行上一次任务
        self._stop.wait(_RUNNER_INTERVAL_SEC)
        while not self._stop.is_set():
            self._spawn("runner.py", "定时巡逻")
            self._stop.wait(_RUNNER_INTERVAL_SEC)

    # ── 每日任务：updater + report + quality ────────────────────────────────
    def _daily_loop(self) -> None:
        last_updater: datetime.date | None = None
        last_report: datetime.date | None = None
        last_quality: datetime.date | None = None

        while not self._stop.is_set():
            self._stop.wait(_DAILY_CHECK_SEC)
            if self._stop.is_set():
                break

            now = _now_utc()
            today = now.date()
            cfg = load_config()
            if not cfg:
                continue

            uh, um = _resolve_updater_time(cfg)

            if today != last_updater and now.hour == uh and now.minute == um:
                last_updater = today
                self._spawn("updater.py", "每日数据 OTA")

            if today != last_report and now.hour == 16 and now.minute == 0:
                last_report = today
                self._spawn("report.py", "每日 Telegram 日报")

            # 每日 UTC 00:00 跑一次 IP 污染检测并自动入库
            if today != last_quality and now.hour == 0 and now.minute == 0:
                last_quality = today
                self._spawn("mod_quality.py", "每日 IP 污染检测")

    def _spawn(self, script: str, label: str, *, extra_env: dict[str, str] | None = None) -> None:
        cfg = load_config()
        if not cfg:
            return
        _slog(cfg, "INFO ", f"调度触发: {label} ({script})")
        spawn_py_script(script, log_module="Sched", extra_env=extra_env)


def start_scheduler() -> InternalScheduler:
    """启动内置调度器，返回调度器实例."""
    sched = InternalScheduler()
    sched.start()
    return sched
