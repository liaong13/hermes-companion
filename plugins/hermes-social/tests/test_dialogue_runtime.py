"""Real classifier/controller/dialogue wiring with synthetic model and ACK fixtures.

Model results are scripted protocol fixtures; these tests verify execution and
evidence boundaries, not natural-language accuracy or real QQ delivery.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from support import FakeContext, FakePort, build, event, policy
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


CHAT = 'group:20001'
OTHER = 'group:20002'
QUESTION = '合成旧问题：纸模型采用方案甲还是乙？'
SHARED_TASK = '合成共同任务：安排明日纸模展览的运输。'
SHARED_CONDITION = '合成补充：目的地是星桥展厅，装箱数量为七。'
SHARED_ANSWER = '已按星桥展厅和七箱给出合成运输方案。'


class DialogueRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.token = set_hermes_home_override(self.home)
        self.instances = []
        self.archive = {}
        self.inputs = []
        self.config = policy(targets=[CHAT,OTHER],initiative_mode='local',
            dialogue={'enabled':True,'topic_ttl_seconds':86400,'opportunity_ttl_seconds':3600,
                      'proactive':False})
        self.social,self.ctx,self.port = self.create()

    def create(self):
        ctx,port = FakeContext(),FakePort(self.home)
        social = build(ctx,port,self.config)
        social.policy.ambient_debounce_seconds = 0
        social.policy.ambient_cooldown_seconds = 0
        async def read(chat,ids):
            return [dict(self.archive[(chat,mid)]) for mid in ids if (chat,mid) in self.archive]
        port.read_dialogue_refs = AsyncMock(side_effect=read)
        self.instances.append((social,ctx))
        return social,ctx,port

    async def asyncTearDown(self):
        for social,ctx in reversed(self.instances):
            ctx.release.set()
            await social.stop()
            ctx.unload()
            await asyncio.sleep(0)
            if social.binding.shutdown_task:
                await social.binding.shutdown_task
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def observe(self,text,mid,*,user='10001',chat=CHAT):
        item = event(text,mid=mid,user=user,chat=chat,name='合成成员')
        item.reply_expected = False
        item.bot_id = '90001'
        self.archive[(chat,mid)] = dict(message_id=mid,user_id=user,role='member',
                                        name='合成成员',text=text,reply_to='')
        self.social.observe(item)
        return item

    async def classify(self,item,*,action='respond',reason='followup',audience='group',
                       topic_action='continue',anchor_text=None):
        original = self.ctx.llm._async_caller
        async def provider(**kwargs):
            payload = json.loads(kwargs['messages'][-1]['content'])
            self.inputs.append(payload)
            anchor = 'c0'
            if anchor_text is not None:
                anchor = next((row['ref'] for row in payload['messages'] if row['text'] == anchor_text),'missing')
            self.ctx.selection_response = dict(selected='c0' if action == 'respond' else None,
                assessments=[dict(candidate='c0',audience=audience,action=action,topic=anchor,
                                  reason=reason,topic_action=topic_action)])
            return await original(**kwargs)
        self.ctx.llm._async_caller = provider
        try:
            self.social.queue_ambient(item)
            tasks = tuple(self.social.ambient_tasks.values())
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks),3)
        finally:
            self.ctx.llm._async_caller = original

    def payload(self,item):
        view = self.social.prepare_public_context(item)
        return view,json.loads(view.text[view.text.index('{'):])

    def topic(self,anchor='old'):
        return next(topic for topic in self.social.life.dialogue.context(self.social._scope(CHAT))
                    if topic.anchor_id == anchor)

    async def seed_wait(self,*,age=0,mid='old',text=QUESTION,user='10001'):
        if not age:
            item = self.observe(text,mid,user=user)
            await self.classify(item,action='wait',reason='question')
            return item
        historical = time.time()-age
        clock = SimpleNamespace(time=lambda:historical,monotonic=time.monotonic)
        dialogue = self.social.life.dialogue
        dialogue.clock = lambda:historical
        try:
            with patch('hermes_social.controller.time',clock), patch('hermes_social.life.time',clock), \
                    patch('hermes_social.participation.time',clock):
                item = self.observe(text,mid,user=user)
                await self.classify(item,action='wait',reason='question')
        finally:
            dialogue.clock = time.time
        return item

    async def restart(self):
        await self.social.stop()
        self.social,self.ctx,self.port = self.create()

    def record_ack(self,text,mid,reply_to):
        self.social.record_self(CHAT,text,mid,reply_to_message_id=reply_to)
        self.archive[(CHAT,mid)] = dict(message_id=mid,user_id='90001',role='self',
                                      name='合成机器人',text=text,reply_to=reply_to)

    async def seed_shared_task(self):
        historical = time.time()-900
        clock = SimpleNamespace(time=lambda:historical,monotonic=time.monotonic)
        dialogue = self.social.life.dialogue
        dialogue.clock = lambda:historical
        try:
            with patch('hermes_social.controller.time',clock), patch('hermes_social.life.time',clock), \
                    patch('hermes_social.participation.time',clock):
                first = self.observe(SHARED_TASK,'shared-root',user='10001')
                await self.classify(first,reason='question')
                self.payload(first)
                self.record_ack('这份合成运输需要哪座展厅、多少箱？','shared-ask','shared-root')
                second = self.observe(SHARED_CONDITION,'shared-condition',user='10002')
                await self.classify(second,anchor_text=SHARED_TASK)
                self.assertIn('这份合成运输需要哪座展厅、多少箱？',
                              json.dumps(self.inputs[-1],ensure_ascii=False))
                self.payload(second)
                self.record_ack(SHARED_ANSWER,'shared-answer','shared-condition')
                self.assertEqual(self.topic('shared-root').awaiting_reply,())
        finally:
            dialogue.clock = time.time

    async def test_cross_member_task_restores_conditions_and_actual_answer_after_restart(self):
        await self.seed_shared_task()
        original = {row['message_id']:row for row in self.social.life.dialogue.reference_metadata(
            self.social._scope(CHAT),['shared-root','shared-ask','shared-condition','shared-answer'])}
        await self.restart()
        correcting = self.observe('更正这份合成运输安排：应按周六办理。','shared-correction',user='10003')
        await self.classify(correcting,reason='correction',topic_action='correct',anchor_text=SHARED_TASK)
        selector = json.dumps(self.inputs[-1],ensure_ascii=False)
        self.assertIn(SHARED_CONDITION,selector)
        self.assertIn(SHARED_ANSWER,selector)
        self.assertEqual(self.port.dispatched,[correcting])
        self.port.read_dialogue_refs.assert_awaited_once()
        chat,ids = self.port.read_dialogue_refs.await_args.args
        self.assertEqual(chat,CHAT)
        self.assertLessEqual(len(ids),8)
        self.assertEqual(set(ids),set(original))
        restored = {row.message_id:row for row in self.social.life.recent.routing_history(self.social._scope(CHAT))}
        self.assertEqual(restored['shared-condition'].sender_id,'10002')
        self.assertEqual(restored['shared-answer'].role,'self')
        self.assertEqual(restored['shared-answer'].sender_id,'')
        self.assertEqual(restored['shared-answer'].reply_to_message_id,'shared-condition')
        for mid,metadata in original.items():
            self.assertEqual(restored[mid].observed_at,metadata['observed_at'])
        view,_ = self.payload(correcting)
        self.assertIn(SHARED_CONDITION,view.text)
        self.assertIn(SHARED_ANSWER,view.text)
        self.assertTrue(view.current())

    async def test_forgetting_restored_member_condition_revokes_shared_correction_context(self):
        await self.seed_shared_task()
        await self.restart()
        correcting = self.observe('更正合成方案中的办理顺序。','shared-correction',user='10003')
        await self.classify(correcting,reason='correction',topic_action='correct',anchor_text=SHARED_TASK)
        view,_ = self.payload(correcting)
        self.assertIn(SHARED_CONDITION,view.text)
        self.assertIn(SHARED_ANSWER,view.text)
        self.social.forget_messages(CHAT,{'shared-condition'})
        self.assertFalse(view.current())
        self.assertFalse(self.port.optional_valid(correcting.source))
        self.assertFalse(self.social.life.dialogue.reference_metadata(self.social._scope(CHAT),
            ['shared-condition','shared-answer']))

    async def test_cross_member_conditions_survive_unrelated_burst_in_selector_and_body(self):
        await self.seed_shared_task()
        dispatched_before = len(self.port.dispatched)
        for index in range(20):
            self.observe('另一话题的合成材料编号'+str(index),'background-'+str(index),user='10004')
        correcting = self.observe('更正这份运输计划：改到周日，其他条件照旧。',
                                  'shared-correction',user='10003')
        await self.classify(correcting,reason='correction',topic_action='correct',anchor_text=SHARED_TASK)
        self.assertEqual(self.port.dispatched[dispatched_before:],[correcting])
        self.assertIn(SHARED_CONDITION,json.dumps(self.inputs[-1],ensure_ascii=False))
        self.assertIn(SHARED_ANSWER,json.dumps(self.inputs[-1],ensure_ascii=False))
        view,payload = self.payload(correcting)
        by_id = {row['message_id']:row for row in payload['messages']}
        self.assertIn(SHARED_CONDITION,view.text)
        self.assertIn(SHARED_ANSWER,view.text)
        self.assertNotEqual(by_id['shared-root']['speaker'],by_id['shared-condition']['speaker'])
        self.assertNotEqual(payload['current_speaker'],by_id['shared-condition']['speaker'])
        topic = next(item for item in payload['dialogue'] if item['anchor'] == 'shared-root')
        self.assertIn('shared-condition',topic['messages'])
        self.assertIn('shared-answer',topic['messages'])

    async def test_addressed_cross_member_correction_also_restores_shared_topic(self):
        await self.seed_shared_task()
        await self.restart()
        correcting = self.observe('请按更正后的合成运输日期继续，其他条件照旧。',
                                  'addressed-correction',user='10003')
        correcting.reply_expected = True
        original = self.ctx.llm._async_caller
        async def provider(**kwargs):
            payload = json.loads(kwargs['messages'][-1]['content'])
            root = next(row['ref'] for row in payload['messages'] if row['text'] == SHARED_TASK)
            self.ctx.selection_response = dict(selected='c0',assessments=[dict(candidate='c0',
                audience='bot',action='respond',topic=root,reason='correction',topic_action='correct')])
            return await original(**kwargs)
        self.ctx.llm._async_caller = provider
        self.assertTrue(await self.social.prepare_direct_dialogue(correcting))
        self.assertFalse(self.port.dispatched)
        view,_ = self.payload(correcting)
        self.assertIn(SHARED_CONDITION,view.text)
        self.assertIn(SHARED_ANSWER,view.text)
        self.assertTrue(view.current())

    async def test_native_ingress_recovers_context_without_reviving_old_deferred_sources(self):
        await self.seed_wait(age=900)
        await self.restart()
        observed = self.observe('一条有当前通道许可的合成点名请求。','native-fresh')
        native = copy.copy(observed)
        native.source = copy.copy(observed.source)
        native.reply_expected = True
        self.port.public_ingress_current = lambda item: item is native
        history,topics,pending = await self.social.life.selection_context(CHAT,(native,),())
        self.assertTrue(topics)
        self.assertFalse(pending)
        self.assertIn(QUESTION,[row.text for row in history])
        self.assertFalse(self.port.dispatched)
        self.assertFalse(self.port.optional_valid(native.source))
        self.port.read_dialogue_refs.assert_awaited_once()

    async def test_wait_is_only_context_for_new_source_and_ack_resolves_old_question(self):
        old = await self.seed_wait()
        self.assertFalse(self.port.dispatched)
        fresh = self.observe('回到刚才那个合成问题，我倾向乙。','fresh',user='10002')
        await self.classify(fresh,anchor_text=QUESTION)
        self.assertEqual(self.port.dispatched,[fresh])
        self.assertIs(self.port.dispatched[0].source,fresh.source)
        self.assertFalse(self.port.optional_valid(old.source))
        self.assertEqual(self.topic().awaiting_reply,('old',))
        view,payload = self.payload(fresh)
        self.assertTrue(view.current())
        self.assertEqual(payload['participation']['topic_anchor'],'old')
        self.assertIn(QUESTION,view.text)
        self.assertEqual(self.topic().awaiting_reply,('old',))
        self.social.record_self(CHAT,'已按你的新补充继续讨论。','ack',reply_to_message_id='fresh')
        self.assertEqual(self.topic().awaiting_reply,())
        self.assertEqual(self.social.life.last_error,'')

    async def test_no_reply_and_generation_alone_do_not_clear_old_awaiting(self):
        await self.seed_wait()
        fresh = self.observe('接着刚才的问题继续。','fresh')
        await self.classify(fresh,anchor_text=QUESTION)
        self.payload(fresh)
        self.assertEqual(self.topic().awaiting_reply,('old',))
        self.social.record_self(CHAT,'NO_REPLY','not-an-actual-ack',reply_to_message_id='fresh')
        self.assertEqual(self.topic().awaiting_reply,('old',))
        self.assertFalse(self.social.life.dialogue.reference_metadata(self.social._scope(CHAT),['not-an-actual-ack']))

    async def test_close_and_reopen_follow_classifier_context_and_sent_reply(self):
        await self.seed_wait()
        closing = self.observe('这件合成事项先到这里。','closing')
        await self.classify(closing,action='observe',reason='closing',topic_action='close',anchor_text=QUESTION)
        self.assertFalse(self.port.dispatched)
        self.assertEqual(self.topic().status,'closed')
        self.assertEqual(self.topic().awaiting_reply,())
        _,closed_payload = self.payload(closing)
        self.assertTrue(any(row['state'] == 'closed' for row in closed_payload['dialogue']))
        reopening = self.observe('我们现在再讨论刚才那份合成方案。','reopening')
        await self.classify(reopening,reason='question',topic_action='reopen',anchor_text=QUESTION)
        self.assertEqual(self.port.dispatched,[reopening])
        self.assertEqual(self.topic().status,'active')
        self.assertEqual(self.topic().awaiting_reply,('reopening',))
        _,opened_payload = self.payload(reopening)
        self.assertTrue(any(row['state'] == 'active' and row['awaiting'] == ['reopening']
                            for row in opened_payload['dialogue']))
        self.social.record_self(CHAT,'这次接着你重新提出的合成问题。','reopened-ack',reply_to_message_id='reopening')
        self.assertEqual(self.topic().awaiting_reply,())

    async def test_close_and_respond_conflict_does_not_send_or_close_old_wait(self):
        await self.seed_wait()
        closing = self.observe('这件合成事项先到这里。','closing')
        await self.classify(closing,reason='followup',topic_action='close',anchor_text=QUESTION)
        self.assertFalse(self.port.dispatched)
        self.assertEqual(self.topic().status,'active')
        self.assertEqual(self.topic().awaiting_reply,('old',))

    async def test_correction_supersedes_old_wait_and_ack_resolves_corrected_question(self):
        await self.seed_wait()
        correction = self.observe('更正一下，合成要求应该使用方案丙。','correction')
        await self.classify(correction,action='wait',reason='correction',topic_action='correct',anchor_text=QUESTION)
        self.assertEqual(self.topic().awaiting_reply,('correction',))
        trigger = self.observe('请根据更正后的合成要求继续。','fresh')
        await self.classify(trigger,anchor_text=self.archive[(CHAT,'correction')]['text'])
        self.assertEqual(self.port.dispatched,[trigger])
        _,payload = self.payload(trigger)
        self.assertEqual(payload['participation']['topic_anchor'],'correction')
        self.assertEqual(self.topic().awaiting_reply,('correction',))
        self.social.record_self(CHAT,'已采用方案丙这条更正继续。','corrected-ack',reply_to_message_id='fresh')
        self.assertEqual(self.topic().awaiting_reply,())

    async def test_burst_beyond_twelve_rows_keeps_old_topic_and_deferred_ref(self):
        await self.seed_wait()
        for index in range(20):
            self.observe('另一个合成话题'+str(index),'background-'+str(index),user='10003')
        fresh = self.observe('接续尚未完成的合成问题。','fresh',user='10002')
        await self.classify(fresh,anchor_text=QUESTION)
        selector = self.inputs[-1]
        old_ref = next(row['ref'] for row in selector['messages'] if row['text'] == QUESTION)
        self.assertTrue(any(row['ref'] == old_ref for row in selector['deferred']))
        self.assertTrue(any(row['anchor'] == old_ref for row in selector['topics']))
        self.assertEqual(self.port.dispatched,[fresh])
        view,payload = self.payload(fresh)
        self.assertIn(QUESTION,view.text)
        self.assertEqual(payload['participation']['topic_anchor'],'old')

    async def test_restart_fetches_only_bound_refs_and_preserves_original_author_and_time(self):
        await self.seed_wait(age=900,user='10002')
        metadata = self.social.life.dialogue.reference_metadata(self.social._scope(CHAT),['old'])[0]
        await self.restart()
        fresh = self.observe('我们继续那个尚未回答的合成问题。','fresh',user='10003')
        await self.classify(fresh,anchor_text=QUESTION)
        self.assertEqual(self.port.dispatched,[fresh])
        self.port.read_dialogue_refs.assert_awaited_once()
        chat,ids = self.port.read_dialogue_refs.await_args.args
        self.assertEqual(chat,CHAT)
        self.assertEqual(set(ids),{'old'})
        restored = next(row for row in self.social.life.recent.routing_history(self.social._scope(CHAT))
                        if row.message_id == 'old')
        self.assertEqual(restored.sender_id,'10002')
        self.assertEqual(restored.observed_at,metadata['observed_at'])
        self.assertLess(restored.observed_at,time.time()-850)
        self.assertEqual(self.payload(fresh)[1]['participation']['topic_anchor'],'old')

    async def reject_restored_metadata(self,invalid):
        await self.seed_wait(age=900,user='10002')
        await self.restart()
        self.archive[(CHAT,'old')].update(invalid)
        fresh = self.observe('一条新的合成分享。','fresh')
        await self.classify(fresh,reason='sharing')
        self.assertNotIn(QUESTION,json.dumps(self.inputs[-1],ensure_ascii=False))

    async def test_restart_rejects_wrong_role_in_restored_document(self):
        await self.reject_restored_metadata({'role':'self'})

    async def test_restart_rejects_wrong_actor_in_restored_document(self):
        await self.reject_restored_metadata({'user_id':'10003'})

    async def test_restart_cannot_replace_original_transport_quote(self):
        await self.seed_wait(age=900)
        original = self.social.life.dialogue.reference_metadata(self.social._scope(CHAT),['old'])[0]
        await self.restart()
        self.archive[(CHAT,'old')]['reply_to'] = 'forged-transport-quote'
        fresh = self.observe('一条新的合成分享。','fresh')
        await self.classify(fresh,reason='sharing')
        restored = [row for row in self.social.life.recent.routing_history(self.social._scope(CHAT))
                    if row.message_id == 'old']
        # Rejecting a conflicting fetch or retaining only the original verified
        # quote are both safe. A freshly fetched quote must never replace it.
        for row in restored:
            self.assertEqual(str(row.reply_to_message_id or ''),original['reply_to'])

    async def test_other_chat_ingress_cannot_restore_or_route_old_wait(self):
        await self.seed_wait(age=900)
        await self.restart()
        fresh = self.observe('另一个群的全新合成分享。','other-fresh',chat=OTHER)
        await self.classify(fresh,reason='sharing')
        self.assertEqual(self.port.dispatched,[fresh])
        self.port.read_dialogue_refs.assert_not_awaited()
        self.assertNotIn(QUESTION,json.dumps(self.inputs[-1],ensure_ascii=False))
        self.assertEqual(self.topic().awaiting_reply,('old',))

    async def test_no_fresh_ingress_cannot_restore_pending_or_fetch_old_refs(self):
        old = await self.seed_wait(age=900)
        await self.restart()
        history,topics,pending = await self.social.life.selection_context(CHAT,(old,),())
        self.assertEqual(history,())
        self.assertTrue(topics)
        self.assertEqual(pending,())
        self.port.read_dialogue_refs.assert_not_awaited()

    async def test_forget_after_restore_revokes_context_and_old_resolution(self):
        await self.seed_wait(age=900)
        await self.restart()
        fresh = self.observe('回到那个合成问题。','fresh')
        await self.classify(fresh,anchor_text=QUESTION)
        view,_ = self.payload(fresh)
        self.assertTrue(view.current())
        self.social.forget_messages(CHAT,{'old'})
        self.assertFalse(view.current())
        self.assertFalse(self.social.life.dialogue.reference_metadata(self.social._scope(CHAT),['old','fresh']))
        self.assertFalse(self.port.optional_valid(fresh.source))

    async def test_dialogue_proactive_is_off_until_explicitly_configured(self):
        await self.seed_wait(age=2000)
        self.assertFalse(self.social.policy.dialogue['proactive'])
        self.assertIsNone(await self.social.life.dialogue_opportunity(CHAT,frozenset()))
        self.assertFalse(self.port.attempts)

    async def test_unquoted_actual_contact_ack_resolves_old_wait_without_fabricating_quote(self):
        await self.seed_wait(age=2000)
        self.social.policy.dialogue['proactive'] = True
        self.ctx.response = {'speak':True,'text':'之前那份合成方案还没有接完，现在方便继续聊吗？'}
        for state in self.social.states.values():
            state.last_activity = time.time()-4000
        self.assertTrue(await self.social.tick(CHAT))
        self.assertEqual(self.topic().awaiting_reply,())
        receipt = self.port.last_receipt(CHAT)
        self.assertIsNotNone(receipt)
        metadata = self.social.life.dialogue.reference_metadata(self.social._scope(CHAT),[receipt])[0]
        self.assertEqual(metadata['role'],'self')
        self.assertEqual(metadata['reply_to'],'')
        self.assertEqual(self.social.life.last_error,'')
        self.assertEqual(len(self.social.contacts.rows(CHAT)),1)


if __name__ == '__main__':
    unittest.main()
