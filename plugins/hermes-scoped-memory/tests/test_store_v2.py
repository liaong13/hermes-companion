"""Synthetic storage/recovery tests. No model, network or production data."""
from dataclasses import replace
import json
import sqlite3
import time

import pytest

from conftest import plugin
from scoped_test_plugin.scope import ScopeContext


def identity(**changes):
    values = dict(domain_id='social', platform='napcat', principal_id='napcat:20002',
                  origin_kind='dm', origin_id='dm:20002', session_id='session-1', message_id='message-1')
    values.update(changes)
    return ScopeContext(**values)


@pytest.fixture
def storage(home):
    return plugin.Store(home, domain_id='social')


def leased(storage, text='我喜欢喝绿茶'):
    scope = storage.resolve_scope(identity())
    receipt = storage.append_turn(scope, text, '好呀', enqueue=True)
    job = storage.claim_job('worker-one')
    return scope, receipt, job


def candidate(source_id, content='喜欢绿茶', quote='绿茶', start=4):
    return dict(content=content, source_id=source_id, role='user', quote=quote, start=start)


def backup_content(storage, path):
    with sqlite3.connect(storage.path) as source, sqlite3.connect(path) as target:
        source.backup(target)


def restore_content(storage, path):
    with sqlite3.connect(path) as source, sqlite3.connect(storage.path) as target:
        source.backup(target)


def test_immutable_scope_and_domain_isolation(home):
    legacy = plugin.Store(home)
    item = legacy.add(('napcat:20002', 'dm:20002'), 'legacy secret')
    social = plugin.Store(home, domain_id='social')
    scope = social.resolve_scope(identity())
    assert social.search(scope, 'secret') == []
    with pytest.raises(PermissionError):
        social.get(scope, item['id'])
    with pytest.raises(PermissionError):
        social.search(replace(scope, domain_id='other'), 'secret')
    with pytest.raises(Exception):
        scope.origin_id = 'dm:other'


def test_durable_idempotency_distinguishes_message_and_role(storage):
    scope = storage.resolve_scope(identity())
    first = storage.append_turn(scope, 'same', 'answer', enqueue=True)
    again = storage.append_turn(scope, 'same', 'answer', enqueue=True)
    assert first == again
    other = storage.append_turn(replace(scope, message_id='message-2'), 'same', 'answer', enqueue=True)
    assert first['source_id'] != other['source_id']
    revised = storage.append_turn(scope, 'answer', 'same', enqueue=True)
    assert revised['source_id'] == first['source_id']
    assert revised['source_revision'] == 2
    assert revised['job_id'] != first['job_id']
    with storage.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 2


def test_content_and_outbox_rollback_as_one_transaction(storage):
    scope = storage.resolve_scope(identity())
    with storage.connection(True) as db:
        db.execute("CREATE TRIGGER reject_job BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError):
        storage.append_turn(scope, 'synthetic text', 'answer', enqueue=True)
    with storage.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0


def test_restart_and_lease_fencing(home, storage):
    _, _, old = leased(storage)
    with storage.connection(True) as db:
        db.execute('UPDATE outbox SET lease_until=?', (time.time()-1,))
    restarted = plugin.Store(home, domain_id='social')
    current = restarted.claim_job('worker-two')
    assert current['id'] == old['id']
    assert current['lease_token'] != old['lease_token']
    with pytest.raises(PermissionError):
        storage.complete_job(old, [])
    assert restarted.complete_job(current, [])['status'] == 'done'
    assert restarted.claim_job('worker-three') is None
    with restarted.connection() as db:
        assert db.execute('SELECT revision FROM extraction_windows').fetchone()[0] == 1


def test_retry_budget_and_sanitized_errors(storage):
    _, _, job = leased(storage)
    with pytest.raises(ValueError):
        storage.retry_job(job, 'private text body\ncredential')
    assert storage.retry_job(job, 'model_timeout', max_attempts=1)['status'] == 'failed'
    assert storage.claim_job('next') is None
    with storage.connection() as db:
        assert db.execute('SELECT attempts,error_code FROM outbox').fetchone()[:] == (1, 'model_timeout')


def test_candidate_evidence_is_whitelisted_pending_and_atomic(storage):
    scope, receipt, job = leased(storage)
    good = candidate(receipt['source_id'])
    forged = dict(good, source_id='other-source')
    with pytest.raises(ValueError):
        storage.complete_job(job, [good, forged])
    assert storage.pending(scope) == []
    with storage.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM extraction_windows').fetchone()[0] == 0
    result = storage.complete_job(job, [good])
    assert len(result['memory_ids']) == 1
    assert storage.search(scope, '绿茶') == []
    pending = storage.pending(scope)
    assert pending[0]['state'] == 'pending'
    assert storage.get(scope, pending[0]['id'])['evidence'][0]['message_id'] == 'message-1'
    storage.confirm(scope, pending[0]['id'])
    assert storage.search(scope, '绿茶')[0]['content'] == '喜欢绿茶'
    with storage.connection() as db:
        evidence = dict(db.execute('SELECT * FROM evidence').fetchone())
        assert (evidence['start'], evidence['end'], evidence['unit']) == (4, 6, 'characters')


@pytest.mark.parametrize('changes', [dict(role='assistant'), dict(start=0), dict(start=True), dict(quote='不存在'), dict(owner='forged'), dict(state='active')])
def test_candidate_forged_authority_and_bad_offsets_rejected(storage, changes):
    _, receipt, job = leased(storage)
    invalid = dict(candidate(receipt['source_id']), **changes)
    with pytest.raises(ValueError):
        storage.complete_job(job, [invalid])


def test_revised_source_fences_inflight_result(storage):
    scope, _, job = leased(storage)
    storage.append_turn(scope, '现在改喝红茶', '好', enqueue=True)
    with pytest.raises(PermissionError):
        storage.complete_job(job, [])
    current = storage.claim_job('replacement')
    assert current['source_revision'] == 2


def test_policy_revision_is_explicit_monotonic_and_fences_jobs(storage):
    scope, _, job = leased(storage)
    with pytest.raises(PermissionError):
        storage.resolve_scope(replace(scope, policy_revision=2))
    updated = storage.update_policy(scope, 2)
    assert updated.policy_revision == 2
    with pytest.raises(PermissionError):
        storage.complete_job(job, [])
    assert storage.claim_job('replacement') is None
    with pytest.raises(PermissionError):
        storage.update_policy(updated, 1)


def test_forget_physically_clears_dependencies_and_fences_cached_scope(storage):
    scope, receipt, job = leased(storage)
    ids = storage.complete_job(job, [candidate(receipt['source_id'])])['memory_ids']
    storage.confirm(scope, ids[0])
    linked = storage.add(scope, '关联事实', source_id=receipt['source_id'])
    result = storage.forget(scope, memory_ids=ids)
    assert result['status'] == 'active_storage_cleared'
    assert result['context_status'] == 'host_invalidation_required'
    with pytest.raises(PermissionError):
        storage.search(scope, '绿茶')
    with pytest.raises(PermissionError):
        storage.snapshot(scope)
    current = storage.resolve_scope(identity())
    assert current.deletion_epoch == 1
    assert storage.sources(current) == []
    with pytest.raises(PermissionError):
        storage.get(current, linked['id'])
    with storage.connection() as db:
        for table in ('sources', 'memories', 'terms', 'associations', 'evidence', 'outbox', 'extraction_windows'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] == 0


def test_forget_is_scoped_and_old_soft_delete_remains_readable(storage):
    first = storage.resolve_scope(identity())
    other = storage.resolve_scope(identity(principal_id='napcat:30003', origin_id='dm:30003'))
    mine = storage.add(first, '我的秘密')
    theirs = storage.add(other, '别人的秘密')
    with pytest.raises(PermissionError):
        storage.forget(first, memory_ids=[theirs['id']])
    storage.mutate(first, mine['id'], 'deleted')
    assert storage.get(first, mine['id'])['content'] == '我的秘密'
    storage.forget(first)
    assert storage.get(other, theirs['id'])['content'] == '别人的秘密'


def test_independent_deletion_ledger_replays_after_content_restore(home, storage, tmp_path):
    scope, receipt, _ = leased(storage)
    item = storage.add(scope, '绿茶记忆', source_id=receipt['source_id'])
    snapshot = tmp_path / 'old-content.sqlite3'
    backup_content(storage, snapshot)
    storage.forget(scope, memory_ids=[item['id']])
    restore_content(storage, snapshot)
    recovered = plugin.Store(home, domain_id='social')
    current = recovered.resolve_scope(identity())
    assert current.deletion_epoch == 1
    assert recovered.sources(current) == []
    assert recovered.search(current, '绿茶') == []


def test_ledger_rollback_or_missing_denies_recovery(home, storage, tmp_path):
    scope = storage.resolve_scope(identity())
    item = storage.add(scope, '秘密')
    old_ledger = tmp_path / 'old-ledger.sqlite3'
    with sqlite3.connect(storage.ledger_path) as source, sqlite3.connect(old_ledger) as target:
        source.backup(target)
    storage.forget(scope, memory_ids=[item['id']])
    with sqlite3.connect(old_ledger) as source, sqlite3.connect(storage.ledger_path) as target:
        source.backup(target)
    with pytest.raises(PermissionError):
        storage.resolve_scope(identity())
    storage.ledger_path.unlink()
    with pytest.raises(PermissionError):
        plugin.Store(home, domain_id='social')


def test_forget_of_pending_job_prevents_resurrection(storage):
    scope, receipt, job = leased(storage)
    storage.forget(scope, source_ids=[receipt['source_id']])
    with pytest.raises(PermissionError):
        storage.complete_job(job, [candidate(receipt['source_id'])])
    assert storage.claim_job('after-forget') is None


def legacy_database(home):
    directory = home / 'scoped-memory'
    directory.mkdir()
    path = directory / 'memory.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE sources (id TEXT PRIMARY KEY,owner TEXT NOT NULL,origin TEXT NOT NULL,
            session TEXT NOT NULL,message_id TEXT NOT NULL,created REAL NOT NULL,user_text TEXT,
            assistant_text TEXT,sha256 TEXT NOT NULL,purged REAL)''')
        db.execute("INSERT INTO sources VALUES ('legacy-id','legacy-owner','legacy-origin','s','m',1,'old text','reply','old-hash',NULL)")
    return path


def test_legacy_migration_is_backed_up_and_domain_tagged(home):
    path = legacy_database(home)
    storage = plugin.Store(home, domain_id='social')
    with storage.connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('SELECT domain,revision,user_text FROM sources').fetchone()[:] == ('legacy', 1, 'old text')
    backups = list((home / 'scoped-memory-migrations').glob('*.sqlite3'))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert 'domain' not in [r[1] for r in db.execute('PRAGMA table_info(sources)')]
        assert db.execute('SELECT user_text FROM sources').fetchone()[0] == 'old text'
    plugin.Store(home)
    assert len(list((home / 'scoped-memory-migrations').glob('*.sqlite3'))) == 1


def test_backup_failure_stops_before_schema_mutation(home, monkeypatch):
    path = legacy_database(home)
    def fail(*args):
        raise OSError('synthetic backup failure')
    monkeypatch.setattr(plugin.Store, '_backup_legacy', fail)
    with pytest.raises(OSError):
        plugin.Store(home)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert 'domain' not in [r[1] for r in db.execute('PRAGMA table_info(sources)')]


def test_deletion_log_missing_sequence_denies_restore(storage):
    scope = storage.resolve_scope(identity())
    storage.forget(scope)
    scope = storage.resolve_scope(identity())
    storage.forget(scope)
    with sqlite3.connect(storage.ledger_path) as db:
        db.execute('DELETE FROM deletions WHERE seq=1')
    with pytest.raises(PermissionError):
        storage.resolve_scope(identity())


def test_source_revision_preserves_auditable_text_hash_and_coordinates(storage):
    scope, receipt, job = leased(storage)
    item = storage.complete_job(job, [candidate(receipt['source_id'])])['memory_ids'][0]
    first = storage.source(scope, receipt['source_id'])
    storage.append_turn(scope, '现在改喝红茶', '收到', enqueue=True)
    current = storage.source(scope, receipt['source_id'])
    historic = storage.source(scope, receipt['source_id'], revision=1)
    assert json.loads(historic['excerpt']) == {'user': '我喜欢喝绿茶', 'assistant': '好呀'}
    assert historic['sha256'] == first['sha256']
    assert historic['revision'] == 1 and current['revision'] == 2
    assert historic['sha256'] != current['sha256']
    assert json.loads(current['excerpt'])['user'] == '现在改喝红茶'
    assert storage.get(scope, item)['evidence'] == []
    with pytest.raises(PermissionError):
        storage.source(scope, receipt['source_id'], revision=3)
    with pytest.raises(ValueError):
        storage.source(scope, receipt['source_id'], revision=True)
    foreign = storage.resolve_scope(identity(principal_id='napcat:30003', origin_id='dm:30003'))
    with pytest.raises(PermissionError):
        storage.source(foreign, receipt['source_id'], revision=1)
    storage.forget(scope, source_ids=[receipt['source_id']])
    with storage.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM source_revisions').fetchone()[0] == 0


def test_retention_purges_all_historic_source_bodies(storage):
    scope, receipt, _ = leased(storage)
    storage.append_turn(scope, '修订正文', '修订回答')
    with storage.connection(True) as db:
        db.execute('UPDATE sources SET created=?', (time.time()-86400*3,))
    assert storage.retain(1)['purged_sources'] == 1
    for version in (1, 2):
        source = storage.source(scope, receipt['source_id'], revision=version)
        assert source['purged'] is not None
        assert json.loads(source['excerpt']) == {'user': None, 'assistant': None}


def test_legacy_revision_backfill_does_not_enqueue_extraction(home):
    legacy_database(home)
    storage = plugin.Store(home)
    with storage.connection() as db:
        assert db.execute('SELECT source_id,revision,user_text FROM source_revisions').fetchone()[:] == ('legacy-id', 1, 'old text')
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0


def test_claim_window_waits_for_idle_and_limits_exact_session_scope(storage):
    scope = storage.resolve_scope(identity())
    for i in range(9):
        storage.append_turn(replace(scope, message_id='message-'+str(i)), '原文'+str(i), '回答', enqueue=True)
    other = storage.resolve_scope(identity(session_id='other-session', message_id='other-message'))
    storage.append_turn(other, '其他会话', '回答', enqueue=True)
    foreign = storage.resolve_scope(identity(principal_id='napcat:30003', origin_id='dm:30003'))
    storage.append_turn(foreign, '其他用户', '回答', enqueue=True)
    assert storage.claim_window('waiting', idle_seconds=30) == []
    with storage.connection(True) as db:
        db.execute('UPDATE source_revisions SET created=?', (time.time()-60,))
    first = storage.claim_window('batch-worker', idle_seconds=30)
    assert len(first) == 8
    assert all(job['scope'].session_id == scope.session_id and job['scope'].pair == scope.pair for job in first)
    sources = [storage.job_sources(job)[0] for job in first]
    suggestions = [dict(content='候选'+str(i), source_id=source['id'], role='user', quote=source['user_text'], start=0)
                   for i, source in enumerate(sources)]
    assert len(storage.complete_jobs(first, suggestions)['memory_ids']) == 8
    next_window = storage.claim_window('batch-worker', idle_seconds=30)
    assert len(next_window) == 1 and next_window[0]['scope'].session_id == scope.session_id
    storage.complete_jobs(next_window, [])
    other_window = storage.claim_window('batch-worker', idle_seconds=30)
    assert len(other_window) == 1 and other_window[0]['scope'].session_id == 'other-session'


def test_window_commit_rolls_back_all_cursors_when_one_lease_fenced(storage):
    scope = storage.resolve_scope(identity())
    for i in range(2):
        storage.append_turn(replace(scope, message_id=str(i)), '绿茶', '回答', enqueue=True)
    jobs = storage.claim_window('batch-worker', idle_seconds=0)
    assert len(jobs) == 2
    with storage.connection(True) as db:
        db.execute('UPDATE outbox SET lease_token=? WHERE id=?', ('replacement-token', jobs[1]['id']))
    with pytest.raises(PermissionError):
        storage.complete_jobs(jobs, [])
    with storage.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM extraction_windows').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE state='done'").fetchone()[0] == 0


def test_window_commit_refuses_cross_session_jobs(storage):
    scope = storage.resolve_scope(identity())
    storage.append_turn(scope, '绿茶', '回答', enqueue=True)
    storage.append_turn(replace(scope, session_id='other-session'), '红茶', '回答', enqueue=True)
    jobs = [storage.claim_job('worker'), storage.claim_job('worker')]
    with pytest.raises(PermissionError):
        storage.complete_jobs(jobs, [])
