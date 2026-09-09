"""
smart_run.py — Pre-filter approach (PREVIOUS VERSION — kept for reference)
────────────────────────────────────
Instead of running random PRs and hitting FAIL->FAIL,
this script:
  1. Picks npm-ecosystem PRs with SHAs
  2. Checks out only the BEFORE snapshot
  3. Tries `npm ci` or `npm install` quickly (60s timeout)
  4. If BEFORE installs -> runs the full BEFORE+AFTER comparison
  5. Skips immediately if BEFORE doesn't install

This avoids wasting time on dead repos.
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import git_fetcher as gf
import ecosystem_adapters as ea
import sandbox_executor as sx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s \u2013 %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("smart_run")

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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Smart pre-filter reproduction runner")
    parser.add_argument("--target", type=int, default=25, help="Number of installable repos to queue before starting Phase 2")
    parser.add_argument("--max-probes", type=int, default=100, help="Maximum repos to probe in Phase 1")
    parser.add_argument("--skip-probe", action="store_true", help="Skip Phase 1 probing and run Phase 2 directly on PROBED_OK repos")
    args = parser.parse_args()

    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. First check if we ALREADY have repos marked PROBED_OK from a previous run
    already_probed = conn.execute("""
        SELECT repo, pr_number, ecosystem, before_sha, head_sha, created_at
        FROM pull_requests
        WHERE processing_status = 'PROBED_OK'
        AND before_sha IS NOT NULL AND head_sha IS NOT NULL
        ORDER BY created_at DESC
        LIMIT ?
    """, (args.target,)).fetchall()

    installable = list(already_probed)
    if installable:
        logger.info(f"Found {len(installable)} previously probed installable repos in database!")

    probe_count = 0
    skip_count = 0

    # 2. If we need more installable repos and skip_probe is False, probe remaining candidate PRs
    if len(installable) < args.target and not args.skip_probe:
        rows = conn.execute("""
            SELECT repo, pr_number, ecosystem, before_sha, head_sha, created_at
            FROM pull_requests
            WHERE before_sha IS NOT NULL AND head_sha IS NOT NULL
            AND processing_status NOT IN ('DONE', 'PROBED_OK', 'PROBED_FAIL')
            AND strict_or_complex = 'STRICT'
            ORDER BY created_at DESC
            LIMIT 500
        """).fetchall()

        logger.info(f"=== Smart Run: {len(rows)} candidate PRs (Target: {args.target} installable repos) ===")

        probed = set()

        try:
            for row in rows:
                repo = row["repo"]
                before_sha = row["before_sha"]
                head_sha = row["head_sha"]
                pr_number = row["pr_number"]

                key = (repo, before_sha)
                if key in probed:
                    continue

                probed.add(key)
                probe_count += 1

                logger.info(f"  [{probe_count}] Probing {repo} #{pr_number} @ {before_sha[:10]}...")
                ok, reason = quick_install_test(repo, head_sha, before_sha, pr_number)

                if ok:
                    logger.info(f"    >> INSTALL OK -- queueing for full run ({len(installable)+1}/{args.target})")
                    conn.execute("UPDATE pull_requests SET processing_status='PROBED_OK' WHERE repo=? AND pr_number=?", (repo, pr_number))
                    conn.commit()
                    installable.append(row)
                else:
                    logger.info(f"    x  Skip: {reason}")
                    conn.execute("UPDATE pull_requests SET processing_status='PROBED_FAIL' WHERE repo=? AND pr_number=?", (repo, pr_number))
                    conn.commit()
                    skip_count += 1

                if len(installable) >= args.target:
                    logger.info(f"Reached target of {args.target} installable repos!")
                    break

                if probe_count >= args.max_probes:
                    logger.info(f"Reached maximum probe limit ({args.max_probes})")
                    break

        except KeyboardInterrupt:
            logger.info(f"\n[Ctrl+C] Probing stopped early by user! Transitioning to Phase 2 with {len(installable)} installable repos...")

    logger.info(f"\n=== Probe complete: {len(installable)} installable / {probe_count} probed / {skip_count} skipped ===\n")

    if not installable:
        logger.error("NO installable repos found in probed candidates.")
        return

    # Phase 2: Full BEFORE/AFTER comparison on installable repos only
    stats = {"PASS->PASS": 0, "PASS->FAIL": 0, "FAIL->FAIL": 0, "UNRUNNABLE": 0}

    for i, row in enumerate(installable):
        repo = row["repo"]
        pr_num = row["pr_number"]
        logger.info(f"[{i+1}/{len(installable)}] FULL RUN: {repo}#{pr_num}")

        classification = full_reproduce(conn, row)
        stats[classification] = stats.get(classification, 0) + 1

        logger.info(f"  => {classification}")
        logger.info(f"  Running totals: {stats}")

    logger.info(f"\n{'='*60}")
    logger.info(f"=== FINAL RESULTS ===")
    for k, v in sorted(stats.items()):
        logger.info(f"  {k}: {v}")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
