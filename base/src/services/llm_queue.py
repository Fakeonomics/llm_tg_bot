"""In-RAM FIFO queue for LLM calls. Protects a strained local 35B model.

Serializes completions (1 at a time by default), bounds the waiting line,
rejects with BusyError when overloaded instead of melting the GPU/CPU.
Temporary by design: RAM only, no disk, state dies with the process.
Stdlib only (asyncio) — zero weight.
"""
import asyncio
import logging
import time

logger: logging.Logger = logging.getLogger(__name__)


class BusyError(RuntimeError):
    """Raised when the LLM queue is full; caller should ask to retry later."""


BUSY_RU = ("Модель сейчас занята (очередь переполнена). "
           "Подожди немного и повтори.")


class LLMQueue:
    def __init__(self, max_concurrent: int = 1, max_waiters: int = 6,
                 wait_timeout: int = 300):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._waiting = 0
        self._max_waiters = max_waiters
        self._wait_timeout = wait_timeout
        self._done = 0

    def depth(self) -> int:
        return self._waiting

    async def run(self, fn, *args, **kwargs):
        if self._waiting >= self._max_waiters:
            raise BusyError(f"queue full ({self._waiting} waiting)")
        self._waiting += 1
        t0 = time.monotonic()
        try:
            async with self._sem:
                logger.debug("llm slot acquired after %.1fs",
                             time.monotonic() - t0)
                return await asyncio.wait_for(
                    fn(*args, **kwargs), timeout=self._wait_timeout)
        finally:
            self._waiting -= 1
            self._done += 1


QUEUE = LLMQueue()
