"""Unified live status engine (researched pattern).

Placeholder immediately -> throttled identical-suppressed edits with live
activity -> finalize. Best-effort chain (status never breaks the answer),
typing keepalive alongside where possible, emptyNotice (never silent).
Duck-typed post/edit/remove/finalize so chat messages AND guest/inline
edits share one implementation. No deps beyond asyncio (both venvs safe).
"""
import asyncio
import logging
import time

logger: logging.Logger = logging.getLogger(__name__)

POLL_S = 1.8
MAX_EDITS = 25


def brief_net_error(log, what: str, e: Exception) -> None:
    """Storm-quiet logging for send paths.

    Network noise (timeouts, drops, resets) -> one warning line.
    Real bugs -> full traceback. Never raises.
    """
    try:
        from telegram.error import NetworkError as _NE
        from telegram.error import RetryAfter as _RA
        from telegram.error import Forbidden as _FB
        import httpx as _hx
        if isinstance(e, (_NE, _RA, _FB, ConnectionError, TimeoutError,
                          _hx.ConnectError, _hx.TimeoutException,
                          _hx.NetworkError, _hx.RemoteProtocolError)):
            log.warning("%s net issue: %s", what, type(e).__name__)
            return
    except Exception:
        pass
    try:
        log.exception("%s failed: %s", what, e)
    except Exception:
        pass


async def run_staged(post, edit, remove, finalize, run, sink,
                     thinking="`thinking...`", cancel_event=None,
                     parse_mode="MarkdownV2"):
    """Run async `run()` (fills shared `sink` list with stage notes).

    post(text)->handle, edit(handle,text), remove(handle), finalize(text).
    cancel_event (asyncio.Event, optional): when set, aborts waiting and
    raises CancelledError (caller cleans up + notifies). Status failures
    never propagate (except cancellation).
    """
    handle = None
    try:
        handle = await post(thinking, parse_mode)
    except Exception as e:
        logger.debug("status post failed: %s", e)
        try:
            handle = await post(thinking, None)
        except Exception:
            pass
    stop = False

    async def _poll():
        last = ""
        n = 0
        while not stop:
            await asyncio.sleep(POLL_S)
            try:
                if sink and sink[-1] != last and n < MAX_EDITS:
                    last = sink[-1]
                    n += 1
                    if handle is not None:
                        try:
                            await edit(handle, last[:200], parse_mode)
                        except Exception:
                            try:
                                await edit(handle, last[:200], None)
                            except Exception as e:
                                logger.warning("status edit failed: %s", e)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    pt = asyncio.ensure_future(_poll())
    rt = asyncio.ensure_future(run())
    try:
        if cancel_event is None:
            return await rt
        while not rt.done():
            if cancel_event.is_set():
                try:
                    rt.cancel()
                except Exception:
                    pass
                raise asyncio.CancelledError()
            await asyncio.sleep(0.3)
        return await rt
    finally:
        stop = True
        try:
            pt.cancel()
        except Exception:
            pass
        try:
            if not rt.done():
                rt.cancel()
        except Exception:
            pass
        if handle is not None:
            try:
                await remove(handle)
            except Exception:
                pass


async def typing_keepalive(send_action, stop_event, interval: float = 4.0):
    """Re-send typing until stop_event set. Best-effort, never raises out."""
    try:
        while not stop_event.is_set():
            try:
                await send_action()
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        pass
