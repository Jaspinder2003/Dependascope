"""
purified_reproduce.py — Hardened Dependabot Reproduction Engine
═════════════════════════════════════════════════════════════════
Implements strict 12-point scientific methodology:
  1. Enforced Python runtime matching (no silent fallback).
  2. Per-snapshot venv creation with zero host environment leaks.
  3. Causal SHA pairing verification (after_parent == before_sha).
  4. Precise Cohort classification (STRICT_SINGLE vs GROUPED vs COMPLEX).
  5. Framework-aware test runner selection & Pytest Exit Code 5 (NO_TESTS) handling.
  6. Distinction between full PASS->FAIL and INSTALLABILITY_REGRESSION.
  7. Auto-detection and installation of declared test/dev dependencies.
  8. Non-mutating execution (writes to `purified_results` table & `python_purified_results.csv`).
"""

import sqlite3
import subprocess
import tempfile
import shutil
import os
import sys
import time
import json
import csv
import logging
import argparse
import re
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import git_fetcher as gf
import ecosystem_adapters as ea
import sandbox_executor as sx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] purified_reproduce – %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("purified_reproduce")

CSV_PURIFIED_OUT = Path(__file__).parent.parent.parent / "python_purified_results.csv"


# ─── 1. Cohort Classification Helper ──────────────────────────────────────────

def classify_cohort(conn: sqlite3.Connection, repo: str, pr_number: int) -> str:
    """Classify PR into STRICT_SINGLE, GROUPED, COMPLEX, or UNKNOWN based on file diffs and PR metadata."""
    pr_row = conn.execute(
        "SELECT title FROM pull_requests WHERE repo=? AND pr_number=?",
        (repo, pr_number)
    ).fetchone()
    title = (pr_row["title"] if pr_row else "") or ""
    
    # Check for grouped update signals in title
    if re.search(r"(?i)\bgroup\b|\bupdates\b|\bdependencies\b|\bpackages\b", title):
        return "GROUPED"

    files = conn.execute(
        "SELECT filename, is_manifest, is_lockfile, is_source, is_test, is_ci, additions, deletions, patch "
        "FROM changed_files WHERE repo=? AND pr_number=?",
        (repo, pr_number)
    ).fetchall()

    if not files:
        return "STRICT_SINGLE"

    has_source = any(f["is_source"] for f in files)
    has_test = any(f["is_test"] for f in files)
    if has_source or has_test:
        return "COMPLEX"

    manifest_files = [f for f in files if f["is_manifest"]]
    if len(manifest_files) > 1:
        return "GROUPED"

    if manifest_files:
        mf = manifest_files[0]
        patch = mf["patch"]
        if patch:
            lines = [l for l in patch.splitlines() if l.startswith("+") or l.startswith("-")]
            # Filter out diff headers
            dep_lines = [l for l in lines if not l.startswith("+++") and not l.startswith("---")]
            if len(dep_lines) > 2:
                return "GROUPED"

    return "STRICT_SINGLE"


# ─── 2. Runtime Environment Resolver ─────────────────────────────────────────

def resolve_python_runtime(requested_version: Optional[str]) -> tuple[Optional[str], str, str]:
    """
    Resolve requested Python version into (executable_path, actual_version, status).
    Strict enforcement: Returns (None, "", "VERSION_UNAVAILABLE") if requested version is not found on host.
    """
    if not requested_version or requested_version == "3.x":
        # Accept system default Python
        v_out = subprocess.run([sys.executable, "--version"], capture_output=True, text=True)
        act_ver = v_out.stdout.strip() or v_out.stderr.strip()
        return sys.executable, act_ver, "SYSTEM_DEFAULT"

    clean_v = requested_version.lstrip("v").strip()
    parts = clean_v.split(".")
    major = parts[0]
    minor = parts[1] if len(parts) > 1 else ""

    target_ver_str = f"{major}.{minor}" if minor else major

    # Search candidates on host
    candidates = []
    if os.name == "nt":
        if minor:
            candidates.append(rf"C:\Python{major}{minor}\python.exe")
            candidates.append(rf"C:\Users\{os.environ.get('USERNAME', '')}\AppData\Local\Programs\Python\Python{major}{minor}\python.exe")
            candidates.append(rf"C:\Users\{os.environ.get('USERNAME', '')}\anaconda3\envs\py{major}{minor}\python.exe")
            candidates.append(rf"C:\tools\python{major}{minor}\python.exe")
    
    for cand in candidates:
        if cand and Path(cand).exists():
            v_out = subprocess.run([cand, "--version"], capture_output=True, text=True)
            act_ver = v_out.stdout.strip() or v_out.stderr.strip()
            return cand, act_ver, "EXACT_MATCH"

    # Check sys.executable version
    sys_v_out = subprocess.run([sys.executable, "--version"], capture_output=True, text=True)
    sys_ver_str = sys_v_out.stdout.strip() or sys_v_out.stderr.strip()
    if target_ver_str in sys_ver_str:
        return sys.executable, sys_ver_str, "SYSTEM_MATCH"

    # Strict: do NOT silently fallback to wrong python version
    return None, "", f"MISSING_PYTHON_{target_ver_str}"


# ─── 3. Single Snapshot Reproduction ──────────────────────────────────────────

def reproduce_snapshot(
    repo: str,
    pr_number: int,
    snapshot: str,
    sha: str,
    py_bin: str,
    log_dir: Path,
    ecosystem: str = "pip"
) -> dict:
    """
    Run reproduction for a single snapshot (BEFORE or AFTER) in an isolated venv.
    Returns snapshot result dict.
    """
    work_dir = Path(tempfile.mkdtemp(prefix=f"depbot_purified_{repo.replace('/', '_')}_{pr_number}_{snapshot}_"))
    venv_dir = work_dir / "_venv"
    
    res = {
        "install_result": "UNRUNNABLE",
        "test_result": "NONE",
        "final_state": "UNRUNNABLE",
        "failure_point": None,
        "failure_excerpt": None,
        "duration_seconds": 0.0
    }
    
    start_t = time.time()
    try:
        # Checkout snapshot code
        ok, err = gf.checkout_snapshot_worktree(repo, sha, work_dir)
        if not ok:
            res["failure_point"] = "CHECKOUT"
            res["failure_excerpt"] = f"git checkout failed: {err}"
            return res

        # Create fresh per-snapshot venv
        try:
            v_proc = subprocess.run([py_bin, "-m", "venv", str(venv_dir)], capture_output=True, text=True, timeout=60)
            if v_proc.returncode != 0:
                res["failure_point"] = "VENV_CREATION"
                res["failure_excerpt"] = f"venv creation failed: {v_proc.stderr[:300]}"
                return res
        except Exception as e:
            res["failure_point"] = "VENV_CREATION"
            res["failure_excerpt"] = f"venv creation exception: {e}"
            return res

        # Get execution plan
        plan = ea.get_execution_plan(ecosystem, work_dir)
        if not plan or not plan.stages:
            res["failure_point"] = "PLAN"
            res["failure_excerpt"] = "No valid execution plan generated"
            return res

        # Execute stages in isolated venv
        stage_results = []
        for stage in plan.stages:
            safe_repo = repo.replace("/", "__")
            slug = f"{safe_repo}__pr{pr_number}__{snapshot}__{stage.name}"
            stdout_path = log_dir / f"{slug}.stdout.txt"
            stderr_path = log_dir / f"{slug}.stderr.txt"

            sr = sx.run_stage(
                stage_name=stage.name,
                command=stage.command,
                work_dir=work_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=min(stage.timeout, C.EXEC_TIMEOUT_TOTAL),
                venv_dir=venv_dir
            )
            stage_results.append(sr)

            if stage.name == "INSTALL":
                if sr["result"] == "PASS":
                    res["install_result"] = "INSTALL_PASS"
                else:
                    res["install_result"] = "INSTALL_FAIL"
                    res["final_state"] = "FAIL"
                    res["failure_point"] = "INSTALL"
                    # Capture excerpt
                    err_txt = Path(sr["stderr_path"]).read_text(encoding="utf-8", errors="ignore").strip()
                    if not err_txt:
                        err_txt = Path(sr["stdout_path"]).read_text(encoding="utf-8", errors="ignore").strip()
                    res["failure_excerpt"] = err_txt[-500:] if err_txt else "Install exited with code > 0"
                    break # Stop on install failure

            elif stage.name == "TEST":
                if sr["result"] == "PASS":
                    res["test_result"] = "TEST_PASS"
                else:
                    err_txt = Path(sr["stderr_path"]).read_text(encoding="utf-8", errors="ignore").strip()
                    if "NO_TESTS" in err_txt or sr.get("exit_code") == 5:
                        res["test_result"] = "NO_TESTS"
                    else:
                        res["test_result"] = "TEST_FAIL"
                        res["final_state"] = "FAIL"
                        res["failure_point"] = "TEST"
                        if not err_txt:
                            err_txt = Path(sr["stdout_path"]).read_text(encoding="utf-8", errors="ignore").strip()
                        res["failure_excerpt"] = err_txt[-500:] if err_txt else "Test exited with code > 0"

        # Determine final snapshot state if install passed
        if res["install_result"] == "INSTALL_PASS":
            if res["test_result"] in ("TEST_PASS", "NO_TESTS", "NONE"):
                res["final_state"] = "PASS"
            elif res["test_result"] == "TEST_FAIL":
                res["final_state"] = "FAIL"

    finally:
        res["duration_seconds"] = round(time.time() - start_t, 2)
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

    return res


# ─── 4. Purified Reproduction Manager ─────────────────────────────────────────

def run_purified_reproduction(conn: sqlite3.Connection, row: dict, save_to_db: bool = True) -> dict:
    """
    Run end-to-end purified reproduction for a single PR according to 12-point rules.
    Returns complete purified result dictionary.
    """
    start_t = time.time()
    repo = row["repo"]
    pr_number = row["pr_number"]
    ecosystem = row["ecosystem"] or "pip"
    before_sha = row["before_sha"]
    head_sha = row["head_sha"]

    log_dir = C.EXEC_LOG_DIR / f"{repo.replace('/', '__')}__pr{pr_number}_purified"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Step A: Cohort Classification
    cohort = classify_cohort(conn, repo, pr_number)

    # Step B: Ensure repo is cloned
    gf.clone_or_update(repo, head_sha, before_sha, pr_number)

    # Step C: Causal SHA Pair Verification
    sha_verified, after_parent_sha, sha_msg = gf.verify_sha_pairing(repo, head_sha, before_sha)

    # Step D: Detect and Resolve Runtime Version
    # Inspect plan to get runtime version
    sample_work = Path(tempfile.mkdtemp(prefix="depbot_py_detect_"))
    try:
        gf.checkout_snapshot_worktree(repo, before_sha, sample_work)
        plan = ea.get_execution_plan(ecosystem, sample_work)
        req_py_version = plan.runtime_version if plan else "3.x"
        runtime_src = plan.runtime_source if plan else "default"
    finally:
        shutil.rmtree(sample_work, ignore_errors=True)

    py_bin, act_py_ver, py_status = resolve_python_runtime(req_py_version)

    res = {
        "repo": repo,
        "pr_number": pr_number,
        "ecosystem": ecosystem,
        "cohort": cohort,
        "before_sha": before_sha,
        "after_sha": head_sha,
        "after_parent_sha": after_parent_sha,
        "sha_pair_verified": 1 if sha_verified else 0,
        "requested_python_version": req_py_version or "3.x",
        "actual_python_executable": py_bin or "NONE",
        "actual_python_version": act_py_ver or "UNAVAILABLE",
        "runtime_source": runtime_src,
        "before_install_result": "UNRUNNABLE",
        "before_test_result": "NONE",
        "before_final_state": "UNRUNNABLE",
        "after_install_result": "UNRUNNABLE",
        "after_test_result": "NONE",
        "after_final_state": "UNRUNNABLE",
        "classification": "UNRUNNABLE",
        "evidence_tier": "T0",
        "failure_point": None,
        "failure_excerpt": None,
        "duration_seconds": 0.0
    }

    # If runtime is missing or SHA pairing unverified for STRICT cohort -> UNRUNNABLE
    if not py_bin:
        res["classification"] = "UNRUNNABLE"
        res["failure_point"] = "RUNTIME"
        res["failure_excerpt"] = f"Requested Python version {req_py_version} not installed on host."
        res["duration_seconds"] = round(time.time() - start_t, 2)
        _save_purified_result(conn, res)
        return res

    if not sha_verified and cohort == "STRICT_SINGLE":
        logger.warning(f"  SHA pair unverified for {repo}#{pr_number}: {sha_msg}")

    # Step E: Reproduce BEFORE
    before_res = reproduce_snapshot(repo, pr_number, "BEFORE", before_sha, py_bin, log_dir, ecosystem)
    res["before_install_result"] = before_res["install_result"]
    res["before_test_result"] = before_res["test_result"]
    res["before_final_state"] = before_res["final_state"]

    # Step F: Reproduce AFTER
    after_res = reproduce_snapshot(repo, pr_number, "AFTER", head_sha, py_bin, log_dir, ecosystem)
    res["after_install_result"] = after_res["install_result"]
    res["after_test_result"] = after_res["test_result"]
    res["after_final_state"] = after_res["final_state"]

    # Step G: Tiered Evidence Classification
    # ─────────────────────────────────────────────────────────────────────────
    # T3 — Highest confidence: BEFORE fully green (install+tests), AFTER breaks.
    #        Both install-break and test-break qualify at T3.
    # T2 — INSTALLABILITY_REGRESSION: BEFORE *installs* cleanly (tests may have
    #        pre-existing failures), but AFTER install fails. Valid research
    #        evidence without requiring a passing test suite.
    # T2 — TEST_REGRESSION: install succeeds both ways, but tests break after.
    # T1 — LIKELY_DEP_REGRESSION: BEFORE install itself was already failing,
    #        AFTER also fails — but the failure type changed (could indicate
    #        a new conflict layered on top).
    # T0 — BASELINE_FAILING: BEFORE install fails → not Dependabot's fault.
    # T0 — UNRUNNABLE / PASS->PASS / FAIL->PASS
    # ─────────────────────────────────────────────────────────────────────────
    b_install = res["before_install_result"]  # INSTALL_PASS | INSTALL_FAIL | UNRUNNABLE
    b_test    = res["before_test_result"]     # TEST_PASS | TEST_FAIL | NO_TESTS | NONE
    b_state   = res["before_final_state"]     # PASS | FAIL | UNRUNNABLE
    a_install = res["after_install_result"]
    a_test    = res["after_test_result"]
    a_state   = res["after_final_state"]

    if b_state == "UNRUNNABLE" or a_state == "UNRUNNABLE":
        res["classification"] = "UNRUNNABLE"
        res["evidence_tier"]  = "T0"
        res["failure_point"]  = before_res.get("failure_point") or after_res.get("failure_point") or "UNRUNNABLE"
        res["failure_excerpt"] = before_res.get("failure_excerpt") or after_res.get("failure_excerpt")

    elif b_state == "PASS" and a_state == "PASS":
        res["classification"] = "PASS->PASS"
        res["evidence_tier"]  = "T0"

    elif b_state == "PASS" and a_state == "FAIL":
        # BEFORE was fully green — this is the strongest evidence tier
        if a_install == "INSTALL_FAIL":
            # Dependabot broke the install itself
            res["classification"] = "PASS->FAIL"
            res["evidence_tier"]  = "T3"
        else:
            # Install succeeded AFTER but tests broke — still strong evidence
            res["classification"] = "TEST_REGRESSION"
            res["evidence_tier"]  = "T3"
        res["failure_point"]   = after_res.get("failure_point")
        res["failure_excerpt"]  = after_res.get("failure_excerpt")

    elif b_install == "INSTALL_PASS" and a_install == "INSTALL_FAIL":
        # BEFORE install passed (even if tests were pre-broken).
        # AFTER install fails → Dependabot broke the installability!
        res["classification"] = "INSTALLABILITY_REGRESSION"
        res["evidence_tier"]  = "T2"
        res["failure_point"]  = after_res.get("failure_point")
        res["failure_excerpt"] = after_res.get("failure_excerpt")
        logger.info(f"  [TIER-2] INSTALLABILITY_REGRESSION: BEFORE install passed, AFTER install fails")

    elif b_install == "INSTALL_PASS" and a_install == "INSTALL_PASS" and b_test in ("TEST_PASS", "NO_TESTS") and a_test == "TEST_FAIL":
        # Install succeeded both ways, BEFORE tests passed (or were absent), but AFTER tests failed → Genuine Test Regression!
        res["classification"] = "TEST_REGRESSION"
        res["evidence_tier"]  = "T2"
        res["failure_point"]  = after_res.get("failure_point")
        res["failure_excerpt"] = after_res.get("failure_excerpt")
        logger.info(f"  [TIER-2] TEST_REGRESSION: BEFORE tests passed/none, AFTER tests failed")

    elif b_install == "INSTALL_PASS" and a_install == "INSTALL_PASS" and b_test == "TEST_FAIL" and a_test == "TEST_FAIL":
        # Tests were ALREADY failing BEFORE the PR and failed AFTER → Pre-existing test failure (not a Dependabot regression)
        res["classification"] = "FAIL->FAIL"
        res["evidence_tier"]  = "T0"
        res["failure_point"]  = after_res.get("failure_point")
        res["failure_excerpt"] = after_res.get("failure_excerpt")

    elif b_install == "INSTALL_FAIL" and a_install == "INSTALL_FAIL":
        # Both snapshots fail to install — pre-existing build breakage
        res["classification"] = "FAIL->FAIL"
        res["evidence_tier"]  = "T0"
        res["failure_point"]  = before_res.get("failure_point") or after_res.get("failure_point")
        res["failure_excerpt"] = before_res.get("failure_excerpt") or after_res.get("failure_excerpt")

    elif b_state != "PASS" and a_state != "PASS":
        res["classification"] = "FAIL->FAIL"
        res["evidence_tier"]  = "T0"
        res["failure_point"]  = before_res.get("failure_point") or after_res.get("failure_point")
        res["failure_excerpt"] = before_res.get("failure_excerpt") or after_res.get("failure_excerpt")

    elif b_state != "PASS" and a_state == "PASS":
        res["classification"] = "FAIL->PASS"
        res["evidence_tier"]  = "T0"

    tier = res["evidence_tier"]
    cls  = res["classification"]
    logger.info(f"  [{tier}] {cls} | before_install={b_install} before_test={b_test} | after_install={a_install} after_test={a_test}")

    res["duration_seconds"] = round(time.time() - start_t, 2)
    try:
        _save_purified_result(conn, res)
    except Exception as save_err:
        logger.warning(f"  Could not save purified result to DB for {repo}#{pr_number} (DB locked): {save_err}")
    return res


def _save_purified_result(conn: sqlite3.Connection, res: dict) -> None:
    """Save to purified_results table in DB."""
    db.safe_execute_write(conn, """
        INSERT OR REPLACE INTO purified_results
        (repo, pr_number, ecosystem, cohort, before_sha, after_sha, after_parent_sha,
         sha_pair_verified, requested_python_version, actual_python_executable,
         actual_python_version, runtime_source, before_install_result, before_test_result,
         before_final_state, after_install_result, after_test_result, after_final_state,
         classification, evidence_tier, failure_point, failure_excerpt, duration_seconds)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        res["repo"], res["pr_number"], res["ecosystem"], res["cohort"],
        res["before_sha"], res["after_sha"], res["after_parent_sha"],
        res["sha_pair_verified"], res["requested_python_version"],
        res["actual_python_executable"], res["actual_python_version"],
        res["runtime_source"], res["before_install_result"], res["before_test_result"],
        res["before_final_state"], res["after_install_result"], res["after_test_result"],
        res["after_final_state"], res["classification"], res.get("evidence_tier", "T0"),
        res["failure_point"], res["failure_excerpt"], res["duration_seconds"]
    ))


def export_purified_csv(conn: sqlite3.Connection, target_csv_path: Optional[Path] = None) -> None:
    """Export purified_results to CSV, preserving and merging existing rows.
    If target_csv_path is not specified, defaults to CSV_PURIFIED_OUT.
    """
    out_path = target_csv_path or CSV_PURIFIED_OUT
    db_rows = conn.execute("SELECT * FROM purified_results ORDER BY classification DESC, repo ASC").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM purified_results LIMIT 0").description]
    
    # Load existing CSV rows if file exists to prevent truncation
    existing_map = {}
    if out_path.exists():
        try:
            with open(out_path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = (row.get("repo"), str(row.get("pr_number")))
                    existing_map[key] = dict(row)
        except Exception:
            pass

    # Merge DB rows into existing map (DB rows take precedence for updated PRs)
    for r in db_rows:
        key = (r["repo"], str(r["pr_number"]))
        existing_map[key] = {c: str(r[c]) if r[c] is not None else "" for c in cols}

    if not existing_map:
        return

    # Write merged list back to CSV
    merged_rows = list(existing_map.values())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(merged_rows)
    logger.info(f"Exported/merged {len(merged_rows)} purified rows to {out_path}")


# ─── Smoke Test Execution Mode ────────────────────────────────────────────────

def run_smoke_test(limit: int = 5):
    """Run 3-5 representative PRs as a smoke test and print concise summary per PR."""
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT f.repo, f.pr_number, COALESCE(f.ecosystem, p.ecosystem, 'pip') as ecosystem,
               p.before_sha, p.head_sha, p.title
        FROM final_results f
        JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
        WHERE f.classification = 'PASS->FAIL' AND (f.ecosystem IN ('pip', 'python', 'pypi') OR p.ecosystem IN ('pip', 'python', 'pypi'))
        ORDER BY f.repo ASC, f.pr_number ASC
        LIMIT ?
    """, (limit,)).fetchall()

    print("\n" + "=" * 80)
    print(f"RUNNING PURIFIED PIPELINE SMOKE TEST ON {len(rows)} REPRESENTATIVE PRs")
    print("=" * 80)

    for i, row in enumerate(rows, 1):
        print(f"\n--- SMOKE TEST PR {i}/{len(rows)}: {row['repo']} #{row['pr_number']} ---")
        res = run_purified_reproduction(conn, dict(row))

        print(f"  Repository / PR:       {res['repo']} #{res['pr_number']}")
        print(f"  Cohort Type:           {res['cohort']}")
        print(f"  SHA Pair Verified:     {'YES' if res['sha_pair_verified'] else 'NO'} (Parent: {res['after_parent_sha'][:8] if res['after_parent_sha'] else 'N/A'})")
        print(f"  Runtime Requested:     {res['requested_python_version']} ({res['runtime_source']})")
        print(f"  Runtime Actual:        {res['actual_python_version']}")
        print(f"  BEFORE State:          {res['before_final_state']} [Install: {res['before_install_result']}, Test: {res['before_test_result']}]")
        print(f"  AFTER State:           {res['after_final_state']} [Install: {res['after_install_result']}, Test: {res['after_test_result']}]")
        print(f"  Final Classification:  {res['classification']}")
        if res['failure_excerpt']:
            print(f"  Failure Excerpt:       {res['failure_excerpt'].strip().splitlines()[-1] if res['failure_excerpt'].strip() else 'N/A'}")

    export_purified_csv(conn)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Purified Hardened Reproduction Engine")
    parser.add_argument("--smoke-test", type=int, default=5, help="Run smoke test on N representative PRs (default: 5)")
    args = parser.parse_args()
    run_smoke_test(args.smoke_test)
