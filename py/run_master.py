#!/usr/bin/env python3
"""Master 司令部入口 (由 install_master.sh 部署的 systemd/cron 调用)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from master.bot import main

if __name__ == "__main__":
    main()
