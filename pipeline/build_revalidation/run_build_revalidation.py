"""
run_build_revalidation.py — Build-Revalidation Experiment Orchestrator
══════════════════════════════════════════════════════════════════════
Re-runs existing PASS->FAIL Python candidates through a project BUILD
check to determine if the Dependabot update broke the project build.

Input:  14 PASS->FAIL candidates (from python_rerun_results.csv,
        python_new_runs_results.csv, and user-specified PRs).
Output: C:\\depbot-work\\build_revalidation\\python_build_revalidation_results.csv

CRITICAL: This script does NOT access research.sqlite at all.
          It reads candidate SHAs from the CSV files only.
          Zero existing files are modified.

Usage:
    python pipeline/build_revalidation/run_build_revalidation.py
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import config as C
import git_fetcher as gf
from purified_reproduce import resolve_python_runtime

from build_revalidation.build_executor import run_build_snapshot

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] build_revalidation - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("build_revalidation")

# ─── Paths ────────────────────────────────────────────────────────────────────
BUILD_REVAL_DIR = Path("C:/depbot-work/build_revalidation")
WORK_ROOT       = BUILD_REVAL_DIR / "worktrees"
LOG_DIR         = BUILD_REVAL_DIR / "logs"
OUTPUT_CSV      = BUILD_REVAL_DIR / "python_build_revalidation_results.csv"

# ─── Input CSVs (read-only) ──────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent.parent.parent  # dependabot-failing/
RERUN_CSV   = REPO_ROOT / "python_rerun_results.csv"
NEW_RUNS_CSV = REPO_ROOT / "python_new_runs_results.csv"

# ─── The 14 unique PASS->FAIL candidates ─────────────────────────────────────
TARGET_PRS = [
    ("EvezArt/autoenveloper", 20),
    ("EvezArt/autoenveloper", 22),
    ("EvezArt/autoenveloper", 25),
    ("EvezArt/autonomous-research-orchestrator", 12),
    ("EvezArt/autonomous-research-orchestrator", 13),
    ("EvezArt/autonomous-research-orchestrator", 16),
    ("gaudiyakirtan/gaudiyakirtan", 7),
    ("hoverkraft-tech/ovh-snapshoter", 182),
    ("hoverkraft-tech/ovh-snapshoter", 183),
    ("Justin300507/atlas", 10),
    ("mohitmishra786/matchquill", 267),
    ("mohitmishra786/matchquill", 277),
    ("non-s/non-s.github.io", 261),
    ("zyw0815/ncm-converter", 129),
]

# ─── CSV Output Columns ──────────────────────────────────────────────────────
OUTPUT_COLUMNS = [
    "repo", "pr_number", "pr_title",
    "before_sha", "after_sha", "sha_pair_verified",
    "python_version", "project_rel_path",
    "build_config_source", "build_command",
    "before_install_result", "before_build_result", "before_failure_excerpt",
    "after_install_result", "after_build_result", "after_failure_excerpt",
    "classification", "duration_seconds",
]


def _load_candidate_info() -> dict:
    """
    Load SHA and metadata for TARGET_PRS from the source CSVs.
    Returns: {(repo, pr_number): {before_sha, after_sha, pr_title, ecosystem}}
    """
    info = {}

    for csv_path in [NEW_RUNS_CSV, RERUN_CSV]:
        if not csv_path.exists():
            logger.warning(f"CSV not found: {csv_path}")
            continue
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row["repo"], int(row["pr_number"]))
                if key in [(t[0], t[1]) for t in TARGET_PRS]:
                    if key not in info:
                        info[key] = {
                            "before_sha": row["before_sha"],
                            "after_sha": row["after_sha"],
                            "pr_title": row.get("pr_title", ""),
                            "ecosystem": row.get("ecosystem", "pip"),
                            "requested_python_version": row.get("requested_python_version", "3.x"),
                        }

    return info


def _load_already_done() -> set:
    """Load (repo, pr_number) pairs already in the output CSV to support resume."""
    done = set()
    if OUTPUT_CSV.exists():
        try:
            with open(OUTPUT_CSV, "r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    done.add((row["repo"], int(row["pr_number"])))
        except Exception:
            pass
    return done


def _append_result(row: dict) -> None:
    """Append one result row to the output CSV, creating header if needed."""
    write_header = not OUTPUT_CSV.exists()
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def classify_build_revalidation(before_res: dict, after_res: dict, sha_pair_verified: bool) -> str:
    """
    Classify according to 9 strict research outcomes:

    CONFIRMED_BUILD_REGRESSION  - verified SHA pair + BEFORE install PASS + BEFORE build PASS + AFTER install PASS + AFTER build FAIL
    INSTALLABILITY_REGRESSION   - BEFORE install PASS + AFTER install FAIL
    BUILD_PASS_TO_PASS          - BEFORE install PASS + BEFORE build PASS + AFTER install PASS + AFTER build PASS
    BASELINE_BUILD_FAILING      - BEFORE install PASS + BEFORE build FAIL
    BASELINE_INSTALL_FAILURE    - BEFORE install FAIL + AFTER install FAIL
    NO_PROJECT_BUILD_CONFIG     - No build configuration found
    UNVERIFIED_SHA_PAIR         - SHA pair verification failed (before_sha is not the parent of after_sha)
    RUNTIME_UNAVAILABLE         - Required Python executable not found
    INCONCLUSIVE                - Checkout error or unhandled state
    """
    b_install = before_res["install_result"]
    a_install = after_res["install_result"]
    b_build = before_res["build_result"]
    a_build = after_res["build_result"]

    # Checkout / Venv errors
    if before_res.get("failure_stage") == "CHECKOUT" or after_res.get("failure_stage") == "CHECKOUT":
        return "INCONCLUSIVE"
    if before_res.get("failure_stage") == "VENV" or after_res.get("failure_stage") == "VENV":
        return "RUNTIME_UNAVAILABLE"

    # Require verified SHA pair for regression claims
    if not sha_pair_verified:
        return "UNVERIFIED_SHA_PAIR"

    # Both install failed -> baseline install failure
    if b_install != "PASS" and a_install != "PASS":
        return "BASELINE_INSTALL_FAILURE"

    # BEFORE install PASS, AFTER install FAIL -> installability regression
    if b_install == "PASS" and a_install != "PASS":
        return "INSTALLABILITY_REGRESSION"

    # BEFORE install FAIL, AFTER install PASS -> baseline install failure / inconclusive
    if b_install != "PASS":
        return "BASELINE_INSTALL_FAILURE"

    # Both installed PASS -> examine build results
    if b_build == "NO_CONFIG" and a_build == "NO_CONFIG":
        return "NO_PROJECT_BUILD_CONFIG"

    if b_build in ("FAIL", "TIMEOUT"):
        return "BASELINE_BUILD_FAILING"

    if b_build == "PASS" and a_build == "PASS":
        return "BUILD_PASS_TO_PASS"

    if b_build == "PASS" and a_build in ("FAIL", "TIMEOUT"):
        return "CONFIRMED_BUILD_REGRESSION"

    return "INCONCLUSIVE"


def run_single(repo: str, pr_number: int, candidate: dict) -> dict:
    """Run build revalidation for one PR. Returns result row dict."""
    before_sha = candidate["before_sha"]
    after_sha = candidate["after_sha"]
    ecosystem = candidate.get("ecosystem", "pip")
    req_py = candidate.get("requested_python_version", "3.x")

    logger.info(f"=== {repo} #{pr_number} ===")
    logger.info(f"    BEFORE: {before_sha[:10]}  AFTER: {after_sha[:10]}")

    start_t = time.time()

    # Resolve Python runtime
    py_bin, act_py_ver, py_status = resolve_python_runtime(req_py)

    if not py_bin:
        logger.warning(f"    SKIP: Python {req_py} not available on host")
        return {
            "repo": repo, "pr_number": pr_number,
            "pr_title": candidate.get("pr_title", ""),
            "before_sha": before_sha, "after_sha": after_sha,
            "sha_pair_verified": 0,
            "python_version": f"MISSING ({req_py})",
            "project_rel_path": ".",
            "build_config_source": "", "build_command": "",
            "before_install_result": "UNRUNNABLE", "before_build_result": "SKIPPED",
            "before_failure_excerpt": "",
            "after_install_result": "UNRUNNABLE", "after_build_result": "SKIPPED",
            "after_failure_excerpt": "",
            "classification": "RUNTIME_UNAVAILABLE",
            "duration_seconds": round(time.time() - start_t, 2),
        }

    logger.info(f"    Python: {act_py_ver} ({py_status})")

    # Ensure repo is cloned/fetched
    gf.clone_or_update(repo, after_sha, before_sha, pr_number)

    # Causal SHA Pair Verification
    sha_verified, parent_sha, sha_msg = gf.verify_sha_pairing(repo, after_sha, before_sha)
    sha_pair_verified_int = 1 if sha_verified else 0
    logger.info(f"    SHA Pair Verified: {'YES' if sha_verified else 'NO'} ({sha_msg})")

    # Run BEFORE snapshot
    logger.info(f"    [BEFORE] Running install + build...")
    before_res = run_build_snapshot(
        repo, pr_number, "BEFORE", before_sha,
        py_bin, WORK_ROOT, LOG_DIR, ecosystem
    )
    logger.info(f"    [BEFORE] install={before_res['install_result']}  build={before_res['build_result']}  ({before_res['duration_seconds']}s)")

    # Run AFTER snapshot
    logger.info(f"    [AFTER]  Running install + build...")
    after_res = run_build_snapshot(
        repo, pr_number, "AFTER", after_sha,
        py_bin, WORK_ROOT, LOG_DIR, ecosystem
    )
    logger.info(f"    [AFTER]  install={after_res['install_result']}  build={after_res['build_result']}  ({after_res['duration_seconds']}s)")

    # Classify
    cls = classify_build_revalidation(before_res, after_res, sha_verified)
    total_duration = round(time.time() - start_t, 2)

    logger.info(f"    CLASSIFICATION: {cls}  (total: {total_duration}s)")

    return {
        "repo": repo,
        "pr_number": pr_number,
        "pr_title": candidate.get("pr_title", ""),
        "before_sha": before_sha,
        "after_sha": after_sha,
        "sha_pair_verified": sha_pair_verified_int,
        "python_version": act_py_ver,
        "project_rel_path": before_res.get("project_rel_path", ".") or after_res.get("project_rel_path", "."),
        "build_config_source": before_res["config_source"] or after_res["config_source"] or "",
        "build_command": before_res["build_command"] or after_res["build_command"] or "",
        "before_install_result": before_res["install_result"],
        "before_build_result": before_res["build_result"],
        "before_failure_excerpt": before_res.get("failure_excerpt", "") or "",
        "after_install_result": after_res["install_result"],
        "after_build_result": after_res["build_result"],
        "after_failure_excerpt": after_res.get("failure_excerpt", "") or "",
        "classification": cls,
        "duration_seconds": total_duration,
    }


def main(limit: Optional[int] = None):
    """
    Main entry point: run build revalidation on candidates.
    If limit is specified (e.g. limit=2), runs only N candidates for validation.
    """
    target_list = TARGET_PRS[:limit] if limit else TARGET_PRS

    logger.info("=" * 70)
    logger.info("PYTHON BUILD REVALIDATION EXPERIMENT")
    logger.info(f"Target: {len(target_list)} candidates (out of {len(TARGET_PRS)} total)")
    logger.info(f"Output: {OUTPUT_CSV}")
    logger.info("=" * 70)

    # Setup directories
    BUILD_REVAL_DIR.mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    candidates = _load_candidate_info()
    logger.info(f"Loaded metadata for {len(candidates)} candidates from CSV files")

    already_done = _load_already_done()
    if already_done:
        logger.info(f"Resuming: {len(already_done)} already completed in output CSV")

    total = len(target_list)
    completed = 0
    for i, (repo, pr_number) in enumerate(target_list, 1):
        if (repo, pr_number) in already_done and not limit:
            logger.info(f"[{i}/{total}] SKIP (already done): {repo} #{pr_number}")
            completed += 1
            continue

        candidate = candidates.get((repo, pr_number))
        if not candidate:
            logger.warning(f"[{i}/{total}] SKIP (no metadata): {repo} #{pr_number}")
            continue

        logger.info(f"\n[{i}/{total}] Processing: {repo} #{pr_number}")
        result = run_single(repo, pr_number, candidate)
        _append_result(result)
        completed += 1

        remaining = total - i
        logger.info(f"    Progress: {completed}/{total} done, {remaining} remaining")

    logger.info("\n" + "=" * 70)
    logger.info("BUILD REVALIDATION COMPLETE")
    logger.info(f"Results written to: {OUTPUT_CSV}")
    logger.info("=" * 70)

    if OUTPUT_CSV.exists():
        classifications = {}
        with open(OUTPUT_CSV, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cls = row["classification"]
                classifications[cls] = classifications.get(cls, 0) + 1

        logger.info("\nClassification Summary:")
        for cls, count in sorted(classifications.items()):
            logger.info(f"  {cls}: {count}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Python Build Revalidation Experiment")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N cases for validation test")
    args = parser.parse_args()

    main(limit=args.limit)
