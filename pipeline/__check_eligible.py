import sqlite3

conn = sqlite3.connect(r'C:\depbot-work\output\research.sqlite')

eligible = conn.execute("""
    SELECT COUNT(*) FROM pull_requests 
    WHERE before_sha IS NOT NULL AND head_sha IS NOT NULL 
    AND processing_status NOT IN ('DONE','PROBED_OK','PROBED_FAIL') 
    AND strict_or_complex='STRICT'
""").fetchone()[0]

all_with_shas = conn.execute("""
    SELECT COUNT(*) FROM pull_requests 
    WHERE before_sha IS NOT NULL AND head_sha IS NOT NULL 
    AND processing_status NOT IN ('DONE','PROBED_OK','PROBED_FAIL')
""").fetchone()[0]

validated = conn.execute("SELECT COUNT(*) FROM pull_requests WHERE processing_status='VALIDATED'").fetchone()[0]

print(f"Eligible STRICT PRs with SHAs (ready to probe): {eligible}")
print(f"All PRs with SHAs (including COMPLEX):          {all_with_shas}")
print(f"VALIDATED PRs (fetched but not yet probed):      {validated}")
print(f"\nTo reach 1000 probed, you need more PRs from GitHub if eligible < ~2000")
print(f"Run:  python stage2_fetch_live_github.py --limit 1000")

conn.close()
