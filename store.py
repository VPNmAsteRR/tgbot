"""Small SQLite journal for the relay bot.

No media is stored here — only what was relayed, its status, muted chats and
the update offset. Files live in a temp dir for the duration of one send.
"""

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS relayed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    chat_title  TEXT,
    sender      TEXT,
    kind        TEXT,
    status      TEXT NOT NULL,
    error       TEXT,
    ts          INTEGER NOT NULL,
    UNIQUE (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_relayed_status ON relayed (status);
CREATE INDEX IF NOT EXISTS idx_relayed_ts ON relayed (ts);

CREATE TABLE IF NOT EXISTS muted (
    chat_id  INTEGER PRIMARY KEY,
    title    TEXT,
    ts       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


class Store:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def begin(self, chat_id, message_id, title, sender, kind):
        """Reserve the message. Returns False when it was already handled."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO relayed "
            "(chat_id, message_id, chat_title, sender, kind, status, ts) "
            "VALUES (?,?,?,?,?,'pending',?)",
            (chat_id, message_id, title, sender, kind, int(time.time())),
        )
        self.db.commit()
        return cur.rowcount > 0

    def finish(self, chat_id, message_id, status, error=None):
        self.db.execute(
            "UPDATE relayed SET status=?, error=? WHERE chat_id=? AND message_id=?",
            (status, (error or "")[:500] or None, chat_id, message_id),
        )
        self.db.commit()

    def forget(self, chat_id, message_id):
        self.db.execute(
            "DELETE FROM relayed WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        )
        self.db.commit()

    def status(self, chat_id, message_id):
        row = self.db.execute(
            "SELECT status FROM relayed WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        return row["status"] if row else None

    def stats(self):
        rows = self.db.execute(
            "SELECT status, COUNT(*) AS n FROM relayed GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        fin = self.db.execute(
            "SELECT MIN(ts) AS first, MAX(ts) AS last FROM relayed WHERE status='sent'"
        ).fetchone()
        return {
            "total": total,
            "by_status": by_status,
            "first": fin["first"],
            "last": fin["last"],
            "muted": len(self.muted_list()),
        }

    # ------------------------------------------------------------------ muted
    def mute(self, chat_id, title=None):
        self.db.execute(
            "INSERT OR REPLACE INTO muted (chat_id, title, ts) VALUES (?,?,?)",
            (chat_id, title, int(time.time())),
        )
        self.db.commit()

    def unmute(self, chat_id):
        self.db.execute("DELETE FROM muted WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def is_muted(self, chat_id):
        return (
            self.db.execute("SELECT 1 FROM muted WHERE chat_id=?", (chat_id,)).fetchone()
            is not None
        )

    def muted_list(self):
        return self.db.execute("SELECT * FROM muted ORDER BY ts").fetchall()

    # ------------------------------------------------------------------- meta
    def meta_get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key, value):
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.db.commit()
