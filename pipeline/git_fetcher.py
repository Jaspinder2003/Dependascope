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
    import os
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
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


def clone_or_update(repo: str, head_sha: str, before_sha: Optional[str],
                    pr_number: Optional[int] = None) -> tuple[bool, str]:
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

    # Clean up any stale shallow.lock left over from interrupted fetches
    shallow_lock = cache / "shallow.lock"
    if shallow_lock.exists():
        try:
            shallow_lock.unlink()
        except Exception:
            pass

    # ── Helper: check if a SHA is already present ─────────────────────────────
    def sha_present(sha: str) -> bool:
        rc, _, _ = _run(["git", "-C", str(cache), "cat-file", "-e", sha])
        return rc == 0

    def fetch_sha(sha: str) -> tuple[bool, str]:
        if sha_present(sha):
            return True, ""

        # Strategy 1: fetch the PR ref (always available on GitHub)
        if pr_number:
            rc, out, err = _run([
                "git", "-C", str(cache), "fetch",
                "--depth", str(C.GIT_SHALLOW_DEPTH * 2),
                "origin",
                f"refs/pull/{pr_number}/head",
            ])
            if rc == 0 and sha_present(sha):
                return True, ""

        # Strategy 2: direct SHA fetch
        rc, out, err = _run([
            "git", "-C", str(cache), "fetch",
            "--depth", str(C.GIT_SHALLOW_DEPTH),
            "origin", sha,
        ])
        if rc == 0 and sha_present(sha):
            return True, ""

        # Strategy 3: unshallow the whole repo (slow but reliable)
        rc, out, err = _run([
            "git", "-C", str(cache), "fetch",
            "--unshallow", "origin",
        ])
        if rc == 0 and sha_present(sha):
            return True, ""

        # Strategy 4: plain fetch with more depth
        rc, out, err = _run([
            "git", "-C", str(cache), "fetch",
            "--depth", str(C.GIT_SHALLOW_DEPTH * 20),
            "origin",
        ])
        if rc == 0 and sha_present(sha):
            return True, ""

        return False, f"Could not fetch {sha}: {err}"

    # ── Fetch head_sha ────────────────────────────────────────────────────────
    ok, err = fetch_sha(head_sha)
    if not ok:
        return False, f"Could not fetch head_sha {head_sha}: {err}"

    # ── Fetch before_sha ─────────────────────────────────────────────────────
    if before_sha:
        ok, err = fetch_sha(before_sha)
        if not ok:
            return False, f"Could not fetch before_sha {before_sha}: {err}"

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
    clone_url = f"https://github.com/{repo}.git"

    # Clone from cache using --local (copies objects via hardlinks AND copies
    # the shallow file - unlike --shared which skips the shallow file and causes
    # "reference is not a tree" errors)
    rc, out, err = _run([
        "git", "clone",
        "--local",        # hardlinks + copies shallow boundary file
        "--no-checkout",
        str(cache),
        str(work_dir),
    ])
    if rc != 0:
        return False, f"git clone failed: {err}"

    rc, out, err = _run(["git", "checkout", sha], cwd=work_dir)
    if rc == 0:
        return True, ""

    # Checkout failed - SHA might be missing from shallow clone.
    # Fetch it directly from GitHub and retry.
    logger.debug(f"  Checkout {sha[:8]} failed, fetching directly from GitHub …")
    _run([
        "git", "-C", str(work_dir), "fetch",
        "--depth", str(C.GIT_SHALLOW_DEPTH * 2),
        "origin", sha,
    ])
    rc, out, err = _run(["git", "checkout", sha], cwd=work_dir)
    if rc != 0:
        return False, f"git checkout {sha} failed: {err}"
    return True, ""


def verify_sha_pairing(repo: str, head_sha: str, before_sha: str) -> tuple[bool, str, str]:
    """
    Verify causal pairing: check if before_sha is the first parent of head_sha (or in its direct parent chain).
    Returns (verified_bool, parent_sha, message).
    """
    cache = repo_cache_path(repo)
    if not cache.exists():
        return False, "", "repo cache missing"

    rc, out, err = _run(["git", "-C", str(cache), "rev-parse", f"{head_sha}^1"])
    if rc == 0 and out.strip():
        parent_sha = out.strip()
        if parent_sha.lower() == before_sha.lower():
            return True, parent_sha, "exact_parent_match"
        else:
            # Check if before_sha is in the first parent history chain
            rc2, out2, _ = _run(["git", "-C", str(cache), "rev-list", "--first-parent", "-n", "10", head_sha])
            if rc2 == 0 and before_sha.lower() in [s.strip().lower() for s in out2.splitlines()]:
                return True, parent_sha, "parent_chain_match"
            return False, parent_sha, f"parent_mismatch (head parent: {parent_sha[:8]}, expected: {before_sha[:8]})"

    return False, "", "could_not_parse_parent"
