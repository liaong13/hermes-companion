"""Personal candidate review through authenticated SDK commands, never model tools."""
from __future__ import annotations

from pathlib import Path
import re

from .scope import ScopeContext
from .store import Store


class PersonalMemoryCommands:
    def __init__(self, home, *, settings_reader, identity_reader):
        self.home = Path(home).resolve()
        self.settings_reader, self.identity_reader = settings_reader, identity_reader

    def __call__(self, raw_args, *, source=None, session_id='', platform=None):
        from hermes_yaml import safe_load
        from hermes_constants import get_hermes_home
        try:
            if Path(get_hermes_home()).resolve() != self.home:
                raise PermissionError('profile changed')
            _, settings, _ = self.settings_reader(self.home)
            config = safe_load((self.home / 'config.yaml').read_text()) or {}
            if config.get('memory', {}).get('provider') != 'scoped-memory':
                raise PermissionError('scoped-memory is not the selected provider')
            if settings['mode'] != 'local-only':
                raise PermissionError('personal candidate review requires local-only mode')
            ident = self.identity_reader()
            user, chat, kind = ident.get('user_id'), ident.get('chat_id'), ident.get('chat_type')
            if (ident.get('platform') != 'napcat' or not isinstance(user, str)
                    or re.fullmatch(r'[1-9][0-9]{0,19}', user) is None
                    or not ident.get('session_id') or not ident.get('message_id')
                    or not (kind == 'dm' and chat == 'dm:' + user and not ident.get('thread_id')
                            or kind == 'group' and isinstance(chat, str) and re.fullmatch(r'group:[1-9][0-9]{0,19}', chat))):
                raise PermissionError('trusted live member identity required')
            if ((session_id and session_id != ident['session_id'])
                    or platform is not None and getattr(platform, 'value', platform) != 'napcat'):
                raise PermissionError('command dispatch scope mismatch')
            if source is not None:
                field = source.get if isinstance(source, dict) else lambda key: getattr(source, key, None)
                if any(str(field(key)) != ident[key] for key in ('user_id', 'chat_id', 'chat_type')):
                    raise PermissionError('command source mismatch')
            store = Store(self.home, settings['domain_id'])
            scope = store.resolve_scope(ScopeContext(settings['domain_id'], 'napcat', 'napcat:' + user,
                kind, chat, thread_id=ident.get('thread_id', ''), session_id=ident['session_id'],
                message_id=ident['message_id'], policy_revision=settings['policy_revision']))
            return review_command(store, scope, raw_args)
        except PermissionError:
            return '无法处理：身份或来源未验证，或候选已改变／失效。请在原来源重新查看待确认记忆。'
        except (ValueError, TypeError, KeyError):
            return '无法处理：命令或本地记忆配置无效。'
        except Exception:
            return '本地记忆暂不可用，未确认或拒绝任何候选。'


def review_command(store, scope, raw_args):
    """Called only by authenticated original-message ingress with its own scope."""
    if not isinstance(scope, ScopeContext) or not isinstance(raw_args, str) or len(raw_args) > 512:
        raise ValueError('invalid command scope or input')
    parts = raw_args.strip().split()
    if parts == ['pending']:
        rows = store.pending(scope, 8)
        return ('当前来源没有待确认的个人记忆。' if not rows else
                '当前来源待确认的个人记忆：\n' + '\n'.join(
                    f'{row["id"]}：{row["content"][:160]}' for row in rows)
                + '\n用 /scoped-memory review <ID> 核对内容后确认或拒绝。')
    if len(parts) == 2 and parts[0] == 'review' and re.fullmatch(r'[a-f0-9]{32}', parts[1]):
        card = store.review_candidate(scope, parts[1])
        return (f'待确认个人记忆：{card["content"]}\n仅用于本人当前来源，不发布为群公共事实。\n'
                f'确认：/scoped-memory confirm {card["id"]} {card["digest"]}\n'
                f'拒绝：/scoped-memory reject {card["id"]} {card["digest"]}')
    if (len(parts) == 3 and parts[0] in ('confirm', 'reject')
            and re.fullmatch(r'[a-f0-9]{32}', parts[1]) and re.fullmatch(r'[a-f0-9]{24}', parts[2])):
        store.decide_candidate(scope, parts[1], parts[0], expected_digest=parts[2])
        return '已确认这条个人记忆。' if parts[0] == 'confirm' else '已拒绝这条个人记忆。'
    return '用法：/scoped-memory pending | review <ID> | confirm <ID> <核对码> | reject <ID> <核对码>'
