"""
stage3_enrich_github.py
────────────────────────
For each candidate PR in the DB (processing_status = PENDING):
  1. Fetch PR details from GitHub API
  2. Fetch changed files list
  3. Fetch commit list → extract head_sha and before_sha
  4. Fetch check-run results for CI signal
  5. Update DB record; mark processing_status = ENRICHED

Supports --resume (skips ENRICHED rows), --limit, --dry-run.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import github_client as gh

LOG_DIR = C.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "stage3_enrich.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


def enrich_one_pr(conn, repo: str, pr_number: int, dry_run: bool = False) -> str:
    """
    Enrich a single PR. Returns final processing_status string.
    """
    owner, repo_name = repo.split("/", 1)

    # ── Check repo availability ───────────────────────────────────────────────
    repo_data = gh.get_repo(conn, owner, repo_name)
    if repo_data is None:
        db.log_event(conn, repo, pr_number, "enrich", "Repo not found or private (404)", "WARN")
        if not dry_run:
            conn.execute(
                "UPDATE pull_requests SET processing_status='SKIP', classification_reason='repo_unavailable' WHERE repo=? AND pr_number=?",
                (repo, pr_number)
            )
            conn.commit()
        return "SKIP"

    if repo_data.get("archived"):
        logger.info(f"  {repo}#{pr_number}: repo archived")

    # ── Fetch PR details ──────────────────────────────────────────────────────
    pr_data = gh.get_pr(conn, owner, repo_name, pr_number)
    if not pr_data:
        db.log_event(conn, repo, pr_number, "enrich", "PR not found (404)", "WARN")
        if not dry_run:
            conn.execute(
                "UPDATE pull_requests SET processing_status='SKIP', classification_reason='pr_unavailable' WHERE repo=? AND pr_number=?",
                (repo, pr_number)
            )
            conn.commit()
        return "SKIP"

    head_sha    = (pr_data.get("head") or {}).get("sha")
    merge_sha   = pr_data.get("merge_commit_sha")
    base_sha    = (pr_data.get("base") or {}).get("sha")
    pr_state    = pr_data.get("state")
    merged_at   = pr_data.get("merged_at")

    # ── Fetch commits to get before_sha ───────────────────────────────────────
    before_sha: Optional[str] = None
    commits = gh.get_pr_commits(conn, owner, repo_name, pr_number)
    if commits and len(commits) >= 1:
        # First commit in a Dependabot PR is usually the single update commit
        # before_sha = its parent
        first_commit = commits[0]
        parents = first_commit.get("parents") or []
        if parents:
            before_sha = parents[0].get("sha")

        # If only one commit (most common for dependabot), head_sha = that commit
        if len(commits) == 1 and not head_sha:
            head_sha = first_commit.get("sha")

    # ── Fetch changed files ───────────────────────────────────────────────────
    files = gh.get_pr_files(conn, owner, repo_name, pr_number)
    changed_count = len(files)

    if not dry_run and files:
        for f in files:
            fname = f.get("filename", "")
            status = f.get("status", "")
            additions = f.get("additions", 0)
            deletions = f.get("deletions", 0)
            row = {
                "repo": repo,
                "pr_number": pr_number,
                "filename": fname,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "is_manifest": _is_manifest(fname),
                "is_lockfile": _is_lockfile(fname),
                "is_source":   _is_source(fname),
                "is_test":     _is_test(fname),
                "is_ci":       _is_ci(fname),
            }
            conn.execute(
                """INSERT OR REPLACE INTO changed_files
                   (repo, pr_number, filename, status, additions, deletions,
                    is_manifest, is_lockfile, is_source, is_test, is_ci)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (row["repo"], row["pr_number"], row["filename"], row["status"],
                 row["additions"], row["deletions"], row["is_manifest"],
                 row["is_lockfile"], row["is_source"], row["is_test"], row["is_ci"])
            )

    # ── Fetch CI check runs for head_sha ─────────────────────────────────────
    ci_failed = False
    if head_sha:
        check_data = gh.get_check_runs(conn, owner, repo_name, head_sha)
        if check_data and check_data.get("check_runs"):
            for cr in check_data["check_runs"]:
                conclusion = (cr.get("conclusion") or "").lower()
                if conclusion in ("failure", "timed_out", "cancelled", "action_required"):
                    ci_failed = True
                    break

    # ── Determine ecosystem from repo language ────────────────────────────────
    lang = (repo_data.get("language") or "").lower()
    ecosystem = _lang_to_ecosystem(lang)

    # ── Update DB ─────────────────────────────────────────────────────────────
    if not dry_run:
        conn.execute(
            """UPDATE pull_requests SET
                head_sha=?, before_sha=?, merge_sha=?,
                state=?, merged_at=?, changed_files=?,
                ecosystem=?, processing_status='ENRICHED'
               WHERE repo=? AND pr_number=?""",
            (head_sha, before_sha, merge_sha,
             pr_state, merged_at, changed_count,
             ecosystem, repo, pr_number)
        )
        conn.commit()

    status_str = "ci_failed" if ci_failed else (pr_state or "unknown")
    logger.debug(f"  {repo}#{pr_number}: head={head_sha}, before={before_sha}, "
                 f"files={changed_count}, ci_failed={ci_failed}")
    return "ENRICHED"


def main(args: argparse.Namespace) -> None:
    conn = db.init_db(C.DB_PATH)

    # Check rate limit upfront
    if C.GITHUB_TOKEN:
        rl = gh.check_rate_limit(conn)
        core = (rl.get("resources") or {}).get("core") or {}
        remaining = core.get("remaining", "?")
        limit = core.get("limit", "?")
        logger.info(f"GitHub rate limit: {remaining}/{limit} remaining")
    else:
        logger.warning("No GITHUB_TOKEN – rate limit is 60/hr. Set GITHUB_TOKEN env var.")

    # Determine which status to select
    if args.resume:
        statuses = ("PENDING",)
    else:
        statuses = ("PENDING", "ENRICHED")  # re-enrich if rerun without resume

    placeholders = ",".join("?" for _ in statuses)
    sql = f"""
        SELECT repo, pr_number FROM pull_requests
        WHERE processing_status IN ({placeholders})
        ORDER BY local_priority_score DESC NULLS LAST
    """
    if args.limit:
        sql += f" LIMIT {args.limit}"

    rows = conn.execute(sql, statuses).fetchall()
    logger.info(f"PRs to enrich: {len(rows)}")

    done = 0
    skipped = 0
    errors  = 0

    for i, row in enumerate(rows):
        repo     = row["repo"]
        pr_num   = row["pr_number"]

        if getattr(args, 'only_repo', None) and not repo.startswith(args.only_repo):
            continue

        try:
            status = enrich_one_pr(conn, repo, pr_num, dry_run=args.dry_run)
            if status == "SKIP":
                skipped += 1
            else:
                done += 1
        except Exception as e:
            logger.error(f"Error enriching {repo}#{pr_num}: {e}")
            errors += 1
            db.log_event(conn, repo, pr_num, "enrich", str(e), "ERROR")

        if (i + 1) % 100 == 0:
            logger.info(f"Progress: {i+1}/{len(rows)} | done={done} skipped={skipped} errors={errors}")

        # Small delay to be a polite API client
        time.sleep(0.1)

    logger.info(f"Stage 3 complete: done={done}, skipped={skipped}, errors={errors}")
    conn.close()


# ─── File-type classification helpers ────────────────────────────────────────

import re as _re

_MANIFEST_NAMES = {
    "package.json", "requirements.txt", "requirements-dev.txt",
    "requirements-test.txt", "pyproject.toml", "pipfile",
    "setup.py", "setup.cfg", "pom.xml", "build.gradle",
    "build.gradle.kts", "go.mod", "cargo.toml", "gemfile",
    "composer.json", "directory.packages.props",
}
_LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "pipfile.lock", "cargo.lock", "gemfile.lock", "go.sum",
    "composer.lock", "packages.lock.json",
}
_CSPROJ_RE = _re.compile(r"\.csproj$", _re.I)


def _is_manifest(fname: str) -> int:
    bn = Path(fname).name.lower()
    if bn in _MANIFEST_NAMES:
        return 1
    if _CSPROJ_RE.search(fname):
        return 1
    # GitHub Actions
    if _re.match(r"\.github/workflows/.*\.(ya?ml)$", fname, _re.I):
        return 1
    return 0


def _is_lockfile(fname: str) -> int:
    bn = Path(fname).name.lower()
    return 1 if bn in _LOCKFILE_NAMES else 0


def _is_source(fname: str) -> int:
    ext = Path(fname).suffix.lower()
    return 1 if ext in C.SOURCE_CODE_EXTENSIONS else 0


def _is_test(fname: str) -> int:
    fl = fname.lower()
    for pat in C.TEST_PATH_FRAGMENTS:
        if pat in fl:
            return 1
    return 0


def _is_ci(fname: str) -> int:
    fl = fname.lower()
    for pat in C.CI_PATH_PREFIXES:
        if fl.startswith(pat) or pat in fl:
            return 1
    return 0


_LANG_ECO = {
    "javascript": "npm", "typescript": "npm", "vue": "npm", "svelte": "npm",
    "python": "pip",
    "java": "maven", "kotlin": "gradle", "groovy": "gradle",
    "ruby": "gem",
    "go": "go",
    "rust": "cargo",
    "php": "composer",
    "c#": "nuget", "f#": "nuget",
}


def _lang_to_ecosystem(lang: str) -> Optional[str]:
    return _LANG_ECO.get(lang.lower(), None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 3: Enrich candidates via GitHub API")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="Only process PENDING (skip already-ENRICHED)")
    parser.add_argument("--only-repo", type=str, default=None,
                        help="Only process PRs from this repo (owner/repo)")
    args = parser.parse_args()
    main(args)
