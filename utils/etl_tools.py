"""ETL building blocks the ETL agent can call.

The original version asked the LLM to write pandas code and ran it with exec(). That lets
a model (or a cleverly worded request) run anything on the machine. Here the LLM only
produces a *plan*: a typed list of steps from a fixed menu. This code checks the plan
against the real columns and executes it. The model decides what to do; deterministic
code decides how.

Extraction is also fenced in: http(s) only, no private or local addresses, a timeout,
a size cap, and files can only be written inside the project's data folder.
"""
import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Annotated, Literal, Union
from urllib.parse import urlparse

import pandas as pd
import requests
from pydantic import BaseModel, Field

import config

FORMATS = ("csv", "json", "parquet")


class ETLError(Exception):
    """A problem the agent should report back to the user."""


# ------------------------------------------------------------------ paths
def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_")
    if not stem:
        raise ETLError("Please give the output a simple file name, like 'pokemon' or 'completed_rides'.")
    return stem[:80]


def resolve_input(path: str) -> Path:
    """Inputs must live inside the data folder. Relative paths may start at the project or at data/."""
    p = Path(path)
    data = config.DATA_DIR.resolve()
    candidates = [p] if p.is_absolute() else [config.PROJECT_ROOT / p, config.DATA_DIR / p]
    inside = [c.resolve() for c in candidates if c.resolve().is_relative_to(data)]
    if not inside:
        raise ETLError("Only files inside the data/ folder can be read.")
    for c in inside:
        if c.is_file():
            return c
    raise ETLError(f"File not found: {path}")


def output_path(stage: str, name: str, fmt: str) -> Path:
    if fmt not in FORMATS:
        raise ETLError(f"Unsupported format '{fmt}'. Use one of: {', '.join(FORMATS)}.")
    folder = config.DATA_DIR / stage
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{_safe_name(name)}.{fmt}"


def read_file(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower().lstrip(".")
    if ext == "csv":
        return pd.read_csv(path)
    if ext == "json":
        return pd.read_json(path, lines=True)
    if ext == "parquet":
        return pd.read_parquet(path)
    raise ETLError(f"Unsupported file type: .{ext}")


def write_file(df: pd.DataFrame, path: Path) -> None:
    fmt = path.suffix.lstrip(".")
    if fmt == "csv":
        df.to_csv(path, index=False)
    elif fmt == "json":
        df.to_json(path, orient="records", lines=True, date_format="iso")
    else:
        df.to_parquet(path, index=False)


# ------------------------------------------------------------------ extract
def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ETLError("Only http(s) URLs can be extracted.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror:
        raise ETLError(f"Could not resolve host {parsed.hostname}.") from None
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ETLError("URLs that point to private or local network addresses are not allowed.")


def _records(payload) -> list:
    """Find the list of records in common API shapes: [...], {results: [...]}, {data: [...]}, {...}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "items", "records", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    raise ETLError("The API did not return JSON objects.")


def extract_api(url: str, output_name: str, fmt: str = "csv") -> str:
    _check_url(url)
    try:
        with requests.get(url, timeout=config.HTTP_TIMEOUT_S, stream=True,
                          headers={"User-Agent": "ride-sharing-data-agent/1.0"}) as resp:
            resp.raise_for_status()
            body = resp.raw.read(config.HTTP_MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as e:
        raise ETLError(f"Request failed: {e}") from None
    if len(body) > config.HTTP_MAX_BYTES:
        raise ETLError(f"Response is larger than the {config.HTTP_MAX_BYTES // (1024 * 1024)} MB limit.")
    try:
        payload = json.loads(body)
    except ValueError:
        raise ETLError("The response is not JSON.") from None

    df = pd.json_normalize(_records(payload))
    path = output_path("extract", output_name, fmt)
    write_file(df, path)
    return f"Extracted {len(df):,} rows x {len(df.columns)} columns to {path.relative_to(config.PROJECT_ROOT).as_posix()}"


# ------------------------------------------------------------------ transform plan
FilterOp = Literal["==", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains", "is_null", "not_null"]


class Select(BaseModel):
    op: Literal["select"]
    columns: list[str]


class Rename(BaseModel):
    op: Literal["rename"]
    mapping: dict[str, str]


class Filter(BaseModel):
    op: Literal["filter"]
    column: str
    operator: FilterOp
    value: str | float | int | bool | list[str | float | int] | None = None


class DropDuplicates(BaseModel):
    op: Literal["drop_duplicates"]
    subset: list[str] | None = None


class DropNulls(BaseModel):
    op: Literal["drop_nulls"]
    subset: list[str] | None = None


class Cast(BaseModel):
    op: Literal["cast"]
    column: str
    dtype: Literal["int", "float", "str", "bool", "datetime"]


class Metric(BaseModel):
    column: str
    func: Literal["sum", "mean", "min", "max", "count", "nunique"]
    name: str


class Aggregate(BaseModel):
    op: Literal["aggregate"]
    group_by: list[str]
    metrics: list[Metric]


class Sort(BaseModel):
    op: Literal["sort"]
    by: list[str]
    ascending: bool = True


class Limit(BaseModel):
    op: Literal["limit"]
    n: int = Field(gt=0, le=1_000_000)


Step = Annotated[Union[Select, Rename, Filter, DropDuplicates, DropNulls, Cast, Aggregate, Sort, Limit],
                 Field(discriminator="op")]


class TransformPlan(BaseModel):
    """An ordered list of transform steps. Only these step types exist."""
    steps: list[Step] = Field(min_length=1)
    summary: str = Field(description="One sentence describing what the plan does.")


def _need(df: pd.DataFrame, columns, step: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ETLError(f"Step '{step}' uses unknown column(s) {missing}. Available: {list(df.columns)}")


def apply_plan(df: pd.DataFrame, plan: TransformPlan) -> pd.DataFrame:
    for step in plan.steps:
        if isinstance(step, Select):
            _need(df, step.columns, "select")
            df = df[step.columns]
        elif isinstance(step, Rename):
            _need(df, step.mapping, "rename")
            df = df.rename(columns=step.mapping)
        elif isinstance(step, Filter):
            _need(df, [step.column], "filter")
            col, v = df[step.column], step.value
            if step.operator == "is_null":
                mask = col.isna()
            elif step.operator == "not_null":
                mask = col.notna()
            elif step.operator in ("in", "not_in"):
                values = v if isinstance(v, list) else [v]
                mask = col.isin(values)
                mask = ~mask if step.operator == "not_in" else mask
            elif step.operator == "contains":
                mask = col.astype(str).str.contains(str(v), case=False, regex=False, na=False)
            else:
                if pd.api.types.is_numeric_dtype(col) and isinstance(v, str):
                    v = pd.to_numeric(v)
                mask = {"==": col == v, "!=": col != v, ">": col > v, ">=": col >= v,
                        "<": col < v, "<=": col <= v}[step.operator]
            df = df[mask]
        elif isinstance(step, DropDuplicates):
            if step.subset:
                _need(df, step.subset, "drop_duplicates")
            df = df.drop_duplicates(subset=step.subset)
        elif isinstance(step, DropNulls):
            if step.subset:
                _need(df, step.subset, "drop_nulls")
            df = df.dropna(subset=step.subset)
        elif isinstance(step, Cast):
            _need(df, [step.column], "cast")
            df = df.copy()
            if step.dtype == "datetime":
                df[step.column] = pd.to_datetime(df[step.column], errors="coerce")
            elif step.dtype in ("int", "float"):
                df[step.column] = pd.to_numeric(df[step.column], errors="coerce")
                if step.dtype == "int":
                    df[step.column] = df[step.column].astype("Int64")
            else:
                df[step.column] = df[step.column].astype(step.dtype)
        elif isinstance(step, Aggregate):
            _need(df, step.group_by + [m.column for m in step.metrics], "aggregate")
            named = {m.name: (m.column, m.func) for m in step.metrics}
            df = df.groupby(step.group_by, dropna=False).agg(**named).reset_index()
        elif isinstance(step, Sort):
            _need(df, step.by, "sort")
            df = df.sort_values(step.by, ascending=step.ascending)
        elif isinstance(step, Limit):
            df = df.head(step.n)
    return df.reset_index(drop=True)


def describe(df: pd.DataFrame, rows: int = 3) -> str:
    cols = ", ".join(f"{c} ({t})" for c, t in df.dtypes.astype(str).items())
    return f"{len(df):,} rows. Columns: {cols}\nFirst rows:\n{df.head(rows).to_string(index=False)}"
