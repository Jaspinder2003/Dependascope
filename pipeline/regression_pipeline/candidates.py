"""
candidates.py — Discover Python STRICT single-dependency candidates from the
existing research.sqlite (read-only) and queue them into this pipeline's own
isolated database. research.sqlite and its tables (pull_requests,
purified_results, final_results, ...) are never written to from here.
"""
from __future__ import annotations
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config as C
import db as main_db

from regression_pipeline import db as rdb

# Standard Dependabot PR title formats:
#   "Bump requests from 2.32.3 to 2.33.0"
#   "Build(deps-dev): Bump vite from 8.1.4 to 8.1.5"
#   "chore(deps): update python-dateutil requirement from >=2.9.0 to >=2.9.0.post0"
_TITLE_BUMP_RE = re.compile(r"bump\s+(?P<dep>[\w@./-]+)\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)", re.IGNORECASE)
_TITLE_UPDATE_REQ_RE = re.compile(
    r"update\s+(?P<dep>[\w@./-]+)\s+requirement\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)", re.IGNORECASE)

# Same heuristic purified_reproduce.classify_cohort() uses: these words in the
# title signal a grouped multi-dependency update, which breaks the
# single-cause requirement (we could no longer say *which* dependency
# was responsible for a regression).
_GROUPED_TITLE_RE = re.compile(r"(?i)\bgroup\b|\bupdates\b|\bdependencies\b|\bpackages\b")


def _parse_title_dependency(title: str) -> tuple[str | None, str | None, str | None]:
    if not title:
        return None, None, None
    for pattern in (_TITLE_BUMP_RE, _TITLE_UPDATE_REQ_RE):
        m = pattern.search(title)
        if m:
            strip = lambda s: s.rstrip(",.;:")
            return strip(m.group("dep")), strip(m.group("old")), strip(m.group("new"))
    return None, None, None


# Max candidates kept per repository. The original stage6789_reproduce.py had
# --max-per-repo (default 5) for this reason and the rewrite lost it: 1,046 of
# 1,596 queued candidates shared a repo with another candidate, one repo
# contributing 46. That is both wasted compute (a repo with a broken baseline
# fails identically for all 46) and a dataset-validity problem — confirmed
# cases clustered in two or three repos would be weak evidence in a paper.
MAX_PER_REPO = 3


def prune_over_cap(conn=None) -> int:
    """
    Apply MAX_PER_REPO to candidates already queued. Excess candidates are
    marked SKIPPED_REPO_CAP (preserved with a reason, never deleted, per the
    funnel-auditability requirement). Already-processed (DONE) candidates
    count toward the cap and are never touched.
    """
    own = conn is None
    conn = conn or rdb.get_connection()
    repos = conn.execute(
        "SELECT repo, COUNT(*) c FROM candidates WHERE status IN ('PENDING','DONE') "
        "GROUP BY repo HAVING c > ?", (MAX_PER_REPO,)
    ).fetchall()

    pruned = 0
    for r in repos:
        keep = [row["pr_number"] for row in conn.execute(
            "SELECT pr_number FROM candidates WHERE repo=? AND status IN ('PENDING','DONE') "
            "ORDER BY CASE status WHEN 'DONE' THEN 0 ELSE 1 END, rowid LIMIT ?",
            (r["repo"], MAX_PER_REPO)
        ).fetchall()]
        placeholders = ",".join("?" for _ in keep) or "NULL"
        cur = conn.execute(
            f"UPDATE candidates SET status='SKIPPED_REPO_CAP' "
            f"WHERE repo=? AND status='PENDING' AND pr_number NOT IN ({placeholders})",
            (r["repo"], *keep)
        )
        pruned += cur.rowcount or 0
    conn.commit()
    if own:
        conn.close()
    return pruned


def discover(limit: int = 500) -> int:
    """Pull up to `limit` new Python STRICT single-dependency candidates from
    research.sqlite into the candidates table as PENDING. Returns count
    newly inserted (grouped-update titles are skipped, not counted)."""
    main_conn = main_db.get_connection(C.DB_PATH)
    main_conn.row_factory = sqlite3.Row
    rconn = rdb.get_connection()

    # ecosystem must be an explicit, confirmed 'pip'/'python'/'pypi' — NULL is
    # NOT treated as "probably Python". A large fraction of ecosystem-NULL
    # rows in research.sqlite are actually npm/cargo/etc. that never got
    # classified, and admitting them here would silently violate the
    # Python-only scope of this research.
    # Two passes. CI-verified green->red rows are pulled in FIRST and are not
    # subject to the newest-N window: they are scarce and expensive to collect,
    # and a plain "ORDER BY created_at DESC LIMIT n" silently dropped them
    # whenever the general pool was larger than the window (observed with
    # toolsforexperiments/plottr #428, a valid STRICT pip candidate).
    select_sql = """
        SELECT p.repo, p.pr_number, p.pr_url, p.title, p.before_sha, p.head_sha,
               p.ecosystem, p.strict_or_complex, p.source_file,
               d.dependency, d.old_version, d.new_version
        FROM pull_requests p
        LEFT JOIN dependency_changes d ON d.repo = p.repo AND d.pr_number = p.pr_number
        WHERE p.before_sha IS NOT NULL AND p.head_sha IS NOT NULL
          AND p.ecosystem IN ('pip', 'python', 'pypi')
          AND p.strict_or_complex IN ('STRICT', 'STRICT_SINGLE')
          AND {src_clause}
        ORDER BY p.created_at DESC
        LIMIT ?
    """
    rows = main_conn.execute(
        select_sql.format(src_clause="p.source_file = 'ci_verified_green_to_red'"), (100000,)
    ).fetchall()
    rows += main_conn.execute(
        select_sql.format(src_clause="COALESCE(p.source_file,'') != 'ci_verified_green_to_red'"), (limit,)
    ).fetchall()

    # Existing per-repo counts so the cap spans previous discover() runs too.
    repo_counts = {
        row["repo"]: row["c"] for row in rconn.execute(
            "SELECT repo, COUNT(*) c FROM candidates WHERE status IN ('PENDING','RUNNING','DONE') GROUP BY repo"
        ).fetchall()
    }

    inserted = 0
    skipped_grouped = 0
    skipped_cap = 0
    for r in rows:
        exists = rconn.execute(
            "SELECT 1 FROM candidates WHERE repo=? AND pr_number=?", (r["repo"], r["pr_number"])
        ).fetchone()
        if exists:
            continue

        title = r["title"] or ""
        if _GROUPED_TITLE_RE.search(title):
            skipped_grouped += 1
            continue

        if repo_counts.get(r["repo"], 0) >= MAX_PER_REPO:
            skipped_cap += 1
            continue
        repo_counts[r["repo"]] = repo_counts.get(r["repo"], 0) + 1

        dependency, old_version, new_version = r["dependency"], r["old_version"], r["new_version"]
        if not dependency:
            dependency, old_version, new_version = _parse_title_dependency(title)

        # CI-verified green->red candidates jump the queue: they cost the same
        # local compute as any other candidate but are far likelier to yield.
        priority = 1 if r["source_file"] == "ci_verified_green_to_red" else 5
        rdb.safe_write(rconn, """
            INSERT INTO candidates
            (repo, pr_number, pr_url, pr_title, dependency, old_version, new_version,
             ecosystem, before_sha, after_sha, cohort, status, priority)
            VALUES (?,?,?,?,?,?,?,?,?,?,?, 'PENDING', ?)
        """, (r["repo"], r["pr_number"], r["pr_url"], title, dependency,
              old_version, new_version, "pip", r["before_sha"], r["head_sha"],
              r["strict_or_complex"], priority))
        inserted += 1

    rconn.commit()
    main_conn.close()
    rconn.close()
    import logging
    log = logging.getLogger("regression_pipeline")
    if skipped_grouped:
        log.info(f"Skipped {skipped_grouped} grouped-update titles (multi-dependency, breaks single-cause requirement)")
    if skipped_cap:
        log.info(f"Skipped {skipped_cap} candidates over the {MAX_PER_REPO}-per-repo cap "
                 f"(avoids one repo dominating the dataset)")
    return inserted
