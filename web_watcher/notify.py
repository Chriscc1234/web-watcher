"""
Notification senders — Telegram and email.

Both are independently callable and independently failable.
A failure in one never blocks the other.

Notification content (spec Section 4.5):
  - Watch name
  - Summary from ReasoningResult
  - Link (if any)
  - Timestamp
  - Screenshot attached if the match came from the vision path
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx

from web_watcher.config import NotificationsConfig
from web_watcher.reasoning import ReasoningResult

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Notification payload
# ---------------------------------------------------------------------------

@dataclass
class NotificationPayload:
    watch_name:      str
    result:          ReasoningResult
    timestamp:       datetime
    screenshot_bytes: Optional[bytes] = None   # attached only if vision path was used


# ---------------------------------------------------------------------------
# Public send functions
# ---------------------------------------------------------------------------

def send_telegram(payload: NotificationPayload, cfg: NotificationsConfig) -> bool:
    """
    Send a Telegram message via the Bot API.
    Returns True on success, False on any failure (logs the error).
    """
    t = cfg.telegram
    if not t.bot_token or not t.chat_id:
        log.warning("Telegram not configured — skipping notification for %r", payload.watch_name)
        return False

    text = _format_message(payload)

    try:
        with httpx.Client(timeout=TELEGRAM_TIMEOUT) as client:
            # Send text message first
            r = client.post(
                f"{TELEGRAM_API}/bot{t.bot_token}/sendMessage",
                json={
                    "chat_id":    t.chat_id,
                    "text":       text,
                    "parse_mode": "HTML",
                },
            )
            r.raise_for_status()

            # Attach screenshot if present
            if payload.screenshot_bytes:
                img_r = client.post(
                    f"{TELEGRAM_API}/bot{t.bot_token}/sendPhoto",
                    data={"chat_id": t.chat_id, "caption": f"Screenshot: {payload.watch_name}"},
                    files={"photo": ("screenshot.png", payload.screenshot_bytes, "image/png")},
                )
                img_r.raise_for_status()

        log.info("Telegram notification sent for %r", payload.watch_name)
        return True

    except httpx.HTTPStatusError as exc:
        log.error(
            "Telegram HTTP error for %r: %s — %s",
            payload.watch_name, exc.response.status_code, exc.response.text[:200],
        )
    except Exception as exc:
        log.error("Telegram send failed for %r: %s", payload.watch_name, exc)
    return False


def send_email(payload: NotificationPayload, cfg: NotificationsConfig) -> bool:
    """
    Send an SMTP email notification.
    Returns True on success, False on any failure (logs the error).
    """
    e = cfg.email
    if not all([e.from_address, e.app_password, e.to_address, e.smtp_server]):
        log.warning("Email not configured — skipping notification for %r", payload.watch_name)
        return False

    subject = f"[Web Watcher] {payload.watch_name} — match found"
    body_text = _format_message(payload, html=False)
    body_html = _format_message(payload, html=True)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = e.from_address
    msg["To"]      = e.to_address

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text, "plain"))
    alt.attach(MIMEText(body_html, "html"))
    msg.attach(alt)

    if payload.screenshot_bytes:
        img = MIMEImage(payload.screenshot_bytes, _subtype="png", name="screenshot.png")
        img.add_header("Content-Disposition", "attachment", filename="screenshot.png")
        msg.attach(img)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(e.smtp_server, e.smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(e.from_address, e.app_password)
            server.sendmail(e.from_address, e.to_address, msg.as_string())

        log.info("Email notification sent for %r", payload.watch_name)
        return True

    except smtplib.SMTPAuthenticationError as exc:
        log.error("Email auth failed for %r: %s", payload.watch_name, exc)
    except smtplib.SMTPException as exc:
        log.error("SMTP error for %r: %s", payload.watch_name, exc)
    except Exception as exc:
        log.error("Email send failed for %r: %s", payload.watch_name, exc)
    return False


def send_notifications(
    payload:     NotificationPayload,
    cfg:         NotificationsConfig,
    use_telegram: bool = True,
    use_email:    bool = True,
) -> dict[str, bool]:
    """
    Fire all enabled notification channels.
    Each channel is attempted independently; a failure in one never blocks the other.
    Returns a dict of channel -> success for the run history log.
    """
    results: dict[str, bool] = {}

    if use_telegram:
        results["telegram"] = send_telegram(payload, cfg)

    if use_email:
        results["email"] = send_email(payload, cfg)

    return results


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_message(payload: NotificationPayload, html: bool = True) -> str:
    r    = payload.result
    ts   = payload.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    conf = r.confidence.upper()
    link_line = f"\nLink: {r.link}" if r.link else ""

    if not html:
        return (
            f"Web Watcher Alert\n"
            f"Watch:   {payload.watch_name}\n"
            f"Summary: {r.summary}\n"
            f"Confidence: {conf}{link_line}\n"
            f"Time: {ts}"
        )

    link_html = (
        f'<br><b>Link:</b> <a href="{r.link}">{r.link}</a>'
        if r.link else ""
    )
    confidence_color = {"HIGH": "#4ade80", "MEDIUM": "#fbbf24", "LOW": "#f87171"}.get(conf, "#e2e8f0")

    return f"""\
<html><body style="font-family:sans-serif;color:#e2e8f0;background:#1a1d27;padding:20px;">
  <h2 style="color:#60a5fa;">Web Watcher Alert</h2>
  <table style="border-collapse:collapse;">
    <tr><td style="padding:4px 12px 4px 0;color:#8892a4;">Watch</td>
        <td style="padding:4px 0;"><b>{payload.watch_name}</b></td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#8892a4;">Summary</td>
        <td style="padding:4px 0;">{r.summary}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#8892a4;">Confidence</td>
        <td style="padding:4px 0;"><span style="color:{confidence_color};">{conf}</span></td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#8892a4;">Time</td>
        <td style="padding:4px 0;">{ts}</td></tr>
  </table>
  {link_html}
</body></html>"""
