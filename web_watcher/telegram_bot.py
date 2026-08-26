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

import contextlib
import logging
import random
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
_HEARTBEAT_EVERY_S = 12 * 3600    # default quiet-period before a check-in (config overrides)
_HEARTBEAT_SCAN_S  = 20 * 60      # how often the loop evaluates whether one is due
_VET_TIMEOUT       = 180.0        # how long to wait for a Deep Inspect verdict before saying so
_TYPING_REFRESH_S  = 4.0          # re-send "typing" this often (Telegram expires it after ~5s)
_SLOW_TURN_S       = 12.0         # a hard turn says "hang on" at ~12s, and (if still going) again at 30s
_TOP_COOLDOWN_S    = 25.0         # ignore a second "Show top N" tap within this long (a burst takes a bit)

# A pool for each stage so the reassurance never reads the same twice in a row. The first fires at
# ~12s ("this is taking a moment"); the second, rarely, at ~30s ("still at it").
_THINKING_NUDGES = (
    "Hang on — this one's a bit trickier. Thinking it through…",
    "Give me a sec — working through this one…",
    "Hmm, this one needs a little more thought…",
    "One moment — chewing on this one…",
    "Bear with me — thinking this through…",
    "Hang tight — this one's got a few moving parts…",
    "Let me think on this one for a moment…",
    "Just a sec — digging into this one…",
)
_STILL_WORKING_NUDGES = (
    "Still working on it — hang tight…",
    "Still on it — won't be much longer…",
    "Almost there — thanks for your patience…",
    "Nearly done — appreciate you waiting…",
    "Still crunching this one — hang in there…",
)


class TelegramBridge:
    """Background poller that answers Telegram messages using the app's own assistant.

    Owned by ServiceManager: start() on app start, stop() on shutdown. Never raises out of
    the thread — a Telegram or model failure is logged and the loop continues, because this
    must never be able to take down a running watch.
    """

    def __init__(self, bot_token: str, chat_id: str, dashboard_url: str,
                 allowed_chat_ids: list[str] | None = None,
                 checkin_hours: float = 12.0) -> None:
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
        # A create that collided with an existing watch of the same name, whose settings DIFFER.
        # Held for the user to choose update / replace / leave. {"name", "body"}.
        self._pending_conflict: dict | None = None
        # Debounce the "Show top N" buttons: a top-20 sends 20 cards over several seconds, so
        # tapping 10 then 20 (or double-tapping) would spam two overlapping bursts. chat_id -> ts.
        self._top_cooldown: dict[str, float] = {}
        # Proactive check-in bookkeeping. Seed "last contact" at startup so we don't fire the
        # moment the app launches; a real check-in is a full interval of quiet away.
        self._start_ts = time.time()
        self._last_heartbeat_scan = 0.0
        self._heartbeat_sent: dict[str, float] = {}   # owner chat_id -> last check-in ts
        # Hours of quiet before a check-in (config: notifications.telegram.checkin_hours).
        # 0 disables proactive check-ins entirely.
        self.checkin_s = max(0.0, float(checkin_hours or 0)) * 3600.0

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
                    # Handle each update on its OWN thread so the poll loop keeps reading. A chat
                    # turn or a vet can take minutes on a local model; doing them inline froze the
                    # bot — a message sent during a vet just sat unanswered until it finished.
                    try:
                        t = threading.Thread(target=self._dispatch_safe, args=(update,),
                                             name="telegram-update", daemon=True)
                        t.start()
                    except Exception as exc:               # one bad message must not kill the loop
                        log.warning("Telegram: failed handling update: %s", exc)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # A long poll that times out (or a transient 502/reset) is NORMAL — Telegram just
                # held the request open with nothing to say. Reconnect straight away instead of
                # logging a scary warning and sleeping, which left the bot deaf for 15s each time.
                log.debug("Telegram poll reconnecting (%s)", exc)
                self._stop.wait(1.0)
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

    def _dispatch_safe(self, update: dict) -> None:
        """Thread entry point: one update, never raising out of the thread."""
        try:
            self._dispatch(update)
        except Exception as exc:
            log.warning("Telegram: failed handling update: %s", exc)

    def _dispatch(self, update: dict) -> None:
        cb = update.get("callback_query")
        if cb:
            self._handle_callback(cb)
            return
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

        # A name collision we surfaced is waiting on update / replace / leave — that answer is
        # about the existing watch, not a new request, so handle it before anything else.
        if self._pending_conflict:
            self._typing(to)
            self._resolve_conflict(text, to)
            return

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

        try:
            # Keep "typing" up for the whole wait, and if it runs long, say so — once at ~12s and,
            # only if it's STILL going at ~30s (uncommon), again. A fresh phrase each time so the
            # reassurance never reads the same twice.
            with self._typing_until_sent(to, slow_nudges=[
                    (_SLOW_TURN_S, random.choice(_THINKING_NUDGES)),
                    (30.0, random.choice(_STILL_WORKING_NUDGES))]):
                result = self._ask_watcher(text, owner, sender_name)
        except Exception as exc:
            log.warning("Telegram: assistant turn failed: %s", exc)
            self._send("Sorry — I couldn't think that through just now. Try again in a moment.", to)
            return

        reply = (result.get("message") or "").strip()
        # Is this text already HTML we built (e.g. the settings block)? Then send it as HTML.
        as_html = bool(result.get("html"))

        # "Show me the matches" — the assistant resolves a listing_query and returns rows; the
        # dashboard renders them as cards. On a phone we render a compact scannable list, so the
        # bot actually SHOWS finds instead of only talking about them. The list is HTML, so the
        # model's own prose must be escaped before the two are joined.
        listings = result.get("listings")
        if isinstance(listings, list) and listings:
            import html as _h
            head = reply if as_html else _h.escape(reply)
            reply = (head + "\n\n" + _format_listings(listings)).strip()
            as_html = True

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
            reply = (reply + f"\n\n🗑 Delete {names}? Reply “yes” to confirm.").strip()
        elif sugg:
            self._pending = sugg
            reply = (reply + "\n\n" + _describe_suggestions(result)).strip()
        self._send(reply or "(no reply)", to, html=as_html)

    def _apply_pending(self, pending: list[dict]) -> str:
        """Create/update the proposed watches through the app's own API (the same endpoints the
        dashboard's confirm button uses), and report what happened in plain words."""
        done, already, failed = [], [], []
        conflicts: list[tuple[str, dict, list]] = []      # (name, proposed_body, human diffs)
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
                    # "Already exists" is not a failure. If the proposed watch is the SAME as the
                    # existing one, say so plainly. If it DIFFERS, surface the differences and let
                    # the user choose to update it, replace it, or leave it — a name collision is
                    # usually "I want to change this" or "I forgot I already made it".
                    if r.status_code == 409 or "already exists" in detail.lower():
                        diff = self._watch_diff(name, body)
                        if diff:
                            conflicts.append((name, body, diff))
                        else:
                            already.append(name or "watch")
                    else:
                        # LOG a real 4xx — a bad field in a model-built body used to vanish, so a
                        # watch that "didn't work" left no trace in the activity log to diagnose.
                        log.warning("Telegram: could not apply %r (HTTP %s): %s",
                                    name, r.status_code, detail or "(no detail)")
                        failed.append(f"{name or 'watch'}{f' ({detail})' if detail else ''}")
            except Exception as exc:
                log.warning("Telegram: applying %r failed: %s", name, exc)
                failed.append(f"{name or 'watch'} ({exc})")

        parts = []
        if done:
            parts.append("✅ Done — " + ", ".join(f"“{n}”" for n in done) + ".")
        if already:
            parts.append("👍 " + ", ".join(f"“{n}”" for n in already)
                         + (" is" if len(already) == 1 else " are")
                         + " already set up and watching — nothing to change.")
        if conflicts:
            # Hold the first one for a follow-up choice (a turn almost always carries one watch).
            name, body, diff = conflicts[0]
            self._pending_conflict = {"name": name, "body": body}
            bullets = "\n".join(f"  • {d}" for d in diff)
            parts.append(
                f"You already have a watch called “{name}”. Here's what would change:\n\n"
                f"{bullets}\n\n"
                "What should I do?\n"
                "  • “update” — change the existing watch to the new settings (keeps its finds so far)\n"
                "  • “replace” — delete it and start fresh (clears its finds)\n"
                "  • “leave” — keep it exactly as it is")
        if failed:
            parts.append("⚠️ Couldn't apply " + ", ".join(f"“{n}”" for n in failed) + ".")
        return " ".join(parts) or "Nothing to apply."

    def _watch_diff(self, name: str, body: dict) -> list:
        """CONCRETE, labelled differences between a proposed watch `body` and the EXISTING watch of
        the same name — each a plain "Label: old → new" line, so the user can see exactly what
        would change. [] when they're effectively the same (or the existing one can't be read).
        Reads what the watch API exposes (urls + instruction) and derives the human-facing knobs
        the URLs actually encode: price cap, search radius, and which sites."""
        try:
            r = httpx.get(f"{self.dashboard_url}/api/watches", timeout=15.0)
            data = r.json()
            watches = data if isinstance(data, list) else (data.get("watches") or [])
        except Exception:
            return []
        low = name.strip().lower()
        existing = next((w for w in watches
                         if str(w.get("name", "")).strip().lower() == low), None)
        if not existing:
            return []

        from urllib.parse import urlparse
        try:
            from web_watcher.cl_geo import watch_price_cap, url_radius
        except Exception:
            watch_price_cap = url_radius = None
        try:
            from web_watcher.notify import source_label
        except Exception:
            def source_label(s):
                return s
        pu = [str(u).strip() for u in (body.get("urls") or []) if str(u).strip()]
        eu = [str(u).strip() for u in (existing.get("urls") or []) if str(u).strip()]
        pi = str(body.get("instruction") or "").strip()
        ei = str(existing.get("instruction") or "").strip()

        def _money(n):
            return f"${int(n):,}" if n else "any price"

        def _miles(n):
            return f"{int(n)} mi" if n else "any distance"

        def _sites(urls):
            hosts = []
            for u in urls:
                h = (urlparse(u).hostname or "").replace("www.", "")
                base = ".".join(h.split(".")[-2:]) if h else ""      # skagit.craigslist.org → craigslist.org
                label = source_label(base) or base
                if label and label not in hosts:
                    hosts.append(label)
            return ", ".join(hosts) or "—"

        diffs = []
        if pi and pi != ei:
            diffs.append(f"Looking for: “{ei or '—'}” → “{pi}”")
        if watch_price_cap:
            pc, ec = watch_price_cap(pu, pi), watch_price_cap(eu, ei)
            if pc != ec:
                diffs.append(f"Max price: {_money(ec)} → {_money(pc)}")
        if url_radius:
            pr = next((url_radius(u) for u in pu if url_radius(u)), None)
            er = next((url_radius(u) for u in eu if url_radius(u)), None)
            if pr != er:
                diffs.append(f"Search radius: {_miles(er)} → {_miles(pr)}")
        ps, es = _sites(pu), _sites(eu)
        if pu and ps != es:
            diffs.append(f"Sites: {es} → {ps}")
        # URLs differ in some way none of the labels captured (a category/sort tweak) — say so
        # rather than silently reporting "no differences" and looking like nothing changed.
        if not diffs and set(pu) != set(eu):
            diffs.append("Some search settings changed")
        return diffs

    def _resolve_conflict(self, text: str, chat_id: str) -> None:
        """Act on the user's choice for a name collision (update / replace / leave)."""
        conflict, self._pending_conflict = self._pending_conflict, None
        name, body = conflict["name"], conflict["body"]
        low = (text or "").strip().lower()
        if any(w in low for w in ("update", "change", "modify", "edit", "yes", "keep the history")):
            try:
                r = httpx.put(f"{self.dashboard_url}/api/watches/{quote(name)}", json=body, timeout=30.0)
                ok = r.status_code < 300
            except Exception as exc:
                log.warning("Telegram: conflict update of %r failed: %s", name, exc); ok = False
            self._send(f"✅ Updated “{name}” to the new settings — its match history is kept."
                       if ok else f"⚠️ Couldn't update “{name}”.", chat_id)
        elif any(w in low for w in ("replace", "wipe", "start over", "fresh", "overwrite", "delete")):
            try:
                httpx.delete(f"{self.dashboard_url}/api/watches/{quote(name)}", timeout=30.0)
                r = httpx.post(f"{self.dashboard_url}/api/watches", json=body, timeout=30.0)
                ok = r.status_code < 300
            except Exception as exc:
                log.warning("Telegram: conflict replace of %r failed: %s", name, exc); ok = False
            self._send(f"✅ Replaced “{name}” with a fresh watch on the new settings."
                       if ok else f"⚠️ Couldn't replace “{name}”.", chat_id)
        elif any(w in low for w in ("leave", "keep", "no", "cancel", "never mind", "nevermind")):
            self._send(f"Okay — kept “{name}” exactly as it was.", chat_id)
        else:
            # Unclear answer: hold the choice open and ask once more, plainly.
            self._pending_conflict = conflict
            self._send("Sorry — “update” to change the existing watch, “replace” to start fresh, "
                       "or “leave” to keep it as is?", chat_id)

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
    # Tap-to-vet (the "🔍 Vet this listing" button on an alert)
    # ------------------------------------------------------------------

    def _handle_callback(self, cb: dict) -> None:
        """A tapped inline button. Only 'vet:<token>' exists today: resolve the token back to the
        listing URL and run Deep Inspect (deal rating + scam risk), then reply with the verdict."""
        from web_watcher.notify import vet_entry_for
        data = str(cb.get("data") or "")
        chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
        if not self._authorized(chat):
            log.warning("Telegram: ignoring button from unauthorized chat %s", chat)
            return
        # "Show top N" from a baseline briefing — the backlog we deliberately didn't alert on.
        if data.startswith("top:"):
            # Debounce: one burst at a time. A second tap while a burst is still going (or right
            # after) is swallowed with a toast, not answered with another 10-20 cards.
            now = time.monotonic()
            if now - self._top_cooldown.get(str(chat), 0.0) < _TOP_COOLDOWN_S:
                self._answer_callback(cb.get("id"), "Already sending those — give it a moment.")
                return
            self._top_cooldown[str(chat)] = now
            self._answer_callback(cb.get("id"), "Fetching…")
            self._handle_top_request(data[4:], str(chat))
            return

        self._answer_callback(cb.get("id"), "Vetting…")
        if not data.startswith("vet:"):
            return
        entry = vet_entry_for(data[4:])
        url, title = entry.get("url", ""), entry.get("title", "")
        if not url:
            self._send("I couldn't find that listing to vet — it may be from an older alert.", str(chat))
            return
        with self._typing_until_sent(str(chat)):   # vetting can take a minute+ — keep it visible
            verdict = self._vet_listing(url)
        # RESTATE what was judged, WITH THE PICTURE. A verdict can land long after the alert
        # scrolled away — and after other chatting — so a bare rating is easy to misattach to the
        # wrong listing. Leading with the listing's photo makes it unmistakable which one this is.
        self._send_verdict(str(chat), title, verdict, url)

    def _send_verdict(self, chat: str, title: str, verdict: str, url: str) -> None:
        """Deliver a vet verdict as a photo-card — the listing's image up top, the title, the
        verdict, and its Open button — so it's self-contained and clearly tied to one listing.
        Falls back to a text message when there's no image, the caption won't fit, or the upload
        fails; the verdict is never lost to a missing picture."""
        import html as _h
        import json as _json
        from web_watcher.notify import image_bytes_for_listing
        head = f"🔍 <b>{_h.escape(title)}</b>\n\n" if title else "🔍 <b>Vetted</b>\n\n"
        caption = head + _h.escape(verdict) + f'\n\n<a href="{_h.escape(url, quote=True)}">{_h.escape(url)}</a>'
        buttons = [[{"text": "🔗 Open listing", "url": url}]]

        if len(caption) <= 1024:
            img = image_bytes_for_listing("", url)      # recovers the thumbnail from the listing page
            if img:
                data = {"chat_id": chat, "caption": caption, "parse_mode": "HTML",
                        "reply_markup": _json.dumps({"inline_keyboard": buttons})}
                try:
                    r = httpx.post(f"{TELEGRAM_API}/bot{self.bot_token}/sendPhoto",
                                   data=data, files={"photo": ("listing.jpg", img, "image/jpeg")},
                                   timeout=20.0)
                    if r.status_code == 200:
                        return
                    log.info("Telegram: verdict photo HTTP %s (%s) — text instead",
                             r.status_code, r.text[:160])
                except Exception as exc:
                    log.info("Telegram: verdict photo failed (%s) — text instead", exc)
        self._send(caption, chat, html=True, buttons=buttons)

    def _handle_top_request(self, payload: str, chat: str) -> None:
        """'<token>:<n>' → the best N already-recorded matches for that watch. Reads what the
        judge already rated, so it's instant — no re-browsing, no model call."""
        from web_watcher.notify import watch_for_brief
        token, _, n_raw = payload.partition(":")
        try:
            limit = max(1, min(int(n_raw or 10), 30))
        except ValueError:
            limit = 10
        name = watch_for_brief(token)
        if not name:
            self._send("I couldn't tell which watch that was — it may be from an older message.", chat)
            return
        try:
            r = httpx.get(f"{self.dashboard_url}/api/listings",
                          params={"watch_name": name, "matched": "true", "limit": limit},
                          timeout=30.0)
            r.raise_for_status()
            data = r.json()
            rows = data if isinstance(data, list) else (data.get("listings") or data.get("rows") or [])
        except Exception as exc:
            log.warning("top-N request failed for %r: %s", name, exc)
            self._send("I couldn't fetch those just now — try asking me in chat.", chat)
            return
        if not rows:
            self._send(f"Nothing recorded for “{name}” yet.", chat)
            return
        # One message per listing — a small thumbnail, a compact caption, and its own 🔍 Vet
        # button. Telegram allows a photo OR buttons on a message, but not many photos WITH
        # buttons, so a per-listing card is the only shape that gives BOTH a picture and a
        # tap-to-vet control on every result. A single combined list could show neither.
        self._send(f"⭐ Top {len(rows)} from “{name}” — tap 🔍 to vet any of them:", chat)
        for i, row in enumerate(rows[:limit]):
            self._send_listing_card(row, chat)
            if i < len(rows[:limit]) - 1:
                time.sleep(0.3)          # stay under Telegram's per-chat burst limit; keep order

    def _send_listing_card(self, row: dict, chat: str) -> None:
        """One top-N listing as its own message: thumbnail + compact caption + 🔗 Open / 🔍 Vet.
        Falls back to a text card (still with the Vet button) when there's no image or Telegram
        can't fetch it — a missing picture must never cost the vet control."""
        import html as _h
        import json as _json
        from web_watcher.notify import (remember_vet_link, vet_token, source_label,
                                        image_bytes_for_listing)
        if not isinstance(row, dict):
            return
        url    = str(row.get("url") or "").strip()
        title  = str(row.get("title") or "(listing)").strip()[:90]
        price  = str(row.get("price_text") or row.get("price") or "").strip()
        source = str(row.get("source") or "").strip()
        image  = str(row.get("image") or "").strip()
        try:
            rating = int(row.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        head = ("★" * rating + " ") if rating else ""
        cap  = f"{head}<b>{_h.escape(title)}</b>"
        bits = [b for b in (_h.escape(price) if price else "",
                            _h.escape(source_label(source)) if source else "") if b]
        if bits:
            cap += "\n" + " · ".join(bits)

        buttons = None
        if url.startswith("http"):
            remember_vet_link(url, title)         # so the tapped button resolves back to this URL
            buttons = [[{"text": "🔗 Open", "url": url},
                        {"text": "🔍 Vet", "callback_data": f"vet:{vet_token(url)}"}]]

        # Photo-card when the caption fits Telegram's 1024 cap. UPLOAD the bytes rather than
        # handing Telegram the URL — its server-side fetch of craigslist image URLs 400s, so we
        # fetch (proven to work) and send the bytes. If the row has no stored thumbnail (agent
        # sweeps miss lazy-loaded images), the helper recovers it from the listing's og:image.
        if len(cap) <= 1024:
            img = image_bytes_for_listing(image, url)
            if img:
                data = {"chat_id": chat, "caption": cap, "parse_mode": "HTML"}
                if buttons:
                    data["reply_markup"] = _json.dumps({"inline_keyboard": buttons})
                try:
                    r = httpx.post(f"{TELEGRAM_API}/bot{self.bot_token}/sendPhoto",
                                   data=data,
                                   files={"photo": ("listing.jpg", img, "image/jpeg")},
                                   timeout=20.0)
                    if r.status_code == 200:
                        return
                    log.info("Telegram: top-N photo card HTTP %s (%s) — text instead",
                             r.status_code, r.text[:160])
                except Exception as exc:
                    log.info("Telegram: top-N photo card failed (%s) — text instead", exc)

        # Text fallback keeps the Vet button so every result is still vettable.
        text = cap
        if url.startswith("http"):
            text += f'\n<a href="{_h.escape(url, quote=True)}">open</a>'
        self._send(text, chat, html=True, buttons=buttons)

    def _answer_callback(self, cb_id, text: str = "") -> None:
        """Acknowledge the tap so Telegram stops the button's spinner."""
        try:
            httpx.post(f"{TELEGRAM_API}/bot{self.bot_token}/answerCallbackQuery",
                       json={"callback_query_id": cb_id, "text": text}, timeout=10.0)
        except Exception:
            pass

    def _vet_listing(self, url: str) -> str:
        """Run the app's Deep Inspect on one listing and render its verdict for a phone."""
        try:
            httpx.post(f"{self.dashboard_url}/api/inspect", json={"url": url}, timeout=30.0)
        except Exception as exc:
            log.warning("Telegram: could not start inspect: %s", exc)
            return "Sorry — I couldn't start vetting that one."
        deadline = time.time() + _VET_TIMEOUT
        while time.time() < deadline:
            if self._stop.is_set():
                return "Stopped."
            time.sleep(3.0)
            try:
                r = httpx.get(f"{self.dashboard_url}/api/inspect", params={"url": url}, timeout=20.0)
                st = r.json() or {}
            except Exception:
                continue
            if st.get("status") == "done":
                return _format_verdict(st.get("verdict") or {})
            if st.get("status") == "error":
                return f"Couldn't vet that listing: {st.get('error') or 'unknown error'}"
        return "That one's taking a while to vet — check the app in a minute."

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
        if self.checkin_s <= 0:
            return                                          # proactive check-ins turned off
        watches = self._fetch_watches()
        if not watches:
            return
        prefs = self._fetch_checkin_prefs()      # each person's own cadence beats the default
        for owner in sorted(self.allowed):
            if owner == self.chat_id:
                owned = [w for w in watches if str(w.get("owner") or "") in ("", str(owner))]
            else:
                owned = [w for w in watches if str(w.get("owner") or "") == str(owner)]
            owned = [w for w in owned if w.get("enabled")]      # only what's actually watching
            if not owned:
                continue
            # This person's own setting (set from the bot: "check in every 6 hours"), else default.
            try:
                every = float(prefs.get(str(owner), self.checkin_s / 3600.0)) * 3600.0
            except (TypeError, ValueError):
                every = self.checkin_s
            if every <= 0:
                continue                                        # this person turned check-ins off
            last = self._owner_last_activity(owner, owned)
            if now - last < every:
                continue                                        # recently in touch — stay quiet
            self._send(_heartbeat_message(owned, now - last), owner)
            self._heartbeat_sent[owner] = now
            log.info("Telegram: sent a proactive check-in to %s", owner)

    def _fetch_checkin_prefs(self) -> dict:
        try:
            r = httpx.get(f"{self.dashboard_url}/api/telegram/checkin-prefs", timeout=15.0)
            if r.status_code == 200 and isinstance(r.json(), dict):
                return r.json()
        except Exception as exc:
            log.debug("Telegram: could not fetch check-in prefs (%s)", exc)
        return {}

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

    @contextlib.contextmanager
    def _typing_until_sent(self, chat_id: str = "", slow_nudges: list | None = None):
        """Hold Telegram's "…is typing" for the WHOLE wait, not just the first few seconds.
        Telegram expires a typing action after ~5s, so a single call left the chat looking idle
        while a local model was still thinking. Re-send it on a heartbeat until the reply is ready.

        slow_nudges: a list of (after_seconds, message) — each message is sent once the wait has
        gone that many seconds without an answer. A hard turn (a big local turn, or one escalating
        to Claude) then says "hang on" at ~12s and, only if it's STILL going at ~30s (uncommon),
        "still on it" — instead of sitting silent. Fast turns finish first and never nudge.
        Triggered on elapsed time, not on escalation: we only learn a turn escalated ~a second
        before the answer (too late to be worth a message), whereas "it's taking a while" is
        knowable as it happens."""
        stop = threading.Event()
        start = time.monotonic()
        nudges = sorted(((float(s), m) for (s, m) in (slow_nudges or []) if m),
                        key=lambda x: x[0])
        sent = 0

        def _beat():
            nonlocal sent
            while not stop.is_set():
                self._typing(chat_id)
                elapsed = time.monotonic() - start
                while sent < len(nudges) and elapsed >= nudges[sent][0]:
                    try:
                        self._send(nudges[sent][1], chat_id)
                    except Exception:
                        pass
                    sent += 1
                stop.wait(_TYPING_REFRESH_S)

        t = threading.Thread(target=_beat, name="telegram-typing", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()

    def _typing(self, chat_id: str = "") -> None:
        """Show Telegram's '…is typing' while the model thinks — a local turn isn't instant and
        silence reads as broken. Defaults to the alert chat when no target is given."""
        try:
            httpx.post(f"{TELEGRAM_API}/bot{self.bot_token}/sendChatAction",
                       json={"chat_id": chat_id or self.chat_id, "action": "typing"}, timeout=10.0)
        except Exception:
            pass

    def _send(self, text: str, chat_id: str = "", html: bool = False,
              buttons: list | None = None) -> None:
        """Send a reply. html=True enables Telegram's HTML parse mode — ONLY for text WE built
        (the settings block, a listing list), where every dynamic value is already escaped. Model
        prose is sent as plain text: it can contain < & > that would break the parser (or be eaten),
        and without parse_mode our own tags would print literally — which is what happened to the
        settings block's <i>…</i>."""
        target = chat_id or self.chat_id       # reply to the sender; fall back to the alert chat
        # The model writes Markdown ("**Anacortes Cars Watch**") because that is what chat models
        # do. Sent as plain text those asterisks print literally; sent as Markdown, one stray
        # underscore in a URL breaks the whole message. So we convert the few things it actually
        # uses into Telegram HTML and escape the rest — the text renders the way it was meant to,
        # and nothing the model writes can break the parser.
        if not html:
            text = _markdown_to_telegram_html(text)
            html = True
        chunks = _chunk(text, _MSG_LIMIT)
        for i, chunk in enumerate(chunks):
            body = {"chat_id": target, "text": chunk, "disable_web_page_preview": True}
            if html:
                body["parse_mode"] = "HTML"
            if buttons and i == len(chunks) - 1:     # attach controls to the final chunk
                body["reply_markup"] = {"inline_keyboard": buttons}
            try:
                httpx.post(f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage",
                           json=body, timeout=20.0)
            except Exception as exc:
                log.warning("Telegram: reply send failed: %s", exc)
                return


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

_MD_BOLD_RE   = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_MD_CODE_RE   = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE   = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _markdown_to_telegram_html(text: str) -> str:
    """The Markdown a chat model actually produces → Telegram HTML.

    Order matters: escape FIRST so any < & > in the prose is inert, then re-introduce only the
    tags we intend. Doing it the other way round lets a stray angle bracket in a listing title
    break the message — and Telegram rejects the whole send, so the reply is simply lost.

    Only **bold**, *italic*, `code` and [text](url) are converted; everything else is left as
    written. Under-converting is safe, over-converting loses messages."""
    import html as _h
    if not text:
        return ""
    out = _h.escape(text, quote=False)
    out = _MD_LINK_RE.sub(lambda m: f'<a href="{_h.escape(m.group(2), quote=True)}">{m.group(1)}</a>', out)
    out = _MD_CODE_RE.sub(lambda m: "<code>" + m.group(1) + "</code>", out)
    out = _MD_BOLD_RE.sub(lambda m: "<b>" + m.group(1) + "</b>", out)
    out = _MD_ITALIC_RE.sub(lambda m: "<i>" + m.group(1) + "</i>", out)
    return out


def _format_listings(rows: list, limit: int = 15) -> str:
    """A compact, scannable list of finds for a phone: rating, title, price, tappable link.
    Telegram HTML mode, so only <b>/<a> and escaped text."""
    import html as _h
    out = []
    for r in rows[:limit]:
        if not isinstance(r, dict):
            continue
        title = _h.escape(str(r.get("title") or "(listing)").strip())[:90]
        price = _h.escape(str(r.get("price_text") or r.get("price") or "").strip())
        try:
            rating = int(r.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        stars = ("★" * rating) if rating else ""
        url = str(r.get("url") or "").strip()
        head = f"{stars} " if stars else ""
        line = f"{head}<b>{title}</b>" + (f" — {price}" if price else "")
        if url.startswith("http"):
            line += f'\n<a href="{_h.escape(url, quote=True)}">open</a>'
        out.append(line)
    if not out:
        return ""
    more = f"\n\n…and {len(rows) - limit} more." if len(rows) > limit else ""
    return "\n\n".join(out) + more


def _format_verdict(v: dict) -> str:
    """Render a Deep Inspect verdict for a phone: deal stars, scam risk, why, and any red flags."""
    if not v:
        return "I couldn't read enough from that listing to judge it."

    known = v.get("known") or {}
    saved = _saved_facts_block(known)

    # The listing is gone (removed, expired, or gated). Say so plainly — and still hand over
    # everything that was saved when it was found, which is precisely when it's most useful.
    if v.get("fetched") is False:
        lines = ["🚫 " + str(v.get("error") or "I couldn't open that listing.")]
        if saved:
            lines += ["", "Here's what was saved when it was found:", saved]
        else:
            lines += ["", "I don't have a saved copy of this one either."]
        return "\n".join(lines)

    try:
        dq = int(v.get("deal_quality", 3))
    except (TypeError, ValueError):
        dq = 3
    risk = str(v.get("scam_risk", "low")).lower()
    risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "🟡")
    lines = [f"{'★' * dq}{'☆' * (5 - dq)} deal · {risk_icon} {risk} scam risk"]
    for key in ("summary", "deal_reason"):
        val = str(v.get(key) or "").strip()
        if val:
            lines += ["", val]
            break
    flags = v.get("red_flags") or []
    if flags:
        lines += ["", "⚠️ " + "; ".join(str(f) for f in flags[:4])]
    if saved:
        lines += ["", saved]
    return "\n".join(lines)


def _saved_facts_block(known: dict) -> str:
    """The facts we stored about a listing when we found it, as one short block. Used both to
    ground a verdict and to answer usefully when the page itself has since gone away."""
    if not known:
        return ""
    from web_watcher.notify import source_label
    bits = []
    title = str(known.get("title") or "").strip()
    if title:
        bits.append(title)
    line = []
    if known.get("price_text"):
        line.append(f"💵 {known['price_text']}")
    if known.get("source"):
        line.append(f"📍 {source_label(str(known['source']))}")
    if known.get("posted_at"):
        line.append(f"🗓 {str(known['posted_at'])[:16]}")
    if line:
        bits.append("  ".join(line))
    return "\n".join(bits)


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
        return f"📋 {names[0]}\n\nReply “yes” and I'll set it up."
    return ("📋 " + "\n📋 ".join(names) + "\n\nReply “yes” and I'll set them all up.")
