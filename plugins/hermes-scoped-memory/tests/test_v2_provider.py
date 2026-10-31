"""Synthetic social-domain lifecycle tests; no production home or network."""
import json
import socket
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from threading import Event

import pytest

from conftest import ROOT, context, plugin, tool
from scoped_test_plugin.extraction import HostExtractor
from scoped_test_plugin.settings import validate


@pytest.fixture
def social(home, monkeypatch):
    def offline(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', offline)
    monkeypatch.setattr(socket, 'create_connection', offline)
    (home / 'mem0.json').unlink()
    (home / 'config.yaml').write_text('memory:\n  memory_enabled: false\n  user_profile_enabled: false\n')
    settings = {'schema_version': 2, 'mode': 'local-only', 'domain_id': 'social-test',
                'isolation_reviewed': True, 'max_injection_bytes': 768}
    (home / 'scoped-memory.json').write_text(json.dumps(settings))
    return home


def make_provider(home, **kwargs):
    p = plugin.ScopedMemoryProvider(owner_factory=lambda: pytest.fail('social constructed owner backend'), **kwargs)
    p.initialize('s1', hermes_home=str(home), platform='napcat')
    return p


def enable_extraction(home):
    path = home / 'scoped-memory.json'
    settings = json.loads(path.read_text())
    settings['extraction'] = {'enabled': True, 'egress_reviewed': True}
    path.write_text(json.dumps(settings))


def test_local_only_no_owner_mapping_credentials_or_delegate(social):
    p = make_provider(social)
    try:
        for user, room in [('10001', None), ('20002', None), ('10001', 'group:9')]:
            with context(user, room):
                assert 'result' in tool(p, 'add', content='独立项目安排')
                assert '独立项目' in p.prefetch('独立项目')
                result = p.sync_turn('合成用户原文', '合成助手回答')
                assert result['status'] == 'durably_accepted'
                assert len(tool(p, 'sources')['result']) == 1
        assert p._owner is None
        with pytest.raises(PermissionError):
            p._delegate()
    finally:
        p.shutdown()


def test_durable_sync_duplicate_and_reopen(social):
    p = make_provider(social)
    with context():
        first = p.sync_turn('合成原文', '回答')
        duplicate = p.sync_turn('合成原文', '回答')
        assert first['source_id'] == duplicate['source_id']
    p.shutdown()
    p = make_provider(social)
    try:
        with context():
            assert len(tool(p, 'sources')['result']) == 1
    finally:
        p.shutdown()


def test_disk_failure_never_reports_accepted(social, monkeypatch):
    p = make_provider(social)
    monkeypatch.setattr(p.store, 'append_turn', lambda *a, **k: (_ for _ in ()).throw(OSError('private disk details')))
    try:
        with context():
            assert p.sync_turn('合成原文', '回答')['status'] == 'failed'
        assert 'private' not in p.last_error
    finally:
        p.shutdown()


def test_host_facade_missing_keeps_job_durable(social):
    enable_extraction(social)
    p = make_provider(social)
    try:
        with context():
            result = p.sync_turn('项目周五发布', '收到')
            assert result['status'] == 'durably_accepted'
            assert result['extraction'] == 'blocked_host_facade'
        with p.store.connection() as db:
            assert db.execute("SELECT state FROM outbox").fetchone()[0] == 'pending'
    finally:
        p.shutdown()


class SyntheticLLM:
    def __init__(self, transform=None):
        self.calls = []
        self.transform = transform

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        source = json.loads(kwargs['input'][0]['text'])[0]
        fact = {'content': source['user_text'], 'source_id': source['source_id'],
                'quote': source['user_text'], 'start': 0, 'role': 'user'}
        if self.transform:
            self.transform(fact)
        return SimpleNamespace(parsed={'facts': [fact]})


def test_injected_facade_extracts_pending_without_worker_identity(social):
    enable_extraction(social)
    llm = SyntheticLLM()
    extractor = HostExtractor(llm, home=social, domain_id='social-test', task_verified=True)
    p = make_provider(social, extractor=extractor)
    try:
        with context('20002', 'group:9'):
            p.sync_turn('合成项目周五发布', '收到')
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            with context('20002', 'group:9'):
                pending = tool(p, 'pending')['result']
            if pending:
                break
            time.sleep(0.01)
        assert len(pending) == 1
        with context('20002', 'group:9'):
            assert tool(p, 'search', query='合成项目')['result'] == []
        assert llm.calls[0]['task'] == 'scoped_memory_extract'
        assert not {'provider', 'model', 'profile', 'agent_id', 'tools'} & llm.calls[0].keys()
    finally:
        p.shutdown()


@pytest.mark.parametrize('transform', [
    lambda f: f.update(source_id='foreign'), lambda f: f.update(start=True),
    lambda f: f.update(quote='伪造原文'), lambda f: f.update(owner='napcat:10001'),
    lambda f: f.update(role='assistant'), lambda f: f.update(content=''),
])
def test_extractor_locally_rejects_bad_candidates(social, transform):
    extractor = HostExtractor(SyntheticLLM(transform), home=social, domain_id='social-test', task_verified=True)
    with pytest.raises(ValueError):
        extractor.extract([{'id': 'source', 'user_text': '合成事实'}])


def test_facade_bound_to_exact_domain_and_home(social):
    extractor = HostExtractor(SyntheticLLM(), home=social, domain_id='different', task_verified=True)
    with pytest.raises(PermissionError):
        make_provider(social, extractor=extractor)
    with pytest.raises(PermissionError):
        HostExtractor(SyntheticLLM(), home=social, domain_id='social-test', task_verified=False)


def test_forget_revokes_slow_prefetch_and_removes_source(social, monkeypatch):
    p = make_provider(social)
    try:
        with context():
            accepted = p.sync_turn('合成项目机密', '收到')
            memory = tool(p, 'add', content='合成项目机密', source_id=accepted['source_id'])['result']
            original = p._search
            def search_then_forget(*args, **kwargs):
                result = original(*args, **kwargs)
                assert 'result' in tool(p, 'forget', memory_id=memory['id'])
                return result
            monkeypatch.setattr(p, '_search', search_then_forget)
            assert p.prefetch('合成项目') == ''
            assert 'error' in tool(p, 'get', memory_id=memory['id'])
            assert 'error' in tool(p, 'forget')
    finally:
        p.shutdown()


def test_config_change_fails_closed_and_preflight_without_mem0(social):
    before = {p.name: p.read_bytes() for p in social.iterdir() if p.is_file()}
    result = subprocess.run([sys.executable, str(ROOT/'preflight.py'), '--home', str(social)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['mode'] == 'local-only'
    assert before == {p.name: p.read_bytes() for p in social.iterdir() if p.is_file()}
    p = make_provider(social)
    try:
        path = social / 'scoped-memory.json'
        settings = json.loads(path.read_text())
        settings['domain_id'] = 'changed'
        path.write_text(json.dumps(settings))
        with context():
            assert p.prefetch('合成') == ''
            assert 'error' in tool(p, 'add', content='合成')
    finally:
        p.shutdown()


@pytest.mark.parametrize('changes', [
    {'max_injection_tokens': 512, 'max_injection_bytes': 768},
    {'mode': 'unknown'}, {'mode': 'local-only', 'domain_id': 'legacy'},
    {'mode': 'local-only', 'domain_id': 'social', 'owner_mem0_user_id': 'secret'},
    {'source_retention_days': float('nan')}, {'max_injection_bytes': True},
    {'jobs': {'concurrency': 2}}, {'embedding': {'enabled': 'false'}},
    {'public_group_memory': {'enabled': True}}, {'extraction': {'enabled': True}},
])
def test_unsafe_settings_rejected(changes):
    if changes.get('mode', 'composite') == 'composite':
        changes = dict(owner_mem0_user_id='test-owner', **changes)
    with pytest.raises((ValueError, PermissionError)):
        validate(changes)


def test_deprecated_byte_alias():
    with pytest.warns(DeprecationWarning):
        assert validate({'owner_mem0_user_id': 'test-owner', 'max_injection_tokens': 512})['max_injection_bytes'] == 512


def test_embedding_wait_is_bounded_and_does_not_build_backlog(social):
    path = social / 'scoped-memory.json'
    settings = json.loads(path.read_text())
    settings['embedding'] = {'enabled': True}
    path.write_text(json.dumps(settings))
    entered, release = Event(), Event()
    calls = []
    def slow_embedding(scope, query, timeout):
        calls.append(query)
        entered.set()
        assert release.wait(5)
        return {'vector': [1, 0], 'fingerprint': None}
    class Retriever:
        def __init__(self, store):
            self.store = store
        def search(self, *args, **kwargs):
            pytest.fail('late vector must not enter a later query')
    p = make_provider(social, retriever_factory=Retriever, query_embedder=slow_embedding)
    try:
        with context():
            assert 'result' in tool(p, 'add', content='合成关键词')
            started = time.monotonic()
            assert '合成关键词' in p.prefetch('合成关键词')
            assert time.monotonic()-started < 2
            assert entered.is_set()
            assert '合成关键词' in p.prefetch('合成关键词')
            assert calls == ['合成关键词']
    finally:
        release.set()
        p.shutdown()


def test_quiet_session_batches_turns_in_one_extraction(social):
    enable_extraction(social)
    llm = SyntheticLLM()
    p = make_provider(social, extractor=HostExtractor(llm, home=social, domain_id='social-test', task_verified=True))
    try:
        from gateway import session_context as sc
        with context():
            for number in range(3):
                token = sc._SESSION_MESSAGE_ID.set('message-'+str(number))
                try:
                    assert p.sync_turn('合成事实'+str(number), '收到')['status'] == 'durably_accepted'
                finally:
                    sc._SESSION_MESSAGE_ID.reset(token)
        deadline = time.monotonic()+5
        done = 0
        while time.monotonic() < deadline:
            with p.store.connection() as db:
                done = db.execute("SELECT COUNT(*) FROM outbox WHERE state='done'").fetchone()[0]
            if done == 3:
                break
            time.sleep(0.01)
        assert done == 3
        assert len(llm.calls) == 1
        assert len(json.loads(llm.calls[0]['input'][0]['text'])) == 3
    finally:
        p.shutdown()


def test_source_revision_tool_is_scoped(social):
    p = make_provider(social)
    try:
        with context():
            first = p.sync_turn('原版合成资料', '回答')
            p.sync_turn('纠正版合成资料', '回答')
            source = tool(p, 'source', source_id=first['source_id'], source_revision=1)['result']
            assert json.loads(source['excerpt'])['user'] == '原版合成资料'
            assert 'error' in tool(p, 'source', source_id=first['source_id'], source_revision=True)
        with context('30003'):
            assert 'error' in tool(p, 'source', source_id=first['source_id'], source_revision=1)
    finally:
        p.shutdown()


def test_v2_real_provider_discovery_without_mem0(social):
    shutil.copytree(ROOT, social/'plugins'/'scoped-memory',
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', 'tests'))
    from plugins.memory import load_memory_provider
    from agent.memory_manager import MemoryManager
    p = load_memory_provider('scoped-memory')
    assert p is not None and p.is_available()
    manager = MemoryManager()
    manager.add_provider(p)
    manager.initialize_all(session_id='s1', platform='napcat', hermes_home=str(social), agent_context='primary')
    try:
        with context('10001', 'group:9'):
            result = json.loads(manager.handle_tool_call('scoped_memory', {'action': 'add', 'content': '合成社交资料'}))
            assert 'result' in result
            assert '合成社交资料' in manager.prefetch_all('合成社交', session_id='s1')
        assert p._owner is None
    finally:
        manager.shutdown_all()


def test_formal_host_binding_is_verified_and_revocation_is_rechecked(social):
    enable_extraction(social)
    class BoundLLM(SyntheticLLM):
        revoked = False
        def binding(self, task):
            if self.revoked:
                raise PermissionError('revoked')
            return {'contract_version': 1, 'plugin_id': 'scoped-memory', 'profile_home': str(social),
                    'task': task, 'task_registered': True}
    llm = BoundLLM()
    p = make_provider(social, host_llm=llm)
    try:
        assert p._extractor is not None
        with context():
            p._extractor.extract([{'id': 'source', 'user_text': '合成证据'}])
            llm.revoked = True
            with pytest.raises(PermissionError):
                p._extractor.extract([{'id': 'source', 'user_text': '合成证据'}])
        assert len(llm.calls) == 1
    finally:
        p.shutdown()


@pytest.mark.parametrize('changes', [{'profile_home': '/tmp/other-profile'}, {'plugin_id': 'another-plugin'},
                                   {'task_registered': False}, {'contract_version': 2}, {'profile_home': ''}])
def test_formal_host_binding_rejects_wrong_attribution(social, changes):
    enable_extraction(social)
    class BoundLLM(SyntheticLLM):
        def binding(self, task):
            return {'contract_version': 1, 'plugin_id': 'scoped-memory', 'profile_home': str(social),
                    'task': task, 'task_registered': True, **changes}
    with pytest.raises(PermissionError):
        make_provider(social, host_llm=BoundLLM())


def test_public_tools_only_offer_proposal_read_and_own_withdrawal(social):
    from scoped_test_plugin.public_memory import PublicMemory
    path = social/'scoped-memory.json'
    settings = json.loads(path.read_text())
    settings['public_group_memory'] = {'enabled': True, 'approval': 'explicit'}
    path.write_text(json.dumps(settings))
    p = make_provider(social, public_factory=PublicMemory)
    try:
        with context('20002', 'group:9'):
            source = p.sync_turn('合成群约定', '收到')['source_id']
            proposal = tool(p, 'public_propose', content='合成群约定', source_id=source,
                            quote='合成群约定', start=0, topic='合成约定')['result']
            assert tool(p, 'public_lookup', query='合成群约定')['result'] == []
            assert tool(p, 'public_pending')['result'][0]['id'] == proposal['id']
            assert 'error' in tool(p, 'public_confirm', memory_id=proposal['id'])
            card = p._public.confirmation_request(p._identity()[1], proposal['id'])
            assert card['confirm_command'].startswith('/memory-confirm '+proposal['id'])
        with context('30003', 'group:9'):
            assert 'error' in tool(p, 'public_withdraw', contribution_id=proposal['contribution_id'])
            with pytest.raises(PermissionError):
                p._public.confirmation_request(p._identity()[1], proposal['id'])
        with context('20002', 'group:9'):
            assert 'result' in tool(p, 'public_withdraw', contribution_id=proposal['contribution_id'])
    finally:
        p.shutdown()
