"""
rerun_python_pass_fail.py — Re-runs all Python PASS->FAIL cases under the new isolated venv runner.
"""
import sqlite3
import subprocess
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rerun_pass_fail")

def main():
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Find all Python PASS->FAIL PRs
    rows = conn.execute("""
        SELECT f.repo, f.pr_number
        FROM final_results f
        JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
        WHERE f.classification = 'PASS->FAIL' AND p.ecosystem IN ('pip', 'python', 'pypi')
    """).fetchall()

    logger.info(f"Found {len(rows)} Python PASS->FAIL cases to re-verify under the new isolated venv runner.")

    # Reset processing_status to PROBED_OK so smart_run_python.py will process them
    for r in rows:
        conn.execute(
            "UPDATE pull_requests SET processing_status='PROBED_OK' WHERE repo=? AND pr_number=?",
            (r["repo"], r["pr_number"])
        )
    conn.commit()
    conn.close()

    logger.info("Reset processing_status to PROBED_OK. Triggering smart_run_python.py run...")

    # Execute smart_run_python.py run
    PYTHON = sys.executable
    cmd = [PYTHON, "smart_run_python.py", "run", "--batch", str(len(rows))]
    subprocess.run(cmd, cwd=str(Path(__file__).parent))

if __name__ == "__main__":
    main()
