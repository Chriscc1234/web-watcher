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
import uuid
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

class ModelsConfig(BaseModel):
    text_model:    str = "qwen2.5:7b"
    vision_model:  str = "qwen2.5vl:7b"
    # council_model drives the get-unstuck recovery pass, the continuous-mode listing
    # judge, and the dashboard assistant. Empty => reuse text_model (the installer pulls
    # text_model on every tier, so the fallback is always available). Set explicitly to
    # use a stronger reasoning model on capable hardware.
    council_model: str = ""
    ocr_threshold: int = 200  # chars — if DOM text is below this, fall back to vision OCR

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
    # you can watch where it's clicking. Only visible with headless=False. Off by default: it
    # adds a DOM element a site could see, so it slightly reduces stealth when on.
    show_agent_cursor: bool = False
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

class AppConfig(BaseModel):
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
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

    if assigned and p.exists():
        try:
            save(config, p)
        except Exception:
            pass
    return config


def save(config: AppConfig, path: Path | str | None = None) -> None:
    """Serialise AppConfig back to config.yaml, preserving human-readable YAML."""
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    data = config.model_dump(exclude_none=False)
    with p.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def round_trip(path: Path | str | None = None) -> AppConfig:
    """Load -> save -> reload. Returns the reloaded config. Used to verify stability."""
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    cfg = load(p)
    save(cfg, p)
    return load(p)
