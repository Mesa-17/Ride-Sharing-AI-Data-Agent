"""Deterministic SQL guard: the layer that does not depend on an LLM's judgement.

Every generated query is parsed (not pattern-matched) and must pass these rules before
the LLM judge or the database ever see it:

  1. It parses as PostgreSQL, and is exactly one statement.
  2. It is a read query (SELECT / UNION / CTE). Any write, DDL or command anywhere in
     the tree is a HARD block: the request is refused and never retried.
  3. It calls no dangerous functions (sleep, file access, remote connections, settings).
  4. It only reads tables the agent is allowed to see.
  5. Its result is capped: a LIMIT is added if missing, or lowered if too high.

Failures are split into two kinds:
  * HARD    -> the request itself is unsafe; stop and tell the user.
  * FIXABLE -> the query is wrong but harmless (syntax, unknown table); send the reason
               back to the SQL agent so it can correct itself.
"""
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

HARD = "hard"
FIXABLE = "fixable"

WRITE_NODES = tuple(
    getattr(exp, name)
    for name in ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
                 "TruncateTable", "Command", "Copy", "Grant", "Set", "Transaction",
                 "Commit", "Rollback", "Use", "Into", "LoadData", "Analyze")
    if hasattr(exp, name)
)

BLOCKED_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_exec", "dblink_connect",
    "set_config", "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "pg_advisory_lock", "pg_try_advisory_lock", "query_to_xml", "pg_logical_emit_message",
}


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""
    kind: str = ""      # HARD or FIXABLE when not ok
    reason: str = ""


def _fail(kind: str, reason: str) -> GuardResult:
    return GuardResult(ok=False, kind=kind, reason=reason)


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return (node.sql_name() or "").lower()


def check_sql(sql: str, allowed_tables: set[str], schema: str = "public",
              max_rows: int = 100) -> GuardResult:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return _fail(FIXABLE, "The query is empty.")

    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except ParseError as e:
        return _fail(FIXABLE, f"The query is not valid PostgreSQL: {str(e).splitlines()[0]}")

    if len(statements) != 1:
        return _fail(HARD, "Only one statement is allowed per request.")
    tree = statements[0]

    # 2. read-only, anywhere in the tree (catches writes hidden inside CTEs)
    for node in tree.walk():
        if isinstance(node, WRITE_NODES):
            return _fail(HARD, f"{type(node).__name__.upper()} is not allowed: the agent is read-only.")
    if not isinstance(tree, exp.Query):
        return _fail(HARD, "Only SELECT queries are allowed.")
    if any(isinstance(lock, exp.Lock) for lock in tree.find_all(exp.Lock)):
        return _fail(HARD, "Row locks (FOR UPDATE / FOR SHARE) are not allowed.")

    # 3. dangerous functions
    for fn in tree.find_all(exp.Func):
        name = _function_name(fn)
        if name in BLOCKED_FUNCTIONS or name.startswith("pg_read") or name.startswith("lo_"):
            return _fail(HARD, f"Function {name}() is not allowed.")

    # 4. table allow-list (names defined in WITH clauses are fine)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    allowed = {t.lower() for t in allowed_tables}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        db = (table.db or "").lower()
        if not name:  # e.g. a function used as a table source
            return _fail(FIXABLE, "Only plain tables can be queried.")
        if db and db != schema.lower():
            return _fail(FIXABLE, f"Schema '{db}' is not available. Use only: {', '.join(sorted(allowed))}.")
        if name not in allowed and name not in cte_names:
            return _fail(FIXABLE, f"Table '{name}' does not exist. Available tables: {', '.join(sorted(allowed))}.")

    # 5. cap the result size on the outermost query
    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(max_rows)
    else:
        try:
            current = int(limit.expression.name) if isinstance(limit, exp.Limit) else max_rows + 1
        except (TypeError, ValueError, AttributeError):
            current = max_rows + 1
        if current > max_rows:
            tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))

    return GuardResult(ok=True, sql=tree.sql(dialect="postgres", pretty=True))
