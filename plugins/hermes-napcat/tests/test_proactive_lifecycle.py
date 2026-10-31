"""Offline regressions for proactive observation and exact-source turn liveness.

Reuse the real hosted adapter/PluginLlm fixture, not its TestCase class: importing
that class into this module would make unittest discover run its tests twice.
All OneBot calls and model completions below remain synthetic and local.
"""
import asyncio
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_proactive as base
from hermes_napcat.isolation import AdmissionDenied
from social_plugin_helpers import install_social, optional_valid, raw_policy, release_optional

# Tool dispatch is real, but unrelated installed plugins must not be discovered
# just because this offline regression imports the host dispatcher.
with patch('hermes_cli.plugins.discover_plugins', return_value=None):
    import model_tools


def raw_text(text, mid, *, private=False, group=20001, user=10001):
    return base.event(message_id=mid, message_type='private' if private else 'group',
                      group_id=group, user_id=user,
                      sender={'user_id': user, 'card': '当前群昵称', 'nickname': '昵称'},
                      message=[{'type': 'text', 'data': {'text': text}}])


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    parsed = base.SocialTests.parsed

    async def asyncSetUp(self):
        await base.SocialTests.asyncSetUp(self)
        # The lightweight fixture predates the exact-permit lifetime hook.
        self.binding.validate_active = lambda: None
        self.dispatched, self.jobs = [], []
        self.adapter._queue_dispatch = self.dispatched.append
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0

    async def asyncTearDown(self):
        self.release.set()
        for job in self.jobs:
            if not job.done():
                job.cancel()
        if self.jobs:
            await asyncio.gather(*self.jobs, return_exceptions=True)
        await base.SocialTests.asyncTearDown(self)

    def spawn(self, coroutine):
        job = asyncio.create_task(coroutine)
        self.jobs.append(job)
        return job

    async def wait_model(self):
        await asyncio.wait_for(self.entered.wait(), 3)

    async def drain_ambient(self):
        tasks = list(self.social.ambient_tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 3)

    async def receive_dm(self, text, mid):
        with patch.object(self.adapter, 'handle_message', new_callable=AsyncMock) as handler:
            await self.adapter._receive(raw_text(text, mid, private=True))
        handler.assert_awaited_once()
        return handler.await_args.args[0]

    async def issue_ingress(self, inbound):
        """Use the real bridge handoff; never forge lifecycle admission flags."""
        async def accepted(event):
            event._gateway_accepted = True
        route = self.adapter.settings.isolation.resolve(inbound.source)
        with patch.object(self.adapter, 'handle_message', side_effect=accepted) as handler:
            await self.adapter._gateway_group_turn(inbound, route, [])
        handler.assert_awaited_once_with(inbound)
        self.assertEqual(self.adapter._group_ingress_route(inbound.source, inbound), route)
        return route

    async def start_group(self, inbound):
        await self.issue_ingress(inbound)
        self.assertTrue(self.adapter.admit_group_source(inbound.source))
        await self.adapter.on_processing_start(inbound)
        self.assertTrue(self.adapter.admit_group_source(inbound.source))
        authorize = self.adapter.tool_authority_for_source(inbound.source)
        self.assertIsNone(authorize('web_search', {'query': '本群公开话题'}))
        self.assertIsNotNone(authorize('read_file', {'path': '~/.hermes/MEMORY.md'}))
        self.adapter._check_reply_target(inbound.source.chat_id, inbound.message_id)
        return authorize

    async def ambient_pair(self):
        # A is copied into the RAM context; only B is the gateway/source anchor.
        await self.adapter._receive(raw_text('周末露营天气预报说会下雨。', 41, user=77777))
        await self.adapter._receive(raw_text('大家觉得周末露营下雨时怎么安排？', 42, user=88888))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), 1)
        inbound = self.dispatched[0]
        self.assertEqual(inbound.message_id, '42')
        self.assertEqual(inbound.source.user_id, '88888')
        self.assertFalse(inbound.reply_expected)
        self.assertEqual(inbound.text, '大家觉得周末露营下雨时怎么安排？')
        self.assertIn('周末露营天气预报说会下雨。', self.social.prepare_public_context(inbound).text)
        self.assertTrue(optional_valid(self.adapter, inbound.source))
        return inbound

    async def test_ambiguous_short_question_reaches_model_with_public_context(self):
        await self.adapter._receive(raw_text('今天下午去了一家新咖啡馆。', 43, user=77777))
        await self.adapter._receive(raw_text('你觉得呢？', 44, user=88888))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0].message_id, '44')
        await self.start_group(self.dispatched[0])
        prompt = self.adapter.conversation_prompt_for_source(self.dispatched[0].source)
        self.assertIn('新咖啡馆', prompt)
        self.assertIn('NO_REPLY', prompt)
        await self.adapter.on_processing_complete(self.dispatched[0], 'completed')
        self.assertFalse(self.social.ambient_tasks)
        self.assertIn('你觉得呢？', json.dumps(self.social.context('group:20001'), ensure_ascii=False))

    async def invalidate_ambient(self, reason):
        if reason == 'pause':
            await self.adapter._receive(raw_text('/napcat-social pause', 90, user=77777))
            self.assertTrue(self.social.states['group:20001'].paused)
        elif reason in {'context_recall', 'source_recall'}:
            mid = 41 if reason == 'context_recall' else 42
            await self.adapter._receive(dict(post_type='notice', notice_type='group_recall',
                self_id=90001, group_id=20001, user_id=77777 if mid == 41 else 88888,
                operator_id=10001, message_id=mid))
        elif reason == 'new_message':
            await self.adapter._receive(raw_text('公开名字，后来明确向你的请求C', 93, user=77777))
        elif reason == 'disconnect':
            self.adapter._connection_lost()
        else:
            raise AssertionError(reason)

    def assert_ambient_denied(self, inbound, authorize=None):
        self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertIsNone(self.adapter._group_ingress_route(inbound.source, inbound))
        self.assertFalse(self.adapter.admit_group_source(inbound.source))
        with self.assertRaises(AdmissionDenied):
            self.adapter.tool_authority_for_source(inbound.source)
        if authorize is not None:
            self.assertIsNotNone(authorize('web_search', {'query': '迟到的公开搜索'}))
        with self.assertRaises(AdmissionDenied):
            self.adapter._check_reply_target(inbound.source.chat_id, inbound.message_id)

    async def check_ingress_revocation(self, reason):
        inbound = await self.ambient_pair()
        await self.issue_ingress(inbound)
        await self.invalidate_ambient(reason)
        self.assert_ambient_denied(inbound)
        with self.assertRaises(AdmissionDenied):
            await self.adapter.on_processing_start(inbound)
        self.assertFalse(self.sent)

    async def check_model_late_revocation(self, reason):
        inbound = await self.ambient_pair()
        authorize = await self.start_group(inbound)
        self.delay = True

        async def model_callback():
            # Exercise the actual facade/provider callback, with the lifecycle's
            # ContextVar snapshot still present when the slow model returns.
            response = await self.binding.facade().acomplete(
                messages=[{'role': 'user', 'content': inbound.text}],
                max_tokens=128, timeout=5, purpose='napcat_lifecycle_regression')
            text = json.loads(response.text)['text']
            late_denial = authorize('web_search', {'query': '模型回来后才派发'})
            from agent.tool_authority import bind_tool_authority
            from tools import web_tools
            with patch('hermes_cli.plugins.discover_plugins', return_value=None), \
                    bind_tool_authority(authorize), patch.object(
                        web_tools, 'web_search_tool', return_value='{"success":true}') as search:
                tool_result = model_tools.handle_function_call('web_search', {'query': '迟到工具'},
                    skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
                    skip_tool_execution_middleware=True)
            search.assert_not_called()
            self.assertIn('group_tool_principal_expired', tool_result)
            sent = await self.adapter.send(inbound.source.chat_id, text, reply_to=inbound.message_id)
            return late_denial, sent

        task = self.spawn(model_callback())
        try:
            await self.wait_model()
            await self.invalidate_ambient(reason)
            self.assert_ambient_denied(inbound, authorize)
            self.release.set()
            late_denial, sent = await asyncio.wait_for(task, 5)
            self.assertIsNotNone(late_denial)
            self.assertFalse(sent.success)
            self.assertFalse(self.sent)
            self.assertEqual(len(self.calls), 1)
        finally:
            self.release.set()
            await self.adapter.on_processing_complete(inbound, None)

    async def enable_observation_only_group(self):
        cfg = base.social_config()
        # This group is an idle/observation target, NOT a participation group.
        policy = raw_policy(targets=['dm:10001', 'group:20001', 'group:20002'])
        self.assertEqual(policy['participation_groups'], ['20001'])
        client = self.adapter.client
        await self.adapter.disconnect()
        self.adapter = base.HostedNapCatAdapter(cfg, self.binding, self.bridge)
        self.adapter.client = client
        self.adapter._queue_dispatch = self.dispatched.append
        self.social = install_social(self.adapter, policy, llm=self.llm)
        for state in self.social.states.values():
            state.last_activity = time.time() - 4000
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0

    async def test_authenticated_friend_recall_removes_dm_context_and_stales_idle_model(self):
        await self.receive_dm('只能在主人私聊出现的A', 11)
        self.social.observe(self.parsed('同ID但属于群的内容', mid=11))
        state = self.social.states['dm:10001']
        state.last_activity = time.time() - 4000
        generation = state.generation
        self.delay = True
        idle = self.spawn(self.social.tick('dm:10001'))
        await self.wait_model()
        await self.adapter._receive(dict(post_type='notice', notice_type='friend_recall',
                                         self_id=90001, user_id=10001, message_id=11))
        self.assertEqual(state.generation, generation + 1)
        self.assertEqual(self.social.context('dm:10001'), [])
        self.assertIn('同ID但属于群的内容', json.dumps(self.social.context('group:20001'), ensure_ascii=False))
        self.release.set()
        self.assertFalse(await asyncio.wait_for(idle, 3))
        self.assertEqual([action for action, _ in self.sent], ['get_friend_list'])
        self.assertEqual(self.social.counters['stale'], 1)

    async def test_wrong_self_or_user_friend_recall_cannot_remove_context_or_revoke_idle(self):
        await self.receive_dm('合法私聊缓存', 12)
        state = self.social.states['dm:10001']
        state.last_activity = time.time() - 4000
        generation = state.generation
        self.delay = True
        idle = self.spawn(self.social.tick('dm:10001'))
        await self.wait_model()
        for changes in ({'self_id': 90002}, {'user_id': 10002}, {'user_id': 77777}):
            with self.subTest(changes=changes):
                notice = dict(post_type='notice', notice_type='friend_recall',
                              self_id=90001, user_id=10001, message_id=12)
                notice.update(changes)
                await self.adapter._receive(notice)
                self.assertEqual(state.generation, generation)
                self.assertIn('合法私聊缓存', json.dumps(self.social.context('dm:10001'), ensure_ascii=False))
        self.release.set()
        self.assertTrue(await asyncio.wait_for(idle, 3))
        self.assertEqual([action for action, _ in self.sent],
                         ['get_friend_list', 'send_private_msg', 'get_msg'])

    async def test_friend_recall_does_not_revoke_original_native_owner_reply(self):
        completed = []
        async def native_callback(inbound):
            await self.adapter.on_processing_start(inbound)
            try:
                await self.adapter._receive(dict(post_type='notice', notice_type='friend_recall',
                    self_id=90001, user_id=10001, message_id=inbound.message_id))
                self.assertEqual(self.social.context('dm:10001'), [])
                self.adapter._check_reply_target('dm:10001', inbound.message_id)
                completed.append(await self.adapter.send('dm:10001', '正常主人回复', reply_to=inbound.message_id))
            finally:
                await self.adapter.on_processing_complete(inbound, None)
        with patch.object(self.adapter, 'handle_message', side_effect=native_callback):
            await self.adapter._receive(raw_text('普通主人请求', 13, private=True))
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0].success)
        self.assertEqual([action for action, _ in self.sent], ['get_friend_list', 'send_private_msg'])

    async def test_authenticated_dm_is_observed_before_friend_refresh_await(self):
        state = self.social.states['dm:10001']
        generation = state.generation
        self.delay = True
        idle = self.spawn(self.social.tick('dm:10001'))
        await self.wait_model()
        refreshing, finish_refresh = asyncio.Event(), asyncio.Event()
        async def slow_refresh():
            refreshing.set()
            await finish_refresh.wait()
        try:
            with patch.object(self.adapter, '_refresh_friends', side_effect=slow_refresh), \
                    patch.object(self.adapter, 'handle_message', new_callable=AsyncMock) as handler:
                incoming = self.spawn(self.adapter._receive(raw_text('刷新好友时已经收到的新私聊', 14, private=True)))
                await asyncio.wait_for(refreshing.wait(), 3)
                self.assertEqual(state.generation, generation + 1)
                self.assertIn('刷新好友时已经收到的新私聊', json.dumps(self.social.context('dm:10001'), ensure_ascii=False))
                handler.assert_not_awaited()
                self.release.set()
                self.assertFalse(await asyncio.wait_for(idle, 3))
                self.assertFalse(self.sent)
                finish_refresh.set()
                await asyncio.wait_for(incoming, 3)
                handler.assert_awaited_once()
                # A second observation after display-name enrichment must dedupe.
                self.assertEqual(state.generation, generation + 1)
        finally:
            finish_refresh.set()

    async def test_unauthenticated_private_input_cannot_observe_or_refresh_friends(self):
        state = self.social.states['dm:10001']
        generation, activity = state.generation, state.last_activity
        with patch.object(self.adapter, '_refresh_friends', new_callable=AsyncMock) as refresh, \
                patch.object(self.adapter, 'handle_message', new_callable=AsyncMock) as handler:
            for changes in ({'self_id': 90002}, {'sender': {'user_id': 77777}},
                            {'user_id': 77777, 'sender': {'user_id': 77777}}):
                with self.subTest(changes=changes):
                    raw = raw_text('伪造私聊不得打断空闲模型', 15, private=True)
                    raw.update(changes)
                    await self.adapter._receive(raw)
            refresh.assert_not_awaited()
            handler.assert_not_awaited()
        self.assertEqual(state.generation, generation)
        self.assertEqual(state.last_activity, activity)
        self.assertEqual(self.social.context('dm:10001'), [])

    async def test_target_without_participation_observes_unaddressed_but_never_dispatches(self):
        await self.enable_observation_only_group()
        state = self.social.states['group:20002']
        generation, activity = state.generation, state.last_activity
        raw = raw_text('仅观察目标群的未点名消息', 21, group=20002, user=77777)
        await self.adapter._receive(raw)
        await self.drain_ambient()
        self.assertEqual(state.generation, generation + 1)
        self.assertGreater(state.last_activity, activity)
        self.assertIn('仅观察目标群的未点名消息', json.dumps(self.social.context('group:20002'), ensure_ascii=False))
        self.assertFalse(self.social.due('group:20002'))
        self.assertFalse(self.dispatched)
        self.assertFalse(self.social.ambient_tasks)
        self.assertFalse(self.calls)
        self.assertFalse(self.sent)
        self.assertIsNone(self.adapter.convert_event(raw))
        await self.adapter._receive(raw)
        self.assertEqual(state.generation, generation + 1)

    async def test_observation_only_target_new_input_revokes_inflight_idle(self):
        await self.enable_observation_only_group()
        self.delay = True
        idle = self.spawn(self.social.tick('group:20002'))
        await self.wait_model()
        await self.adapter._receive(raw_text('尚未点名但有人正在聊天', 22, group=20002, user=77777))
        self.release.set()
        self.assertFalse(await asyncio.wait_for(idle, 3))
        self.assertFalse(self.dispatched)
        self.assertFalse(self.sent)
        self.assertEqual(self.social.counters['stale'], 1)

    async def test_observation_only_target_keeps_identity_and_group_boundaries(self):
        await self.enable_observation_only_group()
        state = self.social.states['group:20002']
        generation = state.generation
        for changes in ({'self_id': 90002}, {'sender': {'user_id': 88888}},
                        {'group_id': 29999}, {'user_id': 90001, 'sender': {'user_id': 90001}}):
            with self.subTest(changes=changes):
                raw = raw_text('不得观察的来源', 23, group=20002, user=77777)
                raw.update(changes)
                await self.adapter._receive(raw)
                self.assertEqual(state.generation, generation)
        self.assertEqual(self.social.context('group:20002'), [])
        self.assertFalse(self.dispatched)

    async def test_unaddressed_controls_use_formal_gateway_and_resume_is_owner_only(self):
        await self.enable_observation_only_group()
        for group in (20001, 20002):
            state = self.social.states['group:' + str(group)]
            for offset, (action, user, paused) in enumerate((
                    ('pause', 77777, True), ('status', 77777, True),
                    ('resume', 77777, True), ('resume', 10001, False))):
                with self.subTest(group=group, action=action, user=user):
                    self.dispatched.clear()
                    await self.adapter._receive(raw_text('/napcat-social ' + action,
                        group + offset, group=group, user=user))
                    await self.drain_ambient()
                    self.assertEqual(len(self.dispatched), 1)
                    inbound = self.dispatched[0]
                    self.assertTrue(inbound.reply_expected)
                    self.assertFalse(inbound.allow_gateway_control)
                    self.assertEqual(inbound.source.user_id, str(user))
                    self.assertIn('本地已核验控制结果', inbound.text)
                    self.assertEqual(state.paused, paused)
                    if action == 'resume' and user != 10001:
                        self.assertIn('只有已验证主人', inbound.text)
                    authorize = await self.start_group(inbound)
                    try:
                        self.assertIsNone(authorize('web_search', {'query': '正常工具面不变'}))
                    finally:
                        await self.adapter.on_processing_complete(inbound, None)
        self.assertFalse(self.calls)
        self.assertFalse(self.sent)

    async def test_ambient_automatically_gets_exact_source_grant_and_normal_reply(self):
        inbound = await self.ambient_pair()
        self.assertFalse(optional_valid(self.adapter, copy.copy(inbound.source)))
        authorize = await self.start_group(inbound)
        try:
            self.assertIsNone(authorize('web_extract', {'urls': ['https://example.org']}))
            sent = await self.adapter.send('group:20001', '只在获准测试群自然接话', reply_to='42')
            self.assertTrue(sent.success)
            self.assertEqual(self.sent[0][1]['group_id'], 20001)
        finally:
            await self.adapter.on_processing_complete(inbound, None)
        self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertFalse(self.adapter.admit_group_source(inbound.source))
        self.assertFalse((await self.adapter.send('group:20001', '不得借旧permit')).success)

    async def test_pause_revokes_ambient_before_lifecycle_starts(self):
        await self.check_ingress_revocation('pause')

    async def test_context_a_recall_revokes_b_ingress_before_lifecycle_starts(self):
        await self.check_ingress_revocation('context_recall')

    async def test_pause_rejects_late_model_reply_and_tool(self):
        await self.check_model_late_revocation('pause')

    async def test_context_a_recall_rejects_b_late_model_reply_and_tool(self):
        await self.check_model_late_revocation('context_recall')

    async def test_source_recall_rejects_late_model_reply_and_tool(self):
        await self.check_model_late_revocation('source_recall')

    async def test_new_input_rejects_late_model_reply_and_tool(self):
        await self.check_model_late_revocation('new_message')

    async def test_disconnect_rejects_late_model_reply_and_tool(self):
        await self.check_model_late_revocation('disconnect')

    async def test_copied_ambient_source_markers_cannot_mint_gateway_admission(self):
        inbound = await self.ambient_pair()
        forged = copy.copy(inbound)
        forged.source = copy.copy(inbound.source)
        self.assertEqual(forged.source.to_dict(), inbound.source.to_dict())
        self.assertFalse(optional_valid(self.adapter, forged.source))
        route = self.adapter.settings.isolation.resolve(forged.source)
        with patch.object(self.adapter, 'handle_message', new_callable=AsyncMock) as handler:
            with self.assertRaises(AdmissionDenied):
                await self.adapter._gateway_group_turn(forged, route, [])
            handler.assert_not_awaited()
        self.assert_ambient_denied(forged)
        # A failed forged source must not revoke the original source's grant.
        self.assertTrue(optional_valid(self.adapter, inbound.source))

    async def test_source_marker_without_transport_ledger_cannot_mint_admission(self):
        inbound = await self.ambient_pair()
        release_optional(self.adapter, inbound.source)
        self.assert_ambient_denied(inbound)
        route = self.adapter.settings.isolation.resolve(inbound.source)
        with patch.object(self.adapter, 'handle_message', new_callable=AsyncMock) as handler:
            with self.assertRaises(AdmissionDenied):
                await self.adapter._gateway_group_turn(inbound, route, [])
            handler.assert_not_awaited()

    async def test_ambient_deadline_expires_even_with_exact_source(self):
        inbound = await self.ambient_pair()
        with patch('hermes_napcat.participation.time.monotonic', return_value=float('inf')):
            self.assertFalse(optional_valid(self.adapter, inbound.source))
        self.assertFalse(self.sent)

    async def test_releasing_copy_does_not_release_original_exact_source(self):
        inbound = await self.ambient_pair()
        release_optional(self.adapter, copy.copy(inbound.source))
        self.assertTrue(optional_valid(self.adapter, inbound.source))
        release_optional(self.adapter, inbound.source)
        self.assertFalse(optional_valid(self.adapter, inbound.source))

    async def test_pause_context_recall_and_new_input_do_not_revoke_addressed_turn(self):
        for offset, reason in enumerate(('pause', 'context_recall', 'new_message')):
            with self.subTest(reason=reason):
                inbound = self.parsed('公开名字正常点名仍可查公开资料', mid=110 + offset)
                self.assertTrue(inbound.reply_expected)
                authorize = await self.start_group(inbound)
                try:
                    await self.invalidate_ambient(reason)
                    self.assertTrue(self.adapter.admit_group_source(inbound.source))
                    self.assertIsNone(authorize('web_search', {'query': '正常点名'}))
                    self.adapter._check_reply_target('group:20001', inbound.message_id)
                finally:
                    await self.adapter.on_processing_complete(inbound, None)

    async def test_disconnect_does_not_block_fresh_addressed_turn_on_new_epoch(self):
        old = self.parsed('公开名字断线前的问题', mid=120)
        old_authorize = await self.start_group(old)
        self.adapter._connection_lost()
        self.assertFalse(self.adapter.admit_group_source(old.source))
        self.assertIsNotNone(old_authorize('web_search', {'query': '旧连接'}))
        await self.adapter.on_processing_complete(old, None)
        new = self.parsed('公开名字新连接的新问题', mid=121)
        new_authorize = await self.start_group(new)
        try:
            self.assertIsNone(new_authorize('web_search', {'query': '新连接'}))
            self.adapter._check_reply_target('group:20001', '121')
        finally:
            await self.adapter.on_processing_complete(new, None)
