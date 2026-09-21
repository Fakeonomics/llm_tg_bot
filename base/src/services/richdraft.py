"""Native Telegram AI UX via raw Bot API (stdlib only, no lib needed).

- Thinking drafts (sendRichMessageDraft + thinking block): ephemeral native
  "Thinking" bubble, no rate limits like edits, auto-expires. No flicker.
- Rich final (sendRichMessage): native tables/headings/details/code.
- Fallback to classic sends on any failure (never worse than before).
"""
import json
import logging
import os
import urllib.request

logger: logging.Logger = logging.getLogger(__name__)

_CLIENT = None


def _client():
    """Shared httpx client (keepalive). One-shot urllib handshakes per
    call cost seconds on flaky nets; pooling kills that overhead."""
    global _CLIENT
    if _CLIENT is None:
        import httpx
        _CLIENT = httpx.Client(timeout=httpx.Timeout(12.0, connect=8.0))
    return _CLIENT


def classify(text: str) -> str:
    """'plain' -> cheap sendMessage; 'rich' -> sendRichMessage.

    Plain prose skips the rich round-trip entirely (faster + fewer
    handshakes). Anything structured goes rich with plain fallback.
    """
    import re as _re
    t = (text or "").strip()
    if not t or len(t) > 30000:
        return "plain"
    for line in t.splitlines():
        s = line.strip()
        if not s:
            continue
        if _re.match(r"#{1,6}\s", s):
            return "rich"
        if s.startswith(("```", ">", "- ", "* ", "|")):
            return "rich"
        if _re.match(r"\d+[.)]\s", s):
            return "rich"
        if "||" in s:
            return "rich"
    if ("**" in t or "__" in t or "`" in t or "](" in t
            or _re.search(r"https?://", t)):
        return "rich"
    return "plain"


def _token() -> str:
    return os.getenv("BOT_TOKEN", "")


def _post(method: str, payload: dict, timeout: int = 8):
    tok = _token()
    if not tok:
        return None
    url = f"https://api.telegram.org/bot{tok}/{method}"
    try:
        r = _client().post(url, json=payload, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        pass
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        logger.debug("rich api %s failed: %s", method, e)
        return None


def draft_thinking(chat_id, draft_id: int, text: str = "Thinking") -> bool:
    # <tg-thinking> is draft-only html (docs); private chats only anyway.
    r = _post("sendRichMessageDraft", {
        "chat_id": chat_id, "draft_id": draft_id,
        "rich_message": {"html": f"<tg-thinking>{text}</tg-thinking>"}})
    return bool(r and r.get("ok"))


def draft_markdown(chat_id, draft_id: int, md_text: str) -> bool:
    r = _post("sendRichMessageDraft", {
        "chat_id": chat_id, "draft_id": draft_id,
        "rich_message": {"markdown": (md_text or "")[:30000]}})
    return bool(r and r.get("ok"))


def send_rich_markdown(chat_id, md_text: str, reply_to: int = 0):
    payload = {"chat_id": chat_id,
               "rich_message": {"markdown": (md_text or "")[:30000]}}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return _post("sendRichMessage", payload, timeout=12)


def send_rich_html(chat_id, html_text: str, reply_to: int = 0):
    """True-rich path: HTML flavor with real <table>/<pre>/<blockquote>."""
    payload = {"chat_id": chat_id,
               "rich_message": {"html": (html_text or "")[:30000]}}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return _post("sendRichMessage", payload, timeout=12)


def send_rich_blocks(chat_id, blocks, reply_to: int = 0):
    """True-rich path: typed JSON blocks (tables, headings, lists...)."""
    if not blocks:
        return None
    payload = {"chat_id": chat_id, "rich_message": {"blocks": blocks}}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return _post("sendRichMessage", payload, timeout=12)


def edit_rich_blocks(inline_message_id: str, blocks) -> bool:
    """Edit an inline/guest message to rich blocks (documented param)."""
    if not inline_message_id or not blocks:
        return False
    r = _post("editMessageText",
              {"inline_message_id": inline_message_id,
               "rich_message": {"blocks": blocks}}, timeout=12)
    return bool(r and r.get("ok"))


def _rt_inline(md: str):
    """GFM inline -> RichText (plain str, nested dict, or list thereof)."""
    import re as _re
    pat = _re.compile(
        r"`([^`\n]+)`"
        r"|\[([^\]\n]+)\]\((https?[^)\s]+)\)"
        r"|\*\*([^*`\n]+)\*\*"
        r"|__([^_`\n]+)__"
        r"|(?<!\*)\*([^*\n]+)\*(?!\*)"
        r"|(?<!_)_(?!_)([^_\n]+)(?<!_)_(?!_)"
        r"|~~([^~\n]+)~~"
        r"|\|\|([^|\n]+)\|\|")
    segs = []
    pos = 0
    src = md or ""
    for m in pat.finditer(src):
        if m.start() > pos:
            segs.append(src[pos:m.start()])
        g = m.groups()
        if g[0] is not None:
            segs.append({"type": "code", "text": g[0]})
        elif g[1] is not None:
            segs.append({"type": "url", "text": g[1],
                         "url": g[2].strip()})
        elif g[3] is not None:
            segs.append({"type": "bold", "text": g[3]})
        elif g[4] is not None:
            segs.append({"type": "underline", "text": g[4]})
        elif g[5] is not None:
            segs.append({"type": "italic", "text": g[5]})
        elif g[6] is not None:
            segs.append({"type": "italic", "text": g[6]})
        elif g[7] is not None:
            segs.append({"type": "strikethrough", "text": g[7]})
        elif g[8] is not None:
            segs.append({"type": "spoiler", "text": g[8]})
        pos = m.end()
    if pos < len(src):
        segs.append(src[pos:])
    segs = [s for s in segs if s != ""]
    if not segs:
        return ""
    if len(segs) == 1:
        return segs[0]
    return segs


def md_to_blocks(md: str):
    """Model GFM -> typed JSON blocks. None = too big, use fallback."""
    import re as _re
    src = (md or "").strip()
    if not src:
        return []
    pres = []

    def _stash_pre(m):
        pres.append(((m.group(1) or "").strip(), m.group(2)))
        return f"\x00PRE{len(pres) - 1}\x00"

    src = _re.sub(r"```(\w*)\n(.*?)```", _stash_pre, src, flags=_re.DOTALL)
    blocks = []
    # normalize fancy bullets to GFM list markers (models love •/·/◦)
    lines = [_re.sub(r"^(\s*)[•·◦▪▸]\s+", r"\1- ", ln)
             for ln in src.split("\n")]
    i, n = 0, len(lines)

    def _para(text):
        t = (text or "").strip()
        if not t:
            return None
        return {"type": "paragraph", "text": _rt_inline(t)}

    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if len(blocks) >= 450:
            return None
        if s.startswith("\x00PRE"):
            try:
                k = int(s[4:].rstrip("\x00"))
                lang, code = pres[k]
            except Exception:
                i += 1
                continue
            b = {"type": "pre", "text": code.strip()}
            if lang:
                b["language"] = lang
            blocks.append(b)
            i += 1
            continue
        m = _re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            t = _rt_inline(m.group(2).strip())
            if t != "":
                blocks.append({"type": "heading", "text": t,
                               "size": min(6, len(m.group(1)))})
            i += 1
            continue
        if _re.match(r"\s*\|(?!\|).*\|", line):
            rows = []
            while i < n and _re.match(r"\s*\|(?!\|).*\|", lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            header = len(rows) >= 2 and _is_delim_row(rows[1])
            rows = [r for r in rows if not _is_delim_row(r)]
            if rows:
                width = max(len(r) for r in rows)
                cells = []
                for ri, r in enumerate(rows):
                    r = (r + [""] * width)[:width]
                    cells.append([{
                        "align": "left", "valign": "top",
                        "text": _rt_inline(c),
                        **({"is_header": True}
                           if header and ri == 0 else {}),
                    } for c in r])
                blocks.append({"type": "table", "cells": cells,
                               "is_bordered": True, "is_striped": True})
            continue
        if s.startswith(">"):
            qs = []
            while i < n and lines[i].strip().startswith(">"):
                qs.append(_re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            kids = [k for k in (_para(q) for q in qs) if k]
            if kids:
                blocks.append({"type": "blockquote", "blocks": kids})
            continue
        if _re.match(r"\s*([-*]|\d+[.)])\s+", line):
            items = []
            while i < n:
                if not lines[i].strip():
                    # blank line: keep grouping if more items follow
                    j = i + 1
                    while j < n and not lines[j].strip():
                        j += 1
                    if (j < n and _re.match(r"\s*([-*]|\d+[.)])\s+",
                                               lines[j])):
                        i = j
                        continue
                    break
                if not _re.match(r"\s*([-*]|\d+[.)])\s+", lines[i]):
                    break
                items.append(_re.sub(r"^\s*([-*]|\d+[.)])\s+", "",
                                     lines[i]))
                i += 1
            lst = []
            for x in items:
                p = _para(x)
                if p:
                    lst.append({"blocks": [p]})
            if lst:
                blocks.append({"type": "list", "items": lst})
            continue
        if _re.match(r"^\s*(-{3,}|\*{3,})\s*$", s):
            blocks.append({"type": "divider"})
            i += 1
            continue
        p = _para(s)
        if p:
            blocks.append(p)
        i += 1
    return blocks


def has_table(md: str) -> bool:
    """GFM table present (2+ consecutive pipe rows)?"""
    import re as _re
    rows = 0
    for line in (md or "").splitlines():
        # spoiler ||...|| starts with double pipe — not a table row
        if _re.match(r"\s*\|(?!\|).*\|", line):
            rows += 1
            if rows >= 2:
                return True
        else:
            rows = 0
    return False


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;"))


def _inline(md: str) -> str:
    """GFM inline -> rich HTML subset, single pass (gaps escaped, tags
    intact). Code spans first-class so their contents stay literal."""
    import re as _re
    pat = _re.compile(
        r"`([^`\n]+)`"
        r"|\[([^\]\n]+)\]\((https?[^)\s]+)\)"
        r"|\*\*([^*`\n]+)\*\*"
        r"|__([^_`\n]+)__"
        r"|(?<!\*)\*([^*\n]+)\*(?!\*)"
        r"|(?<!_)_(?!_)([^_\n]+)(?<!_)_(?!_)"
        r"|~~([^~\n]+)~~"
        r"|\|\|([^|\n]+)\|\|")
    out = []
    pos = 0
    for m in pat.finditer(md or ""):
        out.append(_esc(md[pos:m.start()]))
        g = m.groups()
        if g[0] is not None:
            out.append(f"<code>{_esc(g[0])}</code>")
        elif g[1] is not None:
            out.append(f'<a href="{_esc(g[2].strip())}">{_esc(g[1])}</a>')
        elif g[3] is not None:
            out.append(f"<b>{_esc(g[3])}</b>")
        elif g[4] is not None:
            out.append(f"<u>{_esc(g[4])}</u>")
        elif g[5] is not None:
            out.append(f"<i>{_esc(g[5])}</i>")
        elif g[6] is not None:
            out.append(f"<i>{_esc(g[6])}</i>")
        elif g[7] is not None:
            out.append(f"<s>{_esc(g[7])}</s>")
        elif g[8] is not None:
            out.append(f"<tg-spoiler>{_esc(g[8])}</tg-spoiler>")
        pos = m.end()
    out.append(_esc((md or "")[pos:]))
    return "".join(out)


def _is_delim_row(cells) -> bool:
    import re as _re
    return bool(cells) and all(
        _re.match(r"^:?-+:?$", c.strip()) for c in cells)


def _split_row(line: str):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def md_to_html(md: str) -> str:
    """Model GFM -> rich HTML: real tables, headings, lists, code, quotes."""
    import re as _re
    src = (md or "").strip()
    # stash fenced code blocks
    pres = []

    def _stash_pre(m):
        lang = (m.group(1) or "").strip()
        pres.append((lang, m.group(2)))
        return f"\x00PRE{len(pres) - 1}\x00"

    src = _re.sub(r"```(\w*)\n(.*?)```", _stash_pre, src, flags=_re.DOTALL)
    blocks = []
    lines = src.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("\x00PRE"):
            blocks.append(s)
            i += 1
            continue
        m = _re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            blocks.append(f"<b>{_inline(m.group(2))}</b>")
            i += 1
            continue
        if _re.match(r"\s*\|.*\|\s*$", line):
            rows = []
            while i < n and _re.match(r"\s*\|.*\|\s*$", lines[i]):
                rows.append(_split_row(lines[i]))
                i += 1
            rows = [r for r in rows if not _is_delim_row(r)]
            if rows:
                t = ["<table bordered striped>"]
                for r in rows:
                    t.append("<tr>" + "".join(
                        f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
                t.append("</table>")
                blocks.append("".join(t))
            continue
        if s.startswith(">"):
            qs = []
            while i < n and lines[i].strip().startswith(">"):
                qs.append(_re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append("<blockquote>" + "<br>".join(
                _inline(q) for q in qs) + "</blockquote>")
            continue
        if _re.match(r"\s*([-*])\s+", line):
            items = []
            while i < n and _re.match(r"\s*([-*])\s+", lines[i]):
                items.append(_re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            blocks.append("<ul>" + "".join(
                f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        if _re.match(r"\s*\d+[.)]\s+", line):
            items = []
            while i < n and _re.match(r"\s*\d+[.)]\s+", lines[i]):
                items.append(_re.sub(r"^\s*\d+[.)]\s+", "", lines[i]))
                i += 1
            blocks.append("<ol>" + "".join(
                f"<li>{_inline(x)}</li>" for x in items) + "</ol>")
            continue
        if _re.match(r"^\s*(-{3,}|\*{3,})\s*$", s):
            i += 1
            continue
        blocks.append(_inline(s))
        i += 1
    out = "\n".join(blocks)
    for k, (lang, code) in enumerate(pres):
        out = out.replace(f"\x00PRE{k}\x00",
                          f"<pre>{_esc(code.strip())}</pre>")
    return out[:30000]
