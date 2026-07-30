import sqlite3

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== CURRENT CLASSIFICATION SUMMARY IN SQLITE ===")
rows = conn.execute("""
    SELECT classification, COUNT(*) as count
    FROM final_results
    GROUP BY classification
    ORDER BY count DESC
""").fetchall()

for r in rows:
    print(f"  {r['classification']}: {r['count']}")

print("\n=== ALL PASS->FAIL REPOS FOUND SO FAR ===")
pf_rows = conn.execute("""
    SELECT repo, pr_number, ecosystem, before_result, after_result
    FROM final_results
    WHERE classification = 'PASS->FAIL'
""").fetchall()

if pf_rows:
    for r in pf_rows:
        print(f"  [PASS->FAIL] {r['repo']} #{r['pr_number']} [{r['ecosystem']}] BEFORE={r['before_result']} -> AFTER={r['after_result']}")
else:
    print("  None in this active batch yet.")

conn.close()
