import sqlite3, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db
import stage6789_reproduce as sr

conn = db.init_db(sr.C.DB_PATH)
conn.row_factory = sqlite3.Row

row = conn.execute("""
    SELECT repo, pr_number, ecosystem, before_sha, head_sha
    FROM pull_requests
    WHERE repo = 'sloria/cosroom' AND pr_number = 44
""").fetchone()

if row:
    print(f"Testing reproduction for {row['repo']}#{row['pr_number']}...")
    res = sr.reproduce_pr(
        conn=conn,
        repo=row['repo'],
        pr_number=row['pr_number'],
        ecosystem='pip',
        before_sha=row['before_sha'],
        head_sha=row['head_sha']
    )
    print("Result:", res)
else:
    print("PR not found in DB")

conn.close()
