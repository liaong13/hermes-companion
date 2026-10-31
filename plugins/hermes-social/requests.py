"""Scoped request metadata and source-bound reply plans, without message bodies.

Classification is a model hypothesis about authenticated observations. It never
creates an actor, restores an old ingress, or supplies channel send permission.
The caller records a reply only after its channel has verified actual delivery.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import threading
import uuid

from .context import ContextScope
from .dialogue import _ident, _route


STATES = frozenset({'pending', 'waiting', 'deferred', 'responded', 'closed', 'cancelled'})
TERMINAL = frozenset({'responded', 'closed', 'cancelled'})
ACTIONS = frozenset({'new', 'request', 'continue', 'supplement', 'adopt', 'correct',
                     'wait', 'defer', 'close', 'reopen', 'cancel', 'observe', 'none'})


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


@dataclass(frozen=True)
class ConditionSnapshot:
    source_id: str
    provider_id: str
    relation: str
    adopted_by: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()

    def row(self):
        return dict(source_id=self.source_id, provider_id=self.provider_id,
                    relation=self.relation, adopted_by=list(self.adopted_by),
                    supersedes=list(self.supersedes))


@dataclass(frozen=True)
class RequestSnapshot:
    request_id: str
    owner_id: str
    topic_anchor: str
    state: str
    revision: int
    message_ids: tuple[str, ...]
    conditions: tuple[ConditionSnapshot, ...]
    expires_at: float

    @property
    def version(self):
        return self.revision

    @property
    def condition_ids(self):
        return tuple(item.source_id for item in self.conditions)

    def row(self):
        return dict(request_id=self.request_id, owner_id=self.owner_id,
                    topic_anchor=self.topic_anchor, state=self.state, revision=self.revision,
                    message_ids=list(self.message_ids), conditions=[item.row() for item in self.conditions],
                    expires_at=self.expires_at)


@dataclass(eq=False)
class ReplyPlan:
    request_ids: tuple[str, ...]
    message_ids: frozenset[str]
    trigger: object
    source: object
    expires_at: float
    mode: str
    _ledger: object = field(repr=False)
    _key: str = field(repr=False)
    _scope: object = field(repr=False)
    _route: tuple = field(repr=False)
    _text: str = field(repr=False)
    _epoch: int = field(repr=False)
    _revisions: dict = field(repr=False)
    _valid: object = field(default=None, repr=False)
    _settled: dict = field(default_factory=dict, repr=False)
    _outbound: set = field(default_factory=set, repr=False)

    def current(self):
        return self._ledger.plan_current(self)


class RequestLedger:
    """A bounded, exact-scope ledger; content rollback cannot undo revocation.

    Conditions retain the real supplying actor. ``adopt`` records the request
    owner's explicit reuse; a contributor never becomes that request's owner.
    ``record_reply`` is an integration callback for a verified channel ACK,
    not a model-facing operation. Plans and live event bindings are RAM only.
    """

    def __init__(self, path, *, ttl=86400, max_requests=32, max_messages=512,
                 fresh_ttl=300, clock=time.time, trusted_ingress=None):
        if (any(type(value) is not int for value in (ttl, max_requests, max_messages, fresh_ttl))
                or not 60 <= ttl <= 604800 or not 1 <= max_requests <= 128
                or not max_requests+1 <= max_messages <= 4096 or not 5 <= fresh_ttl <= 600
                or trusted_ingress is not None and not callable(trusted_ingress)):
            raise ValueError('invalid request ledger bounds')
        self.path = Path(path).absolute()
        self.control_path = self.path.with_name(self.path.name+'.control.sqlite3')
        self.ttl, self.max_requests, self.max_messages = ttl, max_requests, max_messages
        self.fresh_ttl, self.clock, self.trusted_ingress = fresh_ttl, clock, trusted_ingress
        self._bindings, self._plans, self._epochs = {}, {}, {}
        self._lock = threading.RLock()
        self._closed = False
        if self.path.parent.resolve() != self.path.parent:
            raise ValueError('request directory must not be a symlink')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stat = self.path.parent.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o022:
            raise ValueError('request directory must be owner controlled')
        for filename in (self.path, self.control_path):
            if filename.is_symlink():
                raise ValueError('request file must not be a symlink')
            if filename.exists():
                stat = filename.stat()
                if not filename.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                    raise ValueError('request storage permissions invalid')
            else:
                descriptor = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
        with self._connection(self.control_path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS revoked(scope TEXT NOT NULL,id TEXT NOT NULL,
                    created REAL NOT NULL,PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS outcomes(scope TEXT NOT NULL,request TEXT NOT NULL,
                    state TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(scope,request));
            ''')
        with self._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,id TEXT NOT NULL,actor TEXT NOT NULL,role TEXT NOT NULL,
                    observed REAL NOT NULL,UNIQUE(scope,id));
                CREATE TABLE IF NOT EXISTS requests(scope TEXT NOT NULL,id TEXT NOT NULL,
                    owner TEXT NOT NULL,topic TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
                    updated REAL NOT NULL,expires REAL NOT NULL,PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS members(scope TEXT NOT NULL,request TEXT NOT NULL,
                    message TEXT NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(scope,request,message));
                CREATE TABLE IF NOT EXISTS conditions(scope TEXT NOT NULL,request TEXT NOT NULL,
                    source TEXT NOT NULL,relation TEXT NOT NULL,supersedes TEXT NOT NULL,
                    PRIMARY KEY(scope,request,source));
                CREATE TABLE IF NOT EXISTS adoptions(scope TEXT NOT NULL,request TEXT NOT NULL,
                    source TEXT NOT NULL,actor TEXT NOT NULL,message TEXT NOT NULL,
                    PRIMARY KEY(scope,request,source,actor));
                CREATE TABLE IF NOT EXISTS dependencies(scope TEXT NOT NULL,child TEXT NOT NULL,
                    parent TEXT NOT NULL,PRIMARY KEY(scope,child,parent));
                CREATE TABLE IF NOT EXISTS classifications(scope TEXT NOT NULL,request TEXT NOT NULL,
                    source TEXT NOT NULL,fingerprint TEXT NOT NULL,PRIMARY KEY(scope,request,source));
                CREATE TABLE IF NOT EXISTS acknowledgements(scope TEXT NOT NULL,id TEXT NOT NULL,
                    anchor TEXT NOT NULL,PRIMARY KEY(scope,id));
                CREATE INDEX IF NOT EXISTS request_members ON members(scope,request);
            ''')

    @contextmanager
    def _connection(self, path=None):
        if self._closed:
            raise ValueError('request ledger closed')
        db = sqlite3.connect(path or self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _scope(scope):
        if (not isinstance(scope, ContextScope)
                or scope.chat_id.startswith('group:') and scope.subject_id):
            raise ValueError('invalid request scope')
        return hashlib.sha256(json.dumps((scope.profile_home, scope.platform,
                                         scope.chat_id, scope.subject_id)).encode()).hexdigest()

    def _revoked(self, key):
        with self._connection(self.control_path) as db:
            return {row[0] for row in db.execute('SELECT id FROM revoked WHERE scope=?', (key,))}

    def _control_state(self, key, request_id, state, revision):
        with self._connection(self.control_path) as db:
            db.execute('INSERT INTO outcomes VALUES(?,?,?,?) ON CONFLICT(scope,request) '
                       'DO UPDATE SET state=excluded.state,revision=excluded.revision '
                       'WHERE excluded.revision>=outcomes.revision',
                       (key, request_id, state, revision))

    def _repair(self, db, key):
        revoked = self._revoked(key)
        with self._connection(self.control_path) as control:
            prior_outcomes = {row['request']: row for row in
                              control.execute('SELECT * FROM outcomes WHERE scope=?', (key,))}
        edges = list(db.execute('SELECT child,parent FROM dependencies WHERE scope=?', (key,)))
        while True:
            derived = {row['child'] for row in edges if row['parent'] in revoked}
            if derived <= revoked:
                break
            revoked.update(derived)
        if revoked:
            with self._connection(self.control_path) as control:
                control.executemany('INSERT OR IGNORE INTO revoked VALUES(?,?,?)',
                                    ((key, mid, self.clock()) for mid in revoked))
            affected = {row[0] for row in db.execute('SELECT request FROM members WHERE scope=?', (key,))
                        if row[0] in revoked}
            for mid in revoked:
                affected.update(row[0] for row in db.execute(
                    'SELECT request FROM members WHERE scope=? AND message=?', (key, mid)))
                affected.update(row[0] for row in db.execute(
                    'SELECT request FROM conditions WHERE scope=? AND source=?', (key, mid)))
                db.execute('DELETE FROM messages WHERE scope=? AND id=?', (key, mid))
                db.execute('DELETE FROM acknowledgements WHERE scope=? AND id=?', (key, mid))
                db.execute('DELETE FROM requests WHERE scope=? AND id=?', (key, mid))
                db.execute('DELETE FROM members WHERE scope=? AND message=?', (key, mid))
                db.execute('DELETE FROM conditions WHERE scope=? AND source=?', (key, mid))
                db.execute('DELETE FROM classifications WHERE scope=? AND (request=? OR source=?)', (key, mid, mid))
                db.execute('DELETE FROM adoptions WHERE scope=? AND (source=? OR message=?)', (key, mid, mid))
                db.execute('DELETE FROM dependencies WHERE scope=? AND (child=? OR parent=?)', (key, mid, mid))
                db.execute('UPDATE requests SET topic=id WHERE scope=? AND topic=?', (key, mid))
            for request_id in affected-revoked:
                row = db.execute('SELECT state,revision FROM requests WHERE scope=? AND id=?',
                                 (key, request_id)).fetchone()
                if row and row['state'] not in {'closed', 'cancelled'}:
                    prior = prior_outcomes.get(request_id)
                    revision = max(row['revision'], prior['revision'] if prior else 0)+1
                    db.execute("UPDATE requests SET state='pending',revision=? WHERE scope=? AND id=?",
                               (revision, key, request_id))
                    self._control_state(key, request_id, 'pending', revision)
        with self._connection(self.control_path) as control:
            outcomes = list(control.execute('SELECT * FROM outcomes WHERE scope=?', (key,)))
        for row in outcomes:
            db.execute('UPDATE requests SET state=?,revision=? WHERE scope=? AND id=? AND revision<=?',
                       (row['state'], row['revision'], key, row['request'], row['revision']))
        self._prune(db, key)
        return frozenset(revoked)

    def _prune(self, db, key, *, protect_id=None):
        now = self.clock()
        db.execute('DELETE FROM requests WHERE scope=? AND expires<=?', (key, now))
        rows = list(db.execute('SELECT id,state FROM requests WHERE scope=? '
                               'ORDER BY updated DESC,id', (key,)))
        rows.sort(key=lambda row: (row['id'] == protect_id, row['state'] not in TERMINAL), reverse=True)
        evicted = {row['id'] for row in rows[self.max_requests:]}
        db.executemany('DELETE FROM requests WHERE scope=? AND id=?',
                       ((key, row['id']) for row in rows[self.max_requests:]))
        db.execute('DELETE FROM messages WHERE scope=? AND observed<=?', (key, now-self.ttl))
        for table in ('members', 'conditions', 'adoptions', 'classifications'):
            db.execute(f'DELETE FROM {table} WHERE scope=? AND request NOT IN '
                       '(SELECT id FROM requests WHERE scope=?)', (key, key))
        roots = {row[0] for row in db.execute('SELECT id FROM requests WHERE scope=?', (key,))}
        messages = list(db.execute('SELECT id FROM messages WHERE scope=? ORDER BY observed DESC,seq DESC', (key,)))
        keep = set(roots) | ({protect_id} if protect_id else set())
        for row in messages:
            if len(keep) >= self.max_messages:
                break
            keep.add(row[0])
        db.executemany('DELETE FROM messages WHERE scope=? AND id=?',
                       ((key, row[0]) for row in messages if row[0] not in keep))
        removed = {row[0] for row in messages if row[0] not in keep}
        for token, plan in list(self._plans.items()):
            if self._scope(plan._scope) == key and (set(plan.request_ids) & evicted or plan.message_ids & removed):
                self._plans.pop(token, None)
        db.execute('DELETE FROM members WHERE scope=? AND message NOT IN '
                   '(SELECT id FROM messages WHERE scope=?)', (key, key))
        db.execute('DELETE FROM conditions WHERE scope=? AND source NOT IN '
                   '(SELECT id FROM messages WHERE scope=?)', (key, key))
        db.execute('DELETE FROM classifications WHERE scope=? AND source NOT IN '
                   '(SELECT id FROM messages WHERE scope=?)', (key, key))
        db.execute('DELETE FROM adoptions WHERE scope=? AND (source NOT IN '
                   '(SELECT id FROM messages WHERE scope=?) OR message NOT IN '
                   '(SELECT id FROM messages WHERE scope=?))', (key, key, key))
        db.execute('DELETE FROM dependencies WHERE scope=? AND (child NOT IN '
                   '(SELECT id FROM messages WHERE scope=?) OR parent NOT IN '
                   '(SELECT id FROM messages WHERE scope=?))', (key, key, key))
        db.execute('DELETE FROM acknowledgements WHERE scope=? AND id NOT IN '
                   '(SELECT id FROM messages WHERE scope=?)', (key, key))

    def _message(self, db, key, mid):
        row = db.execute('SELECT * FROM messages WHERE scope=? AND id=?', (key, _ident(mid))).fetchone()
        if row is None or mid in self._revoked(key):
            raise ValueError('request source unavailable')
        return row

    def _request(self, db, key, request_id):
        row = db.execute('SELECT * FROM requests WHERE scope=? AND id=?',
                         (key, _ident(request_id))).fetchone()
        if row is None:
            raise ValueError('request unavailable')
        return row

    @staticmethod
    def _later(source, anchor):
        # Exact readback may import an older reference after a fresh message.
        # Preserve original chronology; equal timestamps retain receive order.
        return (source['observed'], source['seq']) > (anchor['observed'], anchor['seq'])

    def _binding(self, scope, event_or_id):
        key = self._scope(scope)
        mid = str(event_or_id.message_id) if not isinstance(event_or_id, str) else event_or_id
        binding = self._bindings.get((key, _ident(mid)))
        if binding is None or self.clock() >= binding[4]:
            raise ValueError('request requires a fresh authenticated observation')
        event = binding[0] if isinstance(event_or_id, str) else event_or_id
        exact = event is binding[0] and event.source is binding[1] and event.text == binding[3]
        trusted = (not exact and self.trusted_ingress is not None
                   and self.trusted_ingress(scope, event) is True)
        if (not (exact or trusted) or _route(event) != binding[2]
                or str(event.user_id) != binding[2][2] or str(event.message_id) != binding[2][3]
                or not isinstance(event.text, str)):
            raise ValueError('request event source changed')
        return key, mid, event, binding

    @_serialized
    def observe_event(self, scope, event):
        key = self._scope(scope)
        expected = (scope.chat_id, scope.chat_id.partition(':')[0], str(event.user_id), str(event.message_id))
        if (_route(event) != expected or not isinstance(event.text, str)
                or scope.chat_id.startswith('dm:') and str(event.user_id) != scope.subject_id):
            raise ValueError('request event scope mismatch')
        mid, actor = _ident(str(event.message_id)), _ident(str(event.user_id))
        now = self.clock()
        with self._connection() as db:
            self._repair(db, key)
            if mid in self._revoked(key):
                raise ValueError('request source revoked')
            row = db.execute('SELECT * FROM messages WHERE scope=? AND id=?', (key, mid)).fetchone()
            if row is not None:
                old = self._bindings.get((key, mid))
                if (old is None or old[0] is not event or old[1] is not event.source
                        or old[2] != expected or old[3] != event.text or row['actor'] != actor
                        or row['role'] != 'member'):
                    raise ValueError('historical request source cannot become fresh ingress')
                return
            if (key, mid) in self._bindings:
                raise ValueError('pruned request observation cannot renew its ingress')
            db.execute('INSERT INTO messages(scope,id,actor,role,observed) VALUES(?,?,?,?,?)',
                       (key, mid, actor, 'member', now))
            self._prune(db, key, protect_id=mid)
        self._bindings = {ident: value for ident, value in self._bindings.items() if value[4] > now}
        self._bindings[(key, mid)] = (event, event.source, expected, event.text,
                                     now+min(self.fresh_ttl,self.ttl))
        while len(self._bindings) > 512:
            self._bindings.pop(next(iter(self._bindings)))

    @_serialized
    def observe_reference(self, scope, message_id, *, actor_id, role, observed_at):
        """Import channel-verified original metadata, never a fresh ingress.

        The trusted caller has checked exact readback against known original
        scope/actor/role/time. No body, event binding, request or TTL is created.
        Existing observations retain their original timestamp and live deadline.
        """
        key, mid = self._scope(scope), _ident(message_id)
        if not isinstance(role, str) or role not in {'member', 'self'}:
            raise ValueError('invalid reference role')
        actor = _ident(actor_id, empty=role == 'self')
        now = self.clock()
        if (type(observed_at) not in (int, float) or not math.isfinite(observed_at)
                or not 0 < observed_at <= now or observed_at <= now-self.ttl
                or scope.chat_id.startswith('dm:') and role == 'member' and actor != scope.subject_id):
            raise ValueError('reference original scope or timestamp unavailable')
        with self._connection() as db:
            self._repair(db, key)
            if mid in self._revoked(key):
                raise ValueError('reference source revoked')
            previous = db.execute('SELECT * FROM messages WHERE scope=? AND id=?', (key, mid)).fetchone()
            if previous is not None:
                if previous['actor'] != actor or previous['role'] != role:
                    raise ValueError('reference source identity conflict')
                return
            if (key, mid) in self._bindings:
                raise ValueError('pruned request observation cannot be restored as ingress')
            db.execute('INSERT INTO messages(scope,id,actor,role,observed) VALUES(?,?,?,?,?)',
                       (key, mid, actor, role, observed_at))
            self._prune(db, key)
            if db.execute('SELECT 1 FROM messages WHERE scope=? AND id=?', (key, mid)).fetchone() is None:
                raise ValueError('reference exceeds request metadata budget')

    @staticmethod
    def _refs(values, *, maximum=16):
        if (not isinstance(values, (tuple, list, set, frozenset)) or len(values) > maximum
                or any(not isinstance(value, str) for value in values)):
            raise ValueError('request references exceed budget')
        return tuple(dict.fromkeys(_ident(value) for value in values))

    @_serialized
    def assess(self, scope, event_or_id, *, kind='new', request_id=None, topic_anchor=None,
               condition_ids=(), supersedes=()):
        if kind not in ACTIONS:
            raise ValueError('unknown request relation')
        key, mid, event, binding = self._binding(scope, event_or_id)
        conditions, replaced = self._refs(condition_ids), self._refs(supersedes)
        if kind in {'new', 'request'}:
            conditions = tuple(ref for ref in conditions if ref != mid)
        with self._connection() as db:
            self._repair(db, key)
            source = self._message(db, key, mid)
            if source['role'] != 'member':
                raise ValueError('request must originate from a member')
            for ref in (*conditions, *replaced, *((topic_anchor,) if topic_anchor else ())):
                if self._later(self._message(db, key, ref), source):
                    raise ValueError('request points to a future observation')
            if kind in {'observe', 'none'}:
                if conditions or replaced:
                    raise ValueError('observation cannot attach conditions')
                return None
            if kind in {'new', 'request'}:
                if request_id not in (None, mid) or replaced:
                    raise ValueError('new request owner cannot be chosen')
                request_id = mid
                existing = db.execute('SELECT * FROM requests WHERE scope=? AND id=?', (key, mid)).fetchone()
                if existing:
                    if existing['owner'] != source['actor'] or existing['topic'] != (topic_anchor or mid):
                        raise ValueError('request identity cannot be reassigned')
                    fingerprint = json.dumps(('new', sorted(conditions), sorted(replaced), topic_anchor or mid))
                    classified = db.execute('SELECT fingerprint FROM classifications WHERE '
                        'scope=? AND request=? AND source=?', (key, mid, mid)).fetchone()
                    if classified is None or classified[0] != fingerprint:
                        raise ValueError('source conditions cannot be changed in place')
                    return self._snapshot(db, key, mid)
                with self._connection(self.control_path) as control:
                    if control.execute('SELECT 1 FROM outcomes WHERE scope=? AND request=?', (key, mid)).fetchone():
                        raise ValueError('pruned request identity cannot be recreated')
                db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)',
                           (key, mid, source['actor'], topic_anchor or mid, 'pending', 1,
                            self.clock(), source['observed']+self.ttl))
                self._control_state(key, mid, 'pending', 1)
                row = self._request(db, key, mid)
            else:
                if request_id is None:
                    raise ValueError('request relation needs a real request root')
                row = self._request(db, key, request_id)
                root = self._message(db, key, request_id)
                if self._later(root, source):
                    raise ValueError('request root is in the future')
                if topic_anchor is not None and topic_anchor != row['topic']:
                    raise ValueError('continuation cannot change the topic anchor')
                if kind in {'adopt', 'close', 'reopen', 'cancel'} and row['owner'] != source['actor']:
                    raise ValueError('another member cannot adopt or control this request')
                if row['state'] in {'closed', 'cancelled'} and kind != 'reopen':
                    raise ValueError('closed request needs explicit owner reopening')
                if kind == 'adopt' and not conditions:
                    raise ValueError('adoption requires exact condition sources')
                if kind not in {'adopt', 'correct'} and conditions:
                    raise ValueError('conditions require explicit adoption or correction')
                if kind != 'correct' and replaced:
                    raise ValueError('only corrections can supersede conditions')
                if any(not db.execute('SELECT 1 FROM conditions WHERE scope=? AND request=? AND source=?',
                                      (key, request_id, ref)).fetchone() for ref in replaced):
                    raise ValueError('correction cannot supersede another request condition')
                # Repeating the exact relation is idempotent. Different later
                # information belongs to another authenticated message.
                previous = db.execute('SELECT kind FROM members WHERE scope=? AND request=? AND message=?',
                                      (key, request_id, mid)).fetchone()
                if previous:
                    if previous['kind'] != kind:
                        raise ValueError('source classification cannot be changed in place')
                    fingerprint = json.dumps((kind, sorted(conditions), sorted(replaced), row['topic']))
                    classified = db.execute('SELECT fingerprint FROM classifications WHERE '
                        'scope=? AND request=? AND source=?', (key, request_id, mid)).fetchone()
                    if classified is None or classified[0] != fingerprint:
                        raise ValueError('source conditions cannot be changed in place')
                    return self._snapshot(db, key, request_id)
                state = {'wait': 'waiting', 'defer': 'deferred', 'close': 'closed',
                         'cancel': 'cancelled'}.get(kind, 'pending')
                revision = row['revision']+1
                db.execute('UPDATE requests SET state=?,revision=?,updated=? WHERE scope=? AND id=?',
                           (state, revision, self.clock(), key, request_id))
                self._control_state(key, request_id, state, revision)
            db.execute('INSERT INTO members VALUES(?,?,?,?)', (key, request_id, mid, kind))
            fingerprint = json.dumps(('new' if kind in {'new','request'} else kind,
                                       sorted(conditions), sorted(replaced), row['topic']))
            db.execute('INSERT INTO classifications VALUES(?,?,?,?)', (key, request_id, mid, fingerprint))
            dependencies = {request_id, *conditions, *replaced}-{mid}
            db.executemany('INSERT OR IGNORE INTO dependencies VALUES(?,?,?)',
                           ((key, mid, ref) for ref in dependencies))
            if kind in {'new', 'request', 'supplement', 'correct'}:
                relation = {'new': 'provided', 'request': 'provided',
                            'supplement': 'supplement', 'correct': 'correction'}[kind]
                db.execute('INSERT OR REPLACE INTO conditions VALUES(?,?,?,?,?)',
                           (key, request_id, mid, relation, json.dumps(replaced)))
                if source['actor'] == row['owner']:
                    db.execute('INSERT OR REPLACE INTO adoptions VALUES(?,?,?,?,?)',
                               (key, request_id, mid, source['actor'], mid))
            for ref in conditions:
                supplied = self._message(db, key, ref)
                relation = ('adopted' if kind == 'adopt' or supplied['actor'] != source['actor']
                            else 'provided')
                db.execute('INSERT OR IGNORE INTO conditions VALUES(?,?,?,?,?)',
                           (key, request_id, ref, relation, '[]'))
                db.execute('INSERT OR REPLACE INTO adoptions VALUES(?,?,?,?,?)',
                           (key, request_id, ref, source['actor'], mid))
            self._prune(db, key, protect_id=request_id)
            return self._snapshot(db, key, request_id)

    def _snapshot(self, db, key, request_id):
        row = self._request(db, key, request_id)
        members = list(db.execute('SELECT m.id FROM messages m JOIN members r ON '
                                 'r.scope=m.scope AND r.message=m.id WHERE r.scope=? AND r.request=? '
                                 'ORDER BY m.seq', (key, request_id)))
        conditions = []
        for item in db.execute('SELECT c.*,m.actor FROM conditions c JOIN messages m ON '
                               'c.scope=m.scope AND c.source=m.id WHERE c.scope=? AND c.request=? '
                               'ORDER BY m.seq', (key, request_id)):
            adopters = tuple(ref[0] for ref in db.execute('SELECT actor FROM adoptions WHERE '
                'scope=? AND request=? AND source=? ORDER BY actor', (key, request_id, item['source'])))
            conditions.append(ConditionSnapshot(item['source'], item['actor'], item['relation'],
                                                 adopters, tuple(json.loads(item['supersedes']))))
        ids = tuple(dict.fromkeys((request_id, *(mid[0] for mid in members),
                                   *(item.source_id for item in conditions))))
        return RequestSnapshot(row['id'], row['owner'], row['topic'], row['state'],
                               row['revision'], ids, tuple(conditions), row['expires'])

    @_serialized
    def context(self, scope, *, limit=8):
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError('request context exceeds budget')
        key = self._scope(scope)
        with self._connection() as db:
            self._repair(db, key)
            ids = [row[0] for row in db.execute('SELECT id FROM requests WHERE scope=? '
                "ORDER BY CASE WHEN state IN ('pending','waiting','deferred') THEN 0 ELSE 1 END,updated DESC,id LIMIT ?",
                (key, limit))]
            return tuple(self._snapshot(db, key, mid) for mid in ids)

    @_serialized
    def reference_metadata(self, scope, message_ids):
        ids, key = self._refs(message_ids, maximum=32), self._scope(scope)
        with self._connection() as db:
            self._repair(db, key)
            rows = [db.execute('SELECT id,actor,role,observed FROM messages WHERE scope=? AND id=?',
                               (key, mid)).fetchone() for mid in ids]
            return tuple(dict(message_id=row['id'], actor_id=row['actor'], role=row['role'],
                              observed_at=row['observed']) for row in rows if row is not None)

    @_serialized
    def bind_reply(self, scope, event, request_ids, *, dependencies=(), mode='single', valid=None):
        ids = self._refs(request_ids, maximum=8)
        extra = self._refs(dependencies, maximum=64)
        if mode not in {'single', 'combined', 'clarify'} or not ids or valid is not None and not callable(valid):
            raise ValueError('invalid reply plan')
        key, mid, event, binding = self._binding(scope, event)
        with self._connection() as db:
            self._repair(db, key)
            trigger = self._message(db, key, mid)
            revisions, refs = {}, {mid, *extra}
            for request_id in ids:
                request = self._request(db, key, request_id)
                if request['state'] in TERMINAL or self._later(self._message(db, key, request_id), trigger):
                    raise ValueError('reply plan covers an unavailable request')
                snapshot = self._snapshot(db, key, request_id)
                refs.update(snapshot.message_ids)
                revisions[request_id] = request['revision']
            for ref in refs:
                if self._later(self._message(db, key, ref), trigger):
                    raise ValueError('reply depends on a future message')
            plan = ReplyPlan(ids, frozenset(refs), event, event.source, binding[4], mode,
                             self, uuid.uuid4().hex, scope, _route(event), event.text,
                             self._epochs.get(key, 0), revisions, valid)
            self._plans[plan._key] = plan
            while len(self._plans) > 128:
                self._plans.pop(next(iter(self._plans)))
            return plan

    @_serialized
    def plan_current(self, plan):
        try:
            if (not isinstance(plan, ReplyPlan) or self._closed or plan._ledger is not self
                    or self._plans.get(plan._key) is not plan or self.clock() >= plan.expires_at
                    or plan.trigger.source is not plan.source or _route(plan.trigger) != plan._route
                    or plan.trigger.text != plan._text or plan._valid is not None and plan._valid() is not True):
                return False
            key, _, _, _ = self._binding(plan._scope, plan.trigger)
            if self._epochs.get(key, 0) != plan._epoch:
                return False
            with self._connection() as db:
                self._repair(db, key)
                if any(ref in self._revoked(key) for ref in plan.message_ids):
                    return False
                for ref in plan.message_ids:
                    self._message(db, key, ref)
                return all(self._request(db, key, request_id)['revision'] == revision
                           for request_id, revision in plan._revisions.items())
        except Exception:
            return False

    @_serialized
    def record_reply(self, plan, outbound_id, *, completed_ids=None, waiting_ids=None):
        """Record a channel-verified ACK; unknown or unsent output must not call it."""
        mid = _ident(outbound_id)
        completed = (plan.request_ids if plan.mode != 'clarify' else ()) if completed_ids is None else self._refs(completed_ids, maximum=8)
        waiting = ((plan.request_ids if plan.mode == 'clarify' else ()) if waiting_ids is None
                   else self._refs(waiting_ids, maximum=8))
        if (set(completed) & set(waiting) or not (set(completed) | set(waiting)) <= set(plan.request_ids)):
            raise ValueError('reply outcome exceeds its captured requests')
        if not self.plan_current(plan):
            raise ValueError('reply plan is no longer current')
        key = self._scope(plan._scope)
        changed = {}
        with self._connection() as db:
            self._repair(db, key)
            # Recheck under the write transaction: recall or another delivery
            # can win after the first optimistic plan_current() check.
            if self.clock() >= plan.expires_at:
                raise ValueError('reply plan expired before delivery recording')
            for ref in plan.message_ids:
                self._message(db, key, ref)
            for request_id, revision in plan._revisions.items():
                if self._request(db, key, request_id)['revision'] != revision:
                    raise ValueError('request changed before delivery recording')
            existing = db.execute('SELECT role FROM messages WHERE scope=? AND id=?', (key, mid)).fetchone()
            if existing:
                if mid not in plan._outbound or existing['role'] != 'self':
                    raise ValueError('reply ACK conflicts with an existing source')
                return tuple(self._snapshot(db, key, request_id) for request_id in plan.request_ids)
            for request_id, state in [*((ref, 'responded') for ref in completed),
                                      *((ref, 'waiting') for ref in waiting)]:
                previous = plan._settled.get(request_id)
                if previous is not None and previous != state and not (previous == 'waiting' and state == 'responded'):
                    raise ValueError('later fragments cannot change a settled outcome')
                if self._request(db, key, request_id)['revision'] != plan._revisions[request_id]:
                    raise ValueError('request changed before delivery')
            db.execute('INSERT INTO messages(scope,id,actor,role,observed) VALUES(?,?,?,?,?)',
                       (key, mid, '', 'self', self.clock()))
            db.execute('INSERT INTO acknowledgements VALUES(?,?,?)',
                       (key, mid, str(plan.trigger.message_id)))
            db.executemany('INSERT INTO dependencies VALUES(?,?,?)',
                           ((key, mid, ref) for ref in plan.message_ids))
            for request_id, state in [*((ref, 'responded') for ref in completed),
                                      *((ref, 'waiting') for ref in waiting)]:
                if plan._settled.get(request_id) == state:
                    continue
                request = self._request(db, key, request_id)
                if request['revision'] != plan._revisions[request_id]:
                    raise ValueError('request changed before delivery')
                revision = request['revision']+1
                db.execute('UPDATE requests SET state=?,revision=?,updated=? WHERE scope=? AND id=?',
                           (state, revision, self.clock(), key, request_id))
                self._control_state(key, request_id, state, revision)
                changed[request_id] = (revision, state)
                db.execute('INSERT OR IGNORE INTO members VALUES(?,?,?,?)', (key, request_id, mid, 'reply'))
            result = tuple(self._snapshot(db, key, request_id) for request_id in plan.request_ids)
        for request_id, (revision, state) in changed.items():
            plan._revisions[request_id] = revision
            plan._settled[request_id] = state
        plan._outbound.add(mid)
        return result

    @_serialized
    def observe_reply(self, scope, outbound_id, *, reply_to='', dependencies=()):
        """Register other channel-verified output without completing any request.

        ``reply_to`` is a source dependency, not a declaration that the channel
        emitted a native quote. It must be an observed exact-scope reference.
        """
        key, mid = self._scope(scope), _ident(outbound_id)
        anchor = _ident(reply_to, empty=True)
        refs = set(self._refs(dependencies, maximum=64)) | ({anchor} if anchor else set())
        if mid in refs:
            raise ValueError('reply cannot depend on itself')
        with self._connection() as db:
            self._repair(db, key)
            if mid in self._revoked(key):
                raise ValueError('reply source revoked')
            original = db.execute('SELECT * FROM messages WHERE scope=? AND id=?', (key, mid)).fetchone()
            if original is not None:
                if original['role'] != 'self':
                    raise ValueError('reply ACK conflicts with a member source')
                previous = db.execute('SELECT anchor FROM acknowledgements WHERE scope=? AND id=?',
                                      (key, mid)).fetchone()
                if anchor and previous is not None and previous['anchor'] and previous['anchor'] != anchor:
                    raise ValueError('reply ACK source cannot be changed')
            for ref in refs:
                source = self._message(db, key, ref)
                if original is not None and self._later(source, original):
                    raise ValueError('reply dependency points into the future')
            if original is None:
                db.execute('INSERT INTO messages(scope,id,actor,role,observed) VALUES(?,?,?,?,?)',
                           (key, mid, '', 'self', self.clock()))
                db.execute('INSERT INTO acknowledgements VALUES(?,?,?)', (key, mid, anchor))
            db.executemany('INSERT OR IGNORE INTO dependencies VALUES(?,?,?)',
                           ((key, mid, ref) for ref in refs))
            self._prune(db, key, protect_id=mid)

    @_serialized
    def release(self, plan):
        if isinstance(plan, ReplyPlan) and self._plans.get(plan._key) is plan:
            self._plans.pop(plan._key)

    @_serialized
    def revoke(self, scope, message_ids):
        ids, key = self._refs(message_ids, maximum=64), self._scope(scope)
        with self._connection(self.control_path) as db:
            db.executemany('INSERT OR IGNORE INTO revoked VALUES(?,?,?)',
                           ((key, mid, self.clock()) for mid in ids))
        with self._connection() as db:
            revoked = self._repair(db, key)
        self._epochs[key] = self._epochs.get(key, 0)+1
        self._bindings = {ident: binding for ident, binding in self._bindings.items()
                          if ident[0] != key or ident[1] not in revoked}
        return revoked

    @_serialized
    def reset_connection(self):
        self._plans.clear()
        self._bindings.clear()

    @_serialized
    def close(self):
        self.reset_connection()
        self._closed = True
