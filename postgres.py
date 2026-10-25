"""Small PostgreSQL compatibility layer for the application's SQL helpers."""
import re

import psycopg
try:
    from psycopg_pool import ConnectionPool
except ImportError:  # Allows a controlled fallback until requirements are updated.
    ConnectionPool = None

from config import DATABASE_URL

DatabaseError = psycopg.Error
_pool = None


def _get_pool():
    """One bounded PostgreSQL pool for the whole process, created lazily."""
    global _pool
    if ConnectionPool is None:
        return None
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=2,
            max_size=20,
            timeout=10,
            kwargs={"connect_timeout": 5},
            open=True,
        )
    return _pool


def adapt_sql(sql):
    """Convert the legacy qmark placeholders and SQLite-only inserts to PostgreSQL."""
    statement = sql.strip()
    statement = statement.replace("?", "%s")
    statement = statement.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    statement = statement.replace("AUTOINCREMENT", "")
    # Telegram identifiers can exceed PostgreSQL INTEGER (32-bit) range.
    # Apply this to every *_id column while preserving serial primary keys,
    # plus the legacy actor columns whose names do not end in ``_id``.
    statement = re.sub(r"\b([A-Za-z_]+_id)\s+INTEGER\b", r"\1 BIGINT", statement)
    statement = re.sub(
        r"\b(created_by|added_by|assigned_to|closed_by|processed_by|current_bidder|used_by)\s+INTEGER\b",
        r"\1 BIGINT",
        statement,
    )
    statement = re.sub(r"\bDATETIME\b", "TIMESTAMP", statement, flags=re.I)
    # SQLite accepts 0/1 for BOOLEAN. PostgreSQL requires boolean literals.
    statement = re.sub(
        r"\bBOOLEAN\s+DEFAULT\s+([01])\b",
        lambda match: "BOOLEAN DEFAULT " + ("TRUE" if match.group(1) == "1" else "FALSE"),
        statement,
        flags=re.I,
    )
    boolean_columns = (
        "is_active|is_sold|processing|active|all_channels|is_valid|is_revoked|"
        "reminded_3d|reminded_1d|expired_notified"
    )
    statement = re.sub(
        rf"(?P<prefix>\b(?:\w+\.)?(?:{boolean_columns}))\s*=\s*(?P<value>[01])\b",
        lambda match: f"{match.group('prefix')}={'TRUE' if match.group('value') == '1' else 'FALSE'}",
        statement,
        flags=re.I,
    )
    statement = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", statement, flags=re.I)
    if re.match(r"^INSERT\s+OR\s+IGNORE", sql.strip(), flags=re.I):
        statement = statement.rstrip(";") + " ON CONFLICT DO NOTHING"
    return statement


class Cursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        self._cursor.execute(adapt_sql(sql), params)
        return self

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class Connection:
    def __init__(self):
        self._pooled_connection = None
        pool = _get_pool()
        if pool is None:
            self._connection = psycopg.connect(DATABASE_URL, connect_timeout=5)
        else:
            self._pooled_connection = pool.connection()
            self._connection = self._pooled_connection.__enter__()

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._pooled_connection is not None:
            return self._pooled_connection.__exit__(exc_type, exc, traceback)
        return self._connection.__exit__(exc_type, exc, traceback)

    def cursor(self):
        return Cursor(self._connection.cursor())

    def execute(self, sql, params=None):
        cursor = self.cursor()
        return cursor.execute(sql, params)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        if self._pooled_connection is not None:
            self._pooled_connection.__exit__(None, None, None)
            self._pooled_connection = None
        else:
            self._connection.close()


def connect():
    return Connection()


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
