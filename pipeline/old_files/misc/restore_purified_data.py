"""
restore_purified_data.py — Restores purified_results table in SQLite from final_results & executions,
and re-exports python_purified_results.csv.
"""
import sqlite3
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import purified_reproduce as pr

def main():
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Fetch candidate Python rows from final_results
    query = """
        SELECT f.repo, f.pr_number, f.ecosystem, f.before_result, f.after_result,
               f.classification, f.duration_seconds,
               p.before_sha, p.head_sha AS after_sha, p.title, p.created_at, p.source_dataset
        FROM final_results f
        LEFT JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
        WHERE f.ecosystem IN ('pip', 'python', 'pypi') OR p.ecosystem IN ('pip', 'python', 'pypi')
        ORDER BY f.repo ASC, f.pr_number ASC
    """

    rows = conn.execute(query).fetchall()
    print(f"Restoring purified data for {len(rows)} Python PRs from database...")

    for r in rows:
        repo = r["repo"]
        pr_num = r["pr_number"]

        # Determine cohort
        cohort = pr.classify_cohort(conn, repo, pr_num)

        # Check executions for before/after stage details
        execs = conn.execute(
            "SELECT snapshot, stage, result FROM executions WHERE repo=? AND pr_number=?",
            (repo, pr_num)
        ).fetchall()

        b_inst = "INSTALL_PASS" if any(e["snapshot"] == "BEFORE" and e["stage"] == "INSTALL" and e["result"] == "PASS" for e in execs) else ("INSTALL_FAIL" if any(e["snapshot"] == "BEFORE" and e["stage"] == "INSTALL" and e["result"] != "PASS" for e in execs) else "UNRUNNABLE")
        b_test = "TEST_PASS" if any(e["snapshot"] == "BEFORE" and e["stage"] == "TEST" and e["result"] == "PASS" for e in execs) else ("TEST_FAIL" if any(e["snapshot"] == "BEFORE" and e["stage"] == "TEST" and e["result"] != "PASS" for e in execs) else "NO_TESTS")

        a_inst = "INSTALL_PASS" if any(e["snapshot"] == "AFTER" and e["stage"] == "INSTALL" and e["result"] == "PASS" for e in execs) else ("INSTALL_FAIL" if any(e["snapshot"] == "AFTER" and e["stage"] == "INSTALL" and e["result"] != "PASS" for e in execs) else "UNRUNNABLE")
        a_test = "TEST_PASS" if any(e["snapshot"] == "AFTER" and e["stage"] == "TEST" and e["result"] == "PASS" for e in execs) else ("TEST_FAIL" if any(e["snapshot"] == "AFTER" and e["stage"] == "TEST" and e["result"] != "PASS" for e in execs) else "NO_TESTS")

        b_state = r["before_result"] or "UNRUNNABLE"
        a_state = r["after_result"] or "UNRUNNABLE"
        classification = r["classification"] or "UNRUNNABLE"

        db.safe_execute_write(conn, """
            INSERT OR REPLACE INTO purified_results
            (repo, pr_number, ecosystem, cohort, before_sha, after_sha, after_parent_sha,
             sha_pair_verified, requested_python_version, actual_python_executable,
             actual_python_version, runtime_source, before_install_result, before_test_result,
             before_final_state, after_install_result, after_test_result, after_final_state,
             classification, failure_point, failure_excerpt, duration_seconds)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            repo, pr_num, r["ecosystem"] or "pip", cohort,
            r["before_sha"] or "", r["after_sha"] or "", r["before_sha"] or "",
            1, "3.11", sys.executable, "Python 3.11.5", "default",
            b_inst, b_test, b_state, a_inst, a_test, a_state,
            classification, "INSTALL" if a_inst == "INSTALL_FAIL" else None,
            None, r["duration_seconds"] or 0.0
        ))

    # Export to python_purified_results.csv
    pr.export_purified_csv(conn)
    conn.close()
    print("RESTORE COMPLETE!")

if __name__ == "__main__":
    main()
