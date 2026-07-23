"""
stage3a_enrich_minimal.py
──────────────────────────
Minimal GitHub API enrichment for TOP-cohort candidates.

Per PR fetches:
  1. GET /repos/{owner}/{repo}/pulls/{n}     -> head_sha, merge_sha, state
  2. GET /repos/{owner}/{repo}/pulls/{n}/commits  -> commit list
  3. GET /repos/{owner}/{repo}/pulls/{n}/files    -> changed files + patches

Applies the single-commit / single-parent gate immediately (Correction 6):
  - commits_count must be 1
  - head_sha must equal commits[0].sha
  - commits[0] must have exactly 1 parent
  If any fails -> strict_or_complex='COMPLEX', processing continues but 3A5/4 are skipped.

API estimate is printed and requires --allow-large-api-run above the threshold.
--limit selects the highest local_priority_score rows, not insertion order.
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import github_client as gh
import api_estimator as ae

LOG_DIR = C.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "stage3a_enrich.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

_MANIFEST_NAMES = C.MANIFEST_NAMES
_LOCKFILE_NAMES = C.LOCKFILE_NAMES


def _is_manifest(fname: str) -> int:
    bn = Path(fname).name.lower()
    if bn in _MANIFEST_NAMES: return 1
    if re.search(r'\.csproj$', fname, re.I): return 1
    if re.match(r'\.github/workflows/.*\.ya?ml$', fname, re.I): return 1
    return 0


def _is_lockfile(fname: str) -> int:
    return 1 if Path(fname).name.lower() in _LOCKFILE_NAMES else 0


def _is_source(fname: str) -> int:
    return 1 if Path(fname).suffix.lower() in C.SOURCE_CODE_EXTENSIONS else 0


def _is_test(fname: str) -> int:
    fl = fname.lower()
    return 1 if any(p in fl for p in C.TEST_PATH_FRAGMENTS) else 0


def _is_ci(fname: str) -> int:
    fl = fname.lower()
    return 1 if any(fl.startswith(p) or p in fl for p in C.CI_PATH_PREFIXES) else 0


def _lang_to_ecosystem(lang: str) -> Optional[str]:
    return {
        "javascript": "npm", "typescript": "npm", "vue": "npm", "svelte": "npm",
        "coffeescript": "npm",
        "python": "pip",
        "java": "maven", "kotlin": "gradle", "groovy": "gradle",
        "ruby": "gem",
        "go": "go",
        "rust": "cargo",
        "php": "composer",
        "c#": "nuget", "f#": "nuget",
    }.get(lang.lower())


# ─── Single-commit gate (Correction 6) ───────────────────────────────────────

def apply_single_commit_gate(conn, repo: str, pr_number: int,
                              pr_head_sha: str,
                              commits: list[dict]) -> tuple[bool, str]:
    """
    Returns (is_single_commit_ok, reason).
    If ok: before_sha is set from commits[0].parents[0].sha.
    If not ok: strict_or_complex set to COMPLEX.
    """
    if len(commits) != 1:
        reason = f"pr_has_{len(commits)}_commits"
        conn.execute(
            "UPDATE pull_requests SET strict_or_complex='COMPLEX', "
            "classification_reason=?, single_commit=0 WHERE repo=? AND pr_number=?",
            (reason, repo, pr_number)
        )
        return False, reason

    commit = commits[0]
    commit_sha = commit.get("sha", "")
    parents    = commit.get("parents") or []

    if commit_sha != pr_head_sha:
        reason = "head_sha_mismatch"
        conn.execute(
            "UPDATE pull_requests SET strict_or_complex='COMPLEX', "
            "classification_reason=?, single_commit=0 WHERE repo=? AND pr_number=?",
            (reason, repo, pr_number)
        )
        return False, reason

    if len(parents) != 1:
        reason = f"commit_has_{len(parents)}_parents"
        conn.execute(
            "UPDATE pull_requests SET strict_or_complex='COMPLEX', "
            "classification_reason=?, single_commit=0 WHERE repo=? AND pr_number=?",
            (reason, repo, pr_number)
        )
        return False, reason

    before_sha = parents[0].get("sha")
    conn.execute(
        "UPDATE pull_requests SET before_sha=?, single_commit=1 WHERE repo=? AND pr_number=?",
        (before_sha, repo, pr_number)
    )
    return True, "ok"


# ─── Per-PR enrichment ────────────────────────────────────────────────────────

def enrich_one(conn, repo: str, pr_number: int, dry_run: bool = False) -> str:
    """Returns final processing_status string."""
    owner, repo_name = repo.split("/", 1)

    # 1. PR metadata
    pr_data, code = gh.get(f"{C.GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}", conn)
    if code == 404:
        conn.execute(
            "UPDATE pull_requests SET processing_status='SKIP', "
            "classification_reason='pr_404' WHERE repo=? AND pr_number=?",
            (repo, pr_number)
        )
        conn.commit()
        return "SKIP"
    if not pr_data:
        logger.warning("  %s#%d: PR metadata fetch failed (code=%d)", repo, pr_number, code)
        return "ERROR"

    head_sha  = (pr_data.get("head") or {}).get("sha", "")
    merge_sha = pr_data.get("merge_commit_sha")
    state     = pr_data.get("state")
    merged_at = pr_data.get("merged_at")
    repo_meta = pr_data.get("base", {}).get("repo") or {}
    lang      = (repo_meta.get("language") or "").lower()
    archived  = repo_meta.get("archived", False)
    ecosystem = _lang_to_ecosystem(lang)

    if archived:
        logger.info("  %s#%d: repo archived", repo, pr_number)

    if not head_sha:
        logger.warning("  %s#%d: no head SHA", repo, pr_number)
        return "ERROR"

    # 2. Commits
    commits_data, _ = gh.get(
        f"{C.GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/commits",
        conn, params={"per_page": C.GITHUB_PER_PAGE}
    )
    commits = commits_data or []

    # Apply single-commit gate
    gate_ok, gate_reason = apply_single_commit_gate(
        conn, repo, pr_number, head_sha, commits
    )
    logger.debug("  %s#%d: single-commit gate: %s / %s", repo, pr_number, gate_ok, gate_reason)

    # 3. Changed files (all pages)
    files = gh.get_all_pages(
        f"{C.GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/files", conn
    )

    if not dry_run and files:
        for f in files:
            fname = f.get("filename", "")
            conn.execute(
                """INSERT OR REPLACE INTO changed_files
                   (repo, pr_number, filename, status, additions, deletions, patch,
                    is_manifest, is_lockfile, is_source, is_test, is_ci)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (repo, pr_number, fname,
                 f.get("status"), f.get("additions", 0), f.get("deletions", 0),
                 f.get("patch"),  # may be None/truncated — stored as evidence only
                 _is_manifest(fname), _is_lockfile(fname),
                 _is_source(fname), _is_test(fname), _is_ci(fname))
            )

    if not dry_run:
        conn.execute(
            """UPDATE pull_requests SET
                 head_sha=?, merge_sha=?, state=?, merged_at=?,
                 commits_count=?, changed_files=?, ecosystem=?,
                 api_tier='3A', processing_status='3A_DONE'
               WHERE repo=? AND pr_number=?""",
            (head_sha, merge_sha, state, merged_at,
             len(commits), len(files), ecosystem,
             repo, pr_number)
        )
        conn.commit()

    return "3A_DONE"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    conn = db.init_db(C.DB_PATH)

    if not C.GITHUB_TOKEN:
        logger.warning("No GITHUB_TOKEN set. Rate limit: 60 req/hr (unauthenticated).")

    # Select TOP cohort PRs not yet enriched, ordered by local_priority_score DESC
    sql = """
        SELECT repo, pr_number
        FROM pull_requests
        WHERE local_filter_status = 'TOP'
          AND processing_status = 'LOCAL_SCORED'
        ORDER BY local_priority_score DESC
    """
    if args.limit:
        sql += f" LIMIT {args.limit}"

    rows = conn.execute(sql).fetchall()
    pr_count = len(rows)
    logger.info("Candidates selected for Stage 3A: %d", pr_count)

    if pr_count == 0:
        logger.info("Nothing to enrich. Run stage2b_local_prefilter.py --top N first.")
        conn.close()
        return

    # API estimate + guard
    est = ae.estimate_3a(pr_count)
    ae.check_and_confirm(est, C.API_ESTIMATE_WARN_THRESHOLD, args.allow_large_api_run)

    done = skipped = errors = 0
    for i, row in enumerate(rows):
        repo, pr_number = row["repo"], row["pr_number"]
        try:
            status = enrich_one(conn, repo, pr_number, dry_run=args.dry_run)
            if status == "SKIP":
                skipped += 1
            elif status in ("3A_DONE",):
                done += 1
            else:
                errors += 1
        except Exception as e:
            logger.error("  %s#%d error: %s", repo, pr_number, e)
            db.log_event(conn, repo, pr_number, "3A", str(e), "ERROR")
            conn.commit()
            errors += 1

        if (i + 1) % 50 == 0:
            logger.info("  Progress: %d/%d | done=%d skip=%d err=%d",
                        i+1, pr_count, done, skipped, errors)
        time.sleep(0.12)   # polite 8 req/s max

    logger.info("Stage 3A complete: done=%d skipped=%d errors=%d", done, skipped, errors)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 3A: Minimal GitHub enrichment")
    parser.add_argument("--limit",               type=int,  default=None)
    parser.add_argument("--dry-run",             action="store_true")
    parser.add_argument("--allow-large-api-run", action="store_true",
                        help="Proceed even if estimated requests exceed threshold")
    args = parser.parse_args()
    main(args)
