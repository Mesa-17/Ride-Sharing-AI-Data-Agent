from agents.data_agent import build_data_agent
from evals.run_evals import results_match
from models.schema import RouterDecision

from tests.fakes import FakeLLM


def test_unsupported_requests_are_declined_without_touching_the_database():
    llm = FakeLLM(structured=[RouterDecision(route="unsupported", reason="it asks to delete data")])
    out = build_data_agent(llm=llm, db=object()).invoke({"question": "delete all rides"})
    assert out["status"] == "unsupported" and "delete data" in out["answer"]


def test_result_matching_ignores_order_extra_columns_and_rounding():
    ref = [("Toronto", 10), ("Ottawa", 20)]
    assert results_match(ref, [(20.0, "Ottawa", "x"), (10.001, "Toronto", "y")])
    assert not results_match(ref, [("Toronto", 11), ("Ottawa", 20)])
    assert not results_match(ref, [("Toronto", 10)])
