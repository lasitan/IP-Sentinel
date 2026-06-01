#!/usr/bin/env python3
"""IP 信用净化：区域白名单无害流量注入."""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from config import require_config
from log_util import log_trust
from network import build_curl_context, http_status
from persona import load_lines, pick_session_ua
from session_stats import record_trust_session
from task_lock import acquire_trust_lock, release_trust_lock, trust_busy

REPO_RAW_URL = "https://raw.githubusercontent.com/lasitan/IP-Sentinel/main"
FALLBACK_URLS = [
    "https://en.wikipedia.org/wiki/Special:Random",
    "https://www.apple.com/",
    "https://www.microsoft.com/",
]

TRUST_HEADERS = [
    "Accept: text/html,application/xhtml+xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language: en-US,en;q=0.9",
    "Sec-Fetch-Dest: document",
    "Sec-Fetch-Mode: navigate",
    "Upgrade-Insecure-Requests: 1",
]

SUCCESS_CODE = re.compile(r"^(20[0-9]|30[1-8])$")


def _find_region_json(install: str, region: str) -> Path | None:
    regions_dir = Path(install) / "data" / "regions"
    if regions_dir.is_dir():
        for p in sorted(regions_dir.rglob("*.json")):
            return p
    legacy = regions_dir / f"{region}.json"
    return legacy if legacy.is_file() else None


def load_trust_urls(cfg: dict) -> list[str]:
    install = cfg["INSTALL_DIR"]
    region = cfg.get("REGION_CODE", "US")
    ip_pref = cfg.get("IP_PREF", "4")
    path = _find_region_json(install, region)

    if path is None or not path.is_file():
        path = Path(install) / "data" / "regions" / f"{region}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "curl",
                f"-{ip_pref}",
                "-sL",
                f"{REPO_RAW_URL}/data/regions/{region}.json",
                "-o",
                str(path),
            ],
            check=False,
            timeout=30,
        )

    urls: list[str] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            urls = list(data.get("trust_module", {}).get("white_urls", []) or [])
        except (json.JSONDecodeError, OSError):
            urls = []

    return urls if urls else list(FALLBACK_URLS)


def run(cfg: dict | None = None) -> int:
    cfg = cfg or require_config()
    if not acquire_trust_lock():
        _, holder = trust_busy()
        log_trust(cfg, "WARN ", f"信用净化进行中 (pid={holder})，跳过本次任务。")
        return 0

    try:
        return _run_locked(cfg)
    finally:
        release_trust_lock()


def _run_locked(cfg: dict) -> int:
    region = cfg.get("REGION_CODE", "US")

    def _log(level: str, msg: str) -> None:
        log_trust(cfg, level, msg)

    trust_urls = load_trust_urls(cfg)
    ua_file = Path(cfg["INSTALL_DIR"]) / "data" / "user_agents.txt"
    ua_pool = load_lines(ua_file) if ua_file.is_file() else []
    seed_ip = cfg.get("PUBLIC_IP") or cfg.get("BIND_IP") or "127.0.0.1"
    current_ua = pick_session_ua(ua_pool, str(seed_ip))

    _log("START", "========== 启动区域 IP 信用净化会话 ==========")
    _log("INFO ", f"已载入 [{region}] 区域白名单，配置库条目: {len(trust_urls)} 个")
    _log("INFO ", f"已锁定本地伪装指纹: {' '.join(current_ua.split()[:2])}...")

    ctx = build_curl_context(cfg, _log)
    step_count = random.randint(2, 4)
    success = 0

    for i in range(1, step_count + 1):
        target = random.choice(trust_urls)
        code = http_status(
            target,
            ctx,
            ua=current_ua,
            timeout=15,
            extra_headers=TRUST_HEADERS,
            compressed=True,
        )
        if SUCCESS_CODE.match(code):
            _log("EXEC ", f"动作[{i}/{step_count}]完成 | 状态: {code} | 注入: {target}")
            success += 1
        else:
            _log("EXEC ", f"动作[{i}/{step_count}]异常 | 状态: {code} | 阻拦: {target}")

        if i < step_count:
            sleep_time = random.randint(15, 35)
            _log("WAIT ", f"正在浏览本地高权重页面，模拟停留 {sleep_time} 秒...")
            time.sleep(sleep_time)

    if success >= step_count // 2:
        conclusion = f"✅ 信用净化完成 (已成功注入 {success} 条无害流量)"
        _log("SCORE", f"自检结论: {conclusion}")
    else:
        conclusion = "❌ 净化受阻 (部分站点拦截或网络超时)"
        _log("SCORE", f"自检结论: {conclusion}")

    record_trust_session(
        cfg,
        conclusion=conclusion,
        success_steps=success,
        total_steps=step_count,
    )

    _log("END  ", "========== 会话结束，释放进程 ==========")
    _log("INFO ", "系统级调度完毕，信任因子持续积累中...")
    return 0


def main() -> None:
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except Exception as exc:
        try:
            cfg = require_config()
            log_trust(cfg, "ERROR", f"信用净化未捕获异常: {exc}")
        except SystemExit:
            print(f"[Trust] FATAL: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
