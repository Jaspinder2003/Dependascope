#!/usr/bin/env python3
"""
run_pipeline.py
───────────────
Top-level pipeline runner. Calls each stage in sequence.

Usage examples:
  python run_pipeline.py --limit 50 --dry-run
  python run_pipeline.py --limit 200 --workers 2 --ecosystem npm
  python run_pipeline.py --resume --only-strict --max-per-repo 3
  python run_pipeline.py --skip-stages 1,2 --limit 100

Stages:
  1  Inspect dataset files and produce schema report
  2  Extract Dependabot candidates into SQLite
  3  Enrich candidates via GitHub API
  4  Validate single-dependency classification
  5  Score and prioritize candidates
  6789 Fetch, reproduce, and record experiments
  10 Generate final report
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C

LOG_DIR = C.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Dependabot Failure Research Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--dry-run",        action="store_true",
                        help="Do not write to DB or files")
    parser.add_argument("--limit",          type=int,  default=None,
                        help="Max rows/PRs to process per stage")
    parser.add_argument("--workers",        type=int,  default=1,
                        help="Parallel workers for reproduction stage")
    parser.add_argument("--ecosystem",      type=str,  default=None,
                        help="Filter to one ecosystem (npm, pip, maven, go, …)")
    parser.add_argument("--only-strict",    action="store_true",
                        help="Only process STRICT single-dep PRs")
    parser.add_argument("--resume",         action="store_true",
                        help="Skip already-completed records")
    parser.add_argument("--prioritize-failures", action="store_true",
                        help="Sort queue by failure likelihood")
    parser.add_argument("--max-per-repo",   type=int,  default=C.MAX_PRS_PER_REPO,
                        help="Max PRs per repository in reproduction")
    parser.add_argument("--timeout",        type=int,  default=C.EXEC_TIMEOUT,
                        help="Per-stage timeout seconds")
    parser.add_argument("--skip-stages",    type=str,  default="",
                        help="Comma-separated stages to skip (e.g. '1,2')")
    parser.add_argument("--only-stage",     type=str,  default=None,
                        help="Run only this stage (1-10)")
    parser.add_argument("--github-token",   type=str,  default=None,
                        help="(NOT RECOMMENDED) Pass token via arg instead of env var")

    args = parser.parse_args()

    # Token: env var only (never log it)
    if args.github_token:
        os.environ["GITHUB_TOKEN"] = args.github_token
        logger.warning("Token passed via --github-token arg. Prefer GITHUB_TOKEN env var.")

    C.GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
    if not C.GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set. Stage 3 will use unauthenticated API (60 req/hr).")

    skip_stages = set(s.strip() for s in args.skip_stages.split(",") if s.strip())
    only_stage  = args.only_stage

    def should_run(stage_id: str) -> bool:
        if only_stage:
            return stage_id == only_stage
        return stage_id not in skip_stages

    # ── Stage 1 ───────────────────────────────────────────────────────────────
    if should_run("1"):
        logger.info("=== STAGE 1: Dataset Inspection ===")
        import stage1_inspect
        stage1_inspect.main(args)

    # ── Stage 2 ───────────────────────────────────────────────────────────────
    if should_run("2"):
        logger.info("=== STAGE 2: Extract Candidates ===")
        import stage2_extract_candidates
        stage2_extract_candidates.main(args)

    # ── Stage 3 ───────────────────────────────────────────────────────────────
    if should_run("3"):
        logger.info("=== STAGE 3: GitHub Enrichment ===")
        import stage3_enrich_github
        stage3_enrich_github.main(args)

    # ── Stage 4 ───────────────────────────────────────────────────────────────
    if should_run("4"):
        logger.info("=== STAGE 4: Validate Single-Dependency ===")
        import stage4_validate_single_dep
        stage4_validate_single_dep.main(args)

    # ── Stage 5 ───────────────────────────────────────────────────────────────
    if should_run("5"):
        logger.info("=== STAGE 5: Prioritize ===")
        import stage5_prioritize
        stage5_prioritize.main(args)

    # ── Stages 6-9 ────────────────────────────────────────────────────────────
    if should_run("6789"):
        logger.info("=== STAGES 6-9: Reproduce Experiments ===")
        import stage6789_reproduce
        stage6789_reproduce.main(args)

    # ── Stage 10 ──────────────────────────────────────────────────────────────
    if should_run("10"):
        logger.info("=== STAGE 10: Final Report ===")
        import stage10_report
        stage10_report.main(args)

    logger.info("Pipeline complete.")


if __name__ == "__main__":
    main()
