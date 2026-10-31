"""Real controller/facade/ledger wiring with independently authored fixtures.

Model decisions and channel ACKs are explicit synthetic protocol fixtures. These
tests do not establish language accuracy, connect QQ, or read runtime profiles.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from support import FakeContext,FakePort,build,event,policy
from hermes_constants import set_hermes_home_override,reset_hermes_home_override


CHAT='group:20001'
OTHER='group:20002'
TASK_A='合成展台甲需要三组纸卡，请给出摆放安排。'
TASK_B='我的独立合成展台乙需要两组木片，请分别安排。'
CONDITION_B='我为展台甲补充一个条件：纸卡采用三角排列。'


class OrchestrationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home=Path(self.temp.name)
        self.token=set_hermes_home_override(self.home)
        self.instances=[]
        self.archive={}
        self.texts={}
        self.model_inputs=[]
        self.selection=None
        self.review=dict(completed=[],waiting=[],methods=[])
        self.fail_review=False
        self.delay_review=False
        self.review_entered=asyncio.Event()
        self.review_release=asyncio.Event()
        self.config=policy(targets=[CHAT,OTHER],initiative_mode='local',
            dialogue={'enabled':True,'orchestration':True,'proactive':False,
                      'topic_ttl_seconds':86400,'opportunity_ttl_seconds':3600})
        self.social,self.ctx,self.port=self.create()

    def create(self):
        ctx,port=FakeContext(),FakePort(self.home)
        social=build(ctx,port,self.config)
        social.policy.ambient_debounce_seconds=0
        social.policy.ambient_cooldown_seconds=0
        async def read(chat,ids):
            return [dict(self.archive[(chat,mid)]) for mid in ids if (chat,mid) in self.archive]
        port.read_dialogue_refs=AsyncMock(side_effect=read)
        async def provider(**kwargs):
            payload=json.loads(kwargs['messages'][-1]['content'])
            self.model_inputs.append(payload)
            if payload.get('kind')=='public_participation_selection':
                result=self.selection(payload)
            elif payload.get('kind')=='social_reply_coverage':
                if self.delay_review:
                    self.review_entered.set()
                    await self.review_release.wait()
                if self.fail_review:
                    raise ValueError('synthetic coverage failure')
                result=self.review(payload) if callable(self.review) else self.review
            else:
                raise AssertionError('unexpected synthetic model purpose')
            return ('offline','same-host-model',SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(result,ensure_ascii=False)))],usage=None))
        ctx.llm._async_caller=provider
        self.instances.append((social,ctx))
        return social,ctx,port

    async def asyncTearDown(self):
        self.review_release.set()
        for social,ctx in reversed(self.instances):
            ctx.release.set()
            await social.stop()
            ctx.unload()
            await asyncio.sleep(0)
            if social.binding.shutdown_task:
                await social.binding.shutdown_task
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def observe(self,text,mid,user='10001',chat=CHAT):
        item=event(text,mid=mid,user=user,chat=chat,name='合成成员')
        item.reply_expected=False
        item.bot_id='90001'
        self.archive[(chat,mid)]=dict(message_id=mid,user_id=user,role='member',
            name='合成成员',text=text,reply_to='')
        self.texts[mid]=text
        self.social.observe(item)
        return item

    def ref(self,payload,mid):
        text=self.texts[mid]
        for row in (*payload['messages'],*payload['candidates']):
            if row['text']==text:
                return row['ref']
        raise AssertionError('required synthetic reference absent: '+mid)

    @staticmethod
    def assessment(candidate,*,topic=None,relation='new',target=None,conditions=(),
                   action='respond',reason='question',supersedes=()):
        return dict(candidate=candidate,audience='group',action=action,topic=topic or candidate,
            reason=reason,topic_action='continue',request=dict(action=relation,
                target=target or candidate,conditions=list(conditions),supersedes=list(supersedes)))

    async def classify(self,*items):
        self.social.busy[CHAT]=1
        for item in items:
            self.social.queue_ambient(item)
        self.social.busy[CHAT]=0
        tasks=tuple(self.social.ambient_tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks),3)

    def view(self,item):
        view=self.social.prepare_public_context(item)
        return view,json.loads(view.text[view.text.index('{'):])

    def requests(self):
        return {row.request_id:row for row in self.social.life.enhanced.request_context(CHAT)}

    def ack(self,item,text,mid):
        self.social.record_self(CHAT,text,mid,reply_to_message_id=item.message_id)
        self.archive[(CHAT,mid)]=dict(message_id=mid,user_id='90001',role='self',
            name='合成机器人',text=text,reply_to=item.message_id)
        self.texts[mid]=text

    async def seed_two(self):
        first=self.observe(TASK_A,'task-a')
        second=self.observe(TASK_B,'task-b','10002')
        def selection(payload):
            a,b=self.ref(payload,'task-a'),self.ref(payload,'task-b')
            return dict(selected=b,assessments=[self.assessment(a),self.assessment(b,topic=a)],
                reply_plan=dict(covers=[a,b],mode='combined',defer=[]))
        self.selection=selection
        await self.classify(first,second)
        self.assertEqual(self.port.dispatched,[second])
        return first,second

    async def test_two_owners_combined_plan_completes_only_after_verified_ack(self):
        _,second=await self.seed_two()
        requests=self.requests()
        self.assertEqual(requests['task-a'].owner_id,'10001')
        self.assertEqual(requests['task-b'].owner_id,'10002')
        self.assertNotIn('task-b',requests['task-a'].condition_ids)
        self.assertNotIn('task-a',requests['task-b'].condition_ids)
        view,payload=self.view(second)
        self.assertEqual(payload['reply_plan']['covers'],['task-a','task-b'])
        public={row['request_id']:row for row in payload['requests']}
        self.assertNotEqual(public['task-a']['requester'],public['task-b']['requester'])
        self.review=dict(completed=['task-a','task-b'],waiting=[],methods=[])
        draft='展台甲的三组纸卡排成一列，展台乙的两组木片排成两列。'
        await self.social.prepare_reply(second,draft)
        self.assertTrue(any(row.get('kind')=='social_reply_coverage' for row in self.model_inputs))
        self.assertEqual({row.state for row in self.requests().values()},{'pending'})
        self.social.record_self(CHAT,'NO_REPLY','not-an-ack',reply_to_message_id=second.message_id)
        self.assertEqual({row.state for row in self.requests().values()},{'pending'})
        self.assertTrue(view.current())
        self.ack(second,draft,'combined-ack')
        self.assertEqual({row.state for row in self.requests().values()},{'responded'})

    async def test_actual_draft_can_leave_one_request_waiting_and_the_other_unanswered(self):
        _,second=await self.seed_two()
        self.view(second)
        self.review=dict(completed=[],waiting=['task-a'],methods=[])
        draft='展台甲还需要确认纸卡的颜色；展台乙的安排稍后继续。'
        await self.social.prepare_reply(second,draft)
        self.ack(second,draft,'clarify-ack')
        requests=self.requests()
        self.assertEqual(requests['task-a'].state,'waiting')
        self.assertEqual(requests['task-b'].state,'pending')

    async def test_review_failure_preserves_exact_sent_body_and_pending_requests(self):
        _,second=await self.seed_two()
        self.view(second)
        self.fail_review=True
        draft='这是一条审核失败但已通过其他通道检查的合成正文。'
        await self.social.prepare_reply(second,draft)
        self.assertEqual(self.social.life.last_error,'reply_coverage_unavailable')
        self.ack(second,draft,'unreviewed-ack')
        self.assertEqual({row.state for row in self.requests().values()},{'pending'})
        recent=self.social.recent.routing_history(self.social._scope(CHAT))
        self.assertEqual(next(row.text for row in recent if row.message_id=='unreviewed-ack'),draft)

    async def test_processing_completion_without_ack_releases_plan_and_keeps_requests_pending(self):
        _,second=await self.seed_two()
        self.social.processing_start(second)
        self.view(second)
        self.review=dict(completed=['task-a','task-b'],waiting=[],methods=[])
        await self.social.prepare_reply(second,'这是尚未收到真实回执的合成正文。')
        key=(CHAT,second.message_id)
        plan=self.social.life.enhanced.plans[key]
        self.assertTrue(plan.current())
        self.social.processing_complete(second)
        self.assertFalse(plan.current())
        self.assertNotIn(key,self.social.life.enhanced.plans)
        self.assertEqual({row.state for row in self.requests().values()},{'pending'})

    async def seed_supplement(self):
        root=self.observe(TASK_A,'task-a')
        self.selection=lambda payload:dict(selected=None,
            assessments=[self.assessment('c0',action='wait')],
            reply_plan=dict(covers=[],mode='single',defer=['c0']))
        await self.classify(root)
        contribution=self.observe(CONDITION_B,'condition-b','10002')
        def selection(payload):
            a=self.ref(payload,'task-a')
            return dict(selected=None,assessments=[self.assessment('c0',relation='supplement',
                target=a,topic=a,action='wait',reason='followup')],
                reply_plan=dict(covers=[],mode='single',defer=[a]))
        self.selection=selection
        await self.classify(contribution)
        return root,contribution

    async def adopt(self,mid='adoption-a'):
        adopting=self.observe('我采用刚刚提供的排列条件，请继续安排。',mid)
        def selection(payload):
            a,b=self.ref(payload,'task-a'),self.ref(payload,'condition-b')
            return dict(selected='c0',assessments=[self.assessment('c0',relation='adopt',
                target=a,topic=a,conditions=[b],reason='followup')],
                reply_plan=dict(covers=[a],mode='single',defer=[]))
        self.selection=selection
        await self.classify(adopting)
        return adopting

    async def test_foreign_supplement_and_explicit_adoption_survive_real_context_mapping(self):
        await self.seed_supplement()
        initial=self.requests()['task-a']
        condition=next(row for row in initial.conditions if row.source_id=='condition-b')
        self.assertEqual(condition.provider_id,'10002')
        self.assertFalse(condition.adopted_by)
        adopting=await self.adopt()
        result=self.requests()['task-a']
        condition=next(row for row in result.conditions if row.source_id=='condition-b')
        self.assertEqual(condition.provider_id,'10002')
        self.assertEqual(condition.adopted_by,('10001',))
        _,payload=self.view(adopting)
        request=next(row for row in payload['requests'] if row['request_id']=='task-a')
        condition=next(row for row in request['conditions'] if row['message_id']=='condition-b')
        self.assertNotEqual(condition['provider'],request['requester'])
        self.assertEqual(condition['adopted_by'],[request['requester']])

    async def test_interleaving_and_restart_restore_exact_conditions_with_original_authors(self):
        await self.seed_supplement()
        original=self.social.life.enhanced.requests.reference_metadata(self.social._scope(CHAT),
            ['task-a','condition-b'])
        await self.social.stop()
        self.social,self.ctx,self.port=self.create()
        for index in range(18):
            self.observe('另一个合成任务的材料编号'+str(index),'noise-'+str(index),'10003')
        adopting=await self.adopt('after-restart')
        self.port.read_dialogue_refs.assert_awaited_once()
        chat,ids=self.port.read_dialogue_refs.await_args.args
        self.assertEqual(chat,CHAT)
        self.assertLessEqual(len(ids),8)
        self.assertTrue({'task-a','condition-b'}<=set(ids))
        view,payload=self.view(adopting)
        self.assertIn(TASK_A,view.text)
        self.assertIn(CONDITION_B,view.text)
        restored=self.social.life.enhanced.requests.reference_metadata(self.social._scope(CHAT),
            ['task-a','condition-b'])
        self.assertEqual(restored,original)
        condition=next(row for row in self.requests()['task-a'].conditions if row.source_id=='condition-b')
        self.assertEqual(condition.provider_id,'10002')

    async def test_forget_winning_during_draft_review_discards_late_completion(self):
        await self.seed_supplement()
        adopting=await self.adopt()
        view,_=self.view(adopting)
        self.review=dict(completed=['task-a'],waiting=[],methods=[])
        self.delay_review=True
        task=asyncio.create_task(self.social.prepare_reply(adopting,'已采用条件完成这份合成安排。'))
        await asyncio.wait_for(self.review_entered.wait(),2)
        self.social.forget_messages(CHAT,['condition-b'])
        self.review_release.set()
        await asyncio.wait_for(task,2)
        self.assertFalse(view.current())
        self.ack(adopting,'已经在网络中的迟到合成正文。','late-ack')
        self.assertEqual(self.requests()['task-a'].state,'pending')
        self.assertFalse(self.social.life.enhanced.requests.reference_metadata(self.social._scope(CHAT),['late-ack']))

    async def test_topic_eviction_does_not_drop_a_still_waiting_independent_request(self):
        root=self.observe(TASK_A,'task-a')
        self.selection=lambda payload:dict(selected='c0',
            assessments=[self.assessment('c0')],
            reply_plan=dict(covers=['c0'],mode='clarify',defer=[]))
        await self.classify(root)
        self.view(root)
        self.review=dict(completed=[],waiting=['task-a'],methods=[])
        await self.social.prepare_reply(root,'纸卡采用哪一种排列方式？')
        self.ack(root,'纸卡采用哪一种排列方式？','first-clarifying-ack')
        self.observe(CONDITION_B,'condition-b','10002')
        adopting=await self.adopt()
        self.view(adopting)
        self.review=dict(completed=[],waiting=['task-a'],methods=[])
        draft='纸卡的排列已有建议，还需要确认纸卡采用哪种颜色。'
        await self.social.prepare_reply(adopting,draft)
        self.ack(adopting,draft,'clarifying-ack')
        self.assertEqual(self.requests()['task-a'].state,'waiting')
        original={row['message_id']:row for row in
            self.social.life.enhanced.requests.reference_metadata(self.social._scope(CHAT),['task-a','condition-b'])}
        old_source=adopting.source
        await self.social.stop()
        self.social,self.ctx,self.port=self.create()
        for index in range(40):
            self.observe('独立合成背景的材料标记'+str(index),'burst-'+str(index),'10003')
        self.assertEqual(self.requests()['task-a'].state,'waiting')
        self.assertNotIn('task-a',[row.anchor_id for row in
            self.social.life.dialogue.context(self.social._scope(CHAT))])
        resumed=await self.adopt('resume-after-topic-eviction')
        self.assertEqual(self.port.dispatched,[resumed])
        self.assertIs(self.port.dispatched[0].source,resumed.source)
        self.assertIsNot(resumed.source,old_source)
        self.assertFalse(self.port.optional_valid(old_source))
        with self.assertRaises(ValueError):
            self.social.life.enhanced.requests.bind_reply(self.social._scope(CHAT),root,['task-a'])
        view,_=self.view(resumed)
        self.assertIn(CONDITION_B,view.text)
        restored={row.message_id:row for row in self.social.life.recent.routing_history(self.social._scope(CHAT))}
        self.assertEqual(restored['condition-b'].sender_id,'10002')
        self.assertEqual(restored['condition-b'].observed_at,original['condition-b']['observed_at'])
        self.port.read_dialogue_refs.assert_awaited_once()
        ids=self.port.read_dialogue_refs.await_args.args[1]
        self.assertLessEqual(len(ids),8)
        self.assertTrue({'task-a','condition-b'}<=set(ids))

    async def test_forgetting_one_independent_request_preserves_the_other_but_revokes_combined_ack(self):
        _,second=await self.seed_two()
        view,_=self.view(second)
        self.review=dict(completed=['task-a','task-b'],waiting=[],methods=[])
        await self.social.prepare_reply(second,'分别完成两个合成展台的安排。')
        self.ack(second,'分别完成两个合成展台的安排。','combined-ack')
        self.assertEqual(self.requests()['task-a'].topic_anchor,self.requests()['task-b'].topic_anchor)
        self.social.forget_messages(CHAT,['task-a'])
        self.assertFalse(view.current())
        requests=self.requests()
        self.assertNotIn('task-a',requests)
        self.assertIn('task-b',requests)
        self.assertEqual(requests['task-b'].owner_id,'10002')
        self.assertEqual(requests['task-b'].state,'pending')
        metadata=self.social.life.enhanced.requests.reference_metadata(self.social._scope(CHAT),['combined-ack'])
        self.assertFalse(metadata)

    async def test_legacy_exchange_upgrade_reads_original_conditions_and_repairs_unknown_request_without_reviving_ingress(self):
        await self.social.stop()
        self.config['dialogue']['orchestration']=False
        self.social,self.ctx,self.port=self.create()
        original=self.observe(TASK_A,'legacy-question','10001')
        self.selection=lambda payload:dict(selected='c0',assessments=[dict(candidate='c0',audience='group',
            action='respond',topic='c0',reason='question',topic_action='continue')])
        await self.classify(original)
        self.view(original)
        question='合成展台的纸卡采用哪一种排列方式？'
        self.ack(original,question,'legacy-bot-question')
        scope=self.social._scope(CHAT)
        before={row['message_id']:row for row in self.social.life.dialogue.reference_metadata(
            scope,['legacy-question','legacy-bot-question'])}
        self.assertEqual(before['legacy-question']['actor_id'],'10001')
        self.assertEqual(before['legacy-bot-question']['role'],'self')
        self.assertIsNone(self.social.life.enhanced.requests)
        await self.social.stop()
        self.config['dialogue']['orchestration']=True
        self.social,self.ctx,self.port=self.create()
        ledger=self.social.life.enhanced.requests
        self.assertFalse(self.requests())
        self.assertFalse(ledger._bindings)
        fresh=self.observe('采用三角排列。','upgrade-current','10002')
        attempts,prompts=[],[]
        provider=self.ctx.llm._async_caller
        async def scripted_provider(**kwargs):
            payload=json.loads(kwargs['messages'][-1]['content'])
            if payload.get('kind')=='public_participation_selection':
                prompts.append(kwargs['messages'][0]['content'])
            return await provider(**kwargs)
        self.ctx.llm._async_caller=scripted_provider
        def selection(payload):
            attempts.append(payload)
            old=self.ref(payload,'legacy-question')
            self.assertEqual(payload['request_targets'],dict(existing=[],new=['c0']))
            if len(attempts)==1:
                # This old topic has never been registered as a request. The
                # parser must reject it before the static protocol repair.
                return dict(selected='c0',assessments=[self.assessment('c0',topic=old,
                    relation='continue',target=old,reason='followup')],
                    reply_plan=dict(covers=[old],mode='single',defer=[]))
            return dict(selected='c0',assessments=[self.assessment('c0',topic=old,
                conditions=[old],reason='followup')],
                reply_plan=dict(covers=['c0'],mode='single',defer=[]))
        self.selection=selection
        await self.classify(fresh)
        self.assertEqual(len(attempts),2)
        self.assertEqual(attempts[0],attempts[1],'repair must reuse exactly the same source-bound frame')
        self.assertNotIn('本次错误是把历史话题当成已有事项',prompts[0])
        self.assertIn('request_targets.existing',prompts[1])
        self.assertIn('request.action=new',prompts[1])
        self.assertIn('reply_plan.covers',prompts[1])
        self.assertEqual(self.port.dispatched,[fresh])
        self.port.read_dialogue_refs.assert_awaited_once()
        read_chat,read_ids=self.port.read_dialogue_refs.await_args.args
        self.assertEqual(read_chat,CHAT)
        self.assertTrue({'legacy-question','legacy-bot-question'}<=set(read_ids))
        imported={row['message_id']:row for row in ledger.reference_metadata(scope,list(before))}
        for mid,row in before.items():
            self.assertEqual((imported[mid]['actor_id'],imported[mid]['role'],imported[mid]['observed_at']),
                             (row['actor_id'],row['role'],row['observed_at']))
        self.assertEqual(set(ledger._bindings),{(ledger._scope(scope),'upgrade-current')})
        with self.assertRaises(ValueError):
            ledger.assess(scope,'legacy-question',kind='new')
        with self.assertRaises(ValueError):
            ledger.bind_reply(scope,original,['upgrade-current'])
        request=self.requests()['upgrade-current']
        self.assertEqual((request.owner_id,request.topic_anchor),('10002','legacy-question'))
        condition=next(row for row in request.conditions if row.source_id=='legacy-question')
        self.assertEqual((condition.provider_id,condition.adopted_by),('10001',('10002',)))
        view,payload=self.view(fresh)
        self.assertIn(TASK_A,view.text)
        self.assertIn(question,view.text)
        self.assertEqual(payload['reply_plan']['covers'],['upgrade-current'])
        self.review=dict(completed=['upgrade-current'],waiting=[],methods=[])
        draft='沿用原来的三组纸卡条件，按刚刚确认的三角排列布置合成展台。'
        await self.social.prepare_reply(fresh,draft)
        self.assertEqual(self.requests()['upgrade-current'].state,'pending')
        self.ack(fresh,draft,'upgraded-final-ack')
        self.assertEqual(self.requests()['upgrade-current'].state,'responded')
        self.assertEqual(self.requests()['upgrade-current'].owner_id,'10002')


if __name__=='__main__':
    unittest.main()
