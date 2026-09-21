from __future__ import annotations

import json
import re
import uuid

from .base import LLMBackend, LLMMessage, ToolCall, ToolDefinition

# Models known to support Ollama's native tool/function calling API.
# Names are matched as prefixes (strips ":tag" version suffixes).
_TOOL_CAPABLE_PREFIXES = {
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "mistral-nemo",
    "mistral-small",
    "qwen2.5",
    "qwen2.5-coder",
    "command-r",
    "command-r-plus",
    "firefunction-v2",
    "hermes3",
}

_REACT_TOOL_CALL_RE = re.compile(
    r"TOOL:\s*(\w+)\s*\nARGS:\s*(\{.*?\})\s*\nEND",
    re.DOTALL | re.IGNORECASE,
)


class OllamaBackend(LLMBackend):
    def __init__(
        self,
        model_name: str,
        host: str = "http://localhost:11434",
        num_ctx: int | None = None,
        think: bool | None = None,
    ) -> None:
        self._model_name = model_name
        self._host = host
        self._num_ctx = num_ctx
        self._think = think
        self._client = None  # lazily initialised to avoid import-time crash

    @property
    def name(self) -> str:
        return f"ollama:{self._model_name}"

    @property
    def supports_native_tools(self) -> bool:
        base = self._model_name.split(":")[0].lower()
        return base in _TOOL_CAPABLE_PREFIXES

    async def get_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> tuple[str | None, list[ToolCall], list[Any]]:
        if self.supports_native_tools:
            return await self._native_call(messages, tools, system_prompt)
        else:
            return await self._react_call(messages, tools, system_prompt)

    # ------------------------------------------------------------------
    # Native tool use path
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            import ollama as ollama_sdk
            self._client = ollama_sdk.AsyncClient(host=self._host)
        return self._client

    async def _native_call(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> tuple[str | None, list[ToolCall], list[Any]]:
        ollama_messages = _to_ollama_messages(messages, system_prompt)
        ollama_tools = [_tool_def_to_ollama(t) for t in tools]

        response = await self._chat(
            model=self._model_name,
            messages=ollama_messages,
            tools=ollama_tools,
        )
        msg = response.message

        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=str(uuid.uuid4()),
                    name=tc.function.name,
                    arguments=dict(tc.function.arguments),
                )
                for tc in msg.tool_calls
            ]
            return None, tool_calls, []

        text = (msg.content or "").strip()
        return (text or None, [], [])

    # ------------------------------------------------------------------
    # ReAct text-protocol fallback path
    # ------------------------------------------------------------------

    def _build_react_system(
        self, original_system: str, tools: list[ToolDefinition]
    ) -> str:
        tool_descriptions = "\n".join(
            f"- {t.name}: {t.description}" for t in tools
        )
        tool_names = ", ".join(t.name for t in tools)
        return (
            f"{original_system}\n\n"
            "---\n"
            "You have access to tools. To call a tool, output EXACTLY this format "
            "(nothing else on those lines):\n\n"
            "TOOL: <tool_name>\n"
            "ARGS: <json object>\n"
            "END\n\n"
            f"Available tools ({tool_names}):\n{tool_descriptions}\n\n"
            "After receiving a RESULT, continue your reasoning normally. "
            "When you have a final answer, respond without any TOOL/ARGS/END block."
        )

    async def _react_call(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> tuple[str | None, list[ToolCall], list[Any]]:
        react_system = self._build_react_system(system_prompt, tools)
        ollama_messages = _to_ollama_messages(messages, react_system)

        response = await self._chat(
            model=self._model_name,
            messages=ollama_messages,
        )
        text = (response.message.content or "").strip()

        match = _REACT_TOOL_CALL_RE.search(text)
        if match:
            tool_name = match.group(1).strip()
            args_str = match.group(2).strip()
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                # Malformed args — return the raw text so the loop can terminate
                return text, [], []
            return None, [ToolCall(id=str(uuid.uuid4()), name=tool_name, arguments=args)], []

        return text or None, [], []

    # ------------------------------------------------------------------
    # Tool result formatting
    # ------------------------------------------------------------------

    def format_tool_result(self, call_id: str, name: str, result: str) -> LLMMessage:
        if self.supports_native_tools:
            # Ollama native: role="tool"
            return LLMMessage(role="tool_result", content=result, tool_call_id=call_id)
        else:
            # ReAct: inject as user message with RESULT prefix
            return LLMMessage(role="user", content=f"RESULT: {result}")

    async def _chat(self, **kwargs):
        options = dict(kwargs.pop("options", {}) or {})
        if self._num_ctx:
            options["num_ctx"] = self._num_ctx
        if options:
            kwargs["options"] = options
        if self._think is not None:
            kwargs["think"] = self._think

        try:
            return await self._get_client().chat(**kwargs)
        except TypeError as e:
            if "think" not in kwargs:
                raise
            kwargs.pop("think", None)
            return await self._get_client().chat(**kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_ollama_messages(
    messages: list[LLMMessage], system_prompt: str
) -> list[dict]:
    result: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in messages:
        if msg.role == "user":
            if isinstance(msg.content, list):
                import base64
                text_parts = [item["text"] for item in msg.content if item.get("type") == "text"]
                image_parts = [item["data"] for item in msg.content if item.get("type") == "image"]
                entry: dict = {"role": "user", "content": " ".join(text_parts)}
                if image_parts:
                    entry["images"] = [base64.b64encode(img).decode() for img in image_parts]
                result.append(entry)
            else:
                result.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            if isinstance(msg.content, list):
                # Tool calls — Ollama represents these differently
                tool_calls = [
                    {
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        }
                    }
                    for tc in msg.content
                    if isinstance(tc, ToolCall)
                ]
                result.append({"role": "assistant", "tool_calls": tool_calls})
            else:
                result.append({"role": "assistant", "content": msg.content or ""})
        elif msg.role == "tool_result":
            result.append({"role": "tool", "content": msg.content})
    return result


def _tool_def_to_ollama(t: ToolDefinition) -> dict:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }
