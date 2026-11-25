import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    streak INTEGER NOT NULL DEFAULT 0,
    last_active_date TEXT,
    tasks_done INTEGER NOT NULL DEFAULT 0,
    on_time_done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    difficulty TEXT NOT NULL DEFAULT 'normal',
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    postpones INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    points_awarded INTEGER
);
CREATE TABLE IF NOT EXISTS user_badges (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge TEXT NOT NULL,
    earned_at TEXT NOT NULL,
    PRIMARY KEY (user_id, badge)
);
"""


def _row_to_dict(row):
    if row is None or isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            pass
    if hasattr(row, "model_dump"):
        return row.model_dump()
    if hasattr(row, "__dict__"):
        return dict(vars(row))
    return dict(row)


class D1Statement:
    def __init__(self, d1, sql):
        self._d1 = d1
        self._sql = sql
        self._params = ()

    def bind(self, *params):
        self._params = params
        return self

    def _prepared(self):
        return self._d1.prepare(self._sql).bind(*self._params)

    async def all(self):
        result = await self._prepared().all()
        return [_row_to_dict(r) for r in result.results]

    async def first(self):
        return _row_to_dict(await self._prepared().first())

    async def run(self):
        await self._prepared().run()


class D1Database:
    def __init__(self, d1):
        self.d1 = d1

    def prepare(self, sql):
        return D1Statement(self.d1, sql)

    async def ensure_schema(self):
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                await self.prepare(stmt).run()


class SqliteStatement:
    def __init__(self, conn, sql):
        self._conn = conn
        self._sql = sql
        self._params = ()

    def bind(self, *params):
        self._params = params
        return self

    async def all(self):
        cur = self._conn.execute(self._sql, self._params)
        return [dict(r) for r in cur.fetchall()]

    async def first(self):
        cur = self._conn.execute(self._sql, self._params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    async def run(self):
        self._conn.execute(self._sql, self._params)
        self._conn.commit()


class SqliteDatabase:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def prepare(self, sql):
        return SqliteStatement(self.conn, sql)


_local_db = None
_d1_ready = False


async def get_database(request):
    global _local_db, _d1_ready
    env = request.scope.get("env") if request is not None else None
    d1 = getattr(env, "DB", None) if env is not None else None
    if d1 is not None:
        db = D1Database(d1)
        if not _d1_ready:
            try:
                await db.prepare("SELECT 1 FROM users LIMIT 1").first()
            except Exception:
                await db.ensure_schema()
            _d1_ready = True
        return db
    if _local_db is None:
        path = os.environ.get(
            "QUESTLOG_DB",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app.db"))
        _local_db = SqliteDatabase(path)
    return _local_db
