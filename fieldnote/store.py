"""SQLite owns the visible session index and saved traces in both modes."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

STOP_WORDS = set("""
a about after again all am an and any are as at be been before but by can could did do
does for from had has have how i if in into is it its last me my of on or our please
recall remember s should tell than that the their them then there these they this those
to us was we were what when where which who why will with would you your
""".split())


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def words(text):
    return {word for word in re.findall(r"[^\W_]+", text.casefold())
            if len(word) > 1 and word not in STOP_WORDS}


class Store:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, agent TEXT NOT NULL,
                    created TEXT NOT NULL, remote_id TEXT
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    question TEXT NOT NULL, answer TEXT NOT NULL, created TEXT NOT NULL,
                    captured INTEGER NOT NULL, trace TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, text TEXT NOT NULL, kind TEXT NOT NULL,
                    created TEXT NOT NULL, session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL
                );
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def sessions(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM sessions ORDER BY created DESC, rowid DESC"
            )]

    def session(self, session_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def create_session(self, title, agent):
        session = dict(id=uuid4().hex, title=title, agent=agent, created=now(), remote_id=None)
        with self.db() as db:
            db.execute(
                "INSERT INTO sessions VALUES (:id, :title, :agent, :created, :remote_id)",
                session,
            )
        return session

    def set_remote_id(self, session_id, remote_id):
        with self.db() as db:
            db.execute("UPDATE sessions SET remote_id=? WHERE id=?", (remote_id, session_id))

    def turns(self, session_id):
        with self.db() as db:
            rows = db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY rowid", (session_id,))
            return [dict(row) | {"trace": json.loads(row["trace"])} for row in rows]

    def save_turn(self, session, turn_id, question, answer, trace, captured):
        turn = dict(id=turn_id, session_id=session["id"], question=question,
                    answer=answer, created=now(), captured=int(captured), trace=trace)
        with self.db() as db:
            db.execute("""
                INSERT INTO turns VALUES (:id, :session_id, :question, :answer,
                                          :created, :captured, :trace)
            """, turn | {"trace": json.dumps(trace)})
        return turn

    def delete_session(self, session_id):
        with self.db() as db:
            # Explicit memories remain after their originating chat is removed.
            db.execute("DELETE FROM turns WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def memory_rows(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT m.*, s.title AS session_title, s.agent FROM memories m
                LEFT JOIN sessions s ON s.id=m.session_id ORDER BY m.created DESC, m.rowid DESC
            """)]

    def save_memory(self, session, text, kind):
        memory = dict(id=uuid4().hex, text=text, kind=kind, created=now(), session_id=session["id"])
        with self.db() as db:
            db.execute(
                "INSERT INTO memories VALUES (:id, :text, :kind, :created, :session_id)", memory
            )
        return memory | {"session_title": session["title"], "agent": session["agent"]}

    def delete_memory(self, memory_id):
        with self.db() as db:
            return bool(db.execute("DELETE FROM memories WHERE id=?", (memory_id,)).rowcount)

    def conversation_rows(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT t.id, t.question AS text, t.created, t.session_id,
                       s.title AS session_title, s.agent
                FROM turns t JOIN sessions s ON s.id=t.session_id
                WHERE t.captured=1 ORDER BY t.created DESC, t.rowid DESC
            """)]
