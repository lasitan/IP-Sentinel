"""加载 Master 配置 master.conf."""

from __future__ import annotations

import os
from typing import Any

DEFAULT_MASTER_DIR = "/opt/ip_sentinel_master"
DEFAULT_CONF = f"{DEFAULT_MASTER_DIR}/master.conf"


def load_master_config(path: str | None = None) -> dict[str, Any]:
    cfg_path = path or os.environ.get("IP_SENTINEL_MASTER_CONFIG", DEFAULT_CONF)
    cfg: dict[str, Any] = {}
    if not os.path.isfile(cfg_path):
        return cfg

    with open(cfg_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip().strip('"').strip("'")

    cfg.setdefault("MASTER_DIR", DEFAULT_MASTER_DIR)
    cfg.setdefault("DB_FILE", f"{cfg['MASTER_DIR']}/sentinel.db")
    cfg.setdefault("MASTER_VERSION", "4.1.1")
    cfg.setdefault("IS_OFFICIAL_GATEWAY", "false")
    cfg.setdefault("ENABLE_MASTER_OTA", "false")
    cfg.setdefault("FORUM_MODE", "false")
    cfg.setdefault("FORUM_CHAT_ID", "")
    return cfg


def require_master_config(path: str | None = None) -> dict[str, Any]:
    cfg = load_master_config(path)
    if not cfg.get("TG_TOKEN"):
        raise SystemExit("master.conf 缺失或无效")
    return cfg
