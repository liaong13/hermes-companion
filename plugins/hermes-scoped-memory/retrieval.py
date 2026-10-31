"""Permission-filtered exact vectors and an explicitly injected embedding transport.

No optional vector extension, model SDK, credential discovery, or network calls at
import time. SQLite facts remain authoritative. HTTP retries belong to the durable
job runner; each call here performs at most one request and reserves its own cost.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
import time
import uuid
from urllib.request import HTTPRedirectHandler, Request, build_opener


class EmbeddingError(RuntimeError):
    """A static error code; response bodies and credentials never enter errors."""


def unit_vector(value, dimensions):
    if (not isinstance(value, (list, tuple)) or len(value) != dimensions
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
        raise ValueError('invalid_vector')
    norm = math.hypot(*value)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError('invalid_vector')
    return [v / norm for v in value]


@dataclass(frozen=True)
class Fingerprint:
    model: str
    provider: str
    dimensions: int
    model_revision: str = 'unversioned'
    preprocess_version: str = 'summary-v1'
    dtype: str = 'float64'
    normalize: str = 'l2'
    distance: str = 'cosine'

    def __post_init__(self):
        if (type(self.dimensions) is not int or not 1 <= self.dimensions <= 16384
                or any(not isinstance(v, str) or not v or len(v) > 256
                       for v in (self.model, self.provider, self.model_revision, self.preprocess_version))
                or (self.dtype, self.normalize, self.distance) != ('float64', 'l2', 'cosine')):
            raise ValueError('invalid_fingerprint')

    def encode(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))


class VectorIndex:
    """Exact cosine over SQL-authorized rows, never global Top-K then filtering."""

    def __init__(self, store):
        self.store = store
        with store.connection(True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS retrieval_generations (
                    domain TEXT NOT NULL, generation TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'shadow',
                    created REAL NOT NULL, PRIMARY KEY(domain,generation));
                CREATE UNIQUE INDEX IF NOT EXISTS retrieval_one_active
                    ON retrieval_generations(domain) WHERE state='active';
                CREATE TABLE IF NOT EXISTS retrieval_vectors (
                    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL, domain TEXT NOT NULL,
                    generation TEXT NOT NULL, content_hash TEXT NOT NULL, vector TEXT NOT NULL,
                    PRIMARY KEY(memory_id,revision,domain,generation),
                    FOREIGN KEY(domain,generation)
                        REFERENCES retrieval_generations(domain,generation));
            ''')

    def create_generation(self, generation, fingerprint):
        if not isinstance(generation, str) or not generation or len(generation) > 128:
            raise ValueError('invalid_generation')
        if not isinstance(fingerprint, Fingerprint):
            raise ValueError('invalid_fingerprint')
        with self.store.connection(True) as db:
            row = db.execute('''SELECT fingerprint FROM retrieval_generations
                WHERE domain=? AND generation=?''', (self.store.domain_id, generation)).fetchone()
            if row and row['fingerprint'] != fingerprint.encode():
                raise ValueError('generation_fingerprint_conflict')
            db.execute('''INSERT OR IGNORE INTO retrieval_generations
                (domain,generation,fingerprint,created) VALUES (?,?,?,?)''',
                (self.store.domain_id, generation, fingerprint.encode(), time.time()))
        return generation

    def _generation(self, db, generation=None, active=False):
        if generation is None:
            row = db.execute('''SELECT * FROM retrieval_generations
                WHERE domain=? AND state='active' ''', (self.store.domain_id,)).fetchone()
        else:
            row = db.execute('''SELECT * FROM retrieval_generations
                WHERE domain=? AND generation=?''', (self.store.domain_id, generation)).fetchone()
        if not row or row['state'] == 'quarantined' or (active and row['state'] != 'active'):
            raise ValueError('generation_unavailable')
        return dict(row), Fingerprint(**json.loads(row['fingerprint']))

    def put(self, scope, memory_id, generation, vector, revision=None, *, vector_fingerprint=None):
        with self.store.connection(True) as db:
            self.store.validate_scope(db, scope)
            row = self.store.owned(db, 'memories', memory_id, scope)
            if (row['state'] != 'active' or (row['expires'] is not None and row['expires'] <= time.time())
                    or (revision is not None and row['revision'] != revision)):
                raise ValueError('stale_fact')
            _, fingerprint = self._generation(db, generation)
            if not isinstance(vector_fingerprint, Fingerprint) or vector_fingerprint != fingerprint:
                raise ValueError('vector_fingerprint_mismatch')
            normalized = unit_vector(vector, fingerprint.dimensions)
            db.execute('''INSERT OR REPLACE INTO retrieval_vectors
                VALUES (?,?,?,?,?,?)''', (memory_id, row['revision'], self.store.domain_id, generation,
                                        hashlib.sha256(row['content'].encode()).hexdigest(), json.dumps(normalized)))

    def activate(self, generation, expected_count=None):
        """Maintainer operation: validate full current coverage, then switch atomically."""
        with self.store.connection(True) as db:
            row, fingerprint = self._generation(db, generation)
            memories = db.execute('''SELECT m.id,m.content,m.revision,v.vector,v.content_hash
                FROM memories m LEFT JOIN retrieval_vectors v ON v.memory_id=m.id
                    AND v.revision=m.revision AND v.domain=m.domain AND v.generation=?
                WHERE m.domain=? AND m.state='active' AND (m.expires IS NULL OR m.expires>?)''',
                (generation, self.store.domain_id, time.time())).fetchall()
            if expected_count is not None and (type(expected_count) is not int or len(memories) != expected_count):
                raise ValueError('generation_count_mismatch')
            for memory in memories:
                if memory['vector'] is None or memory['content_hash'] != hashlib.sha256(memory['content'].encode()).hexdigest():
                    raise ValueError('generation_incomplete')
                unit_vector(json.loads(memory['vector']), fingerprint.dimensions)
            db.execute("UPDATE retrieval_generations SET state='shadow' WHERE domain=? AND state='active'",
                       (self.store.domain_id,))
            db.execute("UPDATE retrieval_generations SET state='active' WHERE domain=? AND generation=?",
                       (self.store.domain_id, row['generation']))
        return len(memories)

    def search(self, scope, query_vector, limit=8, generation=None, min_similarity=0.2,
               *, query_fingerprint=None):
        if type(min_similarity) not in (int, float) or not -1 <= min_similarity <= 1:
            raise ValueError('invalid_similarity_threshold')
        scope = self.store.snapshot(scope)
        limit = max(1, min(int(limit), 20))
        with self.store.connection() as db:
            self.store.validate_scope(db, scope)
            owner, origin = self.store.scope_pair(scope)
            info, fingerprint = self._generation(db, generation, active=True)
            if not isinstance(query_fingerprint, Fingerprint) or query_fingerprint != fingerprint:
                raise ValueError('query_fingerprint_mismatch')
            query = unit_vector(query_vector, fingerprint.dimensions)
            # Authorize in SQL before touching vector bytes or computing distances.
            rows = db.execute('''SELECT m.*,v.vector,v.content_hash FROM memories m
                JOIN retrieval_vectors v ON m.id=v.memory_id AND m.revision=v.revision
                WHERE m.domain=? AND m.owner=? AND m.origin=? AND m.state='active'
                    AND (m.expires IS NULL OR m.expires>?) AND v.domain=m.domain
                    AND v.generation=?''',
                (self.store.domain_id, owner, origin, time.time(), info['generation'])).fetchall()
            results = []
            try:
                for source in rows:
                    row = dict(source)
                    if row.pop('content_hash') != hashlib.sha256(row['content'].encode()).hexdigest():
                        raise ValueError('stale_vector')
                    vector = unit_vector(json.loads(row.pop('vector')), fingerprint.dimensions)
                    similarity = math.fsum(a*b for a, b in zip(query, vector))
                    if similarity >= min_similarity:
                        row['vector_score'] = similarity
                        results.append(row)
            except (ValueError, TypeError, OverflowError):
                # Quarantine after closing this read transaction below.
                results = None
        if results is None:
            with self.store.connection(True) as db:
                db.execute("UPDATE retrieval_generations SET state='quarantined' WHERE domain=? AND generation=?",
                           (self.store.domain_id, info['generation']))
            raise ValueError('generation_corrupt')
        with self.store.connection() as db:
            self.store.validate_scope(db, scope)
        return sorted(results, key=lambda row: (-row['vector_score'], row['id']))[:limit]


class HybridRetriever:
    def __init__(self, store, index):
        self.store, self.index = store, index
        self.last_status = 'lexical'

    def search(self, scope, query, query_vector=None, generation=None, limit=8, *, query_fingerprint=None):
        scope = self.store.snapshot(scope)
        limit = max(1, min(int(limit), 20))
        lexical = self.store.search(scope, query, 20)
        vector = []
        self.last_status = 'lexical'
        if query_vector is not None:
            try:
                vector = self.index.search(scope, query_vector, 20, generation,
                                           query_fingerprint=query_fingerprint)
                self.last_status = 'hybrid'
            except (ValueError, TypeError, OverflowError):
                self.last_status = 'degraded'
        scores, rows = {}, {}
        for ranked in (lexical, vector):
            for rank, row in enumerate(ranked, 1):
                rows[row['id']] = row
                scores[row['id']] = scores.get(row['id'], 0) + 1 / (60 + rank)
        ranked_ids = sorted(scores, key=lambda ident: (-scores[ident], ident))
        result = []
        # Re-read just before returning to exclude concurrently archived/deleted facts.
        for ident in ranked_ids:
            try:
                row = self.store.get(scope, ident)
            except PermissionError:
                continue
            if row['state'] == 'active' and (row['expires'] is None or row['expires'] > time.time()):
                row['rrf_score'] = scores[ident]
                result.append(row)
                if len(result) == limit:
                    break
        with self.store.connection() as db:
            self.store.validate_scope(db, scope)
        return result


def _money(value):
    value = Decimal(str(value))
    if not value.is_finite() or value <= 0:
        raise ValueError('invalid_budget')
    return value


class BudgetLedger:
    """Independent charge ledger with a separately retained recovery checkpoint.

    Charges never release reserved credit. Content backups cannot roll billing
    back. A checkpoint outside the profile is required before actual egress;
    UUID, sequence and hash-chain validation reject a missing/replaced/older
    ledger. Rolling back the ledger *and* its external checkpoint is outside this
    local proof and must be prohibited by the deployment backup policy.
    """
    def __init__(self, store, *, daily_usd, monthly_usd, scope_daily_usd=None, checkpoint_path=None):
        self.store = store
        self.daily = _money(daily_usd)
        self.monthly = _money(monthly_usd)
        self.scope_daily = _money(scope_daily_usd) if scope_daily_usd is not None else self.daily
        home = store.path.parent.parent.resolve()
        directory = home / 'scoped-memory-billing'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / 'ledger.sqlite3'
        self.lock_path = directory / 'ledger.lock'
        existed = self.path.exists()
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.lock_path, 0o600)
        self.checkpoint_path = Path(checkpoint_path).resolve() if checkpoint_path is not None else None
        if self.checkpoint_path is not None and self.checkpoint_path.is_relative_to(home):
            raise ValueError('billing_checkpoint_must_be_outside_profile')
        self._lock = Lock()
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('''CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), uuid TEXT NOT NULL)''')
            db.execute('INSERT OR IGNORE INTO identity VALUES (1,?)', (uuid.uuid4().hex,))
            db.execute('''CREATE TABLE IF NOT EXISTS embedding_charges (
                id TEXT PRIMARY KEY, domain TEXT NOT NULL, scope_hash TEXT NOT NULL,
                day TEXT NOT NULL, month TEXT NOT NULL, reserved_nano INTEGER NOT NULL,
                status TEXT NOT NULL, usage_tokens INTEGER)''')
            db.execute('''CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL)''')
        # Carry old conservative reservations forward, never refunding or
        # double-counting them. Their old content table is retained for audit.
        with store.connection(True) as db:
            old = (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embedding_charges'").fetchone())
            legacy = [dict(r) for r in db.execute('SELECT * FROM embedding_charges')] if old else []
        with sqlite3.connect(self.path, timeout=15) as db:
            for row in legacy:
                if db.execute('SELECT 1 FROM embedding_charges WHERE id=?', (row['id'],)).fetchone():
                    continue
                db.execute('INSERT INTO embedding_charges VALUES (?,?,?,?,?,?,?,?)', tuple(row[k] for k in
                           ('id', 'domain', 'scope_hash', 'day', 'month', 'reserved_nano', 'status', 'usage_tokens')))
                self._append_event(db, {'kind': 'reserve', **row})
        with self.connection(write=True, initialize=not existed):
            pass

    @staticmethod
    def _append_event(db, payload):
        previous = db.execute('SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        seq, before = (previous[0]+1, previous[1]) if previous else (1, '0'*64)
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256((str(seq)+'\n'+before+'\n'+encoded).encode()).hexdigest()
        db.execute('INSERT INTO events VALUES (?,?,?,?)', (seq, encoded, before, digest))

    def _verify(self, db):
        ledger_id = db.execute('SELECT uuid FROM identity WHERE id=1').fetchone()[0]
        seq, before, materialized = 0, '0'*64, {}
        hashes = {0: before}
        for row in db.execute('SELECT seq,payload,previous_hash,hash FROM events ORDER BY seq'):
            number, payload, parent, digest = row
            expected = hashlib.sha256((str(number)+'\n'+before+'\n'+payload).encode()).hexdigest()
            if number != seq+1 or parent != before or digest != expected:
                raise EmbeddingError('billing_ledger_corrupt')
            event = json.loads(payload)
            if event['kind'] == 'reserve':
                materialized[event['id']] = {k: event[k] for k in
                    ('id', 'domain', 'scope_hash', 'day', 'month', 'reserved_nano', 'status', 'usage_tokens')}
            elif event['kind'] == 'finish' and event['id'] in materialized:
                materialized[event['id']].update(status=event['status'], usage_tokens=event['usage_tokens'])
            else:
                raise EmbeddingError('billing_ledger_corrupt')
            seq, before = number, digest
            hashes[seq] = digest
        rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM embedding_charges')}
        if rows != materialized:
            raise EmbeddingError('billing_ledger_corrupt')
        point = {'ledger_id': ledger_id, 'sequence': seq, 'hash': before}
        with self.store.connection() as content:
            row = content.execute("SELECT value FROM store_meta WHERE key='billing_checkpoint'").fetchone()
        if row is not None:
            self._check_checkpoint(json.loads(row[0]), point, hashes)
        if self.checkpoint_path is not None and self.checkpoint_path.exists():
            self._check_checkpoint(json.loads(self.checkpoint_path.read_text()), point, hashes)
        return point

    @staticmethod
    def _check_checkpoint(checkpoint, point, hashes):
        if (not isinstance(checkpoint, dict) or set(checkpoint) != {'ledger_id', 'sequence', 'hash'}
                or checkpoint['ledger_id'] != point['ledger_id']
                or type(checkpoint['sequence']) is not int
                or hashes.get(checkpoint['sequence']) != checkpoint['hash']):
            raise EmbeddingError('billing_ledger_rollback_or_replacement')

    def _publish(self, point):
        encoded = json.dumps(point, sort_keys=True)
        if self.checkpoint_path is not None:
            parent = self.checkpoint_path.parent
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = parent / ('.billing-'+uuid.uuid4().hex)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'w') as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.checkpoint_path)
                descriptor = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if temporary.exists():
                    temporary.unlink()
        with self.store.connection(True) as db:
            db.execute("INSERT OR REPLACE INTO store_meta VALUES ('billing_checkpoint',?)", (encoded,))

    @contextmanager
    def connection(self, write=False, *, initialize=False):
        with self._lock:
            lock_file = self.lock_path.open('r+')
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            try:
                db.execute('BEGIN IMMEDIATE')
                point = self._verify(db)
                if self.checkpoint_path is not None and not self.checkpoint_path.exists():
                    if not initialize or point['sequence']:
                        raise EmbeddingError('billing_recovery_checkpoint_missing')
                yield db
                point = self._verify(db)
                db.commit()
                self._publish(point)
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()

    def assert_recovery_proven(self):
        if self.checkpoint_path is None or not self.checkpoint_path.is_file():
            raise EmbeddingError('embedding_policy_blocked')
        with self.connection():
            pass

    def reserve(self, scope, amount):
        amount = _money(amount)
        nano = int((amount * 10**9).to_integral_value(rounding=ROUND_CEILING))
        day, month = time.strftime('%Y-%m-%d', time.gmtime()), time.strftime('%Y-%m', time.gmtime())
        scope = self.store.snapshot(scope)
        with self.connection(write=True) as db:
            scope_hash = hashlib.sha256(json.dumps((self.store.domain_id, *self.store.scope_pair(scope))).encode()).hexdigest()
            for predicate, values, ceiling in (
                    ('day=?', (day,), self.daily), ('month=?', (month,), self.monthly),
                    ('day=? AND scope_hash=?', (day, scope_hash), self.scope_daily)):
                total = db.execute(f'''SELECT COALESCE(SUM(reserved_nano),0) FROM embedding_charges
                    WHERE domain=? AND {predicate}''', (self.store.domain_id, *values)).fetchone()[0]
                if Decimal(total + nano) > ceiling * 10**9:
                    raise EmbeddingError('embedding_budget_exhausted')
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO embedding_charges VALUES (?,?,?,?,?,?,?,NULL)',
                       (ident, self.store.domain_id, scope_hash, day, month, nano, 'reserved'))
            self._append_event(db, dict(kind='reserve', id=ident, domain=self.store.domain_id,
                scope_hash=scope_hash, day=day, month=month, reserved_nano=nano, status='reserved', usage_tokens=None))
        return ident

    def finish(self, reservation, *, status, usage_tokens=None):
        if status not in ('success', 'unknown', 'failed'):
            raise ValueError('invalid_charge_status')
        if usage_tokens is not None and (type(usage_tokens) is not int or usage_tokens < 0):
            raise ValueError('invalid_usage')
        with self.connection(write=True) as db:
            if not db.execute('SELECT 1 FROM embedding_charges WHERE id=? AND domain=?',
                              (reservation, self.store.domain_id)).fetchone():
                raise EmbeddingError('unknown_billing_reservation')
            db.execute('''UPDATE embedding_charges SET status=?,usage_tokens=?
                WHERE id=? AND domain=?''', (status, usage_tokens, reservation, self.store.domain_id))
            self._append_event(db, dict(kind='finish', id=reservation, status=status, usage_tokens=usage_tokens))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_request(payload, key, timeout):
    request = Request('https://openrouter.ai/api/v1/embeddings',
                      data=json.dumps(payload).encode(), method='POST',
                      headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise EmbeddingError('embedding_response_too_large')
        return json.loads(body)


class _DeadlineSlot:
    """One daemon transport call at a time; late results are never published.

    A caller deadline cannot safely interrupt a blocking resolver/socket in a
    portable way. The daemon may finish later, but the single occupied slot
    refuses further calls until it does. This bounds both latency and threads.
    """
    def __init__(self):
        self._lock = Lock()
        self._active = None

    def call(self, request, payload, key, timeout):
        started = time.monotonic()
        with self._lock:
            if self._active is not None and self._active.is_alive():
                raise EmbeddingError('embedding_transport_busy')
            ready, outcome = Event(), {}
            def run():
                try:
                    outcome['value'] = request(payload, key, timeout)
                except BaseException:
                    outcome['failed'] = True
                finally:
                    ready.set()
            thread = Thread(target=run, name='scoped-embedding-transport', daemon=True)
            self._active = thread
            thread.start()
        remaining = max(0, timeout - (time.monotonic()-started))
        if not ready.wait(remaining) or time.monotonic()-started > timeout:
            raise EmbeddingError('embedding_transport_deadline')
        if outcome.get('failed'):
            raise EmbeddingError('embedding_transport_failed')
        return outcome['value']


class OpenRouterEmbeddings:
    """Disabled until the host supplies reviewed egress policy and a token bound.

    token_upper_bound must be a deployment-verified billing-token upper bound for
    the selected endpoint. UTF-8 character/byte lengths alone are not assumed to
    prove an upper bound for an arbitrary model tokenizer. Reservations remain
    conservative even when the response reports fewer tokens.
    """
    def __init__(self, fingerprint, *, enabled=False, key_supplier=None,
                 approved_providers=(), policy_verified=False, token_upper_bound=None,
                 max_price_per_token=None, ledger=None, request=None,
                 budget_recovery_verified=False):
        if not isinstance(fingerprint, Fingerprint):
            raise ValueError('invalid_fingerprint')
        self.fingerprint = fingerprint
        self.enabled = enabled is True
        self.key_supplier = key_supplier
        self.approved_providers = tuple(approved_providers)
        self.policy_verified = policy_verified is True
        self.budget_recovery_verified = budget_recovery_verified is True
        self.token_upper_bound = token_upper_bound
        self.price = None if max_price_per_token is None else _money(max_price_per_token)
        self.ledger = ledger
        self.request = request or _http_request
        self._transport = _DeadlineSlot()
        self.failures = 0
        self.paused = False

    def embed(self, scope, texts, *, input_type='document', timeout=20, egress_authorized=False):
        if (not self.enabled or not self.policy_verified or not self.budget_recovery_verified
                or egress_authorized is not True
                or self.paused or not self.key_supplier or not self.token_upper_bound
                or self.price is None or self.ledger is None
                or not self.approved_providers or self.fingerprint.provider not in self.approved_providers
                or any(not isinstance(p, str) or not p or len(p) > 128 for p in self.approved_providers)):
            raise EmbeddingError('embedding_policy_blocked')
        # A boolean flag is an opt-in, never proof that a rolled-back budget is
        # current. Require and verify the independently retained checkpoint.
        self.ledger.assert_recovery_proven()
        scope = self.ledger.store.snapshot(scope)
        if (not isinstance(texts, list) or not 1 <= len(texts) <= 16
                or any(not isinstance(t, str) or not t.strip() or len(t.encode()) > 8192 for t in texts)
                or input_type not in ('query', 'document')
                or type(timeout) not in (int, float) or not 0 < timeout <= 20):
            raise EmbeddingError('embedding_invalid_input')
        try:
            bounds = [self.token_upper_bound(t) for t in texts]
        except Exception:
            raise EmbeddingError('embedding_token_bound_unavailable') from None
        if any(type(n) is not int or not 0 < n <= 512 for n in bounds) or sum(bounds) > 4096:
            raise EmbeddingError('embedding_token_bound_exceeded')
        reservation = self.ledger.reserve(scope, self.price * sum(bounds))
        payload = {'model': self.fingerprint.model, 'input': texts, 'encoding_format': 'float',
                   'input_type': 'search_query' if input_type == 'query' else 'search_document',
                   'provider': {'only': [self.fingerprint.provider], 'allow_fallbacks': False,
                                'data_collection': 'deny', 'zdr': True}}
        try:
            key = self.key_supplier()
            if not isinstance(key, str) or not key or '\n' in key or '\r' in key:
                raise EmbeddingError('embedding_key_unavailable')
            reply = self._transport.call(self.request, payload, key, timeout)
            vectors, usage = self._validate(reply, len(texts))
            if usage is not None and usage > sum(bounds):
                self.paused = True
                raise EmbeddingError('embedding_token_bound_violated')
            # Recheck the trusted scope after a slow HTTP response before publication.
            with self.ledger.store.connection() as db:
                self.ledger.store.validate_scope(db, scope)
            self.ledger.finish(reservation, status='success', usage_tokens=usage)
            self.failures = 0
            return vectors
        except Exception:
            self.failures += 1
            if self.failures >= 3:
                self.paused = True
            try:
                self.ledger.finish(reservation, status='unknown')
            except Exception:
                pass  # A pre-request reservation is already durable; never release it.
            raise EmbeddingError('embedding_request_failed') from None

    def _validate(self, reply, count):
        if (not isinstance(reply, dict) or reply.get('model') != self.fingerprint.model
                or not isinstance(reply.get('data'), list) or len(reply['data']) != count
                or reply.get('provider') != self.fingerprint.provider):
            raise ValueError('invalid_embedding_response')
        result = [None] * count
        for row in reply['data']:
            if not isinstance(row, dict) or type(row.get('index')) is not int:
                raise ValueError('invalid_embedding_response')
            i = row['index']
            if not 0 <= i < count or result[i] is not None:
                raise ValueError('invalid_embedding_response')
            result[i] = unit_vector(row.get('embedding'), self.fingerprint.dimensions)
        usage = reply.get('usage')
        tokens = usage.get('prompt_tokens', usage.get('total_tokens')) if isinstance(usage, dict) else None
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError('invalid_embedding_usage')
        return result, tokens
