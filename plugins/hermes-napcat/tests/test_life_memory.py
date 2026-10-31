"""Original QQ ingress to the independent person service; real namespace probe.

OneBot is captured and no model or real QQ connection is used. The gateway
handoff is captured so prompt data and send-time revocation can be inspected.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_proactive as base
from hermes_napcat.isolation import AdmissionDenied
from hermes_napcat.runtime_bridge import SandboxedWorker

ROOT = Path(__file__).resolve().parents[2]/'hermes-life-memory'
spec = importlib.util.spec_from_file_location('life_memory_test_plugin', ROOT/'__init__.py',
                                             submodule_search_locations=[str(ROOT)])
life = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = life
spec.loader.exec_module(life)


@unittest.skipUnless(shutil.which('bwrap'), 'requires bubblewrap for the actual worker probe')
class PersonIngress(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.SocialTests.asyncSetUp(self)
        self.worker_temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        worker_home = Path(self.worker_temp.name)
        (worker_home/'scoped-memory.json').write_text(json.dumps({'schema_version': 2, 'mode': 'local-only',
            'domain_id': 'social-example', 'policy_revision': 1, 'host_contract': 4}))
        self.bridge.worker = SandboxedWorker(worker_home, domain_id='social-example', policy_revision=1)
        self.config = dict(enabled=True, instance_id='test-companion', domain_id='people-test',
                           policy_revision=1, groups=['20001'])
        self.callbacks, self.factories = [], []
        self.context = SimpleNamespace(manifest=SimpleNamespace(name='hermes-life-memory'),
            get_config=lambda key, default=None: self.config,
            on_unload=self.callbacks.append,
            register_platform_handler=lambda platform, factory: self.factories.append(factory))
        life.register(self.context)
        self.service = self.factories[0](None, self.adapter)
        self.mid = 100

    async def asyncTearDown(self):
        for callback in reversed(self.callbacks): callback()
        await base.SocialTests.asyncTearDown(self)
        self.worker_temp.cleanup()

    def raw(self, text, *, mid=None, user=77777, group=20001):
        self.mid += 1
        return base.event(message_id=self.mid if mid is None else mid, user_id=user, group_id=group,
            sender={'user_id':user, 'card':'同名群友', 'nickname':'同名群友'},
            message=[{'type':'text','data':{'text':text}}])

    async def command(self, text, **kwargs):
        before = len(self.sent)
        await self.adapter._receive(self.raw(text, **kwargs))
        tasks = list(self.adapter._control_tasks)
        if tasks: await asyncio.wait_for(asyncio.gather(*tasks), 8)
        replies = [p['message'] for a,p in self.sent[before:] if a == 'send_group_msg']
        return ''.join(segment['data']['text'] for reply in replies for segment in reply
                       if segment['type'] == 'text')

    async def remember(self, value='拿铁', *, user=77777):
        response = await self.command('/social-memory remember interest '+value, user=user)
        match = re.search(r'review ([a-f0-9]{32})', response)
        self.assertIsNotNone(match, response)
        ident = match[1]
        review = await self.command('/social-memory review '+ident, user=user)
        confirm = review.split('确认：')[1].split('\n')[0]
        return ident, confirm

    async def gateway_input(self, text='公开名字，我喜欢什么？', *, user=77777, process=False, observe=False):
        inbound = self.adapter.convert_event(self.raw(text, user=user))
        self.assertIsNotNone(inbound)
        if observe:
            self.social.observe(inbound)
        route = self.adapter.settings.isolation.resolve(inbound.source)
        async def accepted(event):
            event._gateway_accepted = True
        with patch.object(self.adapter, 'handle_message', side_effect=accepted):
            await self.adapter._gateway_group_turn(inbound, route, [])
        await self.adapter.on_processing_start(inbound)
        inbound.test_person_prompt = self.adapter.conversation_prompt_for_source(inbound.source)
        if not process: await self.adapter.on_processing_complete(inbound, None)
        return inbound

    async def test_original_command_confirmation_context_and_forget_without_model(self):
        ident, confirmation = await self.remember()
        before = await self.gateway_input()
        self.assertNotIn('拿铁', before.test_person_prompt)
        self.assertIn('已确认', await self.command(confirmation))
        inbound = await self.gateway_input(process=True)
        self.assertIn('兴趣：拿铁', inbound.test_person_prompt)
        self.assertNotIn('拿铁', inbound.text)  # Durable user text stays original.
        self.assertNotIn('napcat:77777', inbound.test_person_prompt)
        from gateway.run_turn_runner import TurnRunner
        turn = SimpleNamespace(_ctx=SimpleNamespace(source=inbound.source, context_prompt='PRIVATE_CANARY'),
            _runner=SimpleNamespace(_delivery_adapter_for=lambda source: self.adapter))
        prompt = TurnRunner._combined_ephemeral_prompt(turn)
        self.assertIn('兴趣：拿铁', prompt)
        self.assertNotIn('PRIVATE_CANARY', prompt)
        self.adapter._check_reply_target(inbound.source.chat_id, inbound.message_id)
        self.assertIn('已忘记', await self.command('/social-memory forget '+ident))
        with self.assertRaises(AdmissionDenied):
            self.adapter._check_reply_target(inbound.source.chat_id, inbound.message_id)
        self.assertFalse(self.adapter.admit_group_source(inbound.source))
        await self.adapter.on_processing_complete(inbound, None)
        self.assertNotIn('拿铁', (await self.gateway_input()).test_person_prompt)
        with self.assertRaises(AdmissionDenied):
            TurnRunner._combined_ephemeral_prompt(turn)
        self.assertFalse(self.calls)  # Commands never enter a model loop.
        self.assertFalse(self.social.context('group:20001'))  # Nor a fake social conversation.

    async def test_same_nickname_other_member_group_self_and_model_cannot_confirm(self):
        ident, confirmation = await self.remember()
        self.assertIn('无法处理', await self.command(confirmation, user=88888))
        self.assertNotIn('兴趣：拿铁', (await self.gateway_input(user=88888)).test_person_prompt)
        self.assertEqual(await self.command(confirmation, group=20002), '')
        self.assertEqual(await self.command(confirmation, user=90001), '')
        # A real outbound bot reply containing a command never runs ingress.
        inbound = await self.gateway_input(process=True)
        sent = await self.adapter.send('group:20001', confirmation, reply_to=inbound.message_id)
        self.assertTrue(sent.success)
        await self.adapter.on_processing_complete(inbound, None)
        self.assertEqual(len(self.service.people.list(self.service.scope(
            self.adapter.settings.isolation.resolve(self.adapter.convert_event(self.raw('公开名字，普通消息')).source)), pending=True)), 1)
        await self.command(confirmation)
        self.assertIn('兴趣：拿铁', (await self.gateway_input()).test_person_prompt)

    async def test_revision_reject_duplicate_and_live_disable(self):
        ident, confirmation = await self.remember()
        self.assertIn('已拒绝', await self.command(confirmation.replace(' confirm ', ' reject ')))
        self.assertIn('无法处理', await self.command(confirmation))
        ident, confirmation = await self.remember('绿茶')
        await self.command(confirmation)
        inbound = await self.gateway_input(process=True)
        authorize = self.adapter.tool_authority_for_source(inbound.source)
        self.assertIsNone(authorize('web_search', {'query':'公开资料'}))
        self.config['enabled'] = False
        self.assertIsNotNone(authorize('web_search', {'query':'迟到查询'}))
        self.config['enabled'] = True
        self.assertIsNotNone(authorize('web_search', {'query':'旧许可不能复活'}))
        self.assertFalse(self.service.active)
        with self.assertRaises(AdmissionDenied): self.adapter._check_reply_target(inbound.source.chat_id)
        await self.adapter.on_processing_complete(inbound, None)
        self.assertNotIn('兴趣：绿茶', (await self.gateway_input()).test_person_prompt)

    async def test_routed_profile_keeps_exact_authority_and_forget_still_revokes_it(self):
        from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
        ident, confirmation = await self.remember()
        await self.command(confirmation)
        inbound = await self.gateway_input(process=True)
        authorize = self.adapter.tool_authority_for_source(inbound.source)
        token = set_hermes_home_override(Path(self.worker_temp.name))
        try:
            self.assertIsNone(authorize('web_search', {'query':'公开资料'}))
            self.assertTrue(self.adapter.admit_group_source(inbound.source))
            self.assertEqual(Path(get_hermes_home()), Path(self.worker_temp.name))
        finally:
            reset_hermes_home_override(token)
        await self.command('/social-memory forget '+ident)
        token = set_hermes_home_override(Path(self.worker_temp.name))
        try:
            self.assertIsNotNone(authorize('web_search', {'query':'迟到查询'}))
            self.assertFalse(self.adapter.admit_group_source(inbound.source))
        finally:
            reset_hermes_home_override(token)
        await self.adapter.on_processing_complete(inbound, None)

    async def test_duplicate_original_command_and_default_disabled_factory(self):
        raw = self.raw('/social-memory remember preference 请简短一些')
        for _ in range(2):
            await self.adapter._receive(raw)
            if self.adapter._control_tasks:
                await asyncio.wait_for(asyncio.gather(*list(self.adapter._control_tasks)), 8)
        scope = self.service.scope(self.adapter.settings.isolation.resolve(
            self.adapter.convert_event(self.raw('公开名字，普通消息')).source))
        self.assertEqual(len(self.service.people.list(scope, pending=True)), 1)
        factories, callbacks = [], []
        context = SimpleNamespace(manifest=SimpleNamespace(name='hermes-life-memory'),
            get_config=lambda key, default=None: {}, on_unload=callbacks.append,
            register_platform_handler=lambda platform, factory: factories.append(factory))
        life.register(context)
        with patch('plugins.plugin_storage.plugin_data_dir', side_effect=AssertionError('disabled storage')):
            self.assertIsNone(factories[0](None, self.adapter))
        for callback in callbacks: callback()

    async def test_explicit_natural_group_memory_without_raw_collection(self):
        self.adapter.uninstall_social_memory(self.service)
        self.config['explicit_memory'] = {'enabled': True, 'ttl_days': 30}
        self.config['agreements'] = {'enabled': True}
        self.service = life.service.LifeMemoryService(self.home, self.home/'explicit-memory', self.config,
            lambda: self.config, channel_domain=self.adapter.settings.isolation.domain_id,
            channel_revision=self.adapter.settings.isolation.revision)
        self.adapter.install_social_memory(self.service, registration_key=('hermes-life-memory', 'explicit-test'))
        self.addCleanup(self.service.close)
        reply = await self.command('请记住：我喜欢合成纸卡工坊')
        ident = re.search(r'review ([a-f0-9]{32})', reply)[1]
        review = await self.command('/social-memory review '+ident)
        confirmation = review.split('确认：')[1].split('\n')[0]
        self.assertIn('已确认', await self.command(confirmation))
        inbound = await self.gateway_input('公开名字，请按本人资料给一句建议')
        self.assertIn('合成纸卡工坊', inbound.test_person_prompt)
        self.assertIn('evidence_and_validity', inbound.test_person_prompt)
        self.assertEqual(self.social.context('group:20001'), [])
        self.assertFalse(self.service.config['conversation']['enabled'])
        with self.service.store.connection() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT user_text FROM sources')], ['合成纸卡工坊'])
        original_call, counter = self.adapter.client.call, 99000
        async def unique_ack(action, payload):
            nonlocal counter
            result = await original_call(action, payload)
            if action == 'send_group_msg':
                counter += 1
                self.echo['message_id'] = counter
                return {'message_id': counter}
            return result
        self.adapter.client.call = unique_ack
        inbound = await self.gateway_input('公开名字，按已确认资料接续', process=True, observe=True)
        delivered = await self.adapter.send('group:20001', '我按合成纸卡工坊来接续。', reply_to=inbound.message_id)
        self.assertTrue(delivered.success)
        await self.adapter.on_processing_complete(inbound, None)
        self.assertIn('合成纸卡工坊', json.dumps(self.social.context('group:20001'), ensure_ascii=False))
        self.assertIn('已忘记', await self.command('/social-memory forget '+ident))
        self.assertNotIn('合成纸卡工坊', json.dumps(self.social.context('group:20001'), ensure_ascii=False))
        due = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        response = await self.command('/social-memory plan '+due+' 核对合成纸卡清单')
        plan_id = re.search(r'review ([a-f0-9]{32})', response)[1]
        review = await self.command('/social-memory review '+plan_id)
        self.assertIn('已确认', await self.command(review.split('确认：')[1].split('\n')[0]))
        prompt = (await self.gateway_input('公开名字，下次约定是什么')).test_person_prompt
        self.assertIn('核对合成纸卡清单', prompt)
        self.assertIn('agreement', prompt)
        response = await self.command('/social-memory episode 我们在合成工坊核对过纸卡编号')
        experience_id = re.search(r'review ([a-f0-9]{32})', response)[1]
        review = await self.command('/social-memory review '+experience_id)
        self.assertIn('已确认', await self.command(review.split('确认：')[1].split('\n')[0]))
        prompt = (await self.gateway_input('公开名字，上次经历是什么')).test_person_prompt
        self.assertIn('我们在合成工坊核对过纸卡编号', prompt)
        self.assertIn('subject_confirmed_statement', prompt)

    async def test_recall_and_plugin_unload_revoke_inflight_memory(self):
        ident, confirmation = await self.remember()
        await self.command(confirmation)
        inbound = await self.gateway_input(process=True)
        # The original self-report was message 101 in this fixture.
        await self.adapter._receive(dict(post_type='notice', notice_type='group_recall', self_id=90001,
            group_id=20001, user_id=77777, operator_id=10001, message_id=101))
        with self.assertRaises(AdmissionDenied): self.adapter._check_reply_target(inbound.source.chat_id)
        await self.adapter.on_processing_complete(inbound, None)
        self.assertNotIn('拿铁', (await self.gateway_input()).test_person_prompt)
        ident, confirmation = await self.remember('绿茶')
        await self.command(confirmation)
        inbound = await self.gateway_input(process=True)
        for callback in reversed(self.callbacks): callback()
        with self.assertRaises(AdmissionDenied): self.adapter._check_reply_target(inbound.source.chat_id)
        await self.adapter.on_processing_complete(inbound, None)

    async def test_real_sdk_registration_opt_in_and_no_memory_provider_switch(self):
        ident, confirmation = await self.remember()
        await self.command(confirmation)
        self.adapter.uninstall_social_memory(self.service)
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        manifest = parse_manifest_file(ROOT/'plugin.yaml', ROOT, 'user', '')
        manager = PluginManager(scope_key=str(self.home))
        context = PluginContext(manifest, manager)
        config = {'memory': {'provider':'mem0'}, 'plugins': {'entries': {
            'hermes-life-memory': {'settings': {'memory': self.config}}}}}
        with patch('hermes_cli.plugins.load_config_readonly', return_value=config), \
                patch.object(context, 'register_memory_provider', side_effect=AssertionError('provider switch forbidden')):
            life.register(context)
            factory, owner = manager.get_platform_handler_factories('napcat')[0]
            service = factory(None, self.adapter)
            self.assertIs(factory(None, self.adapter), service)
            self.assertEqual(config['memory']['provider'], 'mem0')
        key = (owner, factory.__qualname__)
        # The fixture calls the factory directly; emulate the SDK wire cache.
        if self.adapter._plugin_handlers_wired is None:
            self.adapter._plugin_handlers_wired = set()
        self.adapter._plugin_handlers_wired.add(key)
        self.assertTrue(manager.unload(owner))
        self.assertNotIn(key, self.adapter._plugin_handlers_wired)
        self.assertFalse(service.active)
        self.assertIsNone(factory(None, self.adapter))
        # This SDK retains platform factories on targeted unload. A full
        # rediscovery clears that registry before loading the new generation.
        manager.unload()
        self.assertEqual(manager.get_platform_handler_factories('napcat'), [])
        reloaded_context = PluginContext(manifest, manager)
        with patch('hermes_cli.plugins.load_config_readonly', return_value=config):
            life.register(reloaded_context)
            new_factory, new_owner = manager.get_platform_handler_factories('napcat')[0]
            replacement = new_factory(None, self.adapter)
            self.assertIsNot(replacement, service)
            self.assertIn('兴趣：拿铁', (await self.gateway_input()).test_person_prompt)
            # A delayed old detach cannot close or detach the replacement.
            self.adapter.uninstall_social_memory(service)
            self.assertIs(self.adapter._life_memory, replacement)
            self.assertTrue(replacement.active)
        self.assertTrue(manager.unload(new_owner))
