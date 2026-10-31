import sqlite3

import pytest

from local_life_test_helpers import route
from local_life_test.core import memory_api
from local_life_test.rules import self_statement


def confirm(service, r, ident):
    scope = service.scope(r)
    card = service.people.review(scope, ident)
    service.command(r, '/social-memory confirm '+ident+' '+card['digest'])


def fact_and_reply(service):
    r = route()
    service.observe(r, '我喜欢天文学', can_extract=True)
    ident = service.people.list(service.scope(r), pending=True)[0]['id']
    confirm(service, route('2'), ident)
    request = route('3')
    service.observe(request, '天文学里面的恒星是什么？')
    view = service.recall(request)
    service.record_reply(request, '900', '你喜欢天文学，我们刚才聊到恒星。', view)
    return ident, request, view


def test_public_api_is_versioned_stable_and_not_a_provider(service):
    api = memory_api()
    assert api is memory_api()
    assert api.API_VERSION == 1
    assert not hasattr(api, 'register')
    assert isinstance(service.store, api.Store)
    assert not (service.home/'mem0.json').exists()


@pytest.mark.parametrize('text,expected', [
    ('我喜欢天文学', ('interest','天文学',3)),
    ('我对古典音乐感兴趣。', ('interest','古典音乐',2)),
    ('我希望你回复简短一些', ('preference','回复简短一些',4)),
    ('我不希望被主动私聊', ('boundary','被主动私聊',4)),
    ('他说我喜欢天文学', None), ('如果我喜欢天文学呢？', None),
    ('我喜欢“天文学”这个说法', None), ('我不喜欢天文学', None),
    ('我喜欢天文学，但是现在不想聊', None), ('我喜欢天文学吗？', None),
])
def test_rules_extract_exact_self_spans_and_abstain_on_ambiguity(text, expected):
    assert self_statement(text) == expected


def test_natural_candidates_stay_pending_and_original_delivery_is_idempotent(service):
    r = route()
    for _ in range(2): service.observe(r, '我喜欢天文学', can_extract=True)
    pending = service.people.list(service.scope(r), pending=True)
    assert len(pending) == 1
    assert service.recall(r).facts == ()
    confirm(service, route('2'), pending[0]['id'])
    assert service.recall(route('3')).facts[0]['content'] == '兴趣：天文学'
    with service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM life_events').fetchone()[0] == 1


@pytest.mark.parametrize('other', [route(user='88888'), route(group='20002')])
def test_events_and_pending_candidates_never_cross_people_or_origins(service, other):
    service.observe(route(), '我喜欢天文学', can_extract=True)
    assert service.recall(other).conversation['events'] == []
    assert service.people.list(service.scope(other), pending=True) == []


def test_topics_questions_real_replies_and_closing_signals(service):
    r = route()
    service.observe(r, '恒星里面的氢聚变是什么？')
    topic = service.recall(r).conversation['topic_id']
    assert service.recall(r).conversation['awaiting_reply'] == ['1']
    service.record_reply(r, '900', '我们来讨论氢聚变。', service.recall(r))
    follow = route('2')
    service.observe(follow, '那恒星里面还能发生什么？', reply_to='900', verified_bot_reply=True)
    state = service.recall(follow).conversation
    assert state['topic_id'] == topic
    assert state['awaiting_reply'] == ['2']
    assert [e['role'] for e in state['events']] == ['member', 'self']
    service.observe(route('3'), '先这样', reply_to='900', verified_bot_reply=True)
    assert service.recall(route('3')).conversation['closed']
    service.observe(route('4'), '今天晚饭吃什么？')
    assert service.recall(route('4')).conversation['topic_id'] != topic


def test_forget_cascades_through_raw_replies_and_future_bot_quotes(service):
    ident, request, view = fact_and_reply(service)
    follow = route('4')
    service.observe(follow, '那我们接着说恒星？', reply_to='900', verified_bot_reply=True)
    service.record_reply(follow, '901', '继续天文学的话题。', service.recall(follow))
    assert view.current()
    assert '已忘记' in service.command(route('5'), '/social-memory forget '+ident)
    assert not view.current()
    assert service.suppressed_messages('group:20001') == {'900','901'}
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text LIKE '%天文学%'").fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_revisions WHERE assistant_text LIKE ?', ('%天文学%',)).fetchone()[0] == 0


def test_recalled_ordinary_question_revokes_reply_and_replay_cannot_restore_it(service):
    r = route()
    service.observe(r, '恒星是什么？')
    service.record_reply(r, '900', '恒星的解释。', service.recall(r))
    service.revoke('group:20001', '1')
    assert service.suppressed_messages('group:20001') == {'900'}
    with pytest.raises(PermissionError): service.observe(r, '恒星是什么？')


def test_source_revision_revokes_inflight_view_and_derivatives(service):
    r = route()
    service.observe(r, '恒星是什么？')
    view = service.recall(route('2'))
    service.record_reply(r, '900', '恒星的解释。', service.recall(r))
    service.store.append_turn(service.scope(r), '修订后的问题', '')
    assert not view.current()
    service.recall(route('3'))
    assert service.suppressed_messages('group:20001') == {'900'}


def test_restore_old_content_replays_deletions_for_person_and_derived_sources(service, tmp_path):
    ident, request, _ = fact_and_reply(service)
    backup = tmp_path/'old.sqlite3'
    with sqlite3.connect(service.store.path) as source, sqlite3.connect(backup) as dest: source.backup(dest)
    service.command(route('4'), '/social-memory forget '+ident)
    with sqlite3.connect(backup) as source, sqlite3.connect(service.store.path) as dest: source.backup(dest)
    assert service.recall(route('5')).facts == ()
    assert service.suppressed_messages('group:20001') == {'900'}


def test_late_ack_after_unload_can_only_add_a_tombstone(service):
    r = route()
    service.observe(r, '恒星是什么？')
    view = service.recall(r)
    service.close()
    with pytest.raises(PermissionError): service.record_reply(r, '900', '迟到内容', view)
    service.block_delivered(r, '900')
    assert service.suppressed_messages('group:20001') == {'900'}
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text='迟到内容'").fetchone()[0] == 0


def test_public_bot_quote_dependencies_revoke_other_members_derivatives(service):
    ident, _, _ = fact_and_reply(service)
    other = route('4', user='88888')
    service.observe(other, '这个问题我也想了解')
    view = service.recall(other, quote_message_id='900')
    assert view.facts == ()  # Public quote metadata confers no person-profile read.
    assert any(r['kind']=='quote' for r in view.references)
    service.record_reply(other, '902', '这条公开引用提到了天文学。', view)
    service.command(route('5'), '/social-memory forget '+ident)
    assert not view.current()
    assert {'900','902'}.issubset(service.suppressed_messages('group:20001'))
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text LIKE '%天文学%'").fetchone()[0] == 0


def test_conversation_retention_preserves_confirmed_evidence_but_removes_unconfirmed_raw_text(service):
    service.observe(route(), '我喜欢天文学', can_extract=True)
    ident = service.people.list(service.scope(route()), pending=True)[0]['id']
    confirm(service, route('2'), ident)
    service.observe(route('3'), '未确认的普通聊天原文')
    with service.store.connection(True) as db: db.execute('UPDATE life_events SET created=0')
    view = service.recall(route('4'))
    assert view.facts[0]['content'] == '兴趣：天文学'
    assert view.conversation['events'] == []
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sources WHERE user_text='未确认的普通聊天原文'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sources WHERE user_text='我喜欢天文学'").fetchone()[0] == 1


def test_long_dependency_chain_is_checked_without_recursive_fanout(service):
    previous = ''
    for index in range(20):
        r = route(str(index+1))
        service.observe(r, '那我们继续讨论恒星？' if index else '恒星是什么？',
                        reply_to=previous, verified_bot_reply=bool(previous))
        view = service.recall(r)
        assert view.current()
        previous = str(900+index)
        service.record_reply(r, previous, '继续恒星的讨论。', view)
    service.revoke('group:20001', '1')
    assert len(service.suppressed_messages('group:20001')) == 20


def test_rejected_source_and_forgotten_command_are_not_recreated_on_replay(service):
    r = route()
    service.observe(r, '我喜欢天文学', can_extract=True)
    ident = service.people.list(service.scope(r), pending=True)[0]['id']
    card = service.people.review(service.scope(r), ident)
    service.command(route('2'), '/social-memory reject '+ident+' '+card['digest'])
    service.observe(r, '我喜欢天文学', can_extract=True)
    assert service.people.list(service.scope(r), pending=True) == []
    manual = route('3')
    text = '/social-memory remember interest 古典音乐'
    service.command(manual, text)
    ident = service.people.list(service.scope(manual), pending=True)[0]['id']
    service.command(route('4'), '/social-memory forget '+ident)
    assert '无法处理' in service.command(manual, text)
    assert service.people.list(service.scope(route('5')), pending=True) == []


def test_oversized_original_input_is_not_archived_or_made_a_candidate(service):
    assert service.observe(route(), '我喜欢'+('天文学'*300), can_extract=True) is False
    with service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0
    assert service.people.list(service.scope(route()), pending=True) == []
