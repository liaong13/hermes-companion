"""Real Hermes discovery/MemoryManager + real installed Mem0 class, offline backend."""
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys

import pytest

from conftest import context, ROOT


class OfflineMem0Backend:
    def __init__(self):
        self.calls = []
        self.data = {'owner-key': {'id': 'owner-key', 'memory': 'OWNER_SECRET phoenix', 'user_id': 'test-owner'}}
    def search(self, query, **kwargs):
        self.calls.append(('search', query, kwargs))
        return list(self.data.values())
    def add(self, messages, **kwargs):
        self.calls.append(('add', messages, kwargs))
        return {'event_id': 'offline-test'}
    def get(self, ident):
        self.calls.append(('get', ident))
        return self.data.get(ident)
    def update(self, ident, text):
        self.calls.append(('update', ident, text))
        self.data[ident]['memory'] = text
        return {'id': ident}
    def delete(self, ident):
        self.calls.append(('delete', ident))
        self.data.pop(ident, None)
        return {'id': ident}
    def close(self):
        self.calls.append(('close',))


def wait_manager(manager):
    # The public shutdown drains too, but this barrier keeps tools usable for read-back.
    with manager._sync_executor_lock:
        pending = list(manager._background_futures)
    if pending:
        done, unfinished = concurrent.futures.wait(pending, timeout=10)
        assert not unfinished
        for future in done:
            future.result()


@pytest.fixture
def mem0_source():
    configured = os.environ.get('MEM0_PLUGIN_SOURCE')
    if configured is None:
        pytest.skip('set MEM0_PLUGIN_SOURCE to reviewed Mem0 plugin source for offline integration')
    source = Path(configured).expanduser().resolve()
    if not configured.strip() or not all((source / name).is_file() for name in ('__init__.py', 'plugin.yaml')):
        pytest.fail('MEM0_PLUGIN_SOURCE must contain Mem0 __init__.py and plugin.yaml')
    return source


def test_real_discovery_manager_and_installed_mem0_offline(home, monkeypatch, mem0_source):
    def no_network(*args, **kwargs):
        raise AssertionError('network access forbidden in offline integration')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    # Copy SOURCE ONLY, never config, secrets, state or installed-plugin edits.
    plugins = home / 'plugins'
    plugins.mkdir()
    shutil.copytree(ROOT, plugins / 'scoped-memory', ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', 'tests'))
    # This integration targets the reviewed flat Python plugin, not a profile
    # export. Never copy neighboring configuration, credentials or state.
    destination = plugins / 'mem0'
    destination.mkdir()
    for item in mem0_source.iterdir():
        if item.suffix == '.py' or item.name in ('plugin.yaml', 'LICENSE'):
            if item.is_symlink() or not item.is_file():
                pytest.fail('Mem0 source files must be regular files')
            shutil.copyfile(item, destination / item.name)
    settings = json.loads((home / 'mem0.json').read_text())
    settings['api_key'] = 'offline-placeholder-not-a-secret'
    (home / 'mem0.json').write_text(json.dumps(settings))
    # Select only in THIS temporary home, as the actual host does.
    with (home / 'config.yaml').open('a') as f:
        f.write('\n')
    import hermes_yaml as yaml
    cfg = yaml.safe_load((home / 'config.yaml').read_text())
    cfg['memory']['provider'] = 'scoped-memory'
    (home / 'config.yaml').write_text(yaml.safe_dump(cfg))

    from plugins.memory import load_memory_provider, list_memory_provider_names
    from agent.memory_manager import MemoryManager
    assert 'scoped-memory' in list_memory_provider_names()
    p = load_memory_provider('scoped-memory')
    assert p is not None and p.is_available()
    assert load_memory_provider('scoped-memory') is not p
    # Exercise existing Mem0 implementation, replacing only its backend constructor.
    installed_provider = load_memory_provider('mem0')
    assert installed_provider is not None
    backend = OfflineMem0Backend()
    monkeypatch.setattr(type(installed_provider), '_create_backend', lambda self: backend)
    manager = MemoryManager()
    manager.add_provider(p)
    assert p in manager.providers
    manager.initialize_all(session_id='s1', platform='napcat', hermes_home=str(home), agent_context='primary')
    assert manager.has_tool('scoped_memory')
    assert manager.get_all_tool_schemas()[0]['name'] == 'scoped_memory'
    try:
        with context('10001'):
            manager.on_turn_start(1, 'phoenix', author_id='10001')
            recalled = manager.prefetch_all('phoenix', session_id='s1')
            assert 'OWNER_SECRET' in recalled
            assert json.loads(manager.handle_tool_call('scoped_memory', {'action': 'add', 'content': 'owner fact'})).get('error') is None
            manager.sync_all('owner turn', 'owner reply', session_id='s1', turn_author={'id': '10001'})
            wait_manager(manager)
            assert p._owner._user_id == 'test-owner'
            # Existing Mem0 owns its own asynchronous extraction; join before checking calls.
            thread = p._owner._sync_thread
            if thread:
                thread.join(timeout=10)
                assert not thread.is_alive()
            assert any(c[0] == 'search' and c[2]['filters'] == {'user_id':'test-owner'} for c in backend.calls)
            assert any(c[0] == 'add' and c[2]['user_id'] == 'test-owner' for c in backend.calls)
            backend.data['foreign'] = {'id': 'foreign', 'memory': 'not owner', 'user_id': 'other'}
            backend.data['unknown'] = {'id': 'unknown', 'memory': 'unverifiable'}
            backend.data['mutable-owner'] = {'id': 'mutable-owner', 'memory': 'owner mutable', 'user_id': 'test-owner'}
            for action in ('update', 'delete'):
                for bad in ('foreign', 'unknown', 'not-found'):
                    response = json.loads(manager.handle_tool_call('scoped_memory', {'action': action, 'memory_id': bad, 'content': 'changed'}))
                    assert response == {'error': 'denied_or_not_found'}
            assert not any(c[0] in ('update', 'delete') for c in backend.calls)
            assert 'error' not in json.loads(manager.handle_tool_call('scoped_memory', {'action': 'update', 'memory_id': 'mutable-owner', 'content': 'changed'}))
            assert backend.data['mutable-owner']['memory'] == 'changed'
            assert 'error' not in json.loads(manager.handle_tool_call('scoped_memory', {'action': 'delete', 'memory_id': 'mutable-owner'}))
            assert 'mutable-owner' not in backend.data
            backend.data.pop('foreign')
            backend.data.pop('unknown')
        # Same instance, initialized owner backend: must not leak via any later group hook.
        baseline = len(backend.calls)
        for user, room in [('10001', 'group:9'), ('20002', None), ('20002', 'group:9')]:
            with context(user, room):
                manager.on_turn_start(2, 'GROUP_CONTENT', author_id=user)
                assert 'OWNER_SECRET' not in manager.prefetch_all('phoenix', session_id='s1')
                made = json.loads(manager.handle_tool_call('scoped_memory', {'action': 'add', 'content': 'LOCAL_PHOENIX phoenix'}))
                assert 'result' in made
                manager.sync_all('GROUP_CONTENT '+user, 'local reply', session_id='s1', turn_author={'id': user})
                manager.queue_prefetch_all('GROUP_CONTENT', session_id='s1')
                wait_manager(manager)
                p.drain()
                result = json.loads(manager.handle_tool_call('scoped_memory', {'action': 'search', 'query': 'phoenix'}))
                assert result['result'][0]['owner'] == 'napcat:'+user
                assert result['result'][0]['origin'] == (room or 'dm:'+user)
        assert len(backend.calls) == baseline
        # Session reset does not select a new Mem0 principal.
        with context('10001', sid='new'):
            manager.on_session_switch('new', reset=True)
            assert 'OWNER_SECRET' in manager.prefetch_all('phoenix', session_id='new')
            assert p._owner._user_id == 'test-owner'
    finally:
        manager.shutdown_all()
    assert p._closed
    with p.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 4
