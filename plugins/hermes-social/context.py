"""Read-only, exact-scope recent context; no production long-term memory hookup.

A public group scope has subject_id='', never a member's private scope. DMs
have the exact target's subject_id. A future scoped-memory bridge must return
records with precisely the requested profile/platform/chat/subject scope.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, Sequence
import json
import time


class ContextScopeError(ValueError):
    pass


@dataclass(frozen=True)
class ContextScope:
    profile_home: str
    platform: str
    chat_id: str
    subject_id: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'profile_home', str(Path(self.profile_home).resolve()))
        kind, sep, target = self.chat_id.partition(':')
        if (not self.platform or not sep or not target or kind not in {'dm', 'group'}
                or (kind == 'dm' and self.subject_id != target)):
            raise ContextScopeError('invalid context scope')


@dataclass(frozen=True)
class ContextRecord:
    scope: ContextScope
    observed_at: float
    role: str
    display_name: str
    text: str
    message_id: str
    # Trusted transport metadata for routing only; never exported by row().
    sender_id: str = ''
    at_targets: tuple[str, ...] = ()
    reply_to_message_id: str | None = None

    def row(self):
        if (self.role not in {'self', 'member'} or not isinstance(self.display_name, str)
                or not 1 <= len(self.display_name) <= 80 or not isinstance(self.text, str)
                or not 1 <= len(self.text) <= 1000 or not isinstance(self.message_id, str)
                or not self.message_id):
            raise ValueError('invalid recent context record')
        return {k: getattr(self, k) for k in ('role', 'display_name', 'text', 'message_id')}


@dataclass(eq=False)
class PublicConversationView:
    text: str
    _check: object
    message_ids: frozenset[str] = frozenset()
    _revoked: bool = False

    def revoke(self, message_ids):
        if self.message_ids.intersection(message_ids):
            self._revoked = True

    def current(self):
        try:
            return not self._revoked and bool(self._check())
        except (ValueError, PermissionError):
            return False


class ContextProvider(Protocol):
    """No write, search-all, identity-resolution or send capability."""
    def read(self, scope: ContextScope) -> Sequence[ContextRecord]: ...


class ScopedContext:
    """Reject wrong requests before the provider call and wrong returned scopes.

    An injected provider must enforce its own I/O authority before reading;
    checking returned records cannot undo an unauthorized backend read.
    """
    def __init__(self, provider: ContextProvider, scope: ContextScope):
        self.provider, self.scope = provider, scope

    def read(self, scope: ContextScope, *, budget=16000, exclude_message_id=None):
        if scope != self.scope:
            raise ContextScopeError('context scope mismatch')
        records = self.provider.read(scope)
        rows = []
        now = time.time()
        for index, record in enumerate(records):
            if index >= 16:
                raise ValueError('recent context exceeds record limit')
            if not isinstance(record, ContextRecord) or record.scope != scope:
                raise ContextScopeError('provider returned a different scope')
            row = record.row()
            if 0 <= now - record.observed_at < 3600 and row['message_id'] != exclude_message_id:
                rows.append(row)
        while rows and len(json.dumps(rows, ensure_ascii=False).encode()) > max(0, budget):
            rows.pop(0)
        return rows


class RecentContext:
    """In-process, bounded real observations only; never reads profile files."""
    def __init__(self):
        self._records = {}

    def append(self, record: ContextRecord):
        record.row()
        self._records.setdefault(record.scope, deque(maxlen=64)).append(record)

    def read(self, scope: ContextScope):
        return tuple(self._records.get(scope, ()))[-16:]

    def routing_history(self, scope: ContextScope):
        """Routing can see more than the legacy 16-row model/idle window."""
        return tuple(self._records.get(scope, ()))

    def recall(self, scope: ContextScope, message_id: str):
        self._records[scope] = deque((r for r in self.routing_history(scope)
                                      if r.message_id != message_id), maxlen=64)

    def remove_messages(self, scope: ContextScope, message_ids):
        rows = self.routing_history(scope)
        kept = [r for r in rows if r.message_id not in message_ids]
        self._records[scope] = deque(kept, maxlen=64)
        return len(rows)-len(kept)

    def replace_text(self, scope: ContextScope, message_id: str, old_suffix: str, text: str) -> bool:
        """Update one still-live observation in place, never resurrect a recalled row."""
        rows = self._records.get(scope)
        if rows is None:
            return False
        for index, record in enumerate(rows):
            if (record.message_id == message_id and record.text.endswith(old_suffix)
                    and 0 <= time.time() - record.observed_at < 3600):
                updated = replace(record, text=text)
                updated.row()
                rows[index] = updated
                return True
        return False

    def clear(self):
        self._records.clear()
