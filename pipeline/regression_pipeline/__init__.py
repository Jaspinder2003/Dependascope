"""
regression_pipeline — Corrected Python Dependabot regression methodology.

Primary target dataset: verified "working before -> dependency update -> broken
after" cases (TIER_3_CONFIRMED). Install-only regressions are preserved as a
separate TIER_2 signal, never folded into the primary dataset. Rejected /
inconclusive candidates are preserved with a reason, never silently dropped.

Completely isolated from research.sqlite / purified_results / final_results —
reads candidate metadata from research.sqlite (read-only) but writes all new
evidence to its own database under WORK_ROOT/regression_pipeline/.
"""
