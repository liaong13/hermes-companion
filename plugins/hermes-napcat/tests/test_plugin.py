"""Real Hermes types + real aiohttp loopback OneBot peer, no QQ credentials."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import atexit
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
# Isolate host lazy config/status/plugin discovery from every active profile.
_TEST_HOME = tempfile.TemporaryDirectory(prefix='test-hermes-home-', dir=os.environ.get('TMPDIR', ROOT))
def _cleanup_test_home():
    # Adapters publish status asynchronously. Join the host's bounded writer
    # before removing this synthetic home, rather than racing it at process exit.
    status = sys.modules.get('gateway.status')
    if status is not None and not status.flush_runtime_status(timeout=2):
        raise RuntimeError('test runtime-status writer did not flush')
    _TEST_HOME.cleanup()
atexit.register(_cleanup_test_home)
os.environ['HERMES_HOME'] = _TEST_HOME.name
# Importing AIAgent may bootstrap a newer host inside this synthetic home.
# Tests must use preinstalled dependencies, never provision or re-exec a runtime.
os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
# All cloud/provider tests use synthetic credentials, including direct discovery.
os.environ.pop('OPENROUTER_API_KEY', None)
os.environ.pop('NAPCAT_MEDIA_OPENROUTER_API_KEY', None)
if 'HERMES_SOURCE' in os.environ:
    configured = os.environ['HERMES_SOURCE']
    HERMES = Path(configured).expanduser().resolve()
    if not configured.strip() or not (HERMES / 'gateway' / '__init__.py').is_file():
        raise RuntimeError('HERMES_SOURCE must point to a Hermes source checkout containing gateway')
    sys.path.insert(0, str(HERMES))
elif importlib.util.find_spec('gateway') is None:
    raise RuntimeError('run with the Hermes Python environment or set HERMES_SOURCE to its source checkout')
spec = importlib.util.spec_from_file_location('hermes_napcat', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from hermes_napcat.adapter import NapCatAdapter, Settings, register, validate_config
from hermes_napcat import standalone
from hermes_napcat.transport import OneBotClient, OneBotError
from gateway.config import PlatformConfig, Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent
from gateway import platform_registry
from gateway.platform_registry import PlatformEntry, PlatformRegistry

# Match the host's registration-before-adapter lifecycle, without touching a live registry.
_TEST_REGISTRY = PlatformRegistry()
platform_registry.platform_registry = _TEST_REGISTRY
register(SimpleNamespace(register_platform=lambda **kw: _TEST_REGISTRY.register(PlatformEntry(**kw))))


def config(**overrides):
    extra = dict(self_id='90001', owner_id='10001', allowed_users=['10001', '10002'],
                 allowed_groups=['20001'], require_mention=True)
    extra.update(overrides)
    return PlatformConfig(enabled=True, extra=extra)


def event(**overrides):
    raw = dict(post_type='message', self_id=90001, user_id=10001, message_id=5,
               message_type='group', group_id=20001,
               sender=dict(user_id=10001, card='群名片', nickname='昵称'),
               message=[{'type': 'at', 'data': {'qq': '90001'}},
                        {'type': 'text', 'data': {'text': '你好'}}])
    raw.update(overrides)
    return raw


class SendRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_delivery_is_not_replayed_as_plain_text(self):
        adapter = NapCatAdapter(config())
        adapter.client = SimpleNamespace(call=AsyncMock(side_effect=OneBotError('unknown delivery')))
        result = await adapter._send_with_retry('group:20001', '公开回答')
        self.assertFalse(result.success)
        self.assertEqual(adapter.client.call.await_count, 1)

    async def test_denied_reply_is_final_and_keeps_only_safe_reason(self):
        cfg = config(isolation=dict(mode='strict', private_profile='default', social_profile='social',
                     domain_id='social-example', policy_revision=1, guest_dm_users=[]))
        adapter = NapCatAdapter(cfg)
        adapter.client = SimpleNamespace(call=AsyncMock())
        with patch.object(adapter, 'send', wraps=adapter.send) as send:
            result = await adapter._send_with_retry('group:20001', 'PRIVATE_BODY_CANARY')
        self.assertFalse(result.success)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(result.raw_response['error_code'], 'reply_target_denied')
        self.assertNotIn('PRIVATE_BODY_CANARY', result.error)
        adapter.client.call.assert_not_awaited()


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = NapCatAdapter(config())

    def test_real_hermes_types_and_registration(self):
        self.assertIsInstance(self.adapter, BasePlatformAdapter)
        captured = {}
        register(SimpleNamespace(register_platform=lambda **kw: captured.update(kw)))
        self.assertEqual(captured['name'], 'napcat')
        self.assertFalse(captured['allow_update_command'])
        self.assertNotIn('send_message_handler', captured)
        self.assertEqual(captured['allowed_users_env'], 'NAPCAT_ALLOWED_USERS')
        self.assertIs(captured['parse_target_ref_fn'], standalone.parse_target)
        self.assertIs(captured['standalone_sender_fn'], standalone.send_owner_notice)

    def test_real_host_loader_and_authorization_extra(self):
        from hermes_cli.plugins_loader import PluginLoaderMixin
        from hermes_cli.plugins_manifest import PluginManifest
        from gateway.authz_mixin import GatewayAuthorizationMixin
        manifest = PluginManifest(name='hermes-napcat', kind='platform', source='user', path=str(ROOT))
        loaded = PluginLoaderMixin._load_directory_module(
            SimpleNamespace(), manifest, module_name='hermes_plugins.hermes_napcat_test')
        captured = {}
        loaded.register(SimpleNamespace(register_platform=lambda **kw: captured.update(kw)))
        self.assertEqual(captured['name'], 'napcat')
        adapter = self.adapter
        class Harness(GatewayAuthorizationMixin):
            def _delivery_adapter_for(self, source):
                return adapter
        source = adapter.convert_event(event()).source
        # Registry is already populated above: do not discover unrelated installed plugins.
        with patch('hermes_cli.plugins.discover_plugins', return_value=None):
            self.assertTrue(Harness()._adapter_extra_allowlist_authorizes(source, '10001', True))
            self.assertFalse(Harness()._adapter_extra_allowlist_authorizes(source, '77777', True))

    def test_staging_dry_run_and_apply_are_not_install(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            destination = Path(temp) / 'stage'
            command = [sys.executable, str(ROOT/'scripts/stage_install.py'), '--destination', str(destination)]
            dry = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertIn('DRY RUN', dry.stdout)
            self.assertFalse(destination.exists())
            applied = subprocess.run(command+['--apply'],capture_output=True,text=True,check=True)
            self.assertIn('APPLY',applied.stdout)
            self.assertTrue((destination/'__init__.py').is_file())
            self.assertTrue((destination/'isolation.py').is_file())
            self.assertTrue((destination/'process_owner.py').is_file())
            self.assertFalse((destination/'config.yaml').exists())
            refused = subprocess.run([sys.executable,str(ROOT/'scripts/stage_install.py'),
                                      '--destination',str(Path(temp)/'plugins'/'hermes-napcat'),'--apply'],
                                     capture_output=True,text=True)
            self.assertNotEqual(refused.returncode,0)

    def test_fail_closed_settings(self):
        for extra in [{}, {'self_id': '90001'}, config(allowed_users=[]).extra,
                      config(owner_id='77777').extra, config(self_id='090001').extra,
                      config(require_mention='false').extra, config(allowed_users='10001').extra,
                      config(access_token='not-a-secret').extra, config(allowed_users=['90001','10001']).extra]:
            with self.subTest(extra=extra), self.assertRaises((ValueError, TypeError)):
                Settings(extra)
        for url in ['ws://localhost:13011', 'ws://192.168.1.1:13011',
                    'https://127.0.0.1:13011', 'ws://127.0.0.1:13011?access_token=x',
                    'ws://x@127.0.0.1:13011', 'ws://127.0.0.1:13011/#x']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Settings(config(ws_url=url).extra)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(validate_config(config()))

    def test_acl_including_group_member(self):
        for change in [dict(user_id=77777), dict(group_id=77777), dict(self_id=77777),
                       dict(user_id=90001), dict(post_type='message_sent'), dict(post_type='notice'),
                       dict(message_type='private', user_id=77777),
                       dict(sender={'user_id':10002, 'card':'owner'}),
                       dict(message_type='private', sub_type='group')]:
            with self.subTest(change=change):
                self.assertIsNone(self.adapter.convert_event(event(**change)))

    def test_mentions_strict_not_text_or_reply(self):
        for segments in [[{'type':'text','data':{'text':'[CQ:at,qq=90001]'}}],
                         [{'type':'at','data':{'qq':'all'}}],
                         [{'type':'reply','data':{'id':'1'}}],
                         [{'type':'at','data':{'qq':'77777'}}]]:
            self.assertIsNone(self.adapter.convert_event(event(message=segments)))
        other = NapCatAdapter(config(require_mention=False))
        self.assertIsNotNone(other.convert_event(event(message=[{'type':'text','data':{'text':'hello'}}])))

    def test_group_conversion_metadata_and_reply(self):
        raw = event()
        raw['message'] += [{'type':'at','data':{'qq':'10002'}}, {'type':'reply','data':{'id':'-42'}}]
        result = self.adapter.convert_event(raw)
        self.assertIsInstance(result, MessageEvent)
        self.assertEqual(result.source.platform, Platform('napcat'))
        self.assertEqual(result.source.chat_id, 'group:20001')
        self.assertEqual(result.source.chat_type, 'group')
        self.assertEqual(result.user_id, '10001')
        self.assertEqual(result.text, '你好@10002')
        self.assertEqual(result.reply_to_message_id, '-42')
        self.assertEqual(result.metadata, {})
        self.assertEqual(result.user_name, '群名片')
        self.assertEqual(result.source.user_name, '群名片')
        self.assertFalse(result.source.role_authorized)

    def test_native_admission_dm_and_group(self):
        # Exercise the host's durable ingress boundary, not only MessageEvent types.
        from gateway.session_envelope import _adapter_event, _validate_native
        from gateway.authz_mixin import GatewayAuthorizationMixin
        adapter = self.adapter

        class Harness(GatewayAuthorizationMixin):
            config = SimpleNamespace(multiplex_profiles=False)

            def _delivery_adapter_for(self, source):
                return adapter

            def _adapter_for_source(self, source):
                return adapter

            def _adapter_profile_for_source(self, source):
                return None

            def _adapter_flag(self, platform, name, profile=None):
                return False

            def _is_user_authorized_for_source(self, source, **kwargs):
                return self._adapter_extra_allowlist_authorizes(source, source.user_id, True)

        for kind in ('private', 'group'):
            with self.subTest(kind=kind):
                converted = adapter.convert_event(event(message_type=kind))
                with patch('hermes_cli.plugins.discover_plugins', return_value=None):
                    encoded = _validate_native(Harness(), _adapter_event(converted))
                self.assertEqual(encoded['user_id'], '10001')
                self.assertEqual(converted.metadata, {})
                self.assertEqual(converted.source.user_name, '群名片' if kind == 'group' else '昵称')

    def test_identity_scope_fallbacks_and_untrusted_values(self):
        for kind, sender, expected in [
            ('private', {'card': '不能用于私聊', 'nickname': 'QQ昵称'}, 'QQ昵称'),
            ('group', {'card': '群昵称', 'nickname': 'QQ昵称'}, '群昵称'),
            ('group', {'card': '  ', 'nickname': 'QQ昵称'}, 'QQ昵称'),
            ('private', {'nickname': None, 'card': '群昵称'}, '10001'),
            ('group', {'card': {}, 'nickname': []}, '群友'),
        ]:
            with self.subTest(kind=kind, sender=sender):
                result = NapCatAdapter(config()).convert_event(event(message_type=kind, sender=sender))
                self.assertEqual(result.user_name, expected)
                data = json.loads(result.channel_context.split(': ', 1)[1])
                self.assertEqual('group_nickname' in data, kind == 'group')
                self.assertEqual('friend_remark' in data, kind == 'private')
                self.assertEqual(result.metadata, {})

    def test_dm_and_dedup_scoped(self):
        dm = event(message_type='private')
        first = self.adapter.convert_event(dm)
        self.assertEqual(first.source.chat_id, 'dm:10001')
        self.assertEqual(first.source.chat_type, 'dm')
        self.assertIsNone(self.adapter.convert_event(dm))
        self.assertIsNotNone(self.adapter.convert_event(event()))

    def test_media_never_fetch_or_propagate_url(self):
        raw = event(message_type='private', message=[{'type':'image','data':{'url':'http://127.0.0.1/secret','file':'/etc/passwd'}}])
        result = self.adapter.convert_event(raw)
        self.assertEqual(result.text, '[unsupported:image]')
        self.assertEqual(result.media_urls, [])
        self.assertIsNone(result.raw_message)

    def test_malformed_events(self):
        for value in [None, [], {}, event(message='[CQ:at,qq=90001]'), event(message=[None]),
                      event(message=[{'type':'text','data':{'text':55}}]), event(message_id=True)]:
            self.assertIsNone(self.adapter.convert_event(value))

    def test_dedup_bound_and_expiry(self):
        self.adapter._seen[('group:20001','5')] = 0
        self.assertIsNotNone(self.adapter.convert_event(event()))
        for n in range(4200):
            self.adapter.convert_event(event(message_id=n+100))
        self.assertLessEqual(len(self.adapter._seen),4096)


class SelfReplyTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_to_verified_bot_message_without_mention(self):
        cfg = config(group_trigger={'mode': 'name_or_mention', 'name': '测试实例甲'})
        adapter = NapCatAdapter(cfg)
        adapter.handle_message = AsyncMock()
        quoted = dict(message_type='group', group_id=20001, message_id=42,
                      self_id=90001, user_id=90001, sender={'user_id': 90001})
        client = SimpleNamespace(call=AsyncMock(return_value=quoted))
        adapter.client = client
        raw = event(user_id=10002, sender={'user_id': 10002, 'nickname': '小号'},
                    message=[{'type': 'reply', 'data': {'id': '42'}},
                             {'type': 'text', 'data': {'text': '接着说'}}])
        self.assertIsNone(adapter.convert_event(raw))
        await adapter._receive(raw)
        adapter.handle_message.assert_awaited_once()
        call = adapter.handle_message.await_args
        assert call is not None
        accepted = call[0][0]
        self.assertEqual((accepted.user_id, accepted.text, accepted.reply_to_message_id),
                         ('10002', '接着说', None))
        client.call.assert_awaited_once_with('get_msg', {'message_id': 42})

    async def test_reply_to_other_or_cross_group_cannot_wake(self):
        cfg = config(group_trigger={'mode': 'name_or_mention', 'name': '测试实例甲'})
        adapter = NapCatAdapter(cfg)
        adapter.handle_message = AsyncMock()
        quoted = dict(message_type='group', group_id=20001, message_id=42,
                      self_id=90001, user_id=90001, sender={'user_id': 90001})
        client = SimpleNamespace(call=AsyncMock(return_value=quoted))
        adapter.client = client
        raw = event(user_id=10002, sender={'user_id': 10002},
                    message=[{'type': 'reply', 'data': {'id': '42'}},
                             {'type': 'text', 'data': {'text': '接着说'}}])
        for change in ({'user_id': 10002, 'sender': {'user_id': 10002}},
                       {'group_id': 20002}, {'message_id': 43},
                       {'sender': {'user_id': 10002}, 'self_id': 90002}):
            with self.subTest(change=change):
                client.call.return_value = dict(quoted, **change)
                await adapter._receive(raw)
                adapter.handle_message.assert_not_awaited()
        client.call.return_value = quoted
        await adapter._receive(event(user_id=10002, sender={'user_id': 10002},
                                     message=[{'type': 'text', 'data': {'text': '普通消息'}}]))
        adapter.handle_message.assert_not_awaited()
        # A legitimate explicit name still works when the lookup is unavailable.
        client.call.side_effect = RuntimeError('disconnected')
        await adapter._receive(event(user_id=10002, sender={'user_id': 10002},
                                     message=[{'type': 'text', 'data': {'text': '测试实例甲你好'}}]))
        adapter.handle_message.assert_awaited_once()


class Peer:
    def __init__(self):
        self.sockets = []
        self.requests = []
        self.auth = []
        self.login_id = 90001
        self.withhold = set()
        self.reverse = []
        self.hold_pair = False
        self.reject = False

    async def handler(self, request):
        self.auth.append(request.headers.get('Authorization'))
        if request.headers.get('Authorization') != 'Bearer test-only-token':
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        async for frame in ws:
            if frame.type != web.WSMsgType.TEXT:
                continue
            body = json.loads(frame.data)
            self.requests.append(body)
            action = body['action']
            if action in self.withhold:
                continue
            data = {'user_id':self.login_id} if action == 'get_login_info' else {'message_id':len(self.requests), 'value':body['params'].get('value')}
            if action == 'get_friend_list':
                data = [{'user_id': 10001, 'nickname': '好友昵称', 'remark': '好友备注'},
                        {'user_id': 77777, 'nickname': '未授权', 'remark': '不缓存'}]
            response = {'status':'failed' if self.reject else 'ok', 'retcode':1 if self.reject else 0, 'data':data,'echo':body['echo']}
            if self.hold_pair and action == 'pair':
                self.reverse.append(response)
                if len(self.reverse) == 2:
                    await ws.send_json(self.reverse[1]); await ws.send_json(self.reverse[0])
            else:
                await ws.send_json(response)
        return ws


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = Peer()
        app = web.Application()
        app.router.add_get('/', self.peer.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner,'127.0.0.1',0)
        await self.site.start()
        self.url = 'ws://127.0.0.1:' + str(self.site._server.sockets[0].getsockname()[1])
        self.received = []
        async def collect(raw): self.received.append(raw)
        self.client = OneBotClient(self.url,'test-only-token','90001',collect,
                                   timeout=.5,reconnect_min=.02,reconnect_max=.05)

    async def asyncTearDown(self):
        await self.client.close()
        for ws in self.peer.sockets:
            await ws.close()
        await self.runner.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate(): await asyncio.sleep(.005)

    async def test_authenticated_roundtrip_and_out_of_order_echo(self):
        await self.client.start()
        self.assertEqual(self.peer.auth, ['Bearer test-only-token'])
        self.peer.hold_pair = True
        results = await asyncio.gather(self.client.call('pair',{'value':'a'}), self.client.call('pair',{'value':'b'}))
        self.assertEqual([x['value'] for x in results],['a','b'])
        self.assertFalse(self.client.pending)
        await self.peer.sockets[-1].send_json({'echo':'unknown','status':'ok','retcode':0,'data':{}})
        await self.peer.sockets[-1].send_json({'echo':[]})
        await self.peer.sockets[-1].send_str('invalid json')
        await self.peer.sockets[-1].send_json(event())
        await self.until(lambda: len(self.received)==1)

    async def test_connection_loss_clears_source_provenance(self):
        cfg = config(allowed_groups=['20001', '20002'], isolation=dict(mode='strict', private_profile='default',
            social_profile='social', domain_id='social-example', policy_revision=5,
            guest_dm_users=[]), media=dict(enabled=True, groups=['20001', '20002'],
            types=['image'], references=True))
        from test_isolation import Bridge
        adapter = NapCatAdapter(cfg, isolation_bridge=Bridge())
        self.client.on_event = adapter._receive
        self.client.on_disconnect = adapter._connection_lost
        adapter.client = self.client
        await self.client.start()
        await self.peer.sockets[-1].send_json(event(message_id=701,
            message=[{'type': 'image', 'data': {'file_id': 'synthetic'}}]))
        await self.peer.sockets[-1].send_json(event(group_id='20002', message_id='702',
            message=[{'type': 'image', 'data': {'file_id': 'synthetic'}}]))
        await self.until(lambda: len(adapter.media.reference_sources.rows) == 2)
        self.assertEqual({key[3] for key in adapter.media.reference_sources.rows},
                         {'group:20001', 'group:20002'})
        await self.peer.sockets[-1].close()
        await self.until(lambda: not adapter.media.reference_sources.rows)

    async def test_private_remark_real_socket_cache_and_failure(self):
        adapter = NapCatAdapter(config())
        adapter.client = self.client
        handler = AsyncMock()
        adapter.handle_message = handler
        self.client.on_event = adapter._receive
        await self.client.start()
        for mid in (71, 72):
            await self.peer.sockets[-1].send_json(event(message_type='private', message_id=mid))
            await self.until(lambda: handler.await_count == mid - 70)
        result = handler.call_args.args[0]
        self.assertEqual(result.user_name, '好友备注')
        self.assertEqual(result.source.user_name, '好友备注')
        self.assertIn('好友备注', result.channel_context)
        self.assertNotIn('群名片', result.channel_context)
        self.assertEqual(set(adapter._friends), {'10001'})
        self.assertEqual(sum(r['action'] == 'get_friend_list' for r in self.peer.requests), 1)
        adapter._friends_refresh_at = 0
        self.peer.reject = True
        await self.peer.sockets[-1].send_json(event(message_type='private', message_id=73))
        await self.until(lambda: handler.await_count == 3)
        self.assertEqual(handler.call_args.args[0].user_name, '昵称')
        self.assertEqual(adapter._friends, {})

    async def test_bad_auth_fails_and_closes(self):
        self.client._token = 'wrong'
        with self.assertRaises(asyncio.TimeoutError): await self.client.start()
        self.assertIsNone(self.client._session)
        self.assertFalse(self.received)

    async def test_private_lookup_timeout_and_denied_sender(self):
        adapter = NapCatAdapter(config())
        adapter.client = SimpleNamespace(call=AsyncMock(side_effect=asyncio.TimeoutError))
        adapter.handle_message = AsyncMock()
        await adapter._receive(event(message_type='private', user_id=77777))
        adapter.client.call.assert_not_awaited()
        await adapter._receive(event(message_type='private', message_id=81))
        result = adapter.handle_message.call_args.args[0]
        self.assertEqual(result.user_name, '昵称')
        self.assertEqual(result.metadata, {})
        await adapter._receive(event(message_type='private', message_id=82))
        adapter.client.call.assert_awaited_once()

    async def test_private_remark_refresh_and_removal(self):
        adapter = NapCatAdapter(config())
        adapter.client = SimpleNamespace(call=AsyncMock(return_value=[
            {'user_id':10001, 'nickname':'接口昵称', 'remark':'原备注'}]))
        adapter.handle_message = AsyncMock()
        await adapter._receive(event(message_type='private', message_id=91))
        self.assertEqual(adapter.handle_message.call_args.args[0].user_name, '原备注')
        adapter._friends_refresh_at = 0
        adapter.client.call.return_value = [{'user_id':10001, 'nickname':'接口昵称', 'remark':''}]
        await adapter._receive(event(message_type='private', message_id=92, sender={}))
        self.assertEqual(adapter.handle_message.call_args.args[0].user_name, '接口昵称')
        await adapter._receive(event(message_type='group', message_id=93))
        self.assertEqual(adapter.handle_message.call_args.args[0].user_name, '群名片')
        self.assertEqual(adapter.client.call.await_count, 2)

    async def test_login_self_id_mismatch_fails_closed(self):
        self.peer.login_id = 77777
        with self.assertRaises(asyncio.TimeoutError): await self.client.start()
        self.assertFalse(self.client.ready.is_set())
        self.assertFalse(self.received)

    async def test_event_self_validation_and_adapter_acl_real_socket(self):
        adapter = NapCatAdapter(config())
        handler = AsyncMock()
        adapter.handle_message = handler
        self.client.on_event = adapter._receive
        await self.client.start()
        for raw in [event(self_id=77777),event(user_id=77777),event(post_type='message_sent'),
                    event(user_id=90001), event(group_id=77777), event(message=[]),event(),event()]:
            await self.peer.sockets[-1].send_json(raw)
        await self.until(lambda: handler.await_count == 1)
        await asyncio.sleep(.02)
        self.assertEqual(handler.await_count,1)
        self.assertEqual(handler.call_args.args[0].text,'你好')

    async def test_real_send_text_segments_no_cq_injection_chunking_acl(self):
        await self.client.start()
        adapter = NapCatAdapter(config())
        adapter.client = self.client
        text = '[CQ:at,qq=all]' + '中'*4100
        result = await adapter.send('group:20001',text,reply_to='-42')
        self.assertTrue(result.success)
        requests = [r for r in self.peer.requests if r['action']=='send_group_msg']
        self.assertEqual(len(requests),2)
        self.assertEqual(''.join(r['params']['message'][1]['data']['text'] for r in requests),text)
        self.assertEqual(requests[0]['params']['message'][0],{'type':'reply','data':{'id':'-42'}})
        self.assertTrue((await adapter.send('dm:10001','hi')).success)
        count = len(self.peer.requests)
        for target in ['10001','group:10001','dm:20001','dm:77777']:
            self.assertFalse((await adapter.send(target,'hi')).success)
        self.assertEqual(len(self.peer.requests),count)

    async def test_disconnect_fails_pending_reconnect_no_replay(self):
        await self.client.start()
        self.peer.withhold.add('send_private_msg')
        action = asyncio.create_task(self.client.call('send_private_msg',{'value':'sent once'}))
        await self.until(lambda: any(r['action']=='send_private_msg' for r in self.peer.requests))
        await self.peer.sockets[-1].close()
        with self.assertRaises(OneBotError): await action
        await self.until(lambda: len(self.peer.sockets)>=2 and self.client.ready.is_set())
        self.assertEqual(sum(r['action']=='send_private_msg' for r in self.peer.requests),1)
        self.assertFalse(self.client.pending)

    async def test_timeout_and_action_error_cleanup(self):
        await self.client.start()
        self.peer.withhold.add('never')
        with self.assertRaises(OneBotError): await self.client.call('never')
        self.assertFalse(self.client.pending)
        self.peer.reject = True
        with self.assertRaises(OneBotError): await self.client.call('reject')
        self.assertFalse(self.client.pending)

    async def test_bounded_queue_responses_not_blocked(self):
        gate = asyncio.Event()
        entered = asyncio.Event()
        async def block(raw):
            entered.set(); await gate.wait()
        self.client.on_event = block
        self.client.queue = asyncio.Queue(maxsize=2)
        await self.client.start()
        await self.peer.sockets[-1].send_json(event())
        await entered.wait()
        for i in range(20): await self.peer.sockets[-1].send_json(event(message_id=i+10))
        self.assertIn('message_id',await self.client.call('send_private_msg'))
        self.assertLessEqual(self.client.queue.qsize(),2)
        self.assertGreater(self.client.dropped_events,0)
        gate.set()

    async def test_typing_optional_and_handler_survives_exception(self):
        await self.client.start()
        adapter = NapCatAdapter(config())
        adapter.client = self.client
        before = len(self.peer.requests)
        await adapter.send_typing('dm:10001')
        self.assertEqual(len(self.peer.requests),before)
        adapter.settings.typing = True
        await adapter.send_typing('dm:10001')
        self.assertEqual(self.peer.requests[-1]['action'],'set_input_status')
        self.client.on_event = AsyncMock(side_effect=RuntimeError('test'))
        await self.peer.sockets[-1].send_json(event())
        await self.until(lambda:self.client.handler_errors==1)
        self.assertIn('message_id',await self.client.call('still_alive'))

    async def test_send_partial_failure_retains_ids_without_replay(self):
        await self.client.start()
        adapter = NapCatAdapter(config())
        adapter.client = self.client
        original = self.client.call
        async def fail_second(action, params=None, **kwargs):
            if sum(r['action']=='send_group_msg' for r in self.peer.requests) >= 1:
                raise OneBotError('unknown delivery')
            return await original(action,params,**kwargs)
        with patch.object(self.client,'call',side_effect=fail_second):
            result = await adapter.send('group:20001','x'*4100)
        self.assertFalse(result.success)
        self.assertEqual(len(result.raw_response['message_ids']),1)
        self.assertFalse(result.retryable)
        self.assertEqual(sum(r['action']=='send_group_msg' for r in self.peer.requests),1)

    async def test_action_capacity_is_bounded(self):
        await self.client.start()
        self.peer.withhold.add('hold')
        tasks = [asyncio.create_task(self.client.call('hold')) for _ in range(64)]
        await self.until(lambda:len(self.client.pending)==64)
        try:
            with self.assertRaisesRegex(OneBotError,'capacity'):
                await self.client.call('overflow')
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        self.assertFalse(self.client.pending)

    async def test_outbound_media_is_explicitly_unsupported_without_fetch(self):
        adapter = NapCatAdapter(config())
        adapter.send = AsyncMock()
        for name in ('send_image','send_image_file','send_document','send_video','send_voice',
                     'send_audio','send_animation','send_multiple_images'):
            result = await getattr(adapter,name)('dm:10001','file:///etc/passwd')
            self.assertFalse(result.success)
            self.assertIn('unsupported',result.error)
        adapter.send.assert_not_awaited()

    async def test_redirect_is_not_followed(self):
        calls = []
        async def redirect(request):
            raise web.HTTPFound('/target')
        async def target(request):
            calls.append(True)
            return web.Response(status=400)
        app = web.Application()
        app.router.add_get('/redirect',redirect)
        app.router.add_get('/target',target)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client.url = f'ws://127.0.0.1:{port}/redirect'
        try:
            with self.assertRaises(asyncio.TimeoutError): await self.client.start()
            self.assertEqual(calls,[])
        finally:
            await runner.cleanup()

    async def test_adapter_connect_disconnect_with_env(self):
        adapter = NapCatAdapter(config(ws_url=self.url))
        adapter._write_runtime_status_safe = lambda *a,**kw:None
        with patch.dict(os.environ,{'NAPCAT_ACCESS_TOKEN':'test-only-token'}):
            try:
                self.assertTrue(await adapter.connect())
                self.assertTrue((await adapter.send('dm:10001','reply')).success)
            finally:
                await adapter.disconnect()
        self.assertIsNone(adapter.client)


if __name__ == '__main__':
    unittest.main()
