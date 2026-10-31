"""Evidence-backed person metadata over the existing Store implementation.

Only trusted ingress constructs scopes. A fact's subject is its authenticated
speaker; confirming it grants use in that exact origin, never across venues.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import time

from .scope import ScopeContext

FIELDS = {'interest': '兴趣', 'preference': '交流偏好', 'boundary': '联系边界'}


@dataclass(frozen=True)
class PersonView:
    facts: tuple
    _check: object

    def current(self):
        try:
            self._check()
            return True
        except (PermissionError, ValueError):
            return False


class PeopleMemory:
    contract_version = 1

    def __init__(self, store, instance_id):
        if not isinstance(instance_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', instance_id) is None:
            raise ValueError('explicit instance ID required')
        self.store, self.instance_id = store, instance_id
        with store.connection(True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS person_facts (
                memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                instance_id TEXT NOT NULL, subject_id TEXT NOT NULL, asserted_by TEXT NOT NULL,
                field TEXT NOT NULL, visibility TEXT NOT NULL CHECK(visibility='current_origin'),
                UNIQUE(instance_id,memory_id))''')

    def _scope(self, scope):
        peer = scope.principal_id.partition(':')[2] if isinstance(scope, ScopeContext) else ''
        origin_allowed = (isinstance(scope, ScopeContext) and (
            scope.origin_kind == 'group' and re.fullmatch(r'group:[1-9][0-9]{0,19}', scope.origin_id) is not None
            or scope.origin_kind == 'dm' and scope.origin_id == 'dm:'+peer))
        if (not isinstance(scope, ScopeContext) or scope.platform != 'napcat'
                or not origin_allowed or scope.thread_id
                or re.fullmatch(r'napcat:[1-9][0-9]{0,19}', scope.principal_id) is None
                ):
            raise PermissionError('authenticated exact-origin member scope required')
        self.store.snapshot(scope)

    def propose(self, scope, source_id, field, value, *, start, ttl_days=None):
        self._scope(scope)
        if (field not in FIELDS or not isinstance(value, str) or not value.strip()
                or len(value.encode()) > 480 or type(start) is not int or start < 0):
            raise ValueError('invalid person fact')
        with self.store.connection(True) as db:
            source = self.store.owned(db, 'sources', source_id, scope)
            if (source['purged'] is not None or source['user_text'] is None
                    or source['user_text'][start:start+len(value)] != value):
                raise PermissionError('person fact requires current original user evidence')
            old = db.execute('''SELECT p.memory_id FROM person_facts p JOIN memories m ON m.id=p.memory_id
                JOIN evidence e ON e.memory_id=m.id
                WHERE p.instance_id=? AND m.domain=? AND m.owner=? AND m.origin=?
                  AND m.source_id=? AND p.field=? AND e.source_revision=?
                  AND m.state IN ('pending','active','rejected','superseded','archived','deleted')''',
                (self.instance_id, self.store.domain_id, *scope.pair, source_id, field, source['revision'])).fetchone()
            if old is not None:
                return old[0]
            ident = self.store._add(db, scope, FIELDS[field]+'：'+value, source_id,
                                    {'person': [scope.principal_id]}, ttl_days=ttl_days, state='pending')
            db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)',
                       (ident, source_id, source['revision'], 'user', start, start+len(value),
                        'characters', source['sha256']))
            db.execute('INSERT INTO person_facts VALUES (?,?,?,?,?,?)',
                       (ident, self.instance_id, scope.principal_id, scope.principal_id, field, 'current_origin'))
        return ident

    def _owned(self, db, scope, ident):
        memory = self.store.owned(db, 'memories', ident, scope)
        row = db.execute('''SELECT * FROM person_facts WHERE memory_id=? AND instance_id=?
            AND subject_id=? AND asserted_by=? AND visibility='current_origin' ''',
            (ident, self.instance_id, scope.principal_id, scope.principal_id)).fetchone()
        if row is None:
            raise PermissionError('person fact not found in scope')
        return memory, dict(row)

    def _review(self, db, scope, ident):
        _, person = self._owned(db, scope, ident)
        row, base_digest = self.store._candidate(db, scope, ident)
        digest = hashlib.sha256(json.dumps([base_digest, person], sort_keys=True).encode()).hexdigest()[:24]
        return {'id': ident, 'field': person['field'], 'content': row['content'], 'digest': digest}

    def review(self, scope, ident):
        self._scope(scope)
        with self.store.connection(True) as db:
            return self._review(db, scope, ident)

    def decide(self, scope, ident, digest, *, confirm):
        self._scope(scope)
        if type(confirm) is not bool or not isinstance(digest, str):
            raise ValueError('invalid person decision')
        with self.store.connection(True) as db:
            card = self._review(db, scope, ident)
            if card['digest'] != digest:
                raise PermissionError('person preview changed or expired')
            if confirm:
                # Replacement is atomic and limited to this member, instance,
                # origin and field. A third person's statement never supersedes it.
                prior = db.execute('''SELECT m.id,m.revision FROM memories m
                    JOIN person_facts p ON p.memory_id=m.id
                    WHERE m.domain=? AND m.owner=? AND m.origin=? AND m.state='active'
                      AND p.instance_id=? AND p.subject_id=? AND p.field=?
                    ORDER BY m.revision DESC,m.created DESC LIMIT 1''',
                    (self.store.domain_id, *scope.pair, self.instance_id, scope.principal_id, card['field'])).fetchone()
                db.execute('''UPDATE memories SET state='superseded' WHERE domain=? AND owner=? AND origin=?
                    AND state='active' AND id IN (SELECT memory_id FROM person_facts
                        WHERE instance_id=? AND subject_id=? AND field=?)''',
                    (self.store.domain_id, *scope.pair, self.instance_id, scope.principal_id, card['field']))
                if prior is not None:
                    db.execute('UPDATE memories SET supersedes=?,revision=? WHERE id=?',
                               (prior['id'], prior['revision']+1, ident))
            db.execute('UPDATE memories SET state=? WHERE id=?', ('active' if confirm else 'rejected', ident))
        return card

    def list(self, scope, *, pending=False):
        self._scope(scope)
        with self.store.connection() as db:
            self.store.validate_scope(db, scope)
            rows = db.execute('''SELECT m.id,m.content,p.field FROM memories m
                JOIN person_facts p ON p.memory_id=m.id
                WHERE m.domain=? AND m.owner=? AND m.origin=? AND p.instance_id=?
                  AND p.subject_id=? AND p.asserted_by=? AND p.visibility='current_origin'
                  AND m.state=? AND (m.expires IS NULL OR m.expires>?)
                  AND EXISTS(SELECT 1 FROM evidence e JOIN sources s ON s.id=e.source_id
                      WHERE e.memory_id=m.id AND s.purged IS NULL AND s.revision=e.source_revision
                        AND s.sha256=e.sha256)
                ORDER BY m.created DESC,m.id LIMIT 8''',
                (self.store.domain_id, *scope.pair, self.instance_id, scope.principal_id,
                 scope.principal_id, 'pending' if pending else 'active', time.time())).fetchall()
            return [dict(row) for row in rows]

    def recall(self, scope):
        rows = self.list(scope)
        facts = tuple({'id': r['id'], 'field': r['field'], 'content': r['content']} for r in rows)
        def check():
            self._scope(scope)
            current = {r['id']: r for r in self.list(scope)}
            if any(current.get(r['id']) != r for r in facts):
                raise PermissionError('person context changed')
        return PersonView(facts, check)

    def forget(self, scope, ident):
        self._scope(scope)
        with self.store.connection() as db:
            self._owned(db, scope, ident)
        # Store fences async work and deletes its source/evidence/indices; FK
        # cascade also removes person metadata during live and restored reads.
        return self.store.forget(scope, memory_ids=[ident])
