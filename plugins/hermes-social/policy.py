"""Social preferences, not a channel access-control list."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


class SocialPolicy:
    def __init__(self, raw, *, owner_id, groups):
        raw = {} if raw is None else raw
        fields = {'enabled', 'targets', 'participation_groups', 'timezone', 'quiet_hours',
                  'idle_after_seconds', 'min_idle_interval_seconds', 'daily_limit',
                  'ambient_cooldown_seconds', 'ambient_debounce_seconds', 'initiative_mode', 'public_self'}
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
        targets = raw.get('targets', [])
        if (not isinstance(targets, list) or any(not isinstance(t, str) or t not in allowed for t in targets)
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

    def quiet(self, now):
        hour = datetime.fromtimestamp(now, self.timezone).hour
        start, end = self.quiet_hours
        return start <= hour < end if start < end else (hour >= start or hour < end) if start != end else False
