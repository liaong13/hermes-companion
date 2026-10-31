"""Build a scoped configuration without inheriting maintainer identities or secrets."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


PLUGINS = ('hermes-scoped-memory', 'hermes-life-memory', 'hermes-social', 'hermes-napcat')
ENABLED = ('hermes-life-memory', 'hermes-social', 'hermes-napcat')
SKILLS = ('hermes-companion-setup', 'napcat-qq')


class SetupError(RuntimeError):
    pass


def ids(value, label):
    values = value.split(',') if isinstance(value, str) else value
    if not isinstance(values, list) or not values:
        raise SetupError(f'需要 {label}；通过参数或已有 NapCat 配置提供。')
    if any(not isinstance(v, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', v) for v in values):
        raise SetupError(f'{label} 必须是明确的数字 ID。')
    return list(dict.fromkeys(values))


def reading_sources(path):
    """Only caller-supplied exact mappings; never introduce example URLs."""
    def mapping(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate source scope')
            result[key] = value
        return result
    try:
        with Path(path).expanduser().open('rb') as stream:
            content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError('source mapping too large')
        sources = json.loads(content.decode('utf-8'), object_pairs_hook=mapping)
        if (not isinstance(sources, dict) or len(sources) > 16
                or any(not isinstance(chat, str) or not re.fullmatch(r'(?:group|dm):[1-9][0-9]{0,19}', chat)
                       or not isinstance(urls, list) or not 1 <= len(urls) <= 16
                       or any(not isinstance(url, str) or not url or len(url) > 2048 for url in urls)
                       or len(set(urls)) != len(urls) for chat, urls in sources.items())):
            raise ValueError('invalid source mapping')
        return sources
    except (OSError, UnicodeError, ValueError, TypeError):
        raise SetupError('--reading-sources 需要不超过 64 KiB 的 JSON 对象：精确 chat ID -> 不重复的公开 HTTPS URL 列表。') from None


def build_settings(current, options, home):
    """Return exact CLI updates plus a worker policy; leave unrelated settings alone."""
    cfg = deepcopy(current)
    existing_channel = 'napcat' in cfg.get('platforms', {})
    platform = cfg.get('platforms', {}).get('napcat', {})
    extra = deepcopy(platform.get('extra', {}))
    owner = ids([options.get('owner_id') or extra.get('owner_id')], 'owner_id')[0]
    account = ids([options.get('self_id') or extra.get('self_id')], 'self_id')[0]
    if owner == account:
        raise SetupError('owner_id 与机器人 self_id 不能相同。')
    groups = ids(options.get('groups') or extra.get('allowed_groups'), '允许群 groups')
    url = options.get('ws_url') or extra.get('ws_url', 'ws://127.0.0.1:13011')
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'ws' and parsed.hostname in ('127.0.0.1', '::1')
                 and parsed.port is not None and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and parsed.path in ('', '/'))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SetupError('ws_url 必须是带端口的字面量回环 ws 地址。')
    default_trigger = {'mode': 'mention'} if existing_channel else {'mode': 'name_or_mention', 'name': '伙伴'}
    trigger = deepcopy(extra.get('group_trigger', default_trigger))
    if options.get('name'):
        trigger = {'mode': 'name_or_mention', 'name': options['name']}
    policy = deepcopy(extra.get('isolation', {}))
    policy.setdefault('domain_id', 'napcat-public')
    policy.setdefault('policy_revision', 1)
    policy.setdefault('guest_dm_users', [])
    policy.update(mode='strict', private_profile='default', social_profile='social')
    runtime = deepcopy(extra.get('runtime', {}))
    key = hashlib.sha256(str(home).encode()).hexdigest()[:12]
    default_worker = Path.home()/'.local/share/hermes-companion'/key/'worker'
    worker = Path(options.get('runtime_home') or runtime.get('social_home') or default_worker).expanduser().absolute()
    if any(p.is_symlink() for p in (worker, *worker.parents)):
        raise SetupError('worker 路径及父目录不能是符号链接。')
    worker = worker.resolve(strict=False)
    home = home.resolve(strict=False)
    if '.hermes' in worker.parts or worker == home or home in worker.parents or worker in home.parents:
        raise SetupError('worker 运行目录必须位于私人 Hermes home 之外，且路径不能含 .hermes。')
    runtime.update(mode='social_agent', contract=5, social_home=str(worker))
    users = [owner if user == extra.get('owner_id') else user for user in extra.get('allowed_users', [])]
    users = list(dict.fromkeys([*users, owner]))
    guests = policy['guest_dm_users']
    if (not isinstance(guests, list) or any(not isinstance(user, str)
            or not re.fullmatch(r'[1-9][0-9]{0,19}', user) or user in {owner, account} for user in guests)
            or not set(guests).issubset(users)):
        raise SetupError('旧 guest_dm_users 必须仍是既有 allowed_users 中明确允许的非主人好友；不会自动扩大私人白名单。')
    friend = deepcopy(extra.get('friend_dm'))
    if friend is not None and not isinstance(friend, dict):
        raise SetupError('已有 friend_dm 必须是明确配置对象。')
    if options.get('friend_users') is not None:
        permitted = ids(options['friend_users'], '受限好友 friend_users')
        if set(permitted) & {owner, account}:
            raise SetupError('friend_users 只能包含非主人、非机器人账号。')
        friend = {} if friend is None else friend
        friend.update(enabled=True, admission='explicit', users=permitted)
    if options.get('friend_group_members'):
        friend = {} if friend is None else friend
        friend.update(enabled=True, allow_group_members=True)
    if options.get('friend_media'):
        if friend is None and guests:
            friend = dict(enabled=True, admission='explicit', users=[])
        if not friend or friend.get('enabled') is not True:
            raise SetupError('--friend-media 需要既有启用的好友配置或同时指定 --friend-users；不会自动开放好友。')
        friend['media'] = True
    if friend is not None:
        extra['friend_dm'] = friend
    friend_enabled = bool(friend.get('enabled', False)) if friend is not None else bool(guests)
    friend_users = set(guests)
    if friend is not None:
        configured_users = friend.get('users', [])
        if (not isinstance(configured_users, list) or any(not isinstance(user, str)
                or not re.fullmatch(r'[1-9][0-9]{0,19}', user) or user in {owner, account} for user in configured_users)):
            raise SetupError('friend_dm.users 必须是明确允许的普通好友数字 ID。')
        friend_users.update(configured_users)
    admission = options.get('group_admission') or extra.get('group_admission',
                                                        'allowed_users' if existing_channel else 'all_members')
    extra.update(ws_url=url, self_id=account, owner_id=owner, allowed_users=users,
                 allowed_groups=groups, require_mention=True, typing=False,
                 group_admission=admission, group_trigger=trigger, isolation=policy, runtime=runtime)
    shared = deepcopy(extra.get('shared_identity', {}))
    if not isinstance(shared, dict):
        raise SetupError('已有 shared_identity 必须是明确配置对象。')
    if options.get('share_persona') or options.get('share_daily_memory'):
        shared['enabled'] = True
    if options.get('share_daily_memory'):
        shared['daily_memory'] = True
    if shared:
        extra['shared_identity'] = shared
    entries = deepcopy(cfg.get('plugins', {}).get('entries', {}))
    stopped = set(cfg.get('plugins', {}).get('disabled', []))
    previous_life = entries.get('hermes-life-memory', {}).get('settings', {}).get('memory')
    life = deepcopy(previous_life or {})
    life.setdefault('enabled', previous_life is None)
    if 'hermes-life-memory' in stopped:
        life['enabled'] = False
    memory_groups = options.get('memory_groups')
    if memory_groups:
        life['groups'] = ids(memory_groups, '记忆群 memory_groups')
    else:
        life.setdefault('groups', groups)
    if not set(life['groups']).issubset(groups):
        raise SetupError('已有记忆群超出新通道范围；请用 --memory-groups 明确调整，不能自动扩展或迁移。')
    life.setdefault('instance_id', 'companion-'+key)
    life.setdefault('domain_id', 'people-'+key)
    life.setdefault('policy_revision', 1)
    features = options.get('features')
    if features is not None:
        life['enabled'] = True
        enabled = features == 'local-memory'
        for section in ('conversation', 'candidates', 'episodes', 'agreements'):
            life.setdefault(section, {})['enabled'] = enabled
        if not enabled:
            life['agreements']['proactive'] = False
    local = bool(life['enabled'] and life.get('conversation', {}).get('enabled', False))
    if local:
        extra['local_conversation_context'] = True
    previous_social = entries.get('hermes-social', {}).get('settings', {}).get('channels', {}).get('napcat')
    social = deepcopy(previous_social or {})
    social.setdefault('enabled', previous_social is None)
    if 'hermes-social' in stopped:
        social['enabled'] = False
    if options.get('enable_social') or options.get('complete_social') or options.get('enhance_social'):
        social['enabled'] = True
    if options.get('enhance_social'):
        if not life['enabled'] and life.get('conversation',{}).get('enabled'):
            raise SetupError('旧停用记忆保留了原文采集选择；请显式配合 --features basic 后启用语义增强，不能随增强恢复采集。')
        life['enabled'] = True
        life['friend_dm'] = friend_enabled
        life.setdefault('explicit_memory', {}).update(enabled=True)
        life['explicit_memory'].setdefault('ttl_days',30)
        life.setdefault('agreements', {})['enabled'] = True
        # Filter old native/cached history even though no conversation body is
        # collected. Confirmed semantic facts must not resurrect via an agent.
        extra['local_conversation_context'] = True
        social.setdefault('dialogue', {}).update(enabled=True,orchestration=True)
        social.setdefault('learning', {}).update(enabled=True,semantic=True,semantic_auto_trials=True)
    if life['enabled'] and not social['enabled']:
        raise SetupError('life-memory 依赖 social；要恢复社交时显式使用 --enable-social，不能升级时自动恢复。')
    social.setdefault('initiative_mode', 'model' if previous_social is not None else 'local')
    social.setdefault('timezone', 'Asia/Shanghai')
    social.setdefault('quiet_hours', [23, 9])
    social.setdefault('targets', [])
    if options.get('complete_social'):
        social['initiative_mode'] = 'model'
        for name, values in (('dialogue', dict(enabled=True, proactive=True)),
                             ('activities', dict(enabled=True, proactive=True)),
                             ('learning', dict(enabled=True, auto_trials=True))):
            section = social.setdefault(name, {})
            if not isinstance(section, dict):
                raise SetupError('已有 '+name+' 必须是社交配置对象。')
            section.update(values)
    # Fresh installs follow the ACL. An existing explicit test/restricted scope
    # is not permission to expand participation during an ordinary upgrade.
    if options.get('follow_allowed_groups'):
        social.pop('participation_groups', None)
    if options.get('idle_groups'):
        idle = ids(options['idle_groups'], '空闲联系群 idle_groups')
        if not set(idle).issubset(groups):
            raise SetupError('空闲联系目标必须属于本次允许群。')
        social['targets'] = ['group:'+g for g in idle]
    if options.get('idle_friends') is not None:
        idle_friends = ids(options['idle_friends'], '主动联系好友 idle_friends')
        if not friend_enabled or not set(idle_friends).issubset(friend_users):
            raise SetupError('--idle-friends 必须是独立显式好友名单的子集，不能凭群成员资格或全部好友模式授权。')
        social['targets'] = [chat for chat in social['targets']
                             if not chat.startswith('dm:') or chat == 'dm:'+owner]
        social['targets'] += ['dm:'+user for user in idle_friends]
    if options.get('proactive_agreements'):
        if not local or not social['targets']:
            raise SetupError('主动约定需要 --features local-memory 和明确的 --idle-groups。')
        life.setdefault('agreements', {}).update(enabled=True, proactive=True)
    allowed_targets = {'group:'+g for g in groups} | {'dm:'+owner}
    def friend_target(chat):
        return (friend_enabled and isinstance(chat, str) and re.fullmatch(r'dm:[1-9][0-9]{0,19}', chat)
                and chat[3:] not in {owner, account}
                and (chat[3:] in friend_users or friend and (friend.get('admission') == 'verified_friends'
                                                           or friend.get('allow_group_members') is True)))
    if any(chat not in allowed_targets and not friend_target(chat) for chat in social['targets']):
        raise SetupError('已有 idle targets 超出新白名单；请先显式调整这些目标。')
    if options.get('reading_sources') is not None:
        sources = reading_sources(options['reading_sources'])
        observed = set(social['targets']) | {'group:'+g for g in social.get('participation_groups', groups)}
        if any(chat not in observed or chat not in allowed_targets and not friend_target(chat) for chat in sources):
            raise SetupError('阅读来源只能绑定已观察的明确群或好友目标；好友需另用 --idle-friends 指定。')
        section = social.setdefault('activities', {})
        if not isinstance(section, dict):
            raise SetupError('已有 activities 必须是社交配置对象。')
        section['sources'] = sources
    for name in ENABLED:
        entries.setdefault(name, {}).setdefault('settings', {})
    entries['hermes-life-memory']['settings']['memory'] = life
    entries['hermes-social']['settings'].setdefault('channels', {})['napcat'] = social
    enabled = list(dict.fromkeys([*cfg.get('plugins', {}).get('enabled', []), *ENABLED]))
    disabled = [n for n in cfg.get('plugins', {}).get('disabled', []) if n not in ENABLED]
    updated_platform = deepcopy(platform)
    updated_platform.update(enabled=True, gateway_restart_notification=False, extra=extra)
    updates = {'platforms.napcat': updated_platform, 'plugins.entries': entries,
               'plugins.enabled': enabled, 'plugins.disabled': disabled}
    worker_policy = dict(schema_version=2, mode='local-only', domain_id=policy['domain_id'],
                         policy_revision=policy['policy_revision'], host_contract=4)
    return updates, worker, worker_policy, bool(extra.get('local_conversation_context', False) or shared.get('enabled'))
