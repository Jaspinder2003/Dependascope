import py_compile
import sqlite3
from pathlib import Path

p_dir = Path(r"C:\Users\jaspi\OneDrive\Desktop\dependabot-failing\Dependascope\pipeline")

print("=== PRE-FLIGHT SYSTEM VERIFICATION ===")

# 1. Syntax Verification
files = ["config.py", "db.py", "sandbox_executor.py", "smart_run.py", "stage2_fetch_live_github.py", "autopilot.py"]
for f in files:
    py_compile.compile(str(p_dir / f), doraise=True)
print("1. Syntax & Compilation: OK (All 6 core files compile clean)")

# 2. Database Integrity Check
db_path = Path(r"C:\depbot-work\output\research.sqlite")
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()
cursor.execute("PRAGMA integrity_check")
res = cursor.fetchone()[0]
print(f"2. Database Integrity:   {res}")

# 3. Queue & Candidate Check
ok_count = cursor.execute("SELECT COUNT(*) FROM pull_requests WHERE processing_status='PROBED_OK'").fetchone()[0]
cand_count = cursor.execute("SELECT COUNT(*) FROM pull_requests WHERE processing_status NOT IN ('DONE','PROBED_OK','PROBED_FAIL') AND source_dataset='live_github_api'").fetchone()[0]
print(f"3. Work Queues:         {ok_count} PROBED_OK ready to run, {cand_count} candidates ready to probe")

# 4. Total PASS->FAIL count
pf_count = cursor.execute("SELECT COUNT(*) FROM final_results WHERE classification='PASS->FAIL'").fetchone()[0]
print(f"4. Confirmed Breakages:  {pf_count} PASS->FAIL cases currently saved in research.sqlite")
conn.close()
