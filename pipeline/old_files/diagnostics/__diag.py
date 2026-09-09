import sqlite3, os

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== FULL RESULT DISTRIBUTION ===")
rows = conn.execute("SELECT classification, COUNT(*) as n FROM final_results GROUP BY classification ORDER BY n DESC").fetchall()
for r in rows: print(f"  {r[0]}: {r[1]}")

print("\n=== BEFORE/AFTER STAGE BREAKDOWN ===")
rows = conn.execute("SELECT before_result, after_result, COUNT(*) as n FROM final_results GROUP BY before_result, after_result ORDER BY n DESC LIMIT 20").fetchall()
for r in rows: print(f"  BEFORE={r[0]:12} AFTER={r[1]:12} n={r[2]}")

print("\n=== REMAINING RUNNABLE (STRICT, has SHAs, not DONE) by ecosystem ===")
rows = conn.execute("""
    SELECT ecosystem, COUNT(*) as n FROM pull_requests
    WHERE strict_or_complex='STRICT'
    AND before_sha IS NOT NULL AND head_sha IS NOT NULL
    AND processing_status != 'DONE'
    GROUP BY ecosystem ORDER BY n DESC
""").fetchall()
for r in rows: print(f"  {r[0] or 'unknown':15} {r[1]}")

print("\n=== DATE RANGE of new P2 JSON data ===")
rows = conn.execute("""
    SELECT substr(created_at, 1, 7) as ym, COUNT(*) as n
    FROM pull_requests
    WHERE source_dataset='partition2_json'
    GROUP BY ym ORDER BY ym LIMIT 20
""").fetchall()
for r in rows: print(f"  {r[0]}: {r[1]}")

print("\n=== TIMEOUT failures ===")
rows = conn.execute("SELECT repo, pr_number, before_result, after_result FROM final_results WHERE before_result='TIMEOUT' OR after_result='TIMEOUT'").fetchall()
for r in rows: print(f"  {r[0]}#{r[1]}  BEFORE={r[2]} AFTER={r[3]}")

print("\n=== PASS results (any PASS at all?) ===")
rows = conn.execute("SELECT repo, pr_number, ecosystem, before_result, after_result, classification FROM final_results WHERE before_result='PASS' OR after_result='PASS'").fetchall()
if rows:
    for r in rows: print(f"  {r[0]}#{r[1]} [{r[2]}] BEFORE={r[3]} AFTER={r[4]} => {r[5]}")
else:
    print("  ZERO PASS results anywhere")

conn.close()
