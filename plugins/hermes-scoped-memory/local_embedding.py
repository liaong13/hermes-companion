"""Opt-in loopback embeddings; never discovers credentials or starts a model server."""
from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .retrieval import EmbeddingError, Fingerprint, HybridRetriever, VectorIndex, _DeadlineSlot, _NoRedirect, unit_vector
from .scope import ScopeContext


def validate_local_http(raw):
    fields = {'endpoint', 'model', 'model_revision', 'dimensions', 'query_prefix', 'document_prefix'}
    if not isinstance(raw, dict) or set(raw) - fields:
        raise ValueError('invalid local embedding configuration')
    endpoint = raw.get('endpoint')
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(c.isspace() for c in endpoint):
        raise ValueError('local embedding endpoint must be literal loopback HTTP')
    try:
        url = urlsplit(endpoint)
        valid = (url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1')
                 and url.port is not None and url.port > 0 and url.username is None and url.password is None
                 and not url.query and not url.fragment and url.path == '/v1/embeddings')
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('local embedding endpoint must be literal loopback HTTP /v1/embeddings')
    prefixes = [raw.get(key, '') for key in ('query_prefix', 'document_prefix')]
    if any(not isinstance(value, str) or len(value) > 256 for value in prefixes):
        raise ValueError('invalid local embedding input prefix')
    if not isinstance(raw.get('model_revision'), str) or raw['model_revision'] == 'unversioned':
        raise ValueError('local embedding requires an explicit model_revision')
    fingerprint = Fingerprint(
        raw.get('model'), 'local-http:' + hashlib.sha256(endpoint.encode()).hexdigest()[:16],
        raw.get('dimensions'), model_revision=raw['model_revision'],
        preprocess_version='local-text-v1:' + hashlib.sha256(json.dumps(prefixes).encode()).hexdigest()[:16])
    return fingerprint


class LocalEmbeddingRuntime:
    """Profile-owned vector maintenance and queries over approved local facts only."""

    def __init__(self, store, raw, *, allowed):
        self.fingerprint = validate_local_http(raw)
        self.store, self.raw, self.allowed = store, dict(raw), allowed
        self.index = VectorIndex(store)
        self.retriever = HybridRetriever(store, self.index)
        self.generation = 'local-' + hashlib.sha256(self.fingerprint.encode().encode()).hexdigest()[:24]
        self.index.create_generation(self.generation, self.fingerprint)
        self.transport = _DeadlineSlot()
        self.state = 'indexing'
        self.failures, self.retry_after = 0, 0.0

    def _request(self, payload, _key, timeout):
        request = Request(self.raw['endpoint'], data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(8_000_001)
        if len(body) > 8_000_000:
            raise EmbeddingError('local_embedding_response_too_large')
        return json.loads(body)

    def _embed(self, texts, *, kind, timeout):
        if not self.allowed():
            raise PermissionError('local embedding binding expired')
        prefix = self.raw.get('query_prefix' if kind == 'query' else 'document_prefix', '')
        if (not 1 <= len(texts) <= 8 or any(not isinstance(text, str) or not text.strip()
                or len(text.encode()) > 16000 for text in texts)):
            raise ValueError('invalid local embedding input')
        payload = {'model': self.fingerprint.model, 'input': [prefix + text for text in texts],
                   'encoding_format': 'float'}
        reply = self.transport.call(self._request, payload, None, timeout)
        if not self.allowed():
            raise PermissionError('local embedding binding expired')
        if (not isinstance(reply, dict) or reply.get('model') != self.fingerprint.model
                or not isinstance(reply.get('data'), list) or len(reply['data']) != len(texts)):
            raise ValueError('invalid local embedding response')
        vectors = [None] * len(texts)
        for row in reply['data']:
            if not isinstance(row, dict) or type(row.get('index')) is not int:
                raise ValueError('invalid local embedding response')
            position = row['index']
            if not 0 <= position < len(vectors) or vectors[position] is not None:
                raise ValueError('invalid local embedding response')
            vectors[position] = unit_vector(row.get('embedding'), self.fingerprint.dimensions)
        return vectors

    def query(self, scope, text, *, timeout=0.8):
        self.store.snapshot(scope)
        vector = self._embed([text], kind='query', timeout=timeout)[0]
        self.store.snapshot(scope)
        return {'vector': vector, 'fingerprint': self.fingerprint}

    def refresh(self, *, limit=8):
        if time.monotonic() < self.retry_after:
            return False
        try:
            changed = self._refresh(limit=limit)
            self.failures, self.retry_after = 0, 0.0
            return changed
        except Exception:
            self.failures += 1
            self.retry_after = time.monotonic() + min(60, 2 ** min(self.failures, 6))
            self.state = 'degraded_keywords'
            raise

    def _refresh(self, *, limit):
        if not self.allowed():
            return False
        # This trusted maintenance service reads active facts, never raw turns.
        # Capture each stored scope's epoch before I/O; put() rejects revocation.
        with self.store.connection() as db:
            rows = db.execute('''SELECT m.*,s.policy_revision,s.deletion_epoch
                FROM memories m JOIN scope_versions s
                  ON s.domain=m.domain AND s.owner=m.owner AND s.origin=m.origin
                LEFT JOIN retrieval_vectors v ON v.memory_id=m.id AND v.domain=m.domain
                  AND v.revision=m.revision AND v.generation=?
                WHERE m.domain=? AND m.state='active' AND (m.expires IS NULL OR m.expires>?)
                  AND v.memory_id IS NULL ORDER BY m.created,m.id LIMIT ?''',
                (self.generation, self.store.domain_id, time.time(), limit)).fetchall()
            rows = [dict(row) for row in rows]
        if rows:
            self.state = 'indexing'
            vectors = self._embed([row['content'] for row in rows], kind='document', timeout=5)
            for row, vector in zip(rows, vectors):
                if not self.allowed():
                    return False
                scope = ScopeContext(self.store.domain_id, 'stored', row['owner'], 'stored', row['origin'],
                                     policy_revision=row['policy_revision'], deletion_epoch=row['deletion_epoch'])
                self.index.put(scope, row['id'], self.generation, vector, revision=row['revision'],
                               vector_fingerprint=self.fingerprint)
        if not rows and self.state == 'ready':
            return False
        try:
            self.index.activate(self.generation)
            self.state = 'ready'
        except ValueError as exc:
            if str(exc) != 'generation_incomplete':
                raise
            self.state = 'indexing'
        return bool(rows)
