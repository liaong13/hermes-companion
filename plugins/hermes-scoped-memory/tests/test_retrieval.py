import json
from concurrent.futures import ThreadPoolExecutor
import math
import time
import sqlite3
from threading import Event

import pytest

from conftest import plugin
from scoped_test_plugin.retrieval import (
    BudgetLedger, EmbeddingError, Fingerprint, HybridRetriever, OpenRouterEmbeddings,
    VectorIndex, unit_vector,
)


@pytest.fixture
def indexed(home):
    store = plugin.Store(home)
    index = VectorIndex(store)
    fingerprint = Fingerprint('synthetic-model', 'synthetic-provider', 2, model_revision='fixed-test-v1')
    index.create_generation('first', fingerprint)
    return store, index, fingerprint


def add(indexed, scope, text, vector, generation='first'):
    store, index, _ = indexed
    memory = store.add(scope, text)
    index.put(scope, memory['id'], generation, vector, vector_fingerprint=indexed[2])
    return memory


def test_semantic_and_rrf_only_touch_authorized_rows(indexed):
    store, index, _ = indexed
    scope = ('napcat:20002', 'group:9')
    semantic = add(indexed, scope, '会议挪到周五', [1, 0])
    lexical = add(indexed, scope, '讨论评审时间', [0.8, 0.6])
    add(indexed, ('napcat:30003', 'group:9'), '别人的评审时间', [1, 0])
    add(indexed, ('napcat:20002', 'group:10'), '别群的评审时间', [1, 0])
    add(indexed, ('napcat:20002', 'group:9:thread:x'), '线程的评审时间', [1, 0])
    index.activate('first', expected_count=5)
    # Poison a foreign vector: a correct prefilter never parses or quarantines it.
    with store.connection(True) as db:
        db.execute("""UPDATE retrieval_vectors SET vector='INVALID' WHERE memory_id IN
            (SELECT id FROM memories WHERE owner='napcat:30003')""")
    assert index.search(scope, [1, 0], query_fingerprint=indexed[2])[0]['id'] == semantic['id']
    rows = HybridRetriever(store, index).search(scope, '评审时间', [1, 0], query_fingerprint=indexed[2])
    assert rows[0]['id'] == lexical['id']  # appears in both ranked lists
    assert {row['id'] for row in rows} == {lexical['id'], semantic['id']}


def test_generation_fingerprint_and_atomic_coverage(indexed):
    store, index, fingerprint = indexed
    scope = ('napcat:20002', 'dm:20002')
    one = add(indexed, scope, '项目', [1, 0])
    assert index.create_generation('first', fingerprint) == 'first'
    with pytest.raises(ValueError, match='fingerprint_conflict'):
        index.create_generation('first', Fingerprint('other-model', 'synthetic-provider', 2))
    index.activate('first', expected_count=1)
    second_fp = Fingerprint('other-model', 'other-provider', 3)
    index.create_generation('second', second_fp)
    with pytest.raises(ValueError, match='generation_incomplete'):
        index.activate('second')
    assert index.search(scope, [1, 0], query_fingerprint=fingerprint)[0]['id'] == one['id']
    index.put(scope, one['id'], 'second', [0, 1, 0], vector_fingerprint=second_fp)
    with pytest.raises(ValueError, match='count_mismatch'):
        index.activate('second', expected_count=2)
    assert index.activate('second', expected_count=1) == 1
    with pytest.raises(ValueError, match='unavailable'):
        index.search(scope, [1, 0], generation='first', query_fingerprint=fingerprint)
    assert index.search(scope, [0, 1, 0], query_fingerprint=second_fp)[0]['id'] == one['id']


@pytest.mark.parametrize('vector', [[0, 0], [math.nan, 1], [math.inf, 1], [True, 1], [1], 'bad'])
def test_vector_validation(vector):
    with pytest.raises(ValueError, match='invalid_vector'):
        unit_vector(vector, 2)


def test_corrupt_generation_degrades_to_keywords(indexed):
    store, index, _ = indexed
    scope = ('napcat:20002', 'dm:20002')
    row = add(indexed, scope, '关键词', [1, 0])
    index.activate('first')
    with store.connection(True) as db:
        db.execute("UPDATE retrieval_vectors SET vector='[0,0]' WHERE memory_id=?", (row['id'],))
    retriever = HybridRetriever(store, index)
    assert retriever.search(scope, '关键词', [1, 0], query_fingerprint=indexed[2])[0]['id'] == row['id']
    assert retriever.last_status == 'degraded'
    with store.connection() as db:
        assert db.execute('SELECT state FROM retrieval_generations').fetchone()[0] == 'quarantined'


def test_archived_expired_and_superseded_are_excluded(indexed):
    store, index, _ = indexed
    scope = ('napcat:20002', 'dm:20002')
    old = add(indexed, scope, '原计划', [1, 0])
    expired = add(indexed, scope, '过期计划', [1, 0])
    archived = add(indexed, scope, '归档计划', [1, 0])
    index.activate('first')
    store.mutate(scope, archived['id'], 'archived')
    store.add(scope, '新计划', supersedes=old['id'])
    with store.connection(True) as db:
        db.execute('UPDATE memories SET expires=? WHERE id=?', (time.time()-1, expired['id']))
    assert index.search(scope, [1, 0], query_fingerprint=indexed[2]) == []
    with pytest.raises(ValueError, match='stale_fact'):
        index.put(scope, old['id'], 'first', [1, 0], vector_fingerprint=indexed[2])
    with pytest.raises(PermissionError):
        index.put(('napcat:99999', scope[1]), old['id'], 'first', [1, 0], vector_fingerprint=indexed[2])


def embedding_client(indexed, *, request=None, budget='1', **overrides):
    store, _, fingerprint = indexed
    ledger = BudgetLedger(store, daily_usd=budget, monthly_usd=budget,
                          checkpoint_path=store.path.parent.parent.parent / (store.path.parent.parent.name+'-billing-checkpoint.json'))
    settings = dict(enabled=True, key_supplier=lambda: 'SYNTHETIC_KEY',
                    approved_providers=('synthetic-provider',), policy_verified=True,
                    budget_recovery_verified=True,
                    token_upper_bound=lambda text: len(text.encode()) + 1,
                    max_price_per_token='0.00000001', ledger=ledger, request=request)
    settings.update(overrides)
    return OpenRouterEmbeddings(fingerprint, **settings)


def reply(count=1, **updates):
    result = dict(model='synthetic-model', provider='synthetic-provider',
                  data=[dict(index=i, embedding=[1, i+1]) for i in range(count)],
                  usage={'prompt_tokens': count})
    result.update(updates)
    return result


def test_embedding_request_privacy_shape_order_and_budget(indexed):
    captured = []
    def request(payload, key, timeout):
        captured.append((payload, key, timeout))
        value = reply(2)
        value['data'].reverse()
        return value
    client = embedding_client(indexed, request=request)
    vectors = client.embed(('napcat:20002', 'dm:20002'), ['合成摘要', '其他摘要'],
                           input_type='query', timeout=.8, egress_authorized=True)
    assert vectors[0] == unit_vector([1, 1], 2)
    assert vectors[1] == unit_vector([1, 2], 2)
    payload, key, timeout = captured[0]
    assert payload['provider'] == {'only': ['synthetic-provider'], 'allow_fallbacks': False,
                                   'data_collection': 'deny', 'zdr': True}
    assert payload['input_type'] == 'search_query'
    assert timeout == .8 and key == 'SYNTHETIC_KEY'
    with client.ledger.connection() as db:
        charge = dict(db.execute('SELECT * FROM embedding_charges').fetchone())
        assert charge['status'] == 'success' and charge['usage_tokens'] == 2
        assert charge['reserved_nano'] > 0 and '20002' not in charge['scope_hash']


@pytest.mark.parametrize('overrides', [dict(enabled=False), dict(policy_verified=False),
    dict(budget_recovery_verified=False),
    dict(approved_providers=()), dict(key_supplier=None), dict(token_upper_bound=None),
    dict(max_price_per_token=None)])
def test_policy_blocks_before_any_transport(indexed, overrides):
    client = embedding_client(indexed, request=lambda *args: pytest.fail('network attempted'), **overrides)
    with pytest.raises(EmbeddingError, match='policy_blocked'):
        client.embed(('napcat:20002', 'dm:20002'), ['合成'], egress_authorized=True)
    assert OpenRouterEmbeddings(indexed[2]).enabled is False


@pytest.mark.parametrize('changes', [dict(model='wrong'), dict(provider='wrong'),
    dict(data=[]), dict(data=[dict(index=1, embedding=[1, 0])]),
    dict(data=[dict(index=0, embedding=[math.nan, 1])]), dict(usage={'prompt_tokens': -1})])
def test_damaged_response_not_accepted_and_not_refunded(indexed, changes):
    client = embedding_client(indexed, request=lambda *a: reply(**changes))
    with pytest.raises(EmbeddingError, match='request_failed'):
        client.embed(('napcat:20002', 'dm:20002'), ['合成'], egress_authorized=True)
    with client.ledger.connection() as db:
        row = db.execute('SELECT status,reserved_nano FROM embedding_charges').fetchone()
        assert row['status'] == 'unknown' and row['reserved_nano'] > 0


def test_duplicate_indices_and_batch_count_rejected(indexed):
    response = reply(2)
    response['data'][1]['index'] = 0
    client = embedding_client(indexed, request=lambda *args: response)
    with pytest.raises(EmbeddingError):
        client.embed(('napcat:20002', 'dm:20002'), ['合成', '例子'], egress_authorized=True)


def test_timeout_reservation_survives_restart_and_budget_is_atomic(indexed):
    scope = ('napcat:20002', 'dm:20002')
    calls = []
    def timeout(*args):
        calls.append(1)
        raise TimeoutError('SENSITIVE_BODY_API_KEY')
    # Each synthetic call reserves exactly 20 nano dollars; only one fits.
    client = embedding_client(indexed, request=timeout, budget='0.00000002')
    with pytest.raises(EmbeddingError) as error:
        client.embed(scope, ['x'], egress_authorized=True)
    assert 'SENSITIVE' not in str(error.value)
    restarted = embedding_client(indexed, request=timeout, budget='0.00000002')
    with pytest.raises(EmbeddingError, match='budget_exhausted'):
        restarted.embed(scope, ['x'], egress_authorized=True)
    assert calls == [1]
    ledger = BudgetLedger(indexed[0], daily_usd='0.00000004', monthly_usd='0.00000004')
    def reserve(_):
        try:
            return ledger.reserve(scope, '0.00000002')
        except EmbeddingError:
            return None
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(bool(x) for x in pool.map(reserve, range(5))) == 1


def test_circuit_breaker_does_not_retry_or_expose_remote_errors(indexed):
    calls = []
    def fail(*args):
        calls.append(1)
        raise RuntimeError('SENSITIVE_RESPONSE')
    client = embedding_client(indexed, request=fail)
    for _ in range(3):
        with pytest.raises(EmbeddingError, match='^embedding_request_failed$'):
            client.embed(('napcat:20002', 'dm:20002'), ['合成'], egress_authorized=True)
    with pytest.raises(EmbeddingError, match='policy_blocked'):
        client.embed(('napcat:20002', 'dm:20002'), ['合成'], egress_authorized=True)
    assert len(calls) == 3


def test_usage_missing_stays_unknown_and_token_bound_breach_pauses(indexed):
    client = embedding_client(indexed, request=lambda *args: reply(usage=None))
    client.embed(('napcat:20002', 'dm:20002'), ['合成'], egress_authorized=True)
    with client.ledger.connection() as db:
        assert db.execute('SELECT usage_tokens FROM embedding_charges').fetchone()[0] is None
    client.request = lambda *args: reply(usage={'prompt_tokens': 100000})
    with pytest.raises(EmbeddingError):
        client.embed(('napcat:20002', 'dm:20002'), ['合成'], egress_authorized=True)
    assert client.paused


def test_token_bound_guard_never_sends_oversized_or_unauthorized_inputs(indexed):
    client = embedding_client(indexed, request=lambda *args: pytest.fail('network attempted'))
    scope = ('napcat:20002', 'dm:20002')
    for texts in ([], [''], ['x']*17, ['中'*1000]):
        with pytest.raises(EmbeddingError):
            client.embed(scope, texts, egress_authorized=True)
    with pytest.raises(EmbeddingError, match='policy_blocked'):
        client.embed(scope, ['合成'])


def test_domain_isolation_and_forget_cascades_vectors(home):
    from scoped_test_plugin.scope import ScopeContext
    left, right = plugin.Store(home, 'left'), plugin.Store(home, 'right')
    left_scope = ScopeContext('left', 'napcat', 'napcat:20002', 'group', 'group:9')
    right_scope = ScopeContext('right', 'napcat', 'napcat:20002', 'group', 'group:9')
    left_index, right_index = VectorIndex(left), VectorIndex(right)
    fingerprint = Fingerprint('synthetic-model', 'synthetic-provider', 2)
    for store, index, scope in ((left, left_index, left_scope), (right, right_index, right_scope)):
        index.create_generation('first', fingerprint)
        row = store.add(scope, '同名项目', ttl_days=1)
        index.put(scope, row['id'], 'first', [1, 0], vector_fingerprint=fingerprint)
        index.activate('first')
    left_id = left_index.search(left_scope, [1, 0], query_fingerprint=fingerprint)[0]['id']
    assert right_index.search(right_scope, [1, 0], query_fingerprint=fingerprint)[0]['id'] != left_id
    with pytest.raises(PermissionError):
        left_index.search(right_scope, [1, 0], query_fingerprint=fingerprint)
    left.forget(left_scope, memory_ids=[left_id])
    with pytest.raises(PermissionError):
        left_index.search(left_scope, [1, 0], query_fingerprint=fingerprint)
    with left.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM retrieval_vectors WHERE memory_id=?', (left_id,)).fetchone()[0] == 0
    assert right_index.search(right_scope, [1, 0], query_fingerprint=fingerprint)


def test_same_dimension_query_or_document_from_other_model_is_rejected(indexed):
    store, index, fingerprint = indexed
    scope = ('napcat:20002', 'dm:20002')
    row = add(indexed, scope, '合成关键词', [1, 0])
    index.activate('first')
    wrong = Fingerprint('wrong-model', fingerprint.provider, fingerprint.dimensions)
    for query_fp in (None, wrong):
        with pytest.raises(ValueError, match='query_fingerprint_mismatch'):
            index.search(scope, [1, 0], query_fingerprint=query_fp)
        retriever = HybridRetriever(store, index)
        assert retriever.search(scope, '合成关键词', [1, 0], query_fingerprint=query_fp)[0]['id'] == row['id']
        assert retriever.last_status == 'degraded'
    for vector_fp in (None, wrong):
        with pytest.raises(ValueError, match='vector_fingerprint_mismatch'):
            index.put(scope, row['id'], 'first', [1, 0], vector_fingerprint=vector_fp)


def test_slow_embedding_cannot_publish_after_forget(indexed):
    store = indexed[0]
    scope = store.snapshot(('napcat:20002', 'dm:20002'))
    def delete_during_request(*args):
        store.forget(scope)
        return reply()
    client = embedding_client(indexed, request=delete_during_request)
    with pytest.raises(EmbeddingError, match='request_failed'):
        client.embed(scope, ['合成'], egress_authorized=True)
    with client.ledger.connection() as db:
        assert db.execute('SELECT status FROM embedding_charges').fetchone()[0] == 'unknown'


def test_response_must_prove_reviewed_provider(indexed):
    value = reply()
    value.pop('provider')
    client = embedding_client(indexed, request=lambda *args: value)
    with pytest.raises(EmbeddingError):
        client.embed(('napcat:20002', 'dm:20002'), ['合成'], egress_authorized=True)


def test_boolean_recovery_flag_cannot_replace_external_checkpoint(indexed):
    ledger = BudgetLedger(indexed[0], daily_usd='1', monthly_usd='1')
    client = OpenRouterEmbeddings(indexed[2], enabled=True, policy_verified=True,
        budget_recovery_verified=True, approved_providers=('synthetic-provider',),
        key_supplier=lambda: pytest.fail('key read before recovery proof'),
        token_upper_bound=lambda text: 1, max_price_per_token='0.01', ledger=ledger,
        request=lambda *args: pytest.fail('network attempted'))
    with pytest.raises(EmbeddingError, match='policy_blocked'):
        client.embed(('napcat:20002', 'dm:20002'), ['合成'], egress_authorized=True)


def backup_sqlite(source, target):
    with sqlite3.connect(source) as incoming, sqlite3.connect(target) as outgoing:
        incoming.backup(outgoing)


def test_content_backup_restore_cannot_refund_independent_reservations(indexed, tmp_path):
    client = embedding_client(indexed, budget='0.00000002', request=lambda *args: reply())
    backup = tmp_path/'before-content.sqlite3'
    backup_sqlite(indexed[0].path, backup)
    client.embed(('napcat:20002', 'dm:20002'), ['x'], egress_authorized=True)
    backup_sqlite(backup, indexed[0].path)
    reopened = embedding_client(indexed, budget='0.00000002', request=lambda *args: pytest.fail('budget reset'))
    with pytest.raises(EmbeddingError, match='budget_exhausted'):
        reopened.embed(('napcat:20002', 'dm:20002'), ['x'], egress_authorized=True)
    assert client.ledger.path.parent.name == 'scoped-memory-billing'
    assert client.ledger.path.stat().st_mode & 0o777 == 0o600
    assert client.ledger.path.parent.stat().st_mode & 0o777 == 0o700


def test_billing_rollback_and_lost_checkpoint_fail_closed(indexed, tmp_path):
    client = embedding_client(indexed, request=lambda *args: reply())
    backup = tmp_path/'old-billing.sqlite3'
    backup_sqlite(client.ledger.path, backup)
    client.embed(('napcat:20002', 'dm:20002'), ['x'], egress_authorized=True)
    backup_sqlite(backup, client.ledger.path)
    with pytest.raises(EmbeddingError, match='rollback_or_replacement'):
        client.ledger.assert_recovery_proven()
    client.ledger.checkpoint_path.unlink()
    with pytest.raises(EmbeddingError, match='policy_blocked'):
        client.ledger.assert_recovery_proven()


def test_tampered_charge_materialization_is_detected(indexed):
    client = embedding_client(indexed, request=lambda *args: reply())
    client.embed(('napcat:20002', 'dm:20002'), ['x'], egress_authorized=True)
    with sqlite3.connect(client.ledger.path) as db:
        db.execute('UPDATE embedding_charges SET reserved_nano=0')
    with pytest.raises(EmbeddingError, match='ledger_corrupt'):
        client.ledger.assert_recovery_proven()


def test_http_total_deadline_discards_late_reply_and_bounds_active_calls(indexed):
    entered, release, finished = Event(), Event(), Event()
    calls = []
    def slow_request(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        finished.set()
        return reply()
    client = embedding_client(indexed, request=slow_request)
    try:
        start = time.monotonic()
        with pytest.raises(EmbeddingError, match='request_failed'):
            client.embed(('napcat:20002', 'dm:20002'), ['合成'], timeout=.05, egress_authorized=True)
        assert time.monotonic()-start < .5
        assert entered.is_set()
        with pytest.raises(EmbeddingError, match='request_failed'):
            client.embed(('napcat:20002', 'dm:20002'), ['后来查询'], timeout=.05, egress_authorized=True)
        assert calls == [1]
        with client.ledger.connection() as db:
            assert [r[0] for r in db.execute('SELECT status FROM embedding_charges')] == ['unknown', 'unknown']
    finally:
        release.set()
        assert finished.wait(2)
