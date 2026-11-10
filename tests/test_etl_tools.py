import pandas as pd
import pytest

from utils.etl_tools import ETLError, TransformPlan, apply_plan, extract_api, resolve_input

DF = pd.DataFrame({"city": ["Toronto", "Ottawa", "Toronto", "Halifax"],
                   "fare": [10.0, 20.0, 30.0, None], "status": ["completed", "completed", "cancelled", "completed"]})


def plan(*steps):
    return TransformPlan.model_validate({"summary": "test", "steps": list(steps)})


def test_filter_aggregate_sort():
    out = apply_plan(DF, plan(
        {"op": "filter", "column": "status", "operator": "==", "value": "completed"},
        {"op": "drop_nulls", "subset": ["fare"]},
        {"op": "aggregate", "group_by": ["city"], "metrics": [{"column": "fare", "func": "sum", "name": "total"}]},
        {"op": "sort", "by": ["total"], "ascending": False}))
    assert out.to_dict("records") == [{"city": "Ottawa", "total": 20.0}, {"city": "Toronto", "total": 10.0}]


def test_unknown_columns_are_rejected_with_the_real_column_list():
    with pytest.raises(ETLError, match="Available"):
        apply_plan(DF, plan({"op": "select", "columns": ["town"]}))


def test_plans_cannot_contain_arbitrary_steps():
    with pytest.raises(Exception):
        plan({"op": "python", "code": "import os; os.remove('x')"})


@pytest.mark.parametrize("path", ["../feed_db.py", "/etc/passwd", "../../.env"])
def test_reads_are_confined_to_the_data_folder(path):
    with pytest.raises(ETLError):
        resolve_input(path)


@pytest.mark.parametrize("url", ["http://localhost:5432/", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data",
                                 "file:///etc/passwd", "ftp://example.com/x"])
def test_extract_refuses_local_and_non_http_urls(url):
    with pytest.raises(ETLError):
        extract_api(url, "x")
