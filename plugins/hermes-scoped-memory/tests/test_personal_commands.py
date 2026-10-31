"""Trusted slash-command review; model tools cannot grant confirmation authority."""
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import ROOT, context, plugin, tool
from scoped_test_plugin.commands import PersonalMemoryCommands
from scoped_test_plugin.scope import ScopeContext
from scoped_test_plugin.store import Store


def test_provider_and_personal_command_work_without_pyyaml(local_home):
    script = '''
import importlib.abc
import importlib.util
from pathlib import Path
import sys

class NoPyYaml(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'yaml' or fullname.startswith('yaml.'):
            raise ModuleNotFoundError('PyYAML is not installed in the managed runtime')

sys.meta_path.insert(0, NoPyYaml())
root, home = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('runtime_probe', root/'__init__.py',
                                           submodule_search_locations=[str(root)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from hermes_constants import set_hermes_home_override
from runtime_probe.commands import PersonalMemoryCommands
set_hermes_home_override(home)
provider = module.ScopedMemoryProvider()
identity = dict(platform='napcat', user_id='20002', chat_id='group:9',
                chat_type='group', thread_id='', session_id='s1', message_id='m1')
command = PersonalMemoryCommands(home, settings_reader=provider._read_settings,
                                 identity_reader=lambda: identity)
assert command('pending') == '当前来源没有待确认的个人记忆。'
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT), str(local_home)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def local_home(home):
    (home / 'mem0.json').unlink()
    (home / 'scoped-memory.json').write_text(json.dumps({
        'mode': 'local-only', 'domain_id': 'personal-review', 'isolation_reviewed': True}))
    path = home / 'config.yaml'
    path.write_text(path.read_text().replace('memory:\n', 'memory:\n  provider: scoped-memory\n', 1))
    return home


def candidate(home, text='我喜欢拿铁'):
    store = Store(home, 'personal-review')
    scope = store.resolve_scope(ScopeContext('personal-review', 'napcat', 'napcat:20002', 'group',
        'group:9', session_id='s1', message_id='source'))
    accepted = store.append_turn(scope, text, '收到', enqueue=True)
    job = store.claim_job('review-fixture')
    made = store.complete_job(job, [{'content': text, 'source_id': accepted['source_id'],
                                    'quote': text, 'start': 0, 'role': 'user'}])
    return store, scope, made['memory_ids'][0]


def registered_command(home):
    captured = SimpleNamespace()
    def register_command(name, handler, **kwargs):
        assert name == 'scoped-memory'
        captured.handler = handler
    plugin.register(SimpleNamespace(register_memory_provider=lambda p: setattr(captured, 'provider', p),
        register_auxiliary_task=lambda *a, **k: object(), register_command=register_command))
    return captured


def test_registered_command_reviews_confirms_and_recall_then_forget(local_home):
    store, scope, ident = candidate(local_home)
    captured = registered_command(local_home)
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    with context('20002', 'group:9'):
        source = SimpleNamespace(user_id='20002', chat_id='group:9', chat_type='group')
        invoke = lambda args: invoke_plugin_command(captured.handler, args, source=source,
                                                     session_id='s1', platform='napcat')
        assert ident in invoke('pending')
        preview = invoke('review ' + ident)
        card = store.review_candidate(scope, ident)
        assert card['content'] in preview and card['digest'] in preview
        assert not store.search(scope, '拿铁')
        assert invoke(f'confirm {ident} {card["digest"]}') == '已确认这条个人记忆。'
        assert store.search(scope, '拿铁')[0]['id'] == ident
        assert '无法处理' in invoke(f'confirm {ident} {card["digest"]}')
        provider = captured.provider
        provider.initialize('s1', hermes_home=str(local_home), platform='napcat')
        try:
            assert '拿铁' in provider.prefetch('拿铁')
            assert tool(provider, 'confirm', memory_id=ident).get('error')
            assert 'result' in tool(provider, 'forget', memory_id=ident)
            assert provider.prefetch('拿铁') == ''
        finally:
            provider.shutdown()


def test_real_memory_loader_registers_sdk_command_without_initializing_an_agent(local_home):
    store, scope, ident = candidate(local_home)
    shutil.copytree(ROOT, local_home/'plugins'/'scoped-memory',
                    ignore=shutil.ignore_patterns('tests', '__pycache__', '.pytest_cache'))
    path = local_home / 'config.yaml'
    path.write_text(path.read_text() + '\nplugins:\n  isolation: in_process\n')
    from plugins.memory import load_memory_provider
    from hermes_cli.plugins import get_plugin_command_handler, get_plugin_manager
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    provider = load_memory_provider('scoped-memory')
    assert provider is not None
    try:
        handler = get_plugin_command_handler('scoped-memory')
        assert callable(handler)
        with context('20002', 'group:9'):
            preview = invoke_plugin_command(handler, 'review ' + ident,
                source=SimpleNamespace(user_id='20002', chat_id='group:9', chat_type='group'),
                session_id='s1', platform='napcat')
            assert store.review_candidate(scope, ident)['digest'] in preview
    finally:
        provider.shutdown()
        get_plugin_manager().unload()


@pytest.mark.parametrize('user,chat,thread', [('30003', 'group:9', ''), ('20002', 'group:10', ''),
                                           ('20002', 'dm:20002', ''), ('20002', 'group:9', 'other')])
def test_foreign_person_origin_and_thread_cannot_review_or_confirm(local_home, user, chat, thread):
    store, scope, ident = candidate(local_home)
    card = store.review_candidate(scope, ident)
    handler = registered_command(local_home).handler
    with context(user, chat, kind='dm' if chat.startswith('dm:') else 'group', thread=thread):
        assert ident not in handler('pending')
        assert '无法处理' in handler('review ' + ident)
        assert '无法处理' in handler(f'confirm {ident} {card["digest"]}')
    assert store.get(scope, ident)['state'] == 'pending'


def test_missing_identity_and_dispatch_spoofing_denied(local_home, monkeypatch):
    store, scope, ident = candidate(local_home)
    handler = registered_command(local_home).handler
    monkeypatch.setenv('HERMES_SESSION_USER_ID', '20002')
    assert '无法处理' in handler('pending')
    with context('20002', 'group:9'):
        assert '无法处理' in handler('pending', session_id='different-session')
        assert '无法处理' in handler('pending', platform='cli')
        assert '无法处理' in handler('pending', source={'user_id': '30003', 'chat_id': 'group:9', 'chat_type': 'group'})
    assert store.get(scope, ident)['state'] == 'pending'


def test_switching_provider_revokes_registered_personal_command(local_home):
    store, scope, ident = candidate(local_home)
    handler = registered_command(local_home).handler
    path = local_home / 'config.yaml'
    path.write_text(path.read_text().replace('provider: scoped-memory', 'provider: builtin'))
    with context('20002', 'group:9'):
        assert '无法处理' in handler('pending')
        assert '无法处理' in handler('review ' + ident)
    assert store.get(scope, ident)['state'] == 'pending'


def test_reject_and_stale_preview_do_not_promote(local_home):
    store, scope, ident = candidate(local_home)
    handler = registered_command(local_home).handler
    card = store.review_candidate(scope, ident)
    with context('20002', 'group:9'):
        assert '无法处理' in handler(f'confirm {ident} {"0"*24}')
        assert handler(f'reject {ident} {card["digest"]}') == '已拒绝这条个人记忆。'
    assert store.get(scope, ident)['state'] == 'rejected'
    assert not store.search(scope, '拿铁')


@pytest.mark.parametrize('change', ['revision', 'purge', 'expired', 'forget', 'policy'])
def test_source_or_epoch_changes_revoke_candidate_confirmation(local_home, change):
    store, scope, ident = candidate(local_home)
    card = store.review_candidate(scope, ident)
    if change == 'revision':
        store.append_turn(scope, '我现在不喜欢拿铁', '收到')
    elif change == 'purge':
        with store.connection(True) as db:
            db.execute('UPDATE sources SET purged=1,user_text=NULL,assistant_text=NULL')
    elif change == 'expired':
        with store.connection(True) as db:
            db.execute('UPDATE memories SET expires=1 WHERE id=?', (ident,))
    elif change == 'policy':
        scope = store.update_policy(scope, 2)
    else:
        store.forget(scope, memory_ids=[ident])
    with pytest.raises(PermissionError):
        store.decide_candidate(scope, ident, 'confirm', expected_digest=card['digest'])
