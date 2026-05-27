#!/usr/bin/env python3
"""数据 OTA：UA 库、关键词、区域 JSON、探针脚本与日志清理."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from config import require_config
from log_util import LOG_RETENTION_DAYS, log, prune_log_file
from network import build_curl_context

REPO_RAW_URL = "https://raw.githubusercontent.com/lasitan/IP-Sentinel/main"
UA_COOLDOWN_SEC = 2592000  # 30 天
def _curl_download(ctx, url: str, dest: Path, timeout: int = 60) -> bool:
    cmd = ["curl", *ctx.bind_opt, ctx.ip_flag, "-fsSL", "-m", str(timeout), url, "-o", str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
        return r.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def run() -> int:
    cfg = require_config()
    install = Path(cfg["INSTALL_DIR"])
    core = install / "core"
    core.mkdir(parents=True, exist_ok=True)
    data = install / "data"
    ua_time_file = core / ".ua_last_update"

    def _log(level: str, msg: str) -> None:
        log(cfg, "Updater", level, msg)

    _log("INFO ", "========== 触发后台静默 OTA 热数据更新 ==========")
    ctx = build_curl_context(cfg, _log)

    now = int(time.time())
    last = 0
    if ua_time_file.is_file():
        try:
            last = int(ua_time_file.read_text(encoding="utf-8").strip())
        except ValueError:
            last = 0

    if now - last >= UA_COOLDOWN_SEC or last == 0:
        tmp_ua = Path("/tmp/ip_sentinel_ua.txt")
        if _curl_download(ctx, f"{REPO_RAW_URL}/data/user_agents.txt", tmp_ua):
            (data / "user_agents.txt").parent.mkdir(parents=True, exist_ok=True)
            tmp_ua.replace(data / "user_agents.txt")
            ua_time_file.write_text(str(now), encoding="utf-8")
            _log("INFO ", "✅ 设备指纹池 (User-Agents) 30天错峰滚动更新成功")
        else:
            tmp_ua.unlink(missing_ok=True)
            _log("WARN ", "❌ UA 池拉取失败，保留本地旧数据防崩溃")
    else:
        days_left = (UA_COOLDOWN_SEC - (now - last)) // 86400
        _log("INFO ", f"⏳ 设备指纹池处于 30 天静默期 (剩余约 {days_left} 天)，跳过拉取")

    region = cfg.get("REGION_CODE", "US")
    tmp_kw = Path("/tmp/ip_sentinel_kw.txt")
    if _curl_download(ctx, f"{REPO_RAW_URL}/data/keywords/kw_{region}.txt", tmp_kw):
        (data / "keywords").mkdir(parents=True, exist_ok=True)
        tmp_kw.replace(data / "keywords" / f"kw_{region}.txt")
        _log("INFO ", f"✅ 区域搜索词库 (kw_{region}) 每日同步成功")
    else:
        tmp_kw.unlink(missing_ok=True)
        _log("WARN ", "❌ 搜索词库拉取失败，保留本地旧数据防崩溃")

    regions = list((data / "regions").rglob("*.json")) if (data / "regions").is_dir() else []
    if regions:
        region_json = regions[0]
        rel = region_json.relative_to(install).as_posix()
        tmp_json = Path("/tmp/ip_sentinel_region.json")
        if _curl_download(ctx, f"{REPO_RAW_URL}/{rel}", tmp_json):
            tmp_json.replace(region_json)
            _log("INFO ", f"✅ 区域规则 ({rel}) 同步成功")
        else:
            tmp_json.unlink(missing_ok=True)
            _log("WARN ", "❌ 区域规则下载失败，保留本地旧数据")

    tmp_probe = Path("/tmp/ip_sentinel_probe.sh")
    if _curl_download(ctx, "https://raw.githubusercontent.com/xykt/IPQuality/main/ip.sh", tmp_probe):
        text = tmp_probe.read_text(encoding="utf-8", errors="ignore")
        if "xykt" in text:
            dest = core / "ip_probe.sh"
            tmp_probe.replace(dest)
            dest.chmod(0o755)
            _log("INFO ", "✅ ip_probe.sh 已更新")
        else:
            tmp_probe.unlink(missing_ok=True)
            _log("WARN ", "❌ ip_probe.sh 下载失败，保留本地旧版本")
    else:
        tmp_probe.unlink(missing_ok=True)
        _log("WARN ", "❌ 探针源文件拉取失败，保留本地旧版本")

    log_file = Path(cfg.get("LOG_FILE", install / "logs" / "sentinel.log"))
    removed = prune_log_file(log_file, keep_days=LOG_RETENTION_DAYS)
    if removed > 0:
        _log("INFO ", f"🧹 系统日志已清理 (保留最近 {LOG_RETENTION_DAYS} 天，删除 {removed} 行)")

    _log("INFO ", "========== OTA 养料注入与系统维护结束 ==========")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
