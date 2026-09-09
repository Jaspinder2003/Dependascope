import sqlite3

conn = sqlite3.connect(r'C:\depbot-work\output\research.sqlite')
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT repo, pr_number, processing_status, created_at
    FROM pull_requests
    WHERE before_sha IS NOT NULL AND head_sha IS NOT NULL
    ORDER BY created_at DESC
    LIMIT 10
""").fetchall()

print("Top 10 most recent PRs in DB:")
for r in rows:
    print(f"  {r['repo']} #{r['pr_number']} -> status={r['processing_status']} (created: {r['created_at']})")

conn.close()
