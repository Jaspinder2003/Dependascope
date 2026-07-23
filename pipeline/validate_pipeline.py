"""
validate_pipeline.py
─────────────────────
Integration validation script.
Tests the pipeline on a very small sample without needing GitHub token
(uses DB manipulation to simulate enriched state).
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import stage2_extract_candidates as s2
import stage4_validate_single_dep as s4
import stage5_prioritize as s5

print("=" * 60)
print("PIPELINE VALIDATION")
print("=" * 60)

# ── 1. Test title parsing ──────────────────────────────────────────
tests = [
    ("Bump lodash from 4.17.20 to 4.17.21", "lodash", "4.17.20", "4.17.21", "patch"),
    ("Bump react from 17.0.2 to 18.0.0", "react", "17.0.2", "18.0.0", "major"),
    ("Bump @types/node from 16.9.4 to 16.10.2", "@types/node", "16.9.4", "16.10.2", "minor"),
    ("Update dependency axios to v1.0.0", None, None, None, None),  # 'dependency' is not dep name
    ("Bump the production-dependencies group with 5 updates", None, None, None, None),
]

all_pass = True
for title, exp_dep, exp_old, exp_new, exp_bump in tests:
    dep, old, new = s2.extract_dep_from_title(title)
    bump = s2.classify_version_bump(old, new) if old and new else None
    ok = (dep == exp_dep) and (old == exp_old) and (new == exp_new)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  [{status}] Title parse: '{title[:50]}'")
    if not ok:
        print(f"         Expected: dep={exp_dep}, old={exp_old}, new={exp_new}")
        print(f"         Got:      dep={dep}, old={old}, new={new}")

print()

# ── 2. Test author detection ───────────────────────────────────────
authors = [
    ("dependabot[bot]", True),
    ("dependabot-preview[bot]", True),
    ("dependabot", True),
    ("dependabot-preview", True),
    ("DEPENDABOT[BOT]", True),   # case insensitive
    ("octocat", False),
    ("renovate[bot]", False),
    ("", False),
    (None, False),
]
for author, expected in authors:
    result = s2.is_dependabot_author(author)
    ok = result == expected
    if not ok:
        all_pass = False
    print(f"  [{'PASS' if ok else 'FAIL'}] Author '{author}' -> {result} (expected {expected})")

print()

# ── 3. Test DB state ──────────────────────────────────────────────
conn = db.init_db(C.DB_PATH)
n = conn.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
print(f"  [INFO] PRs in DB: {n}")

# Check a sample row
sample = conn.execute("SELECT * FROM pull_requests LIMIT 1").fetchone()
if sample:
    print(f"  [INFO] Sample PR: {sample['repo']}#{sample['pr_number']}: {sample['title'][:60]}")

# ── 4. Test resume logic ──────────────────────────────────────────
# Simulate enriched rows
test_repo = "test-owner/test-repo"
test_pr   = 99999

conn.execute("DELETE FROM pull_requests WHERE repo=? AND pr_number=?", (test_repo, test_pr))
conn.commit()

db.upsert_pr(conn, {
    "repo": test_repo, "pr_number": test_pr,
    "pr_url": f"https://github.com/{test_repo}/pull/{test_pr}",
    "author": "dependabot[bot]", "title": "Bump lodash from 4.17.20 to 4.17.21",
    "body": "test body", "state": "closed", "created_at": "2023-01-01",
    "merged_at": "", "closed_at": "2023-01-02",
    "head_sha": "abc123def456", "before_sha": "111222333444",
    "merge_sha": None, "ecosystem": "npm", "labels": '["dependencies"]',
    "comments_count": 0, "commits_count": 1,
    "additions": 1, "deletions": 1, "changed_files": 2,
    "priority_score": None, "strict_or_complex": "UNKNOWN",
    "classification_reason": "", "processing_status": "PENDING",
    "source_dataset": "test", "source_file": "test",
})
conn.commit()

# Add a changed_files row (to simulate post-enrichment)
conn.execute("""
    INSERT OR REPLACE INTO changed_files
    (repo, pr_number, filename, status, additions, deletions,
     is_manifest, is_lockfile, is_source, is_test, is_ci)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)
""", (test_repo, test_pr, "package.json", "modified", 1, 1, 1, 0, 0, 0, 0))
conn.execute("""
    INSERT OR REPLACE INTO changed_files
    (repo, pr_number, filename, status, additions, deletions,
     is_manifest, is_lockfile, is_source, is_test, is_ci)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)
""", (test_repo, test_pr, "package-lock.json", "modified", 100, 100, 0, 1, 0, 0, 0))
conn.execute(
    "UPDATE pull_requests SET processing_status='ENRICHED' WHERE repo=? AND pr_number=?",
    (test_repo, test_pr)
)
conn.commit()

# Run Stage 4 on just this test PR
class FakeArgs:
    limit = None
    dry_run = False
    resume = True

import argparse
args = argparse.Namespace(limit=None, dry_run=False, resume=True)
# Only validate our test row
rows_to_validate = [(test_repo, test_pr)]
for repo, pr_num in rows_to_validate:
    classification, reason = s4.validate_pr(conn, repo, pr_num, dry_run=False)
    print(f"  [INFO] Stage 4 result: {classification} / {reason}")
    ok = classification == "STRICT"
    if not ok:
        all_pass = False
    print(f"  [{'PASS' if ok else 'FAIL'}] Stage 4 classification: {classification} (expected STRICT)")

# Check it was written to DB
row = conn.execute(
    "SELECT strict_or_complex, processing_status FROM pull_requests WHERE repo=? AND pr_number=?",
    (test_repo, test_pr)
).fetchone()
if row:
    ok = row["strict_or_complex"] == "STRICT"
    if not ok: all_pass = False
    print(f"  [{'PASS' if ok else 'FAIL'}] DB updated: strict_or_complex={row['strict_or_complex']}")

# ── 5. Test Stage 5 scoring ───────────────────────────────────────
pr_dict = {
    "state": "closed",
    "merged_at": "",
    "title": "Bump lodash from 4.17.20 to 4.17.21 [fails tests]",
    "body": "This update breaks the CI pipeline",
    "labels": '["dependencies"]',
    "comments_count": 3,
    "changed_files": 2,
    "ecosystem": "npm",
    "_dep_info": {"version_change_type": "patch", "dependency": "lodash"},
}
score, reasons = s5.score_pr(pr_dict)
print(f"\n  [INFO] Priority score: {score} | reasons: {reasons}")
ok = score > 0
if not ok: all_pass = False
print(f"  [{'PASS' if ok else 'FAIL'}] Priority score > 0: {score}")

# ── Cleanup test data ─────────────────────────────────────────────
conn.execute("DELETE FROM pull_requests WHERE repo=?", (test_repo,))
conn.execute("DELETE FROM changed_files WHERE repo=?", (test_repo,))
conn.execute("DELETE FROM dependency_changes WHERE repo=?", (test_repo,))
conn.commit()
conn.close()

print()
print("=" * 60)
if all_pass:
    print("ALL VALIDATION TESTS PASSED")
else:
    print("SOME TESTS FAILED - review output above")
print("=" * 60)
