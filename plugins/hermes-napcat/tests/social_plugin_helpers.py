"""Test-only bridge to the independent social plugin's public factory.

No test imports its controller/policy implementation or gives it an adapter.
Only the transport-created participation port crosses the plugin boundary.
"""
import importlib.util
from pathlib import Path
import sys

from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest

SOCIAL_ROOT = Path(__file__).resolve().parents[2] / 'hermes-social'


def social_plugin():
    if 'hermes_social' not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            'hermes_social', SOCIAL_ROOT / '__init__.py',
            submodule_search_locations=[str(SOCIAL_ROOT)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
    return sys.modules['hermes_social']


def raw_policy(*, shared=False, **overrides):
    policy = dict(enabled=True, targets=['dm:10001', 'group:20001'], quiet_hours=[0, 0])
    if not shared:
        # Deliberate test-only override; production shares port.groups.
        policy['participation_groups'] = ['20001']
    policy.update(overrides)
    return policy


def plugin_context(*, manager=None, llm=None):
    manager = manager if manager is not None else PluginManager()
    ctx = PluginContext(PluginManifest(
        name='hermes-social', kind='standalone', source='user', path=str(SOCIAL_ROOT)), manager)
    if llm is not None:
        ctx._llm = llm
    return ctx


def new_controller(adapter, policy=None, *, llm=None, ctx=None):
    ctx = ctx if ctx is not None else plugin_context(llm=llm)
    return social_plugin().create_controller(
        ctx, adapter.participation_port(), raw_policy() if policy is None else policy)


def install_social(adapter, policy=None, *, llm=None, ctx=None):
    controller = new_controller(adapter, policy, llm=llm, ctx=ctx)
    adapter.install_participation(controller)
    return controller


def optional_valid(adapter, source):
    """Inspect the transport proof, never a social-controller-owned ledger."""
    return adapter._optional_sources.valid(source)


def release_optional(adapter, source):
    adapter._optional_sources.release(source)
