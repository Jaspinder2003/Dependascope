import sqlite3
import csv
import os
from pathlib import Path

db_path = r'C:\depbot-work\output\research.sqlite'
output_csv = r'C:\depbot-work\output\dependabot_research_sample.csv'
output_md = r'C:\depbot-work\output\dependabot_research_sample.md'

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

query = """
    SELECT 
        f.repo,
        f.pr_number,
        f.ecosystem,
        f.classification,
        f.before_result,
        f.after_result,
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

# 1. Export CSV file for Excel
with open(output_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow([
        "Repository", "PR Number", "Ecosystem", "Classification",
        "Before Result", "After Result", "PR Title", "Created At",
        "Source Dataset", "Before SHA", "After SHA"
    ])
    for r in rows:
        writer.writerow([
            r["repo"], r["pr_number"], r["ecosystem"], r["classification"],
            r["before_result"], r["after_result"], r["title"], r["created_at"],
            r["source_dataset"], r["before_sha"], r["head_sha"]
        ])

# 2. Export Markdown file for report / email
with open(output_md, "w", encoding="utf-8") as f:
    f.write("# Dependabot Research Data Sample\n\n")
    f.write(f"Total Reproduced PRs: **{len(rows)}**\n\n")
    f.write("| Repository | PR | Ecosystem | Classification | Before | After | Title |\n")
    f.write("| :--- | :---: | :---: | :---: | :---: | :---: | :--- |\n")
    for r in rows:
        title_clean = (r["title"] or "").replace("|", "-")
        f.write(f"| `{r['repo']}` | #{r['pr_number']} | `{r['ecosystem']}` | **{r['classification']}** | {r['before_result']} | {r['after_result']} | {title_clean} |\n")

print(f"CSV Exported to: {output_csv}")
print(f"Markdown Exported to: {output_md}")
print(f"Total Rows Exported: {len(rows)}")
