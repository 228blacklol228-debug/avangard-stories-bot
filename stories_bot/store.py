import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

def dump(v): return json.dumps(v, ensure_ascii=False)

class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0, brand TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS series (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL,
          topic INTEGER NOT NULL, owner INTEGER NOT NULL, project TEXT NOT NULL, brand TEXT NOT NULL,
          revision INTEGER NOT NULL, draft TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS revisions (series INTEGER, revision INTEGER, draft TEXT NOT NULL,
          PRIMARY KEY(series,revision));
        CREATE TABLE IF NOT EXISTS messages (chat INTEGER, message INTEGER, series INTEGER,
          slide INTEGER, PRIMARY KEY(chat,message));
        CREATE TABLE IF NOT EXISTS inbox (id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS usage (chat INTEGER, day TEXT, count INTEGER,
          PRIMARY KEY(chat,day));
        CREATE TABLE IF NOT EXISTS operations (update_id INTEGER PRIMARY KEY, series INTEGER, revision INTEGER);
        ''')
        self.db.commit()

    def kv(self,key,default=None):
        row=self.db.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def setkv(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(key,dump(value)));self.db.commit()

    def brand(self,chat,default):
        row=self.db.execute('SELECT brand FROM chats WHERE id=?',(chat,)).fetchone()
        return json.loads(row[0]) if row else json.loads(dump(default))

    def setbrand(self,chat,brand):
        self.db.execute('INSERT INTO chats(id,brand) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET brand=excluded.brand',
                        (chat,dump(brand)));self.db.commit()

    def enable(self,chat,default):
        self.setbrand(chat,self.brand(chat,default))
        self.db.execute('UPDATE chats SET enabled=1 WHERE id=?',(chat,));self.db.commit()

    def enabled(self,chat):
        row=self.db.execute('SELECT enabled FROM chats WHERE id=?',(chat,)).fetchone()
        return bool(row and row[0])

    def create(self,chat,topic,owner,project,brand,draft,update_id=None):
        cur=self.db.execute('INSERT INTO series(chat,topic,owner,project,brand,revision,draft,created) VALUES (?,?,?,?,?,1,?,?)',
                            (chat,topic,owner,dump(project),dump(brand),dump(draft),time.time()))
        sid=cur.lastrowid
        self.db.execute('INSERT INTO revisions VALUES (?,1,?)',(sid,dump(draft)))
        if update_id is not None:self.db.execute('INSERT INTO operations VALUES (?,?,1)',(update_id,sid))
        self.db.commit()
        return self.get(sid,chat,topic)

    def get(self,sid,chat,topic):
        row=self.db.execute('SELECT * FROM series WHERE id=? AND chat=? AND topic=?',(sid,chat,topic)).fetchone()
        if not row:return None
        d=dict(row)
        for k in ('draft','brand','project'):d[k]=json.loads(d[k])
        return d

    def recent(self,chat,topic):
        return self.db.execute('SELECT id,owner FROM series WHERE chat=? AND topic=? ORDER BY id DESC LIMIT 20',
                               (chat,topic)).fetchall()

    def revise(self,series,draft,update_id=None):
        rev=series['revision']+1
        self.db.execute('UPDATE series SET revision=?,draft=? WHERE id=?',(rev,dump(draft),series['id']))
        self.db.execute('INSERT INTO revisions VALUES (?,?,?)',(series['id'],rev,dump(draft)))
        if update_id is not None:self.db.execute('INSERT INTO operations VALUES (?,?,?)',(update_id,series['id'],rev))
        self.db.commit()

    def operation(self,uid,chat,topic):
        row=self.db.execute('SELECT series,revision FROM operations WHERE update_id=?',(uid,)).fetchone()
        if not row:return None
        series=self.get(row['series'],chat,topic)
        if not series:return None
        rev=self.db.execute('SELECT draft FROM revisions WHERE series=? AND revision=?',(row['series'],row['revision'])).fetchone()
        series['draft']=json.loads(rev[0]);series['revision']=row['revision']
        return series

    def link(self,chat,message,sid,slide=None):
        self.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?)',(chat,message,sid,slide));self.db.commit()

    def linked(self,chat,message):
        row=self.db.execute('SELECT series,slide FROM messages WHERE chat=? AND message=?',(chat,message)).fetchone()
        return dict(row) if row else None

    def use(self,chat,limit):
        day=datetime.now(timezone.utc).date().isoformat()
        row=self.db.execute('SELECT count FROM usage WHERE chat=? AND day=?',(chat,day)).fetchone()
        if row and row[0]>=limit:return False
        self.db.execute('INSERT INTO usage VALUES (?,?,1) ON CONFLICT(chat,day) DO UPDATE SET count=count+1',(chat,day))
        self.db.commit();return True

    def enqueue(self,update):
        self.db.execute('INSERT OR IGNORE INTO inbox(id,payload) VALUES (?,?)',(update['update_id'],dump(update)))
        self.db.commit()

    def pending(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM inbox WHERE status='pending' ORDER BY id")]

    def finish(self,uid):
        self.db.execute("UPDATE inbox SET status='done',payload='{}' WHERE id=?",(uid,));self.db.commit()
