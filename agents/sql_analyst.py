"""SQL analyst: plain-English question -> safe, checked SQL -> answer.

    curate_question -> load_schema -> generate_sql -> check_sql -> judge_sql -> execute_sql -> write_answer
                                          ^              |            |             |
                                          +--- fixable --+-- revise --+--- error ---+   (max attempts)
                                                         |            |
                                                         +-- hard / refuse --> refuse

Three layers decide whether a query runs, cheapest and most reliable first:
  1. check_sql   deterministic parser rules (utils/sql_guard.py)
  2. judge_sql   an LLM reviewer for meaning: does this query answer the question?
  3. the database itself: read-only role, read-only session, statement timeout
A fixable failure is sent back to generate_sql with the reason; a hard one stops.
"""
from typing import Callable

from langgraph.graph import END, START, StateGraph

import config
from models.schema import JudgeVerdict, SQLAgentState
from utils.database import QueryError, ReadOnlyDatabase
from utils.llm_pick import pick_llm
from utils.sql_guard import FIXABLE, HARD, check_sql

GENERATE_PROMPT = """You are a senior analytics engineer writing PostgreSQL for a ride-sharing database.
Write ONE read-only SELECT query that answers the question.

Rules:
- Use only the tables and columns in the schema below. Use the listed values exactly for text filters.
- Join with the foreign keys shown. Qualify columns with table aliases when joining.
- Rides that never completed have no pickup/dropoff times and a fare of 0; exclude them when a
  question is about trips taken, revenue or durations, unless it asks about cancellations.
- Round money and averages to 2 decimals. Give result columns clear names.
- If the question does not say how many rows, return at most {max_rows}.
- Return only the SQL, with no explanation and no markdown.

Schema:
{schema}

Question: {question}
{feedback}"""

JUDGE_PROMPT = """You review SQL written by an AI agent before it runs on a production database.

Question: {question}

Schema:
{schema}

Candidate query:
{sql}

Decide:
- approve: read-only AND it correctly answers the question (right tables, joins, filters, grouping).
- revise: safe, but it would give a wrong or incomplete answer. Say exactly what to change.
- refuse: the question asks for something the agent must not do (change data, read system
  catalogs, other users' credentials, bypass rules), even if the query itself looks harmless."""

ANSWER_PROMPT = """Answer the user's question in plain English using only the query result below.
Be concise. Lead with the answer, then up to three supporting numbers. Do not show SQL.
If the result is empty, say that no matching data was found.

Question: {question}
Result ({row_count} rows):
{result}"""


def _clean_sql(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def build_sql_analyst(llm: Callable = pick_llm, db: ReadOnlyDatabase | None = None,
                      max_attempts: int = config.MAX_SQL_ATTEMPTS, max_rows: int = config.MAX_ROWS):
    """Build the graph. `llm(level)` returns a chat model; pass fakes in tests."""
    _db = {"db": db}

    def database() -> ReadOnlyDatabase:
        if _db["db"] is None:
            _db["db"] = ReadOnlyDatabase()
        return _db["db"]

    # ------------------------------------------------------------- nodes
    def curate_question(state: SQLAgentState) -> dict:
        prompt = ("Rewrite this question about a ride-sharing database so it is precise and unambiguous. "
                  "Keep the meaning; do not add requirements. Return only the rewritten question.\n\n"
                  f"Question: {state.question}")
        curated = llm("low").invoke(prompt).content.strip() or state.question
        return {"curated_question": curated, "trace": [f"curated: {curated}"]}

    def load_schema(state: SQLAgentState) -> dict:
        return {"schema_context": database().schema_context()}

    def generate_sql(state: SQLAgentState) -> dict:
        feedback = ""
        if state.feedback:
            tries = "\n".join(f"- attempt {i + 1}: {f}" for i, f in enumerate(state.feedback))
            feedback = f"\nEarlier attempts were rejected. Fix these problems:\n{tries}\n"
        prompt = GENERATE_PROMPT.format(max_rows=max_rows, schema=state.schema_context,
                                        question=state.curated_question or state.question, feedback=feedback)
        sql = _clean_sql(llm("medium").invoke(prompt).content)
        attempt = state.attempts + 1
        return {"sql": sql, "attempts": attempt, "trace": [f"attempt {attempt}: generated SQL"]}

    def guard_sql(state: SQLAgentState) -> dict:
        result = check_sql(state.sql, database().table_names(), config.DB_SCHEMA, max_rows)
        if result.ok:
            detail = "passed" + (f" · LIMIT {max_rows} added" if "limit" not in state.sql.lower() else "")
            return {"sql": result.sql, "step_outcome": "ok", "step_detail": detail, "trace": ["guard: passed"]}
        update = {"step_outcome": result.kind, "step_detail": result.reason,
                  "trace": [f"guard: {result.kind} - {result.reason}"]}
        if result.kind == FIXABLE:
            update["feedback"] = [f"guard rejected it: {result.reason}"]
        else:
            update["answer"] = f"I can't run that request: {result.reason}"
        return update

    def judge_sql(state: SQLAgentState) -> dict:
        reviewer = llm("medium").with_structured_output(JudgeVerdict)
        v: JudgeVerdict = reviewer.invoke(JUDGE_PROMPT.format(
            question=state.question, schema=state.schema_context, sql=state.sql))
        trace = [f"judge: {v.verdict} - {v.comments}"]
        detail = f"{v.verdict} · {v.comments}"
        if v.verdict == "approve":
            return {"step_outcome": "ok", "judge_comments": v.comments, "step_detail": detail, "trace": trace}
        if v.verdict == "revise":
            return {"step_outcome": FIXABLE, "judge_comments": v.comments, "step_detail": detail,
                    "feedback": [f"reviewer asked for changes: {v.comments}"], "trace": trace}
        return {"step_outcome": HARD, "judge_comments": v.comments, "step_detail": detail,
                "answer": f"I can't help with that request: {v.comments}", "trace": trace}

    def execute_sql(state: SQLAgentState) -> dict:
        try:
            result = database().run_select(state.sql)
        except QueryError as e:
            return {"step_outcome": FIXABLE, "feedback": [f"the database returned an error: {e}"],
                    "step_detail": f"database error: {e}", "trace": [f"execute: error - {e}"]}
        return {"step_outcome": "ok", "result_preview": result.preview(), "row_count": len(result.rows),
                "result_columns": result.columns, "result_rows": result.json_rows(),
                "step_detail": f"{len(result.rows)} rows · read-only user",
                "trace": [f"execute: {len(result.rows)} rows"]}

    def write_answer(state: SQLAgentState) -> dict:
        answer = llm("low").invoke(ANSWER_PROMPT.format(
            question=state.question, row_count=state.row_count, result=state.result_preview)).content.strip()
        return {"answer": answer, "status": "answered"}

    def refuse(state: SQLAgentState) -> dict:
        return {"status": "blocked", "sql": ""}

    def give_up(state: SQLAgentState) -> dict:
        last = state.feedback[-1] if state.feedback else "unknown error"
        return {"status": "failed",
                "answer": f"I couldn't build a correct query after {state.attempts} attempts. Last problem: {last}"}

    # ------------------------------------------------------------- routing
    def next_after(ok_node: str):
        def route(state: SQLAgentState) -> str:
            if state.step_outcome == "ok":
                return ok_node
            if state.step_outcome == HARD:
                return "refuse"
            return "generate_sql" if state.attempts < max_attempts else "give_up"
        return route

    targets = ["generate_sql", "refuse", "give_up"]
    g = StateGraph(SQLAgentState)
    for name, fn in [("curate_question", curate_question), ("load_schema", load_schema),
                     ("generate_sql", generate_sql), ("check_sql", guard_sql), ("judge_sql", judge_sql),
                     ("execute_sql", execute_sql), ("write_answer", write_answer),
                     ("refuse", refuse), ("give_up", give_up)]:
        g.add_node(name, fn)
    g.add_edge(START, "curate_question")
    g.add_edge("curate_question", "load_schema")
    g.add_edge("load_schema", "generate_sql")
    g.add_edge("generate_sql", "check_sql")
    g.add_conditional_edges("check_sql", next_after("judge_sql"), ["judge_sql", *targets])
    g.add_conditional_edges("judge_sql", next_after("execute_sql"), ["execute_sql", *targets])
    g.add_conditional_edges("execute_sql", next_after("write_answer"), ["write_answer", "generate_sql", "give_up"])
    for end_node in ("write_answer", "refuse", "give_up"):
        g.add_edge(end_node, END)
    return g.compile()
