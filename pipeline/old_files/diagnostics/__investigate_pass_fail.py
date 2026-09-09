import sqlite3

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== CHECKING ALL PASS->FAIL CLASSIFICATIONS IN SQLITE ===")

# All rows in final_results where classification = PASS->FAIL
fr_rows = conn.execute("""
    SELECT repo, pr_number, ecosystem, before_result, after_result
    FROM final_results
    WHERE classification = 'PASS->FAIL'
""").fetchall()

print(f"final_results table count for PASS->FAIL: {len(fr_rows)}")

# Check executions table for any PRs where BEFORE=PASS and AFTER=FAIL (or AFTER != PASS)
exec_prs = conn.execute("""
    SELECT DISTINCT repo, pr_number 
    FROM executions e1
    WHERE e1.snapshot = 'BEFORE' AND e1.result = 'PASS'
      AND EXISTS (
          SELECT 1 FROM executions e2 
          WHERE e2.repo = e1.repo AND e2.pr_number = e1.pr_number 
            AND e2.snapshot = 'AFTER' AND e2.result != 'PASS'
      )
""").fetchall()

print(f"executions table distinct PRs where BEFORE=PASS and AFTER!=PASS: {len(exec_prs)}")

# Check pull_requests table where processing_status='DONE' or 'PROBED_OK'
print("\n=== DETAILED LIST OF ALL PASS->FAIL PRs IN SQLITE ===")
for r in fr_rows:
    print(f"  {r['repo']} #{r['pr_number']} [{r['ecosystem']}] BEFORE={r['before_result']} AFTER={r['after_result']}")

conn.close()
