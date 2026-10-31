import json
import os
import subprocess
import sys

import pytest

from conftest import context, plugin, tool, ROOT


@pytest.mark.parametrize('identity', [None, '', '   ', 123, False])
def test_composite_requires_explicit_owner_before_backend_access(home, identity):
    path = home / 'scoped-memory.json'
    settings = json.loads(path.read_text())
    if identity is None:
        settings.pop('owner_mem0_user_id')
    else:
        settings['owner_mem0_user_id'] = identity
    path.write_text(json.dumps(settings))
    p = plugin.ScopedMemoryProvider(owner_factory=lambda: pytest.fail('must not construct owner backend'))
    assert not p.is_available()
    with pytest.raises(PermissionError, match='explicit non-empty owner_mem0_user_id'):
        p.initialize('s1', hermes_home=str(home), platform='cli')
    assert p.home is None and p._owner is None


def test_preflight_missing_owner_mapping_is_readonly_and_blocked(home):
    path = home / 'scoped-memory.json'
    path.write_text(json.dumps({'isolation_reviewed': True}))
    before = {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}
    result = subprocess.run([sys.executable, str(ROOT/'preflight.py'), '--home', str(home)],
                            capture_output=True, text=True, env=os.environ, timeout=20)
    assert result.returncode == 2
    assert json.loads(result.stdout)['reason'] == 'composite requires an explicit non-empty owner_mem0_user_id'
    assert before == {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}


def test_preflight_readonly_pass_and_block(home):
    before = {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}
    result = subprocess.run([sys.executable, str(ROOT/'preflight.py'), '--home', str(home)], capture_output=True, text=True, env=os.environ, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['safe_to_activate'] == 'NOT_CERTIFIED'
    assert before == {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}
    (home/'scoped-memory.json').write_text('{}')
    result = subprocess.run([sys.executable, str(ROOT/'preflight.py'), '--home', str(home)], capture_output=True, text=True, env=os.environ, timeout=20)
    assert result.returncode == 2
    assert json.loads(result.stdout)['safe_to_activate'] is False


def test_persistence_and_reset_keeps_user_origin(home):
    factory = lambda: (_ for _ in ()).throw(AssertionError('must never create cloud'))
    p = plugin.ScopedMemoryProvider(owner_factory=factory)
    p.initialize('s1', hermes_home=str(home), platform='napcat')
    with context('20002', 'group:9'):
        old = tool(p, 'add', content='永久火星计划')['result']
        p.sync_turn('原文', '答复')
    p.shutdown()
    p = plugin.ScopedMemoryProvider(owner_factory=factory)
    p.initialize('new', hermes_home=str(home), platform='napcat')
    try:
        with context('20002', 'group:9', sid='new'):
            p.on_session_switch('new', reset=True)
            assert tool(p, 'search', query='火星')['result'][0]['id'] == old['id']
            assert len(tool(p, 'sources')['result']) == 1
        with context('20002', 'group:10', sid='new'):
            assert tool(p, 'search', query='火星')['result'] == []
    finally:
        p.shutdown()


def test_incomplete_live_context_never_falls_back_cli_owner(home):
    p = plugin.ScopedMemoryProvider(owner_factory=lambda: (_ for _ in ()).throw(AssertionError('owner fallback')))
    p.initialize('s1', hermes_home=str(home), platform='cli')
    try:
        with context('10001', 'group:9', platform=''):
            assert p.prefetch('owner') == ''
            assert 'error' in tool(p, 'add', content='key')
        assert p._owner is None
    finally:
        p.shutdown()


def test_runtime_gate_change_owner_mapping_and_shutdown(home, provider):
    with context():
        made = tool(provider, 'add', content='keeper')
        assert 'result' in made
    config = (home/'config.yaml').read_text().replace('10001', '99999')
    (home/'config.yaml').write_text(config)
    with context('10001'):
        assert provider.prefetch('keeper') == ''
        assert 'error' in tool(provider, 'search', query='keeper')
    provider.shutdown()
    provider.shutdown()  # idempotent
    with context():
        assert 'error' in tool(provider, 'add', content='after close')
