"""
GPU VRAM detection and model-tier selection.

Detection order
---------------
1. nvidia-smi  — most reliable for NVIDIA cards
2. PowerShell / WMI  — fallback; note that WMI's AdapterRAM saturates at 4 GB
   on some Windows driver versions, so the result may be under-reported for
   high-VRAM cards.  We cap the WMI path's influence at 16 GB to avoid
   treating an erroneous 4 GB read as authoritative when the nvidia-smi path
   is unavailable for a non-NVIDIA (e.g. AMD) card with real 8-12 GB VRAM.

Tier table (bigger hardware gets bigger models — 16GB is not the ceiling)
-------------------------------------------------------------------------
≥48 000 MB  →  48GB+    qwen2.5:72b           qwen2.5vl:32b
≥24 000 MB  →  24GB     qwen2.5:32b           qwen2.5vl:7b
≥16 000 MB  →  16GB     qwen2.5:14b           qwen2.5vl:7b
≥ 8 000 MB  →  8-12GB   qwen2.5:7b            qwen2.5vl:7b
≥ 6 000 MB  →  6GB      qwen2.5:7b            moondream
     <6 000  →  CPU      qwen2.5:3b            (vision disabled)

council_model (recovery/judge/assistant reasoning) defaults to each tier's text_model.

Usage
-----
    from web_watcher.gpu_detect import detect_tier, validate_inference, TierInfo

    tier = detect_tier()
    print(tier.tier_name, tier.text_model, tier.vision_model)

    ok = validate_inference(tier.text_model)
    if not ok:
        tier = tier.fallback()
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"

# ---------------------------------------------------------------------------
# Tier table — ordered from highest to lowest VRAM requirement
# ---------------------------------------------------------------------------

@dataclass
class TierInfo:
    tier_name:    str
    min_vram_mb:  int
    text_model:   str
    vision_model: Optional[str]
    # Model for the reasoning-heavy roles: the get-unstuck recovery pass, the
    # continuous-mode listing judge, and the dashboard assistant. Defaults to the
    # tier's text_model (one strong model handles both), but a tier can name a larger
    # model here when there is VRAM headroom for the extra reasoning quality.
    council_model: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.council_model:
            self.council_model = self.text_model

    def fallback(self) -> "TierInfo":
        """Return the next-lower tier (useful when GPU inference is too slow)."""
        idx = _TIERS.index(self)
        if idx + 1 < len(_TIERS):
            return _TIERS[idx + 1]
        return self  # already at CPU tier

    def as_dict(self) -> dict:
        return {
            "tier_name":     self.tier_name,
            "text_model":    self.text_model,
            "vision_model":  self.vision_model,
            "council_model": self.council_model,
            "min_vram_mb":   self.min_vram_mb,
        }


# Tier table — ordered highest → lowest VRAM. Model tags are real Ollama registry
# names (e.g. "qwen2.5vl:7b" with NO dash — "qwen2.5-vl" is not a valid Ollama tag).
# Bigger cards get bigger models: the app ships to varied hardware, so 16GB is not
# the ceiling. council_model is left as text_model except where a card has the room
# to run a heavier reasoner for the recovery/judge/assistant roles.
_TIERS: list[TierInfo] = [
    TierInfo("48GB+",   48_000, "qwen2.5:72b",       "qwen2.5vl:32b", "qwen2.5:72b"),
    TierInfo("24GB",    24_000, "qwen2.5:32b",       "qwen2.5vl:7b",  "qwen2.5:32b"),
    TierInfo("16GB",    16_000, "qwen2.5:14b",       "qwen2.5vl:7b",  "qwen2.5:14b"),
    TierInfo("8-12GB",   8_000, "qwen2.5:7b",        "qwen2.5vl:7b",  "qwen2.5:7b"),
    TierInfo("6GB",      6_000, "qwen2.5:7b",        "moondream",     "qwen2.5:7b"),
    TierInfo("CPU",          0, "qwen2.5:3b",         None,           "qwen2.5:3b"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_tier() -> TierInfo:
    """Detect installed VRAM and return the appropriate model tier."""
    vram_mb = _detect_vram_mb()
    tier = _select_tier(vram_mb)
    log.info(
        "GPU detect: vram=%s MB  →  tier=%s  text=%s  vision=%s",
        vram_mb, tier.tier_name, tier.text_model, tier.vision_model,
    )
    return tier


def detect_vram_mb() -> Optional[int]:
    """Return detected VRAM in MB, or None if no discrete GPU is found."""
    return _detect_vram_mb()


def validate_inference(
    model: str,
    *,
    max_seconds: float = 45.0,
    base_url: str = OLLAMA_URL,
) -> bool:
    """
    Send a minimal inference request and verify it completes within max_seconds.

    Returns True on success, False if the call times out, errors, or Ollama
    is not reachable.  The caller should fall back one tier when this returns False.
    """
    prompt = "Reply with exactly one word: OK"
    payload = {
        "model":   model,
        "messages": [{"role": "user", "content": prompt}],
        "stream":  False,
    }
    try:
        start = time.monotonic()
        with httpx.Client(timeout=max_seconds + 5) as client:
            r = client.post(f"{base_url}/api/chat", json=payload)
            elapsed = time.monotonic() - start

        if not r.is_success:
            log.warning("validate_inference: HTTP %s for %s", r.status_code, model)
            return False

        if elapsed > max_seconds:
            log.warning(
                "validate_inference: %s took %.1fs (threshold %.0fs) — likely CPU fallback",
                model, elapsed, max_seconds,
            )
            return False

        log.info("validate_inference: %s responded in %.1fs ✓", model, elapsed)
        return True

    except Exception as exc:
        log.warning("validate_inference failed for %s: %s", model, exc)
        return False


# ---------------------------------------------------------------------------
# Full system probe (for the Settings → System panel + re-scan)
# ---------------------------------------------------------------------------

# Approximate DOWNLOAD sizes (MB) for the models we ship, used to warn the user before a
# multi-GB pull. Only a fallback: for models already installed we report Ollama's real size.
# Ollama's default qwen2.5 tags are q4_K_M quantisations.
_MODEL_SIZE_MB: dict[str, int] = {
    "qwen2.5:3b":     1_900,
    "qwen2.5:7b":     4_700,
    "qwen2.5:14b":    9_000,
    "qwen2.5:32b":   20_000,
    "qwen2.5:72b":   47_000,
    "qwen2.5vl:7b":   6_000,
    "qwen2.5vl:32b": 21_000,
    "moondream":      1_700,
}

# Plain-English explanation of what choosing each tier means. Shown next to the model selector
# so a non-technical user understands the trade-off before committing to a download.
_TIER_BLURB: dict[str, tuple[str, str]] = {
    # tier_name: (what it is, the trade-off)
    "48GB+":   ("Largest models. Best possible understanding.",
                "Needs a 48GB+ workstation GPU. Huge download and very heavy on your machine."),
    "24GB":    ("Very strong reasoning; handles tricky requests well.",
                "Needs a 24GB GPU. Large download; noticeably heavier load."),
    "16GB":    ("Strong all-rounder — the sweet spot for most gaming GPUs.",
                "Needs a 16GB GPU. The assistant rarely misunderstands you."),
    "8-12GB":  ("Good understanding with modest resource use.",
                "Needs an 8GB+ GPU. Solid for everyday watch creation."),
    "6GB":     ("Capable assistant that fits a small GPU.",
                "Needs ~6GB VRAM. Reliable at creating and editing watches."),
    "CPU":     ("Runs on any computer, no graphics card required.",
                "Lightest load, but the assistant is noticeably weaker: it can confuse a new "
                "watch with an existing one and misread requests. Vision is disabled. "
                "Use a GPU tier if you can."),
}


def model_size_mb(name: str) -> Optional[int]:
    """Approximate download size for a model tag, or None if unknown."""
    return _MODEL_SIZE_MB.get((name or "").strip())


def tier_catalog(vram_mb: Optional[int] = None) -> list[dict]:
    """Every selectable model set, largest first, annotated for the Settings model selector.

    `fits` says whether the detected VRAM can hold it — the UI still lets the user pick a bigger
    set (they may want to override a bad GPU probe, or knowingly run partly on CPU), but warns.
    `recommended` marks the set this hardware maps to.
    """
    if vram_mb is None:
        vram_mb = _detect_vram_mb() or 0
    best = _select_tier(vram_mb)
    out: list[dict] = []
    for t in _TIERS:
        models = [m for m in (t.text_model, t.vision_model, t.council_model) if m]
        models = list(dict.fromkeys(models))          # dedupe, keep order
        what, tradeoff = _TIER_BLURB.get(t.tier_name, ("", ""))
        out.append({
            **t.as_dict(),
            "models":        models,
            "download_mb":   sum(_MODEL_SIZE_MB.get(m, 0) for m in models),
            "fits":          (t.min_vram_mb == 0) or (vram_mb >= t.min_vram_mb),
            "recommended":   t.tier_name == best.tier_name,
            "what":          what,
            "tradeoff":      tradeoff,
        })
    return out


def probe_system() -> dict:
    """Gather a human-readable hardware summary + the tier this hardware maps to. Used by the
    Settings 'System' panel and the 'Re-scan hardware' button (so a user who swaps a GPU can
    re-detect without reinstalling). All probes are best-effort and time-boxed — never raise."""
    import platform as _pf
    vram = _detect_vram_mb()
    tier = _select_tier(vram)
    return {
        "os":               _pf.platform(),
        "cpu":              (_pf.processor() or _pf.machine() or "Unknown CPU"),
        "cpu_cores":        _os_cpu_count(),
        "ram_mb":           _total_ram_mb(),
        "gpu_name":         _gpu_name(),
        "vram_mb":          vram,
        "recommended_tier": tier.tier_name,
        "recommended":      tier.as_dict(),
    }


def _os_cpu_count() -> Optional[int]:
    import os
    try:
        return os.cpu_count()
    except Exception:
        return None


def _total_ram_mb() -> Optional[int]:
    """Total physical RAM in MB. Uses a Windows API call (no psutil dependency); falls back to
    POSIX sysconf on Linux/mac."""
    import sys
    try:
        if sys.platform == "win32":
            import ctypes

            class _MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = _MEMSTAT()
            stat.dwLength = ctypes.sizeof(_MEMSTAT)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys) // (1024 * 1024)
        else:
            import os
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return (pages * page_size) // (1024 * 1024)
    except Exception as exc:
        log.debug("RAM probe failed: %s", exc)
    return None


def _gpu_name() -> Optional[str]:
    """The discrete GPU's name (nvidia-smi first, then WMI), or None on a CPU-only machine."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            names = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
            if names:
                return names[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception as exc:
        log.debug("nvidia-smi name probe failed: %s", exc)
    # WMI fallback — returns the adapter name even for AMD/Intel.
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-WmiObject Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            names = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
            # Prefer a discrete GPU name over a basic display adapter if several are listed.
            for n in names:
                if not any(k in n.lower() for k in ("basic", "microsoft")):
                    return n
            if names:
                return names[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception as exc:
        log.debug("WMI GPU name probe failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _select_tier(vram_mb: Optional[int]) -> TierInfo:
    if vram_mb is None:
        return _TIERS[-1]  # CPU
    for tier in _TIERS:
        if vram_mb >= tier.min_vram_mb:
            return tier
    return _TIERS[-1]


def _detect_vram_mb() -> Optional[int]:
    """Try nvidia-smi first, then WMI via PowerShell."""
    result = _vram_from_nvidia_smi()
    if result is not None:
        return result
    return _vram_from_wmi()


def _vram_from_nvidia_smi() -> Optional[int]:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        lines = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
        if not lines:
            return None
        # Take the GPU with the most VRAM if there are multiple
        vrams = []
        for line in lines:
            try:
                vrams.append(int(line))
            except ValueError:
                pass
        if vrams:
            return max(vrams)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception as exc:
        log.debug("nvidia-smi probe failed: %s", exc)
    return None


def _vram_from_wmi() -> Optional[int]:
    """
    Query WMI via PowerShell.

    Caveat: Win32_VideoController.AdapterRAM is a DWORD (32-bit) and saturates
    at 4 294 967 295 bytes (~4 GB) for cards with more than 4 GB of VRAM on
    older drivers.  We return the value as-is; callers should prefer
    nvidia-smi when available.
    """
    try:
        script = (
            "Get-WmiObject Win32_VideoController | "
            "Select-Object -ExpandProperty AdapterRAM"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return None
        values = []
        for line in proc.stdout.strip().splitlines():
            line = line.strip()
            try:
                val = int(line)
                if val > 0:
                    values.append(val)
            except ValueError:
                pass
        if values:
            best_bytes = max(values)
            return best_bytes // (1024 * 1024)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception as exc:
        log.debug("WMI VRAM probe failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# CLI helper — `python -m web_watcher.gpu_detect`
# ---------------------------------------------------------------------------

def _cli() -> None:
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    vram = _detect_vram_mb()
    tier = _select_tier(vram)

    print(f"VRAM detected : {vram} MB" if vram else "VRAM detected : none (CPU-only)")
    print(f"Selected tier : {tier.tier_name}")
    print(f"Text model    : {tier.text_model}")
    print(f"Vision model  : {tier.vision_model or 'disabled'}")

    if "--validate" in sys.argv:
        print(f"\nValidating inference ({tier.text_model}) ...")
        ok = validate_inference(tier.text_model)
        if ok:
            print("  Inference OK — GPU is being used ✓")
        else:
            print("  Inference too slow or failed — falling back")
            fallback = tier.fallback()
            print(f"  Fallback tier : {fallback.tier_name}")
            print(f"  Fallback model: {fallback.text_model}")


if __name__ == "__main__":
    _cli()
