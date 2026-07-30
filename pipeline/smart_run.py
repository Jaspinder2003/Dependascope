"""
smart_run.py — Resumable two-phase Dependabot reproduction runner
═════════════════════════════════════════════════════════════════════
Supports two independent modes that can run in parallel terminals:

  Terminal 1:  python smart_run.py probe --target 1000
               (Phase 1: probes candidate PRs and marks them PROBED_OK)

  Terminal 2:  python smart_run.py run
               (Phase 2: picks up PROBED_OK repos and runs full BEFORE/AFTER)

Both are fully resumable:
  - `probe` skips anything already PROBED_OK / PROBED_FAIL / DONE
  - `run` only picks PROBED_OK repos and marks them DONE after execution
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
logger = logging.getLogger("smart_run")


# ─────────────────────────── Probe helper ───────────────────────────

def quick_install_test(repo: str, head_sha: str, before_sha: str, pr_number: int, timeout: int = 180) -> tuple[bool, str]:
    """
    Ensure repo is cloned, check out a SHA and try install. Returns (success, reason).
    """
    # First clone/fetch commits if not cached
    ok, err = gf.clone_or_update(repo, head_sha, before_sha, pr_number)
    if not ok:
        return False, f"git_fetch_failed: {err}"

    work_dir = Path(tempfile.mkdtemp(prefix=f"depbot_probe_{repo.replace('/', '_')}_"))
    try:
        ok, err = gf.checkout_snapshot_worktree(repo, before_sha, work_dir)
        if not ok:
            return False, f"checkout_failed: {err}"

        # Detect ecosystem from files
        plan = ea.get_execution_plan("unknown", work_dir)
        if not plan or not plan.stages:
            return False, "no_execution_plan"

        # Allow all supported ecosystems on this system
        supported = {"npm", "pip", "maven", "gradle", "cargo", "go"}
        if plan.ecosystem not in supported:
            return False, f"unsupported_ecosystem: {plan.ecosystem}"

        # Run just the install stage
        install_stage = plan.stages[0]
        env = sx._safe_env()

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
            proc.communicate()
            return False, "install_timeout"
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except:
            pass


# ──────────────────────── Full reproduce helper ─────────────────────

def full_reproduce(conn, row, attempt=1):
    """Run the full BEFORE/AFTER comparison for a single PR."""
    repo = row["repo"]
    pr_number = row["pr_number"]
    ecosystem = row["ecosystem"] or "unknown"
    before_sha = row["before_sha"]
    head_sha = row["head_sha"]

    results = {"before_result": None, "after_result": None}

    gf.clone_or_update(repo, head_sha, before_sha, pr_number)

    for snapshot, sha in [("BEFORE", before_sha), ("AFTER", head_sha)]:
        work_dir = Path(tempfile.mkdtemp(prefix=f"depbot_{repo.replace('/', '_')}_{pr_number}_{snapshot}_"))
        try:
            ok, err = gf.checkout_snapshot_worktree(repo, sha, work_dir)
            if not ok:
                results[f"{snapshot.lower()}_result"] = "UNRUNNABLE"
                continue

            plan = ea.get_execution_plan(ecosystem, work_dir)
            if not plan or not plan.stages:
                results[f"{snapshot.lower()}_result"] = "UNRUNNABLE"
                continue

            log_dir = C.EXEC_LOG_DIR / f"{repo.replace('/', '__')}__pr{pr_number}"
            first_failure, stage_results = sx.run_plan(
                plan, work_dir, log_dir, snapshot, repo, pr_number, attempt
            )
            results[f"{snapshot.lower()}_result"] = first_failure

            # Save individual executions
            for sr in stage_results:
                conn.execute("""
                    INSERT OR REPLACE INTO executions
                    (repo, pr_number, snapshot, stage, command, exit_code,
                     duration_seconds, result, stdout_path, stderr_path, attempt_number)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (repo, pr_number, snapshot, sr["stage"], sr["command"],
                      sr["exit_code"], sr["duration_seconds"], sr["result"],
                      sr["stdout_path"], sr["stderr_path"], attempt))
        finally:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except:
                pass

    before_r = results["before_result"] or "UNRUNNABLE"
    after_r = results["after_result"] or "UNRUNNABLE"

    if before_r == "PASS" and after_r != "PASS":
        classification = "PASS->FAIL"
    elif before_r == "PASS" and after_r == "PASS":
        classification = "PASS->PASS"
    elif before_r == "UNRUNNABLE" or after_r == "UNRUNNABLE":
        classification = "UNRUNNABLE"
    else:
        classification = "FAIL->FAIL"

    conn.execute("""
        INSERT OR REPLACE INTO final_results
        (repo, pr_number, ecosystem, before_result, after_result, classification)
        VALUES (?,?,?,?,?,?)
    """, (repo, pr_number, ecosystem, before_r, after_r, classification))
    conn.execute("""
        UPDATE pull_requests SET processing_status='DONE'
        WHERE repo=? AND pr_number=?
    """, (repo, pr_number))
    conn.commit()

    return classification


# ═══════════════════════════ PROBE command ═══════════════════════════

def cmd_probe(args):
    """Phase 1: Probe candidate PRs for installability. Marks them PROBED_OK or PROBED_FAIL."""
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Count how many are already PROBED_OK (not yet run)
    already_ok = conn.execute(
        "SELECT COUNT(*) FROM pull_requests WHERE processing_status = 'PROBED_OK'"
    ).fetchone()[0]
    logger.info(f"Already have {already_ok} PROBED_OK repos queued for full run.")

    rows = conn.execute("""
        SELECT repo, pr_number, ecosystem, before_sha, head_sha, created_at
        FROM pull_requests
        WHERE before_sha IS NOT NULL AND head_sha IS NOT NULL
        AND processing_status NOT IN ('DONE', 'PROBED_OK', 'PROBED_FAIL')
        AND strict_or_complex = 'STRICT'
        AND source_dataset = 'live_github_api'
        ORDER BY created_at DESC
        LIMIT ?
    """, (args.limit,)).fetchall()

    logger.info(f"=== PROBE: {len(rows)} candidate PRs to scan (target: {args.target} new installable) ===")

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

            logger.info(f"  [{probe_count}] Probing {repo} #{pr_number} @ {before_sha[:10]}...")
            ok, reason = quick_install_test(repo, head_sha, before_sha, pr_number)

            if ok:
                ok_count += 1
                logger.info(f"    >> INSTALL OK -- queued for full run ({ok_count}/{args.target})")
                conn.execute("UPDATE pull_requests SET processing_status='PROBED_OK' WHERE repo=? AND pr_number=?", (repo, pr_number))
                conn.commit()
            else:
                skip_count += 1
                logger.info(f"    x  Skip: {reason}")
                conn.execute("UPDATE pull_requests SET processing_status='PROBED_FAIL' WHERE repo=? AND pr_number=?", (repo, pr_number))
                conn.commit()

            if ok_count >= args.target:
                logger.info(f"Reached target of {args.target} new installable repos!")
                break

            if probe_count >= args.max_probes:
                logger.info(f"Reached maximum probe limit ({args.max_probes})")
                break

    except KeyboardInterrupt:
        logger.info(f"\n[Ctrl+C] Probing stopped. {ok_count} new repos queued so far.")

    total_queued = conn.execute(
        "SELECT COUNT(*) FROM pull_requests WHERE processing_status = 'PROBED_OK'"
    ).fetchone()[0]
    logger.info(f"\n=== Probe done: {ok_count} new installable / {probe_count} probed / {skip_count} skipped ===")
    logger.info(f"=== Total PROBED_OK repos waiting for full run: {total_queued} ===\n")
    logger.info("Now run in another terminal:  python smart_run.py run")


# ════════════════════════════ RUN command ════════════════════════════

def cmd_run(args):
    """Phase 2: Pick PROBED_OK repos from DB and run full BEFORE/AFTER. Marks DONE when finished."""
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Continuously pick up PROBED_OK repos until none remain (or Ctrl+C)
    stats = {"PASS->PASS": 0, "PASS->FAIL": 0, "FAIL->FAIL": 0, "UNRUNNABLE": 0}
    total_run = 0

    try:
        while True:
            # Fetch a batch of PROBED_OK repos to process
            batch = conn.execute("""
                SELECT repo, pr_number, ecosystem, before_sha, head_sha, created_at
                FROM pull_requests
                WHERE processing_status = 'PROBED_OK'
                AND before_sha IS NOT NULL AND head_sha IS NOT NULL
                ORDER BY created_at DESC
                LIMIT ?
            """, (args.batch,)).fetchall()

            if not batch:
                if args.wait:
                    logger.info("No PROBED_OK repos available. Waiting 30s for probing to queue more...")
                    time.sleep(30)
                    continue
                else:
                    logger.info("No more PROBED_OK repos to process.")
                    break

            for row in batch:
                repo = row["repo"]
                pr_num = row["pr_number"]
                total_run += 1
                logger.info(f"[{total_run}] FULL RUN: {repo}#{pr_num}")

                try:
                    classification = full_reproduce(conn, row)
                except Exception as e:
                    logger.error(f"  ERROR on {repo}#{pr_num}: {e}")
                    # Mark as DONE so we don't retry forever
                    conn.execute("UPDATE pull_requests SET processing_status='DONE' WHERE repo=? AND pr_number=?", (repo, pr_num))
                    conn.commit()
                    classification = "UNRUNNABLE"

                stats[classification] = stats.get(classification, 0) + 1
                logger.info(f"  => {classification}")
                logger.info(f"  Running totals: {stats}")

    except KeyboardInterrupt:
        logger.info(f"\n[Ctrl+C] Execution stopped after {total_run} repos.")

    logger.info(f"\n{'='*60}")
    logger.info(f"=== FINAL RESULTS ({total_run} repos executed) ===")
    for k, v in sorted(stats.items()):
        logger.info(f"  {k}: {v}")
    logger.info(f"{'='*60}")


# ═══════════════════════════ STATUS command ══════════════════════════

def cmd_status(args):
    """Print current pipeline status."""
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=== PIPELINE STATUS ===\n")

    # Processing status breakdown
    rows = conn.execute("SELECT processing_status, COUNT(*) as c FROM pull_requests GROUP BY processing_status ORDER BY c DESC").fetchall()
    print("Pull Request Processing Status:")
    for r in rows:
        print(f"  {r['processing_status']}: {r['c']}")

    # Classification breakdown
    print("\nFinal Results Classification:")
    rows2 = conn.execute("SELECT classification, COUNT(*) as c FROM final_results GROUP BY classification ORDER BY c DESC").fetchall()
    for r in rows2:
        print(f"  {r['classification']}: {r['c']}")

    # PASS->FAIL details
    pf = conn.execute("SELECT repo, pr_number, ecosystem FROM final_results WHERE classification='PASS->FAIL'").fetchall()
    if pf:
        print(f"\nConfirmed PASS->FAIL Cases ({len(pf)}):")
        for r in pf:
            print(f"  {r['repo']} #{r['pr_number']} [{r['ecosystem']}]")

    conn.close()


# ═══════════════════════════ CLI ENTRYPOINT ══════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Dependabot reproduction pipeline (resumable, parallel-safe)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Terminal 1:  python smart_run.py probe --target 500
  Terminal 2:  python smart_run.py run --wait
  Any time:    python smart_run.py status
        """
    )
    subparsers = parser.add_subparsers(dest="command")

    # -- probe --
    p_probe = subparsers.add_parser("probe", help="Phase 1: Probe PRs for installability")
    p_probe.add_argument("--target", type=int, default=100, help="How many new installable repos to find (default: 100)")
    p_probe.add_argument("--max-probes", type=int, default=2000, help="Maximum PRs to attempt probing (default: 2000)")
    p_probe.add_argument("--limit", type=int, default=5000, help="SQL query LIMIT for candidate PRs (default: 5000)")

    # -- run --
    p_run = subparsers.add_parser("run", help="Phase 2: Run full BEFORE/AFTER on PROBED_OK repos")
    p_run.add_argument("--batch", type=int, default=50, help="Batch size to fetch from DB at once (default: 50)")
    p_run.add_argument("--wait", action="store_true", help="Keep waiting for new PROBED_OK repos instead of exiting when queue is empty")

    # -- status --
    p_status = subparsers.add_parser("status", help="Show current pipeline status")

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

