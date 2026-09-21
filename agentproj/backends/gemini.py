from __future__ import annotations

import uuid
from typing import Any

from google import genai
from google.genai import types as gtypes

from .base import LLMBackend, LLMMessage, ToolCall, ToolDefinition


class GeminiBackend(LLMBackend):
    def __init__(self, model_name: str, api_key: str) -> None:
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. "
                "Get a free key at https://aistudio.google.com"
            )
        self._model_name = model_name
        self._client = genai.Client(api_key=api_key)

    @property
    def name(self) -> str:
        return f"gemini:{self._model_name}"

    @property
    def supports_native_tools(self) -> bool:
        return True

    async def get_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> tuple[str | None, list[ToolCall], list[Any]]:
        contents = _to_gemini_contents(messages)
        gemini_tools = _build_gemini_tools(tools)

        response = await self._client.aio.models.generate_content(
            model=self._model_name,
            contents=contents,
            config=gtypes.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=gemini_tools,
                automatic_function_calling=gtypes.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )

        # Check for function calls and preserve all parts (including thoughts)
        tool_calls: list[ToolCall] = []
        all_parts = []
        
        candidate = response.candidates[0] if response.candidates else None
        if candidate and candidate.content and candidate.content.parts:
            all_parts = candidate.content.parts
            for part in all_parts:
                if part.function_call and part.function_call.name:
                    fc = part.function_call
                    tool_calls.append(ToolCall(
                        id=str(uuid.uuid4()),
                        name=fc.name,
                        arguments=dict(fc.args) if fc.args else {},
                    ))

        if tool_calls:
            # Return the parts list as the content so it can be sent back in history
            return None, tool_calls, all_parts

        text = response.text.strip() if response.text else None
        return text, [], []

    def format_tool_result(self, call_id: str, name: str, result: str) -> LLMMessage:
        return LLMMessage(
            role="tool_result",
            content=result,
            tool_call_id=name, # Gemini uses function name as the ID in response
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_gemini_tools(tools: list[ToolDefinition]) -> list[gtypes.Tool]:
    declarations = []
    for t in tools:
        declarations.append(
            gtypes.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters=t.parameters,
            )
        )
    return [gtypes.Tool(function_declarations=declarations)]


def _to_gemini_contents(messages: list[LLMMessage]) -> list[gtypes.Content]:
    """Convert LLMMessage list to Gemini Content objects."""
    contents = []
    for msg in messages:
        if msg.role == "user":
            if isinstance(msg.content, list):
                parts = []
                for item in msg.content:
                    if isinstance(item, dict):
                        if item.get("type") == "image":
                            parts.append(
                                gtypes.Part.from_bytes(
                                    data=item["data"],
                                    mime_type=item.get("mime_type", "image/jpeg"),
                                )
                            )
                        elif item.get("type") == "text":
                            parts.append(gtypes.Part(text=item["text"]))
                contents.append(gtypes.Content(role="user", parts=parts))
            else:
                contents.append(
                    gtypes.Content(role="user", parts=[gtypes.Part(text=str(msg.content))])
                )
        elif msg.role == "assistant":
            if isinstance(msg.content, list):
                # If these are raw Parts (from GeminiBackend.get_response), use them directly.
                # This preserves thought signatures.
                if msg.content and isinstance(msg.content[0], gtypes.Part):
                    contents.append(gtypes.Content(role="model", parts=msg.content))
                else:
                    # Legacy fallback (other backends or simple tool calls)
                    parts = [
                        gtypes.Part(
                            function_call=gtypes.FunctionCall(
                                name=tc.name, args=tc.arguments
                            )
                        )
                        for tc in msg.content
                        if isinstance(tc, ToolCall)
                    ]
                    contents.append(gtypes.Content(role="model", parts=parts))
            else:
                contents.append(
                    gtypes.Content(role="model", parts=[gtypes.Part(text=str(msg.content))])
                )
        elif msg.role == "tool_result":
            contents.append(
                gtypes.Content(
                    role="user",
                    parts=[
                        gtypes.Part(
                            function_response=gtypes.FunctionResponse(
                                name=msg.tool_call_id or "tool",
                                response={"result": msg.content},
                            )
                        )
                    ],
                )
            )
    return contents
