import sqlite3

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== SAMPLE DATE FIELDS IN PULL_REQUESTS ===")
rows = conn.execute("""
    SELECT repo, pr_number, created_at, closed_at, merged_at, source_dataset
    FROM pull_requests
    WHERE created_at IS NOT NULL
    ORDER BY created_at DESC
    LIMIT 5
""").fetchall()

for r in rows:
    print(f"  {r['repo']} #{r['pr_number']}: created={r['created_at']} | closed={r['closed_at']} | source={r['source_dataset']}")

print("\n=== PR COUNT BY YEAR/MONTH (SAMPLE) ===")
ym_rows = conn.execute("""
    SELECT substr(created_at, 1, 7) as ym, COUNT(*) as c
    FROM pull_requests
    WHERE created_at IS NOT NULL AND created_at != ''
    GROUP BY ym
    ORDER BY ym DESC
    LIMIT 10
""").fetchall()

for r in ym_rows:
    print(f"  {r['ym']}: {r['c']} PRs")

conn.close()
