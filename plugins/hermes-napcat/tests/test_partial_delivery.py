"""Single-turn delivery regressions: real host helpers, synthetic actions only.

No model, websocket, runtime configuration or deployment is used here.
"""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import AsyncMock, patch

import test_proactive_lifecycle as lifecycle
from gateway.config import StreamingConfig
from gateway.run import _interim_metadata, _non_conversational_metadata
from gateway.run_turn_runner import TurnRunner
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from social_plugin_helpers import optional_valid
from hermes_napcat.media_io import MediaFetcher


class PartialDelivery(unittest.IsolatedAsyncioTestCase):
    issue_ingress = lifecycle.Lifecycle.issue_ingress
    start_group = lifecycle.Lifecycle.start_group
    drain_ambient = lifecycle.Lifecycle.drain_ambient

    async def asyncSetUp(self):
        await lifecycle.Lifecycle.asyncSetUp(self)
        self.sink, self.echoes = [], {}
        original = self.adapter.client.call
        async def call(action, payload):
            if action == 'send_group_msg':
                self.sink.append(copy.deepcopy(payload))
                mid = 70000+len(self.sink)
                self.echoes[mid] = dict(message_id=mid, user_id=90001, group_id=payload['group_id'],
                    message_type='group', message=copy.deepcopy(payload['message']))
                return {'message_id': mid}
            if action == 'get_msg' and payload['message_id'] in self.echoes:
                return copy.deepcopy(self.echoes[payload['message_id']])
            return await original(action, payload)
        self.adapter.client.call = call
        self.inbound = None

    async def asyncTearDown(self):
        if self.inbound is not None:
            await self.adapter.on_processing_complete(self.inbound, 'diagnostic_complete')
        await lifecycle.Lifecycle.asyncTearDown(self)

    async def begin(self, ambient):
        if ambient:
            await self.adapter._receive(lifecycle.raw_text('合成工坊正在比较两种纸卡排序流程。', 501, user=77777))
            await self.adapter._receive(lifecycle.raw_text('大家认为合成流程甲和乙有什么区别？', 502, user=88888))
            await self.drain_ambient()
            self.assertEqual(len(self.dispatched), 1)
            inbound = self.dispatched[0]
            self.assertFalse(inbound.reply_expected)
            self.assertTrue(optional_valid(self.adapter, inbound.source))
        else:
            raw = lifecycle.raw_text('公开名字，请比较两种合成纸卡流程。', 503, user=77777)
            inbound = self.adapter.convert_event(raw)
            self.assertTrue(inbound.reply_expected)
            self.social.observe(inbound)
        await self.start_group(inbound)
        self.inbound = inbound
        return inbound

    def consumer(self, inbound, metadata=None):
        return GatewayStreamConsumer(self.adapter, inbound.source.chat_id,
            config=StreamConsumerConfig(cursor='', edit_interval=0, buffer_threshold=1),
            metadata=metadata, initial_reply_to_id=inbound.message_id)

    async def final(self, inbound, text='合成最终正文：流程甲按颜色分组，流程乙按编号排序。'):
        return await self.adapter._send_with_retry(inbound.source.chat_id, text,
                                                   reply_to=inbound.message_id)

    async def test_addressed_host_commentary_then_final_delivers_both(self):
        inbound = await self.begin(False)
        consumer = self.consumer(inbound)
        self.assertTrue(await consumer._send_commentary('合成中途说明：我先比较两种流程。'))
        self.assertTrue((await self.final(inbound)).success)
        self.assertEqual(len(self.sink), 2)
        self.assertTrue(optional_valid(self.adapter, inbound.source))

    async def test_addressed_turn_prepares_collective_context_without_optional_reply_gate(self):
        self.social.policy.dialogue['enabled'] = True
        async def prepare(event):
            self.assertTrue(self.social.port.public_ingress_current(event))
            self.assertFalse(self.social.port.public_ingress_current(copy.copy(event)))
            clone = copy.copy(event)
            clone.source = copy.copy(event.source)
            self.assertFalse(self.social.port.public_ingress_current(clone))
            self.assertTrue(self.social.port.public_ingress_current(event))
            return False
        self.social.prepare_direct_dialogue = AsyncMock(side_effect=prepare)
        inbound = await self.begin(False)
        self.social.prepare_direct_dialogue.assert_awaited_once_with(inbound)
        self.assertTrue(self.social.port.public_ingress_current(inbound))
        self.assertTrue((await self.final(inbound)).success)
        await self.adapter.on_processing_complete(inbound, 'completed')
        self.inbound = None
        self.assertFalse(self.social.port.public_ingress_current(inbound))

    async def test_ambient_host_commentary_must_not_revoke_its_own_final(self):
        inbound = await self.begin(True)
        consumer = self.consumer(inbound)
        self.assertTrue(await consumer._send_commentary('合成中途说明：我先比较两种流程。'))
        result = await self.final(inbound)
        self.assertTrue(result.success, result.raw_response)
        self.assertEqual(len(self.sink), 2)

    async def test_addressed_two_original_anchor_fragments_both_deliver(self):
        inbound = await self.begin(False)
        first = await self.final(inbound, '合成正文第一段：流程甲按颜色分组。')
        second = await self.final(inbound, '合成正文第二段：流程乙按编号排序。')
        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(len(self.sink), 2)

    async def stream_preview_then_final(self, ambient):
        inbound = await self.begin(ambient)
        consumer = self.consumer(inbound)
        head, tail = '合成预览：流程甲按颜色', '分组，流程乙按编号排序。'
        consumer.on_delta(head)
        task = asyncio.create_task(consumer.run())
        async with asyncio.timeout(2):
            while not self.sink:
                await asyncio.sleep(0)
        consumer.on_delta(tail)
        consumer.finish(final_text=head+tail)
        await asyncio.wait_for(task, 2)
        captured = ''.join(segment['data']['text'] for payload in self.sink
                           for segment in payload['message'] if segment['type'] == 'text')
        return consumer, captured, head+tail

    async def test_real_addressed_stream_preview_edit_fallback_keeps_complete_final(self):
        consumer, captured, expected = await self.stream_preview_then_final(False)
        self.assertEqual(captured, expected)
        self.assertTrue(consumer.final_content_delivered)

    async def test_forced_ambient_stream_must_not_lose_tail_after_preview(self):
        # Diagnostic only: quiet_optional_reply prevents constructing this
        # stream for an actual normal ambient turn in the current host.
        consumer, captured, expected = await self.stream_preview_then_final(True)
        self.assertEqual(captured, expected)
        self.assertTrue(consumer.final_content_delivered)

    async def test_ambient_two_fragments_must_keep_the_same_exact_source_until_complete(self):
        inbound = await self.begin(True)
        self.assertTrue((await self.final(inbound, '合成正文第一段：流程甲按颜色分组。')).success)
        second = await self.final(inbound, '合成正文第二段：流程乙按编号排序。')
        self.assertTrue(second.success, second.raw_response)
        self.assertEqual(len(self.sink), 2)

    async def test_nonconversational_status_does_not_consume_ambient_final(self):
        inbound = await self.begin(True)
        metadata = _interim_metadata(_non_conversational_metadata(platform=inbound.source.platform))
        consumer = self.consumer(inbound, metadata)
        self.assertTrue(await consumer._send_commentary('合成生命周期状态，仅用于诊断。'))
        self.assertTrue(optional_valid(self.adapter, inbound.source))
        self.assertTrue((await self.final(inbound)).success)
        self.assertEqual(len(self.sink), 2)

    async def one_call_chunks(self, ambient):
        inbound = await self.begin(ambient)
        text = '合成完整正文。'*1200
        result = await self.final(inbound, text)
        self.assertTrue(result.success, result.raw_response)
        fragments = [segment['data']['text'] for payload in self.sink
                     for segment in payload['message'] if segment['type'] == 'text']
        self.assertGreater(len(fragments), 1)
        self.assertEqual(''.join(fragments), text)
        anchors = [segment['data']['id'] for payload in self.sink
                   for segment in payload['message'] if segment['type'] == 'reply']
        self.assertTrue(all(anchor == inbound.message_id for anchor in anchors))

    async def test_single_addressed_send_splits_all_transport_chunks_before_recording(self):
        await self.one_call_chunks(False)

    async def test_single_ambient_final_splits_all_transport_chunks_before_revocation(self):
        await self.one_call_chunks(True)

    async def test_real_host_quiet_optional_setup_has_no_stream_or_interim(self):
        inbound = await self.begin(True)
        ctx = SimpleNamespace(mute_notification_reply=False, streaming_tts_consumer_holder=[None],
            resolve_display_setting=lambda *args: True, user_config={}, scheduled_heartbeat=False,
            reply_expected=False, interim_assistant_messages_enabled=True, source=inbound.source)
        runner = object.__new__(TurnRunner)
        runner._ctx = ctx
        runner._runner = SimpleNamespace(config=SimpleNamespace(streaming=StreamingConfig(enabled=True)))
        consumer, deltas, interim, want_interim = runner._setup_stream_consumer('napcat')
        self.assertIsNone(consumer)
        self.assertIsNone(deltas)
        self.assertFalse(want_interim)
        # The factory returns a callable; _wire_turn_agent_callbacks leaves it
        # unwired when want_interim is False. Merely returning it is no send.
        self.assertTrue(callable(interim))
        self.assertFalse(self.sink)
        self.assertTrue((await self.final(inbound)).success)

    async def test_new_explicit_ingress_still_revokes_an_old_ambient_source(self):
        inbound = await self.begin(True)
        await self.adapter._receive(lifecycle.raw_text('公开名字，请处理新的合成任务。', 504, user=77777))
        self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertFalse((await self.final(inbound)).success)
        self.assertFalse(self.sink)

    async def test_interim_is_unshaped_and_not_recorded_as_a_complete_self_reply(self):
        inbound = await self.begin(True)
        self.social.reply_char_limit = lambda chat: 32
        text = '合成中途状态，仅为进度说明。'*10
        with patch.object(self.social, 'record_self', wraps=self.social.record_self) as record, \
                patch.object(self.adapter, '_record_memory_delivery', wraps=self.adapter._record_memory_delivery) as memory:
            self.assertTrue(await self.consumer(inbound)._send_commentary(text))
            record.assert_not_called()
            memory.assert_not_called()
            sent = ''.join(segment['data']['text'] for segment in self.sink[0]['message'] if segment['type'] == 'text')
            self.assertEqual(sent, text)
            self.assertTrue((await self.final(inbound)).success)
            record.assert_called_once()
            self.assertIs(record.call_args.kwargs['source'], inbound.source)

    async def test_same_turn_keeps_original_source_deadline_epoch_and_frozen_view(self):
        inbound = await self.begin(True)
        before = self.adapter._optional_sources.grants[id(inbound.source)]
        permit = self.adapter._group_permit.get()
        view = permit.public_context_view
        frozen = view.text
        for text in ('合成第一段正文。', '合成第二段正文。'):
            self.assertTrue((await self.final(inbound, text)).success)
            after = self.adapter._optional_sources.grants[id(inbound.source)]
            self.assertIs(after[0], before[0])
            self.assertIs(after[1], before[1])
            self.assertEqual(after[2:4], before[2:4])
            self.assertEqual(after[5:], before[5:])  # connection and deadline
            self.assertIs(permit.public_context_view, view)
            self.assertEqual(view.text, frozen)
            self.assertTrue(view.current())
        await self.adapter.on_processing_complete(inbound, 'completed')
        self.assertNotIn(id(inbound.source), self.adapter._optional_sources.grants)
        self.assertFalse((await self.final(inbound, '合成迟到第三段。')).success)

    async def other_opportunities(self):
        other = self.adapter.convert_event(lifecycle.raw_text('合成另一条普通分享。', 505, user=77777))
        self.assertFalse(other.reply_expected)
        self.social.observe(other)
        self.social.port.issue_optional(other)
        self.assertTrue(optional_valid(self.adapter, other.source))
        grant = self.social.port.claim_idle('group:20001', 'd'*32)
        self.assertTrue(self.social.port._grant_valid(grant))
        return other, grant

    async def test_first_fragment_revokes_other_sources_and_idle_but_keeps_own_source(self):
        inbound = await self.begin(True)
        other, idle = await self.other_opportunities()
        self.assertTrue((await self.final(inbound, '合成第一段正文。')).success)
        self.assertTrue(optional_valid(self.adapter, inbound.source))
        self.assertFalse(optional_valid(self.adapter, other.source))
        self.assertFalse(self.social.port._grant_valid(idle))
        self.assertTrue(idle.spent)
        self.assertTrue((await self.final(inbound, '合成第二段正文。')).success)

    async def test_clone_cannot_be_used_to_preserve_an_admitted_source(self):
        inbound = await self.begin(True)
        self.social.port.invalidate_optional(inbound.source.chat_id, preserve_source=copy.copy(inbound.source))
        self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertFalse((await self.final(inbound)).success)

    async def test_another_context_cannot_preserve_even_the_real_source(self):
        inbound = await self.begin(True)
        token = self.adapter._group_permit.set(None)
        try:
            self.social.port.invalidate_optional(inbound.source.chat_id, preserve_source=inbound.source)
        finally:
            self.adapter._group_permit.reset(token)
        self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertFalse((await self.final(inbound)).success)

    async def test_changed_route_cannot_be_used_to_preserve_source(self):
        inbound = await self.begin(True)
        chat = inbound.source.chat_id
        inbound.source.chat_id = 'group:20002'
        try:
            self.social.port.invalidate_optional(chat, preserve_source=inbound.source)
        finally:
            inbound.source.chat_id = chat
        self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertFalse((await self.final(inbound)).success)

    async def test_expired_grant_is_never_recreated_or_extended(self):
        inbound = await self.begin(True)
        self.assertTrue((await self.final(inbound, '合成第一段正文。')).success)
        grant = self.adapter._optional_sources.grants[id(inbound.source)]
        self.adapter._optional_sources.grants[id(inbound.source)] = (*grant[:-1], time.monotonic()-1)
        self.social.port.invalidate_optional(inbound.source.chat_id, preserve_source=inbound.source)
        self.assertNotIn(id(inbound.source), self.adapter._optional_sources.grants)
        self.assertFalse((await self.final(inbound, '合成已过期第二段。')).success)
        self.assertEqual(len(self.sink), 1)

    async def revoked_after_first(self, reason):
        inbound = await self.begin(True)
        self.assertTrue((await self.final(inbound, '合成第一段正文。')).success)
        if reason == 'forget':
            self.assertTrue(self.social.forget_messages(inbound.source.chat_id, {'501'}))
        elif reason == 'pause':
            await self.adapter._receive(lifecycle.raw_text('/napcat-social pause', 506, user=77777))
        elif reason == 'recall':
            await self.adapter._receive(dict(post_type='notice', notice_type='group_recall',
                self_id=90001, group_id=20001, user_id=88888, operator_id=10001, message_id=502))
        elif reason == 'disconnect':
            self.adapter._connection_lost()
        else:
            raise AssertionError(reason)
        self.social.port.invalidate_optional(inbound.source.chat_id, preserve_source=inbound.source)
        self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertFalse((await self.final(inbound, '合成被撤销的第二段。')).success)

    async def test_forgetting_context_after_first_fragment_still_revokes_second(self):
        await self.revoked_after_first('forget')

    async def test_pause_after_first_fragment_still_revokes_second(self):
        await self.revoked_after_first('pause')

    async def test_recall_after_first_fragment_still_revokes_second(self):
        await self.revoked_after_first('recall')

    async def test_disconnect_after_first_fragment_still_revokes_second(self):
        await self.revoked_after_first('disconnect')

    async def test_new_ingress_during_rpc_ack_cannot_be_undone_by_late_self_record(self):
        inbound = await self.begin(True)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.adapter.client.call
        async def call(action, payload):
            if action == 'send_group_msg':
                entered.set()
                await release.wait()
            return await original(action, payload)
        self.adapter.client.call = call
        task = asyncio.create_task(self.final(inbound, '合成已开始发送的第一段。'))
        await asyncio.wait_for(entered.wait(), 2)
        await self.adapter._receive(lifecycle.raw_text('公开名字，请处理新的合成任务。', 507, user=77777))
        release.set()
        self.assertTrue((await task).success)  # an already-started RPC may ACK
        self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertFalse((await self.final(inbound, '合成禁止复活的第二段。')).success)

    async def test_native_generated_image_between_fragments_keeps_exact_turn_alive(self):
        inbound = await self.begin(True)
        image = 'https://example.invalid/synthetic-generated.png'
        authority = self.adapter.tool_authority_for_source(inbound.source)
        authority.record_result('image_generate', json.dumps({'success': True, 'image': image}))
        self.assertTrue((await self.final(inbound, '合成图片前的正文。')).success)
        with patch.object(MediaFetcher, 'fetch', new_callable=AsyncMock,
                          return_value=(b'\x89PNG\r\n\x1a\nsynthetic', 'image/png')):
            result = await self.adapter.send_image(inbound.source.chat_id, image, reply_to=inbound.message_id)
        self.assertTrue(result.success)
        self.assertTrue(optional_valid(self.adapter, inbound.source))
        self.assertTrue((await self.final(inbound, '合成图片后的正文。')).success)


if __name__ == '__main__':
    unittest.main()
