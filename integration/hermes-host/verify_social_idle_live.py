#!/usr/bin/env python3
"""Real independent social-plugin LLM facade check; no QQ or state migration.

Only supplied public test context is used. No controller, adapter, private
history or send capability is created. This is NOT idle delivery acceptance.
"""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace


async def verify_participation(ctx, module_name, report):
    """Authored public fixtures; this probes selection, not gateway delivery."""
    from importlib import import_module
    context = import_module(module_name+'.context')
    participation = import_module(module_name+'.participation')
    scope = context.ContextScope('/tmp/synthetic-selection-scope', 'napcat', 'group:20001')
    now = time.time()
    def event(text, mid, user, name):
        source = SimpleNamespace(chat_id='group:20001', chat_type='group', user_id=user, message_id=mid)
        return SimpleNamespace(source=source, text=text, message_id=mid, user_id=user,
                               user_name=name, bot_id='90001')
    def row(text, mid, user, age, *, role='member', reply_to=None):
        return context.ContextRecord(scope, now-age, role, '合成成员', text, mid,
                                     sender_id=user, reply_to_message_id=reply_to)
    cases = [
        ('earlier_sharing', [event('我完成了纸模型的新折叠示意图，发现对称折法更省材料，想听听大家怎么看。',
                                  'share', '10001', '合成成员甲'),
                             event('哈哈', 'echo', '10002', '合成成员乙')], [], 'c0'),
        ('other_member', [event('合成成员乙，请把材料一交给合成成员丙。这条仅请合成成员乙处理。',
                               'directed', '10001', '合成成员甲')], [], None),
        ('interleaved_short_answer', [event('选项乙', 'short', '10001', '合成成员甲')],
         [row('合成任务采用哪份方案？', 'question', '10001', 90),
          row('你选择选项甲还是选项乙？', 'answer', '', 85, role='self', reply_to='question'),
          row('我在整理另一批合成材料。', 'other', '10002', 10)], 'c0'),
    ]
    results = []
    for name, events, history, expected in cases:
        frame = participation.ParticipationFrame(events, history, scope)
        started = time.monotonic()
        response = await ctx.llm.acomplete(messages=frame.messages, max_tokens=2048,
                                          timeout=15, purpose='social_selection_acceptance')
        elapsed = time.monotonic()-started
        choice, assessments = frame.parse(response.text)
        selected = next((ref for ref, item in frame.events.items() if choice is not None and item is choice.event), None)
        passed = selected == expected and elapsed < 20
        if name == 'other_member':
            passed = passed and assessments[0].audience == 'member'
        if name == 'interleaved_short_answer':
            passed = passed and choice is not None and choice.topic_message_id in {'question', 'answer'}
        result = dict(name=name, passed=passed, selected=selected, expected=expected,
                      actual_response=json.loads(response.text), seconds=round(elapsed, 3),
                      provider=response.provider, model=response.model, total_tokens=response.usage.total_tokens)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        report['cases'] = results
    report.update(passed=all(case['passed'] for case in results),
                  scope='real public selection facade only; synthetic fixtures; no controller or delivery')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--host', type=Path, required=True)
    parser.add_argument('--owner-home', type=Path, required=True)
    parser.add_argument('--social-plugin-root', type=Path, required=True)
    parser.add_argument('--participation', action='store_true', help='probe three authored public participation decisions')
    parser.add_argument('--managed', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.live:
        raise SystemExit('Refusing a real model call without --live.')
    if not args.managed:
        sys.path.insert(0, str(args.host.resolve(strict=True)))
        from hermes_cli._launchers import runtime_command
        code = f"import runpy; runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')"
        raise SystemExit(subprocess.call(runtime_command(args.host, args=[*sys.argv[1:], '--managed'], code=code)))
    os.umask(0o077)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.session_context import set_session_vars
    root = args.social_plugin_root.resolve(strict=True)
    module_name = 'hermes_social_idle_acceptance'
    spec = importlib.util.spec_from_file_location(module_name, root/'__init__.py', submodule_search_locations=[str(root)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    output = Path(tempfile.mkdtemp(prefix='social-idle-facade-', dir=os.environ['TMPDIR'])) / 'report.json'
    report = {'passed': False, 'qq_delivery_tested': False, 'controllers_created': 0,
              'scope': 'real plugin LLM facade only; no QQ or idle delivery',
              'social_plugin_root': str(root)}
    home = args.owner_home.resolve(strict=True)
    token = set_hermes_home_override(home)
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='hermes-social', kind='standalone', source='user', path=str(root)), manager)
    try:
        module.register(ctx)
        # This intentionally does NOT run the registered platform factory.
        # No social counters, legacy-state migration or live QQ transport is used.
        set_session_vars(platform='napcat', source='napcat', chat_id='group:20001',
                         chat_type='group', user_id='', message_id='', async_delivery=False,
                         session_history_delivery='', cwd='')
        from importlib import import_module
        prompts = import_module(module_name + '.prompts')
        messages = prompts.idle_messages('group:20001', [
            {'role': 'member', 'display_name': '合成群友', 'message_id': '1',
             'text': '刚才在聊开放世界游戏，大家更喜欢探索还是跟主线呢？'}])
        async def call():
            return await ctx.llm.acomplete(messages=messages, max_tokens=2048, timeout=60,
                                           purpose='social_idle_acceptance')
        if args.participation:
            asyncio.run(verify_participation(ctx, module_name, report))
            assert len(module._controllers) == 0, 'selection probe must not create a controller'
            print(json.dumps({'passed': report['passed'], 'report': str(output)}, ensure_ascii=False))
            return 0 if report['passed'] else 1
        response = asyncio.run(call())
        assert len(module._controllers) == 0, 'facade probe must not create a controller'
        result = json.loads(response.text)
        assert (isinstance(result, dict) and set(result) == {'speak', 'text'}
                and type(result['speak']) is bool and isinstance(result['text'], str)
                and len(result['text']) <= 1000), 'invalid real idle response'
        report.update(passed=True, provider=response.provider, model=response.model,
                      actual_response=result, total_tokens=response.usage.total_tokens)
        print(json.dumps({**report, 'report': str(output)}, ensure_ascii=False))
    except Exception as exc:
        report['blocker_type'] = type(exc).__name__
        print(json.dumps({'passed': False, 'blocker_type': type(exc).__name__, 'report': str(output)}))
    finally:
        manager.unload()
        reset_hermes_home_override(token)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        output.chmod(0o600)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
