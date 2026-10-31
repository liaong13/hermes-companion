"""Actual namespace worker and local synthetic model; never connects QQ."""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest

from test_plugin import NapCatAdapter, event
from test_isolation import strict_config
from hermes_napcat.isolation import Admission, AdmissionDenied
from hermes_napcat.runtime_bridge import ProcessIsolationBridge, SandboxedWorker


@unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap is required for the real namespace contract')
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='napcat-isolation-')
        self.home = Path(self.temp.name)/'social'
        self.home.mkdir(mode=0o700)
        (self.home/'scoped-memory.json').write_text(json.dumps({
            'schema_version': 2, 'mode': 'local-only', 'domain_id': 'social-example', 'policy_revision': 1}))
        self.worker = SandboxedWorker(self.home, domain_id='social-example', policy_revision=1,
                                      network_reviewed=True, timeout=5)
        self.adapter = NapCatAdapter(strict_config())
        self.message = self.adapter.convert_event(event())
        self.route = self.adapter.settings.isolation.resolve(self.message.source)
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(body)
                text = body['messages'][-1]['content']
                message = ({'content': 'must not execute', 'tool_calls': [{'function': {'name': 'terminal', 'arguments': '{}'}}]}
                           if text == 'request-tool' else {'content': getattr(owner, 'model_reply', 'synthetic reply')})
                data = json.dumps({'choices': [{'message': message}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        (self.home/'chat.json').write_text(json.dumps({'endpoint': f'http://127.0.0.1:{self.server.server_port}/chat',
                    'model': 'synthetic-model', 'allow_loopback_test': True}))
        key = self.home/'model.key'
        key.write_text('synthetic-key')
        key.chmod(0o600)

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    async def test_real_namespace_cannot_read_private_files_or_host_environment(self):
        secret = Path(self.temp.name)/'private-canary'
        secret.write_text('synthetic-private-canary')
        base = self.worker.command()
        command = base[:base.index('--')+1] + ['/usr/bin/python3', '-I', '-c',
                  'import os,pathlib; assert not pathlib.Path('+repr(str(secret))+').exists(); '
                  'assert not os.environ.get("OWNER_SECRET"); print("isolated")']
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, env={'OWNER_SECRET': 'synthetic-env-canary'})
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(stdout.strip(), b'isolated')

    async def test_real_probe_and_conversation_has_no_tool_surface(self):
        bridge = ProcessIsolationBridge(self.worker)
        admission = await bridge.admit(self.route)
        replies = []
        async def reply(text):
            replies.append(text)
        await bridge.dispatch(self.message, admission, reply)
        self.assertEqual(replies, ['synthetic reply'])
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('tools', self.requests[0])
        self.assertNotIn('tool_choice', self.requests[0])
        self.assertTrue((self.home/'scoped-memory'/'memory.sqlite3').exists())
        with self.assertRaises(AdmissionDenied):
            await bridge.dispatch(self.message, admission, reply)

    async def test_model_tool_requests_are_rejected_without_execution(self):
        from dataclasses import asdict
        with self.assertRaises(AdmissionDenied):
            await self.worker.request({'operation': 'chat', 'route': asdict(self.route), 'text': 'request-tool'})

    async def test_private_route_and_forged_admission_never_use_social_worker(self):
        bridge = ProcessIsolationBridge(self.worker)
        with self.assertRaises(AdmissionDenied):
            await bridge.admit(replace(self.route, domain_id='private', profile='default', chat_type='dm', chat_id='dm:10001'))
        with self.assertRaises(AdmissionDenied):
            await bridge.dispatch(self.message, Admission(self.route), lambda text: None)
        self.assertEqual(self.requests, [])

    async def test_policy_or_origin_change_is_denied_before_model(self):
        bridge = ProcessIsolationBridge(self.worker)
        with self.assertRaises(AdmissionDenied):
            await bridge.admit(replace(self.route, policy_revision=2))
        admission = await bridge.admit(self.route)
        changed = SimpleNamespace(source=SimpleNamespace(**{**vars(self.message.source), 'chat_id': 'group:99999'}), text='x')
        with self.assertRaises(AdmissionDenied):
            await bridge.dispatch(changed, admission, lambda text: None)
        self.assertEqual(self.requests, [])

    async def test_social_home_with_private_backend_refused(self):
        (self.home/'mem0.json').write_text('{}')
        with self.assertRaises(AdmissionDenied):
            SandboxedWorker(self.home, domain_id='social-example', policy_revision=1)

    async def test_forget_starts_clean_context_in_next_worker(self):
        from dataclasses import asdict
        import importlib.util
        import sys
        root = Path(__file__).resolve().parents[2]/'hermes-scoped-memory'
        spec = importlib.util.spec_from_file_location('worker_test_memory', root/'scope.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        # Import the worker's stdlib-only memory package to inspect the synthetic DB.
        worker_spec = importlib.util.spec_from_file_location('test_social_worker', root/'isolated_worker.py')
        worker_module = importlib.util.module_from_spec(worker_spec)
        worker_spec.loader.exec_module(worker_module)
        Store, Scope, _ = worker_module.memory_types()
        store = Store(self.home, 'social-example')
        scope = store.resolve_scope(Scope('social-example', 'napcat', 'napcat:10001', 'group', 'group:20001'))
        memory = store.add(scope, 'synthetic key to forget')
        payload = {'operation': 'chat', 'route': asdict(self.route), 'text': 'first synthetic turn'}
        await self.worker.request(payload)
        payload['route']['message_id'] = '6'
        payload['text'] = '/forget '+memory['id']
        await self.worker.request(payload)
        payload['route']['message_id'] = '7'
        payload['text'] = 'new synthetic turn'
        await self.worker.request(payload)
        self.assertEqual(len(self.requests), 2)
        rendered = json.dumps(self.requests[-1]['messages'])
        self.assertNotIn('first synthetic turn', rendered)
        self.assertNotIn('synthetic key to forget', rendered)

    async def test_personal_candidate_commands_use_original_member_not_model_output(self):
        from dataclasses import asdict
        import importlib.util
        root = Path(__file__).resolve().parents[2]/'hermes-scoped-memory'
        spec = importlib.util.spec_from_file_location('personal_review_worker', root/'isolated_worker.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        Store, Scope, _ = module.memory_types()
        store = Store(self.home, 'social-example')
        scope = store.resolve_scope(Scope('social-example', 'napcat', 'napcat:10001', 'group',
                                         'group:20001', session_id='original', message_id='original'))
        accepted = store.append_turn(scope, '我喜欢拿铁', '收到', enqueue=True)
        job = store.claim_job('namespace-review')
        ident = store.complete_job(job, [{'source_id': accepted['source_id'], 'content': '喜欢拿铁',
                                         'quote': '喜欢拿铁', 'start': 1, 'role': 'user'}])['memory_ids'][0]
        route = asdict(self.route)
        preview = await self.worker.request({'operation': 'chat', 'route': route,
                                            'text': '/scoped-memory review ' + ident})
        command = preview['reply'].split('确认：')[1].split('\n')[0]
        self.assertEqual(self.requests, [])
        with self.assertRaises(AdmissionDenied):
            await self.worker.request({'operation': 'chat', 'route': {**route, 'user_id': '10002'}, 'text': command})
        self.model_reply = command
        await self.worker.request({'operation': 'chat', 'route': {**route, 'message_id': '6'}, 'text': '普通消息'})
        self.assertEqual(store.get(scope, ident)['state'], 'pending')
        confirmed = await self.worker.request({'operation': 'chat', 'route': {**route, 'message_id': '7'}, 'text': command})
        self.assertEqual(confirmed['reply'], '已确认这条个人记忆。')
        self.assertEqual(store.get(scope, ident)['state'], 'active')
        self.assertEqual(len(self.requests), 1)  # The real command never invokes the model.

    async def test_public_confirmation_uses_authenticated_command_and_revokes_other_members_context(self):
        from dataclasses import asdict
        settings = json.loads((self.home/'scoped-memory.json').read_text())
        settings['public_group_memory'] = {'enabled': True, 'approval': 'explicit',
                                           'confirmers': {'group:20001': ['napcat:10002']}}
        (self.home/'scoped-memory.json').write_text(json.dumps(settings))
        route = asdict(self.route)
        proposal = await self.worker.request({'operation': 'chat', 'route': route,
                    'text': '/memory-propose 合成项目 | 合成项目周五发布'})
        command = proposal['reply'].split('确认命令：')[1]
        # The proposer is not on the exact-group confirmation allowlist.
        route['message_id'] = '6'
        with self.assertRaises(AdmissionDenied):
            await self.worker.request({'operation': 'chat', 'route': route, 'text': command})
        other = {**route, 'user_id': '10002', 'message_id': '7'}
        confirmation = await self.worker.request({'operation': 'chat', 'route': other, 'text': command})
        self.assertIn('active', confirmation['reply'])
        other['message_id'] = '8'
        await self.worker.request({'operation': 'chat', 'route': other, 'text': '合成项目旧上下文'})
        self.assertIn('合成项目周五发布', json.dumps(self.requests[-1]['messages'], ensure_ascii=False))
        route['message_id'] = '9'
        await self.worker.request({'operation': 'chat', 'route': route, 'text': '/forget-all'})
        other['message_id'] = '10'
        await self.worker.request({'operation': 'chat', 'route': other, 'text': '新上下文'})
        rendered = json.dumps(self.requests[-1]['messages'], ensure_ascii=False)
        self.assertNotIn('合成项目周五发布', rendered)
        self.assertNotIn('合成项目旧上下文', rendered)

    async def test_conflicting_public_statements_require_separate_group_resolution(self):
        from dataclasses import asdict
        settings = json.loads((self.home/'scoped-memory.json').read_text())
        settings['public_group_memory'] = {'enabled': True, 'approval': 'explicit',
            'confirmers': {'group:20001': ['napcat:10002']}, 'resolvers': {'group:20001': ['napcat:10002']}}
        (self.home/'scoped-memory.json').write_text(json.dumps(settings))
        route, latest_id = asdict(self.route), None
        for number, content in enumerate(('合成项目周五发布', '合成项目周六发布')):
            route['message_id'] = str(20+number*2)
            result = await self.worker.request({'operation': 'chat', 'route': route,
                                               'text': '/memory-propose 合成项目 | '+content})
            command = result['reply'].split('确认命令：')[1]
            latest_id = command.split()[1]
            other = {**route, 'user_id': '10002', 'message_id': str(21+number*2)}
            result = await self.worker.request({'operation': 'chat', 'route': other, 'text': command})
        self.assertIn('conflict', result['reply'])
        route['message_id'] = '25'
        with self.assertRaises(AdmissionDenied):
            await self.worker.request({'operation': 'chat', 'route': route, 'text': '/memory-resolution '+latest_id})
        other['message_id'] = '26'
        card = await self.worker.request({'operation': 'chat', 'route': other, 'text': '/memory-resolution '+latest_id})
        command = card['reply'].split('决议命令：')[1]
        other['message_id'] = '27'
        resolved = await self.worker.request({'operation': 'chat', 'route': other, 'text': command})
        self.assertIn('active', resolved['reply'])
