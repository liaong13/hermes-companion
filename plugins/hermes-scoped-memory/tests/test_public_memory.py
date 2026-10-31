"""Public group statements use explicit synthetic host capabilities, never roles."""
from dataclasses import replace
import json
import sqlite3
import time

import pytest

from conftest import plugin
from scoped_test_plugin.scope import ScopeContext
from scoped_test_plugin.public_memory import ConfirmationAuthority, ConfirmationEvidence, PublicMemory


def group_scope(store, user='20002', group='9', thread='', **changes):
    values = dict(domain_id=store.domain_id, platform='napcat', principal_id='napcat:'+user,
                  origin_kind='group', origin_id='group:'+group, thread_id=thread,
                  session_id='session-'+user, message_id='message-'+user)
    values.update(changes)
    return store.resolve_scope(ScopeContext(**values))


def source(store, scope, text, suffix=''):
    scope = replace(scope, message_id=scope.message_id+suffix)
    return store.append_turn(scope, text, '合成回复')['source_id']


@pytest.fixture
def public(home):
    store = plugin.Store(home, domain_id='social')
    known = {}
    seen = []
    def verify(event, descriptor):
        seen.append(dict(descriptor))
        if type(event) is not object or event not in known:
            raise PermissionError('unrecognized synthetic host event')
        return known[event]
    authority = ConfirmationAuthority(verify_event=verify, home=home, domain_id='social')
    service = PublicMemory(store, confirmation_authority=authority)
    service.events, service.descriptors = known, seen
    return service


def propose(public, scope=None, text='群规：只分享公开资料', topic='群规', suffix=''):
    scope = scope or group_scope(public.store)
    sid = source(public.store, scope, text, suffix)
    result = public.propose(scope, text, sid, text, 0, topic, participants=['资料中的参与人'])
    return scope, sid, result


def approval(public, proposal, *, scope=None, quote='我确认将此约定在本群公开', independent=None, suffix='-confirm'):
    scope = scope or group_scope(public.store, '30003')
    sid = source(public.store, scope, quote, suffix)
    event = object()
    public.events[event] = ConfirmationEvidence(scope, sid, quote, 0, independent)
    grant = public.issue_confirmation(event, proposal['id'])
    return grant, scope, sid


def active(public, *, independent=False):
    author, sid, proposal = propose(public)
    content = '群规：只分享公开资料'
    grant, confirmer, confirmation_sid = approval(public, proposal, quote=content if independent else '我确认将此约定在本群公开',
                                                 independent=content if independent else None)
    assert public.confirm(grant, proposal['id'])['state'] == 'active'
    return author, sid, proposal, confirmer, confirmation_sid


def test_proposals_private_until_host_confirmation(public):
    scope, _, proposal = propose(public)
    other = group_scope(public.store, '30003')
    assert public.lookup(scope, '群规') == []
    assert public.proposals(other) == []
    own = public.proposals(scope)
    assert own[0]['asserted_by'] == scope.principal_id
    assert own[0]['id'] == proposal['id'] and own[0]['state'] == 'pending'
    for fake in ({'confirmed': True, 'role': 'admin'}, True, 'yes', object()):
        with pytest.raises(PermissionError):
            public.confirm(fake, proposal['id'])
    with pytest.raises(PermissionError):
        public.issue_confirmation({'user_id': '30003', 'role': 'admin'}, proposal['id'])


def test_confirmation_records_separate_asserted_by_and_hides_raw_source(public):
    author, sid, proposal, confirmer, confirm_sid = active(public)
    result = public.lookup(confirmer, '群规')
    assert result[0]['asserted_by'] == [author.principal_id]
    assert not {'quote', 'source_id', 'participants', 'user_text'} & result[0].keys()
    with public.store.connection() as db:
        contributions = db.execute('SELECT principal,source_id,kind FROM public_contributions WHERE fact_id=?',
                                   (proposal['id'],)).fetchall()
        assert {(r['principal'], r['source_id'], r['kind']) for r in contributions} == {
            (author.principal_id, sid, 'assertion'), (confirmer.principal_id, confirm_sid, 'confirmation')}


@pytest.mark.parametrize('changes', [{'group': '10'}, {'thread': 'other-thread'}, {'policy_revision': 2}])
def test_exact_group_thread_and_policy_filter(public, changes):
    _, _, _, _, _ = active(public)
    scope = group_scope(public.store, '40004', **changes)
    assert public.lookup(scope, '群规') == []


def test_cross_domain_and_dm_rejected(public, home):
    author, _, _, _, _ = active(public)
    with pytest.raises(PermissionError):
        public.lookup(replace(author, domain_id='foreign'), '群规')
    dm = public.store.resolve_scope(replace(author, origin_kind='dm', origin_id='dm:20002'))
    with pytest.raises(PermissionError):
        public.lookup(dm, '群规')
    with pytest.raises(PermissionError):
        PublicMemory(plugin.Store(home, domain_id='different'), confirmation_authority=public._authority)


@pytest.mark.parametrize('changes', [dict(start=True), dict(start=999), dict(quote='伪造引文'), dict(source_id='unknown')])
def test_proposal_evidence_is_current_and_owned(public, changes):
    scope = group_scope(public.store)
    sid = source(public.store, scope, '公开约定')
    args = dict(content='公开约定', source_id=sid, quote='公开约定', start=0, topic='项目')
    args.update(changes)
    with pytest.raises((PermissionError, ValueError)):
        public.propose(scope, **args)
    with public.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM public_facts').fetchone()[0] == 0


def test_private_or_other_contributor_evidence_cannot_be_promoted(public):
    author = group_scope(public.store)
    other = group_scope(public.store, '30003')
    sid = source(public.store, other, '他人的原始声明')
    with pytest.raises(PermissionError):
        public.propose(author, '他人的原始声明', sid, '他人的原始声明', 0, '项目')


def test_grants_are_one_use_expiring_and_instance_bound(public, home, monkeypatch):
    _, _, proposal = propose(public)
    grant, _, _ = approval(public, proposal)
    assert public.confirm(grant, proposal['id'])['state'] == 'active'
    with pytest.raises(PermissionError):
        public.confirm(grant, proposal['id'])
    _, _, another = propose(public, text='项目周五评审', topic='项目', suffix='-other')
    grant, _, _ = approval(public, another, suffix='-other-confirm')
    other_authority = ConfirmationAuthority(verify_event=lambda *_: None, home=home, domain_id='social')
    other_service = PublicMemory(public.store, confirmation_authority=other_authority)
    with pytest.raises(PermissionError):
        other_service.confirm(grant, another['id'])
    import scoped_test_plugin.public_memory as module
    now = time.monotonic()
    monkeypatch.setattr(module.time, 'monotonic', lambda: now+1000)
    with pytest.raises(PermissionError):
        public.confirm(grant, another['id'])


def test_confirmation_cannot_cross_origin_or_use_foreign_source(public):
    _, _, proposal = propose(public)
    wrong = group_scope(public.store, '30003', group='10')
    grant, _, _ = approval(public, proposal, scope=wrong)
    with pytest.raises(PermissionError):
        public.confirm(grant, proposal['id'])
    assert public.lookup(group_scope(public.store), '群规') == []


def test_source_revision_and_retention_revoke_public_recall(public):
    author, sid, proposal, other, _ = active(public)
    cached = public.lookup(other, '群规')
    public.store.append_turn(author, '修改后的声明', '合成回复')
    assert public.lookup(other, '群规') == []
    with pytest.raises(PermissionError):
        public.validate_results(other, cached)
    with public.store.connection() as db:
        assert db.execute('SELECT content FROM public_facts WHERE id=?', (proposal['id'],)).fetchone()[0] == ''
        assert db.execute('SELECT COUNT(*) FROM public_terms WHERE fact_id=?', (proposal['id'],)).fetchone()[0] == 0
    # A separate statement is revoked when its original source retention expires.
    author, sid, new = propose(public, text='项目六月发布', topic='发布', suffix='-retain')
    grant, _, _ = approval(public, new, suffix='-retain-confirm')
    public.confirm(grant, new['id'])
    with public.store.connection(True) as db:
        db.execute('UPDATE sources SET created=? WHERE id=?', (time.time()-864000, sid))
    public.store.retain(1)
    assert public.lookup(other, '六月') == []


def test_author_withdraw_uses_deletion_ledger_and_keeps_other_source(public):
    author, sid, proposal, other, confirmation_sid = active(public)
    with pytest.raises(PermissionError):
        public.withdraw(other, proposal['contribution_id'])
    result = public.withdraw(author, proposal['contribution_id'])
    assert result['cleared'] == 'own_source_and_dependents'
    assert result['deletion_sequence'] > 0
    assert public.lookup(other, '群规') == []
    assert public.store.source(other, confirmation_sid)['id'] == confirmation_sid
    assert public.rebuild(other, proposal['id'])['state'] == 'paused'
    with pytest.raises(PermissionError):
        public.store.source(public.store.resolve_scope(author), sid)


def test_independent_confirmation_can_rebuild_without_deleted_contribution(public):
    author, _, proposal, other, _ = active(public, independent=True)
    public.withdraw(author, proposal['contribution_id'])
    assert public.lookup(other, '群规') == []
    rebuilt = public.rebuild(other, proposal['id'])
    assert rebuilt['state'] == 'active'
    result = public.lookup(other, '群规')
    assert result[0]['asserted_by'] == [other.principal_id]
    with public.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM public_contributions WHERE principal=?', (author.principal_id,)).fetchone()[0] == 0


def test_withdrawing_last_confirmation_does_not_promote_unconfirmed_proposal(public):
    author, _, proposal, other, _ = active(public)
    with public.store.connection() as db:
        contribution = db.execute("SELECT id FROM public_contributions WHERE fact_id=? AND kind='confirmation'",
                                  (proposal['id'],)).fetchone()[0]
    public.withdraw(other, contribution)
    assert public.lookup(author, '群规') == []
    assert public.rebuild(author, proposal['id'])['state'] == 'paused'


def test_independent_confirmation_cannot_smuggle_summary(public):
    _, _, proposal = propose(public)
    grant, _, _ = approval(public, proposal, independent='未出现在确认人消息中的内容')
    with pytest.raises(ValueError):
        public.confirm(grant, proposal['id'])


def test_confirmed_conflicts_require_explicit_host_resolution(public):
    _, _, first, _, _ = active(public)
    author = group_scope(public.store, '40004')
    _, _, second = propose(public, author, text='群规：允许分享全部资料', topic='群规')
    grant, other, _ = approval(public, second, suffix='-conflict')
    assert public.confirm(grant, second['id'])['state'] == 'conflict'
    assert public.lookup(other, '群规') == []
    with pytest.raises(PermissionError):
        public.rebuild(other, first['id'])
    with pytest.raises(PermissionError):
        public.issue_confirmation(object(), first['id'])
    sid = source(public.store, other, '按群规则决定采用最初公开资料约定', '-resolve')
    event = object()
    public.events[event] = ConfirmationEvidence(other, sid, '按群规则决定采用最初公开资料约定', 0)
    resolution = public.issue_resolution(event, first['id'])
    assert public.descriptors[-1]['action'] == 'resolve_conflict'
    assert public.resolve_conflict(resolution, first['id'])['state'] == 'active'
    assert public.lookup(other, '群规')[0]['content'] == '群规：只分享公开资料'
    assert public.proposals(author)[0]['state'] == 'superseded'


def test_unconfirmed_conflict_does_not_disable_existing_approved_statement(public):
    _, _, _, other, _ = active(public)
    propose(public, group_scope(public.store, '40004'), text='群规：相反的未经确认主张', topic='群规')
    assert len(public.lookup(other, '群规')) == 1


def test_content_restore_replays_public_deletion_triggers(public, tmp_path):
    author, sid, proposal, other, _ = active(public, independent=True)
    backup = tmp_path/'content-backup.sqlite3'
    with sqlite3.connect(public.store.path) as source_db, sqlite3.connect(backup) as target:
        source_db.backup(target)
    public.withdraw(author, proposal['contribution_id'])
    with sqlite3.connect(backup) as source_db, sqlite3.connect(public.store.path) as target:
        source_db.backup(target)
    reopened = plugin.Store(public.store.path.parent.parent, domain_id='social')
    restored = PublicMemory(reopened)
    other = reopened.resolve_scope(other)
    assert restored.lookup(other, '群规') == []
    with reopened.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sources WHERE id=?', (sid,)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM public_contributions WHERE principal=?', (author.principal_id,)).fetchone()[0] == 0
    assert restored.rebuild(other, proposal['id'])['state'] == 'active'


def test_policy_change_revokes_contributor_and_stale_approval(public):
    author, _, _, other, _ = active(public)
    public.store.update_policy(author, 2)
    assert public.lookup(other, '群规') == []
    with pytest.raises(PermissionError):
        public.proposals(author)


def test_group_context_revision_changes_for_other_participant_after_forget(public):
    author, _, proposal, other, _ = active(public, independent=True)
    original_scope = public.store.snapshot(other)
    original_context = public.context_revision(other)
    public.withdraw(author, proposal['contribution_id'])
    assert public.store.snapshot(other) == original_scope
    after_forget = public.context_revision(other)
    assert after_forget > original_context
    public.rebuild(other, proposal['id'])
    assert public.context_revision(other) > after_forget
    elsewhere = group_scope(public.store, '50005', group='10')
    assert public.context_revision(elsewhere) != public.context_revision(other)


def test_group_context_revision_updates_on_publish_purge_and_policy(public):
    observer = group_scope(public.store, '50005')
    before = public.context_revision(observer)
    author, sid, proposal = propose(public)
    pending = public.context_revision(observer)
    assert pending > before
    grant, _, _ = approval(public, proposal)
    public.confirm(grant, proposal['id'])
    published = public.context_revision(observer)
    assert published > pending
    public.store.update_policy(author, 2)
    assert public.context_revision(observer) > published
    author, sid, proposal = propose(public, text='新的公开约定', topic='新约定', suffix='-purge',
                                    scope=group_scope(public.store, '40004'))
    grant, _, _ = approval(public, proposal, suffix='-purge-confirm')
    public.confirm(grant, proposal['id'])
    before_purge = public.context_revision(observer)
    with public.store.connection(True) as db:
        db.execute('UPDATE sources SET created=? WHERE id=?', (time.time()-864000, sid))
    public.store.retain(1)
    assert public.context_revision(observer) > before_purge


def test_restore_context_namespace_includes_independent_deletion_watermark(public, tmp_path):
    author, _, proposal, other, _ = active(public, independent=True)
    backup = tmp_path/'namespace-backup.sqlite3'
    with sqlite3.connect(public.store.path) as source_db, sqlite3.connect(backup) as target:
        source_db.backup(target)
    before_delete = {public.context_revision(other)}
    # Advance the content-local counter after the snapshot; replay must not
    # accidentally reuse any of these old, contaminated session namespaces.
    for i in range(4):
        propose(public, text='额外公开候选'+str(i), topic='额外'+str(i), suffix='-extra-'+str(i))
        before_delete.add(public.context_revision(other))
    public.withdraw(author, proposal['contribution_id'])
    with sqlite3.connect(backup) as source_db, sqlite3.connect(public.store.path) as target:
        source_db.backup(target)
    restored_store = plugin.Store(public.store.path.parent.parent, domain_id='social')
    restored = PublicMemory(restored_store)
    other = restored_store.resolve_scope(other)
    assert restored.context_revision(other) not in before_delete


def test_missing_host_authority_never_promotes_pending(public):
    scope, _, proposal = propose(public)
    plain = PublicMemory(public.store)
    assert plain.proposals(scope)[0]['state'] == 'pending'
    with pytest.raises(PermissionError):
        plain.issue_confirmation(object(), proposal['id'])
    with pytest.raises(PermissionError):
        plain.confirm({'approved': True}, proposal['id'])
