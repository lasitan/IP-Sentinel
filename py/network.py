"""curl 出站封装：网卡绑定、双栈与 HTTP 探测."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class CurlContext:
    bind_opt: list[str]
    ip_flag: str  # "-4" or "-6"

    @property
    def ip_version(self) -> int:
        return 6 if self.ip_flag == "-6" else 4


def _ip_on_interface(raw_bind_ip: str) -> bool:
    try:
        out = subprocess.run(
            ["ip", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode != 0:
            return False
        return re.search(rf"\b{re.escape(raw_bind_ip)}\b", out.stdout) is not None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def build_curl_context(cfg: dict[str, Any], log_fn=None) -> CurlContext:
    bind_ip = cfg.get("BIND_IP", "")
    ip_pref = cfg.get("IP_PREF", "4")
    ip_flag = f"-{ip_pref or '4'}"
    bind_opt: list[str] = []

    if not bind_ip or not re.match(r"^[0-9a-fA-F:.]+$", bind_ip):
        return CurlContext(bind_opt=[], ip_flag=ip_flag)

    raw = bind_ip.strip("[]")
    if not _ip_on_interface(raw):
        if log_fn:
            log_fn(
                "WARN ",
                f"检测到配置的出口 IP ({raw}) 已丢失，自动降级为系统默认路由出网！",
            )
        return CurlContext(bind_opt=[], ip_flag=ip_flag)

    bind_opt = ["--interface", bind_ip]
    if ":" in bind_ip:
        ip_flag = "-6"
        if log_fn:
            log_fn("INFO ", f"底层路由锁定: 绑定 IPv6 出口及协议 ({bind_ip})")
    elif "." in bind_ip:
        ip_flag = "-4"
        if log_fn:
            log_fn("INFO ", f"底层路由锁定: 绑定 IPv4 出口及协议 ({bind_ip})")
    return CurlContext(bind_opt=bind_opt, ip_flag=ip_flag)


def _base_cmd(ctx: CurlContext, timeout: int) -> list[str]:
    return ["curl", *ctx.bind_opt, ctx.ip_flag, "-m", str(timeout)]


def http_status(
    url: str,
    ctx: CurlContext,
    *,
    ua: str | None = None,
    follow: bool = False,
    timeout: int = 15,
    extra_headers: list[str] | None = None,
    compressed: bool = False,
) -> str:
    cmd = _base_cmd(ctx, timeout) + ["-s", "-o", "/dev/null", "-w", "%{http_code}"]
    if follow:
        cmd.append("-L")
    if compressed:
        cmd.append("--compressed")
    if ua:
        cmd.extend(["-A", ua])
    if extra_headers:
        for h in extra_headers:
            cmd.extend(["-H", h])
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5, check=False)
        return (r.stdout or "").strip() or "000"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "000"


def fetch_text(
    url: str,
    ctx: CurlContext,
    *,
    ua: str | None = None,
    follow: bool = True,
    timeout: int = 10,
    cookie: str | None = None,
    extra_headers: list[str] | None = None,
    tls13: bool = False,
    fail_on_http_error: bool = False,
) -> str:
    cmd = _base_cmd(ctx, timeout) + ["-s"]
    if fail_on_http_error:
        cmd.append("-f")
    if follow:
        cmd.append("-L")
    if tls13:
        cmd.append("--tlsv1.3")
    if ua:
        cmd.extend(["-A", ua])
    if cookie:
        cmd.extend(["-b", cookie])
    if extra_headers:
        for h in extra_headers:
            cmd.extend(["-H", h])
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5, check=False)
        return r.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def fetch_headers(url: str, ctx: CurlContext, *, timeout: int = 10) -> str:
    cmd = _base_cmd(ctx, timeout) + ["-sI", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5, check=False)
        return r.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def preflight(ctx: CurlContext, timeout: int = 4) -> bool:
    cmd = _base_cmd(ctx, timeout) + ["-s", "https://www.cloudflare.com/cdn-cgi/trace"]
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
            check=False,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
