"""Google Maps：Chromium + Geolocation API 覆写（与搜索 URL 坐标一致）."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

LogFn = Callable[[str, str], None]


def parse_lang_locale(lang_params: str) -> str:
    hl, gl = "en", "US"
    for part in lang_params.split("&"):
        if part.startswith("hl="):
            hl = part[3:].strip() or hl
        elif part.startswith("gl="):
            gl = part[3:].strip() or gl
    if hl == "zh":
        return f"zh-{gl}" if len(gl) == 2 else "zh-CN"
    return f"{hl}-{gl}" if len(gl) == 2 else hl


def visit_google_maps(
    *,
    maps_url: str,
    latitude: float,
    longitude: float,
    user_agent: str,
    locale: str = "en-US",
    dwell_sec: int | None = None,
    log: LogFn | None = None,
) -> str:
    """
    使用 Chromium 打开 Maps，并通过浏览器 Geolocation 返回指定经纬度（非 GPS 真值）。

    返回: ok | skip (未安装 playwright/浏览器) | error:...
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "skip"

    dwell = dwell_sec if dwell_sec is not None else random.randint(45, 75)

    def _log(level: str, msg: str) -> None:
        if log:
            log(level, msg)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            context = browser.new_context(
                user_agent=user_agent,
                locale=locale,
                geolocation={"latitude": latitude, "longitude": longitude},
                permissions=["geolocation"],
            )
            page = context.new_page()
            # CDP 覆写：与 Playwright context 一致，供 Maps 的 Geolocation API 使用
            try:
                cdp = context.new_cdp_session(page)
                cdp.send(
                    "Emulation.setGeolocationOverride",
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "accuracy": 30,
                    },
                )
            except Exception:
                pass

            _log("INFO ", f"Maps 浏览器定位覆写: {latitude}, {longitude}")
            page.goto(maps_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(min(dwell, 120) * 1000)
            browser.close()
        return "ok"
    except Exception as exc:
        return f"error:{exc}"


def maps_geo_enabled(cfg: dict[str, Any]) -> str:
    """auto | true | false"""
    return str(cfg.get("ENABLE_MAPS_GEO", "auto")).strip().lower() or "auto"
