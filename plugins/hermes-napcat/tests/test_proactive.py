"""Opt-in social paths: real adapter/source types, offline model/OneBot fixtures."""
import asyncio
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_plugin import config, event
from hermes_napcat.adapter import NapCatAdapter, Settings
from hermes_napcat.host_runtime import HostedNapCatAdapter, HostIsolationBridge
from hermes_napcat.isolation import AdmissionDenied
from social_plugin_helpers import install_social, new_controller, raw_policy
from agent.plugin_llm import PluginLlm, _TrustPolicy
from gateway.platforms.base import SendResult


def social_config():
    cfg = config(allowed_groups=['20001', '20002'], group_admission='all_members',
                 group_trigger={'mode': 'name_or_mention', 'name': '公开名字'},
                 isolation={'mode': 'strict', 'private_profile': 'default', 'social_profile': 'social',
                            'domain_id': 'social-example', 'policy_revision': 1, 'guest_dm_users': []},
                 runtime={'mode': 'social_agent', 'contract': 5, 'social_home': '/unused-test-home'})
    return cfg


def social_adapter(cfg=None, policy=None):
    from hermes_constants import get_hermes_home
    binding = SimpleNamespace(home=Path(get_hermes_home()).resolve(), active=True,
                              validate=lambda: None, validate_active=lambda: None)
    cfg = social_config() if cfg is None else cfg
    bridge = HostIsolationBridge(binding, SimpleNamespace(request=AsyncMock()),
                                 gateway_tools=cfg.extra['runtime']['contract'] == 5)
    adapter = HostedNapCatAdapter(cfg, binding, bridge)
    install_social(adapter, policy)
    return adapter


class PolicyTests(unittest.TestCase):
    def test_only_explicit_group_opens_unaddressed_and_peek_does_not_consume(self):
        adapter = social_adapter()
        raw = event(message=[{'type': 'text', 'data': {'text': '接着聊刚才那个话题'}}], user_id=77777,
                    sender={'user_id': 77777, 'card': '公开昵称'})
        peek = adapter.convert_event(raw, observe_only=True)
        self.assertIsNotNone(peek)
        self.assertFalse(peek.reply_expected)
        actual = adapter.convert_event(raw)
        self.assertEqual(actual.source.to_dict(), peek.source.to_dict())
        self.assertFalse(actual.internal)
        self.assertIsNone(adapter.convert_event(raw))
        raw['group_id'], raw['message_id'] = 20002, 6
        self.assertIsNone(adapter.convert_event(raw))
        raw['message'] = [{'type': 'at', 'data': {'qq': '90001'}}, {'type': 'text', 'data': {'text': '你好'}}]
        self.assertTrue(adapter.convert_event(raw).reply_expected)

    def test_opt_in_does_not_open_unknown_venues_dms_self_or_forged_sender(self):
        for changes in ({'group_id': 99999}, {'sender': {'user_id': 88888}}, {'user_id': 90001},
                        {'self_id': 88888}, {'post_type': 'message_sent'},
                        {'message_type': 'private', 'user_id': 77777}, {'sender': {'nickname': 'fake'}}):
            adapter = social_adapter()
            with self.subTest(changes=changes):
                self.assertIsNone(adapter.convert_event(event(message=[{'type': 'text', 'data': {'text': 'hello'}}], **changes)))

    def test_invalid_policies_and_non_contract5_are_rejected(self):
        invalid = [{'targets': ['group:99999']}, {'targets': ['dm:10002']}, {'targets': ['group:020001']},
                   {'enabled': 'true'}, {'quiet_hours': [24, 9]}, {'daily_limit': True},
                   {'participation_groups': ['99999']}, {'ambient_cooldown_seconds': 0}, {'unknown': True}]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises((ValueError, TypeError)):
                social_adapter(policy=raw_policy(**overrides))
        cfg = social_config()
        cfg.extra['runtime']['contract'] = 4
        with self.assertRaises(AdmissionDenied):
            social_adapter(cfg)

    def test_quiet_hours_use_configured_timezone(self):
        policy = social_adapter(policy=raw_policy(quiet_hours=[23, 9])).participation.policy
        zone = ZoneInfo('Asia/Shanghai')
        self.assertTrue(policy.quiet(datetime(2026, 10, 29, 23, tzinfo=zone).timestamp()))
        self.assertTrue(policy.quiet(datetime(2026, 10, 29, 8, 59, tzinfo=zone).timestamp()))
        self.assertFalse(policy.quiet(datetime(2026, 10, 29, 9, tzinfo=zone).timestamp()))


class SocialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.home = Path(self.temp.name)
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        scope = set_hermes_home_override(self.home)
        self.addCleanup(reset_hermes_home_override, scope)
        self.calls, self.sent, self.prepares = [], [], []
        self.response = {'speak': True, 'text': '最近有什么让你觉得挺有意思的小事？'}
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.delay = False
        async def provider(**kw):
            from gateway.session_context import get_session_env
            self.calls.append((kw, get_session_env('HERMES_SESSION_CHAT_ID'), get_session_env('HERMES_SESSION_ID')))
            self.entered.set()
            if self.delay:
                await self.release.wait()
            return ('offline', 'same-host-main', SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(self.response, ensure_ascii=False)))], usage=None))
        llm = PluginLlm(plugin_id='hermes-social', policy_loader=lambda p: _TrustPolicy(plugin_id=p), async_caller=provider)
        self.llm = llm
        self.binding = SimpleNamespace(home=self.home, active=True, validate=lambda: None,
                                       validate_active=lambda: None, facade=lambda: llm)
        async def request(payload):
            self.prepares.append(payload)
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            return {'status': 'ok', 'host_contract': 4, 'digest': digest}
        self.worker = SimpleNamespace(request=AsyncMock(side_effect=request))
        self.bridge = HostIsolationBridge(self.binding, self.worker, gateway_tools=True)
        self.adapter = HostedNapCatAdapter(social_config(), self.binding, self.bridge)
        ready = asyncio.Event()
        ready.set()
        self.echo = None
        self.fail_send = False
        async def call(action, payload):
            self.sent.append((action, payload))
            if action.startswith('send_'):
                if self.fail_send:
                    raise TimeoutError('synthetic receipt loss')
                kind = 'group' if action == 'send_group_msg' else 'private'
                self.echo = dict(message_id=901, user_id=90001, message_type=kind,
                                 group_id=payload.get('group_id'), target_id=payload.get('user_id'), message=payload['message'])
                return {'message_id': 901}
            if action == 'get_msg':
                return self.echo
            if action == 'get_friend_list':
                return []
        self.adapter.client = SimpleNamespace(call=call, ready=ready, close=AsyncMock())
        self.social = install_social(self.adapter, llm=self.llm)
        for state in self.social.states.values():
            state.last_activity = time.time()-4000

    async def asyncTearDown(self):
        self.release.set()
        await self.adapter.disconnect()
        self.temp.cleanup()

    def parsed(self, text, mid=5, private=False):
        return self.adapter.convert_event(event(message_id=mid, message_type='private' if private else 'group',
                    message=[{'type': 'text', 'data': {'text': text}}]))

    async def test_actual_facade_model_to_single_target_send_and_readback(self):
        self.assertTrue(await self.social.tick('group:20001'))
        self.assertEqual([x[0] for x in self.sent], ['send_group_msg', 'get_msg'])
        self.assertEqual(self.sent[0][1]['group_id'], 20001)
        self.assertEqual(self.social.counters['sent'], 1)
        self.assertNotIn('route', self.prepares[0])
        self.assertNotIn('user_id', self.prepares[0]['automation'])
        kw = self.calls[0][0]
        self.assertNotIn('tools', kw)
        self.assertNotIn('provider', kw)
        self.assertNotIn('model', kw)
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((await self.adapter.send('group:20001', 'not a user reply')).success)

    async def test_private_target_has_no_group_worker_and_no_cross_chat_history(self):
        self.social.observe(self.parsed('PRIVATE_CONVERSATION_CANARY', private=True))
        self.social.states['dm:10001'].last_activity = time.time()-4000
        self.assertTrue(await self.social.tick('group:20001'))
        self.assertNotIn('PRIVATE_CONVERSATION_CANARY', json.dumps(self.calls[-1][0]))
        self.assertTrue(await self.social.tick('dm:10001'))
        self.assertEqual([x[0] for x in self.sent], ['send_group_msg', 'get_msg', 'send_private_msg', 'get_msg'])
        self.assertEqual(len(self.prepares), 1)
        self.assertIn('PRIVATE_CONVERSATION_CANARY', json.dumps(self.calls[-1][0]))

    async def test_silence_has_no_send_and_check_interval_is_bounded(self):
        self.response = {'speak': False, 'text': ''}
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.sent)

    async def test_invalid_response_cannot_select_target_or_create_side_effect(self):
        self.response['target'] = 'group:20002'
        with self.assertRaises(ValueError):
            await self.social.tick('group:20001')
        self.assertFalse(self.sent)

    async def test_new_human_input_during_inference_revokes_initiative(self):
        self.delay = True
        task = asyncio.create_task(self.social.tick('group:20001'))
        await self.entered.wait()
        self.social.observe(self.parsed('新的人类消息', mid=7))
        self.release.set()
        self.assertFalse(await task)
        self.assertFalse(self.sent)
        self.assertEqual(self.social.counters['stale'], 1)

    async def test_disconnect_during_inference_revokes_initiative(self):
        self.delay = True
        task = asyncio.create_task(self.social.tick('group:20001'))
        await self.entered.wait()
        self.adapter._connection_lost()
        self.release.set()
        self.assertFalse(await task)
        self.assertFalse(self.sent)

    async def test_no_unapproved_target_busy_paused_or_quiet_send(self):
        self.assertFalse(self.social.due('group:20002'))
        self.social.busy['group:20001'] = 1
        self.assertFalse(self.social.due('group:20001'))
        self.social.busy.clear()
        self.social.states['group:20001'].paused = True
        self.assertFalse(self.social.due('group:20001'))
        self.social.states['group:20001'].paused = False
        with patch.object(self.social.policy, 'quiet', return_value=True):
            self.assertFalse(self.social.due('group:20001'))
        with self.assertRaises(AdmissionDenied):
            self.social.port.claim_idle('group:20002', 'a'*32)
        self.assertFalse(self.sent)

    async def test_ambiguous_send_is_consumed_persistently_and_never_retried(self):
        self.fail_send = True
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual([x[0] for x in self.sent], ['send_group_msg'])
        replacement_adapter = HostedNapCatAdapter(social_config(), self.binding, self.bridge)
        replacement_adapter.client = self.adapter.client
        self.addAsyncCleanup(replacement_adapter.disconnect)
        replacement = install_social(replacement_adapter, llm=self.llm)
        replacement.states['group:20001'].last_activity = time.time()-4000
        self.assertFalse(replacement.due('group:20001'))
        self.assertEqual(replacement.states['group:20001'].daily, 1)
        self.assertTrue(replacement.states['group:20001'].unanswered)

    async def test_state_stores_no_messages_or_display_names(self):
        self.social.observe(self.parsed('PUBLIC_CONVERSATION_CANARY'))
        self.social.observe(self.parsed('PRIVATE_CONVERSATION_CANARY', private=True))
        data = self.social.path.read_text()
        self.assertNotIn('CONVERSATION_CANARY', data)
        self.assertNotIn('群名片', data)
        self.assertEqual(self.social.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.social.path.parent.stat().st_mode & 0o777, 0o700)

    async def test_recall_removes_only_current_chat_context(self):
        self.social.observe(self.parsed('群内容', mid=7))
        self.social.observe(self.parsed('私聊内容', mid=7, private=True))
        self.social.recall('group:20001', '7')
        self.assertEqual(self.social.context('group:20001'), [])
        self.assertIn('私聊内容', json.dumps(self.social.context('dm:10001'), ensure_ascii=False))

    async def test_pause_and_owner_only_resume_are_exact_current_chat_controls(self):
        pause = self.parsed('/napcat-social pause')
        self.assertIn('暂停', self.social.control(pause))
        self.assertTrue(self.social.states['group:20001'].paused)
        other = self.parsed('/napcat-social resume', mid=6)
        other.user_id = '77777'
        self.assertIn('只有', self.social.control(other))
        self.assertTrue(self.social.states['group:20001'].paused)
        self.assertIn('启用', self.social.control(self.parsed('/napcat-social resume', mid=7)))
        self.assertFalse(self.social.states['dm:10001'].paused)

    async def test_real_receive_ambient_is_coalesced_not_private_handler(self):
        self.adapter._queue_dispatch = lambda item: self.dispatched.append(item)
        self.dispatched = []
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0
        messages = {
            10: '周末露营天气预报说会下雨。',
            11: '露营地那边可能一整天都有雨。',
            12: '大家觉得周末露营下雨要怎么安排？',
        }
        for mid, text in messages.items():
            await self.adapter._receive(event(message_id=mid,
                message=[{'type': 'text', 'data': {'text': text}}]))
        await asyncio.gather(*self.social.ambient_tasks.values())
        self.assertEqual(len(self.dispatched), 1)
        actual = self.dispatched[0]
        self.assertEqual(actual.message_id, '12')
        self.assertFalse(actual.reply_expected)
        self.assertFalse(actual.internal)
        self.assertEqual(actual.text, messages[12])
        self.assertIn(messages[10], self.social.prepare_public_context(actual).text)
        self.assertIn('NO_REPLY', self.social.prompt_for_chat('group:20001'))
        self.assertFalse(actual.allow_gateway_control)
        self.assertEqual(actual.user_id, '10001')
        self.assertEqual(actual.source.message_id, '12')

    async def test_disconnected_pending_ambient_cannot_dispatch(self):
        self.dispatched = []
        self.adapter._queue_dispatch = self.dispatched.append
        self.social.policy.ambient_debounce_seconds = 0
        value = self.parsed('未点名', mid=8)
        self.social.queue_ambient(value)
        self.adapter._connection_lost()
        await asyncio.sleep(0)
        self.assertFalse(self.dispatched)

    async def test_pause_does_not_block_original_user_reply_permit(self):
        self.social.states['group:20001'].paused = True
        value = self.parsed('公开名字你好', mid=8)
        self.assertTrue(value.reply_expected)
        self.social.observe(value)
        self.assertTrue(self.social.states['group:20001'].paused)
        self.assertFalse(self.social.due('group:20001'))

    async def test_failed_optional_storage_pauses_automation_not_addressed_chat(self):
        with patch.object(self.social.store, 'save', side_effect=OSError('synthetic full disk')):
            await self.adapter._receive(event(message_id=19,
                message=[{'type': 'text', 'data': {'text': '没有点名，不该未经节流直接交给模型'}}]))
        self.assertFalse(self.social.healthy)
        self.assertFalse(self.social.ambient_tasks)
        self.assertFalse(self.calls)
        self.assertFalse(self.sent)
        self.assertTrue(self.parsed('公开名字正常回复', mid=20).reply_expected)
        status = self.adapter.convert_event(event(message_id=21, message=[
            {'type': 'at', 'data': {'qq': '90001'}},
            {'type': 'text', 'data': {'text': '/napcat-social status'}}]))
        self.assertIsNotNone(status)
        self.assertIn('存储不可用', self.social.control(status))

    async def test_corrupt_state_disables_only_optional_controller(self):
        self.social.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.social.path.write_text('{invalid json')
        self.social.path.chmod(0o600)
        replacement_adapter = HostedNapCatAdapter(social_config(), self.binding, self.bridge)
        replacement_adapter.client = self.adapter.client
        self.addAsyncCleanup(replacement_adapter.disconnect)
        replacement = install_social(replacement_adapter, llm=self.llm)
        self.assertFalse(replacement.healthy)
        self.assertFalse(replacement.due('group:20001'))
