"""
stage6789_reproduce.py
───────────────────────
Main reproduction runner for Stages 6–9:
  Stage 6: Fetch required Git history
  Stage 7: Detect execution plan from CI config
  Stage 8: (Subprocess sandbox – Docker unavailable)
  Stage 9: Run before/after experiments; record results

Writes to:
  - pull_requests.processing_status
  - executions table
  - final_results table
  - execution_logs/ (per-stage stdout/stderr)

Supports: --resume, --limit, --workers, --ecosystem, --timeout, --only-strict,
          --dry-run, --max-per-repo
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db
import git_fetcher as gf
import ecosystem_adapters as ea
import sandbox_executor as sx

LOG_DIR = C.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
C.EXEC_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "stage6789_reproduce.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


# ─── Per-PR reproduction ──────────────────────────────────────────────────────

def reproduce_pr(
    repo: str,
    pr_number: int,
    head_sha: str,
    before_sha: str,
    ecosystem: str,
    dry_run: bool = False,
    attempt: int = 1,
    conn=None,
) -> dict:
    """
    Run full BEFORE/AFTER reproduction for one PR.
    Returns a result dict suitable for final_results table.
    """
    result = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "before_sha": before_sha,
        "ecosystem": ecosystem,
        "before_result": "UNKNOWN",
        "after_result": "UNKNOWN",
        "classification": "UNKNOWN",
        "failure_point": None,
        "reproduced": 0,
        "attempt": attempt,
        "log_paths": [],
        "error": None,
    }

    if dry_run:
        logger.info(f"  [DRY RUN] Would reproduce {repo}#{pr_number} ({ecosystem})")
        result["classification"] = "DRY_RUN"
        return result

    # ── Stage 6: Fetch Git history ─────────────────────────────────────────
    logger.info(f"Fetching commits for {repo}#{pr_number} …")
    ok, err = gf.clone_or_update(repo, head_sha, before_sha)
    if not ok:
        logger.warning(f"  Git fetch failed: {err}")
        result["error"] = f"git_fetch_failed: {err}"
        result["classification"] = "UNRUNNABLE"
        _save_result(conn, result)
        return result

    # ── Stage 7/8: Check out BEFORE, detect plan, run ──────────────────────
    for snapshot, sha in [("BEFORE", before_sha), ("AFTER", head_sha)]:
        logger.info(f"  Snapshot: {snapshot} SHA={sha}")

        work_dir = Path(tempfile.mkdtemp(prefix=f"depbot_{repo.replace('/', '_')}_{pr_number}_{snapshot}_"))

        try:
            ok, err = gf.checkout_snapshot_worktree(repo, sha, work_dir)
            if not ok:
                logger.warning(f"  Checkout failed: {err}")
                result[f"{snapshot.lower()}_result"] = "UNRUNNABLE"
                _save_exec(conn, repo, pr_number, snapshot, "CHECKOUT", "",
                           -1, 0, "FAIL", None, None, attempt)
                continue

            # Detect ecosystem plan from actual files on disk
            plan = ea.get_execution_plan(ecosystem, work_dir)
            if not plan or not plan.stages:
                logger.warning(f"  No execution plan for ecosystem: {ecosystem}")
                result[f"{snapshot.lower()}_result"] = "UNRUNNABLE"
                continue

            log_dir = C.EXEC_LOG_DIR / f"{repo.replace('/', '__')}__pr{pr_number}"
            first_failure, stage_results = sx.run_plan(
                plan, work_dir, log_dir, snapshot, repo, pr_number, attempt
            )

            result[f"{snapshot.lower()}_result"] = first_failure
            result["log_paths"].extend([
                r["stdout_path"] for r in stage_results
            ] + [r["stderr_path"] for r in stage_results])

            # Save each stage to executions table
            for sr in stage_results:
                _save_exec(
                    conn, repo, pr_number, snapshot,
                    sr["stage"], sr["command"],
                    sr["exit_code"], sr["duration_seconds"], sr["result"],
                    sr["stdout_path"], sr["stderr_path"], attempt,
                )

        finally:
            # Always clean up work dir
            try:
                shutil.rmtree(str(work_dir), ignore_errors=True)
            except Exception:
                pass

    # ── Classify ─────────────────────────────────────────────────────────────
    before = result["before_result"]
    after  = result["after_result"]

    if before == "PASS" and after == "PASS":
        classification = "PASS->PASS"
    elif before == "PASS" and after not in ("PASS", "UNKNOWN", "UNRUNNABLE", "DRY_RUN"):
        classification = "PASS->FAIL"
        result["failure_point"] = after
    elif before not in ("PASS", "UNKNOWN", "UNRUNNABLE") and after not in ("PASS", "UNKNOWN", "UNRUNNABLE"):
        classification = "FAIL->FAIL"
    elif before not in ("PASS", "UNKNOWN", "UNRUNNABLE") and after == "PASS":
        classification = "FAIL->PASS"
    elif "TIMEOUT" in (before, after):
        classification = "TIMEOUT"
    elif "UNRUNNABLE" in (before, after):
        classification = "UNRUNNABLE"
    else:
        classification = "UNKNOWN"

    result["classification"] = classification
    logger.info(f"  {repo}#{pr_number}: {before} → {after} = {classification}")

    _save_result(conn, result)
    return result


def _save_exec(conn, repo, pr_number, snapshot, stage, command,
               exit_code, duration, result_str, stdout_path, stderr_path, attempt):
    if conn is None:
        return
    try:
        conn.execute(
            """INSERT OR REPLACE INTO executions
               (repo, pr_number, snapshot, stage, command, exit_code,
                duration_seconds, result, stdout_path, stderr_path, attempt_number)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (repo, pr_number, snapshot, stage, command, exit_code,
             duration, result_str,
             str(stdout_path) if stdout_path else None,
             str(stderr_path) if stderr_path else None,
             attempt)
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"Exec insert error: {e}")


def _save_result(conn, result: dict):
    if conn is None:
        return
    try:
        dep_row = None
        if conn:
            dep_row = conn.execute(
                "SELECT * FROM dependency_changes WHERE repo=? AND pr_number=? LIMIT 1",
                (result["repo"], result["pr_number"])
            ).fetchone()

        dep      = dep_row["dependency"]   if dep_row else None
        old_v    = dep_row["old_version"]  if dep_row else None
        new_v    = dep_row["new_version"]  if dep_row else None

        conn.execute(
            """INSERT OR REPLACE INTO final_results
               (repo, pr_number, dependency, old_version, new_version,
                ecosystem, before_sha, after_sha, before_result, after_result,
                classification, failure_point, reproduced, confidence,
                reproduction_attempts, log_paths, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result["repo"], result["pr_number"],
                dep, old_v, new_v,
                result.get("ecosystem"),
                result.get("before_sha"), result.get("head_sha"),
                result.get("before_result"), result.get("after_result"),
                result.get("classification"), result.get("failure_point"),
                result.get("reproduced", 0),
                "medium",
                result.get("attempt", 1),
                json.dumps(result.get("log_paths", [])),
                result.get("error"),
            )
        )
        conn.commit()

        # Update PR status
        conn.execute(
            "UPDATE pull_requests SET processing_status='DONE' WHERE repo=? AND pr_number=?",
            (result["repo"], result["pr_number"])
        )
        conn.commit()

    except Exception as e:
        logger.error(f"Result save error: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    conn = db.init_db(C.DB_PATH)

    # Select PRs to run
    where_clauses = ["pr.before_sha IS NOT NULL", "pr.head_sha IS NOT NULL"]
    params = []

    if args.resume:
        where_clauses.append("pr.processing_status = 'QUEUED'")
    else:
        where_clauses.append("pr.processing_status IN ('QUEUED','VALIDATED')")

    if args.only_strict:
        where_clauses.append("pr.strict_or_complex = 'STRICT'")

    if args.ecosystem:
        where_clauses.append("pr.ecosystem = ?")
        params.append(args.ecosystem)

    where = " AND ".join(where_clauses)
    sql = f"""
        SELECT pr.repo, pr.pr_number, pr.head_sha, pr.before_sha,
               pr.ecosystem, pr.priority_score
        FROM pull_requests pr
        WHERE {where}
        ORDER BY pr.priority_score DESC NULLS LAST
    """
    if args.limit:
        sql += f" LIMIT {args.limit}"

    rows = conn.execute(sql, params).fetchall()
    logger.info(f"PRs to reproduce: {len(rows)}")

    # Enforce max-per-repo
    repo_counts: dict[str, int] = collections.defaultdict(int)
    selected = []
    for row in rows:
        repo = row["repo"]
        if repo_counts[repo] < args.max_per_repo:
            selected.append(row)
            repo_counts[repo] += 1
    logger.info(f"After max-per-repo={args.max_per_repo}: {len(selected)} selected")

    # Run
    pass_to_fail = []
    pass_to_pass = []
    baseline_failures = []
    unrunnable = []

    def run_one(row):
        return reproduce_pr(
            repo=row["repo"],
            pr_number=row["pr_number"],
            head_sha=row["head_sha"],
            before_sha=row["before_sha"],
            ecosystem=row["ecosystem"] or "unknown",
            dry_run=args.dry_run,
            attempt=1,
            conn=conn,
        )

    if args.workers <= 1:
        for i, row in enumerate(selected):
            logger.info(f"[{i+1}/{len(selected)}] {row['repo']}#{row['pr_number']}")
            res = run_one(row)
            _categorise(res, pass_to_fail, pass_to_pass, baseline_failures, unrunnable)
    else:
        # Threaded (note: git operations and SQLite writes; use with care)
        # SQLite connection is NOT thread-safe; create per-thread connections
        def run_one_threaded(row):
            local_conn = db.get_connection(C.DB_PATH)
            res = reproduce_pr(
                repo=row["repo"],
                pr_number=row["pr_number"],
                head_sha=row["head_sha"],
                before_sha=row["before_sha"],
                ecosystem=row["ecosystem"] or "unknown",
                dry_run=args.dry_run,
                attempt=1,
                conn=local_conn,
            )
            local_conn.close()
            return res

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_one_threaded, row): row for row in selected}
            for i, future in enumerate(as_completed(futures)):
                try:
                    res = future.result()
                    _categorise(res, pass_to_fail, pass_to_pass, baseline_failures, unrunnable)
                except Exception as e:
                    row = futures[future]
                    logger.error(f"Thread error {row['repo']}#{row['pr_number']}: {e}")
                if (i + 1) % 10 == 0:
                    logger.info(f"Progress: {i+1}/{len(selected)}")

    # ── Re-run PASS->FAIL candidates to confirm ────────────────────────────
    logger.info(f"Re-running {len(pass_to_fail)} PASS->FAIL candidates to confirm …")
    confirmed = []
    for res in pass_to_fail:
        row = conn.execute(
            "SELECT head_sha, before_sha, ecosystem FROM pull_requests WHERE repo=? AND pr_number=?",
            (res["repo"], res["pr_number"])
        ).fetchone()
        if not row:
            continue
        res2 = reproduce_pr(
            repo=res["repo"], pr_number=res["pr_number"],
            head_sha=row["head_sha"], before_sha=row["before_sha"],
            ecosystem=row["ecosystem"] or "unknown",
            dry_run=args.dry_run, attempt=2, conn=conn,
        )
        if res2["classification"] == "PASS->FAIL":
            conn.execute(
                "UPDATE final_results SET reproduced=1, reproduction_attempts=2 WHERE repo=? AND pr_number=?",
                (res["repo"], res["pr_number"])
            )
            conn.commit()
            confirmed.append(res2)
            logger.info(f"  CONFIRMED: {res['repo']}#{res['pr_number']}")

    # ── Export results ────────────────────────────────────────────────────────
    if not args.dry_run:
        _export_csvs(conn)

    logger.info("=== Summary ===")
    logger.info(f"  PASS->FAIL (unconfirmed): {len(pass_to_fail)}")
    logger.info(f"  PASS->FAIL (confirmed):   {len(confirmed)}")
    logger.info(f"  PASS->PASS:               {len(pass_to_pass)}")
    logger.info(f"  Baseline failures:        {len(baseline_failures)}")
    logger.info(f"  Unrunnable:               {len(unrunnable)}")

    conn.close()


def _categorise(res, pass_to_fail, pass_to_pass, baseline_failures, unrunnable):
    c = res.get("classification", "UNKNOWN")
    if c == "PASS->FAIL":
        pass_to_fail.append(res)
    elif c == "PASS->PASS":
        pass_to_pass.append(res)
    elif c == "FAIL->FAIL":
        baseline_failures.append(res)
    elif c == "UNRUNNABLE":
        unrunnable.append(res)


def _export_csvs(conn):
    import csv
    import io

    exports = {
        "confirmed_pass_to_fail": (
            "SELECT fr.*, pr.pr_url FROM final_results fr "
            "LEFT JOIN pull_requests pr ON pr.repo=fr.repo AND pr.pr_number=fr.pr_number "
            "WHERE fr.classification='PASS->FAIL' AND fr.reproduced=1"
        ),
        "pass_to_pass": (
            "SELECT fr.*, pr.pr_url FROM final_results fr "
            "LEFT JOIN pull_requests pr ON pr.repo=fr.repo AND pr.pr_number=fr.pr_number "
            "WHERE fr.classification='PASS->PASS'"
        ),
        "baseline_failures": (
            "SELECT fr.*, pr.pr_url FROM final_results fr "
            "LEFT JOIN pull_requests pr ON pr.repo=fr.repo AND pr.pr_number=fr.pr_number "
            "WHERE fr.classification='FAIL->FAIL'"
        ),
        "unrunnable_cases": (
            "SELECT fr.*, pr.pr_url FROM final_results fr "
            "LEFT JOIN pull_requests pr ON pr.repo=fr.repo AND pr.pr_number=fr.pr_number "
            "WHERE fr.classification='UNRUNNABLE'"
        ),
    }

    for name, sql in exports.items():
        try:
            rows = conn.execute(sql).fetchall()
            if not rows:
                continue
            cols = [d[0] for d in conn.execute(sql + " LIMIT 0").description]
            out_path = C.OUTPUT_DIR / f"{name}.csv"
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(cols)
                for row in rows:
                    writer.writerow(list(row))
            logger.info(f"Exported {out_path} ({len(rows)} rows)")
        except Exception as e:
            logger.warning(f"Export {name} failed: {e}")

    # Also export execution_results.parquet
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        rows = conn.execute("SELECT * FROM executions").fetchall()
        if rows:
            cols = [d[0] for d in conn.execute("SELECT * FROM executions LIMIT 0").description]
            data = {c: [] for c in cols}
            for row in rows:
                for c, v in zip(cols, row):
                    data[c].append(v)
            table = pa.table(data)
            out = C.OUTPUT_DIR / "execution_results.parquet"
            pq.write_table(table, str(out))
            logger.info(f"Exported {out}")
    except Exception as e:
        logger.warning(f"Parquet export failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stages 6-9: Fetch, plan, run, record experiments")
    parser.add_argument("--limit",        type=int,   default=None,
                        help="Max PRs to run")
    parser.add_argument("--workers",      type=int,   default=1,
                        help="Parallel workers (use 1 for safety)")
    parser.add_argument("--ecosystem",    type=str,   default=None,
                        help="Filter to one ecosystem (npm, pip, …)")
    parser.add_argument("--timeout",      type=int,   default=C.EXEC_TIMEOUT,
                        help="Per-stage timeout in seconds")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--resume",       action="store_true",
                        help="Only process QUEUED (skip DONE)")
    parser.add_argument("--only-strict",  action="store_true",
                        help="Only run STRICT cohort PRs")
    parser.add_argument("--max-per-repo", type=int,   default=C.MAX_PRS_PER_REPO,
                        help="Max PRs per repository")
    args = parser.parse_args()
    C.EXEC_TIMEOUT = args.timeout
    main(args)
