import sqlite3

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== ECOSYSTEM BREAKDOWN IN FINAL_RESULTS ===")
rows = conn.execute("SELECT ecosystem, COUNT(*) as c FROM final_results GROUP BY ecosystem ORDER BY c DESC").fetchall()
for r in rows:
    print(f"  {r['ecosystem'] or 'unknown'}: {r['c']}")

print("\n=== ECOSYSTEM BREAKDOWN IN PULL_REQUESTS (ALL PULLED) ===")
rows2 = conn.execute("SELECT ecosystem, COUNT(*) as c FROM pull_requests GROUP BY ecosystem ORDER BY c DESC").fetchall()
for r in rows2:
    print(f"  {r['ecosystem'] or 'unknown'}: {r['c']}")

print("\n=== LABELS SAMPLE FROM PULL_REQUESTS ===")
sample_labels = conn.execute("SELECT repo, pr_number, ecosystem, labels, strict_or_complex FROM pull_requests WHERE labels IS NOT NULL AND labels != '[]' LIMIT 5").fetchall()
for r in sample_labels:
    print(f"  {r['repo']} #{r['pr_number']} [{r['ecosystem']}] strict={r['strict_or_complex']} labels={r['labels']}")

conn.close()
