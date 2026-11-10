"""Durable requests and outbox. SQLite deployment: one application worker."""
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

log = logging.getLogger(__name__)

class Conflict(Exception): 
    pass

class Storage:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        
        # Підключення для запису (використовує блокування)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=20)
        
        # Підключення лише для читання (без блокувань, використовує WAL)
        self.read_db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=10)
        
        if os.name == 'posix':
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
                
        self.db.row_factory = sqlite3.Row
        self.read_db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA busy_timeout=20000')
        self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS conversations(
            session TEXT PRIMARY KEY,
            messages TEXT NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated DESC);
        
        CREATE TABLE IF NOT EXISTS leads(
            id TEXT PRIMARY KEY,
            session TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            payload TEXT NOT NULL,
            created REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'new',
            notify_state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0,
            claimed REAL,
            claim_token TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS lead_outbox ON leads(notify_state, next_attempt);
        CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created DESC);
        
        CREATE TABLE IF NOT EXISTS rate_limits(
            key TEXT NOT NULL,
            window INTEGER NOT NULL,
            n INTEGER NOT NULL,
            PRIMARY KEY(key, window)
        );
        CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY,
            created REAL NOT NULL,
            action TEXT NOT NULL,
            lead TEXT
        );
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException as e:
                self.db.execute('ROLLBACK')
                log.error(f"Transaction failed, rolled back: {e}")
                raise

    def history(self, session):
        """Читання історії (не блокує запис завдяки WAL)."""
        row = self.read_db.execute('SELECT messages FROM conversations WHERE session=?', (session,)).fetchone()
        if not row:
            return []
        try:
            return json.loads(row['messages'])
        except (json.JSONDecodeError, TypeError) as e:
            log.warning(f"Corrupted history for session {session}: {e}")
            return []

    def save_history(self, session, messages):
        with self.lock:
            # Збільшено глибину історії до 40 повідомлень для комерційної консультації
            dump = json.dumps(messages[-40:], ensure_ascii=False)
            self.db.execute(
                'INSERT OR REPLACE INTO conversations(session, messages, updated) VALUES(?, ?, ?)',
                (session, dump, time.time())
            )

    def delete_history(self, session):
        with self.lock:
            self.db.execute('DELETE FROM conversations WHERE session=?', (session,))

    def list_dialogs(self, limit=50, offset=0):
        """Отримання списку діалогів через read_db (щоб не блокувати Event Loop)."""
        count = self.read_db.execute('SELECT count(*) FROM conversations').fetchone()[0]
        rows = self.read_db.execute(
            'SELECT session, messages, updated FROM conversations ORDER BY updated DESC LIMIT ? OFFSET ?',
            (limit, offset)
        ).fetchall()
        
        dialogs = []
        for r in rows:
            try:
                msgs = json.loads(r['messages'])
            except Exception:
                msgs = []
            dialogs.append({
                'session': r['session'],
                'updated': r['updated'],
                'messages': msgs
            })
        return {'total': count, 'dialogs': dialogs}

    def allow(self, rules):
        now = time.time()
        with self.transaction():
            buckets = [(key, int(now // seconds) * seconds, limit) for key, limit, seconds in rules]
            for key, window, limit in buckets:
                row = self.db.execute('SELECT n FROM rate_limits WHERE key=? AND window=?', (key, window)).fetchone()
                if row and row['n'] >= limit:
                    return False
            for key, window, _ in buckets:
                self.db.execute(
                    'INSERT INTO rate_limits VALUES(?, ?, 1) ON CONFLICT(key, window) DO UPDATE SET n=n+1',
                    (key, window)
                )
        return True

    def existing(self, request_id, session, fingerprint):
        """Перевірка дублів лідів через read_db."""
        row = self.read_db.execute('SELECT session, fingerprint FROM leads WHERE id=?', (request_id,)).fetchone()
        if not row:
            return False
        if row['session'] != session or row['fingerprint'] != fingerprint:
            raise Conflict()
        return True

    def create_lead(self, request_id, session, fingerprint, payload):
        with self.transaction():
            # Перевіряємо ще раз всередині транзакції для уникнення race condition
            row = self.db.execute('SELECT session, fingerprint FROM leads WHERE id=?', (request_id,)).fetchone()
            if row:
                if row['session'] != session or row['fingerprint'] != fingerprint:
                    raise Conflict()
                return False
                
            self.db.execute(
                'INSERT INTO leads(id, session, fingerprint, payload, created) VALUES(?, ?, ?, ?, ?)',
                (request_id, session, fingerprint, json.dumps(payload, ensure_ascii=False), time.time())
            )
            self.db.execute('INSERT INTO audit(created, action, lead) VALUES(?, ?, ?)', (time.time(), 'lead_created', request_id))
        return True

    def claim_notification(self):
        now = time.time()
        with self.transaction():
            self.db.execute(
                "UPDATE leads SET notify_state='pending', claim_token=NULL WHERE notify_state='sending' AND claimed<?",
                (now - 120,)
            )
            row = self.db.execute(
                "SELECT * FROM leads WHERE notify_state='pending' AND next_attempt<=? ORDER BY created LIMIT 1",
                (now,)
            ).fetchone()
            if not row:
                return None
                
            token = str(uuid.uuid4())
            self.db.execute(
                "UPDATE leads SET notify_state='sending', claimed=?, claim_token=?, attempts=attempts+1 WHERE id=?",
                (now, token, row['id'])
            )
            return dict(row) | {
                'payload': json.loads(row['payload']),
                'claim_token': token,
                'attempts': row['attempts'] + 1
            }

    def finish_notification(self, lead, ok, error=None):
        attempts = lead['attempts']
        state = 'sent' if ok else ('needs_review' if attempts >= 8 else 'pending')
        with self.transaction():
            self.db.execute(
                'UPDATE leads SET notify_state=?, next_attempt=?, error=?, claim_token=NULL WHERE id=? AND claim_token=?',
                (state, time.time() + min(3600, 10 * 2**attempts), error, lead['id'], lead['claim_token'])
            )

    def list_leads(self, state=None, limit=50, offset=0):
        where, args = (' WHERE state=?', [state]) if state else ('', [])
        count = self.read_db.execute('SELECT count(*) FROM leads' + where, args).fetchone()[0]
        rows = self.read_db.execute(
            'SELECT * FROM leads' + where + ' ORDER BY created DESC LIMIT ? OFFSET ?',
            args + [limit, offset]
        ).fetchall()
        return {
            'total': count,
            'leads': [
                {k: r[k] for k in ('id', 'created', 'state', 'notify_state', 'attempts', 'error')} |
                {'details': json.loads(r['payload'])}
                for r in rows
            ]
        }

    def set_state(self, request_id, state):
        with self.transaction():
            changed = self.db.execute('UPDATE leads SET state=? WHERE id=?', (state, request_id)).rowcount
            if changed:
                self.db.execute('INSERT INTO audit(created, action, lead) VALUES(?, ?, ?)', (time.time(), 'state:' + state, request_id))
            return changed

    def stats(self):
        return dict(self.read_db.execute('SELECT notify_state, count(*) FROM leads GROUP BY notify_state').fetchall())

    def cleanup(self, history_days, lead_days):
        now = time.time()
        with self.transaction():
            self.db.execute('DELETE FROM conversations WHERE updated<?', (now - history_days * 86400,))
            self.db.execute('DELETE FROM leads WHERE created<?', (now - lead_days * 86400,))
            self.db.execute('DELETE FROM audit WHERE created<?', (now - lead_days * 86400,))
            self.db.execute('DELETE FROM rate_limits WHERE window<?', (now - 2 * 86400,))
            self.db.execute('PRAGMA incremental_vacuum(500)') # Очищення фрагментації файлу БД

    def backup(self, target):
        with self.lock, sqlite3.connect(target) as destination:
            self.db.backup(destination)

    def close(self):
        with self.lock:
            self.read_db.close()
            self.db.close()
