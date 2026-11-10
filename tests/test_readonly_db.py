"""Integration: proves the database itself refuses writes, independent of the guard and judge.
Runs only when a database is configured (DB_AGENT_PASSWORD set and reachable)."""
import os

import psycopg2
import pytest

import config
from utils.database import QueryError, ReadOnlyDatabase


def _db_or_skip():
    if not os.environ.get("DB_AGENT_PASSWORD"):
        pytest.skip("no database configured")
    db = ReadOnlyDatabase()
    try:
        db.run_select("SELECT 1")
    except Exception as e:
        pytest.skip(f"database not reachable: {e}")
    return db


@pytest.mark.parametrize("sql", ["DELETE FROM rides", "UPDATE users SET city = 'x'", "CREATE TABLE t (a int)"])
def test_writes_fail_inside_the_database(sql):
    with pytest.raises(QueryError, match="read-only"):
        _db_or_skip().run_select(sql)


def test_role_has_no_write_grants_even_in_a_read_write_transaction():
    _db_or_skip()
    conn = psycopg2.connect(**config.agent_db().connect_kwargs())
    conn.autocommit = True  # each statement is its own transaction
    try:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = off")
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM rides")
    finally:
        conn.close()


def test_long_queries_are_cancelled():
    with pytest.raises(QueryError, match="timeout"):
        _db_or_skip().run_select(f"SELECT pg_sleep({config.STATEMENT_TIMEOUT_MS / 1000 + 2})")
