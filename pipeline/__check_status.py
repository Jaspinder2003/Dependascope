import sqlite3

conn = sqlite3.connect(r'C:\depbot-work\output\research.sqlite')
rows = conn.execute("SELECT processing_status, COUNT(*) FROM pull_requests GROUP BY processing_status").fetchall()
for r in rows:
    print(f"  {r[0]}: {r[1]}")
conn.close()
