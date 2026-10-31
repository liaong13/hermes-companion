"""Private JSON pipe to the selected Hermes interpreter; never emits credential values."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path/'__init__.py', submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    host, operation = Path(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, str(host))
    from hermes_constants import get_hermes_home
    from hermes_yaml import safe_load
    from agent.secret_scope import load_env_file
    home = Path(get_hermes_home()).resolve()
    payload = json.load(sys.stdin)
    if operation == 'snapshot':
        config = safe_load((home/'config.yaml').read_text()) if (home/'config.yaml').is_file() else {}
        from hermes_cli.plugins import PluginContext
        required = ('register_platform', 'register_platform_handler', 'register_skill', 'on_unload', 'spawn_task')
        try:
            version = importlib.metadata.version('aiohttp')
            numbers = tuple(map(int, version.split('.')[:2]))
            dependency = (3, 12) <= numbers < (4, 0)
        except (importlib.metadata.PackageNotFoundError, ValueError):
            dependency = False
        return {'config': config or {}, 'token_present': bool(load_env_file(home/'.env').get('NAPCAT_ACCESS_TOKEN')),
                'sdk_ready': all(callable(getattr(PluginContext, name, None)) for name in required),
                'dependency_ready': dependency, 'bwrap': bool(shutil.which('bwrap')), 'python': sys.version.split()[0]}
    if operation == 'validate':
        root = Path(payload['plugins'])
        channel = load('_companion_setup_napcat', root/'hermes-napcat')
        people = load('_companion_setup_life', root/'hermes-life-memory')
        social = load('_companion_setup_social', root/'hermes-social')
        extra = payload['updates']['platforms.napcat']['extra']
        channel.adapter.Settings(extra)
        entries = payload['updates']['plugins.entries']
        people.service.settings(entries['hermes-life-memory']['settings']['memory'])
        social.SocialPolicy(entries['hermes-social']['settings']['channels']['napcat'],
                            owner_id=extra['owner_id'], groups=frozenset(extra['allowed_groups']))
        versions = {}
        for name in ('hermes-napcat', 'hermes-life-memory', 'hermes-social', 'hermes-scoped-memory'):
            manifest = safe_load((root/name/'plugin.yaml').read_text())
            versions[name] = str(manifest['version'])
            project = root/name/'pyproject.toml'
            if project.is_file() and tomllib.loads(project.read_text())['project']['version'] != versions[name]:
                raise ValueError('package and plugin manifest versions differ')
        return {'valid': True, 'versions': versions}
    if operation == 'token':
        from hermes_cli.config import save_env_value
        token = payload['token']
        if not isinstance(token, str) or not token or any(c in token for c in '\r\n\0'):
            raise ValueError('invalid credential')
        path = home/'.env'
        existed = path.exists()
        before = path.read_bytes() if existed else b''
        if load_env_file(path).get('NAPCAT_ACCESS_TOKEN') != token:
            save_env_value('NAPCAT_ACCESS_TOKEN', token)
        after = path.read_bytes()
        if load_env_file(home/'.env').get('NAPCAT_ACCESS_TOKEN') != token:
            raise ValueError('credential persistence failed')
        from hermes_cli.config import _env_line_defines_key
        def other_lines(data):
            return [line for line in data.decode().splitlines()
                    if not _env_line_defines_key(line, 'NAPCAT_ACCESS_TOKEN')]
        if other_lines(before) != other_lines(after):
            raise ValueError('concurrent credential change')
        return {'saved': True, 'before': hashlib.sha256(before).hexdigest() if existed else None,
                'after': hashlib.sha256(after).hexdigest()}
    if operation == 'worker':
        root = home/'plugins'
        channel = load('_companion_worker_probe', root/'hermes-napcat')
        from _companion_worker_probe.runtime_bridge import SandboxedWorker
        extra = payload['extra']
        policy = extra['isolation']
        worker = SandboxedWorker(Path(extra['runtime']['social_home']), domain_id=policy['domain_id'],
                                 policy_revision=policy['policy_revision'], plugin_root=root, readonly_home=True)
        route = dict(domain_id=policy['domain_id'], policy_revision=policy['policy_revision'],
                     profile='social', user_id=extra['owner_id'], chat_id='group:'+extra['allowed_groups'][0],
                     chat_type='group', message_id='1', platform='napcat', role='member')
        result = asyncio.run(worker.request({'operation': 'probe', 'route': route}))
        if result.get('host_contract') != 4 or result.get('tools') != []:
            raise ValueError('worker contract mismatch')
        return {'worker_verified': True}
    if operation == 'status':
        from gateway.control_socket import query_gateway_control
        result = query_gateway_control(home, 'status', timeout=3)
        return {'running': bool(result and result.get('gateway_state') == 'running'),
                'pid': result.get('pid') if result else None,
                'home': result.get('hermes_home') if result else None,
                'napcat': result.get('platforms', {}).get('napcat', {}).get('state') if result else None}
    raise ValueError('unknown operation')


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as error:
        # Exception text, config and request payloads may contain secrets.
        print(json.dumps({'error': type(error).__name__}))
        raise SystemExit(2)
