"""Actual plugin + patched host discovery/RPC, with synthetic model responses."""
import json
import shutil
import time
import sqlite3

import pytest

from conftest import ROOT, context


@pytest.mark.parametrize('isolation', ['in_process', 'host'])
def test_actual_scoped_provider_uses_bound_facade_in_each_loading_path(home, monkeypatch, isolation):
    from hermes_cli.plugins import PluginContext, get_plugin_manager
    if not hasattr(PluginContext, 'bound_llm'):
        pytest.skip('requires integration/hermes-host/llm-facade.patch')
    from agent.plugin_llm import PluginLlm, PluginLlmStructuredResult
    from plugins.memory import load_memory_provider
    (home/'mem0.json').unlink()
    (home/'config.yaml').write_text('memory:\n  memory_enabled: false\n  user_profile_enabled: false\n'
                                   'plugins:\n  isolation: '+isolation+'\n')
    (home/'scoped-memory.json').write_text(json.dumps({'schema_version': 2, 'mode': 'local-only',
        'domain_id': 'bound-integration', 'isolation_reviewed': True,
        'extraction': {'enabled': True, 'egress_reviewed': True}}))
    shutil.copytree(ROOT, home/'plugins'/'scoped-memory', ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', 'tests'))
    calls = []
    def synthetic(self, **kwargs):
        calls.append(kwargs)
        source = json.loads(kwargs['input'][0]['text'])[0]
        fact = {'content': source['user_text'], 'source_id': source['source_id'], 'quote': source['user_text'],
                'start': 0, 'role': 'user'}
        return PluginLlmStructuredResult(text='', provider='synthetic', model='synthetic', agent_id='',
                                          parsed={'facts': [fact]}, content_type='json')
    monkeypatch.setattr(PluginLlm, 'complete_structured', synthetic)
    provider = load_memory_provider('scoped-memory')
    assert provider is not None
    provider.initialize('s1', hermes_home=str(home), platform='napcat')
    try:
        with context('20002', 'group:9'):
            provider.sync_turn('合成宿主接线证据', '收到', session_id='s1')
        deadline = time.monotonic()+7
        pending = []
        while time.monotonic() < deadline:
            with context('20002', 'group:9'):
                result = json.loads(provider.handle_tool_call('scoped_memory', {'action': 'pending'}))
                pending = result.get('result', [])
            if pending:
                break
            time.sleep(0.02)
        diagnostics = {'calls': len(calls), 'last_error': provider.last_error}
        if (home/'scoped-memory'/'memory.sqlite3').exists():
            with sqlite3.connect(home/'scoped-memory'/'memory.sqlite3') as db:
                diagnostics['jobs'] = db.execute('SELECT state,error_code FROM outbox').fetchall()
                diagnostics['sources'] = db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
        assert len(pending) == 1, diagnostics
        assert pending[0]['content'] == '合成宿主接线证据'
        assert calls and calls[0]['task'] == 'scoped_memory_extract'
        assert not {'provider', 'profile', 'model', 'tools'} & calls[0].keys()
    finally:
        provider.shutdown()
        manager = get_plugin_manager()
        manager.unload()
        if isolation == 'host':
            manager._plugin_host().shutdown()
