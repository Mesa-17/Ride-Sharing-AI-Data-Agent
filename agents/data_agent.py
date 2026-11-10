"""Data agent: the entry point. A router sends each request to the right specialist.

    router --sql--------> sql_analyst  (subgraph)
           --etl--------> etl_analyst  (subgraph)
           --unsupported-> decline       (e.g. "delete the cancelled rides")
"""
from typing import Callable

from langgraph.graph import END, START, StateGraph

from agents.etl_analyst import build_etl_analyst, run_etl
from agents.sql_analyst import build_sql_analyst
from models.schema import DataAgentState, RouterDecision
from utils.database import ReadOnlyDatabase
from utils.llm_pick import pick_llm

ROUTER_PROMPT = """Classify this request for a ride-sharing data assistant.
- sql: a question answered by reading the ride-sharing database (users, vehicles, rides, payments, ratings).
- etl: extract data from a web API, or inspect / transform a data file.
- unsupported: anything else, including requests to insert, update or delete data, or to change the database.

Request: {question}"""


def build_data_agent(llm: Callable = pick_llm, db: ReadOnlyDatabase | None = None):
    sql_analyst = build_sql_analyst(llm=llm, db=db)
    etl_analyst = build_etl_analyst(llm=llm)

    def router(state: DataAgentState) -> dict:
        d: RouterDecision = llm("low").with_structured_output(RouterDecision).invoke(
            ROUTER_PROMPT.format(question=state.question))
        return {"route": d.route, "route_reason": d.reason}

    def sql_node(state: DataAgentState) -> dict:
        out = sql_analyst.invoke({"question": state.question})
        return {"answer": out["answer"], "status": out["status"], "sql": out.get("sql", ""),
                "attempts": out.get("attempts", 0)}

    def etl_node(state: DataAgentState) -> dict:
        return {"answer": run_etl(etl_analyst, state.question), "status": "done"}

    def decline(state: DataAgentState) -> dict:
        return {"status": "unsupported",
                "answer": "I can answer questions about the ride-sharing data or run extract/transform jobs, "
                          f"but not this one. ({state.route_reason})"}

    g = StateGraph(DataAgentState)
    g.add_node("router", router)
    g.add_node("sql_analyst", sql_node)
    g.add_node("etl_analyst", etl_node)
    g.add_node("decline", decline)
    g.add_edge(START, "router")
    g.add_conditional_edges("router", lambda s: {"sql": "sql_analyst", "etl": "etl_analyst"}.get(s.route, "decline"),
                            ["sql_analyst", "etl_analyst", "decline"])
    for n in ("sql_analyst", "etl_analyst", "decline"):
        g.add_edge(n, END)
    return g.compile()
