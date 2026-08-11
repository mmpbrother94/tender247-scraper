"""
Storage for the fresh-tenders-only system.

Deliberately its own database file rather than another table inside
tenders_vault.db: this pipeline has one job -- track the account's Fresh
Results feed -- and keeping it in its own file means the whole thing can be
copied, published, or wiped without touching anything else, and the nightly
upload stays small (a few MB instead of ~750 MB).
"""
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "fresh_tenders.db"

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    result_id                  INTEGER UNIQUE PRIMARY KEY,
    tender_number              TEXT,
    title                      TEXT,
    location                   TEXT,
    organization_name          TEXT,
    organization_type          TEXT,
    tender_value               REAL,
    contract_value             REAL,
    stage                      TEXT,
    winner_bidder_name         TEXT,
    submission_date            TEXT,
    status_update_date         TEXT,
    created_date               TEXT,
    mail_date                  TEXT,
    tender_result_id           INTEGER,
    tender_result_created_date TEXT,
    is_favorite                INTEGER,
    -- when this scraper first saw the row, which is what "new today" means;
    -- status_update_date is tender247's own date and can be far older
    first_seen_at              TEXT DEFAULT CURRENT_TIMESTAMP,
    -- bumped whenever the upstream record changes, so a tender that moves
    -- Technical -> Financial -> AOC is updated rather than frozen at first sight
    last_updated_at            TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tenders_number      ON tenders(tender_number);
CREATE INDEX IF NOT EXISTS idx_tenders_stage       ON tenders(stage);
CREATE INDEX IF NOT EXISTS idx_tenders_first_seen  ON tenders(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_tenders_status_date ON tenders(status_update_date);
CREATE INDEX IF NOT EXISTS idx_tenders_org         ON tenders(organization_name);
CREATE INDEX IF NOT EXISTS idx_tenders_location    ON tenders(location);

-- tender247 only ever exposes a tender's CURRENT stage and the date it reached
-- it -- there is no per-stage date history in any endpoint. But this scraper
-- re-reads the whole feed nightly, so it can observe the transitions itself and
-- build the history tender247 doesn't give: one row per (tender, stage), with
-- the date that stage was reached.
--
-- Only works forwards from the day a tender is first seen. A tender already at
-- AOC when we first meet it has no recoverable Technical/Financial dates,
-- because that information does not exist upstream.
CREATE TABLE IF NOT EXISTS stage_history (
    result_id   INTEGER,
    stage       TEXT,
    stage_date  TEXT,              -- status_update_date reported for that stage
    observed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (result_id, stage),
    FOREIGN KEY (result_id) REFERENCES tenders(result_id)
);
CREATE INDEX IF NOT EXISTS idx_stage_history_result ON stage_history(result_id);

CREATE TABLE IF NOT EXISTS bidders (
    result_id        INTEGER,
    bidder_name      TEXT,
    technical_status INTEGER,
    financial_status INTEGER,
    aoc_status       INTEGER,
    bid_value        REAL,
    bidder_rank      TEXT,
    scraped_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (result_id, bidder_name),
    FOREIGN KEY (result_id) REFERENCES tenders(result_id)
);
CREATE INDEX IF NOT EXISTS idx_bidders_result ON bidders(result_id);
CREATE INDEX IF NOT EXISTS idx_bidders_name   ON bidders(bidder_name);

-- One row per result_id whose bidder list has been fetched, including results
-- that genuinely have none. Without it, "no bidders" and "never fetched" look
-- identical and every run would re-fetch the same results forever.
CREATE TABLE IF NOT EXISTS bidder_fetch_log (
    result_id    INTEGER UNIQUE PRIMARY KEY,
    bidder_count INTEGER,
    fetched_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    new_tenders    INTEGER DEFAULT 0,
    updated_tenders INTEGER DEFAULT 0,
    new_bidders    INTEGER DEFAULT 0,
    seconds        INTEGER DEFAULT 0,
    ok             INTEGER DEFAULT 1,
    note           TEXT DEFAULT ''
);
"""

TENDER_COLUMNS = (
    "result_id", "tender_number", "title", "location", "organization_name",
    "organization_type", "tender_value", "contract_value", "stage",
    "winner_bidder_name", "submission_date", "status_update_date",
    "created_date", "mail_date", "tender_result_id",
    "tender_result_created_date", "is_favorite",
)

#: Fields that can legitimately change after first sight, as a tender moves
#: through Technical -> Financial -> AOC. Everything else is fixed identity.
MUTABLE_COLUMNS = (
    "stage", "winner_bidder_name", "contract_value", "tender_value",
    "status_update_date", "mail_date", "tender_result_id",
    "tender_result_created_date",
)


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    logger.info("Fresh-tenders DB ready at %s", DB_PATH)


def upsert_tenders(records: list[dict]) -> tuple[int, int]:
    """
    Inserts new tenders and updates ones whose stage/winner/values moved.

    Returns (new_count, updated_count). Updating matters here: a fresh tender
    is usually seen first at Technical stage and only later becomes AOC with a
    winner, so a plain INSERT OR IGNORE would leave it permanently stale.
    """
    if not records:
        return 0, 0

    cols = ", ".join(TENDER_COLUMNS)
    binds = ", ".join(f":{c}" for c in TENDER_COLUMNS)
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in MUTABLE_COLUMNS)
    changed = " OR ".join(f"tenders.{c} IS NOT excluded.{c}" for c in MUTABLE_COLUMNS)

    with get_connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
        conn.executemany(
            f"""
            INSERT INTO tenders ({cols}) VALUES ({binds})
            ON CONFLICT(result_id) DO UPDATE SET
                {set_clause},
                last_updated_at = CURRENT_TIMESTAMP
            WHERE {changed}
            """,
            records,
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
        updated = conn.execute(
            "SELECT COUNT(*) FROM tenders WHERE last_updated_at > first_seen_at "
            "AND last_updated_at >= datetime('now', '-2 minutes')"
        ).fetchone()[0]

        # Record whatever stage each tender is in right now. INSERT OR IGNORE
        # keeps the FIRST date we saw for a given (tender, stage) -- so a
        # tender passing Technical -> Financial -> AOC accumulates one row per
        # stage rather than overwriting, which is the history tender247 lacks.
        conn.executemany(
            "INSERT OR IGNORE INTO stage_history (result_id, stage, stage_date) "
            "VALUES (:result_id, :stage, :status_update_date)",
            [r for r in records if r.get("stage")],
        )
        conn.commit()
    return after - before, updated


def stage_history_for(result_ids: list) -> dict:
    """{result_id: {stage: stage_date}} for the given tenders."""
    if not result_ids:
        return {}
    marks = ",".join("?" * len(result_ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT result_id, stage, stage_date, observed_at FROM stage_history "
            f"WHERE result_id IN ({marks})", result_ids,
        ).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["result_id"], {})[row["stage"]] = {
            "date": row["stage_date"], "first_observed": row["observed_at"]
        }
    return out


def insert_bidders(records: list[dict]) -> int:
    if not records:
        return 0
    with get_connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM bidders").fetchone()[0]
        conn.executemany(
            """
            INSERT OR REPLACE INTO bidders
                (result_id, bidder_name, technical_status, financial_status,
                 aoc_status, bid_value, bidder_rank)
            VALUES (:result_id, :bidder_name, :technical_status, :financial_status,
                    :aoc_status, :bid_value, :bidder_rank)
            """,
            records,
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM bidders").fetchone()[0]
    return after - before


def mark_fetched(rows: list[tuple]) -> None:
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bidder_fetch_log (result_id, bidder_count, fetched_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            rows,
        )
        conn.commit()


def result_ids_needing_bidders() -> list[int]:
    """
    Tenders with no bidder list yet, plus any whose stage moved since their
    bidders were last fetched -- a tender reaching AOC gains ranks and bid
    values that were not there at Technical stage.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.result_id FROM tenders t
            LEFT JOIN bidder_fetch_log f ON f.result_id = t.result_id
            WHERE f.result_id IS NULL
               OR t.last_updated_at > f.fetched_at
            ORDER BY t.result_id
            """
        ).fetchall()
    return [r[0] for r in rows]


def log_run(new_tenders: int, updated_tenders: int, new_bidders: int,
            seconds: int, ok: bool = True, note: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO runs (new_tenders, updated_tenders, new_bidders, seconds, ok, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_tenders, updated_tenders, new_bidders, seconds, int(ok), note),
        )
        conn.commit()


def counts() -> dict:
    with get_connection() as conn:
        return {
            "tenders": conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0],
            "bidders": conn.execute("SELECT COUNT(*) FROM bidders").fetchone()[0],
        }
