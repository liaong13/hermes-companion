"""Durable contact consumption and feedback; metadata only, no model calls."""
from __future__ import annotations

from contextlib import contextmanager
import os
import sqlite3
import time


class ContactLedger:
    def __init__(self,path):
        self.path = path
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        for item in (path.parent,path):
            if item.is_symlink() or item.exists() and (item.stat().st_uid!=os.getuid() or item.stat().st_mode&0o077):
                raise ValueError('contact ledger must be private and owner-only')
        fd = os.open(path,os.O_CREAT|os.O_RDWR,0o600)
        os.close(fd)
        with self.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS budgets (
                chat TEXT PRIMARY KEY, day TEXT NOT NULL, daily INTEGER NOT NULL,
                last_attempt REAL NOT NULL, unanswered INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, chat TEXT NOT NULL, kind TEXT NOT NULL,
                reference TEXT NOT NULL UNIQUE, recipient TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, status TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '',
                feedback_actor TEXT NOT NULL DEFAULT '', feedback_message TEXT NOT NULL DEFAULT '')''')

    @contextmanager
    def connection(self):
        db = None
        try:
            db = sqlite3.connect(self.path,timeout=10)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            raise ValueError('contact ledger unavailable') from exc
        finally:
            if db is not None:
                db.close()

    def restore(self,states,*,expire=False):
        changes = []
        with self.connection() as db:
            if expire:
                db.execute("UPDATE attempts SET status='no_response' WHERE expires<=? AND status='acknowledged'",
                           (time.time(),))
            for chat,state in states.items():
                row = db.execute('SELECT * FROM budgets WHERE chat=?',(chat,)).fetchone()
                if row is None:
                    continue
                if state.day==row['day']: state.daily=max(state.daily,row['daily'])
                elif row['last_attempt']>=state.last_attempt: state.day,state.daily=row['day'],row['daily']
                if row['last_attempt']>=state.last_attempt:
                    state.last_attempt,state.unanswered=row['last_attempt'],bool(row['unanswered'])
            if expire:
                # Replaying a stable no-response record also repairs a crash
                # between expiry and publishing feedback into the activity DB.
                changes = [(r['chat'], r['reference'], r['recipient'], 'no_response') for r in db.execute(
                    "SELECT chat,reference,recipient FROM attempts WHERE status='no_response' ORDER BY created DESC LIMIT 128")
                    if r['chat'] in states]
        return changes

    def contains(self,reference):
        with self.connection() as db:
            return db.execute('SELECT 1 FROM attempts WHERE reference=?',(reference,)).fetchone() is not None

    def references(self,chat):
        with self.connection() as db:
            return frozenset(r[0] for r in db.execute('SELECT reference FROM attempts WHERE chat=?',(chat,)))

    def reserve(self,chat,attempt,*,kind,reference,recipient,expires,day,limit,interval,state):
        now=time.time()
        with self.connection() as db:
            row=db.execute('SELECT * FROM budgets WHERE chat=?',(chat,)).fetchone()
            daily=state.daily if state.day==day else 0
            last=state.last_attempt
            unanswered=state.unanswered
            if row is not None:
                daily=max(daily,row['daily'] if row['day']==day else 0)
                last=max(last,row['last_attempt'])
                unanswered=unanswered or bool(row['unanswered'])
            if (kind not in {'idle','agreement','activity','dialogue'} or daily>=limit or unanswered or now-last<interval
                    or db.execute('SELECT 1 FROM attempts WHERE reference=?',(reference,)).fetchone() is not None):
                raise ValueError('contact budget or one-shot reference consumed')
            db.execute('INSERT INTO attempts(id,chat,kind,reference,recipient,created,expires,status) VALUES (?,?,?,?,?,?,?,?)',
                       (attempt,chat,kind,reference,recipient,now,expires,'reserved'))
            db.execute('INSERT OR REPLACE INTO budgets VALUES (?,?,?,?,1)',(chat,day,daily+1,now))
        state.day,state.daily,state.last_attempt,state.unanswered=day,daily+1,now,True

    def finish(self,attempt,receipt):
        with self.connection() as db:
            db.execute('''UPDATE attempts SET status=CASE WHEN status='reserved' THEN ? ELSE status END,
                message_id=? WHERE id=?''',('acknowledged' if receipt else 'unknown',receipt or '',attempt))

    def activity(self,chat,actor,message_id,reply_to,*,decline=False,reference=None,closure=None):
        changes=[]
        with self.connection() as db:
            rows=db.execute('''SELECT * FROM attempts WHERE chat=? AND status NOT IN
                ('replied','declined','cancelled','closed_by_member') AND (recipient='' OR recipient=?)''',(chat,actor)).fetchall()
            for row in rows:
                if reference is not None:
                    if row['reference']!=reference: continue
                    status=closure
                elif row['message_id'] and str(reply_to)==row['message_id']:
                    status='declined' if decline else 'replied'
                elif row['status'] in {'reserved','acknowledged','unknown','no_response'}:
                    status='activity_observed'
                else:
                    continue
                if status not in {'declined','replied','activity_observed','cancelled','closed_by_member'}:
                    raise ValueError('invalid contact feedback')
                db.execute('UPDATE attempts SET status=?,feedback_actor=?,feedback_message=? WHERE id=?',
                           (status,actor,message_id,row['id']))
                changes.append((row['reference'],row['recipient'],status))
            pending=db.execute('''SELECT 1 FROM attempts WHERE chat=? AND status IN
                ('reserved','acknowledged','unknown','no_response') LIMIT 1''',(chat,)).fetchone()
            db.execute('UPDATE budgets SET unanswered=? WHERE chat=?',(int(pending is not None),chat))
        return changes,pending is not None

    def expire(self,chat):
        with self.connection() as db:
            db.execute("UPDATE attempts SET status='no_response' WHERE chat=? AND expires<=? AND status='acknowledged'",
                       (chat,time.time()))

    def rows(self,chat):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM attempts WHERE chat=? ORDER BY created DESC LIMIT 32',(chat,))]
