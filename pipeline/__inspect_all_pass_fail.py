import sqlite3, os

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== ALL CONFIRMED PASS->FAIL REPOSITORIES ===")
rows = conn.execute("""
    SELECT repo, pr_number, ecosystem, before_result, after_result
    FROM final_results
    WHERE classification = 'PASS->FAIL'
""").fetchall()

for r in rows:
    print(f"\n=============================================")
    print(f"Repo: {r['repo']} #{r['pr_number']} [{r['ecosystem']}]")
    print(f"Before: {r['before_result']} | After: {r['after_result']}")
    
    pr = conn.execute("SELECT title, created_at, head_sha, before_sha FROM pull_requests WHERE repo=? AND pr_number=?", (r['repo'], r['pr_number'])).fetchone()
    if pr:
        print(f"PR Title:   {pr['title']}")
        print(f"Before SHA: {pr['before_sha']}")
        print(f"After SHA:  {pr['head_sha']}")
        print(f"Created At: {pr['created_at']}")
    
    execs = conn.execute("SELECT snapshot, stage, command, exit_code, duration_seconds, result, stderr_path FROM executions WHERE repo=? AND pr_number=?", (r['repo'], r['pr_number'])).fetchall()
    print("\nExecutions:")
    for ex in execs:
        print(f"  [{ex['snapshot']}] stage={ex['stage']} cmd='{ex['command']}' exit={ex['exit_code']} res={ex['result']}")
        if ex['result'] == 'FAIL' and ex['stderr_path'] and os.path.exists(ex['stderr_path']):
            with open(ex['stderr_path'], 'rb') as f:
                content = f.read(4000).decode('utf-8', errors='replace')
            print(f"    --> STDERR snippet:\n{content[-1200:]}")

conn.close()
