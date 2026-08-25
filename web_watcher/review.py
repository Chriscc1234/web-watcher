"""
Chat Review — an audit of the app's OWN conversations, so problems surface as a short report
instead of the user re-reading every thread in the live app looking for the moment it went weird.

Two passes, deliberately split by what each is actually good at:

  1. MECHANICAL (regex, free, certain). Things that are true or not true — raw HTML tags leaking
     into a reply, an empty or duplicated answer, a stack trace or timeout in the text, a reply
     that never came. An LLM adds nothing here and can only introduce doubt, so it isn't asked.

  2. JUDGEMENT (the biggest installed local model, no time limit). Things only a reader can see —
     the bot misunderstood, ignored what was asked, contradicted itself, answered confidently
     about the wrong watch, went in circles. This is the user's "smartest agent, zero limits on
     time" tier: quality over speed, run on demand, never in the sweep's hot path.

Every finding carries the turns it came from (thread + timestamp + the actual text) and a
`check_live` pointer — WHERE in the running app to go look — because the report's job is to send
the user straight to the real thing, not to be believed on its own.

Two things this file is careful about:
  • GPU manners. The review runs CHUNK BY CHUNK and releases the GPU slot between chunks. One
    huge call would hold the lock for the entire run and freeze chat behind it; many bounded
    calls let a person's message slip in between them.
  • Context window. Ollama defaults to a 4096-token window and silently truncates from the front,
    so a "read all of this" prompt can quietly become "read the last bit of this". Chunks are
    sized to fit and the window is stated explicitly (llm.chat's num_ctx).

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  resolve_review_model  the biggest INSTALLED local model (quality tier)
  collect_turns         every thread's turns since the watermark, oldest first
  scan_mechanical       pass 1: the certain, regex-findable defects
  review_chats          the whole audit: collect → mechanical → chunked model pass → report
  save_report/latest_report/watermark   the persisted report + "reviewed up to here" mark
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# The quality tier, biggest first — a GENERAL reasoning model, never a coder tune (reading a
# conversation for confusion is not a coding task). Whichever is installed wins. On a 16 GB card
# the 72b spills into system RAM and runs slowly; that is the deliberate trade the user asked
# for ("zero limits on time"), and chunking keeps any single call bounded.
_REVIEW_PREFERENCE = ("qwen2.5:72b", "llama3.3:70b", "qwen2.5:32b", "qwen2.5:14b")

# One chunk = at most this many turns / characters, whichever comes first. Small enough that a
# single call finishes in a sane time and releases the GPU, big enough to keep an exchange intact.
_CHUNK_TURNS = 12
_CHUNK_CHARS = 9_000
_NUM_CTX     = 8_192            # must comfortably hold a chunk + the reply
_CHUNK_TIMEOUT_S = 1_800.0      # 30 min per chunk — effectively "take as long as you need"

_REVIEW_FILENAME  = "chat_review.json"      # the watermark
_REPORTS_DIRNAME  = "chat_reviews"


# ---------------------------------------------------------------------------
# Model + paths
# ---------------------------------------------------------------------------

def resolve_review_model(cfg=None) -> str:
    """The biggest suitable installed local model. Falls back to the configured council model
    (always present) so a review can never be impossible to run."""
    from web_watcher.inspect import _installed_model_names

    installed = _installed_model_names()
    for name in _REVIEW_PREFERENCE:
        # Ollama reports names with the tag attached ("qwen2.5:72b"); tolerate an extra suffix.
        if any(n == name or n.startswith(name + "-") for n in installed):
            return name
    try:
        return cfg.models.council or cfg.models.text          # type: ignore[union-attr]
    except Exception:
        return "qwen2.5:14b"


def _data_dir(data_dir: Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    from web_watcher import paths
    return paths.data_dir()


def _history_files(data_dir: Path | None = None) -> list[tuple[str | None, Path]]:
    """(owner, path) for every stored conversation. owner None = the desktop dock thread."""
    from web_watcher import paths
    main = paths.watcher_history_path() if data_dir is None else Path(data_dir) / "watcher_history.json"
    out: list[tuple[str | None, Path]] = []
    if main.exists():
        out.append((None, main))
    try:
        for p in sorted(main.parent.glob("watcher_history_*.json")):
            token = p.stem[len("watcher_history_"):]
            if token:
                out.append((token, p))
    except Exception as exc:
        log.warning("chat review: could not list threads: %s", exc)
    return out


def _owner_names(data_dir: Path | None = None) -> dict:
    from web_watcher import paths
    main = paths.watcher_history_path() if data_dir is None else Path(data_dir) / "watcher_history.json"
    try:
        p = main.with_name("watcher_owners.json")
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Collecting the turns
# ---------------------------------------------------------------------------

def collect_turns(since_ts: float = 0.0, data_dir: Path | None = None) -> list[dict]:
    """Every stored turn newer than `since_ts`, across all threads, oldest first.

    Each turn is {"id", "thread", "owner", "role", "content", "ts"} — `id` is a stable index the
    model cites so findings can be mapped back to real evidence instead of trusting a paraphrase.
    """
    names = _owner_names(data_dir)
    rows: list[dict] = []
    for owner, path in _history_files(data_dir):
        label = "Desktop (you)" if owner is None else (names.get(owner) or f"Telegram {owner}")
        try:
            hist = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("chat review: could not read %s: %s", path.name, exc)
            continue
        if not isinstance(hist, list):
            continue
        for m in hist:
            if not isinstance(m, dict):
                continue
            ts = float(m.get("ts") or 0.0)
            if ts <= since_ts:
                continue
            rows.append({"thread": label, "owner": owner, "role": m.get("role", "?"),
                         "content": str(m.get("content", "")), "ts": ts})
    rows.sort(key=lambda r: r["ts"])
    for i, r in enumerate(rows):
        r["id"] = i
    return rows


# ---------------------------------------------------------------------------
# Pass 1 — mechanical defects (no model involved)
# ---------------------------------------------------------------------------

# A real HTML tag in text the user READS. The bot formats with Telegram HTML on the way out, so a
# tag surviving into stored prose means it was shown literally, or the model is now imitating tags
# it saw in its own history — the exact "<i>Change yours:</i>" bug, and how it spreads.
_HTML_TAG_RE = re.compile(r"</?(b|i|u|s|code|pre|a|br|p|div|span|html|body|table|tr|td)\b[^>]*>", re.I)
_ERROR_RE = re.compile(
    r"traceback \(most recent call last\)|"
    r"\b(internal server error|http 5\d\d|status 5\d\d)\b|"
    r"\b(timed out|timeout)\b|"
    r"pydantic|valueerror|keyerror|typeerror|attributeerror|"
    r"something went wrong|couldn't reach|could not reach|failed to",
    re.I,
)
# The bot telling the user it can't do something — worth a human eye: sometimes true, sometimes a
# capability it actually HAS and just didn't find.
_REFUSAL_RE = re.compile(
    r"\bi (can'?t|cannot|am not able to|don'?t have (the )?(ability|access))\b|"
    r"\b(not|isn'?t) (currently )?(supported|available|implemented)\b",
    re.I,
)
_EMPTY_MAX = 2          # an "answer" this short is not an answer
_LONG_REPLY = 3_000     # a wall of text in a chat window


def scan_mechanical(turns: list[dict]) -> list[dict]:
    """The findings a regex can be certain about. Cheap, deterministic, and run every time —
    these ground the report so it isn't only a model's opinion."""
    out: list[dict] = []

    def add(sev, kind, what, ids, check_live, fix=""):
        out.append({"severity": sev, "kind": kind, "what": what, "turns": ids,
                    "check_live": check_live, "fix": fix, "source": "mechanical"})

    prev_assistant: dict | None = None
    for t in turns:
        text = t["content"]
        if t["role"] != "assistant":
            continue

        tags = _HTML_TAG_RE.findall(text)
        if tags:
            add("high", "raw_html",
                f"A reply contains literal HTML tags ({', '.join(sorted(set(x.lower() for x in tags))[:4])}) "
                "in the stored text, so the person may have seen the markup instead of formatting.",
                [t["id"]],
                f"Open the {t['thread']} thread in Chats and look at this reply as it was delivered.",
                "Store plain text and apply Telegram HTML only at send time (notify/_send).")

        if len(text.strip()) <= _EMPTY_MAX:
            add("high", "empty_reply", "A reply was empty or a single character.", [t["id"]],
                f"{t['thread']} thread — check whether the person got any answer at all.",
                "A degraded turn should still say something; check the error path in oversight_chat.")

        if _ERROR_RE.search(text):
            add("medium", "error_text", "A reply shows an error/timeout to the user.", [t["id"]],
                f"{t['thread']} thread — then check the log around this time for the real cause.",
                "")

        if _REFUSAL_RE.search(text):
            add("low", "claimed_inability",
                "The bot said it couldn't do something — confirm that's actually true.",
                [t["id"]], f"{t['thread']} thread — try the same request in the live app.", "")

        if len(text) > _LONG_REPLY:
            add("low", "wall_of_text",
                f"A reply is {len(text):,} characters — long for a chat message, especially on a phone.",
                [t["id"]], f"{t['thread']} thread — read it on Telegram to see how it lands.", "")

        if (prev_assistant is not None
                and text.strip() and text.strip() == prev_assistant["content"].strip()):
            add("medium", "repeated_reply", "The bot sent the same reply twice.",
                [prev_assistant["id"], t["id"]],
                f"{t['thread']} thread — check whether it looped or the send was retried.", "")
        prev_assistant = t

    # A user message with no assistant turn after it = a question that never got answered.
    for i, t in enumerate(turns):
        if t["role"] != "user":
            continue
        later = [x for x in turns[i + 1:] if x["thread"] == t["thread"]]
        if later and later[0]["role"] == "user":
            add("medium", "unanswered",
                "The person sent a message and the next thing in the thread is another message "
                "from them — the first one looks unanswered.",
                [t["id"], later[0]["id"]],
                f"{t['thread']} thread — check whether a reply was sent but not stored.",
                "")
    return out


# ---------------------------------------------------------------------------
# Pass 2 — the judgement pass (the big model)
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = (
    "You are reviewing the conversation log of an assistant that runs a marketplace-watching app. "
    "The app watches listing sites for the user, alerts them on matches, and takes instructions in "
    "chat (create/pause/delete a watch, show matches, change settings, check in).\n\n"
    "Your job is to find where the ASSISTANT went wrong or behaved oddly — so a developer can fix "
    "it. Read the turns and report only REAL problems you can point at.\n\n"
    "Look for:\n"
    "- It misunderstood what the person asked for.\n"
    "- It ignored part of a request, or answered a different question.\n"
    "- It claimed it did something without evidence it did, or contradicted an earlier turn.\n"
    "- It got confused about WHICH watch, whose watch, or what state things are in.\n"
    "- It repeated itself, went in circles, or asked for something already given.\n"
    "- It gave a confusing, malformed, or hard-to-read answer.\n"
    "- The person sounded confused, frustrated, or had to repeat themselves — a strong signal.\n\n"
    "Do NOT report: correct answers you merely dislike, style preferences, or anything you are "
    "guessing about. If a turn is fine, say nothing about it. An empty findings list is a valid, "
    "GOOD result — do not invent problems to seem useful.\n\n"
    "Each turn is labelled [#N]. Cite the exact N values in \"turns\".\n\n"
    "Return ONLY JSON: {\"findings\": [{\"severity\": \"high|medium|low\", \"kind\": \"short_snake_case\", "
    "\"what\": \"one or two sentences on what went wrong\", \"turns\": [N, ...], "
    "\"check_live\": \"what to go try in the running app to confirm it\"}]}"
)


def _chunks(turns: list[dict]) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for t in turns:
        n = len(t["content"]) + 40
        if cur and (len(cur) >= _CHUNK_TURNS or size + n > _CHUNK_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += n
    if cur:
        out.append(cur)
    return out


def _render(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["ts"])) if t["ts"] else "?"
        who = "USER" if t["role"] == "user" else "ASSISTANT"
        body = t["content"].strip()
        if len(body) > 2_000:                     # keep one giant turn from eating the chunk
            body = body[:2_000] + " …[truncated]"
        lines.append(f"[#{t['id']}] ({t['thread']}, {when}) {who}: {body}")
    return "\n\n".join(lines)


def _judge_chunk(chunk: list[dict], model: str, cfg) -> list[dict]:
    from web_watcher import llm

    prompt = ("Review this stretch of conversation and report the assistant's problems.\n\n"
              + _render(chunk))
    raw = llm.chat(
        [{"role": "system", "content": _REVIEW_SYSTEM}, {"role": "user", "content": prompt}],
        role="review", local_model=model, cfg=cfg, format_json=True,
        timeout=_CHUNK_TIMEOUT_S, num_ctx=_NUM_CTX, force_local=True, max_tokens=2_048,
    )
    data = json.loads(raw)
    found = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(found, list):
        return []
    valid_ids = {t["id"] for t in chunk}
    out = []
    for f in found:
        if not isinstance(f, dict) or not str(f.get("what", "")).strip():
            continue
        ids = [int(n) for n in (f.get("turns") or []) if isinstance(n, (int, float))]
        out.append({
            "severity": str(f.get("severity", "medium")).lower().strip() or "medium",
            "kind": re.sub(r"[^a-z0-9_]+", "_", str(f.get("kind", "issue")).lower())[:40] or "issue",
            "what": str(f["what"]).strip(),
            "turns": [i for i in ids if i in valid_ids],       # drop hallucinated citations
            "check_live": str(f.get("check_live", "")).strip(),
            "fix": "",
            "source": "model",
        })
    return out


# ---------------------------------------------------------------------------
# The whole pass
# ---------------------------------------------------------------------------

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def review_chats(cfg=None, since: Optional[float] = None, model: str = "",
                 progress: Optional[Callable[[str], None]] = None,
                 data_dir: Path | None = None, advance_watermark: bool = True) -> dict:
    """Audit every conversation since the last review. Returns the report dict (also saved).

    since: override the watermark (0.0 = review everything ever stored).
    progress: called with short status strings so a caller can show a live line.
    """
    def say(msg: str) -> None:
        log.info("chat review: %s", msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    if cfg is None:
        try:
            from web_watcher.config import load as load_config
            cfg = load_config()
        except Exception:
            cfg = None

    mark = watermark(data_dir)
    since_ts = mark.get("last_ts", 0.0) if since is None else float(since)
    turns = collect_turns(since_ts, data_dir)
    threads = sorted({t["thread"] for t in turns})

    started = time.time()
    if not turns:
        say("nothing new to review")
        report = _report(started, model or "", since_ts, [], [], threads, note="No new messages since the last review.")
        save_report(report, data_dir)
        return report

    say(f"{len(turns)} turns across {len(threads)} thread(s)")
    mech = scan_mechanical(turns)
    say(f"mechanical scan: {len(mech)} finding(s)")

    model = model or resolve_review_model(cfg)
    chunks = _chunks(turns)
    say(f"reading with {model} — {len(chunks)} chunk(s), no time limit")

    judged: list[dict] = []
    failures = 0
    for i, ch in enumerate(chunks, 1):
        try:
            got = _judge_chunk(ch, model, cfg)
            judged.extend(got)
            say(f"chunk {i}/{len(chunks)} — {len(got)} finding(s)")
        except Exception as exc:
            failures += 1
            log.warning("chat review: chunk %d failed: %s", i, exc)
            say(f"chunk {i}/{len(chunks)} failed ({type(exc).__name__})")

    findings = mech + judged
    findings.sort(key=lambda f: (_SEV_ORDER.get(f["severity"], 1), f["kind"]))
    note = "" if not failures else f"{failures} of {len(chunks)} chunks could not be read by the model."
    report = _report(started, model, since_ts, findings, turns, threads, note=note)
    save_report(report, data_dir)
    if advance_watermark and turns:
        set_watermark(max(t["ts"] for t in turns), data_dir)
    say(f"done — {len(findings)} finding(s) in {report['took_s']}s")
    return report


def _report(started: float, model: str, since_ts: float, findings: list[dict],
            turns: list[dict], threads: list[str], note: str = "") -> dict:
    by_id = {t["id"]: t for t in turns}
    for f in findings:                       # attach the real evidence to each finding
        f["evidence"] = [{
            "thread": by_id[i]["thread"],
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(by_id[i]["ts"])) if by_id[i]["ts"] else "",
            "role": by_id[i]["role"],
            "text": by_id[i]["content"][:400],
        } for i in f.get("turns", []) if i in by_id]
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low")}
    return {
        "generated_at": time.time(),
        "took_s": round(time.time() - started, 1),
        "model": model,
        "reviewed_since": since_ts,
        "turns_reviewed": len(turns),
        "threads": threads,
        "counts": counts,
        "findings": findings,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Persistence — the watermark and the saved reports
# ---------------------------------------------------------------------------

def watermark(data_dir: Path | None = None) -> dict:
    """{"last_ts": <newest turn already reviewed>, "last_run_at": ...}. Never raises."""
    try:
        p = _data_dir(data_dir) / _REVIEW_FILENAME
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {"last_ts": 0.0, "last_run_at": 0.0}


def set_watermark(last_ts: float, data_dir: Path | None = None) -> None:
    try:
        p = _data_dir(data_dir) / _REVIEW_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_ts": float(last_ts), "last_run_at": time.time()}, indent=2),
                     encoding="utf-8")
    except Exception as exc:
        log.warning("chat review: could not save the watermark: %s", exc)


def _reports_dir(data_dir: Path | None = None) -> Path:
    return _data_dir(data_dir) / _REPORTS_DIRNAME


def save_report(report: dict, data_dir: Path | None = None) -> None:
    try:
        d = _reports_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"report_{int(report.get('generated_at', time.time()))}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    except Exception as exc:
        log.warning("chat review: could not save the report: %s", exc)


def latest_report(data_dir: Path | None = None) -> dict | None:
    try:
        p = _reports_dir(data_dir) / "latest.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Rendering (plain text — for the log, Telegram, or a quick read)
# ---------------------------------------------------------------------------

_SEV_MARK = {"high": "!!", "medium": "!", "low": "·"}


def render_report(report: dict, max_findings: int = 40) -> str:
    """A plain-text summary of a report. No markup — callers format for their own channel."""
    if not report:
        return "No review has been run yet."
    c = report.get("counts") or {}
    head = (f"Chat review — {report.get('turns_reviewed', 0)} turns across "
            f"{len(report.get('threads') or [])} thread(s), read by {report.get('model', '?')} "
            f"in {report.get('took_s', 0)}s\n"
            f"{c.get('high', 0)} high · {c.get('medium', 0)} medium · {c.get('low', 0)} low")
    if report.get("note"):
        head += f"\nNote: {report['note']}"
    findings = report.get("findings") or []
    if not findings:
        return head + "\n\nNothing to flag."
    lines = [head, ""]
    for i, f in enumerate(findings[:max_findings], 1):
        lines.append(f"{i}. [{_SEV_MARK.get(f.get('severity'), '·')}] {f.get('kind', 'issue')} — {f.get('what', '')}")
        if f.get("check_live"):
            lines.append(f"   Check live: {f['check_live']}")
        for ev in (f.get("evidence") or [])[:2]:
            snippet = " ".join(ev.get("text", "").split())[:160]
            lines.append(f"   \"{snippet}\"  ({ev.get('thread', '')} {ev.get('when', '')})")
        lines.append("")
    if len(findings) > max_findings:
        lines.append(f"…and {len(findings) - max_findings} more.")
    return "\n".join(lines).rstrip()
