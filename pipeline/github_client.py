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
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    data, code = get(url, conn)
    return data


def get_pr_files(conn: Any, owner: str, repo: str, pr_number: int,
                 per_page: int = 100) -> list[dict]:
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
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/commits"
    data, _ = get(url, conn, params={"per_page": 100})
    return data or []


def get_repo(conn: Any, owner: str, repo: str) -> Optional[dict]:
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}"
    data, _ = get(url, conn)
    return data


def get_check_runs(conn: Any, owner: str, repo: str, ref: str) -> Optional[dict]:
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{ref}/check-runs"
    data, _ = get(url, conn, params={"per_page": 100})
    return data


def get_commit_statuses(conn: Any, owner: str, repo: str, ref: str) -> Optional[dict]:
    url = f"{C.GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{ref}/status"
    data, _ = get(url, conn)
    return data


def check_rate_limit(conn: Any) -> dict:
    url = f"{C.GITHUB_API_BASE}/rate_limit"
    data, _ = get(url, conn, force_refresh=True)
    return data or {}
