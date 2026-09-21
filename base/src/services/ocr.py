"""Lightweight OCR fallback (tesseract-ocr/tesseract, 76k★).

Used only when the running model has NO vision. Tesseract is a small
CPU binary (no GPU, tens of MB) — fits next to a 35B model.
Returns extracted text or "" if tesseract is missing/unusable.
"""
import logging
import shutil
import subprocess

logger: logging.Logger = logging.getLogger(__name__)


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image(image_path: str, lang: str = "rus+eng") -> str:
    """Run tesseract on a file. Returns text (may be empty)."""
    if not tesseract_available():
        logger.warning("tesseract binary missing; install it for OCR")
        return ""
    try:
        p = subprocess.run(
            ["tesseract", image_path, "stdout", "-l", lang],
            capture_output=True, text=True, timeout=60,
        )
        return (p.stdout or "").strip()
    except Exception as e:
        logger.warning("ocr failed: %s", e)
        return ""
