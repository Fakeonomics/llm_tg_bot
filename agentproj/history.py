from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class ChatHistory:
    capture_buffer: list[dict] = field(default_factory=list)
    capture_active: bool = False


class HistoryManager:
    def __init__(self) -> None:
        self._chats: dict[int, ChatHistory] = defaultdict(ChatHistory)

    def record_message(self, chat_id: int, username: str, text: str) -> None:
        """Called for every incoming text message. Only buffers when capture is active."""
        state = self._chats[chat_id]
        if state.capture_active:
            state.capture_buffer.append({"username": username, "text": text})

    def record_photo(
        self,
        chat_id: int,
        username: str,
        caption: str | None,
        image_bytes: bytes | None = None,
    ) -> None:
        """Record a photo message into the buffer when capture is active."""
        state = self._chats[chat_id]
        if state.capture_active:
            text = f"[photo{f': {caption}' if caption else ''}]"
            entry: dict = {"username": username, "text": text}
            if image_bytes:
                entry["image_bytes"] = image_bytes
            state.capture_buffer.append(entry)

    def get_images_from_buffer(self, chat_id: int) -> list[bytes]:
        """Return all stored image bytes from the capture buffer, in order."""
        return [
            entry["image_bytes"]
            for entry in self._chats[chat_id].capture_buffer
            if "image_bytes" in entry
        ]

    def begin_capture(self, chat_id: int) -> None:
        self._chats[chat_id].capture_active = True

    def end_capture(self, chat_id: int) -> None:
        self._chats[chat_id].capture_active = False

    def clear_capture(self, chat_id: int) -> None:
        state = self._chats[chat_id]
        state.capture_buffer.clear()
        state.capture_active = False

    def get_context_for_agent(self, chat_id: int) -> list[dict] | None:
        """
        Returns the capture buffer if non-empty, else None.
        Callers should return an error to the user when None is returned.
        """
        buf = self._chats[chat_id].capture_buffer
        return list(buf) if buf else None

    def get_capture_buffer(self, chat_id: int) -> list[dict]:
        """Used by the get_context tool so the model can inspect the buffer."""
        return list(self._chats[chat_id].capture_buffer)

    def get_status(self, chat_id: int) -> dict:
        state = self._chats[chat_id]
        return {
            "capture_active": state.capture_active,
            "buffer_count": len(state.capture_buffer),
        }

    @staticmethod
    def format_for_llm(messages: list[dict]) -> str:
        """Format a list of message dicts as a readable transcript."""
        if not messages:
            return "(empty)"
        return "\n".join(f"{m['username']}: {m['text']}" for m in messages)
