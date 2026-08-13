"""
export_python_legitimate_dataset.py — Creates a separate enriched CSV for real/legitimate Python projects.
Original database and original CSVs are 100% untouched.
"""
import sqlite3
import csv
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import github_client as gh

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("export_python_legitimate")

CSV_PATH_ROOT = Path(__file__).parent.parent.parent / "python_real_projects_dataset.csv"

def export_legitimate_python_data():
    conn = db.init_db(C.DB_PATH)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT 
            f.repo,
            f.pr_number,
            COALESCE(f.ecosystem, p.ecosystem, 'pip') as ecosystem,
            f.classification,
            f.before_result,
            f.after_result,
            f.duration_seconds,
            p.title,
            p.created_at,
            p.source_dataset,
            p.before_sha,
            p.head_sha AS after_sha
        FROM final_results f
        LEFT JOIN pull_requests p 
            ON f.repo = p.repo AND f.pr_number = p.pr_number
        WHERE (f.ecosystem IN ('pip', 'python', 'pypi') OR p.ecosystem IN ('pip', 'python', 'pypi'))
          AND f.classification IN ('PASS->PASS', 'PASS->FAIL')
        ORDER BY f.classification DESC, f.repo ASC, f.pr_number ASC
    """

    rows = conn.execute(query).fetchall()
    logger.info(f"Fetched {len(rows)} Python candidate rows from database. Fetching GitHub metadata...")

    headers = [
        "Repository", "PR Number", "Ecosystem", "Classification", "Cohort",
        "Before Result", "After Result", "Execution Time (s)",
        "Stars", "Forks", "Open Issues", "Last Pushed At", "Is Fork",
        "PR Title", "Created At", "Source Dataset", "Before SHA", "After SHA"
    ]

    filtered_rows = []
    repo_meta_cache = {}

    import re
    for r in rows:
        repo_name = r["repo"]
        if repo_name not in repo_meta_cache:
            try:
                owner, name = repo_name.split("/", 1)
                meta = gh.get_repo(conn, owner, name)
                if meta:
                    repo_meta_cache[repo_name] = {
                        "stars": meta.get("stargazers_count", 0),
                        "forks": meta.get("forks_count", 0),
                        "issues": meta.get("open_issues_count", 0),
                        "pushed_at": meta.get("pushed_at", ""),
                        "is_fork": meta.get("fork", False)
                    }
                else:
                    repo_meta_cache[repo_name] = {"stars": 0, "forks": 0, "issues": 0, "pushed_at": "", "is_fork": False}
            except Exception as e:
                logger.warning(f"Could not fetch metadata for {repo_name}: {e}")
                repo_meta_cache[repo_name] = {"stars": 0, "forks": 0, "issues": 0, "pushed_at": "", "is_fork": False}

        m = repo_meta_cache[repo_name]

        dur = r["duration_seconds"]
        dur_str = f"{dur:.2f}" if dur is not None else "N/A"

        title_str = (r["title"] or "").lower()
        if re.search(r"\bgroup\b|\bupdates\b|\bdependencies\b|\bpackages\b", title_str):
            cohort = "GROUPED"
        else:
            cohort = "STRICT_SINGLE"

        row_data = [
            r["repo"],
            r["pr_number"],
            r["ecosystem"],
            r["classification"],
            cohort,
            r["before_result"],
            r["after_result"],
            dur_str,
            m["stars"],
            m["forks"],
            m["issues"],
            m["pushed_at"],
            "Yes" if m["is_fork"] else "No",
            r["title"] or "",
            r["created_at"] or "",
            r["source_dataset"] or "",
            r["before_sha"] or "",
            r["after_sha"] or ""
        ]

        # Legitimacy Filter: Keep repos with stars >= 1 OR forks >= 1 OR open issues > 0 OR established org
        if m["stars"] > 0 or m["forks"] > 0 or m["issues"] > 0:
            filtered_rows.append(row_data)

    # Read existing CSV rows if present to prevent truncation
    existing_map = {}
    if CSV_PATH_ROOT.exists():
        try:
            with open(CSV_PATH_ROOT, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    key = (r.get("Repository"), str(r.get("PR Number")))
                    existing_map[key] = dict(r)
        except Exception:
            pass

    # Merge newly filtered rows into existing map
    for row_list in filtered_rows:
        dict_row = dict(zip(headers, row_list))
        key = (dict_row["Repository"], str(dict_row["PR Number"]))
        existing_map[key] = dict_row

    merged_output = list(existing_map.values())
    with open(CSV_PATH_ROOT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(merged_output)

    logger.info("=" * 60)
    logger.info("SEPARATE LEGITIMATE PYTHON DATASET EXPORT COMPLETE")
    logger.info(f"  Saved to:              {CSV_PATH_ROOT}")
    logger.info(f"  Total Verified Repos:   {len(filtered_rows)} / {len(rows)}")
    logger.info("  Original data:         100% untouched")
    logger.info("=" * 60)

    conn.close()

if __name__ == "__main__":
    export_legitimate_python_data()
