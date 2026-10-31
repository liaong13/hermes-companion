"""Explicitly approved, exact-group public statements with separable evidence.

This service does not establish host admission. The host must hold the confirmation
authority in trusted code; only proposal/read/withdraw operations belong in model
tools. Python code in the same process remains trusted, not an OS security domain.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets
from threading import RLock
import time
from types import MappingProxyType
import unicodedata
import uuid

from .scope import ScopeContext
from .store import terms


@dataclass(frozen=True)
class ConfirmationEvidence:
    """Verified explicit consent from a host-authenticated message.

    ``independent_content`` is optional and must repeat the complete public text
    verbatim in this person's own evidence. A bare 'yes' may approve a statement,
    but cannot reconstruct it after its original contributor is forgotten.
    """

    scope: ScopeContext
    source_id: str
    quote: str
    start: int
    independent_content: str | None = None


@dataclass(frozen=True, slots=True)
class _ConfirmationGrant:
    _authority: object
    _nonce: str


class ConfirmationAuthority:
    """Instance-bound capability issuer, never exposed to the model.

    verify_event(event, descriptor) must verify authenticated identity and an
    explicit decision about the displayed descriptor. It must not infer consent
    from a tool call, nickname, role string, model content or untrusted metadata.
    No global authority or serialized grant is accepted.
    """

    def __init__(self, *, verify_event, home, domain_id, ttl_seconds=60):
        if not callable(verify_event):
            raise PermissionError('trusted confirmation verifier required')
        if type(ttl_seconds) not in (int, float) or not 0 < ttl_seconds <= 300:
            raise ValueError('invalid confirmation lifetime')
        self.home, self.domain_id = Path(home).resolve(), domain_id
        self._verify_event, self._ttl = verify_event, ttl_seconds
        self._grants, self._lock = {}, RLock()

    def bound_to(self, store):
        return self.home == store.path.parent.parent.resolve() and self.domain_id == store.domain_id

    def issue(self, event, descriptor):
        evidence = self._verify_event(event, MappingProxyType(dict(descriptor)))
        if type(evidence) is not ConfirmationEvidence:
            raise PermissionError('authenticated explicit confirmation required')
        nonce = secrets.token_urlsafe(32)
        grant = _ConfirmationGrant(self, nonce)
        now = time.monotonic()
        with self._lock:
            self._grants = {k: v for k, v in self._grants.items() if v[0] > now}
            if len(self._grants) >= 256:
                raise PermissionError('too many outstanding confirmations')
            self._grants[nonce] = (now+self._ttl, grant, dict(descriptor), evidence)
        return grant

    def consume(self, grant, descriptor):
        if type(grant) is not _ConfirmationGrant or grant._authority is not self:
            raise PermissionError('foreign confirmation capability')
        with self._lock:
            pending = self._grants.pop(grant._nonce, None)
        if (pending is None or pending[0] <= time.monotonic() or pending[1] is not grant
                or pending[2] != descriptor):
            raise PermissionError('expired, replayed or stale confirmation')
        return pending[3]


class PublicMemory:
    def __init__(self, store, *, confirmation_authority=None):
        self.store = store
        if confirmation_authority is not None and (
                type(confirmation_authority) is not ConfirmationAuthority
                or not confirmation_authority.bound_to(store)):
            raise PermissionError('confirmation authority home/domain mismatch')
        self._authority = confirmation_authority
        with store.connection(True) as db:
            for statement in (
                '''CREATE TABLE IF NOT EXISTS public_facts (
                    id TEXT PRIMARY KEY, domain TEXT NOT NULL, origin TEXT NOT NULL,
                    topic_key TEXT NOT NULL, policy_revision INTEGER NOT NULL,
                    content TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
                    revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL)''',
                '''CREATE INDEX IF NOT EXISTS public_scope
                    ON public_facts(domain,origin,policy_revision,state,topic_key)''',
                '''CREATE TABLE IF NOT EXISTS public_context_versions (
                    domain TEXT NOT NULL, origin TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(domain,origin))''',
                '''CREATE TABLE IF NOT EXISTS public_contributions (
                    id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES public_facts(id) ON DELETE CASCADE,
                    domain TEXT NOT NULL, origin TEXT NOT NULL, principal TEXT NOT NULL,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    source_revision INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
                    quote TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
                    content TEXT NOT NULL, topic TEXT NOT NULL, participants TEXT NOT NULL,
                    kind TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
                    policy_revision INTEGER NOT NULL, created REAL NOT NULL,
                    UNIQUE(fact_id,principal,source_id,source_revision,kind))''',
                '''CREATE INDEX IF NOT EXISTS public_contributor
                    ON public_contributions(domain,origin,principal,source_id)''',
                '''CREATE TABLE IF NOT EXISTS public_terms (
                    fact_id TEXT NOT NULL REFERENCES public_facts(id) ON DELETE CASCADE,
                    term TEXT NOT NULL, PRIMARY KEY(fact_id,term))''',
                '''CREATE TRIGGER IF NOT EXISTS public_contribution_deleted
                    AFTER DELETE ON public_contributions BEGIN
                      UPDATE public_facts SET content='',state='paused',revision=revision+1
                        WHERE id=OLD.fact_id;
                      DELETE FROM public_terms WHERE fact_id=OLD.fact_id;
                    END''',
                '''CREATE TRIGGER IF NOT EXISTS public_source_changed
                    AFTER UPDATE OF revision,purged ON sources
                    WHEN NEW.revision != OLD.revision OR (NEW.purged IS NOT NULL AND OLD.purged IS NULL)
                    BEGIN DELETE FROM public_contributions WHERE source_id=OLD.id; END''',
                '''CREATE TRIGGER IF NOT EXISTS public_policy_changed
                    AFTER UPDATE OF policy_revision ON scope_versions
                    WHEN NEW.policy_revision != OLD.policy_revision
                    BEGIN DELETE FROM public_contributions
                      WHERE domain=NEW.domain AND origin=NEW.origin AND principal=NEW.owner; END''',
                '''CREATE TRIGGER IF NOT EXISTS public_context_inserted
                    AFTER INSERT ON public_facts BEGIN
                      INSERT INTO public_context_versions VALUES (NEW.domain,NEW.origin,1)
                        ON CONFLICT(domain,origin) DO UPDATE SET revision=revision+1;
                    END''',
                '''CREATE TRIGGER IF NOT EXISTS public_context_changed
                    AFTER UPDATE OF content,state,revision ON public_facts BEGIN
                      INSERT INTO public_context_versions VALUES (NEW.domain,NEW.origin,1)
                        ON CONFLICT(domain,origin) DO UPDATE SET revision=revision+1;
                    END''',
                '''CREATE TRIGGER IF NOT EXISTS public_context_deleted
                    AFTER DELETE ON public_facts BEGIN
                      INSERT INTO public_context_versions VALUES (OLD.domain,OLD.origin,1)
                        ON CONFLICT(domain,origin) DO UPDATE SET revision=revision+1;
                    END''',
            ):
                db.execute(statement)

    def context_revision(self, scope):
        """Shared namespace revision for every participant's host conversation.

        Include the independently retained deletion watermark, not just a counter
        from a restorable content snapshot. Otherwise replaying a deletion could
        collide with a namespace used before that deletion. Hosts must check this
        value before using context, and rotate/rebuild all derived conversation
        state when it changes; it cannot retract replies already sent.
        """
        with self.store.connection() as db:
            self._scope(db, scope)
            row = db.execute('SELECT revision FROM public_context_versions WHERE domain=? AND origin=?',
                             (scope.domain_id, scope.origin)).fetchone()
            revision = row[0] if row else 0
            watermark = int(db.execute("SELECT value FROM store_meta WHERE key='deletion_watermark'").fetchone()[0])
        if type(revision) is not int or not 0 <= revision < 2**64:
            raise PermissionError('public context revision unavailable')
        return (watermark << 64) | revision

    def _scope(self, db, scope):
        if (type(scope) is not ScopeContext or scope.platform != 'napcat' or scope.origin_kind != 'group'
                or not re.fullmatch(r'group:[0-9]+', scope.origin_id)
                or not re.fullmatch(r'napcat:[0-9]+', scope.principal_id)):
            raise PermissionError('trusted exact group scope required')
        self.store.validate_scope(db, scope)

    @staticmethod
    def _text(value, name, limit):
        if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit:
            raise ValueError('invalid '+name)
        return value

    @staticmethod
    def _participants(values):
        values = [] if values is None else values
        if (not isinstance(values, list) or len(values) > 16
                or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in values)):
            raise ValueError('invalid participant labels')
        return json.dumps(values, ensure_ascii=False)

    def _evidence(self, db, scope, source_id, quote, start):
        self._scope(db, scope)
        if not isinstance(source_id, str) or not source_id:
            raise ValueError('invalid evidence source')
        source = self.store.owned(db, 'sources', source_id, scope)
        if (source['purged'] is not None or not isinstance(source['user_text'], str)
                or not isinstance(quote, str) or not quote or len(quote.encode()) > 8000
                or type(start) is not int or start < 0
                or source['user_text'][start:start+len(quote)] != quote):
            raise ValueError('public evidence must match current user-source characters')
        return source

    def propose(self, scope, content, source_id, quote, start, topic, participants=None):
        content = self._text(content, 'public statement', 4000)
        topic = self._text(topic, 'public topic', 512)
        labels = self._participants(participants)
        topic_key = hashlib.sha256(unicodedata.normalize('NFKC', topic).casefold().strip().encode()).hexdigest()
        ident, contribution = uuid.uuid4().hex, uuid.uuid4().hex
        with self.store.connection(True) as db:
            source = self._evidence(db, scope, source_id, quote, start)
            db.execute('''INSERT INTO public_facts(id,domain,origin,topic_key,policy_revision,created)
                          VALUES (?,?,?,?,?,?)''',
                       (ident, scope.domain_id, scope.origin, topic_key, scope.policy_revision, time.time()))
            self._insert_contribution(db, contribution, ident, scope, source, quote, start,
                                      content, topic, labels, 'assertion', approved=False)
        return {'id': ident, 'contribution_id': contribution, 'state': 'pending',
                'asserted_by': scope.principal_id, 'requires': 'authenticated_explicit_confirmation'}

    def _insert_contribution(self, db, ident, fact, scope, source, quote, start,
                             content, topic, participants, kind, approved):
        db.execute('''INSERT INTO public_contributions
            (id,fact_id,domain,origin,principal,source_id,source_revision,source_sha256,
             quote,start,end,content,topic,participants,kind,approved,policy_revision,created)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (ident, fact, scope.domain_id, scope.origin, scope.principal_id, source['id'], source['revision'],
             source['sha256'], quote, start, start+len(quote), content, topic, participants, kind,
             int(approved), scope.policy_revision, time.time()))

    def proposals(self, scope, limit=20):
        with self.store.connection() as db:
            self._scope(db, scope)
            rows = db.execute('''SELECT f.id,f.state,f.revision,c.id AS contribution_id,c.content,c.topic,
                       c.principal AS asserted_by,c.source_id,c.source_revision,c.quote,c.start,c.end
                FROM public_facts f JOIN public_contributions c ON c.fact_id=f.id
                WHERE f.domain=? AND f.origin=? AND f.policy_revision=?
                  AND c.principal=? AND c.kind='assertion' ORDER BY f.created DESC LIMIT ?''',
                (scope.domain_id, scope.origin, scope.policy_revision, scope.principal_id,
                 max(1, min(int(limit), 20)))).fetchall()
        return [dict(row, unit='characters') for row in rows]

    def _proposal(self, db, ident):
        row = db.execute('''SELECT f.*,c.content AS proposed_content,c.topic,c.id AS contribution_id,
                            c.principal AS asserted_by FROM public_facts f
                            JOIN public_contributions c ON c.fact_id=f.id AND c.kind='assertion'
                            WHERE f.id=? AND f.domain=?''', (ident, self.store.domain_id)).fetchone()
        if row is None:
            raise PermissionError('public proposal unavailable')
        return dict(row)

    @staticmethod
    def _descriptor(proposal, action='confirm'):
        return {'action': action, **{k: proposal[k] for k in ('id', 'domain', 'origin', 'revision', 'policy_revision',
                                                            'proposed_content', 'topic', 'asserted_by')}}

    def issue_confirmation(self, host_event, proposal_id):
        """Trusted host entry point; NEVER expose as a model tool."""
        if self._authority is None:
            raise PermissionError('host confirmation authority unavailable')
        with self.store.connection() as db:
            proposal = self._proposal(db, proposal_id)
            if proposal['state'] not in ('pending', 'active'):
                raise PermissionError('public proposal no longer confirmable')
        return self._authority.issue(host_event, self._descriptor(proposal))

    def confirmation_request(self, scope, proposal_id):
        """Render the author's explicit, revision-bound confirmation request."""
        with self.store.connection() as db:
            self._scope(db, scope)
            proposal = self._proposal(db, proposal_id)
            if (proposal['origin'] != scope.origin or proposal['policy_revision'] != scope.policy_revision
                    or proposal['asserted_by'] != scope.principal_id or proposal['state'] not in ('pending', 'active')):
                raise PermissionError('confirmation request unavailable in current ownership')
            descriptor = self._descriptor(proposal)
        digest = hashlib.sha256(json.dumps(descriptor, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        return {'id': proposal_id, 'content': descriptor['proposed_content'], 'revision': descriptor['revision'],
                'confirm_command': f'/memory-confirm {proposal_id} {digest}'}

    def resolution_request(self, scope, proposal_id):
        """Trusted host preview; actual group authorization is verified on grant issue."""
        with self.store.connection() as db:
            self._scope(db, scope)
            proposal = self._proposal(db, proposal_id)
            if (proposal['origin'] != scope.origin or proposal['policy_revision'] != scope.policy_revision
                    or proposal['state'] != 'conflict'):
                raise PermissionError('conflict resolution unavailable in current scope')
            descriptor = self._descriptor(proposal, 'resolve_conflict')
        digest = hashlib.sha256(json.dumps(descriptor, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        return {'id': proposal_id, 'content': descriptor['proposed_content'], 'revision': descriptor['revision'],
                'resolve_command': f'/memory-resolve {proposal_id} {digest}'}

    def confirm(self, grant, proposal_id):
        """Consume a host-issued capability and store its separate evidence."""
        if self._authority is None:
            raise PermissionError('host confirmation authority unavailable')
        with self.store.connection(True) as db:
            proposal = self._proposal(db, proposal_id)
            evidence = self._authority.consume(grant, self._descriptor(proposal))
            scope = evidence.scope
            source = self._evidence(db, scope, evidence.source_id, evidence.quote, evidence.start)
            if (scope.domain_id != proposal['domain'] or scope.origin != proposal['origin']
                    or scope.policy_revision != proposal['policy_revision']
                    or proposal['state'] not in ('pending', 'active')):
                raise PermissionError('confirmation crosses scope or revision')
            independent = evidence.independent_content
            if independent is not None and (independent != proposal['proposed_content'] or independent != evidence.quote):
                raise ValueError('independent confirmation must repeat the exact complete statement')
            self._insert_contribution(db, uuid.uuid4().hex, proposal_id, scope, source, evidence.quote,
                                      evidence.start, independent or '', '', '[]', 'confirmation', approved=True)
            # Explicit confirmation authorizes this displayed statement, not its
            # participants as an ACL and not another person's private profile.
            db.execute('UPDATE public_contributions SET approved=1 WHERE id=?', (proposal['contribution_id'],))
            conflicts = db.execute('''SELECT id FROM public_facts WHERE domain=? AND origin=?
                AND policy_revision=? AND topic_key=? AND id!=? AND state IN ('active','conflict')
                AND content!=?''', (scope.domain_id, scope.origin, scope.policy_revision,
                                   proposal['topic_key'], proposal_id, proposal['proposed_content'])).fetchall()
            if conflicts:
                ids = [proposal_id]+[row[0] for row in conflicts]
                for ident in ids:
                    db.execute("UPDATE public_facts SET state='conflict',revision=revision+1 WHERE id=?", (ident,))
                    db.execute('DELETE FROM public_terms WHERE fact_id=?', (ident,))
                # Keep each contributor's separate proposal/evidence for review.
                return {'id': proposal_id, 'state': 'conflict', 'requires': 'explicit_conflict_resolution'}
            self._publish(db, proposal_id, proposal['proposed_content'])
        return {'id': proposal_id, 'state': 'active'}

    def issue_resolution(self, host_event, proposal_id):
        """Host policy must authorize changing the group's conflicting decision."""
        if self._authority is None:
            raise PermissionError('host confirmation authority unavailable')
        with self.store.connection() as db:
            proposal = self._proposal(db, proposal_id)
            if proposal['state'] != 'conflict':
                raise PermissionError('public proposal is not conflicted')
        return self._authority.issue(host_event, self._descriptor(proposal, 'resolve_conflict'))

    def resolve_conflict(self, grant, proposal_id):
        """Apply an explicit host-authorized group decision; never a model tool."""
        if self._authority is None:
            raise PermissionError('host confirmation authority unavailable')
        with self.store.connection(True) as db:
            proposal = self._proposal(db, proposal_id)
            evidence = self._authority.consume(grant, self._descriptor(proposal, 'resolve_conflict'))
            scope = evidence.scope
            source = self._evidence(db, scope, evidence.source_id, evidence.quote, evidence.start)
            if (scope.domain_id != proposal['domain'] or scope.origin != proposal['origin']
                    or scope.policy_revision != proposal['policy_revision'] or proposal['state'] != 'conflict'):
                raise PermissionError('resolution crosses scope or revision')
            independent = evidence.independent_content
            if independent is not None and (independent != proposal['proposed_content'] or independent != evidence.quote):
                raise ValueError('independent resolution must repeat the exact complete statement')
            self._insert_contribution(db, uuid.uuid4().hex, proposal_id, scope, source, evidence.quote,
                                      evidence.start, independent or '', '', '[]', 'resolution', approved=True)
            others = db.execute('''SELECT id FROM public_facts WHERE domain=? AND origin=?
                AND policy_revision=? AND topic_key=? AND id!=? AND state IN ('active','conflict')''',
                (scope.domain_id, scope.origin, scope.policy_revision, proposal['topic_key'], proposal_id)).fetchall()
            for row in others:
                db.execute("UPDATE public_facts SET state='superseded',revision=revision+1 WHERE id=?", (row['id'],))
                db.execute('DELETE FROM public_terms WHERE fact_id=?', (row['id'],))
            db.execute('UPDATE public_contributions SET approved=1 WHERE id=?', (proposal['contribution_id'],))
            self._publish(db, proposal_id, proposal['proposed_content'])
        return {'id': proposal_id, 'state': 'active', 'resolved_conflicts': len(others)}

    @staticmethod
    def _publish(db, ident, content):
        db.execute("UPDATE public_facts SET content=?,state='active',revision=revision+1 WHERE id=?", (content, ident))
        db.execute('DELETE FROM public_terms WHERE fact_id=?', (ident,))
        db.executemany('INSERT INTO public_terms VALUES (?,?)', [(ident, term) for term in terms(content)])

    def _current_contributions(self, db, fact):
        return db.execute('''SELECT c.* FROM public_contributions c JOIN sources s ON s.id=c.source_id
            WHERE c.fact_id=? AND c.domain=? AND c.origin=? AND c.policy_revision=? AND c.approved=1
              AND s.domain=c.domain AND s.owner=c.principal AND s.origin=c.origin
              AND s.revision=c.source_revision AND s.sha256=c.source_sha256 AND s.purged IS NULL''',
            (fact['id'], fact['domain'], fact['origin'], fact['policy_revision'])).fetchall()

    @staticmethod
    def _approved_text(contributors, content):
        # Proposing is not explicit public consent. At least one still-current
        # host-authenticated confirmation must survive, including a contributor's
        # independent repetition when the original assertion was removed.
        return (any(c['kind'] in ('confirmation', 'resolution') for c in contributors)
                and any(c['content'] == content for c in contributors))

    def rebuild(self, scope, fact_id):
        """Rebuild only from surviving, independently approved contributor text."""
        with self.store.connection(True) as db:
            self._scope(db, scope)
            fact = db.execute('''SELECT * FROM public_facts WHERE id=? AND domain=? AND origin=?
                AND policy_revision=?''', (fact_id, scope.domain_id, scope.origin, scope.policy_revision)).fetchone()
            if fact is None:
                raise PermissionError('public fact unavailable')
            if fact['state'] == 'conflict':
                raise PermissionError('conflicts require new explicit resolution')
            current = self._current_contributions(db, fact)
            independent = [dict(row) for row in current if row['content']]
            contents = {row['content'] for row in independent}
            if len(contents) != 1 or not self._approved_text(current, next(iter(contents), '')):
                return {'id': fact_id, 'state': fact['state'], 'requires': 'independent_evidence_or_confirmation'}
            content = next(iter(contents))
            other = db.execute('''SELECT 1 FROM public_facts WHERE domain=? AND origin=? AND policy_revision=?
                AND topic_key=? AND state IN ('active','conflict') AND id!=? AND content!=?''',
                (scope.domain_id, scope.origin, scope.policy_revision, fact['topic_key'], fact_id, content)).fetchone()
            if other:
                raise PermissionError('public fact remains conflicted')
            self._publish(db, fact_id, content)
        return {'id': fact_id, 'state': 'active', 'rebuilt_from_independent_contributions': len(independent)}

    def lookup(self, scope, query, limit=8):
        query_terms = terms(self._text(query, 'public query', 4000))[:64]
        if not query_terms:
            return []
        marks = ','.join('?' for _ in query_terms)
        with self.store.connection() as db:
            self._scope(db, scope)
            rows = db.execute(f'''SELECT f.*,COUNT(*) AS score FROM public_facts f
                JOIN public_terms t ON t.fact_id=f.id WHERE f.domain=? AND f.origin=?
                AND f.policy_revision=? AND f.state='active' AND t.term IN ({marks})
                GROUP BY f.id ORDER BY score DESC,f.created DESC LIMIT ?''',
                (scope.domain_id, scope.origin, scope.policy_revision, *query_terms,
                 max(1, min(int(limit), 20)))).fetchall()
            result = []
            for row in rows:
                contributors = self._current_contributions(db, row)
                if not self._approved_text(contributors, row['content']):
                    continue
                result.append({'id': row['id'], 'content': row['content'], 'revision': row['revision'],
                               'state': 'active', 'visibility': 'exact_group_thread',
                               'asserted_by': sorted({c['principal'] for c in contributors if c['content'] == row['content']})})
        return result

    def validate_results(self, scope, rows):
        """Revalidate a pending injection after asynchronous work or deletion."""
        with self.store.connection() as db:
            self._scope(db, scope)
            for row in rows:
                fact = db.execute('''SELECT * FROM public_facts WHERE id=? AND domain=? AND origin=?
                    AND policy_revision=? AND state='active' AND revision=? AND content=?''',
                    (row['id'], scope.domain_id, scope.origin, scope.policy_revision, row['revision'], row['content'])).fetchone()
                if fact is None:
                    raise PermissionError('public result revoked or changed')
                if not self._approved_text(self._current_contributions(db, fact), fact['content']):
                    raise PermissionError('public evidence revoked')
        return rows

    def withdraw(self, scope, contribution_id):
        """Retract one's source through the independent, replayable deletion ledger.

        A source is one contributor's completed turn. Retraction clears that
        entire source and its dependent local/public data, never another person's
        independent source. It deliberately does not make a false slice-erasure
        promise for a shared source that cannot be safely separated.
        """
        with self.store.connection() as db:
            self._scope(db, scope)
            row = db.execute('''SELECT source_id FROM public_contributions
                WHERE id=? AND domain=? AND origin=? AND principal=?''',
                (contribution_id, scope.domain_id, scope.origin, scope.principal_id)).fetchone()
            if row is None:
                raise PermissionError('contribution unavailable in current ownership')
            source_id = row['source_id']
        result = self.store.forget(scope, source_ids=[source_id])
        result.update({'withdrawn': contribution_id, 'cleared': 'own_source_and_dependents',
                       'public_state': 'paused_until_independent_rebuild',
                       'host_context_revocation': 'requires_host_session_rebuild'})
        return result
