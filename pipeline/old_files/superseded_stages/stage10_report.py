"""
stage10_report.py
──────────────────
Generate the final research report:
  - confirmed_pass_to_fail.csv (primary deliverable)
  - summary statistics
  - Parquet exports for all result categories
  - Human-readable markdown report
"""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db

LOG_DIR = C.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "stage10_report.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


def main(args: argparse.Namespace) -> None:
    conn = db.init_db(C.DB_PATH)

    # ── Stats ─────────────────────────────────────────────────────────────────
    stats = {}
    for label, sql in {
        "total_candidates":       "SELECT COUNT(*) FROM pull_requests",
        "strict":                 "SELECT COUNT(*) FROM pull_requests WHERE strict_or_complex='STRICT'",
        "complex":                "SELECT COUNT(*) FROM pull_requests WHERE strict_or_complex='COMPLEX'",
        "enriched":               "SELECT COUNT(*) FROM pull_requests WHERE processing_status IN ('ENRICHED','VALIDATED','QUEUED','DONE')",
        "done":                   "SELECT COUNT(*) FROM pull_requests WHERE processing_status='DONE'",
        "pass_to_fail_all":       "SELECT COUNT(*) FROM final_results WHERE classification='PASS->FAIL'",
        "pass_to_fail_confirmed": "SELECT COUNT(*) FROM final_results WHERE classification='PASS->FAIL' AND reproduced=1",
        "pass_to_pass":           "SELECT COUNT(*) FROM final_results WHERE classification='PASS->PASS'",
        "fail_to_fail":           "SELECT COUNT(*) FROM final_results WHERE classification='FAIL->FAIL'",
        "unrunnable":             "SELECT COUNT(*) FROM final_results WHERE classification='UNRUNNABLE'",
        "timeout":                "SELECT COUNT(*) FROM final_results WHERE classification='TIMEOUT'",
    }.items():
        stats[label] = conn.execute(sql).fetchone()[0]

    # ── Ecosystem breakdown ───────────────────────────────────────────────────
    eco_rows = conn.execute(
        "SELECT ecosystem, COUNT(*) as cnt FROM pull_requests "
        "WHERE strict_or_complex='STRICT' GROUP BY ecosystem ORDER BY cnt DESC"
    ).fetchall()
    stats["ecosystem_breakdown"] = {r["ecosystem"] or "unknown": r["cnt"] for r in eco_rows}

    # ── Failure point breakdown ───────────────────────────────────────────────
    fp_rows = conn.execute(
        "SELECT failure_point, COUNT(*) as cnt FROM final_results "
        "WHERE classification='PASS->FAIL' GROUP BY failure_point ORDER BY cnt DESC"
    ).fetchall()
    stats["failure_point_breakdown"] = {r["failure_point"] or "unknown": r["cnt"] for r in fp_rows}

    # ── Export confirmed PASS->FAIL ───────────────────────────────────────────
    ptf_rows = conn.execute("""
        SELECT fr.repo, fr.pr_number, pr.pr_url, fr.dependency,
               fr.old_version, fr.new_version, fr.ecosystem,
               fr.before_sha, fr.after_sha, fr.before_result, fr.after_result,
               fr.failure_point, fr.reproduction_attempts, fr.log_paths
        FROM final_results fr
        LEFT JOIN pull_requests pr ON pr.repo=fr.repo AND pr.pr_number=fr.pr_number
        WHERE fr.classification='PASS->FAIL' AND fr.reproduced=1
        ORDER BY fr.pr_number
    """).fetchall()

    ptf_cols = [
        "repo", "pr_number", "pr_url", "dependency", "old_version", "new_version",
        "ecosystem", "before_sha", "after_sha", "before_result", "after_result",
        "first_failure_stage", "reproduction_attempts", "log_paths"
    ]
    ptf_path = C.OUTPUT_DIR / "confirmed_pass_to_fail.csv"
    with open(ptf_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(ptf_cols)
        for row in ptf_rows:
            writer.writerow([
                row["repo"], row["pr_number"], row["pr_url"] or "",
                row["dependency"] or "", row["old_version"] or "", row["new_version"] or "",
                row["ecosystem"] or "", row["before_sha"] or "", row["after_sha"] or "",
                row["before_result"] or "", row["after_result"] or "",
                row["failure_point"] or "", row["reproduction_attempts"] or 0,
                row["log_paths"] or "[]",
            ])
    logger.info(f"Confirmed PASS->FAIL: {len(ptf_rows)} rows → {ptf_path}")

    # ── Markdown report ───────────────────────────────────────────────────────
    _write_markdown(stats, ptf_rows, C.OUTPUT_DIR / "final_report.md")

    # ── Print to console ──────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("RESEARCH PIPELINE – FINAL REPORT")
    print("="*60)
    print(f"  Total candidate PRs:          {stats['total_candidates']:>8,}")
    print(f"  STRICT (single-dep) PRs:      {stats['strict']:>8,}")
    print(f"  COMPLEX (grouped/multi) PRs:  {stats['complex']:>8,}")
    print(f"  PRs run (DONE):               {stats['done']:>8,}")
    print(f"  PASS -> FAIL (unconfirmed):   {stats['pass_to_fail_all']:>8,}")
    print(f"  PASS -> FAIL (confirmed):     {stats['pass_to_fail_confirmed']:>8,}")
    print(f"  PASS -> PASS:                 {stats['pass_to_pass']:>8,}")
    print(f"  FAIL -> FAIL (baseline fail): {stats['fail_to_fail']:>8,}")
    print(f"  Unrunnable:                   {stats['unrunnable']:>8,}")
    print()
    print("  Ecosystem breakdown (STRICT PRs):")
    for eco, cnt in sorted(stats["ecosystem_breakdown"].items(), key=lambda x: -x[1]):
        print(f"    {eco or 'unknown':20s} {cnt:>6,}")
    print()
    print("  Failure point distribution:")
    for fp, cnt in sorted(stats["failure_point_breakdown"].items(), key=lambda x: -x[1]):
        print(f"    {fp or 'unknown':20s} {cnt:>6,}")
    print()
    print(f"  Primary output:  {ptf_path}")
    print("="*60)

    conn.close()


def _write_markdown(stats: dict, ptf_rows: list, path: Path) -> None:
    lines = [
        "# Dependabot Failure Study – Final Report",
        "",
        "## Pipeline Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Total candidate PRs | {stats['total_candidates']:,} |",
        f"| STRICT (single-dep) | {stats['strict']:,} |",
        f"| COMPLEX (grouped/multi) | {stats['complex']:,} |",
        f"| PRs reproduced | {stats['done']:,} |",
        f"| **PASS → FAIL (confirmed)** | **{stats['pass_to_fail_confirmed']:,}** |",
        f"| PASS → PASS | {stats['pass_to_pass']:,} |",
        f"| FAIL → FAIL | {stats['fail_to_fail']:,} |",
        f"| Unrunnable | {stats['unrunnable']:,} |",
        "",
        "## Ecosystem Breakdown (STRICT PRs)",
        "",
        "| Ecosystem | Count |",
        "|-----------|-------|",
    ]
    for eco, cnt in sorted(stats["ecosystem_breakdown"].items(), key=lambda x: -x[1]):
        lines.append(f"| {eco or 'unknown'} | {cnt:,} |")

    lines += [
        "",
        "## First Failure Stage Distribution",
        "",
        "| Failure Stage | Count |",
        "|---------------|-------|",
    ]
    for fp, cnt in sorted(stats["failure_point_breakdown"].items(), key=lambda x: -x[1]):
        lines.append(f"| {fp or 'unknown'} | {cnt:,} |")

    lines += [
        "",
        "## Confirmed PASS → FAIL Cases",
        "",
        "| # | Repo | PR | Dependency | Old → New | Ecosystem | Failure Stage |",
        "|---|------|----|------------|-----------|-----------|---------------|",
    ]
    for i, row in enumerate(ptf_rows[:50], 1):
        dep    = row["dependency"] or "?"
        old_v  = row["old_version"] or "?"
        new_v  = row["new_version"] or "?"
        lines.append(
            f"| {i} | {row['repo']} | [{row['pr_number']}]({row['pr_url'] or '#'}) "
            f"| {dep} | {old_v} → {new_v} | {row['ecosystem'] or '?'} "
            f"| {row['failure_point'] or '?'} |"
        )

    if len(ptf_rows) > 50:
        lines.append(f"| … | *(and {len(ptf_rows)-50} more in confirmed_pass_to_fail.csv)* | | | | | |")

    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"Markdown report: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 10: Generate final research report")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(args)
