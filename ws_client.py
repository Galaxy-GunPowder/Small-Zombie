"""ws_client — the base CDP transport for Small_Zombie.

One websocket per connection (browser-level or page-level), driven by a SINGLE background
reader task that ALWAYS drains the socket. This is the crux: Chrome's DevTools endpoint
drops any client that is too slow to read, and with Network/DOM/CSS events enabled a real
page emits thousands of messages. The old design only read the socket *inside* a
`send_cmd(get_res=True)` loop, so during any gap (sleeps, polling, idle) the socket went
undrained, Chrome's send buffer overflowed, and the connection died mid-navigation
(`ConnectionClosedError: no close frame received or sent`). Tiny fixtures never triggered
it; real ATS pages always did.

Design:
  * `_reader()` loops over the socket forever, routing each frame:
      - has "id"      -> a command RESPONSE: resolve the waiting future (O(1) by id).
      - has "method"  -> an EVENT: fan out to registered listeners, then enqueue for recv().
  * `send_cmd(get_res=True)` registers a future keyed by the command id, sends, and awaits
    that future with a timeout — it never touches the socket's read side.
  * `recv()` returns the next EVENT from a bounded queue (drop-oldest so a never-draining
    caller can't leak memory); `wait_for_event()` waits for a specific CDP method.
  * teardown cancels the reader and fails every pending call with a clear error.

Public surface preserved for main.py / driver.py: get_ws_id, send_cmd, connect, send,
recv, ws_close, __aexit__.
"""
from __future__ import annotations

import asyncio
import json

import websockets

from logger import LoggerMixin

# Bound the event backlog: the agent flow issues many commands and rarely calls recv(),
# so events would otherwise accumulate forever. Keep the most recent N, drop the oldest.
_EVENT_BACKLOG = 4096

# CDP domains enabled on every fresh connection. Page+Runtime+DOM are what the driver and
# browser.js actually use; Network/CSS/Storage stay on for existing features (cookies,
# scraping) and are now safe because the reader drains their events continuously.
_ENABLE_METHODS = [
    "Page.enable",
    "Runtime.enable",
    "DOM.enable",
    "Network.enable",
    "CSS.enable",
    "Storage.enable",
]


class CDPError(RuntimeError):
    """A CDP command came back with an `error` field, or the socket died under a call."""


class WebsocketClient(LoggerMixin):
    def __init__(self):
        self._ws_msg_id = 0
        self.ws = None
        self._ws_lock = asyncio.Lock()          # serializes id allocation
        self._send_lock = asyncio.Lock()        # serializes writes to the socket
        self._logged_ws_endpoint = None
        self.ws_logger = self.get_logger("SZ-WS")

        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_BACKLOG)
        self._listeners: dict[str, list] = {}
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    # ---- ids -------------------------------------------------------------------------
    async def get_ws_id(self) -> int:
        async with self._ws_lock:
            self._ws_msg_id += 1
            return self._ws_msg_id

    # ---- background reader (the fix) -------------------------------------------------
    async def _reader(self, ws) -> None:
        """Drain `ws` forever, routing responses to futures and events to the queue."""
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                mid = msg.get("id")
                if mid is not None:
                    fut = self._pending.pop(mid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    continue
                method = msg.get("method")
                if method:
                    for cb in self._listeners.get(method, ()):  # fan out (never fatal)
                        try:
                            cb(msg)
                        except Exception:  # noqa: BLE001
                            self.ws_logger.debug(f"listener for {method} raised", exc_info=True)
                    self._enqueue_event(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — socket closed / protocol error
            self._fail_pending(CDPError(f"CDP socket closed: {type(e).__name__}: {e}"))
        else:
            self._fail_pending(CDPError("CDP socket ended"))

    def _enqueue_event(self, msg: dict) -> None:
        if self._events.full():                 # drop-oldest so a non-draining caller can't leak
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._events.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    # ---- event listeners (base utility) ----------------------------------------------
    def on(self, method: str, callback) -> None:
        """Register `callback(msg)` for a CDP event method (e.g. 'Page.loadEventFired')."""
        self._listeners.setdefault(method, []).append(callback)

    def off(self, method: str, callback) -> None:
        try:
            self._listeners.get(method, []).remove(callback)
        except ValueError:
            pass

    async def wait_for_event(self, method: str, timeout: float = 10.0) -> dict | None:
        """Resolve when the next `method` event arrives (via a one-shot listener)."""
        loop = asyncio.get_event_loop()
        fut = loop.create_future()

        def _cb(msg):
            if not fut.done():
                fut.set_result(msg)

        self.on(method, _cb)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.off(method, _cb)

    # ---- commands --------------------------------------------------------------------
    async def send_cmd(self, method, params=None, get_res=False, timeout=30):
        if self.ws is None or self._closed:
            raise CDPError(f"CDP not connected (method={method})")
        cmd_id = await self.get_ws_id()
        message = json.dumps({"id": cmd_id, "method": method, "params": params or {}})

        fut = None
        if get_res:
            fut = asyncio.get_event_loop().create_future()
            self._pending[cmd_id] = fut

        try:
            async with self._send_lock:
                await self.ws.send(message)
        except Exception as e:  # noqa: BLE001 — write failed (socket gone)
            if fut is not None:
                self._pending.pop(cmd_id, None)
            raise CDPError(f"CDP send failed for {method}: {type(e).__name__}: {e}") from e

        if not get_res:
            return None

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise CDPError(f"CDP timeout after {timeout}s waiting for {method}") from None

    # ---- connection lifecycle --------------------------------------------------------
    async def connect(self, page_address=None):
        url_to_use = page_address or self._logged_ws_endpoint
        if url_to_use is None:
            self.ws_logger.error("ws url is none, failed connection.")
            return

        # Tear down any previous connection/reader so a reconnect starts clean.
        await self._teardown_reader()
        self._fail_pending(CDPError("reconnecting"))
        self._drain_events()
        self._closed = False

        self.ws = await websockets.connect(
            url_to_use,
            ping_interval=None,   # Chrome DevTools doesn't speak ws-ping; keepalive off
            ping_timeout=None,
            max_size=None,        # CDP frames (screenshots, big evaluate results) exceed 1 MiB
            max_queue=None,       # never backpressure the reader
        )
        self._logged_ws_endpoint = url_to_use
        self._reader_task = asyncio.create_task(self._reader(self.ws))

        for method in _ENABLE_METHODS:
            try:
                await self.send_cmd(method, get_res=True, timeout=10)
            except CDPError as e:
                self.ws_logger.debug(f"enable {method} skipped: {e}")
        self.ws_logger.debug(f"Connected to {url_to_use}")

    def _drain_events(self) -> None:
        while not self._events.empty():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _teardown_reader(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def send(self, msg):
        if self.ws is None or self._closed:
            raise CDPError("CDP not connected (send)")
        async with self._send_lock:
            await self.ws.send(msg)

    async def recv(self):
        """Next CDP EVENT (not command responses — those resolve their futures)."""
        return await self._events.get()

    async def ws_close(self):
        self._closed = True
        await self._teardown_reader()
        self._fail_pending(CDPError("connection closed"))
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.ws_close()
