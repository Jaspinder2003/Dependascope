"""
stage2b_local_prefilter.py
───────────────────────────
Assign a local priority score and cohort label to every PR using only
locally available fields — no GitHub API calls.

Cohort labels (stored in local_filter_status):
  TOP          High local score; version parseable; primary enrichment queue
  LOW_PRIORITY Parseable but weak signals
  DIGEST       SHA digest updates (abc123 -> def456)
  ACTION       GitHub Actions uses: updates
  RANGE        Version specifier/range changes (^1.0.0 -> ^2.0.0)
  UNPARSED     Title present but no version pattern extractable
  GROUPED      Multi-dep grouped updates -> immediately COMPLEX

Only GROUPED becomes COMPLEX immediately; all others are preserved for analysis.
--limit selects the top N by local_priority_score, not insertion order.
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db

LOG_DIR = C.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "stage2b_prefilter.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

# ─── Regex patterns ───────────────────────────────────────────────────────────
_BUMP_FROM_TO = re.compile(
    r"(?i)(?:bump|update|upgrade)\s+(\S+)\s+from\s+(\S+)\s+to\s+(\S+)"
)
_BUMP_TO_ONLY = re.compile(
    r"(?i)(?:bump|update|upgrade)\s+(\S+)\s+to\s+(\S+)"
)
_GROUPED  = re.compile(
    r"(?i)(group\s+with\s+\d+|bump\s+\d+\s+package|update\s+\d+\s+dep"
    r"|multiple\s+dep|several\s+dep|\d+\s+update)"
)
_SECURITY = re.compile(r"(?i)\[security\]")
_DIGEST   = re.compile(r"[0-9a-f]{7,40}")    # looks like a SHA
_RANGE    = re.compile(r"[\^~>=<]")           # version specifier characters

# GitHub Actions uses: owner/action@ref
_ACTION   = re.compile(r"(?i)(actions/|github\.com/)?\S+@(v\d+|[0-9a-f]{40})")


def classify_title(title: str) -> dict:
    """
    Return a dict with:
      dep_name, old_version, new_version  (None if not parseable)
      cohort_hint  one of: bump_from_to | bump_to_only | digest | action | range | grouped | unparsed
      version_type  major | minor | patch | range | digest | github_action | unknown
    """
    t = (title or "").strip()

    # Grouped first (highest priority rejection)
    if _GROUPED.search(t):
        return {"dep": None, "old": None, "new": None,
                "cohort_hint": "grouped", "version_type": "unknown"}

    # Full "Bump X from A to B"
    m = _BUMP_FROM_TO.search(t)
    if m:
        dep, old, new = m.group(1), m.group(2), m.group(3)
        vtype = _classify_version(old, new)
        cohort = "bump_from_to"
        if vtype == "digest":
            cohort = "digest"
        elif _ACTION.search(dep):
            cohort = "action"
        elif _RANGE.search(new):
            cohort = "range"
        return {"dep": dep, "old": old, "new": new,
                "cohort_hint": cohort, "version_type": vtype}

    # "Bump X to B" (no old version)
    m = _BUMP_TO_ONLY.search(t)
    if m:
        dep, new = m.group(1), m.group(2)
        vtype = "unknown"
        cohort = "bump_to_only"
        if _DIGEST.fullmatch(new.lstrip("v")):
            cohort = "digest"
        elif _ACTION.search(dep):
            cohort = "action"
        elif _RANGE.search(new):
            cohort = "range"
        return {"dep": dep, "old": None, "new": new,
                "cohort_hint": cohort, "version_type": vtype}

    # Fallback: unparsed
    return {"dep": None, "old": None, "new": None,
            "cohort_hint": "unparsed", "version_type": "unknown"}


def _classify_version(old: str, new: str) -> str:
    if not old or not new:
        return "unknown"
    old_c = old.lstrip("v")
    new_c = new.lstrip("v")
    # Digest?
    if (_DIGEST.fullmatch(old_c) and _DIGEST.fullmatch(new_c)
            and not old_c.replace(".", "").isdigit()):
        return "digest"
    # Range?
    if _RANGE.search(old_c) or _RANGE.search(new_c):
        return "range"
    try:
        op = [int(x) for x in old_c.split(".")[:3]]
        np = [int(x) for x in new_c.split(".")[:3]]
        while len(op) < 3: op.append(0)
        while len(np) < 3: np.append(0)
        if np[0] != op[0]: return "major"
        if np[1] != op[1]: return "minor"
        if np[2] != op[2]: return "patch"
        return "unknown"
    except Exception:
        return "unknown"


def _cohort_from_hint(hint: str) -> str:
    """Map cohort_hint to a cohort label."""
    mapping = {
        "grouped":      C.COHORT_GROUPED,
        "digest":       C.COHORT_DIGEST,
        "action":       C.COHORT_ACTION,
        "range":        C.COHORT_RANGE,
        "unparsed":     C.COHORT_UNPARSED,
        "bump_from_to": None,   # resolved by score
        "bump_to_only": None,
    }
    return mapping.get(hint, C.COHORT_UNPARSED)


def score_pr_locally(pr: dict) -> tuple[float, str]:
    """
    Compute local_priority_score and cohort label from local fields.
    Returns (score, cohort).
    """
    W     = C.LOCAL_SCORE
    score = 0.0
    title = pr.get("title") or ""
    info  = classify_title(title)
    hint  = info["cohort_hint"]
    vtype = info["version_type"]

    # ── Immediate cohort assignments ──────────────────────────────────────────
    if hint == "grouped":
        return 0.0, C.COHORT_GROUPED

    cohort_override = _cohort_from_hint(hint)

    # ── Score from local signals ───────────────────────────────────────────────
    author = (pr.get("author") or "").lower()
    if author == "dependabot[bot]":
        score += W["author_modern_dependabot"]
    elif "dependabot" in author:
        score += W["author_preview_dependabot"]

    if hint == "bump_from_to":
        score += W["title_bump_from_to"]
    if _SECURITY.search(title):
        score += W["title_security_prefix"]

    commits = pr.get("commits_count")
    if commits is not None:
        if commits == 1:
            score += W["single_commit"]
        elif commits > 1:
            # Multi-commit: won't be STRICT; penalise but keep for COMPLEX
            score -= 2.0

    files = pr.get("changed_files")
    if files is not None:
        if files <= 3:
            score += W["files_lte3"]
            if files == 1:
                score += W["files_eq1"]
        elif files > 10:
            score -= 1.0

    state     = (pr.get("state") or "").lower()
    merged_at = pr.get("merged_at") or ""
    if state == "closed" and not merged_at:
        score += W["closed_not_merged"]

    comments = pr.get("comments_count") or 0
    if comments >= 1:
        score += W["has_comments"]
    if comments >= 3:
        score += W["many_comments"]

    if vtype == "major":
        score += W["major_bump"]
    elif vtype == "minor":
        score += W["minor_bump"]
    elif vtype == "patch":
        score += W["patch_bump"]

    eco = (pr.get("ecosystem") or "").lower()
    if eco in C.SUPPORTED_ECOSYSTEMS:
        score += W["supported_ecosystem"]

    # Archived status — xlsx has Repos.Archived but we may not have it in DB
    # Skip for now; treated as 'unknown' (no penalty)

    # ── Assign final cohort ───────────────────────────────────────────────────
    if cohort_override:
        cohort = cohort_override
    elif score >= C.LOCAL_TOP_MIN_SCORE:
        cohort = C.COHORT_TOP
    elif score >= C.LOCAL_LOW_PRIORITY_MIN:
        cohort = C.COHORT_LOW_PRIORITY
    else:
        cohort = C.COHORT_UNPARSED

    return score, cohort


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    conn = db.init_db(C.DB_PATH)

    # Load all PENDING PRs
    rows = conn.execute(
        "SELECT * FROM pull_requests WHERE processing_status='PENDING'"
    ).fetchall()
    logger.info("Scoring %d PENDING PRs locally ...", len(rows))

    scored: list[tuple[float, str, str, int]] = []   # (score, cohort, repo, pr_number)

    batch = []
    for row in rows:
        pr = dict(row)
        score, cohort = score_pr_locally(pr)

        immediate_complex = (cohort == C.COHORT_GROUPED)
        batch.append((
            score, cohort,
            "COMPLEX" if immediate_complex else "UNKNOWN",
            "grouped_update" if immediate_complex else "",
            pr["repo"], pr["pr_number"]
        ))
        scored.append((score, cohort, pr["repo"], pr["pr_number"]))

        if len(batch) >= 5000:
            _flush_batch(conn, batch)
            batch.clear()

    if batch:
        _flush_batch(conn, batch)
    conn.commit()

    # ── Assign TOP / LOW_PRIORITY by rank (respects --limit) ─────────────────
    # Sort by score descending; assign TOP to top N if --top flag given
    scored.sort(key=lambda x: x[0], reverse=True)

    if args.top:
        top_set = {(repo, pr_num) for _, _, repo, pr_num in scored[:args.top]}
        for _, cohort, repo, pr_num in scored:
            if cohort in (C.COHORT_TOP, C.COHORT_LOW_PRIORITY):
                new_cohort = C.COHORT_TOP if (repo, pr_num) in top_set else C.COHORT_LOW_PRIORITY
                conn.execute(
                    "UPDATE pull_requests SET local_filter_status=? WHERE repo=? AND pr_number=?",
                    (new_cohort, repo, pr_num)
                )
        conn.commit()
        logger.info("Promoted top %d to TOP cohort.", args.top)

    # ── Summary ───────────────────────────────────────────────────────────────
    for cohort_label in [C.COHORT_TOP, C.COHORT_LOW_PRIORITY, C.COHORT_DIGEST,
                         C.COHORT_ACTION, C.COHORT_RANGE, C.COHORT_UNPARSED, C.COHORT_GROUPED]:
        n = conn.execute(
            "SELECT COUNT(*) FROM pull_requests WHERE local_filter_status=?",
            (cohort_label,)
        ).fetchone()[0]
        logger.info("  %-15s : %d", cohort_label, n)

    # ── Export Parquet queue ──────────────────────────────────────────────────
    if not args.dry_run:
        _export_queue(conn, args.top)

    conn.close()
    logger.info("Stage 2B complete.")


def _flush_batch(conn: sqlite3.Connection, batch: list) -> None:
    conn.executemany(
        """UPDATE pull_requests
           SET local_priority_score=?, local_filter_status=?,
               strict_or_complex=?, classification_reason=?,
               processing_status='LOCAL_SCORED'
           WHERE repo=? AND pr_number=?""",
        batch
    )


def _export_queue(conn: sqlite3.Connection, top_n: Optional[int]) -> None:
    sql = """
        SELECT repo, pr_number, pr_url, title, ecosystem, state, merged_at,
               local_priority_score, local_filter_status, strict_or_complex,
               commits_count, changed_files, author, created_at
        FROM pull_requests
        WHERE local_filter_status IN ('TOP','LOW_PRIORITY')
        ORDER BY local_priority_score DESC
    """
    if top_n:
        sql += f" LIMIT {top_n}"
    rows = conn.execute(sql).fetchall()
    if not rows:
        logger.info("No TOP/LOW_PRIORITY rows to export.")
        return
    cols = [d[0] for d in conn.execute(sql + " LIMIT 0").description]
    data = {c: [] for c in cols}
    for row in rows:
        for c, v in zip(cols, row):
            data[c].append(v)
    table = pa.table(data)
    out = C.OUTPUT_DIR / "local_priority_queue.parquet"
    pq.write_table(table, str(out))
    logger.info("Local priority queue exported: %s (%d rows)", out, len(rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 2B: Local prefilter and priority scoring")
    parser.add_argument("--top",      type=int, default=None,
                        help="Mark this many highest-scoring PRs as TOP (rest become LOW_PRIORITY)")
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()
    main(args)
