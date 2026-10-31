"""Synthetic host bridge; proves adapter boundaries, not a sandbox deployment."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_plugin import NapCatAdapter, Settings, config, event
from hermes_napcat.isolation import Admission, AdmissionDenied, IsolationBridge


def strict_config(**overrides):
    policy = dict(mode='strict', private_profile='default', social_profile='social',
                  domain_id='social-example', policy_revision=1, guest_dm_users=['10002'])
    policy.update(overrides)
    return config(isolation=policy)


class Bridge(IsolationBridge):
    async def admit(self, route):
        return Admission(route)

    async def dispatch(self, event_value, admission, reply):
        return None

    def __init__(self):
        self.admit = AsyncMock(side_effect=lambda route: Admission(route))
        self.dispatch = AsyncMock()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.adapter = NapCatAdapter(strict_config())
        self.policy = self.adapter.settings.isolation

    def route(self, **overrides):
        return self.policy.resolve(self.adapter.convert_event(event(**overrides)).source)

    def test_exact_dm_and_group_routes(self):
        dm = self.route(message_type='private')
        group = self.route(message_id=6)
        guest = self.route(message_type='private', user_id=10002,
                           sender={'user_id': 10002, 'nickname': 'owner'}, message_id=7)
        self.assertEqual((dm.domain_id, dm.profile), ('private', 'default'))
        self.assertEqual((group.domain_id, group.profile), ('social-example', 'social'))
        self.assertEqual((guest.domain_id, guest.profile), ('social-example', 'social'))
        self.assertNotEqual(dm.session_key, group.session_key)
        self.assertNotEqual(group.session_key, replace(group, user_id='10002').session_key)
        self.assertNotEqual(group.session_key, replace(group, policy_revision=2).session_key)
        with self.assertRaises(FrozenInstanceError):
            dm.profile = 'social'

    def test_unknown_dm_and_malformed_scope_are_denied(self):
        self.policy.guests = frozenset()
        with self.assertRaises(AdmissionDenied):
            self.route(message_type='private', user_id=10002, sender={'user_id': 10002})
        source = self.adapter.convert_event(event(message_id=8)).source
        for changes in ({'chat_type': 'dm'}, {'chat_id': 'group:99999'}, {'user_id': '99999'},
                        {'thread_id': 'untrusted'}, {'parent_chat_id': 'dm:10001'},
                        {'profile_route_rejected': True},
                        {'message_id': None}, {'platform': SimpleNamespace(value='cli')}):
            values = vars(source).copy()
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(AdmissionDenied):
                self.policy.resolve(SimpleNamespace(**values))

    def test_names_roles_and_broad_host_profile_do_not_authorize(self):
        source = self.adapter.convert_event(event(sender={'user_id': 10001, 'nickname': 'owner',
                    'card': 'default', 'role': 'owner'})).source
        source.profile = 'default'  # A bad broad owner route cannot change this policy.
        source.role_authorized = True
        self.assertEqual(self.policy.resolve(source).profile, 'social')

    def test_settings_reject_fake_attestation_and_ambiguous_runtimes(self):
        for changes in ({'mode': 'false'}, {'reviewed': True}, {'social_profile': 'default'},
                        {'private_profile': 'other'}, {'domain_id': 'private'},
                        {'domain_id': '../home'}, {'policy_revision': True},
                        {'policy_revision': 0}, {'guest_dm_users': ['10001']},
                        {'guest_dm_users': ['99999']}, {'social_profile': None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Settings(strict_config(**changes).extra)
        extra = strict_config().extra
        extra['require_mention'] = False
        with self.assertRaises(ValueError):
            Settings(extra)

    def test_bad_sender_does_not_poison_replay_window(self):
        self.assertIsNone(self.adapter.convert_event(event(sender={'user_id': 10002})))
        self.assertIsNotNone(self.adapter.convert_event(event()))


class StrictIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_bridge_cannot_connect_or_reach_legacy_handler(self):
        adapter = NapCatAdapter(strict_config())
        adapter.handle_message = AsyncMock()
        adapter._mark_disconnected = lambda: None
        with patch('hermes_napcat.adapter.OneBotClient') as client:
            self.assertFalse(await adapter.connect())
            client.assert_not_called()
        await adapter._receive(event())
        adapter.handle_message.assert_not_awaited()
        self.assertEqual(adapter.isolation_status()['admission']['denied'], 1)
        self.assertFalse((await adapter.send('group:20001', 'unsolicited')).success)

    async def test_bridge_denial_error_and_mismatched_permit_never_fall_back(self):
        for outcome in (AdmissionDenied('no_runtime'), RuntimeError('no_runtime'), None, 'approved'):
            bridge = Bridge()
            bridge.admit.side_effect = outcome if isinstance(outcome, Exception) else None
            bridge.admit.return_value = outcome
            adapter = NapCatAdapter(strict_config(), isolation_bridge=bridge)
            adapter.handle_message = AsyncMock()
            await adapter._receive(event())
            bridge.dispatch.assert_not_awaited()
            adapter.handle_message.assert_not_awaited()
            self.assertEqual(adapter.admission_counts['denied'], 1)
        bridge = Bridge()
        bridge.admit.side_effect = lambda route: Admission(replace(route, profile='default'))
        adapter = NapCatAdapter(strict_config(), isolation_bridge=bridge)
        await adapter._receive(event())
        bridge.dispatch.assert_not_awaited()

    async def test_reply_capability_is_bound_and_revoked_on_return(self):
        bridge = Bridge()
        adapter = NapCatAdapter(strict_config(), isolation_bridge=bridge)
        adapter.client = SimpleNamespace(call=AsyncMock(return_value={'message_id': 99}))
        saved = []
        async def dispatch(event_value, admission, reply):
            saved.append(reply)
            self.assertFalse((await adapter.send('dm:10001', 'cross-domain')).success)
            self.assertFalse((await adapter.send('group:20001', 'wrong quote', reply_to='999')).success)
            self.assertTrue((await reply('allowed')).success)
        bridge.dispatch.side_effect = dispatch
        await adapter._receive(event())
        self.assertEqual(adapter.client.call.await_count, 1)
        self.assertEqual(adapter.client.call.call_args.args, ('send_group_msg', {
            'group_id': 20001, 'message': [{'type': 'reply', 'data': {'id': '5'}},
                                         {'type': 'text', 'data': {'text': 'allowed'}}]}))
        self.assertFalse((await saved[0]('late reply')).success)
        self.assertEqual(adapter.client.call.await_count, 1)

    async def test_background_snapshot_cannot_reuse_revoked_permit(self):
        bridge = Bridge()
        adapter = NapCatAdapter(strict_config(), isolation_bridge=bridge)
        adapter.client = SimpleNamespace(call=AsyncMock(return_value={'message_id': 99}))
        gate = asyncio.Event()
        pending = []
        async def dispatch(event_value, admission, reply):
            async def delayed():
                await gate.wait()
                return await reply('late')
            pending.append(asyncio.create_task(delayed()))
        bridge.dispatch.side_effect = dispatch
        await adapter._receive(event())
        gate.set()
        self.assertFalse((await pending[0]).success)
        adapter.client.call.assert_not_awaited()

    async def test_dispatch_failure_never_calls_legacy_and_revokes_send(self):
        bridge = Bridge()
        bridge.dispatch.side_effect = RuntimeError('synthetic failure')
        adapter = NapCatAdapter(strict_config(), isolation_bridge=bridge)
        adapter.handle_message = AsyncMock()
        await adapter._receive(event())
        adapter.handle_message.assert_not_awaited()
        self.assertEqual(adapter.admission_counts['failed'], 1)
        self.assertFalse((await adapter.send('group:20001', 'late')).success)

    async def test_cancellation_revokes_inherited_reply_capability(self):
        bridge = Bridge()
        adapter = NapCatAdapter(strict_config(), isolation_bridge=bridge)
        adapter.client = SimpleNamespace(call=AsyncMock(return_value={'message_id': 99}))
        started, release = asyncio.Event(), asyncio.Event()
        pending = []
        async def dispatch(event_value, admission, reply):
            async def delayed():
                await release.wait()
                return await reply('cancelled')
            pending.append(asyncio.create_task(delayed()))
            started.set()
            await asyncio.Event().wait()
        bridge.dispatch.side_effect = dispatch
        task = asyncio.create_task(adapter._receive(event()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        self.assertFalse((await pending[0]).success)
        adapter.client.call.assert_not_awaited()

    async def test_parallel_scopes_keep_distinct_reply_targets(self):
        bridge = Bridge()
        adapter = NapCatAdapter(strict_config(), isolation_bridge=bridge)
        adapter.client = SimpleNamespace(call=AsyncMock(return_value={'message_id': 99}))
        gate = asyncio.Event()
        count = 0
        async def dispatch(event_value, admission, reply):
            nonlocal count
            count += 1
            if count == 2:
                gate.set()
            await gate.wait()
            await reply(admission.route.domain_id)
        bridge.dispatch.side_effect = dispatch
        await asyncio.gather(adapter._receive(event()), adapter._receive(event(message_type='private', message_id=7)))
        calls = adapter.client.call.call_args_list
        self.assertEqual({call.args[0] for call in calls}, {'send_private_msg', 'send_group_msg'})
        self.assertEqual(adapter.admission_counts['private'], 1)
        self.assertEqual(adapter.admission_counts['social'], 1)
