"""Bounded public activities, contact offers and evidence-backed strategy trials.

This module has no network client, model, private-memory reader or sender. The
host supplies an authorized public fetch callback and consumes ContactLedger
before claiming an offer. Content and control records deliberately live apart.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid


CHAT = re.compile(r'^(?:group|dm):[1-9][0-9]*$')
FEEDBACK = frozenset({'replied', 'declined', 'corrected', 'no_response',
                      'activity_observed', 'cancelled', 'closed_by_member'})
DEFAULT_STRATEGY = {'cooldown_multiplier': 1.0, 'max_reply_chars': 600, 'sharing_enabled': True}
PUBLIC_SOURCE_DEPENDENCY = 'public-source:'


def _clip(text, limit):
    return text.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def _hash(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _url(value):
    if (not isinstance(value, str) or len(value) > 2048
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise ValueError('invalid public source URL')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        valid = (parsed.scheme == 'https' and host and not parsed.username
                 and not parsed.password and parsed.port in (None, 443) and not parsed.fragment)
        if host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local', '.internal')):
            valid = False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass  # The trusted fetcher still verifies DNS and binds the public address.
        else:
            valid = valid and address.is_global
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('public source must be an exact HTTPS URL')
    return value


def _number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('invalid '+label)
    return float(value)


@dataclass(frozen=True)
class PublishedDocument:
    requested_url: str
    url: str
    text: str
    title: str = ''
    fetched_at: float = 0
    status_code: int = 200


@dataclass(frozen=True)
class ActivityOpportunity:
    reference: str
    chat_id: str
    recipient: str
    text: str
    expires_at: float
    source_id: str
    reason: str
    _store: object
    _attempt: list

    def current(self):
        try:
            return self._store._offer_current(self.chat_id, self.reference, self._attempt[0])
        except (PermissionError, ValueError):
            return False

    def claim(self, attempt):
        self._store.claim(self.chat_id, self.reference, attempt)
        self._attempt[0] = attempt

    def finish(self, status):
        return self._store.finish(self.chat_id, self.reference, self._attempt[0], status)

    def record_delivery(self, message_id):
        return self._store.record_delivery(self.chat_id, self.reference, message_id)

    def block_delivery(self, message_id):
        return self._store.block_delivery(self.chat_id, self.reference, message_id)


class ActivityStore:
    """Private local activity state, always filtered by current exact-chat policy."""

    def __init__(self, path, *, allowed_chats=(), allowed_sources=None, policy_revision=1,
                 retention_days=30, max_activities=128, clock=None):
        self.path = Path(path)
        self.control_path = self.path.with_name(self.path.stem+'-control.sqlite3')
        self.allowed_chats = frozenset(allowed_chats)
        if any(not isinstance(chat, str) or not CHAT.fullmatch(chat) for chat in self.allowed_chats):
            raise ValueError('activity targets must be exact chats')
        if type(policy_revision) is not int or policy_revision < 1:
            raise ValueError('invalid activity policy revision')
        if type(retention_days) is not int or not 1 <= retention_days <= 90:
            raise ValueError('invalid activity retention')
        if type(max_activities) is not int or not 1 <= max_activities <= 512:
            raise ValueError('invalid activity capacity')
        self.revision, self.retention, self.capacity = policy_revision, retention_days*86400, max_activities
        self.clock = time.time if clock is None else clock
        self.active = True
        self.allowed_sources = {}
        for chat, urls in (allowed_sources or {}).items():
            self._chat(chat)
            if not isinstance(urls, (tuple, list, set, frozenset)) or len(urls) > 32:
                raise ValueError('invalid public source list')
            self.allowed_sources[chat] = frozenset(_url(url) for url in urls)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for target in (self.path.parent, self.path, self.control_path):
            if target.is_symlink() or target.exists() and (target.stat().st_uid != os.getuid() or target.stat().st_mode & 0o077):
                raise ValueError('activity storage must be private and owner-only')
        for target in (self.path, self.control_path):
            fd = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS activities (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, request_key TEXT NOT NULL,
                    url TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, due REAL NOT NULL, expires REAL NOT NULL,
                    lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, error_type TEXT NOT NULL DEFAULT '',
                    UNIQUE(chat,request_key));
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
                    chat TEXT NOT NULL, url TEXT NOT NULL, digest TEXT NOT NULL,
                    fetched REAL NOT NULL, expires REAL NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS contents (
                    source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, text TEXT NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS opportunities (
                    reference TEXT PRIMARY KEY, chat TEXT NOT NULL,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    recipient TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL,
                    due REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, reference TEXT NOT NULL,
                    source_id TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL,
                    message_id TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS exchanges (
                    reference TEXT PRIMARY KEY, chat TEXT NOT NULL, message_id TEXT NOT NULL,
                    recipient TEXT NOT NULL, reply_to TEXT NOT NULL, dependencies TEXT NOT NULL,
                    revision INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS trials (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, patch TEXT NOT NULL,
                    evidence TEXT NOT NULL, baseline TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    started REAL NOT NULL DEFAULT 0, expires REAL NOT NULL,
                    evaluation TEXT NOT NULL DEFAULT '');
            ''')
        with self.connection(control=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS revoked_sources (
                    chat TEXT NOT NULL, source_id TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(chat,source_id));
                CREATE TABLE IF NOT EXISTS consumed (
                    reference TEXT PRIMARY KEY, chat TEXT NOT NULL, attempt TEXT NOT NULL,
                    created REAL NOT NULL, state TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS revoked_messages (
                    chat TEXT NOT NULL, message_id TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(chat,message_id));
                CREATE TABLE IF NOT EXISTS read_budgets (
                    chat TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL,
                    PRIMARY KEY(chat,day));
            ''')

    @contextmanager
    def connection(self, *, control=False):
        db = None
        try:
            db = sqlite3.connect(self.control_path if control else self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            raise ValueError('activity storage unavailable') from exc
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _chat(self, chat):
        if not self.active or chat not in self.allowed_chats:
            raise PermissionError('activity chat is not authorized')

    def _source_allowed(self, chat, url):
        self._chat(chat)
        if _url(url) not in self.allowed_sources.get(chat, ()):
            raise PermissionError('public source is not configured for this chat')

    def _revoked(self, chat, source_id):
        with self.connection(control=True) as db:
            return db.execute('SELECT 1 FROM revoked_sources WHERE chat=? AND source_id=?', (chat, source_id)).fetchone() is not None

    def _consumed(self, reference):
        with self.connection(control=True) as db:
            row = db.execute('SELECT * FROM consumed WHERE reference=?', (reference,)).fetchone()
            return dict(row) if row is not None else None

    def consume_read_budget(self, chat, day, limit):
        """Called immediately before trusted fetch; failed requests still cost a slot."""
        self._chat(chat)
        try:
            valid_day = isinstance(day, str) and datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') == day
        except ValueError:
            valid_day = False
        if not valid_day or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('invalid public reading budget')
        with self.connection(control=True) as db:
            old = db.execute('SELECT used FROM read_budgets WHERE chat=? AND day=?', (chat, day)).fetchone()
            used = old[0] if old is not None else 0
            if used >= limit:
                raise PermissionError('public reading budget exhausted')
            db.execute('INSERT OR REPLACE INTO read_budgets VALUES (?,?,?)', (chat, day, used+1))
        return used+1

    def read_budget_used(self, chat, day):
        self._chat(chat)
        with self.connection(control=True) as db:
            row = db.execute('SELECT used FROM read_budgets WHERE chat=? AND day=?', (chat, day)).fetchone()
        return row[0] if row is not None else 0

    def queue_read(self, chat, url, *, request_key=None, due_at=None, expires_at=None):
        self._source_allowed(chat, url)
        now = self.clock()
        due = now if due_at is None else _number(due_at, now-1, now+30*86400, 'activity due')
        expires = due+86400 if expires_at is None else _number(expires_at, due+1, due+7*86400, 'activity expiry')
        key = request_key or _hash(chat, url, int(now//86400))
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ValueError('invalid activity request key')
        with self.connection() as db:
            old = db.execute('SELECT * FROM activities WHERE chat=? AND request_key=?', (chat, key)).fetchone()
            if old is not None:
                if old['url'] != url or old['revision'] != self.revision:
                    raise PermissionError('activity request key changed scope')
                return old['id']
            count = db.execute('SELECT COUNT(*) FROM activities WHERE chat=?', (chat,)).fetchone()[0]
            if count >= self.capacity:
                raise ValueError('activity capacity reached')
            ident = uuid.uuid4().hex
            db.execute('''INSERT INTO activities(id,chat,request_key,url,revision,state,created,due,expires)
                          VALUES (?,?,?,?,?,'ready',?,?,?)''',
                       (ident, chat, key, url, self.revision, now, due, expires))
            return ident

    def _claim_read(self, chat):
        self._chat(chat)
        now = self.clock()
        with self.connection() as db:
            db.execute("UPDATE activities SET state='ready',lease='' WHERE chat=? AND state='running' AND lease_until<=?", (chat, now))
            db.execute("UPDATE activities SET state='expired',lease='' WHERE chat=? AND expires<=? AND state IN ('ready','running')", (chat, now))
            rows = db.execute("SELECT * FROM activities WHERE chat=? AND state='ready' AND revision=? AND due<=? AND expires>? ORDER BY due,created LIMIT 32",
                              (chat, self.revision, now, now)).fetchall()
            row = next((row for row in rows if row['url'] in self.allowed_sources.get(chat, ()) and row['attempts'] < 3), None)
            if row is None:
                return None
            lease = uuid.uuid4().hex
            db.execute("UPDATE activities SET state='running',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?", (lease, now+60, row['id']))
            return dict(row), lease

    async def advance(self, fetch, *, chat=None, budget=1, valid=None):
        if type(budget) is not int or not 1 <= budget <= 4 or not callable(fetch):
            raise ValueError('invalid activity execution budget')
        chats = sorted(self.allowed_chats) if chat is None else [chat]
        results = []
        for target in chats:
            self._chat(target)
            while len(results) < budget:
                if valid is not None and not valid():
                    return results
                claimed = self._claim_read(target)
                if claimed is None:
                    break
                activity, lease = claimed
                try:
                    async with asyncio.timeout(30):
                        document = fetch(activity['url'])
                        if inspect.isawaitable(document):
                            document = await document
                    if valid is not None and not valid():
                        raise PermissionError('activity execution was revoked')
                    source_id = self._complete_read(activity, lease, document)
                    results.append(dict(activity_id=activity['id'], state='completed', source_id=source_id))
                except asyncio.CancelledError:
                    self._failed_read(activity, lease, 'cancelled', retry=True)
                    raise
                except Exception as exc:
                    self._failed_read(activity, lease, type(exc).__name__)
                    results.append(dict(activity_id=activity['id'], state='failed', error_type=type(exc).__name__))
            if len(results) >= budget:
                break
        return results

    def _failed_read(self, activity, lease, error_type, *, retry=False):
        with self.connection() as db:
            db.execute('''UPDATE activities SET state=?,lease='',error_type=?,due=?
                          WHERE id=? AND chat=? AND lease=? AND state='running' ''',
                       ('ready' if retry else 'failed', error_type, self.clock()+60,
                        activity['id'], activity['chat'], lease))

    def retry_read(self, chat, activity_id):
        self._chat(chat)
        with self.connection() as db:
            row = db.execute('SELECT * FROM activities WHERE id=? AND chat=?', (activity_id, chat)).fetchone()
            if row is None or row['state'] != 'failed' or row['attempts'] >= 3 or row['expires'] <= self.clock():
                raise ValueError('activity cannot be retried')
            self._source_allowed(chat, row['url'])
            db.execute("UPDATE activities SET state='ready',due=? WHERE id=?", (self.clock(), activity_id))

    def cancel_read(self, chat, activity_id):
        self.activity(chat, activity_id)
        with self.connection() as db:
            db.execute("UPDATE activities SET state='cancelled',lease='' WHERE chat=? AND id=? AND state IN ('ready','running','failed')",
                       (chat, activity_id))

    def _complete_read(self, activity, lease, document):
        chat, now = activity['chat'], self.clock()
        self._source_allowed(chat, activity['url'])
        if (not isinstance(document, PublishedDocument) or document.requested_url != activity['url']
                or document.url != activity['url'] or type(document.status_code) is not int
                or not 200 <= document.status_code < 300 or not isinstance(document.text, str)
                or not 1 <= len(document.text.strip().encode()) <= 65536
                or not isinstance(document.title, str) or len(document.title.encode()) > 512
                or not now-300 <= document.fetched_at <= now+5):
            raise ValueError('public reading lacks valid execution evidence')
        digest = hashlib.sha256(document.text.encode()).hexdigest()
        source_id = _hash(chat, activity['id'], document.url, digest)
        if self._revoked(chat, source_id):
            raise PermissionError('public source was revoked')
        note = _clip(document.text.strip(), 900)
        with self.connection() as db:
            current = db.execute('SELECT * FROM activities WHERE id=? AND chat=?', (activity['id'], chat)).fetchone()
            if (current is None or current['state'] != 'running' or current['lease'] != lease
                    or current['lease_until'] <= now or current['revision'] != self.revision
                    or current['expires'] <= now):
                raise PermissionError('activity execution lease expired')
            db.execute('INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?)',
                       (source_id, activity['id'], chat, document.url, digest, document.fetched_at,
                        min(now+self.retention, current['expires']+self.retention), self.revision))
            db.execute('INSERT OR REPLACE INTO contents VALUES (?,?,?,?)',
                       (source_id, document.title.strip(), document.text, note))
            db.execute("UPDATE activities SET state='completed',lease='',error_type='' WHERE id=?", (activity['id'],))
        return source_id

    def source(self, chat, source_id):
        self._chat(chat)
        if self._revoked(chat, source_id):
            raise PermissionError('public source was revoked')
        with self.connection() as db:
            row = db.execute('''SELECT s.*,c.title,c.note FROM sources s JOIN contents c ON c.source_id=s.id
                                WHERE s.chat=? AND s.id=? AND s.revision=? AND s.expires>?''',
                             (chat, source_id, self.revision, self.clock())).fetchone()
        if row is None:
            raise PermissionError('public source is unavailable in this chat')
        self._source_allowed(chat, row['url'])
        return dict(row)

    def notes(self, chat, *, limit=4):
        self._chat(chat)
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('invalid public note limit')
        with self.connection() as db:
            rows = db.execute('SELECT id FROM sources WHERE chat=? ORDER BY fetched DESC LIMIT 64', (chat,)).fetchall()
        results = []
        for row in rows:
            try:
                results.append(self.source(chat, row['id']))
            except PermissionError:
                continue
            if len(results) >= limit:
                break
        return results

    def public_context(self, chat, budget=2400):
        self._chat(chat)
        if type(budget) is not int or not 256 <= budget <= 12000:
            raise ValueError('invalid public activity context budget')
        result = []
        for row in self.notes(chat):
            item = dict(source_id=row['id'], url=row['url'], title=row['title'],
                        fetched_at=row['fetched'], excerpt=row['note'], provenance='public_reading_excerpt',
                        dependency=PUBLIC_SOURCE_DEPENDENCY+row['id'])
            if len(json.dumps([*result, item], ensure_ascii=False).encode()) > budget:
                continue
            result.append(item)
        return result

    def create_opportunity(self, chat, source_id, *, reason='reading', recipient='', due_at=None, expires_at=None):
        source = self.source(chat, source_id)
        if reason not in {'reading', 'discovery', 'followup'} or not isinstance(recipient, str):
            raise ValueError('invalid public contact opportunity')
        if recipient and (not recipient.isdigit() or recipient.startswith('0')):
            raise ValueError('invalid contact recipient')
        if len(source['url']) > 800:
            raise ValueError('public source URL exceeds sharing text budget')
        if chat.startswith('dm:') and recipient not in ('', chat[3:]):
            raise PermissionError('DM opportunity recipient differs from chat')
        recipient = chat[3:] if chat.startswith('dm:') else recipient
        now = self.clock()
        due = now if due_at is None else _number(due_at, now-1, now+7*86400, 'opportunity due')
        expires = min(source['expires'], due+86400) if expires_at is None else _number(expires_at, due+1, min(source['expires'], due+7*86400), 'opportunity expiry')
        reference = 'activity:'+_hash(chat, source['url'], source['digest'], reason, recipient)
        if self._consumed(reference):
            return reference
        with self.connection() as db:
            db.execute('''INSERT OR IGNORE INTO opportunities(reference,chat,source_id,recipient,reason,created,due,expires,state)
                          VALUES (?,?,?,?,?,?,?,?,'ready')''',
                       (reference, chat, source_id, recipient, reason, now, due, expires))
        return reference

    def _opportunity(self, chat, reference):
        self._chat(chat)
        with self.connection() as db:
            row = db.execute('SELECT * FROM opportunities WHERE chat=? AND reference=?', (chat, reference)).fetchone()
        if row is None:
            raise PermissionError('opportunity is outside this chat')
        self.source(chat, row['source_id'])
        return dict(row)

    def _offer_current(self, chat, reference, attempt=''):
        row = self._opportunity(chat, reference)
        now = self.clock()
        if (row['state'] not in {'ready', 'reserved'} or not row['due'] <= now < row['expires']
                or not self.strategy(chat)['sharing_enabled']):
            return False
        consumed = self._consumed(reference)
        if attempt:
            return bool(consumed and consumed['chat'] == chat and consumed['attempt'] == attempt
                        and consumed['state'] == 'reserved')
        return consumed is None and row['state'] == 'ready'

    def cancel_opportunity(self, chat, reference):
        self._opportunity(chat, reference)
        with self.connection(control=True) as db:
            old = db.execute('SELECT * FROM consumed WHERE chat=? AND reference=?', (chat, reference)).fetchone()
            if old is None:
                db.execute('INSERT INTO consumed(reference,chat,attempt,created,state) VALUES (?,?,\'\',?,\'cancelled\')',
                           (reference, chat, self.clock()))
            elif old['state'] == 'reserved':
                db.execute("UPDATE consumed SET state='cancelled' WHERE reference=?", (reference,))
        with self.connection() as db:
            db.execute("UPDATE opportunities SET state='cancelled' WHERE chat=? AND reference=?", (chat, reference))

    def next_opportunity(self, chat, *, excluded=frozenset()):
        self._chat(chat)
        with self.connection() as db:
            rows = db.execute("SELECT reference FROM opportunities WHERE chat=? AND state='ready' AND due<=? AND expires>? ORDER BY due,created LIMIT 128",
                              (chat, self.clock(), self.clock())).fetchall()
        for item in rows:
            reference = item['reference']
            if reference in excluded:
                continue
            try:
                if not self._offer_current(chat, reference):
                    continue
                row = self._opportunity(chat, reference)
                source = self.source(chat, row['source_id'])
            except PermissionError:
                continue
            title = (source['title'] or '一份公开资料')[:80]
            stamp = datetime.fromtimestamp(source['fetched'], timezone.utc).isoformat(timespec='seconds')
            prefix = '公开阅读摘录：'+title+'\n'
            suffix = '\n抓取时间：'+stamp+'\n来源：'+source['url']
            text = prefix+source['note'][:1000-len(prefix)-len(suffix)]+suffix
            return ActivityOpportunity(reference, chat, row['recipient'], text, row['expires'],
                                       row['source_id'], row['reason'], self, [''])
        return None

    def claim(self, chat, reference, attempt):
        if not isinstance(attempt, str) or not 1 <= len(attempt) <= 128 or not self._offer_current(chat, reference):
            raise PermissionError('public contact opportunity is not claimable')
        # 独立消费账本先落盘；内容库恢复不能让未知发送变为可重试。
        with self.connection(control=True) as db:
            if db.execute('SELECT 1 FROM consumed WHERE reference=?', (reference,)).fetchone():
                raise PermissionError('public contact opportunity already consumed')
            db.execute('INSERT INTO consumed(reference,chat,attempt,created,state) VALUES (?,?,?,?,\'reserved\')',
                       (reference, chat, attempt, self.clock()))
        with self.connection() as db:
            db.execute("UPDATE opportunities SET state='reserved' WHERE chat=? AND reference=?", (chat, reference))

    def finish(self, chat, reference, attempt, status):
        self._chat(chat)
        if status not in {'notified', 'unknown'}:
            raise ValueError('invalid public activity delivery status')
        with self.connection(control=True) as db:
            row = db.execute('SELECT * FROM consumed WHERE reference=? AND chat=? AND attempt=?', (reference, chat, attempt)).fetchone()
            if row is None:
                raise PermissionError('public activity delivery is not claimed')
            if status == 'notified' and not row['message_id']:
                raise PermissionError('verified delivery ID is required before notified')
            if row['state'] == 'reserved':
                db.execute('UPDATE consumed SET state=? WHERE reference=?', (status, reference))
        with self.connection() as db:
            db.execute("UPDATE opportunities SET state=? WHERE chat=? AND reference=? AND state='reserved'", (status, chat, reference))

    def record_delivery(self, chat, reference, message_id):
        self._chat(chat)
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
            raise ValueError('invalid verified delivery ID')
        if not self._consumed(reference):
            raise PermissionError('unclaimed delivery')
        try:
            self._opportunity(chat, reference)
        except PermissionError:
            self.block_delivery(chat, reference, message_id)
            return False
        with self.connection(control=True) as db:
            db.execute('UPDATE consumed SET message_id=? WHERE reference=? AND chat=?', (message_id, reference, chat))
        with self.connection() as db:
            db.execute('UPDATE opportunities SET message_id=? WHERE reference=? AND chat=?', (message_id, reference, chat))
        return True

    def block_delivery(self, chat, reference, message_id):
        self._chat(chat)
        with self.connection(control=True) as db:
            db.execute("UPDATE consumed SET state='unknown',message_id='' WHERE reference=? AND chat=?", (reference, chat))
        with self.connection() as db:
            db.execute("UPDATE opportunities SET state='unknown',message_id='' WHERE reference=? AND chat=?", (reference, chat))
        return False

    def record_feedback(self, chat, reference, status, *, actor='', message_id='', reply_to=None):
        exchange = reference.startswith('exchange:') if isinstance(reference, str) else False
        row = self._exchange(chat, reference) if exchange else self._opportunity(chat, reference)
        if status not in FEEDBACK:
            raise ValueError('invalid activity feedback')
        if row['recipient'] and actor and actor != row['recipient']:
            raise PermissionError('feedback actor differs from contact recipient')
        receipt = dict(message_id=row['message_id'], state='notified') if exchange else self._consumed(reference)
        if receipt is None:
            raise PermissionError('feedback has no contact attempt')
        if status in {'replied', 'declined', 'corrected'}:
            if not actor or not message_id or not receipt['message_id'] or str(reply_to) != receipt['message_id']:
                raise PermissionError('feedback must reference the verified delivery')
        elif status == 'no_response' and (self.clock() < row['expires'] or receipt['state'] != 'notified'):
            raise PermissionError('no-response feedback is premature or delivery unknown')
        if message_id:
            self._assert_messages_current(chat, [message_id])
        ident = _hash(chat, reference, actor, message_id, '' if message_id else status)
        with self.connection() as db:
            previous = db.execute('SELECT status FROM feedback WHERE id=? AND chat=?', (ident, chat)).fetchone()
            effective_status = status
            if previous is not None and previous['status'] != status:
                may_refine = (previous['status'] == 'activity_observed' and status in {'replied', 'declined', 'corrected'}
                              or previous['status'] == 'replied' and status in {'declined', 'corrected'})
                if may_refine:
                    db.execute('UPDATE feedback SET status=? WHERE id=? AND chat=?', (status, ident, chat))
                    if status in {'declined', 'corrected'}:
                        for trial in db.execute("SELECT id,evidence FROM trials WHERE chat=? AND state IN ('proposed','trial','retained')", (chat,)).fetchall():
                            if ident in json.loads(trial['evidence']):
                                db.execute("UPDATE trials SET state='reverted',evaluation='contradictory_feedback' WHERE id=?", (trial['id'],))
                else:
                    effective_status = previous['status']
            db.execute('INSERT OR IGNORE INTO feedback VALUES (?,?,?,?,?,?,?,?)',
                       (ident, chat, reference, reference if exchange else row['source_id'], status, actor, message_id, self.clock()))
            if effective_status != 'activity_observed':
                table = 'exchanges' if exchange else 'opportunities'
                db.execute('UPDATE '+table+' SET state=? WHERE reference=? AND chat=?', (effective_status, reference, chat))
            db.execute('''DELETE FROM feedback WHERE chat=? AND id NOT IN
                          (SELECT id FROM feedback WHERE chat=? ORDER BY created DESC,rowid DESC LIMIT 512)''',
                       (chat, chat))
        return ident

    def register_exchange(self, chat, message_id, *, recipient='', reply_to='', dependencies=()):
        """Caller has verified delivery; retain IDs only, never conversation text."""
        self._chat(chat)
        reply_to = '' if reply_to is None else reply_to
        identifiers = [message_id, *dependencies, *([reply_to] if reply_to else [])]
        if (not isinstance(message_id, str) or not message_id or len(dependencies) > 128
                or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in identifiers)
                or not isinstance(recipient, str) or recipient and (not recipient.isdigit() or recipient.startswith('0'))):
            raise ValueError('invalid verified social exchange')
        if chat.startswith('dm:') and recipient not in ('', chat[3:]):
            raise PermissionError('exchange recipient differs from DM')
        recipient = chat[3:] if chat.startswith('dm:') else recipient
        self._assert_messages_current(chat, identifiers)
        reference = 'exchange:'+_hash(chat, message_id)
        deps = json.dumps(sorted(set([*dependencies, *([reply_to] if reply_to else [])])))
        now = self.clock()
        with self.connection() as db:
            old = db.execute('SELECT * FROM exchanges WHERE reference=? AND chat=?', (reference, chat)).fetchone()
            if old is not None:
                if (old['message_id'], old['recipient'], old['reply_to'], old['dependencies'], old['revision']) != (message_id, recipient, reply_to, deps, self.revision):
                    raise PermissionError('exchange receipt scope changed')
                return reference
            db.execute('INSERT INTO exchanges VALUES (?,?,?,?,?,?,?,?,?,\'notified\')',
                       (reference, chat, message_id, recipient, reply_to, deps, self.revision, now, now+self.retention))
            db.execute('''DELETE FROM exchanges WHERE chat=? AND reference NOT IN
                          (SELECT reference FROM exchanges WHERE chat=? ORDER BY created DESC,rowid DESC LIMIT 512)''',
                       (chat, chat))
        return reference

    def _assert_messages_current(self, chat, identifiers):
        for ident in identifiers:
            if ident.startswith(PUBLIC_SOURCE_DEPENDENCY):
                self.source(chat, ident[len(PUBLIC_SOURCE_DEPENDENCY):])
        with self.connection(control=True) as db:
            if any(db.execute('SELECT 1 FROM revoked_messages WHERE chat=? AND message_id=?', (chat, ident)).fetchone()
                   for ident in identifiers if not ident.startswith(PUBLIC_SOURCE_DEPENDENCY)):
                raise PermissionError('social exchange source was revoked')

    def _exchange(self, chat, reference):
        self._chat(chat)
        with self.connection() as db:
            row = db.execute('SELECT * FROM exchanges WHERE chat=? AND reference=? AND revision=? AND expires>?',
                             (chat, reference, self.revision, self.clock())).fetchone()
        if row is None or row['state'] == 'revoked':
            raise PermissionError('social exchange is unavailable in this chat')
        self._assert_messages_current(chat, [row['message_id'], *json.loads(row['dependencies'])])
        return dict(row)

    def exchange_for_delivery(self, chat, message_id):
        self._chat(chat)
        reference = 'exchange:'+_hash(chat, str(message_id))
        self._exchange(chat, reference)
        return reference

    def forget_messages(self, chat, message_ids):
        self._chat(chat)
        message_ids = set(message_ids)
        if len(message_ids) > 1024 or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in message_ids):
            raise ValueError('invalid social source revocation')
        with self.connection() as db:
            rows = db.execute('SELECT reference,message_id,reply_to,dependencies FROM exchanges WHERE chat=? LIMIT 513', (chat,)).fetchall()
            if len(rows) > 512:
                raise ValueError('social source revocation capacity exceeded')
            dependencies = {row['reference']: {*json.loads(row['dependencies']), row['reply_to']}-{''} for row in rows}
            affected = set()
            while True:
                newly_affected = [row for row in rows if row['reference'] not in affected and
                                  (row['message_id'] in message_ids or dependencies[row['reference']].intersection(message_ids))]
                if not newly_affected:
                    break
                affected.update(row['reference'] for row in newly_affected)
                message_ids.update(row['message_id'] for row in newly_affected)
            # 派生消息的撤销先进入独立栅栏；恢复旧内容库也不能复活中间一跳。
            with self.connection(control=True) as controls:
                controls.executemany('INSERT OR IGNORE INTO revoked_messages VALUES (?,?,?)',
                                     ((chat, ident, self.clock()) for ident in message_ids))
            db.executemany("UPDATE exchanges SET state='revoked' WHERE reference=? AND chat=?",
                           ((reference, chat) for reference in affected))
            feedback_ids = {row['id'] for row in db.execute('SELECT id,reference,message_id FROM feedback WHERE chat=?', (chat,))
                            if row['reference'] in affected or row['message_id'] in message_ids}
            for trial in db.execute('SELECT id,evidence FROM trials WHERE chat=?', (chat,)).fetchall():
                if feedback_ids.intersection(json.loads(trial['evidence'])):
                    db.execute("UPDATE trials SET state='reverted',evaluation='evidence_revoked' WHERE id=?", (trial['id'],))
        return tuple(sorted(affected))

    def outbound_ids(self, chat, references):
        """Return only this chat's already revoked actual outgoing message IDs."""
        self._chat(chat)
        references = tuple(dict.fromkeys(references))
        if len(references) > 512 or any(not isinstance(ref, str) or not ref.startswith('exchange:') or len(ref) > 128 for ref in references):
            raise ValueError('invalid revoked social exchange references')
        with self.connection() as db:
            rows = [db.execute('SELECT message_id,state FROM exchanges WHERE reference=? AND chat=?',
                               (ref, chat)).fetchone() for ref in references]
        if any(row is None or row['state'] != 'revoked' for row in rows):
            raise PermissionError('revoked social exchange is outside this chat')
        return tuple(sorted({row['message_id'] for row in rows}))

    @staticmethod
    def _patch(patch):
        if not isinstance(patch, dict) or not patch or set(patch)-set(DEFAULT_STRATEGY):
            raise ValueError('invalid strategy trial patch')
        if 'cooldown_multiplier' in patch:
            _number(patch['cooldown_multiplier'], 1, 4, 'cooldown multiplier')
        if 'max_reply_chars' in patch and (type(patch['max_reply_chars']) is not int or not 160 <= patch['max_reply_chars'] <= 1000):
            raise ValueError('invalid reply length trial')
        if 'sharing_enabled' in patch and type(patch['sharing_enabled']) is not bool:
            raise ValueError('invalid sharing trial')
        return dict(patch)

    def _evidence(self, chat, identifiers, *, after=None):
        self._chat(chat)
        if not identifiers or len(identifiers) > 128 or len(set(identifiers)) != len(identifiers):
            raise ValueError('bounded distinct feedback evidence is required')
        results = []
        for ident in identifiers:
            with self.connection() as db:
                row = db.execute('SELECT * FROM feedback WHERE id=? AND chat=?', (ident, chat)).fetchone()
            if row is None or row['status'] in {'activity_observed', 'cancelled', 'closed_by_member'}:
                raise PermissionError('feedback is not usable trial evidence')
            if after is not None and row['created'] <= after:
                raise PermissionError('trial evaluation must use later feedback')
            if row['message_id']:
                self._assert_messages_current(chat, [row['message_id']])
            if row['source_id'].startswith('exchange:'):
                self._exchange(chat, row['source_id'])
            else:
                self.source(chat, row['source_id'])
            results.append(dict(row))
        return results

    def feedback_evidence(self, chat, *, after=None, limit=16):
        self._chat(chat)
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('invalid feedback evidence limit')
        if after is not None:
            _number(after, 0, self.clock()+5, 'feedback evidence cutoff')
        with self.connection() as db:
            rows = db.execute('''SELECT id FROM feedback WHERE chat=? AND created>?
                AND status IN ('replied','declined','corrected','no_response') ORDER BY created DESC,rowid DESC LIMIT 128''',
                              (chat, -1 if after is None else after)).fetchall()
        result = []
        for row in rows:
            try:
                self._evidence(chat, [row['id']], after=after)
            except PermissionError:
                continue
            result.append(row['id'])
            if len(result) >= limit:
                break
        return result

    def current_trials(self, chat):
        self._chat(chat)
        with self.connection() as db:
            rows = db.execute("SELECT id FROM trials WHERE chat=? AND state IN ('proposed','trial','retained') AND expires>? ORDER BY created DESC LIMIT 16",
                              (chat, self.clock())).fetchall()
        result = []
        for row in rows:
            trial = self.trial(chat, row['id'])
            try:
                self._evidence(chat, trial['evidence'])
            except PermissionError:
                self.revert_trial(chat, row['id'], reason='evidence_revoked')
                continue
            result.append(trial)
        return result

    def propose_trial(self, chat, patch, evidence_ids, *, days=7):
        self._chat(chat)
        patch = self._patch(patch)
        if len(evidence_ids) > 16:
            raise ValueError('strategy proposal evidence budget exceeded')
        self._evidence(chat, evidence_ids)
        if type(days) is not int or not 1 <= days <= 14:
            raise ValueError('invalid trial lifetime')
        ident = _hash(chat, patch, sorted(evidence_ids))
        with self.connection() as db:
            old = db.execute('SELECT 1 FROM trials WHERE id=? AND chat=?', (ident, chat)).fetchone()
            if old is None and db.execute('SELECT COUNT(*) FROM trials WHERE chat=?', (chat,)).fetchone()[0] >= 128:
                raise ValueError('strategy trial capacity reached')
            db.execute('''INSERT OR IGNORE INTO trials(id,chat,patch,evidence,baseline,state,created,expires)
                          VALUES (?,?,?,?,?,'proposed',?,?)''',
                       (ident, chat, json.dumps(patch, sort_keys=True), json.dumps(sorted(evidence_ids)), json.dumps(sorted(evidence_ids)),
                        self.clock(), self.clock()+days*86400))
        return ident

    def reflect(self, chat):
        """Form a conservative candidate from actual feedback, without a model."""
        self._chat(chat)
        with self.connection() as db:
            rows = db.execute("SELECT * FROM feedback WHERE chat=? AND status IN ('declined','corrected','no_response') ORDER BY created DESC LIMIT 16", (chat,)).fetchall()
        for status in ('declined', 'corrected', 'no_response'):
            candidates = [row for row in rows if row['status'] == status]
            usable = []
            for row in candidates:
                try:
                    self._evidence(chat, [row['id']])
                except PermissionError:
                    continue
                usable.append(row)
            if len({(row['reference'], row['message_id']) for row in usable}) < 2:
                continue
            row = usable[0]
            patch = ({'sharing_enabled': False} if row['status'] == 'declined' else
                     {'max_reply_chars': 280} if row['status'] == 'corrected' else
                     {'cooldown_multiplier': 2.0})
            try:
                ident = self.propose_trial(chat, patch, [item['id'] for item in usable[:4]])
            except PermissionError:
                continue
            with self.connection() as db:
                state = db.execute('SELECT state FROM trials WHERE id=?', (ident,)).fetchone()[0]
            if state == 'proposed':
                return ident
        return None

    def trial(self, chat, trial_id):
        self._chat(chat)
        with self.connection() as db:
            row = db.execute('SELECT * FROM trials WHERE chat=? AND id=?', (chat, trial_id)).fetchone()
        if row is None:
            raise PermissionError('strategy trial is outside this chat')
        result = dict(row)
        result['patch'], result['evidence'], result['baseline'] = json.loads(row['patch']), json.loads(row['evidence']), json.loads(row['baseline'])
        return result

    def start_trial(self, chat, trial_id, *, duration_seconds=None):
        row = self.trial(chat, trial_id)
        self._evidence(chat, row['evidence'])
        if row['state'] != 'proposed' or row['expires'] <= self.clock():
            raise ValueError('strategy trial is not startable')
        expires = row['expires']
        if duration_seconds is not None:
            expires = min(expires, self.clock()+_number(duration_seconds, 600, 604800, 'strategy trial duration'))
        with self.connection() as db:
            if db.execute("SELECT 1 FROM trials WHERE chat=? AND state='trial' AND expires>?", (chat, self.clock())).fetchone():
                raise ValueError('another strategy trial is active')
            db.execute("UPDATE trials SET state='trial',started=?,expires=? WHERE chat=? AND id=? AND state='proposed'", (self.clock(), expires, chat, trial_id))
        return self.strategy(chat)

    def strategy(self, chat):
        self._chat(chat)
        with self.connection() as db:
            db.execute("UPDATE trials SET state='reverted',evaluation='expired' WHERE chat=? AND state='trial' AND expires<=?", (chat, self.clock()))
            rows = db.execute("SELECT * FROM trials WHERE chat=? AND state IN ('trial','retained') AND expires>? ORDER BY started DESC,created DESC", (chat, self.clock())).fetchall()
        for row in rows:
            try:
                self._evidence(chat, json.loads(row['evidence']))
            except (PermissionError, ValueError):
                self.revert_trial(chat, row['id'], reason='evidence_revoked')
                continue
            return {**DEFAULT_STRATEGY, **self._patch(json.loads(row['patch'])), 'trial_id': row['id'], 'state': row['state']}
        return {**DEFAULT_STRATEGY, 'trial_id': '', 'state': 'baseline'}

    def evaluate_trial(self, chat, trial_id, evidence_ids):
        trial = self.trial(chat, trial_id)
        if trial['state'] not in {'trial', 'retained'} or trial['expires'] <= self.clock():
            raise ValueError('strategy trial is not active')
        baseline = self._evidence(chat, trial['baseline'])
        observed = self._evidence(chat, evidence_ids, after=trial['started'])
        if any(row['status'] in {'declined', 'corrected', 'no_response'} for row in observed):
            state, reason = 'reverted', 'contradictory_feedback'
        elif trial['state'] == 'retained':
            state, reason = 'retained', trial['evaluation']
        elif len({row['message_id'] for row in observed if row['status'] == 'replied'}) >= 2:
            old_rate = sum(row['status'] == 'replied' for row in baseline)/len(baseline)
            new_rate = sum(row['status'] == 'replied' for row in observed)/len(observed)
            state, reason = ('retained', 'observed_improvement') if new_rate > old_rate else ('trial', 'inconclusive')
        else:
            state, reason = 'trial', 'insufficient_followup'
        all_evidence = sorted(set([*trial['evidence'], *evidence_ids]))
        if len(all_evidence) > 128:
            self.revert_trial(chat, trial_id, reason='evidence_capacity')
            return dict(state='reverted', reason='evidence_capacity', baseline_count=len(baseline), followup_count=len(observed))
        with self.connection() as db:
            db.execute('UPDATE trials SET state=?,evidence=?,evaluation=? WHERE chat=? AND id=?',
                       (state, json.dumps(all_evidence), reason, chat, trial_id))
        return dict(state=state, reason=reason, baseline_count=len(baseline), followup_count=len(observed))

    def revert_trial(self, chat, trial_id, *, reason='manual'):
        self.trial(chat, trial_id)
        if reason not in {'manual', 'evidence_revoked', 'contradictory_feedback', 'expired', 'evidence_capacity'}:
            raise ValueError('invalid strategy rollback reason')
        with self.connection() as db:
            db.execute("UPDATE trials SET state='reverted',evaluation=? WHERE chat=? AND id=?", (reason, chat, trial_id))

    def forget_source(self, chat, source_id):
        self._chat(chat)
        with self.connection() as db:
            row = db.execute('SELECT 1 FROM sources WHERE chat=? AND id=?', (chat, source_id)).fetchone()
            dependency = PUBLIC_SOURCE_DEPENDENCY+source_id
            affected_messages = {exchange['message_id'] for exchange in db.execute(
                'SELECT message_id,dependencies FROM exchanges WHERE chat=?', (chat,)).fetchall()
                if dependency in json.loads(exchange['dependencies'])}
        if row is None and not self._revoked(chat, source_id):
            raise PermissionError('public source is outside this chat')
        affected_exchanges = self.forget_messages(chat, affected_messages)
        affected_messages = self.outbound_ids(chat, affected_exchanges)
        with self.connection(control=True) as db:
            db.execute('INSERT OR IGNORE INTO revoked_sources VALUES (?,?,?)', (chat, source_id, self.clock()))
        with self.connection() as db:
            feedback_ids = {row['id'] for row in db.execute('SELECT id,source_id,reference FROM feedback WHERE chat=?', (chat,))
                            if row['source_id'] == source_id or row['reference'] in affected_exchanges}
            for trial in db.execute('SELECT id,evidence FROM trials WHERE chat=?', (chat,)).fetchall():
                if feedback_ids.intersection(json.loads(trial['evidence'])):
                    db.execute("UPDATE trials SET state='reverted',evaluation='evidence_revoked' WHERE id=?", (trial['id'],))
            db.execute('DELETE FROM sources WHERE chat=? AND id=?', (chat, source_id))
            db.execute('DELETE FROM feedback WHERE chat=? AND source_id=?', (chat, source_id))
        return tuple(sorted(affected_messages))

    def activity(self, chat, activity_id):
        self._chat(chat)
        with self.connection() as db:
            row = db.execute('SELECT * FROM activities WHERE chat=? AND id=?', (chat, activity_id)).fetchone()
        if row is None:
            raise PermissionError('activity is outside this chat')
        return dict(row)

    def prune(self):
        now = self.clock()
        with self.connection() as db:
            db.execute('DELETE FROM sources WHERE expires<=?', (now,))
            db.execute('DELETE FROM feedback WHERE created<=?', (now-self.retention,))
            db.execute('DELETE FROM exchanges WHERE expires<=?', (now,))
            db.execute('''DELETE FROM activities WHERE state IN ('failed','expired','cancelled') AND expires<=?''', (now,))
            db.execute('''DELETE FROM activities WHERE created<=? AND state!='running'
                          AND NOT EXISTS (SELECT 1 FROM sources WHERE activity_id=activities.id AND expires>?)''',
                       (now-self.retention, now))
            db.execute('DELETE FROM trials WHERE expires<=? AND state!=\'trial\'', (now-self.retention,))

    def close(self):
        self.active = False
