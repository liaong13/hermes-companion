"""Behavioral regressions for opt-in public style and social-only context.

The real model is exercised separately by verify_napcat_social_live.py. These
local tests cover source/target authority, prompt transport and timing only.
"""
import copy
from types import SimpleNamespace
from typing import Any
import unittest

import test_proactive_lifecycle as life
from hermes_napcat.isolation import AdmissionDenied
from social_plugin_helpers import optional_valid


class PublicConversation(unittest.IsolatedAsyncioTestCase):
    adapter: Any
    social: Any
    dispatched: list
    asyncSetUp = life.Lifecycle.asyncSetUp
    asyncTearDown = life.Lifecycle.asyncTearDown
    parsed = life.Lifecycle.parsed
    issue_ingress = life.Lifecycle.issue_ingress
    start_group = life.Lifecycle.start_group
    drain_ambient = life.Lifecycle.drain_ambient

    async def test_public_style_requires_exact_live_source_and_uses_configured_name(self):
        self.social.policy.public_self = {'name':'另一实例'}
        self.social.observe(self.parsed('PRIVATE_STYLE_CANARY', mid=30, private=True))
        incoming = self.parsed('公开名字你好', mid=31)
        await self.start_group(incoming)
        try:
            prompt = self.adapter.conversation_prompt_for_source(incoming.source)
            self.assertIn('另一实例', prompt)
            self.assertNotIn('公开名字', prompt)
            self.assertNotIn('PRIVATE_STYLE_CANARY', prompt)
            self.assertNotIn('UNCONFIGURED_ROLE_CANARY', prompt)
            self.assertNotIn('PRIVATE_IDENTITY_CANARY', prompt)
            with self.assertRaises(AdmissionDenied):
                self.adapter.conversation_prompt_for_source(copy.deepcopy(incoming.source))
        finally:
            await self.adapter.on_processing_complete(incoming, 'completed')
        with self.assertRaises(AdmissionDenied):
            self.adapter.conversation_prompt_for_source(incoming.source)

    async def test_non_opted_in_group_keeps_original_prompt(self):
        incoming = self.adapter.convert_event(life.raw_text('公开名字你好', 32, group=20002))
        await self.start_group(incoming)
        try:
            self.assertEqual(self.adapter.conversation_prompt_for_source(incoming.source), '')
        finally:
            await self.adapter.on_processing_complete(incoming, 'completed')

    async def test_owner_dm_style_is_live_and_contains_no_group_participation(self):
        self.social.policy.public_self = {'name':'另一实例'}
        prompts = []
        async def receive(incoming):
            await self.adapter.on_processing_start(incoming)
            try:
                prompts.append(self.adapter.conversation_prompt_for_source(incoming.source))
            finally:
                await self.adapter.on_processing_complete(incoming, 'completed')
        self.adapter.handle_message = receive
        await self.adapter._receive(life.raw_text('你好', 33, private=True))
        self.assertEqual(len(prompts), 1)
        self.assertIn('另一实例', prompts[0])
        self.assertNotIn('公开名字', prompts[0])
        self.assertNotIn('NO_REPLY', prompts[0])

    async def test_pause_keeps_addressed_style_but_revokes_ambient_style(self):
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨要怎么安排？', 34))
        await self.drain_ambient()
        candidate = self.dispatched[0]
        await self.start_group(candidate)
        try:
            self.assertTrue(self.adapter.conversation_prompt_for_source(candidate.source))
            self.social.control(self.parsed('/napcat-social pause', mid=35))
            with self.assertRaises(AdmissionDenied):
                self.adapter.conversation_prompt_for_source(candidate.source)
        finally:
            await self.adapter.on_processing_complete(candidate, 'completed')
        addressed = self.parsed('公开名字还是可以正常聊天', mid=36)
        await self.start_group(addressed)
        try:
            self.assertTrue(self.adapter.conversation_prompt_for_source(addressed.source))
        finally:
            await self.adapter.on_processing_complete(addressed, 'completed')

    async def test_typed_notifications_do_not_become_social_context(self):
        incoming = self.parsed('公开名字你好', mid=37)
        await self.start_group(incoming)
        try:
            original = self.social.states['group:20001'].generation
            for metadata in ({'non_conversational': True}, {'non_conversational': True, '_interim_send': True}):
                result = await self.adapter.send('group:20001', 'SYSTEM_OR_PROGRESS_CANARY',
                                                 reply_to='37', metadata=metadata)
                self.assertTrue(result.success)
            self.assertEqual(self.social.states['group:20001'].generation, original)
            self.assertEqual(self.social.context('group:20001'), [])
            result = await self.adapter.send('group:20001', '我有个不同的想法。', reply_to='37',
                                             metadata={'_interim_send': True})
            self.assertTrue(result.success)
            self.assertEqual(self.social.states['group:20001'].generation, original)
            self.assertEqual(self.social.context('group:20001'), [])
            result = await self.adapter.send('group:20001', '这次是真的接着聊天。', reply_to='37')
            self.assertTrue(result.success)
            self.assertEqual([row['text'] for row in self.social.context('group:20001')],
                             ['这次是真的接着聊天。'])
        finally:
            await self.adapter.on_processing_complete(incoming, 'completed')

    async def test_ambient_context_does_not_duplicate_current_message(self):
        await self.adapter._receive(life.raw_text('周末露营天气预报说会下雨。', 38))
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨要怎么安排？', 39))
        await self.drain_ambient()
        actual = self.dispatched[0]
        self.assertEqual(actual.message_id, '39')
        self.assertEqual(actual.text.count('大家觉得周末露营下雨要怎么安排？'), 1)
        await self.start_group(actual)
        try:
            prompt = self.adapter.conversation_prompt_for_source(actual.source)
            self.assertIn('周末露营天气预报说会下雨。',prompt)
            self.assertNotIn('大家觉得周末露营下雨要怎么安排？',prompt)
        finally:
            await self.adapter.on_processing_complete(actual,'completed')
        self.assertNotIn('PRIVATE_STYLE_CANARY', actual.text)
        self.assertEqual(actual.source.user_id, '10001')
        self.assertFalse(actual.reply_expected)

    async def test_short_answer_after_actual_sent_bot_question_reenters_optional_turn(self):
        await self.adapter._receive(life.raw_text('合成活动应该采用哪一份方案？', 45))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), 1)
        original = self.dispatched[0]
        await self.start_group(original)
        try:
            receipt = await self.adapter.send('group:20001', '你选择选项甲还是选项乙？', reply_to='45')
            self.assertTrue(receipt.success)
            sent = self.social.recent.routing_history(self.social._scope('group:20001'))[-1]
            self.assertEqual((sent.role, sent.reply_to_message_id), ('self', '45'))
        finally:
            await self.adapter.on_processing_complete(original, 'completed')
        await self.adapter._receive(life.raw_text('选项乙', 46))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), 2)
        continued = self.dispatched[-1]
        self.assertEqual(continued.message_id, '46')
        self.assertFalse(continued.reply_expected)
        self.assertEqual(continued.text,'选项乙')
        await self.start_group(continued)
        try:
            prompt = self.adapter.conversation_prompt_for_source(continued.source)
            self.assertIn('你选择选项甲还是选项乙？',prompt)
            self.assertIn('recent_exchange',prompt)
        finally:
            await self.adapter.on_processing_complete(continued,'completed')

    async def test_auto_reset_notice_keeps_ambient_live_without_opening_followup(self):
        from gateway.run_turn_prepare import GatewayTurnPrepareMixin
        await self.adapter._receive(life.raw_text('还可以继续聊吗？', 43))
        await self.drain_ambient()
        incoming = self.dispatched[0]
        await self.start_group(incoming)
        state = self.social.states['group:20001']
        generation, activity, rows = state.generation, state.last_activity, self.social.context('group:20001')
        runner = SimpleNamespace(_delivery_adapter_for=lambda source: self.adapter,
                                 _thread_metadata_for_source=lambda source: None,
                                 _reset_notice_session_info=lambda source: '')
        try:
            await GatewayTurnPrepareMixin._hmwa_deliver_auto_reset_notice(
                runner, SimpleNamespace(auto_reset_reason='suspended'), incoming.source, [])
            self.assertEqual([action for action, _ in self.sent], ['send_group_msg'])
            self.assertEqual(state.generation, generation)
            self.assertEqual(state.last_activity, activity)
            self.assertEqual(state.last_reply_at, 0)
            self.assertEqual(self.social.context('group:20001'), rows)
            self.assertTrue(optional_valid(self.adapter, incoming.source))
            self.assertTrue(self.adapter.conversation_prompt_for_source(incoming.source))
        finally:
            await self.adapter.on_processing_complete(incoming, 'completed')

    async def test_separate_runtime_footer_is_not_social_activity(self):
        from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨要怎么安排？', 44))
        await self.drain_ambient()
        incoming = self.dispatched[0]
        await self.start_group(incoming)
        state = self.social.states['group:20001']
        generation, rows = state.generation, self.social.context('group:20001')
        runner = SimpleNamespace(_delivery_adapter_for=lambda source: self.adapter,
                                 _should_send_voice_reply=lambda *args, **kwargs: False,
                                 _event_thread_metadata=lambda event, source: None)
        try:
            result = await GatewayTurnPersistenceMixin._hmwa_deliver_turn_response(
                runner, incoming, incoming.source, SimpleNamespace(session_id='synthetic-footer'),
                'synthetic-session', 1, {'already_sent': True, 'media_already_delivered': True},
                [], '正文已通过流式送达', 'RUNTIME_FOOTER_CANARY', False)
            self.assertIsNone(result)
            self.assertEqual([action for action, _ in self.sent], ['send_group_msg'])
            self.assertEqual(state.generation, generation)
            self.assertEqual(state.last_reply_at, 0)
            self.assertEqual(self.social.context('group:20001'), rows)
            self.assertTrue(optional_valid(self.adapter, incoming.source))
        finally:
            await self.adapter.on_processing_complete(incoming, 'completed')

    async def test_followup_uses_debounce_not_cold_join_cooldown(self):
        state = self.social.states['group:20001']
        self.social.policy.ambient_debounce_seconds = 3
        self.social.policy.ambient_cooldown_seconds = 20
        state.ambient_at = 1000
        self.assertEqual(self.social.ambient_delay('group:20001', now=1001), 19)
        self.social.record_self('group:20001', '正在进行的正常对话', '41')
        state.ambient_at = state.last_activity
        self.assertEqual(self.social.ambient_delay('group:20001', now=state.last_activity+1), 3)
        state.ambient_at = state.last_activity+200
        self.assertEqual(self.social.ambient_delay('group:20001', now=state.ambient_at+1), 19)
        self.assertNotIn('正在进行', self.social.path.read_text() if self.social.path.exists() else '')


if __name__ == '__main__':
    unittest.main()
