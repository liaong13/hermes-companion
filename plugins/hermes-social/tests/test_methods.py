"""Synthetic semantic-method lifecycle and evidence tests; no model or QQ I/O."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import support
from hermes_social.methods import MethodStore


CHAT = 'group:20001'
OTHER = 'group:20002'
FRIEND = 'dm:10002'
SECOND_FRIEND = 'dm:10003'


class Clock:
    def __init__(self): self.now = 1800000000.0
    def __call__(self): return self.now
    def step(self, seconds=1): self.now += seconds


class MethodTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.root = Path(self.temp.name)
        self.path = self.root/'methods.sqlite3'
        self.clock = Clock()
        self.evidence = {}
        self.instances = []
        self.store = self.create()
        self.feedback('origin', status='corrected', mid='correction', source='original-out')

    def tearDown(self):
        for instance in self.instances:
            instance.close()
        self.temp.cleanup()

    def resolve(self, chat, ids):
        return [dict(self.evidence[(chat, ident)]) for ident in ids if (chat, ident) in self.evidence]

    def create(self, **overrides):
        options = dict(allowed_chats=[CHAT, OTHER, FRIEND, SECOND_FRIEND],
                       evidence_resolver=self.resolve, clock=self.clock)
        options.update(overrides)
        instance = MethodStore(self.path, **options)
        self.instances.append(instance)
        return instance

    def feedback(self, ident, *, status='replied', chat=CHAT, mid=None, source=None, reference=None):
        row = dict(id=ident, chat=chat, status=status, message_id=mid or 'feedback-'+ident,
                   source_id=source or 'exchange-'+ident, created=self.clock(),
                   text='RAW_TRANSCRIPT_CANARY_NEVER_PERSISTED')
        if reference is not None:
            row['reference'] = reference
        self.evidence[(chat, ident)] = row
        return row

    def propose(self, *, chat=CHAT, **overrides):
        values = dict(category='attribution', condition='合成讨论中有多位资料提供者时',
                      instruction='先核对每条资料的原始作者，再区分建议提出者和采用者。',
                      evidence_ids=['origin'], dependencies=['original-question'],
                      generation_case_ids=['generation-fixture'])
        values.update(overrides)
        return self.store.propose(chat, **values)

    def validate(self, ident, case='new-case-one', *, baseline=False, method=True, **overrides):
        values = dict(validator='deterministic', model='synthetic-route:v1', baseline_model='synthetic-route:v1',
                      budget_tokens=2048, baseline_cost=None, method_cost=None)
        values.update(overrides)
        return self.store.record_validation(CHAT, ident, case, baseline, method, **values)

    def trial(self):
        ident = self.propose()
        self.clock.step()
        self.assertEqual(self.validate(ident)['state'], 'validated')
        self.clock.step()
        self.assertEqual(self.store.start_trial(CHAT, ident)['state'], 'trial')
        return ident

    def retained(self):
        ident = self.trial()
        self.clock.step()
        self.assertEqual(self.validate(ident, 'new-case-two', baseline=True)['state'], 'retained')
        return ident

    def test_proposal_is_advisory_candidate_not_active_context(self):
        ident = self.propose()
        row = self.store.method(CHAT, ident)
        self.assertEqual(row['state'], 'proposed')
        self.assertIn('原始作者', row['instruction'])
        self.assertEqual(row['condition'], '合成讨论中有多位资料提供者时')
        self.assertEqual(self.store.context(CHAT), [])
        self.assertEqual(len(self.store.list_methods(CHAT)), 1)
        with self.assertRaises(PermissionError):
            self.store.start_trial(CHAT, ident)
        with self.assertRaises(PermissionError):
            self.store.record_use(CHAT, ident, 'not-a-real-active-use')

    def test_origin_feedback_and_dependencies_are_bounded_metadata(self):
        ident = self.propose()
        row = self.store.method(CHAT, ident)
        self.assertEqual(set(row['dependencies']), {'origin','correction','original-out','original-question'})
        self.assertIn('origin', row['generation_cases'])
        self.assertNotIn('RAW_TRANSCRIPT_CANARY', self.path.read_bytes().decode(errors='ignore'))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.control_path.stat().st_mode & 0o777, 0o600)

    def test_missing_or_unsupported_feedback_never_forms_semantic_method(self):
        with self.assertRaises(PermissionError): self.propose(evidence_ids=['missing'])
        self.feedback('ordinary', status='replied')
        with self.assertRaises(PermissionError): self.propose(evidence_ids=['ordinary'])
        self.feedback('silent', status='no_response', mid='')
        with self.assertRaises(PermissionError): self.propose(evidence_ids=['silent'])
        empty = self.create(evidence_resolver=None)
        with self.assertRaises(PermissionError):
            empty.propose(CHAT, 'kind','condition','instruction',['origin'])

    def test_resolver_cannot_substitute_chat_duplicate_or_future_evidence(self):
        original = dict(self.evidence[(CHAT,'origin')])
        for update in ({'chat':OTHER}, {'created':self.clock()+30}, {'created':self.clock()-30*86400},
                       {'message_id':''}, {'status':'activity_observed'}, {'id':'forged'}):
            self.evidence[(CHAT,'origin')] = {**original,**update}
            with self.subTest(update=update), self.assertRaises((PermissionError,ValueError)):
                self.propose()

    def test_same_request_key_is_idempotent_but_not_rewritable(self):
        first = self.propose(request_key='same-source-set')
        self.assertEqual(first,self.propose(request_key='same-source-set'))
        with self.assertRaises(PermissionError):
            self.propose(request_key='same-source-set',instruction='另一种合成建议。')

    def test_exact_chat_and_two_friend_isolation_precedes_content_access(self):
        ident = self.propose()
        for chat in (OTHER, FRIEND, SECOND_FRIEND, 'group:99999'):
            with self.subTest(chat=chat), self.assertRaises(PermissionError):
                self.store.method(chat, ident)
        self.feedback('origin', status='corrected', chat=FRIEND)
        friend_method = self.propose(chat=FRIEND)
        with self.assertRaises(PermissionError): self.store.method(SECOND_FRIEND,friend_method)
        self.assertEqual(self.store.list_methods(OTHER), [])

    def test_trial_requires_independent_validation_or_trusted_explicit_approval(self):
        ident = self.propose()
        self.assertEqual(self.validate(ident)['state'], 'validated')
        self.assertEqual(self.store.context(CHAT), [])
        self.clock.step()
        first = self.store.start_trial(CHAT,ident,duration_seconds=600)
        self.clock.step(20)
        second = self.store.start_trial(CHAT,ident,duration_seconds=1000)
        self.assertEqual(first['started'],second['started'])
        self.assertEqual(first['trial_until'],second['trial_until'])
        self.assertEqual(len(self.store.context(CHAT)),1)
        self.assertEqual(self.store.context(CHAT)[0]['provenance'],'chat_scoped_advisory_not_authority')

    def test_explicit_trial_approval_is_a_boolean_trusted_caller_action(self):
        ident = self.propose()
        with self.assertRaises(ValueError): self.store.start_trial(CHAT,ident,approved='true')
        self.assertEqual(self.store.start_trial(CHAT,ident,approved=True)['state'],'trial')

    def test_model_self_score_or_changed_model_route_cannot_promote(self):
        ident = self.propose()
        for update in ({'validator':'llm'}, {'validator':'model'}, {'baseline_model':'other-route'},
                       {'baseline_cost':float('nan')}, {'budget_tokens':True}):
            with self.subTest(update=update), self.assertRaises((PermissionError,ValueError)):
                self.validate(ident,**update)
        self.assertEqual(self.store.method(CHAT,ident)['state'],'proposed')

    def test_generation_case_and_origin_sources_are_not_heldout(self):
        ident = self.propose()
        for case in ('generation-fixture','origin','correction','original-out','original-question'):
            with self.subTest(case=case), self.assertRaises(PermissionError):
                self.validate(ident,case)

    def test_two_independent_cases_with_measured_improvement_retain(self):
        ident = self.retained()
        row = self.store.method(CHAT,ident)
        self.assertEqual(row['reason'],'heldout_improvement')
        validations = self.store.validations(CHAT,ident)
        self.assertEqual(len(validations),2)
        self.assertIsNone(validations[0]['baseline_cost'])
        self.assertIsNone(validations[0]['method_cost'])
        self.assertEqual(validations[0]['budget_tokens'],2048)
        self.clock.step(86401)
        self.assertEqual(self.store.method(CHAT,ident)['state'],'retained')

    def test_equal_success_is_not_claimed_improvement(self):
        ident = self.propose()
        self.validate(ident,baseline=True)
        self.clock.step()
        self.store.start_trial(CHAT,ident)
        self.clock.step()
        self.assertEqual(self.validate(ident,'case-two',baseline=True)['state'],'trial')

    def test_duplicate_validation_does_not_count_twice_and_cannot_be_rewritten(self):
        ident = self.trial()
        self.clock.step()
        self.validate(ident)
        self.assertEqual(len(self.store.validations(CHAT,ident)),1)
        self.assertEqual(self.store.method(CHAT,ident)['state'],'trial')
        with self.assertRaises(PermissionError): self.validate(ident,baseline=True)

    def test_validation_failure_reverts_candidates_and_retained_methods(self):
        ident = self.propose()
        self.assertEqual(self.validate(ident,method=False)['state'],'reverted')
        with self.assertRaises(PermissionError): self.store.start_trial(CHAT,ident,approved=True)
        other = self.propose(instruction='将合成任务中的对象分别核对。',request_key='different')
        self.validate(other)
        self.clock.step()
        self.store.start_trial(CHAT,other)
        self.clock.step()
        self.validate(other,'second')
        self.clock.step()
        self.assertEqual(self.validate(other,'counterexample',baseline=True,method=False)['state'],'reverted')
        self.assertEqual(self.store.context(CHAT),[])

    def test_actual_delivery_use_is_immutable_and_queryable_by_outbound(self):
        ident = self.trial()
        usage = self.store.record_use(CHAT,ident,'ack-one',request_id='request',reference='exchange-one')
        self.assertEqual(usage,self.store.record_use(CHAT,ident,'ack-one',request_id='request',reference='exchange-one'))
        self.assertEqual(self.store.uses_for_delivery(CHAT,'ack-one')[0]['id'],usage)
        with self.assertRaises(PermissionError):
            self.store.record_use(CHAT,ident,'ack-one',request_id='different',reference='exchange-one')
        self.assertEqual(self.store.uses_for_delivery(OTHER,'ack-one'),[])

    def test_later_explicit_negative_feedback_revokes_the_exact_used_method(self):
        ident = self.retained()
        usage = self.store.record_use(CHAT,ident,'actual-ack',reference='exact-exchange')
        self.clock.step()
        self.feedback('negative',status='declined',reference='exact-exchange')
        row = self.store.record_outcome(CHAT,ident,usage,'negative')
        self.assertEqual(row['state'],'reverted')
        self.assertEqual(row['reason'],'contradictory_feedback')
        self.assertFalse(self.store.current(CHAT,[ident]))

    def test_same_real_reply_can_refine_to_correction_but_negative_cannot_be_weakened(self):
        ident = self.retained()
        usage = self.store.record_use(CHAT,ident,'actual-ack',reference='exact-exchange')
        self.clock.step()
        self.feedback('refined',status='replied',reference='exact-exchange')
        self.assertEqual(self.store.record_outcome(CHAT,ident,usage,'refined')['state'],'retained')
        self.evidence[(CHAT,'refined')]['status'] = 'corrected'
        self.assertEqual(self.store.record_outcome(CHAT,ident,usage,'refined')['state'],'reverted')
        self.evidence[(CHAT,'refined')]['status'] = 'replied'
        with self.assertRaises(PermissionError):
            self.store.record_outcome(CHAT,ident,usage,'refined')

    def test_negative_advice_cannot_change_id_by_using_new_feedback_or_whitespace(self):
        ident = self.trial()
        usage = self.store.record_use(CHAT,ident,'actual-ack',reference='exact-exchange')
        self.clock.step()
        self.feedback('negative',status='declined',reference='exact-exchange')
        self.store.record_outcome(CHAT,ident,usage,'negative')
        self.feedback('fresh-origin',status='corrected')
        with self.assertRaises(PermissionError):
            self.propose(evidence_ids=['fresh-origin'],request_key='new-key')
        with self.assertRaises(PermissionError):
            self.propose(evidence_ids=['fresh-origin'],request_key='another-key',
                condition='  合成讨论中有多位资料提供者时  ',instruction='先核对每条资料的原始作者，再区分建议提出者和采用者。\n')

    def test_independent_failure_digest_survives_content_rollback(self):
        ident = self.propose()
        backup = self.root/'before-failure.sqlite3'
        shutil.copyfile(self.path,backup)
        self.validate(ident,method=False)
        shutil.copyfile(backup,self.path)
        restored = self.create()
        self.feedback('new-feedback',status='corrected')
        with self.assertRaises(PermissionError):
            restored.propose(CHAT,'attribution','合成讨论中有多位资料提供者时',
                '先核对每条资料的原始作者，再区分建议提出者和采用者。',['new-feedback'])

    def test_unrelated_or_earlier_feedback_cannot_evaluate_a_use(self):
        ident = self.trial()
        usage = self.store.record_use(CHAT,ident,'ack',reference='exact-exchange')
        self.feedback('too-early',status='corrected',reference='exact-exchange')
        with self.assertRaises(PermissionError): self.store.record_outcome(CHAT,ident,usage,'too-early')
        self.clock.step()
        self.feedback('other-exchange',status='corrected',reference='other-exchange')
        with self.assertRaises(PermissionError): self.store.record_outcome(CHAT,ident,usage,'other-exchange')
        self.assertEqual(self.store.method(CHAT,ident)['state'],'trial')

    def test_reply_or_silence_does_not_mean_semantic_improvement(self):
        ident = self.propose()
        self.store.start_trial(CHAT,ident,approved=True)
        usage = self.store.record_use(CHAT,ident,'ack',reference='exact-exchange')
        for status in ('replied','no_response'):
            self.clock.step()
            self.feedback(status,status=status,reference='exact-exchange')
            self.assertEqual(self.store.record_outcome(CHAT,ident,usage,status)['state'],'trial')
        self.assertEqual(self.store.validations(CHAT,ident),[])

    def test_restart_retains_methods_but_not_more_rights(self):
        ident = self.retained()
        restarted = self.create()
        self.assertEqual(restarted.context(CHAT)[0]['id'],ident)
        self.assertEqual(restarted.context(FRIEND),[])
        restricted = self.create(allowed_chats=[FRIEND])
        with self.assertRaises(PermissionError): restricted.context(CHAT)

    def test_missing_live_origin_feedback_revokes_method_and_cannot_restore_it(self):
        ident = self.retained()
        saved = self.evidence.pop((CHAT,'origin'))
        self.assertEqual(self.store.context(CHAT),[])
        self.evidence[(CHAT,'origin')] = saved
        self.assertEqual(self.store.method(CHAT,ident)['state'],'reverted')
        with self.assertRaises(PermissionError): self.propose()

    def test_forget_original_source_cascades_delivered_method_derivatives(self):
        first = self.trial()
        self.store.record_use(CHAT,first,'first-delivery',reference='exchange-first')
        self.clock.step()
        self.feedback('second-origin',status='corrected',source='exchange-first')
        second = self.propose(evidence_ids=['second-origin'],dependencies=['first-delivery'],request_key='second')
        revoked = self.store.forget_messages(CHAT,['original-question'])
        self.assertIn(first,revoked)
        self.assertIn('first-delivery',revoked)
        self.assertIn(second,revoked)
        self.assertEqual(self.store.method(CHAT,first)['state'],'reverted')
        self.assertEqual(self.store.method(CHAT,second)['state'],'reverted')
        with self.assertRaises(PermissionError):
            self.propose(instruction='新文字不能复活旧来源。',dependencies=['first-delivery'])

    def test_forgetting_validation_or_followup_evidence_also_revokes(self):
        ident = self.retained()
        self.store.forget_evidence(CHAT,['new-case-two'])
        self.assertEqual(self.store.method(CHAT,ident)['state'],'reverted')

    def test_content_rollback_cannot_revive_forgotten_or_failed_method(self):
        ident = self.retained()
        backup = self.root/'snapshot.sqlite3'
        shutil.copyfile(self.path,backup)
        self.store.forget_messages(CHAT,['original-question'])
        shutil.copyfile(backup,self.path)
        restored = self.create()
        self.assertEqual(restored.context(CHAT),[])
        self.assertEqual(restored.method(CHAT,ident)['state'],'reverted')
        self.assertEqual(restored.method(CHAT,ident)['condition'],'')
        self.assertEqual(restored.method(CHAT,ident)['instruction'],'')
        self.assertTrue(all(not row['condition'] and not row['instruction'] for row in restored.list_methods(CHAT)))

    def test_forgotten_method_is_redacted_even_if_it_was_already_reverted(self):
        ident = self.retained()
        self.store.revert(CHAT,ident)
        self.store.forget_messages(CHAT,['original-question'])
        row = self.store.method(CHAT,ident)
        self.assertEqual(row['instruction'],'')
        self.assertEqual(row['condition'],'')
        self.assertNotIn('先核对每条资料的原始作者',self.path.read_bytes().decode(errors='ignore'))

    def test_expired_trial_reverts_and_cannot_be_renewed(self):
        ident = self.propose()
        self.store.start_trial(CHAT,ident,approved=True,duration_seconds=60)
        self.clock.step(61)
        self.assertEqual(self.store.context(CHAT),[])
        with self.assertRaises(PermissionError): self.store.start_trial(CHAT,ident,approved=True)

    def test_context_is_bounded_without_truncating_method_instructions(self):
        ident = self.trial()
        rows = self.store.context(CHAT,limit=3,budget=1200)
        self.assertEqual(rows[0]['id'],ident)
        self.assertLessEqual(len(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode()),1200)
        self.assertEqual(self.store.context(CHAT,budget=128),[])
        self.assertEqual(rows[0]['instruction'],self.store.method(CHAT,ident)['instruction'])

    def test_new_candidates_do_not_push_old_active_method_out_of_recall(self):
        ident = self.retained()
        for index in range(33):
            self.clock.step()
            self.propose(instruction='另一项尚未验证的合成建议'+str(index))
        self.assertEqual(self.store.context(CHAT)[0]['id'],ident)

    def test_capacity_retires_only_reverted_methods_and_keeps_revocation_fence(self):
        small = self.create(max_methods=1)
        first = small.propose(CHAT,'kind','condition','old advice',['origin'])
        with self.assertRaises(ValueError): small.propose(CHAT,'kind','condition','new advice',['origin'])
        small.revert(CHAT,first)
        second = small.propose(CHAT,'kind','condition','new advice',['origin'])
        self.assertEqual(len(small.list_methods(CHAT)),1)
        self.assertEqual(small.list_methods(CHAT)[0]['id'],second)
        with self.assertRaises(PermissionError): small.propose(CHAT,'kind','condition','old advice',['origin'])

    def test_proposal_and_context_inputs_are_strictly_bounded(self):
        for update in ({'condition':'x'*201},{'instruction':'x'*501},{'evidence_ids':[]},
                       {'dependencies':['duplicate','duplicate']},{'instruction':'bad\x00data'}):
            with self.subTest(update=update), self.assertRaises((ValueError,PermissionError)):
                self.propose(**update)
        for kwargs in ({'limit':True},{'budget':127},{'limit':4},{'budget':1201}):
            with self.assertRaises(ValueError): self.store.context(CHAT,**kwargs)

    def test_generation_attempt_is_consumed_before_model_and_survives_content_rollback(self):
        backup = self.root/'snapshot.sqlite3'
        shutil.copyfile(self.path,backup)
        self.assertTrue(self.store.reserve_generation(CHAT,'key-one','2027-01-15',evidence_ids=['origin']))
        self.assertFalse(self.store.reserve_generation(CHAT,'key-one','2027-01-16',evidence_ids=['origin']))
        self.assertTrue(self.store.reserve_generation(CHAT,'key-two','2027-01-15',evidence_ids=['origin']))
        self.assertFalse(self.store.reserve_generation(CHAT,'key-three','2027-01-15',evidence_ids=['origin']))
        shutil.copyfile(backup,self.path)
        restarted = self.create()
        self.assertFalse(restarted.reserve_generation(CHAT,'key-three','2027-01-15',evidence_ids=['origin']))

    def test_generation_budget_is_atomic_for_parallel_attempts(self):
        def attempt(index):
            return self.store.reserve_generation(CHAT,'parallel-'+str(index),'2027-01-15',evidence_ids=['origin'])
        with ThreadPoolExecutor(max_workers=6) as pool:
            answers = list(pool.map(attempt,range(8)))
        self.assertEqual(sum(answers),2)

    def test_generation_requires_current_negative_evidence_and_fixed_key(self):
        with self.assertRaises(PermissionError):
            self.store.reserve_generation(CHAT,'missing','2027-01-15',evidence_ids=['missing'])
        self.assertTrue(self.store.reserve_generation(CHAT,'fixed','2027-01-15',evidence_ids=['origin']))
        self.feedback('another',status='corrected')
        with self.assertRaises(PermissionError):
            self.store.reserve_generation(CHAT,'fixed','2027-01-15',evidence_ids=['another'])
        self.store.forget_evidence(CHAT,['origin'])
        with self.assertRaises(PermissionError):
            self.store.reserve_generation(CHAT,'later','2027-01-16',evidence_ids=['origin'])

    def test_closed_store_and_insecure_storage_are_rejected(self):
        self.store.close()
        with self.assertRaises(PermissionError): self.store.list_methods(CHAT)
        with self.assertRaises(ValueError): MethodStore(self.root/'bad.sqlite3',allowed_chats=['not-a-chat'])
        link = self.root/'linked.sqlite3'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError): MethodStore(link)
        unsafe = self.root/'unsafe'
        unsafe.mkdir(mode=0o755)
        with self.assertRaises(ValueError): MethodStore(unsafe/'methods.sqlite3')


if __name__ == '__main__':
    unittest.main()
