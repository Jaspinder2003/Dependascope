"""
Reset all npm/unknown ecosystem PRs back to VALIDATED so they re-run with the fixes:
- CI=true (prevents Jest watch mode hanging)
- No BUILD stage (avoids vsce/webpack false failures)
- Auto-detect ecosystem for unknown PRs
- Lower timeouts (120s test, 300s install)
"""
import sqlite3

conn = sqlite3.connect(r'C:\depbot-work\output\research.sqlite')

# Reset all npm PRs
n1 = conn.execute("""
    UPDATE pull_requests SET processing_status='VALIDATED'
    WHERE repo IN (
        SELECT repo FROM final_results WHERE ecosystem='npm'
    )
""").rowcount
print(f"Reset {n1} npm PRs")

# Delete their old results so they get fresh runs
n2 = conn.execute("DELETE FROM final_results WHERE ecosystem='npm'").rowcount
print(f"Deleted {n2} old npm results")

n3 = conn.execute("""
    DELETE FROM executions WHERE repo IN (
        SELECT DISTINCT repo FROM pull_requests
        WHERE ecosystem='npm' OR ecosystem IS NULL
    )
""").rowcount
print(f"Deleted {n3} old npm executions")

conn.commit()

# Check how many npm PRs are ready
rows = conn.execute("""
    SELECT COUNT(*) FROM pull_requests
    WHERE before_sha IS NOT NULL AND head_sha IS NOT NULL
    AND processing_status != 'DONE'
    AND (ecosystem = 'npm' OR ecosystem IS NULL)
""").fetchone()
print(f"\nReady to run: {rows[0]} npm + unknown-ecosystem PRs")

conn.close()
print("Done. Now run: python stage6789_reproduce.py --limit 200")
