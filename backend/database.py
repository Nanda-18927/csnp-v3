"""Connection factory, schema creation and first-run seeding.

The schema (``database/schema.sql``) and the seed data (``database/seed.sql``) are the
single source of truth for the database; this module only executes them.

Paths are resolved relative to the repository root, so the server can be
started from any working directory.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Repository root = parent of the ``backend`` package.
ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "database"
FRONTEND_FILE = ROOT / "frontend" / "index.html"

#: Override with the CSN_DB_PATH environment variable (useful for tests).
DB_PATH = Path(os.environ.get("CSN_DB_PATH") or (ROOT / "backend" / "crimesight.db"))

# Columns added to ``users`` after the first release. Kept for databases that
# were created by an older version of this project.
_USER_COLUMNS = (
    ("badge", "TEXT"),
    ("rank", "TEXT"),
    ("station_name", "TEXT"),
    ("district", "TEXT"),
    ("state", "TEXT"),
    ("squad", "TEXT"),
    ("official_id", "TEXT"),
    ("designation", "TEXT"),
    ("department", "TEXT"),
    ("zone", "TEXT"),
    ("email", "TEXT"),
    ("phone", "TEXT"),
    ("session_token", "TEXT"),
)


#: Columns added to ``criminals`` by the South India dataset import.
_CRIMINAL_COLUMNS = (
    ("gender", "TEXT"),
    ("city", "TEXT"),
    ("state", "TEXT"),
    ("photo_url", "TEXT"),
    ("crime_type", "TEXT"),
    ("status", "TEXT"),
    ("station", "TEXT"),
    ("officer", "TEXT"),
    ("case_ref", "TEXT"),
    ("lat", "REAL"),
    ("lng", "REAL"),
)

#: Built after the column migration, never in schema.sql: on an existing
#: database the users table may not yet have these columns when the schema
#: script runs.
_CONTACT_INDEXES = (
    ("idx_users_email",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
     "ON users(email) WHERE email IS NOT NULL AND email != ''"),
    ("idx_users_phone",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone "
     "ON users(phone) WHERE phone IS NOT NULL AND phone != ''"),
)


#: Below this many stations the database is still the 25-row demo seed, so the
#: South India import is (re)applied.
SOUTH_STATION_FLOOR = 1000


def dict_factory(cursor, row):
    """Convert a sqlite3 row into a plain dict so jsonify() can serialise it."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_db():
    """Open a connection that returns dicts instead of tuples.

    The caller owns the connection and must close it (``try/finally``).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = dict_factory
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _read_sql(name: str) -> str:
    path = SQL_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing SQL file: {path}. Run the server from a full checkout "
            "of the repository."
        )
    return path.read_text(encoding="utf-8")


def _migrate_users(conn) -> None:
    """Add any missing profile columns to a legacy ``users`` table."""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not table:
        return
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for col, typedef in _USER_COLUMNS:
        if col not in existing:
            # Column names come from the _USER_COLUMNS allow-list above, never
            # from user input, so this interpolation is safe.
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typedef}")
    # contact columns now exist, so the lookup indexes can finally be built
    for _name, _ddl in _CONTACT_INDEXES:
        conn.execute(_ddl)
    # the live session token of each account, so a request can be traced back
    # to the citizen who raised it
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_session ON users(session_token)"
    )


def _migrate_criminals(conn) -> None:
    """Add the dataset columns to a ``criminals`` table created by an older run."""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='criminals'"
    ).fetchone()
    if not table:
        return
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(criminals)")}
    for col, typedef in _CRIMINAL_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE criminals ADD COLUMN {col} {typedef}")


def table_counts_from(conn) -> dict:
    """Row counts per table using an open connection."""
    out = {}
    for name in ("police_stations", "officers", "criminals", "cases",
                 "users", "criminal_links"):
        try:
            out[name] = conn.execute(f"SELECT COUNT(*) AS c FROM {name}").fetchone()["c"]
        except sqlite3.OperationalError:      # table missing on an old database
            out[name] = 0
    return out


def init_db(load_south_india: bool | None = None) -> Path:
    """Create the schema and, on a brand new database, load the seed data.

    Safe to call more than once: ``schema.sql`` uses ``IF NOT EXISTS`` and the
    seed script uses ``INSERT OR IGNORE``.

    ``load_south_india`` controls the real station/criminal import. It defaults
    to the ``CSN_SOUTH_INDIA`` environment variable (on unless set to "0"), and
    the smoke tests pass ``False`` so they keep asserting the 25-row seed.
    """
    if load_south_india is None:
        load_south_india = os.environ.get("CSN_SOUTH_INDIA", "1").lower() not in (
            "0", "false", "no"
        )
    conn = get_db()
    try:
        conn.executescript(_read_sql("schema.sql"))
        conn.commit()

        row = conn.execute("SELECT COUNT(*) AS c FROM police_stations").fetchone()
        if row["c"] == 0:
            conn.executescript(_read_sql("seed.sql"))
            conn.commit()
            print(
                f"[*] Seeded {DB_PATH.name}: 25 stations, 20 officers, "
                "11 criminals, 75 FIR cases, 3 demo users."
            )

        _migrate_users(conn)
        conn.commit()

        # South India intelligence import (real station + criminal datasets).
        # Runs only while the database is still the small demo dataset, so an
        # already-populated database is never touched.
        _migrate_criminals(conn)
        conn.commit()
        row = conn.execute("SELECT COUNT(*) AS c FROM police_stations").fetchone()
        south_sql = SQL_DIR / "south_india.sql"
        if load_south_india and row["c"] < SOUTH_STATION_FLOOR and south_sql.is_file():
            conn.executescript(south_sql.read_text(encoding="utf-8"))
            conn.commit()
            counts = table_counts_from(conn)
            print(
                f"[*] South India dataset loaded: "
                f"{counts['police_stations']:,} stations, "
                f"{counts['criminals']:,} criminals, "
                f"{counts['criminal_links']:,} relations."
            )
    finally:
        conn.close()
    return DB_PATH


def table_counts() -> dict:
    """Row counts per table — handy for smoke tests and /api/stats."""
    conn = get_db()
    try:
        return table_counts_from(conn)
    finally:
        conn.close()
