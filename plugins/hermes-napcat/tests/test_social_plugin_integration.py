"""SDK registration/port regressions, without QQ or external model calls.

The live verifier separately keeps the full worker/FIFO/codec/AIAgent pipeline.
These tests use real SDK registries and transport proof, not mocked admission.
"""
import asyncio
import copy
import unittest
from unittest.mock import Mock, patch

import test_plugin
import test_proactive as base
import test_proactive_lifecycle as life
from hermes_napcat.host_runtime import HostedNapCatAdapter, HostBinding, HostIsolationBridge
from hermes_napcat.isolation import AdmissionDenied
from gateway import platform_registry
from gateway.platform_registry import PlatformRegistry
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from social_plugin_helpers import plugin_context, raw_policy, social_plugin, optional_valid, new_controller


class PluginIntegration(unittest.IsolatedAsyncioTestCase):
    parsed = life.Lifecycle.parsed
    issue_ingress = life.Lifecycle.issue_ingress
    start_group = life.Lifecycle.start_group
    drain_ambient = life.Lifecycle.drain_ambient

    async def asyncSetUp(self):
        await base.SocialTests.asyncSetUp(self)
        # A transport starts without a social controller, even when strict.
        await self.social.stop()
        client = self.adapter.client
        self.dispatched = []
        self.manager = PluginManager(scope_key=str(self.home))
        self.registry = PlatformRegistry()
        self.settings = {'plugins': {'entries': {'hermes-social': {'settings': {
            'channels': {'napcat': raw_policy(shared=True)}}}}}}
        self.manager_patch = patch('hermes_cli.plugins.get_plugin_manager', return_value=self.manager)
        self.config_patch = patch('hermes_cli.plugins.load_config_readonly', return_value=self.settings)
        self.registry_patch = patch.object(platform_registry, 'platform_registry', self.registry)
        for patcher in (self.manager_patch, self.config_patch, self.registry_patch):
            patcher.start()
            self.addCleanup(patcher.stop)
        napcat_ctx = PluginContext(PluginManifest(name='hermes-napcat', kind='platform',
                                  source='user', path=str(test_plugin.ROOT)), self.manager)
        napcat_ctx._llm = self.llm
        test_plugin.plugin.register(napcat_ctx)
        self.binding = HostBinding(napcat_ctx)
        self.bridge = HostIsolationBridge(self.binding, self.worker, gateway_tools=True)
        self.adapter = HostedNapCatAdapter(base.social_config(), self.binding, self.bridge)
        self.adapter.client = client
        self.adapter._queue_dispatch = self.dispatched.append

    async def asyncTearDown(self):
        self.manager.unload()
        await base.SocialTests.asyncTearDown(self)

    def register_social(self):
        ctx = plugin_context(manager=self.manager, llm=self.llm)
        social_plugin().register(ctx)
        return ctx

    def wire(self):
        self.adapter._wire_plugin_handlers(None)
        self.social = self.adapter.participation
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0
        return self.social

    async def test_missing_plugin_keeps_addressed_and_owner_dm_but_no_optional_participation(self):
        self.adapter._wire_plugin_handlers(None)
        self.assertFalse(self.adapter.participation.active)
        self.assertFalse(self.adapter.participation.allows_unaddressed('group:20001'))
        self.assertIsNone(self.parsed('没有点名', mid=401))
        inbound = self.parsed('公开名字正常点名', mid=402)
        self.assertTrue(inbound.reply_expected)
        await self.start_group(inbound)
        try:
            self.assertEqual(self.adapter.conversation_prompt_for_source(inbound.source), '')
            self.assertIsNone(self.adapter.tool_authority_for_source(inbound.source)(
                'web_search', {'query': '正常工具面'}))
        finally:
            await self.adapter.on_processing_complete(inbound, 'completed')
        owner = self.parsed('主人原有私聊', mid=403, private=True)
        self.assertIsNotNone(owner)
        self.assertTrue(owner.reply_expected)
        self.assertIsNone(self.adapter.convert_event(life.raw_text('陌生私聊', 404, private=True, user=77777)))
        self.assertFalse(self.calls)
        self.assertFalse(self.sent)

    async def test_plugin_loaded_after_transport_wires_real_sdk_factory_and_shared_allowlist(self):
        self.adapter._wire_plugin_handlers(None)
        self.assertFalse(self.adapter.participation.active)
        self.register_social()
        self.assertEqual(len(self.manager.get_platform_handler_factories('napcat')), 1)
        self.adapter.rewire_plugin_handlers()
        self.social = self.adapter.participation
        self.assertTrue(self.social.active)
        self.assertEqual(self.social.policy.participation_groups, {'20001', '20002'})
        self.assertNotIn('proactive', self.adapter.config.extra)
        self.assertFalse(hasattr(self.adapter.settings, 'proactive'))
        self.assertEqual(self.adapter.participation_port().groups, frozenset({'20001', '20002'}))
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨要怎么安排？', 405, group=20002, user=77777))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), 1)
        incoming = self.dispatched[0]
        await self.start_group(incoming)
        try:
            self.assertTrue(optional_valid(self.adapter, incoming.source))
            self.assertEqual(incoming.source.user_id, '77777')
            self.assertTrue(self.adapter.conversation_prompt_for_source(incoming.source))
            with self.assertRaises(AdmissionDenied):
                self.adapter.tool_authority_for_source(copy.copy(incoming.source))
        finally:
            await self.adapter.on_processing_complete(incoming, 'completed')

    async def test_repeated_wiring_and_native_rebuild_do_not_duplicate_controller_or_dispatch(self):
        self.register_social()
        module = social_plugin()
        factory = module.create_controller
        created = []
        def track_controller(*args, **kwargs):
            controller = factory(*args, **kwargs)
            created.append(controller)
            return controller
        # Observe the real constructor results; do not replace source/port proof.
        with patch.object(module, 'create_controller', side_effect=track_controller):
            original = self.wire()
            for _ in range(3):
                self.adapter._wire_plugin_handlers(None)
                self.adapter.rewire_plugin_handlers()
            # Simulate SDK native-client rebuilding, not a real OneBot connection.
            self.adapter._wire_plugin_handlers(object())
        self.assertIs(self.adapter.participation, original)
        self.assertTrue(created)
        self.assertTrue(all(controller is original for controller in created),
                        'rewiring created a second social controller before transport rejection')
        self.assertLessEqual(len([task for task in [original.task] if task and not task.done()]), 1)
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨要怎么安排？', 406, user=77777))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.social.counters['ambient'], 1)

    async def test_unload_revokes_exact_optional_source_but_preserves_addressed_permit(self):
        self.register_social()
        self.wire()
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨要怎么安排？', 407, user=77777))
        await self.drain_ambient()
        optional = self.dispatched[0]
        await self.issue_ingress(optional)
        addressed = self.parsed('公开名字正常点名', mid=408)
        authorize = await self.start_group(addressed)
        try:
            self.assertTrue(self.manager.unload('hermes-social'))
            # Close must synchronously revoke; yielding for cancellation is not authorization.
            self.assertFalse(self.social.active)
            self.assertFalse(optional_valid(self.adapter, optional.source))
            self.assertFalse(self.adapter.admit_group_source(optional.source))
            self.assertTrue(self.adapter.admit_group_source(addressed.source))
            self.assertIsNone(authorize('web_search', {'query': '正常点名不被卸载牵连'}))
            self.adapter._check_reply_target('group:20001', addressed.message_id)
            await asyncio.sleep(0)
            self.assertFalse(self.social.ambient_tasks)
        finally:
            await self.adapter.on_processing_complete(addressed, 'completed')
        self.assertIsNone(self.parsed('卸载后不再主动参与', mid=409))

    async def test_unload_invalidates_idle_grant_before_persistent_consume_or_send(self):
        self.register_social()
        self.wire()
        port = self.social.port
        grant = port.claim_idle('group:20001', 'c'*32)
        self.manager.unload('hermes-social')
        consume = Mock()
        try:
            sent = await port.send_idle(grant, '卸载后不能迟到发言', consume, lambda: True)
        except AdmissionDenied:
            sent = False
        self.assertFalse(sent)
        consume.assert_not_called()
        self.assertFalse(self.sent)

    async def test_disconnect_reconnect_keeps_plugin_but_rejects_old_optional_source(self):
        self.register_social()
        original = self.wire()
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨要怎么安排？', 410, user=77777))
        await self.drain_ambient()
        old = self.dispatched[0]
        self.adapter._connection_lost()
        self.assertFalse(optional_valid(self.adapter, old.source))
        self.adapter._wire_plugin_handlers(None)
        self.assertIs(self.adapter.participation, original)
        self.dispatched.clear()
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨时怎么安排？', 411, user=88888))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), 1)
        incoming = self.dispatched[0]
        await self.start_group(incoming)
        try:
            self.assertTrue(self.adapter.admit_group_source(incoming.source))
            self.assertTrue(optional_valid(self.adapter, incoming.source))
            self.assertFalse(self.adapter.admit_group_source(old.source))
        finally:
            await self.adapter.on_processing_complete(incoming, 'completed')


    async def test_disabled_registered_policy_keeps_normal_chat_without_optional_tasks(self):
        self.settings['plugins']['entries']['hermes-social']['settings']['channels']['napcat']['enabled'] = False
        self.register_social()
        self.wire()
        self.assertFalse(self.social.policy.enabled)
        self.assertFalse(self.social.allows_unaddressed('group:20002'))
        self.assertIsNone(self.parsed('不该自动接话', mid=417))
        self.assertIsNone(self.social.task)
        addressed = self.parsed('公开名字正常点名', mid=418)
        await self.start_group(addressed)
        try:
            self.assertTrue(self.adapter.admit_group_source(addressed.source))
            self.assertEqual(self.adapter.conversation_prompt_for_source(addressed.source), '')
        finally:
            await self.adapter.on_processing_complete(addressed, 'completed')

    async def test_new_input_and_disconnect_invalidate_idle_handle_before_consumption(self):
        self.register_social()
        self.wire()
        port = self.social.port
        for mid, reason in ((419, 'new_input'), (420, 'disconnect')):
            with self.subTest(reason=reason):
                grant = port.claim_idle('group:20001', 'd'*32)
                if reason == 'new_input':
                    await self.adapter._receive(life.raw_text('真实新输入', mid, user=77777))
                else:
                    self.adapter._connection_lost()
                consume = Mock()
                sent = await port.send_idle(grant, '旧空闲句柄不能复用', consume, lambda: True)
                self.assertFalse(sent)
                consume.assert_not_called()
        self.assertFalse(self.sent)

    async def test_two_groups_and_owner_dm_keep_recent_context_scoped(self):
        self.register_social()
        self.wire()
        for text, mid, group, user, private in (
                ('GROUP_ONE_MEMBER_A', 412, 20001, 77777, False),
                ('GROUP_ONE_MEMBER_B', 413, 20001, 88888, False),
                ('GROUP_TWO_ONLY', 414, 20002, 77777, False),
                ('PRIVATE_OWNER_ONLY', 415, 20001, 10001, True)):
            incoming = self.adapter.convert_event(life.raw_text(
                text, mid, group=group, user=user, private=private), observe_only=True)
            self.assertIsNotNone(incoming)
            self.social.observe(incoming)
        one = str(self.social.context('group:20001'))
        two = str(self.social.context('group:20002'))
        owner = str(self.social.context('dm:10001'))
        self.assertIn('GROUP_ONE_MEMBER_A', one)
        self.assertIn('GROUP_ONE_MEMBER_B', one)
        self.assertNotIn('GROUP_TWO_ONLY', one)
        self.assertNotIn('PRIVATE_OWNER_ONLY', one + two)
        self.assertNotIn('GROUP_ONE_MEMBER', two + owner)
        self.assertIn('GROUP_TWO_ONLY', two)
        self.assertIn('PRIVATE_OWNER_ONLY', owner)

    async def test_other_profile_cannot_read_context_or_install_on_this_port(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        self.register_social()
        self.wire()
        self.social.observe(self.parsed('PROFILE_A_ONLY', mid=416))
        other = self.home / 'unrelated-profile'
        other.mkdir(mode=0o700)
        token = set_hermes_home_override(other)
        try:
            self.assertFalse(self.social.allows_unaddressed('group:20001'))
            with self.assertRaises(ValueError):
                self.social.context('group:20001')
            with self.assertRaises(ValueError):
                new_controller(self.adapter, llm=self.llm)
        finally:
            reset_hermes_home_override(token)
        self.assertIn('PROFILE_A_ONLY', str(self.social.context('group:20001')))


if __name__ == '__main__':
    unittest.main()
