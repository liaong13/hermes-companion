"""Fixed synthetic holdouts execute tasks; deterministic checks never self-grade."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import support
from hermes_social.methods import MethodStore
from hermes_social.method_validation import CASES, cases_for, check_case, validate_method
from hermes_social.prompts import GROUP_TOPIC_POLICY


CHAT = 'group:20001'


class MethodValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.root = Path(self.temp.name)
        self.now = 1800000000.0
        self.active = True
        self.feedback = dict(id='origin', chat=CHAT, status='corrected',
            message_id='explicit-feedback', source_id='verified-outbound', created=self.now)
        self.store = MethodStore(self.root/'methods.sqlite3', allowed_chats=[CHAT], clock=lambda:self.now,
            evidence_resolver=lambda chat,ids:[dict(self.feedback)] if ids == ('origin',) else [])
        self.calls = []

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def propose(self, category='authorship', **overrides):
        values = dict(category=category, condition='合成多人活动出现建议与采用者时',
            instruction='将条件提供者与选择采用者分别核对。', evidence_ids=['origin'],
            generation_case_ids=['different-generation-scene'])
        values.update(overrides)
        return self.store.propose(CHAT,**values)

    async def run_validator(self, ident, *, baseline=False, method=True, auto_trials=True,
                            cancel_at=None, failure_at=None, route_change_at=None):
        cases = cases_for(self.store.method(CHAT,ident)['category'])
        async def complete(messages, *, max_tokens, purpose):
            index = len(self.calls)
            self.now += 1
            self.calls.append(dict(messages=messages,max_tokens=max_tokens,purpose=purpose))
            if failure_at is not None and index == failure_at:
                raise RuntimeError('SYNTHETIC_PRIVATE_API_ERROR')
            if cancel_at is not None and index == cancel_at:
                self.active = False
            passed = baseline if index % 2 == 0 else method
            text = cases[index//2].expected_json if passed else '{"not_the_requested_task_product":true}'
            model_name = 'synthetic-route:changed' if index == route_change_at else 'synthetic-route:v1'
            return SimpleNamespace(text=text, model=model_name, provider='synthetic',
                usage=dict(input_tokens=100,output_tokens=20,total_tokens=120,api_key='DO_NOT_COPY_USAGE_SECRET'))
        return await validate_method(self.store,CHAT,ident,complete,
            current=lambda:self.active, model='synthetic-route:v1',budget_tokens=768,auto_trials=auto_trials)

    async def test_two_fresh_cases_with_actual_improvement_retain_method(self):
        ident = self.propose()
        result = await self.run_validator(ident)
        self.assertEqual(result['status'],'retained')
        self.assertEqual(result['state'],'retained')
        self.assertEqual(result['calls'],4)
        self.assertEqual(len(result['cases']),2)
        self.assertTrue(all(not row['baseline']['passed'] and row['method']['passed'] for row in result['cases']))
        self.assertEqual(len(self.store.validations(CHAT,ident)),2)
        self.assertNotIn('DO_NOT_COPY_USAGE_SECRET',json.dumps(result))
        self.assertNotIn('not_the_requested_task_product',self.store.path.read_bytes().decode(errors='ignore'))

    async def test_baseline_uses_same_current_policy_and_token_budget(self):
        ident = self.propose()
        await self.run_validator(ident)
        for index,call in enumerate(self.calls):
            self.assertEqual(call['purpose'],'social_method_validation')
            self.assertEqual(call['max_tokens'],768)
            self.assertIn(GROUP_TOPIC_POLICY,call['messages'][0]['content'])
            if index % 2:
                self.assertIn('将条件提供者与选择采用者分别核对',call['messages'][0]['content'])
            else:
                self.assertNotIn('将条件提供者与选择采用者分别核对',call['messages'][0]['content'])
            self.assertNotIn('正确答案',call['messages'][0]['content'])
            self.assertNotIn('baseline_pass',json.dumps(call['messages']))

    async def test_unsupported_category_does_not_make_model_calls_or_auto_trial(self):
        ident = self.propose('not_a_known_validator')
        result = await self.run_validator(ident)
        self.assertEqual(result['status'],'unsupported_category')
        self.assertEqual(result['calls'],0)
        self.assertEqual(self.store.method(CHAT,ident)['state'],'proposed')

    async def test_baseline_already_correct_does_not_claim_learning_improvement(self):
        ident = self.propose()
        result = await self.run_validator(ident,baseline=True)
        self.assertEqual(result['status'],'no_measured_improvement')
        self.assertEqual(self.store.method(CHAT,ident)['state'],'trial')

    async def test_auto_trials_false_keeps_validated_method_out_of_live_context(self):
        ident = self.propose()
        result = await self.run_validator(ident,auto_trials=False)
        self.assertEqual(result['status'],'validated')
        self.assertEqual(self.store.context(CHAT),[])
        self.assertEqual(self.store.method(CHAT,ident)['state'],'validated')

    async def test_candidate_failure_rolls_back_without_running_more_cases(self):
        ident = self.propose()
        result = await self.run_validator(ident,method=False)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['calls'],2)
        self.assertEqual(self.store.method(CHAT,ident)['state'],'reverted')

    async def test_generation_case_overlap_is_rejected_before_inference(self):
        case = cases_for('authorship')[0]
        ident = self.propose(generation_case_ids=[case.case_id])
        result = await self.run_validator(ident)
        self.assertEqual(result['status'],'generation_case_overlap')
        self.assertEqual(result['calls'],0)

    async def test_repeated_validation_does_not_spend_or_rewrite_results(self):
        ident = self.propose()
        first = await self.run_validator(ident)
        self.calls.clear()
        second = await self.run_validator(ident)
        self.assertEqual(first['status'],'retained')
        self.assertEqual(second['status'],'already_evaluated')
        self.assertEqual(second['calls'],0)

    async def test_partial_validation_resumes_only_unseen_case(self):
        ident = self.propose()
        first,second = cases_for('authorship')
        self.store.record_validation(CHAT,ident,first.case_id,False,True,validator='deterministic',
            model='synthetic-route:v1',baseline_model='synthetic-route:v1',budget_tokens=768)
        async def complete(messages, **kwargs):
            self.now += 1
            self.calls.append(messages)
            self.assertEqual(messages[-1]['content'],second.scenario)
            return SimpleNamespace(text=second.expected_json,model='synthetic-route:v1',provider='synthetic')
        result = await validate_method(self.store,CHAT,ident,complete,current=lambda:True,
            model='synthetic-route:v1',auto_trials=True)
        self.assertEqual(result['status'],'retained')
        self.assertEqual(result['calls'],2)
        self.assertEqual(len(self.store.validations(CHAT,ident)),2)

    async def test_async_cancellation_propagates_and_rolls_back_partial_trial(self):
        ident = self.propose()
        first = cases_for('authorship')[0]
        async def complete(messages, **kwargs):
            self.now += 1
            self.calls.append(messages)
            if len(self.calls) == 3:
                raise asyncio.CancelledError
            return SimpleNamespace(text=first.expected_json,model='synthetic-route:v1',provider='synthetic')
        with self.assertRaises(asyncio.CancelledError):
            await validate_method(self.store,CHAT,ident,complete,current=lambda:True,
                model='synthetic-route:v1',auto_trials=True)
        self.assertEqual(self.store.method(CHAT,ident)['state'],'reverted')

    async def test_connection_or_evidence_change_before_model_result_discards_validation(self):
        ident = self.propose()
        result = await self.run_validator(ident,cancel_at=0)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['calls'],1)
        self.assertEqual(self.store.validations(CHAT,ident),[])

    async def test_cancellation_after_trial_start_reverts_partial_experiment(self):
        ident = self.propose()
        result = await self.run_validator(ident,cancel_at=2)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['calls'],3)
        self.assertEqual(self.store.method(CHAT,ident)['state'],'reverted')

    async def test_infrastructure_failure_does_not_count_as_semantic_test_failure(self):
        ident = self.propose()
        result = await self.run_validator(ident,failure_at=0)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['error_type'],'RuntimeError')
        self.assertNotIn('SYNTHETIC_PRIVATE_API_ERROR',json.dumps(result))
        self.assertEqual(self.store.method(CHAT,ident)['state'],'proposed')
        self.assertEqual(self.store.validations(CHAT,ident),[])

    async def test_api_failure_after_trial_start_reverts_partial_experiment(self):
        ident = self.propose()
        result = await self.run_validator(ident,failure_at=3)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(self.store.method(CHAT,ident)['state'],'reverted')

    async def test_model_route_change_cannot_be_reported_as_comparable_result(self):
        ident = self.propose()
        result = await self.run_validator(ident,route_change_at=1)
        self.assertEqual(result['status'],'route_changed')
        self.assertEqual(self.store.validations(CHAT,ident),[])

    async def test_method_resolver_revocation_prevents_trials_even_after_task_success(self):
        ident = self.propose()
        case = cases_for('authorship')[0]
        async def complete(messages, **kwargs):
            self.now += 1
            self.store.forget_messages(CHAT,['verified-outbound'])
            return case.expected_json
        result = await validate_method(self.store,CHAT,ident,complete,current=lambda:True,
                                       model='synthetic-route:v1',auto_trials=True)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(self.store.context(CHAT),[])

    async def test_supported_categories_have_distinct_authored_cases_and_expected_schema(self):
        ids = [case.case_id for case in CASES]
        self.assertEqual(len(ids),len(set(ids)))
        for category in ('authorship','request_separation','followup'):
            self.assertEqual(len(cases_for(category)),2)
        for case in CASES:
            with self.subTest(case=case.case_id):
                self.assertTrue(check_case(case.case_id,case.expected_json))
                self.assertFalse(check_case(case.case_id,'{"score":1.0,"method_is_good":true}'))
                self.assertFalse(check_case(case.case_id,'```json\n'+case.expected_json+'\n```'))
                value = json.loads(case.expected_json)
                value['unrequested_claim'] = True
                self.assertFalse(check_case(case.case_id,json.dumps(value,ensure_ascii=False)))
        box = cases_for('request_separation')[0]
        value = json.loads(box.expected_json)
        value['known']['quantity'] = True
        self.assertFalse(check_case(box.case_id,json.dumps(value,ensure_ascii=False)))


if __name__ == '__main__':
    unittest.main()
