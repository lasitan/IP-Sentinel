"""Hash-Seeded Persona：UA 池与 LBS 坐标抖动."""

from __future__ import annotations

import random
import zlib
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def cksum_seed(text: str) -> int:
    return zlib.crc32(text.encode()) & 0xFFFFFFFF


def load_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]


def is_mobile_ua(ua: str) -> bool:
    u = ua.lower()
    return any(k in u for k in ("mobile", "android", "iphone", "ipad", "ipod"))


def pick_session_ua(ua_pool: Sequence[str], seed_ip: str) -> str:
    if not ua_pool:
        return DEFAULT_UA
    total = len(ua_pool)
    seed = cksum_seed(seed_ip)
    idx1 = seed % total
    idx2 = (seed * 17) % total
    idx3 = (seed * 31) % total
    pool = [ua_pool[idx1], ua_pool[idx2], ua_pool[idx3]]
    return random.choice(pool)


def pick_browser_ua(ua_pool: Sequence[str], seed_ip: str) -> str:
    """Playwright/Chromium 用桌面 UA（Earth Web 在移动端 UA 下常不加载 earth-app）."""
    desktop = [ua for ua in ua_pool if ua and not is_mobile_ua(ua)]
    if desktop:
        return pick_session_ua(desktop, seed_ip)
    return DEFAULT_UA


def random_coord(base: float, range_units: int) -> float:
    offset = ((random.randint(0, range_units * 2) - range_units) / 10000.0)
    return base + offset


def uri_encode_keyword(keyword: str) -> str:
    return quote(keyword, safe="")
