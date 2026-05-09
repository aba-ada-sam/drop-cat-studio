"""Telegram push notifications for job completion.

Usage:
    from core.telegram_notifier import notify_job, send_message
"""
import logging
import threading

import requests

_log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


def send_message(token: str, chat_id: str, text: str) -> bool:
    """POST a message to a Telegram chat. Returns True on success."""
    if not token or not chat_id:
        return False
    try:
        url = _API.format(token=token, method="sendMessage")
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        if not r.ok:
            _log.warning("[telegram] Send failed: %s %s", r.status_code, r.text[:200])
        return r.ok
    except Exception as exc:
        _log.warning("[telegram] Send error: %s", exc)
        return False


def fetch_chat_id(token: str) -> str | None:
    """Return the chat_id of the first user who messaged this bot, or None."""
    if not token:
        return None
    try:
        url = _API.format(token=token, method="getUpdates")
        r = requests.get(url, params={"limit": 10, "timeout": 0}, timeout=10)
        data = r.json()
        updates = data.get("result", [])
        for upd in updates:
            msg = upd.get("message") or upd.get("channel_post")
            if msg and "chat" in msg:
                return str(msg["chat"]["id"])
        return None
    except Exception as exc:
        _log.warning("[telegram] fetch_chat_id error: %s", exc)
        return None


def notify_job(job) -> None:
    """Send a Telegram notification for a finished job. Fire-and-forget."""
    from core import config as cfg
    token   = cfg.get("telegram_bot_token") or ""
    chat_id = cfg.get("telegram_chat_id") or ""
    if not token or not chat_id:
        return

    status = job.status
    label  = job.label or job.type
    elapsed = ""
    if job.started_at and job.finished_at:
        secs = int(job.finished_at - job.started_at)
        elapsed = f" ({secs // 60}m {secs % 60}s)" if secs >= 60 else f" ({secs}s)"

    if status == "done":
        icon = "Done"
    elif status == "error":
        icon = "Failed"
    elif status == "stopped":
        icon = "Stopped"
    else:
        return  # don't notify for cancelled / queued

    msg = f"[DCS] {icon}: {label}{elapsed}"
    if status == "error" and job.error:
        msg += f"\n{job.error[:200]}"

    threading.Thread(target=send_message, args=(token, chat_id, msg), daemon=True).start()
