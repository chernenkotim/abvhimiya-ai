"""Durable single-node storage. For horizontal scaling use PostgreSQL + Redis."""
import sqlite3
import threading
import time
import json
from pathlib import Path

class Storage:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.conn=sqlite3.connect(self.path,check_same_thread=False,timeout=10)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=5000')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, history TEXT NOT NULL DEFAULT '[]', updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS leads(id TEXT PRIMARY KEY, created REAL NOT NULL, state TEXT NOT NULL, name TEXT NOT NULL,
           phone TEXT NOT NULL, product_id TEXT, product_name TEXT, price REAL, quantity INTEGER DEFAULT 1, note TEXT DEFAULT '',
           crm_ref TEXT, last_error TEXT, telegram_sent INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, session TEXT NOT NULL,
           kind TEXT NOT NULL, user_text TEXT, bot_text TEXT);
        ''')
        self.conn.commit()

    def history(self,sid):
        with self.lock:
            row=self.conn.execute('SELECT history FROM sessions WHERE id=?',(sid,)).fetchone()
            return json.loads(row['history']) if row else []

    def save_history(self,sid,h):
        with self.lock,self.conn:
            self.conn.execute('INSERT INTO sessions(id,history,updated) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET history=excluded.history, updated=excluded.updated',(sid,json.dumps(h[-12:],ensure_ascii=False),time.time()))

    def create_lead(self,lead):
        with self.lock,self.conn:
            cur=self.conn.execute('INSERT OR IGNORE INTO leads(id,created,state,name,phone,product_id,product_name,price,quantity,note) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (lead['id'],time.time(),'pending',lead['name'],lead['phone'],lead.get('product_id'),lead.get('product_name'),lead.get('price'),lead.get('quantity',1),lead.get('note','')))
            return cur.rowcount>0

    def update_lead(self,lid,**attrs):
        allow={'state','crm_ref','last_error','telegram_sent'}
        attrs={k:v for k,v in attrs.items() if k in allow}
        if not attrs:return
        with self.lock,self.conn:
            self.conn.execute(f"UPDATE leads SET {','.join(k+'=?' for k in attrs)} WHERE id=?",(*attrs.values(),lid))

    def lead(self,lid):
        with self.lock:
            row=self.conn.execute('SELECT * FROM leads WHERE id=?',(lid,)).fetchone()
            return dict(row) if row else None

    def pending(self,limit=25):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM leads WHERE state IN ('pending','retry','needs_review') ORDER BY created LIMIT ?",(limit,)).fetchall()]

    def log(self,sid,kind,user,bot):
        with self.lock,self.conn:
            self.conn.execute('INSERT INTO logs(created,session,kind,user_text,bot_text) VALUES (?,?,?,?,?)',(time.time(),sid,kind,user[:1500],bot[:1500]))

    def logs(self,limit=150):
        with self.lock:
            return [dict(r) for r in self.conn.execute('SELECT * FROM logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]

    def clean(self,days=30):
        cutoff=time.time()-days*86400
        with self.lock,self.conn:
            self.conn.execute('DELETE FROM logs WHERE created < ?',(cutoff,))
            self.conn.execute('DELETE FROM sessions WHERE updated < ?',(time.time()-4*3600,))
            # Retain leads for business handling, purge via documented data retention policy manually

    def close(self):self.conn.close()
