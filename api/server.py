"""Public web API for the live demo: streams each agent step to the browser as it happens.

    uvicorn api.server:app --host 0.0.0.0 --port 8000

POST /api/ask  {"question": "..."}  ->  text/event-stream, one JSON event per line:
    {"type": "step",   "node": "check_sql", "state": "ok|warn|bad", "detail": "...", "ms": 31, "attempt": 1}
    {"type": "sql",    "sql": "...", "attempt": 1}
    {"type": "result", "columns": [...], "rows": [[...], ...]}
    {"type": "answer", "status": "answered|blocked|failed|declined", "text": "...", "attempts": 2, "ms": 8012}
    {"type": "error",  "message": "..."}

Public-demo limits (all set by environment variables):
  * questions per visitor per hour (DEMO_PER_HOUR, default 5) and for the whole site per day (DEMO_PER_DAY)
  * question length (300 characters)
  * SQL only: ETL jobs write files and call outside URLs, so they are switched off here
"""
import hashlib
import json
import os
import threading
import time
from collections import deque
from datetime import date
from typing import Callable, Iterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agents.data_agent import ROUTER_PROMPT
from agents.sql_analyst import build_sql_analyst
from models.schema import RouterDecision
from utils.database import ReadOnlyDatabase
from utils.llm_pick import pick_llm

MAX_QUESTION = 300
PER_HOUR = int(os.environ.get("DEMO_PER_HOUR", "5"))
PER_DAY = int(os.environ.get("DEMO_PER_DAY", "300"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "https://sandeepmenon.dev,http://localhost:4321").split(",") if o.strip()]


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)


class RateLimiter:
    """In-memory limits: N questions per visitor per hour, M per day for the whole site.
    Good enough for one small server; a multi-instance deploy would move this to Redis."""

    def __init__(self, per_hour: int = PER_HOUR, per_day: int = PER_DAY):
        self.per_hour, self.per_day = per_hour, per_day
        self.hits: dict[str, deque] = {}
        self.day, self.day_count = date.today(), 0
        self.lock = threading.Lock()

    def check(self, key: str) -> str | None:
        """Return None if allowed (and count it), or a message saying why not."""
        now = time.time()
        with self.lock:
            if date.today() != self.day:
                self.day, self.day_count = date.today(), 0
            if self.day_count >= self.per_day:
                return "The demo has reached today's limit. It resets at midnight UTC; the example runs still work."
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.per_hour:
                wait = int(3600 - (now - q[0])) // 60 + 1
                return f"You've asked {self.per_hour} questions this hour. Try again in about {wait} minutes."
            q.append(now)
            self.day_count += 1
            return None


def _event(obj: dict) -> str:
    return f"data: {json.dumps(obj, default=str)}\n\n"


def _visitor(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    return hashlib.sha256(ip.encode()).hexdigest()[:16]  # never store the raw IP


STATE = {"ok": "ok", "fixable": "warn", "hard": "bad"}


def stream_answer(question: str, llm: Callable, sql_graph) -> Iterator[str]:
    """Run router + SQL analyst and yield one SSE event per step."""
    t0 = last = time.perf_counter()

    def ms() -> int:
        nonlocal last
        now = time.perf_counter()
        took, last = int((now - last) * 1000), now
        return took

    def total() -> int:
        return int((time.perf_counter() - t0) * 1000)

    # 1. route
    d: RouterDecision = llm("low").with_structured_output(RouterDecision).invoke(
        ROUTER_PROMPT.format(question=question))
    if d.route != "sql":
        reason = ("extract and transform jobs are switched off in the public demo"
                  if d.route == "etl" else d.reason)
        yield _event({"type": "step", "node": "router", "state": "bad", "detail": f"declined · {reason}", "ms": ms()})
        text = ("This demo only answers questions about the ride-sharing data. "
                + ("ETL jobs can be run from the code on GitHub." if d.route == "etl" else f"This request {reason.rstrip('.')}."))
        yield _event({"type": "answer", "status": "declined", "text": text, "attempts": 0, "ms": total()})
        return
    yield _event({"type": "step", "node": "router", "state": "ok", "detail": "sent to the SQL agent", "ms": ms()})

    # 2. SQL analyst, one event per node as LangGraph finishes it
    state: dict = {}
    for update in sql_graph.stream({"question": question}, stream_mode="updates"):
        for node, change in update.items():
            change = change or {}
            state.update({k: v for k, v in change.items() if k not in ("trace", "feedback")})
            took, attempt = ms(), state.get("attempts", 0)
            outcome = STATE.get(change.get("step_outcome", "ok"), "ok")
            if node == "curate_question":
                yield _event({"type": "step", "node": node, "state": "ok", "detail": state.get("curated_question", ""), "ms": took})
            elif node == "load_schema":
                yield _event({"type": "step", "node": node, "state": "ok", "detail": "tables, keys, column values", "ms": took})
            elif node == "generate_sql":
                yield _event({"type": "step", "node": node, "state": "ok", "attempt": attempt,
                              "detail": "one PostgreSQL query" if attempt == 1 else "rewritten with the feedback", "ms": took})
                yield _event({"type": "sql", "sql": state.get("sql", ""), "attempt": attempt})
            elif node in ("check_sql", "judge_sql"):
                yield _event({"type": "step", "node": node, "state": outcome, "detail": change.get("step_detail", ""),
                              "attempt": attempt, "ms": took})
                if node == "check_sql" and outcome == "ok":
                    yield _event({"type": "sql", "sql": state.get("sql", ""), "attempt": attempt})
            elif node == "execute_sql":
                yield _event({"type": "step", "node": node, "state": outcome, "detail": change.get("step_detail", ""),
                              "attempt": attempt, "ms": took})
                if outcome == "ok":
                    yield _event({"type": "result", "columns": state.get("result_columns", []),
                                  "rows": state.get("result_rows", [])})
            elif node == "write_answer":
                yield _event({"type": "step", "node": node, "state": "ok", "detail": "plain-English summary", "ms": took})
                yield _event({"type": "answer", "status": "answered", "text": state.get("answer", ""),
                              "attempts": attempt, "ms": total()})
            elif node == "refuse":
                yield _event({"type": "answer", "status": "blocked", "text": state.get("answer", ""),
                              "attempts": attempt, "ms": total()})
            elif node == "give_up":
                yield _event({"type": "answer", "status": "failed", "text": state.get("answer", ""),
                              "attempts": attempt, "ms": total()})


def create_app(llm: Callable = pick_llm, db: ReadOnlyDatabase | None = None,
               limiter: RateLimiter | None = None) -> FastAPI:
    app = FastAPI(title="Ride-sharing data agent", docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["POST", "GET"],
                       allow_headers=["Content-Type"])
    limiter = limiter or RateLimiter()
    graph = {"sql": None}

    def sql_graph():
        if graph["sql"] is None:
            graph["sql"] = build_sql_analyst(llm=llm, db=db)
        return graph["sql"]

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/api/ask")
    def ask(body: AskBody, request: Request):
        question = " ".join(body.question.split())
        who = _visitor(request)
        blocked = limiter.check(who)
        if blocked:
            return JSONResponse({"error": blocked}, status_code=429)

        def run():
            start, status = time.perf_counter(), "error"
            try:
                for chunk in stream_answer(question, llm, sql_graph()):
                    if '"type": "answer"' in chunk:
                        status = json.loads(chunk[6:])["status"]
                    yield chunk
            except Exception as e:  # never leak a stack trace to the browser
                print(json.dumps({"event": "error", "visitor": who, "error": f"{type(e).__name__}: {e}"}), flush=True)
                yield _event({"type": "error", "message": "The agent hit an error. Try again, or pick an example."})
            finally:
                print(json.dumps({"event": "ask", "visitor": who, "question": question, "status": status,
                                  "ms": int((time.perf_counter() - start) * 1000)}), flush=True)

        return StreamingResponse(run(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app()
