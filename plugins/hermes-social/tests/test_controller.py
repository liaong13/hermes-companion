# pyright: reportMissingImports=false
import asyncio
import copy
from contextvars import ContextVar
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import support
from support import FakeContext, FakePort, build, event, policy
from hermes_social import SocialController
from hermes_social.binding import SocialBinding
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.token = set_hermes_home_override(self.home)
        self.ctx, self.port = FakeContext(), FakePort(self.home)
        self.social = build(self.ctx, self.port)
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0
        self.jobs = []
        self.idle()

    async def asyncTearDown(self):
        self.ctx.release.set()
        await self.social.stop()
        for job in self.jobs:
            if not job.done():
                job.cancel()
        if self.jobs:
            await asyncio.gather(*self.jobs, return_exceptions=True)
        self.ctx.unload()
        await asyncio.sleep(0)
        if self.social.binding.shutdown_task:
            await self.social.binding.shutdown_task
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def idle(self):
        for state in self.social.states.values():
            state.last_activity = time.time() - 4000

    def job(self, coro):
        task = asyncio.create_task(coro)
        self.jobs.append(task)
        return task

    async def drain(self):
        tasks = list(self.social.ambient_tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)

    async def test_real_host_facade_claim_consumption_send_and_receipt(self):
        original = self.port.claim_idle
        def claim(chat, task):
            self.assertFalse(self.ctx.calls)
            return original(chat, task)
        with patch.object(self.port, 'claim_idle', side_effect=claim):
            self.assertTrue(await self.social.tick('group:20001'))
        self.assertEqual(self.port.sequence, ['claim', 'consume', 'send', 'readback'])
        self.assertEqual(self.social.counters['sent'], 1)
        self.assertEqual(self.social.last_receipts['group:20001'], self.port.last_receipt('group:20001'))
        kwargs, chat, session, home = self.ctx.calls[0]
        self.assertEqual((chat, home), ('group:20001', str(self.home)))
        for key in ('tools', 'model', 'provider', 'profile', 'agent_id'):
            self.assertNotIn(key, kwargs)
        self.assertEqual(len(self.port.prepared), 1)
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(len(self.ctx.calls), 1)

    async def test_group_and_dm_contexts_are_isolated_without_private_files(self):
        (self.home / 'SOUL.md').write_text('PRIVATE_SOUL_CANARY')
        (self.home / 'USER.md').write_text('PRIVATE_USER_CANARY')
        (self.home / 'MEMORY.md').write_text('PRIVATE_MEMORY_CANARY')
        self.social.observe(event('DM_PRIVATE_CANARY', chat='dm:10001'))
        self.social.observe(event('OTHER_GROUP_CANARY', chat='group:20002'))
        self.social.observe(event('GROUP_PUBLIC_CANARY', mid='2'))
        self.idle()
        self.assertTrue(await self.social.tick('group:20001'))
        request = json.dumps(self.ctx.calls[-1][0], ensure_ascii=False)
        self.assertIn('GROUP_PUBLIC_CANARY', request)
        for canary in ['DM_PRIVATE_CANARY', 'OTHER_GROUP_CANARY', 'PRIVATE_SOUL_CANARY',
                       'PRIVATE_USER_CANARY', 'PRIVATE_MEMORY_CANARY']:
            self.assertNotIn(canary, request)
        self.assertTrue(await self.social.tick('dm:10001'))
        private_request = json.dumps(self.ctx.calls[-1][0], ensure_ascii=False)
        self.assertIn('DM_PRIVATE_CANARY', private_request)
        self.assertNotIn('GROUP_PUBLIC_CANARY', private_request)
        self.assertNotIn('PRIVATE_SOUL_CANARY', private_request)

    async def test_private_contextvars_do_not_reach_model_completion(self):
        secret = ContextVar('private_history_canary', default='empty')
        secret.set('PRIVATE_CONTEXT_CANARY')
        values = []
        original = self.ctx.llm._async_caller
        assert original is not None
        async def caller(**kwargs):
            values.append(secret.get())
            return await original(**kwargs)
        self.ctx.llm._async_caller = caller
        from gateway.session_context import set_session_vars
        set_session_vars(session_id='PRIVATE_SESSION_CANARY', cwd='/private-path')
        self.assertTrue(await self.social.tick('group:20001'))
        self.assertEqual(values, ['empty'])
        self.assertNotEqual(self.ctx.calls[0][2], 'PRIVATE_SESSION_CANARY')

    async def test_silence_is_bounded_without_send(self):
        self.ctx.response = {'speak': False, 'text': ''}
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertFalse(self.port.attempts)
        self.assertEqual(self.social.counters['silent'], 1)

    async def test_no_idle_target_is_inferred_from_participation_groups(self):
        self.assertTrue(self.social.allows_unaddressed('group:20002'))
        self.assertFalse(self.social.due('group:20002'))
        self.assertFalse(await self.social.tick('group:20002'))
        self.assertFalse(self.ctx.calls)

    async def test_intentional_silence_marker_is_not_sent_as_idle_text(self):
        self.ctx.response = {'speak': True, 'text': 'NO_REPLY'}
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(self.social.counters['silent'], 1)
        self.assertFalse(self.port.attempts)

    async def test_no_idle_while_busy_paused_quiet_or_daily_exhausted(self):
        state = self.social.states['group:20001']
        self.social.busy['group:20001'] = 1
        self.assertFalse(self.social.due('group:20001'))
        self.social.busy.clear()
        state.paused = True
        self.assertFalse(self.social.due('group:20001'))
        self.assertTrue(self.social.observes('group:20001'))
        self.assertFalse(self.social.allows_unaddressed('group:20001'))
        state.paused = False
        with patch.object(self.social.policy, 'quiet', return_value=True):
            self.assertFalse(self.social.due('group:20001'))
        state.day = self.social._day(time.time())
        state.daily = self.social.policy.daily_limit
        self.assertFalse(self.social.due('group:20001'))

    async def test_response_cannot_select_target_or_add_side_effect_fields(self):
        for result in [{'speak': True, 'text': 'test', 'target': 'group:20002'},
                       {'speak': 1, 'text': 'test'}, {'speak': True, 'text': 'x' * 1001}, []]:
            self.ctx.response = result
            self.social.states['group:20001'].last_check = 0
            with self.subTest(result=result), self.assertRaises(ValueError):
                await self.social.tick('group:20001')
        self.assertFalse(self.port.attempts)

    async def test_ambiguous_send_is_consumed_and_survives_restart_without_retry(self):
        self.port.ambiguous = True
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(len(self.port.attempts), 1)
        replacement = SocialController(self.port, self.social.policy, self.social.binding)
        replacement.states['group:20001'].last_activity = time.time() - 4000
        self.assertFalse(replacement.due('group:20001'))
        self.assertEqual(replacement.states['group:20001'].daily, 1)
        self.assertTrue(replacement.states['group:20001'].unanswered)
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(len(self.port.attempts), 1)

    async def test_port_denial_and_prepare_denial_cannot_call_model(self):
        self.port.prepare_denied = True
        with self.assertRaises(ValueError):
            await self.social.tick('group:20001')
        self.assertFalse(self.ctx.calls)
        self.port.prepare_denied = False
        self.port.claim_denied = True
        self.social.states['group:20001'].last_check = 0
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertFalse(self.ctx.calls)

    async def test_final_port_boundary_rechecks_after_controller_check(self):
        self.port.before_send = lambda: self.social.observe(event('新消息', mid='7'))
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertFalse(self.port.attempts)
        self.assertFalse(self.social.states['group:20001'].unanswered)

    async def test_parallel_tick_does_not_duplicate_model_or_send(self):
        self.ctx.delayed = True
        first = self.job(self.social.tick('group:20001'))
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        self.assertFalse(await self.social.tick('group:20001'))
        self.ctx.release.set()
        self.assertTrue(await first)
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertEqual(len(self.port.attempts), 1)

    async def stale(self, reason):
        self.ctx.delayed = True
        task = self.job(self.social.tick('group:20001'))
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        if reason == 'message':
            self.social.observe(event('后来消息', mid='7'))
        elif reason == 'self':
            self.social.record_self('group:20001', '正常回复', '8')
        elif reason == 'recall':
            self.social.recall('group:20001', '1')
        elif reason == 'pause':
            self.social.control(event('/social pause', mid='9', user='10002'))
        elif reason == 'epoch':
            self.port.connection += 1
        elif reason == 'disconnect':
            self.port.online = False
            self.social.reset_connection()
        elif reason == 'unload':
            self.ctx.unload()
        self.ctx.release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(result[0] is False or isinstance(result[0], asyncio.CancelledError))
        self.assertFalse(self.port.attempts)

    async def test_new_message_stales_inflight_idle(self): await self.stale('message')
    async def test_real_self_reply_stales_inflight_idle(self): await self.stale('self')
    async def test_recall_stales_inflight_idle(self): await self.stale('recall')
    async def test_pause_stales_inflight_idle(self): await self.stale('pause')
    async def test_epoch_change_stales_inflight_idle(self): await self.stale('epoch')
    async def test_disconnect_stales_inflight_idle(self): await self.stale('disconnect')
    async def test_unload_stales_inflight_idle(self): await self.stale('unload')

    async def test_optional_coalesces_real_source_then_issues_port_grant_before_queue(self):
        events = [event('大家觉得' + ('真实消息' + str(mid) if mid % 2 else '跑步路线')
                        + '怎么样？', mid=str(mid), user='10001' if mid % 2 else '10002')
                  for mid in range(1, 4)]
        for item in events:
            self.social.observe(item)
            self.social.queue_ambient(item)
        await self.drain()
        self.assertEqual(self.port.dispatched, [events[-1]])
        self.assertIs(self.port.dispatched[0].source, events[-1].source)
        self.assertEqual(self.port.sequence, ['issue', 'dispatch'])
        self.assertEqual(events[-1].text,'大家觉得真实消息3怎么样？')
        self.assertIn('真实消息1',self.social.prepare_public_context(events[-1]).text)
        self.assertFalse(events[-1].reply_expected)
        self.assertFalse(events[-1].allow_gateway_control)
        self.assertFalse(events[-1].internal)
        self.assertTrue(self.port.optional_valid(events[-1].source))
        self.assertFalse(self.port.optional_valid(copy.copy(events[-1].source)))
        self.assertFalse(self.ctx.calls)

    async def test_optional_uses_ephemeral_speaker_aliases_not_qq_ids(self):
        first = event('露营天气怎么样？', mid='speaker-a', user='10001', name='同名')
        second = event('露营下雨怎么办？', mid='speaker-b', user='10002', name='同名')
        current = event('大家觉得露营下雨怎么办？', mid='speaker-c', user='10001', name='同名')
        for item in (first, second, current):
            self.social.observe(item)
        self.social.queue_ambient(current)
        await self.drain()
        self.assertEqual(self.port.dispatched, [current])
        view = self.social.prepare_public_context(current)
        payload = json.loads(view.text[view.text.index('{'):])
        self.assertEqual(payload['current_speaker'], '发言者1')
        self.assertEqual({row['speaker'] for row in payload['messages']}, {'发言者1', '发言者2'})
        self.assertNotIn('10001', view.text)
        self.assertNotIn('10002', view.text)

    async def test_unread_image_is_only_presence_not_content_in_recent_context(self):
        item = event('[attachment:image]', mid='image-1')
        item.unread_image = True
        self.social.observe(item)
        rows = self.social.context('group:20001')
        self.assertEqual(len(rows), 1)
        self.assertIn('图片内容未识别', rows[0]['text'])
        self.assertNotIn('图里写着', rows[0]['text'])

        self.assertFalse(self.social.record_image_context('group:20001', 'other', '别人的图'))
        self.assertFalse(self.social.record_image_context('group:20002', 'image-1', '别群的图'))
        self.assertTrue(self.social.record_image_context('group:20001', 'image-1', '红色方块'))
        updated = self.social.context('group:20001')[0]['text']
        self.assertIn('模型派生的不可信内容', updated)
        self.assertIn('红色方块', updated)
        self.assertNotIn('内容未识别', updated)
        self.assertFalse(self.social.record_image_context('group:20001', 'image-1', '迟到的新结果'))
        self.assertNotIn('迟到', self.social.context('group:20001')[0]['text'])

    async def test_image_derivative_updates_retained_routing_row_after_16_new_messages(self):
        item = event('[attachment:image]', mid='image-old')
        item.unread_image = True
        self.social.observe(item)
        for index in range(16):
            self.social.observe(event('其他公开话题', mid=f'later-{index}'))
        scope = self.social._scope('group:20001')
        self.assertFalse(any(row.message_id == 'image-old' for row in self.social.recent.read(scope)))
        self.assertTrue(self.social.record_image_context('group:20001', 'image-old', '红色方块'))
        old = next(row for row in self.social.recent.routing_history(scope) if row.message_id == 'image-old')
        self.assertIn('红色方块', old.text)
        self.assertNotIn('图片内容未识别', old.text)
        self.social.recall('group:20001', 'image-old')
        self.assertFalse(self.social.record_image_context('group:20001', 'image-old', '迟到结果'))

    async def test_evicted_image_is_not_recreated_by_late_derivative(self):
        item = event('[attachment:image]', mid='image-evicted')
        item.unread_image = True
        self.social.observe(item)
        for index in range(64):
            self.social.observe(event('其他公开话题', mid=f'newer-{index}'))
        self.assertFalse(self.social.record_image_context('group:20001', 'image-evicted', '迟到结果'))

    async def test_image_recognition_cannot_resurrect_recalled_or_paused_observation(self):
        item = event('[attachment:image]', mid='image-2')
        item.unread_image = True
        self.social.observe(item)
        self.social.states['group:20001'].paused = True
        self.assertFalse(self.social.record_image_context('group:20001', 'image-2', '不应保存'))
        self.social.states['group:20001'].paused = False
        self.social.recall('group:20001', 'image-2')
        self.assertFalse(self.social.record_image_context('group:20001', 'image-2', '迟到结果'))
        self.assertEqual(self.social.context('group:20001'), [])

    async def test_optional_uses_port_revocation_for_all_activity_and_unload(self):
        for reason in ['message', 'self', 'recall', 'pause']:
            item = event('大家觉得这个候选怎么样？', mid=reason)
            self.social.states['group:20001'].paused = False
            self.social.queue_ambient(item)
            await self.drain()
            self.assertTrue(self.port.optional_valid(item.source))
            if reason == 'message': self.social.observe(event('新消息', mid='new'))
            if reason == 'self': self.social.record_self('group:20001', '正常聊天', 'self')
            if reason == 'recall': self.social.recall('group:20001', 'old')
            if reason == 'pause': self.social.control(event('/napcat-social pause'))
            self.assertFalse(self.port.optional_valid(item.source))
            self.assertEqual(self.port.invalidations[-1], 'group:20001')
        self.social.states['group:20001'].paused = False
        self.social.queue_ambient(event('大家觉得最后候选怎么样？', mid='last'))
        await self.drain()
        self.ctx.unload()
        self.assertFalse(self.social.active)
        self.assertTrue(self.port.detached)
        self.assertFalse(self.port.optional)

    async def test_observation_deduplicates_recall_keeps_other_chat(self):
        item = event('公开内容')
        self.social.observe(item)
        self.social.observe(item)
        self.assertEqual(self.social.states['group:20001'].generation, 1)
        self.social.observe(event('私聊内容', chat='dm:10001'))
        self.social.recall('group:20001', '1')
        self.assertEqual(self.social.context('group:20001'), [])
        self.assertEqual(self.social.context('dm:10001')[0]['text'], '私聊内容')

    async def test_pause_resume_scope_and_owner_identity_not_display_name(self):
        self.assertTrue(self.social.recognizes_control('group:20001', '/social pause'))
        self.assertFalse(self.social.recognizes_control('group:20003', '/social pause'))
        self.assertFalse(self.social.recognizes_control('group:20001', '/social pause extra'))
        self.assertFalse(self.social.recognizes_control('group:20001', 'some /social pause'))
        self.assertIn('暂停', self.social.control(event('/social pause', user='10002', name='主人')))
        self.assertTrue(self.social.recognizes_control('group:20001', '/napcat-social resume'))
        self.assertIn('只有已验证主人', self.social.control(event('/social resume', user='10002', name='主人')))
        self.assertFalse(self.social.allows_unaddressed('group:20001'))
        self.assertTrue(self.social.observes('group:20001'))
        self.assertIn('启用', self.social.control(event('/napcat-social resume', user='10001', name='新昵称')))
        self.assertFalse(self.social.states['dm:10001'].paused)
        self.assertIsNone(self.social.control(event('/social pause extra')))

    async def test_processing_is_idempotent_and_late_complete_cannot_clear_new_turn(self):
        old, new = event(mid='3'), event(mid='4')
        self.social.processing_start(old)
        self.social.processing_start(old)
        self.assertEqual(self.social.busy['group:20001'], 1)
        self.social.reset_connection()
        self.social.processing_start(new)
        self.social.processing_complete(old)
        self.assertEqual(self.social.busy['group:20001'], 1)
        self.social.processing_complete(new)
        self.social.processing_complete(new)
        self.assertEqual(self.social.busy['group:20001'], 0)

    async def test_record_self_ignores_silence_and_only_real_reply_opens_followup(self):
        self.social.record_self('group:20001', 'NO_REPLY', '1')
        state = self.social.states['group:20001']
        self.assertEqual(state.last_reply_at, 0)
        self.social.policy.ambient_cooldown_seconds = 20
        state.ambient_at = 100
        self.assertEqual(self.social.ambient_delay('group:20001', now=101), 19)
        self.social.record_self('group:20001', '实质聊天', '2')
        self.assertEqual(self.social.ambient_delay('group:20001'), 0)

    async def test_record_self_keeps_trusted_outbound_reply_anchor(self):
        self.social.observe(event('合成活动应该采用哪一份方案？', mid='41'))
        self.social.record_self('group:20001', '你选择选项甲还是选项乙？', '42', reply_to_message_id='41')
        rows = self.social.recent.routing_history(self.social._scope('group:20001'))
        self.assertEqual(rows[-1].reply_to_message_id, '41')
        self.assertEqual(rows[-1].role, 'self')

    async def test_dependency_forget_is_exact_group_and_idempotent(self):
        self.social.observe(event('本群原文', mid='81'))
        self.social.record_self('group:20001', '派生回复', '82', reply_to_message_id='81')
        self.social.observe(event('另一群同号原文', mid='82', chat='group:20002'))
        state = self.social.states['group:20001']
        generation = state.generation
        self.assertTrue(self.social.forget_messages('group:20001', {'82'}))
        self.assertEqual(state.generation, generation+1)
        self.assertEqual([r['message_id'] for r in self.social.context('group:20001')], ['81'])
        self.assertEqual([r['message_id'] for r in self.social.context('group:20002')], ['82'])
        self.assertFalse(self.social.forget_messages('group:20001', {'82','missing'}))
        self.assertEqual(state.generation, generation+1)
    async def test_public_prompt_is_static_and_contains_no_member_history(self):
        before = self.social.prompt_for_chat('group:20001')
        self.social.observe(event('CHAT_CANARY', name='NAME_CANARY'))
        self.social.control(event('/social pause'))
        after = self.social.prompt_for_chat('group:20001')
        self.assertEqual(before, after)
        self.assertNotIn('公开名字', before)
        self.assertIn('无需问号、关键词或再次点名', before)
        self.assertNotIn('CHAT_CANARY', before)
        self.assertNotIn('NAME_CANARY', before)
        self.assertEqual(self.social.prompt_for_chat('group:20003'), '')

    async def test_controller_has_no_transport_or_legacy_source_ledger(self):
        for attribute in ['adapter', 'worker', 'client', 'ambient_grants', 'issue_ambient',
                          'ambient_valid', 'release_ambient']:
            self.assertFalse(hasattr(self.social, attribute), attribute)
        self.assertIs(self.social.port, self.port)
