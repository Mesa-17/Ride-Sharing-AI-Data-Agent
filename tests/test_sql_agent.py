"""The SQL analyst's control flow, with a scripted LLM and a fake database."""
from agents.sql_analyst import build_sql_analyst
from models.schema import JudgeVerdict
from utils.database import QueryError, QueryResult

from tests.fakes import FakeLLM

OK = JudgeVerdict(verdict="approve", comments="correct")


class FakeDB:
    def __init__(self, errors=0):
        self.errors = errors
        self.executed = []

    def schema_context(self):
        return "Table rides\n  - status (character varying) values: 'completed', 'cancelled'"

    def table_names(self):
        return {"rides", "users"}

    def run_select(self, sql):
        self.executed.append(sql)
        if self.errors:
            self.errors -= 1
            raise QueryError('column "fair" does not exist')
        return QueryResult(["n"], [(16073,)])


def run(texts, verdicts=(), db=None, max_attempts=3):
    db = db or FakeDB()
    llm = FakeLLM(texts=texts, structured=list(verdicts))
    out = build_sql_analyst(llm=llm, db=db, max_attempts=max_attempts).invoke({"question": "q"})
    return out, db, llm


def test_happy_path_runs_once():
    out, db, _ = run(["q", "SELECT COUNT(*) AS n FROM rides", "16,073 rides."], [OK])
    assert out["status"] == "answered" and out["attempts"] == 1 and len(db.executed) == 1


def test_reviewer_feedback_triggers_a_corrected_query():
    revise = JudgeVerdict(verdict="revise", comments="use status = 'completed'")
    out, db, llm = run(["q", "SELECT COUNT(*) FROM rides", "SELECT COUNT(*) FROM rides WHERE status='completed'", "ok"],
                       [revise, OK])
    assert out["status"] == "answered" and out["attempts"] == 2
    assert "use status = 'completed'" in llm.prompts[3]          # feedback reached the second generation
    assert len(db.executed) == 1                                   # the rejected query never ran


def test_unknown_table_is_fixed_without_asking_the_judge():
    out, _, llm = run(["q", "SELECT * FROM ride", "SELECT * FROM rides", "ok"], [OK])
    assert out["attempts"] == 2 and out["status"] == "answered"
    assert "Table 'ride' does not exist" in llm.prompts[2]


def test_a_write_is_blocked_immediately_and_never_retried():
    out, db, _ = run(["q", "DELETE FROM rides"])
    assert out["status"] == "blocked" and out["attempts"] == 1 and db.executed == []


def test_database_errors_are_fed_back():
    out, db, llm = run(["q", "SELECT fair FROM rides", "SELECT fare FROM rides", "ok"], [OK, OK], db=FakeDB(errors=1))
    assert out["status"] == "answered" and out["attempts"] == 2
    assert 'column "fair" does not exist' in llm.prompts[3]


def test_gives_up_after_max_attempts():
    out, db, _ = run(["q", "SELECT * FROM a", "SELECT * FROM b", "SELECT * FROM c"], max_attempts=3)
    assert out["status"] == "failed" and out["attempts"] == 3 and db.executed == []


def test_judge_refusal_stops_the_request():
    out, db, _ = run(["q", "SELECT email FROM users"], [JudgeVerdict(verdict="refuse", comments="credential lookup")])
    assert out["status"] == "blocked" and db.executed == []
