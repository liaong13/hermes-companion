"""API-free is an enforced local policy, including rejection of loopback APIs."""
import json
import socket

import pytest

from conftest import context, plugin, tool
from scoped_test_plugin.settings import validate


@pytest.mark.parametrize('extra', [
    {'mode':'composite', 'schema_version':1, 'domain_id':'legacy', 'owner_mem0_user_id':'demo'},
    {'embedding':{'enabled':True}},
    {'embedding':{'enabled':False, 'local_http':{'base_url':'http://127.0.0.1:8080'}}},
    {'extraction':{'enabled':True,'egress_reviewed':True}},
])
def test_api_free_rejects_remote_and_loopback_runtime_configuration(extra):
    with pytest.raises(PermissionError):
        validate(dict(mode='local-only', schema_version=2, domain_id='offline', api_free=True, **extra)
                 if 'mode' not in extra else {**extra,'api_free':True})


def test_api_free_flag_is_an_explicit_boolean():
    with pytest.raises(ValueError): validate({'mode':'local-only','domain_id':'offline','api_free':'true'})


def test_actual_provider_crud_archive_keywords_and_forget_without_any_socket(home, monkeypatch):
    def denied(*args, **kwargs): raise AssertionError('API/network access forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    (home/'mem0.json').unlink()
    (home/'scoped-memory.json').write_text(json.dumps(dict(schema_version=2, mode='local-only',
        domain_id='offline', api_free=True, isolation_reviewed=True, max_injection_bytes=768)))
    provider = plugin.ScopedMemoryProvider(owner_factory=lambda: pytest.fail('must not load Mem0'))
    provider.initialize('s1', hermes_home=str(home), platform='napcat')
    try:
        with context('20002', 'group:9'):
            assert provider.sync_turn('天文学阅读计划', '合成回执')['status'] == 'durably_accepted'
            ident = tool(provider, 'add', content='天文学阅读计划')['result']['id']
            assert '天文学' in provider.prefetch('天文学')
            assert tool(provider, 'forget', memory_id=ident)['result']['status'] == 'active_storage_cleared'
        assert provider._owner is None
        assert provider._local_embedding is None
        assert provider._extractor is None
        assert provider.capabilities()['api_free'] is True
    finally:
        provider.shutdown()
