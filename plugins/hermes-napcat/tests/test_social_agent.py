"""Web-only group agent contract, using an offline OpenAI-shaped model."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, skip
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import test_plugin  # pins the source package, not an installed plugin snapshot
from hermes_napcat.host_runtime import run_social_agent, validate_agent_home
from hermes_napcat.isolation import AdmissionDenied


@skip('legacy standalone agent fixture; social_agent now enters the gateway turn')
class AgentWebRound(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='napcat-agent-', dir=Path(__file__).resolve().parents[1])
        root = Path(self.temp.name)
        self.owner, self.worker, self.home = (root/x for x in ('owner', 'worker', 'agent'))
        for p in (self.owner, self.worker, self.home):
            p.mkdir(mode=0o700)
        (self.owner/'MEMORY.md').write_text('PRIVATE_MEMORY_CANARY')
        self.requests = []
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'messages' not in data:
                    self.send_response(404)
                    self.end_headers()
                    return
                parent.requests.append(data)
                calls = [m for m in data['messages'] if m.get('role') == 'tool']
                if not calls:
                    msg = {'role': 'assistant', 'content': None, 'tool_calls': [{
                        'id': 'call_search', 'type': 'function', 'function': {
                            'name': 'web_search', 'arguments': json.dumps({'query': 'public test'})}}]}
                elif len(calls) == 1:
                    msg = {'role': 'assistant', 'content': None, 'tool_calls': [{
                        'id': 'call_extract', 'type': 'function', 'function': {
                            'name': 'web_extract', 'arguments': json.dumps({'urls': ['https://example.org/public']})}}]}
                else:
                    msg = {'role': 'assistant', 'content': '已查证公开结果'}
                response = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1,
                            'model': 'synthetic-model', 'choices': [{'index': 0, 'message': msg,
                            'finish_reason': 'stop' if len(calls) > 1 else 'tool_calls'}],
                            'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}}
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = self.home/'config.yaml'
        self.config.write_text('model:\n  provider: custom\n  default: synthetic-model\n  streaming: false\n'
                               f'  base_url: http://127.0.0.1:{self.server.server_port}/v1\n')
        self.config.chmod(0o600)
        self.env = self.home/'.env'
        self.env.write_text('NAPCAT_SOCIAL_API_KEY=synthetic-social-key\n')
        self.env.chmod(0o600)
        self.route = SimpleNamespace(chat_id='group:20001', user_id='77777')
        self.messages = [{'role': 'system', 'content': '仅可查公开网络。'},
                         {'role': 'user', 'content': '公开测试问题'}]
        self.binding = SimpleNamespace(home=self.owner)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_profile_boundary(self):
        validate_agent_home(self.home, self.worker, self.binding)
        for path in (self.owner, self.worker, self.owner/'child'):
            with self.assertRaises(AdmissionDenied):
                validate_agent_home(path, self.worker, self.binding)
        self.env.unlink()
        with self.assertRaises(AdmissionDenied):
            validate_agent_home(self.home, self.worker, self.binding)
        self.env.write_text('OPENAI_API_KEY=OWNER_SECRET_CANARY\n')
        self.env.chmod(0o600)
        with self.assertRaisesRegex(AdmissionDenied, 'social_agent_model_not_configured'):
            validate_agent_home(self.home, self.worker, self.binding)

    def test_codex_uses_borrowed_oauth_without_copying_token(self):
        self.config.write_text('model:\n  provider: openai-codex\n  default: gpt-6-sol\n'
                               '  base_url: https://chatgpt.com/backend-api/codex\n')
        self.env.write_text('')
        with patch('hermes_cli.auth.read_credential_pool', return_value=[{'id': 'synthetic'}]):
            validate_agent_home(self.home, self.worker, self.binding)
            class FakeAgent:
                tools = [{'function': {'name': 'web_search'}},
                         {'function': {'name': 'web_extract'}}]
                def __init__(self, **kwargs):
                    self.kwargs = kwargs
                def run_conversation(self, *args, **kwargs):
                    self.test_result = {'completed': True, 'final_response': '群聊搜索完成'}
                    return self.test_result
                def close(self):
                    pass
            with patch.object(run_agent, 'AIAgent', FakeAgent), \
                 patch.dict(os.environ, {'OPENAI_API_KEY': 'OWNER_SECRET_CANARY'}):
                self.assertEqual(run_social_agent(self.home, self.messages, self.route), '群聊搜索完成')
            self.assertNotIn('OWNER_SECRET_CANARY', self.env.read_text())
        with patch('hermes_cli.auth.read_credential_pool', return_value=[]):
            with self.assertRaisesRegex(AdmissionDenied, 'social_agent_model_not_configured'):
                validate_agent_home(self.home, self.worker, self.binding)
        self.config.write_text('model:\n  provider: openai-codex\n  default: gpt-6-sol\n'
                               '  base_url: https://attacker.example/codex\n')
        with patch('hermes_cli.auth.read_credential_pool', return_value=[{'id': 'synthetic'}]):
            with self.assertRaisesRegex(AdmissionDenied, 'social_agent_model_not_configured'):
                validate_agent_home(self.home, self.worker, self.binding)

    def test_real_agent_model_tool_round(self):
        validate_agent_home(self.home, self.worker, self.binding)
        from tools import web_tools
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'OWNER_SECRET_CANARY'}), \
             patch.object(web_tools, 'web_search_tool', return_value='synthetic search result') as search, \
             patch.object(web_tools, 'web_extract_tool', return_value='synthetic public page') as extract:
            result = run_social_agent(self.home, self.messages, self.route)
        self.assertIn('已查证公开结果', result)
        search.assert_called_once_with('public test', limit=5)
        extract.assert_called_once_with(['https://example.org/public'], 'markdown', char_limit=None)
        self.assertGreaterEqual(len(self.requests), 3)
        self.assertEqual({t['function']['name'] for t in self.requests[0]['tools']},
                         {'web_search', 'web_extract'})
        self.assertIn('synthetic public page', json.dumps(self.requests[-1]))
        self.assertNotIn('OWNER_SECRET_CANARY', json.dumps(self.requests))
        self.assertNotIn('PRIVATE_MEMORY_CANARY', json.dumps(self.requests))
        validate_agent_home(self.home, self.worker, self.binding)
