"""
git_fetcher.py
──────────────
Safely fetch only the required commits from a GitHub repository.
Avoids full clones; uses shallow fetches with SHA-based fallback.

Repositories are cached under REPO_CACHE_DIR/owner__repo.
"""

from __future__ import annotations
import logging
import os
import re
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
    # Windows silently strips trailing dots and spaces from directory names, so
    # a repo like "Ryanditko/E.V." is created as "Ryanditko__E.V" and every
    # later exists() check fails — reported as "repo cache missing" and the
    # candidate rejected for a fetch that actually succeeded. Only names that
    # Windows cannot represent are rewritten, so every existing cache
    # directory keeps its current path.
    if os.name == "nt":
        stripped = safe.rstrip(". ")
        if stripped != safe:
            safe = stripped + "_dot"
        safe = re.sub(r'[<>:"|?*]', "_", safe)
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
        """
        True when `sha` already exists in the local cache clone.

        Checked before every fetch so that repeated candidates from the same
        repository do not re-download objects already held locally.
        """
        rc, _, _ = _run(["git", "-C", str(cache), "cat-file", "-e", sha])
        return rc == 0

    def fetch_sha(sha: str) -> tuple[bool, str]:
        """
        Ensure `sha` is present locally, fetching it if needed.

        Returns (success, error_message). Tries progressively broader strategies:
        the pull request ref first, since GitHub always publishes it and it works
        even when the contributor's branch has since been deleted, then falling
        back to wider fetches. Shallow depths are used throughout, because the
        pipeline needs only the two commits under comparison rather than full
        history.
        """
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


_BOT_AUTHORS = ("dependabot[bot]", "dependabot-preview[bot]", "dependabot")


def _commit_author(cache: Path, sha: str) -> Optional[str]:
    """
    Author name of `sha`, or None when the commit cannot be read.
    """
    rc, out, _ = _run(["git", "-C", str(cache), "log", "-1", "--format=%an", sha])
    return out.strip() if rc == 0 and out.strip() else None


def _is_bot_commit(cache: Path, sha: str) -> bool:
    """
    True when `sha` was authored by a known bot account.

    Used to anchor the before/after pair on the Dependabot commit itself
    rather than on whatever happens to sit at the branch head, which may
    include later human commits that would contaminate the comparison.
    """
    author = _commit_author(cache, sha)
    return bool(author) and any(b in author.lower() for b in _BOT_AUTHORS)


def resolve_dependabot_pair(repo: str, head_sha: str) -> tuple[Optional[str], Optional[str], str]:
    """
    Anchor the BEFORE/AFTER pair on the Dependabot commit itself.

    Returns (after_sha, before_sha, message); (None, None, reason) when the
    pair cannot be anchored safely.

    The SHAs recorded at collection time are the PR head and the PR's *base*,
    and the base drifts: once the default branch moves on after the PR is
    opened, the recorded before_sha stops being the parent of the head and can
    even stop being an ancestor of it entirely (observed in 6 of 10 sampled
    mismatches). Rejecting those loses good candidates while the genuinely
    correct BEFORE commit — the Dependabot commit's own parent — is sitting
    one step away in the graph.

    Two shapes are handled:
      A. head_sha IS the Dependabot commit           -> anchor = head_sha
      B. head_sha is a merge of the base branch into
         the Dependabot branch, whose first parent
         is the Dependabot commit                     -> anchor = head_sha^1

    In both cases BEFORE becomes anchor^1, so the BEFORE->AFTER diff is
    exactly one Dependabot commit — a stronger causality claim than the
    recorded pair, not a weaker one.

    Deliberately gives up rather than guessing when the anchor's parent is
    itself a bot commit: that means a rebased branch carrying several bumps,
    where one commit back does not isolate a single dependency change.
    """
    cache = repo_cache_path(repo)
    if not cache.exists():
        return None, None, "repo cache missing"

    anchor = None
    if _is_bot_commit(cache, head_sha):
        anchor = head_sha
        shape = "head_is_bot_commit"
    else:
        rc, out, _ = _run(["git", "-C", str(cache), "rev-parse", f"{head_sha}^1"])
        first_parent = out.strip() if rc == 0 else ""
        if first_parent and _is_bot_commit(cache, first_parent):
            anchor = first_parent
            shape = "head_is_merge_of_bot_commit"

    if not anchor:
        return None, None, "no dependabot commit at head or head^1"

    rc, out, _ = _run(["git", "-C", str(cache), "rev-parse", f"{anchor}^1"])
    parent = out.strip() if rc == 0 else ""
    if not parent:
        return None, None, "dependabot commit has no reachable parent (shallow boundary)"

    if _is_bot_commit(cache, parent):
        # Rebased branch with several bumps stacked on it; one step back does
        # not isolate a single dependency update, so this is not usable
        # evidence and must not be guessed at.
        return None, None, "parent of the dependabot commit is also a bot commit (stacked bumps)"

    return anchor, parent, f"anchored_on_dependabot_commit ({shape})"


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
