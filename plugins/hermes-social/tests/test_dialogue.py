"""Independent authored dialogue metadata fixtures; never invokes a model/QQ."""
import copy
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import support
from support import event
from hermes_social.context import ContextScope
from hermes_social.dialogue import DialogueState


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.path = Path(self.temp.name)/'dialogue.sqlite3'
        self.now = [10000.0]
        self.scope = ContextScope(str(Path(self.temp.name)/'profile'), 'napcat', 'group:20001')
        self.other = ContextScope(self.scope.profile_home, 'napcat', 'group:20002')
        self.state = self.make_state()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def make_state(self, **kwargs):
        return DialogueState(self.path,clock=lambda:self.now[0],**kwargs)

    def incoming(self, mid, actor='10001', *, scope=None, text='合成公开消息'):
        scope = scope or self.scope
        item = event(text,mid=mid,chat=scope.chat_id,user=actor,name='不应存入文件的昵称')
        item.reply_expected = False
        self.state.observe_event(scope,item)
        return item

    def wait(self, mid, *, anchor=None, scope=None, reason='question'):
        return self.state.assess(scope or self.scope,mid,topic_anchor=anchor,
            action='wait',reason=reason,audience='group')

    def test_wait_is_preserved_for_a_later_fresh_trigger(self):
        original = self.incoming('question')
        topic = self.wait('question')
        self.assertEqual(self.state.pending(self.scope,original).opportunities,())
        next_turn = self.incoming('new',actor='10002')
        view = self.state.pending(self.scope,next_turn)
        self.assertTrue(view.current())
        self.assertIs(view.trigger,next_turn)
        self.assertIs(view.source,next_turn.source)
        self.assertEqual([offer.message_id for offer in view.opportunities],['question'])
        self.assertEqual(view.opportunities[0].topic_id,topic.topic_id)
        self.assertFalse(hasattr(view.opportunities[0],'source'))

    def test_unselected_response_is_deferred_but_selected_is_consumed(self):
        self.incoming('share')
        self.state.assess(self.scope,'share',action='respond',audience='group',reason='sharing')
        newer = self.incoming('next')
        self.assertEqual(self.state.pending(self.scope,newer).opportunities[0].status,'deferred')
        self.state.assess(self.scope,'share',action='respond',audience='group',reason='sharing',selected=True)
        self.assertEqual(self.state.pending(self.scope,newer).opportunities,())

    def test_observed_elsewhere_or_no_contribution_is_not_deferred(self):
        self.incoming('directed')
        self.state.assess(self.scope,'directed',audience='member',reason='addressed_elsewhere')
        self.incoming('echo')
        self.state.assess(self.scope,'echo',reason='no_contribution')
        newer = self.incoming('next')
        self.assertEqual(self.state.pending(self.scope,newer).opportunities,())

    def test_two_interleaved_topics_preserve_independent_awaiting_questions(self):
        self.incoming('a-question',actor='10001')
        a = self.wait('a-question')
        self.incoming('b-question',actor='10002')
        b = self.wait('b-question')
        self.incoming('a-followup',actor='10003')
        joined = self.state.assess(self.scope,'a-followup',topic_anchor='a-question',
                                  action='wait',reason='followup',audience='bot')
        self.assertEqual(joined.topic_id,a.topic_id)
        self.assertNotEqual(joined.topic_id,b.topic_id)
        self.assertEqual(joined.participants,('10001','10003'))
        topics = {topic.topic_id:topic for topic in self.state.context(self.scope)}
        self.assertEqual(topics[a.topic_id].awaiting_reply,('a-question',))
        self.assertEqual(topics[b.topic_id].awaiting_reply,('b-question',))

    def test_same_actor_can_start_unrelated_topics(self):
        self.incoming('first')
        first = self.wait('first')
        self.incoming('second')
        second = self.wait('second')
        self.assertNotEqual(first.topic_id,second.topic_id)

    def test_close_and_reopen_are_explicit_semantics_without_word_matching(self):
        self.incoming('question')
        initial = self.wait('question')
        self.incoming('end',text='文案不包含固定结束词')
        closed = self.state.assess(self.scope,'end',topic_anchor='question',
                                  action='observe',reason='followup',audience='group',topic_action='close')
        self.assertEqual(closed.status,'closed')
        self.assertEqual(closed.awaiting_reply,())
        next_turn = self.incoming('unrelated')
        self.assertFalse(self.state.pending(self.scope,next_turn).opportunities)
        self.incoming('reopen',text='文案也不包含固定重开词')
        reopened = self.state.assess(self.scope,'reopen',topic_anchor='question',action='wait',
                                    reason='question',audience='group',topic_action='reopen')
        self.assertEqual(reopened.topic_id,initial.topic_id)
        self.assertEqual(reopened.status,'active')
        self.assertEqual(reopened.awaiting_reply,('reopen',))
        newer = self.incoming('newer')
        self.assertEqual([offer.message_id for offer in self.state.pending(self.scope,newer).opportunities],['reopen'])

    def test_correction_supersedes_old_question_and_keeps_dependency(self):
        self.incoming('old-question')
        initial = self.wait('old-question')
        self.incoming('correction')
        corrected = self.state.assess(self.scope,'correction',topic_anchor='old-question',action='wait',
                                     reason='correction',audience='bot',topic_action='correct')
        self.assertEqual(corrected.topic_id,initial.topic_id)
        self.assertEqual(corrected.awaiting_reply,('correction',))
        newer = self.incoming('next')
        self.assertEqual([offer.message_id for offer in self.state.pending(self.scope,newer).opportunities],['correction'])
        revoked = self.state.revoke(self.scope,{'old-question'})
        self.assertEqual(revoked,frozenset({'old-question','correction'}))

    def test_actual_reply_resolves_only_bound_question_or_explicit_same_topic_refs(self):
        self.incoming('question')
        self.wait('question')
        self.incoming('followup')
        self.state.assess(self.scope,'followup',topic_anchor='question',action='wait',
                          audience='bot',reason='followup')
        result = self.state.record_reply(self.scope,'ack','followup',resolves=['question'])
        self.assertEqual(result.awaiting_reply,())
        self.assertIn('ack',result.message_ids)
        newer = self.incoming('next')
        self.assertFalse(self.state.pending(self.scope,newer).opportunities)

    def test_reply_cannot_resolve_another_topic_and_is_atomic(self):
        self.incoming('a')
        self.wait('a')
        self.incoming('b')
        self.wait('b')
        with self.assertRaises(ValueError):
            self.state.record_reply(self.scope,'ack','a',resolves=['b'])
        self.assertFalse(any('ack' in topic.message_ids for topic in self.state.context(self.scope)))

    def test_future_unknown_and_revoked_topic_anchors_are_rejected(self):
        self.incoming('first')
        self.incoming('future')
        for anchor in ['future','missing']:
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                self.state.assess(self.scope,'first',topic_anchor=anchor)
        self.state.revoke(self.scope,{'future'})
        with self.assertRaises(ValueError):
            self.state.assess(self.scope,'first',topic_anchor='future')

    def test_profile_platform_chat_and_dm_scopes_are_independent(self):
        scopes = [self.scope,self.other,
            ContextScope(self.scope.profile_home,'other-platform','group:20001'),
            ContextScope(self.scope.profile_home+'-other','napcat','group:20001'),
            ContextScope(self.scope.profile_home,'napcat','dm:10001','10001')]
        topics = []
        for scope in scopes:
            self.incoming('same-id',scope=scope)
            topics.append(self.wait('same-id',scope=scope).topic_id)
        self.assertEqual(len(set(topics)),len(scopes))
        self.state.revoke(self.scope,{'same-id'})
        self.assertEqual(self.state.context(self.scope),())
        for scope in scopes[1:]:
            self.assertEqual(len(self.state.context(scope)),1)

    def test_pending_cannot_accept_copied_event_or_source_or_other_scope(self):
        self.incoming('old')
        self.wait('old')
        trigger = self.incoming('fresh')
        with self.assertRaises(ValueError):
            self.state.pending(self.scope,copy.copy(trigger))
        with self.assertRaises(ValueError):
            self.state.pending(self.other,trigger)
        original = trigger.source
        trigger.source = copy.copy(original)
        with self.assertRaises(ValueError):
            self.state.pending(self.scope,trigger)
        trigger.source = original
        view = self.state.pending(self.scope,trigger)
        trigger.source.chat_id = self.other.chat_id
        self.assertFalse(view.current())

    def test_new_source_not_metadata_observation_is_required(self):
        self.state.observe(self.scope,'known','10001')
        trigger = event(mid='known')
        with self.assertRaises(ValueError):
            self.state.pending(self.scope,trigger)
        with self.assertRaises(ValueError):
            self.state.observe_event(self.scope,trigger)

    def test_restart_and_disconnect_require_a_new_real_message(self):
        old = self.incoming('old')
        self.wait('old')
        before = self.incoming('before')
        view = self.state.pending(self.scope,before)
        self.state.disconnect(self.scope)
        self.assertFalse(view.current())
        with self.assertRaises(ValueError):
            self.state.pending(self.scope,before)
        self.state.close()
        self.state = self.make_state()
        for stale in (old,before):
            with self.assertRaises(ValueError):
                self.state.observe_event(self.scope,stale)
        fresh = self.incoming('after')
        restored = self.state.pending(self.scope,fresh)
        self.assertTrue(restored.current())
        self.assertIs(restored.source,fresh.source)
        self.assertEqual([offer.message_id for offer in restored.opportunities],['old'])

    def test_expired_binding_needs_new_event_and_opportunities_expire(self):
        self.state.close()
        self.state = self.make_state(topic_ttl=120,opportunity_ttl=90)
        self.incoming('old')
        self.wait('old')
        trigger = self.incoming('fresh')
        view = self.state.pending(self.scope,trigger)
        self.now[0] += 61
        self.assertFalse(view.current())
        with self.assertRaises(ValueError):
            self.state.pending(self.scope,trigger)
        self.now[0] += 30
        newer = self.incoming('newer')
        self.assertEqual(self.state.pending(self.scope,newer).opportunities,())
        self.now[0] += 121
        self.assertEqual(self.state.context(self.scope),())

    def test_reassessing_does_not_extend_original_opportunity_deadline(self):
        self.incoming('old')
        self.wait('old')
        self.incoming('fresh')
        first = self.state.pending(self.scope,self.incoming('one')).opportunities[0]
        self.now[0] += 30
        self.wait('old')
        later = self.state.pending(self.scope,self.incoming('two')).opportunities[0]
        self.assertEqual(first.expires_at,later.expires_at)

    def test_revocation_invalidates_dependent_views_but_preserves_other_chat(self):
        self.incoming('root')
        self.wait('root')
        self.incoming('derived')
        self.wait('derived',anchor='root')
        trigger = self.incoming('fresh')
        view = self.state.pending(self.scope,trigger)
        self.incoming('root',scope=self.other)
        other_topic = self.wait('root',scope=self.other)
        self.assertEqual(self.state.revoke(self.scope,{'root'}),frozenset({'root','derived'}))
        self.assertFalse(view.current())
        self.assertEqual(self.state.context(self.other)[0].topic_id,other_topic.topic_id)
        self.assertEqual(self.state.revoke(self.scope,{'root'}),frozenset())

    def test_restoring_old_content_cannot_restore_forgotten_sources(self):
        self.incoming('root')
        self.wait('root')
        self.incoming('derived')
        self.wait('derived',anchor='root')
        self.incoming('unrelated',scope=self.other)
        backup = self.path.read_bytes()
        self.state.revoke(self.scope,{'root'})
        self.state.close()
        self.path.write_bytes(backup)
        self.state = self.make_state()
        self.assertEqual(self.state.context(self.scope),())
        self.assertEqual(len(self.state.context(self.other)),1)
        with self.assertRaises(ValueError):
            self.state.observe(self.scope,'derived','10001')

    def test_rollback_discovers_previously_compacted_descendants(self):
        self.incoming('root')
        self.incoming('derived')
        self.wait('derived',anchor='root')
        backup = self.path.read_bytes()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM messages WHERE id=?',('derived',))
        self.state.revoke(self.scope,{'root'})
        self.state.close()
        self.path.write_bytes(backup)
        self.state = self.make_state()
        self.assertEqual(self.state.context(self.scope),())
        with self.assertRaises(ValueError):
            self.state.observe(self.scope,'derived','10001')

    def test_capacity_bounds_topics_messages_and_pending(self):
        self.state.close()
        self.state = self.make_state(max_topics=4,max_messages=8,max_pending=2)
        for index in range(6):
            self.incoming('m'+str(index))
            self.wait('m'+str(index))
        self.assertLessEqual(len(self.state.context(self.scope)),4)
        newer = self.incoming('fresh')
        self.assertEqual(len(self.state.pending(self.scope,newer).opportunities),2)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertLessEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],8)

    def test_active_root_is_preserved_when_message_budget_is_full(self):
        self.state.close()
        self.state = self.make_state(max_topics=2,max_messages=5,max_pending=2)
        self.incoming('root')
        for index in range(20):
            mid = 'followup'+str(index)
            self.incoming(mid)
            self.wait(mid,anchor='root')
        topic = self.state.context(self.scope)[0]
        self.assertEqual(topic.anchor_id,'root')
        self.assertIn('root',topic.message_ids)
        self.assertLessEqual(len(topic.message_ids),5)

    def test_burst_over_topic_capacity_preserves_waiting_topic_and_new_observation(self):
        self.state.close()
        self.state = self.make_state(max_topics=3,max_messages=12,max_pending=3)
        self.incoming('waiting')
        pending_topic = self.wait('waiting')
        for index in range(25):
            snapshot = self.state.observe(self.scope,'noise'+str(index),'10002')
            self.assertIn('noise'+str(index),snapshot.message_ids)
            self.assertLessEqual(len(self.state.context(self.scope)),3)
        self.assertIn(pending_topic.topic_id,{topic.topic_id for topic in self.state.context(self.scope)})
        trigger = self.incoming('new-trigger')
        self.assertEqual([offer.message_id for offer in self.state.pending(self.scope,trigger).opportunities],['waiting'])

    def test_all_waiting_topics_at_capacity_still_accepts_new_observation(self):
        self.state.close()
        self.state = self.make_state(max_topics=3,max_messages=12,max_pending=3)
        for index in range(3):
            self.incoming('waiting'+str(index))
            self.wait('waiting'+str(index))
        newest = self.state.observe(self.scope,'newest','10002')
        self.assertIn('newest',newest.message_ids)
        self.assertEqual(len(self.state.context(self.scope)),3)

    def test_mutated_event_actor_or_id_invalidates_reconsideration(self):
        self.incoming('waiting')
        self.wait('waiting')
        trigger = self.incoming('trigger')
        view = self.state.pending(self.scope,trigger)
        trigger.user_id = '10002'
        self.assertFalse(view.current())
        trigger.user_id = '10001'
        self.assertTrue(view.current())
        trigger.message_id = 'forged'
        self.assertFalse(view.current())

    def test_body_nickname_and_send_capability_are_never_persisted(self):
        item = self.incoming('raw-id',text='RAW_MESSAGE_CANARY_MUST_NOT_PERSIST')
        self.wait('raw-id')
        self.incoming('fresh')
        for path in (self.path,self.state.control_path):
            data = path.read_bytes()
            self.assertNotIn(b'RAW_MESSAGE_CANARY_MUST_NOT_PERSIST',data)
            self.assertNotIn('不应存入文件的昵称'.encode(),data)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertFalse(hasattr(self.state,'send'))
        self.assertIsNotNone(item.source)

    def test_identity_conflict_and_unsafe_storage_are_rejected(self):
        self.incoming('same')
        with self.assertRaises(ValueError):
            self.state.observe(self.scope,'same','different')
        unsafe = Path(self.temp.name)/'unsafe.sqlite3'
        unsafe.write_bytes(b'')
        unsafe.chmod(0o644)
        with self.assertRaises(ValueError):
            DialogueState(unsafe)
        linked = Path(self.temp.name)/'linked.sqlite3'
        linked.symlink_to(self.path)
        with self.assertRaises(ValueError):
            DialogueState(linked)

    def test_invalid_semantic_changes_do_not_mutate_state(self):
        self.incoming('source')
        initial = self.state.context(self.scope)
        for changes in ({'topic_action':'invent'}, {'action':'respond','audience':'member'},
                        {'action':'respond','audience':'group','reason':'closing'}, {'selected':True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.state.assess(self.scope,'source',**changes)
        self.assertEqual(self.state.context(self.scope),initial)

    def test_reference_metadata_preserves_real_actor_time_role_and_reply_only(self):
        self.incoming('original',actor='10002',text='RAW_BODY_CANARY')
        observed = self.now[0]
        self.now[0] += 30
        self.incoming('model-linked',actor='10003')
        self.wait('model-linked',anchor='original')
        self.state.record_reply(self.scope,'actual-ack','model-linked',actor_id='90001')
        rows = {row['message_id']:row for row in self.state.reference_metadata(
            self.scope,['original','model-linked','actual-ack'])}
        self.assertEqual(rows['original']['actor_id'],'10002')
        self.assertEqual(rows['original']['observed_at'],observed)
        self.assertEqual(rows['original']['role'],'member')
        self.assertEqual(rows['model-linked']['reply_to'],'')
        self.assertEqual(rows['actual-ack']['role'],'self')
        self.assertEqual(rows['actual-ack']['reply_to'],'model-linked')
        self.assertNotIn('RAW_BODY_CANARY',str(rows))
        reply = event('真实引用回复',mid='verified',user='10003')
        reply.reply_to_message_id = 'actual-ack'
        reply.verified_reply_author_id = '90001'
        self.state.observe_event(self.scope,reply)
        self.assertEqual(self.state.reference_metadata(self.scope,['verified'])[0]['reply_to'],'actual-ack')

    def test_reference_metadata_is_bounded_exact_scope_revocable_and_expiring(self):
        self.state.close()
        self.state = self.make_state(topic_ttl=60,opportunity_ttl=30)
        self.incoming('original')
        self.incoming('other-only',scope=self.other)
        self.assertEqual(self.state.reference_metadata(self.scope,['other-only','unknown']),())
        with self.assertRaises(ValueError):
            self.state.reference_metadata(self.scope,['original']*17)
        self.state.revoke(self.scope,{'original'})
        self.assertEqual(self.state.reference_metadata(self.scope,['original']),())
        self.now[0] += 61
        self.assertEqual(self.state.reference_metadata(self.other,['other-only']),())

    def test_unquoted_delivery_resolves_logical_topic_without_fabricating_qq_quote(self):
        self.incoming('question')
        self.wait('question')
        resolved = self.state.record_reply(self.scope,'outbound','question',quoted=False)
        self.assertEqual(resolved.awaiting_reply,())
        self.assertEqual(self.state.reference_metadata(self.scope,['outbound'])[0]['reply_to'],'')
        self.assertEqual(self.state.revoke(self.scope,{'question'}),frozenset({'question','outbound'}))

    def test_repeated_reply_cannot_change_whether_transport_used_a_quote(self):
        self.incoming('question')
        self.wait('question')
        self.state.record_reply(self.scope,'outbound','question',quoted=False)
        self.state.record_reply(self.scope,'outbound','question',quoted=False)
        with self.assertRaises(ValueError):
            self.state.record_reply(self.scope,'outbound','question',quoted=True)
        with self.assertRaises(ValueError):
            self.state.record_reply(self.scope,'another','question',quoted='no')


if __name__ == '__main__':
    unittest.main()
