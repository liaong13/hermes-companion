"""Social preferences, not a channel access-control list."""
from __future__ import annotations

from datetime import datetime
import re
from zoneinfo import ZoneInfo


class SocialPolicy:
    def __init__(self, raw, *, owner_id, groups, dm_target_allowed=None):
        raw = {} if raw is None else raw
        fields = {'enabled', 'targets', 'participation_groups', 'timezone', 'quiet_hours',
                  'idle_after_seconds', 'min_idle_interval_seconds', 'daily_limit',
                  'ambient_cooldown_seconds', 'ambient_debounce_seconds', 'initiative_mode', 'public_self',
                  'dialogue', 'activities', 'learning'}
        if not isinstance(raw, dict) or set(raw) - fields:
            raise ValueError('invalid social policy')
        public_self = raw.get('public_self', {})
        if not isinstance(public_self, dict) or set(public_self) - {'name', 'description', 'communication_preferences'}:
            raise ValueError('invalid public self description')
        for key, limit in (('name', 80), ('description', 2000)):
            if key in public_self and (not isinstance(public_self[key], str)
                    or not 1 <= len(public_self[key]) <= limit or '\x00' in public_self[key]):
                raise ValueError('invalid public self '+key)
        preferences = public_self.get('communication_preferences', [])
        if (not isinstance(preferences, list) or len(preferences) > 8
                or any(not isinstance(item, str) or not 1 <= len(item) <= 120 or '\x00' in item for item in preferences)):
            raise ValueError('invalid public communication preferences')
        self.public_self = {**public_self}
        if 'communication_preferences' in public_self:
            self.public_self['communication_preferences'] = list(preferences)
        self.enabled = raw.get('enabled', False)
        self.initiative_mode = raw.get('initiative_mode', 'model')
        if self.initiative_mode not in {'local', 'model'}:
            raise ValueError('initiative_mode must be local or model')
        if type(self.enabled) is not bool:
            raise ValueError('social enabled must be a boolean')
        groups = frozenset(groups)
        allowed = {'dm:' + owner_id} | {'group:' + g for g in groups}
        def allowed_chat(chat):
            return (chat in allowed or isinstance(chat, str) and re.fullmatch(r'dm:[1-9][0-9]{0,19}', chat)
                    and callable(dm_target_allowed) and bool(dm_target_allowed(chat)))
        targets = raw.get('targets', [])
        if (not isinstance(targets, list) or any(not isinstance(t, str) or not allowed_chat(t) for t in targets)
                or len(targets) != len(set(targets)) or len(targets) > 16):
            raise ValueError('social targets must be exact already-authorized chats')
        self.targets = frozenset(targets)
        parts = raw.get('participation_groups', sorted(groups))
        if (not isinstance(parts, list) or any(not isinstance(g, str) or g not in groups for g in parts)
                or len(parts) != len(set(parts))):
            raise ValueError('participation groups must belong to the channel groups')
        self.participation_groups = frozenset(parts) if self.enabled else frozenset()
        self.observed_chats = self.targets | frozenset('group:' + g for g in self.participation_groups)
        if self.enabled and not self.observed_chats:
            raise ValueError('social mode requires an authorized chat')
        try:
            self.timezone = ZoneInfo(raw.get('timezone', 'Asia/Shanghai'))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('invalid social timezone') from exc
        quiet = raw.get('quiet_hours', [23, 9])
        if (not isinstance(quiet, list) or len(quiet) != 2
                or any(type(x) is not int or not 0 <= x <= 23 for x in quiet)):
            raise ValueError('quiet_hours must contain two hours, start inclusive/end exclusive')
        self.quiet_hours = tuple(quiet)
        for key, default, low, high in (
            ('idle_after_seconds', 1800, 300, 86400),
            ('min_idle_interval_seconds', 7200, 300, 604800),
            ('daily_limit', 3, 1, 12),
            ('ambient_cooldown_seconds', 20, 5, 300),
            ('ambient_debounce_seconds', 3, 1, 30),
        ):
            value = raw.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('invalid social ' + key)
            setattr(self, key, value)
        self.dialogue = self._section(raw.get('dialogue', {}),
            {'enabled': True, 'proactive': False, 'orchestration': False,
             'topic_ttl_seconds': 86400, 'opportunity_ttl_seconds': 3600})
        for name in ('enabled', 'proactive', 'orchestration'):
            self._boolean(self.dialogue, name)
        if self.dialogue['orchestration'] and not self.dialogue['enabled']:
            raise ValueError('request orchestration requires dialogue state')
        self._integer(self.dialogue, 'topic_ttl_seconds', 600, 604800)
        self._integer(self.dialogue, 'opportunity_ttl_seconds', 60, self.dialogue['topic_ttl_seconds'])
        self.activities = self._section(raw.get('activities', {}),
            {'enabled': False, 'sources': {}, 'read_interval_seconds': 21600,
             'daily_read_limit': 3, 'proactive': False, 'policy_revision': 1, 'retention_days': 30})
        for name in ('enabled', 'proactive'):
            self._boolean(self.activities, name)
        for name, low, high in (('read_interval_seconds', 300, 604800), ('daily_read_limit', 1, 12),
                                ('policy_revision', 1, 2147483647), ('retention_days', 1, 90)):
            self._integer(self.activities, name, low, high)
        sources = self.activities['sources']
        if not isinstance(sources, dict) or len(sources) > 16:
            raise ValueError('invalid reading source mapping')
        from .reading import validate_public_url
        for chat, urls in sources.items():
            if not allowed_chat(chat) or chat not in self.observed_chats:
                raise ValueError('reading source chat is outside observed scope')
            if (not isinstance(urls, list) or not 1 <= len(urls) <= 16
                    or any(not isinstance(url, str) for url in urls) or len(set(urls)) != len(urls)):
                raise ValueError('invalid reading sources')
            for url in urls:
                validate_public_url(url)
        self.learning = self._section(raw.get('learning', {}),
            {'enabled': False, 'auto_trials': False, 'min_feedback': 3, 'trial_seconds': 86400,
             'semantic': False, 'semantic_auto_trials': False, 'method_ttl_days': 30,
             'daily_method_generations': 2})
        for name in ('enabled', 'auto_trials', 'semantic', 'semantic_auto_trials'):
            self._boolean(self.learning, name)
        self._integer(self.learning, 'min_feedback', 2, 20)
        self._integer(self.learning, 'trial_seconds', 600, 604800)
        self._integer(self.learning, 'method_ttl_days', 1, 90)
        self._integer(self.learning, 'daily_method_generations', 1, 4)
        if self.learning['semantic'] and not self.learning['enabled']:
            raise ValueError('semantic methods require feedback learning')

    @staticmethod
    def _section(raw, defaults):
        if not isinstance(raw, dict) or set(raw)-set(defaults):
            raise ValueError('invalid social capability settings')
        return {**defaults, **raw}

    @staticmethod
    def _boolean(raw, name):
        if type(raw[name]) is not bool:
            raise ValueError('invalid social '+name)

    @staticmethod
    def _integer(raw, name, low, high):
        if type(raw[name]) is not int or not low <= raw[name] <= high:
            raise ValueError('invalid social '+name)

    def quiet(self, now):
        hour = datetime.fromtimestamp(now, self.timezone).hour
        start, end = self.quiet_hours
        return start <= hour < end if start < end else (hour >= start or hour < end) if start != end else False
