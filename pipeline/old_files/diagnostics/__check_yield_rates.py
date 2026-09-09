import sqlite3

db_path = r'C:\depbot-work\output\research.sqlite'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== LIVE GITHUB API PIPELINE CONVERSION & YIELD METRICS ===")

# Total live PRs pulled
total_live = conn.execute("SELECT COUNT(*) FROM pull_requests WHERE source_dataset = 'live_github_api'").fetchone()[0]

# Probing status breakdown for live_github_api
probe_stats = conn.execute("""
    SELECT processing_status, COUNT(*) as c 
    FROM pull_requests 
    WHERE source_dataset = 'live_github_api' 
    GROUP BY processing_status
""").fetchall()

# Final results breakdown for live_github_api
results_stats = conn.execute("""
    SELECT f.classification, COUNT(*) as c
    FROM final_results f
    JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
    WHERE p.source_dataset = 'live_github_api'
    GROUP BY f.classification
""").fetchall()

conn.close()

res_dict = {r['classification']: r['c'] for r in results_stats}
pass_fail = res_dict.get('PASS->FAIL', 0)
pass_pass = res_dict.get('PASS->PASS', 0)
fail_fail = res_dict.get('FAIL->FAIL', 0)
unrunnable = res_dict.get('UNRUNNABLE', 0)

total_executed = pass_fail + pass_pass + fail_fail + unrunnable

print(f"Total Live GitHub PRs Ingested: {total_live}")

print("\nProcessing Status Breakdown:")
for r in probe_stats:
    print(f"  {r['processing_status']}: {r['c']}")

print("\nPhase 2 Execution Breakdown:")
print(f"  PASS->FAIL (Confirmed Failures): {pass_fail}")
print(f"  PASS->PASS (Healthy Updates):     {pass_pass}")
print(f"  FAIL->FAIL (Pre-existing Fail):   {fail_fail}")
print(f"  UNRUNNABLE:                       {unrunnable}")
print(f"  Total Executed in Phase 2:       {total_executed}")

if total_executed > 0:
    pf_rate = (pass_fail / total_executed) * 100
    clean_rate = ((pass_fail + pass_pass) / total_executed) * 100
    print(f"\n--- CALCULATED YIELD RATES ---")
    print(f"PASS->FAIL Yield Rate:              {pf_rate:.2f}%  ({pass_fail}/{total_executed})")
    print(f"Clean Execution (PASS/PASS+FAIL):    {clean_rate:.2f}%  ({pass_fail + pass_pass}/{total_executed})")
