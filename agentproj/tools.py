from __future__ import annotations

import ast
import asyncio
import os
import tempfile
from backends.base import ToolDefinition

# ---------------------------------------------------------------------------
# Tool definitions (consumed by LLM backends)
# ---------------------------------------------------------------------------

TOOL_RUN_PYTHON = ToolDefinition(
    name="run_python",
    description=(
        "Execute Python code in a subprocess and return its stdout output. "
        "Use this for ALL arithmetic, calculations, data processing, currency "
        "conversion, statistics, or any task involving numbers. "
        "Do NOT guess numeric results — always use this tool. "
        "Print your results with print(). Timeout: 10 seconds. "
        "Only calculation-oriented Python is allowed; filesystem, process, "
        "network, and introspection APIs are blocked."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Valid Python code. Use print() to output results.",
            }
        },
        "required": ["code"],
    },
)

TOOL_WEB_SEARCH = ToolDefinition(
    name="web_search",
    description=(
        "Search the web using DuckDuckGo and return titles, URLs, and snippets. "
        "Use for current events, facts, exchange rates, prices, or anything "
        "that requires up-to-date information not in the conversation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to return (1-10). Default: 5.",
                "default": 5,
            },
        },
        "required": ["query"],
    },
)

TOOL_GET_CONTEXT = ToolDefinition(
    name="get_context",
    description=(
        "Returns the current conversation capture buffer for this chat — "
        "all messages recorded since /begin was last used. "
        "Call this to review the full conversation before answering."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)

ALL_TOOLS: list[ToolDefinition] = [TOOL_RUN_PYTHON, TOOL_WEB_SEARCH, TOOL_GET_CONTEXT]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def run_python(code: str) -> str:
    """Execute calculation-oriented Python in a restricted subprocess."""
    validation_error = _validate_python_tool_code(code)
    if validation_error:
        return f"Error: {validation_error}"

    try:
        with tempfile.TemporaryDirectory(prefix="telegram-bot-python-") as tmpdir:
            env = {
                "HOME": tmpdir,
                "PATH": os.environ.get("PATH", ""),
                "PYTHONIOENCODING": "utf-8",
            }
            kwargs = {}
            if hasattr(os, "setsid"):
                kwargs["preexec_fn"] = _limit_child_process

            proc = await asyncio.create_subprocess_exec(
                "python3", "-I", "-c", _build_restricted_runner(code),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=env,
                **kwargs,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return "Error: code execution timed out (10s limit)."

            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                return f"Error: {err[:2000]}"

            output = stdout.decode(errors="replace").strip()
            if len(output) > 4000:
                output = output[:4000] + "\n... (output truncated)"
            return output if output else "(no output)"

    except Exception as e:
        return f"Error: {e}"


_ALLOWED_IMPORTS = {
    "calendar",
    "collections",
    "datetime",
    "decimal",
    "fractions",
    "functools",
    "itertools",
    "json",
    "math",
    "operator",
    "random",
    "re",
    "statistics",
}

_BLOCKED_NAMES = {
    "__builtins__",
    "__import__",
    "breakpoint",
    "compile",
    "dir",
    "eval",
    "exec",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "open",
    "quit",
    "type",
    "vars",
}


def _validate_python_tool_code(code: str) -> str | None:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"invalid Python syntax: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name.split(".", 1)[0] for alias in node.names]
            if any(module not in _ALLOWED_IMPORTS for module in modules):
                allowed = ", ".join(sorted(_ALLOWED_IMPORTS))
                return f"only these imports are allowed: {allowed}"

        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                return "relative imports are not allowed"
            module = node.module.split(".", 1)[0]
            if module not in _ALLOWED_IMPORTS:
                allowed = ", ".join(sorted(_ALLOWED_IMPORTS))
                return f"only these imports are allowed: {allowed}"

        if isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id in _BLOCKED_NAMES:
                return f"name '{node.id}' is not allowed"

        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                return f"attribute '{node.attr}' is not allowed"

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_NAMES:
                return f"call to '{node.func.id}' is not allowed"

    return None


def _limit_child_process() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except Exception:
        pass


def _build_restricted_runner(code: str) -> str:
    return f"""
import builtins

_allowed_builtin_names = {{
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
    "float", "format", "int", "len", "list", "map", "max", "min", "pow",
    "print", "range", "round", "set", "slice", "sorted", "str", "sum",
    "tuple", "zip"
}}
_allowed_imports = {sorted(_ALLOWED_IMPORTS)!r}

def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if root not in _allowed_imports:
        raise ImportError(f"Import '{{name}}' is not allowed")
    return __import__(name, globals, locals, fromlist, level)

_safe_builtins = {{name: getattr(builtins, name) for name in _allowed_builtin_names}}
_safe_builtins["__import__"] = _restricted_import

_scope = {{"__builtins__": _safe_builtins}}
exec({code!r}, _scope, _scope)
"""


async def web_search(query: str, max_results: int = 5) -> str:
    """Search DuckDuckGo and return formatted results."""
    max_results = max(1, min(max_results, 10))
    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(
            None, _ddg_search, query, max_results
        )
    except Exception as e:
        return f"Error: web search failed — {e}"

    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("href", "")
        snippet = r.get("body", "")
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


def _ddg_search(query: str, max_results: int) -> list[dict]:
    """Synchronous DuckDuckGo search (run in executor)."""
    from duckduckgo_search import DDGS
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


async def get_context_tool(chat_id: int, history_manager) -> str:
    """Return the formatted capture buffer for this chat."""
    from history import HistoryManager
    msgs = history_manager.get_capture_buffer(chat_id)
    return HistoryManager.format_for_llm(msgs)
