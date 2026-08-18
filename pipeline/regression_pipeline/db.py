"""
db.py — Isolated SQLite database for the corrected regression pipeline.

Lives entirely separate from research.sqlite / purified_results so this
methodology correction can never corrupt or be blocked by the older
pipeline's data. Safe to delete and rebuild from research.sqlite at any time
via candidates.discover().
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config as C

REGRESSION_DIR = C.WORK_ROOT / "regression_pipeline"
DB_PATH = REGRESSION_DIR / "regression.sqlite"

DDL = """
-- Candidate funnel: every PR we know about and its current processing status.
CREATE TABLE IF NOT EXISTS candidates (
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    pr_url          TEXT,
    pr_title        TEXT,
    dependency      TEXT,
    old_version     TEXT,
    new_version     TEXT,
    ecosystem       TEXT,
    before_sha      TEXT,
    after_sha       TEXT,
    cohort          TEXT,
    status          TEXT DEFAULT 'PENDING',   -- PENDING | RUNNING | DONE
    discovered_at   TEXT DEFAULT (datetime('now')),
    claimed_at      TEXT,   -- set when status becomes RUNNING; lets recovery
                             -- distinguish a genuinely orphaned claim (old
                             -- timestamp) from one a sibling worker is
                             -- actively processing right now.
    PRIMARY KEY (repo, pr_number)
);

-- Full evidence + verdict for every candidate that was actually run.
CREATE TABLE IF NOT EXISTS results (
    repo                        TEXT NOT NULL,
    pr_number                   INTEGER NOT NULL,
    dependency                  TEXT,
    old_version                 TEXT,
    new_version                 TEXT,
    ecosystem                   TEXT,
    before_sha                  TEXT,
    after_sha                   TEXT,
    sha_pair_verified           INTEGER,
    python_version_requested    TEXT,
    python_version_actual       TEXT,
    execution_strategy          TEXT,   -- pytest_real | import_smoke | none
    execution_detail            TEXT,
    before_install_result       TEXT,
    before_install_excerpt      TEXT,
    before_execution_result     TEXT,
    before_execution_excerpt    TEXT,
    after_install_result        TEXT,
    after_install_excerpt       TEXT,
    after_execution_result      TEXT,
    after_execution_excerpt     TEXT,
    classification               TEXT,
    tier                         TEXT,  -- TIER_3_CONFIRMED | TIER_2_INSTALL_REGRESSION | CONTROL | REJECTED
    reason                       TEXT,  -- human-readable justification for the tier/classification
    duration_seconds             REAL,
    log_dir                      TEXT,
    created_at                   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (repo, pr_number)
);

-- Cached BEFORE-snapshot outcome, keyed by the exact commit.
-- 546 queued candidates repeat a before_sha another candidate already
-- covers (one sha appears 18 times), and re-installing + re-testing the
-- identical commit that many times is pure wasted wall-clock. The BEFORE
-- state of a given commit is a property of that commit, so it is computed
-- once and reused.
CREATE TABLE IF NOT EXISTS baseline_cache (
    repo                TEXT NOT NULL,
    before_sha          TEXT NOT NULL,
    install_result      TEXT,
    install_excerpt     TEXT,
    execution_strategy  TEXT,
    execution_detail    TEXT,
    execution_result    TEXT,
    execution_excerpt   TEXT,
    evidence_strength   TEXT,
    import_targets_json TEXT,
    failure_stage       TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (repo, before_sha)
);

CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_repo   ON candidates(repo);
CREATE INDEX IF NOT EXISTS idx_results_tier       ON results(tier);
CREATE INDEX IF NOT EXISTS idx_results_class      ON results(classification);
"""


def get_connection() -> sqlite3.Connection:
    REGRESSION_DIR.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit. Python's sqlite3 otherwise opens an
    # implicit transaction before every INSERT/UPDATE, which then collides with
    # the explicit "BEGIN IMMEDIATE" in claim_next_pending and raises
    # "cannot start a transaction within a transaction". That killed all three
    # workers via the consecutive-error guard. In autocommit mode our explicit
    # BEGIN IMMEDIATE is the only transaction control, which is exactly what
    # the multi-worker claim needs.
    conn = sqlite3.connect(str(DB_PATH), timeout=120.0, check_same_thread=False,
                            isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()]
    if "claimed_at" not in cols:
        conn.execute("ALTER TABLE candidates ADD COLUMN claimed_at TEXT")

    if "priority" not in cols:
        # Lower = claimed first. CI-verified green->red candidates are worth
        # far more per unit of local compute than the older unfiltered pool,
        # so workers must drain them first rather than in discovery order.
        conn.execute("ALTER TABLE candidates ADD COLUMN priority INTEGER DEFAULT 5")

    rcols = [r[1] for r in conn.execute("PRAGMA table_info(results)").fetchall()]
    for col, decl in (("evidence_strength", "TEXT"),
                       ("confirmation", "TEXT"),
                       ("attempts", "INTEGER DEFAULT 1"),
                       ("baseline_reused", "INTEGER DEFAULT 0")):
        if col not in rcols:
            conn.execute(f"ALTER TABLE results ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def safe_write(conn: sqlite3.Connection, sql: str, params: tuple = (), retries: int = 10, base_delay: float = 0.5) -> None:
    """Execute a write with lock-retry backoff (mirrors pipeline/db.py's helper)."""
    for attempt in range(retries):
        try:
            conn.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(min(base_delay * (1.5 ** attempt), 3.0))
            else:
                raise


def claim_next_pending(conn: sqlite3.Connection, retries: int = 10, base_delay: float = 0.5):
    """
    Atomically claim one PENDING candidate and flip it to RUNNING. Safe for
    multiple concurrent 'run' processes sharing this database.

    Uses an explicit BEGIN IMMEDIATE, which takes SQLite's write lock before
    any reads happen in this transaction. That's a stronger guarantee than a
    single UPDATE-with-subquery statement: a plain "UPDATE ... WHERE rowid =
    (SELECT ...)" can still let two connections evaluate the subquery against
    the same pre-lock snapshot and both target the same row (observed in
    practice, not just theoretical). BEGIN IMMEDIATE forces every other
    writer to fully wait until this transaction commits before it can even
    start reading for its own claim, which is what actually serializes them.

    Returns a sqlite3.Row for the claimed candidate, or None if the queue is
    genuinely empty.
    """
    for attempt in range(retries):
        try:
            # Defensive: never try to nest a transaction, whatever a caller
            # may have left open.
            if conn.in_transaction:
                conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if ("locked" in msg or "within a transaction" in msg) and attempt < retries - 1:
                time.sleep(min(base_delay * (1.5 ** attempt), 3.0))
                continue
            raise
        try:
            row = conn.execute(
                "SELECT * FROM candidates WHERE status='PENDING' "
                "ORDER BY COALESCE(priority, 5), rowid LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE candidates SET status='RUNNING', claimed_at=datetime('now') WHERE repo=? AND pr_number=?",
                (row["repo"], row["pr_number"]),
            )
            conn.commit()
            return row
        except Exception:
            conn.rollback()
            raise
    return None
