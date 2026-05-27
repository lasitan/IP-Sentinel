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
from maps_browser import maps_geo_enabled, parse_lang_locale, visit_google_maps
from network import build_curl_context, fetch_headers, fetch_text, http_status
from persona import load_lines, pick_session_ua, random_coord, uri_encode_keyword

MODULE = "Google"


def run(cfg: dict | None = None) -> int:
    cfg = cfg or require_config()
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

    base_lat = float(cfg.get("BASE_LAT", 0))
    base_lon = float(cfg.get("BASE_LON", 0))
    session_lat = random_coord(base_lat, 270)
    session_lon = random_coord(base_lon, 270)
    total_actions = random.randint(5, 8)
    lang = cfg.get("LANG_PARAMS", "hl=en&gl=US")
    maps_geo_mode = maps_geo_enabled(cfg)
    maps_locale = parse_lang_locale(lang)

    log(cfg, MODULE, "INFO ", f"当前出网 IP: {current_ip}")
    log(cfg, MODULE, "INFO ", f"设备指纹锁定: {session_ua[:45]}...")
    log(cfg, MODULE, "INFO ", f"虚拟驻留坐标: {session_lat}, {session_lon}")

    def _log(level: str, msg: str) -> None:
        log(cfg, MODULE, level, msg)

    ctx = build_curl_context(cfg, _log)

    for i in range(1, total_actions + 1):
        action_lat = random_coord(session_lat, 1)
        action_lon = random_coord(session_lon, 1)
        keyword = random.choice(keywords)
        encoded = uri_encode_keyword(keyword)
        action_type = random.randint(1, 4)

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
            if maps_geo_mode in ("true", "auto"):
                geo_result = visit_google_maps(
                    maps_url=url,
                    latitude=action_lat,
                    longitude=action_lon,
                    user_agent=session_ua,
                    locale=maps_locale,
                    log=_log,
                )
                if geo_result == "ok":
                    code = 200
                    log(
                        cfg,
                        MODULE,
                        "INFO ",
                        f"Maps 已用 Chromium Geolocation 定位至搜索坐标: {action_lat}, {action_lon}",
                    )
                else:
                    log(cfg, MODULE, "WARN ", f"Maps 浏览器访问失败 ({geo_result})，回退为 HTTP。")

            if code != 200:
                if maps_geo_mode == "true":
                    log(cfg, MODULE, "ERROR", f"Maps 浏览器定位失败 ({geo_result})，已跳过 HTTP 回退。")
                else:
                    code = http_status(url, ctx, ua=session_ua, follow=False, timeout=15)
        else:
            url = "https://connectivitycheck.gstatic.com/generate_204"
            code = http_status(url, ctx, ua=session_ua, follow=False, timeout=10)

        log(
            cfg,
            MODULE,
            "EXEC ",
            f"动作[{i}/{total_actions}]完成 | HTTP状态: {code} | 抖动坐标: {action_lat}, {action_lon}",
        )

        if i < total_actions:
            sleep_time = random.randint(45, 75)
            log(cfg, MODULE, "WAIT ", f"阅读当前页面内容，模拟停留 {sleep_time} 秒...")
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
    log(cfg, MODULE, "END  ", "========== 会话结束，释放进程 ==========")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
