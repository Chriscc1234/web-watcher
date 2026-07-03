"""
Reasoning layer — calls Ollama locally and returns a strict structured result.

Output contract (always returned, never raises):
    ReasoningResult(
        found       : bool
        summary     : str
        confidence  : "high" | "medium" | "low"
        link        : str | None
        error       : str | None   -- set on parse failure, None on success
        raw_output  : str | None   -- preserved when parsing fails
    )

Reliability strategy (spec Section 4.3 + Section 9 risk note):
  1. Use Ollama's `format:"json"` to constrain output syntax (eliminates most
     JSON parse errors, especially important on 3B-7B quantized models).
  2. Validate the returned dict against the required schema.
  3. On schema mismatch, send one repair re-prompt asking for the missing fields.
  4. On second failure, log raw output and return found=False with error flag.
     The watch loop never crashes on bad model output.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_BASE    = "http://localhost:11434"
OLLAMA_TIMEOUT = 120.0        # seconds; vision path can be slow
MAX_TEXT_CHARS = 12_000       # conservative limit for 7B context windows

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ReasoningResult:
    found:      bool
    summary:    str
    confidence: str               # "high" | "medium" | "low"
    link:       Optional[str]
    error:      Optional[str] = None
    raw_output: Optional[str] = None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a webpage content analyser.
The user gives you page content and an instruction describing what to look for.
Respond with a JSON object using EXACTLY these keys:
  "found"      : true or false
  "summary"    : short human-readable description of what matched, or why nothing matched
  "confidence" : one of "high", "medium", or "low"
  "link"       : the most relevant URL on the page if applicable, otherwise null
Output ONLY the JSON object. No explanation, no markdown, no code fences."""

_USER_TEXT = """\
Page URL: {url}

Page content:
---
{content}
---

Instruction: {instruction}"""

_USER_VISION = """\
Page URL: {url}

Instruction: {instruction}

Analyse the page screenshot provided."""

_REPAIR = """\
Your previous response did not include all required fields.
Respond with ONLY a JSON object containing exactly these keys: \
"found" (boolean), "summary" (string), "confidence" ("high"/"medium"/"low"), "link" (string or null).

Previous response:
{bad_output}"""


# ---------------------------------------------------------------------------
# Reasoner
# ---------------------------------------------------------------------------

class Reasoner:
    """
    Wraps Ollama calls for both text and vision paths.

    Instantiate once per application run (or per watch if model overrides differ).
    Thread-safe — each call creates its own httpx.Client.
    """

    def __init__(
        self,
        text_model:   str,
        vision_model: str,
        base_url:     str = OLLAMA_BASE,
        timeout:      float = OLLAMA_TIMEOUT,
    ) -> None:
        self.text_model   = text_model
        self.vision_model = vision_model
        self.base_url     = base_url.rstrip("/")
        self.timeout      = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyse_text(
        self,
        text:        str,
        instruction: str,
        url:         str = "",
    ) -> ReasoningResult:
        content = _truncate(text, MAX_TEXT_CHARS)
        user_msg = _USER_TEXT.format(url=url, content=content, instruction=instruction)
        return self._call_with_retry(self.text_model, user_msg)

    def analyse_image(
        self,
        image_bytes: bytes,
        instruction: str,
        url:         str = "",
    ) -> ReasoningResult:
        import base64
        b64      = base64.b64encode(image_bytes).decode()
        user_msg = _USER_VISION.format(url=url, instruction=instruction)
        return self._call_with_retry(self.vision_model, user_msg, images=[b64])

    # ------------------------------------------------------------------
    # Internal: retry / repair loop
    # ------------------------------------------------------------------

    def _call_with_retry(
        self,
        model:    str,
        user_msg: str,
        images:   list[str] | None = None,
    ) -> ReasoningResult:
        # First attempt
        raw = self._chat(model, user_msg, images)
        result = _parse_and_validate(raw)
        if result is not None:
            return result

        # Repair attempt — send bad output back and ask for correction
        log.warning("[%s] JSON schema mismatch on first attempt — sending repair prompt", model)
        repair_msg = _REPAIR.format(bad_output=raw[:2000])
        raw2 = self._chat(model, repair_msg)
        result = _parse_and_validate(raw2)
        if result is not None:
            return result

        # Both failed — log and return a safe sentinel
        log.error(
            "[%s] JSON parse/validation failed after repair.\nFirst output: %s\nRepair output: %s",
            model, raw[:500], raw2[:500],
        )
        return ReasoningResult(
            found=False,
            summary="Model output could not be parsed — check logs for raw output.",
            confidence="low",
            link=None,
            error="json_parse_failed_after_repair",
            raw_output=raw2,
        )

    # ------------------------------------------------------------------
    # Internal: single Ollama call
    # ------------------------------------------------------------------

    def _chat(
        self,
        model:    str,
        user_msg: str,
        images:   list[str] | None = None,
    ) -> str:
        """
        Call Ollama /api/chat.

        Uses format:"json" to constrain output syntax at the model level.
        This eliminates the most common failure mode on small quantized models.
        """
        messages: list[dict] = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": user_msg},
        ]
        if images:
            messages[-1]["images"] = images

        payload: dict = {
            "model":    model,
            "messages": messages,
            "stream":   False,
            "format":   "json",   # constrains output to valid JSON syntax
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(f"{self.base_url}/api/chat", json=payload)
                r.raise_for_status()
                return r.json()["message"]["content"]
        except httpx.ConnectError as exc:
            raise OllamaUnavailableError(
                f"Cannot reach Ollama at {self.base_url} — is it running?"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaUnavailableError(
                f"Ollama returned HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc


# ---------------------------------------------------------------------------
# Parse + validate helpers
# ---------------------------------------------------------------------------

def _parse_and_validate(raw: str) -> Optional[ReasoningResult]:
    """
    Attempt to parse raw model output as JSON and validate the schema.
    Returns None if parsing or validation fails.
    """
    text = raw.strip()

    # Strip markdown code fences — even with format:"json" some models add them
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text.strip())

    # Direct parse
    try:
        data = json.loads(text)
        return _validate_schema(data)
    except json.JSONDecodeError:
        pass

    # Fallback: find first {...} block in the output
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return _validate_schema(data)
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _validate_schema(data: object) -> Optional[ReasoningResult]:
    """Validate that the parsed object has all required fields."""
    if not isinstance(data, dict):
        return None
    if "found" not in data:
        return None

    confidence = str(data.get("confidence", "low")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    link = data.get("link")
    if link is not None:
        link = str(link).strip() or None

    return ReasoningResult(
        found=bool(data["found"]),
        summary=str(data.get("summary", "")).strip(),
        confidence=confidence,
        link=link,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n... [truncated {len(text) - max_chars} characters] ...\n\n"
        + text[-half:]
    )


class OllamaUnavailableError(RuntimeError):
    """Raised when Ollama cannot be reached (connection refused, wrong port, etc.)."""
