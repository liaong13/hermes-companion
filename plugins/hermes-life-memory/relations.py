"""Evidence-backed joint conversations and explicit, one-person group agreements.

No model extraction, time guessing or third-person consent. Contact permission
is reviewed separately from merely storing an agreement.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import time

from .core import memory_api


@dataclass(frozen=True)
class AgreementOffer:
    reference: str
    recipient: str
    text: str
    expires_at: float
    _claim: object
    _current: object
    _finish: object
    _record: object
    _block: object

    def current(self):
        try:
            self._current()
            return True
        except (PermissionError, ValueError):
            return False

    def claim(self, attempt): return self._claim(attempt)
    def finish(self, status): return self._finish(status)
    def record_delivery(self, message_id): return self._record(message_id)
    def block_delivery(self, message_id): return self._block(message_id)


class Relations:
    def __init__(self, store, conversations, instance_id):
        self.store, self.conversations, self.instance_id = store, conversations, instance_id
        with store.connection(True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS life_episodes (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                instance_id TEXT NOT NULL, topic_id TEXT NOT NULL, UNIQUE(instance_id,topic_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS life_explicit_episodes (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                instance_id TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS life_agreements (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                instance_id TEXT NOT NULL, due REAL NOT NULL, expires REAL NOT NULL,
                contact_allowed INTEGER NOT NULL CHECK(contact_allowed IN (0,1)),
                status TEXT NOT NULL DEFAULT 'planned', attempt TEXT NOT NULL DEFAULT '')''')

    def capture_episode(self, scope, *, retention_days):
        with self.store.connection(True) as db:
            rows = [r for r in self.conversations._rows(db, scope) if self.conversations._valid(db, scope, r)]
            closing = next((r for r in rows if r['role']=='member' and r['message_id']==scope.message_id
                            and r['kind']=='closing'), None)
            if closing is None:
                return None
            rows = [r for r in rows if r['topic_id']==closing['topic_id']][:16]
            if not {'member','self'} <= {r['role'] for r in rows}:
                return None
            old = db.execute('SELECT memory_id FROM life_episodes WHERE instance_id=? AND topic_id=?',
                             (self.instance_id, closing['topic_id'])).fetchone()
            if old is not None:
                return old[0]
            sources = [(r, self.store.owned(db, 'sources', r['source_id'], scope)) for r in reversed(rows)]
            excerpts = [(r, s, memory_api().clip(s['user_text'] if r['role']=='member' else s['assistant_text'], 240))
                        for r,s in sources]
            content = '同群交流摘录：'+'；'.join(('本人：' if r['role']=='member' else '伙伴：')+text for r,s,text in excerpts)
            anchor = self.store.owned(db, 'sources', closing['source_id'], scope)
            ident = self.store.evidenced_in(db, scope, content, anchor['id'], role='user', start=0,
                end=len(anchor['user_text']), quote=anchor['user_text'],
                associations={'event':['life:'+self.instance_id+':episode']}, ttl_days=retention_days, state='active')
            for r,s,text in excerpts:
                role = 'user' if r['role']=='member' else 'assistant'
                if s['id'] != anchor['id']:
                    db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)',
                        (ident,s['id'],s['revision'],role,0,len(text),'characters',s['sha256']))
            db.execute('INSERT INTO life_episodes VALUES (?,?,?)', (ident,self.instance_id,closing['topic_id']))
            return ident

    def propose_episode(self, scope, source_id, value, *, start, ttl_days):
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > 480:
            raise ValueError('bounded explicit experience required')
        with self.store.connection(True) as db:
            old = db.execute('''SELECT m.id FROM memories m JOIN life_explicit_episodes e ON e.memory_id=m.id
                WHERE m.domain=? AND m.owner=? AND m.origin=? AND m.source_id=? AND e.instance_id=?''',
                (self.store.domain_id, *scope.pair, source_id, self.instance_id)).fetchone()
            if old is not None:
                return old[0]
            count = db.execute('''SELECT COUNT(*) FROM memories m JOIN life_explicit_episodes e ON e.memory_id=m.id
                WHERE m.domain=? AND m.owner=? AND m.origin=? AND e.instance_id=?
                  AND m.state IN ('active','pending') AND (m.expires IS NULL OR m.expires>?)''',
                (self.store.domain_id, *scope.pair, self.instance_id, time.time())).fetchone()[0]
            if count >= 8:
                raise ValueError('explicit experience limit reached')
            ident = self.store.evidenced_in(db, scope, '本人经历陈述：'+value, source_id, role='user', start=start,
                end=start+len(value), quote=value, associations={'event':['life:'+self.instance_id+':episode']},
                ttl_days=ttl_days)
            db.execute('INSERT INTO life_episodes VALUES (?,?,?)', (ident, self.instance_id, 'explicit:'+source_id))
            db.execute('INSERT INTO life_explicit_episodes VALUES (?,?)', (ident, self.instance_id))
            return ident

    def explicit_episode(self, db, scope, ident):
        self.store.owned(db, 'memories', ident, scope)
        return db.execute('SELECT * FROM life_explicit_episodes WHERE memory_id=? AND instance_id=?',
                          (ident, self.instance_id)).fetchone() is not None

    def _review_episode(self, db, scope, ident):
        if not self.explicit_episode(db, scope, ident):
            raise PermissionError('explicit experience not in scope')
        row, digest = self.store.candidate_in(db, scope, ident)
        digest = hashlib.sha256(json.dumps([digest, self.instance_id, scope.pair, 'explicit_episode']).encode()).hexdigest()[:24]
        return dict(id=ident, content=row['content'], digest=digest)

    def review_episode(self, scope, ident):
        with self.store.connection(True) as db:
            return self._review_episode(db, scope, ident)

    def decide_episode(self, scope, ident, digest, *, confirm):
        with self.store.connection(True) as db:
            card = self._review_episode(db, scope, ident)
            if card['digest'] != digest:
                raise PermissionError('explicit experience review expired')
            db.execute('UPDATE memories SET state=? WHERE id=?', ('active' if confirm else 'rejected', ident))
        return card

    @staticmethod
    def check_plan(due_text,value):
        due = datetime.fromisoformat(due_text)
        if due.tzinfo is None or not 0 < due.timestamp()-time.time() <= 90*86400:
            raise ValueError('explicit timezone and a future time within 90 days required')
        if not value.strip() or len(value.encode()) > 360:
            raise ValueError('bounded agreement text required')
        return due

    def propose(self, scope, source_id, due_text, value, *, start, proactive, grace_hours):
        due = self.check_plan(due_text,value)
        expires = due.timestamp()+grace_hours*3600
        with self.store.connection(True) as db:
            old = db.execute('''SELECT m.id FROM memories m JOIN life_agreements a ON a.memory_id=m.id
                WHERE m.domain=? AND m.owner=? AND m.origin=? AND m.source_id=? AND a.instance_id=?''',
                (self.store.domain_id,*scope.pair,source_id,self.instance_id)).fetchone()
            if old is not None:
                return old[0]
            count = db.execute('''SELECT COUNT(*) FROM memories m JOIN life_agreements a ON a.memory_id=m.id
                WHERE m.domain=? AND m.owner=? AND m.origin=? AND a.instance_id=?
                  AND m.state IN ('active','pending') AND a.expires>?''',
                (self.store.domain_id,*scope.pair,self.instance_id,time.time())).fetchone()[0]
            if count>=8: raise ValueError('agreement limit reached')
            ident = self.store.evidenced_in(db, scope, '约定：'+value, source_id, role='user', start=start,
                end=start+len(value), quote=value, associations={'event':['life:'+self.instance_id+':agreement']},
                ttl_days=(expires-time.time())/86400)
            db.execute('INSERT INTO life_agreements VALUES (?,?,?,?,?,\'planned\',\'\')',
                       (ident,self.instance_id,due.timestamp(),expires,int(proactive)))
            return ident

    def owns(self, db, scope, ident):
        self.store.owned(db, 'memories', ident, scope)
        return db.execute('SELECT * FROM life_agreements WHERE memory_id=? AND instance_id=?',
                          (ident,self.instance_id)).fetchone()

    def _review(self, db, scope, ident):
        agreement = self.owns(db, scope, ident)
        if agreement is None:
            raise PermissionError('agreement not in scope')
        row, digest = self.store.candidate_in(db, scope, ident)
        digest = hashlib.sha256(json.dumps([digest,dict(agreement)],sort_keys=True).encode()).hexdigest()[:24]
        return {**dict(agreement), 'id':ident, 'content':row['content'], 'digest':digest}

    def review(self, scope, ident):
        with self.store.connection(True) as db: return self._review(db, scope, ident)

    def decide(self, scope, ident, digest, *, confirm):
        with self.store.connection(True) as db:
            row = self._review(db, scope, ident)
            if row['digest'] != digest or row['expires'] <= time.time():
                raise PermissionError('agreement review expired')
            db.execute('UPDATE memories SET state=? WHERE id=?', ('active' if confirm else 'rejected',ident))
        return row

    def close(self, scope, ident, status):
        if status not in {'cancelled','closed_by_member'}:
            raise ValueError('invalid agreement closure')
        with self.store.connection(True) as db:
            row = self.owns(db, scope, ident)
            if row is None:
                raise PermissionError('agreement not in scope')
        # Cancellation is a durable revocation, including restored content DBs.
        # Retire its consent source through the independent deletion ledger.
        self.store.forget(scope, memory_ids=[ident])
        return self.reference(ident), status

    def reference(self, ident):
        return hashlib.sha256((self.instance_id+':'+self.store.domain_id+':'+ident).encode()).hexdigest()

    def view(self, scope, *, episodes=True, agreements=True, explicit_episodes=False):
        records, refs = [], []
        with self.store.connection() as db:
            self.store.validate_scope(db, scope)
            for table,kind,enabled in (('life_agreements','agreement',agreements),('life_episodes','episode',episodes or explicit_episodes)):
                if not enabled: continue
                explicit_filter = (" AND EXISTS(SELECT 1 FROM life_explicit_episodes x WHERE x.memory_id=m.id AND x.instance_id=r.instance_id)"
                                   if kind == 'episode' and not episodes else '')
                rows = db.execute('''SELECT m.* FROM memories m JOIN '''+table+''' r ON r.memory_id=m.id
                    WHERE m.domain=? AND m.owner=? AND m.origin=? AND r.instance_id=? AND m.state='active'
                      AND (m.expires IS NULL OR m.expires>?)'''+explicit_filter+''' ORDER BY m.created DESC LIMIT 4''',
                    (self.store.domain_id,*scope.pair,self.instance_id,time.time())).fetchall()
                for row in rows:
                    try:
                        item_refs = [self.conversations._reference(db,scope,'memory',row['id'])]
                        item_refs += [self.conversations._reference(db,scope,'source',e['source_id'],e['source_revision'],archival=True)
                            for e in db.execute('SELECT * FROM evidence WHERE memory_id=?',(row['id'],))]
                    except PermissionError:
                        continue
                    item = {'id':row['id'],'kind':kind,'content':memory_api().clip(row['content'],360)}
                    evidence = []
                    for proof in db.execute('''SELECT e.start,e.end,s.message_id,s.created,s.user_text FROM evidence e
                        JOIN sources s ON s.id=e.source_id WHERE e.memory_id=? ORDER BY s.created LIMIT 4''', (row['id'],)):
                        evidence.append(dict(message_id=proof['message_id'], observed_at=proof['created'],
                                             quote=(proof['user_text'] or '')[proof['start']:proof['end']]))
                    item.update(observed_at=row['created'], expires_at=row['expires'], evidence=evidence)
                    if kind == 'episode':
                        item['verification'] = ('subject_confirmed_statement' if self.explicit_episode(db, scope, row['id'])
                                                else 'literal_conversation_excerpt')
                    if kind=='agreement':
                        plan = self.owns(db,scope,row['id'])
                        item.update(due=plan['due'],status=plan['status'])
                    if len(records)<4 and len(json.dumps([*records,item],ensure_ascii=False).encode())<=2400:
                        records.append(item)
                        refs.extend(item_refs)
        return records, tuple(refs)

    def pending(self,scope, *, agreements=True, episodes=False):
        with self.store.connection() as db:
            self.store.validate_scope(db,scope)
            rows = [dict(row) for row in db.execute('''SELECT m.id,m.content FROM memories m
                JOIN life_agreements a ON a.memory_id=m.id WHERE m.domain=? AND m.owner=? AND m.origin=?
                  AND a.instance_id=? AND m.state='pending' AND a.expires>? ORDER BY m.created LIMIT 8''',
                (self.store.domain_id,*scope.pair,self.instance_id,time.time()))] if agreements else []
            if episodes:
                rows += [dict(row) for row in db.execute('''SELECT m.id,m.content FROM memories m
                    JOIN life_explicit_episodes e ON e.memory_id=m.id
                    WHERE m.domain=? AND m.owner=? AND m.origin=? AND e.instance_id=?
                      AND m.state='pending' AND (m.expires IS NULL OR m.expires>?) ORDER BY m.created LIMIT 8''',
                    (self.store.domain_id,*scope.pair,self.instance_id,time.time()))]
            return rows[:8]

    def due(self, chat, scope_factory, validate, *, excluded=frozenset()):
        validate()
        with self.store.connection() as db:
            rows = db.execute('''SELECT m.id,m.owner,a.due FROM memories m JOIN life_agreements a ON a.memory_id=m.id
                WHERE m.domain=? AND m.origin=? AND a.instance_id=? AND m.state='active'
                  AND a.contact_allowed=1 AND a.status='planned' AND a.due<=? AND a.expires>?
                ORDER BY a.due,m.created LIMIT 128''', (self.store.domain_id,chat,self.instance_id,time.time(),time.time())).fetchall()
        for selected in rows:
            reference = self.reference(selected['id'])
            if reference in excluded:
                continue
            try:
                scope = scope_factory(selected['owner'])
                with self.store.connection() as db:
                    memory = self.store.owned(db,'memories',selected['id'],scope)
                    self.conversations._reference(db,scope,'memory',memory['id'])
                    plan = dict(self.owns(db,scope,memory['id']))
            except PermissionError:
                continue
            recipient = scope.principal_id.partition(':')[2]
            text = ('之前约好继续的事情到时间了：'+memory['content'][3:]+
                    '。现在方便的话，我们可以接着聊。\n取消这条约定：/social-memory cancel '+memory['id'])
            claim_id = [None]
            def current(scope=scope, ident=memory['id'], plan=plan):
                validate()
                with self.store.connection() as db:
                    self.conversations._reference(db,scope,'memory',ident)
                    row = self.owns(db,scope,ident)
                    if (row['due'] != plan['due'] or row['expires']<=time.time() or not row['contact_allowed']
                            or row['status'] != ('planned' if claim_id[0] is None else 'attempting')
                            or row['attempt'] != (claim_id[0] or '')):
                        raise PermissionError('agreement contact permission changed')
            def claim(attempt, scope=scope, ident=memory['id']):
                current()
                with self.store.connection(True) as db:
                    current_row = self.owns(db,scope,ident)
                    if current_row['status']!='planned': raise PermissionError('agreement already attempted')
                    db.execute("UPDATE life_agreements SET status='attempting',attempt=? WHERE memory_id=?",(attempt,ident))
                claim_id[0] = attempt
            def finish(status, scope=scope, ident=memory['id']):
                validate()
                if status not in {'notified','unknown'}: raise ValueError('invalid delivery result')
                with self.store.connection(True) as db:
                    self.store.validate_scope(db,scope)
                    db.execute("UPDATE life_agreements SET status=? WHERE memory_id=? AND status='attempting' AND attempt=?",
                               (status,ident,claim_id[0]))
            def record(message_id, scope=scope, ident=memory['id'], text=text):
                try:
                    current()
                    with self.store.connection() as db:
                        refs = [self.conversations._reference(db,scope,'memory',ident)]
                        refs += [self.conversations._reference(db,scope,'source',e['source_id'],e['source_revision'],archival=True)
                            for e in db.execute('SELECT * FROM evidence WHERE memory_id=?',(ident,))]
                    self.conversations.record_delivery(scope,message_id,text,refs,ident)
                    return True
                except PermissionError:
                    self.conversations.block_delivered(scope,message_id)
                    return False
            def block(message_id, scope=scope):
                self.conversations.block_delivered(scope,message_id)
            return AgreementOffer(reference,recipient,text,plan['expires'],claim,current,finish,record,block)
        return None
