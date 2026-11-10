import pytest

from utils.sql_guard import FIXABLE, HARD, check_sql

TABLES = {"users", "vehicles", "rides", "payments", "ratings"}


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM rides WHERE status = 'completed'",
    "SELECT u.city, AVG(r.fare) FROM rides r JOIN users u ON u.user_id = r.rider_id GROUP BY 1",
    "WITH c AS (SELECT rider_id FROM rides) SELECT COUNT(*) FROM c",
    "SELECT status FROM rides UNION SELECT payment_status FROM payments",
    "```sql\nSELECT 1 FROM rides\n```".strip("`").removeprefix("sql").strip(),
])
def test_read_queries_pass(sql):
    assert check_sql(sql, TABLES).ok


@pytest.mark.parametrize("sql", [
    "DELETE FROM rides",
    "UPDATE users SET city = 'Toronto'",
    "INSERT INTO users (user_id) VALUES (1)",
    "DROP TABLE payments",
    "TRUNCATE ratings",
    "CREATE TABLE rides_backup AS SELECT * FROM rides",
    "SELECT * INTO rides_backup FROM rides",
    "WITH gone AS (DELETE FROM rides RETURNING *) SELECT * FROM gone",
    "SELECT 1; DROP TABLE users",
    "SELECT pg_sleep(60)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM rides FOR UPDATE",
    "COPY rides TO '/tmp/rides.csv'",
    "SELECT set_config('statement_timeout', '0', false)",
])
def test_writes_and_dangerous_calls_are_hard_blocks(sql):
    r = check_sql(sql, TABLES)
    assert not r.ok and r.kind == HARD, r


@pytest.mark.parametrize("sql", [
    "SELECT * FROM ride",                       # typo in table name
    "SELECT * FROM pg_catalog.pg_shadow",      # outside the allowed schema
    "SELECT * FROM information_schema.tables",
    "SELEC * FROM rides",                       # syntax error
])
def test_fixable_problems_go_back_to_the_agent(sql):
    r = check_sql(sql, TABLES)
    assert not r.ok and r.kind == FIXABLE, r


def test_limit_is_added_and_capped():
    assert check_sql("SELECT * FROM rides", TABLES, max_rows=50).sql.endswith("LIMIT 50")
    assert check_sql("SELECT * FROM rides LIMIT 100000", TABLES, max_rows=50).sql.endswith("LIMIT 50")
    assert check_sql("SELECT * FROM rides LIMIT 5", TABLES, max_rows=50).sql.endswith("LIMIT 5")
