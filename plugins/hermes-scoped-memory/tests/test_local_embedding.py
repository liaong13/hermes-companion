"""Normal registration to a real loopback embedding fixture; no cloud service."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import time
from types import SimpleNamespace

import pytest

from conftest import context, plugin, tool
from scoped_test_plugin.local_embedding import LocalEmbeddingRuntime, validate_local_http
from scoped_test_plugin.scope import ScopeContext
from scoped_test_plugin.store import Store


@pytest.fixture
def endpoint():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(payload)
            data = [{'index': i, 'embedding': [0, 1] if '雨衣' in text else [1, 0]}
                    for i, text in enumerate(payload['input'])]
            body = json.dumps({'model': payload['model'], 'data': list(reversed(data))}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {'endpoint': f'http://127.0.0.1:{server.server_port}/v1/embeddings',
           'model': 'fixture-embedding', 'model_revision': 'test-v1', 'dimensions': 2}, calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def configure(home, local=None):
    (home/'mem0.json').unlink(missing_ok=True)
    embedding = {'enabled': True}
    if local is not None:
        embedding['local_http'] = local
    (home/'scoped-memory.json').write_text(json.dumps({'mode': 'local-only', 'domain_id': 'vectors',
        'isolation_reviewed': True, 'embedding': embedding}))


def registered_provider():
    captured = []
    plugin.register(SimpleNamespace(register_memory_provider=captured.append,
        register_auxiliary_task=lambda *a, **k: object(), register_command=lambda *a, **k: object()))
    return captured[0]


def test_normal_registration_indexes_local_facts_and_semantically_retrieves_in_exact_scope(home, endpoint, monkeypatch):
    raw, calls = endpoint
    configure(home, raw)
    monkeypatch.setenv('http_proxy', 'http://192.0.2.1:1')
    monkeypatch.setenv('no_proxy', '')
    provider = registered_provider()
    provider.initialize('s1', hermes_home=str(home), platform='napcat')
    try:
        with context('20002', 'group:9'):
            coffee = tool(provider, 'add', content='我爱喝拿铁')['result']['id']
            tool(provider, 'add', content='我有一件雨衣')
        with context('30003', 'group:9'):
            foreign = tool(provider, 'add', content='别人的拿铁偏好')['result']['id']
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            with provider.store.connection() as db:
                if db.execute('SELECT COUNT(*) FROM retrieval_vectors').fetchone()[0] == 3:
                    break
            time.sleep(.02)
        with context('20002', 'group:9'):
            results = tool(provider, 'search', query='咖啡偏好')['result']
            assert [row['id'] for row in results] == [coffee]
            assert foreign not in {row['id'] for row in results}
            status = tool(provider, 'status')['result']
            assert status['embedding'] == 'ready'
            assert status['last_retrieval'] == 'hybrid'
        assert calls and all(set(call) == {'model', 'input', 'encoding_format'} for call in calls)
        assert all('原始对话' not in str(call) for call in calls)
    finally:
        provider.shutdown()


def test_enabled_without_runtime_has_explicit_diagnostic_and_keyword_results(home):
    configure(home)
    provider = registered_provider()
    provider.initialize('s1', hermes_home=str(home), platform='napcat')
    try:
        with context():
            tool(provider, 'add', content='项目凤凰')
            assert tool(provider, 'search', query='凤凰')['result']
        assert provider.capabilities()['embedding'] == 'blocked_missing_runtime'
        assert provider.last_error == 'embedding_unavailable_missing_runtime'
    finally:
        provider.shutdown()


@pytest.mark.parametrize('endpoint', ['https://example.org/v1/embeddings', 'http://localhost:123/v1/embeddings',
    'http://127.0.0.1/v1/embeddings', 'http://user:password@127.0.0.1:123/v1/embeddings',
    'http://127.0.0.1:123/v1/embeddings?token=x', 'http://192.168.1.2:123/v1/embeddings'])
def test_no_hostname_credentials_or_remote_endpoint(endpoint):
    with pytest.raises(ValueError):
        validate_local_http({'endpoint': endpoint, 'model': 'm', 'model_revision': 'v1', 'dimensions': 2})


def test_forget_during_document_embedding_cannot_publish_old_scope(home, endpoint, monkeypatch):
    raw, _ = endpoint
    store = Store(home, 'vectors')
    scope = store.resolve_scope(ScopeContext('vectors', 'napcat', 'napcat:20002', 'group', 'group:9'))
    memory = store.add(scope, '我爱喝拿铁')
    runtime = LocalEmbeddingRuntime(store, raw, allowed=lambda: True)
    def revoke(*args, **kwargs):
        store.forget(scope, memory_ids=[memory['id']])
        return [[1, 0]]
    monkeypatch.setattr(runtime, '_embed', revoke)
    with pytest.raises(PermissionError):
        runtime.refresh()
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM retrieval_vectors').fetchone()[0] == 0


def test_index_failure_backs_off_without_publishing_or_repeated_transport(home, endpoint, monkeypatch):
    raw, _ = endpoint
    store = Store(home, 'vectors')
    scope = store.resolve_scope(ScopeContext('vectors', 'napcat', 'napcat:20002', 'group', 'group:9'))
    store.add(scope, '合成事实')
    runtime = LocalEmbeddingRuntime(store, raw, allowed=lambda: True)
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise ValueError('synthetic failure')
    monkeypatch.setattr(runtime, '_embed', fail)
    with pytest.raises(ValueError):
        runtime.refresh()
    assert runtime.state == 'degraded_keywords'
    assert runtime.refresh() is False
    assert calls == [True]


def test_disable_during_embedding_discards_vectors(home, endpoint, monkeypatch):
    raw, _ = endpoint
    store = Store(home, 'vectors')
    allowed = [True]
    runtime = LocalEmbeddingRuntime(store, raw, allowed=lambda: allowed[0])
    def response(*args):
        allowed[0] = False
        return {'model': raw['model'], 'data': [{'index': 0, 'embedding': [1, 0]}]}
    monkeypatch.setattr(runtime, '_request', response)
    scope = store.resolve_scope(ScopeContext('vectors', 'napcat', 'napcat:20002', 'group', 'group:9'))
    with pytest.raises(PermissionError):
        runtime.query(scope, '咖啡')


def test_policy_change_during_query_cannot_return_keyword_fallback(home, endpoint, monkeypatch):
    raw, _ = endpoint
    configure(home, raw)
    provider = registered_provider()
    provider.initialize('s1', hermes_home=str(home), platform='napcat')
    def change(*args, **kwargs):
        path = home / 'scoped-memory.json'
        settings = json.loads(path.read_text())
        settings['policy_revision'] = 2
        path.write_text(json.dumps(settings))
        raise PermissionError('policy changed')
    monkeypatch.setattr(provider, '_query_embedder', change)
    try:
        with context():
            tool(provider, 'add', content='合成凤凰安排')
            assert tool(provider, 'search', query='凤凰') == {'error': 'denied_or_not_found'}
    finally:
        provider.shutdown()
