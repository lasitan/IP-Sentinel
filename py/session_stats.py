"""会话统计：模块结束时写入结构化记录，日报直接读取（不再解析日志）."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_RETENTION_DAYS = 7
_MAX_LINES = 2000


def _stats_path(cfg: dict[str, Any]) -> Path:
    install = cfg.get("INSTALL_DIR", "/opt/ip_sentinel")
    return Path(install) / "core" / "session_stats.jsonl"


def classify_outcome(conclusion: str) -> str:
    text = conclusion or ""
    if "❌" in text:
        return "fail"
    if "⚠️" in text or "🚨" in text:
        return "warn"
    if "✅" in text:
        return "ok"
    return "unknown"


def append_session(cfg: dict[str, Any], record: dict[str, Any]) -> None:
    path = _stats_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        **record,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _prune_file(path)


def _prune_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    if len(lines) <= _MAX_LINES:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)
        kept = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(row.get("ts", ""))
            if ts and ts >= cutoff:
                kept.append(line)
        if len(kept) < len(lines):
            _atomic_write(path, kept)
        return
    kept = lines[-_MAX_LINES:]
    _atomic_write(path, kept)


def _atomic_write(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def _parse_ts(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(text.replace("+0000", " UTC"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def load_sessions(cfg: dict[str, Any], *, hours: float = 24.0) -> list[dict[str, Any]]:
    path = _stats_path(cfg)
    if not path.is_file():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(str(row.get("ts", "")))
            if ts is None or ts < cutoff:
                continue
            out.append(row)
    except OSError:
        return []
    return out


def record_google_session(
    cfg: dict[str, Any],
    *,
    conclusion: str,
    maps_visits: int,
    earth_visits: int,
    actions_done: int,
    jump_gl: str = "",
    yt_premium_gl: str = "",
    yt_music_gl: str = "",
) -> None:
    append_session(
        cfg,
        {
            "module": "google",
            "outcome": classify_outcome(conclusion),
            "conclusion": conclusion,
            "maps_visits": maps_visits,
            "earth_visits": earth_visits,
            "actions_done": actions_done,
            "jump_gl": jump_gl,
            "yt_premium_gl": yt_premium_gl,
            "yt_music_gl": yt_music_gl,
        },
    )


def record_trust_session(
    cfg: dict[str, Any],
    *,
    conclusion: str,
    success_steps: int,
    total_steps: int,
) -> None:
    append_session(
        cfg,
        {
            "module": "trust",
            "outcome": classify_outcome(conclusion),
            "conclusion": conclusion,
            "success_steps": success_steps,
            "total_steps": total_steps,
        },
    )


def record_quality_session(
    cfg: dict[str, Any],
    *,
    ip: str,
    scam_score: str,
    youtube_region: str,
    youtube_status: str,
    play_status: str,
    gemini_status: str,
) -> None:
    append_session(
        cfg,
        {
            "module": "quality",
            "outcome": "ok",
            "conclusion": f"IP 质量检测 IP={ip}",
            "ip": ip,
            "scam_score": scam_score,
            "youtube_region": youtube_region,
            "youtube_status": youtube_status,
            "play_status": play_status,
            "gemini_status": gemini_status,
        },
    )


def summarize_google(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in sessions if r.get("module") == "google"]
    total = len(rows)
    ok = sum(1 for r in rows if r.get("outcome") == "ok")
    fail = sum(1 for r in rows if r.get("outcome") == "fail")
    warn = sum(1 for r in rows if r.get("outcome") == "warn")
    maps_geo = sum(int(r.get("maps_visits") or 0) for r in rows)
    earth_geo = sum(int(r.get("earth_visits") or 0) for r in rows)
    rate = f"{(ok / total * 100):.1f}" if total else "0.0"
    return {
        "total": total,
        "ok": ok,
        "fail": fail,
        "warn": warn,
        "maps_geo": maps_geo,
        "earth_geo": earth_geo,
        "rate": rate,
    }


def summarize_trust(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in sessions if r.get("module") == "trust"]
    total = len(rows)
    ok = sum(1 for r in rows if r.get("outcome") == "ok")
    fail = sum(1 for r in rows if r.get("outcome") == "fail")
    rate = f"{(ok / total * 100):.1f}" if total else "0.0"
    return {"total": total, "ok": ok, "fail": fail, "rate": rate}


def latest_snapshot(sessions: list[dict[str, Any]]) -> dict[str, str]:
    if not sessions:
        return {"module": "System", "ts": "", "conclusion": "暂无数据"}

    def _sort_key(r: dict[str, Any]) -> datetime:
        return _parse_ts(str(r.get("ts", ""))) or datetime.min.replace(tzinfo=timezone.utc)

    last = max(sessions, key=_sort_key)
    mod = str(last.get("module", "system"))
    label = {"google": "Google", "trust": "Trust", "quality": "Quality"}.get(mod, mod)
    return {
        "module": label,
        "ts": str(last.get("ts", "")),
        "conclusion": str(last.get("conclusion", "")),
    }
