"""Public data proofs through real receive/admission, using authored OneBot input."""
import asyncio
import copy
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_deployment as base
from test_participation_port import ChannelConsumer
from hermes_napcat.isolation import AdmissionDenied


class Observer(ChannelConsumer):
    def __init__(self, port):
        super().__init__(port)
        self.observed, self.optional, self.proofs_at_observation = [], [], []

    def observes(self, chat):
        return self.active and chat in {'group:20001', 'dm:77777', 'dm:88888'}

    def observe(self, event):
        self.observed.append(event)
        self.proofs_at_observation.append(self.port.public_observation_current(event))

    def queue_ambient(self, event):
        self.optional.append(event)


class PublicObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.HostFactoryTests.asyncSetUp(self)
        await self.adapter.disconnect()
        self.cfg.extra['runtime'].update(mode='social_agent', contract=5)
        self.cfg.extra['group_admission'] = 'all_members'
        self.cfg.extra['friend_dm'] = dict(enabled=True, admission='explicit', users=['77777', '88888'])
        with patch.dict('os.environ', {'NAPCAT_ACCESS_TOKEN': 'synthetic-onebot'}):
            self.adapter = self.registry.create_adapter('napcat', self.cfg)
        self.friends = ['77777', '88888']
        self.actions = []
        async def call(action, payload):
            self.actions.append((action, copy.deepcopy(payload)))
            if action == 'get_friend_list':
                return [dict(user_id=user, nickname='合成好友') for user in self.friends]
            raise AssertionError('unexpected synthetic OneBot action '+action)
        self.adapter.client = SimpleNamespace(call=call, close=AsyncMock(), ready=asyncio.Event())
        self.adapter.client.ready.set()
        self.port = self.adapter.participation_port()
        self.observer = Observer(self.port)
        self.adapter.install_participation(self.observer)
        self.native, self.private = [], []
        self.adapter._queue_dispatch = self.native.append
        self.adapter._queue_friend_dm = lambda event, raw: self.private.append(event)

    async def asyncTearDown(self):
        await base.HostFactoryTests.asyncTearDown(self)

    def raw(self, mid=51, user=77777, *, private=False, named=False, **overrides):
        raw = base.event(message_id=mid, user_id=user,
                         message_type='private' if private else 'group',
                         sender=dict(user_id=user, nickname='合成成员'),
                         message=[base.text('测试实例甲，核对合成材料' if named else '合成材料甲的准备情况？')])
        if private:
            raw.pop('group_id', None)
        raw.update(overrides)
        return raw

    async def native_admit(self, event, attachments=()):
        async def accepted(inbound):
            inbound._gateway_accepted = True
        route = self.adapter.settings.isolation.resolve(event.source)
        with patch.object(self.adapter, 'handle_message', side_effect=accepted):
            await self.adapter._gateway_group_turn(event, route, list(attachments))

    async def test_ambient_candidate_and_actual_event_have_data_before_policy_without_send_authority(self):
        await self.adapter._receive(self.raw())
        candidate, actual = self.observer.observed[-1], self.observer.optional[-1]
        self.assertIsNot(candidate, actual)
        self.assertEqual(self.observer.proofs_at_observation, [True])
        self.assertFalse(actual.reply_expected)
        self.assertTrue(self.port.public_observation_current(candidate))
        self.assertTrue(self.port.public_observation_current(actual))
        self.assertFalse(self.port.public_ingress_current(candidate))
        self.assertFalse(self.port.public_ingress_current(actual))
        self.assertFalse(self.adapter.admit_group_source(actual.source))
        with self.assertRaises(AdmissionDenied):
            self.port.dispatch_optional(actual)
        with self.assertRaises(AdmissionDenied):
            self.adapter.tool_authority_for_source(actual.source)
        self.assertFalse(self.actions)

    async def test_two_members_in_same_batch_keep_both_original_observation_roots(self):
        await self.adapter._receive(self.raw(51, 77777))
        first, first_wrapper = self.observer.observed[-1], self.observer.optional[-1]
        original = self.adapter._public_observations.events[id(first)].root
        await self.adapter._receive(self.raw(52, 88888))
        second = self.observer.observed[-1]
        self.assertEqual(self.observer.proofs_at_observation, [True, True])
        self.assertTrue(all(self.port.public_observation_current(event)
                            for event in (first, first_wrapper, second)))
        self.assertIs(self.adapter._public_observations.events[id(first_wrapper)].root, original)
        self.assertLessEqual(original.deadline-time.monotonic(), 300)

    async def test_observation_only_group_keeps_data_while_ambient_is_paused(self):
        self.observer.paused = True
        await self.adapter._receive(self.raw())
        event = self.observer.observed[-1]
        self.assertTrue(self.port.public_observation_current(event))
        self.assertFalse(self.observer.optional)
        self.assertFalse(self.native)
        self.assertFalse(self.adapter._group_ingress)

    async def test_current_group_whitelist_removal_revokes_data_despite_old_controller_policy(self):
        await self.adapter._receive(self.raw())
        event = self.observer.observed[-1]
        self.assertTrue(self.port.public_observation_current(event))
        self.assertTrue(self.observer.observes('group:20001'))
        self.adapter.settings.groups = frozenset()
        self.assertIn('20001', self.adapter.settings.isolation.groups)
        self.assertTrue(self.observer.observes('group:20001'))
        self.assertFalse(self.port.public_observation_current(event))
        self.assertFalse(self.adapter._remember_public_observation(event))

    async def test_native_and_media_handoff_bind_exact_wrapper_without_renewing_initial_deadline(self):
        await self.adapter._receive(self.raw(named=True))
        candidate, actual = self.observer.observed[-1], self.native[-1]
        original = self.adapter._public_observations.events[id(candidate)].root
        self.assertFalse(self.port.public_observation_current(actual))
        await self.native_admit(actual, [{'type': 'text', 'text': '合成媒体资料'}])
        self.assertTrue(self.port.public_observation_current(actual))
        self.assertTrue(self.port.public_ingress_current(actual))
        self.assertIs(self.adapter._public_observations.events[id(actual)].root, original)
        self.assertFalse(self.port.public_observation_current(copy.copy(actual)))

    async def test_optional_actual_native_wrapper_does_not_expand_formal_ingress_capability(self):
        await self.adapter._receive(self.raw())
        actual = self.observer.optional[-1]
        self.port.issue_optional(actual)
        await self.native_admit(actual)
        self.assertTrue(self.port.public_observation_current(actual))
        self.assertFalse(self.port.public_ingress_current(actual))
        self.assertTrue(self.adapter.admit_group_source(actual.source))

    async def test_cloned_events_sources_and_route_or_actor_mutations_are_rejected(self):
        for index, mutation in enumerate(('source', 'chat', 'actor', 'event_actor', 'event_mid', 'deleted_source')):
            await self.adapter._receive(self.raw(60+index))
            event = self.observer.optional[-1]
            self.assertFalse(self.port.public_observation_current(copy.copy(event)))
            source = event.source
            if mutation == 'source':
                event.source = copy.copy(source)
            elif mutation == 'chat':
                source.chat_id = 'group:29999'
            elif mutation == 'actor':
                source.user_id = '88888'
            elif mutation == 'event_actor':
                event.user_id = '88888'
            elif mutation == 'event_mid':
                event.message_id = '999'
            else:
                del event.source
            self.assertFalse(self.port.public_observation_current(event), mutation)
            if mutation == 'source':
                event.source = source
                self.assertFalse(self.port.public_observation_current(event))

    async def test_observe_activity_cannot_register_an_authored_unreceived_event(self):
        event = self.adapter.convert_event(self.raw())
        self.port.observe_activity(event)
        self.observer.observe(event)
        self.assertFalse(self.port.public_observation_current(event))
        self.assertFalse(self.port.public_ingress_current(event))
        self.assertFalse(self.adapter._public_observations.roots)

    async def test_expired_original_cannot_be_remarked_or_renewed_by_native_wrapper(self):
        await self.adapter._receive(self.raw(named=True))
        candidate, actual = self.observer.observed[-1], self.native[-1]
        root = self.adapter._public_observations.events[id(candidate)].root
        with patch('hermes_napcat.participation.time.monotonic', return_value=root.deadline+1):
            self.assertFalse(self.port.public_observation_current(candidate))
            self.assertFalse(self.adapter._remember_public_observation(candidate))
            await self.native_admit(actual)
            self.assertFalse(self.port.public_observation_current(actual))
            self.assertEqual(self.adapter._public_observations.roots[('group:20001', '51')].deadline,
                             root.deadline)
        self.assertFalse(self.adapter._public_observations.events)

    async def test_recall_group_qualification_detach_and_disconnect_revoke_exact_data(self):
        await self.adapter._receive(self.raw(71))
        recalled = self.observer.observed[-1]
        await self.adapter._receive(dict(post_type='notice', notice_type='group_recall', self_id=90001,
                                         group_id=20001, user_id=77777, message_id=71))
        self.assertFalse(self.port.public_observation_current(recalled))
        await self.adapter._receive(self.raw(72))
        withdrawn = self.observer.observed[-1]
        original_groups = self.adapter.settings.isolation.groups
        self.adapter.settings.isolation.groups = frozenset()
        self.assertFalse(self.port.public_observation_current(withdrawn))
        self.adapter.settings.isolation.groups = original_groups
        await self.adapter._receive(self.raw(73))
        disconnected = self.observer.observed[-1]
        self.adapter._connection_lost()
        self.assertFalse(self.port.public_observation_current(disconnected))
        self.assertFalse(self.adapter._public_observations.events)
        self.assertFalse(self.adapter._public_observations.roots)
        await self.adapter._receive(self.raw(74))
        detached = self.observer.observed[-1]
        self.port.detach()
        self.assertFalse(self.port.public_observation_current(detached))
        self.assertFalse(self.adapter._public_observations.events)

    async def test_verified_friend_dm_is_data_only_and_loses_proof_on_friend_withdrawal(self):
        await self.adapter._receive(self.raw(private=True))
        event = self.private[-1]
        self.assertEqual(self.observer.proofs_at_observation, [True])
        self.assertTrue(self.port.public_observation_current(event))
        self.assertFalse(self.port.public_ingress_current(event))
        self.assertFalse(self.adapter._native_permits)
        self.assertEqual([action for action, _ in self.actions], ['get_friend_list'])
        self.adapter.settings.friend_dm.update(())
        self.assertFalse(self.port.public_observation_current(event))
        self.adapter.settings.friend_dm.update({'77777', '88888'})
        self.assertFalse(self.adapter._remember_public_observation(event))

    async def test_friend_memory_control_and_unauthenticated_input_never_enter_observation_ram(self):
        with patch.object(self.adapter, '_friend_memory_control', return_value=True):
            await self.adapter._receive(self.raw(private=True))
        self.assertFalse(self.observer.observed)
        self.assertFalse(self.adapter._public_observations.events)
        self.assertFalse(self.adapter._public_observations.roots)
        for raw in (self.raw(81, self_id=99999), self.raw(82, sender={'user_id': 88888}),
                    self.raw(83, group_id=29999), self.raw(84, 99999, private=True)):
            await self.adapter._receive(raw)
        self.assertFalse(self.observer.observed)
        self.assertFalse(self.adapter._public_observations.events)

    async def test_observation_object_capacity_evicts_old_but_keeps_new_data(self):
        await self.adapter._receive(self.raw(1000))
        oldest = self.observer.observed[-1]
        for mid in range(1001, 1260):
            await self.adapter._receive(self.raw(mid))
        self.assertLessEqual(len(self.adapter._public_observations.events), 512)
        self.assertLessEqual(len(self.adapter._public_observations.roots), 4096)
        self.assertFalse(self.port.public_observation_current(oldest))
        self.assertTrue(self.port.public_observation_current(self.observer.observed[-1]))


if __name__ == '__main__':
    unittest.main()
