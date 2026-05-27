"""Google Maps：Chromium + Geolocation API 覆写（与搜索 URL 坐标一致）."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

from playwright.sync_api import sync_playwright

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

    返回: ok | error:...
    """
    dwell = dwell_sec if dwell_sec is not None else random.randint(45, 75)

    def _log(level: str, msg: str) -> None:
        if log:
            log(level, msg)

    _log("INFO ", f"[MAPS_GEO] 准备虚拟定位 | 坐标: {latitude}, {longitude}")

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
            except Exception as exc:
                _log("WARN ", f"[MAPS_GEO] CDP Geolocation 覆写失败: {exc}")
            else:
                _log("INFO ", f"[MAPS_GEO] CDP Geolocation 覆写: {latitude}, {longitude}")

            page.goto(maps_url, wait_until="domcontentloaded", timeout=60_000)

            geo_read = page.evaluate(
                """() => new Promise((resolve) => {
                    if (!navigator.geolocation) {
                        resolve({ error: 'geolocation unavailable' });
                        return;
                    }
                    navigator.geolocation.getCurrentPosition(
                        (p) => resolve({
                            latitude: p.coords.latitude,
                            longitude: p.coords.longitude,
                            accuracy: p.coords.accuracy,
                        }),
                        (e) => resolve({ error: e.message || String(e) }),
                        { timeout: 15000, maximumAge: 0, enableHighAccuracy: true }
                    );
                })"""
            )
            if isinstance(geo_read, dict) and "error" not in geo_read:
                _log(
                    "INFO ",
                    "[MAPS_GEO] 网页 Geolocation API 读数: "
                    f"{geo_read.get('latitude')}, {geo_read.get('longitude')} "
                    f"(accuracy={geo_read.get('accuracy')})",
                )
            else:
                err = geo_read.get("error", geo_read) if isinstance(geo_read, dict) else geo_read
                _log("WARN ", f"[MAPS_GEO] 网页 Geolocation API 未返回坐标: {err}")

            page.wait_for_timeout(min(dwell, 120) * 1000)
            browser.close()
        _log("INFO ", f"[MAPS_GEO] 访问完成 | 虚拟坐标: {latitude}, {longitude}")
        return "ok"
    except Exception as exc:
        return f"error:{exc}"


def maps_geo_enabled(cfg: dict[str, Any]) -> str:
    """true | auto | false — true 失败不回退 HTTP；auto 失败回退；false 仅 HTTP"""
    return str(cfg.get("ENABLE_MAPS_GEO", "true")).strip().lower() or "true"
