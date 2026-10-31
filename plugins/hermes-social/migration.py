"""Explicit one-time counters migration at attachment, never background history.

The old gateway must already be stopped (or be this very process). The normal
controller never opens the legacy connector path. Existing destinations win.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from .state import PERSISTED

MAX_SOURCE_BYTES = 128 * 1024


def _validate(data):
    if (not isinstance(data, dict) or set(data) != {'schema_version', 'chats'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or not isinstance(data['chats'], dict)):
        raise ValueError('invalid legacy social state schema')
    for chat, row in data['chats'].items():
        if (not isinstance(chat, str) or not re.fullmatch(r'(?:dm|group):[1-9][0-9]{0,19}', chat)
                or not isinstance(row, dict) or set(row) - set(PERSISTED)):
            raise ValueError('legacy social state is not counters-only')
        for key in ('last_attempt', 'last_check'):
            value = row.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 10**12:
                raise ValueError('invalid legacy social timestamp')
        for key in ('unanswered', 'paused'):
            if type(row.get(key, False)) is not bool:
                raise ValueError('invalid legacy social boolean')
        day, daily = row.get('day', ''), row.get('daily', 0)
        if (not isinstance(day, str) or len(day) > 10
                or type(daily) is not int or not 0 <= daily <= 10000):
            raise ValueError('invalid legacy social count')
    return data


def _directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('unsafe social migration directory')


def migrate_napcat_state_v1(port):
    """Return absent/exists/migrated; refuse another live gateway, never overwrite."""
    home = Path(port.home).resolve()
    from hermes_constants import get_hermes_home
    if Path(get_hermes_home()).resolve() != home:
        raise ValueError('social migration profile mismatch')
    source = home / 'napcat' / 'social-state.json'
    destination = home / 'plugin-data' / 'hermes-social' / 'napcat' / 'social-state.json'
    if os.path.lexists(destination):
        return 'exists'
    try:
        before = source.lstat()
    except FileNotFoundError:
        return 'absent'
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > MAX_SOURCE_BYTES):
        raise ValueError('legacy social state must be a bounded owner-only regular file')
    from gateway.status import live_gateway_pid_for_home
    pid = live_gateway_pid_for_home(home)
    if pid is not None and pid != os.getpid():
        raise RuntimeError('social state migration requires the old gateway to be stopped')
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as incoming:
        opened = os.fstat(incoming.fileno())
        if (opened.st_ino != before.st_ino or opened.st_dev != before.st_dev
                or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600):
            raise ValueError('legacy social state changed during migration')
        content = incoming.read(MAX_SOURCE_BYTES + 1)
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError('legacy social state too large')
    data = _validate(json.loads(content))
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    # Create private plugin directories before the official helper, which by
    # itself follows the process umask rather than promising mode 0700.
    root = home / 'plugin-data' / 'hermes-social'
    _directory(root.parent)
    _directory(root)
    from plugins.plugin_storage import plugin_data_dir
    parent = plugin_data_dir('hermes-social') / 'napcat'
    _directory(parent)
    if parent.stat().st_mode & 0o077:
        raise ValueError('social migration target directory must be private')
    temporary = parent / ('.state-migration-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        try:
            # link() atomically publishes the finished file and fails with
            # EEXIST, unlike replace(); a racing destination is never clobbered.
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            return 'exists'
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return 'migrated'
