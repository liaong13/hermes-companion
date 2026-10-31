"""Original bounded OneBot 11 forward-WebSocket transport (no HTTP API)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import random
import uuid

import aiohttp


class OneBotError(RuntimeError):
    """Safe diagnostic: never includes server payloads, URLs or credentials."""


class OneBotClient:
    def __init__(self, url, token, self_id, on_event, *, on_disconnect=None, queue_size=64,
                 timeout=10.0, reconnect_min=1.0, reconnect_max=30.0):
        if not token or any(c in token for c in '\r\n'):
            raise ValueError('NAPCAT_ACCESS_TOKEN must be nonempty and header-safe')
        self.url, self._token, self.self_id = url, token, self_id
        self.on_event, self.timeout = on_event, timeout
        self.on_disconnect = on_disconnect
        self.reconnect_min, self.reconnect_max = reconnect_min, reconnect_max
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.pending = {}
        self.ready = asyncio.Event()
        self._session = self._ws = self._runner = self._worker = None
        self._closing = False
        self.dropped_events = 0
        self.handler_errors = 0

    async def start(self):
        if self._runner:
            return
        self._closing = False
        trace = aiohttp.TraceConfig()
        async def reject_redirect(session, context, params):
            raise OneBotError('OneBot redirects are forbidden')
        trace.on_request_redirect.append(reject_redirect)
        self._session = aiohttp.ClientSession(
            trust_env=False, trace_configs=[trace],
            timeout=aiohttp.ClientTimeout(total=self.timeout))
        self._worker = asyncio.create_task(self._consume())
        self._runner = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self.ready.wait(), self.timeout)
        except BaseException:
            await self.close()
            raise

    async def _consume(self):
        while True:
            event = await self.queue.get()
            try:
                await self.on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.handler_errors += 1
            finally:
                self.queue.task_done()

    async def call(self, action, params=None, *, _handshake=False):
        if action in {'get_record', 'get_image', 'get_file'}:
            # These upstream responses can include entire base64 files. Keep the
            # 1MiB WebSocket frame cap; use reviewed URLs/shared exports instead.
            raise OneBotError('inline-base64 media actions are disabled')
        ws = self._ws
        if not ws or ws.closed or (not _handshake and not self.ready.is_set()):
            raise OneBotError('OneBot disconnected')
        if len(self.pending) >= 64:
            raise OneBotError('OneBot action capacity exhausted')
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await ws.send_json({'action': action, 'params': params or {}, 'echo': echo})
            result = await asyncio.wait_for(future, self.timeout)
            if result.get('status') != 'ok' or result.get('retcode') != 0:
                raise OneBotError('OneBot action rejected')
            data = result.get('data')
            expected_type = list if action == 'get_friend_list' else dict
            if not isinstance(data, expected_type):
                raise OneBotError('OneBot malformed action response')
            return data
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            raise OneBotError('OneBot action failed or timed out; delivery may be unknown') from exc
        finally:
            self.pending.pop(echo, None)

    async def _read(self, ws):
        async for frame in ws:
            if frame.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(frame.data)
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            if 'echo' in payload:
                echo = payload['echo']
                future = self.pending.get(echo) if isinstance(echo, str) else None
                if future is not None and not future.done():
                    future.set_result(payload)
                continue
            if not self.ready.is_set() or str(payload.get('self_id', '')) != self.self_id:
                continue
            try:
                self.queue.put_nowait(payload)
            except asyncio.QueueFull:
                self.dropped_events += 1  # drop newest, never block action responses

    def _fail_pending(self):
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(OneBotError('OneBot disconnected; delivery may be unknown'))

    async def _run(self):
        delay = self.reconnect_min
        while not self._closing:
            reader = None
            try:
                self._ws = await self._session.ws_connect(
                    self.url, headers={'Authorization': 'Bearer ' + self._token},
                    heartbeat=20, max_msg_size=1024 * 1024,
                    timeout=aiohttp.ClientWSTimeout(ws_close=3))
                reader = asyncio.create_task(self._read(self._ws))
                login = await self.call('get_login_info', _handshake=True)
                if not isinstance(login, dict) or str(login.get('user_id', '')) != self.self_id:
                    raise OneBotError('OneBot login self_id mismatch')
                self.ready.set()
                delay = self.reconnect_min
                await reader
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # never log exception strings containing handshake credentials
            finally:
                self.ready.clear()
                self._fail_pending()
                if reader:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await reader
                if self._ws:
                    await self._ws.close()
                    self._ws = None
                # Pending events belong to the old authenticated connection.
                # Do not let them create provenance after its index is cleared.
                while not self.queue.empty():
                    self.queue.get_nowait()
                    self.queue.task_done()
                    self.dropped_events += 1
                if self.on_disconnect is not None:
                    self.on_disconnect()
            if not self._closing:
                await asyncio.sleep(min(self.reconnect_max, delay * random.uniform(1, 1.2)))
                delay = min(self.reconnect_max, delay * 2)

    async def close(self):
        self._closing = True
        self.ready.clear()
        for task in (self._runner, self._worker):
            if task:
                task.cancel()
        for task in (self._runner, self._worker):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._fail_pending()
        if self._session:
            await self._session.close()
        self._runner = self._worker = self._session = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
