"""xykt/IPQuality 探针编排（对齐 legacy bash 深海声呐包装逻辑）."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from network import _ip_on_interface, build_curl_context, preflight

LogFn = Callable[[str, str], None]

_XYKT_MARKER = "xykt"
_XYKT_URLS = (
    "https://raw.githubusercontent.com/xykt/IPQuality/main/ip.sh",
    "https://IP.Check.Place",
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_PROBE_TIMEOUT = 300


def _log(log_fn: LogFn | None, level: str, msg: str) -> None:
    if log_fn:
        log_fn(level, msg)


def probe_script_path(cfg: dict[str, Any]) -> Path:
    install = cfg.get("INSTALL_DIR", "/opt/ip_sentinel")
    return Path(install) / "core" / "ip_probe.sh"


def _valid_script(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return _XYKT_MARKER in text


def ensure_probe_script(cfg: dict[str, Any], log_fn: LogFn | None = None) -> Path | None:
    """下载并校验 xykt ip.sh（防伪：必须含 xykt 标记）."""
    path = probe_script_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file() and not _valid_script(path):
        _log(log_fn, "WARN ", "本地 ip_probe.sh 校验失败，已删除")
        path.unlink(missing_ok=True)

    if _valid_script(path):
        return path

    for url in _XYKT_URLS:
        _log(log_fn, "INFO ", f"拉取 xykt 探针: {url}")
        try:
            subprocess.run(
                ["curl", "-fsSL", "-m", "15" if "Check.Place" in url else "10", url, "-o", str(path)],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if _valid_script(path):
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            _log(log_fn, "INFO ", "xykt 探针脚本就绪")
            return path
        path.unlink(missing_ok=True)

    _log(log_fn, "ERROR", "无法获取有效 xykt ip_probe.sh")
    return None


def _resolve_bind(cfg: dict[str, Any]) -> tuple[str, str]:
    """返回 (raw_bind_ip, ip_pref)，无有效绑定时 raw 为空."""
    ip_pref = str(cfg.get("IP_PREF") or "4")
    bind_ip = (cfg.get("BIND_IP") or "").strip()
    if not bind_ip or not re.match(r"^[0-9a-fA-F:\[\]\.]+$", bind_ip):
        return "", ip_pref

    raw = bind_ip.strip("[]")
    if not _ip_on_interface(raw):
        return "", ip_pref

    if ":" in raw:
        return raw, "6"
    if "." in raw:
        return raw, "4"
    return raw, ip_pref


def _argv_base() -> list[str]:
    return ["-y", "-j", "-f"]


def _argv_for_tier(raw_bind: str, ip_pref: str, *, with_bind: bool, with_proto: bool) -> list[str]:
    args = _argv_base()
    if with_bind and raw_bind:
        args.extend(["-i", raw_bind])
    if with_proto and ip_pref in ("4", "6"):
        args.append(f"-{ip_pref}")
    return args


def _preflight_tier(cfg: dict[str, Any], raw_bind: str, ip_pref: str, *, with_bind: bool, with_proto: bool) -> bool:
    tier_cfg = dict(cfg)
    tier_cfg["BIND_IP"] = raw_bind if with_bind else ""
    tier_cfg["IP_PREF"] = ip_pref if with_proto else str(cfg.get("IP_PREF") or "4")
    ctx = build_curl_context(tier_cfg)
    return preflight(ctx, timeout=4)


def select_probe_argv(cfg: dict[str, Any], log_fn: LogFn | None = None) -> list[str]:
    """阶梯寻路：绑定+协议 → 仅协议 → 裸跑."""
    raw_bind, ip_pref = _resolve_bind(cfg)

    tiers: list[tuple[str, list[str]]] = []
    if raw_bind:
        tiers.append(("阶梯0: 绑定出口+协议", _argv_for_tier(raw_bind, ip_pref, with_bind=True, with_proto=True)))
    tiers.append(("阶梯1: 仅协议", _argv_for_tier("", ip_pref, with_bind=False, with_proto=True)))
    tiers.append(("阶梯2: 系统默认路由", _argv_for_tier("", ip_pref, with_bind=False, with_proto=False)))

    for label, argv in tiers:
        with_bind = "-i" in argv
        with_proto = argv[-1] in ("-4", "-6") if argv else False
        tier_bind = raw_bind if with_bind else ""
        tier_pref = ip_pref if with_proto else str(cfg.get("IP_PREF") or "4")
        if _preflight_tier(cfg, tier_bind, tier_pref, with_bind=with_bind, with_proto=with_proto):
            _log(log_fn, "INFO ", f"预检通过 ({label}): {' '.join(argv)}")
            return argv

    _log(log_fn, "WARN ", "预检均未通过，仍尝试裸跑参数")
    return _argv_for_tier("", ip_pref, with_bind=False, with_proto=False)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _parse_xykt_output(raw: str) -> dict[str, Any] | None:
    cleaned = _strip_ansi(raw)
    start = cleaned.find("{")
    if start < 0:
        return None
    blob = cleaned[start:]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    # 容错：截取首个完整 JSON 对象
    depth = 0
    for idx, ch in enumerate(blob):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(blob[: idx + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run_xykt_probe(cfg: dict[str, Any], log_fn: LogFn | None = None) -> dict[str, Any] | None:
    script = ensure_probe_script(cfg, log_fn)
    if not script:
        return None

    argv = select_probe_argv(cfg, log_fn)
    cmd = ["bash", str(script), *argv]
    _log(log_fn, "INFO ", f"执行 xykt 探针: bash {script.name} {' '.join(argv)}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            cwd=str(script.parent),
            env={**os.environ},
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log(log_fn, "ERROR", f"xykt 探针超时 ({_PROBE_TIMEOUT}s)")
        return None
    except FileNotFoundError:
        _log(log_fn, "ERROR", "未找到 bash，无法执行 xykt 探针")
        return None

    raw = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not raw.strip():
        _log(log_fn, "ERROR", f"xykt 探针退出码 {proc.returncode}")
        return None

    data = _parse_xykt_output(raw)
    if not data or not (data.get("Head") or {}).get("IP"):
        _log(log_fn, "ERROR", "xykt 输出中未解析到有效 JSON / Head.IP")
        return None

    _log(log_fn, "INFO ", f"xykt 探针完成，出口 IP={(data.get('Head') or {}).get('IP')}")
    return data
