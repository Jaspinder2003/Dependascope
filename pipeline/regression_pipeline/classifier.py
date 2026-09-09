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
    """
    The outcome assigned to a single candidate.

    `classification` is the fine-grained machine-readable outcome (for example
    CONFIRMED_REGRESSION or BASELINE_INSTALL_FAILURE); `tier` is the coarse
    bucket used for reporting; `reason` is the human-readable justification
    stored alongside the result so any verdict can be audited later.
    """
    classification: str
    tier: str    # TIER_3_CONFIRMED | TIER_3_TEST_REGRESSION | TIER_2_INSTALL_REGRESSION
                 # | CONTROL | REJECTED
    reason: str


# A suite has to be substantial enough that "these tests were green" means
# something. One passing test flipping in a two-test suite is noise; this is
# the floor below which a subset regression is not worth claiming.
MIN_BASELINE_PASSING = 3


def test_regressions(before: dict, after: dict) -> tuple[list, int, int]:
    """
    Tests that passed BEFORE and no longer pass AFTER.

    Returns (regressed_node_ids, baseline_passing_count, after_ran_count).

    A test that vanishes from the AFTER report counts as regressed: when a
    dependency update breaks an import, pytest cannot collect the module at
    all and its tests simply disappear rather than reporting failures. Not
    counting those would hide the most severe breakages of all.
    """
    b_tests = before.get("tests") or {}
    a_tests = after.get("tests") or {}
    baseline_passing = [t for t, o in b_tests.items() if o == "pass"]
    regressed = [t for t in baseline_passing if a_tests.get(t, "missing") in ("fail", "error", "missing")]
    return sorted(regressed), len(baseline_passing), len(a_tests)


def classify(before: dict, after: dict, sha_verified: bool) -> Verdict:
    # ── Environment-level failures (not evidence about the dependency) ──────
    """
    Decide the verdict for one candidate from its two snapshots.

    Takes the BEFORE and AFTER snapshot dictionaries and whether the commit
    pair was verified, and returns a Verdict.

    The checks run in a deliberate order, from least to most informative, so
    that a result is only ever attributed to the dependency once every cheaper
    explanation has been ruled out:

    1. Environment failures (checkout, virtualenv) -- these say nothing about
       the update and are rejected as inconclusive.
    2. Platform gaps -- a project needing a Unix-only module on a Windows host
       cannot be reproduced here, and must not be recorded as "already broken".
    3. Causality gate -- an unverified commit pair blocks any regression claim.
    4. Baseline health -- if BEFORE did not install or did not pass, there is
       no working state to regress from.
    5. Only then, the AFTER comparison, which can yield an install regression
       or a test regression.
    """
    if before.get("failure_stage") == "CHECKOUT" or after.get("failure_stage") == "CHECKOUT":
        return Verdict("INCONCLUSIVE", "REJECTED",
                        "git checkout failed for the BEFORE or AFTER snapshot")
    if before.get("failure_stage") == "VENV" or after.get("failure_stage") == "VENV":
        return Verdict("RUNTIME_UNAVAILABLE", "REJECTED",
                        "could not create an isolated venv (Python runtime issue)")

    # ── Platform gap: the project needs a Unix-only module we cannot provide ─
    # Being unable to import fcntl/resource/etc. on Windows says nothing about
    # the dependency update — the project can only run on Linux, where its CI
    # already exercised it. Kept out of BASELINE_EXECUTION_FAILURE so the
    # dataset never claims such a project was "already broken".
    if before.get("platform_incompatible") or after.get("platform_incompatible"):
        return Verdict("PLATFORM_INCOMPATIBLE", "REJECTED",
                        "project requires a Unix-only module (fcntl/resource/termios/...) not available on "
                        "this Windows host — reproduction is impossible here, NOT evidence about the update")

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

    # ── An unreachable package index is our fault, never the project's ─────
    # pip reporting zero candidate versions for something like django or
    # setuptools means it could not see the index at all. Calling that
    # "already broken" puts a false statement in the dataset, and on the AFTER
    # snapshot it silently manufactured TIER_2 install regressions: 3 of the
    # first 23 were this, not a real resolver conflict.
    if b_install == "NETWORK" or a_install == "NETWORK":
        which = "BEFORE" if b_install == "NETWORK" else "AFTER"
        return Verdict("INSTALL_INDEX_UNREACHABLE", "REJECTED",
                        f"the {which} dependency install could not reach the package index (pip saw no "
                        f"candidate versions) even after a retry — a network fault on our side, NOT "
                        f"evidence about the project or the dependency; safe to re-queue")

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
        # The suite was not fully green before the update — but that does not
        # mean it carries no evidence. If specific tests were passing at the
        # parent commit and fail at the Dependabot commit, with the dependency
        # install still succeeding, those individual tests are a working->broken
        # transition caused by the update, regardless of what the rest of the
        # suite was already doing. Held in its own tier: the causal claim is
        # per-test rather than whole-suite, so it must not be silently mixed
        # into the clean green->red dataset.
        regressed, baseline_passing, after_ran = test_regressions(before, after)
        if (b_strategy == "pytest_real" and a_install == "PASS"
                # AFTER must have actually attempted the same suite. If its
                # strategy is "none" the tests are absent because we never ran
                # them, and every baseline test would look "missing" — an
                # inconclusive run must never be read as total breakage.
                and after.get("execution_strategy") == "pytest_real"
                and after.get("execution_result") not in ("TIMEOUT", "NOT_RUN")
                and baseline_passing >= MIN_BASELINE_PASSING and regressed):
            collapsed = " (AFTER collected no tests at all — the update broke collection)" if after_ran == 0 else ""
            shown = ", ".join(regressed[:5]) + (" ..." if len(regressed) > 5 else "")
            return Verdict("TEST_LEVEL_REGRESSION", "TIER_3_TEST_REGRESSION",
                            f"{len(regressed)} of {baseline_passing} tests that PASSED at the parent commit "
                            f"fail after the update{collapsed}; dependency install still succeeds, so the "
                            f"failure is in the shipped code, not in resolution. Regressed: {shown}")
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

    regressed, baseline_passing, _after_ran = test_regressions(before, after)
    detail = ""
    if regressed:
        shown = ", ".join(regressed[:5]) + (" ..." if len(regressed) > 5 else "")
        detail = (f" {len(regressed)} of {baseline_passing} previously-passing tests now fail: {shown}")
    return Verdict("CONFIRMED_REGRESSION", "TIER_3_CONFIRMED",
                    f"BEFORE install+{b_strategy} PASS, AFTER install PASS but the identical {b_strategy} "
                    f"execution FAILED — verified causal working->broken transition.{detail}")
