"""Plan/apply a matched bundle, with exact-file provenance and rollback."""
from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import contextmanager
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

from .host import CommandError, discover_host, environment, helper, interpreter, patch_overlay, run
from .settings import PLUGINS, ENABLED, SKILLS, SetupError, build_settings
from .transaction import Transaction, atomic, digest, no_symlinks


RECEIPT = 'companion-install.json'


def inventory(repo):
    result = {}
    for top in [*(Path('plugins')/n for n in PLUGINS), *(Path('skills')/n for n in SKILLS)]:
        if not (repo/top).is_dir():
            raise SetupError('源码缺少配套目录：'+str(top))
        for path in (repo/top).rglob('*'):
            relative = path.relative_to(repo)
            if set(relative.parts) & {'.git', '__pycache__', '.pytest_cache', '.stage', 'runtime', 'secrets'}:
                continue
            if path.is_symlink():
                raise SetupError('分发源码不能包含符号链接：'+str(relative))
            if path.is_file() and (path.suffix in ('.py', '.md', '.toml', '.yaml', '.yml', '.json', '.txt')
                                   or path.name == 'LICENSE'):
                data = path.read_bytes()
                if path.suffix == '.py':
                    compile(data, str(relative), 'exec')
                result[str(relative)] = data
    return result


def check_targets(home, files, receipt, adopt=False):
    managed = receipt.get('files', {})
    conflicts = []
    for name, data in files.items():
        path = home/name
        no_symlinks(path)
        current = digest(path)
        desired = hashlib.sha256(data).hexdigest()
        if current and current != desired and managed.get(name) != current and not adopt:
            conflicts.append(name)
    if conflicts:
        raise SetupError('目标含未管理的本地修改；审阅后可加 --adopt-existing，并自动备份：'+', '.join(conflicts[:8]))
    # Two worker distributions are ambiguous, and provider ownership must not silently change.
    if (home/'plugins/scoped-memory').exists():
        raise SetupError('已有 plugins/scoped-memory；须先审阅旧 provider 与兄弟数据层的迁移，不能并存两个 worker 源。')


def source_identity(repo):
    sha = run(['git', '-C', repo, 'rev-parse', 'HEAD']).strip()
    dirty = bool(run(['git', '-C', repo, 'status', '--porcelain']).strip())
    origin = run(['git', '-C', repo, 'remote', 'get-url', 'origin']).strip()
    if origin.startswith('git@github.com:'):
        origin = 'https://github.com/'+origin.partition(':')[2]
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.fragment or parsed.query:
        raise SetupError('源码 origin 需要可审阅的 HTTPS Git 地址或 GitHub SSH 地址。')
    netloc = parsed.hostname + (':'+str(parsed.port) if parsed.port else '')
    clean = urlunsplit(('https', netloc, parsed.path, '', ''))
    return sha, dirty, clean


@contextmanager
def install_lock(home):
    no_symlinks(home/'.companion-install.lock')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home/'.companion-install.lock').open('a') as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('另一个 Companion 安装正在进行。') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_json(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def read_token(args):
    if args.token_file:
        path = args.token_file.expanduser()
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise SetupError('token-file 必须是仅当前用户可读写的普通文件（0600）。')
        token = path.read_text().strip()
    else:
        token = os.environ.get('NAPCAT_ACCESS_TOKEN', '')
    if token and (len(token) > 8192 or any(c in token for c in '\r\n\0')):
        raise SetupError('token 不能包含换行或 NUL。')
    return token


def make_plan(args, repo):
    if sys.platform != 'linux':
        raise SetupError('当前自动完整接线支持 Linux/bubblewrap；其他平台只可参考组件文档。')
    home = args.home.expanduser().absolute()
    no_symlinks(home)
    home = home.resolve(strict=False)
    launcher = str(args.hermes or shutil.which('hermes') or '')
    if not launcher:
        raise SetupError('请先安装 Hermes Agent 并完成 hermes setup。')
    host = discover_host(home, args.host_source or os.environ.get('HERMES_SOURCE'), launcher)
    if home == repo or repo in home.parents or home in repo.parents:
        raise SetupError('源码和目标 profile 必须互相独立，不能把安装数据写进源码或反向嵌套。')
    python = interpreter(host, home, args.python or os.environ.get('HERMES_PYTHON'))
    snapshot = helper(python, repo, host, home, 'snapshot')
    live = helper(python, repo, host, home, 'status')
    if not snapshot['sdk_ready'] or not snapshot['bwrap']:
        raise SetupError('缺少兼容的 Hermes SDK 或 bubblewrap；先按 INSTALL.md 准备宿主。')
    if not snapshot['config'].get('model'):
        raise SetupError('Hermes 尚未配置对话模型；先运行 hermes setup，安装器不会代选账号或模型。')
    token = read_token(args)
    if not token and not snapshot['token_present']:
        raise SetupError('需要 NAPCAT_ACCESS_TOKEN 环境变量或 --token-file；不要把密钥写入命令参数。')
    updates, worker, worker_policy, local = build_settings(snapshot['config'], vars(args), home)
    no_symlinks(worker)
    if worker.exists():
        if not worker.is_dir() or worker.stat().st_mode & 0o077:
            raise SetupError('已有 worker 目录必须为 0700。')
        if any(p.name != 'scoped-memory.json' or p.is_symlink() for p in worker.iterdir()):
            raise SetupError('worker 目录含非策略文件；不能复用为只读公共 worker。')
        existing = read_json(worker/'scoped-memory.json')
        if existing and existing != worker_policy:
            raise SetupError('已有 worker 的域或策略不同；请指定新的 --runtime-home，不改写旧域。')
    validated = helper(python, repo, host, home, 'validate', {'plugins': str(repo/'plugins'), 'updates': updates})
    files = inventory(repo)
    previous = read_json(home/RECEIPT)
    check_targets(home, files, previous, args.adopt_existing)
    host_changes, patches = patch_overlay(repo, host, local)
    sha, dirty, source = source_identity(repo)
    targets = [*(home/name for name in files), *(host/name for name in host_changes),
               home/'config.yaml', home/RECEIPT, worker/'scoped-memory.json']
    if token:
        targets.append(home/'.env')
    expected = {str(path): digest(path) for path in targets}
    return dict(home=home, host=host, python=python, launcher=launcher, updates=updates,
                worker=worker, worker_policy=worker_policy, files=files, previous=previous,
                host_changes=host_changes, patches=patches, revision=sha, dirty=dirty, token=token,
                expected=expected, config_before=snapshot['config'], versions=validated.get('versions', {}), source=source,
                gateway_pid_before=live.get('pid'))


def public_plan(plan):
    entries = plan['updates']['plugins.entries']
    memory = entries['hermes-life-memory']['settings']['memory']
    social = entries['hermes-social']['settings']['channels']['napcat']
    extra = plan['updates']['platforms.napcat']['extra']
    friend = extra.get('friend_dm') or {}
    return {'status': 'planned', 'home': str(plan['home']), 'host': str(plan['host']),
            'source': plan['source'],
            'source_revision': plan['revision'], 'source_dirty': plan['dirty'],
            'plugins': list(PLUGINS), 'skills': list(SKILLS)+['hermes-social:social-participation'],
            'versions': plan.get('versions', {}),
            'configuration': {'group_admission': extra['group_admission'], 'allowed_group_count': len(extra['allowed_groups']),
                              'social_enabled': social['enabled'], 'idle_target_count': len(social['targets']),
                              'friend_dm_enabled': bool(friend.get('enabled',
                                                        bool(extra['isolation'].get('guest_dm_users')))),
                              'friend_admission': friend.get('admission', 'explicit'),
                              'friend_user_count': len(set(friend.get('users', []))
                                                       | set(extra['isolation'].get('guest_dm_users', []))),
                              'friend_media': bool(friend.get('media', False)),
                              'friend_group_members': bool(friend.get('allow_group_members', False)),
                              'shared_persona': bool(extra.get('shared_identity', {}).get('enabled', False)),
                              'shared_daily_memory': bool(extra.get('shared_identity', {}).get('daily_memory', False)),
                              'dialogue_proactive': bool(social.get('dialogue', {}).get('proactive', False)),
                              'activities_enabled': bool(social.get('activities', {}).get('enabled', False)),
                              'reading_source_count': sum(len(urls) for urls in social.get('activities', {}).get('sources', {}).values()),
                              'learning_enabled': bool(social.get('learning', {}).get('enabled', False)),
                              'memory_enabled': memory['enabled'], 'memory_group_count': len(memory['groups']),
                              'conversation_enabled': bool(memory.get('conversation', {}).get('enabled')),
                              'request_orchestration': bool(social.get('dialogue', {}).get('orchestration')),
                              'semantic_methods': bool(social.get('learning', {}).get('semantic')),
                              'explicit_semantic_memory': bool(memory.get('explicit_memory', {}).get('enabled')),
                              'friend_memory': bool(memory.get('friend_dm')),
                              'agreements_proactive': bool(memory.get('agreements', {}).get('proactive'))},
            'distribution_files': len(plan['files']), 'host_patches': plan['patches'],
            'host_files_to_change': sorted(plan['host_changes']),
            'config_keys': list(plan['updates']), 'worker_home': str(plan['worker']),
            'credentials': 'present; value never included', 'private_memory_provider': 'preserved',
            'restart': 'only with --restart; uses the official self-restart guard'}


def apply_plan(plan, args, repo):
    home, host, python = plan['home'], plan['host'], plan['python']
    env = environment(home, host)
    with install_lock(home):
        # Recheck under the lock; a stale plan never overwrites an edit made in between.
        for name, before in plan['expected'].items():
            no_symlinks(Path(name))
            if digest(Path(name)) != before:
                raise SetupError('计划生成后目标已改变，请重新检查：'+name)
        check_targets(home, plan['files'], read_json(home/RECEIPT), args.adopt_existing)
        tx = Transaction(home)
        try:
            for name, data in plan['host_changes'].items():
                tx.write(host/name, data, 0o644)
            for name, data in plan['files'].items():
                tx.write(home/name, data, (repo/name).stat().st_mode & 0o777)
            plan['worker'].mkdir(parents=True, exist_ok=True, mode=0o700)
            tx.write(plan['worker']/'scoped-memory.json',
                     json.dumps(plan['worker_policy'], indent=2).encode())
            metadata_path = home/'plugins/.install-metadata.json'
            no_symlinks(metadata_path)
            with (metadata_path.with_name(metadata_path.name+'.lock')).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                metadata = read_json(metadata_path)
                for name in PLUGINS:
                    metadata[name] = dict(pinned=True, revision=plan['revision'],
                                          source=plan['source']+'#plugins/'+name)
                tx.write(metadata_path, json.dumps(metadata, indent=2).encode())
            tx.phase = 'configuration'
            tx.save()
            config = home/'config.yaml'
            tx.remember(config)
            expected_config = deepcopy(plan['config_before'])
            for key, value in plan['updates'].items():
                if helper(python, repo, host, home, 'snapshot')['config'] != expected_config:
                    raise SetupError('配置被其他写入者修改，已保留该变化。')
                target = expected_config
                parts = key.split('.')
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                if target.get(parts[-1]) == value:
                    continue
                target[parts[-1]] = value
                run([plan['launcher'], 'config', 'set', key, json.dumps(value, ensure_ascii=False)], env=env)
                if helper(python, repo, host, home, 'snapshot')['config'] != expected_config:
                    raise SetupError('CLI 写入后的配置包含未知差异，未将其认领为本轮写入。')
                tx.observe(config)
            if plan['token']:
                env_file = home/'.env'
                tx.remember(env_file)
                before = digest(env_file)
                saved = helper(python, repo, host, home, 'token', {'token': plan['token']})
                if saved['before'] != before or saved['after'] != digest(env_file):
                    raise SetupError('凭据文件出现并发修改，未将其认领为本轮写入。')
                tx.observe(env_file)
            actual = helper(python, repo, host, home, 'snapshot')['config']
            for key, wanted in plan['updates'].items():
                value = actual
                for part in key.split('.'):
                    value = value[part]
                if value != wanted:
                    raise SetupError('配置写后读回不一致：'+key)
            tx.phase = 'dependencies'
            tx.save()
            run([plan['launcher'], 'pm', 'install', 'python'], env=env, timeout=1800)
            python = interpreter(host, home, args.python)
            helper(python, repo, host, home, 'validate', {'plugins': str(home/'plugins'), 'updates': plan['updates']})
            helper(python, repo, host, home, 'worker', {'extra': plan['updates']['platforms.napcat']['extra']})
            for name, data in plan['files'].items():
                if (home/name).read_bytes() != data:
                    raise SetupError('安装读回不一致：'+name)
            receipt = {'schema_version': 1, 'source': plan['source'], 'revision': plan['revision'], 'source_dirty': plan['dirty'],
                       'files': {name: hashlib.sha256(data).hexdigest() for name, data in plan['files'].items()},
                       'backup': str(tx.backup), 'status': 'installed', 'updated_at': int(time.time()),
                       'gateway_pid_before': plan.get('gateway_pid_before'), 'needs_restart': True}
            tx.write(home/RECEIPT, json.dumps(receipt, indent=2).encode())
            tx.phase = 'installed'
            tx.save()
        except Exception as error:
            failed_phase = tx.phase
            detail = str(error) if isinstance(error, SetupError) else type(error).__name__
            if isinstance(error, CommandError):
                atomic(tx.backup/'command-error.txt', error.output.encode(), 0o600)
            conflicts = tx.rollback()
            raise SetupError('安装未完成（'+failed_phase+'：'+detail+'），已尝试恢复本轮文件；备份：'+str(tx.backup)
                             +('；检测到并发修改，未覆盖：'+', '.join(conflicts) if conflicts else '')
                             +'。受管依赖由 Hermes PM 管理，未直接回写其内部状态。') from None
    result = public_plan(plan)
    result.update(status='restart_required', backup=str(tx.backup), worker_verified=True)
    if not args.restart:
        return result
    try:
        before = helper(python, repo, host, home, 'status')
        verb = 'restart' if before['running'] else 'ensure'
        run([plan['launcher'], 'gateway', verb], env=env, timeout=120)
        for _ in range(15):
            status = helper(python, repo, host, home, 'status')
            fresh = not before['running'] or status['pid'] != before['pid']
            bound = status.get('home') == str(home)
            if status['running'] and status['napcat'] == 'connected' and fresh and bound:
                with install_lock(home):
                    if read_json(home/RECEIPT) != receipt:
                        raise SetupError('激活期间安装记录发生变化，未认领其他安装。')
                    if any(digest(home/name) != expected for name, expected in receipt['files'].items()):
                        raise SetupError('激活期间安装文件发生变化。')
                    receipt.update(status='connected', gateway_pid=status['pid'], needs_restart=False)
                    tx.write(home/RECEIPT, json.dumps(receipt, indent=2).encode())
                    tx.phase = 'connected'
                    tx.save()
                result.update(status='connected', gateway_pid=status['pid'])
                return result
            time.sleep(1)
    except SetupError:
        pass
    # A tool inside the gateway cannot restart its own parent; never detach or schedule a bypass.
    result['activation_command'] = 'hermes gateway restart'
    result['activation_note'] = '在目标 profile 的独立终端执行；未使用强制中断、分离进程或自重启绕过。'
    return result


def parser():
    p = argparse.ArgumentParser(description='Hermes Companion 成套安装；默认只输出计划。')
    p.add_argument('--home', type=Path, default=Path(os.environ.get('HERMES_HOME', Path.home()/'.hermes')))
    p.add_argument('--host-source', type=Path)
    p.add_argument('--hermes', type=Path)
    p.add_argument('--python', type=Path)
    for name in ('self-id', 'owner-id', 'groups', 'memory-groups', 'name', 'ws-url', 'runtime-home', 'idle-groups'):
        p.add_argument('--'+name)
    p.add_argument('--features', choices=('basic', 'local-memory'))
    p.add_argument('--share-persona', action='store_true', help='显式允许群回合读取当前 profile 的 SOUL.md')
    p.add_argument('--share-daily-memory', action='store_true',
                   help='显式允许私密侧筛选内置 MEMORY/USER 中的非敏感日常经历；含共享人格，不加载私聊原文')
    p.add_argument('--friend-users', help='逗号分隔的独立普通好友白名单；显式启用受限私聊，不加入 allowed_users')
    p.add_argument('--friend-media', action='store_true', help='显式允许已获准好友复用既有媒体解析权限；不自动开云模态')
    p.add_argument('--friend-group-members', action='store_true',
                   help='显式允许白名单群当前成员中的已核验普通好友受限私聊；不自动添加主动目标')
    p.add_argument('--idle-friends', help='逗号分隔的主动私聊好友；必须属于独立显式好友名单，不自动授权群友')
    p.add_argument('--complete-social', action='store_true',
                   help='显式启用模型社交、话题续接、阅读活动及反馈试验；不增加目标、来源或原始对话记忆')
    p.add_argument('--enhance-social', action='store_true',
                   help='显式启用多人事项编排、本人核对的跨天记忆和经验证的方法学习；保留原文采集、阅读来源及主动范围')
    p.add_argument('--reading-sources', type=Path,
                   help='JSON 文件：精确 group:<id>/dm:<id> 映射到公开 HTTPS URL 列表，替换旧阅读来源')
    p.add_argument('--group-admission', choices=('allowed_users', 'all_members'), help='显式调整成员准入；升级默认保留')
    p.add_argument('--proactive-agreements', action='store_true')
    p.add_argument('--enable-social', action='store_true', help='显式恢复既有停用的 social；普通升级保留停用选择')
    p.add_argument('--follow-allowed-groups', action='store_true', help='显式移除旧参与测试范围，跟随通道群白名单')
    p.add_argument('--token-file', type=Path)
    p.add_argument('--adopt-existing', action='store_true', help='备份后接管经过审阅的旧安装差异')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--check', action='store_true', help='只读检查安装文件、worker 与当前网关状态')
    p.add_argument('--restart', action='store_true', help='通过官方 CLI 重载；不绕过自重启保护')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    repo = Path(__file__).resolve().parents[2]
    try:
        plan = make_plan(args, repo)
        if args.check:
            if args.apply:
                raise SetupError('--check 与 --apply 不能同时使用。')
            changed = [name for name, data in plan['files'].items()
                       if digest(plan['home']/name) != hashlib.sha256(data).hexdigest()]
            configuration = []
            for key, wanted in plan['updates'].items():
                actual = plan['config_before']
                for part in key.split('.'):
                    actual = actual.get(part, {}) if isinstance(actual, dict) else None
                if actual != wanted:
                    configuration.append(key)
            if changed or plan['host_changes'] or configuration:
                state = 'update_required' if changed or plan['host_changes'] else 'configuration_required'
                print(json.dumps({'status': state, 'files': changed, 'config_keys': configuration,
                                  'host_files': sorted(plan['host_changes'])}, ensure_ascii=False, indent=2))
                return 2
            helper(plan['python'], repo, plan['host'], plan['home'], 'worker',
                   {'extra': plan['updates']['platforms.napcat']['extra']})
            result = helper(plan['python'], repo, plan['host'], plan['home'], 'status')
            ready = result['running'] and result['napcat'] == 'connected' and result.get('home') == str(plan['home'])
            receipt = read_json(plan['home']/RECEIPT)
            if not receipt:
                ready = False
            elif receipt.get('needs_restart'):
                ready = ready and result['pid'] != receipt.get('gateway_pid_before')
            else:
                ready = ready and result['pid'] == receipt.get('gateway_pid')
            print(json.dumps({'status': 'connected' if ready else 'restart_required',
                              'gateway_pid': result['pid'], 'worker_verified': True}, ensure_ascii=False))
            return 0 if ready else 3
        result = apply_plan(plan, args, repo) if args.apply else public_plan(plan)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if args.apply and result['status'] != 'connected' else 0
    except (SetupError, OSError, ValueError) as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error) if isinstance(error, SetupError)
                          else type(error).__name__}, ensure_ascii=False))
        return 2
