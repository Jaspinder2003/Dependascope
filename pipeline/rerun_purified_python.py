"""
rerun_purified_python.py — Re-runs the Python PASS->FAIL candidate PRs under the 12-point purified runner.

DEDICATED RERUN FILE: Outputs EXCLUSIVELY to `python_rerun_results.csv`.
Does NOT touch `python_purified_results.csv` (the 542-row master dataset).

APPEND-ONLY & RESUME-SAFE:
  - Reads `python_rerun_results.csv` on launch to find already-completed rerun PRs.
  - Skips already-completed PRs automatically.
  - Appends new rerun results directly to `python_rerun_results.csv` after each PR finishes.
  - 100% immune to SQLite DB lock contention.
  - Safe to Ctrl+C and restart at any time.
"""
import csv
import logging
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import purified_reproduce as pr

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s – %(message)s")
logger = logging.getLogger("rerun_purified")

# DEDICATED RERUN OUTPUT FILE — keeps master python_purified_results.csv untouched!
RERUN_CSV_PATH = Path(__file__).parent.parent.parent / "python_rerun_results.csv"

CSV_HEADERS = [
    "repo", "pr_number", "ecosystem", "cohort", "before_sha", "after_sha",
    "after_parent_sha", "sha_pair_verified", "requested_python_version",
    "actual_python_executable", "actual_python_version", "runtime_source",
    "before_install_result", "before_test_result", "before_final_state",
    "after_install_result", "after_test_result", "after_final_state",
    "classification", "evidence_tier", "failure_point", "failure_excerpt",
    "duration_seconds"
]


def _load_already_done() -> set:
    """Read python_rerun_results.csv and return set of (repo, pr_number) already re-run."""
    done = set()
    if not RERUN_CSV_PATH.exists():
        return done
    try:
        with open(RERUN_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    done.add((row["repo"], int(row["pr_number"])))
                except (KeyError, ValueError):
                    pass
    except Exception as e:
        logger.warning(f"Could not read existing rerun CSV: {e}")
    return done


def _append_to_csv(res: dict) -> None:
    """Append a single purified result row directly to python_rerun_results.csv."""
    file_exists = RERUN_CSV_PATH.exists() and RERUN_CSV_PATH.stat().st_size > 0
    with open(RERUN_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(res)


def main():
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Fetch all Python PASS->FAIL candidate PRs (~74 candidates)
    rows = conn.execute("""
        SELECT f.repo, f.pr_number, COALESCE(f.ecosystem, p.ecosystem, 'pip') as ecosystem,
               p.before_sha, p.head_sha, p.title, p.created_at, p.source_dataset
        FROM final_results f
        JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
        WHERE f.classification = 'PASS->FAIL'
        AND (f.ecosystem IN ('pip', 'python', 'pypi') OR p.ecosystem IN ('pip', 'python', 'pypi'))
        ORDER BY f.repo ASC, f.pr_number ASC
    """).fetchall()

    # Start strictly from candidate 47/74 (houcem58/realtime-streaming-pipeline #3)
    START_INDEX = 46  # 0-indexed (skips first 46 items)
    rows = rows[START_INDEX:]

    # Skip PRs already finished in python_rerun_results.csv
    already_done = _load_already_done()
    pending = [r for r in rows if (r["repo"], int(r["pr_number"])) not in already_done]

    total_candidates = len(rows)
    total_done = len(already_done)
    total_pending = len(pending)

    logger.info("=" * 75)
    logger.info("PURIFIED RE-RUN FOR PYTHON PASS->FAIL CANDIDATES")
    logger.info(f"  Total Candidate PRs                : {total_candidates}")
    logger.info(f"  Already Completed in Rerun CSV     : {total_done}")
    logger.info(f"  Remaining Pending                  : {total_pending}")
    logger.info(f"  Dedicated Rerun CSV Output         : {RERUN_CSV_PATH.name}")
    logger.info("=" * 75)

    if total_pending == 0:
        logger.info(f"All {total_candidates} candidate PRs are already processed in {RERUN_CSV_PATH.name}. Nothing to do.")
        conn.close()
        return

    stats = {"PASS->FAIL": 0, "PASS->PASS": 0, "FAIL->FAIL": 0,
             "INSTALLABILITY_REGRESSION": 0, "TEST_REGRESSION": 0, "UNRUNNABLE": 0}

    for i, row in enumerate(pending, 47):
        repo   = row["repo"]
        pr_num = row["pr_number"]
        logger.info(f"\n[{i}/74] Re-run Candidate: {repo} #{pr_num}")

        try:
            res = pr.run_purified_reproduction(conn, dict(row), save_to_db=False)
            classification = res["classification"]
        except Exception as e:
            logger.error(f"  ERROR on {repo}#{pr_num}: {e}")
            classification = "UNRUNNABLE"
            res = {
                "repo": repo, "pr_number": pr_num, "ecosystem": "pip", "cohort": "STRICT_SINGLE",
                "before_sha": row["before_sha"] or "", "after_sha": row["head_sha"] or "",
                "after_parent_sha": row["before_sha"] or "", "sha_pair_verified": 0,
                "requested_python_version": "3.x", "actual_python_executable": "NONE",
                "actual_python_version": "UNAVAILABLE", "runtime_source": "default",
                "before_install_result": "UNRUNNABLE", "before_test_result": "NONE",
                "before_final_state": "UNRUNNABLE", "after_install_result": "UNRUNNABLE",
                "after_test_result": "NONE", "after_final_state": "UNRUNNABLE",
                "classification": "UNRUNNABLE", "evidence_tier": "T0",
                "failure_point": "EXECUTION", "failure_excerpt": str(e),
                "duration_seconds": 0.0
            }

        stats[classification] = stats.get(classification, 0) + 1

        # Direct CSV append — immune to DB locks!
        _append_to_csv(res)
        logger.info(f"  => [{res.get('evidence_tier', 'T0')}] {classification} | Progress: {i}/{total_pending} | Stats so far: {stats}")

    logger.info("\n" + "=" * 75)
    logger.info("PURIFIED RE-RUN COMPLETE!")
    logger.info(f"  Dedicated Rerun CSV saved to : {RERUN_CSV_PATH}")
    logger.info(f"  Rerun Final Breakdown        : {stats}")
    logger.info("=" * 75)

    conn.close()


if __name__ == "__main__":
    main()
