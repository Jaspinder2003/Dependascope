"""
api_estimator.py
─────────────────
Compute minimum and likely API request estimates before any enrichment stage runs.
Aborts if the estimate exceeds the configured threshold unless --allow-large-api-run
is passed.

Estimates are labelled as minimum/likely because:
  - paginated endpoints require ceil(N / per_page) requests per item
  - the actual item count is not known until the first page is fetched
  - reviews, comments, and files can span multiple pages

Pagination overhead factor: API_PAGINATION_FACTOR (default 1.5×).
"""

import sys
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EstimateResult:
    def __init__(self, pr_count: int, calls_per_pr_min: int,
                 calls_per_pr_likely: float, label: str):
        self.pr_count            = pr_count
        self.calls_per_pr_min    = calls_per_pr_min
        self.calls_per_pr_likely = calls_per_pr_likely
        self.label               = label

    @property
    def total_min(self) -> int:
        return self.pr_count * self.calls_per_pr_min

    @property
    def total_likely(self) -> float:
        return self.pr_count * self.calls_per_pr_likely

    def display(self) -> str:
        lines = [
            f"",
            f"  API Request Estimate — {self.label}",
            f"  PRs to process       : {self.pr_count:,}",
            f"  Calls per PR (min)   : {self.calls_per_pr_min}",
            f"  Calls per PR (likely): {self.calls_per_pr_likely:.1f}  "
            f"(pagination overhead included)",
            f"  ─────────────────────────────────────────────────────",
            f"  Total minimum        : {self.total_min:,}  requests",
            f"  Total likely         : {int(self.total_likely):,}  requests",
            f"  Note: actual may be higher if PRs have many files,",
            f"        commits, or review comments requiring extra pages.",
            f"",
        ]
        return "\n".join(lines)


# ─── Stage-specific estimators ────────────────────────────────────────────────

def estimate_3a(pr_count: int) -> EstimateResult:
    """
    Stage 3A (minimal enrichment):
      1  GET /pulls/{n}
      1  GET /pulls/{n}/commits          (usually 1 page)
      2  GET /pulls/{n}/files            (avg 2 pages for 100 files/page)
      ──
      4  minimum per PR
      × 1.5 pagination factor → 6 likely
    """
    from config import API_PAGINATION_FACTOR
    calls_min    = 4
    calls_likely = calls_min * API_PAGINATION_FACTOR
    return EstimateResult(pr_count, calls_min, calls_likely, "Stage 3A – Minimal Enrichment")


def estimate_3a5(pr_count: int) -> EstimateResult:
    """
    Stage 3A.5 (manifest fetch):
      2  GET /contents/{manifest}?ref=sha  (before + after)
      fallback (if truncated): +2 (tree + blob × 2)
      ──
      2  minimum, 4 typical per PR
      × 1.5 → 3–6 likely
    """
    from config import API_PAGINATION_FACTOR
    calls_min    = 2
    calls_likely = 4.0   # many PRs will need tree+blob fallback
    return EstimateResult(pr_count, calls_min, calls_likely, "Stage 3A.5 – Manifest Fetch")


def estimate_3b(pr_count: int) -> EstimateResult:
    """
    Stage 3B (deep enrichment):
      1  GET /commits/{sha}/check-runs
      1  GET /commits/{sha}/status
      2  GET /issues/{n}/comments        (avg 1.5 pages)
      2  GET /pulls/{n}/comments         (avg 1.5 pages)
      2  GET /pulls/{n}/reviews          (avg 1.5 pages)
      1  GET /repos/{owner}/{repo}       (refresh)
      ──
      9  minimum per PR
      × 1.5 pagination → ~13 likely
    """
    from config import API_PAGINATION_FACTOR
    calls_min    = 9
    calls_likely = calls_min * API_PAGINATION_FACTOR
    return EstimateResult(pr_count, calls_min, calls_likely, "Stage 3B – Deep Enrichment")


# ─── Guard function ───────────────────────────────────────────────────────────

def check_and_confirm(estimate: EstimateResult, threshold: int,
                      allow_large: bool) -> None:
    """
    Print the estimate. If total_likely exceeds `threshold` and `allow_large`
    is False, print a message and sys.exit(1).
    """
    print(estimate.display())

    if estimate.total_likely > threshold:
        if allow_large:
            logger.warning(
                "Large API run allowed via --allow-large-api-run "
                "(likely ~%d requests).", int(estimate.total_likely)
            )
        else:
            print(
                f"  ABORTED: Likely request count ({int(estimate.total_likely):,}) "
                f"exceeds threshold ({threshold:,}).\n"
                f"  Re-run with --allow-large-api-run to proceed, or reduce --limit.\n"
            )
            sys.exit(1)
    else:
        logger.info(
            "API estimate within threshold (%d <= %d). Proceeding.",
            int(estimate.total_likely), threshold
        )
