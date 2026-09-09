"""
github_client.py
────────────────
Authenticated GitHub REST API client with:
  - local SQLite response caching
  - retry / exponential backoff
  - rate-limit detection & pause
  - token read from GITHUB_TOKEN env var only
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
import sys

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db

logger = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """
    Return the process-wide requests session, creating it on first use.

    The session carries the API version headers and, when GITHUB_TOKEN is set,
    the bearer token. Without a token the API allows only 60 requests an hour,
    so the absence of one is logged as a warning rather than passing silently.
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        token = C.GITHUB_TOKEN
        if token:
            _SESSION.headers["Authorization"] = f"Bearer {token}"
            logger.info("GitHub token configured.")
        else:
            logger.warning("No GITHUB_TOKEN found. Unauthenticated requests (60/hr limit).")
        _SESSION.headers["Accept"] = "application/vnd.github+json"
        _SESSION.headers["X-GitHub-Api-Version"] = "2022-11-28"
    return _SESSION


def _cache_key(url: str, params: Optional[dict] = None) -> str:
    """
    Stable cache key for a URL and its query parameters.

    Parameters are sorted so that the same request always produces the same
    key regardless of dictionary ordering.
    """
    s = url
    if params:
        s += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return s


def get(
    url: str,
    conn: Any,
    params: Optional[dict] = None,
    force_refresh: bool = False,
    accept_404: bool = True,
) -> tuple[Optional[Any], int]:
    """
    Fetch URL with caching, retry, backoff.
    Returns (data_or_None, status_code).
    """
    key = _cache_key(url, params)

    # Check cache
    if not force_refresh:
        cached = db.cache_get(conn, key)
        if cached is not None:
            return cached, 200

    session = _get_session()

    for attempt in range(C.GITHUB_RETRY_MAX):
        try:
            resp = session.get(
                url,
                params=params,
                timeout=C.GITHUB_REQUEST_TIMEOUT
            )

            if resp.status_code == 200:
                data = resp.json()
                db.cache_set(conn, key, data, 200)
                return data, 200

            elif resp.status_code == 404:
                logger.debug(f"404: {url}")
                if accept_404:
                    db.cache_set(conn, key, None, 404)
                return None, 404

            elif resp.status_code in (403, 429):
                # Rate limited
                reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + C.GITHUB_RATE_LIMIT_PAUSE))
                wait = max(1, reset_ts - int(time.time())) + 5
                logger.warning(f"Rate limited (HTTP {resp.status_code}). Sleeping {wait}s ...")
                time.sleep(min(wait, 300))

            elif resp.status_code >= 500:
                wait = (C.GITHUB_RETRY_BACKOFF ** attempt) * 5
                logger.warning(f"Server error {resp.status_code} for {url}. Retry in {wait:.0f}s ...")
                time.sleep(wait)

            else:
                logger.warning(f"Unexpected status {resp.status_code} for {url}")
                return None, resp.status_code

        except requests.RequestException as e:
            wait = C.GITHUB_RETRY_BACKOFF ** attempt
            logger.warning(f"Request error attempt {attempt+1}: {e}. Retry in {wait:.1f}s ...")
            time.sleep(wait)

    logger.error(f"All {C.GITHUB_RETRY_MAX} attempts failed for {url}")
    return None, -1


def get_pr(conn: Any, owner: str, repo: str, pr_number: int) -> Optional[dict]:
    """
    Fetch a single pull request. Returns None when unavailable.
    """
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    data, code = get(url, conn)
    return data


def get_pr_files(conn: Any, owner: str, repo: str, pr_number: int,
                 per_page: int = 100) -> list[dict]:
    """
    Fetch every file changed by a pull request, following pagination.

    The file list determines both the ecosystem (from the manifests touched)
    and whether the change is manifest-only, so it must be complete rather
    than just the first page.
    """
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    all_files = []
    page = 1
    while True:
        data, code = get(url, conn, params={"per_page": per_page, "page": page})
        if not data:
            break
        all_files.extend(data)
        if len(data) < per_page:
            break
        page += 1
    return all_files


def get_pr_commits(conn: Any, owner: str, repo: str, pr_number: int) -> list[dict]:
    """
    Fetch the commits belonging to a pull request. Empty list when unavailable.
    """
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/commits"
    data, _ = get(url, conn, params={"per_page": 100})
    return data or []


def get_repo(conn: Any, owner: str, repo: str) -> Optional[dict]:
    """
    Fetch repository metadata, used for the stars, commits and contributors gates.
    """
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}"
    data, _ = get(url, conn)
    return data


def get_check_runs(conn: Any, owner: str, repo: str, ref: str) -> Optional[dict]:
    """
    Fetch the check runs recorded for a commit.

    One of the two sources of CI state; modern GitHub Actions results appear
    here rather than in the legacy combined status.
    """
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{ref}/check-runs"
    data, _ = get(url, conn, params={"per_page": 100})
    return data


def get_commit_statuses(conn: Any, owner: str, repo: str, ref: str) -> Optional[dict]:
    """
    Fetch the legacy combined commit status.

    The second source of CI state, used alongside check runs because older
    projects and third-party CI services still report through this API.
    """
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{ref}/status"
    data, _ = get(url, conn)
    return data


def check_rate_limit(conn: Any) -> dict:
    """
    Fetch the current API rate-limit state, bypassing the cache.

    Always requested fresh, since a cached rate limit would defeat the purpose
    of checking it.
    """
    url = f"{C.GITHUB_API_BASE}/rate_limit"
    data, _ = get(url, conn, force_refresh=True)
    return data or {}


def check_commit_ci_status(conn: Any, owner: str, repo: str, sha: str) -> Optional[str]:
    """
    Shared CI-status check used both at candidate-discovery time (skip a PR
    before it's even queued) and as a run-time pre-screen (skip local
    reproduction). Returns:
      "failure" — GitHub's combined status or check-runs show a definitive
                  failure for this commit
      "success" — combined status is definitively green
      None      — no CI signal at all (very common for small repos), or the
                  signal is inconclusive (pending/mixed) — callers must treat
                  this as "unknown", never as evidence either way.
    """
    statuses = get_commit_statuses(conn, owner, repo, sha)
    if statuses and statuses.get("total_count", 0) > 0:
        state = statuses.get("state")
        if state == "failure":
            return "failure"
        if state == "success":
            return "success"

    check_runs = get_check_runs(conn, owner, repo, sha)
    if check_runs and check_runs.get("total_count", 0) > 0:
        runs = check_runs.get("check_runs", [])
        completed = [r for r in runs if r.get("status") == "completed"]

        # Only "decisive" conclusions say anything about the code. Conditional
        # jobs routinely report skipped/neutral/stale, and cancelled means a
        # human or a newer run interrupted it — none of those are evidence
        # either way. Requiring *every* run to be "success" (the previous
        # behaviour) meant a single skipped job made an otherwise-green commit
        # unreadable, which is why ~80% of candidates looked like "no CI data"
        # when repos in fact had 20-228 check runs.
        conclusions = [
            r.get("conclusion") for r in completed
            if r.get("conclusion") not in (None, "skipped", "neutral", "stale", "cancelled", "action_required")
        ]
        if conclusions:
            if any(c in ("failure", "timed_out") for c in conclusions):
                return "failure"
            if all(c == "success" for c in conclusions):
                return "success"

    return None


def commit_ci_breakdown(conn: Any, owner: str, repo: str, sha: str) -> tuple[int, int]:
    """
    (successful_checks, failed_checks) for a commit, counting only decisive
    conclusions.

    check_commit_ci_status() collapses this to a single verdict and calls the
    commit "failure" if *any* job failed — right for picking green->red
    candidates, far too blunt for deciding whether to bother reproducing one.
    A repo whose lint or CodeQL job is red while its test suite is entirely
    green is not a broken project, and discarding it costs us a candidate for
    no reason.
    """
    n_success = n_failure = 0

    statuses = get_commit_statuses(conn, owner, repo, sha)
    if statuses and statuses.get("total_count", 0) > 0:
        for s in statuses.get("statuses", []) or []:
            if s.get("state") == "success":
                n_success += 1
            elif s.get("state") in ("failure", "error"):
                n_failure += 1

    check_runs = get_check_runs(conn, owner, repo, sha)
    if check_runs and check_runs.get("total_count", 0) > 0:
        for r in check_runs.get("check_runs", []):
            if r.get("status") != "completed":
                continue
            c = r.get("conclusion")
            if c == "success":
                n_success += 1
            elif c in ("failure", "timed_out"):
                n_failure += 1

    return n_success, n_failure


def get_paginated_count(url: str, params: Optional[dict] = None) -> Optional[int]:
    """
    Determine total item count for a paginated list endpoint (e.g. commits,
    contributors) without fetching every page — uses GitHub's standard
    pagination trick: request per_page=1 and read the last page number from
    the Link response header. Not run through the JSON response cache
    (headers aren't stored in github_cache); cheap enough to call fresh.
    Returns None if the request fails.
    """
    session = _get_session()
    p = dict(params or {})
    p["per_page"] = 1
    try:
        resp = session.get(url, params=p, timeout=C.GITHUB_REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    link = resp.headers.get("Link", "")
    m = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
    if m:
        return int(m.group(1))
    try:
        data = resp.json()
        return len(data) if isinstance(data, list) else None
    except Exception:
        return None
