"""
repo_lock.py — Cross-process per-repository lock.

git_fetcher caches each repository as a single shared bare repo under
REPO_CACHE_DIR. With multiple concurrent workers this is contended state:
~65% of queued candidates share a repository with another candidate, so two
workers routinely fetch/clone the same bare repo at the same moment (observed
live: djleamen/doc-reader #152 and #153 installing simultaneously). Git takes
its own index/shallow locks and will fail rather than corrupt, but those
failures surface as spurious CHECKOUT errors and get recorded as INCONCLUSIVE.

This serialises access per repository only — workers on different repos never
block each other.
"""
from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import config as C

LOCK_DIR = C.WORK_ROOT / "repo_locks"

# A lock older than this is assumed to belong to a killed process (we kill
# workers fairly often) and is broken rather than waited on forever.
STALE_SECONDS = 900


def _lock_path(repo: str) -> Path:
    return LOCK_DIR / (repo.replace("/", "__") + ".lock")


@contextmanager
def repo_lock(repo: str, timeout: int = 900, poll: float = 0.5):
    """
    Acquire an exclusive lock for `repo` for the duration of the block.
    Falls through (unlocked) rather than raising if the lock can't be
    acquired within `timeout` — a slow run is strictly better than a
    candidate lost to a lock error.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _lock_path(repo)
    fd = None
    deadline = time.time() + timeout

    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            break
        except OSError as e:
            if e.errno != errno.EEXIST:
                break   # unexpected FS problem — proceed unlocked
            try:
                if time.time() - path.stat().st_mtime > STALE_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                break   # give up waiting; proceed unlocked
            time.sleep(poll)

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
