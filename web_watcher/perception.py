"""
Perception layer — decides what to hand to the reasoning layer (text or image).

Flow per watch.perception setting:
  "text"   -> always use extracted DOM text, skip heuristic
  "vision" -> always use screenshot
  "auto"   -> use text if heuristic passes; fall back to screenshot otherwise

The scheduler is responsible for ensuring page_result.screenshot_bytes is
populated (browser.run_watch(watch, screenshot=True)) whenever the watch
perception mode is "vision" or "auto".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional  # noqa: F401 — used in _try_ocr type hint

from web_watcher.browser import PageResult
from web_watcher.config import Watch

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 200

# Patterns that indicate the page didn't actually render meaningful content
_JS_REQUIRED = [
    "please enable javascript",
    "you need to enable javascript",
    "javascript is required",
    "javascript is not enabled",
    "javascript is disabled",
    "enable javascript to",
    "requires javascript to",
    "this page requires javascript",
    "this site works best with javascript",
    "browser does not support javascript",
]

# Repeated loading placeholders = SPA skeleton not yet resolved
_LOADING_MARKERS = [
    "loading...",
    "please wait...",
    "content is loading",
    "fetching data",
]
_LOADING_REPEAT_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class PerceptionResult:
    mode_used:        str             # "text" or "vision"
    text:             Optional[str]   # populated when mode_used == "text"
    image_bytes:      Optional[bytes] # populated when mode_used == "vision"
    heuristic_passed: bool            # False if text failed the quality check
    heuristic_notes:  list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def perceive(page_result: PageResult, watch: Watch) -> PerceptionResult:
    """
    Select the perception mode for a single URL result and return the
    content the reasoning layer should analyse.
    """
    mode = watch.perception

    if mode == "text":
        return _as_text(page_result, passed=True, notes=["mode=text (forced)"])

    if mode == "vision":
        return _as_vision(page_result, passed=True, notes=["mode=vision (forced)"])

    # auto — text → OCR → vision LLM
    passed, notes = check_text_usable(page_result.text)
    if passed:
        return _as_text(page_result, passed=True, notes=notes)

    log.info(
        "Text heuristic failed for watch %r (%s) — trying OCR",
        watch.name, "; ".join(notes),
    )

    if page_result.screenshot_bytes:
        ocr_result = _try_ocr(page_result, notes)
        if ocr_result is not None:
            return ocr_result

        log.info("OCR fallback unsuccessful for %r — trying vision LLM", watch.name)
        return _as_vision(page_result, passed=False, notes=notes)

    log.warning(
        "OCR/vision fallback needed for %r but no screenshot available "
        "(was browser.run_watch called with screenshot=True?). Using raw text.",
        watch.name,
    )
    return _as_text(
        page_result,
        passed=False,
        notes=notes + ["ocr/vision fallback failed: no screenshot"],
    )


def check_text_usable(text: str) -> tuple[bool, list[str]]:
    """
    Heuristic quality gate for extracted DOM text.

    Returns (is_usable, notes).
    notes is empty on success; on failure it contains the first failing reason.
    """
    if not text or not text.strip():
        return False, ["text is empty"]

    stripped = text.strip()

    if len(stripped) < MIN_TEXT_CHARS:
        return False, [
            f"text too short: {len(stripped)} chars (minimum {MIN_TEXT_CHARS})"
        ]

    lower = stripped.lower()

    for pattern in _JS_REQUIRED:
        if pattern in lower:
            return False, [f"JS-required indicator found: {pattern!r}"]

    for marker in _LOADING_MARKERS:
        count = lower.count(marker)
        if count >= _LOADING_REPEAT_THRESHOLD:
            return False, [
                f"loading placeholder repeated {count}x: {marker!r} "
                f"(threshold {_LOADING_REPEAT_THRESHOLD})"
            ]

    return True, []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _try_ocr(
    page_result: PageResult,
    original_notes: list[str],
) -> Optional[PerceptionResult]:
    """
    Attempt OCR on the page screenshot.  Returns a PerceptionResult with
    mode_used="ocr" if the extracted text passes the quality heuristic,
    otherwise returns None so the caller can try the vision-LLM fallback.
    """
    from web_watcher.ocr import ocr_extract
    ocr_text = ocr_extract(page_result.screenshot_bytes)  # type: ignore[arg-type]
    if not ocr_text:
        return None

    passed, ocr_notes = check_text_usable(ocr_text)
    if passed:
        log.info("OCR produced usable text (%d chars)", len(ocr_text))
        return PerceptionResult(
            mode_used="ocr",
            text=ocr_text,
            image_bytes=None,
            heuristic_passed=True,
            heuristic_notes=["ocr extraction succeeded"],
        )

    log.info("OCR text failed quality check (%s)", "; ".join(ocr_notes))
    return None


def _as_text(
    page_result: PageResult,
    *,
    passed: bool,
    notes: list[str],
) -> PerceptionResult:
    return PerceptionResult(
        mode_used="text",
        text=page_result.text or "",
        image_bytes=None,
        heuristic_passed=passed,
        heuristic_notes=notes,
    )


def _as_vision(
    page_result: PageResult,
    *,
    passed: bool,
    notes: list[str],
) -> PerceptionResult:
    return PerceptionResult(
        mode_used="vision",
        text=None,
        image_bytes=page_result.screenshot_bytes,
        heuristic_passed=passed,
        heuristic_notes=notes,
    )
