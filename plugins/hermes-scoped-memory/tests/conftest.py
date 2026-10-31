"""All host imports are isolated from production HERMES_HOME before collection."""
import atexit
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

SANDBOX = tempfile.mkdtemp(prefix='scoped-memory-tests-', dir=os.environ['TMPDIR'])
os.environ['HERMES_HOME'] = SANDBOX
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('scoped_test_plugin', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
import sys
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


class FakeOwner:
    def __init__(self):
        self.calls = []
        self.fail = False
    def is_available(self):
        return True
    def initialize(self, **kwargs):
        self.calls.append(('initialize', kwargs))
    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.fail:
                raise RuntimeError('private exception details')
            if name == 'prefetch':
                return 'OWNER_ONLY memory'
            if name == 'handle_tool_call':
                return json.dumps({'owner': True})
        return call


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('memory:\n  memory_enabled: false\n  user_profile_enabled: false\nplatforms:\n  napcat:\n    extra:\n      owner_id: "10001"\n')
    (tmp_path / 'scoped-memory.json').write_text(json.dumps({'isolation_reviewed': True, 'max_injection_tokens': 768,
                                                         'owner_mem0_user_id': 'test-owner'}))
    (tmp_path / 'mem0.json').write_text(json.dumps({'mode': 'platform', 'user_id': 'test-owner', 'agent_id': 'hermes'}))
    from gateway import session_context as sc
    sc.reset_session_vars()
    yield tmp_path
    sc.reset_session_vars()


@pytest.fixture
def provider(home):
    owner = FakeOwner()
    p = plugin.ScopedMemoryProvider(owner_factory=lambda: owner)
    p.initialize('s1', hermes_home=str(home), platform='napcat', agent_context='primary')
    p.fake_owner = owner
    yield p
    p.shutdown()


from contextlib import contextmanager

@contextmanager
def context(user='20002', room=None, sid='s1', kind=None, platform='napcat', thread=''):
    from gateway import session_context as sc
    # Properly restore tokens ourselves: clear_session_vars intentionally is not nestable.
    tokens = sc.set_session_vars(platform=platform, user_id=user, chat_id=room or 'dm:'+user,
                                chat_type=kind or ('group' if room else 'dm'), session_id=sid,
                                message_id='m1', thread_id=thread)
    try:
        yield
    finally:
        for token in reversed(tokens):
            token.var.reset(token)


def tool(p, action, **kwargs):
    return json.loads(p.handle_tool_call('scoped_memory', dict(action=action, **kwargs)))
