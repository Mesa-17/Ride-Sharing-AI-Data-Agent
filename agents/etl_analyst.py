"""ETL analyst: a ReAct agent with three fenced-in tools.

    llm_node <-> tool_node  (until the model stops calling tools, or the step limit is hit)

Tools:
  * extract_api     pull JSON from a public http(s) API into data/extract/
  * inspect_file    show a data file's columns and first rows
  * transform_file  the LLM writes a typed TransformPlan; deterministic code runs it into data/transform/

No tool runs model-written code. A failing tool returns the reason as text, so the agent
can correct its next call instead of crashing.
"""
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from models.schema import ETLAgentState
from utils import etl_tools
from utils.etl_tools import ETLError, TransformPlan
from utils.llm_pick import pick_llm

MAX_STEPS = 12  # LangGraph recursion limit for one request

SYSTEM = """You are a data engineer running small ETL jobs with tools.
- Use extract_api to pull data from a URL. Pick a short output_name from the request.
- Before transforming a file you haven't seen, call inspect_file.
- Use transform_file with a precise instruction; it writes to data/transform/.
- If a tool returns an error, fix the call and try again once; otherwise explain the problem.
- When the job is done, reply with one or two sentences: what was produced and where."""

PLAN_PROMPT = """Turn the request into a TransformPlan for the data below.
Use only these columns, with exact names. Keep the plan as short as possible.

Data:
{description}

Request: {instruction}"""


def build_tools(llm: Callable = pick_llm):
    @tool
    def extract_api(url: str, output_name: str, format: str = "csv") -> str:
        """Extract JSON records from a public http(s) API and save them to data/extract/<output_name>.<format>.
        format is one of csv, json, parquet."""
        try:
            return etl_tools.extract_api(url, output_name, format)
        except ETLError as e:
            return f"ERROR: {e}"

    @tool
    def inspect_file(path: str) -> str:
        """Show the row count, columns with types and first rows of a csv/json/parquet file in data/."""
        try:
            return etl_tools.describe(etl_tools.read_file(etl_tools.resolve_input(path)))
        except ETLError as e:
            return f"ERROR: {e}"

    @tool
    def transform_file(input_path: str, instruction: str, output_name: str, format: str = "csv") -> str:
        """Transform a data file as described in plain English (filter, select, rename, cast, dedupe,
        aggregate, sort, limit) and save it to data/transform/<output_name>.<format>."""
        try:
            src = etl_tools.resolve_input(input_path)
            df = etl_tools.read_file(src)
            planner = llm("medium").with_structured_output(TransformPlan)
            plan: TransformPlan = planner.invoke(PLAN_PROMPT.format(
                description=etl_tools.describe(df), instruction=instruction))
            out = etl_tools.apply_plan(df, plan)
            dest = etl_tools.output_path("transform", output_name, format)
            etl_tools.write_file(out, dest)
        except ETLError as e:
            return f"ERROR: {e}"
        steps = ", ".join(s.op for s in plan.steps)
        rel = dest.relative_to(etl_tools.config.PROJECT_ROOT).as_posix()
        return f"{plan.summary} Steps: {steps}. Wrote {len(out):,} of {len(df):,} rows to {rel}."

    return [extract_api, inspect_file, transform_file]


def build_etl_analyst(llm: Callable = pick_llm):
    tools = build_tools(llm)
    by_name = {t.name: t for t in tools}

    def llm_node(state: ETLAgentState) -> dict:
        model = llm("medium").bind_tools(tools)
        return {"messages": [model.invoke([SystemMessage(SYSTEM), *state.messages])]}

    def tool_node(state: ETLAgentState) -> dict:
        results = []
        for call in state.messages[-1].tool_calls:
            t = by_name.get(call["name"])
            output = t.invoke(call["args"]) if t else f"ERROR: unknown tool {call['name']}"
            results.append(ToolMessage(content=str(output), tool_call_id=call["id"]))
        return {"messages": results}

    def has_tool_calls(state: ETLAgentState) -> str:
        return "tool_node" if getattr(state.messages[-1], "tool_calls", None) else END

    g = StateGraph(ETLAgentState)
    g.add_node("llm_node", llm_node)
    g.add_node("tool_node", tool_node)
    g.add_edge(START, "llm_node")
    g.add_conditional_edges("llm_node", has_tool_calls, ["tool_node", END])
    g.add_edge("tool_node", "llm_node")
    return g.compile().with_config(recursion_limit=MAX_STEPS)


def run_etl(graph, request: str) -> str:
    out = graph.invoke({"messages": [HumanMessage(request)]})
    content = out["messages"][-1].content if out["messages"] else ""
    if isinstance(content, list):  # Anthropic can return content blocks
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content.strip()
