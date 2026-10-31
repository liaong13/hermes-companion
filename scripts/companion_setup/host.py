"""Read-only host discovery and patch composition in a disposable overlay."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .settings import SetupError


PATCHES = ('napcat-public-turn', 'napcat-social-participation', 'napcat-ambient-quiet',
           'napcat-local-memory-context')


class CommandError(SetupError):
    def __init__(self, message, output):
        super().__init__(message)
        self.output = output


def run(command, *, cwd=None, env=None, input=None, timeout=120):
    try:
        result = subprocess.run(list(map(str, command)), cwd=cwd, env=env, input=input,
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError('命令无法完成：'+type(exc).__name__) from None
    if result.returncode:
        # The child may have read credentials. Keep its output out of agent logs.
        raise CommandError(f'命令退出 {result.returncode}：{Path(str(command[0])).name}',
                           result.stdout+'\n'+result.stderr)
    return result.stdout


def environment(home, host):
    return dict(os.environ, HERMES_HOME=str(home), HERMES_SOURCE=str(host),
                PYTHONPATH=str(host), PYTHONDONTWRITEBYTECODE='1', HERMES_DISABLE_LAZY_INSTALLS='1')


def discover_host(home, explicit, launcher):
    # Always bind CLI and patch ownership, including when an explicit path was supplied.
    result = run([launcher, '--print-runtime-command'], env=environment(home, home))
    try:
        command = json.loads(result)
        tree = ast.parse(command[command.index('-c')+1])
        roots = [node.args[1].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and ast.unparse(node.func) == 'sys.path.insert' and len(node.args) == 2
                 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)]
        root = Path(roots[0]).resolve(strict=True)
    except (ValueError, IndexError, KeyError, TypeError, SyntaxError):
        raise SetupError('无法从 Hermes launcher 解析绑定宿主；请使用兼容的安装 launcher。') from None
    if explicit and Path(explicit).expanduser().resolve(strict=True) != root:
        raise SetupError('--host-source/HERMES_SOURCE 与 Hermes launcher 不属于同一宿主。')
    if not (root/'gateway').is_dir() or not (root/'hermes_yaml.py').is_file():
        raise SetupError('此入口需要支持 hermes_yaml 的 Hermes 源码安装；请提供兼容的 --host-source。')
    return root


def interpreter(host, home, explicit=None):
    if explicit:
        python = Path(explicit).expanduser().absolute()
    else:
        script = ('from pathlib import Path; from pm.environments import selected_venv, venv_python; '
                  'print(venv_python(selected_venv(Path(__import__("sys").argv[1]))))')
        python = Path(run([sys.executable, '-B', '-c', script, host], env=environment(home, host)).strip())
    if not python.is_file():
        raise SetupError('找不到 Hermes 实际解释器；请提供 --python，不要安装到系统 Python。')
    return python


def helper(python, repo, host, home, operation, payload=None):
    output = run([python, '-B', repo/'scripts/companion_setup/runtime.py', host, operation],
                 env=environment(home, host), input=json.dumps(payload or {}), timeout=180)
    try:
        return json.loads(output)
    except ValueError:
        raise SetupError('宿主检查没有返回有效 JSON。') from None


def patch_overlay(repo, host, local_memory):
    patches = {name: repo/'integration/hermes-host'/(name+'.patch') for name in PATCHES}
    targets = set()
    for patch in patches.values():
        for line in patch.read_text().splitlines():
            if line.startswith('+++ b/'):
                name = line[6:]
                if Path(name).is_absolute() or '..' in Path(name).parts:
                    raise SetupError('补丁包含越界目标。')
                targets.add(name)
    with tempfile.TemporaryDirectory(prefix='companion-host-plan-') as directory:
        overlay = Path(directory)
        original = {}
        for name in targets:
            path = host/name
            if path.is_symlink():
                raise SetupError('宿主补丁目标不能是符号链接：'+name)
            if path.exists():
                original[name] = path.read_bytes()
                target = overlay/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original[name])
        applied = set()
        def check(name, reverse=False):
            command = ['git', 'apply', '--check', *(['--reverse'] if reverse else []), str(patches[name])]
            return subprocess.run(command, cwd=overlay, capture_output=True).returncode == 0
        # Later increments overlap earlier hunks; unwrap the entire stack first.
        for name in reversed(PATCHES):
            if check(name, True):
                run(['git', 'apply', '--reverse', patches[name]], cwd=overlay)
                applied.add(name)
        needed = set(PATCHES if local_memory else PATCHES[:-1]) | applied
        for name in PATCHES:
            if name not in needed:
                continue
            if not check(name):
                raise SetupError('宿主补丁不能无冲突组合：'+name+'；未修改目标宿主。')
            run(['git', 'apply', patches[name]], cwd=overlay)
        changes = {name: (overlay/name).read_bytes() for name in targets
                   if (overlay/name).exists() and (overlay/name).read_bytes() != original.get(name)}
        for name, content in changes.items():
            if name.endswith('.py'):
                compile(content, name, 'exec')
        return changes, {name: ('already_applied' if name in applied else 'will_apply')
                         for name in PATCHES if name in needed}
