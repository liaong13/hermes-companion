"""Opt-in group media service, after strict domain admission and before chat dispatch.

All attachment bytes and derivatives are task-local. The only retained cache is
bounded, short-lived metadata with an exact account/domain/chat/author scope.
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
import time

from .media_io import ExportReader, MediaError, MediaFetcher, MediaSandbox, validate_download_policy
from .media_provider import AUDIO_MODELS, CallBudget, CodexMedia, OpenRouterMedia, ProviderManagedBudget
from .transport import OneBotError

KINDS = frozenset({'image', 'file', 'record'})


class MediaPolicy:
    def __init__(self, raw, *, groups, strict, require_mention, safe_trigger=None, friend_dm=False):
        fields = {'enabled', 'groups', 'types', 'observe_metadata', 'references', 'download_hosts', 'download_policy',
                  'export_root', 'export_prefix', 'cloud', 'allow_derived_text', 'audio_fallback_model',
                  'budget_directory', 'daily_calls', 'daily_usd', 'reserve_usd', 'budget_mode'}
        if not isinstance(raw, dict) or set(raw) - fields:
            raise ValueError('unknown media setting; credentials and test endpoints are forbidden')
        for field, default in (('enabled', False), ('observe_metadata', False), ('references', False),
                               ('allow_derived_text', False)):
            value = raw.get(field, default)
            if type(value) is not bool:
                raise ValueError('media settings require explicit booleans')
            setattr(self, field, value)
        self.groups = raw.get('groups', [])
        self.friend_dm = friend_dm is True
        self.types = raw.get('types', [])
        self.cloud = raw.get('cloud', [])
        self.hosts = raw.get('download_hosts', [])
        self.download_policy = validate_download_policy(raw.get('download_policy', 'allowlist'))
        for value in (self.groups, self.types, self.cloud, self.hosts):
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValueError('media allowlists must be explicit lists')
        if not set(self.groups) <= groups or not set(self.types) <= KINDS or not set(self.cloud) <= {'image', 'audio'}:
            raise ValueError('media cannot expand ingress ACL or modality')
        if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', h) or '*' in h for h in self.hosts):
            raise ValueError('download hosts must be exact lowercase ASCII names')
        self.audio_model = raw.get('audio_fallback_model')
        if self.audio_model is not None and self.audio_model not in AUDIO_MODELS:
            raise ValueError('fallback must explicitly support audio input')
        safe_trigger = require_mention is True if safe_trigger is None else safe_trigger
        if self.enabled and (not strict or not safe_trigger or not (self.groups or self.friend_dm) or not self.types):
            raise ValueError('media requires strict isolation, bounded group trigger and explicit group/type allowlists')
        self.budget_mode = raw.get('budget_mode', 'local')
        if self.budget_mode not in {'local', 'provider'}:
            raise ValueError('budget_mode must explicitly be local or provider')
        if self.budget_mode == 'provider' and any(key in raw for key in ('budget_directory', 'daily_calls', 'daily_usd', 'reserve_usd')):
            raise ValueError('provider budget mode must not mix local budget fields')
        if bool(raw.get('export_root')) != bool(raw.get('export_prefix')):
            raise ValueError('shared export requires root and container prefix')
        self.raw = raw


@dataclass(frozen=True)
class MediaRef:
    kind: str
    origin: str
    author: str
    index: int
    handle: str = ''
    name: str = ''
    location: str = ''
    busid: str = ''
    text: str = ''


@dataclass(frozen=True)
class CandidateBinding:
    scope: tuple
    ref: MediaRef | None = None
    created: float = 0
    expires: float = 0
    error: str | None = None


@dataclass(frozen=True)
class MediaRequest:
    scope: tuple
    direct: tuple = ()
    reply_id: str | None = None
    candidate: CandidateBinding | None = None
    error: str | None = None


def safe_value(value, limit):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ''
    value = str(value)
    return value if len(value) <= limit and not any(ord(c) < 32 for c in value) else ''


def refs_from(raw, origin, author):
    segments = raw.get('message')
    if not isinstance(segments, list) or len(segments) > 512:
        raise MediaError('invalid_segments')
    refs = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
            raise MediaError('invalid_segments')
        kind, data = segment.get('type'), segment['data']
        if kind not in KINDS:
            continue
        if len(refs) >= 4:
            raise MediaError('attachment_limit')
        handle = safe_value(data.get('file_id', ''), 256)
        name = safe_value(data.get('name', data.get('file', '')), 256)
        location = safe_value(data.get('url', ''), 8192)
        if not location:
            for key in ('path', 'file'):
                candidate = safe_value(data.get(key, ''), 8192)
                if candidate.startswith('/'):
                    location = candidate
                    break
        refs.append(MediaRef(kind, origin, author, index, handle, name, location))
    return refs


class MetadataIndex:
    def __init__(self, *, ttl=120, capacity=128):
        self.ttl, self.capacity = ttl, capacity
        self.rows = OrderedDict()
        self._timer = None

    def clear(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self.rows.clear()

    def _expire(self):
        self._timer = None
        self.sweep()
        self._schedule()

    def _schedule(self):
        if self.rows and self._timer is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # synchronous inspection still enforces expiry on access
            delay = max(.001, min(row[0] for row in self.rows.values())+self.ttl-time.monotonic())
            self._timer = loop.call_later(delay, self._expire)

    def sweep(self):
        now = time.monotonic()
        for key, (created, _) in list(self.rows.items()):
            if now - created >= self.ttl:
                self.rows.pop(key, None)

    def add(self, scope, refs):
        self.sweep()
        for ref in refs:
            # Notice/message de-dup only when the same nonempty opaque file ID
            # exists in this exact scope. Never match a filename or an inferred ID.
            key = (*scope, ref.kind, ref.handle or (ref.origin, ref.index))
            previous = self.rows.get(key)
            if previous:
                old = previous[1]
                if old.origin.startswith('notice:') and not ref.origin.startswith('notice:'):
                    self.rows[key] = (previous[0], ref)
                continue
            same_scope = [k for k in self.rows if k[:len(scope)] == scope]
            while len(same_scope) >= 10:
                self.rows.pop(same_scope.pop(0))
            while len(self.rows) >= self.capacity:
                self.rows.popitem(last=False)
            self.rows[key] = (time.monotonic(), ref)
        self._schedule()

    def select(self, scope, kind, handle=None):
        binding = self.bind(scope, kind, handle)
        if binding.error:
            raise MediaError(binding.error)
        return [binding.ref]

    def bind(self, scope, kind, handle=None):
        self.sweep()
        rows = [(created, ref) for key, (created, ref) in self.rows.items() if key[:len(scope)] == scope
                and ref.kind == kind and (handle is None or ref.handle == handle)]
        if len(rows) != 1:
            return CandidateBinding(scope, error='candidate_ambiguous' if rows else 'candidate_missing')
        created, ref = rows[0]
        return CandidateBinding(scope, ref, created, created+self.ttl)

    def validate(self, binding, scope):
        if binding.scope != scope:
            raise MediaError('candidate_scope_changed')
        if binding.error:
            raise MediaError(binding.error)
        if binding.ref is None:
            raise MediaError('candidate_invalidated')
        if time.monotonic() >= min(binding.expires, binding.created+self.ttl):
            raise MediaError('candidate_expired')
        self.sweep()
        if not any(key[:len(scope)] == scope and created == binding.created and ref == binding.ref
                   for key, (created, ref) in self.rows.items()):
            raise MediaError('candidate_invalidated')
        return [binding.ref]

    def revoke(self, scope_prefix, origin):
        for key, (_, ref) in list(self.rows.items()):
            if key[:len(scope_prefix)] == scope_prefix and ref.origin == origin:
                self.rows.pop(key, None)


class ReferenceSources:
    """Bounded provenance from authenticated group events; no message content."""
    def __init__(self, *, ttl=120, capacity=4096):
        self.ttl, self.capacity = ttl, capacity
        self.rows = OrderedDict()

    def add(self, account, domain, revision, group, message, author):
        now = time.monotonic()
        self.sweep(now)
        key = (account, domain, revision, group, message)
        self.rows[key] = (now, author)
        self.rows.move_to_end(key)
        while len(self.rows) > self.capacity:
            self.rows.popitem(last=False)

    def sweep(self, now=None):
        now = time.monotonic() if now is None else now
        while self.rows and now - next(iter(self.rows.values()))[0] >= self.ttl:
            self.rows.popitem(last=False)

    def matches(self, account, route, message, author):
        self.sweep()
        row = self.rows.get((account, route.domain_id, route.policy_revision,
                             route.chat_id, message))
        return row is not None and row[1] == author

    def revoke(self, account, domain, revision, group, message):
        self.rows.pop((account, domain, revision, group, message), None)

    def clear(self):
        self.rows.clear()


def selection(text):
    """Recognize a short media reference, never select its author or location.

    Natural wording only supplies a kind. The unchanged scoped index must still
    bind exactly one live candidate before any I/O; do not guess an ordinal or
    repair a malformed explicit handle command.
    """
    value = text.strip()
    match = re.fullmatch(r'/media (image|file|record)(?: ([A-Za-z0-9._:-]{1,256}))?', value)
    if match:
        return match[1], match[2]
    if value.startswith('/media') or re.match(r'^(?:请)?(?:别|不要|不用|无需|停止|取消|不是)', value):
        return None
    reference = (
        r'(?<![上前])(?:刚(?:刚|才)?发(?:送)?(?:过)?的(?:[这那][张个条份])?'
        r'|[上前](?:一|1)?[张个条份]'
        r'|(?:上面|前面|刚才)(?:的)?(?:[这那])?[张个条份]?'
        r'|[这那](?:一|1)?[张个条份]?)\s*')
    request = (r'^(?:(?:请|麻烦|劳烦|帮我|帮忙|你|您)\s*)*'
               r'(?:看(?:看|一下|一眼|下)?|识别|辨认|描述|分析|解读|转写|提取|读(?:读|一下)?)'
               r'\s*(?:一下|一眼)?\s*')
    question = (r'(?:(?:里|上|中)(?:面)?)?的?'
                r'(?:写了什么|写的是什么|是什么(?:内容)?|是啥(?:内容)?|有什么'
                r'|什么内容|啥内容|内容(?:是什么|是啥)?|什么意思)[?？。！!]*$')
    kinds = set()
    for kind, noun in (
        ('image', r'(?:图片|图像|照片|相片|截图|表情包|梗图|动图|图)'),
        ('file', r'(?:文件|文档)'),
        ('record', r'(?:语音|录音|音频)'),
    ):
        if (re.search(reference + noun, value) or re.search(request + noun, value)
                or re.fullmatch(noun + question, value)):
            kinds.add(kind)
    return (kinds.pop(), None) if len(kinds) == 1 else None


class MediaService:
    def __init__(self, policy, *, fetcher=None, sandbox=None, provider=None):
        self.policy = policy
        self.fetcher = fetcher or MediaFetcher(policy.hosts, download_policy=policy.download_policy)
        self.sandbox = sandbox or MediaSandbox()
        self.export = ExportReader(policy.raw['export_root'], policy.raw['export_prefix']) if policy.raw.get('export_root') else None
        self.provider = provider
        if self.provider is None and policy.cloud:
            raw = policy.raw
            if policy.budget_mode == 'provider':
                budget = ProviderManagedBudget()
            else:
                if not all(key in raw for key in ('budget_directory', 'daily_calls', 'daily_usd', 'reserve_usd')):
                    raise ValueError('cloud media requires explicit durable call budget')
                budget = CallBudget(raw['budget_directory'], daily_calls=raw['daily_calls'],
                                    daily_usd=raw['daily_usd'], reserve_usd=raw['reserve_usd'])
            # Image understanding follows the gateway's current GPT Codex
            # default. Audio fallback retains its separately configured model.
            audio = OpenRouterMedia(budget) if 'audio' in policy.cloud else None
            self.provider = CodexMedia(budget, audio_provider=audio) if 'image' in policy.cloud else audio
        self.index = MetadataIndex()
        self.reference_sources = ReferenceSources()

    @staticmethod
    def scope(route, account):
        return (account, route.domain_id, route.policy_revision, route.chat_id, route.user_id)

    def wants(self, raw, event):
        allowed = (event.source.chat_type == 'group' and event.source.chat_id[6:] in self.policy.groups
                   or event.source.chat_type == 'dm' and self.policy.friend_dm)
        return (allowed
                and (any(s.get('type') in KINDS for s in raw.get('message', []) if isinstance(s, dict))
                     or bool(event.reply_to_message_id) or selection(event.text) is not None
                     or re.fullmatch(r'/media cancel -?[0-9]{1,20}', event.text.strip()) is not None))

    def bind(self, raw, event, route, account):
        """Freeze request-time metadata only; no download, model or remote lookup."""
        scope = self.scope(route, account)
        try:
            if len(json.dumps(event.text, ensure_ascii=False).encode()) > 12000:
                raise MediaError('question_limit')
            refs = tuple(refs_from(raw, route.message_id, route.user_id))
            candidate = None
            if not refs and not event.reply_to_message_id and (chosen := selection(event.text)):
                candidate = (self.index.bind(scope, *chosen) if self.policy.observe_metadata
                             else CandidateBinding(scope, error='observation_disabled'))
            return MediaRequest(scope, refs, event.reply_to_message_id, candidate)
        except MediaError as exc:
            return MediaRequest(scope, error=str(exc))

    async def resolve(self, request, event, route, account, client, allowed_users, *, group_policy=None, allow_text=False):
        scope = self.scope(route, account)
        def author_allowed(author):
            if not isinstance(author, str) or author == account:
                return False
            if route.chat_type == 'dm':
                return author == route.user_id and group_policy is not None and group_policy.allows_guest_dm(author)
            if group_policy is not None:
                return group_policy.allows_group_user(author, route.chat_id[6:])
            return author in allowed_users and re.fullmatch(r'[1-9][0-9]{0,19}', author) is not None
        venue_allowed = (route.chat_type == 'group' and route.chat_id[6:] in self.policy.groups
                         or route.chat_type == 'dm' and self.policy.friend_dm
                         and route.chat_id == 'dm:'+route.user_id and route.domain_id != 'private')
        if (request.scope != scope or not self.policy.enabled or not venue_allowed or not author_allowed(route.user_id)):
            raise MediaError('media_request_policy_changed')
        if request.error:
            raise MediaError(request.error)
        refs = list(request.direct)
        if request.reply_id:
            if not self.policy.references:
                raise MediaError('references_disabled')
            try:
                result = await client.call('get_msg', {'message_id': int(request.reply_id)})
            except (OneBotError, asyncio.TimeoutError):
                raise MediaError('reference_lookup_failed') from None
            if not isinstance(result, dict):
                raise MediaError('reference_source_denied')
            # ID alone is insufficient; require explicit same-group source evidence
            # and a canonical authenticated author. No recursive reply/forward processing.
            author = str(result.get('user_id', ''))
            sender = result.get('sender', {})
            has_venue = ('message_type' in result and (route.chat_type == 'dm' or 'group_id' in result))
            wrong_venue = (result.get('message_type', 'group') != 'group'
                           or 'group_id' in result and str(result['group_id']) != route.chat_id[6:]) if route.chat_type == 'group' else (
                           result.get('message_type') != 'private' or author != route.user_id
                           or 'group_id' in result or 'target_id' in result and str(result['target_id']) not in {account, route.user_id})
            if (wrong_venue
                    or not has_venue and not self.reference_sources.matches(account, route, request.reply_id, author)
                    or str(result.get('message_id', '')) != request.reply_id
                    or 'self_id' in result and str(result['self_id']) != account
                    or not author_allowed(author) or not isinstance(sender, dict)
                    or route.chat_type == 'group' and group_policy is not None and group_policy.group_admission == 'all_members' and 'user_id' not in sender
                    or 'user_id' in sender and str(sender['user_id']) != author):
                raise MediaError('reference_source_denied')
            refs += refs_from(result, request.reply_id, author)
            if allow_text:
                parts = []
                for segment in result['message']:
                    if segment.get('type') == 'text':
                        value = segment['data'].get('text')
                        if not isinstance(value, str):
                            raise MediaError('reference_source_denied')
                        parts.append(value)
                quoted_text = ''.join(parts)
                if len(quoted_text.encode()) > 12000:
                    raise MediaError('question_limit')
                if quoted_text.strip():
                    refs.append(MediaRef('text', request.reply_id, author, 0, text=quoted_text))
        elif request.candidate is not None:
            if not self.policy.observe_metadata:
                raise MediaError('observation_disabled')
            refs = self.index.validate(request.candidate, scope)
        if any(not author_allowed(ref.author) or not (ref.kind in self.policy.types or allow_text and ref.kind == 'text') for ref in refs):
            raise MediaError('media_source_policy_changed')
        if len(refs) > 4:
            raise MediaError('attachment_limit')
        return refs

    async def acquire(self, ref, route, client):
        location = ref.location
        if not location and ref.kind == 'file' and ref.handle:
            try:
                if route.chat_type == 'dm':
                    result = await client.call('get_private_file_url', {'file_id': ref.handle})
                else:
                    result = await client.call('get_group_file_url', {'group_id': route.chat_id[6:], 'file_id': ref.handle})
            except (OneBotError, asyncio.TimeoutError):
                raise MediaError('handle_lookup_failed') from None
            if not isinstance(result, dict):
                raise MediaError('handle_lookup_failed')
            location = safe_value(result.get('url', ''), 8192)
        # get_record/get_image/get_file may inline base64 before the frame can be
        # inspected. They are never used. No invented Stream API endpoint.
        if location.startswith('/') and self.export:
            cancelled = threading.Event()
            try:
                return await asyncio.to_thread(self.export.read, location, self.fetcher.max_bytes, cancelled)
            finally:
                cancelled.set()
        if location.startswith(('http://', 'https://')):
            return await self.fetcher.fetch(location)
        raise MediaError('safe_transport_unavailable')

    async def recognize(self, ref, route, client, question):
        if ref.kind == 'text':
            return {'kind': 'text', 'origin': ref.origin, 'author': ref.author, 'segment': ref.index,
                    'text': ref.text, 'warnings': [], 'method': 'same-group-quote', 'billing': 'local', 'usage': {}}
        if ref.kind not in self.policy.types:
            raise MediaError('media_type_disabled')
        if ref.kind == 'image' and ('image' not in self.policy.cloud or self.provider is None):
            raise MediaError('cloud_image_denied')
        data, mime = await self.acquire(ref, route, client)
        suffix = Path(ref.name).suffix.lower() if ref.kind == 'file' else ''
        result = await self.sandbox.process(ref.kind, data, suffix)
        if ref.kind == 'image':
            if mime not in ('', 'application/octet-stream', result['source_mime']):
                raise MediaError('mime_mismatch')
            recognized = await self.provider.recognize('image', base64.b64decode(result['data']), question=question)
            result = {**recognized, 'warnings': result['warnings'] + recognized.get('warnings', [])}
        elif ref.kind == 'record':
            if mime and mime != 'application/octet-stream' and not mime.startswith('audio/'):
                raise MediaError('mime_mismatch')
            wav = base64.b64decode(result['data'])
            original, reason = None, None
            try:
                original = await self.sandbox.process('whisper', wav)
            except MediaError as exc:
                reason = str(exc)
            # Only these explicit failure/quality rules permit the independently
            # authorized audio model. Silence/decode/duration failures never do.
            needs_fallback = reason in {'whisper_unavailable', 'dependency_unavailable'} or (original is not None and original.get('low_quality') is True)
            if needs_fallback and self.policy.audio_model and 'audio' in self.policy.cloud and self.provider:
                try:
                    fallback = await self.provider.recognize('audio', wav, question='', audio_model=self.policy.audio_model)
                    if original:
                        result = {'text': 'Whisper原转写：\n'+original['text']+'\n音频模型参考转写（歧义请核对）：\n'+fallback['text'],
                                  'warnings': original.get('warnings', [])+['两个转写均保留，未合并改写'], 'model': fallback['model']}
                    else:
                        result = fallback
                except MediaError:
                    if not original:
                        raise
                    result = {**original, 'warnings': original.get('warnings', [])+['音频模型失败，保留原转写']}
            elif original is not None:
                result = original
            else:
                raise MediaError(reason or 'whisper_failed')
        elif mime and mime != 'application/octet-stream':
            expected = {'.pdf': {'application/pdf'}, '.txt': {'text/plain'}, '.md': {'text/plain', 'text/markdown'},
                        '.csv': {'text/csv', 'text/plain'},
                        '.docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
                        '.xlsx': {'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}}
            if mime not in expected.get(suffix, set()):
                raise MediaError('mime_mismatch')
        return {'kind': ref.kind, 'origin': ref.origin, 'author': ref.author, 'segment': ref.index,
                'text': result.get('text', '')[:12000], 'warnings': result.get('warnings', []),
                'method': result.get('model', 'whisper' if ref.kind == 'record' else 'local-text'),
                'billing': result.get('billing', 'local'), 'usage': result.get('usage', {})}

    async def enrich(self, refs, route, client, question):
        if not self.policy.allow_derived_text:
            raise MediaError('derived_text_egress_denied')
        results = []
        for ref in refs:
            try:
                result = await self.recognize(ref, route, client, question)
            except MediaError as exc:
                result = {'kind': ref.kind, 'origin': ref.origin, 'author': ref.author,
                          'segment': ref.index, 'error': str(exc)}
            results.append(result)
        # Worker pipe is 64KiB; keep a total UTF-8 budget independent of character count.
        remaining = 24000
        for result in results:
            text = result.get('text', '')
            size = lambda value: len(json.dumps(value, ensure_ascii=False).encode())
            if size(text) > remaining:
                low, high = 0, len(text)
                while low < high:
                    middle = (low+high+1)//2
                    if size(text[:middle]) <= remaining:
                        low = middle
                    else:
                        high = middle-1
                result['text'] = text[:low]
                result.setdefault('warnings', []).append('任务总文本额度已截断')
            remaining = max(0, remaining-size(result.get('text', '')))
        return results
