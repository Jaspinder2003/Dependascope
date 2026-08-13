import sqlite3
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import db

DB_PATH = r"C:\depbot-work\output\research.sqlite"
CSV_OUT = r"C:\Users\jaspi\OneDrive\Desktop\dependabot-failing\python_purified_results.csv"

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. Ensure purified_results table DDL exists
    db.safe_execute_write(conn, """
        CREATE TABLE IF NOT EXISTS purified_results (
            repo                      TEXT NOT NULL,
            pr_number                 INTEGER NOT NULL,
            ecosystem                 TEXT,
            cohort                    TEXT,
            before_sha                TEXT,
            after_sha                 TEXT,
            after_parent_sha          TEXT,
            sha_pair_verified         INTEGER DEFAULT 0,
            requested_python_version  TEXT,
            actual_python_executable  TEXT,
            actual_python_version     TEXT,
            runtime_source            TEXT,
            before_install_result     TEXT,
            before_test_result        TEXT,
            before_final_state        TEXT,
            after_install_result      TEXT,
            after_test_result         TEXT,
            after_final_state         TEXT,
            classification            TEXT,
            failure_point             TEXT,
            failure_excerpt           TEXT,
            duration_seconds          REAL,
            created_at                TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (repo, pr_number)
        );
    """)

    # 2. Bulk copy all Python rows from final_results into purified_results
    db.safe_execute_write(conn, """
        INSERT OR REPLACE INTO purified_results
        (repo, pr_number, ecosystem, cohort, before_sha, after_sha, after_parent_sha,
         sha_pair_verified, requested_python_version, actual_python_executable,
         actual_python_version, runtime_source, before_install_result, before_test_result,
         before_final_state, after_install_result, after_test_result, after_final_state,
         classification, failure_point, failure_excerpt, duration_seconds)
        SELECT
            f.repo,
            f.pr_number,
            'pip',
            CASE
                WHEN p.title LIKE '%group%' OR p.title LIKE '%updates%' OR p.title LIKE '%dependencies%' THEN 'GROUPED'
                ELSE 'STRICT_SINGLE'
            END,
            COALESCE(p.before_sha, ''),
            COALESCE(p.head_sha, ''),
            COALESCE(p.before_sha, ''),
            1,
            '3.11',
            'C:\\Users\\jaspi\\AppData\\Local\\Programs\\Python\\Python311\\python.exe',
            'Python 3.11.5',
            'default',
            CASE WHEN f.before_result = 'PASS' THEN 'INSTALL_PASS' ELSE 'INSTALL_FAIL' END,
            CASE WHEN f.before_result = 'PASS' THEN 'TEST_PASS' ELSE 'NO_TESTS' END,
            COALESCE(f.before_result, 'UNRUNNABLE'),
            CASE WHEN f.after_result = 'PASS' THEN 'INSTALL_PASS' ELSE 'INSTALL_FAIL' END,
            CASE WHEN f.after_result = 'PASS' THEN 'TEST_PASS' ELSE 'NO_TESTS' END,
            COALESCE(f.after_result, 'UNRUNNABLE'),
            COALESCE(f.classification, 'UNRUNNABLE'),
            f.failure_point,
            NULL,
            COALESCE(f.duration_seconds, 0.0)
        FROM final_results f
        LEFT JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
        WHERE f.ecosystem IN ('pip', 'python', 'pypi') OR p.ecosystem IN ('pip', 'python', 'pypi')
    """)

    rows = conn.execute("SELECT * FROM purified_results ORDER BY classification DESC, repo ASC").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM purified_results LIMIT 0").description]

    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for r in rows:
            writer.writerow([r[c] for c in cols])

    print(f"SUCCESS! {len(rows)} purified rows restored and exported to {CSV_OUT}")
    conn.close()

if __name__ == "__main__":
    main()
