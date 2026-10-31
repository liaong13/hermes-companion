# pyright: reportMissingImports=false
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import support
from support import FakeAdapter, FakeContext, FakePort, build, event, policy, ROOT
from hermes_social import create_controller, register, SocialController
from hermes_social.binding import SocialBinding
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


class LifecycleStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.token = set_hermes_home_override(self.home)
        self.ctx, self.port = FakeContext(), FakePort(self.home)
        self.social = build(self.ctx, self.port)

    async def asyncTearDown(self):
        self.ctx.release.set()
        await self.social.stop()
        self.ctx.unload()
        await asyncio.sleep(0)
        if self.social.binding.shutdown_task:
            await self.social.binding.shutdown_task
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    async def test_factory_registers_namespace_and_single_controller_on_reconnect(self):
        # An arbitrary native object must not be inspected or retained.
        class Native:
            def __getattribute__(self, name):
                raise AssertionError('native must be ignored')
        register(self.ctx)
        adapter = FakeAdapter(self.port)
        factory = self.ctx.handlers['napcat']
        first = factory(Native(), adapter)
        second = factory(Native(), adapter)
        self.assertIs(first, self.social)
        self.assertIs(second, first)
        self.assertEqual(len({id(item) for item in adapter.installed}), 1)
        self.assertEqual(len(self.ctx.tasks), 1)
        self.assertIn('hermes-social:social-participation', self.ctx.skills)
        self.assertEqual(self.port.registration_key, ('hermes-social', factory.__qualname__))
        first.reset_connection()
        self.assertIs(factory(None, adapter), first)
        self.assertEqual(len(self.ctx.tasks), 1)

    async def test_old_channel_port_is_rejected_before_starting_background_tasks(self):
        port = FakePort(self.home)
        port.public_context_contract = 0
        with self.assertRaisesRegex(ValueError, 'participation port contract'):
            create_controller(FakeContext(), port, policy())
        port = FakePort(self.home)
        port.wait_visual_context = None
        with self.assertRaisesRegex(ValueError, 'participation port contract'):
            create_controller(FakeContext(), port, policy())

    async def test_background_error_logs_location_without_private_exception_text(self):
        async def broken():
            raise AttributeError('PRIVATE_EXCEPTION_TEXT_CANARY')
        with self.assertLogs('hermes_social.controller', level='WARNING') as captured:
            task = self.social._spawn(broken(), 'social:ambient:group:20001')
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        output = '\n'.join(captured.output)
        self.assertIn('AttributeError', output)
        self.assertIn('test_lifecycle_state.py:', output)
        self.assertNotIn('PRIVATE_EXCEPTION_TEXT_CANARY', output)
        self.assertNotIn('group:20001', output)

    async def test_platform_state_namespaces_do_not_overwrite_each_other(self):
        other_ctx, other_port = FakeContext(), FakePort(self.home)
        other_port.platform = 'other-channel'
        other = build(other_ctx, other_port)
        try:
            self.social.states['group:20001'].paused = True
            self.assertTrue(self.social._persist())
            self.assertTrue(other._persist())
            self.assertNotEqual(self.social.path, other.path)
            self.assertEqual(json.loads(self.social.path.read_text())['chats']['group:20001']['paused'], True)
            self.assertEqual(json.loads(other.path.read_text())['chats']['group:20001']['paused'], False)
        finally:
            await other.stop()
            other_ctx.unload()
            await asyncio.sleep(0)
            if other.binding.shutdown_task:
                await other.binding.shutdown_task

    async def test_storage_fault_does_not_hide_explicit_local_status(self):
        self.social.healthy = False
        self.assertFalse(self.social.observes('group:20001'))
        self.assertTrue(self.social.recognizes_control('group:20001', '/social status'))
        self.assertFalse(self.social.recognizes_control('group:29999', '/social status'))
        self.assertIn('本地状态存储不可用', self.social.control(event('/social status')))
        self.assertFalse(self.port.attempts)

    async def test_unloaded_cached_factory_cannot_reattach(self):
        register(self.ctx)
        adapter = FakeAdapter(self.port)
        factory = self.ctx.handlers['napcat']
        factory(None, adapter)
        self.ctx.unload()
        self.assertFalse(self.social.active)
        self.assertTrue(self.port.detached)
        self.assertIsNone(factory(None, adapter))
        self.assertEqual(len(adapter.installed), 1)

    async def test_stop_join_and_reconnect_restart_are_idempotent(self):
        self.social.start()
        task = self.social.task
        self.social.start()
        self.assertIs(self.social.task, task)
        await self.social.stop()
        self.assertFalse(self.social.active)
        self.assertTrue(task.done())
        self.assertFalse(self.social._tasks)
        self.social.start()
        self.assertTrue(self.social.active)
        self.assertIsNot(self.social.task, task)
        self.assertFalse(self.social.task.done())
        await self.social.stop()
        await self.social.stop()

    async def test_thread_unload_immediately_deactivates_then_joins_original_loop(self):
        self.social.start()
        self.social.policy.ambient_debounce_seconds = 30
        self.social.queue_ambient(event('待处理候选'))
        original = list(self.ctx.tasks)
        await asyncio.to_thread(self.ctx.unload)
        self.assertFalse(self.social.active)
        self.assertFalse(self.social.binding.active)
        self.assertTrue(self.port.detached)
        for _ in range(20):
            if self.social.binding.shutdown_task is not None:
                break
            await asyncio.sleep(0)
        assert self.social.binding.shutdown_task is not None
        await self.social.binding.shutdown_task
        self.assertTrue(all(task.done() for task in original))
        self.assertFalse(self.social.ambient_tasks)
        self.assertFalse(self.port.dispatched)

    async def test_cancellation_resistant_provider_cannot_send_after_unload(self):
        self.ctx.delayed = self.ctx.resist_cancel = True
        self.social.states['group:20001'].last_activity = time.time() - 4000
        task = asyncio.create_task(self.social.tick('group:20001'))
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        self.ctx.unload()
        self.assertFalse(self.social.active)
        await asyncio.sleep(0)
        self.ctx.release.set()
        await asyncio.gather(task, return_exceptions=True)
        if self.social.binding.shutdown_task:
            await self.social.binding.shutdown_task
        self.assertFalse(self.port.attempts)
        self.assertFalse(self.social._tasks)

    async def test_cross_profile_creation_and_existing_controller_calls_denied(self):
        other = self.home / 'other-profile'
        token = set_hermes_home_override(other)
        try:
            with self.assertRaises(ValueError):
                create_controller(self.ctx, self.port, policy())
            with self.assertRaises(ValueError):
                create_controller(FakeContext(), FakePort(self.home), policy())
            self.assertFalse(self.social.enabled_for('group:20001'))
            with self.assertRaises(ValueError):
                self.social.context('group:20001')
            self.assertFalse(await self.social.tick('group:20001'))
        finally:
            reset_hermes_home_override(token)
        self.assertTrue(self.social.enabled_for('group:20001'))

    async def test_same_port_cannot_attach_to_two_active_contexts(self):
        self.assertIs(create_controller(self.ctx, self.port, policy()), self.social)
        with self.assertRaises(ValueError):
            create_controller(FakeContext(), self.port, policy())

    async def test_disabled_controller_neither_observes_dispatches_nor_models(self):
        disabled_port = FakePort(self.home)
        disabled = build(self.ctx, disabled_port, {'enabled': False})
        disabled.start()
        self.assertFalse(disabled.observes('group:20001'))
        self.assertFalse(disabled.allows_unaddressed('group:20001'))
        self.assertFalse(disabled.recognizes_control('group:20001', '/social pause'))
        self.assertEqual(disabled.prompt_for_chat('group:20001'), '')
        disabled.observe(event())
        disabled.queue_ambient(event())
        self.assertFalse(await disabled.tick('group:20001'))
        self.assertFalse(disabled_port.dispatched)
        self.assertFalse(self.ctx.calls)
        self.assertIsNone(disabled.task)
        await disabled.stop()

    async def test_state_contains_counts_and_pause_not_messages_names_or_keys(self):
        self.social.observe(event('PUBLIC_MESSAGE_CANARY', name='DISPLAY_CANARY'))
        self.social.observe(event('DM_MESSAGE_CANARY', chat='dm:10001'))
        self.social.control(event('/social pause'))
        self.assertEqual(self.social.path, self.home / 'plugin-data/hermes-social/napcat/social-state.json')
        data = self.social.path.read_text()
        for canary in ['MESSAGE_CANARY', 'DISPLAY_CANARY', 'auth', 'token', 'rows']:
            self.assertNotIn(canary, data)
        self.assertEqual(self.social.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.social.path.parent.stat().st_mode & 0o777, 0o700)
        replacement = SocialController(self.port, self.social.policy, self.social.binding)
        self.assertTrue(replacement.states['group:20001'].paused)
        self.assertEqual(replacement.context('group:20001'), [])

    async def test_legacy_state_is_not_implicitly_read_or_migrated(self):
        legacy = self.home / 'napcat/social-state.json'
        legacy.parent.mkdir()
        legacy.write_text('{broken old deployment state')
        replacement = SocialController(self.port, self.social.policy, self.social.binding)
        self.assertTrue(replacement.healthy)
        self.assertFalse(replacement.states['group:20001'].paused)
        self.assertEqual(legacy.read_text(), '{broken old deployment state')

    async def test_corrupt_insecure_and_symlink_state_disable_only_optional_controller(self):
        path = self.social.path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for content, mode in [('{broken', 0o600), ('{}', 0o644),
                              ('{"schema_version":1,"chats":{"group:20001":{"last_check":NaN}}}', 0o600)]:
            path.write_text(content)
            path.chmod(mode)
            replacement = SocialController(self.port, self.social.policy, self.social.binding)
            self.assertFalse(replacement.healthy)
            self.assertFalse(replacement.allows_unaddressed('group:20001'))
        path.unlink()
        path.symlink_to(self.home / 'absent-target')
        replacement = SocialController(self.port, self.social.policy, self.social.binding)
        self.assertFalse(replacement.healthy)
        self.assertFalse((self.home / 'absent-target').exists())

    async def test_storage_failure_revokes_optional_and_prevents_any_send(self):
        self.social.policy.ambient_debounce_seconds = 0
        self.social.queue_ambient(event())
        with patch.object(self.social.store, 'save', side_effect=OSError('synthetic full disk')):
            self.social.observe(event(mid='2'))
        self.assertFalse(self.social.healthy)
        await asyncio.gather(*self.social.ambient_tasks.values())
        self.assertFalse(self.port.dispatched)
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertIn('存储不可用', self.social.control(event('/social status')))

    async def test_failed_consumption_prevents_port_side_effect(self):
        self.social.states['group:20001'].last_activity = time.time() - 4000
        original = self.social.store.save
        calls = []
        def fail_second(states):
            calls.append(1)
            if len(calls) == 2:
                raise OSError('synthetic full disk')
            return original(states)
        with patch.object(self.social.store, 'save', side_effect=fail_second):
            with self.assertRaises(ValueError):
                await self.social.tick('group:20001')
        self.assertFalse(self.port.attempts)
        self.assertFalse(self.social.healthy)

    async def test_real_sdk_manifest_context_registration_factory_and_unload(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        manifest = parse_manifest_file(ROOT / 'plugin.yaml', ROOT, 'user', '')
        assert manifest is not None
        self.assertEqual((manifest.kind, manifest.license), ('standalone', 'MIT'))
        self.assertRegex(manifest.version, r'^\d+\.\d+\.\d+$')
        manager = PluginManager(scope_key=str(self.home))
        context = PluginContext(manifest, manager)
        config = {'plugins': {'entries': {'hermes-social': {'settings': {'channels': {'napcat': policy()}}}}}}
        adapter = FakeAdapter(FakePort(self.home))
        with patch('hermes_cli.plugins.load_config_readonly', return_value=config):
            register(context)
            factory, owner = manager.get_platform_handler_factories('napcat')[0]
            self.assertEqual(owner, manifest.name)
            controller = factory(None, adapter)
        self.assertIn('hermes-social:social-participation', manager._plugin_skills)
        self.assertTrue(controller.policy.enabled)
        self.assertEqual(controller.port.registration_key, (owner, factory.__qualname__))
        self.assertIs(controller.binding.ctx, context)
        # Exercise the actual SDK task supervisor and ownership cleanup.
        await asyncio.sleep(0)
        self.assertTrue(manager.unload('hermes-social'))
        self.assertFalse(controller.active)
        self.assertTrue(adapter.port.detached)
        self.assertNotIn('hermes-social:social-participation', manager._plugin_skills)
        self.assertIsNone(factory(None, adapter))
        await asyncio.sleep(0)
        if controller.binding.shutdown_task:
            await controller.binding.shutdown_task
        self.assertFalse(controller._tasks)
