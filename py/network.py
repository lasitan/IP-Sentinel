"""HTTP 出站封装：出口 IP 绑定（requests）及 curl 兼容 bind_opt."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter


@dataclass
class CurlContext:
    bind_opt: list[str]
    ip_flag: str  # "-4" or "-6"

    @property
    def ip_version(self) -> int:
        return 6 if self.ip_flag == "-6" else 4


class _SourceAddressAdapter(HTTPAdapter):
    def __init__(self, source_address: tuple[str, int], **kwargs: Any) -> None:
        self._source_address = source_address
        super().__init__(**kwargs)

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any):
        pool_kwargs["source_address"] = self._source_address
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


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
            log_fn("INFO ", f"底层路由锁定: 绑定 IPv6 出口 ({bind_ip})")
    elif "." in bind_ip:
        ip_flag = "-4"
        if log_fn:
            log_fn("INFO ", f"底层路由锁定: 绑定 IPv4 出口 ({bind_ip})")
    return CurlContext(bind_opt=bind_opt, ip_flag=ip_flag)


def _bind_ip_from_ctx(ctx: CurlContext) -> str | None:
    if len(ctx.bind_opt) >= 2 and ctx.bind_opt[0] == "--interface":
        return ctx.bind_opt[1]
    return None


def _session(ctx: CurlContext) -> requests.Session:
    sess = requests.Session()
    bind = _bind_ip_from_ctx(ctx)
    if bind:
        raw = bind.strip("[]")
        adapter = _SourceAddressAdapter((raw, 0))
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
    return sess


def _header_dict(
    *,
    ua: str | None = None,
    cookie: str | None = None,
    extra_headers: list[str] | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if ua:
        headers["User-Agent"] = ua
    if cookie:
        headers["Cookie"] = cookie
    for raw in extra_headers or []:
        if ":" in raw:
            key, val = raw.split(":", 1)
            headers[key.strip()] = val.strip()
    return headers


def _timeout(seconds: int) -> tuple[float, float]:
    return (float(seconds), float(seconds))


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
    del compressed  # requests 自动处理 Accept-Encoding
    headers = _header_dict(ua=ua, extra_headers=extra_headers)
    try:
        with _session(ctx) as sess:
            resp = sess.get(
                url,
                headers=headers,
                timeout=_timeout(timeout),
                allow_redirects=follow,
                stream=True,
            )
            return str(resp.status_code)
    except requests.RequestException:
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
    del tls13
    headers = _header_dict(ua=ua, cookie=cookie, extra_headers=extra_headers)
    try:
        with _session(ctx) as sess:
            resp = sess.get(
                url,
                headers=headers,
                timeout=_timeout(timeout),
                allow_redirects=follow,
            )
            if fail_on_http_error:
                resp.raise_for_status()
            return resp.text
    except requests.RequestException:
        return ""


def fetch_headers(url: str, ctx: CurlContext, *, timeout: int = 10, ua: str | None = None) -> str:
    headers = _header_dict(ua=ua)
    try:
        with _session(ctx) as sess:
            resp = sess.head(url, headers=headers, timeout=_timeout(timeout), allow_redirects=True)
            lines = [f"HTTP/1.1 {resp.status_code} {resp.reason}"]
            for key, val in resp.headers.items():
                lines.append(f"{key}: {val}")
            return "\n".join(lines) + "\n"
    except requests.RequestException:
        return ""


def fetch_http_response(
    url: str,
    ctx: CurlContext,
    *,
    ua: str | None = None,
    follow: bool = False,
    head_only: bool = False,
    timeout: int = 30,
) -> tuple[int, str, dict[str, str]]:
    """
    返回 (HTTP 状态码, 响应体, 响应头字典小写键).
    head_only=True 时响应体为空。
    """
    headers = _header_dict(ua=ua)
    try:
        with _session(ctx) as sess:
            if head_only:
                resp = sess.head(url, headers=headers, timeout=_timeout(timeout), allow_redirects=follow)
                body = ""
            else:
                resp = sess.get(url, headers=headers, timeout=_timeout(timeout), allow_redirects=follow)
                body = resp.text
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status_code, body, hdrs
    except requests.RequestException:
        return 0, "", {}


def preflight(ctx: CurlContext, timeout: int = 4) -> bool:
    try:
        with _session(ctx) as sess:
            resp = sess.get(
                "https://www.cloudflare.com/cdn-cgi/trace",
                timeout=_timeout(timeout),
            )
            return resp.ok
    except requests.RequestException:
        return False
