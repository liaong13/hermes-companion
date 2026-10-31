"""Profile-owned model facade and thread-safe plugin unload boundary."""
from __future__ import annotations

import asyncio
from contextvars import Context
import inspect
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SocialBinding:
    def __init__(self, ctx):
        from hermes_constants import get_hermes_home
        self.home = Path(get_hermes_home()).resolve()
        self.ctx, self.active = ctx, True
        self.controller = None
        self.loop = None
        self.shutdown_task = None
        ctx.on_unload(self.close)

    def attach(self, controller):
        self.validate()
        if self.controller is not None and self.controller is not controller:
            raise ValueError('binding already has a controller')
        self.controller = controller
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.loop is not None and self.loop is not loop:
            raise ValueError('social controller cannot move event loops')
        self.loop = loop

    def validate(self):
        from hermes_constants import get_hermes_home
        if not self.active or Path(get_hermes_home()).resolve() != self.home:
            raise ValueError('social profile scope expired')

    def facade(self):
        self.validate()
        llm = self.ctx.llm
        if (not callable(getattr(llm, 'acomplete', None))
                or 'messages' not in inspect.signature(llm.acomplete).parameters):
            raise ValueError('host model facade unavailable')
        return llm

    def spawn(self, coro, *, name):
        self.validate()
        started = False
        async def scoped():
            nonlocal started
            started = True
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
            token = set_hermes_home_override(self.home)
            try:
                if not self.active:
                    raise asyncio.CancelledError
                self.validate()
                return await coro
            finally:
                coro.close()
                reset_hermes_home_override(token)
        wrapper = scoped()
        try:
            task = Context().run(self.ctx.spawn_task, wrapper, name=name)
            task.add_done_callback(lambda _: None if started else coro.close())
            return task
        except BaseException:
            wrapper.close()
            raise

    def close(self):
        # Must happen synchronously, even when unload is called from a thread
        # without a running loop. Cancellation alone cannot stop late results.
        if not self.active:
            return
        self.active = False
        if self.controller is None:
            return
        controller = self.controller
        controller.deactivate()
        controller.port.detach()
        loop = self.loop
        if loop is None or loop.is_closed():
            return

        def schedule():
            if self.shutdown_task is None:
                self.shutdown_task = loop.create_task(controller.stop(), context=Context())
                self.shutdown_task.add_done_callback(finished)

        def finished(task):
            if not task.cancelled() and task.exception() is not None:
                logger.warning('Social cleanup failed (%s)', type(task.exception()).__name__)

        loop.call_soon_threadsafe(schedule)
