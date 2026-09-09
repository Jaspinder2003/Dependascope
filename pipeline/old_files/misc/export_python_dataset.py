"""
export_python_dataset.py — Exports Python PASS->PASS and PASS->FAIL cases to CSV.
"""
import sqlite3
import csv
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("export_python")

OUT_DIR = C.OUTPUT_DIR
OUT_DIR.mkdir(parents=True, exist_ok=True)

CSV_PATH_OUT = OUT_DIR / "python_pass_and_pass_fail_dataset.csv"
CSV_PATH_ROOT = Path(__file__).parent.parent.parent / "python_pass_and_pass_fail_dataset.csv"


def export_python_data():
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

    headers = [
        "Repository", "PR Number", "Ecosystem", "Classification",
        "Before Result", "After Result", "Execution Time (s)", "PR Title",
        "Created At", "Source Dataset", "Before SHA", "After SHA"
    ]

    processed_rows = []
    pf_count = 0
    pp_count = 0

    for r in rows:
        dur = r["duration_seconds"]
        dur_str = f"{dur:.2f}" if dur is not None else "N/A"
        
        if r["classification"] == "PASS->FAIL":
            pf_count += 1
        elif r["classification"] == "PASS->PASS":
            pp_count += 1

        processed_rows.append([
            r["repo"],
            r["pr_number"],
            r["ecosystem"],
            r["classification"],
            r["before_result"],
            r["after_result"],
            dur_str,
            r["title"] or "",
            r["created_at"] or "",
            r["source_dataset"] or "",
            r["before_sha"] or "",
            r["after_sha"] or ""
        ])

    for path in [CSV_PATH_OUT, CSV_PATH_ROOT]:
        existing_map = {}
        if path.exists():
            try:
                with open(path, "r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        key = (r.get("Repository"), str(r.get("PR Number")))
                        existing_map[key] = dict(r)
            except Exception:
                pass

        for row_list in processed_rows:
            dict_row = dict(zip(headers, row_list))
            key = (dict_row["Repository"], str(dict_row["PR Number"]))
            existing_map[key] = dict_row

        merged_output = list(existing_map.values())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(merged_output)
        logger.info(f"Python dataset exported/merged to: {path}")

    logger.info("=" * 60)
    logger.info("PYTHON DATASET SUMMARY EXPORT")
    logger.info(f"  Total Python PASS->FAIL cases: {pf_count}")
    logger.info(f"  Total Python PASS->PASS cases: {pp_count}")
    logger.info(f"  Total Exported Rows:          {len(processed_rows)}")
    logger.info("=" * 60)

    conn.close()


if __name__ == "__main__":
    export_python_data()
