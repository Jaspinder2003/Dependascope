"""
smart_run_python.py — Resumable two-phase Dependabot reproduction runner for Python (pip / PyPI)
════════════════════════════════════════════════════════════════════════════════════════════
Supports two independent modes that can run in parallel terminals:

  Terminal 1:  python smart_run_python.py probe --target 500
               (Phase 1: probes Python candidate PRs and marks them PROBED_OK)

  Terminal 2:  python smart_run_python.py run
               (Phase 2: picks up Python PROBED_OK repos and runs full BEFORE/AFTER)

Both are fully resumable:
  - `probe` skips anything already PROBED_OK / PROBED_FAIL / DONE
  - `run` only picks Python PROBED_OK repos and marks them DONE after execution
  - Ctrl+C is safe at any time; just restart the same command
"""
import sqlite3
import subprocess
import tempfile
import shutil
import os
import sys
import time
import json
import logging
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import git_fetcher as gf
import ecosystem_adapters as ea
import sandbox_executor as sx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("smart_run_python")


def db_write(conn, sql, params=(), retries=5, base_delay=1.0):
    """Execute a write with retry-on-lock to handle concurrent access."""
    try:
        db.safe_execute_write(conn, sql, params, retries=retries, base_delay=base_delay)
        conn.commit()
    except Exception as e:
        logger.warning(f"  DB write warning in smart_run: {e}")


# ─────────────────────────── Probe helper ───────────────────────────

import purified_reproduce as pr

def quick_install_test(repo: str, head_sha: str, before_sha: str, pr_number: int, timeout: int = 180) -> tuple[bool, str]:
    """
    Strict Probing Helper:
      1. Causal SHA pairing check (head parent == before_sha)
      2. Exact Python runtime availability (no fallback)
      3. Isolated per-snapshot venv installation
    """
    ok, err = gf.clone_or_update(repo, head_sha, before_sha, pr_number)
    if not ok:
        return False, f"git_fetch_failed: {err}"

    # Check 1: Causal SHA Pairing Verification
    sha_ok, parent_sha, sha_msg = gf.verify_sha_pairing(repo, head_sha, before_sha)
    if not sha_ok:
        return False, f"sha_unverified: {sha_msg}"

    work_dir = Path(tempfile.mkdtemp(prefix=f"depbot_pyprobe_{repo.replace('/', '_')}_"))
    venv_dir = work_dir / "_venv"

    try:
        ok, err = gf.checkout_snapshot_worktree(repo, before_sha, work_dir)
        if not ok:
            return False, f"checkout_failed: {err}"

        # Check 2: Detect execution plan and requested Python version
        plan = ea.get_execution_plan("pip", work_dir)
        if not plan or not plan.stages:
            return False, "no_execution_plan"

        python_ecosystems = {"pip", "python", "pypi"}
        if plan.ecosystem not in python_ecosystems:
            return False, f"non_python_ecosystem: {plan.ecosystem}"

        req_py_version = plan.runtime_version
        py_bin, act_py_ver, py_status = pr.resolve_python_runtime(req_py_version)
        if not py_bin:
            return False, f"missing_python_version_{req_py_version}"

        # Check 3: Create fresh isolated per-snapshot venv
        try:
            v_proc = subprocess.run([py_bin, "-m", "venv", str(venv_dir)], capture_output=True, text=True, timeout=60)
            if v_proc.returncode != 0:
                return False, f"venv_creation_failed: {v_proc.stderr[:100]}"
        except Exception as e:
            return False, f"venv_creation_exception: {e}"

        # Run install stage inside fresh venv
        install_stage = plan.stages[0]
        env = sx._safe_env(venv_dir=venv_dir)

        try:
            proc = subprocess.Popen(
                install_stage.command,
                shell=True, cwd=str(work_dir),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )
            stdout, stderr = proc.communicate(timeout=timeout)
            if proc.returncode == 0:
                return True, "install_ok"
            else:
                err_text = stderr.decode('utf-8', errors='replace')[-500:]
                return False, f"install_fail: {err_text[:200]}"
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return False, "install_timeout"
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


# ─────────────────────────── Full reproduce helper ───────────────────────────

def full_reproduce(conn, row, attempt=1):
    """Run full purified 12-point reproduction for a single Python PR."""
    purified_res = pr.run_purified_reproduction(conn, row)
    classification = purified_res["classification"]

    return classification


# ─────────────────────────── PROBE command ───────────────────────────

def cmd_probe(args):
    """Phase 1: Probe candidate Python PRs for installability. Marks them PROBED_OK or PROBED_FAIL."""
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    already_ok = conn.execute(
        "SELECT COUNT(*) FROM pull_requests WHERE processing_status = 'PROBED_OK' AND (ecosystem IN ('pip', 'python', 'pypi') OR ecosystem IS NULL)"
    ).fetchone()[0]
    logger.info(f"Already have {already_ok} Python PROBED_OK repos queued for full run.")

    rows = conn.execute("""
        SELECT repo, pr_number, ecosystem, before_sha, head_sha, created_at
        FROM pull_requests
        WHERE before_sha IS NOT NULL AND head_sha IS NOT NULL
        AND processing_status NOT IN ('DONE', 'PROBED_OK', 'PROBED_FAIL')
        AND strict_or_complex = 'STRICT'
        AND source_dataset = 'live_github_api'
        AND (ecosystem IN ('pip', 'python', 'pypi') OR ecosystem IS NULL OR ecosystem = 'unknown')
        ORDER BY created_at DESC
        LIMIT ?
    """, (args.limit,)).fetchall()

    logger.info(f"=== PYTHON PROBE: {len(rows)} candidate PRs to scan (target: {args.target} new installable) ===")

    probed_set = set()
    probe_count = 0
    ok_count = 0
    skip_count = 0

    try:
        for row in rows:
            repo = row["repo"]
            before_sha = row["before_sha"]
            head_sha = row["head_sha"]
            pr_number = row["pr_number"]

            key = (repo, before_sha)
            if key in probed_set:
                continue
            probed_set.add(key)
            probe_count += 1

            logger.info(f"  [{probe_count}] Probing Python repo {repo} #{pr_number} @ {before_sha[:10]}...")
            ok, reason = quick_install_test(repo, head_sha, before_sha, pr_number)

            if ok:
                ok_count += 1
                logger.info(f"    >> PYTHON INSTALL OK -- queued for full run ({ok_count}/{args.target})")
                db_write(conn, "UPDATE pull_requests SET processing_status='PROBED_OK', ecosystem='pip' WHERE repo=? AND pr_number=?", (repo, pr_number))
            else:
                skip_count += 1
                logger.info(f"    x  Skip: {reason}")
                db_write(conn, "UPDATE pull_requests SET processing_status='PROBED_FAIL' WHERE repo=? AND pr_number=?", (repo, pr_number))

            if ok_count >= args.target:
                logger.info(f"Reached target of {args.target} new installable Python repos!")
                break

            if probe_count >= args.max_probes:
                logger.info(f"Reached maximum probe limit ({args.max_probes})")
                break

    except KeyboardInterrupt:
        logger.info(f"\n[Ctrl+C] Python Probing stopped. {ok_count} new Python repos queued so far.")

    total_queued = conn.execute(
        "SELECT COUNT(*) FROM pull_requests WHERE processing_status = 'PROBED_OK' AND ecosystem IN ('pip', 'python', 'pypi')"
    ).fetchone()[0]
    logger.info(f"\n=== Python Probe done: {ok_count} new installable / {probe_count} probed / {skip_count} skipped ===")
    logger.info(f"=== Total Python PROBED_OK repos waiting for full run: {total_queued} ===\n")
    logger.info("Now run in another terminal:  python smart_run_python.py run")


# ─────────────────────────── RUN command ───────────────────────────

def cmd_run(args):
    """Phase 2: Pick Python PROBED_OK repos from DB and run full BEFORE/AFTER. Marks DONE when finished."""
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    stats = {"PASS->PASS": 0, "PASS->FAIL": 0, "FAIL->FAIL": 0, "UNRUNNABLE": 0}
    total_run = 0

    try:
        while True:
            batch = conn.execute("""
                SELECT repo, pr_number, ecosystem, before_sha, head_sha, created_at
                FROM pull_requests
                WHERE processing_status = 'PROBED_OK'
                AND (ecosystem IN ('pip', 'python', 'pypi') OR ecosystem IS NULL)
                AND before_sha IS NOT NULL AND head_sha IS NOT NULL
                ORDER BY created_at DESC
                LIMIT ?
            """, (args.batch,)).fetchall()

            if not batch:
                if args.wait:
                    logger.info("No Python PROBED_OK repos available. Waiting 30s for probing to queue more...")
                    time.sleep(30)
                    continue
                else:
                    logger.info("No more Python PROBED_OK repos to process.")
                    break

            for row in batch:
                repo = row["repo"]
                pr_num = row["pr_number"]
                total_run += 1
                logger.info(f"[{total_run}] FULL PYTHON RUN: {repo}#{pr_num}")

                try:
                    classification = full_reproduce(conn, row)
                except Exception as e:
                    logger.error(f"  ERROR on {repo}#{pr_num}: {e}")
                    classification = "UNRUNNABLE"

                # Always mark DONE so this PR is never picked up again on restart.
                # Wrapped in its own try/except so a DB lock here can't crash the loop.
                try:
                    db_write(conn, "UPDATE pull_requests SET processing_status='DONE' WHERE repo=? AND pr_number=?", (repo, pr_num))
                except Exception as done_err:
                    logger.warning(f"  Could not mark DONE for {repo}#{pr_num}: {done_err} — will retry on next start")

                # Export new run result exclusively to python_new_runs_results.csv
                # Keeps python_rerun_results.csv and python_purified_results.csv completely untouched!
                NEW_RUNS_CSV_PATH = Path(__file__).parent.parent.parent / "python_new_runs_results.csv"
                pr.export_purified_csv(conn, target_csv_path=NEW_RUNS_CSV_PATH)

                stats[classification] = stats.get(classification, 0) + 1
                logger.info(f"  => {classification}")
                logger.info(f"  Running totals: {stats}")

    except KeyboardInterrupt:
        logger.info(f"\n[Ctrl+C] Python execution stopped after {total_run} repos.")

    logger.info(f"\n{'='*60}")
    logger.info(f"=== FINAL PYTHON RESULTS ({total_run} repos executed) ===")
    for k, v in sorted(stats.items()):
        logger.info(f"  {k}: {v}")
    logger.info(f"{'='*60}")


# ─────────────────────────── STATUS command ───────────────────────────

def cmd_status(args):
    """Print current Python pipeline status."""
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=== PYTHON PIPELINE STATUS ===\n")

    rows = conn.execute("""
        SELECT processing_status, COUNT(*) as c 
        FROM pull_requests 
        WHERE ecosystem IN ('pip', 'python', 'pypi')
        GROUP BY processing_status ORDER BY c DESC
    """).fetchall()
    print("Python Pull Request Processing Status:")
    for r in rows:
        print(f"  {r['processing_status']}: {r['c']}")

    print("\nPython Final Results Classification:")
    rows2 = conn.execute("""
        SELECT classification, COUNT(*) as c 
        FROM final_results 
        WHERE ecosystem IN ('pip', 'python', 'pypi')
        GROUP BY classification ORDER BY c DESC
    """).fetchall()
    for r in rows2:
        print(f"  {r['classification']}: {r['c']}")

    pf = conn.execute("SELECT repo, pr_number, ecosystem, duration_seconds FROM final_results WHERE classification='PASS->FAIL' AND ecosystem IN ('pip', 'python', 'pypi')").fetchall()
    if pf:
        print(f"\nConfirmed Python PASS->FAIL Cases ({len(pf)}):")
        for r in pf:
            dur = f"{r['duration_seconds']:.2f}s" if r['duration_seconds'] else "N/A"
            print(f"  {r['repo']} #{r['pr_number']} [{r['ecosystem']}] (Time: {dur})")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Python/pip Dependabot reproduction pipeline (resumable, parallel-safe)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Terminal 1:  python smart_run_python.py probe --target 500
  Terminal 2:  python smart_run_python.py run --wait
  Any time:    python smart_run_python.py status
        """
    )
    subparsers = parser.add_subparsers(dest="command")

    # -- probe --
    p_probe = subparsers.add_parser("probe", help="Phase 1: Probe Python PRs for installability")
    p_probe.add_argument("--target", type=int, default=100, help="How many new installable Python repos to find (default: 100)")
    p_probe.add_argument("--max-probes", type=int, default=2000, help="Maximum PRs to attempt probing (default: 2000)")
    p_probe.add_argument("--limit", type=int, default=5000, help="SQL query LIMIT for candidate PRs (default: 5000)")

    # -- run --
    p_run = subparsers.add_parser("run", help="Phase 2: Run full BEFORE/AFTER on Python PROBED_OK repos")
    p_run.add_argument("--batch", type=int, default=50, help="Batch size to fetch from DB at once (default: 50)")
    p_run.add_argument("--wait", action="store_true", help="Keep waiting for new Python PROBED_OK repos")

    # -- status --
    p_status = subparsers.add_parser("status", help="Show current Python pipeline status")

    args = parser.parse_args()

    if args.command == "probe":
        cmd_probe(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
