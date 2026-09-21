"""Real AI agent engine: huggingface/smolagents CodeAgent (29400★) on 8080.

Thought -> code -> observation loop with tools (read/search/run/status).
No manual /commands: the user just talks, the agent acts.
Guards: threading semaphore (one model call at a time, bounded waiters),
per-user RAM agents (temporary memory, resettable via save flag),
non-disclosure prefix + output scrub (never what/where is running).
Sync library -> handlers call run_task via asyncio.to_thread.
"""
import logging
import os
import shlex
import threading
import time

logger: logging.Logger = logging.getLogger(__name__)

try:
    from smolagents import CodeAgent, OpenAIServerModel, tool
    _SMO_OK = True
except Exception as e:  # pragma: no cover
    _SMO_OK = False
    logger.warning("smolagents unavailable: %s", e)

from src.services import agent_tools as tools
from src.services import llama_probe as probe
from src.services import llm_config as _lc


def _patch_code_parser():
    """Tolerant <code> parsing for local coder models.

    Cyber-Tiel/Ornith often answer in plain text (sometimes with a stray
    </code> and no opening tag) instead of the mandatory <code> blob.
    Stock smolagents raises ValueError -> error loop -> fallback-only
    answers. Wrapper: accept plain ``` fences, else wrap the text as
    final_answer(...) so the turn completes instead of error-looping.
    Normal <code> blobs pass through untouched.
    """
    try:
        import smolagents.agents as _ag
        import smolagents.utils as _u
    except Exception:
        return
    _orig = _ag.parse_code_blobs

    def _repair(code: str) -> str:
        """Fix multiline final_answer("...") -> triple quotes.

        Coder models emit direct answers as final_answer("line1\nline2")
        with REAL newlines inside single double-quotes -> SyntaxError at
        exec, error loop in logs. Repair only that call; anything else
        passes through to the stock error path.
        """
        import ast as _ast
        import re as _re
        try:
            _ast.parse(code)
            return code
        except SyntaxError:
            pass

        def _fix_arg(m):
            inner = m.group(2)
            if "\n" not in inner:
                return m.group(0)
            safe = inner.replace('"""', '"\\"\\"')
            return f'final_answer("""{safe}""")'

        fixed = _re.sub(r'final_answer\(\s*(["\'])(.*?)\1\s*\)', _fix_arg,
                        code, flags=_re.DOTALL)
        try:
            _ast.parse(fixed)
            return fixed
        except SyntaxError:
            return code

    def _tolerant(text, tags):
        try:
            return _repair(_orig(text, tags))
        except ValueError as e:
            orig_err = e
        import re as _re
        t = (text or "").strip()
        m = _re.search(r"```(?:python|py)?\s*\n(.*?)```", t, _re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip()
        cleaned = _re.sub(r"</?code>", "", t).strip()
        lines = cleaned.splitlines()
        if len(lines) > 1 and _re.match(r"\s*thoughts?\s*:",
                                        lines[0], _re.IGNORECASE):
            cleaned = "\n".join(lines[1:]).strip()
        if cleaned:
            safe = cleaned.replace("\\", "\\\\").replace('"""', '"\\"\\"')
            return f'final_answer("""{safe}""")'
        raise orig_err

    _ag.parse_code_blobs = _tolerant
    try:
        _u.parse_code_blobs = _tolerant
    except Exception:
        pass


if _SMO_OK:
    _patch_code_parser()


def _patch_usage():
    """Tolerate missing `usage` in completions (NVIDIA reasoning models
    omit it) — smolagents crashes on response.usage.prompt_tokens."""
    try:
        from smolagents.models import OpenAIModel as _OM
    except Exception:
        return
    if getattr(_OM.create_client, "_usage_patched", False):
        return
    _orig = _OM.create_client

    def _patched(self):
        cli = _orig(self)
        try:
            import functools as _ft

            inner = cli.chat.completions.create

            @_ft.wraps(inner)
            def _create(*a, **k):
                resp = inner(*a, **k)
                try:
                    if getattr(resp, "usage", None) is None:
                        import types as _ts
                        resp.usage = _ts.SimpleNamespace(
                            prompt_tokens=0, completion_tokens=0,
                            total_tokens=0)
                except Exception:
                    pass
                return resp

            cli.chat.completions.create = _create
        except Exception:
            pass
        return cli

    _patched._usage_patched = True
    _OM.create_client = _patched


if _SMO_OK:
    _patch_usage()

LLAMA_BASE_URL, _LLM_KEY, _LLM_MODEL = _lc.get_active()


def refresh_llm():
    """Re-read backend (after /setapi) + rebuild model + drop agents."""
    global LLAMA_BASE_URL, _LLM_KEY, _LLM_MODEL, _MODEL
    LLAMA_BASE_URL, _LLM_KEY, _LLM_MODEL = _lc.get_active()
    try:
        _agents.clear()
        _order.clear()
    except Exception:
        pass
    if _SMO_OK:
        from smolagents import OpenAIServerModel as _M
        _MODEL = _M(model_id=_LLM_MODEL or "agent",
                    api_base=LLAMA_BASE_URL, api_key=_LLM_KEY,
                    client_kwargs=_lc.smolagents_kwargs(300))
    return LLAMA_BASE_URL, _LLM_KEY, _LLM_MODEL
MAX_STEPS = 5
MAX_TURNS = 30
MAX_AGENTS = 50

# Harmless stdlib the coder models love (stock sandbox allows only a few;
# anything else -> InterpreterError -> retry loop -> slow + log noise).
_EXTRA_IMPORTS = ["string", "json", "typing", "functools", "operator",
                  "copy", "hashlib", "base64", "decimal", "fractions",
                  "enum", "calendar", "textwrap", "html", "difflib",
                  "pprint", "numbers", "uuid"]


def do_web_search(query: str) -> str:
    """Plain live web search (no side effects). Shared by engines.

    Backends: DDG html/lite (captcha-aware) -> Wikipedia entity fallback.
    Network here blocks most search engines; Wiki/wttr.in are reliable.
    """
    import re as _re
    import urllib.request as _u

    def _fetch(url: str):
        req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _u.urlopen(req, timeout=8) as r:
            if getattr(r, "status", 200) != 200:
                return ""
            return r.read(300000).decode("utf-8", "replace")

    def _ddg_parse(html: str, k: int = 5):
        if not html or any(m in html.lower() for m in
                           ("anomaly-modal", "captcha", "challenge-platform")):
            return []
        links = _re.findall(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, flags=_re.DOTALL) or _re.findall(
            r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, flags=_re.DOTALL)
        out = []
        for href, title in links:
            if not href.startswith("http") or "duckduckgo" in href:
                continue
            t = _re.sub(r"<[^>]+>", "", title).strip()
            if t and len(t) > 8:
                out.append(f"- {t} ({href[:130]})")
            if len(out) >= k:
                break
        return out

    q = (query or "").strip()
    if not q:
        return "no results"
    try:
        lite = _fetch("https://lite.duckduckgo.com/lite/?q=" + _u.quote(q))
        out = []
        for m in _re.finditer(
                r"<a[^>]*href=\"([^\"]+)\"[^>]*class='result-link'>(.*?)</a>"
                r"|<a[^>]*class='result-link'[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
                lite, flags=_re.DOTALL):
            href, title = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            um = _re.search(r"uddg=([^&]+)", href or "")
            if um:
                href = _u.unquote(um.group(1))
            t = _re.sub(r"<[^>]+>", "", title or "").strip()
            if href.startswith("http") and t and len(t) > 8:
                out.append(f"- {t} ({href[:130]})")
            if len(out) >= 5:
                break
        if out:
            logger.info("web_search: %d hits (lite)", len(out))
            return "\n".join(out)
    except Exception:
        pass
    for base in ("https://html.duckduckgo.com/html/?q=",
                 "https://lite.duckduckgo.com/lite/?q="):
        try:
            hits = _ddg_parse(_fetch(base + _u.quote(q)))
            if hits:
                logger.info("web_search: %d hits", len(hits))
                return "\n".join(hits)
        except Exception:
            continue
    try:
        import json as _ji
        raw = _fetch("https://api.duckduckgo.com/?q=" + _u.quote(q) +
                     "&format=json&no_html=1&skip_disambig=1")
        d = _ji.loads(raw)
        abs_ = (d.get("AbstractText") or "").strip()
        if abs_:
            src = d.get("AbstractURL", "")
            logger.info("web_search: instant abstract")
            return f"- {abs_[:400]} ({src[:120]})"
    except Exception:
        pass
    try:
        import json as _js
        raw = _fetch("https://en.wikipedia.org/w/api.php?action=query"
                     "&list=search&format=json&srlimit=3&srsearch=" + _u.quote(q))
        sr = _js.loads(raw).get("query", {}).get("search", [])
        out = []
        for x in sr:
            t = x.get("title", "")
            s = _re.sub(r"<[^>]+>", "", x.get("snippet", "")).strip()
            if t:
                out.append(f"- {t}: {s[:250]}")
        if out:
            return "[Reference (Wikipedia):]\n" + "\n".join(out)
    except Exception:
        pass
    return "no results"


def do_fetch_page(url: str) -> str:
    """Plain page fetch (no side effects). Shared by engines."""
    import re as _re2
    import urllib.request as _u2
    try:
        req = _u2.Request(url or "", headers={"User-Agent": "Mozilla/5.0"})
        with _u2.urlopen(req, timeout=10) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return "unsupported content type"
            html = r.read(200000).decode("utf-8", "replace")
        html = _re2.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>",
                        " ", html, flags=_re2.DOTALL | _re2.IGNORECASE)
        txt = _re2.sub(r"<[^>]+>", " ", html)
        txt = _re2.sub(r"\s+", " ", txt).strip()
        return txt[:3000] or "empty page"
    except Exception as e:
        return f"fetch error: {e}"

TASK_PREFIX = (
    "[Respond in the user's language. You may state your model name if "
    "asked. Never reveal hosts, file paths, ports, or where/how you run. "
    "You HAVE live internet access via tools. Tools (ONLY these two): "
    "web_search(query) for live web info, "
    "fetch_page(url) to read a page. Use a tool when you need "
    "current/external facts; else answer directly with NO tool call. "
    "Never call the same tool twice in a row — answer from results you "
    "already have. "
    "FORMAT (mandatory every reply): Thoughts: <one-line reasoning> "
    "then <code> block with EITHER a tool call OR "
    "final_answer(\"your answer\") for direct answers. "
    "Never output bare text, never output </code> without opening <code>. "
    "Example direct answer: Thoughts: greeting, no tools needed. "
    "<code> final_answer(\"Hi! How can I help?\") </code>. "
    "Rich format (STRICT GitHub markdown, every answer): # headings, "
    "**bold** key terms, *italic*, `code`, ```lang fenced code, "
    "GFM | tables | with header row, - lists, > quotes, ||spoiler||. "
    "NEVER • bullets (use -), NEVER bare code without fences, no HTML. "
    "Use structure only when it helps; trivial replies stay plain "
    "1-2 sentences with zero boilerplate sections. "
    "Prefer direct article/page URLs; avoid google.com/search URLs (blocked). "
    "NEVER import os, subprocess, or sys; never use shell or write files. "
    "NEVER claim no internet access. Answer concisely (under 150 words "
    "unless detail requested). Just do the task.]\n"
)

# Durable per-user transcript (survives restarts). Tiny capped JSON, not a DB.
MEM_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "memory")
MEM_PAIRS = 20
MEM_CHARS = 1200

BUSY_RU = ("Model is busy. Wait a bit and retry.")

_sem = threading.Semaphore(2)
_waiting = 0
_waiting_lock = threading.Lock()
_agents: dict = {}
_order: list = []
_tls = threading.local()

_PROBE_CACHE = {"t": 0.0, "v": None}


def _probe_cached():
    """probe_llama cached 20s: called on EVERY message, extra HTTP each."""
    now = time.monotonic()
    if _PROBE_CACHE["v"] is not None and now - _PROBE_CACHE["t"] < 20:
        return _PROBE_CACHE["v"]
    v = probe.probe_llama(LLAMA_BASE_URL)
    _PROBE_CACHE.update(t=now, v=v)
    return v
# Sink for live "thinking/acting" status, per-run via ContextVar.
# A plain global crossed streams when 2 runs share the semaphore.
_SINK_VAR = None


def _get_sink_var():
    global _SINK_VAR
    if _SINK_VAR is None:
        import contextvars as _cv
        _SINK_VAR = _cv.ContextVar("smo_sink", default=None)
    return _SINK_VAR


def _note(text: str) -> None:
    """Terminal-style status: `$ <tool> <args>`, raw markdown code span.

    Rendered via rich-markdown drafts (no V2 escaping). Callers use
    Russian shorthand or `$ cmd`; mapped here to terminal form.
    """
    try:
        sink = _get_sink_var().get()
        if sink is None:
            return
        t = str(text or "")
        cmd = t if t.startswith("$") else ("$ " + t if t else "")
        # Raw text: sink renders via rich-markdown, NOT MarkdownV2
        # (V2 backslash-escaping would show literal backslashes).
        sink.append(f"`{cmd}`"[:220])
        try:
            logger.info("tool: %s", (cmd.split() + ["?"])[1])
        except Exception:
            pass
    except Exception:
        pass


if _SMO_OK:
    @tool
    def read_file(path: str) -> str:
        """Read a text file from the workspace and return its content.

        Args:
            path: relative file path to read.
        """
        _note(f"$ read {path}"[:120])
        return str(tools.read_file(path))[:3000]

    @tool
    def search_files(pattern: str, path: str = ".") -> str:
        """Search files in the workspace by regex and return matches.

        Args:
            pattern: regex pattern to search for.
            path: relative directory to search in.
        """
        _note(f"$ search {pattern}"[:120])
        return str(tools.search(pattern, path))[:3000]

    @tool
    def run_code(command: str) -> str:
        """Run a shell command in an auto-cleaned sandbox, return output.

        Args:
            command: shell command to run, e.g. python3 -c 'print(1)'.
        """
        _note(f"$ run {command}"[:120])
        try:
            res = tools.run_code(
                shlex.split(command),
                user=str(getattr(_tls, "uid", "agent")))
        except Exception as e:
            return f"tool error: {e}"
        out = (f"code={res['code']}\nSTDOUT:\n{res['stdout']}\n"
               f"STDERR:\n{res['stderr']}")
        return out[:3000]

    @tool
    def get_status() -> str:
        """Check whether the local model sees images and queue depth.

        Args:
            _dummy: unused, pass empty string.
        """
        _note("$ status")
        return _status_text()

    @tool
    def web_search(query: str) -> str:
        """Search the live web and return titles/urls/snippets.

        Args:
            query: search query.
        """
        _note(f"$ search {query}"[:120])
        return do_web_search(query)

    @tool
    def fetch_page(url: str) -> str:
        """Fetch a web page and return clean text (for grounding).

        Args:
            url: page URL to read.
        """
        _note(f"$ fetch {url[:80]}")
        return do_fetch_page(url)

    @tool
    def write_file(path: str, content: str) -> str:
        """Save a file to the user inbox (persistent). For deliverables.

        Args:
            path: file name (saved under user inbox).
            content: text content to write.
        """
        _note(f"$ save {path}"[:120])
        try:
            uid = str(int(getattr(_tls, "uid", 0) or 0))
            tag = f"u{abs(hash(uid)) % 10**8:08d}"
            base = tools.WORKDIR
            import os as _os, re as _re
            name = _re.sub(r"[^A-Za-z0-9_.\- ]+", "_",
                           _os.path.basename(path or "file"))[:80] or "file"
            ap = _os.path.abspath(_os.path.join(base, "inbox", tag, name))
            if ap != base and not ap.startswith(base + _os.sep):
                return "bad path"
            _os.makedirs(_os.path.dirname(ap), exist_ok=True)
            data = str(content or "")
            if len(data.encode("utf-8", "replace")) > 5 * 1024 * 1024:
                return "too large (5MB max)"
            with open(ap, "w", encoding="utf-8") as f:
                f.write(data)
            return f"saved: inbox/{tag}/{name}"
        except Exception as e:
            return f"tool error: {e}"

    @tool
    def send_file(path: str) -> str:
        """Queue a file to send to the user (only what you choose).

        Args:
            path: inbox-relative or absolute path of file to send.
        """
        _note(f"$ send {path}"[:120])
        try:
            import os as _os2
            uid2 = str(int(getattr(_tls, "uid", 0) or 0))
            tag2 = f"u{abs(hash(uid2)) % 10**8:08d}"
            base2 = tools.WORKDIR
            cand = (path or "").strip()
            if not _os2.path.isabs(cand):
                cand = _os2.path.join(base2, "inbox", tag2,
                                      _os2.path.basename(cand))
            ap2 = _os2.path.abspath(cand)
            if ap2 != base2 and not ap2.startswith(base2 + _os2.sep):
                return "bad path"
            if not _os2.path.isfile(ap2):
                return "not found"
            if _os2.path.getsize(ap2) > 20 * 1024 * 1024:
                return "too large (20MB max)"
            key2 = (int(uid2), str(getattr(_tls, "cid", uid2)))
            _OUTBOX.setdefault(key2, []).append(ap2)
            return f"queued: {_os2.path.basename(ap2)}"
        except Exception as e:
            return f"tool error: {e}"

    _TOOLS = [web_search, fetch_page]
    _MODEL = OpenAIServerModel(
        model_id=_LLM_MODEL or "agent", api_base=LLAMA_BASE_URL,
        api_key=_LLM_KEY,
        client_kwargs=_lc.smolagents_kwargs(300))
else:  # pragma: no cover
    _TOOLS, _MODEL = [], None


_OUTBOX = {}


def drain_outbox(uid, cid):
    """Take agent-queued file paths for sending (clears the queue)."""
    import os as _os
    try:
        key = (int(uid), str(cid))
    except Exception:
        return []
    paths = _OUTBOX.pop(key, [])
    out = []
    for p in paths:
        try:
            if _os.path.isfile(p) and _os.path.getsize(p) <= 20 * 1024 * 1024:
                out.append(p)
        except OSError:
            continue
    return out


def _status_text() -> str:
    """Operational only: vision yes/no + queue. No identity, no where."""
    from src.services.llm_queue import QUEUE
    p = probe.probe_llama(LLAMA_BASE_URL)
    if not p["ok"]:
        return "model unavailable"
    return (f"vision: {'yes' if p['has_vision'] else 'no'}, "
            f"queue: {QUEUE.depth()}")


def _get_agent(uid: int, cid: int):
    key = (int(uid), int(cid))
    ent = _agents.get(key)
    if ent is None or ent[1] >= MAX_TURNS:
        ent = (CodeAgent(tools=_TOOLS, model=_MODEL,
                         additional_authorized_imports=_EXTRA_IMPORTS,
                         max_steps=MAX_STEPS, verbosity_level=0), 0)
        _agents[key] = ent
        _order.append(key)
        while len(_order) > MAX_AGENTS:
            _agents.pop(_order.pop(0), None)
    return ent[0], key


def _try_fast(text: str, transcript: str):
    """One direct generation for trivial questions (no agent loop).

    Returns the answer, or None if the model asks for tools (marker
    NEED_TOOLS) or the call fails. Keeps simple questions at exactly
    1 generation instead of up to MAX_STEPS slow agent turns.
    """
    try:
        _b, _k, _m = _lc.get_active()
        cli = _lc.make_openai(timeout=120, base=_b, key=_k)
        sys = ("Answer briefly in the user's language. STRICT GitHub "
               "markdown format "
               "in every answer: # headings, **bold**, *italic*, `code`, "
               "``` fenced blocks with language, GFM |tables| with header, "
               "- lists, > quotes, ||spoiler||. NEVER • bullets (use -), "
               "NEVER bare code without fences, no HTML. "
               "Use structure only when it helps; trivial replies stay plain "
               "1-2 sentences with zero boilerplate sections. "
               "If you need fresh data from the internet or files "
               "to answer — reply with exactly NEED_TOOLS "
               "и ничего больше.")
        content = ((transcript + "\n" + text) if transcript else text)
        r = cli.chat.completions.create(
            model=_m or "local",
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": content}],
            temperature=0.2)
        out = ((r.choices[0].message.content) or "").strip()
        t = out.upper().strip().strip("`\"'")
        if out and not (t == "NEED_TOOLS"
                        or t.startswith("NEED_TOOLS\n")
                        or t.startswith("NEED_TOOLS ")):
            return out
        return None
    except Exception:
        return None


def run_task(text: str, uid: int, cid: int, save_on: bool = True,
             max_wait_s: int = 120, step_sink=None) -> str:
    """Blocking agent run. Raises BusyError/RuntimeError on overload/fail.

    step_sink: optional list receiving live 'thinking/acting' notes from
    the tools themselves (no smolagents callbacks involved).
    """
    global _waiting
    if not _SMO_OK:  # pragma: no cover
        raise RuntimeError("agent engine unavailable")
    with _waiting_lock:
        if _waiting >= 20:
            raise RuntimeError(BUSY_RU)
        _waiting += 1
    try:
        _tls.uid = int(uid)
    except Exception:
        pass
    try:
        _tls.cid = cid
    except Exception:
        pass
    try:
        _lfm = _detect_lfm()
        logger.info("engine: %s", "lfm" if _lfm else "code")
        if _lfm:
            acquired = _sem.acquire(timeout=max(1, max_wait_s))
            if not acquired:
                raise RuntimeError(BUSY_RU)
            try:
                _get_sink_var().set(step_sink)
                tc = transcript_context(uid, cid) if save_on else ""
                fast = _try_fast(text, tc)
                if fast:
                    return final_clean(fast)
                return final_clean(run_lfm_task(text, True, tc, step_sink))
            finally:
                _get_sink_var().set(None)
                _sem.release()
    except RuntimeError:
        raise
    except Exception:
        pass
    t0 = time.monotonic()
    try:
        _tls.uid = int(uid)
        try:
            _tls.cid = cid
        except Exception:
            pass
        try:
            if not _lc.proxy_alive_now():
                _lc.drop_proxy_cache()
                refresh_llm()
        except Exception:
            pass
        if save_on:
            agent, key = _get_agent(uid, cid)
        else:
            agent = CodeAgent(tools=_TOOLS, model=_MODEL,
                              additional_authorized_imports=_EXTRA_IMPORTS,
                              max_steps=MAX_STEPS, verbosity_level=0)
            key = None
        if time.monotonic() - t0 > max_wait_s:
            raise RuntimeError(BUSY_RU)
        # Semaphore wait bounded by remaining budget (poll for simplicity).
        acquired = _sem.acquire(timeout=max(1, max_wait_s - (time.monotonic() - t0)))
        if not acquired:
            raise RuntimeError(BUSY_RU)
        try:
            _get_sink_var().set(step_sink)
            prompt = TASK_PREFIX + (text or "")
            if save_on:
                tc2 = transcript_context(uid, cid)
                prompt = tc2 + prompt
                fast = _try_fast(text, tc2)
                if fast:
                    return final_clean(fast)
            try:
                out = agent.run(prompt)
            except Exception as e:
                s = str(e).lower()
                if ("connect" in s or "timeout" in s or "timed out" in s
                        or "451" in s or "forbidden" in s
                        or "remoteprotocol" in s or "network" in s):
                    # Flaky egress proxy: fresh hunt + rebuild + one retry.
                    _lc.drop_proxy_cache()
                    refresh_llm()
                    if save_on:
                        agent, _ = _get_agent(uid, cid)
                    else:
                        agent = CodeAgent(
                            tools=_TOOLS, model=_MODEL,
                            additional_authorized_imports=_EXTRA_IMPORTS,
                            max_steps=MAX_STEPS, verbosity_level=0)
                    out = agent.run(prompt)
                else:
                    raise
        finally:
            _get_sink_var().set(None)
            _sem.release()
        if save_on and key is not None:
            ag, turns = _agents.get(key, (agent, 0))
            _agents[key] = (ag, turns + 1)
        return final_clean(str(out or ""))
    except Exception as e:
        from src.services.status import brief_net_error as _bneR
        _bneR(logger, "agent run", e)
        raise
    finally:
        try:
            _tls.uid = 0
            _tls.cid = 0
        except Exception:
            pass
        with _waiting_lock:
            _waiting -= 1


def _detect_lfm() -> bool:
    """Robust LFM check: props id, /v1/models list, or AGENT_ENGINE=lfm."""
    try:
        mid = str(_probe_cached().get("active_model_id", "") or "")
        if "lfm" in mid.lower():
            return True
    except Exception:
        pass
    try:
        import urllib.request as _u
        import json as _j
        with _u.urlopen(LLAMA_BASE_URL + "/models", timeout=4) as r:
            d = _j.loads(r.read(20000).decode("utf-8", "replace"))
        for m in (d.get("models") or d.get("data") or []):
            s = str(m.get("model", "") or m.get("name", "")
                    or m.get("id", ""))
            if "lfm" in s.lower():
                return True
    except Exception:
        pass
    try:
        if os.getenv("AGENT_ENGINE", "").lower() == "lfm":
            return True
    except Exception:
        pass
    return False


def _lfm_scrub(out: str) -> str:
    """Strip tool-call markup from final text (never show it to user)."""
    import re as _re
    return _re.sub(r"<\|tool_call_start\|>.*?<\|tool_call_end\|>", "",
                   out or "", flags=_re.DOTALL).strip()


def final_clean(text: str) -> str:
    """Last-line defense: never show agent scaffolding to the user.

    - final_answer("...") (bare or in <code>) -> inner text.
    - <code>...</code> code samples -> real ``` fences.
    - lone tags / Thought-preamble lines -> dropped.
    """
    import re as _re
    t = _lfm_scrub(text or "")
    if not t:
        return ""
    m = _re.search(r'final_answer\(\s*("""|\'\'\'|"|\')(.*?)\1\s*\)',
                   t, _re.DOTALL)
    if m and m.group(2).strip():
        t = m.group(2).strip()
    else:
        t = _re.sub(r"<code>(.*?)</code>", r"```\1```", t,
                    flags=_re.DOTALL | _re.IGNORECASE)
    t = _re.sub(r"</?code>", "", t).strip()
    # full-line tool-call statements never reach the user
    t = _re.sub(r"(?m)^[ \t]*(?:\w+\s*=\s*)?(?:web_search|fetch_page)\s*\(.*$",
                "", t).strip()
    t = _re.sub(r"\n{3,}", "\n\n", t).strip()
    lines = t.splitlines()
    if (len(lines) > 1 and len(lines[0]) < 300
            and _re.match(r"\s*thought[^\n:]{0,4}:", lines[0],
                          _re.IGNORECASE)):
        t = "\n".join(lines[1:]).strip()
    return t


def _lfm_schema():
    return [
        {"name": "web_search",
         "description": "Search the live web for current/external facts.",
         "parameters": {"type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"]}},
        {"name": "fetch_page",
         "description": "Fetch a web page URL and return clean text.",
         "parameters": {"type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"]}},
    ]


def _lfm_parse(out: str):
    """Extract (name, arg) from <|tool_call_start|>[name(arg="v")]<|...|>."""
    import re as _re
    m = _re.search(r"<\|tool_call_start\|>\s*\[(.*?)\]\s*<\|tool_call_end\|>",
                   out or "", flags=_re.DOTALL)
    if not m:
        return None
    call = m.group(1).strip()
    m2 = _re.match(r"(\w+)\s*\((.*)\)\s*$", call, flags=_re.DOTALL)
    if not m2:
        return None
    name, argstr = m2.group(1), m2.group(2)
    m3 = _re.search(r'''["'](.*)["']''', argstr, flags=_re.DOTALL)
    arg = m3.group(1) if m3 else argstr.strip().strip("\"'")
    return name, arg


def run_lfm_task(text: str, save_on: bool, transcript: str,
                 step_sink=None) -> str:
    """Native LFM tool loop (docs-driven): tools JSON in system, parse
    <|tool_call_start|>, `tool` role replies. temperature 0.2 per docs."""
    import json as _json
    _bb, _kk, _mm = _lc.get_active()
    client = _lc.make_openai(timeout=300, base=_bb, key=_kk)
    tools = _lfm_schema()
    sys = ("You are a helpful assistant. Respond in the user's language. "
           "Rich format (STRICT GitHub markdown, every answer): # headings, "
           "**bold** key terms, *italic*, `code`, ```lang fenced code, "
           "GFM | tables | with header row, - lists, > quotes, "
           "||spoiler||. NEVER • bullets (use -), NEVER bare code "
           "without fences, no HTML. "
    "Use structure only when it helps; trivial replies stay plain "
    "1-2 sentences with zero boilerplate sections. "
           "You may state your model name if asked. Never reveal hosts, "
           "file paths, ports, or where/how you run. "
           f"List of tools: {_json.dumps(tools)} "
           "If you need current/external facts, call ONE tool as "
           "<|tool_call_start|>[name(arg=\"value\")]<|tool_call_end|> "
           "and nothing else. Otherwise answer directly.")
    msgs = [{"role": "system", "content": sys}]
    if transcript:
        msgs.append({"role": "user",
                     "content": transcript + "\n[Current task:]\n" + text})
    else:
        msgs.append({"role": "user", "content": text})
    last = ""
    seen = set()
    for i in range(MAX_STEPS):
        r = client.chat.completions.create(
            model=_mm or "local", messages=msgs, temperature=0.2)
        out = (r.choices[0].message.content or "").strip()
        last = out
        parsed = _lfm_parse(out)
        if not parsed:
            return _lfm_scrub(out) or "Failed to answer."
        name, arg = parsed
        key = (name, arg[:120])
        if key in seen or i >= MAX_STEPS - 1:
            # Repeat or out of steps: force a direct answer from results.
            msgs.append({"role": "assistant", "content": out})
            msgs.append({"role": "user", "content":
                         "Answer the original question in the user's "
                         "language right now, without calling tools. Use "
                         "the results above."})
            try:
                r2 = client.chat.completions.create(
                    model=_mm or "local", messages=msgs, temperature=0.2)
                fin = (r2.choices[0].message.content or "").strip()
            except Exception:
                fin = ""
            return _lfm_scrub(fin) or _lfm_scrub(out) or last
        seen.add(key)
        if name == "web_search":
            res = do_web_search(arg)
            disp = f"`$ search {arg[:60]}`"
        elif name == "fetch_page":
            res = do_fetch_page(arg)
            disp = f"`$ fetch {arg[:60]}`"
        else:
            res = f"unknown tool: {name}. Answer directly."
            disp = "`$ unknown tool`"
        if step_sink is not None:
            try:
                step_sink.append(disp)
            except Exception:
                pass
        msgs.append({"role": "assistant", "content": out})
        # NOTE: `tool` role is NOT used — plain GGUF chat templates
        # mangle it and the model re-searches in a loop. User-role
        # results are understood and answered from.
        msgs.append({"role": "user", "content":
                     f"[{name} result:]\n{str(res)[:3000]}\n"
                     "If that is enough — answer in the user's language. "
                     "Call a tool again only if data is clearly missing."})
    return _lfm_scrub(last) or "Failed to answer."


def reset_memory(uid: int, cid: int) -> None:
    _agents.pop((int(uid), str(cid)), None)
    _agents.pop((int(uid), int(cid)), None)
    try:
        os.remove(_mem_path(uid, cid))
    except OSError:
        pass


def reset_user(uid: int) -> None:
    """Purge all agent memory + transcripts for a user (any chat)."""
    try:
        u = int(uid)
    except Exception:
        return
    for key in [k for k in list(_agents.keys())
                if isinstance(k, tuple) and k and int(k[0]) == u]:
        _agents.pop(key, None)
    try:
        import glob as _glob
        for fp in _glob.glob(os.path.join(MEM_DIR, f"{u}_*.json")):
            try:
                os.remove(fp)
            except OSError:
                pass
    except Exception:
        pass


def _mem_path(uid: int, cid: int) -> str:
    return os.path.join(MEM_DIR, f"{int(uid)}_{int(cid)}.json")


def load_transcript(uid: int, cid: int) -> list:
    try:
        with open(_mem_path(uid, cid), "r", encoding="utf-8") as f:
            d = __import__("json").load(f)
    except (FileNotFoundError, ValueError, OSError):
        return []
    return d if isinstance(d, list) else []


def remember(uid: int, cid: int, q: str, a: str) -> None:
    """Persist one sent turn (capped). Called with what the user SAW."""
    try:
        os.makedirs(MEM_DIR, exist_ok=True)
        pairs = load_transcript(uid, cid)
        pairs.append({"q": str(q or "")[:MEM_CHARS],
                      "a": str(a or "")[:MEM_CHARS]})
        pairs = pairs[-MEM_PAIRS:]
        import json as _json
        tmp = _mem_path(uid, cid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(pairs, f, ensure_ascii=False)
        os.replace(tmp, _mem_path(uid, cid))
    except OSError:
        pass


def transcript_context(uid: int, cid: int) -> str:
    pairs = load_transcript(uid, cid)
    if not pairs:
        return ""
    lines = ["[Previous conversation (for memory):]"]
    for p in pairs[-8:]:
        lines.append(f"User: {p.get('q', '')[:MEM_CHARS]}")
        lines.append(f"Assistant: {p.get('a', '')[:MEM_CHARS]}")
    return "\n".join(lines) + "\n[End of memory. Current task:]\n"
