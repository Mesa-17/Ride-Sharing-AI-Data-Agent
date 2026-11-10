"""Central settings, read once from the environment (.env is loaded automatically).

Two database identities are used on purpose:
  * the ADMIN user creates tables and loads data (feed_db.py only)
  * the AGENT user is a read-only role the SQL agent connects with
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"


def _env(name: str, *fallbacks: str, default: str | None = None) -> str | None:
    """Read NAME, then any older names this project used, then the default."""
    for key in (name, *fallbacks):
        value = os.environ.get(key)
        if value not in (None, ""):
            return value
    return default


def _require(value: str | None, name: str) -> str:
    if value is None:
        raise RuntimeError(f"Missing setting {name}. Copy .env.example to .env and fill it in.")
    return value


@dataclass(frozen=True)
class DBSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str | None = None   # "require" for hosted Postgres such as Neon

    def connect_kwargs(self) -> dict:
        kwargs = {"host": self.host, "port": self.port, "dbname": self.dbname,
                  "user": self.user, "password": self.password}
        if self.sslmode:
            kwargs["sslmode"] = self.sslmode
        return kwargs


def admin_db() -> DBSettings:
    return DBSettings(
        host=_env("DB_HOST", "host", default="localhost"),
        port=int(_env("DB_PORT", "port", default="5432")),
        dbname=_env("DB_NAME", "database", default="postgres"),
        user=_require(_env("DB_ADMIN_USER", "user"), "DB_ADMIN_USER"),
        password=_require(_env("DB_ADMIN_PASSWORD", "password"), "DB_ADMIN_PASSWORD"),
        sslmode=_env("DB_SSLMODE"),
    )


def agent_db() -> DBSettings:
    return DBSettings(
        host=_env("DB_HOST", "host", default="localhost"),
        port=int(_env("DB_PORT", "port", default="5432")),
        dbname=_env("DB_NAME", "database", default="postgres"),
        user=_env("DB_AGENT_USER", default="agent_readonly"),
        password=_require(_env("DB_AGENT_PASSWORD"), "DB_AGENT_PASSWORD"),
        sslmode=_env("DB_SSLMODE"),
    )


# Guardrail limits
DB_SCHEMA = _env("DB_SCHEMA", default="public")
MAX_ROWS = int(_env("AGENT_MAX_ROWS", default="100"))              # hard cap on rows a query may return
STATEMENT_TIMEOUT_MS = int(_env("AGENT_STATEMENT_TIMEOUT_MS", default="5000"))
MAX_SQL_ATTEMPTS = int(_env("AGENT_MAX_SQL_ATTEMPTS", default="3"))  # first try + 2 self-corrections

# Models: a cheap tier for wording, a stronger tier for SQL and judging
MODEL_LOW = _env("LLM_MODEL_LOW", default="claude-haiku-4-5")
MODEL_MEDIUM = _env("LLM_MODEL_MEDIUM", default="claude-sonnet-5")
MODEL_HIGH = _env("LLM_MODEL_HIGH", default="claude-opus-5")

# ETL limits
HTTP_TIMEOUT_S = int(_env("ETL_HTTP_TIMEOUT_S", default="20"))
HTTP_MAX_BYTES = int(_env("ETL_HTTP_MAX_BYTES", default=str(20 * 1024 * 1024)))
