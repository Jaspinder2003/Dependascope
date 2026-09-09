import sqlite3, os

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT repo, pr_number, snapshot, command, result, stdout_path, stderr_path
    FROM executions
    WHERE stage='TEST'
    LIMIT 10
""").fetchall()

for r in rows:
    print(f"\n==========================================")
    print(f"Repo: {r['repo']} #{r['pr_number']} ({r['snapshot']})")
    print(f"Command: {r['command']}")
    print(f"Result: {r['result']}")
    print("---------------- STDOUT ----------------")
    if r['stdout_path'] and os.path.exists(r['stdout_path']):
        with open(r['stdout_path'], 'rb') as f:
            print(f.read(2000).decode('utf-8', errors='replace')[-1500:])
    print("---------------- STDERR ----------------")
    if r['stderr_path'] and os.path.exists(r['stderr_path']):
        with open(r['stderr_path'], 'rb') as f:
            print(f.read(2000).decode('utf-8', errors='replace')[-1500:])

conn.close()
