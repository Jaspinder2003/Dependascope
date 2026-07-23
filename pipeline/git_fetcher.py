"""
git_fetcher.py
──────────────
Safely fetch only the required commits from a GitHub repository.
Avoids full clones; uses shallow fetches with SHA-based fallback.

Repositories are cached under REPO_CACHE_DIR/owner__repo.
"""

from __future__ import annotations
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import config as C

logger = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: Optional[Path] = None,
         timeout: int = C.GIT_FETCH_TIMEOUT) -> tuple[int, str, str]:
    """Run a subprocess command. Returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def repo_cache_path(repo: str) -> Path:
    """owner/repo → cache directory path."""
    safe = repo.replace("/", "__")
    return C.REPO_CACHE_DIR / safe


def clone_or_update(repo: str, head_sha: str, before_sha: Optional[str]) -> tuple[bool, str]:
    """
    Ensure both head_sha and before_sha are available in the local cache.
    Returns (success, error_message).
    """
    owner, repo_name = repo.split("/", 1)
    clone_url = f"https://github.com/{repo}.git"
    cache = repo_cache_path(repo)
    C.REPO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Init bare repo if not present ────────────────────────────────────────
    if not cache.exists():
        logger.debug(f"Initialising repo cache: {cache}")
        rc, out, err = _run(["git", "init", "--bare", str(cache)])
        if rc != 0:
            return False, f"git init failed: {err}"
        _run(["git", "-C", str(cache), "remote", "add", "origin", clone_url])

    # ── Helper: check if a SHA is already present ─────────────────────────────
    def sha_present(sha: str) -> bool:
        rc, _, _ = _run(["git", "-C", str(cache), "cat-file", "-e", sha])
        return rc == 0

    def fetch_sha(sha: str) -> tuple[bool, str]:
        # Try direct SHA fetch first
        rc, out, err = _run([
            "git", "-C", str(cache), "fetch", "--depth", str(C.GIT_SHALLOW_DEPTH),
            "origin", sha
        ])
        if rc == 0:
            return True, ""
        # Fallback: fetch PR head ref
        rc, out, err = _run([
            "git", "-C", str(cache), "fetch", "--depth", str(C.GIT_SHALLOW_DEPTH * 5),
            "origin",
        ])
        if rc == 0 and sha_present(sha):
            return True, ""
        return False, err

    # ── Fetch head_sha ────────────────────────────────────────────────────────
    if not sha_present(head_sha):
        ok, err = fetch_sha(head_sha)
        if not ok:
            return False, f"Could not fetch head_sha {head_sha}: {err}"

    # ── Fetch before_sha ──────────────────────────────────────────────────────
    if before_sha and not sha_present(before_sha):
        # before_sha is the parent; it might already be in shallow history
        rc, out, _ = _run([
            "git", "-C", str(cache), "rev-parse", "--verify", before_sha
        ])
        if rc != 0:
            # Deepen fetch
            rc, out, err = _run([
                "git", "-C", str(cache), "fetch", "--depth",
                str(C.GIT_SHALLOW_DEPTH * 10), "origin"
            ])
            if rc != 0 or not sha_present(before_sha):
                return False, f"Could not fetch before_sha {before_sha}"

    return True, ""


def checkout_snapshot(repo: str, sha: str, work_dir: Path) -> tuple[bool, str]:
    """
    Checkout a specific SHA into work_dir from the bare cache repo.
    Returns (success, error_message).
    """
    cache = repo_cache_path(repo)
    if not cache.exists():
        return False, f"Repo cache not found: {cache}"

    work_dir.mkdir(parents=True, exist_ok=True)

    # Use git archive to extract without touching working tree
    rc, out, err = _run([
        "git", "-C", str(cache),
        "archive", "--format=tar", sha
    ], timeout=120)

    if rc != 0:
        return False, f"git archive failed: {err}"

    # Extract the tar into work_dir
    import tarfile, io
    try:
        with tarfile.open(fileobj=io.BytesIO(out.encode("latin-1")), mode="r:") as tf:
            tf.extractall(path=str(work_dir))
        return True, ""
    except Exception as e:
        return False, f"tar extract failed: {e}"


def checkout_snapshot_worktree(repo: str, sha: str, work_dir: Path) -> tuple[bool, str]:
    """
    Alternative: use git worktree / clone approach for non-bare repos.
    More reliable for binary files.
    """
    cache = repo_cache_path(repo)

    # Clone from cache into work_dir
    rc, out, err = _run([
        "git", "clone",
        "--shared",          # use objects from cache
        "--no-checkout",
        str(cache),
        str(work_dir),
    ])
    if rc != 0:
        return False, f"git clone failed: {err}"

    rc, out, err = _run(["git", "checkout", sha], cwd=work_dir)
    if rc != 0:
        return False, f"git checkout {sha} failed: {err}"

    return True, ""
