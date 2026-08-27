"""
Config loader/validator for web-watcher.
Source of truth is config.yaml — this module owns all read/write access to it.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  Watch              ~L102   Per-watch config dataclass (urls, instruction, autonomous, etc.)
  ModelsConfig       ~L70    text_model / vision_model / council_model / ocr_threshold
  AppConfig          ~L143   Top-level config (models, browser, notifications, watches)
  load()             ~L157   Read config.yaml → AppConfig
  save()             ~L167   Write AppConfig → config.yaml
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Click-path step
# ---------------------------------------------------------------------------

VALID_ACTIONS = {"click", "select", "scroll", "wait_for_selector", "wait_ms"}


class ClickStep(BaseModel):
    action: str
    target: str | None = None
    amount: int | None = None  # used by 'scroll'

    @field_validator("action")
    @classmethod
    def action_must_be_known(cls, v: str) -> str:
        if v not in VALID_ACTIONS:
            raise ValueError(f"Unknown click-path action '{v}'. Valid: {VALID_ACTIONS}")
        return v

    @model_validator(mode="after")
    def check_required_fields(self) -> "ClickStep":
        if self.action == "scroll" and self.amount is None:
            raise ValueError("'scroll' step requires 'amount'")
        if self.action in {"click", "select", "wait_for_selector"} and not self.target:
            raise ValueError(f"'{self.action}' step requires 'target'")
        return self


# ---------------------------------------------------------------------------
# Notification settings
# ---------------------------------------------------------------------------

class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: str = ""
    # Two-way chat: when on (and token+chat_id are set), the app polls Telegram for messages
    # you send the bot and answers with the SAME assistant as the in-app dock, so your phone
    # becomes one conversation with The Watcher. Off = alerts only. See telegram_bot.py.
    two_way: bool = False
    # Extra Telegram chat IDs allowed to TALK to the bot, beyond chat_id (which is also where
    # alerts are sent). A bot token is effectively public — anyone who finds the bot can message
    # it — so only the IDs listed here plus chat_id are ever answered. Use this to let a second
    # person (e.g. you AND your buddy) drive the same watcher.
    allowed_chat_ids: list[str] = Field(default_factory=list)
    # Quiet-period check-ins: how many hours of NO contact (no alert, no chat, no prior check-in)
    # before the bot pings you with "still on watch" + an offer to broaden or vet. 12 = twice a
    # day; 6 = every few hours. 0 turns proactive check-ins off entirely. See telegram_bot.py.
    checkin_hours: float = 12.0


class EmailConfig(BaseModel):
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    from_address: str = ""
    app_password: str = ""
    to_address: str = ""


class NotificationsConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)


# ---------------------------------------------------------------------------
# Model settings
# ---------------------------------------------------------------------------

class ProviderRoute(BaseModel):
    """Where one LLM role runs. provider "local" (default) uses the Ollama model; "anthropic"
    uses `model` (a cloud id like "claude-haiku-4-5"; blank => llm.py's per-role default)."""
    provider: str = "local"   # "local" | "anthropic"
    model:    str = ""


class CloudConfig(BaseModel):
    """Opt-in cloud (Anthropic) routing. DEFAULT IS FULLY LOCAL: with no roles mapped and no
    key, nothing changes. The key is the USER'S OWN — from here or $ANTHROPIC_API_KEY; a blank
    key with no env var keeps every role local. See web_watcher/llm.py for routing + fallback."""
    anthropic_api_key: str = ""
    # role name → route. Recognized roles: "judge" (per-sweep listing rating), "chat"
    # (the Watcher assistant), "inspect" (Deep Inspect), "terms", "reason". Unlisted roles
    # or provider="local" stay on the local model.
    roles: dict[str, ProviderRoute] = Field(default_factory=dict)
    # Hard monthly spend ceiling in USD (estimated from token usage). 0 = no cap. When this
    # month's estimated spend reaches it, cloud calls stop and fall back to the LOCAL models
    # for the rest of the month — so the bill can't run past what you set. See web_watcher/llm.py.
    monthly_budget_usd: float = 0.0
    # A second, tighter ceiling on ONE DAY's spend. The monthly cap alone can be emptied in an
    # afternoon by a loop nobody noticed; the daily cap turns "I lost the month" into "I lost a
    # day" and it heals itself at midnight. 0 = no daily cap.
    daily_budget_usd: float = 0.0
    # AUTO ROUTING (the default, and what the UI exposes instead of a model chooser). Every call
    # runs on the LOCAL model first; cloud is used only when the local answer objectively fails a
    # check, and then it climbs the cheapest-first ladder. Nothing is routed on a guess, so a
    # working local model costs nothing. Set False to use the explicit `roles` map instead.
    auto: bool = True


class ModelsConfig(BaseModel):
    text_model:    str = "qwen2.5:7b"
    vision_model:  str = "qwen2.5vl:7b"
    # council_model drives the get-unstuck recovery pass, the continuous-mode listing
    # judge, and the dashboard assistant. Empty => reuse text_model (the installer pulls
    # text_model on every tier, so the fallback is always available). Set explicitly to
    # use a stronger reasoning model on capable hardware.
    council_model: str = ""
    # inspect_model drives Deep Inspect — the slow, thorough deal/scam evaluation of a single
    # listing. Empty => auto-pick the biggest GENERAL model installed (see inspect.py), so a
    # pulled qwen2.5:72b is used automatically without editing config. Not the coder tune.
    inspect_model: str = ""
    ocr_threshold: int = 200  # chars — if DOM text is below this, fall back to vision OCR
    # Opt-in cloud routing (default: all roles local). See CloudConfig / web_watcher/llm.py.
    cloud: CloudConfig = Field(default_factory=CloudConfig)

    @property
    def effective_council_model(self) -> str:
        """The council/judge/assistant model, falling back to text_model when unset."""
        return self.council_model or self.text_model


# ---------------------------------------------------------------------------
# Browser settings
# ---------------------------------------------------------------------------

class BrowserConfig(BaseModel):
    # Visible browser by default: a fresh install should SHOW the user what the agent is
    # doing (trust + debuggability). Power users can turn headless back on in Settings.
    headless: bool = False
    stealth:  bool = True  # human-like mouse/timing behaviour (disable for simple/trusted sites)
    # Draw a visible fake cursor in the agent's browser that follows its synthetic mouse, so
    # you can watch where it's clicking (CDP moves the browser's mouse without moving a
    # visible OS cursor — this dot is what makes the motion visible). Only shows with
    # headless=False. ON by default now the browser is visible by default; the drawn dot is
    # a same-origin overlay a site could in principle detect, so turn it off in Settings if
    # you'd rather maximize stealth on a touchy site.
    show_agent_cursor: bool = True
    # Persistent profile directory for login-required sites (e.g. Facebook). When a
    # watch sets use_login_profile=True the browser launches with this on-disk profile
    # so a one-time manual login is reused. None => default location (data/profiles/default).
    profile_dir: str | None = None


# ---------------------------------------------------------------------------
# Per-watch notify override
# ---------------------------------------------------------------------------

class WatchNotify(BaseModel):
    telegram: bool = True
    email: bool = True


# ---------------------------------------------------------------------------
# Watch definition
# ---------------------------------------------------------------------------

VALID_PERCEPTION = {"text", "vision", "auto"}
VALID_MODE = {"schedule", "continuous"}


class Watch(BaseModel):
    # Stable identity, decoupled from the human-editable name so renames don't orphan
    # the listing/observation history keyed to it. Assigned + persisted on first load
    # (see config.load) for watches created before this existed. None = needs one.
    id: str | None = None
    name: str
    enabled: bool = True
    # Who this watch belongs to: a Telegram chat_id (as a string), or "" for unassigned.
    # Via the Telegram bot a person sees and manages ONLY watches whose owner is their own
    # chat_id — so you can hand the bot to your buddy and he sees just his. The desktop
    # dashboard is the admin view and shows every watch regardless of owner. See
    # server._watches_for_owner / telegram_bot.
    owner: str = ""
    urls: list[str] = Field(min_length=1)
    click_path: list[ClickStep] = Field(default_factory=list)
    interval_minutes: int | None = None  # mutually exclusive with cron_expression
    cron_expression: str | None = None
    instruction: str
    perception: str = "auto"
    notify: WatchNotify = Field(default_factory=WatchNotify)
    model_override:  str | None = None
    autonomous:      bool = True   # AI-driven browsing (agent loop)
    max_agent_steps: int  = 15     # safety cap on autonomous actions
    judgment_prompt: str | None = None  # optional post-browse reasoning step

    # ── Match quality (rating 1-5, inspired by ai-marketplace-monitor) ─────────
    # The judge rates each listing 1-5 against the criteria (1=no match / suspicious,
    # 2=missing essential info, 3=acceptable, 4=good match, 5=great deal). A listing is
    # ALERTED only if its rating >= min_rating. This is the user's "alert volume knob":
    # raise it to 4 to hear only about strong finds, drop to 2 to catch more. Only takes
    # effect when a judgment_prompt is set (the graded judge runs then).
    min_rating: int = 3

    # ── Cheap keyword pre-filter (runs BEFORE the LLM judge) ───────────────────
    # keywords: if set, the listing's title/details must contain AT LEAST ONE (any that
    # matches passes). antikeywords: if the title/details contain ANY of these, the
    # listing is dropped outright ("parts", "repair", "salvage", "wanted"). Both are
    # plain case-insensitive substring lists — free, deterministic, and they cut the LLM
    # judge's load + false alerts. The chat can set them ("ignore anything that says parts").
    keywords:     list[str] = Field(default_factory=list)
    antikeywords: list[str] = Field(default_factory=list)

    # ── Execution mode ────────────────────────────────────────────────────────
    # "schedule"   — run every interval_minutes / cron_expression (the default).
    # "continuous" — run a non-stop sweep loop (scroll → collect → dedup → alert on
    #                NEW listings → vary search → idle → repeat) until stopped.
    mode: str = "schedule"

    # ── Continuous-mode settings (ignored when mode == "schedule") ────────────
    continuous_scroll_passes: int = 4    # scroll bursts per sweep to load more listings
    continuous_idle_seconds:  int = 45   # pause between sweeps (interruptible)
    continuous_search_variation: bool = True  # rotate sort/price each sweep to fight the feed algorithm
    continuous_max_alerts: int = 8       # cap new-listing alerts per sweep (rest summarised)

    # Use the persistent login browser profile (for sites that require sign-in, e.g. Facebook)
    use_login_profile: bool = False

    # Record this watch's browser session to video (one .webm per page, under
    # data/recordings/<watch>/). Off by default — it costs disk and a little CPU. Meant for
    # SUPERVISED runs on a site where the account is what's at risk and you want to review
    # exactly what the agent did afterwards, rather than inferring it from log lines.
    record_video: bool = False

    # ── Goal watch (monitor a CONDITION, not listings) ────────────────────────
    # goal_kind "" = a normal listings watch (everything above). "restock" = watch a specific
    # product page (urls[0]) for a size/variant coming back IN STOCK, and alert on the flip.
    # This is the first slice of the general goal/condition monitor — listings is one template.
    goal_kind:   str = ""      # "" | "restock"
    target_size: str = ""      # e.g. "34W x 30L" — the variant to watch (restock)

    @field_validator("perception")
    @classmethod
    def perception_must_be_valid(cls, v: str) -> str:
        if v not in VALID_PERCEPTION:
            raise ValueError(f"perception must be one of {VALID_PERCEPTION}")
        return v

    @field_validator("min_rating")
    @classmethod
    def min_rating_in_range(cls, v: int) -> int:
        # Clamp rather than reject — a chat suggestion with an out-of-range value should
        # still create a working watch, not 400.
        return max(1, min(5, int(v)))

    @field_validator("mode")
    @classmethod
    def mode_must_be_valid(cls, v: str) -> str:
        if v not in VALID_MODE:
            raise ValueError(f"mode must be one of {VALID_MODE}")
        return v

    @model_validator(mode="after")
    def must_have_schedule(self) -> "Watch":
        # Continuous watches run a perpetual loop and need no interval/cron schedule.
        if self.mode == "continuous":
            if self.continuous_idle_seconds < 1:
                raise ValueError(f"Watch '{self.name}' continuous_idle_seconds must be >= 1")
            return self
        # Scheduled watches require exactly one of interval_minutes / cron_expression.
        if self.interval_minutes is None and self.cron_expression is None:
            raise ValueError(
                f"Watch '{self.name}' must specify either 'interval_minutes' or 'cron_expression'"
            )
        if self.interval_minutes is not None and self.cron_expression is not None:
            raise ValueError(
                f"Watch '{self.name}' must not specify both 'interval_minutes' and 'cron_expression'"
            )
        if self.interval_minutes is not None and self.interval_minutes < 1:
            raise ValueError(f"Watch '{self.name}' interval_minutes must be >= 1")
        return self


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

class ReviewConfig(BaseModel):
    """The self-audit: the biggest local model reads the app's OWN conversations and reports
    where it went wrong. Off by default — it's slow (that's the point) and it should be the
    user's choice when to spend the GPU on introspection rather than on watching."""
    enabled: bool = False
    every_hours: float = 24.0
    # Message the admin on Telegram when a run turns up HIGH-severity findings. A report nobody
    # reads is a report that didn't happen.
    notify: bool = True


class AppConfig(BaseModel):
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    watches: list[Watch] = Field(default_factory=list)

    # Cross-watch matching: when one watch surfaces a fresh listing, also test it against
    # your OTHER continuous watches' criteria — so a Corvette the truck watch stumbles on
    # gets offered to the sports-car watch instead of being lost. Costs extra local-LLM
    # judge calls (one per other-watch per sweep that has new candidates). On by default;
    # set false to keep each watch fully independent.
    cross_watch_matching: bool = True

    # One-time config migrations already applied to THIS file (see load()). Lets a
    # changed default reach existing installs exactly once without ever re-overriding
    # a value the user later sets back deliberately.
    applied_migrations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Load / save helpers
# ---------------------------------------------------------------------------

from web_watcher import paths

_DEFAULT_CONFIG_PATH = paths.config_path()

# Serialises config writers inside this process. Re-entrant because mutate() holds it across a
# load+save and save() takes it again. Cross-PROCESS safety comes from the atomic os.replace in
# save() plus single_instance, which already stops a second copy of the app running.
_CONFIG_LOCK = threading.RLock()

# Windows refuses os.replace() while another handle has the target open, and this app reads
# config.yaml constantly. ~1.5s of retries total, which is far longer than any read here takes.
_REPLACE_RETRIES = 8


def load(path: Path | str | None = None) -> AppConfig:
    """Load and validate config.yaml. Raises ValidationError on schema violations."""
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Self-heal a config accidentally written in cp1252 (e.g. an older installer that
            # wrote the em-dash default watch name without encoding="utf-8"). Read it as
            # cp1252 and rewrite it as UTF-8 so this only ever happens once.
            text = p.read_text(encoding="cp1252")
            try:
                p.write_text(text, encoding="utf-8")
            except Exception:
                pass
        raw = yaml.safe_load(text) or {}
    config = AppConfig.model_validate(raw)

    # Migration: ensure every watch has a stable id, then persist once so the id is
    # durable (the data layer keys observations by it). Only writes when something was
    # missing and there's a real file to update.
    assigned = False
    for w in config.watches:
        if not w.id:
            w.id = uuid.uuid4().hex
            assigned = True

    # Migration: show the browser by default (0.23.x flipped the DEFAULT to visible,
    # but both existing installs carry headless: true from when that was OUR default,
    # not a choice anyone made). Applied exactly once — if the user turns headless
    # back on afterwards, it stays on.
    if "show_browser_default" not in config.applied_migrations:
        config.applied_migrations.append("show_browser_default")
        if config.browser.headless:
            config.browser.headless = False
        assigned = True

    # Migration: turn the visible agent cursor ON once, so existing installs actually SEE
    # the agent's mouse now that the browser is visible by default. One-time — a later
    # off toggle sticks.
    if "show_cursor_default" not in config.applied_migrations:
        config.applied_migrations.append("show_cursor_default")
        if not config.browser.show_agent_cursor:
            config.browser.show_agent_cursor = True
        assigned = True

    if assigned and p.exists():
        try:
            save(config, p)
        except Exception:
            pass
    return config


def save(config: AppConfig, path: Path | str | None = None) -> None:
    """Serialise AppConfig back to config.yaml — ATOMICALLY, under a lock.

    config.yaml holds EVERY watch: it is the app's entire state in one file. This used to be a
    plain open("w"), which truncates first and then writes, and roughly fifteen API endpoints do
    load → mutate → save. FastAPI runs sync endpoints on a threadpool, so the desktop dashboard,
    the owner's bot and a buddy's bot can all be inside that sequence at once. Two real hazards:

      • CORRUPTION — a crash (or a reader) between truncate and flush leaves a half-written file.
        For this file that means every watch is gone.
      • LOST UPDATES — two readers each load, each mutate a different watch, and the second save
        silently discards the first person's change.

    Fixed here by writing a sibling temp file, fsync-ing it, then os.replace() — which is atomic
    on Windows and POSIX, so a reader sees either the old file or the new one, never a partial —
    all under _CONFIG_LOCK so writers inside one process serialise. Callers that read-modify-write
    should hold `mutate()` to close the lost-update window across the whole sequence."""
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    data = config.model_dump(exclude_none=False)
    text = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    with _CONFIG_LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())     # the bytes are on disk before anything is swapped
            # os.replace is atomic — readers see old or new, never half. On WINDOWS it also
            # fails with PermissionError while any other handle has the target open, and this
            # app reads config.yaml constantly (every orchestrator cycle, every chat turn), so
            # an unretried replace loses the write outright. Caught by a concurrent-reader test.
            # Retry briefly: the competing read is a few milliseconds long.
            for attempt in range(_REPLACE_RETRIES):
                try:
                    os.replace(tmp, p)
                    break
                except PermissionError:
                    if attempt == _REPLACE_RETRIES - 1:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


@contextmanager
def lock():
    """Hold the config lock across a read-modify-write done with explicit load()/save().

        with config.lock():
            cfg = load()
            ...mutate...
            save(cfg)

    Preferred over mutate() when the block has early returns or raises that must NOT write —
    the save stays explicit, so the existing control flow is preserved exactly while the
    lost-update window (two endpoints both loading before either saves) is closed."""
    with _CONFIG_LOCK:
        yield


@contextmanager
def mutate(path: Path | str | None = None):
    """Read-modify-write config.yaml without losing a concurrent edit.

        with config.mutate() as cfg:
            cfg.watches.append(new_watch)

    Holds the lock across load AND save, so two endpoints editing different watches can no longer
    clobber each other — the second one loads what the first one wrote. Saves on clean exit only;
    an exception leaves the file untouched."""
    with _CONFIG_LOCK:
        cfg = load(path)
        yield cfg
        # save() re-takes the lock; it is an RLock precisely so this nesting is safe.
        save(cfg, path)


def round_trip(path: Path | str | None = None) -> AppConfig:
    """Load -> save -> reload. Returns the reloaded config. Used to verify stability."""
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    cfg = load(p)
    save(cfg, p)
    return load(p)
