"""Back up exact targets and refuse rollback over concurrent user edits."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .settings import SetupError


def digest(path):
    if path.exists() and not path.is_file():
        raise SetupError('文件安装目标不是普通文件：'+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def no_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SetupError('安装目标或父目录为符号链接：'+str(path))


def atomic(path, data, mode):
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix='.companion-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


class Transaction:
    def __init__(self, home):
        parent = home/'backups/hermes-companion'
        no_symlinks(parent)
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backup = Path(tempfile.mkdtemp(prefix='install-', dir=parent))
        self.backup.chmod(0o700)
        self.records = {}
        self.phase = 'prepared'

    def remember(self, path):
        no_symlinks(path)
        key = str(path)
        if key not in self.records:
            before = path.read_bytes() if path.is_file() else None
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            record = {'path': key, 'before': digest(path), 'after': digest(path), 'mode': mode}
            if before is not None:
                saved = self.backup/'files'/str(len(self.records))
                atomic(saved, before, 0o600)
                record['backup'] = str(saved.relative_to(self.backup))
            self.records[key] = record
            self.save()

    def observe(self, path):
        self.records[str(path)]['after'] = digest(path)
        self.save()

    def write(self, path, data, mode=0o600):
        no_symlinks(path)
        if path.is_file() and path.read_bytes() == data:
            return
        self.remember(path)
        existing_mode = self.records[str(path)]['mode'] if path.exists() else mode
        atomic(path, data, existing_mode)
        self.observe(path)

    def save(self):
        atomic(self.backup/'manifest.json', json.dumps(
            {'phase': self.phase, 'files': list(self.records.values())}, indent=2).encode(), 0o600)

    def rollback(self):
        conflicts = []
        for record in reversed(list(self.records.values())):
            path = Path(record['path'])
            try:
                no_symlinks(path)
                same = digest(path) == record['after']
            except SetupError:
                same = False
            if not same:
                conflicts.append(record['path'])
                continue
            if record['before'] is None:
                if path.exists():
                    path.unlink()
            else:
                atomic(path, (self.backup/record['backup']).read_bytes(), record['mode'])
        self.phase = 'rollback_conflict' if conflicts else 'rolled_back'
        self.save()
        return conflicts
