"""Local conversation episodes and dependency revocation, without inference APIs.

Events retain transport metadata only. Plaintext lives in authoritative sources;
deleted source pointers become tombstones, including for already-sent bot IDs.
"""
from __future__ import annotations

from dataclasses import replace
import re
import time
import uuid

from .core import memory_api

_CLOSE = re.compile(r'^(?:先这样|下次再聊|不聊了|这个话题到此为止)[。！!~～]?$')
_FOLLOW = re.compile(r'^(?:继续上次|接着上次|刚才|那|那么|所以|然后)')
_STOP = {'我们', '你们', '这个', '那个', '什么', '怎么', '喜欢', '兴趣', '请问', '觉得', '继续', '上次', '话题'}


def keywords(text):
    return set(memory_api().terms(text)) - _STOP


class ConversationMemory:
    def __init__(self, store, instance_id, *, retention_days=7, max_events=200):
        self.store, self.instance_id = store, instance_id
        self.retention_days, self.max_events = retention_days, max_events
        with store.connection(True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS life_events (
                id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, domain TEXT NOT NULL,
                owner TEXT NOT NULL, origin TEXT NOT NULL,
                source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
                message_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('member','self')),
                reply_to TEXT NOT NULL, topic_id TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL,
                UNIQUE(instance_id,domain,owner,origin,message_id,role))''')
            db.execute('''CREATE INDEX IF NOT EXISTS life_event_scope
                ON life_events(instance_id,domain,owner,origin,created)''')
            db.execute('''CREATE TABLE IF NOT EXISTS life_dependencies (
                event_id TEXT NOT NULL REFERENCES life_events(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('memory','source','quote')), ref_id TEXT NOT NULL,
                memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
                source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
                revision INTEGER NOT NULL, PRIMARY KEY(event_id,kind,ref_id))''')
            if 'archival' not in {r['name'] for r in db.execute('PRAGMA table_info(life_dependencies)')}:
                db.execute('ALTER TABLE life_dependencies ADD COLUMN archival INTEGER NOT NULL DEFAULT 0')

    def _params(self, scope):
        return self.instance_id, self.store.domain_id, *self.store.scope_pair(scope)

    def _reference(self, db, scope, kind, ident, revision=None, *, ancestry=True, archival=False):
        if kind not in {'source','memory','quote'}:
            raise PermissionError('conversation dependency kind denied')
        self.store.validate_scope(db, scope)
        if kind == 'quote':
            # Only metadata of an acknowledged bot message in this very group
            # is visible across members. No foreign source plaintext is read.
            row = db.execute('''SELECT s.id,s.owner,s.revision,s.purged FROM sources s JOIN life_events e ON e.source_id=s.id
                WHERE e.instance_id=? AND s.domain=? AND s.origin=? AND e.role='self' AND s.id=?''',
                (self.instance_id, self.store.domain_id, scope.origin, ident)).fetchone()
        elif kind == 'source':
            row = db.execute('SELECT id,owner,revision,purged FROM sources WHERE domain=? AND owner=? AND origin=? AND id=?',
                             (self.store.domain_id, *scope.pair, ident)).fetchone()
        else:
            row = db.execute('SELECT id,owner,revision,state,expires FROM memories WHERE domain=? AND owner=? AND origin=? AND id=?',
                             (self.store.domain_id, *scope.pair, ident)).fetchone()
        if row is None:
            raise PermissionError('conversation dependency not in allowed scope')
        if (kind in {'source','quote'} and row['purged'] is not None
                or kind == 'memory' and (row['state'] != 'active'
                    or row['expires'] is not None and row['expires'] <= time.time())
                or revision is not None and row['revision'] != revision):
            raise PermissionError('conversation dependency expired')
        if kind == 'memory':
            evidence = db.execute('''SELECT COUNT(*) AS total, SUM(CASE WHEN s.purged IS NULL
                AND s.domain=? AND s.owner=? AND s.origin=? AND s.revision=e.source_revision
                AND s.sha256=e.sha256 THEN 1 ELSE 0 END) AS valid
                FROM evidence e JOIN sources s ON s.id=e.source_id WHERE e.memory_id=?''',
                (self.store.domain_id, *scope.pair, ident)).fetchone()
            if not evidence['total'] or evidence['valid'] != evidence['total']:
                raise PermissionError('person dependency evidence expired')
        if kind in {'source','quote'} and ancestry:
            self._check_ancestry(db, self._owner_scope(db, scope, row['owner']), ident, archival=archival)
        return {'kind': kind, 'id': ident, 'revision': row['revision'], **({'archival':True} if archival else {})}

    def _owner_scope(self, db, scope, owner):
        if owner == scope.principal_id:
            return scope
        version = db.execute('SELECT deletion_epoch FROM scope_versions WHERE domain=? AND owner=? AND origin=?',
                             (self.store.domain_id, owner, scope.origin)).fetchone()
        if version is None:
            raise PermissionError('quote origin policy missing')
        return replace(scope, principal_id=owner, deletion_epoch=version['deletion_epoch'])

    def _check_ancestry(self, db, scope, source_id, *, archival=False):
        # Iterative traversal bounds work without recursive fan-out in long
        # conversations. Only metadata is read, including public quote edges.
        pending, visited = [(scope, source_id, archival)], set()
        while pending:
            owner_scope, ident, archive = pending.pop()
            self.store.validate_scope(db, owner_scope)
            if (ident,archive) in visited:
                continue
            visited.add((ident,archive))
            if len(visited) > 4096:
                raise PermissionError('conversation dependency budget exceeded')
            event = db.execute('SELECT id,created FROM life_events WHERE instance_id=? AND source_id=?',
                               (self.instance_id, ident)).fetchone()
            if event is None:
                continue
            if not archive and event['created'] < time.time()-self.retention_days*86400:
                raise PermissionError('conversation ancestor expired')
            for dep in db.execute('SELECT * FROM life_dependencies WHERE event_id=?', (event['id'],)):
                target = dep['memory_id'] if dep['kind']=='memory' else dep['source_id']
                if target is None:
                    raise PermissionError('conversation ancestor deleted')
                self._reference(db, owner_scope, dep['kind'], target, dep['revision'], ancestry=False)
                if dep['kind'] != 'memory':
                    owner = db.execute('SELECT owner FROM sources WHERE id=?', (target,)).fetchone()[0]
                    pending.append((self._owner_scope(db, owner_scope, owner), target, archive or bool(dep['archival'])))

    def _valid(self, db, scope, row):
        if row['source_id'] is None or row['created'] < time.time()-self.retention_days*86400:
            return False
        try:
            self._reference(db, scope, 'source', row['source_id'])
            return True
        except PermissionError:
            return False

    def _rows(self, db, scope, *, include_controls=False):
        self.store.validate_scope(db, scope)
        params = self._params(scope)
        return db.execute('''SELECT * FROM life_events WHERE instance_id=? AND domain=? AND owner=? AND origin=?
            AND source_id IS NOT NULL '''+('' if include_controls else "AND kind!='control' ")+
            'ORDER BY created DESC,id DESC', params).fetchall()

    def assert_message_current(self, scope):
        with self.store.connection() as db:
            self.store.validate_scope(db, scope)
            row = db.execute('''SELECT source_id FROM life_events WHERE instance_id=? AND domain=?
                AND owner=? AND origin=? AND message_id=? AND role='member' ''',
                (*self._params(scope), scope.message_id)).fetchone()
            if row is not None and row['source_id'] is None:
                raise PermissionError('original source was revoked')

    def remember_receipt(self, scope, receipt):
        with self.store.connection(True) as db:
            self.store.validate_scope(db, scope)
            db.execute('''INSERT OR IGNORE INTO life_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (uuid.uuid4().hex, *self._params(scope), receipt['source_id'], scope.message_id,
                 'member', '', 'control', 'control', time.time()))

    def prune(self, scope):
        """Delete stale raw derivatives through the same independent deletion ledger.

        Keep text-free message tombstones so a later bot quote cannot revive a
        deleted derivative. Repeating this after restoring content is idempotent.
        """
        for _ in range(self.max_events+1):
            with self.store.connection() as db:
                rows = self._rows(db, scope, include_controls=True)
                invalid = []
                for index,row in enumerate(rows):
                    if index < self.max_events and self._valid(db, scope, row):
                        continue
                    # Confirmed facts retain their exact evidence until their
                    # own expiry/forget. Expired conversation views still omit
                    # them; unconfirmed raw conversation has bounded retention.
                    protected = db.execute('''SELECT 1 FROM memories m
                        JOIN evidence e ON e.memory_id=m.id JOIN sources s ON s.id=e.source_id
                        WHERE m.domain=? AND m.owner=? AND m.origin=? AND m.state='active'
                          AND (EXISTS(SELECT 1 FROM person_facts p WHERE p.memory_id=m.id AND p.instance_id=?)
                            OR EXISTS(SELECT 1 FROM associations a WHERE a.memory_id=m.id AND a.kind='event'
                              AND a.label IN (?,?)))
                          AND (m.expires IS NULL OR m.expires>?) AND e.source_id=? AND s.purged IS NULL
                          AND s.revision=e.source_revision AND s.sha256=e.sha256 LIMIT 1''',
                        (self.store.domain_id, *scope.pair, self.instance_id,'life:'+self.instance_id+':episode',
                         'life:'+self.instance_id+':agreement',time.time(),row['source_id'])).fetchone()
                    if protected is None:
                        invalid.append(row['source_id'])
                    else:
                        try:
                            # A retained episode cannot protect a reply whose
                            # original fact/quote dependency was revoked.
                            self._reference(db,scope,'source',row['source_id'],archival=True)
                        except PermissionError:
                            invalid.append(row['source_id'])
            if not invalid:
                return scope
            receipt = self.store.forget(scope, source_ids=sorted(set(invalid)))
            scope = replace(scope, deletion_epoch=receipt['deletion_epoch'])
        raise PermissionError('conversation dependency cleanup did not converge')

    def observe(self, scope, text, *, reply_to='', verified_bot_reply=False):
        self._params(scope)
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 2000:
            raise ValueError('conversation text must be bounded original input')
        scope = self.prune(scope)
        with self.store.connection() as db:
            prior = db.execute('''SELECT source_id FROM life_events WHERE instance_id=? AND domain=?
                AND owner=? AND origin=? AND message_id=? AND role='member' ''',
                (*self._params(scope), scope.message_id)).fetchone()
            if prior is not None and prior['source_id'] is None:
                raise PermissionError('original conversation source was revoked')
            rows = [r for r in self._rows(db, scope) if self._valid(db, scope, r)]
            anchor = next((r for r in rows if verified_bot_reply and r['role']=='self'
                           and r['message_id']==reply_to), None)
            own = [r for r in rows if r['role']=='member']
            latest = own[0] if own else None
            topic = anchor['topic_id'] if anchor is not None else None
            if topic is None and latest is not None and latest['kind'] != 'closing':
                old = self.store.owned(db, 'sources', latest['source_id'], scope)['user_text']
                if (time.time()-latest['created'] <= 600 and (_FOLLOW.search(text) or _CLOSE.fullmatch(text))
                        or len(keywords(text) & keywords(old)) >= 2):
                    topic = latest['topic_id']
        receipt = self.store.append_turn(scope, text, '', enqueue=False)
        kind = 'closing' if _CLOSE.fullmatch(text) else 'question' if re.search(r'[?？]|[吗呢]$', text) else 'statement'
        with self.store.connection(True) as db:
            self.store.validate_scope(db, scope)
            db.execute('''INSERT INTO life_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instance_id,domain,owner,origin,message_id,role)
                DO UPDATE SET source_id=excluded.source_id,kind=excluded.kind''',
                (uuid.uuid4().hex, *self._params(scope), receipt['source_id'], scope.message_id,
                 'member', reply_to if verified_bot_reply else '', topic or uuid.uuid4().hex, kind, time.time()))
        return scope, receipt

    def view(self, scope, *, exclude_message_id=''):
        with self.store.connection() as db:
            rows = [r for r in self._rows(db, scope) if self._valid(db, scope, r)]
            current = next((r for r in rows if r['role']=='member' and r['message_id']==scope.message_id), None)
            topic = current['topic_id'] if current is not None else (rows[0]['topic_id'] if rows else '')
            selected = [r for r in rows if r['topic_id']==topic and r['message_id']!=exclude_message_id][:8]
            events = []
            refs = []
            for row in reversed(selected):
                source = self.store.owned(db, 'sources', row['source_id'], scope)
                refs.append(self._reference(db, scope, 'source', source['id']))
                events.append({'event_id': row['id'], 'role': row['role'], 'message_id': row['message_id'],
                    'reply_to': row['reply_to'], 'kind': row['kind'], 'time': row['created'],
                    'text': memory_api().clip(source['user_text'] if row['role']=='member' else source['assistant_text'], 600)})
            answered = {r['reply_to'] for r in rows if r['role']=='self'}
            state = {'topic_id': topic, 'events': events,
                'awaiting_reply': [r['message_id'] for r in rows if r['topic_id']==topic
                    and r['kind']=='question' and r['message_id'] not in answered][:8],
                'closed': bool(current is not None and current['kind']=='closing')}
            return state, tuple(refs)

    def check_references(self, scope, refs):
        self.store.snapshot(scope)
        with self.store.connection() as db:
            for ref in refs:
                self._reference(db, scope, ref['kind'], ref['id'], ref['revision'], archival=ref.get('archival',False))

    def quote_reference(self, scope, message_id):
        with self.store.connection() as db:
            self.store.validate_scope(db, scope)
            row = db.execute('''SELECT source_id FROM life_events WHERE instance_id=? AND domain=? AND origin=?
                AND message_id=? AND role='self' ''', (self.instance_id, self.store.domain_id, scope.origin, message_id)).fetchone()
            if row is None:
                return None  # Existing public transport quote, outside this memory service.
            if row['source_id'] is None:
                raise PermissionError('bot quote was revoked')
            return self._reference(db, scope, 'quote', row['source_id'])

    def record_reply(self, scope, message_id, text, refs):
        if re.fullmatch(r'-?[0-9]{1,20}', message_id) is None or not isinstance(text, str) or len(text.encode()) > 16000:
            raise ValueError('invalid acknowledged reply')
        with self.store.connection(True) as db:
            request = next((r for r in self._rows(db, scope)
                            if r['role']=='member' and r['message_id']==scope.message_id), None)
            if request is None or not self._valid(db, scope, request):
                raise PermissionError('conversation request expired')
            refs = (*refs, self._reference(db, scope, 'source', request['source_id']))
            self._record_in(db, scope, message_id, text, refs, scope.message_id, request['topic_id'], 'reply')

    def record_delivery(self, scope, message_id, text, refs, topic_id):
        if re.fullmatch(r'-?[0-9]{1,20}', message_id) is None or not isinstance(text, str) or len(text.encode()) > 16000:
            raise ValueError('invalid acknowledged contact')
        with self.store.connection(True) as db:
            self._record_in(db, scope, message_id, text, refs, '', topic_id, 'contact')

    def _record_in(self, db, scope, message_id, text, refs, reply_to, topic_id, kind):
        self.store.validate_scope(db, scope)
        # Validate before inserting plaintext; all evidence and metadata
        # commit atomically with the delivered source.
        for ref in refs:
            self._reference(db, scope, ref['kind'], ref['id'], ref['revision'], archival=ref.get('archival', False))
        old = db.execute('''SELECT source_id FROM life_events WHERE instance_id=? AND domain=?
            AND owner=? AND origin=? AND message_id=? AND role='self' ''', (*self._params(scope), message_id)).fetchone()
        if old is not None:
            if old['source_id'] is None:
                raise PermissionError('delivered source was revoked')
            return
        receipt = self.store.append_in(db, replace(scope, message_id='sent:'+message_id), '', text)
        ident = uuid.uuid4().hex
        db.execute('''INSERT OR IGNORE INTO life_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (ident, *self._params(scope), receipt['source_id'], message_id, 'self',
             reply_to, topic_id, kind, time.time()))
        row = db.execute('''SELECT id FROM life_events WHERE instance_id=? AND domain=? AND owner=? AND origin=?
            AND message_id=? AND role='self' ''', (*self._params(scope), message_id)).fetchone()
        for ref in refs:
            self._reference(db, scope, ref['kind'], ref['id'], ref['revision'], archival=ref.get('archival',False))
            db.execute('''INSERT OR IGNORE INTO life_dependencies
                (event_id,kind,ref_id,memory_id,source_id,revision,archival) VALUES (?,?,?,?,?,?,?)''',
                (row['id'], ref['kind'], ref['id'], ref['id'] if ref['kind']=='memory' else None,
                 ref['id'] if ref['kind'] in {'source','quote'} else None, ref['revision'],int(ref.get('archival',False))))

    def suppressed(self, chat):
        with self.store.connection() as db:
            return {r[0] for r in db.execute('''SELECT message_id FROM life_events
                WHERE instance_id=? AND domain=? AND origin=? AND role='self' AND source_id IS NULL''',
                (self.instance_id, self.store.domain_id, chat))}

    def block_delivered(self, scope, message_id):
        """A late transport ACK may only write a text-free restriction."""
        if re.fullmatch(r'-?[0-9]{1,20}', message_id) is None:
            raise ValueError('invalid acknowledged message ID')
        with self.store.connection(True) as db:
            db.execute('''INSERT OR IGNORE INTO life_events VALUES (?,?,?,?,?,NULL,?,?,?,?,?,?)''',
                (uuid.uuid4().hex, *self._params(scope), message_id, 'self', scope.message_id,
                 'revoked', 'reply', time.time()))
