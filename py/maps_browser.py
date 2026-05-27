"""Google Maps / Earth：Chromium + Geolocation 覆写（与会话纠偏坐标一致）."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright

LogFn = Callable[[str, str], None]
OnPageLoadedFn = Callable[[Page], None]

_AUTO_ALLOW_GEO_SCRIPT = """
(() => {
  const granted = { state: 'granted', onchange: null };
  if (navigator.permissions && navigator.permissions.query) {
    const orig = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (desc) =>
      desc && desc.name === 'geolocation' ? Promise.resolve(granted) : orig(desc);
  }
})();
"""

_READ_GEO_JS = """
() => new Promise((resolve) => {
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
})
"""

_GEO_PERMISSIONS = ["geolocation"]
_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

_DEFAULT_GEO_ORIGINS = (
    "https://www.google.com",
    "https://google.com",
    "https://maps.google.com",
    "https://earth.google.com",
)

# Earth Web「显示您的位置」在 earth-app 多层 Shadow DOM 内
_EARTH_MY_LOCATION_JS = r"""
() => {
  const LABELS = [
    '显示您的位置', '显示你的位置', '显示您目前的位置',
    'show your location', 'show your position', 'my location', 'your location',
    'go to my location', 'current location', '我的位置', '你的位置', '目前位置',
  ];
  const norm = (s) => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const labelHit = (text) => {
    const t = norm(text);
    return LABELS.some((k) => t.includes(norm(k)));
  };
  const elementText = (el) => {
    const parts = [
      el.getAttribute?.('aria-label'),
      el.getAttribute?.('title'),
      el.getAttribute?.('data-tooltip'),
      el.getAttribute?.('tooltip'),
      el.getAttribute?.('aria-description'),
      el.getAttribute?.('data-title'),
    ];
    return parts.filter(Boolean).join(' ');
  };
  const clickEl = (el) => {
    const target =
      el.closest?.(
        'button, cr-icon-button, paper-icon-button, paper-button, [role="button"]'
      ) || el;
    target.dispatchEvent(
      new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window })
    );
    target.dispatchEvent(
      new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window })
    );
    target.dispatchEvent(
      new MouseEvent('click', { bubbles: true, cancelable: true, view: window })
    );
    if (typeof target.click === 'function') target.click();
    return {
      tag: target.tagName,
      title: target.getAttribute?.('title') || '',
      aria: target.getAttribute?.('aria-label') || '',
    };
  };
  const walk = (root, depth) => {
    if (!root || depth > 30) return null;
    for (const el of root.querySelectorAll(
      'button, cr-icon-button, paper-icon-button, [role="button"], [title], [aria-label]'
    )) {
      if (labelHit(elementText(el))) return el;
    }
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        const hit = walk(el.shadowRoot, depth + 1);
        if (hit) return hit;
      }
    }
    return null;
  };
  const roots = [];
  const app = document.querySelector('earth-app');
  if (app?.shadowRoot) roots.push(app.shadowRoot);
  roots.push(document);
  for (const root of roots) {
    const hit = walk(root, 0);
    if (hit) return { ok: true, how: 'label', ...clickEl(hit) };
  }
  const buttons = [];
  const seen = new Set();
  const collect = (root) => {
    for (const el of root.querySelectorAll(
      'button, cr-icon-button, paper-icon-button, [role="button"]'
    )) {
      if (seen.has(el)) continue;
      seen.add(el);
      const r = el.getBoundingClientRect();
      if (r.width < 28 || r.width > 72 || r.height < 28 || r.height > 72) continue;
      if (r.top < window.innerHeight * 0.55) continue;
      if (r.left < window.innerWidth * 0.45) continue;
      buttons.push({ el, x: r.left + r.width / 2, y: r.top + r.height / 2 });
    }
    for (const host of root.querySelectorAll('*')) {
      if (host.shadowRoot) collect(host.shadowRoot);
    }
  };
  if (app?.shadowRoot) collect(app.shadowRoot);
  collect(document);
  if (!buttons.length) return { ok: false, how: 'none' };
  buttons.sort((a, b) => a.y - b.y || a.x - b.x);
  const rows = [];
  for (const b of buttons) {
    const row = rows.find((r) => Math.abs(r[0].y - b.y) < 18);
    if (row) row.push(b);
    else rows.push([b]);
  }
  const pick = (row) => {
    row.sort((a, b) => a.x - b.x);
    if (row.length >= 3) return row[Math.floor(row.length / 2)];
    if (row.length === 2) return row[0];
    return row[0];
  };
  let best = null;
  for (const row of rows) {
    if (row.length >= 2) {
      const c = pick(row);
      if (!best || row.length > best.rowLen) best = { btn: c, rowLen: row.length };
    }
  }
  if (!best && buttons.length) best = { btn: buttons[0], rowLen: 1 };
  if (best) return { ok: true, how: 'toolbar-middle', ...clickEl(best.btn.el) };
  return { ok: false, how: 'none' };
}
"""

_EARTH_LOCATION_LABELS = (
    "显示您的位置",
    "显示你的位置",
    "Show your location",
    "Show Your Location",
    "My location",
    "Your location",
)


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


def earth_explore_url(latitude: float, longitude: float) -> str:
    """Google Earth Web 探索视图深链（相机落在指定经纬度）."""
    return (
        f"https://earth.google.com/web/@{latitude},{longitude},"
        "500a,3000d,35y,0h,0t,0r"
    )


def _origin_from_url(url: str) -> str | None:
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


def _wire_auto_geolocation(
    context: BrowserContext,
    latitude: float,
    longitude: float,
    *,
    seed_url: str,
    extra_origins: tuple[str, ...] = (),
) -> None:
    granted_origins: set[str] = set()

    def grant_for_origin(origin: str) -> None:
        if origin in granted_origins:
            return
        try:
            context.grant_permissions(_GEO_PERMISSIONS, origin=origin)
            granted_origins.add(origin)
        except Exception:
            pass

    context.grant_permissions(_GEO_PERMISSIONS)
    context.set_geolocation(
        {"latitude": latitude, "longitude": longitude, "accuracy": 30}
    )
    context.add_init_script(_AUTO_ALLOW_GEO_SCRIPT)

    for origin in filter(
        None,
        (_origin_from_url(seed_url), *_DEFAULT_GEO_ORIGINS, *extra_origins),
    ):
        grant_for_origin(origin)

    def _on_frame_navigated(frame) -> None:
        origin = _origin_from_url(frame.url)
        if origin:
            grant_for_origin(origin)

    def _on_page(page: Page) -> None:
        page.on("framenavigated", _on_frame_navigated)
        _on_frame_navigated(page.main_frame)

    context.on("page", _on_page)


def _log_geo_read(page: Page, _log: LogFn, tag: str) -> None:
    geo_read = page.evaluate(_READ_GEO_JS)
    if isinstance(geo_read, dict) and "error" not in geo_read:
        _log(
            "INFO ",
            f"[{tag}] 网页 Geolocation API 读数: "
            f"{geo_read.get('latitude')}, {geo_read.get('longitude')} "
            f"(accuracy={geo_read.get('accuracy')})",
        )
    else:
        err = geo_read.get("error", geo_read) if isinstance(geo_read, dict) else geo_read
        _log("WARN ", f"[{tag}] 网页 Geolocation API 未返回坐标: {err}")


def _apply_cdp_geolocation(
    context: BrowserContext,
    page: Page,
    latitude: float,
    longitude: float,
    _log: LogFn,
    tag: str,
) -> None:
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
        _log("INFO ", f"[{tag}] CDP Geolocation 覆写: {latitude}, {longitude}")
    except Exception as exc:
        _log("WARN ", f"[{tag}] CDP Geolocation 覆写失败: {exc}")


def _goto_follow_redirects(
    page: Page,
    url: str,
    _log: LogFn | None,
    tag: str,
    *,
    timeout: float = 120_000,
) -> None:
    """
    导航并容忍重定向链导致的 ERR_ABORTED（首跳被下一跳取消时仍视为成功）.
    """
    last_exc: Exception | None = None

    def _emit(level: str, msg: str) -> None:
        if _log:
            _log(level, msg)

    for wait_until in ("commit", "domcontentloaded", "load"):
        try:
            resp = page.goto(url, wait_until=wait_until, timeout=timeout)
            final = page.url or ""
            if final and not final.startswith("about:"):
                status = resp.status if resp else "?"
                _emit(
                    "INFO ",
                    f"[{tag}] 导航完成 ({wait_until}) HTTP {status} → {final}",
                )
                return
        except Exception as exc:
            last_exc = exc
            err_text = str(exc)
            current = page.url or ""
            if "ERR_ABORTED" in err_text and current and not current.startswith("about:"):
                _emit(
                    "INFO ",
                    f"[{tag}] 首跳因重定向中断 (ERR_ABORTED)，已跟随至: {current}",
                )
                for state in ("domcontentloaded", "load", "networkidle"):
                    try:
                        page.wait_for_load_state(state, timeout=60_000)
                        _emit("INFO ", f"[{tag}] 重定向后页面就绪 ({state})")
                        return
                    except Exception:
                        continue
                if "earth.google.com" in current or "google.com" in current:
                    return
            if wait_until == "load":
                break

    if last_exc:
        raise last_exc


def _try_click_text(page: Page, labels: tuple[str, ...], timeout_ms: int = 3000) -> str | None:
    for label in labels:
        try:
            loc = page.get_by_text(label, exact=False).first
            if loc.is_visible(timeout=timeout_ms):
                loc.click(timeout=10_000)
                return label
        except Exception:
            continue
    return None


def _earth_enter_explore_and_locate(
    page: Page,
    latitude: float,
    longitude: float,
    _log: LogFn,
) -> None:
    page.wait_for_timeout(2000)
    clicked = _try_click_text(
        page,
        ("Launch Wasm Multiple Threaded", "Launch Wasm Single Threaded"),
        timeout_ms=4000,
    )
    if clicked:
        _log("INFO ", f"[EARTH_GEO] 已启动 Earth Wasm: {clicked}")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=120_000)
        except Exception:
            pass
        page.wait_for_timeout(5000)

    explore = earth_explore_url(latitude, longitude)
    if "/web/@" not in page.url:
        _log("INFO ", f"[EARTH_GEO] 跳转探索地球: {explore}")
        _goto_follow_redirects(page, explore, _log, "EARTH_GEO")
        page.wait_for_timeout(4000)
    else:
        _log("INFO ", "[EARTH_GEO] 已在 Earth Web 探索视图")

    _earth_trigger_my_location(page, _log)
    page.wait_for_timeout(2000)


def _earth_wait_ui_ready(page: Page, _log: LogFn) -> None:
    try:
        page.wait_for_selector("earth-app", state="attached", timeout=90_000)
    except Exception:
        _log("WARN ", "[EARTH_GEO] earth-app 未在时限内挂载，继续尝试定位按钮")
    page.wait_for_timeout(5000)


def _earth_click_show_my_location_playwright(page: Page, _log: LogFn) -> bool:
    """Playwright 穿透开放 Shadow DOM，优先匹配 title/aria「显示您的位置」."""
    selectors = []
    for label in _EARTH_LOCATION_LABELS:
        selectors.extend(
            (
                f'button[title="{label}"]',
                f'[title="{label}"]',
                f'[aria-label="{label}"]',
                f'cr-icon-button[title="{label}"]',
                f'earth-app >> button[title="{label}"]',
                f'earth-app >> [aria-label="{label}"]',
            )
        )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=6000)
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=10_000, force=True)
            _log("INFO ", f"[EARTH_GEO] Playwright 已点击: {sel}")
            return True
        except Exception:
            continue

    for label in _EARTH_LOCATION_LABELS:
        for factory in (
            lambda lb=label: page.get_by_title(lb, exact=True),
            lambda lb=label: page.get_by_title(lb),
            lambda lb=label: page.get_by_role("button", name=lb),
            lambda lb=label: page.get_by_label(lb, exact=False),
        ):
            try:
                loc = factory(label).first
                loc.wait_for(state="visible", timeout=5000)
                loc.click(timeout=10_000, force=True)
                _log("INFO ", f"[EARTH_GEO] Playwright 已点击控件: {label}")
                return True
            except Exception:
                continue
    return False


def _earth_trigger_my_location(page: Page, _log: LogFn) -> None:
    _earth_wait_ui_ready(page, _log)

    if _earth_click_show_my_location_playwright(page, _log):
        page.wait_for_timeout(2500)
        return

    result = page.evaluate(_EARTH_MY_LOCATION_JS)
    if isinstance(result, dict) and result.get("ok"):
        how = result.get("how", "?")
        title = result.get("title") or result.get("aria") or result.get("tag", "")
        _log("INFO ", f"[EARTH_GEO] JS 已点击「显示您的位置」({how}): {title}")
        page.wait_for_timeout(2500)
        return

    _log(
        "WARN ",
        "[EARTH_GEO] 未找到「显示您的位置」按钮，已依赖深链坐标与 Geolocation API",
    )


def _visit_with_geolocation(
    *,
    seed_url: str,
    latitude: float,
    longitude: float,
    user_agent: str,
    locale: str,
    dwell_sec: int | None,
    log: LogFn | None,
    tag: str,
    on_loaded: OnPageLoadedFn | None = None,
) -> str:
    dwell = dwell_sec if dwell_sec is not None else random.randint(45, 75)

    def _log(level: str, msg: str) -> None:
        if log:
            log(level, msg)

    _log("INFO ", f"[{tag}] 准备虚拟定位 | 坐标: {latitude}, {longitude}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
            context = browser.new_context(
                user_agent=user_agent,
                locale=locale,
                geolocation={"latitude": latitude, "longitude": longitude},
            )
            _wire_auto_geolocation(
                context,
                latitude,
                longitude,
                seed_url=seed_url,
            )
            page = context.new_page()
            _apply_cdp_geolocation(context, page, latitude, longitude, _log, tag)

            _goto_follow_redirects(page, seed_url, _log, tag)
            if on_loaded:
                on_loaded(page)

            _log_geo_read(page, _log, tag)
            page.wait_for_timeout(min(dwell, 120) * 1000)
            browser.close()
        _log("INFO ", f"[{tag}] 访问完成 | 虚拟坐标: {latitude}, {longitude}")
        return "ok"
    except Exception as exc:
        return f"error:{exc}"


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
    """Chromium 打开 Maps，Geolocation 返回指定经纬度."""
    return _visit_with_geolocation(
        seed_url=maps_url,
        latitude=latitude,
        longitude=longitude,
        user_agent=user_agent,
        locale=locale,
        dwell_sec=dwell_sec,
        log=log,
        tag="MAPS_GEO",
    )


def visit_google_earth(
    *,
    latitude: float,
    longitude: float,
    user_agent: str,
    locale: str = "en-US",
    dwell_sec: int | None = None,
    log: LogFn | None = None,
) -> str:
    """
    访问 earth.google.com → 进入探索地球 → 定位到会话虚拟坐标（自动允许定位权限）.
    """
    lat, lon = latitude, longitude

    def _earth_log(level: str, msg: str) -> None:
        if log:
            log(level, msg)

    def _on_loaded(page: Page) -> None:
        _earth_log("INFO ", "[EARTH_GEO] Earth 探索视图已加载，进入定位流程…")
        _earth_enter_explore_and_locate(page, lat, lon, _earth_log)

    # 先进入 /web/ 探索入口（根域常会 302→/web，易触发 ERR_ABORTED）
    seed = earth_explore_url(latitude, longitude)
    return _visit_with_geolocation(
        seed_url=seed,
        latitude=latitude,
        longitude=longitude,
        user_agent=user_agent,
        locale=locale,
        dwell_sec=dwell_sec,
        log=log,
        tag="EARTH_GEO",
        on_loaded=_on_loaded,
    )


def maps_geo_enabled(cfg: dict[str, Any]) -> str:
    """true | auto | false — true 失败不回退 HTTP；auto 失败回退；false 仅 HTTP"""
    return str(cfg.get("ENABLE_MAPS_GEO", "true")).strip().lower() or "true"
