"""Transport capabilities tested independently of a social implementation."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_deployment as base
from hermes_napcat.isolation import AdmissionDenied
from hermes_napcat.participation import NullParticipation


class ChannelConsumer:
    """An explicit test consumer; it never calls an LLM or a real OneBot."""
    def __init__(self, port):
        self.port = port
        self.binding = SimpleNamespace(home=port.home)
        self.policy = SimpleNamespace(targets=frozenset({'group:20001'}))
        self.active = True
        self.paused = False

    def enabled_for(self, chat): return self.active
    def allows_unaddressed(self, chat): return self.active and not self.paused and chat == 'group:20001'
    def observes(self, chat): return self.active and chat == 'group:20001'
    def recognizes_control(self, chat, text): return False
    def observe(self, event): pass
    def record_self(self, *args): pass
    def recall(self, *args): pass
    def control(self, event): return None
    def queue_ambient(self, event): pass
    def processing_start(self, event): pass
    def processing_complete(self, event): pass
    def prompt_for_chat(self, chat): return 'Public test style'
    def start(self): pass
    def reset_connection(self): pass
    async def stop(self): self.active = False


class ParticipationPortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.HostFactoryTests.asyncSetUp(self)
        await self.adapter.disconnect()
        self.cfg.extra['runtime']['mode'] = 'social_agent'
        self.cfg.extra['runtime']['contract'] = 5
        self.cfg.extra['group_admission'] = 'all_members'
        with patch.dict('os.environ', {'NAPCAT_ACCESS_TOKEN': 'synthetic-onebot'}):
            self.adapter = self.registry.create_adapter('napcat', self.cfg)
        self.wire = []
        async def call(action, payload):
            if action == 'get_friend_list': return []
            self.wire.append((action, payload))
            if action == 'get_msg':
                return {'message_id': 901, 'message_type': 'group', 'user_id': 90001,
                        'group_id': 20001, 'message': [{'type': 'text', 'data': {'text': '测试发言'}}]}
            return {'message_id': 901}
        self.adapter.client = SimpleNamespace(call=call, close=AsyncMock(), ready=asyncio.Event())
        self.adapter.client.ready.set()
        self.port = self.adapter.participation_port()
        self.consumer = ChannelConsumer(self.port)
        self.adapter.install_participation(self.consumer)
        self.assertTrue(self.port.ready())

    async def asyncTearDown(self):
        await base.HostFactoryTests.asyncTearDown(self)

    def raw(self, mid=41, named=False):
        return base.event(message_id=mid, user_id=77777,
                          sender={'user_id': 77777, 'nickname': '测试群友'},
                          message=[base.text('测试实例甲测试' if named else '大家怎么看？')])

    async def test_idle_send_is_single_use_and_has_no_user_permit(self):
        grant = self.port.claim_idle('group:20001', 'a'*32)
        consumed = []
        self.assertTrue(await self.port.send_idle(grant, '测试发言', lambda: consumed.append(True), lambda: True))
        self.assertFalse(await self.port.send_idle(grant, '测试发言', lambda: consumed.append(True), lambda: True))
        self.assertEqual(consumed, [True])
        self.assertEqual([a for a, _ in self.wire], ['send_group_msg', 'get_msg'])
        self.assertEqual(self.port.last_receipt('group:20001'), '901')
        self.assertFalse(self.adapter._group_permits)
        self.assertFalse(self.adapter._native_permits)

    async def test_real_arrival_revokes_idle_before_dispatch(self):
        grant = self.port.claim_idle('group:20001', 'b'*32)
        await self.adapter._receive(self.raw())
        self.assertFalse(await self.port.send_idle(grant, '测试发言', lambda: self.fail('must not consume'), lambda: True))
        self.assertFalse(self.wire)

    async def test_pause_and_detach_revoke_idle_without_sending(self):
        for detach in (False, True):
            grant = self.port.claim_idle('group:20001', 'c'*32)
            if detach:
                self.port.detach()
            else:
                self.port.invalidate_optional('group:20001')
            self.assertFalse(await self.port.send_idle(grant, '测试发言', lambda: self.fail('must not consume'), lambda: True))
        self.assertIsInstance(self.adapter.participation, NullParticipation)
        self.assertFalse(self.wire)

    async def test_disconnect_revokes_idle_and_future_claims(self):
        grant = self.port.claim_idle('group:20001', 'd'*32)
        self.adapter.client.ready.clear()
        self.adapter._connection_lost()
        self.assertFalse(await self.port.send_idle(grant, '测试发言', lambda: None, lambda: True))
        with self.assertRaises(AdmissionDenied):
            self.port.claim_idle('group:20001', 'e'*32)
        self.assertFalse(self.wire)

    async def test_wrong_target_and_fabricated_delivery_are_denied(self):
        for target in ('group:29999', 'dm:10002', '20001'):
            with self.assertRaises(AdmissionDenied):
                self.port.claim_idle(target, 'f'*32)
        with self.assertRaises(AdmissionDenied):
            await self.adapter._send_initiative_text(object(), '测试发言', self.port)
        grant = self.port.claim_idle('group:20001', '1'*32)
        # Merely possessing an unconsumed handle is not the delivery seal.
        with self.assertRaises(AdmissionDenied):
            await self.adapter._send_initiative_text(grant, '测试发言', self.port)
        self.assertFalse(self.wire)

    async def test_duplicate_install_is_idempotent_and_foreign_home_denied(self):
        revision = self.adapter._optional_sources.revision('group:20001')
        self.assertIs(self.adapter.install_participation(self.consumer), self.consumer)
        self.assertEqual(revision, self.adapter._optional_sources.revision('group:20001'))
        foreign = ChannelConsumer(self.adapter.participation_port())
        foreign.binding.home = Path(self.owner)/'different-profile'
        with self.assertRaises(AdmissionDenied): self.adapter.install_participation(foreign)
        self.assertIs(self.adapter.participation, self.consumer)

    async def test_late_old_cleanup_cannot_detach_or_revoke_replacement(self):
        old = self.port
        old.detach()
        self.port = self.adapter.participation_port()
        self.consumer = ChannelConsumer(self.port)
        self.adapter.install_participation(self.consumer)
        grant = self.port.claim_idle('group:20001', '2'*32)
        old.invalidate_optional()
        old.detach()
        self.assertTrue(await self.port.send_idle(grant, '测试发言', lambda: None, lambda: True))
        with self.assertRaises(AdmissionDenied): old.claim_idle('group:20001', '3'*32)

    async def test_optional_marker_without_exact_source_ledger_is_not_authority(self):
        inbound = self.adapter.convert_event(self.raw())
        inbound.source._napcat_ambient_candidate = True
        self.assertFalse(self.adapter._optional_sources.valid(inbound.source))
        self.port.issue_optional(inbound)
        self.assertTrue(self.adapter._optional_sources.valid(inbound.source))
        clone = self.adapter.build_source(chat_id=inbound.source.chat_id, chat_type='group',
                                          user_id=inbound.user_id, user_name='相同名字',
                                          message_id=inbound.message_id)
        clone._napcat_ambient_candidate = True
        self.assertFalse(self.adapter._optional_sources.valid(clone))
        self.port.invalidate_optional('group:20001')
        self.assertFalse(self.adapter._optional_sources.valid(inbound.source))

    async def test_pause_does_not_revoke_an_ordinary_named_turn(self):
        inbound = self.adapter.convert_event(self.raw(named=True))
        await base.issue_group_ingress(self.adapter, inbound)
        await self.adapter.on_processing_start(inbound)
        try:
            self.assertTrue(self.adapter.admit_group_source(inbound.source))
            self.consumer.paused = True
            self.port.invalidate_optional('group:20001')
            self.assertTrue(self.adapter.admit_group_source(inbound.source))
            self.port.detach()
            self.assertTrue(self.adapter.admit_group_source(inbound.source))
        finally:
            await self.adapter.on_processing_complete(inbound, None)

    async def test_consume_failure_cannot_replay_and_never_sends(self):
        grant = self.port.claim_idle('group:20001', '4'*32)
        def fail(): raise OSError('synthetic state failure')
        with self.assertRaises(OSError): await self.port.send_idle(grant, '测试发言', fail, lambda: True)
        self.assertFalse(await self.port.send_idle(grant, '测试发言', lambda: None, lambda: True))
        self.assertFalse(self.wire)

    async def test_unverified_receipt_stays_consumed_without_recording_success(self):
        self.adapter.client.call = AsyncMock(return_value={'message_id': 901})
        grant = self.port.claim_idle('group:20001', '5'*32)
        consumed = []
        self.assertFalse(await self.port.send_idle(grant, '测试发言', lambda: consumed.append(True), lambda: True))
        self.assertFalse(await self.port.send_idle(grant, '测试发言', lambda: consumed.append(True), lambda: True))
        self.assertEqual(consumed, [True])
        self.assertIsNone(self.port.last_receipt('group:20001'))
        self.assertEqual(self.adapter.client.call.await_count, 2)

    async def test_storage_fault_still_routes_explicit_status_but_not_ambient(self):
        from social_plugin_helpers import install_social
        self.port.detach()
        controller = install_social(self.adapter, llm=self.ctx.llm)
        queued = []
        self.adapter._queue_dispatch = queued.append
        controller.healthy = False
        try:
            raw = self.raw(mid=81)
            raw['message'] = [base.text('/social status')]
            await self.adapter._receive(raw)
            self.assertEqual(len(queued), 1)
            self.assertTrue(queued[0].reply_expected)
            self.assertIn('本地状态存储不可用', queued[0].text)
            self.assertFalse(controller.ambient_tasks)
            self.assertIsNotNone(self.adapter.convert_event(self.raw(mid=82, named=True)))
        finally:
            await controller.stop()
            controller.binding.close()
            await asyncio.sleep(0)
            if controller.binding.shutdown_task:
                await controller.binding.shutdown_task

    async def test_detach_only_clears_its_own_sdk_factory_key(self):
        self.port.set_registration_key(('test-social', 'register.attach'))
        self.adapter._plugin_handlers_wired = {('test-social', 'register.attach'), ('unrelated', 'native.handler')}
        self.port.detach()
        self.assertEqual(self.adapter._plugin_handlers_wired, {('unrelated', 'native.handler')})
        self.assertIsNone(self.adapter.convert_event(self.raw()))
        self.assertIsNotNone(self.adapter.convert_event(self.raw(mid=42, named=True)))
        self.assertFalse(hasattr(self.adapter.settings, 'proactive'))


if __name__ == '__main__': unittest.main()
