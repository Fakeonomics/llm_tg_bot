from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMMessage:
    role: str  # "user" | "assistant" | "tool_result"
    content: str | list[Any]
    # For tool_result messages, store the call_id for backends that need it
    tool_call_id: str | None = field(default=None)


class LLMBackend(ABC):
    """
    Abstract LLM backend. Backends implement the single-step get_response()
    call; the agentic loop in agent.py drives repeated calls until tool_calls
    is empty and a final text response is returned.
    """

    @abstractmethod
    async def get_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> tuple[str | None, list[ToolCall], list[Any]]:
        """
        Make one LLM call.

        Returns (final_text, tool_calls, raw_parts):
        - If tool_calls is non-empty, caller executes them and calls again.
        - If final_text is set and tool_calls is empty, loop terminates.
        - raw_parts (optional) are raw model response parts (for thought preservation).
        """
        ...

    @abstractmethod
    def format_tool_result(self, call_id: str, name: str, result: str) -> LLMMessage:
        """
        Format a tool execution result as an LLMMessage to append to the
        conversation. Each backend has a different wire format for this.
        """
        ...

    @property
    @abstractmethod
    def supports_native_tools(self) -> bool:
        """
        True if the model supports structured tool/function calling natively.
        False triggers ReAct text-protocol fallback in OllamaBackend.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of the active model, for /status display."""
        ...
