"""
stage5_prioritize.py
─────────────────────
Compute a priority score for each VALIDATED/STRICT PR and write an
ordered execution queue.

Score is transparent and additive; all weights are in config.py.
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db

LOG_DIR = C.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "stage5_prioritize.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

_FAIL_KW_RE = re.compile(
    r"(?i)(" + "|".join(re.escape(k) for k in C.FAILURE_KEYWORDS) + r")",
)

SUPPORTED_ECOSYSTEMS = {"npm", "pip", "maven", "gradle", "go", "cargo", "gem"}


def score_pr(pr: dict) -> tuple[float, list[str]]:
    """
    Return (score, reasons) for a single PR dict.
    """
    score  = 0.0
    reasons = []
    W = C.PRIORITY_WEIGHTS

    # CI failure signal
    # Check changed_files for CI columns not easily available here;
    # use available metadata instead

    # Closed without merge (but state=closed) → possible failure
    state    = (pr.get("state") or "").lower()
    merged_at = pr.get("merged_at") or ""
    if state == "closed" and not merged_at:
        score += W["closed_without_merge"]
        reasons.append("closed_not_merged")

    # Merged (lower failure chance)
    if merged_at:
        score += W["merged_pr"]
        reasons.append("merged")

    # Body/title failure keywords
    text = " ".join([pr.get("title") or "", pr.get("body") or ""])
    if _FAIL_KW_RE.search(text):
        score += W["failure_keyword"]
        reasons.append("failure_keyword_in_text")

    # Security label
    labels_raw = pr.get("labels") or "[]"
    try:
        labels = json.loads(labels_raw)
    except Exception:
        labels = []
    if any("security" in str(l).lower() for l in labels):
        score += W["security_label"]
        reasons.append("security_label")

    # Major version bump
    dep_info = pr.get("_dep_info")  # injected below from dependency_changes join
    if dep_info:
        bump_type = dep_info.get("version_change_type", "")
        if bump_type == "major":
            score += W["major_version_bump"]
            reasons.append("major_version_bump")
        if bump_type in ("minor", "patch"):
            score += 1
            reasons.append(f"{bump_type}_bump")

    # Ecosystem supported
    eco = (pr.get("ecosystem") or "").lower()
    if eco in SUPPORTED_ECOSYSTEMS:
        score += W["ecosystem_supported"]
        reasons.append(f"supported_ecosystem:{eco}")

    # Has comments (discussion may indicate problems)
    comments = pr.get("comments_count") or 0
    if comments > 0:
        score += W["has_review_comment"]
        reasons.append(f"has_comments:{comments}")

    # Changed files count – single file is clean
    changed = pr.get("changed_files") or 0
    if 1 <= changed <= 3:
        score += 1
        reasons.append("small_diff")

    return score, reasons


def main(args: argparse.Namespace) -> None:
    conn = db.init_db(C.DB_PATH)

    # Pull VALIDATED PRs with dep info joined
    rows = conn.execute("""
        SELECT pr.*, dc.version_change_type, dc.dependency,
               dc.old_version, dc.new_version
        FROM pull_requests pr
        LEFT JOIN dependency_changes dc
            ON dc.repo = pr.repo AND dc.pr_number = pr.pr_number
        WHERE pr.strict_or_complex = 'STRICT'
           OR pr.strict_or_complex = 'UNKNOWN'
    """).fetchall()

    logger.info(f"Scoring {len(rows)} PRs ...")

    scored = []
    for row in rows:
        pr_dict = dict(row)
        pr_dict["_dep_info"] = {
            "version_change_type": row["version_change_type"],
            "dependency": row["dependency"],
        }
        score, reasons = score_pr(pr_dict)
        scored.append((score, reasons, pr_dict))

    # Sort descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # Update DB
    for score, reasons, pr_dict in scored:
        if not args.dry_run:
            conn.execute(
                "UPDATE pull_requests SET priority_score=?, processing_status='QUEUED' "
                "WHERE repo=? AND pr_number=?",
                (score, pr_dict["repo"], pr_dict["pr_number"])
            )
    if not args.dry_run:
        conn.commit()

    # Export execution queue as Parquet
    if not args.dry_run:
        _export_queue(scored)

    # Print top 20
    logger.info("=== Top 20 Priority PRs ===")
    for rank, (score, reasons, pr) in enumerate(scored[:20], 1):
        logger.info(
            f"#{rank:3d}  score={score:4.1f}  {pr['repo']}#{pr['pr_number']:5d}  "
            f"[{pr.get('ecosystem','?')}]  {pr.get('title','')[:60]}"
        )
        logger.info(f"       reasons: {', '.join(reasons)}")

    logger.info(f"Stage 5 complete. {len(scored)} PRs scored and queued.")
    conn.close()


def _export_queue(scored: list) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        queue_rows = []
        for rank, (score, reasons, pr) in enumerate(scored, 1):
            queue_rows.append({
                "rank":            rank,
                "priority_score":  score,
                "priority_reasons": ", ".join(reasons),
                "repo":            pr["repo"],
                "pr_number":       pr["pr_number"],
                "pr_url":          pr.get("pr_url"),
                "ecosystem":       pr.get("ecosystem"),
                "title":           pr.get("title"),
                "state":           pr.get("state"),
                "merged_at":       pr.get("merged_at"),
                "head_sha":        pr.get("head_sha"),
                "before_sha":      pr.get("before_sha"),
                "dependency":      pr.get("dependency"),
                "old_version":     pr.get("old_version"),
                "new_version":     pr.get("new_version"),
                "strict_or_complex": pr.get("strict_or_complex"),
            })

        if not queue_rows:
            return

        cols = list(queue_rows[0].keys())
        data = {c: [r[c] for r in queue_rows] for c in cols}
        table = pa.table(data)
        out = C.OUTPUT_DIR / "prioritized_execution_queue.parquet"
        pq.write_table(table, str(out))
        logger.info(f"Execution queue exported: {out} ({len(queue_rows)} rows)")
    except Exception as e:
        logger.warning(f"Queue export failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 5: Score and prioritize PRs for execution")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(args)
