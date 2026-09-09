import sqlite3

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== ALL CONFIRMED PASS->FAIL REPOSITORIES IN SQLITE ===")
rows = conn.execute("""
    SELECT f.repo, f.pr_number, f.ecosystem, f.before_result, f.after_result, p.title, p.created_at
    FROM final_results f
    LEFT JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
    WHERE f.classification = 'PASS->FAIL'
    ORDER BY p.created_at DESC
""").fetchall()

print(f"Total PASS->FAIL Repositories Found: {len(rows)}\n")

for i, r in enumerate(rows, 1):
    print(f"  [{i}] {r['repo']} #{r['pr_number']} [{r['ecosystem']}]")
    print(f"      Title: {r['title']}")
    print(f"      Before: {r['before_result']} -> After: {r['after_result']}")
    print(f"      Created At: {r['created_at']}")
    print("-" * 60)

conn.close()
