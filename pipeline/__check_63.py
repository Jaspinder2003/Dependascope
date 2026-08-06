import sqlite3

conn = sqlite3.connect(r'C:\depbot-work\output\research.sqlite')
conn.row_factory = sqlite3.Row

print("=== CHECKING THE 63 EXECUTIONS WHERE BEFORE=PASS AND AFTER!=PASS ===")
rows = conn.execute("""
    SELECT DISTINCT e1.repo, e1.pr_number, p.ecosystem, f.classification, f.before_result, f.after_result
    FROM executions e1
    JOIN pull_requests p ON e1.repo = p.repo AND e1.pr_number = p.pr_number
    LEFT JOIN final_results f ON e1.repo = f.repo AND e1.pr_number = f.pr_number
    WHERE e1.snapshot = 'BEFORE' AND e1.result = 'PASS'
      AND EXISTS (
          SELECT 1 FROM executions e2 
          WHERE e2.repo = e1.repo AND e2.pr_number = e1.pr_number 
            AND e2.snapshot = 'AFTER' AND e2.result != 'PASS'
      )
""").fetchall()

print(f"Total found: {len(rows)}\n")

for r in rows:
    print(f"  {r['repo']} #{r['pr_number']} [{r['ecosystem']}] -> final_results classification: {r['classification']} (before={r['before_result']}, after={r['after_result']})")

conn.close()
