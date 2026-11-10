"""Read-only database access for the SQL agent.

Defence in depth: even if a bad query got past the SQL guard and the LLM judge,
  * the agent logs in as a role that only has SELECT grants,
  * every session is forced read-only (default_transaction_read_only=on),
  * every statement has a timeout, and
  * transactions are always rolled back, never committed.
"""
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

import psycopg2
from psycopg2 import sql

import config

# Text columns with only a few distinct values are listed in the prompt, so the model
# writes status = 'completed' instead of guessing 'Completed' or 'done'.
CATEGORICAL_MAX_DISTINCT = 12


class QueryError(Exception):
    """A query failed in the database. The message is safe to show to the SQL agent."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]

    def json_rows(self) -> list[list]:
        """Rows with values a JSON encoder can handle (Decimal -> float, dates -> ISO text)."""
        def clean(v):
            if isinstance(v, Decimal):
                return float(v)
            if isinstance(v, (dt.date, dt.datetime, dt.time)):
                return v.isoformat()
            if isinstance(v, (int, float, str, bool)) or v is None:
                return v
            return str(v)
        return [[clean(v) for v in row] for row in self.rows]

    def preview(self, max_rows: int = 50) -> str:
        if not self.rows:
            return "(no rows)"
        lines = [" | ".join(self.columns)]
        lines += [" | ".join("" if v is None else str(v) for v in row) for row in self.rows[:max_rows]]
        if len(self.rows) > max_rows:
            lines.append(f"... {len(self.rows) - max_rows} more rows")
        return "\n".join(lines)


class ReadOnlyDatabase:
    def __init__(self, settings: config.DBSettings | None = None, schema: str = config.DB_SCHEMA,
                 statement_timeout_ms: int = config.STATEMENT_TIMEOUT_MS):
        self.settings = settings or config.agent_db()
        self.schema = schema
        self.statement_timeout_ms = statement_timeout_ms

    def _connect(self):
        conn = psycopg2.connect(
            **self.settings.connect_kwargs(),
            connect_timeout=5,
            options=f"-c default_transaction_read_only=on -c statement_timeout={self.statement_timeout_ms}",
        )
        conn.set_session(readonly=True, autocommit=False)
        return conn

    # ------------------------------------------------------------------ queries
    def run_select(self, query: str) -> QueryResult:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(query)
                columns = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchall() if cur.description else []
            return QueryResult(columns, rows)
        except psycopg2.Error as e:
            message = (e.pgerror or str(e)).strip().splitlines()[0]
            raise QueryError(message) from e
        finally:
            conn.rollback()  # never commit anything
            conn.close()

    # ------------------------------------------------------------------ schema
    def table_names(self) -> set[str]:
        return set(self._schema_snapshot()["tables"])

    def schema_context(self) -> str:
        return self._schema_snapshot()["context"]

    @lru_cache(maxsize=1)
    def _schema_snapshot(self) -> dict:
        """Tables, columns, keys, sample rows and categorical values; built once per process."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    ORDER BY table_name""", (self.schema,))
                tables = [r[0] for r in cur.fetchall()]

                cur.execute("""
                    SELECT kcu.table_name, kcu.column_name, ccu.table_name, ccu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s""", (self.schema,))
                fks = cur.fetchall()

                parts = [f"PostgreSQL schema: {self.schema}"]
                for table in tables:
                    cur.execute("""
                        SELECT column_name, data_type FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        ORDER BY ordinal_position""", (self.schema, table))
                    columns = cur.fetchall()
                    parts.append(f"\nTable {table}")
                    for col, dtype in columns:
                        line = f"  - {col} ({dtype})"
                        if dtype in ("character varying", "text"):
                            values = self._categorical_values(cur, table, col)
                            if values:
                                line += f"  values: {', '.join(repr(v) for v in values)}"
                        parts.append(line)
                    for t, c, rt, rc in fks:
                        if t == table:
                            parts.append(f"  * {c} -> {rt}.{rc}")
                    cur.execute(sql.SQL("SELECT * FROM {}.{} LIMIT 3").format(
                        sql.Identifier(self.schema), sql.Identifier(table)))
                    sample = cur.fetchall()
                    if sample:
                        parts.append("  sample rows:")
                        parts += [f"    {row}" for row in sample]
            return {"tables": tables, "context": "\n".join(parts)}
        finally:
            conn.rollback()
            conn.close()

    def _categorical_values(self, cur, table: str, column: str) -> list[str]:
        cur.execute(sql.SQL("SELECT DISTINCT {c} FROM {s}.{t} WHERE {c} IS NOT NULL LIMIT %s").format(
            c=sql.Identifier(column), s=sql.Identifier(self.schema), t=sql.Identifier(table)),
            (CATEGORICAL_MAX_DISTINCT + 1,))
        values = [r[0] for r in cur.fetchall()]
        return sorted(values) if len(values) <= CATEGORICAL_MAX_DISTINCT else []
