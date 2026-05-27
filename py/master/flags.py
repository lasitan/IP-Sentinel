"""区域 ISO 代码 → 旗帜 emoji."""

from __future__ import annotations

FLAGS: dict[str, str] = {
    "US": "🇺🇸", "JP": "🇯🇵", "HK": "🇭🇰", "TW": "🇹🇼", "SG": "🇸🇬",
    "UK": "🇬🇧", "GB": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷", "NL": "🇳🇱",
    "CA": "🇨🇦", "AU": "🇦🇺", "KR": "🇰🇷", "IN": "🇮🇳", "BR": "🇧🇷",
    "RU": "🇷🇺", "CH": "🇨🇭", "SE": "🇸🇪", "NO": "🇳🇴", "DK": "🇩🇰",
    "FI": "🇫🇮", "IT": "🇮🇹", "ES": "🇪🇸", "PT": "🇵🇹", "IE": "🇮🇪",
    "PL": "🇵🇱", "AT": "🇦🇹", "BE": "🇧🇪", "TR": "🇹🇷", "ZA": "🇿🇦",
    "AE": "🇦🇪", "MY": "🇲🇾", "ID": "🇮🇩", "VN": "🇻🇳", "TH": "🇹🇭",
    "PH": "🇵🇭", "NZ": "🇳🇿", "AR": "🇦🇷", "CL": "🇨🇱", "MX": "🇲🇽",
    "IL": "🇮🇱", "SA": "🇸🇦", "EG": "🇪🇬", "NG": "🇳🇬", "KE": "🇰🇪",
    "RO": "🇷🇴", "BG": "🇧🇬", "CZ": "🇨🇿", "HU": "🇭🇺", "GR": "🇬🇷",
    "UA": "🇺🇦", "MO": "🇲🇴", "KH": "🇰🇭", "MM": "🇲🇲", "LA": "🇱🇦",
    "MN": "🇲🇳", "NP": "🇳🇵", "BD": "🇧🇩",
}


def get_flag(region: str) -> str:
    base = region.upper().split("-")[0]
    return FLAGS.get(base, "🌐")
