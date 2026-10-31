#!/usr/bin/env python3
"""Live Codex/GatewayRunner acceptance with synthetic QQ and a capture-only sink.

This is NOT QQ-client acceptance. It never connects OneBot, never auto-discovers the
owner's plugins/MCP servers, never reads owner history, SOUL or memory, and never
copies auth/state files. Only model/toolset + reviewed worker configuration is
read from the launch profile. OAuth is resolved read-only in the owner scope,
kept in memory, and injected at the gateway's credential-resolution seam.

Run with the install's managed bootstrap (the stdlib entry point does this):
  python verify_napcat_social_live.py --host "$HERMES_SOURCE" --owner-home "$HERMES_HOME" --live --case first --label baseline
  python verify_napcat_social_live.py --host "$HERMES_SOURCE" --owner-home "$HERMES_HOME" --live --case all --label candidate

All generated homes, databases, logs, reports and transcripts remain in TMPDIR.
A failing behavioral assertion exits 1 but still writes report.json. Infrastructure
failures exit 2. --case all includes two questions, a continuous follow-up and two
quiet negatives, then re-entry, identity and a short answer to a proven bot
question, through the SAME real durable native gateway path.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

sys.dont_write_bytecode = True


CANARY = 'SYNTHETIC_PRIVATE_CONTEXT_CANARY_7d91b63a'
TEST_SELF_NAME = '测试实例乙'
FIXTURE_PROVENANCE = 'synthetic-authored-v2'
CASES = [
    ('first', '合成活动甲适合练习颜色辨认吗？', True),
    ('comparison', '合成活动甲和合成活动乙有什么区别？', True),
    ('followup', '如果只做一轮，又不想准备太多材料，选哪种合成活动？', True),
    ('other_people', '测试成员乙，请把合成材料一交给测试成员丙。本条仅请测试成员乙处理。', False),
    ('emoji', '😂', False),
    ('reentry', '回到合成活动的比较，我想练习按编号排列，哪种更合适？', True),
    ('identity', '这个合成场景里，我如何称呼你？', True),
    ('answer_to_question', '选项乙', True),
    ('shared_context', '按合成计分规则，步骤丙和步骤丁各增加多少点？', True),
]


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='explicitly authorize real model API calls')
    parser.add_argument('--case', choices=['all', *[c[0] for c in CASES]], default='all')
    parser.add_argument('--label', default='candidate')
    parser.add_argument('--host', type=Path, required=True, help='existing Hermes source checkout')
    parser.add_argument('--owner-home', type=Path, required=True, help='explicit current profile home; only readonly config/auth resolution')
    parser.add_argument('--plugin-root', type=Path, help='explicit reviewed installed NapCat directory; defaults to repository source')
    parser.add_argument('--social-plugin-root', type=Path, help='reviewed social plugin directory; defaults beside NapCat')
    parser.add_argument('--shared-allowlist', action='store_true', help='verify participation derives only from allowed_groups, with no idle targets')
    parser.add_argument('--managed', action='store_true', help=argparse.SUPPRESS)
    return parser.parse_args()


def launch(args):
    if not args.live:
        raise SystemExit('Refusing live inference without --live; no QQ connection is ever made.')
    if args.managed:
        return
    sys.path.insert(0, str(args.host.resolve()))
    from hermes_cli._launchers import runtime_command
    code = 'import runpy; runpy.run_path(' + repr(str(Path(__file__).resolve())) + ", run_name='__main__')"
    command = runtime_command(args.host, args=[*sys.argv[1:], '--managed'], code=code)
    raise SystemExit(subprocess.call(command))


def read_route(args):
    """Use official readonly config/auth resolvers BEFORE the synthetic scope."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config_readonly
    from hermes_cli.auth import resolve_codex_runtime_credentials
    token = set_hermes_home_override(args.owner_home.resolve())
    try:
        config = load_config_readonly()
        model = copy.deepcopy(config.get('model') or {})
        if model.get('provider') != 'openai-codex' or not model.get('default'):
            raise RuntimeError('Current profile must explicitly select an existing Codex model')
        runtime_cfg = copy.deepcopy(config['platforms']['napcat']['extra']['runtime'])
        worker_home = Path(runtime_cfg['social_home']).resolve(strict=True)
        # Read only the reviewed worker settings, not its databases or memories.
        worker_settings = json.loads((worker_home / 'scoped-memory.json').read_text())
        if runtime_cfg.get('mode') != 'social_agent' or runtime_cfg.get('contract') != 5:
            raise RuntimeError('Current reviewed runtime must use social_agent contract 5')
        credentials = resolve_codex_runtime_credentials(read_only=True)
        return {
            'model': model, 'credential': credentials['api_key'],
            'toolsets': copy.deepcopy(config.get('platform_toolsets', {}).get('napcat', ['hermes-cli'])),
            'worker_home': worker_home, 'worker_settings': worker_settings,
            'credential_source': credentials.get('source', 'hermes-auth-store'),
        }
    finally:
        reset_hermes_home_override(token)


def isolate(root):
    home, os_home = root / 'hermes-home', root / 'os-home'
    home.mkdir(mode=0o700)
    os_home.mkdir(mode=0o700)
    for directory in ('memories', 'sessions', 'skills', 'plugins', 'logs'):
        (home / directory).mkdir(mode=0o700)
    # This synthetic marker is never sent to the model; public-turn skip_memory
    # and skip_context_files must keep it out even though it exists in the home.
    (home / 'memories' / 'MEMORY.md').write_text(CANARY)
    (home / 'SOUL.md').write_text(CANARY)
    (root / 'AGENTS.md').write_text(CANARY)
    for name in list(os.environ):
        if name.startswith(('HERMES_SESSION_', 'NAPCAT_')) or name in (
            'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY'):
            os.environ.pop(name, None)
    os.environ.update({
        'HOME': str(os_home), 'HERMES_HOME': str(home),
        'HERMES_TEST_ISOLATION': str(home), 'HERMES_DISABLE_LAZY_INSTALLS': '1',
        'HERMES_GATEWAY_LOCK_DIR': str(root / 'gateway-locks'),
        'XDG_CONFIG_HOME': str(os_home / '.config'), 'XDG_STATE_HOME': str(os_home / '.local/state'),
        'NAPCAT_ACCESS_TOKEN': 'synthetic-capture-only-not-a-qq-token',
    })
    from hermes_constants import set_hermes_home_override
    set_hermes_home_override(home)
    import hermes_constants
    hermes_constants._PINNED_PROCESS_HERMES_HOME = home
    os.chdir(root)
    return home


def configure(home, route, args):
    import hermes_yaml
    # Only the existing route/toolset selection is preserved; no private prompts,
    # prefill, credentials, channel IDs, custom tools or MCP servers are copied.
    config = {
        'model': route['model'],
        'platform_toolsets': {'napcat': route['toolsets'], 'cli': route['toolsets']},
        'auxiliary': {'title_generation': {'enabled': False}},
        'agent': {'max_turns': 6},
        'compression': {'codex_gpt55_autoraise_notice': False},
        'display': {'tool_progress': 'all', 'interim_assistant_messages': True, 'streaming': True},
        'session': {'group_sessions_per_user': True},
        'plugins': {'enabled': ['hermes-napcat', 'hermes-social'], 'entries': {'hermes-social': {'settings': {
            'channels': {'napcat': {'enabled': True,
                'public_self': {'name': TEST_SELF_NAME, 'description': '专用于合成场景的测试实例'},
                'targets': [] if args.shared_allowlist else ['group:20001'],
                'quiet_hours': [0, 0], 'ambient_debounce_seconds': 1,
                'ambient_cooldown_seconds': 5}}}}}},
    }
    (home / 'config.yaml').write_text(hermes_yaml.safe_dump(config, allow_unicode=True))
    (home / '.env').write_text('')
    os.chmod(home / '.env', 0o600)
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(
        requested='openai-codex', explicit_api_key=route.pop('credential'),
        explicit_base_url=route['model']['base_url'], target_model=route['model']['default'])
    if runtime.get('api_mode') != 'codex_responses':
        raise RuntimeError('Expected native codex_responses; no login/runtime substitution allowed')
    runtime['credential_pool'] = None
    route['runtime'] = runtime


class CaptureOneBot:
    """A local action sink, never a websocket/HTTP peer or a QQ client."""
    def __init__(self):
        self.ready = asyncio.Event()
        self.ready.set()
        self.calls = []
        self.messages = {}

    async def call(self, action, payload):
        self.calls.append({'action': action, 'payload': copy.deepcopy(payload)})
        if action == 'get_friend_list':
            return []
        if action == 'send_group_msg':
            if payload.get('group_id') != 20001:
                raise AssertionError('capture sink received another target')
            mid = 50000 + len(self.messages)
            self.messages[mid] = {
                'message_id': mid, 'self_id': 90001, 'user_id': 90001,
                'sender': {'user_id': 90001}, 'message_type': 'group',
                'group_id': 20001, 'message': copy.deepcopy(payload['message']),
            }
            return {'message_id': mid}
        if action == 'get_msg':
            return copy.deepcopy(self.messages[int(payload['message_id'])])
        raise AssertionError('unexpected OneBot action: ' + action)

    async def close(self):
        self.ready.clear()


def load_plugin(plugin_root, module_name):
    spec = importlib.util.spec_from_file_location(
        module_name, plugin_root / '__init__.py', submodule_search_locations=[str(plugin_root)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, plugin_root


def raw_message(text, mid, *, user=10001):
    return {
        'post_type': 'message', 'self_id': 90001, 'user_id': user,
        'message_id': mid, 'message_type': 'group', 'group_id': 20001,
        'sender': {'user_id': user, 'card': '合成群友', 'nickname': '合成昵称'},
        'message': [{'type': 'text', 'data': {'text': text}}],
    }


async def drain(adapter, authority, timeout=180):
    # Tasks create tasks at layer boundaries. Join each whole layer, then inspect
    # the authoritative live-session pump as well as transport task registries.
    async with asyncio.timeout(timeout):
        while True:
            tasks = [*adapter.participation.ambient_tasks.values(), *adapter._text_tasks,
                     *adapter._media_tasks, *adapter._background_tasks,
                     *[live.task for live in authority.sessions.values() if live.task is not None]]
            tasks = list({task for task in tasks if not task.done()})
            if not tasks:
                return
            await asyncio.gather(*tasks)


def plain_text(message):
    if isinstance(message, str):
        return message
    return ''.join(segment.get('data', {}).get('text', '') for segment in message
                   if segment.get('type') == 'text')


def instrument(stack, runner, adapter, state):
    """Observe real methods; every wrapper calls its original exactly once."""
    from run_agent import AIAgent
    from gateway.session_ingress import executing_admission
    import gateway.session_envelope as envelope
    import agent.chat_completion_helpers as completion

    original_handle, original_run = runner._handle_message, runner._run_agent
    original_conversation = AIAgent.run_conversation
    original_restore = envelope.restore_native_source
    original_api = completion._build_api_kwargs_for_mode
    original_request = adapter._isolation_bridge.worker.request

    async def handle(event):
        state['handlers'].append({'message_id': event.message_id, 'executing': bool(executing_admission.get()),
                                  'source_object': id(event.source), 'reply_expected': event.reply_expected})
        return await original_handle(event)

    async def run(*args, **kwargs):
        source = kwargs['source']
        permit = adapter._group_permit_for_source(source)
        state['turns'].append({'message_id': source.message_id, 'source_object': id(source),
                               'actor': source.user_id, 'chat_id': source.chat_id,
                               'live_permit': bool(permit and permit.active and permit.source is source),
                               'ambient_valid': adapter._optional_sources.valid(source),
                               'executing': bool(executing_admission.get()),
                               'reply_expected': kwargs.get('reply_expected')})
        result = await original_run(*args, **kwargs)
        state['turns'][-1]['gateway_final'] = result.get('final_response')
        return result

    def conversation(self, *args, **kwargs):
        names = sorted(t['function']['name'] for t in self.tools)
        record = {'class': type(self).__module__ + '.' + type(self).__name__,
                  'model': self.model, 'provider': self.provider, 'tools': names,
                  'input': args[0] if args else kwargs.get('user_message'),
                  'history_count': len(kwargs.get('conversation_history') or []),
                  'thread': threading.current_thread().name}
        state['agents'].append(record)
        result = original_conversation(self, *args, **kwargs)
        record.update(final_response=result.get('final_response'), api_calls=result.get('api_calls'),
                      completed=result.get('completed', True))
        return result

    def restore(payload, host=None):
        source = original_restore(payload, host)
        permit = adapter._group_permit_for_source(source)
        state['restores'].append({'message_id': source.message_id, 'source_object': id(source),
                                  'exact_live_source': bool(permit and permit.source is source),
                                  'permit_active': bool(permit and permit.active)})
        return source

    def api(self, messages, tools_for_api=None):
        serialized = json.dumps(messages, ensure_ascii=False, default=str)
        assert CANARY not in serialized, 'synthetic private context leaked into model input'
        kwargs = original_api(self, messages, tools_for_api)
        wire_tools = kwargs.get('tools') or []
        tool_names = sorted(t.get('function', {}).get('name', t.get('name', '')) for t in wire_tools)
        state['requests'].append({'model': self.model, 'provider': self.provider,
                                  'messages': copy.deepcopy(messages), 'tools': tool_names})
        assert CANARY not in json.dumps(kwargs, ensure_ascii=False, default=str)
        return kwargs

    async def request(payload):
        state['worker_requests'].append({'operation': payload.get('operation'),
                                         'schema_version': payload.get('schema_version'),
                                         'message_id': (payload.get('route') or {}).get('message_id')})
        return await original_request(payload)

    stack.enter_context(patch.object(runner, '_handle_message', handle))
    stack.enter_context(patch.object(runner, '_run_agent', run))
    stack.enter_context(patch.object(AIAgent, 'run_conversation', conversation))
    stack.enter_context(patch.object(envelope, 'restore_native_source', restore))
    stack.enter_context(patch.object(completion, '_build_api_kwargs_for_mode', api))
    stack.enter_context(patch.object(adapter._isolation_bridge.worker, 'request', request))


async def verify(args, root, route, home, report):
    from gateway.run import GatewayRunner, _runtime_agent_kwargs
    import gateway.run as gateway_run
    from gateway.config import Platform, PlatformConfig, HomeChannel
    from gateway import platform_registry
    from gateway.platform_registry import PlatformRegistry
    from gateway.session_authority import initialize_session_authority
    from hermes_state_runtime import list_session_admissions
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.response_filters import is_intentional_silence_response
    plugin, plugin_root = load_plugin((args.plugin_root or
                                      Path(__file__).resolve().parents[2] / 'plugins/hermes-napcat').resolve(strict=True),
                                      'hermes_napcat')
    social, social_root = load_plugin((args.social_plugin_root or
                                      plugin_root.parent / 'hermes-social').resolve(strict=True), 'hermes_social')
    sink = CaptureOneBot()
    registry = PlatformRegistry()
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='hermes-napcat', kind='platform', source='user', path=str(plugin_root)), manager)
    social_ctx = PluginContext(PluginManifest(name='hermes-social', source='user', path=str(social_root)), manager)
    runner, adapter = None, None
    state = {name: [] for name in ('handlers', 'turns', 'agents', 'restores', 'requests', 'worker_requests')}
    report['trace'] = state
    with ExitStack() as stack:
        # Credential seam returns the official resolved route, not fake model
        # output. The full real gateway and AIAgent still perform every request.
        stack.enter_context(patch.object(gateway_run, '_resolve_runtime_agent_kwargs',
                                         lambda: _runtime_agent_kwargs(route['runtime'])))
        stack.enter_context(patch.object(platform_registry, 'platform_registry', registry))
        stack.enter_context(patch('hermes_cli.plugins.get_plugin_manager', return_value=manager))
        # A synthetic profile has no discoverable plugins. Explicit registration
        # below is real PluginContext.register_platform, including Platform enum.
        plugin.register(ctx)
        social.register(social_ctx)
        policy = route['worker_settings']
        cfg = PlatformConfig(enabled=True, extra={
            'self_id': '90001', 'owner_id': '10001', 'allowed_users': ['10001'],
            'allowed_groups': ['20001'], 'group_admission': 'all_members', 'require_mention': True,
            'group_trigger': {'mode': 'name_or_mention', 'name': '测试实例'},
            'isolation': {'mode': 'strict', 'private_profile': 'default', 'social_profile': 'social',
                          'domain_id': policy['domain_id'], 'policy_revision': policy['policy_revision'],
                          'guest_dm_users': []},
            'runtime': {'mode': 'social_agent', 'contract': 5, 'social_home': str(route['worker_home'])},
        })
        report['shared_group_allowlist'] = args.shared_allowlist
        # Provision a synthetic home channel, as in the reviewed deployment.
        # The first-contact nudge otherwise becomes a real outbound notice and
        # invalidates the ambient generation before the model can run.
        cfg.home_channel = HomeChannel(Platform('napcat'), 'group:20001', 'Synthetic capture target')
        # Never instantiate the network client, including accidental connect.
        def forbidden_transport(*_args, **_kwargs):
            report['real_onebot_connection_attempts'] += 1
            raise AssertionError('Real OneBot transport is forbidden')
        stack.enter_context(patch('hermes_napcat.adapter.OneBotClient', forbidden_transport))
        try:
            runner = GatewayRunner()
            runner.config.multiplex_profiles = False
            runner.config.platforms[Platform('napcat')] = cfg
            authority = await initialize_session_authority(runner, profile_id='default', instance_id='synthetic-live-social')
            adapter = registry.create_adapter('napcat', cfg)
            if adapter is None:
                raise RuntimeError('Real plugin factory rejected reviewed worker/runtime')
            adapter.client = sink
            adapter._wire_plugin_handlers(None)
            assert adapter.participation.active, 'independent social factory did not wire the port'
            assert adapter.participation.policy.participation_groups == adapter.settings.groups
            report['registered_plugins'] = ['hermes-napcat', 'hermes-social']
            report['plugin_roots'] = {'hermes-napcat': str(plugin_root), 'hermes-social': str(social_root)}
            adapter.gateway_runner = runner
            runner.adapters[Platform('napcat')] = adapter
            assert adapter.social_ready(), 'strict worker/gateway readiness failed'
            instrument(stack, runner, adapter, state)
            runner._wire_adapter_handlers(adapter)
            # A bounded public synthetic lead-in, through the real observer.
            seed = adapter.convert_event(raw_message(
                '合成活动甲：用两张色卡练习颜色配对。合成活动乙：用六张编号卡练习数字排序。以上规则仅用于测试。',
                800), observe_only=True)
            adapter.participation.observe(seed)
            selected = CASES if args.case == 'all' else [case for case in CASES if case[0] == args.case]
            for index, (name, text, should_speak) in enumerate(selected, 901):
                if name == 'shared_context':
                    for seed_id, user, observation in (
                        (810, 77777, '合成计分规则R1：步骤丙增加三点，步骤丁增加五点。'),
                        (811, 88888, '规则R1仅用于合成测试，本轮按这份规则计算。')):
                        adapter.participation.observe(adapter.convert_event(
                            raw_message(observation, seed_id, user=user), observe_only=True))
                if name == 'answer_to_question':
                    # The transport-send side of this pair is covered by the
                    # NapCat integration test. Seed only verified, synthetic
                    # observations; the new message still runs the whole
                    # worker/FIFO/source-codec/AIAgent gateway path below.
                    prior = adapter.convert_event(raw_message('合成活动应该采用哪一份方案？', 700 + index),
                                                  observe_only=True)
                    adapter.participation.observe(prior)
                    adapter.participation.record_self('group:20001', '你选择选项甲还是选项乙？',
                                                      str(50000 + index),
                                                      reply_to_message_id=prior.message_id)
                before = {key: len(value) for key, value in state.items()}
                send_before = len(sink.calls)
                await adapter._receive(raw_message(text, index))
                await drain(adapter, authority)
                agents, turns = state['agents'][before['agents']:], state['turns'][before['turns']:]
                actions = sink.calls[send_before:]
                sends = [call for call in actions if call['action'].startswith('send_')]
                final = agents[-1].get('final_response') if agents else None
                actual_text = '\n'.join(plain_text(call['payload']['message']) for call in sends)
                substantial = bool(isinstance(final, str) and not is_intentional_silence_response(final)
                                   and (len(re.findall(r'[\u4e00-\u9fff]', final)) >= 8
                                        or name == 'identity' and TEST_SELF_NAME in final))
                errors = []
                wire_requests = state['requests'][before['requests']:]
                worker_requests = state['worker_requests'][before['worker_requests']:]
                early_gate = (not should_speak and not agents and not turns
                              and not wire_requests and not worker_requests)
                if early_gate:
                    # A no-model negative is valid only if the authenticated
                    # group event was actually observed before routing declined.
                    observed = adapter.participation.context('group:20001')
                    if not any(row['message_id'] == str(index) for row in observed):
                        errors.append('Early silence lacked exact-group observation')
                if not early_gate and (not agents or not turns or not wire_requests):
                    errors.append('No complete real AIAgent/model request')
                if not early_gate and (not agents or not all((a.get('api_calls') or 0) > 0 for a in agents)):
                    errors.append('api_calls must be positive')
                if not early_gate and (not agents or not all(a['class'] == 'run_agent.AIAgent' and a['completed']
                                         and a['model'] == route['model']['default']
                                         and a['provider'] == 'openai-codex' for a in agents)):
                    errors.append('Real AIAgent completed on a different route or failed')
                if not early_gate and (not turns or not all(t['executing'] and t['live_permit'] and t['ambient_valid']
                                        and t['actor'] == '10001' and t['chat_id'] == 'group:20001'
                                        and t['reply_expected'] is False for t in turns)):
                    errors.append('Exact-source ambient permit / FIFO / actor proof failed')
                restored = state['restores'][before['restores']:]
                if not early_gate and (not restored or not all(r['exact_live_source'] and r['permit_active'] for r in restored)):
                    errors.append('Durable source codec did not recover exact live source')
                if not early_gate and not any(w['operation'] == 'prepare' for w in worker_requests):
                    errors.append('Real strict worker prepare missing')
                names = set(agents[-1]['tools']) if agents else set()
                if not early_gate and not {'web_search', 'web_extract', 'terminal', 'read_file'}.issubset(names):
                    errors.append('Formal configured tool surface was reduced')
                if not early_gate and (not wire_requests or not all({'terminal', 'read_file', 'tool_call'}.issubset(r['tools'])
                                                for r in wire_requests)):
                    errors.append('Formal tool definitions missing from actual provider request')
                if any('mobile' in n.lower() for n in names):
                    errors.append('Unexpected mobile tool exposed in synthetic profile')
                if should_speak and (not substantial or not sends or not isinstance(final, str)
                                     or final.strip() not in actual_text):
                    errors.append('Expected substantive Chinese reply delivered to capture sink')
                if name == 'identity' and (not isinstance(final, str) or TEST_SELF_NAME not in final):
                    errors.append('Public configured identity was not used')
                if name == 'shared_context' and (not isinstance(final,str)
                        or not re.search(r'步骤丙[^。；;\n]{0,16}[3三][^。；;\n]{0,4}点', final)
                        or not re.search(r'步骤丁[^。；;\n]{0,16}[5五][^。；;\n]{0,4}点', final)):
                    errors.append('Public rules from other members were not understood')
                if name == 'answer_to_question' and (not isinstance(final, str) or '选项乙' not in final
                                                     or '选项甲还是选项乙' in final):
                    errors.append('Proven short answer did not continue the bot question')
                if not should_speak and (sends or not early_gate and
                                         (not isinstance(final, str) or not is_intentional_silence_response(final))):
                    errors.append('Expected intentional silence and zero sends')
                if CANARY in json.dumps([agents, actions], ensure_ascii=False):
                    errors.append('Synthetic private canary leaked')
                entry = runner.session_store.get_or_create_session(adapter.convert_event(
                    raw_message('read-only source lookup', 10000 + index), observe_only=True).source)
                rows = list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False)
                # Report canonical ledger statuses without assuming a provider's
                # ingress-id spelling; all accumulated admissions must settle.
                ledger = [{'status': r['status'], 'outcome': r['outcome']} for r in rows]
                if not early_gate and (not rows or any(r['status'] != 'terminal' or r['outcome'] != 'completed' for r in rows)):
                    errors.append('Durable ledger not terminal/completed')
                case = {'name': name, 'input': text, 'expect_speech': should_speak,
                        'final_response': final, 'api_calls': sum(a.get('api_calls') or 0 for a in agents),
                        'model': agents[-1]['model'] if agents else None,
                        'provider': agents[-1]['provider'] if agents else None,
                        'early_gate': early_gate,
                        'send_count': len(sends), 'captured_text': actual_text,
                        'tool_count': len(names), 'ledger': ledger, 'passed': not errors, 'errors': errors}
                report['cases'].append(case)
                write_report(root, report)
                print(json.dumps(case, ensure_ascii=False), flush=True)
            report['passed'] = all(case['passed'] for case in report['cases'])
            secret = route['runtime']['api_key'].encode()
            report['no_credentials_written'] = all(secret not in path.read_bytes()
                                                   for path in root.rglob('*') if path.is_file())
            assert report['no_credentials_written'], 'Credential unexpectedly persisted in synthetic artifacts'
        finally:
            report['onebot_actions'] = sink.calls
            if adapter is not None:
                await adapter.disconnect()
            if runner is not None:
                for cached in runner._agent_cache.values():
                    agent = cached[0] if isinstance(cached, tuple) else cached
                    if callable(getattr(agent, 'close', None)):
                        agent.close()
                await runner._session_db.close()
            manager.unload()
            from gateway.status import flush_runtime_status
            flush_runtime_status(timeout=2)


def write_report(root, report):
    # No credentials or raw runtime objects are part of this structure.
    path = root / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    os.chmod(path, 0o600)


def main():
    args = arguments()
    launch(args)
    tmp = Path(os.environ['TMPDIR']).resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix='napcat-social-' + re.sub(r'[^a-zA-Z0-9_-]', '_', args.label) + '-', dir=tmp))
    report = {'label': args.label, 'passed': False, 'cases': [],
              'real_onebot_connection_attempts': 0, 'qq_client_acceptance': False,
              'artifact_root': str(root), 'synthetic_content_only': True,
              'fixture_provenance': FIXTURE_PROVENANCE,
              'credential_mode': 'official readonly owner resolver; token in process memory only'}
    print('ARTIFACT_ROOT=' + str(root), flush=True)
    try:
        # Import host config/providers only AFTER isolation. Their import-time
        # plugin discovery must never activate the owner's real plugin tree.
        home = isolate(root)
        route = read_route(args)
        report.update(configured_model=route['model']['default'], configured_provider=route['model']['provider'],
                      configured_toolsets=route['toolsets'], credential_source=route['credential_source'])
        configure(home, route, args)
        asyncio.run(verify(args, root, route, home, report))
        code = 0 if report['passed'] else 1
    except Exception as exc:
        # Deliberately omit exception values: provider failures can carry request
        # headers. Detailed public trace remains in synthetic profile logs.
        report['blocker_type'] = type(exc).__name__
        report['blocker_location'] = [(frame.filename, frame.lineno, frame.name)
                                      for frame in __import__('traceback').extract_tb(exc.__traceback__)]
        print('BLOCKER_TYPE=' + type(exc).__name__, flush=True)
        code = 2
    finally:
        write_report(root, report)
        print('REPORT=' + str(root / 'report.json'), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
