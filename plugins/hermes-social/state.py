"""Social counters only. Recent messages and model credentials never go here."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import uuid

PERSISTED = ('last_attempt', 'last_check', 'unanswered', 'paused', 'day', 'daily')


class SocialStore:
    def __init__(self, home, platform='napcat'):
        from hermes_constants import get_hermes_home
        from plugins.plugin_storage import plugin_data_dir
        if Path(get_hermes_home()).resolve() != Path(home).resolve():
            raise ValueError('social storage profile mismatch')
        if not isinstance(platform, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', platform):
            raise ValueError('canonical channel name required for social storage')
        self.path = plugin_data_dir('hermes-social') / platform / 'social-state.json'

    def load(self, states):
        path = self.path
        if path.is_symlink():
            raise ValueError('social state symlink denied')
        if not path.exists():
            return
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077 or stat.st_size > 128 * 1024:
            raise ValueError('social state must be bounded and owner-only')
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or type(data.get('schema_version')) is not int
                or data['schema_version'] != 1 or not isinstance(data.get('chats'), dict)):
            raise ValueError('invalid social state')
        for target, state in states.items():
            row = data['chats'].get(target, {})
            if not isinstance(row, dict):
                raise ValueError('invalid social state row')
            for key in ('last_attempt', 'last_check'):
                value = row.get(key, 0)
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 10**12:
                    raise ValueError('invalid social timestamp')
                setattr(state, key, value)
            for key in ('unanswered', 'paused'):
                value = row.get(key, False)
                if type(value) is not bool:
                    raise ValueError('invalid social boolean')
                setattr(state, key, value)
            state.day, state.daily = row.get('day', ''), row.get('daily', 0)
            if not isinstance(state.day, str) or type(state.daily) is not int or not 0 <= state.daily <= 10000:
                raise ValueError('invalid social count')

    def save(self, states):
        parent, path = self.path.parent, self.path
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
            raise ValueError('social state directory must be owner-only')
        if path.is_symlink():
            raise ValueError('social state symlink denied')
        data = {'schema_version': 1, 'chats': {t: {k: getattr(s, k) for k in PERSISTED}
                                             for t, s in states.items()}}
        temporary = parent / ('.social-state-' + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out, ensure_ascii=False, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
