"""Typed state and structured outputs for every agent (Pydantic)."""
from operator import add
from typing import Annotated, Literal

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ router
class RouterDecision(BaseModel):
    route: Literal["sql", "etl", "unsupported"] = Field(
        description="sql: a question answered from the ride-sharing database. "
                    "etl: extract data from an API or transform a data file. "
                    "unsupported: anything else, including requests to change or delete data.")
    reason: str = Field(description="One short sentence explaining the choice.")


class DataAgentState(BaseModel):
    question: str
    route: str = ""
    route_reason: str = ""
    answer: str = ""
    status: str = ""                    # answered | blocked | failed | done | unsupported
    sql: str = ""
    attempts: int = 0


# ------------------------------------------------------------------ SQL analyst
class JudgeVerdict(BaseModel):
    verdict: Literal["approve", "revise", "refuse"] = Field(
        description="approve: the query is read-only and correctly answers the question. "
                    "revise: it is safe but wrong or incomplete (wrong table, join, filter, grouping). "
                    "refuse: the request itself tries to read or change something it should not.")
    comments: str = Field(description="What is wrong and how to fix it, or why it is approved.")


class SQLAgentState(BaseModel):
    question: str
    curated_question: str = ""
    schema_context: str = ""
    sql: str = ""                                       # latest candidate query
    feedback: Annotated[list[str], add] = Field(default_factory=list)  # reasons earlier attempts failed
    attempts: int = 0
    step_outcome: str = ""                              # ok | fixable | hard: set by guard / judge / execute
    judge_comments: str = ""
    step_detail: str = ""                               # human-readable result of the latest check
    result_preview: str = ""
    row_count: int = 0
    result_columns: list[str] = Field(default_factory=list)
    result_rows: list[list] = Field(default_factory=list)   # JSON-safe values, for the web demo
    answer: str = ""
    status: str = ""                                    # answered | blocked | failed
    trace: Annotated[list[str], add] = Field(default_factory=list)


# ------------------------------------------------------------------ ETL analyst
class ETLAgentState(BaseModel):
    messages: Annotated[list, add] = Field(default_factory=list)
