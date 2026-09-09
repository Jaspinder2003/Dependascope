"""
run_regression_pipeline.py — Orchestrator for the corrected Python regression
methodology.

Definition of a genuine case (per research requirement):
  BEFORE: dependency install succeeds AND the project executes meaningfully
          (real test suite, or import-smoke as fallback) and that execution
          PASSES.
  AFTER:  the identical dependency install succeeds AND the *same* execution
          now FAILS.

Everything that does not meet that bar is preserved, never silently dropped:
  - TEST_LEVEL_REGRESSION -> TIER_3_TEST_REGRESSION. The suite was not fully
    green before the update, but specific tests that DID pass at the parent
    commit fail at the Dependabot commit while the install still succeeds.
    Same causal claim, made per-test instead of per-suite, so it is tiered
    separately from the clean whole-suite cases.
  - INSTALL_REGRESSION (AFTER install itself fails) -> TIER_2, kept separate
    from the primary TIER_3_CONFIRMED dataset.
  - BASELINE_*, UNVERIFIED_SHA_PAIR, NO_MEANINGFUL_EXECUTION, etc. -> REJECTED
    with a machine-readable reason, so the candidate -> reproduced -> confirmed
    funnel is fully auditable.

Usage:
  python run_regression_pipeline.py discover --limit 500
  python run_regression_pipeline.py run --batch 20      (0 = drain all PENDING)
  python run_regression_pipeline.py validate            (small known-case smoke test)
  python run_regression_pipeline.py status
  python run_regression_pipeline.py export
"""
from __future__ import annotations
import argparse
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import config as C
import db as main_db
import git_fetcher as gf
import ecosystem_adapters as ea
import github_client as gh
from purified_reproduce import resolve_python_runtime

from regression_pipeline import db as rdb
from regression_pipeline.candidates import discover as discover_candidates, prune_over_cap
from regression_pipeline.snapshot_runner import run_snapshot
from regression_pipeline.classifier import classify, Verdict, test_regressions
from regression_pipeline.execution_detector import ExecutionPlan
from regression_pipeline.repo_lock import repo_lock
from regression_pipeline.install_planner import plan_install, python_version_ok

# Interpreters available on this host, newest-first; used to satisfy a
# project's requires-python when the default choice violates it.
# Locations the official Windows installers use. Globbed at runtime rather than
# listed as absolute paths, so the pipeline finds whatever is installed on the
# machine it is actually running on.
_WINDOWS_PYTHON_GLOBS = [
    r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe",
    r"%PROGRAMFILES%\Python3*\python.exe",
    r"%PROGRAMFILES(X86)%\Python3*\python.exe",
    r"C:\Python3*\python.exe",
]

def _installed_interpreters() -> list[tuple[tuple[int, int], str]]:
    """(version_tuple, path) for every interpreter on this host, newest last."""
    found = []
    if os.name == "nt":
        import glob as _glob
        seen = set()
        for pattern in _WINDOWS_PYTHON_GLOBS:
            for p in _glob.glob(os.path.expandvars(pattern)):
                m = re.search(r"Python(\d)(\d+)", p, re.I)
                if not (m and Path(p).exists()):
                    continue
                key = (int(m.group(1)), int(m.group(2)))
                if key not in seen:
                    seen.add(key)
                    found.append((key, p))
    else:
        import glob as _glob
        seen = set()
        for base in ("/usr/bin", "/usr/local/bin"):
            for p in _glob.glob(base + "/python3.*"):
                m = re.search(r"python(\d)\.(\d+)$", p)
                if m and Path(p).exists():
                    key = (int(m.group(1)), int(m.group(2)))
                    if key not in seen:
                        seen.add(key)
                        found.append((key, p))
    return sorted(found)


_VERSION_SENSITIVE_INSTALL = re.compile(
    r"no matching distribution|could not find a version|failed building wheel|"
    r"microsoft visual c\+\+|metadata-generation-failed|requires-python|"
    r"requires a different python|subprocess-exited-with-error",
    re.I,
)


def _alternate_interpreters(current: str, requires_python: Optional[str]):
    """
    Other interpreters worth retrying on, older ones first.

    Older first is deliberate: the failure being retried is usually an old
    pinned dependency with no wheel for a recent interpreter, and dropping back
    a version or two is what finds one.
    """
    out = []
    for ver, path in _installed_interpreters():
        if path == current:
            continue
        if not python_version_ok(requires_python, f"{ver[0]}.{ver[1]}"):
            continue
        out.append((ver, path))
    return out


def _pick_interpreter(req_py: Optional[str], requires_python: Optional[str]) -> tuple[Optional[str], str]:
    """
    Choose the interpreter to reproduce on, treating the CI hint as a
    preference rather than a hard requirement.

    41 of 50 RUNTIME_UNAVAILABLE rejections were "Python 3.10 is not installed
    on this host" — and that 3.10 came from `_extract_python_version`, which
    takes the *first* `python-version:` string it finds anywhere in the repo's
    workflows. In a build matrix, or a lint job pinned to an old version, that
    is an arbitrary pick among several supported versions. Discarding a
    candidate because an arbitrarily-chosen version is missing locally tells us
    nothing about the project or the dependency.

    Substituting a nearby interpreter cannot manufacture a false positive:
    BEFORE and AFTER run on the *same* interpreter, so the comparison stays
    apples-to-apples, and a project that genuinely needs the exact version will
    simply fail its baseline and be rejected honestly.
    """
    installed = _installed_interpreters()
    if not installed:
        return None, "no interpreter found on host"

    def satisfies(ver: tuple[int, int]) -> bool:
        """
        True when interpreter version `ver` satisfies the project's requires-python.
        """
        return python_version_ok(requires_python, f"{ver[0]}.{ver[1]}")

    want = None
    if req_py and req_py != "3.x":
        m = re.match(r"v?(\d+)\.(\d+)", req_py.strip())
        if m:
            want = (int(m.group(1)), int(m.group(2)))

    if want:
        for ver, path in installed:
            if ver == want and satisfies(ver):
                return path, f"Python {ver[0]}.{ver[1]} (exact match)"
        # Nearest by absolute distance, preferring a newer version on a tie.
        eligible = [(v, p) for v, p in installed if satisfies(v)]
        if eligible:
            ver, path = min(eligible, key=lambda vp: (abs(vp[0][1] - want[1]), -vp[0][1]))
            return path, f"Python {ver[0]}.{ver[1]} (substituted for requested {want[0]}.{want[1]})"

    eligible = [(v, p) for v, p in installed if satisfies(v)]
    if eligible:
        ver, path = eligible[-1]
        return path, f"Python {ver[0]}.{ver[1]} (host default for requires-python '{requires_python or 'unspecified'}')"
    return None, f"no installed interpreter satisfies requires-python '{requires_python}'"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] regression_pipeline - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("regression_pipeline")

WORK_ROOT = rdb.REGRESSION_DIR / "worktrees"
LOG_DIR = rdb.REGRESSION_DIR / "logs"

# Small set of already-known PRs (from the earlier install-regression run) used
# to sanity-check the corrected methodology before pointing it at the full pool.
VALIDATION_SET = [
    ("EvezArt/autoenveloper", 20,
     "81742ff3d14307dcc2e16d9e457a3c88b5d555ab", "f3582625f99be30f561322be7ee9b72dd8cd8ee5"),
    ("EvezArt/autonomous-research-orchestrator", 12,
     "e1786c75a8ecda32cbedaa813abda519f470fec8", "b2556ca2d2082c64744c49c540f1ecf531100129"),
    ("Justin300507/atlas", 10,
     "5fa7afa3682694907cf1f9e3745f7f002241c844", "51c551ae5d62d21adc39b0344be12fa2d4486845"),
]


def _prescreen_baseline_ci(repo: str, before_sha: str, main_conn) -> Optional[str]:
    """
    Cheap pre-screen using GitHub's own CI status for the BEFORE commit —
    skip the expensive local install+test cycle entirely if GitHub already
    recorded a definitive failure there (project was broken before
    Dependabot even touched it). This only ever saves wasted compute; it
    never changes a verdict once a case is actually reproduced locally, and
    if GitHub has no CI signal at all (very common for small repos) it
    simply does nothing and local reproduction proceeds as normal.
    Returns a rejection reason string, or None to proceed.

    The bar here is deliberately much higher than "some job was red". It used
    to reject on any failing check, which made this the single largest
    rejection bucket — 43% of everything processed — and most of those were
    repos with one red lint/CodeQL/docs job and a perfectly green test suite.
    Two things make that indefensible now:

      * we compare per-test outcomes, so a baseline that is *partly* red is
        still usable evidence; "already broken" no longer means "no signal";
      * a failing job at the parent commit is often in a workflow we never
        run locally anyway.

    So we now skip only on a total CI collapse — nothing succeeded at all —
    which is the one case where local reproduction really is certain to be
    wasted effort.
    """
    owner, repo_name = repo.split("/", 1)

    # github_client's cache writes (db.cache_set) never commit on their own —
    # the caller owns that. Without an explicit commit here, this connection
    # holds an open write transaction on research.sqlite for the entire
    # multi-hour reproduction run, blocking every other process (e.g. a fresh
    # stage2_fetch_live_github.py fetch) from writing to it at all.
    try:
        n_success, n_failure = gh.commit_ci_breakdown(main_conn, owner, repo_name, before_sha)
    finally:
        main_conn.commit()

    if n_failure > 0 and n_success == 0:
        return (f"GitHub CI at before_sha was a total collapse — {n_failure} check(s) failed and "
                f"none succeeded, so the project was already broken before the Dependabot update")

    return None


def _cache_get_baseline(rconn, repo: str, before_sha: str):
    """Return a BEFORE-snapshot result dict rebuilt from baseline_cache, or None."""
    if rconn is None:
        return None
    row = rconn.execute(
        "SELECT * FROM baseline_cache WHERE repo=? AND before_sha=?", (repo, before_sha)
    ).fetchone()
    if row is None:
        return None
    import json as _json
    targets = None
    if row["import_targets_json"]:
        try:
            targets = _json.loads(row["import_targets_json"])
        except Exception:
            targets = None
    plan = ExecutionPlan(
        strategy=row["execution_strategy"] or "none",
        command="python -m pytest -q" if row["execution_strategy"] == "pytest_real" else None,
        detail=row["execution_detail"] or "",
        import_targets=targets,
        evidence_strength=row["evidence_strength"] or "strong",
    )
    return {
        "install_result": row["install_result"],
        "install_excerpt": row["install_excerpt"] or "",
        "execution_strategy": row["execution_strategy"] or "none",
        "execution_detail": row["execution_detail"] or "",
        "execution_result": row["execution_result"] or "NOT_RUN",
        "execution_excerpt": row["execution_excerpt"] or "",
        "evidence_strength": row["evidence_strength"] or "none",
        "failure_stage": row["failure_stage"],
        "duration_seconds": 0.0,
        "exec_plan": plan,
        # Without this the per-test comparison silently degrades to nothing
        # whenever a baseline is served from cache — which is most of the time,
        # since one before_sha can cover up to 18 candidates.
        "tests": _json.loads(row["tests_json"]) if row["tests_json"] else {},
        "python_bin": row["python_bin"],
    }


def _cache_put_baseline(rconn, repo: str, before_sha: str, res: dict, python_bin: str = "") -> None:
    """
    Store a completed BEFORE snapshot for reuse by other candidates.

    A project's BEFORE state is a property of the commit, not of the pull
    request, so several candidates sharing a parent commit can reuse one
    baseline instead of rebuilding it each time. The interpreter that produced
    the baseline is stored with it: a reused baseline must pin the AFTER run to
    the same interpreter, otherwise a difference between the snapshots could be
    the Python version rather than the dependency. Never raises -- a caching
    failure must not lose the run that produced the result.
    """
    if rconn is None:
        return
    import json as _json
    plan = res.get("exec_plan")
    targets = getattr(plan, "import_targets", None) if plan else None
    try:
        rdb.safe_write(rconn, """
            INSERT OR REPLACE INTO baseline_cache
            (repo, before_sha, install_result, install_excerpt, execution_strategy,
             execution_detail, execution_result, execution_excerpt, evidence_strength,
             import_targets_json, failure_stage, tests_json, python_bin)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (repo, before_sha, res.get("install_result"), (res.get("install_excerpt") or "")[:1000],
              res.get("execution_strategy"), res.get("execution_detail"),
              res.get("execution_result"), (res.get("execution_excerpt") or "")[:1000],
              res.get("evidence_strength"),
              _json.dumps(targets) if targets else None, res.get("failure_stage"),
              _json.dumps(res.get("tests") or {}), python_bin))
        rconn.commit()
    except Exception:
        pass


def _verify_pair_with_retry(repo: str, after_sha: str, before_sha: str,
                            pr_number: int, attempts: int = 3) -> tuple[bool, str, str, str]:
    """
    Establish the BEFORE->AFTER parent relationship, retrying the fetch and
    falling back to re-anchoring the pair on the Dependabot commit.

    Returns (verified, before_sha, after_sha, message) — the SHAs come back
    because they may have been *replaced* by the anchored pair.

    A single attempt is not trustworthy. 312 candidates (39% of the CI-verified
    green->red batch, our highest-value pool) were rejected as
    UNVERIFIED_SHA_PAIR, but re-fetching them later verified 28 of 33 sampled
    with an exact_parent_match. The failure was always "could_not_parse_parent"
    — i.e. `git rev-parse <head>^1` could not see the object because the fetch
    had silently failed, three workers being pointed at the same shared bare
    cache while GitHub throttled us. That is our infrastructure failing, and
    rejecting a candidate for it discards good evidence.

    A genuine "parent_mismatch" is different: the objects are present and
    before_sha simply is not the parent. That is a real finding about the
    candidate, so it returns immediately without burning retries.
    """
    ok = False
    msg = "not attempted"
    for i in range(attempts):
        with repo_lock(repo):
            # Fetch the PR head ONLY (before_sha=None). Asking clone_or_update
            # to also guarantee the recorded before_sha is what produced the
            # SHA_FETCH_FAILED bucket: when that commit is not on the PR branch
            # — which is exactly the case when the recorded base has drifted —
            # the fetch escalates to `--unshallow`, downloading the repository's
            # entire history. On apache/airflow that blows past the 300s timeout,
            # and a deep fetch that fails part-way can leave the shallow boundary
            # such that even `rev-parse head^1` stops resolving, reported as
            # "could_not_parse_parent".
            #
            # We do not need that object anyway: verification only reads
            # `head^1` and compares SHA strings, and the BEFORE we ultimately
            # check out is the anchor's parent, which the PR-ref fetch already
            # brought in at depth GIT_SHALLOW_DEPTH*2.
            gf.clone_or_update(repo, after_sha, None, pr_number)
            ok, _parent, msg = gf.verify_sha_pairing(repo, after_sha, before_sha)
        if ok and msg == "exact_parent_match":
            # BEFORE is already exactly the parent of AFTER — anchoring would
            # return this same pair, so there is nothing to improve.
            return True, before_sha, after_sha, msg
        if ok or msg.startswith("parent_mismatch"):
            break
        if i < attempts - 1:
            logger.info(f"    [sha] {msg} — refetching ({i + 2}/{attempts})")
            time.sleep(2.0 * (i + 1))

    # ── Re-anchor on the Dependabot commit ──────────────────────────────────
    # Reached either because the recorded pair is not a parent/child pair at
    # all, or because it only matched loosely down the first-parent chain (so
    # the BEFORE->AFTER diff would carry unrelated commits alongside the
    # dependency update). Both are fixed by pairing the Dependabot commit with
    # its own parent; see git_fetcher.resolve_dependabot_pair.
    with repo_lock(repo):
        a_sha, b_sha, anchor_msg = gf.resolve_dependabot_pair(repo, after_sha)
        if a_sha and b_sha:
            gf.clone_or_update(repo, a_sha, b_sha, pr_number)
            anchored_ok, _p, anchored_msg = gf.verify_sha_pairing(repo, a_sha, b_sha)
        else:
            anchored_ok, anchored_msg = False, anchor_msg

    if anchored_ok:
        logger.info(f"    [sha] re-anchored on the dependabot commit: "
                    f"{b_sha[:8]} -> {a_sha[:8]} ({anchor_msg})")
        return True, b_sha, a_sha, f"{anchor_msg}; {anchored_msg}"

    if ok:
        # Anchoring did not apply, but the original pair did verify (loosely).
        return True, before_sha, after_sha, msg
    return False, before_sha, after_sha, f"{msg}; anchoring failed: {anchored_msg}"


def _run_pair(repo: str, pr_number: int, before_sha: str, after_sha: str,
              main_conn=None, rconn=None, dependency: str = None) -> dict:
    """
    Reproduce one candidate end to end and return its verdict.

    Runs the BEFORE and AFTER snapshots for a single pull request and
    classifies the difference between them. Work is serialised per repository,
    because the bare git cache is shared across worker processes.

    The commit pair may be re-anchored onto the Dependabot commit and its
    parent rather than the branch head, so that later human commits on the same
    branch cannot contaminate the comparison. Every subsequent step -- the
    snapshots, the baseline cache and the stored result -- uses the re-anchored
    pair.

    Returns a dictionary holding both snapshots, the verdict, the resolved
    commit pair, the interpreter used and timing information.
    """
    t0 = time.time()

    # Serialised per repo: the bare git cache is shared across workers.
    # NOTE: this may *replace* before_sha/after_sha with the Dependabot commit
    # and its parent, so every later use — snapshots, caching, the stored
    # result — must use the returned values, not the arguments.
    recorded_before, recorded_after = before_sha, after_sha
    sha_verified, before_sha, after_sha, sha_msg = _verify_pair_with_retry(
        repo, after_sha, before_sha, pr_number)
    sha_reanchored = (before_sha, after_sha) != (recorded_before, recorded_after)

    # Short-circuit before spending any install/test time: an unverified SHA
    # pair can never produce anything but a rejection, so don't pay for two
    # full local reproduction cycles to learn that.
    if not sha_verified:
        stub = {"failure_stage": None, "install_result": "N/A",
                "execution_result": "NOT_RUN", "execution_strategy": "none",
                "install_excerpt": f"sha verification: {sha_msg}"}
        if sha_msg.startswith("parent_mismatch"):
            # Objects are present, before_sha genuinely is not the parent of
            # after_sha, and the pair could not be re-anchored on a Dependabot
            # commit either — a real property of the candidate, not our failure.
            verdict = Verdict("SHA_PARENT_MISMATCH", "REJECTED",
                              f"before_sha is not the parent of after_sha and the pair could not be "
                              f"anchored on a dependabot commit — {sha_msg}")
        else:
            # Could not retrieve the commit objects at all. Keep this in its own
            # bucket: it says nothing about the candidate, only about our fetch,
            # and lumping it in with real mismatches hid a 39% loss of the best
            # candidates behind a plausible-looking methodology rejection.
            verdict = Verdict("SHA_FETCH_FAILED", "REJECTED",
                              f"could not retrieve the commit objects after 3 fetch attempts "
                              f"({sha_msg}) — harness/network failure, NOT evidence about the project; "
                              f"safe to re-queue")
        return {
            "sha_pair_verified": 0,
            "python_version_requested": "n/a", "python_version_actual": "n/a (skipped — unverified SHA pair)",
            "before": stub, "after": stub,
            "verdict": verdict,
            "before_sha": before_sha, "after_sha": after_sha,
            "sha_reanchored": 1 if sha_reanchored else 0, "sha_note": sha_msg,
            "duration_seconds": round(time.time() - t0, 2),
            "log_dir": "",
        }

    # Cheap CI pre-screen (network+cache, no local install/test) before
    # committing to the expensive part.
    if main_conn is not None:
        prescreen_reason = _prescreen_baseline_ci(repo, before_sha, main_conn)
        if prescreen_reason:
            stub = {"failure_stage": None, "install_result": "N/A",
                    "execution_result": "NOT_RUN", "execution_strategy": "none"}
            return {
                "sha_pair_verified": 1,
                "python_version_requested": "n/a", "python_version_actual": "n/a (skipped — baseline pre-screen)",
                "before": stub, "after": stub,
                "verdict": Verdict("PRE_SCREEN_BASELINE_BROKEN", "REJECTED", prescreen_reason),
                "before_sha": before_sha, "after_sha": after_sha,
                "sha_reanchored": 1 if sha_reanchored else 0, "sha_note": sha_msg,
                "duration_seconds": round(time.time() - t0, 2),
                "log_dir": "",
            }

    # Detect the required Python runtime once from the BEFORE checkout (STRICT
    # candidates only change manifest/lockfile, so this applies to AFTER too).
    probe_dir = Path(tempfile.mkdtemp(prefix="regr_probe_"))
    req_py = "3.x"
    requires_python = None
    try:
        with repo_lock(repo):
            ok, _ = gf.checkout_snapshot_worktree(repo, before_sha, probe_dir)
        if ok:
            plan = ea.get_execution_plan("pip", probe_dir)
            req_py = (plan.runtime_version if plan else None) or "3.x"
            iplan = plan_install(probe_dir)
            requires_python = iplan.requires_python if iplan else None
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

    # The CI hint is a preference; requires-python is the real constraint.
    py_bin, act_py_ver = _pick_interpreter(req_py, requires_python)
    if py_bin and "substituted" in act_py_ver:
        logger.info(f"    [runtime] requested {req_py}, not installed -> {act_py_ver}")

    log_dir = LOG_DIR / f"{repo.replace('/', '__')}__pr{pr_number}"

    if not py_bin:
        env_fail = {"failure_stage": "VENV", "install_result": "FAIL",
                     "execution_result": "NOT_RUN", "execution_strategy": "none"}
        return {
            "sha_pair_verified": 1 if sha_verified else 0,
            "python_version_requested": req_py,
            "python_version_actual": act_py_ver,
            "before": env_fail, "after": env_fail,
            "verdict": Verdict("RUNTIME_UNAVAILABLE", "REJECTED", act_py_ver),
            "before_sha": before_sha, "after_sha": after_sha,
            "sha_reanchored": 1 if sha_reanchored else 0, "sha_note": sha_msg,
            "duration_seconds": round(time.time() - t0, 2),
            "log_dir": "",
        }

    # BEFORE is a property of the commit, not of the PR — reuse it when another
    # candidate already established it for this exact (repo, before_sha).
    baseline_reused = 0
    before_res = _cache_get_baseline(rconn, repo, before_sha)

    # A cached baseline that failed to install for a version-sensitive reason
    # must NOT be reused: the cache short-circuits the interpreter-retry path
    # below, so the candidate would be served the same stale failure it was
    # re-queued to escape. 92 pending candidates were in exactly that trap.
    if (before_res is not None
            and before_res.get("install_result") not in ("PASS", None)
            and _VERSION_SENSITIVE_INSTALL.search(before_res.get("install_excerpt") or "")):
        logger.info("    [baseline cache] discarding cached version-sensitive install failure "
                    "so other interpreters can be tried")
        before_res = None

    if before_res is not None:
        baseline_reused = 1
        logger.info(f"    [baseline cache hit] reusing BEFORE result for {before_sha[:10]}")
        # Pin AFTER to the interpreter the cached BEFORE ran on. Otherwise a
        # baseline computed on 3.11 can be compared against an AFTER run on
        # 3.13, and a difference between them proves nothing about the
        # dependency.
        cached_py = before_res.get("python_bin")
        if cached_py and cached_py != py_bin and Path(cached_py).exists():
            logger.info(f"    [runtime] pinning AFTER to the cached baseline's interpreter: {cached_py}")
            py_bin = cached_py
            act_py_ver = f"{act_py_ver} -> pinned to cached baseline interpreter"
    else:
        before_res = run_snapshot(repo, pr_number, "BEFORE", before_sha, py_bin, WORK_ROOT, log_dir, protected_dep=dependency)

        # ── Retry the baseline on another interpreter ───────────────────────
        # 63% of baseline install failures are version-sensitive: "no matching
        # distribution" (31%) and source builds of C extensions failing (32%).
        # Both usually mean the pinned dependency publishes no wheel for the
        # interpreter we happened to choose — an artefact of this host, not a
        # broken project. Trying another interpreter costs one install and can
        # turn a dead candidate into a usable baseline.
        if (before_res.get("install_result") == "FAIL"
                and _VERSION_SENSITIVE_INSTALL.search(before_res.get("install_excerpt") or "")):
            for alt_ver, alt_path in _alternate_interpreters(py_bin, requires_python)[:2]:
                logger.info(f"    [runtime] baseline install failed on {act_py_ver}; "
                            f"retrying on Python {alt_ver[0]}.{alt_ver[1]}")
                retry = run_snapshot(repo, pr_number, "BEFORE", before_sha, alt_path, WORK_ROOT, log_dir, protected_dep=dependency)
                if retry.get("install_result") == "PASS":
                    before_res = retry
                    # AFTER must run on the same interpreter or the comparison
                    # is meaningless.
                    py_bin = alt_path
                    act_py_ver = f"Python {alt_ver[0]}.{alt_ver[1]} (baseline install retry)"
                    break

        _cache_put_baseline(rconn, repo, before_sha, before_res, py_bin)

    # Reuse BEFORE's detected execution plan for AFTER so both snapshots are
    # compared with the identical exercise mechanism (apples-to-apples).
    after_res = run_snapshot(repo, pr_number, "AFTER", after_sha, py_bin, WORK_ROOT, log_dir,
                              exec_plan_override=before_res.get("exec_plan"), protected_dep=dependency)

    verdict = classify(before_res, after_res, sha_verified)
    attempts = 1
    confirmation = None

    # ── Confirmation re-run for candidate regressions ───────────────────────
    # A single failing AFTER run can come from a flaky or network-dependent
    # test. The primary dataset must survive being challenged, so a candidate
    # TIER_3 is re-run end-to-end (cache bypassed) and only kept if the same
    # verdict reproduces. Non-reproducing cases are preserved as
    # TIER_3_UNSTABLE rather than silently dropped or silently counted.
    if verdict.tier in ("TIER_3_CONFIRMED", "TIER_3_TEST_REGRESSION"):
        logger.info("    [confirmation] candidate regression — re-running BEFORE+AFTER to rule out flakiness")
        before2 = run_snapshot(repo, pr_number, "BEFORE", before_sha, py_bin, WORK_ROOT, log_dir, protected_dep=dependency)
        after2 = run_snapshot(repo, pr_number, "AFTER", after_sha, py_bin, WORK_ROOT, log_dir,
                               exec_plan_override=before2.get("exec_plan"), protected_dep=dependency)
        verdict2 = classify(before2, after2, sha_verified)
        attempts = 2
        # The second run must land in the same tier — a whole-suite regression
        # that only reproduces as a subset regression (or vice versa) is not
        # the same finding and should not be waved through as reproduced.
        if verdict2.tier == verdict.tier:
            confirmation = "REPRODUCED_2_OF_2"
            verdict = Verdict(verdict.classification, verdict.tier,
                              verdict.reason + " [reproduced on an independent second run]")
        else:
            confirmation = f"NOT_REPRODUCED (2nd run: {verdict2.classification})"
            verdict = Verdict("UNSTABLE_REGRESSION", "TIER_3_UNSTABLE",
                              f"first run looked like a regression but a second independent run gave "
                              f"{verdict2.classification} — treated as flaky/non-deterministic, held out "
                              f"of the confirmed dataset")
            before_res, after_res = before2, after2

    return {
        "sha_pair_verified": 1 if sha_verified else 0,
        "python_version_requested": req_py,
        "python_version_actual": act_py_ver,
        "before": before_res, "after": after_res,
        "verdict": verdict,
        "attempts": attempts,
        "confirmation": confirmation,
        "baseline_reused": baseline_reused,
        "tests_evidence": test_regressions(before_res, after_res),
        "before_sha": before_sha, "after_sha": after_sha,
        "sha_reanchored": 1 if sha_reanchored else 0, "sha_note": sha_msg,
        "duration_seconds": round(time.time() - t0, 2),
        "log_dir": str(log_dir),
    }


def _save_result(conn, repo, pr_number, meta: dict, run_out: dict):
    """
    Write one completed result row to the regression database.

    Stores the verdict together with the full evidence needed to audit it
    later: both commit SHAs, the install and execution outcome of each
    snapshot, captured failure output, the interpreter used, the names of any
    regressed tests and the path to the full logs. Uses INSERT OR REPLACE so a
    re-run of the same candidate updates its row rather than duplicating it.
    """
    before, after, verdict = run_out["before"], run_out["after"], run_out["verdict"]
    rdb.safe_write(conn, """
        INSERT OR REPLACE INTO results
        (repo, pr_number, dependency, old_version, new_version, ecosystem,
         before_sha, after_sha, sha_pair_verified,
         python_version_requested, python_version_actual,
         execution_strategy, execution_detail,
         before_install_result, before_install_excerpt,
         before_execution_result, before_execution_excerpt,
         after_install_result, after_install_excerpt,
         after_execution_result, after_execution_excerpt,
         classification, tier, reason, duration_seconds, log_dir,
         evidence_strength, confirmation, attempts, baseline_reused,
         sha_reanchored, sha_note,
         tests_passing_before, tests_regressed, regressed_tests)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        repo, pr_number, meta.get("dependency"), meta.get("old_version"), meta.get("new_version"), "pip",
        # The SHAs actually tested, which differ from the collected ones
        # whenever the pair was re-anchored onto the dependabot commit.
        run_out.get("before_sha") or meta.get("before_sha"),
        run_out.get("after_sha") or meta.get("after_sha"),
        run_out["sha_pair_verified"],
        run_out["python_version_requested"], run_out["python_version_actual"],
        before.get("execution_strategy"), before.get("execution_detail"),
        before.get("install_result"), (before.get("install_excerpt") or "")[:1000],
        before.get("execution_result"), (before.get("execution_excerpt") or "")[:1000],
        after.get("install_result"), (after.get("install_excerpt") or "")[:1000],
        after.get("execution_result"), (after.get("execution_excerpt") or "")[:1000],
        verdict.classification, verdict.tier, verdict.reason,
        run_out["duration_seconds"], run_out.get("log_dir", ""),
        before.get("evidence_strength"), run_out.get("confirmation"),
        run_out.get("attempts", 1), run_out.get("baseline_reused", 0),
        run_out.get("sha_reanchored", 0), run_out.get("sha_note"),
        # (regressed_node_ids, baseline_passing, after_ran)
        (run_out.get("tests_evidence") or ([], None, None))[1],
        len((run_out.get("tests_evidence") or ([],))[0]) or None,
        ("\n".join((run_out.get("tests_evidence") or ([],))[0])[:4000] or None),
    ))
    conn.commit()


def _admits_new_major(old_spec: Optional[str], new_spec: Optional[str]) -> bool:
    """
    True when the update lets a *new major version* of the dependency in.

    Measured on 1,077 processed candidates, this is the strongest predictor of
    a real regression we have:

        admits a new major   n=321   TIER_3 1.25%   (+2 unstable)
        stays same-major     n=731   TIER_3 0.14%

    Nine times the hit rate, and it is exactly what theory predicts: a major
    version is where a library is allowed to remove or change public API, so
    it is where working code stops working. Every confirmed mcp case is a
    ceiling raise from `<2` to `<3`.
    """
    def upper_major(spec):
        """
        Highest major version a version specifier still permits, or None.

        An upper bound such as "<3" permits major 2, so the bound is decremented.
        Used to tell a constraint that merely widens a range apart from one that
        moves to a genuinely newer major version.
        """
        if not spec:
            return None
        lt = re.findall(r"<\s*=?\s*(\d+)", spec)
        if lt:
            return max(int(x) for x in lt) - 1
        m = re.search(r"(\d+)", spec)
        return int(m.group(1)) if m else None

    ou, nu = upper_major(old_spec), upper_major(new_spec)
    return ou is not None and nu is not None and nu > ou


def cmd_prioritize(args):
    """Re-rank the PENDING queue so the highest-probability candidates run first."""
    conn = rdb.get_connection()
    rows = conn.execute(
        "SELECT repo, pr_number, old_version, new_version, COALESCE(priority,5) p "
        "FROM candidates WHERE status='PENDING'").fetchall()
    promoted = 0
    for r in rows:
        if _admits_new_major(r["old_version"], r["new_version"]) and r["p"] > 0:
            rdb.safe_write(conn, "UPDATE candidates SET priority=0 WHERE repo=? AND pr_number=?",
                            (r["repo"], r["pr_number"]))
            promoted += 1
    conn.commit()
    logger.info(f"Promoted {promoted} of {len(rows)} pending candidates to priority 0 "
                f"(dependency update admits a new major version)")
    for row in conn.execute("SELECT COALESCE(priority,5) p, COUNT(*) n FROM candidates "
                             "WHERE status='PENDING' GROUP BY 1 ORDER BY 1"):
        logger.info(f"   priority {row['p']}: {row['n']} pending")
    conn.close()


def cmd_discover(args):
    """
    Move eligible pull requests from the research database into the work queue.

    Applies the candidate filters, then enforces the per-repository cap on
    anything already queued. Candidates trimmed by the cap are marked
    SKIPPED_REPO_CAP and preserved, never deleted, so the funnel stays
    auditable.
    """
    n = discover_candidates(limit=args.limit)
    logger.info(f"Discovered {n} new Python STRICT candidates from research.sqlite")
    pruned = prune_over_cap()
    if pruned:
        logger.info(f"Pruned {pruned} already-queued candidates over the per-repo cap "
                    f"(status SKIPPED_REPO_CAP — preserved, not deleted)")


def cmd_run(args):
    """
    Process queued candidates until the batch size or the queue is exhausted.

    Several workers may run this concurrently against one database; claims are
    atomic, so each candidate is processed exactly once. `--batch 0` drains the
    queue entirely.

    The research database is opened only as an optional CI pre-screen cache. It
    may be absent (a fresh worker machine has no collected pool and no token),
    and the pre-screen skips cleanly when it is, so its absence must never abort
    a run.
    """
    conn = rdb.get_connection()
    # research.sqlite is only the CI-pre-screen cache; on a fresh Linux
    # instance it may not exist, and it is entirely optional (the pre-screen
    # skips cleanly when main_conn is None). Never let its absence abort a run.
    # Linux instance: no GitHub token and no github_cache table here, and the
    # CI pre-screen is a pure compute-saving optimization that skips cleanly
    # when main_conn is None. Disable it rather than crash per-candidate; every
    # candidate simply gets fully reproduced locally, which is what we want.
    main_conn = None

    # Crash recovery: anything stuck RUNNING goes back to PENDING — but only
    # if it's been claimed for longer than any single candidate could
    # legitimately take (worst case ~45min: two full install+timeout+exec
    # cycles). Resetting on status='RUNNING' alone (no age check) is unsafe
    # with multiple concurrent workers: it would treat a sibling worker's
    # in-progress claim as "orphaned" and yank it back to PENDING mid-flight,
    # causing two workers to duplicate the same candidate (this happened).
    stuck = conn.execute(
        "SELECT COUNT(*) c FROM candidates WHERE status='RUNNING' "
        "AND (claimed_at IS NULL OR claimed_at < datetime('now', '-60 minutes'))"
    ).fetchone()["c"]
    if stuck:
        rdb.safe_write(conn, """
            UPDATE candidates SET status='PENDING'
            WHERE status='RUNNING' AND (claimed_at IS NULL OR claimed_at < datetime('now', '-60 minutes'))
        """)
        conn.commit()
        logger.info(f"Recovered {stuck} candidate(s) genuinely stuck RUNNING (claimed >60min ago)")

    processed = 0
    consecutive_errors = 0
    try:
        while args.batch == 0 or processed < args.batch:
            # Every step below is wrapped so nothing — a locked DB, a network
            # blip, an unanticipated exception anywhere in the chain — can
            # ever kill the batch. Worst case: this one candidate is logged
            # as PIPELINE_ERROR / left PENDING for retry, and the loop moves on.
            try:
                # Atomic claim — safe when multiple 'run' processes share this
                # database concurrently (see claim_next_pending docstring).
                row = rdb.claim_next_pending(conn)
                if not row:
                    logger.info("No more PENDING candidates.")
                    break
                repo, pr_number = row["repo"], row["pr_number"]
                logger.info(f"[{processed + 1}] {repo} #{pr_number}  "
                            f"({row['dependency']} {row['old_version']} -> {row['new_version']})")

                try:
                    run_out = _run_pair(repo, pr_number, row["before_sha"], row["after_sha"],
                                        main_conn=main_conn, rconn=conn, dependency=row["dependency"])
                    _save_result(conn, repo, pr_number, dict(row), run_out)
                    v = run_out["verdict"]
                    logger.info(f"    => [{v.tier}] {v.classification} — {v.reason}")
                except Exception as e:
                    logger.exception(f"    ERROR on {repo}#{pr_number}: {e}")
                    try:
                        rdb.safe_write(conn, """
                            INSERT OR REPLACE INTO results (repo, pr_number, classification, tier, reason)
                            VALUES (?,?,?,?,?)
                        """, (repo, pr_number, "PIPELINE_ERROR", "REJECTED", str(e)[:500]))
                        conn.commit()
                    except Exception:
                        pass  # even the error-logging write failed — don't let that crash the loop either

                try:
                    rdb.safe_write(conn, "UPDATE candidates SET status='DONE' WHERE repo=? AND pr_number=?", (repo, pr_number))
                    conn.commit()
                except Exception:
                    logger.warning(f"    Could not mark {repo}#{pr_number} DONE — it will be retried next run")

                processed += 1
                consecutive_errors = 0

            except KeyboardInterrupt:
                raise
            except Exception as loop_err:
                consecutive_errors += 1
                logger.exception(f"Unexpected error in run loop (consecutive: {consecutive_errors}): {loop_err}")
                if consecutive_errors >= 5:
                    logger.error("5 consecutive unexpected errors — stopping this batch to avoid a crash loop. "
                                 "Progress so far is saved; just restart 'run' to continue.")
                    break
                time.sleep(5)
    except KeyboardInterrupt:
        logger.info(f"\n[Ctrl+C] Stopped after {processed} candidates this session. Safe to restart — resumable.")

    logger.info(f"Batch complete: {processed} candidates processed.")
    conn.close()
    main_conn.close() if main_conn is not None else None


def cmd_validate(args):
    """
    Re-run a fixed set of known cases to check the pipeline still behaves.

    Used as a regression test of the harness itself: if a change to the
    detection, execution or classification logic alters the verdict on a case
    whose outcome is already established, this surfaces it.
    """
    conn = rdb.get_connection()
    main_conn = main_db.get_connection(C.DB_PATH)
    logger.info(f"Running validation on {len(VALIDATION_SET)} known cases...")
    for repo, pr_number, before_sha, after_sha in VALIDATION_SET:
        logger.info(f"\n--- VALIDATE: {repo} #{pr_number} ---")
        run_out = _run_pair(repo, pr_number, before_sha, after_sha, main_conn=main_conn, rconn=conn)
        v = run_out["verdict"]
        b, a = run_out["before"], run_out["after"]
        print(f"  {repo} #{pr_number}")
        print(f"    SHA verified:        {bool(run_out['sha_pair_verified'])}")
        print(f"    Python:              {run_out['python_version_actual']}")
        print(f"    BEFORE: install={b.get('install_result')} "
              f"exec[{b.get('execution_strategy')}]={b.get('execution_result')} ({b.get('execution_detail')})")
        print(f"    AFTER:  install={a.get('install_result')} "
              f"exec[{a.get('execution_strategy')}]={a.get('execution_result')} ({a.get('execution_detail')})")
        print(f"    VERDICT: [{v.tier}] {v.classification} — {v.reason}\n")
        _save_result(conn, repo, pr_number, {"before_sha": before_sha, "after_sha": after_sha}, run_out)
    conn.close()
    main_conn.close() if main_conn is not None else None


def cmd_triage(args):
    """
    Re-rank the existing PENDING queue by GitHub's CI signal, without
    re-fetching anything from search. Most of these API responses are already
    in github_cache, so this is fast and nearly free.

      priority 1  green -> red   (the target pattern — claimed first)
      priority 3  unknown        (no decisive CI signal either side)
      priority 8  after was green (CI says the update did not break it)
      priority 9  before was red  (baseline already broken)
    """
    conn = rdb.get_connection()
    main_conn = main_db.get_connection(C.DB_PATH)
    # Only ever look at candidates that have NOT been triaged yet (priority 5
    # is the untouched default) — otherwise repeated supervisor cycles would
    # keep re-checking the same already-ranked rows and burn the API budget.
    rows = conn.execute(
        "SELECT repo, pr_number, before_sha, after_sha FROM candidates "
        "WHERE status='PENDING' AND COALESCE(priority,5)=5 ORDER BY rowid LIMIT ?", (args.limit,)
    ).fetchall()
    logger.info(f"Triaging {len(rows)} pending candidates by CI signal...")

    counts = {}
    for i, r in enumerate(rows, 1):
        try:
            owner, name = r["repo"].split("/", 1)
            before = gh.check_commit_ci_status(main_conn, owner, name, r["before_sha"])
            after = gh.check_commit_ci_status(main_conn, owner, name, r["after_sha"])
            main_conn.commit()
            if before == "success" and after == "failure":
                pri, label = 1, "green->red"
            elif before == "failure":
                pri, label = 9, "baseline_red"
            elif after == "success":
                pri, label = 8, "after_green"
            else:
                pri, label = 3, "unknown"
            rdb.safe_write(conn, "UPDATE candidates SET priority=? WHERE repo=? AND pr_number=?",
                           (pri, r["repo"], r["pr_number"]))
            counts[label] = counts.get(label, 0) + 1
        except Exception as e:
            logger.warning(f"  triage failed for {r['repo']}#{r['pr_number']}: {e}")
        if i % 100 == 0:
            conn.commit()
            logger.info(f"  triaged {i}/{len(rows)} | {counts}")

    conn.commit()
    logger.info(f"Triage complete: {counts}")
    conn.close()
    main_conn.close() if main_conn is not None else None


def cmd_status(args):
    """
    Print the candidate funnel and the results broken down by tier.
    """
    conn = rdb.get_connection()
    print("=== CANDIDATE FUNNEL ===")
    for r in conn.execute("SELECT status, COUNT(*) c FROM candidates GROUP BY status ORDER BY c DESC"):
        print(f"  {r['status']}: {r['c']}")
    print("\n=== RESULTS BY TIER ===")
    for r in conn.execute("SELECT tier, COUNT(*) c FROM results GROUP BY tier ORDER BY c DESC"):
        print(f"  {r['tier']}: {r['c']}")
    print("\n=== RESULTS BY CLASSIFICATION (rejection reasons visible here) ===")
    for r in conn.execute("SELECT classification, COUNT(*) c FROM results GROUP BY classification ORDER BY c DESC"):
        print(f"  {r['classification']}: {r['c']}")
    print("\n=== CONFIRMED-CASE QUALITY ===")
    for r in conn.execute("""SELECT COALESCE(evidence_strength,'?') es, COALESCE(confirmation,'(pre-confirmation run)') cf,
                                    COUNT(*) c FROM results WHERE tier='TIER_3_CONFIRMED'
                             GROUP BY es, cf"""):
        print(f"  evidence={r['es']:8} {r['cf']}: {r['c']}")
    n_unstable = conn.execute("SELECT COUNT(*) c FROM results WHERE tier='TIER_3_UNSTABLE'").fetchone()["c"]
    if n_unstable:
        print(f"  held out as flaky (TIER_3_UNSTABLE): {n_unstable}")
    reused = conn.execute("SELECT COUNT(*) c FROM results WHERE baseline_reused=1").fetchone()["c"]
    if reused:
        print(f"\nBaseline cache hits (BEFORE run skipped): {reused}")

    n_confirmed = conn.execute("SELECT COUNT(*) c FROM results WHERE tier='TIER_3_CONFIRMED'").fetchone()["c"]
    n_subset = conn.execute("SELECT COUNT(*) c FROM results WHERE tier='TIER_3_TEST_REGRESSION'").fetchone()["c"]
    print(f"\nWhole-suite PASS->FAIL cases (TIER_3_CONFIRMED):     {n_confirmed}")
    print(f"Per-test PASS->FAIL cases (TIER_3_TEST_REGRESSION): {n_subset}")
    print(f"Combined tier-3 evidence: {n_confirmed + n_subset} / 100 target")
    if n_subset:
        print("\n=== PER-TEST REGRESSIONS ===")
        for r in conn.execute("""SELECT repo, pr_number, dependency, tests_regressed, tests_passing_before,
                                         COALESCE(confirmation,'(unconfirmed)') cf
                                  FROM results WHERE tier='TIER_3_TEST_REGRESSION'
                                  ORDER BY tests_regressed DESC"""):
            print(f"  {r['repo']}#{r['pr_number']} [{r['dependency']}] "
                  f"{r['tests_regressed']}/{r['tests_passing_before']} tests broke — {r['cf']}")
    conn.close()


def cmd_export(args):
    """
    Write the results table to CSV, one file per tier plus a combined file.

    Every column is exported. The per-tier files are filtered views of the same
    rows, so the combined file alone is sufficient for analysis.
    """
    import csv
    conn = rdb.get_connection()
    cols = [d[0] for d in conn.execute("SELECT * FROM results LIMIT 0").description]
    rows = conn.execute("SELECT * FROM results ORDER BY tier ASC, repo ASC").fetchall()

    out_dir = rdb.REGRESSION_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "full":      out_dir / "regression_funnel_full.csv",
        "confirmed": out_dir / "confirmed_tier3_pass_to_fail.csv",
        "test_regression": out_dir / "tier3_per_test_regressions.csv",
        "tier2":     out_dir / "tier2_install_regressions.csv",
        "rejected":  out_dir / "rejected_inconclusive.csv",
    }
    filters = {
        "full":      lambda r: True,
        "confirmed": lambda r: r["tier"] == "TIER_3_CONFIRMED",
        "test_regression": lambda r: r["tier"] == "TIER_3_TEST_REGRESSION",
        "tier2":     lambda r: r["tier"] == "TIER_2_INSTALL_REGRESSION",
        "rejected":  lambda r: r["tier"] == "REJECTED",
    }

    counts = {}
    for key, path in paths.items():
        subset = [r for r in rows if filters[key](r)]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in subset:
                w.writerow({c: r[c] for c in cols})
        counts[key] = len(subset)

    logger.info(f"Exported: {counts['full']} total | {counts['confirmed']} confirmed (TIER_3) | "
                f"{counts['tier2']} install-regressions (TIER_2) | {counts['rejected']} rejected")
    for key, path in paths.items():
        logger.info(f"  {key}: {path}")
    conn.close()


def main():
    """
    Parse arguments and dispatch to the requested subcommand.

    Subcommands: discover (fill the queue), run (process it), validate (re-run
    known cases), status (report progress) and export (write CSVs).
    """
    parser = argparse.ArgumentParser(description="Corrected Python Dependabot regression pipeline")
    sub = parser.add_subparsers(dest="command")

    p_disc = sub.add_parser("discover")
    p_disc.add_argument("--limit", type=int, default=500)

    p_run = sub.add_parser("run")
    p_run.add_argument("--batch", type=int, default=20, help="0 = run until no PENDING left")

    p_tri = sub.add_parser("triage")
    p_tri.add_argument("--limit", type=int, default=2000)

    sub.add_parser("prioritize")
    sub.add_parser("validate")
    sub.add_parser("status")
    sub.add_parser("export")

    args = parser.parse_args()
    handlers = {"discover": cmd_discover, "run": cmd_run, "validate": cmd_validate,
                "status": cmd_status, "export": cmd_export, "triage": cmd_triage,
                "prioritize": cmd_prioritize}
    handlers.get(args.command, lambda a: parser.print_help())(args)


if __name__ == "__main__":
    # Outer safety net: an unattended overnight run must never exit with a
    # raw traceback. Anything that escapes every inner try/except still gets
    # logged here and the process exits cleanly; whatever was already saved
    # to regression.sqlite is preserved either way.
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n[Ctrl+C] Stopped by user.")
    except Exception as fatal_err:
        logger.exception(f"FATAL (uncaught): {fatal_err}")
        sys.exit(0)
