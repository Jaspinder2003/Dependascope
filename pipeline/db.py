"""
db.py – SQLite schema and helper functions for the Dependabot Security PR pipeline.

All tables use INSERT OR REPLACE / upsert patterns so every stage is resumable.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
DDL = """
-- ── Main PR registry ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pull_requests (
    repo                        TEXT NOT NULL,
    pr_number                   INTEGER NOT NULL,
    pr_url                      TEXT,
    author                      TEXT,
    title                       TEXT,
    body                        TEXT,
    state                       TEXT,
    created_at                  TEXT,
    merged_at                   TEXT,
    closed_at                   TEXT,

    -- Commit / SHA (from Stage 3A)
    head_sha                    TEXT,
    before_sha                  TEXT,         -- first parent of head commit
    merge_sha                   TEXT,
    commits_count               INTEGER,
    single_commit               INTEGER,      -- 1 if exactly 1 commit with 1 parent

    -- File-level counts (from Stage 3A)
    changed_files               INTEGER,
    additions                   INTEGER,
    deletions                   INTEGER,

    -- Classification
    ecosystem                   TEXT,
    strict_or_complex           TEXT DEFAULT 'UNKNOWN',
    classification_reason       TEXT,
    manifest_diff_json          TEXT,         -- JSON result of structural parse

    -- Labels / metadata
    labels                      TEXT,         -- JSON list
    comments_count              INTEGER,
    failure_keyword_in_comments INTEGER DEFAULT 0,

    -- Priority / cohort
    local_priority_score        REAL,
    local_filter_status         TEXT DEFAULT 'PENDING',
    -- TOP | LOW_PRIORITY | DIGEST | ACTION | RANGE | UNPARSED | GROUPED | SKIP

    -- Processing pipeline state
    api_tier                    TEXT,         -- NULL | '3A' | '3A5' | '3B'
    processing_status           TEXT DEFAULT 'PENDING',
    -- PENDING | LOCAL_SCORED | 3A_DONE | 3A5_DONE | VALIDATED | 3B_DONE | QUEUED | DONE | SKIP

    -- Audit
    audit_sample                INTEGER DEFAULT 0,

    -- Source tracking
    source_dataset              TEXT,
    source_file                 TEXT,

    PRIMARY KEY (repo, pr_number)
);

-- ── Changed files (from Stage 3A) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS changed_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    pr_number   INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    status      TEXT,          -- added | modified | removed | renamed
    additions   INTEGER,
    deletions   INTEGER,
    patch       TEXT,          -- unified diff patch (supporting evidence only; may be truncated)
    is_manifest INTEGER DEFAULT 0,
    is_lockfile INTEGER DEFAULT 0,
    is_source   INTEGER DEFAULT 0,
    is_test     INTEGER DEFAULT 0,
    is_ci       INTEGER DEFAULT 0,
    UNIQUE(repo, pr_number, filename)
);

-- ── Complete manifest content at before/after SHA (from Stage 3A.5) ─────────
CREATE TABLE IF NOT EXISTS manifest_contents (
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    snapshot        TEXT NOT NULL,     -- 'BEFORE' | 'AFTER'
    manifest_path   TEXT NOT NULL,
    content         TEXT,              -- full decoded file text
    fetch_method    TEXT,              -- 'contents_api' | 'blob_api'
    fetched_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (repo, pr_number, snapshot, manifest_path)
);

-- ── Structural dependency changes (from Stage 4) ────────────────────────────
CREATE TABLE IF NOT EXISTS dependency_changes (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo                    TEXT NOT NULL,
    pr_number               INTEGER NOT NULL,
    manifest_path           TEXT,
    ecosystem               TEXT,
    dependency              TEXT,
    old_version             TEXT,
    new_version             TEXT,
    version_change_type     TEXT,   -- patch | minor | major | range | digest | github_action | unknown
    direct_dependency_count INTEGER DEFAULT 1,
    transitive_change_count INTEGER DEFAULT 0,
    parse_confidence        TEXT,   -- high | medium | low | failed
    UNIQUE(repo, pr_number, manifest_path, dependency)
);

-- ── PR discussion (from Stage 3B) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pr_comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo         TEXT NOT NULL,
    pr_number    INTEGER NOT NULL,
    comment_type TEXT NOT NULL,   -- 'issue' | 'review_inline' | 'review_summary'
    comment_id   INTEGER,
    author       TEXT,
    body         TEXT,
    created_at   TEXT,
    UNIQUE(repo, pr_number, comment_type, comment_id)
);

-- ── Execution records (from Stage 6789) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS executions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo             TEXT NOT NULL,
    pr_number        INTEGER NOT NULL,
    snapshot         TEXT NOT NULL,    -- BEFORE | AFTER
    stage            TEXT NOT NULL,
    command          TEXT,
    command_source   TEXT,
    exit_code        INTEGER,
    duration_seconds REAL,
    result           TEXT,             -- PASS | FAIL | TIMEOUT | SKIP | ERROR
    stdout_path      TEXT,
    stderr_path      TEXT,
    docker_image     TEXT,
    runtime_version  TEXT,
    network_policy   TEXT,
    attempt_number   INTEGER DEFAULT 1,
    created_at       TEXT DEFAULT (datetime('now')),
    UNIQUE(repo, pr_number, snapshot, stage, attempt_number)
);

-- ── Final results (from Stage 6789) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS final_results (
    repo                  TEXT NOT NULL,
    pr_number             INTEGER NOT NULL,
    dependency            TEXT,
    old_version           TEXT,
    new_version           TEXT,
    ecosystem             TEXT,
    before_sha            TEXT,
    after_sha             TEXT,
    before_result         TEXT,
    after_result          TEXT,
    classification        TEXT,   -- PASS->PASS | PASS->FAIL | FAIL->FAIL | etc.
    failure_point         TEXT,
    reproduced            INTEGER DEFAULT 0,
    confidence            TEXT,
    reproduction_attempts INTEGER DEFAULT 0,
    network_policy        TEXT,
    log_paths             TEXT,   -- JSON list
    notes                 TEXT,
    duration_seconds      REAL,
    created_at            TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (repo, pr_number)
);

-- ── GitHub API response cache ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS github_cache (
    cache_key    TEXT PRIMARY KEY,
    response_body TEXT NOT NULL,
    cached_at    TEXT DEFAULT (datetime('now')),
    status_code  INTEGER
);

-- ── Pipeline gate enforcement ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_gates (
    gate       TEXT PRIMARY KEY,
    passed     INTEGER DEFAULT 0,
    passed_at  TEXT,
    notes      TEXT
);

-- ── Processing event log ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS processing_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo       TEXT,
    pr_number  INTEGER,
    stage      TEXT,
    message    TEXT,
    level      TEXT DEFAULT 'INFO',
    created_at TEXT DEFAULT (datetime('now'))
);
"""

INDICES = """
CREATE INDEX IF NOT EXISTS idx_pr_status    ON pull_requests(processing_status);
CREATE INDEX IF NOT EXISTS idx_pr_cohort    ON pull_requests(local_filter_status);
CREATE INDEX IF NOT EXISTS idx_pr_strict    ON pull_requests(strict_or_complex);
CREATE INDEX IF NOT EXISTS idx_pr_ecosystem ON pull_requests(ecosystem);
CREATE INDEX IF NOT EXISTS idx_pr_priority  ON pull_requests(local_priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_exec_rp      ON executions(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_final_class  ON final_results(classification);
CREATE INDEX IF NOT EXISTS idx_manifest_rp  ON manifest_contents(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_comments_rp  ON pr_comments(repo, pr_number);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection with 60s lock timeout."""
    conn = sqlite3.connect(str(db_path), timeout=60.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create schema and return connection. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    conn.executescript(DDL)
    conn.executescript(INDICES)
    conn.commit()

    # Migration: Ensure duration_seconds column exists in final_results
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(final_results)").fetchall()]
        if "duration_seconds" not in cols:
            conn.execute("ALTER TABLE final_results ADD COLUMN duration_seconds REAL")
            conn.commit()
            logger.info("Migrated final_results table: added duration_seconds column")
    except Exception as e:
        logger.warning(f"Could not check/migrate duration_seconds column: {e}")

    logger.info("Database initialized at %s", db_path)
    return conn


# ─── Upsert helpers ───────────────────────────────────────────────────────────

def safe_execute_write(conn: sqlite3.Connection, sql: str, params: tuple = (), retries: int = 5, base_delay: float = 1.0) -> None:
    """Execute a write query with lock retry exponential backoff."""
    import time
    for attempt in range(retries):
        try:
            conn.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Database locked in write execution, retrying in {delay:.1f}s (attempt {attempt+1}/{retries})")
                time.sleep(delay)
            else:
                raise


def upsert_pr(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_names    = ", ".join(cols)
    pk = {"repo", "pr_number"}
    # Never overwrite processing_status — once a PR is DONE/PROBED_OK/PROBED_FAIL
    # it must stay that way; re-fetching from GitHub must not reset it.
    protected = pk | {"processing_status"}
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in protected)
    sql = (
        f"INSERT INTO pull_requests ({col_names}) VALUES ({placeholders})"
        + (f" ON CONFLICT(repo, pr_number) DO UPDATE SET {update_clause}" if update_clause else
           " ON CONFLICT(repo, pr_number) DO NOTHING")
    )
    safe_execute_write(conn, sql, [row[c] for c in cols])


def upsert_dep_change(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_names    = ", ".join(cols)
    pk = {"repo", "pr_number", "manifest_path", "dependency"}
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    sql = (
        f"INSERT INTO dependency_changes ({col_names}) VALUES ({placeholders})"
        f" ON CONFLICT(repo, pr_number, manifest_path, dependency) DO UPDATE SET {update_clause}"
    )
    safe_execute_write(conn, sql, [row[c] for c in cols])


def upsert_manifest_content(conn: sqlite3.Connection, repo: str, pr_number: int,
                             snapshot: str, manifest_path: str, content: str,
                             fetch_method: str) -> None:
    sql = """INSERT INTO manifest_contents
           (repo, pr_number, snapshot, manifest_path, content, fetch_method)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(repo, pr_number, snapshot, manifest_path)
           DO UPDATE SET content=excluded.content, fetch_method=excluded.fetch_method,
                         fetched_at=datetime('now')"""
    safe_execute_write(conn, sql, (repo, pr_number, snapshot, manifest_path, content, fetch_method))


def log_event(conn: sqlite3.Connection, repo: str, pr_number: Optional[int],
              stage: str, message: str, level: str = "INFO") -> None:
    safe_execute_write(
        conn,
        "INSERT INTO processing_log (repo, pr_number, stage, message, level) VALUES (?,?,?,?,?)",
        (repo, pr_number, stage, message, level)
    )
    # Intentionally not committing here; caller commits in batches


# ─── Cache helpers ────────────────────────────────────────────────────────────

def cache_get(conn: sqlite3.Connection, key: str) -> Optional[Any]:
    row = conn.execute(
        "SELECT response_body FROM github_cache WHERE cache_key=?", (key,)
    ).fetchone()
    return json.loads(row["response_body"]) if row else None


def cache_set(conn: sqlite3.Connection, key: str, data: Any, status_code: int = 200) -> None:
    sql = """INSERT INTO github_cache (cache_key, response_body, status_code)
           VALUES (?,?,?)
           ON CONFLICT(cache_key) DO UPDATE SET response_body=excluded.response_body,
           cached_at=datetime('now'), status_code=excluded.status_code"""
    safe_execute_write(conn, sql, (key, json.dumps(data, default=str), status_code))


# ─── Gate helpers ─────────────────────────────────────────────────────────────

def gate_is_passed(conn: sqlite3.Connection, gate: str) -> bool:
    row = conn.execute(
        "SELECT passed FROM pipeline_gates WHERE gate=?", (gate,)
    ).fetchone()
    return bool(row and row["passed"])


def gate_set_passed(conn: sqlite3.Connection, gate: str, notes: str = "") -> None:
    conn.execute(
        """INSERT INTO pipeline_gates (gate, passed, passed_at, notes)
           VALUES (?,1,datetime('now'),?)
           ON CONFLICT(gate) DO UPDATE SET passed=1, passed_at=datetime('now'), notes=excluded.notes""",
        (gate, notes)
    )
    conn.commit()
