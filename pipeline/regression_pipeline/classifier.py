"""
classifier.py — Turn a BEFORE/AFTER snapshot pair into a defensible verdict.

The primary target dataset is TIER_3_CONFIRMED: a verified causal pair where
the project installed and executed meaningfully BEFORE the Dependabot update,
and the *same* execution now fails AFTER, while install still succeeds. This
isolates the dependency-code-behavior change as the cause, independent of
pip's dependency resolver.

An AFTER install failure (resolver conflict, yanked version, etc.) is real
signal but is NOT proof the shipped code broke — pip failing to resolve
versions is a different phenomenon from the project failing to run. Those
cases are preserved as INSTALL_REGRESSION / TIER_2 and are never folded into
the primary TIER_3_CONFIRMED tier.

Every branch returns a Verdict with a human-readable `reason` so the funnel
(candidate -> reproduced -> confirmed) stays fully auditable. Nothing is
silently dropped.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Verdict:
    classification: str
    tier: str    # TIER_3_CONFIRMED | TIER_2_INSTALL_REGRESSION | CONTROL | REJECTED
    reason: str


def classify(before: dict, after: dict, sha_verified: bool) -> Verdict:
    # ── Environment-level failures (not evidence about the dependency) ──────
    if before.get("failure_stage") == "CHECKOUT" or after.get("failure_stage") == "CHECKOUT":
        return Verdict("INCONCLUSIVE", "REJECTED",
                        "git checkout failed for the BEFORE or AFTER snapshot")
    if before.get("failure_stage") == "VENV" or after.get("failure_stage") == "VENV":
        return Verdict("RUNTIME_UNAVAILABLE", "REJECTED",
                        "could not create an isolated venv (Python runtime issue)")

    # ── Causality gate: hard-blocking, not just a logged warning ───────────
    if not sha_verified:
        return Verdict("UNVERIFIED_SHA_PAIR", "REJECTED",
                        "before_sha is not a verified parent of after_sha — cannot claim causality")

    b_install = before["install_result"]
    a_install = after["install_result"]
    b_exec = before["execution_result"]
    a_exec = after["execution_result"]
    b_strategy = before["execution_strategy"]

    # ── Harness timeouts are NOT evidence about the project ─────────────────
    # A 600s wall-clock limit expiring tells us our harness gave up, not that
    # the project is broken. Labelling these "already broken, not Dependabot's
    # fault" would put a false statement in the dataset, so they get their own
    # inconclusive classes and are never counted as baseline breakage.
    if b_install == "TIMEOUT":
        return Verdict("BASELINE_INSTALL_TIMEOUT", "REJECTED",
                        "BEFORE dependency install exceeded the harness time limit — inconclusive, "
                        "NOT evidence the project was broken")

    if b_exec == "TIMEOUT":
        return Verdict("BASELINE_EXECUTION_TIMEOUT", "REJECTED",
                        f"BEFORE {b_strategy} execution exceeded the harness time limit — inconclusive, "
                        "NOT evidence the project was broken")

    # ── No usable install plan is a harness limitation, not breakage ────────
    if b_install == "NO_PLAN" or a_install == "NO_PLAN":
        return Verdict("NO_INSTALL_PLAN", "REJECTED",
                        "repository exposes no installable manifest (no requirements*.txt / pyproject.toml / "
                        "setup.py / Pipfile) — we cannot establish a baseline, NOT evidence the project is broken")

    # ── BEFORE must be demonstrably healthy ─────────────────────────────────
    if b_install != "PASS":
        return Verdict("BASELINE_INSTALL_FAILURE", "REJECTED",
                        "project did not install BEFORE the update — already broken, not Dependabot's fault")

    if b_strategy == "none":
        return Verdict("NO_MEANINGFUL_EXECUTION", "REJECTED",
                        "no real test suite and no importable top-level package — cannot verify project health")

    if b_exec != "PASS":
        return Verdict("BASELINE_EXECUTION_FAILURE", "REJECTED",
                        f"project already failed its {b_strategy} execution BEFORE the update — pre-existing breakage")

    # ── BEFORE confirmed healthy past this point. Now examine AFTER. ───────
    if a_install == "TIMEOUT":
        return Verdict("AFTER_INSTALL_TIMEOUT", "REJECTED",
                        "BEFORE was healthy but the AFTER install exceeded the harness time limit — "
                        "inconclusive, cannot claim an installability regression from a timeout")

    if a_install != "PASS":
        return Verdict("INSTALL_REGRESSION", "TIER_2_INSTALL_REGRESSION",
                        "BEFORE was healthy (install + meaningful execution both PASS); AFTER dependency install "
                        "failed (resolver/version conflict) — real signal, but does not prove the shipped code "
                        "broke, so it is kept separate from the primary confirmed dataset")

    if after["execution_strategy"] == "none":
        return Verdict("INCONCLUSIVE_AFTER_EXECUTION", "REJECTED",
                        "AFTER install passed but the BEFORE execution strategy could not be re-applied")

    if a_exec == "PASS":
        return Verdict("PASS_TO_PASS", "CONTROL",
                        "project remained healthy both BEFORE and AFTER — useful as a control case")

    if a_exec == "TIMEOUT":
        # Previously this fell through to CONFIRMED_REGRESSION — a harness
        # timeout on AFTER was being promoted into the primary dataset. A
        # post-update hang may well be a genuine regression, but it is not
        # defensible without separate investigation, so it is held out.
        return Verdict("AFTER_EXECUTION_TIMEOUT", "REJECTED",
                        f"BEFORE {b_strategy} passed but AFTER exceeded the harness time limit — "
                        "possible hang-regression, held out of the confirmed dataset pending manual review")

    return Verdict("CONFIRMED_REGRESSION", "TIER_3_CONFIRMED",
                    f"BEFORE install+{b_strategy} PASS, AFTER install PASS but the identical {b_strategy} "
                    "execution FAILED — verified causal working->broken transition")
