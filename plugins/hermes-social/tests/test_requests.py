"""Authored request metadata, SQLite recovery and source binding regressions.

All inputs are independent synthetic tasks. No model, QQ, real messages or
runtime configuration is used; channel-verified ACKs are explicit fixtures.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

import support
from support import event
from hermes_social.context import ContextScope
from hermes_social.requests import RequestLedger


class RequestLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.path = Path(self.temp.name)/'requests.sqlite3'
        self.scope = ContextScope(str(Path(self.temp.name)/'profile'),'napcat','group:20001')
        self.other = ContextScope(self.scope.profile_home,'napcat','group:20002')
        self.now = [1000.0]
        self.ledger = self.make()

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def make(self, **kwargs):
        return RequestLedger(self.path,clock=lambda:self.now[0],**kwargs)

    def incoming(self,mid,actor='10001',*,scope=None,text='合成纸卡任务资料'):
        scope = scope or self.scope
        item = event(text,mid=mid,user=actor,chat=scope.chat_id,name='SYNTHETIC_NAME_CANARY')
        self.ledger.observe_event(scope,item)
        return item

    def new(self,mid='root-a',actor='10001',*,scope=None,topic=None):
        scope = scope or self.scope
        item = self.incoming(mid,actor,scope=scope)
        return item,self.ledger.assess(scope,item,kind='new',topic_anchor=topic)

    def one(self,root='root-a',*,scope=None):
        return next(item for item in self.ledger.context(scope or self.scope) if item.request_id==root)

    def backup(self):
        backup = Path(self.temp.name)/'content-backup.sqlite3'
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        return backup

    def restore(self,backup):
        with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(self.path)) as target:
            source.backup(target)

    def test_same_topic_retains_two_independent_request_owners(self):
        _,first = self.new()
        _,second = self.new('root-b','10002',topic=first.request_id)
        self.assertEqual(first.topic_anchor,second.topic_anchor)
        self.assertNotEqual(first.request_id,second.request_id)
        self.assertEqual((first.owner_id,second.owner_id),('10001','10002'))
        self.assertEqual(first.version,first.revision)
        self.assertEqual(first.row()['owner_id'],'10001')

    def test_foreign_supplement_and_adoption_keep_original_provider(self):
        self.new()
        contributor = self.incoming('condition-b','10002')
        pending = self.ledger.assess(self.scope,contributor,kind='supplement',request_id='root-a')
        condition = next(item for item in pending.conditions if item.source_id=='condition-b')
        self.assertEqual(condition.provider_id,'10002')
        self.assertEqual(condition.adopted_by,())
        adopting = self.incoming('adoption-a')
        result = self.ledger.assess(self.scope,adopting,kind='adopt',request_id='root-a',
                                    condition_ids=['condition-b'])
        adopted = next(item for item in result.conditions if item.source_id=='condition-b')
        self.assertEqual(adopted.provider_id,'10002')
        self.assertEqual(adopted.adopted_by,('10001',))
        self.assertEqual(result.owner_id,'10001')

    def test_new_request_imports_foreign_conditions_only_when_explicitly_listed(self):
        self.new('root-b','10002')
        _,independent=self.new('independent-a')
        self.assertNotIn('root-b',independent.condition_ids)
        current=self.incoming('adopting-new-a')
        result=self.ledger.assess(self.scope,current,kind='new',condition_ids=['root-b'])
        condition=next(item for item in result.conditions if item.source_id=='root-b')
        self.assertEqual(condition.provider_id,'10002')
        self.assertEqual(condition.adopted_by,('10001',))
        self.assertEqual(condition.relation,'adopted')

    def test_other_member_cannot_adopt_close_cancel_or_reopen_for_owner(self):
        self.new()
        contribution = self.incoming('other-contribution','10002')
        for kind in ('adopt','close','cancel','reopen'):
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                self.ledger.assess(self.scope,contribution,kind=kind,request_id='root-a',
                                    condition_ids=['root-a'] if kind=='adopt' else ())
        self.assertEqual(self.one().state,'pending')

    def test_continue_correction_and_explicit_owner_reopen_preserve_owner(self):
        self.new()
        continuing = self.incoming('continue-b','10002')
        self.ledger.assess(self.scope,continuing,kind='continue',request_id='root-a')
        correcting = self.incoming('correction-c','10003')
        result = self.ledger.assess(self.scope,correcting,kind='correct',request_id='root-a',
                                   supersedes=['root-a'])
        condition = next(row for row in result.conditions if row.source_id=='correction-c')
        self.assertEqual(condition.provider_id,'10003')
        self.assertEqual(condition.supersedes,('root-a',))
        self.assertEqual(result.owner_id,'10001')
        closing = self.incoming('close-a')
        self.assertEqual(self.ledger.assess(self.scope,closing,kind='close',request_id='root-a').state,'closed')
        continued = self.incoming('later-a')
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,continued,kind='continue',request_id='root-a')
        opened = self.ledger.assess(self.scope,continued,kind='reopen',request_id='root-a')
        self.assertEqual(opened.state,'pending')
        self.assertEqual(opened.owner_id,'10001')

    def test_classification_is_idempotent_and_cannot_change_source_in_place(self):
        root,initial = self.new()
        self.assertEqual(self.ledger.assess(self.scope,root,kind='new'),initial)
        waiting = self.incoming('wait-a')
        first = self.ledger.assess(self.scope,waiting,kind='wait',request_id='root-a')
        self.assertEqual(self.ledger.assess(self.scope,waiting,kind='wait',request_id='root-a'),first)
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,waiting,kind='close',request_id='root-a')

    def test_repeated_source_cannot_silently_change_adopted_condition_set(self):
        self.new('condition-b','10002')
        self.new('condition-c','10003')
        current=self.incoming('owner-new')
        first=self.ledger.assess(self.scope,current,kind='new',condition_ids=['condition-b'])
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,current,kind='new',condition_ids=['condition-c'])
        self.assertEqual(self.one('owner-new'),first)

    def test_future_unknown_cross_scope_and_wrong_actor_are_rejected(self):
        early = self.incoming('early')
        self.new('future','10002')
        self.new('other','10002',scope=self.other)
        for ref in ('future','missing','other'):
            with self.subTest(ref=ref),self.assertRaises(ValueError):
                self.ledger.assess(self.scope,early,kind='new',topic_anchor=ref)
        forged = self.incoming('forged')
        forged.user_id='10003'
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,forged,kind='new')

    def test_expired_and_cloned_ingress_cannot_assess_or_bind(self):
        original,_ = self.new()
        clone=copy.copy(original)
        clone.source=copy.copy(original.source)
        with self.assertRaises(ValueError):
            self.ledger.bind_reply(self.scope,clone,['root-a'])
        self.now[0]+=301
        self.ledger.observe_event(self.scope,original)
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,original,kind='new')

    def test_trusted_native_wrapper_keeps_the_original_observation_deadline(self):
        original,_ = self.new()
        wrapped=copy.copy(original)
        wrapped.source=copy.copy(original.source)
        self.ledger.trusted_ingress=lambda scope,item: scope==self.scope and item is wrapped
        self.now[0]+=20
        plan=self.ledger.bind_reply(self.scope,wrapped,['root-a'])
        self.assertTrue(plan.current())
        self.assertEqual(plan.expires_at,1300.0)
        clone=copy.copy(wrapped)
        with self.assertRaises(ValueError):
            self.ledger.bind_reply(self.scope,clone,['root-a'])
        self.now[0]=1300
        self.assertFalse(plan.current())

    def test_trusted_callback_cannot_restore_unobserved_or_restart_history(self):
        original,_=self.new()
        self.ledger.close()
        self.ledger=self.make(trusted_ingress=lambda scope,item: True)
        with self.assertRaises(ValueError):
            self.ledger.bind_reply(self.scope,original,['root-a'])
        with self.assertRaises(ValueError):
            self.ledger.observe_event(self.scope,copy.copy(original))

    def test_only_actual_recorded_ack_completes_explicit_covers(self):
        first,_=self.new()
        self.new('root-b','10002',topic='root-a')
        current=self.incoming('fresh-a')
        plan=self.ledger.bind_reply(self.scope,current,['root-a','root-b'],mode='combined')
        self.assertTrue(plan.current())
        self.assertEqual(self.one().state,'pending')
        self.ledger.record_reply(plan,'ack-one',completed_ids=['root-a'])
        self.assertEqual(self.one().state,'responded')
        self.assertEqual(self.one('root-b').state,'pending')
        self.assertTrue(plan.current())

    def test_clarifying_ack_waits_without_claiming_request_complete(self):
        first,_=self.new()
        plan=self.ledger.bind_reply(self.scope,first,['root-a'],mode='clarify')
        self.ledger.record_reply(plan,'clarification-ack')
        self.assertEqual(self.one().state,'waiting')
        self.assertTrue(plan.current())

    def test_a_later_actual_fragment_can_upgrade_waiting_to_completed(self):
        first,_=self.new()
        plan=self.ledger.bind_reply(self.scope,first,['root-a'],mode='clarify')
        self.ledger.record_reply(plan,'first-verified-fragment')
        waiting_revision=self.one().revision
        self.ledger.record_reply(plan,'first-verified-fragment',completed_ids=['root-a'],waiting_ids=())
        self.assertEqual(self.one().state,'waiting')
        self.ledger.record_reply(plan,'second-verified-fragment',completed_ids=['root-a'],waiting_ids=())
        self.assertEqual(self.one().state,'responded')
        self.assertEqual(self.one().revision,waiting_revision+1)
        self.assertTrue(plan.current())

    def test_outcome_sets_cannot_exceed_plan_or_change_settled_fragment(self):
        first,_=self.new()
        self.new('root-b','10002')
        current=self.incoming('fresh-a')
        plan=self.ledger.bind_reply(self.scope,current,['root-a'])
        for completed,waiting in ((['root-b'],()),(['root-a'],['root-a'])):
            with self.assertRaises(ValueError):
                self.ledger.record_reply(plan,'bad-ack',completed_ids=completed,waiting_ids=waiting)
        self.assertEqual(self.one().state,'pending')
        self.ledger.record_reply(plan,'real-ack',completed_ids=['root-a'])
        revision=self.one().revision
        self.ledger.record_reply(plan,'real-ack',completed_ids=['root-a'])
        self.ledger.record_reply(plan,'next-fragment',completed_ids=['root-a'])
        self.assertEqual(self.one().revision,revision)
        with self.assertRaises(ValueError):
            self.ledger.record_reply(plan,'invalid-fragment',completed_ids=(),waiting_ids=['root-a'])
        self.assertTrue(plan.current())

    def test_other_new_information_invalidates_frozen_plan_without_completing_it(self):
        first,_=self.new()
        plan=self.ledger.bind_reply(self.scope,first,['root-a'])
        supplement=self.incoming('additional','10002')
        self.ledger.assess(self.scope,supplement,kind='supplement',request_id='root-a')
        self.assertFalse(plan.current())
        with self.assertRaises(ValueError):
            self.ledger.record_reply(plan,'late-ack')
        self.assertEqual(self.one().state,'pending')

    def test_forget_winning_between_plan_check_and_ack_transaction_rejects_late_fragment(self):
        self.new()
        contribution=self.incoming('condition-b','10002')
        self.ledger.assess(self.scope,contribution,kind='supplement',request_id='root-a')
        current=self.incoming('fresh-a')
        plan=self.ledger.bind_reply(self.scope,current,['root-a'])
        original=self.ledger.plan_current
        def racing_check(value):
            valid=original(value)
            self.ledger.revoke(self.scope,['condition-b'])
            return valid
        self.ledger.plan_current=racing_check
        with self.assertRaises(ValueError):
            self.ledger.record_reply(plan,'late-fragment',completed_ids=(),waiting_ids=())
        self.assertFalse(self.ledger.reference_metadata(self.scope,['late-fragment']))

    def test_plan_copies_mutation_release_and_external_guard_are_rejected(self):
        first,_=self.new()
        allowed=[True]
        plan=self.ledger.bind_reply(self.scope,first,['root-a'],valid=lambda:allowed[0])
        self.assertFalse(copy.copy(plan).current())
        allowed[0]=False
        self.assertFalse(plan.current())
        allowed[0]=True
        first.source.user_id='10003'
        self.assertFalse(plan.current())
        first.source.user_id='10001'
        self.assertTrue(plan.current())
        self.ledger.release(plan)
        self.assertFalse(plan.current())

    def test_forget_propagates_to_adoption_all_fragments_and_preserves_other_task(self):
        self.new()
        independent,_=self.new('root-b','10002')
        adopting=self.incoming('adopt-a')
        self.ledger.assess(self.scope,adopting,kind='adopt',request_id='root-a',condition_ids=['root-b'])
        plan=self.ledger.bind_reply(self.scope,adopting,['root-a'])
        self.ledger.record_reply(plan,'fragment-one')
        self.ledger.record_reply(plan,'fragment-two')
        revoked=self.ledger.revoke(self.scope,['root-a'])
        self.assertTrue({'root-a','adopt-a','fragment-one','fragment-two'}<=revoked)
        self.assertNotIn('root-b',revoked)
        self.assertEqual(self.one('root-b').owner_id,'10002')
        self.assertFalse(plan.current())

    def test_forgetting_only_condition_reopens_owner_request_and_revokes_derivatives(self):
        self.new()
        contributor=self.incoming('condition-b','10002')
        self.ledger.assess(self.scope,contributor,kind='supplement',request_id='root-a')
        adopting=self.incoming('adopt-a')
        self.ledger.assess(self.scope,adopting,kind='adopt',request_id='root-a',condition_ids=['condition-b'])
        plan=self.ledger.bind_reply(self.scope,adopting,['root-a'])
        self.ledger.record_reply(plan,'derived')
        revoked=self.ledger.revoke(self.scope,['condition-b'])
        self.assertTrue({'condition-b','adopt-a','derived'}<=revoked)
        self.assertNotIn('root-a',revoked)
        result=self.one()
        self.assertEqual(result.state,'pending')
        self.assertNotIn('condition-b',result.condition_ids)
        with self.assertRaises(ValueError):
            self.ledger.record_reply(plan,'late-derivative')

    def test_content_restore_cannot_revive_forgotten_condition_or_derivative(self):
        self.new()
        contributor=self.incoming('condition-b','10002')
        self.ledger.assess(self.scope,contributor,kind='supplement',request_id='root-a')
        current=self.incoming('fresh-a')
        plan=self.ledger.bind_reply(self.scope,current,['root-a'])
        self.ledger.record_reply(plan,'derived')
        backup=self.backup()
        self.ledger.revoke(self.scope,['condition-b'])
        self.restore(backup)
        metadata=self.ledger.reference_metadata(self.scope,['condition-b','derived'])
        self.assertFalse(metadata)
        self.assertEqual(self.one().state,'pending')
        self.assertFalse(plan.current())

    def test_content_restore_cannot_reopen_answered_or_cancelled_request(self):
        original,_=self.new()
        backup=self.backup()
        plan=self.ledger.bind_reply(self.scope,original,['root-a'])
        self.ledger.record_reply(plan,'verified-answer')
        self.restore(backup)
        self.assertEqual(self.one().state,'responded')
        later=self.incoming('owner-cancel')
        self.ledger.assess(self.scope,later,kind='cancel',request_id='root-a')
        self.restore(backup)
        self.assertEqual(self.one().state,'cancelled')

    def test_content_restore_after_owner_reopen_cannot_revive_an_old_plan(self):
        original,_=self.new()
        old_plan=self.ledger.bind_reply(self.scope,original,['root-a'])
        backup=self.backup()
        closing=self.incoming('owner-close')
        self.ledger.assess(self.scope,closing,kind='close',request_id='root-a')
        reopening=self.incoming('owner-reopen')
        latest=self.ledger.assess(self.scope,reopening,kind='reopen',request_id='root-a')
        self.restore(backup)
        self.assertEqual(self.one().revision,latest.revision)
        self.assertEqual(self.one().state,'pending')
        self.assertFalse(old_plan.current())

    def test_restart_retains_metadata_but_not_live_event_or_plan(self):
        original,initial=self.new()
        plan=self.ledger.bind_reply(self.scope,original,['root-a'])
        self.ledger.close()
        self.ledger=self.make()
        self.assertEqual(self.one(),initial)
        self.assertFalse(plan.current())
        fresh=self.incoming('fresh-after-restart','10002')
        recovered=self.ledger.bind_reply(self.scope,fresh,['root-a'])
        self.assertTrue(recovered.current())
        self.ledger.reset_connection()
        self.assertFalse(recovered.current())

    def test_concurrent_instances_do_not_lose_request_revisions_or_contributors(self):
        self.new()
        other=self.make()
        first=self.incoming('concurrent-b','10002')
        second=event('另一条合成条件',mid='concurrent-c',user='10003')
        other.observe_event(self.scope,second)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(ledger.assess,self.scope,item,kind='supplement',request_id='root-a')
                         for ledger,item in ((self.ledger,first),(other,second))]
                for future in futures:
                    future.result(timeout=5)
            result=self.one()
            self.assertEqual(result.revision,3)
            self.assertTrue({'concurrent-b','concurrent-c'}<=set(result.condition_ids))
        finally:
            other.close()

    def test_scope_includes_profile_platform_chat_and_exact_dm_subject(self):
        scopes=[self.scope,self.other,
            ContextScope(self.scope.profile_home+'-other','napcat','group:20001'),
            ContextScope(self.scope.profile_home,'other-platform','group:20001'),
            ContextScope(self.scope.profile_home,'napcat','dm:10001','10001'),
            ContextScope(self.scope.profile_home,'napcat','dm:10002','10002')]
        for scope in scopes:
            actor=scope.subject_id or '10001'
            self.new('same-root',actor,scope=scope)
        self.ledger.revoke(self.scope,['same-root'])
        self.assertFalse(self.ledger.context(self.scope))
        for scope in scopes[1:]:
            self.assertEqual(self.one('same-root',scope=scope).owner_id,scope.subject_id or '10001')
        bad=event('合成私聊',mid='bad-dm',user='10003',chat='dm:10001')
        with self.assertRaises(ValueError):
            self.ledger.observe_event(scopes[-2],bad)

    def test_ttl_and_bounds_evict_without_dropping_current_observation(self):
        self.ledger.close()
        self.ledger=self.make(ttl=60,max_requests=2,max_messages=4)
        self.new('one')
        self.now[0]+=1
        self.new('two')
        self.now[0]+=1
        latest,_=self.new('three')
        self.assertEqual({item.request_id for item in self.ledger.context(self.scope)},{'two','three'})
        for index in range(8):
            self.incoming('noise-'+str(index),'10003')
        self.assertEqual(self.one('three').owner_id,'10001')
        self.assertLessEqual(len(self.ledger.reference_metadata(self.scope,
            ['one','two','three',*('noise-'+str(i) for i in range(8))])),4)
        self.now[0]+=61
        self.assertFalse(self.ledger.context(self.scope))
        self.assertFalse(self.ledger.reference_metadata(self.scope,['one','two','three','noise-7']))

    def test_database_has_only_metadata_and_explicit_file_permissions(self):
        current=self.incoming('synthetic-root',text='SYNTHETIC_BODY_CANARY')
        self.ledger.assess(self.scope,current,kind='new')
        for path in (self.path,self.ledger.control_path):
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            content=path.read_bytes()
            self.assertNotIn(b'SYNTHETIC_BODY_CANARY',content)
            self.assertNotIn(b'SYNTHETIC_NAME_CANARY',content)

    def test_ordinary_verified_reply_is_a_topic_reference_without_completing_request(self):
        original,_=self.new()
        self.ledger.observe_reply(self.scope,'ordinary-bot',reply_to='root-a')
        self.assertEqual(self.one().state,'pending')
        self.assertEqual(self.ledger.reference_metadata(self.scope,['ordinary-bot'])[0]['role'],'self')
        current=self.incoming('new-topic','10002')
        new=self.ledger.assess(self.scope,current,kind='new',topic_anchor='ordinary-bot')
        self.assertEqual(new.topic_anchor,'ordinary-bot')
        self.assertEqual(new.owner_id,'10002')

    def test_observe_reply_is_idempotent_after_plan_recording_and_rejects_actor_collision(self):
        original,_=self.new()
        plan=self.ledger.bind_reply(self.scope,original,['root-a'])
        self.ledger.record_reply(plan,'planned-ack')
        revision=self.one().revision
        self.ledger.observe_reply(self.scope,'planned-ack',reply_to='root-a',dependencies=['root-a'])
        self.ledger.observe_reply(self.scope,'planned-ack',reply_to='root-a')
        self.assertEqual(self.one().revision,revision)
        with self.assertRaises(ValueError):
            self.ledger.observe_reply(self.scope,'root-a')
        self.incoming('different-source','10002')
        with self.assertRaises(ValueError):
            self.ledger.observe_reply(self.scope,'planned-ack',reply_to='different-source')

    def test_observed_reply_dependencies_revoke_even_without_a_reply_plan(self):
        source=self.incoming('root-source')
        self.ledger.observe_reply(self.scope,'unplanned-ack',reply_to=source.message_id)
        self.ledger.observe_reply(self.scope,'derived-ack',dependencies=['unplanned-ack'])
        revoked=self.ledger.revoke(self.scope,[source.message_id])
        self.assertTrue({'root-source','unplanned-ack','derived-ack'}<=revoked)
        with self.assertRaises(ValueError):
            self.ledger.observe_reply(self.scope,'late-ack',dependencies=['unplanned-ack'])

    def test_unknown_references_and_invalid_bounds_reject_before_mutation(self):
        self.new()
        current=self.incoming('fresh')
        for ids in (['missing'],['root-a']*9):
            with self.assertRaises(ValueError):
                self.ledger.bind_reply(self.scope,current,ids)
        for kwargs in ({'ttl':True},{'fresh_ttl':0},{'max_requests':2,'max_messages':2}):
            with self.assertRaises(ValueError):
                self.make(**kwargs)

    def test_verified_legacy_metadata_imported_after_current_source_can_supply_explicit_conditions(self):
        current=self.incoming('current-new')
        bindings=dict(self.ledger._bindings)
        self.ledger.observe_reference(self.scope,'legacy-condition',actor_id='10002',role='member',observed_at=900.)
        self.ledger.observe_reference(self.scope,'legacy-bot',actor_id='',role='self',observed_at=950.)
        self.assertEqual(self.ledger._bindings,bindings)
        self.assertFalse(self.ledger.context(self.scope))
        result=self.ledger.assess(self.scope,current,kind='new',topic_anchor='legacy-bot',
                                  condition_ids=['legacy-condition'])
        self.assertEqual((result.owner_id,result.topic_anchor),('10001','legacy-bot'))
        imported=next(item for item in result.conditions if item.source_id=='legacy-condition')
        self.assertEqual((imported.provider_id,imported.adopted_by),('10002',('10001',)))
        plan=self.ledger.bind_reply(self.scope,current,['current-new'],dependencies=['legacy-bot'])
        self.assertTrue(plan.current())
        self.assertTrue({'legacy-condition','legacy-bot'}<=plan.message_ids)
        historical=event('旧资料仅可作为资料',mid='legacy-condition',user='10002')
        for candidate in (historical,'legacy-condition'):
            with self.assertRaises(ValueError):
                self.ledger.assess(self.scope,candidate,kind='new')
        with self.assertRaises(ValueError):
            self.ledger.observe_event(self.scope,historical)
        with self.assertRaises(ValueError):
            self.ledger.bind_reply(self.scope,historical,['current-new'])

    def test_reference_import_preserves_existing_timestamp_and_fresh_deadline(self):
        current=self.incoming('fresh-source')
        binding=self.ledger._bindings[(self.ledger._scope(self.scope),'fresh-source')]
        self.now[0]=1100.
        self.ledger.observe_reference(self.scope,'fresh-source',actor_id='10001',role='member',observed_at=1090.)
        self.assertIs(self.ledger._bindings[(self.ledger._scope(self.scope),'fresh-source')],binding)
        self.assertEqual(self.ledger.reference_metadata(self.scope,['fresh-source'])[0]['observed_at'],1000.)
        self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=900.)
        self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=1090.)
        self.assertEqual(self.ledger.reference_metadata(self.scope,['legacy'])[0]['observed_at'],900.)
        self.now[0]=binding[4]+1
        self.ledger.observe_reference(self.scope,'fresh-source',actor_id='10001',role='member',observed_at=1000.)
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,current,kind='new')

    def test_reference_import_rejects_conflicting_identity_scope_role_and_original_time(self):
        self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=900.)
        invalid=[dict(actor_id='10003',role='member',observed_at=900.),
                 dict(actor_id='10002',role='self',observed_at=900.),
                 dict(actor_id='10002',role='unknown',observed_at=900.),
                 dict(actor_id='10002',role=[],observed_at=900.)]
        invalid.extend(dict(actor_id='10002',role='member',observed_at=value)
                       for value in (True,'900',0,-1,float('nan'),float('inf'),1001.,1000.-self.ledger.ttl))
        for arguments in invalid:
            with self.subTest(arguments=arguments),self.assertRaises(ValueError):
                self.ledger.observe_reference(self.scope,'legacy',**arguments)
        dm=ContextScope(self.scope.profile_home,'napcat','dm:10001','10001')
        with self.assertRaises(ValueError):
            self.ledger.observe_reference(dm,'dm-other',actor_id='10002',role='member',observed_at=900.)
        bad_group=ContextScope(self.scope.profile_home,'napcat','group:20001','10001')
        with self.assertRaises(ValueError):
            self.ledger.observe_reference(bad_group,'group-private',actor_id='10001',role='member',observed_at=900.)
        self.ledger.observe_reference(self.other,'other-only',actor_id='10002',role='member',observed_at=900.)
        current=self.incoming('current')
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,current,kind='new',condition_ids=['other-only'])

    def test_readback_original_time_cannot_make_later_data_a_condition_of_an_earlier_source(self):
        current=self.incoming('earlier')
        self.now[0]=1002.
        self.ledger.observe_reference(self.scope,'later',actor_id='10002',role='member',observed_at=1001.)
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,current,kind='new',condition_ids=['later'])
        self.ledger.assess(self.scope,current,kind='new')
        with self.assertRaises(ValueError):
            self.ledger.bind_reply(self.scope,current,['earlier'],dependencies=['later'])

    def test_imported_reference_survives_restart_as_metadata_without_live_ingress(self):
        self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=900.)
        self.ledger.close()
        self.ledger=self.make()
        self.assertFalse(self.ledger._bindings)
        self.assertEqual(self.ledger.reference_metadata(self.scope,['legacy'])[0]['observed_at'],900.)
        with self.assertRaises(ValueError):
            self.ledger.assess(self.scope,'legacy',kind='new')
        current=self.incoming('current')
        self.ledger.assess(self.scope,current,kind='new',condition_ids=['legacy'])
        self.assertTrue(self.ledger.bind_reply(self.scope,current,['current']).current())

    def test_revoked_imported_condition_and_derived_ack_stay_revoked_after_content_rollback(self):
        self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=900.)
        current=self.incoming('current')
        self.ledger.assess(self.scope,current,kind='new',condition_ids=['legacy'])
        plan=self.ledger.bind_reply(self.scope,current,['current'])
        self.ledger.record_reply(plan,'combined-ack')
        backup=self.backup()
        self.assertTrue({'legacy','current','combined-ack'}<=self.ledger.revoke(self.scope,['legacy']))
        self.restore(backup)
        with self.assertRaises(ValueError):
            self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=900.)
        self.assertFalse(self.ledger.reference_metadata(self.scope,['legacy','current','combined-ack']))
        self.assertFalse(self.ledger.context(self.scope))
        self.assertFalse(plan.current())

    def test_expired_original_metadata_cannot_be_reimported_after_restart(self):
        self.ledger.close()
        self.ledger=self.make(ttl=60)
        self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=950.)
        self.now[0]=1011.
        self.ledger.close()
        self.ledger=self.make(ttl=60)
        with self.assertRaises(ValueError):
            self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=950.)
        self.assertFalse(self.ledger.reference_metadata(self.scope,['legacy']))

    def test_readback_budget_never_evicts_fresher_source_to_keep_late_imported_old_metadata(self):
        self.ledger.close()
        self.ledger=self.make(max_requests=1,max_messages=2)
        self.new('root')
        current=self.incoming('current')
        with self.assertRaises(ValueError):
            self.ledger.observe_reference(self.scope,'legacy',actor_id='10002',role='member',observed_at=900.)
        self.assertEqual({row['message_id'] for row in self.ledger.reference_metadata(self.scope,['root','current'])},
                         {'root','current'})
        self.assertEqual(self.ledger.assess(self.scope,current,kind='continue',request_id='root').owner_id,'10001')


if __name__=='__main__':
    unittest.main()
