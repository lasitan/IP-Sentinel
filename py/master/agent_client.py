"""向 Agent 下发 HTTPS 指令 (自签证书，等同 curl -k)."""

from __future__ import annotations

import ssl
import subprocess
import urllib.error
import urllib.request


def call_agent(url: str, timeout: int = 15) -> str:
    """优先 curl -k，回退 urllib 不校验证书."""
    try:
        r = subprocess.run(
            ["curl", "-k", "-s", "--connect-timeout", "5", "-m", str(timeout), url],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
        if r.returncode == 0:
            return r.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            return resp.read().decode(errors="ignore")
    except (urllib.error.URLError, TimeoutError, OSError):
        return "FAILED"
