"""加载 /opt/ip_sentinel/config.conf 运行时配置."""

from __future__ import annotations

import os
from typing import Any

DEFAULT_INSTALL_DIR = "/opt/ip_sentinel"
DEFAULT_CONFIG_PATH = f"{DEFAULT_INSTALL_DIR}/config.conf"


def load_config(path: str | None = None) -> dict[str, Any]:
    cfg_path = path or os.environ.get("IP_SENTINEL_CONFIG", DEFAULT_CONFIG_PATH)
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

    install_dir = cfg.get("INSTALL_DIR", DEFAULT_INSTALL_DIR)
    cfg.setdefault("INSTALL_DIR", install_dir)
    cfg.setdefault("LOG_FILE", f"{install_dir}/logs/sentinel.log")
    return cfg


def require_config(path: str | None = None) -> dict[str, Any]:
    cfg = load_config(path)
    if not cfg:
        install = os.environ.get("IP_SENTINEL_INSTALL_DIR", DEFAULT_INSTALL_DIR)
        log_file = f"{install}/logs/sentinel.log"
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("[SYSTEM ] [ERROR] 配置文件丢失，子任务退出。\n")
        except OSError:
            pass
        raise SystemExit("配置文件丢失！退出执行。")
    return cfg
