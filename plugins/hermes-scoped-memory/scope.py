"""Immutable, program-created identity for a single memory trust boundary."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ScopeContext:
    domain_id: str
    platform: str
    principal_id: str
    origin_kind: str
    origin_id: str
    thread_id: str = ''
    session_id: str = ''
    message_id: str = ''
    policy_revision: int = 1
    deletion_epoch: int = 0

    def __post_init__(self):
        for name in ('domain_id', 'platform', 'principal_id', 'origin_kind', 'origin_id'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError('invalid scope identity')
        for name in ('thread_id', 'session_id', 'message_id'):
            if not isinstance(getattr(self, name), str) or len(getattr(self, name)) > 512:
                raise ValueError('invalid scope continuity')
        if type(self.policy_revision) is not int or self.policy_revision < 1:
            raise ValueError('invalid policy revision')
        if type(self.deletion_epoch) is not int or self.deletion_epoch < 0:
            raise ValueError('invalid deletion epoch')

    @property
    def owner(self):
        return self.principal_id

    @property
    def origin(self):
        return self.origin_id + (':thread:' + self.thread_id if self.thread_id else '')

    @property
    def pair(self):
        return self.owner, self.origin

    def to_dict(self):
        return asdict(self)
