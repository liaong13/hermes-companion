"""自然接话媒体回归：真实接收/社交/来源/媒体路由，离线捕获端。

OneBot、worker probe、解析字节与视觉响应均为显式 fixture，不访问 QQ 或云。
真实 worker / parser / Qwen / FIFO / AIAgent 的验收由独立 live 脚本承担。
"""
import asyncio
import base64
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_proactive as base
import test_proactive_lifecycle as life
from hermes_napcat.transport import OneBotClient
from hermes_napcat.media import MediaRef


class AmbientMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for guard in (
            patch.object(OneBotClient, 'start', AsyncMock(side_effect=AssertionError('no QQ connection'))),
            patch('aiohttp.ClientSession._request', AsyncMock(side_effect=AssertionError('no network'))),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        cfg = base.social_config()
        cfg.extra['media'] = dict(
            enabled=True, groups=['20001'], types=['image', 'file'],
            cloud=['image'], budget_mode='provider', references=True,
            observe_metadata=True, allow_derived_text=True,
        )
        with patch.object(base, 'social_config', return_value=cfg):
            await base.SocialTests.asyncSetUp(self)
        self.bridge.probe = AsyncMock()  # not a real worker/namespace test
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0
        self.native, self.pending, self.fetches, self.questions, self.outbound = [], [], [], [], []
        self.parser_calls, self.reference_messages = [], {}
        self.delay_stage = None
        self.started, self.unblock = asyncio.Event(), asyncio.Event()
        self.cancelled = []
        self.next_mid = 600

        async def stage(name):
            if self.delay_stage == name:
                self.started.set()
                try:
                    await self.unblock.wait()
                except asyncio.CancelledError:
                    self.cancelled.append(name)
                    raise

        async def fetch(url):
            self.fetches.append(url)
            await stage('fetch')
            return b'OFFLINE_FAKE_IMAGE_BYTES', 'image/png'

        async def parse(kind, data, suffix=''):
            self.parser_calls.append(kind)
            await stage('parse')
            return {'source_mime': 'image/png',
                    'data': base64.b64encode(b'OFFLINE_FAKE_NORMALIZED_IMAGE').decode(),
                    'warnings': []}

        async def recognize(kind, data, *, question, **kwargs):
            self.questions.append(question)
            await stage('vision')
            return {'text': 'OFFLINE_IMAGE_DERIVATIVE', 'model': 'offline-fixture-only', 'warnings': []}

        async def capture(event):
            event._gateway_accepted = True
            self.native.append(event)

        async def call(action, payload):
            if action == 'get_friend_list':
                return []
            if action == 'send_group_msg':
                self.outbound.append(copy.deepcopy(payload))
                mid = 90000 + len(self.outbound)
                self.reference_messages[mid] = dict(
                    message_id=mid, self_id=90001, user_id=90001,
                    sender={'user_id': 90001}, message_type='group', group_id=20001,
                    message=copy.deepcopy(payload['message']))
                return {'message_id': mid}
            if action == 'get_msg':
                return copy.deepcopy(self.reference_messages[int(payload['message_id'])])
            raise AssertionError('unexpected action: ' + action)

        self.adapter.client.call = call
        self.adapter.media.fetcher.fetch = fetch
        self.adapter.media.sandbox.process = parse
        self.adapter.media.provider = SimpleNamespace(recognize=recognize)
        self.adapter.handle_message = capture
        queue = self.social.queue_ambient
        def remember(event):
            self.pending.append(event)
            queue(event)
        self.social.queue_ambient = remember

    async def asyncTearDown(self):
        self.unblock.set()
        await base.SocialTests.asyncTearDown(self)

    def raw(self, text='', *, image=False, mid=None, user=77777, group=20001, segments=None):
        if mid is None:
            self.next_mid += 1
            mid = self.next_mid
        raw = life.raw_text(text, mid, user=user, group=group)
        if segments is not None:
            raw['message'] = copy.deepcopy(segments)
        elif image:
            raw['message'].append({'type': 'image', 'data': {
                'file': 'offline-image-' + str(mid) + '.png',
                'url': 'https://example.invalid/offline-image-' + str(mid) + '.png'}})
        return raw

    async def drain(self):
        for _ in range(10):
            tasks = [t for t in [*self.social.ambient_tasks.values(),
                     *self.adapter._text_tasks, *self.adapter._media_tasks] if not t.done()]
            if not tasks:
                return
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 4)
        self.fail('local queues did not drain')

    async def feed(self, raw, *, drain=True):
        await self.adapter._receive(raw)
        if drain:
            await self.drain()

    async def index_only(self, raw):
        # Keep the cross-message candidate tests about indexing, not the new
        # independently authorized automatic visual path.
        cloud = self.adapter.media.policy.cloud
        self.adapter.media.policy.cloud = []
        try:
            await self.feed(raw)
        finally:
            self.adapter.media.policy.cloud = cloud

    def recall(self, mid, user=77777):
        return dict(post_type='notice', notice_type='group_recall', self_id=90001,
                    group_id=20001, user_id=user, operator_id=10001, message_id=mid)

    def assert_derivative(self, *, optional=True):
        self.assertEqual(len(self.native), 1)
        self.assertEqual(self.native[0].reply_expected, not optional)
        self.assertIn('OFFLINE_IMAGE_DERIVATIVE', self.native[0].text)
        self.assertIn('本群已核验媒体资料', self.native[0].text)
        self.assertNotIn('https://example.invalid/', self.native[0].text)
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(len(self.questions), 1)
        self.assertNotIn('本群此前的近期对话', self.questions[0])
        self.assertNotIn('本条是已允许自主参与', self.questions[0])
        self.assertFalse(self.calls)  # no fallback to the plugin's LLM facade

    async def test_pure_image_uses_actual_media_pipeline_and_only_updates_recent_context(self):
        media = self.adapter.media
        with patch.object(media, 'resolve', wraps=media.resolve) as resolve, \
                patch.object(media, 'enrich', wraps=media.enrich) as enrich:
            await self.feed(self.raw(image=True))
        resolve.assert_awaited_once()
        enrich.assert_awaited_once()
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.parser_calls, ['image'])
        self.assertEqual(len(self.questions), 1)
        self.assertFalse(self.native)
        self.assertFalse(self.pending)
        self.assertEqual(len(self.adapter.media.index.rows), 1)
        self.assertEqual(len(self.adapter.media.reference_sources.rows), 1)
        self.assertFalse(self.outbound)
        rows = self.social.context('group:20001')
        self.assertEqual(len(rows), 1)
        self.assertIn('OFFLINE_IMAGE_DERIVATIVE', rows[0]['text'])
        self.assertNotIn('图片内容未识别', rows[0]['text'])
        self.assertIn('不可信资料', rows[0]['text'])

    async def test_pure_image_cloud_denied_preserves_unread_marker(self):
        await self.index_only(self.raw(image=True))
        self.assertFalse(self.native)
        self.assertFalse(self.fetches)
        self.assertFalse(self.questions)
        self.assertFalse(self.outbound)
        self.assertEqual(len(self.adapter.media.index.rows), 1)
        self.assertIn('图片内容未识别', self.social.context('group:20001')[0]['text'])

    async def test_pure_image_derivative_available_to_later_group_turn_only(self):
        await self.feed(self.raw(image=True, mid=812, user=88888))
        self.assertFalse(self.native)
        self.assertFalse(self.outbound)
        await self.feed(self.raw('大家觉得图片视觉识别有意思吗？', mid=813))
        self.assertEqual(len(self.native), 1)
        self.assertIn('OFFLINE_IMAGE_DERIVATIVE', self.social.prepare_public_context(self.native[0]).text)
        rows = self.social.context('group:20001')
        self.assertEqual(rows[0]['message_id'], '812')
        self.assertEqual(self.native[0].source.user_id, '77777')
        self.assertFalse(self.outbound)

    async def test_pure_image_paused_or_derived_egress_denied_never_uploads(self):
        await self.feed(self.raw('/social pause', mid=801))
        self.assertTrue(self.social.states['group:20001'].paused)
        await self.feed(self.raw(image=True, mid=802))
        self.assertFalse(self.fetches)
        self.assertFalse(self.questions)
        self.assertIn('图片内容未识别', self.social.context('group:20001')[-1]['text'])
        self.adapter.media.policy.allow_derived_text = False
        await self.feed(self.raw('/social resume', mid=803))
        await self.feed(self.raw(image=True, mid=804))
        self.assertFalse(self.fetches)
        self.assertFalse(self.questions)
        self.assertIn('图片内容未识别', self.social.context('group:20001')[-1]['text'])

    async def test_pure_image_vision_error_keeps_unread_and_does_not_send(self):
        from hermes_napcat.media_io import MediaError
        async def failed(*args, **kwargs):
            self.questions.append(kwargs['question'])
            raise MediaError('offline_vision_failure')
        self.adapter.media.provider.recognize = failed
        await self.feed(self.raw(image=True, mid=805))
        self.assertEqual(len(self.questions), 1)
        self.assertIn('图片内容未识别', self.social.context('group:20001')[0]['text'])
        self.assertFalse(self.native)
        self.assertFalse(self.outbound)

    async def test_pure_image_recall_during_vision_cancels_and_removes_context(self):
        self.delay_stage = 'vision'
        await self.feed(self.raw(image=True, mid=806), drain=False)
        await asyncio.wait_for(self.started.wait(), 3)
        await self.feed(self.recall(806))
        self.assertIn('vision', self.cancelled)
        self.assertFalse(self.social.context('group:20001'))
        self.assertFalse(self.native)
        self.assertFalse(self.outbound)

    async def test_new_group_text_does_not_cancel_prior_image_perception(self):
        self.delay_stage = 'vision'
        await self.feed(self.raw(image=True, mid=814, user=88888), drain=False)
        await asyncio.wait_for(self.started.wait(), 3)
        await self.feed(self.raw('大家觉得图片视觉识别有意思吗？', mid=815), drain=False)
        self.assertNotIn('vision', self.cancelled)
        await asyncio.sleep(.02)
        self.assertFalse(self.native)  # bounded wait before the optional model turn
        self.unblock.set()
        await self.drain()
        self.assertIn('OFFLINE_IMAGE_DERIVATIVE', self.social.context('group:20001')[0]['text'])
        self.assertEqual(len(self.native), 1)
        self.assertIn('OFFLINE_IMAGE_DERIVATIVE', self.social.prepare_public_context(self.native[0]).text)
        self.assertFalse(self.outbound)

    async def test_pure_image_fetch_does_not_block_recall_or_disconnect(self):
        self.delay_stage = 'fetch'
        await asyncio.wait_for(self.adapter._receive(self.raw(image=True, mid=807)), 1)
        await asyncio.wait_for(self.started.wait(), 3)
        await asyncio.wait_for(self.adapter._receive(self.recall(807)), 1)
        await self.drain()
        self.assertIn('fetch', self.cancelled)
        self.assertFalse(self.questions)
        self.assertFalse(self.social.context('group:20001'))
        self.assertFalse(self.outbound)

    async def test_pure_image_disconnect_prevents_late_derivative(self):
        self.delay_stage = 'vision'
        await self.feed(self.raw(image=True, mid=808), drain=False)
        await asyncio.wait_for(self.started.wait(), 3)
        self.adapter._connection_lost()
        await self.drain()
        self.assertIn('vision', self.cancelled)
        self.assertFalse(self.social.context('group:20001'))  # reset discards recent rows
        self.assertFalse(self.outbound)

    async def test_pure_image_forged_identity_never_uploads(self):
        raw = self.raw(image=True, mid=809)
        raw['sender']['user_id'] = 88888
        await self.feed(raw)
        self.assertFalse(self.fetches)
        self.assertFalse(self.questions)
        self.assertFalse(self.social.context('group:20001'))
        self.assertFalse(self.outbound)

    async def test_pure_image_does_not_rebind_mutated_raw_url(self):
        raw = self.raw(image=True, mid=810)
        await self.feed(raw, drain=False)
        raw['message'][-1]['data']['url'] = 'https://example.invalid/CHANGED.png'
        await self.drain()
        self.assertEqual(self.fetches, ['https://example.invalid/offline-image-810.png'])
        self.assertFalse(self.native)
        self.assertFalse(self.outbound)

    async def test_pure_image_detach_during_fetch_prevents_late_context(self):
        self.delay_stage = 'fetch'
        await self.feed(self.raw(image=True, mid=811), drain=False)
        await asyncio.wait_for(self.started.wait(), 3)
        self.social.port.detach()
        await self.drain()
        self.assertIn('fetch', self.cancelled)
        self.assertFalse(self.questions)
        self.assertFalse(self.outbound)

    async def test_unread_image_then_ambiguous_text_does_not_invent_visual_data(self):
        await self.index_only(self.raw(image=True, mid=751, user=88888))
        await self.feed(self.raw('大家觉得图片视觉识别有意思吗？', mid=752))
        self.assertFalse(self.fetches)
        self.assertFalse(self.questions)
        self.assertEqual(len(self.native), 1)
        self.assertIn('图片内容未识别', self.social.prepare_public_context(self.native[0]).text)
        self.assertNotIn('OFFLINE_IMAGE_DERIVATIVE', self.native[0].text)

    async def test_unaddressed_question_and_image_reach_native_with_derivative(self):
        await self.feed(self.raw('这张图里写了什么？', image=True))
        self.assert_derivative()
        self.assertIs(self.native[0], self.pending[0])
        self.assertEqual(len(self.prepares[-1]['attachments']), 1)
        self.assertEqual(self.native[0].source.user_id, '77777')

    async def test_generic_question_with_direct_image_needs_no_fixed_trigger(self):
        await self.feed(self.raw('这是什么？', image=True, user=88888))
        self.assert_derivative()
        self.assertEqual(self.native[0].source.user_id, '88888')

    async def test_named_image_keeps_original_media_path(self):
        await self.feed(self.raw('公开名字看一下这张图', image=True))
        self.assert_derivative(optional=False)

    async def test_verified_bot_reply_keeps_quote_and_direct_media(self):
        self.reference_messages[42] = dict(message_id=42, self_id=90001, user_id=90001,
            sender={'user_id': 90001}, message_type='group', group_id=20001,
            message=[{'type': 'text', 'data': {'text': '这是一段新的机器人上文'}}])
        raw = self.raw('继续看这张图', image=True)
        raw['message'].insert(0, {'type': 'reply', 'data': {'id': '42'}})
        await self.feed(raw)
        self.assert_derivative(optional=False)
        self.assertIn('这是一段新的机器人上文', self.native[0].text)

    async def test_reply_to_other_member_does_not_become_a_new_address(self):
        self.reference_messages[43] = dict(message_id=43, self_id=90001, user_id=88888,
            sender={'user_id': 88888}, message_type='group', group_id=20001,
            message=[{'type': 'text', 'data': {'text': '另一位成员的公开消息'}}])
        raw = self.raw('这张图写了什么？', image=True)
        raw['message'].insert(0, {'type': 'reply', 'data': {'id': '43'}})
        await self.feed(raw)
        self.assertFalse(self.native)
        self.assertFalse(self.fetches)

    async def test_image_then_unaddressed_last_image_question_uses_index(self):
        await self.index_only(self.raw(image=True, mid=701))
        self.assertFalse(self.native)
        await self.feed(self.raw('看看我刚发的图里是什么', mid=702))
        self.assert_derivative()
        self.assertEqual(self.prepares[-1]['attachments'][0]['origin'], '701')
        self.assertEqual(self.native[0].message_id, '702')

    async def test_exact_previous_meme_question_uses_the_prior_image(self):
        await self.index_only(self.raw(image=True, mid=730))
        self.assertFalse(self.native)
        question = '看一下上一张表情包是什么内容'
        await self.feed(self.raw(question, mid=731))
        self.assert_derivative()
        self.assertEqual(self.prepares[-1]['attachments'][0]['origin'], '730')
        self.assertEqual(self.native[0].message_id, '731')
        self.assertEqual(self.questions, [question])

    async def test_natural_previous_image_reference_does_not_read_other_member(self):
        await self.index_only(self.raw(image=True, mid=732, user=88888))
        await self.feed(self.raw('看一下上一张表情包是什么内容', mid=733, user=77777))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)
        self.assertEqual(len(self.outbound), 1)
        self.assertIn('candidate_missing', str(self.outbound[0]))

    async def test_natural_previous_image_reference_does_not_pick_ambiguous_image(self):
        await self.index_only(self.raw(image=True, mid=734))
        await self.index_only(self.raw(image=True, mid=735))
        await self.feed(self.raw('看一下上一张表情包是什么内容', mid=736))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)
        self.assertEqual(len(self.outbound), 1)
        self.assertIn('candidate_ambiguous', str(self.outbound[0]))

    async def test_natural_previous_image_reference_does_not_read_other_group(self):
        await self.index_only(self.raw(image=True, mid=737))
        await self.feed(self.raw('看一下上一张表情包是什么内容', mid=738, group=20002))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)

    async def test_natural_previous_image_reference_respects_source_recall(self):
        await self.index_only(self.raw(image=True, mid=739))
        await self.feed(self.recall(739))
        await self.feed(self.raw('看一下上一张表情包是什么内容', mid=740))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)
        self.assertEqual(len(self.outbound), 1)
        self.assertIn('candidate_missing', str(self.outbound[0]))

    async def test_natural_previous_image_reference_respects_expiry(self):
        self.adapter.media.index.ttl = .01
        await self.index_only(self.raw(image=True, mid=741))
        await asyncio.sleep(.025)
        await self.feed(self.raw('看一下上一张表情包是什么内容', mid=742))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)
        self.assertEqual(len(self.outbound), 1)
        self.assertIn('candidate_missing', str(self.outbound[0]))

    async def test_incoming_raw_mutation_does_not_replace_bound_attachment(self):
        self.social.policy.ambient_debounce_seconds = .02
        raw = self.raw('这张图是什么？', image=True, mid=703)
        await self.feed(raw, drain=False)
        raw['message'][-1]['data']['url'] = 'https://example.invalid/CHANGED.png'
        await self.drain()
        self.assert_derivative()
        self.assertEqual(self.fetches, ['https://example.invalid/offline-image-703.png'])

    async def test_cached_candidate_is_frozen_before_debounce(self):
        await self.index_only(self.raw(image=True, mid=704))
        self.social.policy.ambient_debounce_seconds = .02
        await self.feed(self.raw('看看我刚发的图', mid=705), drain=False)
        # A later index entry must not replace the candidate already bound.
        # Keep the original entry: clearing/revoking it should fail closed.
        route = self.adapter.settings.isolation.resolve(self.pending[-1].source)
        scope = self.adapter.media.scope(route, self.adapter.settings.self_id)
        self.adapter.media.index.add(scope, [MediaRef(
            'image', '706', '77777', 0, handle='another-fixture-image',
            location='https://example.invalid/not-the-bound-image.png')])
        self.assertEqual(len(self.adapter.media.index.rows), 2)
        await self.drain()
        self.assert_derivative()
        self.assertEqual(self.prepares[-1]['attachments'][0]['origin'], '704')

    async def test_new_message_during_debounce_drops_old_optional_image(self):
        self.social.policy.ambient_debounce_seconds = .02
        await self.feed(self.raw('这张图是什么？', image=True), drain=False)
        await self.feed(self.raw('大家换个话题，先聊文字怎么样？', user=88888))
        self.assertFalse(self.fetches)
        self.assertEqual(len(self.native), 1)
        self.assertIn('换个话题', self.native[0].text)
        self.assertNotIn('本群已核验媒体资料', self.native[0].text)

    async def test_cleared_bound_candidate_cannot_fall_back_to_another_image(self):
        await self.index_only(self.raw(image=True, mid=719))
        self.social.policy.ambient_debounce_seconds = .02
        await self.feed(self.raw('看看我刚发的图', mid=720), drain=False)
        route = self.adapter.settings.isolation.resolve(self.pending[-1].source)
        scope = self.adapter.media.scope(route, self.adapter.settings.self_id)
        self.adapter.media.index.clear()
        self.adapter.media.index.add(scope, [MediaRef(
            'image', '999', '77777', 0, handle='replacement-not-authorized',
            location='https://example.invalid/never-reselect.png')])
        await self.drain()
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)

    async def test_bound_candidate_original_expiry_is_not_extended_by_queue(self):
        self.adapter.media.index.ttl = .025
        await self.index_only(self.raw(image=True, mid=721))
        self.social.policy.ambient_debounce_seconds = .06
        await self.feed(self.raw('看看我刚发的图', mid=722))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)

    async def test_optional_missing_candidate_error_does_not_require_a_new_at(self):
        await self.feed(self.raw('看看我刚发的图'))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)
        self.assertEqual(len(self.outbound), 1)
        text = str(self.outbound[0]['message'])
        self.assertIn('candidate_missing', text)
        self.assertNotIn('真实 @', text)
        self.assertNotIn('称呼测试实例甲', text)

    async def test_mutated_optional_route_cannot_read_bound_image(self):
        self.social.policy.ambient_debounce_seconds = .02
        await self.feed(self.raw('这张图写了什么？', image=True), drain=False)
        self.pending[-1].source.chat_id = 'group:29999'
        await self.drain()
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)

    async def test_cloud_disabled_is_reported_as_data_not_a_fake_vision_success(self):
        self.adapter.media.policy.cloud = []
        await self.feed(self.raw('这张图写了什么？', image=True))
        self.assertFalse(self.fetches)
        self.assertFalse(self.questions)
        self.assertEqual(len(self.native), 1)
        self.assertIn('cloud_image_denied', self.native[0].text)
        self.assertNotIn('OFFLINE_IMAGE_DERIVATIVE', self.native[0].text)

    async def slow_image(self, *, stage='fetch', mid=710):
        self.delay_stage = stage
        await self.feed(self.raw('这张图是什么？', image=True, mid=mid), drain=False)
        await asyncio.wait_for(self.started.wait(), 3)

    async def test_new_same_group_message_cancels_pending_optional_fetch(self):
        await self.slow_image()
        await self.feed(self.raw('大家换个话题聊聊怎么样？', mid=711, user=88888))
        self.assertIn('fetch', self.cancelled)
        self.assertFalse(self.questions)
        self.assertEqual(len(self.native), 1)
        self.assertIn('换个话题', self.native[0].text)
        self.assertFalse(self.outbound)

    async def test_pause_cancels_optional_media_but_named_media_still_works(self):
        await self.slow_image()
        await self.feed(self.raw('/social pause', mid=712))
        self.assertIn('fetch', self.cancelled)
        self.assertFalse(self.questions)
        self.assertTrue(self.social.states['group:20001'].paused)
        self.delay_stage = None
        self.native.clear()
        self.fetches.clear()
        await self.feed(self.raw('公开名字看这张图', image=True, mid=713))
        self.assert_derivative(optional=False)

    async def test_source_recall_cancels_optional_media(self):
        await self.slow_image()
        await self.feed(self.recall(710))
        self.assertIn('fetch', self.cancelled)
        self.assertFalse(self.native)
        self.assertFalse(self.questions)
        self.assertFalse(self.outbound)

    async def test_recalled_copied_context_cancels_optional_media(self):
        await self.feed(self.raw('先给图片提供一个公开上下文', mid=714, user=88888))
        self.native.clear()
        await self.slow_image(mid=715)
        await self.feed(self.recall(714, user=88888))
        self.assertIn('fetch', self.cancelled)
        self.assertFalse(self.native)
        self.assertFalse(self.questions)
        self.assertFalse(self.outbound)

    async def test_disconnect_cancels_optional_media(self):
        await self.slow_image()
        self.adapter._connection_lost()
        await self.drain()
        self.assertIn('fetch', self.cancelled)
        self.assertFalse(self.native)
        self.assertFalse(self.questions)

    async def test_detach_cancels_optional_media(self):
        await self.slow_image()
        self.social.port.detach()
        await self.drain()
        self.assertIn('fetch', self.cancelled)
        self.assertFalse(self.native)
        self.assertFalse(self.questions)

    async def test_old_port_cleanup_does_not_cancel_replacement_media(self):
        from social_plugin_helpers import install_social
        old_controller = self.social
        old_port = old_controller.port
        old_port.detach()
        await old_controller.stop()
        self.social = install_social(self.adapter, llm=self.llm)
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0
        await self.slow_image(mid=723)
        old_port.invalidate_optional()
        old_port.detach()
        self.assertFalse(self.cancelled)
        self.unblock.set()
        await self.drain()
        self.assert_derivative()

    async def test_recall_during_vision_cancels_without_late_native(self):
        await self.slow_image(stage='vision')
        await self.feed(self.recall(710))
        self.assertIn('vision', self.cancelled)
        self.assertFalse(self.native)
        self.assertFalse(self.outbound)

    async def test_other_group_activity_does_not_cancel_current_media(self):
        await self.slow_image()
        await self.feed(self.raw('另外一个群的正常文字', mid=716, user=88888, group=20002), drain=False)
        self.assertFalse(self.cancelled)
        self.unblock.set()
        await self.drain()
        self.assertEqual(len(self.questions), 1)
        media = [e for e in self.native if '本群已核验媒体资料' in e.text]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].source.chat_id, 'group:20001')

    async def test_denied_sender_or_group_cannot_index_or_analyze(self):
        raw = self.raw('看图', image=True)
        raw['sender']['user_id'] = 88888
        await self.feed(raw)
        await self.feed(self.raw('看图', image=True, group=29999))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)
        self.assertFalse(self.adapter.media.index.rows)
        self.assertFalse(self.adapter.media.reference_sources.rows)

    async def test_media_disabled_group_does_not_download_or_guess_pure_image(self):
        await self.feed(self.raw(image=True, group=20002))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)
        self.assertFalse(self.adapter.media.index.rows)

    async def test_recalled_candidate_is_not_replaced_by_another_image(self):
        await self.index_only(self.raw(image=True, mid=717))
        self.social.policy.ambient_debounce_seconds = .02
        await self.feed(self.raw('看看我刚发的图', mid=718), drain=False)
        await self.feed(self.recall(717))
        self.assertFalse(self.fetches)
        self.assertFalse(self.native)
        self.assertFalse(self.outbound)


if __name__ == '__main__':
    unittest.main()
