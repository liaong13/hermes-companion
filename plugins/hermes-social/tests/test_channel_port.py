# pyright: reportMissingImports=false
"""Sibling connector integration: real port/worker validation, captured OneBot only.

Standalone plugin distributions can run the independent suite without the
sibling source; these integration cases explicitly report that prerequisite.
"""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import support
from support import FakeContext, ROOT, policy
from hermes_social import register
from hermes_constants import set_hermes_home_override, reset_hermes_home_override

NAPCAT = ROOT.parent / 'hermes-napcat'
WORKER = ROOT.parent / 'hermes-scoped-memory/isolated_worker.py'


def load(name, path, *, package=False):
    spec = importlib.util.spec_from_file_location(name, path,
                submodule_search_locations=[str(path.parent)] if package else None)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((NAPCAT / 'participation.py').is_file() and WORKER.is_file(),
                     'requires sibling connector and worker source')
class RealChannelPortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.token = set_hermes_home_override(self.home)
        self.ctx = FakeContext()
        self.plugin = sys.modules.get('hermes_napcat') or load('hermes_napcat', NAPCAT / '__init__.py', package=True)
        from gateway import platform_registry
        registry = platform_registry.PlatformRegistry()
        self.registry_patch = patch.object(platform_registry, 'platform_registry', registry)
        self.registry_patch.start()
        self.plugin.register(SimpleNamespace(register_platform=lambda **kw: registry.register(platform_registry.PlatformEntry(**kw))))
        from gateway.config import PlatformConfig
        from hermes_napcat.host_runtime import HostIsolationBridge, HostedNapCatAdapter
        self.block_connect = patch('hermes_napcat.transport.OneBotClient.start', side_effect=AssertionError('real OneBot forbidden'))
        self.block_connect.start()
        self.host_binding = SimpleNamespace(home=self.home, active=True, validate=lambda: None,
                                            validate_active=lambda: None)
        worker_home = self.home / 'worker'
        worker_home.mkdir()
        (worker_home / 'scoped-memory.json').write_text(json.dumps({
            'schema_version': 2, 'mode': 'local-only', 'domain_id': 'public-test',
            'policy_revision': 1, 'host_contract': 4}))
        worker_module = load('social_contract_worker_test', WORKER)
        self.worker_payloads = []
        async def request(payload):
            self.worker_payloads.append(payload)
            return worker_module.prepare_proactive(payload, worker_home)
        bridge = HostIsolationBridge(self.host_binding, SimpleNamespace(request=request), gateway_tools=True)
        config = PlatformConfig(enabled=True, extra=dict(self_id='90001', owner_id='10001',
                    allowed_users=['10001'], allowed_groups=['20001', '20002'], require_mention=True,
                    group_admission='all_members', group_trigger={'mode': 'name_or_mention', 'name': '公开名字'},
                    isolation={'mode': 'strict', 'private_profile': 'default', 'social_profile': 'social',
                               'domain_id': 'public-test', 'policy_revision': 1, 'guest_dm_users': []}))
        self.adapter = HostedNapCatAdapter(config, self.host_binding, bridge)
        self.sent, self.dispatched = [], []
        ready = asyncio.Event()
        ready.set()
        self.echo = None
        async def call(action, payload):
            self.sent.append((action, payload))
            if action.startswith('send_'):
                self.echo = dict(message_id=90001, user_id=90001,
                    message_type='group' if action == 'send_group_msg' else 'private',
                    group_id=payload.get('group_id'), target_id=payload.get('user_id'), message=payload['message'])
                return {'message_id': 90001}
            if action == 'get_msg':
                return self.echo
            if action == 'get_friend_list':
                return []
            raise AssertionError(action)
        self.adapter.client = SimpleNamespace(call=call, ready=ready, close=AsyncMock())
        self.adapter._queue_dispatch = self.dispatched.append
        self.social = None

    async def asyncTearDown(self):
        self.ctx.release.set()
        self.ctx.unload()
        await asyncio.sleep(0)
        if self.social and self.social.binding.shutdown_task:
            await self.social.binding.shutdown_task
        await self.adapter.disconnect()
        self.block_connect.stop()
        self.registry_patch.stop()
        status = sys.modules.get('gateway.status')
        if status is not None:
            status.flush_runtime_status(timeout=2)
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def raw(self, text='公开话题', mid=1, *, user=10001, group=20001, private=False):
        return dict(post_type='message', self_id=90001, user_id=user, message_id=mid,
                    message_type='private' if private else 'group', group_id=group,
                    sender={'user_id': user, 'card': '当前群昵称', 'nickname': '公开昵称'},
                    message=[{'type': 'text', 'data': {'text': text}}])

    def attach(self):
        register(self.ctx)
        self.social = self.ctx.handlers['napcat'](None, self.adapter)
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0
        return self.social

    async def test_absent_plugin_keeps_addressed_and_owner_dm_but_no_unaddressed(self):
        self.assertIsNone(self.adapter.convert_event(self.raw()))
        self.assertTrue(self.adapter.convert_event(self.raw('公开名字你好', mid=2)).reply_expected)
        self.assertTrue(self.adapter.convert_event(self.raw('主人私聊', mid=3, private=True)).reply_expected)
        self.assertFalse(self.adapter.participation.active)

    async def test_fresh_ports_new_native_and_reconnect_keep_one_controller(self):
        controller = self.attach()
        factory = self.ctx.handlers['napcat']
        self.assertIsNot(self.adapter.participation_port(), controller.port)
        self.assertIs(factory(object(), self.adapter), controller)
        controller.reset_connection()
        self.assertIs(factory(object(), self.adapter), controller)
        self.assertEqual(len(self.ctx.tasks), 1)
        self.assertIs(self.adapter.participation, controller)

    async def test_real_worker_facade_port_consume_single_send_and_exact_readback(self):
        controller = self.attach()
        controller.states['group:20001'].last_activity = time.time() - 4000
        self.assertTrue(await controller.tick('group:20001'))
        self.assertEqual([row[0] for row in self.sent], ['send_group_msg', 'get_msg'])
        self.assertEqual(self.sent[0][1]['group_id'], 20001)
        self.assertEqual(controller.last_receipts['group:20001'], '90001')
        self.assertEqual(self.worker_payloads[0]['operation'], 'prepare_proactive')
        self.assertEqual(self.worker_payloads[0]['automation']['chat_id'], 'group:20001')
        self.assertFalse(await controller.tick('group:20001'))
        self.assertEqual(len(self.ctx.calls), 1)

    async def test_real_source_ledger_copy_pause_recall_and_normal_turn_separation(self):
        controller = self.attach()
        item = self.adapter.convert_event(self.raw('大家觉得公开话题怎么样？', mid=10, user=10002))
        controller.observe(item)
        controller.queue_ambient(item)
        await asyncio.gather(*controller.ambient_tasks.values())
        self.assertEqual(self.dispatched, [item])
        self.assertTrue(self.adapter._optional_sources.valid(item.source))
        self.assertFalse(self.adapter._optional_sources.valid(copy.copy(item.source)))
        pause = self.adapter.convert_event(self.raw('/social pause', mid=11, user=10002))
        controller.control(pause)
        self.assertFalse(self.adapter._optional_sources.valid(item.source))
        self.assertFalse(controller.allows_unaddressed('group:20001'))
        self.assertTrue(controller.observes('group:20001'))
        self.assertTrue(self.adapter.convert_event(self.raw('公开名字点名', mid=12)).reply_expected)
        self.assertTrue(self.adapter.convert_event(self.raw('私聊', mid=13, private=True)).reply_expected)
        resume = self.adapter.convert_event(self.raw('/social resume', mid=14))
        controller.control(resume)
        fresh = self.adapter.convert_event(self.raw('大家觉得另一个话题怎么样？', mid=15))
        controller.observe(fresh)
        controller.queue_ambient(fresh)
        await asyncio.gather(*controller.ambient_tasks.values())
        self.assertTrue(self.adapter._optional_sources.valid(fresh.source))
        controller.recall('group:20001', '10')
        self.assertFalse(self.adapter._optional_sources.valid(fresh.source))

    async def test_parsed_targets_mixed_at_and_verified_reply_not_nickname_or_literal_text(self):
        controller = self.attach()
        mixed = self.raw('大家怎么看？', mid=31, user=10002)
        mixed['message'] = [
            {'type': 'at', 'data': {'qq': '10001'}},
            {'type': 'at', 'data': {'qq': '90001'}},
            {'type': 'text', 'data': {'text': '大家怎么看？'}}]
        parsed = self.adapter.convert_event(mixed)
        self.assertTrue(parsed.reply_expected)
        self.assertEqual(parsed.at_targets, ('10001', '90001'))
        other = self.raw('大家怎么看？', mid=32, user=10002)
        assert isinstance(other['message'], list)
        other['message'].insert(0, {'type': 'at', 'data': {'qq': '10001'}})
        candidate = self.adapter.convert_event(other)
        controller.observe(candidate)
        controller.queue_ambient(candidate)
        await asyncio.gather(*controller.ambient_tasks.values())
        self.assertFalse(self.dispatched)
        literal = self.adapter.convert_event(self.raw('文字中的@90001 大家怎么看？', mid=33))
        self.assertEqual(literal.at_targets, ())

        quote = self.raw('那哪个更好？', mid=34, user=10002)
        assert isinstance(quote['message'], list)
        quote['message'].insert(0, {'type': 'reply', 'data': {'id': '71'}})
        self.echo = dict(message_type='group', group_id=20001, message_id=71,
                         user_id=90001, sender={'user_id': 90001},
                         message=[{'type': 'text', 'data': {'text': '之前的回答'}}])
        self.assertEqual((await self.adapter._verified_self_reply(quote))['origin'], '71')
        with patch.object(self.adapter, '_receive_isolated', new_callable=AsyncMock) as receive:
            await self.adapter._receive(quote)
        receive.assert_awaited_once()
        assert receive.await_args is not None
        anchored = receive.await_args.args[0]
        self.assertEqual(anchored.verified_reply_author_id, '90001')
        self.assertTrue(anchored.reply_expected)
        self.echo['group_id'] = 20002
        self.assertIsNone(await self.adapter._verified_self_reply(quote))
        self.echo['group_id'], self.echo['user_id'] = 20001, 10001
        self.assertIsNone(await self.adapter._verified_self_reply(quote))

    async def test_unload_detaches_to_null_and_rejects_old_idle_handle(self):
        controller = self.attach()
        port = controller.port
        grant = port.claim_idle('group:20001', 'a' * 32)
        self.ctx.unload()
        self.assertFalse(controller.active)
        self.assertFalse(self.adapter.participation.active)
        self.assertFalse(await port.send_idle(grant, '迟到', lambda: self.fail('must not consume'), lambda: True))
        self.assertIsNone(self.adapter.convert_event(self.raw(mid=16)))
        self.assertTrue(self.adapter.convert_event(self.raw('公开名字正常点名', mid=17)).reply_expected)
        self.assertTrue(self.adapter.convert_event(self.raw('正常主人私聊', mid=18, private=True)).reply_expected)

    async def test_real_sdk_wire_late_load_unload_cache_clear_and_reenable(self):
        factories = []
        manager = SimpleNamespace(get_platform_handler_factories=lambda _: factories)
        with patch('hermes_cli.plugins.get_plugin_manager', return_value=manager):
            self.adapter._wire_plugin_handlers()
            self.assertFalse(self.adapter.participation.active)
            register(self.ctx)
            factory = self.ctx.handlers['napcat']
            factories.append((factory, self.ctx.manifest.name))
            self.adapter.rewire_plugin_handlers()
            self.social = self.adapter.participation
            self.assertTrue(self.social.active)
            old = self.social
            self.adapter.rewire_plugin_handlers()
            self.assertIs(self.adapter.participation, old)
            self.ctx.unload()
            self.assertFalse(self.adapter.participation.active)
            self.assertFalse(self.adapter._plugin_handlers_wired)
            await asyncio.sleep(0)
            if old.binding.shutdown_task:
                await old.binding.shutdown_task
            self.ctx = FakeContext()
            register(self.ctx)
            factories[:] = [(self.ctx.handlers['napcat'], self.ctx.manifest.name)]
            self.adapter.rewire_plugin_handlers()
            self.social = self.adapter.participation
            self.assertTrue(self.social.active)
            self.assertIsNot(self.social, old)
