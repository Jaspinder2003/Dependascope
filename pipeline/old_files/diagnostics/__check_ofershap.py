import sqlite3

conn = sqlite3.connect(r'C:\depbot-work\output\research.sqlite')
rows = conn.execute("SELECT repo, pr_number, processing_status FROM pull_requests WHERE processing_status='PROBED_OK' OR repo='ofershap/tiny-queue'").fetchall()
for r in rows:
    print(f"  {r[0]} #{r[1]} -> status: {r[2]}")
conn.close()
