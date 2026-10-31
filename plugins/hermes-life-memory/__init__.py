"""Standalone local person service; deliberately registers no MemoryProvider."""
from __future__ import annotations

from pathlib import Path
from weakref import WeakKeyDictionary, ref

from .service import LifeMemoryService, profile, settings


def register(ctx):
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home()).resolve()
    services = WeakKeyDictionary()
    closed = False

    def close():
        nonlocal closed
        closed = True
        for service in list(services.values()):
            service.close()
        services.clear()
    ctx.on_unload(close)

    def reader():
        return ctx.get_config('memory', {})

    def factory(native, adapter):
        del native
        if closed:
            return None
        with profile(home):
            config = settings(reader())
            if not config['enabled']:
                return None
            old = services.get(adapter)
            if old is not None:
                old.validate()
                return old
            if getattr(adapter, 'social_memory_contract', None) != 1:
                raise ValueError('channel social memory contract 1 required')
            port = adapter.participation_port()
            if Path(port.home).resolve() != home or not set(config['groups']).issubset(port.groups):
                raise PermissionError('memory groups outside channel authorization')
            from plugins.plugin_storage import plugin_data_dir
            service = LifeMemoryService(home, plugin_data_dir('hermes-life-memory'), config, reader,
                channel_domain=port.domain_id, channel_revision=port.policy_revision)
            key = (ctx.manifest.name, factory.__qualname__)
            try:
                adapter.install_social_memory(service, registration_key=key)
            except BaseException:
                service.close()
                raise
            services[adapter] = service
            target = ref(adapter)
            def detach():
                service.close()
                current = target()
                if current is not None:
                    current.uninstall_social_memory(service)
            ctx.on_unload(detach)
            return service
    ctx.register_platform_handler('napcat', factory)
