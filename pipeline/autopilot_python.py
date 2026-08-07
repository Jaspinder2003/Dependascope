"""
autopilot_python.py — Fully automated Python (pip / PyPI) Dependabot pipeline
═════════════════════════════════════════════════════════════════════════════
Loops continuously:
  1. Fetch fresh Python Dependabot PRs from GitHub API (stage2_fetch_live_github.py --ecosystem pip)
  2. Probe them for Python installability (smart_run_python.py probe)
  3. Run full BEFORE/AFTER on all Python PROBED_OK repos (smart_run_python.py run)
  4. Print Python status summary
  5. Repeat

Usage:
  python autopilot_python.py                      # runs until Ctrl+C
  python autopilot_python.py --hours 8            # stops after 8 hours
  python autopilot_python.py --max-cycles 20      # stops after 20 cycles

Safe to Ctrl+C at any time — fully resumable, just restart.
"""
import subprocess
import sys
import time
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] autopilot_python - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("autopilot_python")

PIPELINE_DIR = Path(__file__).parent
PYTHON = sys.executable


def run_step(description, cmd, timeout_minutes=60):
    """Run a subprocess step with timeout."""
    logger.info(f">>> {description}")
    logger.info(f"    Command: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(PIPELINE_DIR),
            timeout=timeout_minutes * 60,
            capture_output=False,  # let output flow to terminal
        )
        if result.returncode == 0:
            logger.info(f"<<< {description} completed successfully")
        else:
            logger.warning(f"<<< {description} exited with code {result.returncode}")
        return result.returncode
    except subprocess.TimeoutExpired:
        logger.warning(f"<<< {description} timed out after {timeout_minutes} minutes")
        return -1
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.error(f"<<< {description} failed: {e}")
        return -1


def print_status():
    """Print current Python pipeline status."""
    try:
        conn = sqlite3.connect(str(C.DB_PATH))
        conn.row_factory = sqlite3.Row

        total = conn.execute("SELECT COUNT(*) FROM pull_requests WHERE source_dataset='live_github_api' AND ecosystem IN ('pip', 'python', 'pypi')").fetchone()[0]
        probed_ok = conn.execute("SELECT COUNT(*) FROM pull_requests WHERE processing_status='PROBED_OK' AND ecosystem IN ('pip', 'python', 'pypi')").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM pull_requests WHERE processing_status='DONE' AND ecosystem IN ('pip', 'python', 'pypi')").fetchone()[0]
        eligible = conn.execute("""
            SELECT COUNT(*) FROM pull_requests
            WHERE before_sha IS NOT NULL AND head_sha IS NOT NULL
            AND processing_status NOT IN ('DONE','PROBED_OK','PROBED_FAIL')
            AND strict_or_complex = 'STRICT'
            AND source_dataset = 'live_github_api'
            AND ecosystem IN ('pip', 'python', 'pypi')
        """).fetchone()[0]

        results = conn.execute("""
            SELECT f.classification, COUNT(*) as c
            FROM final_results f
            JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
            WHERE p.source_dataset = 'live_github_api' AND p.ecosystem IN ('pip', 'python', 'pypi')
            GROUP BY f.classification
        """).fetchall()
        res = {r['classification']: r['c'] for r in results}

        pf = res.get('PASS->FAIL', 0)
        pp = res.get('PASS->PASS', 0)
        ff = res.get('FAIL->FAIL', 0)

        conn.close()

        logger.info("=" * 60)
        logger.info("PYTHON PIPELINE STATUS SUMMARY")
        logger.info(f"  Total Python PRs ingested:  {total}")
        logger.info(f"  Eligible for probing:       {eligible}")
        logger.info(f"  Queued (PROBED_OK):          {probed_ok}")
        logger.info(f"  Fully executed (DONE):       {done}")
        logger.info(f"  ---")
        logger.info(f"  PASS->FAIL (confirmed):      {pf}")
        logger.info(f"  PASS->PASS (healthy):        {pp}")
        logger.info(f"  FAIL->FAIL (pre-existing):   {ff}")
        if (pf + pp + ff) > 0:
            logger.info(f"  Python PASS->FAIL yield rate: {pf/(pf+pp+ff)*100:.1f}%")
        logger.info("=" * 60)
    except Exception as e:
        logger.warning(f"Could not print status: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fully automated Python Dependabot reproduction pipeline")
    parser.add_argument("--hours", type=float, default=0, help="Stop after this many hours (0 = run forever)")
    parser.add_argument("--max-cycles", type=int, default=0, help="Stop after this many fetch/probe/run cycles (0 = unlimited)")
    parser.add_argument("--fetch-limit", type=int, default=200, help="Python PRs to fetch from GitHub per cycle (default: 200)")
    parser.add_argument("--probe-target", type=int, default=50, help="Installable Python repos to find per probe cycle (default: 50)")
    parser.add_argument("--run-batch", type=int, default=50, help="Batch size for full runs (default: 50)")
    parser.add_argument("--target", type=int, default=0, help="Shortcut to set both --probe-target and --run-batch to the same number")
    parser.add_argument("--search-term", type=str, default="", help="Specific search keyword to partition multi-machine runs (e.g., 'pyproject.toml' vs 'requirements.txt')")
    args = parser.parse_args()

    if args.target > 0:
        args.probe_target = args.target
        args.run_batch = args.target

    start_time = datetime.now()
    deadline = start_time + timedelta(hours=args.hours) if args.hours > 0 else None
    cycle = 0

    logger.info("=" * 60)
    logger.info("PYTHON AUTOPILOT STARTED")
    logger.info(f"  Start time:    {start_time.strftime('%Y-%m-%d %H:%M')}")
    if deadline:
        logger.info(f"  Will stop at:  {deadline.strftime('%Y-%m-%d %H:%M')} ({args.hours}h)")
    else:
        logger.info(f"  Will run until Ctrl+C")
    logger.info(f"  Fetch limit:   {args.fetch_limit} Python PRs/cycle")
    logger.info(f"  Probe target:  {args.probe_target} installable/cycle")
    if args.search_term:
        logger.info(f"  Search filter: '{args.search_term}'")
    logger.info("=" * 60)

    try:
        while True:
            if deadline and datetime.now() >= deadline:
                logger.info("Time limit reached. Stopping Python autopilot.")
                break

            if args.max_cycles > 0 and cycle >= args.max_cycles:
                logger.info(f"Cycle limit ({args.max_cycles}) reached. Stopping Python autopilot.")
                break

            cycle += 1
            elapsed = datetime.now() - start_time
            elapsed_h = elapsed.total_seconds() / 3600
            logger.info(f"\n{'#'*60}")
            logger.info(f"### PYTHON CYCLE {cycle} (elapsed: {elapsed_h:.1f}h) ###")
            logger.info(f"{'#'*60}\n")

            # Step 1: Fetch fresh Python PRs from GitHub API
            fetch_cmd = [PYTHON, "stage2_fetch_live_github.py", "--ecosystem", "pip", "--limit", str(args.fetch_limit)]
            if args.search_term:
                fetch_cmd.extend(["--query", f"is:pr author:app/dependabot is:closed is:unmerged status:failure {args.search_term}"])

            run_step(
                f"STEP 1: Fetch {args.fetch_limit} fresh Python PRs from GitHub",
                fetch_cmd,
                timeout_minutes=15
            )

            # Step 2: Probe for installable Python repos
            run_step(
                f"STEP 2: Probe for {args.probe_target} installable Python repos",
                [PYTHON, "smart_run_python.py", "probe", "--target", str(args.probe_target)],
                timeout_minutes=45
            )

            # Step 3: Run full BEFORE/AFTER on all Python PROBED_OK repos
            run_step(
                "STEP 3: Run full BEFORE/AFTER reproduction on queued Python repos",
                [PYTHON, "smart_run_python.py", "run", "--batch", str(args.run_batch)],
                timeout_minutes=120
            )

            # Step 4: Print status
            print_status()

            logger.info("Pausing 10s before next cycle...")
            time.sleep(10)

    except KeyboardInterrupt:
        logger.info("\n[Ctrl+C] Python Autopilot stopped by user.")

    elapsed = datetime.now() - start_time
    logger.info(f"\nPython Autopilot ran for {elapsed.total_seconds()/3600:.1f} hours across {cycle} cycles.")
    print_status()


if __name__ == "__main__":
    main()
