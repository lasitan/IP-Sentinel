#!/usr/bin/env python3
"""Google 区域纠偏：行为模拟 + 三核 Geo 自检."""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

from config import require_config
from geo_probe import (
    parse_jump_gl,
    parse_yt_music_gl,
    parse_yt_premium_gl,
    score_geo_status,
    target_country_code,
)
from log_util import log
from maps_browser import (
    maps_geo_enabled,
    parse_lang_locale,
    visit_google_earth,
    visit_google_maps,
)
from network import build_curl_context, fetch_headers, fetch_text, http_status
from persona import load_lines, pick_browser_ua, pick_session_ua, random_coord, uri_encode_keyword
from session_stats import record_google_session
from task_lock import acquire_browser_lock, browser_busy, release_browser_lock

MODULE = "Google"

# 单次纠偏预算：避免 Playwright 占锁过久阻塞定时任务与质量统计
_SESSION_BUDGET_SEC = 360
_ACTIONS_MIN = 4
_ACTIONS_MAX = 6
_SLEEP_MIN = 8
_SLEEP_MAX = 18
_MAPS_DWELL_SEC = 12
_EARTH_DWELL_SEC = 18
_MAX_MAPS_BROWSER = 3   # Maps 浏览器偏移次数上限；每次 Maps 偏移成功后立即跟一次 Earth 偏移
_MAX_EARTH_BROWSER = 3  # Earth 跟随 Maps，两者次数保持一致


def run(cfg: dict | None = None) -> int:
    cfg = cfg or require_config()
    if not acquire_browser_lock():
        _, holder = browser_busy()
        log(
            cfg,
            MODULE,
            "WARN ",
            f"Google 纠偏进行中 (pid={holder})，跳过本次任务。",
        )
        return 0

    try:
        return _run_locked(cfg)
    finally:
        release_browser_lock()


def _run_locked(cfg: dict) -> int:
    install = cfg["INSTALL_DIR"]
    region_name = cfg.get("REGION_NAME", cfg.get("REGION_CODE", ""))

    log(cfg, MODULE, "START", f"========== 唤醒网络模拟器 [区域: {region_name}] ==========")

    ua_file = Path(install) / "data" / "user_agents.txt"
    kw_file = Path(install) / "data" / "keywords" / f"kw_{cfg.get('REGION_CODE', 'US')}.txt"
    if not ua_file.is_file() or not kw_file.is_file():
        log(cfg, MODULE, "ERROR", "热数据缺失，请检查 data 目录。放弃本次执行。")
        return 1

    ua_pool = load_lines(ua_file)
    keywords = load_lines(kw_file)
    current_ip = cfg.get("PUBLIC_IP") or cfg.get("BIND_IP") or "Unknown"
    session_ua = pick_session_ua(ua_pool, str(current_ip))
    browser_ua = pick_browser_ua(ua_pool, str(current_ip))

    base_lat = float(cfg.get("BASE_LAT", 0))
    base_lon = float(cfg.get("BASE_LON", 0))
    session_lat = random_coord(base_lat, 270)
    session_lon = random_coord(base_lon, 270)
    total_actions = random.randint(_ACTIONS_MIN, _ACTIONS_MAX)
    lang = cfg.get("LANG_PARAMS", "hl=en&gl=US")
    maps_geo_mode = maps_geo_enabled(cfg)
    maps_locale = parse_lang_locale(lang)
    maps_geo_visits = 0
    earth_geo_visits = 0
    maps_browser_left = _MAX_MAPS_BROWSER
    earth_browser_left = _MAX_EARTH_BROWSER
    session_start = time.monotonic()
    actions_done = 0

    log(cfg, MODULE, "INFO ", f"当前出网 IP: {current_ip}")
    log(cfg, MODULE, "INFO ", f"设备指纹锁定: {session_ua[:45]}...")
    if browser_ua != session_ua:
        log(cfg, MODULE, "INFO ", f"浏览器 UA (桌面): {browser_ua[:45]}...")
    log(
        cfg,
        MODULE,
        "INFO ",
        f"虚拟驻留坐标 (会话基准): {session_lat}, {session_lon} | 基准点: {base_lat}, {base_lon}",
    )

    def _log(level: str, msg: str) -> None:
        log(cfg, MODULE, level, msg)

    ctx = build_curl_context(cfg, _log)

    def _run_earth_geo(lat: float, lon: float, phase: str) -> int:
        nonlocal earth_geo_visits
        if maps_geo_mode not in ("true", "auto"):
            return 0
        log(
            cfg,
            MODULE,
            "INFO ",
            f"Earth 动作 ({phase}) | 会话虚拟坐标: {lat}, {lon}",
        )
        result = visit_google_earth(
            latitude=lat,
            longitude=lon,
            user_agent=browser_ua,
            locale=maps_locale,
            dwell_sec=_EARTH_DWELL_SEC,
            log=_log,
        )
        if result == "ok":
            earth_geo_visits += 1
            return 200
        log(cfg, MODULE, "WARN ", f"Earth 浏览器访问失败 ({result})")
        return 0

    log(
        cfg,
        MODULE,
        "INFO ",
        f"会话预算: {_SESSION_BUDGET_SEC}s | 动作 {total_actions} 次 | "
        f"Maps 偏移≤{maps_browser_left} 次 (Earth 跟随)",
    )

    for i in range(1, total_actions + 1):
        elapsed = time.monotonic() - session_start
        if elapsed >= _SESSION_BUDGET_SEC:
            log(
                cfg,
                MODULE,
                "WARN ",
                f"已达会话时间上限 ({int(elapsed)}s)，提前结束动作循环。",
            )
            break

        if maps_geo_visits >= _MAX_MAPS_BROWSER:
            log(
                cfg,
                MODULE,
                "INFO ",
                f"已完成 {_MAX_MAPS_BROWSER} 次 Maps+Earth 偏移对，结束本次任务。",
            )
            break

        action_lat = random_coord(session_lat, 1)
        action_lon = random_coord(session_lon, 1)
        keyword = random.choice(keywords)
        encoded = uri_encode_keyword(keyword)
        action_type = random.randint(1, 5)
        log(
            cfg,
            MODULE,
            "INFO ",
            f"动作 [{i}/{total_actions}] 虚拟坐标: {action_lat}, {action_lon}",
        )

        if action_type == 1:
            url = f"https://www.google.com/search?q={encoded}&{lang}"
            code = http_status(url, ctx, ua=session_ua, follow=True, timeout=15)
        elif action_type == 2:
            url = f"https://news.google.com/home?{lang}"
            code = http_status(url, ctx, ua=session_ua, follow=True, timeout=15)
        elif action_type == 3:
            url = (
                f"https://www.google.com/maps/search/{encoded}/"
                f"@{action_lat},{action_lon},17z?{lang}"
            )
            code = 0
            log(
                cfg,
                MODULE,
                "INFO ",
                f"Maps 动作 | 搜索虚拟坐标: {action_lat}, {action_lon} | 关键词: {keyword[:40]}",
            )
            if maps_geo_mode in ("true", "auto") and maps_browser_left > 0:
                maps_browser_left -= 1
                geo_result = visit_google_maps(
                    maps_url=url,
                    latitude=action_lat,
                    longitude=action_lon,
                    user_agent=browser_ua,
                    locale=maps_locale,
                    dwell_sec=_MAPS_DWELL_SEC,
                    log=_log,
                )
                if geo_result == "ok":
                    code = 200
                    maps_geo_visits += 1
                    # Earth 跟随 Maps：立即以相同坐标执行 Earth 偏移
                    if earth_browser_left > 0:
                        earth_browser_left -= 1
                        _run_earth_geo(action_lat, action_lon, f"跟随 Maps 第 {maps_geo_visits} 次")
                else:
                    log(cfg, MODULE, "WARN ", f"Maps 浏览器访问失败 ({geo_result})，回退为 HTTP。")
            else:
                code = http_status(url, ctx, ua=session_ua, follow=False, timeout=15)

            if code != 200 and maps_geo_mode == "auto":
                code = http_status(url, ctx, ua=session_ua, follow=False, timeout=15)
        elif action_type == 5:
            # Earth 不再独立触发浏览器偏移（已跟随 Maps），此处仅保留 HTTP 探测
            url = "https://earth.google.com/"
            code = http_status(url, ctx, ua=session_ua, follow=True, timeout=15)
        else:
            url = "https://connectivitycheck.gstatic.com/generate_204"
            code = http_status(url, ctx, ua=session_ua, follow=False, timeout=10)

        actions_done = i
        log(
            cfg,
            MODULE,
            "EXEC ",
            f"动作[{i}/{total_actions}]完成 | HTTP状态: {code} | 抖动坐标: {action_lat}, {action_lon}",
        )

        if maps_geo_visits >= _MAX_MAPS_BROWSER:
            log(
                cfg,
                MODULE,
                "INFO ",
                f"已完成 {_MAX_MAPS_BROWSER} 次 Maps+Earth 偏移对，立即进入自检。",
            )
            break

        if i < total_actions and time.monotonic() - session_start < _SESSION_BUDGET_SEC:
            sleep_time = random.randint(_SLEEP_MIN, _SLEEP_MAX)
            remaining = _SESSION_BUDGET_SEC - (time.monotonic() - session_start)
            sleep_time = min(sleep_time, max(0, int(remaining) - 30))
            if sleep_time > 0:
                log(cfg, MODULE, "WAIT ", f"模拟停留 {sleep_time} 秒...")
                time.sleep(sleep_time)

    log(cfg, MODULE, "INFO ", "启动三核交叉验证 (URL跳转 + YT Premium + YT Music) 穿透获取 GeoIP...")

    jump_hdr = fetch_headers("http://www.google.com/", ctx, timeout=10)
    jump_gl = parse_jump_gl(jump_hdr)

    yt_pr_html = fetch_text("https://www.youtube.com/premium", ctx, ua=session_ua, timeout=10)
    yt_pr_gl = parse_yt_premium_gl(yt_pr_html)

    yt_mu_html = fetch_text("https://music.youtube.com/", ctx, ua=session_ua, timeout=10)
    yt_mu_gl = parse_yt_music_gl(yt_mu_html)

    target_cc = target_country_code(cfg.get("REGION_CODE", "US"))
    status = score_geo_status(jump_gl, yt_pr_gl, yt_mu_gl, target_cc)

    log(cfg, MODULE, "SCORE", f"自检结论: {status}")
    log(cfg, MODULE, "INFO ", f"本次会话 Maps 虚拟定位访问: {maps_geo_visits} 次")
    log(cfg, MODULE, "INFO ", f"本次会话 Earth 虚拟定位访问: {earth_geo_visits} 次")
    record_google_session(
        cfg,
        conclusion=status,
        maps_visits=maps_geo_visits,
        earth_visits=earth_geo_visits,
        actions_done=actions_done,
        jump_gl=jump_gl,
        yt_premium_gl=yt_pr_gl,
        yt_music_gl=yt_mu_gl,
    )
    log(cfg, MODULE, "END  ", "========== 会话结束，释放进程 ==========")
    return 0


def main() -> None:
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except Exception as exc:
        try:
            cfg = require_config()
            log(cfg, MODULE, "ERROR", f"Google 纠偏未捕获异常: {exc}")
        except SystemExit:
            print(f"[Google] FATAL: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
