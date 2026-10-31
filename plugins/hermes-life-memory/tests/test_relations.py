"""Evidence, explicit consent, revocation and one-shot contact without sockets."""
from datetime import datetime, timezone
import re
import sqlite3
import time

import pytest

from local_life_test_helpers import route
from local_life_test.relations import Relations


def plan(service, *, confirm=True):
    due = datetime.fromtimestamp(time.time()+600,timezone.utc).isoformat()
    reply = service.command(route(),'/social-memory plan '+due+' 继续讨论恒星')
    ident = re.search(r'review ([a-f0-9]{32})',reply)[1]
    review = service.command(route('2'),'/social-memory review '+ident)
    assert '本群 @ 本人' in review
    command = review.split('确认：')[1].split('\n')[0]
    if confirm:
        assert '已确认' in service.command(route('3'),command)
    return ident,command


def advance(monkeypatch):
    now=time.time()+601
    monkeypatch.setattr(time,'time',lambda:now)


def test_explicit_time_and_consent_required(relations_service, monkeypatch):
    s=relations_service
    for value in ('明天','2026-12-01T12:00:00','2000-01-01T12:00:00+08:00'):
        with pytest.raises(ValueError): Relations.check_plan(value,'继续聊')
    ident,command=plan(s,confirm=False)
    advance(monkeypatch)
    assert s.contact_opportunity('group:20001') is None
    assert '无法处理' in s.command(route('4',user='88888'),command)
    assert '无法处理' in s.command(route('4',group='20002'),command)
    assert '已确认' in s.command(route('5'),command)
    offer=s.contact_opportunity('group:20001')
    assert offer.recipient=='77777' and offer.current()
    assert s.contact_opportunity('group:20002') is None
    offer.claim('attempt-one')
    assert s.contact_opportunity('group:20001') is None
    assert offer.record_delivery('900')
    offer.finish('notified')
    assert not offer.current()
    assert s.contact_opportunity('group:20001') is None
    state=s.recall(route('6')).conversation
    assert state['events'][0]['kind']=='contact'
    assert state['events'][0]['reply_to']==''


def test_cancel_and_restore_cannot_revive_consent_or_late_plaintext(relations_service, monkeypatch, tmp_path):
    s=relations_service
    ident,_=plan(s)
    advance(monkeypatch)
    offer=s.contact_opportunity('group:20001')
    backup=tmp_path/'backup.sqlite3'
    with sqlite3.connect(s.store.path) as source,sqlite3.connect(backup) as dest: source.backup(dest)
    offer.claim('attempt')
    assert '无法处理' in s.command(route('7',user='88888'),'/social-memory cancel '+ident)
    response=s.command(route('8'),'/social-memory cancel '+ident)
    assert response.contact_feedback==((offer.reference,'cancelled'),)
    assert not offer.current()
    assert not offer.record_delivery('901')
    assert '901' in s.suppressed_messages('group:20001')
    with sqlite3.connect(backup) as source,sqlite3.connect(s.store.path) as dest: source.backup(dest)
    assert s.contact_opportunity('group:20001') is None
    with s.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]==0


def test_joint_episode_requires_actual_both_sides_and_owns_evidence(relations_service):
    s=relations_service
    s.observe(route(),'恒星是什么？')
    s.observe(route('2'),'先这样')
    assert s.recall(route('3')).relations==()
    s.observe(route('4'),'恒星里面的氢聚变是什么？')
    s.record_reply(route('4'),'900','我们一起聊了氢聚变。',s.recall(route('4')))
    s.observe(route('5'),'先这样')
    view=s.recall(route('6'))
    assert len(view.relations)==1 and view.relations[0]['kind']=='episode'
    assert '氢聚变' in view.relations[0]['content']
    assert not s.recall(route('6',user='88888')).relations
    with s.store.connection(True) as db: db.execute('UPDATE life_events SET created=created-8*86400')
    retained=s.recall(route('7'))
    assert retained.relations and retained.conversation['events']==[]
    ident=view.relations[0]['id']
    assert '已忘记' in s.command(route('8'),'/social-memory forget '+ident)
    assert not view.current() and not s.recall(route('9')).relations
    assert '900' in s.suppressed_messages('group:20001')


def test_ack_and_dependencies_commit_atomically(relations_service, monkeypatch):
    s=relations_service
    s.observe(route(),'恒星是什么？')
    original=s.store.append_in
    def fail(db,*args,**kwargs):
        original(db,*args,**kwargs)
        raise OSError('simulated metadata failure after source insert')
    monkeypatch.setattr(s.store,'append_in',fail)
    with pytest.raises(OSError): s.record_reply(route(),'900','ATOMIC_CANARY',s.recall(route()))
    with s.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text='ATOMIC_CANARY'").fetchone()[0]==0


def test_feedback_distinguishes_activity_from_reply_and_close(relations_service, monkeypatch):
    s=relations_service
    ident,_=plan(s)
    advance(monkeypatch)
    offer=s.contact_opportunity('group:20001')
    offer.claim('attempt')
    offer.record_delivery('900')
    offer.finish('notified')
    s.contact_feedback('group:20001','88888',offer.reference,'replied')
    assert s.recall(route('6')).relations[0]['status']=='notified'
    s.contact_feedback('group:20001','77777',offer.reference,'activity_observed')
    assert s.recall(route('6')).relations[0]['status']=='activity_observed'
    result=s.command(route('7'),'/social-memory close '+ident)
    assert '不代表外部任务结果已核验' in result
    assert not s.recall(route('8')).relations


def test_episode_cannot_preserve_a_reply_after_its_person_fact_is_forgotten(relations_service):
    s=relations_service
    result=s.command(route(),'/social-memory remember interest 天文学')
    ident=re.search(r'review ([a-f0-9]{32})',result)[1]
    review=s.command(route('2'),'/social-memory review '+ident)
    s.command(route('3'),review.split('确认：')[1].split('\n')[0])
    s.observe(route('4'),'恒星是什么？')
    s.record_reply(route('4'),'900','你喜欢天文学，我们聊恒星。',s.recall(route('4')))
    s.observe(route('5'),'先这样')
    assert s.recall(route('6')).relations
    s.command(route('7'),'/social-memory forget '+ident)
    assert not s.recall(route('8')).relations
    assert '900' in s.suppressed_messages('group:20001')
    with s.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text LIKE '%天文学%'").fetchone()[0]==0


def test_restored_consumed_offer_does_not_block_another_members_due_plan(relations_service,monkeypatch):
    s=relations_service
    ident,_=plan(s)
    due=datetime.fromtimestamp(time.time()+600,timezone.utc).isoformat()
    response=s.command(route('10',user='88888'),'/social-memory plan '+due+' 接着聊音乐')
    second=re.search(r'review ([a-f0-9]{32})',response)[1]
    review=s.command(route('11',user='88888'),'/social-memory review '+second)
    s.command(route('12',user='88888'),review.split('确认：')[1].split('\n')[0])
    advance(monkeypatch)
    offer=s.contact_opportunity('group:20001',excluded={s.relations.reference(ident)})
    assert offer.recipient=='88888' and offer.reference==s.relations.reference(second)
