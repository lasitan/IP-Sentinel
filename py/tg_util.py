"""Agent 侧 Telegram 推送（Markdown 转义、话题单消息删旧新发）."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from config import save_config_keys

_MD_SPECIAL = re.compile(r"([_*\[\]`])")
_CB_UNSAFE = re.compile(r'["\'`$|&;<>\n\r]')


def escape_markdown(text: str) -> str:
    """转义 Telegram legacy Markdown 特殊字符."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def sanitize_callback_field(val: str, max_len: int = 12) -> str:
    """趋势库 callback 字段：去危险字符并截断."""
    cleaned = _CB_UNSAFE.sub("", str(val or "")).strip()
    return (cleaned[:max_len] if cleaned else "?")


def build_svq_callback(
    node_name: str,
    scam_score: str,
    yt_region: str,
    yt_status: str,
    nf_status: str,
    gpt_status: str,
    *,
    max_bytes: int = 64,
) -> str:
    """构造 svq| 入库按钮 callback_data（Telegram 上限 64 字节）."""
    node = sanitize_callback_field(node_name, 28)
    score = re.sub(r"[^0-9]", "", str(scam_score)) or "0"
    goog = yt_region.strip().upper() if yt_region and len(yt_region) <= 4 else ""
    if not goog or len(goog) > 4:
        goog = sanitize_callback_field(yt_status, 8)
    parts = [
        "svq",
        node,
        score[:3],
        sanitize_callback_field(goog, 6),
        sanitize_callback_field(nf_status, 10),
        sanitize_callback_field(gpt_status, 10),
    ]
    cb = "|".join(parts)
    if len(cb.encode("utf-8")) <= max_bytes:
        return cb
    cb = "|".join([parts[0], parts[1], parts[2], parts[3][:4], parts[4][:6], parts[5][:6]])
    enc = cb.encode("utf-8")
    if len(enc) <= max_bytes:
        return cb
    return enc[:max_bytes].decode("utf-8", errors="ignore")


def tg_method_url(api_url: str, method: str) -> str:
    """由 sendMessage 配置 URL 推导 deleteMessage 等 API 地址."""
    if not api_url:
        return ""
    if api_url.endswith("/sendMessage"):
        return api_url[: -len("sendMessage")] + method
    return f"{api_url.rstrip('/')}/{method}"


def tg_delivery(cfg: dict[str, object]) -> tuple[str, int | None]:
    """Agent 推送目标 chat_id 与 forum topic thread_id."""
    chat = str(cfg.get("TG_DEST_CHAT_ID") or cfg.get("CHAT_ID") or "")
    raw = cfg.get("MESSAGE_THREAD_ID", "")
    thread = int(raw) if str(raw).isdigit() else None
    return chat, thread


def tg_topic_bot_id(cfg: dict[str, object]) -> int | None:
    raw = cfg.get("TOPIC_BOT_MESSAGE_ID", "")
    return int(raw) if str(raw).isdigit() else None


def apply_thread(payload: dict[str, object], thread_id: int | None) -> dict[str, object]:
    if thread_id:
        payload["message_thread_id"] = thread_id
    return payload


def _parse_message_id(raw: str) -> int | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    mid = result.get("message_id")
    return int(mid) if mid else None


def tg_post(
    api_url: str,
    payload: dict[str, object],
    *,
    timeout: int = 30,
) -> tuple[bool, str, int | None]:
    """
    发送 Telegram JSON。Markdown 解析失败时去掉 parse_mode 重试一次。
    返回 (成功, 错误或响应摘要, 新 message_id).
    """

    def _send(body: dict[str, object]) -> tuple[bool, str, int | None]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode(errors="ignore")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, str(exc), None
        if '"ok":true' in raw:
            return True, "", _parse_message_id(raw)
        if "message is not modified" in raw.lower():
            return True, "", None
        if "message to delete not found" in raw.lower():
            return True, "", None
        return False, raw[:240], None

    ok, err, mid = _send(payload)
    if ok:
        return True, "", mid
    desc = err.lower()
    if payload.get("parse_mode") and (
        "can't parse" in desc or "parse entities" in desc or "can't find end" in desc
    ):
        plain = {k: v for k, v in payload.items() if k != "parse_mode"}
        ok2, err2, mid2 = _send(plain)
        if ok2:
            return True, "plain", mid2
        return False, err2, None
    return False, err, None


def tg_delete(
    api_url: str,
    chat_id: str,
    message_id: int,
    thread_id: int | None,
    *,
    timeout: int = 15,
) -> None:
    """删除旧消息；不存在时忽略."""
    url = tg_method_url(api_url, "deleteMessage")
    if not url:
        return
    body: dict[str, object] = {"chat_id": chat_id, "message_id": message_id}
    apply_thread(body, thread_id)
    tg_post(url, body, timeout=timeout)


def tg_push(cfg: dict[str, object], payload: dict[str, object], *, timeout: int = 30) -> tuple[bool, str]:
    """
    统一推送：话题模式先发新消息，成功后再删旧 Bot 消息，并更新 TOPIC_BOT_MESSAGE_ID。
    先发后删确保发送失败时旧消息保留，不留空白。
    非话题模式走 sendMessage。
    """
    api_url = str(cfg.get("TG_API_URL") or "")
    chat, thread = tg_delivery(cfg)
    if not api_url or not chat:
        return False, "TG not configured"

    body = dict(payload)
    bot_msg_id = tg_topic_bot_id(cfg)

    if thread:
        body["chat_id"] = chat
        body.pop("message_id", None)
        apply_thread(body, thread)
        ok, err, new_id = tg_post(api_url, body, timeout=timeout)
        if ok:
            if bot_msg_id:
                tg_delete(api_url, chat, bot_msg_id, thread, timeout=timeout)
            if new_id:
                save_config_keys({"TOPIC_BOT_MESSAGE_ID": str(new_id)})
                cfg["TOPIC_BOT_MESSAGE_ID"] = str(new_id)
        return ok, err

    body["chat_id"] = chat
    apply_thread(body, thread)
    ok, err, _ = tg_post(api_url, body, timeout=timeout)
    return ok, err
