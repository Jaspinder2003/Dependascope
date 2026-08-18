"""
stage2_fetch_live_github.py
───────────────────────────
Fetch fresh, live Dependabot PRs directly from GitHub API.
Query GitHub search for closed/merged Dependabot PRs from recent years (2024-2026),
extract base/head SHAs, ecosystem, changed files, and insert into research.sqlite.
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
import sqlite3

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import github_client as gh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("fetch_live_github")


def search_dependabot_prs(conn, query: str, max_pages: int = 10) -> list[dict]:
    """Search GitHub for Dependabot PRs matching a search query."""
    url = f"{C.GITHUB_API_BASE}/search/issues"
    all_items = []
    for page in range(1, max_pages + 1):
        params = {
            "q": query,
            "sort": "created",
            "order": "desc",
            "per_page": 100,
            "page": page,
        }
        data, code = gh.get(url, conn, params=params)
        if not data or "items" not in data or not data["items"]:
            break
        items = data["items"]
        all_items.extend(items)
        logger.info(f"Search page {page}: {len(items)} PRs found (total accumulated: {len(all_items)})")
        if len(items) < 100:
            break
        time.sleep(1)  # Search API rate-limit courtesy pause
    return all_items


def process_pr_item(conn, item: dict, cfg=None, stats=None) -> bool:
    """Fetch details for a single GitHub PR item and upsert into SQLite.

    Checks are ordered cheapest-and-most-discriminating first so the vast
    majority of PRs are discarded after 2-3 API calls rather than ~7.
    """
    cfg = cfg or {}
    stats = stats if stats is not None else {}

    def drop(reason: str) -> bool:
        stats[reason] = stats.get(reason, 0) + 1
        return False

    html_url = item.get("html_url", "")
    # Parse owner/repo and pr_number
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", html_url)
    if not m:
        return False
    repo_full = m.group(1)
    pr_number = int(m.group(2))

    # Fast skip: If PR already exists in SQLite, don't waste API calls on it
    existing = conn.execute("SELECT 1 FROM pull_requests WHERE repo=? AND pr_number=?", (repo_full, pr_number)).fetchone()
    if existing:
        return drop("already_in_db")

    owner, repo_name = repo_full.split("/", 1)

    # Fetch full PR object to get SHAs
    pr_data = gh.get_pr(conn, owner, repo_name, pr_number)
    if not pr_data:
        return False

    head = pr_data.get("head") or {}
    base = pr_data.get("base") or {}

    head_sha = head.get("sha")
    before_sha = base.get("sha")

    if not head_sha or not before_sha:
        return False

    # ── Green→red CI transition gate (the decisive filter) ───────────────────
    # Replicates the collection strategy of a comparable breaking-dependency
    # study on Java/JS, which required preStatus=successful AND postStatus=
    # failed and reported ~26% of collected PRs as "worked before, broke
    # after". Our earlier version only skipped *definitive* failures and let
    # "no CI signal" through, which is why already-broken projects dominated
    # the pool (BASELINE_* was ~60% of all rejections).
    #
    # In strict mode a missing/ambiguous CI signal is a REJECT, not a pass:
    # if GitHub cannot tell us the project was green before the update, we
    # cannot cheaply establish the baseline and the expensive local
    # reproduction is unlikely to pay off.
    if cfg.get("require_ci_transition", True):
        before_status = gh.check_commit_ci_status(conn, owner, repo_name, before_sha)
        if before_status != "success":
            return drop(f"before_ci_not_success({before_status})")
        after_status = gh.check_commit_ci_status(conn, owner, repo_name, head_sha)
        if after_status != "failure":
            return drop(f"after_ci_not_failure({after_status})")

    # ── Repo maturity gate ───────────────────────────────────────────────────
    repo_data = gh.get_repo(conn, owner, repo_name)
    if not repo_data:
        return drop("no_repo_data")
    if repo_data.get("archived") or repo_data.get("disabled"):
        return drop("archived_or_disabled")
    if (repo_data.get("stargazers_count") or 0) < cfg.get("min_stars", 5):
        return drop("below_min_stars")

    commits_url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo_name}/commits"
    commit_count = gh.get_paginated_count(commits_url)
    if commit_count is not None and commit_count < cfg.get("min_commits", 100):
        return drop("below_min_commits")

    contributors_url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo_name}/contributors"
    contributor_count = gh.get_paginated_count(contributors_url, params={"anon": "false"})
    if contributor_count is not None and contributor_count < cfg.get("min_contributors", 11):
        return drop("below_min_contributors")

    # Fetch files to determine strict_or_complex & ecosystem
    files = gh.get_pr_files(conn, owner, repo_name, pr_number)
    file_paths = [f.get("filename", "") for f in (files or [])]

    # Detect ecosystem
    ecosystem = None
    for fp in file_paths:
        if fp.endswith("package.json") or fp.endswith("package-lock.json") or fp.endswith("yarn.lock"):
            ecosystem = "npm"
            break
        elif fp.endswith("requirements.txt") or fp.endswith("pyproject.toml") or fp.endswith("Pipfile"):
            ecosystem = "pip"
            break
        elif fp.endswith("pom.xml"):
            ecosystem = "maven"
            break
        elif fp.endswith("build.gradle") or fp.endswith("build.gradle.kts"):
            ecosystem = "gradle"
            break
        elif fp.endswith("Gemfile") or fp.endswith("Gemfile.lock"):
            ecosystem = "gem"
            break
        elif fp.endswith("go.mod"):
            ecosystem = "go"
            break
        elif fp.endswith("Cargo.toml"):
            ecosystem = "cargo"
            break

    # Determine if strict (manifest/lockfile change only)
    manifest_files = [
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
        "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "Gemfile.lock",
        "go.mod", "go.sum", "Cargo.toml", "Cargo.lock"
    ]
    non_manifest = [fp for fp in file_paths if not any(fp.endswith(m) for m in manifest_files)]

    strict_or_complex = "STRICT" if len(non_manifest) == 0 else "COMPLEX"
    reason = "manifest_only" if strict_or_complex == "STRICT" else f"non_manifest_files:{len(non_manifest)}"

    pr_row = {
        "repo": repo_full,
        "pr_number": pr_number,
        "pr_url": html_url,
        "author": (item.get("user") or {}).get("login", "dependabot[bot]"),
        "title": item.get("title") or "",
        "body": (item.get("body") or "")[:4000],
        "state": item.get("state") or "closed",
        "created_at": item.get("created_at") or "",
        "merged_at": (item.get("pull_request") or {}).get("merged_at") or "",
        "closed_at": item.get("closed_at") or "",
        "head_sha": head_sha,
        "before_sha": before_sha,
        "merge_sha": pr_data.get("merge_commit_sha"),
        "ecosystem": ecosystem,
        "labels": json.dumps([l.get("name", "") for l in (item.get("labels") or [])]),
        "comments_count": item.get("comments", 0),
        "commits_count": pr_data.get("commits", 1),
        "additions": pr_data.get("additions", 0),
        "deletions": pr_data.get("deletions", 0),
        "changed_files": pr_data.get("changed_files", len(file_paths)),
        "strict_or_complex": strict_or_complex,
        "classification_reason": reason,
        "processing_status": "VALIDATED",
        "source_dataset": "live_github_api",
        # Tagged distinctly so this cohort's yield can be measured against the
        # older, unfiltered pool rather than being blended into it.
        "source_file": "ci_verified_green_to_red" if cfg.get("require_ci_transition", True) else "search_issues",
    }

    db.upsert_pr(conn, pr_row)
    return True


def main():
    parser = argparse.ArgumentParser(description="Fetch fresh Dependabot PRs from GitHub API")
    # NOTE: previously defaulted to "... is:unmerged status:failure", which
    # biases the search toward PRs GitHub already flagged as failing —  in
    # practice this over-selected already-broken/abandoned repos (~67% of a
    # 96-candidate sample had a BROKEN BEFORE state, unrelated to the
    # Dependabot update). Our own local BEFORE/AFTER reproduction is the
    # authoritative signal anyway, so the search query no longer needs to
    # pre-guess "failure" — it just needs a broad, unbiased pool of closed
    # Dependabot PRs (this also now includes merged PRs, which are more
    # likely to have started from a healthy BEFORE state).
    # `status:failure is:unmerged` is cheap SERVER-side narrowing toward the
    # "postStatus failed" half of the transition, so we don't burn API calls
    # examining PRs that were green after the update. The other half
    # (preStatus successful) is verified per-commit below — that is the part
    # that actually removes already-broken projects, and it is what the
    # earlier version of this query was missing.
    parser.add_argument("--query", type=str,
                        default="is:pr author:app/dependabot is:closed is:unmerged status:failure",
                        help="GitHub search query")
    parser.add_argument("--min-stars", type=int, default=5)
    parser.add_argument("--min-commits", type=int, default=100)
    parser.add_argument("--min-contributors", type=int, default=11)
    parser.add_argument("--no-ci-transition", action="store_true",
                        help="Disable the strict green->red CI requirement (not recommended)")
    parser.add_argument("--limit", type=int, default=100, help="Max PRs to fetch & process")
    parser.add_argument("--ecosystem", type=str, choices=["npm", "pip", "maven", "gradle", "gem", "all"], default="all")
    parser.add_argument("--since", type=str, default="2025-01-01",
                        help="Partition the search into monthly windows starting here (default matches the "
                             "comparable Java/JS study's 2025-01-01 cutoff)")
    args = parser.parse_args()

    conn = db.init_db(C.DB_PATH)

    # Check rate limit
    rl = gh.check_rate_limit(conn)
    if rl:
        core = rl.get("resources", {}).get("core", {})
        search = rl.get("resources", {}).get("search", {})
        logger.info(f"API Rate limit — Core: {core.get('remaining')}/{core.get('limit')}, Search: {search.get('remaining')}/{search.get('limit')}")

    # Build search queries across ecosystems or search term variations
    base_queries = []
    if args.ecosystem in ("pip", "python"):
        base_queries.append(f"{args.query} label:pip")
        base_queries.append(f"{args.query} label:python")
        base_queries.append(f"{args.query} requirements.txt")
        base_queries.append(f"{args.query} pyproject.toml")
        base_queries.append(f"{args.query} poetry")
        base_queries.append(f"{args.query} pytest")
        base_queries.append(f"{args.query} Pipfile")
        base_queries.append(f"{args.query} setup.py")
    elif args.ecosystem == "all":
        base_queries.append(args.query)
        for eco in ["npm", "pip", "maven", "gradle", "cargo", "go"]:
            base_queries.append(f"{args.query} label:{eco}")
    else:
        base_queries.append(f"{args.query} label:{args.ecosystem}")

    # GitHub's search API returns at most 1000 results per query, no matter how
    # many pages you request. Re-running the same query therefore returns the
    # same already-ingested PRs forever (a fetch attempt examined 6109 items
    # and every single one was already in the database). Partitioning by
    # created-date gives each month its own independent 1000-result budget,
    # which is what actually unlocks a large fresh pool.
    def _month_windows(since: str) -> list[str]:
        from datetime import date
        y, m, _ = (int(x) for x in since.split("-"))
        today = date.today()
        out = []
        while (y, m) <= (today.year, today.month):
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            out.append(f"created:{y:04d}-{m:02d}-01..{ny:04d}-{nm:02d}-01")
            y, m = ny, nm
        return out

    windows = _month_windows(args.since)
    queries = [f"{bq} {w}" for w in windows for bq in base_queries]
    logger.info(f"Built {len(queries)} queries: {len(base_queries)} base x {len(windows)} monthly windows "
                f"since {args.since} (each window has its own 1000-result cap)")

    cfg = {
        "require_ci_transition": not args.no_ci_transition,
        "min_stars": args.min_stars,
        "min_commits": args.min_commits,
        "min_contributors": args.min_contributors,
    }
    stats: dict = {}
    logger.info(f"Filter config: {cfg}")

    success = 0
    total_scanned = 0

    for search_q in queries:
        if success >= args.limit:
            break

        logger.info(f"Searching GitHub PRs: '{search_q}' (target remaining={args.limit - success})...")
        items = search_dependabot_prs(conn, search_q, max_pages=10)
        total_scanned += len(items)

        for i, item in enumerate(items, 1):
            if success >= args.limit:
                break
            if process_pr_item(conn, item, cfg=cfg, stats=stats):
                success += 1
                logger.info(f"  KEPT #{success}/{args.limit}: green->red CI verified")
            if i % 50 == 0:
                logger.info(f"  ...examined {i}/{len(items)} of this query | kept {success} | "
                            f"top drops: {sorted(stats.items(), key=lambda kv: -kv[1])[:3]}")
            # Commit on a fixed cadence of *attempts*, not just successes —
            # the quality/CI filters now reject most candidates, and cache
            # writes from those rejected lookups still need to be committed
            # periodically or this connection holds an open write transaction
            # for a long stretch, blocking any other process's writes to the
            # same research.sqlite (this bit us once already).
            if i % 10 == 0:
                conn.commit()

    conn.commit()
    conn.close()

    logger.info("=" * 70)
    logger.info(f"Fetch complete: kept {success} of {total_scanned} examined")
    logger.info("Drop reasons (why candidates were rejected at collection time):")
    for reason, n in sorted(stats.items(), key=lambda kv: -kv[1]):
        logger.info(f"  {reason:38} {n}")
    logger.info("=" * 70)

    print(f"\nLive GitHub fetch complete! {success} NEW fresh Dependabot PRs saved in {C.DB_PATH} (scanned {total_scanned} search items)")


if __name__ == "__main__":
    main()
