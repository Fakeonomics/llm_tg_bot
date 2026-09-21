from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import config
from backends.base import LLMMessage, ToolCall
from tools import ALL_TOOLS, run_python, web_search, get_context_tool

if TYPE_CHECKING:
    from history import HistoryManager

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a helpful AI assistant embedded in a Telegram group chat.
You have been tagged by a user and should respond to their request concisely using the provided context.

You have access to two tools:
- run_python: Execute Python code for ANY calculation, math, data processing, or number work.
  ALWAYS use this instead of computing numbers yourself — never guess.
- web_search: Search the web for current information, facts, exchange rates, prices, news.

Guidelines for Accuracy:
1. TRIPLE-CHECK the conversation context. Ensure you extract the correct numbers and associate them with the right events or people.
2. For any arithmetic, summary, or numeric result, you MUST use run_python. No exceptions.
3. If the context is ambiguous or missing information, state that clearly rather than hallucinating details.
4. Be concise. Format currency and numbers clearly (e.g. $1,234.50).
5. Use HTML tags for formatting: <b>bold</b>, <i>italic</i>, <code>code</code>. Do not use markdown syntax like ** or *.
"""

_MAX_ITERATIONS = 10


async def get_response(
    user_request: str,
    context_text: str,
    chat_id: int,
    history_manager: "HistoryManager",
    images: list[bytes] | None = None,
) -> str:
    """
    Run the agentic loop for a user request.

    Injects the captured conversation context into the first user message,
    then loops until the backend returns a final text response or the
    iteration limit is reached. If images is provided they are included
    in the first user message for vision-capable backends.
    """
    backend = config.get_backend()
    backend_switched = False

    # Define tools excluding get_context (context is already injected)
    active_tools = [t for t in ALL_TOOLS if t.name != "get_context"]

    text_content = f"[Conversation context]\n{context_text}\n\n[Request]\n{user_request}"
    if images:
        user_content: str | list = [
            {"type": "image", "data": img, "mime_type": "image/jpeg"}
            for img in images
        ] + [{"type": "text", "text": text_content}]
    else:
        user_content = text_content
    messages: list[LLMMessage] = [LLMMessage(role="user", content=user_content)]

    for iteration in range(_MAX_ITERATIONS):
        logger.debug("Agent iteration %d/%d", iteration + 1, _MAX_ITERATIONS)

        try:
            # Backend returns: text, tool_calls, and raw_parts (for Gemini thought preservation)
            res = await backend.get_response(
                messages, active_tools, SYSTEM_PROMPT
            )
            # Res is (text, list[ToolCall]) or (text, list[ToolCall], list[any])
            text = res[0]
            tool_calls = res[1]
            raw_parts = res[2] if len(res) > 2 else None

        except Exception as e:
            logger.error("Backend error: %s", e, exc_info=True)
            if config.using_ollama() and not backend_switched:
                fallback_name = config.set_gemini_fallback_backend()
                if fallback_name:
                    backend = config.get_backend()
                    backend_switched = True
                    logger.warning(
                        "Ollama backend failed; falling back to %s", fallback_name
                    )
                    continue
            return f"Sorry, the LLM backend returned an error from {backend.name}: {e}"

        if not tool_calls:
            answer = text or "(no response)"
            if backend_switched:
                return f"<i>Model: {backend.name} (Gemini fallback after Ollama failed)</i>\n\n{answer}"
            return f"<i>Model: {backend.name}</i>\n\n{answer}"

        # Append the assistant turn. 
        # If we have raw_parts (Gemini), use them to preserve thoughts/signatures.
        if raw_parts:
            messages.append(LLMMessage(role="assistant", content=raw_parts))
        else:
            messages.append(LLMMessage(role="assistant", content=tool_calls))

        # Execute tool calls sequentially and collect results
        for call in tool_calls:
            result = await _dispatch_tool(call, chat_id, history_manager)
            logger.debug("Tool %s → %s", call.name, result[:200])
            # Pass both ID and name to backend formatter
            messages.append(backend.format_tool_result(call.id, call.name, result))

    return f"<i>Model: {backend.name}</i>\n\nI ran into too many steps trying to answer that. Please try rephrasing."


async def _dispatch_tool(
    call: ToolCall,
    chat_id: int,
    history_manager: "HistoryManager",
) -> str:
    try:
        if call.name == "run_python":
            code = call.arguments.get("code", "")
            if not code:
                return "Error: no code provided."
            return await run_python(code)

        elif call.name == "web_search":
            query = call.arguments.get("query", "")
            if not query:
                return "Error: no query provided."
            max_results = int(call.arguments.get("max_results", 5))
            return await web_search(query, max_results)

        elif call.name == "get_context":
            return await get_context_tool(chat_id, history_manager)

        else:
            return f"Error: unknown tool '{call.name}'."

    except Exception as e:
        logger.error("Tool dispatch error for %s: %s", call.name, e, exc_info=True)
        return f"Error executing {call.name}: {e}"
