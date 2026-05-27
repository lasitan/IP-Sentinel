#!/usr/bin/env python3
"""Master 入口 (由 install_master.sh 配置的 systemd/cron 调用)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from master.bot import main

if __name__ == "__main__":
    main()
