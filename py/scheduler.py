#!/usr/bin/env python3
"""内置任务调度器：替代外部 systemd/cron timer，在 agent_daemon 进程内长驻运行."""

from __future__ import annotations

import datetime
import hashlib
import shutil
import subprocess
import threading
from pathlib import Path

from agent_spawn import spawn_py_script
from config import load_config, save_config_keys
from log_util import log

_RUNNER_INTERVAL_SEC = 1200  # 20 分钟
_DAILY_CHECK_SEC = 30         # 每 30 秒扫描一次日历任务窗口


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _slog(cfg: dict, level: str, msg: str) -> None:
    log(cfg, "Sched", level, msg)


# ═══════════════════════════════════════════════════════════════════════════════
# MIGRATE v4.5.7 — 清理旧 systemd timer 单元与 cron 条目
# 下一版本更新时删除以下整块迁移代码（_migrate_* 函数和 run_migration 函数）
# ═══════════════════════════════════════════════════════════════════════════════

def _migrate_remove_legacy_systemd() -> None:
    """停用并删除旧 systemd timer/service 单元."""
    if not shutil.which("systemctl"):
        return

    old_units = [
        "ip-sentinel-runner.timer",
        "ip-sentinel-runner.service",
        "ip-sentinel-updater.timer",
        "ip-sentinel-updater.service",
        "ip-sentinel-report.timer",
        "ip-sentinel-report.service",
    ]
    any_removed = False
    for unit in old_units:
        subprocess.run(
            ["systemctl", "disable", "--now", unit],
            capture_output=True,
            check=False,
        )
        unit_path = Path(f"/etc/systemd/system/{unit}")
        if unit_path.exists():
            unit_path.unlink(missing_ok=True)
            any_removed = True

    if any_removed:
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, check=False)


def _migrate_remove_legacy_cron() -> None:
    """清除 crontab 中旧 runner/updater/report/cron_task 条目."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False
        )
        lines = result.stdout.splitlines()
        keep = [
            ln
            for ln in lines
            if "runner.py" not in ln
            and "updater.py" not in ln
            and "report.py" not in ln
            and "cron_task.sh" not in ln
        ]
        if len(keep) < len(lines):
            new_cron = "\n".join(keep) + ("\n" if keep else "")
            subprocess.run(
                ["crontab", "-"],
                input=new_cron,
                text=True,
                capture_output=True,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        pass


def run_migration() -> None:
    """执行一次性迁移：移除旧外部调度守护。下一版本删除此函数."""
    _migrate_remove_legacy_systemd()
    _migrate_remove_legacy_cron()

# ═══════════════════════════════════════════════════════════════════════════════
# 迁移代码结束
# ═══════════════════════════════════════════════════════════════════════════════


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
    """长驻调度器：管理 runner / updater / report 的周期性触发."""

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

    # ── 每日任务：updater + report ──────────────────────────────────────────
    def _daily_loop(self) -> None:
        last_updater: datetime.date | None = None
        last_report: datetime.date | None = None

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

    def _spawn(self, script: str, label: str) -> None:
        cfg = load_config()
        if not cfg:
            return
        _slog(cfg, "INFO ", f"调度触发: {label} ({script})")
        spawn_py_script(script, log_module="Sched")


def start_scheduler() -> InternalScheduler:
    """执行迁移并启动内置调度器，返回调度器实例."""
    run_migration()
    sched = InternalScheduler()
    sched.start()
    return sched
