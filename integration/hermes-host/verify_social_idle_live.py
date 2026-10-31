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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--host', type=Path, required=True)
    parser.add_argument('--owner-home', type=Path, required=True)
    parser.add_argument('--social-plugin-root', type=Path, required=True)
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
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
