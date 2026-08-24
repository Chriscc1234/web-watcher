r"""
Two-way Telegram — talk to The Watcher from your phone like you'd text a person.

Until now Telegram was OUTBOUND only (alerts). This adds the inbound half: a background
thread long-polls Telegram for messages you send the bot, runs each one through THE SAME
assistant that powers the in-app "Ask The Watcher" dock, and texts the reply back. Because
alerts already go out over this channel, your phone becomes ONE continuous conversation:

    🚨 "found a diesel Tacoma, $9,500"   ← alert
    "is it 4x4?"                          ← you
    "Yes — 4WD, 118k miles, manual."      ← The Watcher

Design notes
  • THIN CLIENT, NOT A SECOND BRAIN. We POST to the app's own /api/oversight/chat on
    127.0.0.1, so the phone shares the dashboard's history, watch context and behaviour.
    There is exactly one assistant; Telegram is just another front door to it.
  • LONG POLLING, NOT A WEBHOOK. The app runs on a home PC with no public URL for Telegram
    to call back to, so we poll getUpdates with a held-open request (cheap + instant).
  • ⚠ SENDER ALLOWLIST. A bot token is effectively public — anyone who learns the bot's
    name can message it. We ONLY answer the configured chat_id and ignore everything else,
    so a stranger can never drive the app. See _authorized().

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  TelegramBridge.start/stop  ~L70   Thread lifecycle (daemon; owned by ServiceManager)
  _loop                      ~L110  Long-poll getUpdates → dispatch → reply
  _authorized                ~L165  The sender allowlist (security boundary)
  _notify_access_request     ~L205  A stranger knocked → alert the admin + park for approval
  _handle_message            ~L180  One inbound message → assistant turn → reply
  _apply_actions             ~L255  Carry out grounded start/stop/enable/disable/delete actions
  _ask_watcher               ~L290  Calls the app's own chat API (shared history + brain)
  _send / _typing            ~L285  Outbound helpers (per-sender target + 4096-char chunking)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

_POLL_TIMEOUT   = 30      # seconds Telegram holds the getUpdates request open
_HTTP_TIMEOUT   = 45.0    # must exceed _POLL_TIMEOUT so the long poll isn't cut short
_MSG_LIMIT      = 4096    # Telegram's hard per-message character cap
_ERROR_BACKOFF  = 15.0    # pause after a transport error so we don't hammer the API
_CHAT_TIMEOUT   = 180.0   # a local model turn can be slow; be patient before apologising

# Proactive check-ins: even when nothing new turns up, ping each person on a cadence so they
# know the Watcher is alive — and, when it's been quiet, OFFER to broaden the search or vet a
# find. Suppressed whenever they were recently contacted (an alert, a reply, or a prior check-in).
_HEARTBEAT_EVERY_S = 6 * 3600     # a quiet check-in at most this often, per person
_HEARTBEAT_SCAN_S  = 20 * 60      # how often the loop evaluates whether one is due


class TelegramBridge:
    """Background poller that answers Telegram messages using the app's own assistant.

    Owned by ServiceManager: start() on app start, stop() on shutdown. Never raises out of
    the thread — a Telegram or model failure is logged and the loop continues, because this
    must never be able to take down a running watch.
    """

    def __init__(self, bot_token: str, chat_id: str, dashboard_url: str,
                 allowed_chat_ids: list[str] | None = None) -> None:
        self.bot_token     = (bot_token or "").strip()
        self.chat_id       = str(chat_id or "").strip()
        self.dashboard_url = dashboard_url.rstrip("/")
        # Everyone permitted to TALK to the bot: the alert chat plus any extra IDs (e.g. you and
        # your buddy). Anyone else is ignored — see _authorized.
        # NB: guard `c is not None` before str() — str(None) is "None", which is truthy and
        # would silently put a bogus entry in the allowlist.
        self.allowed: set[str] = {self.chat_id} | {
            str(c).strip() for c in (allowed_chat_ids or []) if c is not None and str(c).strip()
        }
        self.allowed.discard("")
        self._stop         = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None      # next update_id to fetch
        # Strangers we've already alerted the admin about, so one persistent knocker can't spam
        # the owner's phone. Reset on restart (re-alerting once after a restart is fine).
        self._access_notified: set[str] = set()
        # Watch changes the assistant proposed and is waiting on a yes/no for. The dashboard
        # shows these as click-to-confirm cards; on a phone the confirmation is the next message.
        self._pending: list[dict] | None = None
        # Destructive lifecycle actions (delete) held for a yes. Reversible ones (start/stop/
        # enable/disable) are applied immediately — no confirmation needed.
        self._pending_deletes: list[dict] | None = None
        # Proactive check-in bookkeeping. Seed "last contact" at startup so we don't fire the
        # moment the app launches; a real check-in is a full interval of quiet away.
        self._start_ts = time.time()
        self._last_heartbeat_scan = 0.0
        self._heartbeat_sent: dict[str, float] = {}   # owner chat_id -> last check-in ts

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def start(self) -> bool:
        """Start the poller. Returns False (and does nothing) when not configured."""
        if not self.configured:
            log.info("Telegram two-way chat not started — bot token / chat ID not set")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="telegram-bridge", daemon=True)
        self._thread.start()
        log.info("Telegram two-way chat started — text your bot to talk to The Watcher")
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            # Don't block shutdown on the in-flight long poll; the daemon thread dies with us.
            t.join(timeout=2.0)
        self._thread = None

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        # Skip whatever piled up while the app was closed: on first poll, jump to the newest
        # update. Otherwise every message sent while offline would be answered at once.
        try:
            self._offset = self._latest_offset()
        except Exception as exc:
            log.debug("Telegram: could not prime offset (%s) — starting from the backlog", exc)

        while not self._stop.is_set():
            try:
                for update in self._get_updates():
                    if self._stop.is_set():
                        break
                    try:
                        self._dispatch(update)
                    except Exception as exc:               # one bad message must not kill the loop
                        log.warning("Telegram: failed handling update: %s", exc)
            except Exception as exc:
                log.warning("Telegram poll failed (%s) — retrying in %.0fs", exc, _ERROR_BACKOFF)
                self._stop.wait(_ERROR_BACKOFF)
            # Between polls, see if anyone is due a proactive check-in (never fatal).
            try:
                self._maybe_run_heartbeats()
            except Exception as exc:
                log.debug("Telegram: heartbeat scan failed: %s", exc)

    def _latest_offset(self) -> int | None:
        """The offset just past the newest pending update, so we ignore the offline backlog."""
        r = httpx.get(f"{TELEGRAM_API}/bot{self.bot_token}/getUpdates",
                      params={"timeout": 0}, timeout=15.0)
        result = (r.json() or {}).get("result") or []
        return (result[-1]["update_id"] + 1) if result else None

    def _get_updates(self) -> list[dict]:
        params: dict = {"timeout": _POLL_TIMEOUT}
        if self._offset is not None:
            params["offset"] = self._offset
        r = httpx.get(f"{TELEGRAM_API}/bot{self.bot_token}/getUpdates",
                      params=params, timeout=_HTTP_TIMEOUT)
        data = r.json() or {}
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "getUpdates rejected"))
        updates = data.get("result") or []
        if updates:
            self._offset = updates[-1]["update_id"] + 1      # ack: never re-deliver these
        return updates

    def _dispatch(self, update: dict) -> None:
        msg  = update.get("message") or update.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        frm  = msg.get("from") or {}
        name = (" ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
                or frm.get("username") or (msg.get("chat") or {}).get("title") or "")
        if not text:
            return                                            # photo/sticker/etc — nothing to answer
        if not self._authorized(chat):
            log.warning("Telegram: unauthorized chat %s (%s) — alerting admin", chat, name)
            self._notify_access_request(str(chat), name)
            return
        self._handle_message(text, str(chat), name)

    def _authorized(self, chat_id) -> bool:
        """Security boundary: only the allowed chats may drive the app. A bot token is
        effectively public — anyone who finds the bot can message it — so without this a
        stranger could create watches, read your finds, and change what you're watching."""
        return str(chat_id) in self.allowed

    # ------------------------------------------------------------------
    # One message → assistant turn → reply
    # ------------------------------------------------------------------

    def _handle_message(self, text: str, sender: str = "", sender_name: str = "") -> None:
        log.info("Telegram message received from %s (%s): %r", sender, sender_name, text[:80])
        owner = sender or self.chat_id      # scope this turn to the person who sent it
        to = sender or self.chat_id         # ...and reply to THEM, not always the alert chat

        # A yes to a proposal we're holding APPLIES it — that's how a watch gets created, edited,
        # or deleted from the phone, standing in for the dashboard's confirm button.
        if self._pending or self._pending_deletes:
            sugg_pending, del_pending = self._pending, self._pending_deletes
            self._pending, self._pending_deletes = None, None
            if _is_affirmative(text):
                self._typing(to)
                out = []
                if del_pending:
                    out.append(self._apply_actions(del_pending))
                if sugg_pending:
                    out.append(self._apply_pending(sugg_pending))
                self._send(" ".join(x for x in out if x) or "Done.", to)
                return
            if _is_negative(text):
                self._send("Okay, left everything as it was.", to)
                return
            # Anything else: not an answer to the question — fall through and just chat.

        self._typing(to)
        try:
            result = self._ask_watcher(text, owner, sender_name)
        except Exception as exc:
            log.warning("Telegram: assistant turn failed: %s", exc)
            self._send("Sorry — I couldn't think that through just now. Try again in a moment.", to)
            return

        reply = (result.get("message") or "").strip()

        # Lifecycle actions the assistant grounded + owner-scoped for us. Reversible ones apply
        # right away (snappy — no "are you sure?" for a start/stop); delete needs a yes.
        actions = [a for a in (result.get("watch_actions") or []) if isinstance(a, dict)]
        deletes    = [a for a in actions if str(a.get("action", "")).lower() == "delete"]
        reversible = [a for a in actions if str(a.get("action", "")).lower()
                      in ("start", "stop", "enable", "disable")]
        if reversible:
            reply = (reply + "\n\n" + self._apply_actions(reversible)).strip()

        sugg = _suggestions_of(result)
        if deletes:
            self._pending_deletes = deletes
            names = ", ".join(f"“{a.get('name')}”" for a in deletes)
            reply = (reply + f"\n\n🗑 Delete {names}? Reply *yes* to confirm.").strip()
        elif sugg:
            self._pending = sugg
            reply = (reply + "\n\n" + _describe_suggestions(result)).strip()
        self._send(reply or "(no reply)", to)

    def _apply_pending(self, pending: list[dict]) -> str:
        """Create/update the proposed watches through the app's own API (the same endpoints the
        dashboard's confirm button uses), and report what happened in plain words."""
        done, failed = [], []
        for s in pending:
            name = str(s.get("name") or "").strip()
            body = {k: v for k, v in s.items() if k != "action"}
            try:
                if str(s.get("action") or "").lower() == "update" and name:
                    r = httpx.put(f"{self.dashboard_url}/api/watches/{quote(name)}",
                                  json=body, timeout=30.0)
                else:
                    r = httpx.post(f"{self.dashboard_url}/api/watches", json=body, timeout=30.0)
                if r.status_code < 300:
                    done.append(name or "watch")
                else:
                    detail = ""
                    try:
                        detail = str((r.json() or {}).get("detail", ""))[:120]
                    except Exception:
                        pass
                    failed.append(f"{name or 'watch'}{f' ({detail})' if detail else ''}")
            except Exception as exc:
                log.warning("Telegram: applying %r failed: %s", name, exc)
                failed.append(f"{name or 'watch'} ({exc})")

        parts = []
        if done:
            parts.append("✅ Done — " + ", ".join(f"“{n}”" for n in done) + ".")
        if failed:
            parts.append("⚠️ Couldn't apply " + ", ".join(f"“{n}”" for n in failed) + ".")
        return " ".join(parts) or "Nothing to apply."

    def _notify_access_request(self, chat_id: str, name: str) -> None:
        """A stranger messaged the bot. We DON'T let them in — but we alert the owner on their
        phone and park the request in the app so it can be approved with one click. Alert once
        per stranger per run so a persistent knocker can't spam the owner."""
        chat_id = str(chat_id or "").strip()
        if not chat_id or chat_id in self._access_notified:
            return
        self._access_notified.add(chat_id)
        who = name or "Someone"
        self._send(
            f"🔔 Access request\n{who} (chat id {chat_id}) messaged your Web Watcher bot but "
            f"isn't on your allow-list. Open the app → Chats to let them in, or add {chat_id} "
            f"under Settings → Notifications & Keys.",
            self.chat_id,                      # the owner/admin alert chat
        )
        # Park it for one-click approval in the console (best-effort; the phone alert is primary).
        try:
            httpx.post(f"{self.dashboard_url}/api/telegram/access-request",
                       json={"chat_id": chat_id, "name": name}, timeout=10.0)
        except Exception as exc:
            log.debug("Telegram: could not record access request (%s)", exc)
        # Let the knocker know a human has to approve them, so silence doesn't read as broken.
        self._send("Thanks — you're not on the allow-list yet. I've asked the owner to grant "
                   "you access.", chat_id)

    def _apply_actions(self, actions: list[dict]) -> str:
        """Apply grounded lifecycle actions (start/stop/enable/disable/delete) through the app's
        own mode-aware endpoint — the same one the dashboard buttons use. Ownership was already
        enforced server-side when the action was produced, so we just carry it out and report."""
        labels = {"start": "started", "stop": "stopped", "enable": "enabled",
                  "disable": "disabled", "delete": "deleted"}
        done, failed = [], []
        for a in actions:
            name = str(a.get("name") or "").strip()
            act  = str(a.get("action") or "").strip().lower()
            if not name or not act:
                continue
            try:
                r = httpx.post(f"{self.dashboard_url}/api/watches/{quote(name)}/action",
                               json={"action": act}, timeout=30.0)
                if r.status_code < 300:
                    note = ""
                    try:
                        if act == "start" and (r.json() or {}).get("paused"):
                            note = " (the whole Watcher is paused — resume it to actually run)"
                    except Exception:
                        pass
                    done.append(f"{labels.get(act, act)} “{name}”{note}")
                else:
                    failed.append(name)
            except Exception as exc:
                log.warning("Telegram: action %s on %r failed: %s", act, name, exc)
                failed.append(f"{name} ({exc})")
        parts = []
        if done:
            parts.append("✅ " + "; ".join(done) + ".")
        if failed:
            parts.append("⚠️ Couldn't: " + ", ".join(failed) + ".")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Proactive check-ins (heartbeats)
    # ------------------------------------------------------------------

    def _maybe_run_heartbeats(self) -> None:
        now = time.time()
        if now - self._last_heartbeat_scan < _HEARTBEAT_SCAN_S:
            return
        self._last_heartbeat_scan = now
        self._run_heartbeats(now)

    def _run_heartbeats(self, now: float) -> None:
        """For each person, if it's been quiet for a while (no alert, no chat, no prior check-in),
        send a reassuring note + an offer to broaden or vet. The main chat also covers unassigned
        watches (the admin manages those); a buddy sees only their own."""
        watches = self._fetch_watches()
        if not watches:
            return
        for owner in sorted(self.allowed):
            if owner == self.chat_id:
                owned = [w for w in watches if str(w.get("owner") or "") in ("", str(owner))]
            else:
                owned = [w for w in watches if str(w.get("owner") or "") == str(owner)]
            owned = [w for w in owned if w.get("enabled")]      # only what's actually watching
            if not owned:
                continue
            last = self._owner_last_activity(owner, owned)
            if now - last < _HEARTBEAT_EVERY_S:
                continue                                        # recently in touch — stay quiet
            self._send(_heartbeat_message(owned, now - last), owner)
            self._heartbeat_sent[owner] = now
            log.info("Telegram: sent a proactive check-in to %s", owner)

    def _fetch_watches(self):
        try:
            r = httpx.get(f"{self.dashboard_url}/api/watches", timeout=15.0)
            if r.status_code == 200 and isinstance(r.json(), list):
                return r.json()
        except Exception as exc:
            log.debug("Telegram: could not fetch watches for heartbeat (%s)", exc)
        return None

    def _owner_last_activity(self, owner: str, owned: list) -> float:
        """The most recent moment we were 'in touch' with this person: their last chat message,
        their watches' last match, or the last check-in we sent (seeded at startup so we never
        fire right after launch)."""
        times = [self._heartbeat_sent.get(owner, self._start_ts), self._owner_last_chat_ts(owner)]
        for w in owned:
            times.append(_parse_iso((w.get("stats") or {}).get("last_match_at")))
        return max([t for t in times if t] or [self._start_ts])

    def _owner_last_chat_ts(self, owner: str) -> float:
        try:
            r = httpx.get(f"{self.dashboard_url}/api/oversight/chat/history",
                          params={"owner": owner}, timeout=15.0)
            if r.status_code == 200:
                hist = r.json()
                if isinstance(hist, list) and hist:
                    return float(hist[-1].get("ts") or 0.0)
        except Exception:
            pass
        return 0.0

    def _ask_watcher(self, text: str, owner: str = "", owner_name: str = "") -> dict:
        """Run the message through the app's OWN chat endpoint, so Telegram and the in-app dock
        share one brain. `owner` scopes the turn to this person — their own watches and their own
        chat thread. Sends prior history for continuity; the endpoint persists the turn itself."""
        history: list[dict] = []
        try:
            r = httpx.get(f"{self.dashboard_url}/api/oversight/chat/history",
                          params={"owner": owner} if owner else None, timeout=15.0)
            if r.status_code == 200:
                loaded = r.json()
                if isinstance(loaded, list):
                    # Only the fields the endpoint needs; trim so the prompt stays bounded.
                    history = [{"role": m.get("role", "user"), "content": str(m.get("content", ""))}
                               for m in loaded[-20:]
                               if isinstance(m, dict) and m.get("content")]
        except Exception as exc:
            log.debug("Telegram: could not load chat history (%s) — starting fresh", exc)

        messages = history + [{"role": "user", "content": text}]
        body = {"messages": messages}
        if owner:
            body["owner"] = owner
        if owner_name:
            body["owner_name"] = owner_name
        r = httpx.post(f"{self.dashboard_url}/api/oversight/chat", json=body, timeout=_CHAT_TIMEOUT)
        r.raise_for_status()
        return r.json() or {}

    # ------------------------------------------------------------------
    # Outbound helpers
    # ------------------------------------------------------------------

    def _typing(self, chat_id: str = "") -> None:
        """Show Telegram's '…is typing' while the model thinks — a local turn isn't instant and
        silence reads as broken. Defaults to the alert chat when no target is given."""
        try:
            httpx.post(f"{TELEGRAM_API}/bot{self.bot_token}/sendChatAction",
                       json={"chat_id": chat_id or self.chat_id, "action": "typing"}, timeout=10.0)
        except Exception:
            pass

    def _send(self, text: str, chat_id: str = "") -> None:
        target = chat_id or self.chat_id       # reply to the sender; fall back to the alert chat
        for chunk in _chunk(text, _MSG_LIMIT):
            try:
                httpx.post(f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage",
                           json={"chat_id": target, "text": chunk,
                                 "disable_web_page_preview": True}, timeout=20.0)
            except Exception as exc:
                log.warning("Telegram: reply send failed: %s", exc)
                return


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def _parse_iso(s) -> float:
    """An ISO timestamp string → epoch seconds, or 0.0 if unparseable/empty."""
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _heartbeat_message(owned: list, quiet_s: float) -> str:
    """A quiet-period check-in: reassure that it's still watching, and offer to widen the net or
    vet a find so the user can act. `owned` is the person's enabled watches."""
    hrs = max(1, int(quiet_s // 3600))
    names = [f"“{w.get('name')}”" for w in owned]
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return (
        f"🔭 Still on watch — keeping an eye on {shown}. Nothing new in about the last {hrs}h.\n\n"
        f"Want me to widen the net? I can broaden the search terms, or vet a recent listing so you "
        f"can decide fast. Just say “broaden <watch>”, or ask me anything."
    )


def _chunk(text: str, limit: int) -> list[str]:
    """Split a reply into Telegram-sized pieces, preferring a line break near the edge so a
    message isn't cut mid-sentence."""
    t = text or ""
    if len(t) <= limit:
        return [t]
    out: list[str] = []
    while len(t) > limit:
        cut = t.rfind("\n", 0, limit)
        if cut < limit // 2:                 # no sensible break — hard split
            cut = limit
        out.append(t[:cut])
        t = t[cut:].lstrip("\n")
    if t:
        out.append(t)
    return out


_YES_RE = re.compile(r"^\s*(y|ye|yes|yep|yeah|yup|ok|okay|sure|do it|go|go ahead|apply|"
                     r"confirm|please do|sounds good|make it|save it)\b[\s.!]*$", re.I)
_NO_RE  = re.compile(r"^\s*(n|no|nope|nah|cancel|stop|don'?t|never ?mind|leave it)\b[\s.!]*$", re.I)


def _is_affirmative(text: str) -> bool:
    """Is this a plain yes to the proposal we're holding? Deliberately strict — a message that
    merely STARTS with 'ok' but goes on to say something else is a new request, not a yes."""
    return bool(_YES_RE.match(text or ""))


def _is_negative(text: str) -> bool:
    return bool(_NO_RE.match(text or ""))


def _suggestions_of(result: dict) -> list[dict]:
    """The watch proposals in an assistant turn, in both shapes the API returns."""
    sugg = result.get("watch_suggestions") or []
    if not sugg and result.get("watch_suggestion"):
        sugg = [result["watch_suggestion"]]
    return [s for s in sugg if isinstance(s, dict)]


def _describe_suggestions(result: dict) -> str:
    """The dashboard renders watch proposals as click-to-confirm cards; on a phone the confirm
    is the next message, so say what it will do and ask for a yes."""
    sugg = _suggestions_of(result)
    if not sugg:
        return ""
    verb = lambda s: "Edit" if str(s.get("action") or "").lower() == "update" else "New watch"
    names = [f"{verb(s)}: “{str(s.get('name') or 'untitled')}”" for s in sugg]
    if len(names) == 1:
        return f"📋 {names[0]}\n\nReply *yes* and I'll set it up."
    return ("📋 " + "\n📋 ".join(names) + "\n\nReply *yes* and I'll set them all up.")
