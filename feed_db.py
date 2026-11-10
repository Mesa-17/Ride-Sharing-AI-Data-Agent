"""Create the ride-sharing tables, load the CSVs and set up the agent's read-only role.

Run it as many times as you like: the load replaces the tables' contents inside one
transaction, so a rerun never duplicates rows and a failure never leaves a half-load.

    python feed_db.py            # tables + data + read-only role
    python feed_db.py --no-role  # skip the role (e.g. your user cannot create roles)
"""
import argparse

import psycopg2
from psycopg2 import sql

import config

SCHEMA = config.DB_SCHEMA

# Load order matters: parents before children (foreign keys).
TABLES = {
    "users": ["user_id", "first_name", "last_name", "email", "phone", "city", "province",
              "user_type", "signup_date", "is_active"],
    "vehicles": ["vehicle_id", "driver_id", "make", "model", "year", "license_plate", "color", "is_active"],
    "rides": ["ride_id", "rider_id", "driver_id", "requested_at", "pickup_time", "dropoff_time",
              "pickup_latitude", "pickup_longitude", "dropoff_latitude", "dropoff_longitude",
              "distance_km", "fare", "surge_multiplier", "status", "cancellation_reason"],
    "payments": ["payment_id", "ride_id", "user_id", "amount", "payment_method", "payment_status",
                 "transaction_id", "payment_time"],
    "ratings": ["rating_id", "ride_id", "rider_id", "driver_id", "rating", "comment", "rated_at"],
}


def create_tables(cur) -> None:
    cur.execute((config.PROJECT_ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))


def load_tables(cur) -> None:
    # Replace, don't append: rerunning the load gives the same result every time.
    cur.execute(sql.SQL("TRUNCATE {} CASCADE").format(
        sql.SQL(", ").join(sql.Identifier(SCHEMA, t) for t in reversed(list(TABLES)))))
    for table, columns in TABLES.items():
        path = config.DATA_DIR / f"{table}.csv"
        if not path.exists():
            raise FileNotFoundError(f"CSV file not found: {path}")
        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '')").format(
            sql.Identifier(SCHEMA, table), sql.SQL(", ").join(map(sql.Identifier, columns)))
        with path.open("r", encoding="utf-8") as f:
            cur.copy_expert(copy_sql, f)
        print(f"  loaded {table}")


def create_readonly_role(cur, dbname: str) -> None:
    """A login role that can only read this schema, with read-only sessions and a timeout."""
    agent = config.agent_db()
    role = sql.Identifier(agent.user)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (agent.user,))
    if cur.fetchone():
        cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(role), (agent.password,))
    else:
        cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(role), (agent.password,))
    cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), role))
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(SCHEMA), role))
    cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(sql.Identifier(SCHEMA), role))
    cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(SCHEMA), role))
    cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
    cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = %s").format(role),
                (f"{config.STATEMENT_TIMEOUT_MS}ms",))
    print(f"  read-only role '{agent.user}' is ready")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-role", action="store_true", help="do not create the read-only agent role")
    args = parser.parse_args()

    admin = config.admin_db()
    conn = psycopg2.connect(**admin.connect_kwargs())
    try:
        with conn, conn.cursor() as cur:  # one transaction: all or nothing
            print("Creating tables and loading data...")
            create_tables(cur)
            load_tables(cur)
            if not args.no_role:
                create_readonly_role(cur, admin.dbname)
            print("\nRow counts:")
            for table in TABLES:
                cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(SCHEMA, table)))
                print(f"  {table:<10} {cur.fetchone()[0]:>8,}")
        print("\nDone. Everything was committed in one transaction.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
