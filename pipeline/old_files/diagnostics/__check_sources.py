import sqlite3

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== TOTAL PRs IN DATABASE BY SOURCE DATASET ===")
rows = conn.execute("""
    SELECT source_dataset, COUNT(*) as total_prs
    FROM pull_requests
    GROUP BY source_dataset
""").fetchall()

for r in rows:
    print(f"  {r['source_dataset'] or 'legacy_dataset'}: {r['total_prs']} PRs")

print("\n=== EXECUTED / REPRODUCED PRs IN FINAL_RESULTS BY SOURCE DATASET ===")
executed_rows = conn.execute("""
    SELECT pr.source_dataset, f.classification, COUNT(*) as c
    FROM final_results f
    JOIN pull_requests pr ON f.repo = pr.repo AND f.pr_number = pr.pr_number
    GROUP BY pr.source_dataset, f.classification
    ORDER BY pr.source_dataset, c DESC
""").fetchall()

for r in executed_rows:
    print(f"  [{r['source_dataset'] or 'legacy_dataset'}] {r['classification']}: {r['c']}")

print("\n=== TOTAL UNIQUE EXECUTED PRs BY SOURCE ===")
unique_execs = conn.execute("""
    SELECT pr.source_dataset, COUNT(DISTINCT f.repo || '#' || f.pr_number) as c
    FROM final_results f
    JOIN pull_requests pr ON f.repo = pr.repo AND f.pr_number = pr.pr_number
    GROUP BY pr.source_dataset
""").fetchall()

for r in unique_execs:
    print(f"  {r['source_dataset'] or 'legacy_dataset'}: {r['c']} executed PRs")

conn.close()
