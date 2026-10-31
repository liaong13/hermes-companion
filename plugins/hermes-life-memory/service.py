"""Local-only person service. Entry points are trusted original-message callbacks."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re

from .core import memory_api
from .conversation import ConversationMemory
from .rules import self_statement
from .relations import Relations

COMMAND = '/social-memory'


def settings(raw):
    allowed = {'enabled', 'instance_id', 'domain_id', 'policy_revision', 'groups', 'conversation', 'candidates',
               'episodes', 'agreements'}
    if not isinstance(raw, dict) or set(raw)-allowed or type(raw.get('enabled', False)) is not bool:
        raise ValueError('invalid life memory settings')
    if raw.get('enabled') is not True:
        return {'enabled': False}
    if (not isinstance(raw.get('instance_id'), str) or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', raw['instance_id']) is None
            or not isinstance(raw.get('domain_id'), str) or not raw['domain_id'] or raw['domain_id'] == 'legacy'
            or len(raw['domain_id']) > 256 or type(raw.get('policy_revision')) is not int or raw['policy_revision'] < 1
            or not isinstance(raw.get('groups'), list) or not raw['groups'] or len(raw['groups']) > 100
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
            or (episodes['enabled'] or agreements['enabled']) and not conversation['enabled']
            or agreements['proactive'] and not agreements['enabled']):
        raise ValueError('explicit conversation and bounded relation policy required')
    return {**raw, 'groups': sorted(set(raw['groups'])), 'conversation': conversation, 'candidates': candidates,
            'episodes':episodes, 'agreements':agreements}


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

    def __init__(self, home, storage_home, config, reader, *, channel_domain, channel_revision):
        self.home, self.config = Path(home).resolve(), settings(config)
        if not self.config['enabled']:
            raise ValueError('life memory is disabled')
        self.groups, self.reader = frozenset(self.config['groups']), reader
        self.channel_binding = channel_domain, channel_revision
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

    def validate(self):
        if not self.active:
            raise PermissionError('life memory unloaded')
        with profile(self.home):
            try:
                current = settings(self.reader())
            except (ValueError, TypeError):
                self.close()
                raise PermissionError('life memory policy invalid; reload required') from None
            if current != self.config:
                self.close()
                raise PermissionError('life memory policy changed; reload required')

    def close(self):
        self.active = False

    def scope(self, route):
        self.validate()
        if (getattr(route, 'platform', '') != 'napcat' or route.chat_type != 'group'
                or (route.domain_id, route.policy_revision) != self.channel_binding
                or re.fullmatch(r'[1-9][0-9]{0,19}', route.user_id) is None
                or route.chat_id not in {'group:'+g for g in self.groups}
                or re.fullmatch(r'-?[0-9]{1,20}', route.message_id) is None
                or getattr(route, 'thread_id', '')):
            raise PermissionError('life memory origin or member denied')
        return self.store.resolve_scope(self.Scope(self.config['domain_id'], 'napcat', 'napcat:'+route.user_id,
            'group', route.chat_id, session_id='person:'+self.config['instance_id'],
            message_id=route.message_id, policy_revision=self.config['policy_revision']))

    def recall(self, route, *, quote_message_id='', include_conversation=True):
        scope = self.scope(route)
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
        if quote_message_id:
            quote = self.conversations.quote_reference(scope, quote_message_id)
            if quote is not None:
                refs += (quote,)
        records, relation_refs = self.relations.view(scope, episodes=self.config['episodes']['enabled'],
                                                   agreements=self.config['agreements']['enabled'])
        refs += relation_refs
        def check():
            self.validate()
            if not person.current():
                raise PermissionError('person view changed')
            self.conversations.check_references(scope, refs)
            with self.store.connection() as db:
                for row in records:
                    if row['kind']=='agreement':
                        current = self.relations.owns(db,scope,row['id'])
                        if current is None or current['status']!=row['status'] or current['due']!=row['due']:
                            raise PermissionError('agreement view changed')
        return SocialMemoryView(person.facts, conversation, refs, check, tuple(records))

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

    def suppressed_messages(self, chat):
        if chat not in {'group:'+g for g in self.groups}:
            raise PermissionError('message scope denied')
        return self.conversations.suppressed(chat)

    def block_delivered(self, route, message_id):
        # The adapter supplies a formerly admitted exact route and a real ACK.
        # Unload/changed policy can only add a tombstone, never store plaintext.
        if (route.chat_id not in {'group:'+g for g in self.groups}
                or (route.domain_id, route.policy_revision) != self.channel_binding
                or re.fullmatch(r'[1-9][0-9]{0,19}', route.user_id) is None):
            raise PermissionError('late delivery scope denied')
        scope = self.Scope(self.config['domain_id'], 'napcat', 'napcat:'+route.user_id,
                           'group', route.chat_id, message_id=route.message_id)
        self.conversations.block_delivered(scope, message_id)

    def _prune(self, route):
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
                scope = self.store.resolve_scope(self.Scope(self.store.domain_id, 'napcat', owner, 'group', chat,
                    policy_revision=self.config['policy_revision']))
                self.conversations.prune(scope)
        raise PermissionError('group dependency cleanup budget exceeded')

    def revoke(self, chat, message_id):
        """Trusted channel recall notification; never a user-selectable tool."""
        self.validate()
        if chat not in {'group:'+g for g in self.groups}:
            raise PermissionError('recall origin denied')
        with self.store.connection() as db:
            rows = db.execute('''SELECT DISTINCT s.id,s.owner FROM sources s
                LEFT JOIN life_events e ON e.source_id=s.id AND e.instance_id=?
                WHERE s.domain=? AND s.origin=? AND (s.message_id=? OR e.message_id=?)
                  AND (e.id IS NOT NULL OR EXISTS(SELECT 1 FROM memories m JOIN person_facts p ON p.memory_id=m.id
                    WHERE m.source_id=s.id AND p.instance_id=?))''',
                (self.config['instance_id'], self.store.domain_id, chat, message_id, message_id,
                 self.config['instance_id'])).fetchall()
        for row in rows:
            scope = self.store.resolve_scope(self.Scope(self.store.domain_id, 'napcat', row['owner'],
                'group', chat, policy_revision=self.config['policy_revision']))
            self.store.forget(scope, source_ids=[row['id']])
            if self.config['conversation']['enabled']:
                self.conversations.prune(self.store.resolve_scope(scope))
                self._prune_group(chat)

    def command(self, route, text):
        """Never called on model replies, quoted messages or an arbitrary tool request."""
        try:
            scope = self.scope(route)
            if not isinstance(text, str) or len(text.encode()) > 2000 or '\n' in text or '\r' in text:
                raise ValueError('invalid person command')
            remember = re.fullmatch(r'/social-memory remember (interest|preference|boundary) (\S(?:.*\S)?)', text)
            plan = re.fullmatch(r'/social-memory plan (\S+) (\S(?:.*\S)?)',text)
            if plan:
                if not self.config['agreements']['enabled']:
                    raise PermissionError('agreement collection is disabled')
                self.relations.check_plan(plan[1],plan[2])
                self.conversations.assert_message_current(scope)
                receipt = self.store.append_turn(scope,text,'')
                self.conversations.remember_receipt(scope,receipt)
                ident = self.relations.propose(scope,receipt['source_id'],plan[1],plan[2],start=plan.start(2),
                    proactive=self.config['agreements']['proactive'],grace_hours=self.config['agreements']['grace_hours'])
                return '已保存待核对约定。用 /social-memory review '+ident+' 查看时间、场所与主动联系授权。'
            if remember:
                if len(remember[2].encode()) > 480:
                    raise ValueError('person value exceeds limit')
                self.conversations.assert_message_current(scope)
                receipt = self.store.append_turn(scope, text, '')
                self.conversations.remember_receipt(scope, receipt)
                ident = self.people.propose(scope, receipt['source_id'], remember[1], remember[2], start=remember.start(2))
                if self.store.get(scope, ident)['state'] != 'pending':
                    return '这条来源的候选已经处理；需要修改时请发送新的自述。'
                return '已保存待核对的本人自述。用 /social-memory review '+ident+' 查看；确认前不会召回。'
            parts = text.split()
            if not parts or parts[0] != COMMAND:
                raise ValueError('wrong person command')
            if parts == [COMMAND,'events']:
                records,_ = self.relations.view(scope,episodes=self.config['episodes']['enabled'],
                                               agreements=self.config['agreements']['enabled'])
                from datetime import datetime,timezone
                return '当前群的本人共同经历与约定：\n'+'\n'.join(r['id']+'：'+r['content']+
                    ('；时间：'+datetime.fromtimestamp(r['due'],timezone.utc).isoformat()+'；状态：'+r['status']
                     if r['kind']=='agreement' else '') for r in records)
            if parts in ([COMMAND, 'pending'], [COMMAND, 'mine']):
                rows = self.people.list(scope, pending=parts[1] == 'pending')
                if self.config['agreements']['enabled']:
                    rows += (self.relations.pending(scope) if parts[1]=='pending'
                             else [r for r in self.relations.view(scope,episodes=False)[0]])
                return ('当前群没有相应的本人资料。' if not rows else
                        '本人当前群资料：\n'+'\n'.join(row['id']+'：'+row['content'] for row in rows))
            if len(parts) == 3 and parts[1] in ('review', 'forget','cancel','close') and re.fullmatch(r'[a-f0-9]{32}', parts[2]):
                with self.store.connection() as db:
                    agreement = self.relations.owns(db,scope,parts[2])
                    episode = db.execute('SELECT 1 FROM life_episodes WHERE memory_id=? AND instance_id=?',
                        (parts[2],self.config['instance_id'])).fetchone()
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
                card = self.people.review(scope,parts[2])
                return (card['content']+'\n确认即允许伙伴在当前群回应你时引用；不用于其他群、私聊或无人发言时的主动开口。\n'
                        +'确认：/social-memory confirm '+card['id']+' '+card['digest']
                        +'\n拒绝：/social-memory reject '+card['id']+' '+card['digest'])
            if (len(parts) == 4 and parts[1] in ('confirm', 'reject')
                    and re.fullmatch(r'[a-f0-9]{32}', parts[2]) and re.fullmatch(r'[a-f0-9]{24}', parts[3])):
                with self.store.connection() as db:
                    agreement = self.relations.owns(db,scope,parts[2])
                if agreement is not None:
                    self.relations.decide(scope,parts[2],parts[3],confirm=parts[1]=='confirm')
                else:
                    self.people.decide(scope, parts[2], parts[3], confirm=parts[1] == 'confirm')
                self._prune(route)
                return '已确认本人当前群资料。' if parts[1] == 'confirm' else '已拒绝这条候选。'
            return ('用法：/social-memory remember interest|preference|boundary <本人自述>；'
                    'plan <带时区的ISO时间> <约定>；events；pending；mine；review <ID>；'
                    'confirm|reject <ID> <核对码>；forget|cancel|close <ID>')
        except PermissionError:
            return '无法处理：当前身份、来源或核对码已失效。请在原群重新核对本人资料。'
        except ValueError:
            return '无法处理：人物记忆命令或配置无效。'
