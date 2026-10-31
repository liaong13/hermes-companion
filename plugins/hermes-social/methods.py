"""Chat-scoped, evidence-backed method experiments; never an instruction authority.

The caller is trusted application code. It resolves real feedback, performs
independent validation and records actual deliveries. Model output may propose
the bounded advisory text, but cannot approve an experiment or manufacture an
evaluation. No model, network, sender, private-memory reader or transcript lives
in this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import unicodedata


CHAT = re.compile(r'^(?:group|dm):[1-9][0-9]*$')
STATES = frozenset({'proposed', 'validated', 'trial', 'retained', 'reverted'})
ACTIVE = frozenset({'trial', 'retained'})


def _ident(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or value != value.strip() or any(ord(char) < 32 for char in value)):
        raise ValueError('invalid method reference')
    return value


def _ids(values, *, limit=64, empty=True):
    if (not isinstance(values, (list, tuple, set, frozenset)) or len(values) > limit
            or not empty and not values):
        raise ValueError('invalid bounded method references')
    result = tuple(sorted({_ident(value) for value in values}))
    if len(result) != len(values):
        raise ValueError('method references must be distinct')
    return result


def _text(value, limit):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= limit
            or any(ord(char) < 32 and char not in '\n\t' for char in value)):
        raise ValueError('invalid bounded method description')
    return value.strip()


def _number(value, lower, upper):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError('invalid method measurement')
    return float(value)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(*values):
    return hashlib.sha256(_dump(values).encode()).hexdigest()


def _advice_digest(chat, category, condition, instruction):
    def normalized(value):
        return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    return _hash(chat, *(normalized(value) for value in (category, condition, instruction)))


class MethodStore:
    """Immutable suggestions with durable validation, use and revocation records.

    ``evidence_resolver(chat, ids)`` must validate source policy and revocation
    before returning feedback rows containing id/chat/status/message_id/
    source_id/created. The resolver belongs to the trusted feedback service,
    never to a model or an incoming chat command. A restored content database
    is reconciled against the separate latest control database before use.
    """

    def __init__(self, path, *, allowed_chats=(), evidence_resolver=None,
                 retention_days=30, max_methods=64, clock=None):
        self.path = Path(path).absolute()
        self.control_path = self.path.with_name(self.path.stem+'-control.sqlite3')
        self.allowed_chats = frozenset(allowed_chats)
        if any(not isinstance(chat, str) or not CHAT.fullmatch(chat) for chat in self.allowed_chats):
            raise ValueError('method targets must be exact chats')
        if evidence_resolver is not None and not callable(evidence_resolver):
            raise ValueError('method evidence resolver must be callable')
        if type(retention_days) is not int or not 1 <= retention_days <= 90:
            raise ValueError('invalid method retention')
        if type(max_methods) is not int or not 1 <= max_methods <= 128:
            raise ValueError('invalid method capacity')
        self.evidence_resolver = evidence_resolver
        self.retention, self.capacity = retention_days*86400, max_methods
        self.clock = time.time if clock is None else clock
        self.active = True
        if self.path.parent.resolve() != self.path.parent:
            raise ValueError('method storage directory must not be a symlink')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for target in (self.path.parent, self.path, self.control_path):
            if target.is_symlink() or target.exists() and (target.stat().st_uid != os.getuid() or target.stat().st_mode & 0o077):
                raise ValueError('method storage must be private and owner-only')
        for target in (self.path, self.control_path):
            fd = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
        with self.connection(control=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS revoked_dependencies(
                    chat TEXT NOT NULL,dependency TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(chat,dependency));
                CREATE TABLE IF NOT EXISTS revoked_methods(
                    chat TEXT NOT NULL,id TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(chat,id));
                CREATE TABLE IF NOT EXISTS generation_charges(
                    chat TEXT NOT NULL,request_key TEXT NOT NULL,day TEXT NOT NULL,
                    evidence TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(chat,request_key));
                CREATE TABLE IF NOT EXISTS rejected_advice(
                    chat TEXT NOT NULL,digest TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(chat,digest));
                CREATE TABLE IF NOT EXISTS redacted_methods(
                    chat TEXT NOT NULL,id TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(chat,id));
            ''')
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS methods(
                    id TEXT PRIMARY KEY,chat TEXT NOT NULL,request_key TEXT NOT NULL,
                    category TEXT NOT NULL,condition_text TEXT NOT NULL,instruction TEXT NOT NULL,
                    evidence TEXT NOT NULL,dependencies TEXT NOT NULL,generation_cases TEXT NOT NULL,
                    state TEXT NOT NULL,created REAL NOT NULL,started REAL NOT NULL DEFAULT 0,
                    expires REAL NOT NULL,trial_until REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(chat,request_key));
                CREATE TABLE IF NOT EXISTS validations(
                    method_id TEXT NOT NULL,chat TEXT NOT NULL,case_id TEXT NOT NULL,
                    baseline_pass INTEGER NOT NULL,method_pass INTEGER NOT NULL,
                    validator TEXT NOT NULL,model TEXT NOT NULL,baseline_model TEXT NOT NULL,
                    budget_tokens INTEGER NOT NULL,baseline_cost REAL,method_cost REAL,
                    created REAL NOT NULL,PRIMARY KEY(method_id,case_id));
                CREATE TABLE IF NOT EXISTS uses(
                    id TEXT PRIMARY KEY,method_id TEXT NOT NULL,chat TEXT NOT NULL,
                    outbound_id TEXT NOT NULL,request_id TEXT NOT NULL,reference TEXT NOT NULL,
                    dependencies TEXT NOT NULL,created REAL NOT NULL,
                    UNIQUE(method_id,chat,outbound_id));
                CREATE TABLE IF NOT EXISTS outcomes(
                    method_id TEXT NOT NULL,chat TEXT NOT NULL,use_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,status TEXT NOT NULL,message_id TEXT NOT NULL,
                    created REAL NOT NULL,PRIMARY KEY(method_id,evidence_id));
            ''')

    @contextmanager
    def connection(self, *, control=False):
        db = None
        try:
            db = sqlite3.connect(self.control_path if control else self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            raise ValueError('method storage unavailable') from exc
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _chat(self, chat):
        if not self.active or chat not in self.allowed_chats:
            raise PermissionError('method chat is not authorized')

    def _controls(self, chat):
        with self.connection(control=True) as db:
            deps = {row[0] for row in db.execute('SELECT dependency FROM revoked_dependencies WHERE chat=?', (chat,))}
            methods = {row['id']: row['reason'] for row in db.execute('SELECT id,reason FROM revoked_methods WHERE chat=?', (chat,))}
            # A method may have first failed and later lost its source. Source
            # erasure wins over the older verdict when reconciling a backup.
            methods.update((row[0], 'evidence_revoked') for row in db.execute('SELECT id FROM redacted_methods WHERE chat=?', (chat,)))
        return deps, methods

    def _advice_rejected(self, chat, digest):
        with self.connection(control=True) as db:
            return db.execute('SELECT 1 FROM rejected_advice WHERE chat=? AND digest=?', (chat, digest)).fetchone() is not None

    def _evidence(self, chat, identifiers):
        self._chat(chat)
        identifiers = _ids(identifiers, empty=False)
        if self.evidence_resolver is None:
            raise PermissionError('trusted method evidence resolver is unavailable')
        rows = self.evidence_resolver(chat, identifiers)
        if not isinstance(rows, (tuple, list)) or len(rows) != len(identifiers):
            raise PermissionError('method evidence is incomplete')
        found = {}
        revoked, _ = self._controls(chat)
        for original in rows:
            if not isinstance(original, dict):
                raise PermissionError('method evidence is not verified feedback')
            row = dict(original)
            ident = _ident(row.get('id'))
            if (ident not in identifiers or ident in found or row.get('chat') != chat
                    or row.get('status') not in {'replied', 'corrected', 'declined', 'no_response'}):
                raise PermissionError('method evidence is outside this chat or unsupported')
            _number(row.get('created'), 0, self.clock()+5)
            if (row['created']+self.retention <= self.clock()
                    or row['status'] in {'corrected', 'declined'} and not row.get('message_id')):
                raise PermissionError('semantic method feedback is expired or lacks a real message')
            for name in ('message_id', 'source_id'):
                if name == 'message_id' and row.get(name) == '':
                    continue
                _ident(row.get(name))
            dependencies = {ident, row['source_id'], row['message_id']} - {''}
            if dependencies.intersection(revoked):
                raise PermissionError('method evidence has been revoked')
            found[ident] = row
        return [found[ident] for ident in identifiers]

    @staticmethod
    def _row(row):
        value = dict(row)
        value['condition'] = value.pop('condition_text')
        for field in ('evidence', 'dependencies', 'generation_cases'):
            value[field] = json.loads(value[field])
        return value

    def _revert(self, db, chat, ident, reason):
        reason = _text(reason, 80)
        row = db.execute('SELECT category,condition_text,instruction FROM methods WHERE chat=? AND id=?', (chat, ident)).fetchone()
        digest = _advice_digest(chat, row['category'], row['condition_text'], row['instruction']) if row is not None else ''
        redact = reason in {'evidence_revoked', 'evidence_unavailable'}
        with self.connection(control=True) as controls:
            controls.execute('INSERT OR IGNORE INTO revoked_methods VALUES(?,?,?,?)',
                             (chat, ident, reason, self.clock()))
            if reason in {'contradictory_feedback', 'independent_validation_failed'} and row is not None:
                controls.execute('INSERT OR IGNORE INTO rejected_advice VALUES(?,?,?,?)', (chat, digest, reason, self.clock()))
            if redact:
                controls.execute('INSERT OR IGNORE INTO redacted_methods VALUES(?,?,?,?)', (chat, ident, digest, self.clock()))
        if redact:
            db.execute("UPDATE methods SET state='reverted',reason=?,condition_text='',instruction='',revision=revision+1 "
                       "WHERE chat=? AND id=? AND (state!='reverted' OR condition_text!='' OR instruction!='')", (reason, chat, ident))
        else:
            db.execute("UPDATE methods SET state='reverted',reason=?,revision=revision+1 WHERE chat=? AND id=? AND state!='reverted'",
                       (reason, chat, ident))

    def _refresh(self, db, chat):
        revoked, methods = self._controls(chat)
        rows = db.execute('SELECT * FROM methods WHERE chat=?', (chat,)).fetchall()
        for row in rows:
            reason = methods.get(row['id'])
            if reason is None and set(json.loads(row['dependencies'])).intersection(revoked):
                reason = 'evidence_revoked'
            if reason is None and row['state'] != 'reverted' and row['expires'] <= self.clock():
                reason = 'expired'
            if reason is None and row['state'] == 'trial' and row['trial_until'] <= self.clock():
                reason = 'trial_expired'
            if reason is not None:
                self._revert(db, chat, row['id'], reason)

    def method(self, chat, method_id):
        self._chat(chat)
        _ident(method_id)
        with self.connection() as db:
            self._refresh(db, chat)
            row = db.execute('SELECT * FROM methods WHERE chat=? AND id=?', (chat, method_id)).fetchone()
        if row is None:
            raise PermissionError('method is outside this chat')
        result = self._row(row)
        if result['state'] != 'reverted':
            try:
                self._evidence(chat, result['evidence'])
            except (PermissionError, ValueError):
                self.revert(chat, method_id, reason='evidence_unavailable')
                return self.method(chat, method_id)
        return result

    def propose(self, chat, category, condition, instruction, evidence_ids, *, dependencies=(),
                generation_case_ids=(), request_key=None):
        self._chat(chat)
        category = _text(category, 40)
        condition, instruction = _text(condition, 200), _text(instruction, 500)
        evidence_ids = _ids(evidence_ids, empty=False)
        evidence = self._evidence(chat, evidence_ids)
        if not any(row['status'] in {'corrected', 'declined'} for row in evidence):
            raise PermissionError('semantic method requires explicit corrective evidence')
        source_ids = {value for row in evidence for value in (row['id'], row['source_id'], row['message_id']) if value}
        deps = _ids(tuple(set(_ids(dependencies)) | source_ids))
        generation = _ids(tuple(set(_ids(generation_case_ids)) | set(evidence_ids)))
        ident = 'method:'+_hash(chat, category, condition, instruction, evidence_ids, deps, generation)
        advice_digest = _advice_digest(chat, category, condition, instruction)
        request_key = ident if request_key is None else _ident(request_key)
        revoked, controls = self._controls(chat)
        if set(deps).intersection(revoked) or ident in controls or self._advice_rejected(chat, advice_digest):
            raise PermissionError('forgotten method sources cannot be proposed again')
        with self.connection() as db:
            self._refresh(db, chat)
            revoked, controls = self._controls(chat)
            if set(deps).intersection(revoked) or ident in controls or self._advice_rejected(chat, advice_digest):
                raise PermissionError('method sources were revoked before proposal')
            previous = db.execute('SELECT id FROM methods WHERE chat=? AND request_key=?', (chat, request_key)).fetchone()
            if previous is not None and previous['id'] != ident:
                raise PermissionError('method request key cannot change its proposal')
            if db.execute('SELECT 1 FROM methods WHERE id=?', (ident,)).fetchone() is None:
                count = db.execute('SELECT COUNT(*) FROM methods WHERE chat=?', (chat,)).fetchone()[0]
                if count >= self.capacity:
                    old = db.execute("SELECT id FROM methods WHERE chat=? AND state='reverted' ORDER BY created,id LIMIT 1", (chat,)).fetchone()
                    if old is None:
                        raise ValueError('method capacity reached')
                    removed = {old['id'], *[row[0] for row in db.execute('SELECT outbound_id FROM uses WHERE method_id=? AND chat=?', (old['id'], chat))]}
                    with self.connection(control=True) as controls:
                        controls.executemany('INSERT OR IGNORE INTO revoked_dependencies VALUES(?,?,?)',
                                             ((chat, value, self.clock()) for value in removed))
                    for table in ('validations', 'outcomes', 'uses'):
                        db.execute('DELETE FROM '+table+' WHERE method_id=? AND chat=?', (old['id'], chat))
                    db.execute('DELETE FROM methods WHERE id=? AND chat=?', (old['id'], chat))
                    if set(deps).intersection(removed):
                        raise PermissionError('method proposal depends on retired data')
                db.execute('''INSERT INTO methods(id,chat,request_key,category,condition_text,instruction,
                    evidence,dependencies,generation_cases,state,created,expires)
                    VALUES(?,?,?,?,?,?,?,?,?,'proposed',?,?)''',
                    (ident, chat, request_key, category, condition, instruction, _dump(evidence_ids),
                     _dump(deps), _dump(generation), self.clock(), self.clock()+self.retention))
        return ident

    def reserve_generation(self, chat, request_key, day, limit=2, *, evidence_ids):
        """Consume a daily proposal attempt before inference, including failures.

        Day is the caller's configured local ISO date. The control ledger keeps
        failed attempts and duplicate keys consumed across content rollback.
        """
        self._chat(chat)
        request_key = _ident(request_key)
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError('invalid method generation budget day')
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError('invalid method generation budget')
        ids = _ids(evidence_ids, empty=False)
        rows = self._evidence(chat, ids)
        if not any(row['status'] in {'corrected', 'declined'} for row in rows):
            raise PermissionError('method generation requires explicit corrective evidence')
        encoded = _dump(ids)
        with self.connection(control=True) as db:
            old = db.execute('SELECT evidence FROM generation_charges WHERE chat=? AND request_key=?',
                             (chat, request_key)).fetchone()
            if old is not None:
                if old['evidence'] != encoded:
                    raise PermissionError('method generation key cannot change evidence')
                return False
            if db.execute('SELECT COUNT(*) FROM generation_charges WHERE chat=? AND day=?', (chat, day)).fetchone()[0] >= limit:
                return False
            db.execute('INSERT INTO generation_charges VALUES(?,?,?,?,?)',
                       (chat, request_key, day, encoded, self.clock()))
        return True

    def list_methods(self, chat, *, limit=16):
        self._chat(chat)
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('invalid method list limit')
        with self.connection() as db:
            self._refresh(db, chat)
            ids = [row[0] for row in db.execute('SELECT id FROM methods WHERE chat=? ORDER BY created DESC,id LIMIT ?', (chat, limit))]
        return [self.method(chat, ident) for ident in ids]

    def start_trial(self, chat, method_id, *, duration_seconds=86400, approved=False):
        row = self.method(chat, method_id)
        if type(approved) is not bool:
            raise ValueError('method trial approval must be trusted boolean')
        duration = _number(duration_seconds, 60, 604800)
        if row['state'] in ACTIVE:
            return row  # Idempotent; never renew a running trial's deadline.
        if row['state'] != 'validated' and not (row['state'] == 'proposed' and approved):
            raise PermissionError('method needs independent validation or explicit trial approval')
        with self.connection() as db:
            self._refresh(db, chat)
            changed = db.execute("UPDATE methods SET state='trial',started=?,trial_until=?,revision=revision+1 WHERE chat=? AND id=? AND state IN ('validated','proposed')",
                (self.clock(), min(row['expires'], self.clock()+duration), chat, method_id)).rowcount
            if not changed:
                raise PermissionError('method trial was revoked before start')
        return self.method(chat, method_id)

    def record_validation(self, chat, method_id, case_id, baseline_pass, method_pass, *,
                          validator, model, baseline_model, budget_tokens=2048,
                          baseline_cost=None, method_cost=None):
        """Record trusted, independent case outcomes; model self-grading is invalid.

        The caller enforces the deterministic/human validator itself. This API
        is not a chat command, and strings returned by a model are not proof.
        Unknown costs stay NULL; model routes and token budgets remain explicit.
        """
        row = self.method(chat, method_id)
        case_id = _ident(case_id)
        if type(baseline_pass) is not bool or type(method_pass) is not bool:
            raise ValueError('method validation needs explicit boolean outcomes')
        if validator not in {'deterministic', 'human'}:
            raise PermissionError('a model self-score cannot validate a method')
        model, baseline_model = _text(model, 120), _text(baseline_model, 120)
        if model != baseline_model:
            raise PermissionError('method comparison must use the same model route')
        if type(budget_tokens) is not int or not 32 <= budget_tokens <= 131072:
            raise ValueError('invalid comparable method token budget')
        for cost in (baseline_cost, method_cost):
            if cost is not None:
                _number(cost, 0, 1000000)
        if case_id in {*row['generation_cases'], *row['dependencies'], *row['evidence']}:
            raise PermissionError('generation evidence is not a held-out validation case')
        values = (int(baseline_pass), int(method_pass), validator, model, baseline_model,
                  budget_tokens, baseline_cost, method_cost)
        with self.connection() as db:
            self._refresh(db, chat)
            previous = db.execute('SELECT * FROM validations WHERE method_id=? AND case_id=? AND chat=?', (method_id, case_id, chat)).fetchone()
            if previous is not None:
                if tuple(previous[key] for key in ('baseline_pass','method_pass','validator','model','baseline_model','budget_tokens','baseline_cost','method_cost')) != values:
                    raise PermissionError('method validation case cannot be rewritten')
                return self._row(db.execute('SELECT * FROM methods WHERE id=? AND chat=?', (method_id, chat)).fetchone())
            state = db.execute('SELECT state FROM methods WHERE id=? AND chat=?', (method_id, chat)).fetchone()[0]
            if state == 'reverted':
                raise PermissionError('revoked method cannot receive new validation')
            if db.execute('SELECT COUNT(*) FROM validations WHERE method_id=?', (method_id,)).fetchone()[0] >= 32:
                raise ValueError('method validation capacity reached')
            db.execute('INSERT INTO validations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (method_id, chat, case_id, *values, self.clock()))
            if not method_pass:
                self._revert(db, chat, method_id, 'independent_validation_failed')
            elif state == 'proposed':
                db.execute("UPDATE methods SET state='validated',reason='independent_case_passed',revision=revision+1 WHERE id=? AND chat=?", (method_id, chat))
            elif state == 'trial':
                self._retain(db, chat, method_id)
        return self.method(chat, method_id)

    def _retain(self, db, chat, method_id):
        method = db.execute('SELECT * FROM methods WHERE chat=? AND id=?', (chat, method_id)).fetchone()
        rows = db.execute('SELECT * FROM validations WHERE chat=? AND method_id=?', (chat, method_id)).fetchall()
        if (method['state'] == 'trial' and len(rows) >= 2 and all(row['method_pass'] for row in rows)
                and any(not row['baseline_pass'] for row in rows)
                and any(row['created'] > method['started'] for row in rows)):
            db.execute("UPDATE methods SET state='retained',reason='heldout_improvement',revision=revision+1 WHERE id=? AND chat=?", (method_id, chat))

    def context(self, chat, *, limit=3, budget=1200):
        self._chat(chat)
        if type(limit) is not int or not 1 <= limit <= 3 or type(budget) is not int or not 128 <= budget <= 1200:
            raise ValueError('invalid bounded method context')
        rows = []
        with self.connection() as db:
            self._refresh(db, chat)
            ids = [row[0] for row in db.execute("SELECT id FROM methods WHERE chat=? AND state IN ('trial','retained') ORDER BY started DESC,created DESC,id LIMIT 32", (chat,))]
        for ident in ids:
            method = self.method(chat, ident)
            if method['state'] not in ACTIVE:
                continue
            item = {name: method[name] for name in ('id','category','condition','instruction','state','revision')}
            item['provenance'] = 'chat_scoped_advisory_not_authority'
            if len(_dump([*rows, item]).encode()) > budget:
                continue
            rows.append(item)
            if len(rows) >= limit:
                break
        return rows

    def current(self, chat, method_ids):
        try:
            return all(self.method(chat, ident)['state'] in ACTIVE for ident in _ids(method_ids, limit=8))
        except (PermissionError, ValueError):
            return False

    def dependencies(self, chat, method_id):
        return tuple(sorted({method_id, *self.method(chat, method_id)['dependencies']}))

    def record_use(self, chat, method_id, outbound_id, *, request_id='', dependencies=(), reference=''):
        """Only the trusted caller records a real, confirmed outgoing delivery."""
        method = self.method(chat, method_id)
        if method['state'] not in ACTIVE:
            raise PermissionError('only current trial/retained methods can be used')
        outbound_id = _ident(outbound_id)
        if request_id:
            _ident(request_id)
        reference = _ident(reference or 'exchange:'+_hash(chat, outbound_id))
        deps = _ids(tuple(set(_ids(dependencies)) | set(method['dependencies']) | {outbound_id} | ({request_id} if request_id else set())))
        revoked, _ = self._controls(chat)
        if set(deps).intersection(revoked):
            raise PermissionError('method use references forgotten data')
        ident = 'method-use:'+_hash(chat, method_id, outbound_id)
        with self.connection() as db:
            self._refresh(db, chat)
            current = db.execute('SELECT state FROM methods WHERE id=? AND chat=?', (method_id, chat)).fetchone()
            if current is None or current['state'] not in ACTIVE:
                raise PermissionError('method was revoked before delivery use')
            previous = db.execute('SELECT * FROM uses WHERE id=?', (ident,)).fetchone()
            values = (method_id, chat, outbound_id, request_id, reference, _dump(deps))
            if previous is not None:
                if tuple(previous[key] for key in ('method_id','chat','outbound_id','request_id','reference','dependencies')) != values:
                    raise PermissionError('method delivery use cannot be rewritten')
                return ident
            if db.execute('SELECT COUNT(*) FROM uses WHERE chat=?', (chat,)).fetchone()[0] >= 512:
                raise ValueError('method use capacity reached')
            db.execute('INSERT INTO uses VALUES(?,?,?,?,?,?,?,?)', (ident, *values, self.clock()))
        return ident

    def record_outcome(self, chat, method_id, use_id, feedback_id):
        method = self.method(chat, method_id)
        _ident(use_id)
        evidence = self._evidence(chat, [feedback_id])[0]
        with self.connection() as db:
            self._refresh(db, chat)
            usage = db.execute('SELECT * FROM uses WHERE id=? AND method_id=? AND chat=?', (use_id, method_id, chat)).fetchone()
            if (usage is None or evidence['created'] <= usage['created']
                    or evidence.get('reference') != usage['reference'] and evidence['source_id'] != usage['reference']):
                raise PermissionError('method feedback must follow its exact delivered use')
            previous = db.execute('SELECT * FROM outcomes WHERE method_id=? AND evidence_id=?', (method_id, feedback_id)).fetchone()
            if previous is not None:
                if previous['use_id'] != use_id or previous['message_id'] != evidence['message_id']:
                    raise PermissionError('method outcome evidence cannot be rewritten')
                if previous['status'] != evidence['status']:
                    # A verified quote first counts as a reply. The natural
                    # feedback classifier may then refine that SAME immutable
                    # message into explicit correction/refusal. Accept only
                    # this monotonic refinement, never a negative-to-positive
                    # rewrite or a different delivery/message binding.
                    if previous['status'] not in {'replied', 'no_response'} or evidence['status'] not in {'corrected', 'declined'}:
                        raise PermissionError('method outcome verdict cannot be weakened')
                    db.execute('UPDATE outcomes SET status=? WHERE method_id=? AND evidence_id=? AND chat=?',
                               (evidence['status'], method_id, feedback_id, chat))
                    self._revert(db, chat, method_id, 'contradictory_feedback')
                return self._row(db.execute('SELECT * FROM methods WHERE id=? AND chat=?', (method_id, chat)).fetchone())
            state = db.execute('SELECT state FROM methods WHERE id=? AND chat=?', (method_id, chat)).fetchone()[0]
            if state not in ACTIVE:
                raise PermissionError('method is no longer active')
            if db.execute('SELECT COUNT(*) FROM outcomes WHERE method_id=?', (method_id,)).fetchone()[0] >= 64:
                raise ValueError('method outcome capacity reached')
            db.execute('INSERT INTO outcomes VALUES(?,?,?,?,?,?,?)', (method_id, chat, use_id, feedback_id,
                       evidence['status'], evidence['message_id'], self.clock()))
            if evidence['status'] in {'corrected', 'declined'}:
                self._revert(db, chat, method_id, 'contradictory_feedback')
        # A reply or silence alone never proves that a method improved.
        return self.method(chat, method_id)

    def validations(self, chat, method_id):
        self.method(chat, method_id)
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM validations WHERE chat=? AND method_id=? ORDER BY created,case_id', (chat, method_id))]

    def uses(self, chat, method_id):
        self.method(chat, method_id)
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM uses WHERE chat=? AND method_id=? ORDER BY created,id', (chat, method_id))]

    def uses_for_delivery(self, chat, outbound_id):
        self._chat(chat)
        _ident(outbound_id)
        with self.connection() as db:
            self._refresh(db, chat)
            rows = [dict(row) for row in db.execute('SELECT u.* FROM uses u JOIN methods m ON m.id=u.method_id '
                "WHERE u.chat=? AND u.outbound_id=? AND m.state IN ('trial','retained') ORDER BY u.created,u.id LIMIT 8", (chat, outbound_id))]
        return [row for row in rows if self.method(chat, row['method_id'])['state'] in ACTIVE]

    def revert(self, chat, method_id, *, reason='explicit_revert'):
        self._chat(chat)
        _ident(method_id)
        with self.connection() as db:
            if db.execute('SELECT 1 FROM methods WHERE chat=? AND id=?', (chat, method_id)).fetchone() is None:
                raise PermissionError('method is outside this chat')
            self._revert(db, chat, method_id, reason)
        return True

    def forget_messages(self, chat, message_ids):
        """Revoke source, validation, feedback and transitive delivered-method IDs."""
        self._chat(chat)
        revoked = set(_ids(message_ids, limit=1024))
        with self.connection() as db:
            rows = db.execute('SELECT * FROM methods WHERE chat=?', (chat,)).fetchall()
            uses = db.execute('SELECT * FROM uses WHERE chat=?', (chat,)).fetchall()
            validations = db.execute('SELECT method_id,case_id FROM validations WHERE chat=?', (chat,)).fetchall()
            outcomes = db.execute('SELECT method_id,evidence_id,message_id FROM outcomes WHERE chat=?', (chat,)).fetchall()
            affected = set()
            while True:
                before = len(affected), len(revoked)
                for row in rows:
                    identifiers = {row['id'], *json.loads(row['dependencies']), *json.loads(row['evidence'])}
                    identifiers.update(item['case_id'] for item in validations if item['method_id'] == row['id'])
                    identifiers.update(value for item in outcomes if item['method_id'] == row['id']
                                       for value in (item['evidence_id'], item['message_id']) if value)
                    if identifiers.intersection(revoked):
                        affected.add(row['id'])
                for usage in uses:
                    if usage['method_id'] in affected or set(json.loads(usage['dependencies'])).intersection(revoked):
                        affected.add(usage['method_id'])
                        revoked.add(usage['outbound_id'])
                revoked.update(affected)
                if (len(affected), len(revoked)) == before:
                    break
            # The fence is committed first, even if a later content write fails.
            with self.connection(control=True) as controls:
                controls.executemany('INSERT OR IGNORE INTO revoked_dependencies VALUES(?,?,?)',
                                     ((chat, ident, self.clock()) for ident in revoked))
            for ident in affected:
                self._revert(db, chat, ident, 'evidence_revoked')
        return tuple(sorted(revoked))

    def forget_evidence(self, chat, evidence_ids):
        return self.forget_messages(chat, evidence_ids)

    def close(self):
        self.active = False
