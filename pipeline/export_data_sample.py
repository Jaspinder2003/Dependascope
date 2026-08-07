import sqlite3
import csv
import shutil
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import db

db_path = r'C:\depbot-work\output\research.sqlite'
output_csv = r'C:\depbot-work\output\dependabot_research_sample.csv'
output_timed_csv = r'C:\depbot-work\output\dependabot_research_with_timing.csv'
output_md = r'C:\depbot-work\output\dependabot_research_sample.md'

conn = db.init_db(Path(db_path))

query = """
    SELECT 
        f.repo,
        f.pr_number,
        f.ecosystem,
        f.classification,
        f.before_result,
        f.after_result,
        f.duration_seconds,
        p.title,
        p.created_at,
        p.source_dataset,
        p.head_sha,
        p.before_sha
    FROM final_results f
    LEFT JOIN pull_requests p ON f.repo = p.repo AND f.pr_number = p.pr_number
    ORDER BY 
        CASE f.classification 
            WHEN 'PASS->FAIL' THEN 1 
            WHEN 'PASS->PASS' THEN 2 
            WHEN 'FAIL->FAIL' THEN 3 
            ELSE 4 
        END,
        p.created_at DESC
"""

rows = conn.execute(query).fetchall()
conn.close()

# Helper to format duration
def format_duration(dur):
    if dur is not None and str(dur).strip() != "":
        return f"{float(dur):.2f}"
    return "N/A"

# 1. Export CSV file for Excel (including Execution Time column)
for csv_file_path in [output_csv, output_timed_csv]:
    with open(csv_file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Repository", "PR Number", "Ecosystem", "Classification",
            "Before Result", "After Result", "Execution Time (s)", "PR Title", "Created At",
            "Source Dataset", "Before SHA", "After SHA"
        ])
        for r in rows:
            dur_str = format_duration(r["duration_seconds"])
            writer.writerow([
                r["repo"], r["pr_number"], r["ecosystem"], r["classification"],
                r["before_result"], r["after_result"], dur_str, r["title"], r["created_at"],
                r["source_dataset"], r["before_sha"], r["head_sha"]
            ])

# Also copy to workspace root
root_dir = Path(r"c:\Users\jaspi\OneDrive\Desktop\dependabot-failing")
if root_dir.exists():
    shutil.copy(output_csv, root_dir / "dependabot_research_sample.csv")
    shutil.copy(output_timed_csv, root_dir / "dependabot_research_with_timing.csv")

# 2. Export Markdown file for report / email
with open(output_md, "w", encoding="utf-8") as f:
    f.write("# Dependabot Research Data Sample\n\n")
    f.write(f"Total Reproduced PRs: **{len(rows)}**\n\n")
    f.write("| Repository | PR | Ecosystem | Classification | Before | After | Exec Time (s) | Title |\n")
    f.write("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |\n")
    for r in rows:
        title_clean = (r["title"] or "").replace("|", "-")
        dur_str = format_duration(r["duration_seconds"])
        f.write(f"| `{r['repo']}` | #{r['pr_number']} | `{r['ecosystem']}` | **{r['classification']}** | {r['before_result']} | {r['after_result']} | {dur_str} | {title_clean} |\n")

print(f"Standard CSV Exported to: {output_csv}")
print(f"Timed CSV Exported to: {output_timed_csv}")
print(f"Markdown Exported to: {output_md}")
print(f"Total Rows Exported: {len(rows)}")

