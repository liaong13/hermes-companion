"""Bounded dialogue metadata and deferred opportunities; never stores send grants.

Only authenticated caller observations may enter this module. A model may link
those observations, but cannot create messages, identities, or transport rights.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .context import ContextScope


@dataclass(frozen=True)
class TopicSnapshot:
    topic_id: str
    anchor_id: str
    participants: tuple[str, ...]
    message_ids: tuple[str, ...]
    awaiting_reply: tuple[str, ...]
    status: str
    revision: int
    expires_at: float


@dataclass(frozen=True)
class PendingOpportunity:
    message_id: str
    topic_id: str
    actor_id: str
    audience: str
    reason: str
    status: str
    created_at: float
    expires_at: float
    revision: int


@dataclass(frozen=True)
class Reconsideration:
    trigger: object
    source: object
    opportunities: tuple[PendingOpportunity, ...]
    _check: object

    def current(self):
        try:
            return bool(self._check())
        except (ValueError, PermissionError, sqlite3.Error):
            return False


def _ident(value, *, empty=False):
    if (not isinstance(value, str) or (not value and not empty)
            or len(value) > 256 or value != value.strip()
            or any(ord(char) < 32 for char in value)):
        raise ValueError('invalid dialogue reference')
    return value


def _route(event):
    source = event.source
    return tuple(str(getattr(source, key, '')) for key in
                 ('chat_id', 'chat_type', 'user_id', 'message_id'))


class DialogueState:
    """Per-scope local references; a separate revocation fence survives rollback.

    Content state is bounded per chat. The control ledger retains only revoked
    message references, so restoring an old content database cannot undo forget.
    No original text, nicknames, model reasoning, or event/source objects enter
    either database. Live source bindings are process-local and short-lived.
    """

    def __init__(self, path, *, topic_ttl=86400, opportunity_ttl=3600,
                 max_topics=32, max_messages=256, max_pending=32, clock=time.time):
        self.path = Path(path).absolute()
        self.control_path = self.path.with_name(self.path.name+'.control.sqlite3')
        if (any(isinstance(value,bool) for value in (topic_ttl,opportunity_ttl,max_topics,max_messages,max_pending))
                or not 60 <= topic_ttl <= 604800 or not 5 <= opportunity_ttl <= topic_ttl
                or not 1 <= max_topics <= 128 or not max_topics <= max_messages <= 2048
                or not 1 <= max_pending <= max_messages):
            raise ValueError('invalid dialogue bounds')
        self.topic_ttl, self.opportunity_ttl = topic_ttl, opportunity_ttl
        self.max_topics, self.max_messages, self.max_pending = max_topics, max_messages, max_pending
        self.clock = clock
        self._bindings = {}
        self._epochs = {}
        self._closed = False
        if self.path.parent.resolve() != self.path.parent:
            raise ValueError('dialogue directory must not be a symlink')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = self.path.parent.stat()
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o022:
            raise ValueError('dialogue directory must be owner controlled')
        for filename in (self.path, self.control_path):
            if filename.is_symlink():
                raise ValueError('dialogue file must not be a symlink')
            if filename.exists():
                stat = filename.stat()
                if not filename.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                    raise ValueError('dialogue storage permissions invalid')
            else:
                descriptor = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
        with self._connection(self.control_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS revoked(scope TEXT NOT NULL, message_id TEXT NOT NULL,'
                       'created REAL NOT NULL, PRIMARY KEY(scope,message_id))')
        with self._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS topics(
                    scope TEXT NOT NULL,id TEXT NOT NULL,anchor TEXT NOT NULL,status TEXT NOT NULL,
                    created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL,revision INTEGER NOT NULL,
                    PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS messages(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,id TEXT NOT NULL,
                    actor TEXT NOT NULL,role TEXT NOT NULL,observed REAL NOT NULL,topic TEXT NOT NULL,
                    parent TEXT NOT NULL,reply_to TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,audience TEXT NOT NULL,awaiting INTEGER NOT NULL,
                    action TEXT NOT NULL,selected INTEGER NOT NULL,topic_action TEXT NOT NULL,
                    revision INTEGER NOT NULL,UNIQUE(scope,id));
                CREATE INDEX IF NOT EXISTS dialogue_messages_topic ON messages(scope,topic,seq);
                CREATE TABLE IF NOT EXISTS opportunities(
                    scope TEXT NOT NULL,message_id TEXT NOT NULL,topic TEXT NOT NULL,status TEXT NOT NULL,
                    created REAL NOT NULL,expires REAL NOT NULL,revision INTEGER NOT NULL,
                    PRIMARY KEY(scope,message_id));
            ''')
            if 'reply_to' not in {row['name'] for row in db.execute('PRAGMA table_info(messages)')}:
                db.execute("ALTER TABLE messages ADD COLUMN reply_to TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connection(self, filename=None):
        if self._closed:
            raise ValueError('dialogue state closed')
        db = sqlite3.connect(filename or self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _scope(self, scope):
        if (not isinstance(scope, ContextScope)
                or scope.chat_id.startswith('group:') and scope.subject_id):
            raise ValueError('invalid dialogue scope')
        return hashlib.sha256(json.dumps((scope.profile_home, scope.platform,
                                         scope.chat_id, scope.subject_id)).encode()).hexdigest()

    def _revoked(self, key):
        with self._connection(self.control_path) as db:
            return {row[0] for row in db.execute('SELECT message_id FROM revoked WHERE scope=?', (key,))}

    def _repair(self, db, key):
        revoked = self._revoked(key)
        rows = list(db.execute('SELECT id,parent FROM messages WHERE scope=?', (key,)))
        while True:
            derived = {row['id'] for row in rows if row['parent'] in revoked}
            if derived <= revoked:
                break
            revoked.update(derived)
        if revoked:
            # 旧内容库可能包含当前库未见过的派生记录，恢复时也补入独立撤销栅栏。
            with self._connection(self.control_path) as controls:
                controls.executemany('INSERT OR IGNORE INTO revoked VALUES(?,?,?)',
                                     ((key,mid,self.clock()) for mid in revoked))
            db.executemany('DELETE FROM messages WHERE scope=? AND id=?', ((key, mid) for mid in revoked))
            db.executemany('DELETE FROM opportunities WHERE scope=? AND message_id=?', ((key, mid) for mid in revoked))
            db.executemany('DELETE FROM topics WHERE scope=? AND anchor=?', ((key, mid) for mid in revoked))
        self._prune(db, key)

    def _prune(self, db, key, *, protect_topic=None):
        now = self.clock()
        db.execute('DELETE FROM topics WHERE scope=? AND expires<=?', (key, now))
        db.execute('DELETE FROM opportunities WHERE scope=? AND expires<=?', (key, now))
        topics = list(db.execute('''SELECT t.*,COALESCE(MAX(m.seq),0) AS last_seq,
            COALESCE(MAX(m.awaiting),0) AS awaiting,
            COALESCE(MAX(CASE WHEN m.kind='unassessed' THEN 1 ELSE 0 END),0) AS unassessed,
            EXISTS(SELECT 1 FROM opportunities o WHERE o.scope=t.scope AND o.topic=t.id
                   AND o.status IN ('waiting','deferred')) AS pending
            FROM topics t LEFT JOIN messages m
            ON m.scope=t.scope AND m.topic=t.id WHERE t.scope=? GROUP BY t.id
            ''', (key,)))
        # 活跃待答先于突发噪声；本轮新来源仍需保留，不能返回已被淘汰的 snapshot。
        topics.sort(key=lambda row:(row['id'] == protect_topic,
            row['status'] == 'active' and bool(row['awaiting'] or row['pending']),
            row['status'] == 'active' and bool(row['unassessed']),
            row['status'] == 'active',row['updated'],row['last_seq']),reverse=True)
        db.executemany('DELETE FROM topics WHERE scope=? AND id=?', ((key,row['id']) for row in topics[self.max_topics:]))
        db.execute('DELETE FROM messages WHERE scope=? AND topic NOT IN (SELECT id FROM topics WHERE scope=?)', (key,key))
        db.execute('DELETE FROM opportunities WHERE scope=? AND topic NOT IN (SELECT id FROM topics WHERE scope=?)', (key,key))
        rows = list(db.execute('''SELECT m.id,t.anchor FROM messages m JOIN topics t
            ON t.scope=m.scope AND t.id=m.topic WHERE m.scope=? ORDER BY m.seq DESC''', (key,)))
        keep = {row['anchor'] for row in rows}
        for row in rows:
            if len(keep) >= self.max_messages:
                break
            keep.add(row['id'])
        db.executemany('DELETE FROM messages WHERE scope=? AND id=?',
                       ((key,row['id']) for row in rows if row['id'] not in keep))
        db.execute('DELETE FROM opportunities WHERE scope=? AND message_id NOT IN '
                   '(SELECT id FROM messages WHERE scope=?)', (key,key))
        pending = list(db.execute('SELECT message_id FROM opportunities WHERE scope=? '
                                 "AND status IN ('waiting','deferred') ORDER BY created DESC,message_id", (key,)))
        db.executemany('DELETE FROM opportunities WHERE scope=? AND message_id=?',
                       ((key,row[0]) for row in pending[self.max_pending:]))

    def _message(self, db, key, message_id):
        row = db.execute('SELECT * FROM messages WHERE scope=? AND id=?', (key,message_id)).fetchone()
        if row is None or message_id in self._revoked(key):
            raise ValueError('dialogue source unavailable')
        return row

    def _snapshot(self, db, key, topic):
        head = db.execute('SELECT * FROM topics WHERE scope=? AND id=?', (key,topic)).fetchone()
        if head is None:
            raise ValueError('dialogue topic unavailable')
        rows = list(db.execute('SELECT * FROM messages WHERE scope=? AND topic=? ORDER BY seq', (key,topic)))
        return TopicSnapshot(topic, head['anchor'], tuple(sorted({row['actor'] for row in rows if row['actor']})),
            tuple(row['id'] for row in rows), tuple(row['id'] for row in rows if row['awaiting']),
            head['status'], head['revision'], head['expires'])

    def observe(self, scope, message_id, actor_id, *, role='member', reply_to='',
                verified_reply=False, observed_at=None):
        key = self._scope(scope)
        _ident(message_id)
        _ident(actor_id, empty=role == 'self')
        _ident(reply_to, empty=True)
        if role not in {'member','self'} or type(verified_reply) is not bool:
            raise ValueError('invalid dialogue observation')
        now = self.clock()
        observed = now if observed_at is None else float(observed_at)
        if not now-self.topic_ttl < observed <= now:
            raise ValueError('dialogue observation outside time bounds')
        if message_id in self._revoked(key):
            raise ValueError('dialogue source revoked')
        with self._connection() as db:
            self._repair(db,key)
            existing = db.execute('SELECT * FROM messages WHERE scope=? AND id=?', (key,message_id)).fetchone()
            if existing is not None:
                if existing['actor'] != actor_id or existing['role'] != role:
                    raise ValueError('dialogue source identity conflict')
                return self._snapshot(db,key,existing['topic'])
            anchor = None
            if verified_reply and reply_to:
                anchor = self._message(db,key,reply_to)
                if anchor['observed'] > observed:
                    raise ValueError('dialogue reply points into future')
            topic = anchor['topic'] if anchor else uuid.uuid4().hex
            if anchor is None:
                db.execute('INSERT INTO topics VALUES(?,?,?,?,?,?,?,?)',
                           (key,topic,message_id,'active',observed,observed,observed+self.topic_ttl,1))
            db.execute('''INSERT INTO messages(scope,id,actor,role,observed,topic,parent,reply_to,kind,audience,
                awaiting,action,selected,topic_action,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key,message_id,actor_id,role,observed,topic,reply_to if anchor else '',
                 reply_to if verified_reply else '', 'unassessed','unknown',0,'observe',0,'continue',1))
            db.execute('UPDATE topics SET updated=?,expires=? WHERE scope=? AND id=?',
                       (now,now+self.topic_ttl,key,topic))
            self._prune(db,key,protect_topic=topic)
            return self._snapshot(db,key,topic)

    def observe_event(self, scope, event):
        expected = (scope.chat_id, 'group' if scope.chat_id.startswith('group:') else 'dm',
                    str(event.user_id), str(event.message_id))
        if _route(event) != expected or not isinstance(event.text,str):
            raise ValueError('dialogue event and source differ')
        key = self._scope(scope)
        existing = self._bindings.get((key,str(event.message_id)))
        with self._connection() as db:
            stored = db.execute('SELECT 1 FROM messages WHERE scope=? AND id=?',
                                (key,str(event.message_id))).fetchone()
        if stored is not None and (existing is None or existing[0] is not event or existing[1] is not event.source):
            raise ValueError('historic dialogue source cannot become fresh ingress')
        reply_to = str(getattr(event,'reply_to_message_id','') or '')
        verified = getattr(event,'verified_reply_author_id',None) is not None
        if not verified or not reply_to or not self.reference_metadata(scope,[reply_to]):
            reply_to, verified = '',False
        result = self.observe(scope,str(event.message_id),str(event.user_id),
                              reply_to=reply_to,verified_reply=verified)
        now = self.clock()
        self._bindings = {ident: binding for ident,binding in self._bindings.items() if binding[4] > now}
        self._bindings[(key,str(event.message_id))] = (event,event.source,expected,event.text,now+60)
        while len(self._bindings) > 512:
            self._bindings.pop(next(iter(self._bindings)))
        return result

    def assess(self, scope, message_id, *, topic_anchor=None, action='observe', reason='no_contribution',
               audience='unknown', selected=False, topic_action='continue'):
        key = self._scope(scope)
        if (action not in {'respond','observe','wait'} or audience not in {'bot','group','member','unknown'}
                or reason not in {'question','sharing','followup','correction','joke','closing',
                                  'addressed_elsewhere','unclear','no_contribution'}
                or topic_action not in {'continue','close','reopen','correct'} or type(selected) is not bool
                or selected and action != 'respond'
                or action == 'respond' and (audience not in {'bot','group'}
                    or reason in {'closing','addressed_elsewhere','unclear','no_contribution'})):
            raise ValueError('invalid dialogue assessment')
        _ident(message_id)
        _ident(topic_anchor,empty=True) if topic_anchor is not None else None
        now = self.clock()
        with self._connection() as db:
            self._repair(db,key)
            current = self._message(db,key,message_id)
            anchor = self._message(db,key,topic_anchor) if topic_anchor else current
            if anchor['observed'] > current['observed'] or anchor['seq'] > current['seq']:
                raise ValueError('dialogue topic points into future')
            topic = anchor['topic']
            head = db.execute('SELECT * FROM topics WHERE scope=? AND id=?',(key,topic)).fetchone()
            old_topic = current['topic']
            parent = anchor['id'] if anchor['id'] != message_id else current['parent']
            awaiting = int(reason == 'question' and audience in {'bot','group'})
            if topic_action == 'correct' and anchor['id'] != message_id:
                awaiting = max(awaiting,int(anchor['awaiting'] and audience in {'bot','group'}))
                db.execute('UPDATE messages SET awaiting=0,revision=revision+1 WHERE scope=? AND id=?', (key,anchor['id']))
                db.execute("UPDATE opportunities SET status='superseded',revision=revision+1 WHERE scope=? AND message_id=?",
                           (key,anchor['id']))
            db.execute('''UPDATE messages SET topic=?,parent=?,kind=?,audience=?,awaiting=?,action=?,selected=?,
                       topic_action=?,revision=revision+1 WHERE scope=? AND id=?''',
                       (topic,parent,reason,audience,awaiting,action,int(selected),topic_action,key,message_id))
            status = 'closed' if topic_action == 'close' or reason == 'closing' else head['status']
            if topic_action == 'reopen':
                status = 'active'
            db.execute('UPDATE topics SET status=?,updated=?,expires=?,revision=revision+1 WHERE scope=? AND id=?',
                       (status,now,now+self.topic_ttl,key,topic))
            if status == 'closed':
                db.execute('UPDATE messages SET awaiting=0 WHERE scope=? AND topic=?',(key,topic))
                db.execute("UPDATE opportunities SET status='closed',revision=revision+1 WHERE scope=? AND topic=?",(key,topic))
            pending_status = ('selected' if selected else 'waiting' if action == 'wait'
                              else 'deferred' if action == 'respond' else 'observed')
            if status == 'closed':
                pending_status = 'closed'
            db.execute('''INSERT INTO opportunities VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,message_id)
                       DO UPDATE SET topic=excluded.topic,status=excluded.status,revision=opportunities.revision+1''',
                       (key,message_id,topic,pending_status,now,min(now+self.opportunity_ttl,head['expires']),1))
            if old_topic != topic:
                db.execute('DELETE FROM topics WHERE scope=? AND id=? AND NOT EXISTS '
                           '(SELECT 1 FROM messages WHERE scope=? AND topic=?)',(key,old_topic,key,old_topic))
            self._prune(db,key)
            return self._snapshot(db,key,topic)

    def record_reply(self, scope, message_id, reply_to, actor_id='', *, resolves=(), quoted=True):
        key = self._scope(scope)
        _ident(message_id)
        _ident(actor_id,empty=True)
        if type(quoted) is not bool:
            raise ValueError('invalid dialogue reply evidence')
        actual_reply_to = reply_to if quoted else ''
        resolved = {_ident(mid) for mid in (reply_to,*resolves)}
        with self._connection() as db:
            self._repair(db,key)
            anchor = self._message(db,key,reply_to)
            for mid in resolved:
                source = self._message(db,key,mid)
                if source['topic'] != anchor['topic']:
                    raise ValueError('reply cannot resolve another topic')
            if message_id in self._revoked(key):
                raise ValueError('dialogue reply source revoked')
            prior = db.execute('SELECT * FROM messages WHERE scope=? AND id=?',(key,message_id)).fetchone()
            if prior is not None and (prior['actor'] != actor_id or prior['role'] != 'self'
                                      or prior['parent'] != reply_to or prior['reply_to'] != actual_reply_to):
                raise ValueError('dialogue reply source identity conflict')
            now = self.clock()
            if prior is None:
                db.execute('''INSERT INTO messages(scope,id,actor,role,observed,topic,parent,reply_to,kind,audience,
                    awaiting,action,selected,topic_action,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (key,message_id,actor_id,'self',now,anchor['topic'],reply_to,actual_reply_to,
                     'reply','bot',0,'observe',0,'continue',1))
            db.executemany('UPDATE messages SET awaiting=0,revision=revision+1 WHERE scope=? AND id=?',
                           ((key,mid) for mid in resolved))
            db.executemany("UPDATE opportunities SET status='answered',revision=revision+1 WHERE scope=? AND message_id=?",
                           ((key,mid) for mid in resolved))
            db.execute('UPDATE topics SET revision=revision+1,updated=?,expires=? WHERE scope=? AND id=?',
                       (now,now+self.topic_ttl,key,anchor['topic']))
            self._prune(db,key)
            return self._snapshot(db,key,anchor['topic'])

    def context(self, scope):
        key = self._scope(scope)
        with self._connection() as db:
            self._repair(db,key)
            ids = [row[0] for row in db.execute('SELECT id FROM topics WHERE scope=? ORDER BY updated DESC,id',(key,))]
            return tuple(self._snapshot(db,key,topic) for topic in ids)

    def reference_metadata(self, scope, message_ids):
        """Return exact-scope live source metadata, never text or model links.

        Missing, expired and revoked references are omitted. ``reply_to`` is
        transport-verified evidence; ``messages.parent`` may instead be a model
        topic link and deliberately is not exported as a real reply relation.
        """
        key = self._scope(scope)
        if not isinstance(message_ids,(list,tuple,set,frozenset)) or len(message_ids) > 16:
            raise ValueError('dialogue metadata request exceeds reference budget')
        ids = tuple(dict.fromkeys(_ident(mid) for mid in message_ids))
        with self._connection() as db:
            self._repair(db,key)
            rows = [db.execute('SELECT id,actor,role,observed,reply_to FROM messages WHERE scope=? AND id=?',
                               (key,mid)).fetchone() for mid in ids]
            return tuple(dict(message_id=row['id'],actor_id=row['actor'],role=row['role'],
                              observed_at=row['observed'],reply_to=row['reply_to'])
                         for row in rows if row is not None)

    def pending(self, scope, event):
        key, mid, now = self._scope(scope), str(event.message_id), self.clock()
        binding = self._bindings.get((key,mid))
        if (binding is None or binding[0] is not event or binding[1] is not event.source
                or _route(event) != binding[2] or str(event.user_id) != binding[2][2]
                or str(event.message_id) != binding[2][3]
                or event.text != binding[3] or binding[4] <= now):
            raise ValueError('pending dialogue requires a fresh authenticated event')
        with self._connection() as db:
            self._repair(db,key)
            trigger = self._message(db,key,mid)
            rows = list(db.execute('''SELECT o.*,m.actor,m.audience,m.kind FROM opportunities o
                JOIN messages m ON m.scope=o.scope AND m.id=o.message_id
                JOIN topics t ON t.scope=o.scope AND t.id=o.topic
                WHERE o.scope=? AND o.status IN ('waiting','deferred') AND t.status='active'
                AND o.expires>? AND m.seq<? ORDER BY o.created,m.seq LIMIT ?''',
                (key,now,trigger['seq'],self.max_pending)))
            offers = tuple(PendingOpportunity(row['message_id'],row['topic'],row['actor'],row['audience'],
                row['kind'],row['status'],row['created'],row['expires'],row['revision']) for row in rows)
        epoch = self._epochs.get(key,0)
        def current():
            if (self._closed or epoch != self._epochs.get(key,0) or self.clock() >= binding[4]
                    or self._bindings.get((key,mid)) is not binding or event.source is not binding[1]
                    or _route(event) != binding[2] or str(event.user_id) != binding[2][2]
                    or str(event.message_id) != binding[2][3] or event.text != binding[3]):
                return False
            with self._connection() as db:
                self._repair(db,key)
                self._message(db,key,mid)
                for offer in offers:
                    row = db.execute('SELECT o.status,o.expires,o.revision,t.status AS topic_status '
                        'FROM opportunities o JOIN topics t ON t.scope=o.scope AND t.id=o.topic '
                        'WHERE o.scope=? AND o.message_id=?',(key,offer.message_id)).fetchone()
                    if (row is None or row['status'] not in {'waiting','deferred'} or row['topic_status'] != 'active'
                            or row['expires'] <= self.clock() or row['revision'] != offer.revision):
                        return False
            return True
        return Reconsideration(event,event.source,offers,current)

    def revoke(self, scope, message_ids):
        key = self._scope(scope)
        ids = {_ident(mid) for mid in message_ids}
        if not ids:
            return frozenset()
        with self._connection() as db:
            rows = list(db.execute('SELECT id,parent FROM messages WHERE scope=?',(key,)))
        while True:
            derived = {row['id'] for row in rows if row['parent'] in ids}
            if derived <= ids:
                break
            ids.update(derived)
        with self._connection(self.control_path) as db:
            known = {row[0] for row in db.execute('SELECT message_id FROM revoked WHERE scope=?',(key,))}
            fresh = ids-known
            db.executemany('INSERT OR IGNORE INTO revoked VALUES(?,?,?)',
                           ((key,mid,self.clock()) for mid in fresh))
        for mid in ids:
            self._bindings.pop((key,mid),None)
        with self._connection() as db:
            self._repair(db,key)
        return frozenset(fresh)

    def disconnect(self, scope=None):
        if scope is None:
            keys = {key for key,_ in self._bindings} | set(self._epochs)
            self._bindings.clear()
        else:
            key = self._scope(scope)
            keys = {key}
            self._bindings = {ident: binding for ident,binding in self._bindings.items() if ident[0] != key}
        for key in keys:
            self._epochs[key] = self._epochs.get(key,0)+1

    def close(self):
        self.disconnect()
        self._closed = True
