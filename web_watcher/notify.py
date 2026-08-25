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

import html as _html
import logging
import re
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

def send_telegram(payload: NotificationPayload, cfg: NotificationsConfig,
                  chat_id_override: str = "") -> bool:
    """
    Send a Telegram message via the Bot API.
    Returns True on success, False on any failure (logs the error).

    chat_id_override: when a watch has an OWNER (a Telegram chat_id), the alert goes to THAT
    person, not the main chat — so a friend gets their own watch's finds. Falls back to the
    configured chat when empty.
    """
    t = cfg.telegram
    chat_id = str(chat_id_override or "").strip() or t.chat_id
    if not t.bot_token or not chat_id:
        log.warning("Telegram not configured — skipping notification for %r", payload.watch_name)
        return False

    text = _format_telegram(payload)

    # Tap-to-vet: a button on the alert runs Deep Inspect on this listing (deal + scam risk) and
    # replies with the verdict, so a find can be judged from the phone without opening the app.
    body: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if payload.result.link:
        tok = remember_vet_link(payload.result.link, _listing_title(payload))
        # Two matching buttons: open it, or have The Watcher vet it. A url button looks and taps
        # the same as the callback one, so the pair reads as one control strip.
        body["reply_markup"] = {"inline_keyboard": [[
            {"text": "🔗 Open listing", "url": payload.result.link},
            {"text": "🔍 Vet this listing", "callback_data": f"{_VET_PREFIX}{tok}"},
        ]]}

    # A photo makes a listing readable at a glance — you know whether it's worth opening before
    # you read a word. Telegram will fetch the thumbnail we already stored and put it ABOVE the
    # text as one message (sendPhoto with an HTML caption), buttons and all. Two limits decide
    # whether we can: a caption maxes out at 1024 characters, and Telegram fetches the image
    # itself, so a URL it can't reach fails the whole send. Both fall back to the plain message
    # rather than risk losing an alert over a picture.
    photo = str(_known_facts(payload.result.link).get("image") or "").strip()
    _TG_CAPTION_MAX = 1024

    try:
        with httpx.Client(timeout=TELEGRAM_TIMEOUT) as client:
            sent = False
            if photo.startswith("http") and len(text) <= _TG_CAPTION_MAX:
                photo_body = {k: v for k, v in body.items() if k != "text"}
                photo_body.update(photo=photo, caption=text)
                try:
                    pr = client.post(f"{TELEGRAM_API}/bot{t.bot_token}/sendPhoto", json=photo_body)
                    pr.raise_for_status()
                    sent = True
                except Exception as exc:
                    log.info("Telegram: photo send failed (%s) — sending as text instead", exc)

            if not sent:
                r = client.post(f"{TELEGRAM_API}/bot{t.bot_token}/sendMessage", json=body)
                r.raise_for_status()

            # Attach screenshot if present
            if payload.screenshot_bytes:
                img_r = client.post(
                    f"{TELEGRAM_API}/bot{t.bot_token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": f"Screenshot: {payload.watch_name}"},
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
    owner_chat_id: str = "",
) -> dict[str, bool]:
    """
    Fire all enabled notification channels.
    Each channel is attempted independently; a failure in one never blocks the other.
    Returns a dict of channel -> success for the run history log.

    owner_chat_id: if the watch belongs to a specific person (a Telegram chat_id), their
    alerts go to them instead of the main chat. Empty = the main configured chat.
    """
    results: dict[str, bool] = {}

    if use_telegram:
        results["telegram"] = send_telegram(payload, cfg, chat_id_override=owner_chat_id)

    if use_email:
        results["email"] = send_email(payload, cfg)

    return results


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

# --- "Vet this listing" buttons -------------------------------------------------
# An alert carries a tap-to-vet button. Telegram caps callback_data at 64 bytes, so we send a
# short token and keep token -> URL in a small file the bridge reads back when the button is
# tapped (survives a restart; capped so it can't grow forever). See telegram_bot._handle_callback.
_VET_PREFIX = "vet:"


def _vet_store_path():
    from web_watcher import paths
    return paths.data_dir() / "vet_links.json"


def vet_token(url: str) -> str:
    """A short, stable token for this listing URL."""
    import hashlib
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def remember_vet_link(url: str, title: str = "") -> str:
    """Record url (and what it was called) under its token so a tapped button can be resolved
    later. The TITLE matters: a verdict may arrive long after the alert has scrolled away, so the
    reply restates what it's judging instead of a bare rating. Returns the token."""
    tok = vet_token(url)
    try:
        import json
        p = _vet_store_path()
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        data[tok] = {"url": url, "title": title} if title else url
        if len(data) > 500:                      # keep the newest ~500 only
            data = dict(list(data.items())[-500:])
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        log.debug("could not record vet link: %s", exc)
    return tok


def vet_entry_for(token: str) -> dict:
    """{"url", "title"} behind a vet token ({} if unknown). Tolerates the older bare-string
    form so buttons from alerts sent before titles were stored still work."""
    try:
        import json
        p = _vet_store_path()
        if p.exists():
            v = (json.loads(p.read_text(encoding="utf-8")) or {}).get(token)
            if isinstance(v, dict):
                return {"url": str(v.get("url") or ""), "title": str(v.get("title") or "")}
            if isinstance(v, str) and v:
                return {"url": v, "title": ""}
    except Exception:
        pass
    return {}


def vet_url_for(token: str) -> str:
    """The listing URL behind a vet token, or "" if unknown."""
    return vet_entry_for(token).get("url", "")


def _tg(s, quote: bool = False) -> str:
    """Escape text for Telegram's HTML parse mode (only &, <, > matter; quote for href)."""
    return _html.escape(str(s or ""), quote=quote)


def _listing_title(payload: NotificationPayload) -> str:
    """The human name of the find, pulled off the alert summary ("★★★☆☆ New match: <title> — $x").
    Used so a vet verdict can restate WHAT it judged, since the alert may have scrolled away."""
    first = (str(getattr(payload.result, "summary", "") or "").strip().splitlines() or [""])[0]
    first = re.sub(r"^[★☆\s]+", "", first)
    first = re.sub(r"^New match:\s*", "", first, flags=re.I)
    return first.strip()[:120]


def _format_telegram(payload: NotificationPayload) -> str:
    """A Telegram-native alert. Telegram's HTML mode allows ONLY a small tag set (<b>, <i>,
    <a>, <code>…) — NOT full documents. The email formatter returns a whole <html><body><table>
    doc, which Telegram rejects ("Unsupported start tag html"), so alerts silently failed. This
    builds a compact, phone-friendly message with just the essentials + a tappable link."""
    r = payload.result
    conf = (r.confidence or "").upper()
    icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(conf, "🔔")
    lines = [f"{icon} <b>{_tg(payload.watch_name)}</b>"]
    summary = (r.summary or "").strip()
    if summary:
        lines += ["", _tg(summary)]
    # Marketplace finds carry a ★ rating that IS the quality signal — the generic "confidence"
    # field is hardcoded HIGH for those and just adds noise, so show it only when there's no
    # rating (e.g. a yes/no watch like a weather warning, where confidence is meaningful).
    has_rating = "★" in summary
    if conf and not has_rating:
        lines += ["", f"<b>{conf}</b> confidence"]

    # WHERE it came from. "Is this Craigslist or Facebook?" changes how you read a listing and
    # how you approach the seller, and it's the first thing you want to know on a phone. We
    # already store the source when the watch finds it, so there's no reason to make him guess.
    facts = _known_facts(r.link)
    bits = []
    if facts.get("source"):
        bits.append(f"📍 {_tg(source_label(facts['source']))}")
    if facts.get("price_text") and "$" not in summary:
        bits.append(f"💵 {_tg(facts['price_text'])}")
    if bits:
        lines += ["", "  ".join(bits)]

    if r.link:
        lines.append(f'🔗 <a href="{_tg(r.link, quote=True)}">Open listing</a>')
    return "\n".join(lines)


# Pretty names for the sites we watch — "facebook.com" is not what a person calls it.
_SOURCE_NAMES = {
    "facebook": "Facebook Marketplace",
    "craigslist": "Craigslist",
    "offerup": "OfferUp",
    "ebay": "eBay",
    "mercari": "Mercari",
    "nextdoor": "Nextdoor",
    "govdeals": "GovDeals",
    "publicsurplus": "Public Surplus",
}


def source_label(source: str) -> str:
    """A human name for a listing's source site ('facebook.com' → 'Facebook Marketplace')."""
    s = (source or "").strip().lower()
    if not s:
        return ""
    for key, name in _SOURCE_NAMES.items():
        if key in s:
            return name
    return s.replace("www.", "").split("/")[0]        # unknown site: show its host, tidied


def _known_facts(url: str) -> dict:
    """What we already stored about this listing (source, price, image…). Never raises — an
    alert must go out even if the lookup fails."""
    try:
        from web_watcher import storage
        return storage.get_listing_by_url(url or "") or {}
    except Exception:
        return {}


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

    # Escape dynamic fields for HTML (summaries are RAW now — the sender no longer pre-escapes).
    watch_name_h = _html.escape(str(payload.watch_name or ""))
    summary_h    = _html.escape(str(r.summary or "")).replace("\n", "<br>")
    return f"""\
<html><body style="font-family:sans-serif;color:#e2e8f0;background:#1a1d27;padding:20px;">
  <h2 style="color:#60a5fa;">Web Watcher Alert</h2>
  <table style="border-collapse:collapse;">
    <tr><td style="padding:4px 12px 4px 0;color:#8892a4;">Watch</td>
        <td style="padding:4px 0;"><b>{watch_name_h}</b></td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#8892a4;">Summary</td>
        <td style="padding:4px 0;">{summary_h}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#8892a4;">Confidence</td>
        <td style="padding:4px 0;"><span style="color:{confidence_color};">{conf}</span></td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#8892a4;">Time</td>
        <td style="padding:4px 0;">{ts}</td></tr>
  </table>
  {link_html}
</body></html>"""


def send_plain_telegram(text: str, cfg: NotificationsConfig, chat_id_override: str = "") -> bool:
    """Send one plain-text Telegram message (no listing payload, no parse mode). For the app's
    own notices — a finished self-review, a heads-up — where there is no match to format."""
    t = cfg.telegram
    chat_id = str(chat_id_override or "").strip() or t.chat_id
    if not t.bot_token or not chat_id or not (text or "").strip():
        return False
    try:
        with httpx.Client(timeout=TELEGRAM_TIMEOUT) as client:
            r = client.post(f"{TELEGRAM_API}/bot{t.bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": text})
            r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Telegram plain message failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Baseline briefing — what to do with a backlog we deliberately didn't alert on
# ---------------------------------------------------------------------------
#
# A new or newly-changed watch finds hundreds of listings that were already there. Alerting on
# all of them would be a wall of noise, so we bank them silently — but silence looks exactly
# like a broken watch from the outside ("it never notified me about any boats"). So we send ONE
# message with the numbers and buttons to act on the backlog we just banked.

_TOP_PREFIX = "top:"
_BRIEF_PREFIX = "brief:"


def brief_token(watch_name: str) -> str:
    """A short token for a watch name — Telegram caps callback_data at 64 bytes, and watch names
    are long."""
    import hashlib
    return hashlib.sha1((watch_name or "").encode("utf-8")).hexdigest()[:16]


def remember_brief(watch_name: str) -> str:
    """Store watch_name under its token so a tapped button can resolve it. Shares the vet store
    (one small file, two kinds of key) — keyed under a prefix so they can't collide."""
    tok = brief_token(watch_name)
    try:
        import json
        p = _vet_store_path()
        data = {}
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        data[_BRIEF_PREFIX + tok] = {"watch": watch_name}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data)[:400_000], encoding="utf-8")
    except Exception as exc:
        log.debug("could not store the briefing token: %s", exc)
    return tok


def watch_for_brief(token: str) -> str:
    """The watch name behind a briefing token, or ''."""
    try:
        import json
        p = _vet_store_path()
        if not p.exists():
            return ""
        entry = (json.loads(p.read_text(encoding="utf-8")) or {}).get(_BRIEF_PREFIX + token)
        return str((entry or {}).get("watch") or "")
    except Exception:
        return ""


def send_baseline_briefing(watch_name: str, seen: int, matched: int, cfg: NotificationsConfig,
                           owner_chat_id: str = "") -> bool:
    """One message: what the watch just banked, and what you can do about it."""
    t = cfg.telegram
    chat_id = str(owner_chat_id or "").strip() or t.chat_id
    if not t.bot_token or not chat_id or seen <= 0:
        return False

    tok = remember_brief(watch_name)
    if matched:
        line = (f"{matched} of them look worth a second glance. I didn't alert on each one — "
                "they were already posted before this watch started looking.")
    else:
        line = ("None of them matched what you asked for. They were already posted before this "
                "watch started looking, so nothing was worth alerting on.")
    text = (f"📋 {watch_name}\n\nFirst proper look: {seen} listings.\n{line}\n\n"
            "From here on you'll get an alert for anything NEW.")
    buttons = []
    if matched:
        buttons = [[{"text": "⭐ Show top 10", "callback_data": f"{_TOP_PREFIX}{tok}:10"},
                    {"text": "Show top 20", "callback_data": f"{_TOP_PREFIX}{tok}:20"}]]
        text += "\n\nWant them now? Tap below — or just ask me, e.g. “show me the boats with outboards”."

    body: dict = {"chat_id": chat_id, "text": text}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    try:
        with httpx.Client(timeout=TELEGRAM_TIMEOUT) as client:
            r = client.post(f"{TELEGRAM_API}/bot{t.bot_token}/sendMessage", json=body)
            r.raise_for_status()
        log.info("Baseline briefing sent for %r (%d seen, %d matched)", watch_name, seen, matched)
        return True
    except Exception as exc:
        log.warning("Baseline briefing failed for %r: %s", watch_name, exc)
        return False
