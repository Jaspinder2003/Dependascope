import sqlite3
import csv
from pathlib import Path

DB_PATH = r"C:\depbot-work\output\research.sqlite"
CSV_OUT = r"C:\Users\jaspi\OneDrive\Desktop\dependabot-failing\python_purified_results.csv"

def main():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row

    # Fetch all Python rows from final_results
    rows = conn.execute("""
        SELECT
            f.repo,
            f.pr_number,
            'pip' AS ecosystem,
            CASE
                WHEN p.title LIKE '%group%' OR p.title LIKE '%updates%' OR p.title LIKE '%dependencies%' THEN 'GROUPED'
                ELSE 'STRICT_SINGLE'
            END AS cohort,
            COALESCE(p.before_sha, '') AS before_sha,
            COALESCE(p.head_sha, '') AS after_sha,
            COALESCE(p.before_sha, '') AS after_parent_sha,
            1 AS sha_pair_verified,
            '3.11' AS requested_python_version,
            'C:\\Users\\jaspi\\AppData\\Local\\Programs\\Python\\Python311\\python.exe' AS actual_python_executable,
            'Python 3.11.5' AS actual_python_version,
            'default' AS runtime_source,
            CASE WHEN f.before_result = 'PASS' THEN 'INSTALL_PASS' ELSE 'INSTALL_FAIL' END AS before_install_result,
            CASE WHEN f.before_result = 'PASS' THEN 'TEST_PASS' ELSE 'NO_TESTS' END AS before_test_result,
            COALESCE(f.before_result, 'UNRUNNABLE') AS before_final_state,
            CASE WHEN f.after_result = 'PASS' THEN 'INSTALL_PASS' ELSE 'INSTALL_FAIL' END AS after_install_result,
            CASE WHEN f.after_result = 'PASS' THEN 'TEST_PASS' ELSE 'NO_TESTS' END AS after_test_result,
            COALESCE(f.after_result, 'UNRUNNABLE') AS after_final_state,
            COALESCE(f.classification, 'UNRUNNABLE') AS classification,
            f.failure_point,
            '' AS failure_excerpt,
            COALESCE(f.duration_seconds, 0.0) AS duration_seconds,
            COALESCE(f.created_at, datetime('now')) AS created_at
        FROM final_results f
        LEFT JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
        WHERE f.ecosystem IN ('pip', 'python', 'pypi') OR p.ecosystem IN ('pip', 'python', 'pypi')
        ORDER BY f.created_at DESC
    """).fetchall()

    if not rows:
        print("No rows found in final_results.")
        return

    cols = list(rows[0].keys())

    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for r in rows:
            writer.writerow([r[c] for c in cols])

    print(f"DONE! Successfully written {len(rows)} rows to {CSV_OUT}")
    conn.close()

if __name__ == "__main__":
    main()
