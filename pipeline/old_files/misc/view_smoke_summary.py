import sqlite3

conn = sqlite3.connect(r"C:\depbot-work\output\research.sqlite")
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT * FROM purified_results").fetchall()

print("=" * 100)
print("PURIFIED PIPELINE SMOKE TEST RESULTS (5 REPRESENTATIVE PRs)")
print("=" * 100)

for r in rows:
    print(f"\nRepository / PR:       {r['repo']} #{r['pr_number']}")
    print(f"Cohort Type:           {r['cohort']}")
    print(f"SHA Pair Verified:     {'YES' if r['sha_pair_verified'] else 'NO'} (Parent: {r['after_parent_sha'][:8] if r['after_parent_sha'] else 'N/A'})")
    print(f"Runtime Requested:     {r['requested_python_version']} ({r['runtime_source']})")
    print(f"Runtime Actual:        {r['actual_python_version']}")
    print(f"BEFORE State:          {r['before_final_state']} [Install: {r['before_install_result']}, Test: {r['before_test_result']}]")
    print(f"AFTER State:           {r['after_final_state']} [Install: {r['after_install_result']}, Test: {r['after_test_result']}]")
    print(f"Final Classification:  {r['classification']}")
    excerpt = r['failure_excerpt'] or "None"
    clean_ex = excerpt.strip().splitlines()[-1] if excerpt.strip() else "None"
    print(f"Failure Excerpt:       {clean_ex}")

print("\n" + "=" * 100)
conn.close()
