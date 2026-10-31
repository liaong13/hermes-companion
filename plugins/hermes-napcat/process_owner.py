"""Keep subprocess ownership through repeated caller cancellation.

No fire-and-forget cleanup: callers retain their slot until close() has joined
the same owned finalizer, then cancellation continues to propagate.
"""
from __future__ import annotations

import asyncio
import os
import signal


class ProcessCleanupError(RuntimeError):
    """Safe failure code: subprocess shutdown could not be confirmed."""


class ProcessOwner:
    CLEANUP_TIMEOUT = 5.0
    FORCE_TIMEOUT = 2.0

    def __init__(self, command, *, process_group=False):
        self.process_group = process_group
        self.operation = self.finalizer = None
        # Only local stdlib process creation occurs here, not model/network work.
        # The handle must be obtained even if recall/disconnect cancels its caller.
        self.launch = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={}, start_new_session=process_group))

    async def start(self):
        return await asyncio.shield(self.launch)

    def track(self, operation):
        if self.operation is not None:
            raise RuntimeError('process operation already bound')
        self.operation = asyncio.create_task(operation)
        return self.operation

    async def close(self):
        if self.finalizer is None:
            self.finalizer = asyncio.create_task(self._finish())
        cancellation = None
        while True:
            try:
                await asyncio.shield(self.finalizer)
                break
            except asyncio.CancelledError as exc:
                if self.finalizer.cancelled():
                    raise ProcessCleanupError('process_finalizer_cancelled') from None
                cancellation = cancellation or exc
                # Keep joining the same finalizer. Never await launch/finalizer
                # naked here and never cancel it on a later caller.cancel().
        if cancellation is not None:
            raise cancellation

    def _kill(self, process):
        if process.returncode is not None:
            return
        try:
            if self.process_group:
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        except ProcessLookupError:
            pass

    async def _finish(self):
        try:
            process = await self.launch
        except OSError:
            return  # failed creation has no child; start() reports the original error
        self._kill(process)
        if self.operation is not None and not self.operation.done():
            self.operation.cancel()
        # communicate(None) does not close stdin on supported Python versions.
        # A cancelled launch may leave a namespace helper/child holding stdout
        # while waiting for input; send EOF before joining its pipe readers.
        process.stdin.close()
        try:
            async with asyncio.timeout(self.CLEANUP_TIMEOUT):
                if self.operation is not None:
                    await asyncio.gather(self.operation, return_exceptions=True)
                await process.communicate()
        except (TimeoutError, OSError, RuntimeError):
            # Process has no public close() for interrupted pipe transports.
            # This contained stdlib fallback closes actual FDs, not just reader
            # flags; it is also exercised by the cleanup-deadline regression.
            self._kill(process)
            process._transport.close()
            try:
                async with asyncio.timeout(self.FORCE_TIMEOUT):
                    if self.operation is not None:
                        await asyncio.gather(self.operation, return_exceptions=True)
                    await process.wait()
                    await process.stdout.read()
            except (TimeoutError, OSError, RuntimeError):
                raise ProcessCleanupError('process_shutdown_unconfirmed') from None
        if (process.returncode is None or not process.stdin.is_closing()
                or not process.stdout.at_eof()):
            raise ProcessCleanupError('process_shutdown_unconfirmed')
