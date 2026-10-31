"""Exact ingress policy and the trusted, host-supplied isolation bridge contract.

This module does not load profiles or certify an execution environment. A bridge
must enforce the route before loading context/tools; it is deliberately not
constructible through plugin configuration or message metadata.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import re


class AdmissionDenied(ValueError):
    """Safe reason code, with no message text or protocol identifiers."""


class GroupTrigger:
    """Only original text segments and actual at segments can wake a group."""
    def __init__(self, raw, require_mention):
        self.name = ''
        if raw is None:
            self.mode = 'mention' if require_mention else 'legacy_open'
        else:
            if not isinstance(raw, dict) or set(raw) - {'mode', 'name'}:
                raise ValueError('invalid group trigger policy')
            self.mode = raw.get('mode')
            if self.mode not in {'mention', 'name_or_mention'}:
                raise ValueError('group trigger must be mention or name_or_mention')
            if self.mode == 'name_or_mention':
                self.name = raw.get('name', '')
                if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 32
                        or any(c.isspace() or ord(c) < 32 or c in '[]' for c in self.name)):
                    raise ValueError('trigger name must be an explicit literal')

    @property
    def strict_safe(self):
        return self.mode in {'mention', 'name_or_mention'}

    def text_mentions_name(self, text):
        # A literal CQ token is display text, never trigger syntax or identity.
        clean = re.sub(r'\[CQ:[^\]\r\n]*\]', '', text)
        return self.mode == 'name_or_mention' and self.name in clean

    def strip_address(self, text):
        if self.name and text.startswith(self.name):
            return text[len(self.name):].lstrip(' \t，,:：') or text
        return text


@dataclass(frozen=True)
class Route:
    domain_id: str
    profile: str
    policy_revision: int
    user_id: str
    chat_id: str
    chat_type: str
    message_id: str
    platform: str = 'napcat'
    role: str = 'member'

    @property
    def session_key(self):
        # A policy change never reuses an agent carrying the previous context.
        return (self.domain_id, self.profile, self.policy_revision,
                self.platform, self.chat_type, self.chat_id, self.user_id)


@dataclass(frozen=True)
class Admission:
    route: Route


class IsolationBridge(ABC):
    """Host-owned boundary; both methods must fail closed.

    admit must verify the configured runtime, clean context, tool/file/history
    permissions, and current policy revision. dispatch owns execution entirely:
    it may not fall back to the adapter's normal handle_message or default home.
    Background work must retain the route and enforce it again after recovery.
    reply is the only supported outbound capability and is revoked on return.
    """

    media_contract = 0
    public_group_contract = 0

    def release(self, admission):
        """Release an unused admission after metadata observation/cancellation."""

    async def dispatch_media(self, event, admission, reply, attachments):
        raise AdmissionDenied('media_contract_unavailable')

    @abstractmethod
    async def admit(self, route: Route) -> Admission:
        raise NotImplementedError

    @abstractmethod
    async def dispatch(self, event, admission: Admission, reply):
        raise NotImplementedError


class IsolationPolicy:
    def __init__(self, raw, *, owner_id, users, groups, require_mention, group_trigger=None,
                 group_admission='allowed_users', self_id=None):
        fields = {'mode', 'private_profile', 'social_profile', 'domain_id',
                  'policy_revision', 'guest_dm_users'}
        if not isinstance(raw, dict) or set(raw) - fields or raw.get('mode') != 'strict':
            raise ValueError('isolation must be an explicit strict policy')
        self.private_profile = raw.get('private_profile', 'default')
        self.social_profile = raw.get('social_profile')
        self.domain_id = raw.get('domain_id')
        for value in (self.private_profile, self.social_profile, self.domain_id):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
                raise ValueError('isolation profile/domain identifiers must be explicit safe names')
        if self.private_profile != 'default' or self.social_profile == 'default' or self.domain_id == 'private':
            raise ValueError('strict isolation requires distinct private and social runtimes')
        self.revision = raw.get('policy_revision')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('policy_revision must be a positive integer')
        guests = raw.get('guest_dm_users', [])
        if (not isinstance(guests, list) or any(not isinstance(v, str) for v in guests)
                or not set(guests) <= users or owner_id in guests):
            raise ValueError('guest_dm_users must explicitly name allowed non-owner users')
        if not (group_trigger or GroupTrigger(None, require_mention)).strict_safe:
            raise ValueError('strict isolation requires an explicit bounded group trigger')
        self.owner_id, self.users, self.groups = owner_id, users, groups
        self.guests = frozenset(guests)
        self.group_admission, self.self_id = group_admission, self_id

    def allows_group_user(self, user, group):
        return (isinstance(user, str) and re.fullmatch(r'[1-9][0-9]{0,19}', user) is not None
                and user != self.self_id and group in self.groups
                and (self.group_admission == 'all_members' or user in self.users))

    def resolve(self, source):
        # Names, roles, source.profile and event metadata never grant admission.
        if (getattr(getattr(source, 'platform', None), 'value', None) != 'napcat'
                or not isinstance(getattr(source, 'user_id', None), str)
                or getattr(source, 'profile_route_rejected', False) is True
                or getattr(source, 'thread_id', None)
                or getattr(source, 'parent_chat_id', None)):
            raise AdmissionDenied('invalid_source')
        user, chat, kind = source.user_id, source.chat_id, source.chat_type
        mid = getattr(source, 'message_id', None)
        if not isinstance(mid, str) or not re.fullmatch(r'-?[0-9]{1,20}', mid):
            raise AdmissionDenied('invalid_message')
        if kind == 'dm' and user in self.users and chat == 'dm:' + user:
            if user == self.owner_id:
                domain, profile = 'private', self.private_profile
            elif user in self.guests:
                domain, profile = self.domain_id, self.social_profile
            else:
                raise AdmissionDenied('guest_dm_not_allowed')
        elif (kind == 'group' and isinstance(chat, str) and chat.startswith('group:')
              and self.allows_group_user(user, chat[6:])):
            domain, profile = self.domain_id, self.social_profile
        else:
            raise AdmissionDenied('route_not_allowed')
        return Route(domain, profile, self.revision, user, chat, kind, mid,
                     role='owner' if user == self.owner_id else 'member')
