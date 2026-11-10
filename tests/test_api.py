"""The web API: event stream shape, refusals, declines and rate limits (fake LLM and fake DB)."""
import json

from fastapi.testclient import TestClient

from api.server import RateLimiter, create_app
from models.schema import JudgeVerdict, RouterDecision
from tests.fakes import FakeLLM
from tests.test_sql_agent import FakeDB

OK = JudgeVerdict(verdict="approve", comments="correct")
SQL = RouterDecision(route="sql", reason="reads ride data")


def events(resp):
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def client(texts=(), structured=(), per_hour=5, per_day=100):
    llm = FakeLLM(texts=list(texts), structured=list(structured))
    app = create_app(llm=llm, db=FakeDB(), limiter=RateLimiter(per_hour, per_day))
    return TestClient(app)


def test_answered_question_streams_every_step():
    c = client(["How many rides?", "SELECT COUNT(*) AS n FROM rides", "16,073 rides."], [SQL, OK])
    ev = events(c.post("/api/ask", json={"question": "how many rides"}))
    steps = [e["node"] for e in ev if e["type"] == "step"]
    assert steps == ["router", "curate_question", "load_schema", "generate_sql", "check_sql",
                     "judge_sql", "execute_sql", "write_answer"]
    assert [e for e in ev if e["type"] == "result"][0]["rows"] == [[16073]]
    final = ev[-1]
    assert final["type"] == "answer" and final["status"] == "answered" and final["attempts"] == 1
    assert next(e for e in ev if e.get("node") == "check_sql")["detail"].startswith("passed")


def test_retry_is_visible_in_the_stream():
    revise = JudgeVerdict(verdict="revise", comments="add the 2026 filter")
    c = client(["q", "SELECT COUNT(*) FROM rides", "SELECT COUNT(*) FROM rides WHERE status='completed'", "ok"],
               [SQL, revise, OK])
    ev = events(c.post("/api/ask", json={"question": "q"}))
    judge = [e for e in ev if e.get("node") == "judge_sql"]
    assert [j["state"] for j in judge] == ["warn", "ok"] and judge[0]["detail"].startswith("revise")
    assert ev[-1]["attempts"] == 2


def test_write_is_refused_and_never_reaches_the_database():
    c = client(["q", "DELETE FROM rides"], [SQL])
    ev = events(c.post("/api/ask", json={"question": "delete rides"}))
    guard = next(e for e in ev if e.get("node") == "check_sql")
    assert guard["state"] == "bad" and ev[-1]["status"] == "blocked"
    assert not any(e["type"] == "result" for e in ev)


def test_etl_and_unsupported_requests_are_declined():
    for route in ("etl", "unsupported"):
        c = client(structured=[RouterDecision(route=route, reason="it asks to delete data")])
        ev = events(c.post("/api/ask", json={"question": "x"}))
        assert ev[-1]["status"] == "declined" and ev[0]["state"] == "bad"


def test_rate_limit_per_visitor():
    c = client(structured=[RouterDecision(route="unsupported", reason="no")] * 3, per_hour=2)
    codes = [c.post("/api/ask", json={"question": "x"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_long_and_empty_questions_are_rejected():
    c = client()
    assert c.post("/api/ask", json={"question": "x" * 301}).status_code == 422
    assert c.post("/api/ask", json={"question": ""}).status_code == 422
