"""Local-only person service. Entry points are trusted original-message callbacks."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time

from .core import memory_api
from .conversation import ConversationMemory
from .rules import self_statement, explicit_request, explicit_candidate
from .relations import Relations

COMMAND = '/social-memory'


def settings(raw):
    allowed = {'enabled', 'instance_id', 'domain_id', 'policy_revision', 'groups', 'conversation', 'candidates',
               'episodes', 'agreements', 'friend_dm', 'explicit_memory'}
    if not isinstance(raw, dict) or set(raw)-allowed or type(raw.get('enabled', False)) is not bool:
        raise ValueError('invalid life memory settings')
    if raw.get('enabled') is not True:
        return {'enabled': False}
    if (not isinstance(raw.get('instance_id'), str) or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', raw['instance_id']) is None
            or not isinstance(raw.get('domain_id'), str) or not raw['domain_id'] or raw['domain_id'] == 'legacy'
            or len(raw['domain_id']) > 256 or type(raw.get('policy_revision')) is not int or raw['policy_revision'] < 1
            or type(raw.get('friend_dm', False)) is not bool
            or not isinstance(raw.get('groups'), list) or not (raw['groups'] or raw.get('friend_dm')) or len(raw['groups']) > 100
            or any(not isinstance(g, str) or re.fullmatch(r'[1-9][0-9]{0,19}', g) is None for g in raw['groups'])):
        raise ValueError('explicit instance, domain, policy and memory groups required')
    conversation = dict(raw.get('conversation', {})) if isinstance(raw.get('conversation', {}), dict) else None
    candidates = dict(raw.get('candidates', {})) if isinstance(raw.get('candidates', {}), dict) else None
    if (conversation is None or set(conversation)-{'enabled','retention_days','max_events'}
            or candidates is None or set(candidates)-{'enabled','ttl_days'}):
        raise ValueError('invalid local conversation policy')
    for policy in (conversation, candidates):
        policy.setdefault('enabled', False)
        if type(policy['enabled']) is not bool:
            raise ValueError('explicit local policy boolean required')
    conversation.setdefault('retention_days', 7)
    conversation.setdefault('max_events', 200)
    candidates.setdefault('ttl_days', 30)
    for value,lower,upper in ((conversation['retention_days'],1,90), (conversation['max_events'],16,1000),
                              (candidates['ttl_days'],1,90)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError('invalid local retention bound')
    if candidates['enabled'] and not conversation['enabled']:
        raise ValueError('self-statement candidates require explicit conversation collection')
    explicit = raw.get('explicit_memory', {})
    if not isinstance(explicit, dict) or set(explicit)-{'enabled', 'ttl_days'}:
        raise ValueError('invalid explicit memory policy')
    explicit = {'enabled': False, 'ttl_days': 30, **explicit}
    if type(explicit['enabled']) is not bool or type(explicit['ttl_days']) is not int or not 1 <= explicit['ttl_days'] <= 365:
        raise ValueError('explicit memory requires bounded retention')
    episodes = raw.get('episodes', {})
    agreements = raw.get('agreements', {})
    if (not isinstance(episodes,dict) or set(episodes)-{'enabled','retention_days'}
            or not isinstance(agreements,dict) or set(agreements)-{'enabled','proactive','grace_hours'}):
        raise ValueError('invalid relation policy')
    episodes = {'enabled':False,'retention_days':30,**episodes}
    agreements = {'enabled':False,'proactive':False,'grace_hours':24,**agreements}
    if (any(type(x) is not bool for x in (episodes['enabled'],agreements['enabled'],agreements['proactive']))
            or type(episodes['retention_days']) is not int or not 1<=episodes['retention_days']<=90
            or type(agreements['grace_hours']) is not int or not 1<=agreements['grace_hours']<=72
            or episodes['enabled'] and not conversation['enabled']
            or agreements['enabled'] and not (conversation['enabled'] or explicit['enabled'])
            or agreements['proactive'] and not agreements['enabled']):
        raise ValueError('explicit conversation and bounded relation policy required')
    return {**raw, 'groups': sorted(set(raw['groups'])), 'conversation': conversation, 'candidates': candidates,
            'episodes':episodes, 'agreements':agreements, 'friend_dm': raw.get('friend_dm', False),
            'explicit_memory': explicit}


class CommandReply(str):
    def __new__(cls, text, feedback=()):
        result = str.__new__(cls,text)
        result.contact_feedback = tuple(feedback)
        return result


@dataclass(frozen=True)
class SocialMemoryView:
    facts: tuple
    conversation: dict
    references: tuple
    _check: object
    relations: tuple = ()

    def current(self):
        try:
            self._check()
            return True
        except (PermissionError, ValueError):
            return False


@contextmanager
def profile(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


class LifeMemoryService:
    contract_version = 1
    agreement_contract = 1
    platform = 'napcat'

    def __init__(self, home, storage_home, config, reader, *, channel_domain, channel_revision, dm_allowed=None):
        self.home, self.config = Path(home).resolve(), settings(config)
        if not self.config['enabled']:
            raise ValueError('life memory is disabled')
        self.groups, self.reader = frozenset(self.config['groups']), reader
        self.channel_binding = channel_domain, channel_revision
        self.dm_allowed = dm_allowed
        if self.config['friend_dm'] and not callable(dm_allowed):
            raise ValueError('friend memory requires trusted current DM authorization')
        self.active = True
        api = memory_api()
        if any(not callable(getattr(api.Store,name,None)) for name in ('append_in','evidenced_in','candidate_in')):
            raise ValueError('matching public Store transaction API required')
        self.Scope = api.ScopeContext
        self.store = api.Store(storage_home, self.config['domain_id'])
        self.people = api.PeopleMemory(self.store, self.config['instance_id'])
        self.conversations = ConversationMemory(self.store, self.config['instance_id'],
            retention_days=self.config['conversation']['retention_days'], max_events=self.config['conversation']['max_events'])
        self.relations = Relations(self.store,self.conversations,self.config['instance_id'])
        self.semantic_binding = hashlib.sha256(json.dumps(
            [self.config, self.channel_binding], sort_keys=True).encode()).hexdigest()
        with self.store.connection(True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS life_semantic_sources (
                source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                instance_id TEXT NOT NULL, quote_start INTEGER NOT NULL, quote_end INTEGER NOT NULL,
                binding TEXT NOT NULL)''')
            if 'binding' not in {row['name'] for row in db.execute('PRAGMA table_info(life_semantic_sources)')}:
                db.execute("ALTER TABLE life_semantic_sources ADD COLUMN binding TEXT NOT NULL DEFAULT ''")
            db.execute('''CREATE TABLE IF NOT EXISTS life_confirmations (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                instance_id TEXT NOT NULL, message_id TEXT NOT NULL, confirmed_at REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS life_semantic_deliveries (
                instance_id TEXT NOT NULL, domain TEXT NOT NULL, owner TEXT NOT NULL, origin TEXT NOT NULL,
                message_id TEXT NOT NULL, kind TEXT NOT NULL, ref_id TEXT NOT NULL,
                memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
                source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
                revision INTEGER NOT NULL, PRIMARY KEY(instance_id,domain,owner,origin,message_id,kind,ref_id))''')

    def validate(self):
        if not self.active:
            raise PermissionError('life memory unloaded')
        with profile(self.home):
            try:
                current = settings(self.reader())
            except (ValueError, TypeError):
                self._retire_semantic_sources()
                self.close()
                raise PermissionError('life memory policy invalid; reload required') from None
            if current != self.config:
                self._retire_semantic_sources()
                self.close()
                raise PermissionError('life memory policy changed; reload required')

    def close(self):
        self.active = False

    def scope(self, route):
        self.validate()
        kind = getattr(route, 'chat_type', '')
        group = kind == 'group' and route.chat_id in {'group:'+g for g in self.groups}
        dm = (kind == 'dm' and self.config['friend_dm'] and route.chat_id == 'dm:'+route.user_id
              and callable(self.dm_allowed) and self.dm_allowed(route.chat_id) is True)
        if (getattr(route, 'platform', '') != 'napcat' or not (group or dm)
                or (route.domain_id, route.policy_revision) != self.channel_binding
                or re.fullmatch(r'[1-9][0-9]{0,19}', route.user_id) is None
                or re.fullmatch(r'-?[0-9]{1,20}', route.message_id) is None
                or getattr(route, 'thread_id', '')):
            raise PermissionError('life memory origin or member denied')
        requested = self.Scope(self.config['domain_id'], 'napcat', 'napcat:'+route.user_id,
            kind, route.chat_id, session_id='person:'+self.config['instance_id'],
            message_id=route.message_id, policy_revision=self.config['policy_revision'])
        with self.store.connection() as db:
            existing = db.execute('SELECT policy_revision FROM scope_versions WHERE domain=? AND owner=? AND origin=?',
                                  (self.store.domain_id, *requested.pair)).fetchone()
        scope = (self.store.update_policy(requested, requested.policy_revision)
                 if existing is not None and existing[0] < requested.policy_revision
                 else self.store.resolve_scope(requested))
        with self.store.connection() as db:
            stale = [row[0] for row in db.execute('''SELECT s.id FROM sources s JOIN life_semantic_sources x ON x.source_id=s.id
                WHERE s.domain=? AND s.owner=? AND s.origin=? AND x.instance_id=? AND x.binding!=?''',
                (self.store.domain_id, *scope.pair, self.config['instance_id'], self.semantic_binding))]
        if stale:
            self.store.forget(scope, source_ids=stale)
            scope = self.store.resolve_scope(scope)
        return scope

    def _retire_semantic_sources(self):
        # Explicit settings changes retire only this service's authorized
        # semantic evidence. Unload/restart alone keeps valid confirmed facts.
        with self.store.connection() as db:
            rows = db.execute('''SELECT s.id,s.owner,s.origin FROM sources s
                JOIN life_semantic_sources x ON x.source_id=s.id
                WHERE s.domain=? AND x.instance_id=? AND x.binding=?''',
                (self.store.domain_id, self.config['instance_id'], self.semantic_binding)).fetchall()
        for row in rows:
            try:
                scope = self.store.resolve_scope(self.Scope(self.store.domain_id, 'napcat', row['owner'],
                    row['origin'].partition(':')[0], row['origin'], policy_revision=self.config['policy_revision']))
                self.store.forget(scope, source_ids=[row['id']])
            except PermissionError:
                pass  # A newer scope policy already prevents this generation.

    def recall(self, route, *, quote_message_id='', include_conversation=True):
        scope = self.scope(route)
        scope = self._prune_explicit(scope)
        if self.config['conversation']['enabled']:
            updated = self.conversations.prune(scope)
            if updated.deletion_epoch != scope.deletion_epoch:
                self._prune_group(route.chat_id)
            scope = self.scope(route)
        person = self.people.recall(scope)
        conversation, sources = (self.conversations.view(scope, exclude_message_id=scope.message_id)
                                 if self.config['conversation']['enabled'] and include_conversation else ({}, ()))
        with self.store.connection() as db:
            refs = tuple(self.conversations._reference(db, scope, 'memory', row['id']) for row in person.facts) + sources
            facts = tuple({**row, 'provenance': self._provenance(db, scope, row['id'])} for row in person.facts)
        if quote_message_id:
            quote = self.conversations.quote_reference(scope, quote_message_id)
            if quote is not None:
                refs += (quote,)
        records, relation_refs = self.relations.view(scope, episodes=self.config['episodes']['enabled'],
            agreements=self.config['agreements']['enabled'], explicit_episodes=self.config['explicit_memory']['enabled'])
        with self.store.connection() as db:
            records = [{**row, 'provenance': self._provenance(db, scope, row['id'])} for row in records]
        refs += relation_refs
        def check():
            self.scope(route)
            if not person.current():
                raise PermissionError('person view changed')
            self.conversations.check_references(scope, refs)
            with self.store.connection() as db:
                for row in records:
                    if row['kind']=='agreement':
                        current = self.relations.owns(db,scope,row['id'])
                        if current is None or current['status']!=row['status'] or current['due']!=row['due']:
                            raise PermissionError('agreement view changed')
        return SocialMemoryView(facts, conversation, refs, check, tuple(records))

    def _provenance(self, db, scope, ident):
        memory = self.store.owned(db, 'memories', ident, scope)
        confirmation = db.execute('SELECT message_id,confirmed_at FROM life_confirmations WHERE memory_id=? AND instance_id=?',
                                  (ident, self.config['instance_id'])).fetchone()
        evidence = []
        for row in db.execute('''SELECT e.*,s.message_id,s.created,s.user_text FROM evidence e
            JOIN sources s ON s.id=e.source_id WHERE e.memory_id=? ORDER BY s.created LIMIT 4''', (ident,)):
            offset = db.execute('SELECT quote_start FROM life_semantic_sources WHERE source_id=? AND instance_id=?',
                                (row['source_id'], self.config['instance_id'])).fetchone()
            start = row['start'] if 'start' in row.keys() else row['start_offset']
            end = row['end'] if 'end' in row.keys() else row['end_offset']
            evidence.append(dict(message_id=row['message_id'], observed_at=row['created'],
                quote=(row['user_text'] or '')[start:end], source_revision=row['source_revision'],
                original_start=start+(offset[0] if offset else 0), original_end=end+(offset[0] if offset else 0)))
        return dict(observed_at=memory['created'], expires_at=memory['expires'],
                    confirmation=dict(confirmation) if confirmation else {'status': 'owner_confirmed'}, evidence=evidence)

    def _prune_explicit(self, scope):
        if not self.config['explicit_memory']['enabled']:
            return scope
        with self.store.connection() as db:
            sources = [row[0] for row in db.execute('''SELECT DISTINCT m.source_id FROM memories m
                WHERE m.domain=? AND m.owner=? AND m.origin=? AND m.source_id IS NOT NULL AND m.expires<=?
                  AND (EXISTS(SELECT 1 FROM person_facts p WHERE p.memory_id=m.id AND p.instance_id=?)
                    OR EXISTS(SELECT 1 FROM life_agreements a WHERE a.memory_id=m.id AND a.instance_id=?)
                    OR EXISTS(SELECT 1 FROM life_explicit_episodes e WHERE e.memory_id=m.id AND e.instance_id=?))''',
                (self.store.domain_id, *scope.pair, time.time(), self.config['instance_id'], self.config['instance_id'],
                 self.config['instance_id']))]
        if sources:
            self.store.forget(scope, source_ids=sources)
            return self.store.resolve_scope(scope)
        return scope

    def _authorized_source(self, scope, text, *, start=0, end=None):
        self.conversations.assert_message_current(scope)
        if self.config['explicit_memory']['enabled'] and not self.config['conversation']['enabled']:
            end = len(text) if end is None else end
            excerpt = text[start:end]
            receipt = self.store.append_turn(scope, excerpt, '')
            with self.store.connection(True) as db:
                db.execute('INSERT OR IGNORE INTO life_semantic_sources VALUES (?,?,?,?,?)',
                    (receipt['source_id'], self.config['instance_id'], start, end, self.semantic_binding))
            evidence_start = 0
        else:
            receipt = self.store.append_turn(scope, text, '')
            evidence_start = start
        self.conversations.remember_receipt(scope, receipt)
        return receipt, evidence_start

    def is_explicit_request(self, text):
        return self.config['explicit_memory']['enabled'] and explicit_request(text) is not None

    def explicit_proposal(self, text):
        return explicit_candidate(text) if self.is_explicit_request(text) else None

    def propose_explicit(self, route, text, proposal):
        scope = self.scope(route)
        intent = explicit_request(text)
        if not self.is_explicit_request(text) or intent is None or not isinstance(proposal, dict) or set(proposal) != {'field', 'value', 'start'}:
            raise PermissionError('explicit current self-memory request required')
        field, value, start = proposal['field'], proposal['value'], proposal['start']
        if (not isinstance(field, str) or field not in {'interest', 'preference', 'boundary', 'episode'} or not isinstance(value, str)
                or type(start) is not int or start < intent.start('statement') or len(value.encode()) > 480 or not value.strip()
                or text[start:start+len(value)] != value):
            raise ValueError('semantic candidate must cite an exact original span')
        if len(self.people.list(scope, pending=True)) >= 8:
            raise ValueError('pending candidate budget exceeded')
        receipt, offset = self._authorized_source(scope, text, start=start, end=start+len(value))
        ident = (self.relations.propose_episode(scope, receipt['source_id'], value, start=offset,
                    ttl_days=self.config['explicit_memory']['ttl_days']) if field == 'episode'
                 else self.people.propose(scope, receipt['source_id'], field, value, start=offset,
                    ttl_days=self.config['explicit_memory']['ttl_days']))
        if self.store.get(scope, ident)['state'] != 'pending':
            return '这条来源的候选已经处理；需要修改时请发送新的明确自述。'
        return '已保存待核对的本人自述。用 /social-memory review '+ident+' 查看原文证据和有效期；确认前不会召回。'

    def observe(self, route, text, *, can_extract=False, reply_to='', verified_bot_reply=False):
        scope = self.scope(route)
        if (not self.config['conversation']['enabled'] or not isinstance(text, str) or not text.strip()
                or text.startswith('/') or len(text.encode()) > 2000):
            return False
        scope, receipt = self.conversations.observe(scope, text, reply_to=reply_to, verified_bot_reply=verified_bot_reply)
        if self.config['episodes']['enabled']:
            self.relations.capture_episode(scope,retention_days=self.config['episodes']['retention_days'])
        candidate = self_statement(text) if can_extract and self.config['candidates']['enabled'] else None
        if candidate is not None and len(self.people.list(scope, pending=True)) < 8:
            field,value,start = candidate
            if not any(r['field']==field and r['content'].endswith('：'+value) for r in self.people.list(scope)):
                self.people.propose(scope, receipt['source_id'], field, value, start=start,
                                    ttl_days=self.config['candidates']['ttl_days'])
        return True

    def contact_opportunity(self, chat, *, excluded=frozenset()):
        self.validate()
        if (not self.config['agreements']['enabled'] or not self.config['agreements']['proactive']
                or chat not in {'group:'+g for g in self.groups}):
            return None
        def scope_for(owner):
            if re.fullmatch(r'napcat:[1-9][0-9]{0,19}',owner) is None:
                raise PermissionError('agreement recipient identity invalid')
            return self.store.resolve_scope(self.Scope(self.store.domain_id,'napcat',owner,'group',chat,
                session_id='person:'+self.config['instance_id'], policy_revision=self.config['policy_revision']))
        return self.relations.due(chat,scope_for,self.validate,excluded=excluded)

    def contact_feedback(self, chat, recipient, reference, status):
        self.validate()
        if chat not in {'group:'+g for g in self.groups} or status not in {'replied','declined','activity_observed'}:
            return
        with self.store.connection(True) as db:
            rows = db.execute('''SELECT m.id FROM memories m JOIN life_agreements a ON a.memory_id=m.id
                WHERE m.domain=? AND m.origin=? AND m.owner=? AND a.instance_id=? AND m.state='active' ''',
                (self.store.domain_id,chat,'napcat:'+recipient,self.config['instance_id'])).fetchall()
            for row in rows:
                if self.relations.reference(row['id'])==reference:
                    db.execute('UPDATE life_agreements SET status=? WHERE memory_id=?',(status,row['id']))

    def record_reply(self, route, message_id, text, view):
        if not self.config['conversation']['enabled']:
            return
        if not view.current():
            raise PermissionError('reply dependencies expired')
        self.conversations.record_reply(self.scope(route), message_id, text, view.references)

    def _chat_allowed(self, chat):
        return (chat in {'group:'+g for g in self.groups}
                or self.config['friend_dm'] and isinstance(chat, str) and re.fullmatch(r'dm:[1-9][0-9]{0,19}', chat)
                and callable(self.dm_allowed) and self.dm_allowed(chat) is True)

    def record_reference_delivery(self, route, message_id, view):
        """A text-free dependency fence; never collect ordinary reply plaintext."""
        if not view.current() or re.fullmatch(r'-?[0-9]{1,20}', message_id) is None:
            raise PermissionError('semantic delivery dependencies expired')
        scope = self.scope(route)
        with self.store.connection(True) as db:
            for ref in view.references:
                self.conversations._reference(db, scope, ref['kind'], ref['id'], ref['revision'],
                                              archival=ref.get('archival', False))
                db.execute('''INSERT OR IGNORE INTO life_semantic_deliveries VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (self.config['instance_id'], self.store.domain_id, *scope.pair, message_id, ref['kind'], ref['id'],
                     ref['id'] if ref['kind'] == 'memory' else None,
                     ref['id'] if ref['kind'] in {'source', 'quote'} else None, ref['revision']))

    def suppressed_messages(self, chat):
        if not self._chat_allowed(chat):
            raise PermissionError('message scope denied')
        return self._suppressed_messages(chat)

    def _suppressed_messages(self, chat):
        # Internal deletion metadata only. Trusted withdrawal notices may need
        # to retire old material after transport authorization caches expire.
        with self.store.connection() as db:
            rows = db.execute('''SELECT d.message_id FROM life_semantic_deliveries d
                LEFT JOIN memories m ON m.id=d.memory_id LEFT JOIN sources s ON s.id=d.source_id
                WHERE d.instance_id=? AND d.domain=? AND d.origin=? AND (
                    d.kind='memory' AND (m.id IS NULL OR m.state!='active' OR m.revision!=d.revision OR m.expires<=?)
                    OR d.kind IN ('source','quote') AND (s.id IS NULL OR s.purged IS NOT NULL OR s.revision!=d.revision))''',
                (self.config['instance_id'], self.store.domain_id, chat, time.time())).fetchall()
            blocked = {row[0] for row in rows}
        return self.conversations.suppressed(chat) | blocked

    def block_delivered(self, route, message_id):
        # The adapter supplies a formerly admitted exact route and a real ACK.
        # Unload/changed policy can only add a tombstone, never store plaintext.
        if (not (route.chat_id in {'group:'+g for g in self.groups}
                or self.config['friend_dm'] and route.chat_type == 'dm' and route.chat_id == 'dm:'+route.user_id)
                or (route.domain_id, route.policy_revision) != self.channel_binding
                or re.fullmatch(r'[1-9][0-9]{0,19}', route.user_id) is None):
            raise PermissionError('late delivery scope denied')
        scope = self.Scope(self.config['domain_id'], 'napcat', 'napcat:'+route.user_id,
                           route.chat_type, route.chat_id, message_id=route.message_id)
        self.conversations.block_delivered(scope, message_id)

    def _prune(self, route):
        self._prune_explicit(self.scope(route))
        if self.config['conversation']['enabled']:
            self.conversations.prune(self.scope(route))
            self._prune_group(route.chat_id)

    def _prune_group(self, chat):
        # A public bot quote may carry another member's revoked facts. Traverse
        # only broken dependency metadata in this exact group; never read their
        # raw inputs, infer identities or grant a cross-person recall interface.
        for _ in range(64):
            with self.store.connection() as db:
                owners = [r[0] for r in db.execute('''SELECT DISTINCT e.owner FROM life_events e
                    JOIN life_dependencies d ON d.event_id=e.id
                    WHERE e.instance_id=? AND e.domain=? AND e.origin=? AND e.source_id IS NOT NULL
                      AND ((d.kind='memory' AND d.memory_id IS NULL)
                        OR (d.kind IN ('source','quote') AND d.source_id IS NULL)) LIMIT 64''',
                    (self.config['instance_id'], self.store.domain_id, chat))]
            if not owners:
                return
            for owner in owners:
                scope = self.store.resolve_scope(self.Scope(self.store.domain_id, 'napcat', owner, chat.partition(':')[0], chat,
                    policy_revision=self.config['policy_revision']))
                self.conversations.prune(scope)
        raise PermissionError('group dependency cleanup budget exceeded')

    def revoke(self, chat, message_id, *, revoker=None):
        """Trusted channel recall notification; never a user-selectable tool."""
        self.validate()
        trusted_dm = (self.config['friend_dm'] and isinstance(chat, str)
                      and re.fullmatch(r'dm:[1-9][0-9]{0,19}', chat) is not None
                      and revoker == 'napcat:'+chat.partition(':')[2])
        if (revoker is not None and not trusted_dm) or not (trusted_dm or self._chat_allowed(chat)):
            raise PermissionError('recall origin denied')
        with self.store.connection() as db:
            rows = db.execute('''SELECT DISTINCT s.id,s.owner FROM sources s
                LEFT JOIN life_events e ON e.source_id=s.id AND e.instance_id=?
                WHERE s.domain=? AND s.origin=? AND (s.message_id=? OR e.message_id=?)
                  AND (e.id IS NOT NULL OR EXISTS(SELECT 1 FROM memories m JOIN person_facts p ON p.memory_id=m.id
                    WHERE m.source_id=s.id AND p.instance_id=?)
                    OR EXISTS(SELECT 1 FROM life_semantic_sources x WHERE x.source_id=s.id AND x.instance_id=?)
                    OR EXISTS(SELECT 1 FROM memories m JOIN life_agreements a ON a.memory_id=m.id
                        WHERE m.source_id=s.id AND a.instance_id=?))
                  AND (? IS NULL OR s.owner=?)''',
                (self.config['instance_id'], self.store.domain_id, chat, message_id, message_id,
                 self.config['instance_id'], self.config['instance_id'], self.config['instance_id'], revoker, revoker)).fetchall()
        for row in rows:
            scope = self.store.resolve_scope(self.Scope(self.store.domain_id, 'napcat', row['owner'],
                chat.partition(':')[0], chat, policy_revision=self.config['policy_revision']))
            self.store.forget(scope, source_ids=[row['id']])
            if self.config['conversation']['enabled']:
                self.conversations.prune(self.store.resolve_scope(scope))
                self._prune_group(chat)
        return self._suppressed_messages(chat) if rows else None

    def command(self, route, text):
        """Never called on model replies, quoted messages or an arbitrary tool request."""
        try:
            scope = self.scope(route)
            if not isinstance(text, str) or len(text.encode()) > 2000 or '\n' in text or '\r' in text:
                raise ValueError('invalid person command')
            remember = re.fullmatch(r'/social-memory remember (interest|preference|boundary) (\S(?:.*\S)?)', text)
            experience = re.fullmatch(r'/social-memory episode (\S(?:.*\S)?)', text)
            plan = re.fullmatch(r'/social-memory plan (\S+) (\S(?:.*\S)?)',text)
            if experience:
                if not self.config['explicit_memory']['enabled'] or len(experience[1].encode()) > 480:
                    raise PermissionError('explicit experience is disabled or exceeds budget')
                receipt, offset = self._authorized_source(scope, text, start=experience.start(1))
                ident = self.relations.propose_episode(scope, receipt['source_id'], experience[1], start=offset,
                    ttl_days=self.config['explicit_memory']['ttl_days'])
                return '已保存待本人核对的经历陈述。用 /social-memory review '+ident+' 查看；未替其他参与者确认。'
            if plan:
                if not self.config['agreements']['enabled']:
                    raise PermissionError('agreement collection is disabled')
                self.relations.check_plan(plan[1],plan[2])
                receipt, offset = self._authorized_source(scope, text, start=plan.start(1))
                value_offset = offset+(plan.start(2)-plan.start(1))
                ident = self.relations.propose(scope,receipt['source_id'],plan[1],plan[2],start=value_offset,
                    proactive=self.config['agreements']['proactive'] and route.chat_type == 'group',
                    grace_hours=self.config['agreements']['grace_hours'])
                return '已保存待核对约定。用 /social-memory review '+ident+' 查看时间、场所与主动联系授权。'
            if remember:
                if len(remember[2].encode()) > 480:
                    raise ValueError('person value exceeds limit')
                receipt, offset = self._authorized_source(scope, text, start=remember.start(2))
                ident = self.people.propose(scope, receipt['source_id'], remember[1], remember[2], start=offset,
                    ttl_days=self.config['explicit_memory']['ttl_days'] if self.config['explicit_memory']['enabled'] else None)
                if self.store.get(scope, ident)['state'] != 'pending':
                    return '这条来源的候选已经处理；需要修改时请发送新的自述。'
                return '已保存待核对的本人自述。用 /social-memory review '+ident+' 查看；确认前不会召回。'
            parts = text.split()
            if not parts or parts[0] != COMMAND:
                raise ValueError('wrong person command')
            if parts == [COMMAND,'events']:
                records,_ = self.relations.view(scope,episodes=self.config['episodes']['enabled'],
                    agreements=self.config['agreements']['enabled'], explicit_episodes=self.config['explicit_memory']['enabled'])
                from datetime import datetime,timezone
                return '当前场所的本人经历陈述与约定：\n'+'\n'.join(r['id']+'：'+r['content']+
                    ('；时间：'+datetime.fromtimestamp(r['due'],timezone.utc).isoformat()+'；状态：'+r['status']
                     if r['kind']=='agreement' else '') for r in records)
            if parts in ([COMMAND, 'pending'], [COMMAND, 'mine']):
                rows = self.people.list(scope, pending=parts[1] == 'pending')
                if self.config['agreements']['enabled'] or self.config['explicit_memory']['enabled']:
                    options = dict(agreements=self.config['agreements']['enabled'])
                    rows += (self.relations.pending(scope, episodes=self.config['explicit_memory']['enabled'], **options)
                             if parts[1]=='pending' else self.relations.view(scope, episodes=False,
                                explicit_episodes=self.config['explicit_memory']['enabled'], **options)[0])
                return ('当前场所没有相应的本人资料。' if not rows else
                        '本人当前场所资料：\n'+'\n'.join(row['id']+'：'+row['content'] for row in rows))
            if len(parts) == 3 and parts[1] in ('review', 'forget','cancel','close') and re.fullmatch(r'[a-f0-9]{32}', parts[2]):
                with self.store.connection() as db:
                    agreement = self.relations.owns(db,scope,parts[2])
                    episode = db.execute('SELECT 1 FROM life_episodes WHERE memory_id=? AND instance_id=?',
                        (parts[2],self.config['instance_id'])).fetchone()
                    explicit_episode = self.relations.explicit_episode(db, scope, parts[2])
                if parts[1] in {'cancel','close'}:
                    reference,status = self.relations.close(scope,parts[2],
                        'cancelled' if parts[1]=='cancel' else 'closed_by_member')
                    self._prune(route)
                    return CommandReply('已结束约定并撤销其联系授权；这是本人标记，不代表外部任务结果已核验。',[(reference,status)])
                if parts[1] == 'forget':
                    if agreement is not None or episode is not None:
                        self.store.forget(scope,memory_ids=[parts[2]])
                    else:
                        self.people.forget(scope, parts[2])
                    self._prune(route)
                    return CommandReply('已忘记这条本人资料及其来源；旧核对码与包含它的在途回复已失效。',
                        [(self.relations.reference(parts[2]),'cancelled')] if agreement is not None else [])
                if agreement is not None:
                    from datetime import datetime,timezone
                    card = self.relations.review(scope,parts[2])
                    permission = ('确认同时授权到期后在本群 @ 本人提起一次，仍受空闲、安静时段和统一额度约束。'
                                  if card['contact_allowed'] else '当前只保存约定，不授予主动联系权限。')
                    return (card['content']+'\n时间：'+datetime.fromtimestamp(card['due'],timezone.utc).isoformat()+'\n'+permission+
                        '\n联系有效期至：'+datetime.fromtimestamp(card['expires'],timezone.utc).isoformat()+
                        '\n确认：/social-memory confirm '+card['id']+' '+card['digest']+
                        '\n拒绝：/social-memory reject '+card['id']+' '+card['digest'])
                card = (self.relations.review_episode(scope, parts[2]) if explicit_episode
                        else self.people.review(scope,parts[2]))
                with self.store.connection() as db:
                    provenance = self._provenance(db, scope, card['id'])
                from datetime import datetime, timezone
                expiry = (datetime.fromtimestamp(provenance['expires_at'], timezone.utc).isoformat()
                          if provenance['expires_at'] else '未设置；请核对是否仍适用')
                evidence = '；'.join('消息 '+str(row['message_id'])+' 原文片段：'+row['quote'] for row in provenance['evidence'])
                venue = '本次好友私聊' if route.chat_type == 'dm' else '当前群'
                qualification = '这是待本人核对的经历陈述；确认不代表其他参与者确认或外部事实已经验证。\n' if explicit_episode else ''
                return (qualification+card['content']+'\n来源：'+evidence+'\n有效期至：'+expiry+
                        '\n确认即允许伙伴在'+venue+'回应你时引用；不用于其他场所或无人发言时的主动开口。\n'
                        +'确认：/social-memory confirm '+card['id']+' '+card['digest']
                        +'\n拒绝：/social-memory reject '+card['id']+' '+card['digest'])
            if (len(parts) == 4 and parts[1] in ('confirm', 'reject')
                    and re.fullmatch(r'[a-f0-9]{32}', parts[2]) and re.fullmatch(r'[a-f0-9]{24}', parts[3])):
                with self.store.connection() as db:
                    agreement = self.relations.owns(db,scope,parts[2])
                    explicit_episode = self.relations.explicit_episode(db, scope, parts[2])
                if agreement is not None:
                    self.relations.decide(scope,parts[2],parts[3],confirm=parts[1]=='confirm')
                elif explicit_episode:
                    self.relations.decide_episode(scope,parts[2],parts[3],confirm=parts[1]=='confirm')
                else:
                    self.people.decide(scope, parts[2], parts[3], confirm=parts[1] == 'confirm')
                if parts[1] == 'confirm':
                    with self.store.connection(True) as db:
                        db.execute('INSERT OR REPLACE INTO life_confirmations VALUES (?,?,?,?)',
                                   (parts[2], self.config['instance_id'], route.message_id, time.time()))
                self._prune(route)
                return '已确认本人当前场所资料。' if parts[1] == 'confirm' else '已拒绝这条候选。'
            return ('用法：/social-memory remember interest|preference|boundary <本人自述>；'
                    'episode <本人明确经历>；plan <带时区的ISO时间> <约定>；events；pending；mine；review <ID>；'
                    'confirm|reject <ID> <核对码>；forget|cancel|close <ID>')
        except PermissionError:
            return '无法处理：当前身份、来源或核对码已失效。请在原场所重新核对本人资料。'
        except ValueError:
            return '无法处理：人物记忆命令或配置无效。'
