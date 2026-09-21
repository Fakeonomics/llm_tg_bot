"""Safe rich-text for Telegram (battle-tested telegramify-markdown).

Model/file text (Markdown-ish, may contain <...> pseudo-tags) ->
MarkdownV2 via telegramify. Never raises: falls back to plain text.
Chunked sending splits the SOURCE then converts each piece, so a broken
fence in one chunk degrades to plain instead of killing the message.
"""
import logging

logger: logging.Logger = logging.getLogger(__name__)

try:
    from telegramify_markdown import markdownify
except Exception:  # pragma: no cover
    markdownify = None

from src.services.messages import split_message


def format_one(md_text: str, limit: int = 4000):
    """Return (text, parse_mode) for a short message. Safe fallback plain."""
    src = ((md_text or "").strip() or "(пусто)")[:limit]
    if markdownify is None:
        return src, None
    try:
        return markdownify(src), "MarkdownV2"
    except Exception as e:
        logger.debug("markdownify failed: %s", e)
        return src, None


def format_chunks(md_text: str, limit: int = 3900):
    """Return [(text, parse_mode)] chunks for a long message."""
    src = (md_text or "").strip() or "(пусто)"
    parts = split_message(src, limit=limit)
    out = []
    for p in parts:
        if markdownify is None:
            out.append((p, None))
            continue
        try:
            out.append((markdownify(p), "MarkdownV2"))
        except Exception as e:
            logger.debug("markdownify chunk failed: %s", e)
            out.append((p[:4096], None))
    return out or [(src[:4096], None)]
