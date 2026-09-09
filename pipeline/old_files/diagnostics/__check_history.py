import sqlite3

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== EXECUTIONS WITH STAGE='INSTALL' AND RESULT='PASS' ===")
rows = conn.execute("""
    SELECT DISTINCT repo, pr_number, snapshot
    FROM executions
    WHERE stage='INSTALL' AND result='PASS'
""").fetchall()
for r in rows:
    print(f"  {r['repo']}#{r['pr_number']} snapshot={r['snapshot']}")

print("\n=== EXECUTIONS WITH STAGE='TEST' (ANY RESULT) ===")
rows = conn.execute("""
    SELECT repo, pr_number, snapshot, result, stdout_path, stderr_path
    FROM executions
    WHERE stage='TEST'
""").fetchall()
for r in rows:
    print(f"  {r['repo']}#{r['pr_number']} snapshot={r['snapshot']} result={r['result']}")

conn.close()
