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


def process_pr_item(conn, item: dict) -> bool:
    """Fetch details for a single GitHub PR item and upsert into SQLite."""
    html_url = item.get("html_url", "")
    # Parse owner/repo and pr_number
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", html_url)
    if not m:
        return False
    repo_full = m.group(1)
    pr_number = int(m.group(2))

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
        "source_file": "search_issues",
    }

    db.upsert_pr(conn, pr_row)
    return True


def main():
    parser = argparse.ArgumentParser(description="Fetch fresh Dependabot PRs from GitHub API")
    parser.add_argument("--query", type=str, default="is:pr author:app/dependabot is:closed is:unmerged",
                        help="GitHub search query")
    parser.add_argument("--limit", type=int, default=100, help="Max PRs to fetch & process")
    parser.add_argument("--ecosystem", type=str, choices=["npm", "pip", "maven", "gradle", "gem", "all"], default="all")
    args = parser.parse_args()

    conn = db.init_db(C.DB_PATH)

    # Check rate limit
    rl = gh.check_rate_limit(conn)
    if rl:
        core = rl.get("resources", {}).get("core", {})
        search = rl.get("resources", {}).get("search", {})
        logger.info(f"API Rate limit — Core: {core.get('remaining')}/{core.get('limit')}, Search: {search.get('remaining')}/{search.get('limit')}")

    # Refine search query with ecosystem label if specified
    search_q = args.query
    if args.ecosystem != "all":
        search_q += f" label:{args.ecosystem}"

    logger.info(f"Searching GitHub PRs: '{search_q}' (limit={args.limit})...")
    items = search_dependabot_prs(conn, search_q, max_pages=max(1, args.limit // 100))

    logger.info(f"Processing {len(items[:args.limit])} search results...")
    success = 0
    for i, item in enumerate(items[:args.limit]):
        if process_pr_item(conn, item):
            success += 1
        if (i + 1) % 10 == 0:
            conn.commit()
            logger.info(f"  Processed {i+1}/{min(len(items), args.limit)} | Inserted: {success}")

    conn.commit()
    conn.close()

    print(f"\nLive GitHub fetch complete! {success} fresh Dependabot PRs saved in {C.DB_PATH}")


if __name__ == "__main__":
    main()
