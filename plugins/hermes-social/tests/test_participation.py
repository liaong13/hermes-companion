"""Synthetic public batches and lifecycle races; no model or QQ network."""
import asyncio
import copy
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from support import event
import test_controller as base
from hermes_social.context import ContextRecord, ContextScope
from hermes_social.participation import ParticipationFrame


def assessment(candidate='c0', *, audience='group', action='respond', topic=None, reason='sharing'):
    return dict(candidate=candidate, audience=audience, action=action,
                topic=topic or candidate, reason=reason)


def verdict(*rows, selected='c0'):
    return dict(selected=selected, assessments=list(rows or [assessment()]))


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.scope = ContextScope('/tmp/synthetic-social-frame', 'napcat', 'group:20001')
        self.first = event('我画好了合成模型的示意图', mid='share', name='成员甲')
        self.second = event('哈哈', mid='echo', user='10002', name='成员乙')

    def frame(self, events=None, rows=()):
        return ParticipationFrame(events or [self.first, self.second], rows, self.scope)

    def test_selection_has_a_trusted_clock_in_the_configured_timezone(self):
        instant = datetime(2026,10,31,16,tzinfo=timezone.utc).timestamp()
        for zone,expected in (('UTC','2026-10-31'),('Asia/Shanghai','2026-11-01')):
            frame = ParticipationFrame([self.first],(),self.scope,now=instant,timezone=zone)
            self.assertIn('本轮可信当前日期（宿主时钟）：'+expected,frame.messages[0]['content'])
            self.assertNotIn('本轮可信当前日期',frame.messages[1]['content'])

    def test_earlier_share_can_be_selected_without_rewriting_source(self):
        frame = self.frame()
        choice, rows = frame.parse(json.dumps(verdict(assessment(),
            assessment('c1', action='observe', reason='no_contribution'))))
        self.assertIs(choice.event, self.first)
        self.assertIs(choice.event.source, self.first.source)
        self.assertEqual(choice.topic_message_id, 'share')
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.first.text, '我画好了合成模型的示意图')

    def test_silence_does_not_need_a_fabricated_candidate(self):
        choice, _ = self.frame().parse(json.dumps(verdict(
            assessment(audience='member', action='observe', reason='addressed_elsewhere'),
            assessment('c1', audience='unknown', action='wait', reason='unclear'), selected=None)))
        self.assertIsNone(choice)

    def test_invalid_or_incomplete_decisions_are_rejected(self):
        valid = verdict(assessment(), assessment('c1'))
        invalid = [[], {}, {**valid, 'target': 'dm:10001'},
                   verdict(assessment()), verdict(assessment(), assessment()),
                   {**valid, 'selected': 'c99'}, {**valid, 'selected': ['c0']},
                   verdict(assessment(topic='missing'), assessment('c1')),
                   verdict(assessment(audience='member'), assessment('c1')),
                   verdict(assessment(reason='closing'), assessment('c1')),
                   verdict(assessment(action='observe'), assessment('c1')),
                   verdict({**assessment(action='wait', reason='unclear'), 'topic_action':'close'}, assessment('c1')),
                   verdict({**assessment(), 'topic_action':'reopen'}, assessment('c1')),
                   verdict({**assessment(), 'tools': ['terminal']}, assessment('c1')),
                   verdict(assessment(topic='c1'), assessment('c1'))]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.frame().parse(json.dumps(response))

    def test_malformed_json_and_oversized_response_are_rejected(self):
        for text in ['```json\n{}\n```', 'invalid', 'x'*12001]:
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                self.frame().parse(text)

    def test_cross_chat_source_and_source_actor_mismatch_are_rejected(self):
        for candidate in [event(chat='group:20002'), event()]:
            if candidate.source.chat_id == 'group:20001':
                candidate.source.user_id = 'someone-else'
            with self.assertRaises(ValueError):
                self.frame([candidate])

    def test_background_is_exact_scope_and_uses_distinct_speaker_aliases(self):
        other = ContextRecord(ContextScope('/tmp/synthetic-social-frame', 'napcat', 'group:20002'),
                              time.time(), 'member', '成员乙', 'OTHER_GROUP_CANARY', 'other', sender_id='10002')
        self.first.user_name = self.second.user_name = '同名'
        frame = self.frame(rows=(other,))
        text = json.dumps(frame.messages, ensure_ascii=False)
        self.assertNotIn('OTHER_GROUP_CANARY', text)
        self.assertNotIn('10001', text)
        self.assertNotIn('10002', text)
        self.assertEqual([row['speaker'] for row in frame.payload['candidates']], ['p0', 'p1'])

    def test_real_exchange_is_retained_beyond_display_window(self):
        now = time.time()
        rows = [ContextRecord(self.scope, now-80, 'member', '成员甲', '甲还是乙？', 'request', sender_id='10001'),
                ContextRecord(self.scope, now-79, 'self', '自己', '请选一个方案', 'answer', reply_to_message_id='request')]
        rows += [ContextRecord(self.scope, now-70+i, 'member', '成员乙', '另一个话题', str(i), sender_id='10002')
                 for i in range(14)]
        self.first.text = '乙'
        frame = self.frame([self.first], rows)
        exchange = frame.payload['recent_exchanges'][0]
        self.assertEqual(frame.ids[exchange['request']], 'request')
        self.assertEqual(frame.ids[exchange['answer']], 'answer')
        self.assertEqual(exchange['candidate'], 'c0')

    def test_verified_reply_to_another_member_is_marked_as_background(self):
        row = ContextRecord(self.scope, time.time(), 'member', '成员乙', '给另一个成员的回复', 'quoted',
                            sender_id='10002', verified_reply_author_id='10003')
        self.first.bot_id = '90001'
        frame = self.frame([self.first], [row])
        self.assertTrue(frame.payload['messages'][0]['addressed_elsewhere'])

    def test_future_background_cannot_be_used_as_an_earlier_topic(self):
        now = time.time()
        rows = [ContextRecord(self.scope, now-5, 'member', '成员甲', '原消息', 'share', sender_id='10001'),
                ContextRecord(self.scope, now-1, 'member', '成员乙', '后来换题', 'later', sender_id='10002')]
        frame = self.frame([self.first], rows)
        ref = next(ref for ref, mid in frame.ids.items() if mid == 'later')
        with self.assertRaises(ValueError):
            frame.parse(json.dumps(verdict(assessment(topic=ref))))

    def test_large_input_is_rejected_instead_of_hiding_candidate_text(self):
        events = [event('原文'*2000, mid=str(i)) for i in range(4)]
        with self.assertRaises(ValueError):
            self.frame(events)


class ParticipationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ControllerTests.asyncSetUp
    asyncTearDown = base.ControllerTests.asyncTearDown
    idle = base.ControllerTests.idle
    drain = base.ControllerTests.drain
    job = base.ControllerTests.job

    def observe(self, text, mid, user='10001', chat='group:20001'):
        item = event(text, mid=mid, user=user, chat=chat)
        item.reply_expected = False
        item.bot_id = '90001'
        self.social.observe(item)
        return item

    async def test_batch_selects_earlier_share_and_keeps_real_source(self):
        first = self.observe('我画好了合成模型的示意图', 'share')
        second = self.observe('哈哈', 'echo', user='10002')
        self.ctx.selection_response = verdict(assessment(),
            assessment('c1', action='observe', reason='no_contribution'))
        for item in (first, second):
            self.social.queue_ambient(item)
        await self.drain()
        self.assertEqual(self.port.dispatched, [first])
        self.assertEqual(self.port.sequence, ['issue', 'dispatch'])
        view = self.social.prepare_public_context(first)
        self.assertIn('model_hypothesis_not_authority', view.text)
        self.assertIn('sharing', view.text)
        self.assertEqual(self.social.counters['selections'], 1)

    async def test_text_directed_to_other_member_stays_silent(self):
        item = self.observe('成员乙，请把合成材料交给成员丙。这条只请成员乙处理。', 'other')
        self.ctx.selection_response = verdict(assessment(audience='member', action='observe',
                                                        reason='addressed_elsewhere'), selected=None)
        self.social.queue_ambient(item)
        await self.drain()
        self.assertFalse(self.port.dispatched)
        self.assertFalse(self.port.sequence)

    async def test_transport_other_member_target_never_reaches_selector(self):
        item = self.observe('请你处理', 'at-other')
        item.at_targets = ('10002',)
        self.social.queue_ambient(item)
        await self.drain()
        self.assertFalse(self.ctx.calls)
        self.assertFalse(self.port.dispatched)

    async def test_invalid_selection_has_no_latest_message_fallback(self):
        item = self.observe('公开分享', 'share')
        self.ctx.selection_response = {'selected': 'forged', 'assessments': []}
        self.social.queue_ambient(item)
        await self.drain()
        self.assertFalse(self.port.dispatched)
        self.assertFalse(self.port.sequence)
        self.assertEqual(self.social.counters['failed'], 1)
        self.assertEqual(len(self.ctx.calls), 2)

    async def test_protocol_repair_is_bounded_and_keeps_same_source(self):
        item = self.observe('等待补充的同一真实消息', 'repair')
        original = self.ctx.llm._async_caller
        async def provider(**kwargs):
            self.ctx.selection_response = (verdict({**assessment(action='wait', reason='unclear'),
                'topic_action':'close'}, selected=None) if not self.ctx.calls else verdict())
            return await original(**kwargs)
        self.ctx.llm._async_caller = provider
        self.social.queue_ambient(item)
        await self.drain()
        self.assertEqual(len(self.ctx.calls), 2)
        self.assertEqual(self.port.dispatched, [item])
        self.assertEqual(self.social.counters['failed'], 0)
        first = self.ctx.calls[0][0]['messages'][-1]
        self.assertEqual(first, self.ctx.calls[1][0]['messages'][-1])

    async def test_selector_gets_public_scope_in_fresh_context(self):
        private = ContextVar('private_selection_canary', default='empty')
        token = private.set('PRIVATE_CONTEXT_CANARY')
        values = []
        original = self.ctx.llm._async_caller
        async def provider(**kwargs):
            values.append(private.get())
            return await original(**kwargs)
        self.ctx.llm._async_caller = provider
        self.observe('PRIVATE_DM_CANARY', 'dm', chat='dm:10001')
        self.observe('OTHER_GROUP_CANARY', 'other', chat='group:20002')
        item = self.observe('本群分享', 'share')
        try:
            self.social.queue_ambient(item)
            await self.drain()
        finally:
            private.reset(token)
        request = json.dumps(self.ctx.calls[0][0], ensure_ascii=False)
        self.assertEqual(values, ['empty'])
        self.assertNotIn('PRIVATE_DM_CANARY', request)
        self.assertNotIn('OTHER_GROUP_CANARY', request)
        self.assertNotIn('PRIVATE_CONTEXT_CANARY', request)
        self.assertEqual(self.ctx.calls[0][1], 'group:20001')

    async def test_short_answer_keeps_its_exchange_despite_interleaving(self):
        self.observe('合成方案甲和乙，选哪个？', 'question')
        self.social.record_self('group:20001', '你倾向甲还是乙？', 'answer', reply_to_message_id='question')
        for index in range(14):
            self.observe('另一段合成讨论', 'other'+str(index), user='10002')
        item = self.observe('乙', 'short')
        self.ctx.selection_response = verdict(assessment(topic='h0', reason='followup'))
        self.social.queue_ambient(item)
        await self.drain()
        view = self.social.prepare_public_context(item)
        payload = json.loads(view.text[view.text.index('{'):])
        self.assertEqual(payload['recent_exchange']['request_id'], 'question')
        self.assertEqual(payload['participation']['topic_anchor'], 'question')
        self.assertIn('你倾向甲还是乙', view.text)

    async def test_selection_dependencies_survive_eviction_and_forgetting_revokes_grant(self):
        root = self.observe('合成讨论的原始资料', 'root')
        item = self.observe('接续讨论', 'current')
        self.ctx.selection_response = verdict(assessment(topic='h0', reason='followup'))
        self.social.queue_ambient(item)
        await self.drain()
        view = self.social.prepare_public_context(item)
        for index in range(70):
            self.observe('后续资料', 'next'+str(index))
        self.assertTrue(view.current())
        self.assertTrue(self.social.forget_messages('group:20001', {root.message_id}))
        self.assertFalse(view.current())
        self.assertFalse(self.port.optional_valid(item.source))
        self.assertFalse(self.social.forget_messages('group:20001', {root.message_id}))

    async def stale_selection(self, change):
        item = self.observe('公开分享', 'share')
        self.ctx.delayed = True
        self.social.queue_ambient(item)
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        self.assertFalse(self.port.sequence)
        change(item)
        self.ctx.release.set()
        await self.drain()
        self.assertFalse(self.port.dispatched)

    async def test_pause_cancels_inflight_selection(self):
        await self.stale_selection(lambda _: self.social.control(event('/social pause')))

    async def test_recall_cancels_inflight_selection(self):
        await self.stale_selection(lambda item: self.social.recall('group:20001', item.message_id))

    async def test_forgetting_cancels_inflight_selection(self):
        await self.stale_selection(lambda item: self.social.forget_messages('group:20001', {item.message_id}))

    async def test_epoch_change_cancels_inflight_selection(self):
        await self.stale_selection(lambda _: setattr(self.port, 'connection', self.port.connection+1))

    async def test_new_explicit_request_cancels_inflight_selection(self):
        await self.stale_selection(lambda _: self.social.observe(event('新的点名请求', mid='named')))

    async def test_replaced_source_cannot_receive_selected_grant(self):
        await self.stale_selection(lambda item: setattr(item, 'source', copy.copy(item.source)))

    async def test_mutated_source_chat_cannot_move_selection_to_another_group(self):
        await self.stale_selection(lambda item: setattr(item.source, 'chat_id', 'group:20002'))

    async def test_ordinary_interleaving_does_not_starve_selection(self):
        item = self.observe('公开分享', 'share')
        self.ctx.delayed = True
        self.social.queue_ambient(item)
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        self.observe('普通补充', 'supplement', user='10002')
        self.ctx.release.set()
        await self.drain()
        self.assertEqual(self.port.dispatched, [item])

    async def test_unrelated_repeat_forget_does_not_cancel_new_selection(self):
        item = self.observe('新的公开分享', 'share')
        self.ctx.delayed = True
        self.social.queue_ambient(item)
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        self.assertFalse(self.social.forget_messages('group:20001', {'unrelated-old-message'}))
        self.ctx.release.set()
        await self.drain()
        self.assertEqual(self.port.dispatched, [item])

    async def test_stale_time_budget_drops_selection(self):
        expired = [False]
        original = self.ctx.llm._async_caller
        async def provider(**kwargs):
            result = await original(**kwargs)
            expired[0] = True
            return result
        self.ctx.llm._async_caller = provider
        clock = SimpleNamespace(time=time.time, monotonic=lambda: 121 if expired[0] else 100)
        with patch('hermes_social.controller.time', clock):
            item = self.observe('公开分享', 'share')
            self.social.queue_ambient(item)
            await self.drain()
        self.assertFalse(self.port.dispatched)

    async def test_more_than_eight_new_activities_cancel_selection(self):
        def change(_):
            for index in range(9):
                self.observe('普通公开补充', 'new'+str(index), user='10002')
        await self.stale_selection(change)

    async def test_model_timeout_stays_silent_without_fallback(self):
        async def timeout(**kwargs):
            raise TimeoutError('SYNTHETIC_ERROR_CONTENT_NOT_FOR_LOGS')
        self.ctx.llm._async_caller = timeout
        self.social.queue_ambient(self.observe('公开分享', 'share'))
        with self.assertLogs('hermes_social.controller', level='WARNING') as logs:
            await self.drain()
        self.assertNotIn('SYNTHETIC_ERROR_CONTENT_NOT_FOR_LOGS', '\n'.join(logs.output))
        self.assertFalse(self.port.dispatched)

    async def test_unload_cancels_delayed_selector_without_dispatch(self):
        self.ctx.delayed = True
        self.social.queue_ambient(self.observe('公开分享', 'share'))
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        tasks = tuple(self.social.ambient_tasks.values())
        self.ctx.unload()
        self.ctx.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertFalse(self.port.dispatched)
        self.assertFalse(self.port.optional)

    async def test_disconnect_cancels_delayed_selector_without_dispatch(self):
        self.ctx.delayed = True
        self.social.queue_ambient(self.observe('公开分享', 'share'))
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        tasks = tuple(self.social.ambient_tasks.values())
        self.port.online = False
        self.social.reset_connection()
        self.ctx.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertFalse(self.port.dispatched)

    async def test_large_exchange_keeps_root_and_reply_within_utf8_budget(self):
        self.observe('问题资料'*240, 'question')
        self.social.record_self('group:20001', '回复资料'*240, 'answer', reply_to_message_id='question')
        for index in range(14):
            self.observe('背景资料'*240, 'background'+str(index), user='10002')
        item = self.observe('选项乙', 'short')
        self.ctx.selection_response = verdict(assessment(topic='h0', reason='followup'))
        self.social.queue_ambient(item)
        await self.drain()
        view = self.social.prepare_public_context(item)
        payload = json.loads(view.text[view.text.index('{'):])
        self.assertLessEqual(len(view.text.encode()), 5000)
        self.assertEqual(payload['recent_exchange']['request_id'], 'question')
        self.assertTrue({'question', 'answer'} <= {row['message_id'] for row in payload['messages']})
        self.assertTrue(any(row.get('truncated') for row in payload['messages']))

    async def test_expired_choice_is_not_reused_as_current_topic(self):
        item = self.observe('公开分享', 'share')
        self.social.queue_ambient(item)
        await self.drain()
        view = self.social.prepare_public_context(item)
        expired = time.time()+181
        clock = SimpleNamespace(time=lambda: expired, monotonic=lambda: time.monotonic()+181)
        with patch('hermes_social.controller.time', clock):
            self.assertFalse(view.current())
            current = self.social.prepare_public_context(item)
        self.assertNotIn('model_hypothesis_not_authority', current.text)

    async def test_new_activity_rejects_old_media_but_processes_new_text(self):
        media = self.observe('看看这张图', 'media')
        media._napcat_optional_media = True
        self.ctx.delayed = True
        self.social.queue_ambient(media)
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        newer = self.observe('另一个公开话题', 'text', user='10002')
        self.social.queue_ambient(newer)
        self.ctx.release.set()
        await self.drain()
        self.assertEqual(self.port.dispatched, [newer])

    async def test_channel_denial_does_not_strand_next_batch(self):
        first = self.observe('第一个公开话题', 'first')
        self.ctx.delayed = True
        self.social.queue_ambient(first)
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        newer = self.observe('后来的公开话题', 'next', user='10002')
        self.social.queue_ambient(newer)
        original = self.port.issue_optional
        def issue(item):
            if item is first:
                raise ValueError('synthetic channel rejection')
            return original(item)
        with patch.object(self.port, 'issue_optional', side_effect=issue):
            self.ctx.release.set()
            await self.drain()
        self.assertEqual(self.port.dispatched, [newer])
        self.assertEqual(self.social.counters['failed'], 1)


if __name__ == '__main__':
    unittest.main()
