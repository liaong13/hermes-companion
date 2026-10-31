"""Internet life runtime's conversation capability; stable hermes-social plugin ID."""
from __future__ import annotations

from pathlib import Path
from threading import RLock
from weakref import WeakKeyDictionary, WeakValueDictionary

from .binding import SocialBinding
from .context import ContextProvider, ContextRecord, ContextScope, ContextScopeError
from .controller import SocialController
from .migration import migrate_napcat_state_v1
from .policy import SocialPolicy

__all__ = ['register', 'create_controller', 'SocialBinding', 'SocialPolicy', 'SocialController',
           'ContextProvider', 'ContextRecord', 'ContextScope', 'ContextScopeError']

_controllers = WeakValueDictionary()
_lock = RLock()


def create_controller(ctx, port, config):
    """Build once per exact port lifetime, reusable by host integration tests.

    config is the raw policy at settings.channels.napcat, not a copied channel
    whitelist. The factory owns activation after install_participation().
    """
    with _lock:
        existing = _controllers.get(id(port))
        if existing is not None and existing.port is port and existing.binding.active:
            existing.binding.validate()
            if existing.binding.ctx is not ctx:
                raise ValueError('participation port already attached to another plugin lifetime')
            return existing
        policy = SocialPolicy(config, owner_id=port.owner_id, groups=port.groups,
                              dm_target_allowed=getattr(port, 'dm_target_allowed', None))
        binding = SocialBinding(ctx)
        try:
            controller = SocialController(port, policy, binding)
            binding.attach(controller)
        except BaseException:
            binding.close()
            raise
        _controllers[id(port)] = controller
        return controller


def register(ctx):
    closed = False
    attached = WeakKeyDictionary()
    def close_registration():
        nonlocal closed
        closed = True
        attached.clear()
    ctx.on_unload(close_registration)

    def factory(native, adapter):
        del native
        if closed:
            return None
        # A connector may vend a fresh port on every call. Keep only a weak
        # factory-level adapter key; the controller itself retains just its port.
        existing = attached.get(adapter)
        if existing is not None and existing.binding.active:
            existing.binding.validate()
            adapter.install_participation(existing)
            existing.start()
            return existing
        channels = ctx.get_config('channels', {})
        if not isinstance(channels, dict):
            raise ValueError('social channels must be a mapping')
        port = adapter.participation_port()
        # SDK records (factory, ctx.manifest.name), not necessarily plugin_id.
        port.set_registration_key((ctx.manifest.name, factory.__qualname__))
        migrate = ctx.get_config('migrations.napcat_state_v1', False)
        if type(migrate) is not bool:
            raise ValueError('napcat_state_v1 migration opt-in must be boolean')
        if migrate:
            migrate_napcat_state_v1(port)
        controller = create_controller(ctx, port, channels.get('napcat', {}))
        adapter.install_participation(controller)
        attached[adapter] = controller
        controller.start()
        return controller

    ctx.register_platform_handler('napcat', factory)
    ctx.register_skill('social-participation', Path(__file__).parent / 'skills' / 'social-participation' / 'SKILL.md')
