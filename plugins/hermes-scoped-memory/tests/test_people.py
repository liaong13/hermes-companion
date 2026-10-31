"""Person identity, evidence and forgetting reuse the authoritative Store."""
from dataclasses import replace
import sqlite3

import pytest

from scoped_test_plugin.people import PeopleMemory
from scoped_test_plugin.scope import ScopeContext
from scoped_test_plugin.store import Store


@pytest.fixture
def people(home):
    return PeopleMemory(Store(home, 'people-test'), 'test-companion')


def scope(people, user='20002', group='9', mid='1'):
    return people.store.resolve_scope(ScopeContext('people-test', 'napcat', 'napcat:'+user,
        'group', 'group:'+group, session_id='person', message_id=mid))


def proposal(people, sc, value='拿铁', field='interest'):
    text = '我的自述：'+value
    receipt = people.store.append_turn(sc, text, '')
    return people.propose(sc, receipt['source_id'], field, value, start=5)


def confirm(people, sc, ident):
    card = people.review(sc, ident)
    people.decide(sc, ident, card['digest'], confirm=True)


def test_self_report_pending_confirmation_and_field_replacement(people):
    sc = scope(people)
    ident = proposal(people, sc)
    assert people.recall(sc).facts == ()
    assert proposal(people, sc) == ident  # Duplicate delivery is one candidate.
    confirm(people, sc, ident)
    view = people.recall(sc)
    assert view.facts[0]['content'] == '兴趣：拿铁'
    with pytest.raises(PermissionError):
        confirm(people, sc, ident)
    corrected = proposal(people, replace(sc, message_id='2'), '绿茶')
    confirm(people, sc, corrected)
    assert not view.current()
    assert [r['content'] for r in people.recall(sc).facts] == ['兴趣：绿茶']
    assert people.store.get(sc, ident)['state'] == 'superseded'
    assert people.store.get(sc, corrected)['supersedes'] == ident
    assert people.store.get(sc, corrected)['revision'] == 2
    with people.store.connection() as db:
        row = db.execute('SELECT * FROM person_facts WHERE memory_id=?', (corrected,)).fetchone()
        assert row['subject_id'] == row['asserted_by'] == 'napcat:20002'
        assert row['instance_id'] == 'test-companion'


@pytest.mark.parametrize('user,group', [('30003','9'), ('20002','10')])
def test_other_member_same_display_name_or_other_group_cannot_read_or_decide(people, user, group):
    sc = scope(people)
    ident = proposal(people, sc)
    card = people.review(sc, ident)
    other = scope(people, user, group)
    assert people.recall(other).facts == ()
    for action in (lambda: people.review(other, ident),
                   lambda: people.decide(other, ident, card['digest'], confirm=True),
                   lambda: people.forget(other, ident)):
        with pytest.raises(PermissionError): action()
    assert people.store.get(sc, ident)['state'] == 'pending'


def test_other_instance_no_subject_reassignment_or_foreign_dm_scope(people):
    sc = scope(people)
    ident = proposal(people, sc)
    other = PeopleMemory(people.store, 'another-companion')
    assert other.recall(sc).facts == ()
    with pytest.raises(PermissionError): other.review(sc, ident)
    for invalid in (replace(sc, origin_kind='dm', origin_id='dm:30003'),
            replace(sc, thread_id='other'), replace(sc, platform='other'),
            replace(sc, principal_id='user-supplied-name')):
        with pytest.raises(PermissionError): people.recall(invalid)


def test_exact_peer_dm_reuses_confirmation_without_group_or_other_peer_access(people):
    dm = people.store.resolve_scope(ScopeContext('people-test', 'napcat', 'napcat:20002',
        'dm', 'dm:20002', session_id='person', message_id='1'))
    ident = proposal(people, dm)
    card = people.review(dm, ident)
    other = people.store.resolve_scope(ScopeContext('people-test', 'napcat', 'napcat:30003',
        'dm', 'dm:30003', session_id='person', message_id='2'))
    assert people.recall(other).facts == ()
    with pytest.raises(PermissionError): people.decide(other, ident, card['digest'], confirm=True)
    confirm(people, dm, ident)
    assert people.recall(dm).facts[0]['content'] == '兴趣：拿铁'
    assert people.recall(scope(people)).facts == ()
    with people.store.connection() as db:
        row = db.execute('SELECT subject_id,asserted_by FROM person_facts WHERE memory_id=?', (ident,)).fetchone()
        assert row['subject_id'] == row['asserted_by'] == 'napcat:20002'


def test_mismatched_dm_peer_is_rejected_before_any_store_read(people, monkeypatch):
    invalid = ScopeContext('people-test', 'napcat', 'napcat:20002', 'dm', 'dm:30003')
    monkeypatch.setattr(people.store, 'snapshot', lambda scope: pytest.fail('must reject before storage access'))
    with pytest.raises(PermissionError): people.recall(invalid)


@pytest.mark.parametrize('change', ['source_revision','purge','expire','policy','forget'])
def test_preview_and_inflight_views_fail_when_authority_or_evidence_changes(people, change):
    sc = scope(people)
    ident = proposal(people, sc)
    card = people.review(sc, ident)
    if change == 'source_revision':
        people.store.append_turn(sc, '修改后的原文', '')
    elif change == 'purge':
        with people.store.connection(True) as db: db.execute('UPDATE sources SET purged=1')
    elif change == 'expire':
        with people.store.connection(True) as db: db.execute('UPDATE memories SET expires=1')
    elif change == 'policy':
        sc = people.store.update_policy(sc, 2)
    else:
        people.forget(sc, ident)
    with pytest.raises(PermissionError): people.decide(sc, ident, card['digest'], confirm=True)


def test_forget_invalidates_snapshot_and_restored_content_is_scrubbed(people, home):
    sc = scope(people)
    ident = proposal(people, sc)
    confirm(people, sc, ident)
    view = people.recall(sc)
    backup = home/'before-forget.sqlite3'
    with sqlite3.connect(people.store.path) as db, sqlite3.connect(backup) as dest: db.backup(dest)
    people.forget(sc, ident)
    assert not view.current()
    # Replay the same authoritative deletion ledger after restoring an older content DB.
    with sqlite3.connect(backup) as source, sqlite3.connect(people.store.path) as dest: source.backup(dest)
    restored = PeopleMemory(Store(home, 'people-test'), 'test-companion')
    fresh = scope(restored)
    assert restored.recall(fresh).facts == ()
    with restored.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM person_facts').fetchone()[0] == 0


def test_rejected_and_non_original_evidence_never_become_recallable(people):
    sc = scope(people)
    ident = proposal(people, sc)
    card = people.review(sc, ident)
    people.decide(sc, ident, card['digest'], confirm=False)
    assert people.recall(sc).facts == ()
    assert people.list(sc, pending=True) == []
    receipt = people.store.append_turn(replace(sc, message_id='3'), '其他人的原文', '拿铁')
    with pytest.raises(PermissionError):
        people.propose(sc, receipt['source_id'], 'interest', '拿铁', start=0)
