"""
OCR text extraction from page screenshots.

Two-stage pipeline:
  1. Tesseract (pytesseract) — fast, CPU-only; excellent for standard web text
  2. EasyOCR               — GPU-accelerated deep learning; handles complex
                             layouts, stylised fonts, and low-contrast images

Both engines are optional.  If Tesseract is unavailable the module falls
straight through to EasyOCR.  If neither is installed, ocr_extract() returns
None and the caller must decide how to proceed (e.g. vision-LLM fallback).

The EasyOCR Reader is created once and kept alive (module-level singleton) so
the model stays resident in VRAM/RAM between calls — no repeated load penalty.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Singleton EasyOCR reader — loaded on first use, retained for the lifetime of
# the process.  None means "not yet attempted"; False means "load failed".
_easyocr_reader = None

# Well-known Tesseract install path on Windows (UB Mannheim installer / winget)
_TESSERACT_WIN_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ocr_extract(image_bytes: bytes) -> Optional[str]:
    """
    Extract text from a screenshot PNG/JPEG using OCR.

    Tries Tesseract first (fast, CPU).  If that returns nothing useful,
    falls back to EasyOCR (GPU, more accurate on difficult images).

    Returns the extracted text string, or None if both engines fail or are
    unavailable.
    """
    text = _tesseract(image_bytes)
    if text:
        log.debug("OCR via Tesseract: %d chars", len(text))
        return text

    text = _easyocr(image_bytes)
    if text:
        log.debug("OCR via EasyOCR: %d chars", len(text))
        return text

    log.warning("OCR: all engines returned no text")
    return None


def is_tesseract_available() -> bool:
    """Return True if pytesseract + the Tesseract binary are both usable."""
    try:
        import pytesseract
        _configure_tesseract_path(pytesseract)
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def is_easyocr_available() -> bool:
    """Return True if the easyocr package is installed."""
    try:
        import easyocr  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Private — Tesseract engine
# ---------------------------------------------------------------------------

def _tesseract(image_bytes: bytes) -> Optional[str]:
    try:
        import pytesseract
        from PIL import Image

        _configure_tesseract_path(pytesseract)
        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img, config="--psm 3")
        return text.strip() or None
    except ImportError:
        log.debug("pytesseract / Pillow not installed — skipping Tesseract")
        return None
    except Exception as exc:
        log.debug("Tesseract OCR failed: %s", exc)
        return None


def _configure_tesseract_path(pytesseract_mod) -> None:
    """Point pytesseract at the Windows installer path if needed."""
    import sys, os
    if sys.platform == "win32":
        import pathlib
        win_path = pathlib.Path(_TESSERACT_WIN_PATH)
        if win_path.exists():
            pytesseract_mod.pytesseract.tesseract_cmd = str(win_path)
        # Also check PATH — winget may have added it
        elif not os.getenv("TESSERACT_CMD"):
            pass  # let pytesseract find it on PATH


# ---------------------------------------------------------------------------
# Private — EasyOCR engine
# ---------------------------------------------------------------------------

def _easyocr(image_bytes: bytes) -> Optional[str]:
    global _easyocr_reader
    if _easyocr_reader is False:
        return None  # previous load attempt failed — don't retry

    try:
        import easyocr
        import numpy as np
        from PIL import Image

        if _easyocr_reader is None:
            log.info("Loading EasyOCR model (first use — may take a moment)...")
            gpu = _cuda_available()
            _easyocr_reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)
            log.info("EasyOCR model loaded (gpu=%s)", gpu)

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)
        results = _easyocr_reader.readtext(arr, detail=0, paragraph=True)
        return "\n".join(results).strip() or None

    except ImportError:
        log.debug("easyocr / numpy not installed — skipping EasyOCR")
        _easyocr_reader = False
        return None
    except Exception as exc:
        log.warning("EasyOCR failed: %s", exc)
        _easyocr_reader = False
        return None


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False
