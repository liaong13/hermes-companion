"""Verified friend DMs use a real facade and capture-only transport."""
import asyncio
import base64
import copy
from contextvars import ContextVar
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_proactive as base
from test_plugin import event
from hermes_napcat.adapter import Settings
from hermes_napcat.friend_dm import FriendDmPolicy
from hermes_napcat.isolation import AdmissionDenied


class PolicyTests(unittest.TestCase):
    def test_explicit_users_are_separate_from_private_allowed_users(self):
        cfg = base.social_config()
        cfg.extra['friend_dm'] = {'enabled': True, 'users': ['77777']}
        settings = Settings(cfg.extra)
        self.assertNotIn('77777', settings.users)
        self.assertFalse(settings.friend_dm.allows('77777'))
        settings.friend_dm.update({'77777', '88888'})
        self.assertTrue(settings.friend_dm.allows('77777'))
        self.assertFalse(settings.friend_dm.allows('88888'))

    def test_verified_friends_still_requires_current_transport_evidence(self):
        policy = FriendDmPolicy({'enabled': True, 'admission': 'verified_friends'}, owner_id='10001', self_id='90001')
        policy.update({'77777'})
        self.assertTrue(policy.allows('77777'))
        self.assertFalse(policy.allows('88888'))
        self.assertFalse(policy.permits_target('10001'))
        self.assertFalse(policy.permits_target('90001'))
        policy.expires = time.monotonic()-1
        self.assertFalse(policy.allows('77777'))

    def test_group_member_mode_requires_friend_and_fresh_allowed_group_proof(self):
        policy = FriendDmPolicy({'enabled': True, 'allow_group_members': True},
            owner_id='10001', self_id='90001', groups={'20001'})
        self.assertTrue(policy.permits_target('77777'))
        policy.update({'77777'})
        self.assertFalse(policy.allows('77777'))
        policy.update_membership('77777', {'20002'})
        self.assertFalse(policy.allows('77777'))
        policy.update_membership('77777', {'20001'})
        self.assertTrue(policy.allows('77777'))
        policy.groups = frozenset({'20002'})
        self.assertFalse(policy.allows('77777'))
        policy.groups = frozenset({'20001'})
        policy.update((), ttl=0)
        self.assertFalse(policy.allows('77777'))
        self.assertFalse(policy._memberships)

    def test_disabled_and_malformed_configuration_fail_closed(self):
        for raw in [{'enabled': 'true'}, {'users': ['10001']}, {'users': ['90001']},
                    {'users': [77777]}, {'admission': 'any_user'}, {'unknown': True}]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                FriendDmPolicy(raw, owner_id='10001', self_id='90001')
        policy = FriendDmPolicy(None, owner_id='10001', self_id='90001')
        policy.update({'77777'})
        self.assertFalse(policy.allows('77777'))


class FriendDmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = base.social_config()
        cfg.extra['friend_dm'] = dict(enabled=True, admission='verified_friends', media=True)
        cfg.extra['media'] = dict(enabled=True, groups=['20001'], types=['image', 'file'], cloud=[],
                                  references=True, observe_metadata=True, allow_derived_text=True)
        with patch.object(base, 'social_config', return_value=cfg):
            await base.SocialTests.asyncSetUp(self)
        # Classifier semantics and lifecycle are covered by social's own suite;
        # these transport fixtures explicitly admit the already-verified DM.
        self.social.prepare_direct_dialogue = AsyncMock(return_value=True)
        self.friend_rows = [dict(user_id='77777', nickname='公开好友甲', remark='PRIVATE_REMARK_CANARY'),
                            dict(user_id='88888', nickname='公开好友乙', remark='OTHER_PRIVATE_REMARK_CANARY')]
        self.actions, self.model_calls, self.echoes = [], [], {}
        self.reply_text = '合成好友回复：接着你的具体问题聊。'
        self.model_entered, self.model_release = asyncio.Event(), asyncio.Event()
        self.block_model = False
        self.context_value = ContextVar('friend-private-canary', default='empty')
        async def provider(**kwargs):
            self.model_calls.append((copy.deepcopy(kwargs), self.context_value.get()))
            self.model_entered.set()
            if self.block_model:
                await self.model_release.wait()
            return 'offline', 'same-host-model', SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.reply_text))], usage=None)
        self.llm._async_caller = provider
        async def call(action, payload):
            self.actions.append((action, copy.deepcopy(payload)))
            if action == 'get_friend_list':
                return copy.deepcopy(self.friend_rows)
            if action == 'send_private_msg':
                mid = str(7000+len(self.echoes))
                self.echoes[mid] = dict(message_id=mid, self_id='90001', user_id='90001',
                    message_type='private', target_id=str(payload['user_id']),
                    message=copy.deepcopy(payload['message']))
                return {'message_id': mid}
            if action == 'get_msg':
                return copy.deepcopy(self.echoes.get(str(payload['message_id']), {}))
            if action == 'get_private_file_url':
                return {'url': 'https://example.invalid/private-file.txt'}
            raise AssertionError('unexpected synthetic action '+action)
        self.adapter.client.call = call

    async def asyncTearDown(self):
        self.model_release.set()
        await base.SocialTests.asyncTearDown(self)

    def raw(self, text='好友合成话题', mid=1, user=77777, **overrides):
        raw = event(message_type='private', sub_type='friend', user_id=user, message_id=mid,
                    sender=dict(user_id=user, nickname='公开好友甲'),
                    message=[{'type': 'text', 'data': {'text': text}}])
        raw.pop('group_id', None)
        raw.update(overrides)
        return raw

    async def drain(self):
        while self.adapter._friend_jobs:
            await asyncio.gather(*tuple(self.adapter._friend_jobs), return_exceptions=True)

    async def feed(self, text='好友合成话题', mid=1, user=77777, **overrides):
        await self.adapter._receive(self.raw(text, mid, user, **overrides))
        await self.drain()

    def sends(self):
        return [payload for action, payload in self.actions if action == 'send_private_msg']

    async def test_friend_dm_is_not_dispatched_to_owner_agent(self):
        self.adapter.handle_message = AsyncMock()
        await self.feed()
        self.adapter.handle_message.assert_not_awaited()
        self.assertEqual(len(self.model_calls), 1)
        self.assertEqual(self.sends()[0]['user_id'], 77777)
        self.assertEqual(self.sends()[0]['message'][0]['data']['id'], '1')
        self.assertNotIn('77777', self.adapter.settings.users)

    async def test_fresh_context_and_names_do_not_expose_owner_remarks(self):
        token = self.context_value.set('PRIVATE_CONTEXT_CANARY')
        try:
            await self.feed()
        finally:
            self.context_value.reset(token)
        request = json.dumps(self.model_calls[0][0], ensure_ascii=False)
        self.assertEqual(self.model_calls[0][1], 'empty')
        self.assertNotIn('PRIVATE_REMARK_CANARY', request)
        self.assertNotIn('PRIVATE_CONTEXT_CANARY', request)
        self.assertNotIn('tools', self.model_calls[0][0])

    async def test_unknown_or_spoofed_friend_does_not_call_model(self):
        await self.feed(user=99999)
        await self.feed(mid=2, sender={'user_id': 88888, 'nickname': '主人'})
        await self.feed(mid=3, self_id=11111)
        await self.feed(mid=4, sub_type='group')
        self.assertFalse(self.model_calls)
        self.assertFalse(self.sends())

    async def test_two_friends_do_not_share_context(self):
        await self.feed('FRIEND_A_CANARY', mid=1)
        await self.feed('第二位好友的事情', mid=2, user=88888)
        self.assertNotIn('FRIEND_A_CANARY', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        self.assertEqual([row['user_id'] for row in self.sends()], [77777, 88888])

    async def test_same_friend_can_continue_previous_exchange(self):
        await self.feed('合成方案甲和乙有什么差别？', mid=1)
        await self.feed('我选择乙', mid=2)
        request = json.dumps(self.model_calls[-1][0], ensure_ascii=False)
        self.assertIn('合成方案甲和乙', request)
        self.assertIn('合成好友回复', request)

    async def test_owner_keeps_native_private_path(self):
        self.adapter.handle_message = AsyncMock()
        await self.adapter._receive(self.raw('主人自己的请求', user=10001))
        self.adapter.handle_message.assert_awaited_once()
        self.assertFalse(self.model_calls)

    async def test_friend_removal_before_reply_prevents_send(self):
        self.block_model = True
        await self.adapter._receive(self.raw())
        await asyncio.wait_for(self.model_entered.wait(), 2)
        self.friend_rows = []
        self.model_release.set()
        await self.drain()
        self.assertFalse(self.sends())

    async def test_recall_cancels_inflight_reply(self):
        self.block_model = True
        await self.adapter._receive(self.raw())
        await asyncio.wait_for(self.model_entered.wait(), 2)
        await self.adapter._receive(dict(post_type='notice', notice_type='friend_recall',
            self_id=90001, user_id=77777, message_id=1))
        self.model_release.set()
        await self.drain()
        self.assertFalse(self.sends())

    async def test_disconnect_cancels_inflight_reply(self):
        self.block_model = True
        await self.adapter._receive(self.raw())
        await asyncio.wait_for(self.model_entered.wait(), 2)
        self.adapter._connection_lost()
        self.model_release.set()
        await self.drain()
        self.assertFalse(self.sends())

    async def test_duplicate_event_does_not_repeat_model_or_send(self):
        await self.feed()
        await self.feed()
        self.assertEqual(len(self.model_calls), 1)
        self.assertEqual(len(self.sends()), 1)

    async def test_explicit_user_list_still_requires_verified_friend(self):
        self.adapter.settings.friend_dm.admission = 'explicit'
        self.adapter.settings.friend_dm.users = frozenset({'77777', '99999'})
        await self.feed(user=88888)
        await self.feed(mid=2, user=99999)
        await self.feed(mid=3)
        self.assertEqual(len(self.model_calls), 1)
        self.assertEqual(self.sends()[0]['user_id'], 77777)

    async def test_direct_file_uses_private_handle_and_original_media_pipeline(self):
        self.adapter.media.fetcher.fetch = AsyncMock(return_value=(b'synthetic file bytes', 'text/plain'))
        self.adapter.media.sandbox.process = AsyncMock(return_value={'text': '合成私聊文件正文', 'warnings': []})
        message = [dict(type='text', data={'text': '请看这份资料'}),
                   dict(type='file', data={'file_id': 'synthetic-handle', 'name': 'notes.txt'})]
        await self.feed(message=message)
        self.assertTrue(any(action == 'get_private_file_url' for action, _ in self.actions))
        self.assertIn('合成私聊文件正文', json.dumps(self.model_calls[0][0], ensure_ascii=False))
        self.assertFalse(any(action == 'get_group_file_url' for action, _ in self.actions))

    async def test_cross_friend_media_quote_is_denied(self):
        self.echoes['50'] = dict(message_id='50', self_id='90001', user_id='88888',
            message_type='private', target_id='90001', sender={'user_id': '88888'},
            message=[dict(type='file', data={'url': 'https://example.invalid/other.txt', 'name': 'other.txt'})])
        await self.feed(message=[dict(type='reply', data={'id': '50'}), dict(type='text', data={'text': '读这个'})])
        self.assertFalse(self.model_calls)
        self.assertIn('reference_source_denied', json.dumps(self.sends(), ensure_ascii=False))

    async def test_port_friend_targets_and_permissions_are_live(self):
        await self.adapter._refresh_friends(force=True)
        port = self.social.port
        self.assertTrue(port.friend_dm_enabled)
        self.assertTrue(port.dm_target_allowed('dm:77777'))
        self.assertTrue(port.dm_allowed('dm:77777'))
        self.assertFalse(port.dm_allowed('dm:10001'))
        self.assertEqual(port.public_dm_users, frozenset({'77777', '88888'}))
        self.adapter.settings.friend_dm.update((), ttl=0)
        self.assertFalse(port.dm_allowed('dm:77777'))

    async def test_exact_dm_reference_reads_do_not_cross_friends_or_owner(self):
        self.echoes['51'] = dict(message_id='51', message_type='private', user_id='77777', self_id='90001',
            sender={'user_id': '77777', 'nickname': '公开好友甲', 'remark': 'PRIVATE_REMARK_CANARY'},
            message=[dict(type='text', data={'text': '此前的合成话题'})])
        self.echoes['52'] = dict(message_id='52', message_type='private', user_id='88888', self_id='90001',
            sender={'user_id': '88888', 'nickname': '公开好友乙'}, message=[dict(type='text', data={'text': 'OTHER_FRIEND_CANARY'})])
        rows = await self.social.port.read_dialogue_refs('dm:77777', ['51', '52'])
        self.assertEqual([row['message_id'] for row in rows], ['51'])
        self.assertNotIn('PRIVATE_REMARK_CANARY', json.dumps(rows, ensure_ascii=False))
        self.assertEqual(await self.social.port.read_dialogue_refs('dm:10001', ['51']), [])

    async def test_exact_group_reference_needs_full_group_evidence(self):
        self.echoes['61'] = dict(message_id='61', message_type='group', group_id='20001', user_id='77777',
            sender={'user_id': '77777', 'card': '当前群名片'}, message=[dict(type='text', data={'text': '群公开资料'})])
        self.echoes['62'] = dict(message_id='62', message_type='private', user_id='77777',
            sender={'user_id': '77777'}, message=[dict(type='text', data={'text': 'PRIVATE_DM_CANARY'})])
        rows = await self.social.port.read_dialogue_refs('group:20001', ['61', '62'])
        self.assertEqual([row['message_id'] for row in rows], ['61'])
        self.assertEqual(rows[0]['name'], '当前群名片')
        self.assertEqual(await self.social.port.read_dialogue_refs('group:20002', ['61']), [])

    async def test_reference_requests_are_bounded_and_recalled_sources_are_excluded(self):
        port = self.social.port
        self.assertEqual(await port.read_dialogue_refs('group:20001', ['1']*9), [])
        self.assertEqual(await port.read_dialogue_refs('group:20001', ['not-a-real-ID']), [])
        self.echoes['63'] = dict(message_id='63', message_type='group', group_id='20001', user_id='77777',
            sender={'user_id': '77777'}, message=[dict(type='text', data={'text': '待撤回资料'})])
        self.adapter._media_revoked[('group:20001', '63')] = time.monotonic()
        self.assertEqual(await port.read_dialogue_refs('group:20001', ['63']), [])

    async def test_public_reply_limit_shapes_friend_but_never_owner_private(self):
        self.social.reply_char_limit = lambda chat: 48
        self.reply_text = '这是一段合成的较长回复。'*10
        await self.feed()
        sent = ''.join(segment['data']['text'] for segment in self.sends()[0]['message'] if segment['type'] == 'text')
        self.assertLessEqual(len(sent), 48)
        self.assertEqual(self.adapter._friend_history['dm:77777'][-1]['text'], sent)
        self.assertEqual(self.adapter._shape_public_reply('dm:10001', self.reply_text), self.reply_text)

    async def test_explicit_forget_removes_friend_fallback_context(self):
        await self.feed('FORGET_SOURCE_CANARY', mid=1)
        self.assertTrue(self.social.forget_messages('dm:77777', {'1'}))
        self.assertFalse(self.adapter._friend_history['dm:77777'])
        await self.feed('继续新的讨论', mid=2)
        self.assertNotIn('FORGET_SOURCE_CANARY', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        self.assertFalse(self.social.port.forget_dialogue_refs('dm:10001', {'1'}))

    async def test_image_uses_existing_controlled_parser_and_recognizer(self):
        self.adapter.media.policy.cloud = ['image']
        self.adapter.media.fetcher.fetch = AsyncMock(return_value=(b'synthetic image bytes', 'image/png'))
        self.adapter.media.sandbox.process = AsyncMock(return_value={'source_mime': 'image/png',
            'data': base64.b64encode(b'normalized synthetic pixels').decode(), 'warnings': []})
        self.adapter.media.provider = SimpleNamespace(recognize=AsyncMock(return_value={
            'text': '合成图像显示两个方块', 'model': 'offline', 'warnings': []}))
        await self.feed(message=[dict(type='text', data={'text': '看看图片'}),
            dict(type='image', data={'url': 'https://example.invalid/image.png'})])
        self.assertIn('合成图像显示两个方块', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        self.adapter.media.provider.recognize.assert_awaited_once()

    async def test_unknown_readback_never_records_a_verified_self_reply(self):
        original = self.adapter.client.call
        async def call(action, payload):
            if action == 'get_msg':
                return {'message_id': payload['message_id'], 'message_type': 'group', 'group_id': '20001'}
            return await original(action, payload)
        self.adapter.client.call = call
        with patch.object(self.social, 'record_self', wraps=self.social.record_self) as record:
            await self.feed()
        self.assertEqual(len(self.sends()), 1)
        record.assert_not_called()
        self.assertFalse(self.adapter._friend_history)

    async def test_one_shot_active_contact_uses_same_friend_target_and_budget(self):
        await self.adapter._refresh_friends(force=True)
        port = self.social.port
        self.social.policy.targets = frozenset({'dm:77777'})
        port.bind(self.social)
        await port.prepare_idle('dm:77777', 'a'*32, [])
        grant = port.claim_idle('dm:77777', 'a'*32)
        offer = SimpleNamespace(chat_id='dm:77777', text='合成公开阅读成果', current=lambda: True)
        consumed = []
        self.assertTrue(await port.send_activity(grant, offer, lambda: consumed.append(True), lambda: True))
        self.assertEqual(consumed, [True])
        self.assertFalse(await port.send_activity(grant, offer, lambda: consumed.append(True), lambda: True))
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0]['user_id'], 77777)

    async def test_friend_revocation_after_claim_prevents_contact_and_consumption(self):
        await self.adapter._refresh_friends(force=True)
        port = self.social.port
        self.social.policy.targets = frozenset({'dm:77777'})
        port.bind(self.social)
        grant = port.claim_idle('dm:77777', 'b'*32)
        self.adapter.settings.friend_dm.update((), ttl=0)
        consumed = []
        offer = SimpleNamespace(chat_id='dm:77777', text='不应发送', current=lambda: True)
        self.assertFalse(await port.send_activity(grant, offer, lambda: consumed.append(True), lambda: True))
        self.assertFalse(consumed)
        self.assertFalse(self.sends())

    async def test_missing_target_id_readback_is_bound_to_live_send_receipt(self):
        original = self.adapter.client.call
        async def call(action, payload):
            result = await original(action, payload)
            if action == 'get_msg':
                result.pop('target_id', None)
            return result
        self.adapter.client.call = call
        with patch.object(self.social, 'record_self', wraps=self.social.record_self) as record:
            await self.feed()
        self.assertEqual(len(self.sends()), 1)
        record.assert_called_once()
        mid = next(iter(self.echoes))
        self.assertTrue(self.adapter._private_receipt_matches('dm:77777', mid, self.reply_text))
        self.assertFalse(self.adapter._private_receipt_matches('dm:88888', mid, self.reply_text))
        self.assertEqual(self.sends()[0]['message'][0]['data']['id'], '1')

    async def test_unknown_bot_receipt_without_peer_proof_is_not_readable(self):
        self.echoes['80'] = dict(message_id='80', message_type='private', user_id='90001',
            sender={'user_id': '90001'}, message=[dict(type='text', data={'text': self.reply_text})])
        self.assertEqual(await self.social.port.read_dialogue_refs('dm:77777', ['80']), [])

    async def test_receipt_cannot_be_rebound_to_a_different_friend(self):
        await self.feed()
        mid = next(iter(self.echoes))
        self.echoes[mid].pop('target_id')
        self.echoes[mid]['sender'] = {'user_id': '90001'}
        self.assertEqual(await self.social.port.read_dialogue_refs('dm:88888', [mid]), [])
        rows = await self.social.port.read_dialogue_refs('dm:77777', [mid])
        self.assertEqual([row['message_id'] for row in rows], [mid])

    async def test_live_send_cannot_rebind_a_reused_ack_without_peer(self):
        await self.feed()
        mid = next(iter(self.echoes))
        self.echoes[mid].pop('target_id')
        original = self.adapter.client.call
        async def call(action, payload):
            if action == 'send_private_msg':
                self.actions.append((action, copy.deepcopy(payload)))
                return {'message_id': mid}
            return await original(action, payload)
        self.adapter.client.call = call
        with patch.object(self.social, 'record_self', wraps=self.social.record_self) as record:
            await self.feed(mid=2, user=88888)
        record.assert_not_called()
        self.assertTrue(self.adapter._private_receipt_matches('dm:77777', mid, self.reply_text))
        self.assertFalse(self.adapter._private_receipt_matches('dm:88888', mid, self.reply_text))

    async def test_fresh_adapter_cannot_trust_missing_peer_receipt_from_old_instance(self):
        from hermes_napcat.host_runtime import HostedNapCatAdapter
        from social_plugin_helpers import install_social
        await self.feed()
        mid = next(iter(self.echoes))
        self.echoes[mid].pop('target_id')
        self.echoes[mid]['sender'] = {'user_id': '90001'}
        cfg = base.social_config()
        cfg.extra['friend_dm'] = dict(enabled=True, admission='verified_friends')
        replacement = HostedNapCatAdapter(cfg, self.binding, self.bridge)
        replacement.client = self.adapter.client
        controller = install_social(replacement, llm=self.llm)
        self.addAsyncCleanup(replacement.disconnect)
        self.assertEqual(await controller.port.read_dialogue_refs('dm:77777', [mid]), [])

    async def test_conflicting_target_id_and_mismatched_reply_text_are_not_verified(self):
        original = self.adapter.client.call
        for change in ('peer', 'text'):
            async def call(action, payload):
                result = await original(action, payload)
                if action == 'get_msg':
                    if change == 'peer':
                        result['target_id'] = '88888'
                    else:
                        result['message'] = [dict(type='text', data={'text': 'different reply'})]
                return result
            self.adapter.client.call = call
            with patch.object(self.social, 'record_self', wraps=self.social.record_self) as record:
                await self.feed(mid=90 if change == 'peer' else 91)
            record.assert_not_called()
        self.assertFalse(self.adapter._private_receipts)

    async def test_direct_dialogue_hook_can_keep_a_friend_turn_silent(self):
        self.social.prepare_direct_dialogue = AsyncMock(return_value=False)
        await self.feed()
        self.social.prepare_direct_dialogue.assert_awaited_once()
        self.assertFalse(self.model_calls)
        self.assertFalse(self.sends())

    async def test_real_direct_classifier_and_reply_share_the_verified_dm(self):
        del self.social.prepare_direct_dialogue
        original = self.llm._async_caller
        selected = []
        async def provider(**kwargs):
            if 'public_participation_selection' in kwargs['messages'][-1]['content']:
                selected.append(copy.deepcopy(kwargs))
                result = json.dumps(dict(selected='c0', assessments=[dict(candidate='c0',
                    audience='bot', action='respond', topic='c0', reason='question', topic_action='continue')]))
                return 'offline', 'same-host-model', SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=result))], usage=None)
            return await original(**kwargs)
        self.llm._async_caller = provider
        await self.feed('你怎么看这个合成方案？')
        self.assertEqual(len(selected), 1)
        self.assertEqual(json.loads(selected[0]['messages'][-1]['content'])['chat_type'], 'dm')
        self.assertEqual(len(self.model_calls), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertIn('本轮已核验普通好友私聊资料', self.model_calls[0][0]['messages'][0]['content'])
        self.assertNotIn('tools', selected[0])

    def use_group_members(self, member=True):
        policy = self.adapter.settings.friend_dm
        policy.admission, policy.users, policy.allow_group_members = 'explicit', frozenset(), True
        self.member_present = member
        original = self.adapter.client.call
        async def call(action, payload):
            if action == 'get_group_member_info':
                self.actions.append((action, copy.deepcopy(payload)))
                if self.member_present:
                    return dict(group_id=payload['group_id'], user_id=payload['user_id'])
                return {}
            return await original(action, payload)
        self.adapter.client.call = call

    async def test_allowed_group_friend_uses_separate_membership_proof(self):
        self.use_group_members()
        await self.feed()
        queries = [payload for action, payload in self.actions if action == 'get_group_member_info']
        self.assertGreaterEqual(len(queries), 2)  # ingress and immediately before send
        self.assertTrue(all(str(query['group_id']) in self.adapter.settings.groups
                            and query['user_id'] == '77777' and query['no_cache'] is True for query in queries))
        self.assertEqual(len(self.sends()), 1)
        self.assertNotIn('get_group_member_info', json.dumps(self.model_calls, ensure_ascii=False))
        self.assertNotIn('77777', self.adapter.settings.users)

    async def test_group_membership_without_real_friend_or_with_forged_group_cannot_admit(self):
        self.use_group_members(member=False)
        await self.feed(group_id=20001)
        self.assertFalse(self.model_calls)
        self.assertFalse(self.sends())
        self.actions.clear()
        self.friend_rows = []
        self.member_present = True
        await self.feed(mid=2)
        self.assertFalse(self.model_calls)
        self.assertFalse(any(action == 'get_group_member_info' for action, _ in self.actions))

    async def test_group_departure_during_model_blocks_late_reply(self):
        self.use_group_members()
        self.block_model = True
        await self.adapter._receive(self.raw())
        await asyncio.wait_for(self.model_entered.wait(), 2)
        self.member_present = False
        self.model_release.set()
        await self.drain()
        self.assertFalse(self.sends())
        self.assertFalse(self.social.port.dm_allowed('dm:77777'))

    async def test_group_proof_cache_is_bounded_by_ttl_scope_and_disconnect(self):
        self.use_group_members()
        self.assertTrue(await self.adapter._verify_friend_dm('77777'))
        queries = len([1 for action, _ in self.actions if action == 'get_group_member_info'])
        self.assertTrue(await self.adapter._verify_friend_dm('77777'))
        self.assertEqual(queries, len([1 for action, _ in self.actions if action == 'get_group_member_info']))
        groups, _ = self.adapter.settings.friend_dm._memberships['77777']
        self.adapter.settings.friend_dm._memberships['77777'] = (groups, time.monotonic()-1)
        self.assertTrue(await self.adapter._verify_friend_dm('77777'))
        self.assertGreater(len([1 for action, _ in self.actions if action == 'get_group_member_info']), queries)
        self.adapter.settings.groups = frozenset()
        self.assertFalse(await self.adapter._verify_friend_dm('77777', force=True))
        self.adapter.settings.groups = frozenset({'20001'})
        self.assertTrue(await self.adapter._verify_friend_dm('77777', force=True))
        self.adapter._connection_lost()
        self.assertFalse(self.adapter.settings.friend_dm._memberships)

    async def test_disconnect_during_membership_lookup_cannot_publish_late_proof(self):
        self.use_group_members()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.adapter.client.call
        async def call(action, payload):
            if action == 'get_group_member_info':
                entered.set()
                await release.wait()
            return await original(action, payload)
        self.adapter.client.call = call
        pending = asyncio.create_task(self.adapter._receive(self.raw()))
        await asyncio.wait_for(entered.wait(), 2)
        self.adapter._connection_lost()
        release.set()
        await pending
        await self.drain()
        self.assertFalse(self.adapter.settings.friend_dm._memberships)
        self.assertFalse(self.model_calls)
        self.assertFalse(self.sends())

    async def test_expired_known_friend_refresh_failure_retains_backoff(self):
        self.assertTrue(await self.adapter._verify_friend_dm('77777', force=True))
        self.adapter.settings.friend_dm.expires = 0
        attempts = []
        async def failed(action, payload):
            attempts.append(action)
            raise TimeoutError('synthetic friend refresh failure')
        with patch.object(self.adapter.client, 'call', side_effect=failed):
            self.assertFalse(await self.adapter._verify_friend_dm('77777'))
            self.assertFalse(await self.adapter._verify_friend_dm('77777'))
        self.assertEqual(attempts, ['get_friend_list'])
        self.assertFalse(self.adapter.public_dm_allowed('dm:77777'))

    async def test_verified_private_bot_quote_exposes_only_author_evidence(self):
        await self.feed()
        mid = next(iter(self.echoes))
        self.echoes[mid]['sender'] = {'user_id': '90001'}
        self.echoes[mid].pop('target_id')
        await self.feed('这是我的明确短答', mid=2, message=[dict(type='reply', data={'id': mid}),
                        dict(type='text', data={'text': '这是我的明确短答'})])
        current_event = self.social.prepare_direct_dialogue.await_args.args[0]
        self.assertEqual(current_event.verified_reply_author_id, '90001')


if __name__ == '__main__':
    unittest.main()
