"""Authoritative scoped SQLite content, durable jobs and deletion recovery.

The deletion ledger deliberately lives outside the content/derived-index directory.
Backups must retain that ledger independently; rolling back both is not detectable
without an external watermark and must not be used as a recovery procedure.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock

from .scope import ScopeContext


def terms(text):
    text = text.casefold()
    result = set(re.findall(r'[a-z0-9_]+', text))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        result.update(run[i:i+2] for i in range(len(run)-1))
        if len(run) == 1:
            result.add(run)
    return sorted(result)[:128]


def clip(text, budget):
    return text.encode('utf-8')[:max(0, budget)].decode('utf-8', 'ignore')


class Store:
    def __init__(self, home, domain_id='legacy'):
        if not isinstance(domain_id, str) or not domain_id or len(domain_id) > 256:
            raise ValueError('invalid domain')
        self.domain_id = domain_id
        self._lock = RLock()
        self._ready = False
        directory = Path(home) / 'scoped-memory'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / 'memory.sqlite3'
        content_existed = self.path.exists() and self.path.stat().st_size > 0
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with sqlite3.connect(self.path, timeout=15) as initial:
            old_schema = initial.execute('PRAGMA user_version').fetchone()[0]
            had_sources = initial.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'").fetchone() is not None
            if old_schema > 2:
                raise RuntimeError('unsupported memory schema')
        ledger_directory = Path(home) / 'scoped-memory-deletions'
        ledger_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ledger_directory, 0o700)
        self.ledger_path = ledger_directory / 'ledger.sqlite3'
        ledger_existed = self.ledger_path.exists()
        fd = os.open(self.ledger_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.ledger_path, 0o600)
        with sqlite3.connect(self.ledger_path, timeout=15) as ledger:
            ledger.executescript('''
              PRAGMA synchronous=FULL;
              CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), uuid TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS deletions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL,
                owner TEXT NOT NULL, origin TEXT NOT NULL, epoch INTEGER NOT NULL,
                targets TEXT NOT NULL, created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS deletion_scope ON deletions(domain,owner,origin,epoch);
            ''')
            ledger.execute('INSERT OR IGNORE INTO identity VALUES (1,?)', (uuid.uuid4().hex,))
        if content_existed and had_sources and old_schema < 2:
            self._backup_legacy(Path(home), old_schema)
        with sqlite3.connect(self.path, timeout=15) as initial:
            initial.execute('PRAGMA journal_mode=WAL')
        # Individual DDL statements stay inside one transaction; executescript
        # would implicitly commit before ALTER and break migration rollback.
        with self.connection(True) as db:
            self._schema(db, '''
            CREATE TABLE IF NOT EXISTS sources (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, origin TEXT NOT NULL,
              session TEXT NOT NULL, message_id TEXT NOT NULL, created REAL NOT NULL,
              user_text TEXT, assistant_text TEXT, sha256 TEXT NOT NULL, purged REAL);
            CREATE INDEX IF NOT EXISTS sources_scope ON sources(owner,origin,created);
            CREATE TABLE IF NOT EXISTS memories (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, origin TEXT NOT NULL,
              content TEXT NOT NULL, source_id TEXT REFERENCES sources(id),
              created REAL NOT NULL, expires REAL, state TEXT NOT NULL DEFAULT 'active',
              supersedes TEXT REFERENCES memories(id));
            CREATE INDEX IF NOT EXISTS memories_scope ON memories(owner,origin,state);
            CREATE TABLE IF NOT EXISTS terms (
              memory_id TEXT REFERENCES memories(id), term TEXT NOT NULL,
              PRIMARY KEY(memory_id,term));
            CREATE INDEX IF NOT EXISTS term_lookup ON terms(term,memory_id);
            CREATE TABLE IF NOT EXISTS associations (
              memory_id TEXT REFERENCES memories(id), kind TEXT NOT NULL, label TEXT NOT NULL,
              PRIMARY KEY(memory_id,kind,label));
            CREATE INDEX IF NOT EXISTS association_lookup ON associations(kind,label,memory_id);
            ''')
            # Existing content belongs to legacy, never to a newly configured domain.
            for table in ('sources', 'memories'):
                columns = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
                if 'domain' not in columns:
                    db.execute("ALTER TABLE " + table + " ADD COLUMN domain TEXT NOT NULL DEFAULT 'legacy'")
                if 'revision' not in columns:
                    db.execute('ALTER TABLE ' + table + ' ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')
            self._schema(db, '''
              CREATE TABLE IF NOT EXISTS scope_versions (
                domain TEXT NOT NULL, owner TEXT NOT NULL, origin TEXT NOT NULL,
                policy_revision INTEGER NOT NULL, deletion_epoch INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(domain,owner,origin));
              CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, domain TEXT NOT NULL, owner TEXT NOT NULL, origin TEXT NOT NULL,
                scope_json TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                source_revision INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                lease_until REAL, lease_token TEXT, worker_id TEXT, next_run_at REAL NOT NULL,
                error_code TEXT, created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(state,next_run_at);
              CREATE TABLE IF NOT EXISTS extraction_windows (
                source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL, completed REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS source_revisions (
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL, user_text TEXT, assistant_text TEXT,
                sha256 TEXT NOT NULL, created REAL NOT NULL, purged REAL,
                PRIMARY KEY(source_id,revision));
              CREATE TABLE IF NOT EXISTS evidence (
                memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                source_revision INTEGER NOT NULL, role TEXT NOT NULL,
                start INTEGER NOT NULL, end INTEGER NOT NULL, unit TEXT NOT NULL,
                sha256 TEXT NOT NULL, PRIMARY KEY(memory_id,source_id,start));
              CREATE INDEX IF NOT EXISTS source_identity ON sources(domain,owner,origin,session,message_id);
              CREATE INDEX IF NOT EXISTS memory_domain_scope ON memories(domain,owner,origin,state);
              PRAGMA user_version=2;
            ''')
            # Legacy archives have only their current text. Preserve that version
            # without inventing lost revisions or scheduling cloud re-extraction.
            db.execute('''INSERT OR IGNORE INTO source_revisions
                          SELECT id,revision,user_text,assistant_text,sha256,created,purged FROM sources''')
            meta = dict(db.execute('SELECT key,value FROM store_meta'))
            if meta and not ledger_existed:
                raise PermissionError('independent deletion ledger missing; recovery denied')
            with sqlite3.connect(self.ledger_path, timeout=15) as ledger:
                ledger_id = ledger.execute('SELECT uuid FROM identity WHERE id=1').fetchone()[0]
            if not meta:
                db.executemany('INSERT INTO store_meta VALUES (?,?)', [('ledger_id', ledger_id), ('deletion_watermark', '0')])
        self._ready = True
        with self.connection(True):
            pass

    @staticmethod
    def _schema(db, source):
        for statement in source.split(';'):
            if statement.strip():
                db.execute(statement)

    def _backup_legacy(self, home, version):
        directory = home / 'scoped-memory-migrations'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        destination = directory / ('schema-' + str(version) + '-' + uuid.uuid4().hex + '.sqlite3')
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        # Exceptions deliberately escape before any schema mutation. SQLite's
        # backup API includes committed WAL data; copying the main file does not.
        with sqlite3.connect(self.path, timeout=15) as source, sqlite3.connect(destination, timeout=15) as backup:
            source.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('migration backup verification failed')

    @contextmanager
    def connection(self, write=False):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA secure_delete=ON')
            try:
                # Recovery can write even on a read operation; guard before exposing rows.
                if write or self._ready:
                    db.execute('BEGIN IMMEDIATE')
                if self._ready:
                    self._replay_deletions(db)
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def scope_pair(self, scope):
        if isinstance(scope, ScopeContext):
            if scope.domain_id != self.domain_id:
                raise PermissionError('domain mismatch')
            return scope.pair
        if not isinstance(scope, (tuple, list)) or len(scope) != 2 or not all(isinstance(v, str) and v for v in scope):
            raise ValueError('invalid scope')
        return tuple(scope)

    def validate_scope(self, db, scope):
        pair = self.scope_pair(scope)
        row = db.execute('SELECT * FROM scope_versions WHERE domain=? AND owner=? AND origin=?',
                         (self.domain_id, *pair)).fetchone()
        policy = scope.policy_revision if isinstance(scope, ScopeContext) else 1
        if row is None:
            db.execute('INSERT INTO scope_versions VALUES (?,?,?,?,0)', (self.domain_id, *pair, policy))
            epoch, current_policy = 0, policy
        else:
            epoch, current_policy = row['deletion_epoch'], row['policy_revision']
        if isinstance(scope, ScopeContext) and (scope.policy_revision != current_policy or scope.deletion_epoch != epoch):
            raise PermissionError('stale scope policy or deletion epoch')
        return epoch

    def snapshot(self, scope):
        """Validate an existing snapshot; never refresh an old asynchronous authority."""
        if not isinstance(scope, ScopeContext):
            return self.resolve_scope(scope)
        with self.connection(True) as db:
            self.validate_scope(db, scope)
        return scope

    def resolve_scope(self, scope):
        """Bind freshly authenticated input to the current deletion epoch.

        Only the request identity resolver may call this. Persisted jobs and cached
        results must use snapshot/validate_scope and are rejected when stale.
        """
        pair = self.scope_pair(scope)
        with self.connection(True) as db:
            row = db.execute('SELECT * FROM scope_versions WHERE domain=? AND owner=? AND origin=?',
                             (self.domain_id, *pair)).fetchone()
            if row is None:
                self.validate_scope(db, scope)
                epoch = 0
            else:
                epoch = row['deletion_epoch']
                if isinstance(scope, ScopeContext) and scope.policy_revision != row['policy_revision']:
                    raise PermissionError('stale policy revision')
        if isinstance(scope, ScopeContext):
            return replace(scope, deletion_epoch=epoch)
        return ScopeContext(self.domain_id, 'legacy', pair[0], 'legacy', pair[1], deletion_epoch=epoch)

    def update_policy(self, scope, revision):
        if type(revision) is not int or revision < 1:
            raise ValueError('invalid policy revision')
        pair = self.scope_pair(scope)
        with self.connection(True) as db:
            row = db.execute('SELECT policy_revision FROM scope_versions WHERE domain=? AND owner=? AND origin=?',
                             (self.domain_id, *pair)).fetchone()
            if row and revision < row[0]:
                raise PermissionError('policy revision rollback denied')
            db.execute('''INSERT INTO scope_versions VALUES (?,?,?,?,0)
                          ON CONFLICT(domain,owner,origin) DO UPDATE SET policy_revision=excluded.policy_revision''',
                       (self.domain_id, *pair, revision))
        if not isinstance(scope, ScopeContext):
            scope = ScopeContext(self.domain_id, 'legacy', pair[0], 'legacy', pair[1])
        return self.resolve_scope(replace(scope, policy_revision=revision))

    def _replay_deletions(self, db):
        meta = dict(db.execute('SELECT key,value FROM store_meta'))
        # mode=rw avoids silently creating an empty replacement for a lost ledger.
        with sqlite3.connect(self.ledger_path.as_uri() + '?mode=rw', uri=True, timeout=15) as ledger:
            identity = ledger.execute('SELECT uuid FROM identity WHERE id=1').fetchone()
            seq, count = ledger.execute('SELECT COALESCE(MAX(seq),0),COUNT(*) FROM deletions').fetchone()
            allocated = ledger.execute("SELECT seq FROM sqlite_sequence WHERE name='deletions'").fetchone()
            if (not identity or identity[0] != meta.get('ledger_id') or count != seq
                    or (allocated is not None and allocated[0] != seq)
                    or seq < int(meta.get('deletion_watermark', '0'))):
                raise PermissionError('deletion ledger rollback or replacement; recovery denied')
            rows = ledger.execute('SELECT seq,domain,owner,origin,epoch,targets FROM deletions WHERE seq>? ORDER BY seq',
                                  (int(meta.get('deletion_watermark', '0')),)).fetchall()
        for number, domain, owner, origin, epoch, targets in rows:
            self._scrub(db, domain, (owner, origin), json.loads(targets))
            db.execute('''INSERT INTO scope_versions VALUES (?,?,?,1,?) ON CONFLICT(domain,owner,origin)
                          DO UPDATE SET deletion_epoch=MAX(deletion_epoch,excluded.deletion_epoch)''',
                       (domain, owner, origin, epoch))
            db.execute("UPDATE store_meta SET value=? WHERE key='deletion_watermark'", (str(number),))

    @staticmethod
    def _scrub(db, domain, pair, targets):
        where, params = 'domain=? AND owner=? AND origin=?', (domain, *pair)
        if targets['all']:
            memory_ids = [r[0] for r in db.execute('SELECT id FROM memories WHERE '+where, params)]
            source_ids = [r[0] for r in db.execute('SELECT id FROM sources WHERE '+where, params)]
        else:
            memory_ids = list(targets['memories'])
            source_ids = list(targets['sources'])
        if source_ids:
            marks = ','.join('?' for _ in source_ids)
            memory_ids += [r[0] for r in db.execute('SELECT id FROM memories WHERE '+where+' AND source_id IN ('+marks+')', (*params, *source_ids))]
            memory_ids += [r[0] for r in db.execute('''SELECT m.id FROM memories m JOIN evidence e ON e.memory_id=m.id
                WHERE m.domain=? AND m.owner=? AND m.origin=? AND e.source_id IN ('''+marks+')', (*params, *source_ids))]
        memory_ids = list(set(memory_ids))
        for ident in memory_ids:
            # Old schema FKs have no CASCADE, so clear legacy references explicitly.
            db.execute('UPDATE memories SET supersedes=NULL WHERE supersedes=?', (ident,))
            db.execute('DELETE FROM terms WHERE memory_id=?', (ident,))
            db.execute('DELETE FROM associations WHERE memory_id=?', (ident,))
            db.execute('DELETE FROM evidence WHERE memory_id=?', (ident,))
            db.execute('DELETE FROM memories WHERE '+where+' AND id=?', (*params, ident))
        for ident in source_ids:
            db.execute('DELETE FROM sources WHERE '+where+' AND id=?', (*params, ident))
        # Cancel every unfinished job in the affected scope: its old epoch is revoked.
        db.execute("DELETE FROM outbox WHERE "+where+" AND state!='done'", params)

    def forget(self, scope, memory_ids=None, source_ids=None):
        pair = self.scope_pair(scope)
        memory_ids, source_ids = list(memory_ids or []), list(source_ids or [])
        if any(not isinstance(v, str) or not v for v in memory_ids+source_ids):
            raise ValueError('invalid forget targets')
        # Validate targets without revealing existence across scopes. Removing a fact also
        # removes its raw evidence so a subsequent extraction cannot recreate that fact.
        with self.connection(True) as db:
            self.validate_scope(db, scope)
            for ident in memory_ids:
                row = self.owned(db, 'memories', ident, scope)
                if row['source_id']:
                    source_ids.append(row['source_id'])
                source_ids += [r[0] for r in db.execute('SELECT source_id FROM evidence WHERE memory_id=?', (ident,))]
            for ident in source_ids:
                self.owned(db, 'sources', ident, scope)
            with sqlite3.connect(self.ledger_path, timeout=15) as ledger:
                ledger.execute('BEGIN IMMEDIATE')
                epoch = ledger.execute('SELECT COALESCE(MAX(epoch),0)+1 FROM deletions WHERE domain=? AND owner=? AND origin=?',
                                       (self.domain_id, *pair)).fetchone()[0]
                targets = {'all': not memory_ids and not source_ids, 'memories': sorted(set(memory_ids)), 'sources': sorted(set(source_ids))}
                cursor = ledger.execute('INSERT INTO deletions(domain,owner,origin,epoch,targets,created) VALUES (?,?,?,?,?,?)',
                                        (self.domain_id, *pair, epoch, json.dumps(targets), time.time()))
                seq = cursor.lastrowid
            self._replay_deletions(db)
        return {'status': 'active_storage_cleared', 'deletion_epoch': epoch, 'deletion_sequence': seq,
                'backup_status': 'rotation_required', 'context_status': 'host_invalidation_required'}

    def archive(self, scope, session, message_id, user, assistant):
        """Compatibility append; legacy callers do not create extraction jobs."""
        if not isinstance(user, str) or not isinstance(assistant, str):
            raise ValueError('turn content must be text')
        sid = uuid.uuid4().hex
        digest = hashlib.sha256(json.dumps([user, assistant], ensure_ascii=False).encode()).hexdigest()
        with self.connection(True) as db:
            self.validate_scope(db, scope)
            db.execute('''INSERT INTO sources
              (id,owner,origin,session,message_id,created,user_text,assistant_text,sha256,purged,domain,revision)
              VALUES (?,?,?,?,?,?,?,?,?,NULL,?,1)''',
                       (sid, *self.scope_pair(scope), session, message_id, time.time(), user, assistant, digest, self.domain_id))
            self._record_source_revision(db, sid)
        return sid

    @staticmethod
    def _record_source_revision(db, ident):
        db.execute('''INSERT OR IGNORE INTO source_revisions
                      SELECT id,revision,user_text,assistant_text,sha256,?,purged FROM sources WHERE id=?''',
                   (time.time(), ident))

    def append_turn(self, scope, user, assistant, enqueue=False):
        with self.connection(True) as db:
            return self.append_in(db, scope, user, assistant, enqueue=enqueue)

    def append_in(self, db, scope, user, assistant, enqueue=False):
        """Append in the caller's transaction, including dependent local records."""
        if not isinstance(scope, ScopeContext):
            raise ValueError('durable jobs require an immutable scope snapshot')
        if not isinstance(user, str) or not isinstance(assistant, str):
            raise ValueError('turn content must be text')
        if not scope.session_id:
            raise ValueError('session identity is required')
        sid = uuid.uuid4().hex
        # Hash structured bytes, preserving role boundaries and exact text.
        digest = hashlib.sha256(json.dumps([user, assistant], ensure_ascii=False).encode()).hexdigest()
        self.validate_scope(db, scope)
        # Empty message IDs cannot safely deduplicate distinct identical turns.
        old = None
        if scope.message_id:
            old = db.execute('''SELECT * FROM sources WHERE domain=? AND owner=? AND origin=?
                                AND session=? AND message_id=? ORDER BY revision DESC LIMIT 1''',
                             (self.domain_id, *scope.pair, scope.session_id, scope.message_id)).fetchone()
        revision = 1
        if old is not None:
            sid, revision = old['id'], old['revision']
            if old['sha256'] != digest:
                revision += 1
                db.execute('UPDATE sources SET user_text=?,assistant_text=?,sha256=?,revision=?,purged=NULL WHERE id=?',
                           (user, assistant, digest, revision, sid))
                db.execute("UPDATE memories SET state='superseded' WHERE source_id=? AND state IN ('active','pending')", (sid,))
                db.execute("DELETE FROM outbox WHERE source_id=? AND state!='done'", (sid,))
        else:
            db.execute('''INSERT INTO sources
              (id,owner,origin,session,message_id,created,user_text,assistant_text,sha256,purged,domain,revision)
              VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?)''',
                       (sid, *scope.pair, scope.session_id, scope.message_id, time.time(), user, assistant, digest, self.domain_id, revision))
        self._record_source_revision(db, sid)
        jid = None
        if enqueue:
            key = hashlib.sha256(json.dumps([self.domain_id, sid, revision, scope.policy_revision, scope.deletion_epoch]).encode()).hexdigest()
            jid = uuid.uuid4().hex
            db.execute('''INSERT OR IGNORE INTO outbox
               (id,domain,owner,origin,scope_json,source_id,source_revision,idempotency_key,next_run_at,created)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
                       (jid, self.domain_id, *scope.pair, json.dumps(scope.to_dict()), sid, revision, key, time.time(), time.time()))
            jid = db.execute('SELECT id FROM outbox WHERE idempotency_key=?', (key,)).fetchone()[0]
        return {'status': 'durably_accepted', 'source_id': sid, 'source_revision': revision, 'job_id': jid}

    def owned(self, db, table, ident, scope):
        if table not in ('sources', 'memories'):
            raise ValueError('invalid table')
        self.validate_scope(db, scope)
        row = db.execute(f'SELECT * FROM {table} WHERE id=? AND domain=? AND owner=? AND origin=?',
                         (ident, self.domain_id, *self.scope_pair(scope))).fetchone()
        if row is None:
            raise PermissionError('not found in current scope')
        return dict(row)

    def sources(self, scope, limit=8):
        with self.connection() as db:
            self.validate_scope(db, scope)
            return [dict(r) for r in db.execute('''SELECT id,origin,session,message_id,created,sha256,purged
                FROM sources WHERE domain=? AND owner=? AND origin=? ORDER BY created DESC LIMIT ?''',
                (self.domain_id, *self.scope_pair(scope), max(1, min(int(limit), 20))))]

    def source(self, scope, ident, offset=0, limit=4000, revision=None):
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError('source revision must be a positive integer')
        with self.connection() as db:
            row = self.owned(db, 'sources', ident, scope)
            if revision is not None:
                version = db.execute('''SELECT revision,user_text,assistant_text,sha256,created AS revision_created,purged
                                        FROM source_revisions WHERE source_id=? AND revision=?''',
                                     (ident, revision)).fetchone()
                if version is None:
                    raise PermissionError('source revision not found in current scope')
                row.update(dict(version))
        # Source is explicitly requested, never automatically injected.
        text = json.dumps({'user': row.pop('user_text'), 'assistant': row.pop('assistant_text')}, ensure_ascii=False)
        raw = text.encode('utf-8')
        row['excerpt'] = raw[max(0, offset):max(0, offset)+min(4000, max(0, limit))].decode('utf-8', 'ignore')
        row['total_bytes'] = len(raw)
        return row

    def add(self, scope, content, source_id=None, associations=None, ttl_days=None, supersedes=None):
        with self.connection(True) as db:
            self.validate_scope(db, scope)
            ident = self._add(db, scope, content, source_id, associations, ttl_days, supersedes)
        return self.get(scope, ident)

    def _add(self, db, scope, content, source_id=None, associations=None, ttl_days=None, supersedes=None, state='active'):
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > 16000:
            raise ValueError('content must be nonempty and <=16000 UTF-8 bytes')
        associations = associations or {}
        if not isinstance(associations, dict) or set(associations) - {'topic', 'person', 'event'}:
            raise ValueError('associations must contain topic/person/event lists')
        labels = []
        for kind, values in associations.items():
            if not isinstance(values, list) or len(values) > 16:
                raise ValueError('association lists have max 16 labels')
            for label in values:
                if not isinstance(label, str) or not label.strip() or len(label) > 100:
                    raise ValueError('invalid association label')
                labels.append((kind, label.casefold().strip()))
        expires = None if ttl_days is None else time.time() + float(ttl_days)*86400
        if ttl_days is not None and (type(ttl_days) not in (int, float) or not 0 < float(ttl_days) <= 36500):
            raise ValueError('ttl_days must be in (0,36500]')
        ident = uuid.uuid4().hex
        if source_id:
            self.owned(db, 'sources', source_id, scope)
        revision = 1
        if supersedes:
            old = self.owned(db, 'memories', supersedes, scope)
            if old['state'] != 'active':
                raise ValueError('only active memories can be superseded')
            revision = old['revision'] + 1
            db.execute("UPDATE memories SET state='superseded' WHERE id=?", (supersedes,))
        db.execute('''INSERT INTO memories
          (id,owner,origin,content,source_id,created,expires,state,supersedes,domain,revision)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                   (ident, *self.scope_pair(scope), content, source_id, time.time(), expires, state, supersedes, self.domain_id, revision))
        indexed = terms(content + ' ' + ' '.join(label for _, label in labels))
        db.executemany('INSERT INTO terms VALUES (?,?)', [(ident, term) for term in indexed])
        db.executemany('INSERT OR IGNORE INTO associations VALUES (?,?,?)', [(ident, *label) for label in labels])
        return ident

    def get(self, scope, ident):
        with self.connection() as db:
            row = self.owned(db, 'memories', ident, scope)
            row['associations'] = [dict(r) for r in db.execute('SELECT kind,label FROM associations WHERE memory_id=?', (ident,))]
            # A persistent citation is re-authorized at read time. Revised/purged
            # sources are history, not currently valid evidence for an old fact.
            row['evidence'] = [dict(r) for r in db.execute('''SELECT e.source_id,s.message_id,e.source_revision,
                e.role,e.start,e.end,e.unit,e.sha256 FROM evidence e JOIN sources s ON s.id=e.source_id
                WHERE e.memory_id=? AND s.domain=? AND s.owner=? AND s.origin=?
                AND s.revision=e.source_revision AND s.sha256=e.sha256 AND s.purged IS NULL''',
                (ident, self.domain_id, *self.scope_pair(scope)))]
        return row

    def mutate(self, scope, ident, state):
        if state not in ('deleted', 'archived'):
            raise ValueError('invalid state')
        with self.connection(True) as db:
            self.owned(db, 'memories', ident, scope)
            db.execute('UPDATE memories SET state=? WHERE id=?', (state, ident))
        return {'id': ident, 'state': state}

    def search(self, scope, query, limit=8):
        ts = terms(query)[:64]
        if not ts:
            return []
        marks = ','.join('?' for _ in ts)
        limit = max(1, min(int(limit), 20))
        with self.connection() as db:
            self.validate_scope(db, scope)
            rows = db.execute(f'''SELECT m.*,COUNT(*) AS score FROM memories m
              JOIN terms t ON t.memory_id=m.id WHERE m.domain=? AND m.owner=? AND m.origin=?
              AND m.state='active' AND (m.expires IS NULL OR m.expires>?)
              AND t.term IN ({marks}) GROUP BY m.id ORDER BY score DESC,m.created DESC LIMIT ?''',
              (self.domain_id, *self.scope_pair(scope), time.time(), *ts, limit)).fetchall()
            result = [dict(row) for row in rows]
            if result and len(result) < limit:
                ids = [r['id'] for r in result]
                marks = ','.join('?' for _ in ids)
                linked = db.execute(f'''SELECT DISTINCT m.*,0 AS score FROM memories m
                  JOIN associations a ON a.memory_id=m.id
                  JOIN associations b ON a.kind=b.kind AND a.label=b.label
                  WHERE b.memory_id IN ({marks}) AND m.domain=? AND m.owner=? AND m.origin=?
                  AND m.state='active' AND (m.expires IS NULL OR m.expires>?)
                  AND m.id NOT IN ({marks}) ORDER BY m.created DESC LIMIT ?''',
                  (*ids, self.domain_id, *self.scope_pair(scope), time.time(), *ids, limit-len(result))).fetchall()
                result += [dict(row) for row in linked]
        return result

    def pending(self, scope, limit=20):
        with self.connection() as db:
            self.validate_scope(db, scope)
            return [dict(r) for r in db.execute('''SELECT * FROM memories WHERE domain=? AND owner=? AND origin=?
                AND state='pending' AND (expires IS NULL OR expires>?) ORDER BY created DESC LIMIT ?''',
                (self.domain_id, *self.scope_pair(scope), time.time(), max(1, min(int(limit), 20))))]

    def confirm(self, scope, ident):
        """Operator-only promotion. Never expose this as model-supplied authority."""
        return self.decide_candidate(scope, ident, 'confirm')

    def _candidate(self, db, scope, ident):
        row = self.owned(db, 'memories', ident, scope)
        if (row['state'] != 'pending' or row['expires'] is not None and row['expires'] <= time.time()):
            raise PermissionError('candidate is no longer pending')
        evidence = [dict(item) for item in db.execute('''SELECT e.* FROM evidence e JOIN sources s ON s.id=e.source_id
            WHERE e.memory_id=? AND s.domain=? AND s.owner=? AND s.origin=? AND s.purged IS NULL
              AND s.revision=e.source_revision AND s.sha256=e.sha256 ORDER BY e.source_id,e.source_revision''',
            (ident, self.domain_id, *self.scope_pair(scope)))]
        if not evidence or len(evidence) != db.execute('SELECT COUNT(*) FROM evidence WHERE memory_id=?', (ident,)).fetchone()[0]:
            raise PermissionError('candidate source evidence is no longer current')
        descriptor = {'id': ident, 'domain': self.domain_id, 'scope': self.scope_pair(scope),
                      'epoch': self.validate_scope(db, scope), 'revision': row['revision'],
                      'policy_revision': scope.policy_revision if isinstance(scope, ScopeContext) else 1,
                      'content': row['content'], 'expires': row['expires'], 'evidence': evidence}
        digest = hashlib.sha256(json.dumps(descriptor, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        return row, digest

    def review_candidate(self, scope, ident):
        with self.connection(True) as db:
            row, digest = self._candidate(db, scope, ident)
            return {'id': ident, 'content': row['content'], 'source_id': row['source_id'], 'digest': digest}

    def candidate_in(self, db, scope, ident):
        """Public transaction extension: same evidence/policy review as ordinary candidates."""
        row, digest = self._candidate(db, scope, ident)
        return dict(row), digest

    def evidenced_in(self, db, scope, content, source_id, *, role, start, end, quote,
                     associations=None, ttl_days=None, state='pending'):
        """Create one evidenced record inside a caller-owned Store transaction."""
        source = self.owned(db, 'sources', source_id, scope)
        if (role not in {'user','assistant'} or type(start) is not int or type(end) is not int
                or not isinstance(quote, str) or not quote or not 0 <= start < end
                or source['purged'] is not None or source[role+'_text'] is None
                or end > len(source[role+'_text'])
                or source[role+'_text'][start:end] != quote or state not in {'pending','active'}):
            raise PermissionError('current exact source evidence required')
        ident = self._add(db, scope, content, source_id, associations, ttl_days, state=state)
        db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)',
                   (ident, source_id, source['revision'], role, start, end, 'characters', source['sha256']))
        return ident

    def decide_candidate(self, scope, ident, decision, expected_digest=None):
        if decision not in ('confirm', 'reject'):
            raise ValueError('invalid candidate decision')
        with self.connection(True) as db:
            _, digest = self._candidate(db, scope, ident)
            if expected_digest is not None and expected_digest != digest:
                raise PermissionError('candidate changed since review')
            db.execute('UPDATE memories SET state=? WHERE id=?', ('active' if decision == 'confirm' else 'rejected', ident))
        return self.get(scope, ident)

    @staticmethod
    def _lease_arguments(worker_id, lease_seconds, max_attempts):
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise ValueError('invalid worker identity')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise ValueError('invalid attempt budget')
        if type(lease_seconds) not in (int, float) or not 0 < lease_seconds <= 3600:
            raise ValueError('invalid lease duration')

    @staticmethod
    def _window_key(scope):
        return (scope.domain_id, scope.platform, scope.principal_id, scope.origin_kind,
                scope.origin_id, scope.thread_id, scope.session_id, scope.policy_revision, scope.deletion_epoch)

    def claim_job(self, worker_id, lease_seconds=60, max_attempts=5, scope=None):
        self._lease_arguments(worker_id, lease_seconds, max_attempts)
        with self.connection(True) as db:
            return self._claim_job(db, worker_id, lease_seconds, max_attempts, scope)

    def claim_window(self, worker_id, max_turns=8, idle_seconds=2, lease_seconds=60, max_attempts=5):
        """Lease one quiet, exact-scope session window for a single model call."""
        self._lease_arguments(worker_id, lease_seconds, max_attempts)
        if type(max_turns) is not int or not 1 <= max_turns <= 8:
            raise ValueError('max_turns must be integer 1..8')
        if type(idle_seconds) not in (int, float) or not 0 <= idle_seconds <= 86400:
            raise ValueError('invalid idle duration')
        with self.connection(True) as db:
            first = self._claim_job(db, worker_id, lease_seconds, max_attempts, idle_seconds=idle_seconds)
            if first is None:
                return []
            jobs = [first]
            for _ in range(max_turns-1):
                job = self._claim_job(db, worker_id, lease_seconds, max_attempts, scope=first['scope'])
                if job is None:
                    break
                jobs.append(job)
            return jobs

    def _claim_job(self, db, worker_id, lease_seconds, max_attempts, scope=None, idle_seconds=None):
        now = time.time()
        if scope is not None:
            if not isinstance(scope, ScopeContext):
                raise ValueError('job window requires a scope snapshot')
            self.validate_scope(db, scope)
        db.execute("UPDATE outbox SET state='pending',lease_token=NULL,worker_id=NULL WHERE domain=? AND state='running' AND lease_until<=?", (self.domain_id, now))
        db.execute("UPDATE outbox SET state='failed',error_code='attempt_budget_exhausted' WHERE domain=? AND state='pending' AND attempts>=?", (self.domain_id, max_attempts))
        query = '''SELECT o.* FROM outbox o JOIN sources s ON s.id=o.source_id
                   WHERE o.domain=? AND o.state='pending' AND o.next_run_at<=?'''
        values = [self.domain_id, now]
        if scope is not None:
            query += ' AND o.owner=? AND o.origin=? AND s.session=?'
            values.extend((*scope.pair, scope.session_id))
        if idle_seconds is not None:
            query += ''' AND NOT EXISTS (
                SELECT 1 FROM sources recent JOIN source_revisions history ON history.source_id=recent.id
                WHERE recent.domain=o.domain AND recent.owner=o.owner AND recent.origin=o.origin
                AND recent.session=s.session AND history.created>?)'''
            values.append(now-idle_seconds)
        rows = db.execute(query+' ORDER BY o.created', values).fetchall()
        for row in rows:
            try:
                candidate_scope = ScopeContext(**json.loads(row['scope_json']))
                self.validate_scope(db, candidate_scope)
                source = self.owned(db, 'sources', row['source_id'], candidate_scope)
                if source['revision'] != row['source_revision'] or source['purged'] is not None:
                    raise PermissionError('source changed')
                if scope is not None and self._window_key(candidate_scope) != self._window_key(scope):
                    continue
            except (ValueError, TypeError, PermissionError):
                db.execute("UPDATE outbox SET state='failed',error_code='stale_scope_or_source' WHERE id=?", (row['id'],))
                continue
            token = uuid.uuid4().hex
            db.execute("UPDATE outbox SET state='running',attempts=attempts+1,lease_until=?,lease_token=?,worker_id=? WHERE id=?", (now+lease_seconds, token, worker_id, row['id']))
            result = dict(db.execute('SELECT * FROM outbox WHERE id=?', (row['id'],)).fetchone())
            result['scope'] = candidate_scope
            return result
        return None

    def _job(self, db, job):
        if not isinstance(job, dict) or not job.get('id') or not job.get('lease_token'):
            raise ValueError('invalid leased job')
        row = db.execute("SELECT * FROM outbox WHERE id=? AND domain=? AND state='running' AND lease_token=? AND lease_until>?",
                         (job['id'], self.domain_id, job['lease_token'], time.time())).fetchone()
        if row is None:
            raise PermissionError('job lease expired or fenced')
        scope = ScopeContext(**json.loads(row['scope_json']))
        self.validate_scope(db, scope)
        source = self.owned(db, 'sources', row['source_id'], scope)
        if source['revision'] != row['source_revision'] or source['purged'] is not None:
            raise PermissionError('job source revised or purged')
        return row, scope, source

    def job_sources(self, job):
        with self.connection() as db:
            _, _, source = self._job(db, job)
            return [source]

    def retry_job(self, job, error_code, max_attempts=5, permanent=False):
        if not isinstance(error_code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', error_code):
            raise ValueError('error code must not contain exception details or message text')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise ValueError('invalid attempt budget')
        with self.connection(True) as db:
            row, _, _ = self._job(db, job)
            failed = permanent or row['attempts'] >= max_attempts
            state = 'failed' if failed else 'pending'
            # One total job attempt budget; this layer does not perform HTTP retries.
            db.execute('''UPDATE outbox SET state=?,next_run_at=?,error_code=?,lease_token=NULL,worker_id=NULL,lease_until=NULL
                          WHERE id=?''',
                       (state, time.time()+min(300, 2**row['attempts']), error_code, row['id']))
        return {'status': state}

    def complete_job(self, job, candidates):
        return self.complete_jobs([job], candidates)

    def complete_jobs(self, jobs, candidates):
        """Commit a whole same-scope extraction window, or none of its cursors."""
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= 8:
            raise ValueError('expected 1..8 leased jobs')
        if not isinstance(candidates, list) or len(candidates) > 32:
            raise ValueError('expected at most 32 extraction candidates')
        with self.connection(True) as db:
            leased = [self._job(db, job) for job in jobs]
            scope = leased[0][1]
            if len({row['id'] for row, _, _ in leased}) != len(jobs):
                raise ValueError('duplicate leased job')
            if any(self._window_key(item_scope) != self._window_key(scope) for _, item_scope, _ in leased):
                raise PermissionError('extraction window crosses scope or session')
            sources = {source['id']: source for _, _, source in leased}
            if len(sources) != len(jobs):
                raise ValueError('duplicate source in extraction window')
            validated = []
            for candidate in candidates:
                allowed = {'content', 'source_id', 'quote', 'start', 'role', 'associations'}
                if not isinstance(candidate, dict) or set(candidate)-allowed:
                    raise ValueError('unknown candidate fields or forged authority')
                if candidate.get('source_id') not in sources or candidate.get('role') != 'user':
                    raise ValueError('evidence outside input user-source whitelist')
                source = sources[candidate['source_id']]
                quote, start = candidate.get('quote'), candidate.get('start')
                if not isinstance(quote, str) or not quote or len(quote.encode()) > 8000 or type(start) is not int or start < 0:
                    raise ValueError('invalid evidence coordinates')
                if source['user_text'][start:start+len(quote)] != quote:
                    raise ValueError('evidence does not match source character offsets')
                validated.append((candidate, source, start+len(quote)))
            ids = []
            for candidate, source, end in validated:
                # Extraction supplies suggestions, never an authenticated confirmation.
                ident = self._add(db, scope, candidate.get('content'), source['id'], candidate.get('associations'), state='pending')
                db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)',
                           (ident, source['id'], source['revision'], 'user', candidate['start'], end, 'characters', source['sha256']))
                ids.append(ident)
            for row, _, source in leased:
                db.execute('''INSERT INTO extraction_windows VALUES (?,?,?) ON CONFLICT(source_id)
                              DO UPDATE SET revision=excluded.revision,completed=excluded.completed''',
                           (source['id'], source['revision'], time.time()))
                db.execute("UPDATE outbox SET state='done',lease_token=NULL,worker_id=NULL,lease_until=NULL,error_code=NULL WHERE id=?", (row['id'],))
        return {'status': 'done', 'memory_ids': ids}

    def retain(self, archive_days=365):
        if archive_days is not None and float(archive_days) <= 0:
            raise ValueError('archive retention must be positive or null')
        now = time.time()
        with self.connection(True) as db:
            expired = db.execute("UPDATE memories SET state='archived' WHERE domain=? AND state='active' AND expires<=?", (self.domain_id, now)).rowcount
            purged = 0
            if archive_days is not None:
                purged = db.execute('''UPDATE sources SET user_text=NULL,assistant_text=NULL,purged=?
                    WHERE domain=? AND purged IS NULL AND created<?''', (now, self.domain_id, now-float(archive_days)*86400)).rowcount
                db.execute('''UPDATE source_revisions SET user_text=NULL,assistant_text=NULL,purged=?
                    WHERE purged IS NULL AND source_id IN
                      (SELECT id FROM sources WHERE domain=? AND purged IS NOT NULL)''', (now, self.domain_id))
        return {'expired': expired, 'purged_sources': purged}
