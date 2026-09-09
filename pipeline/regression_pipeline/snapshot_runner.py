"""
snapshot_runner.py — Install + meaningfully execute one BEFORE/AFTER snapshot.

Isolated per-snapshot venv & disposable worktree, same security posture as
sandbox_executor (secrets stripped, wall-clock timeouts). Captures full
evidence (commands run, exit codes, stdout/stderr excerpts) so a rejected or
confirmed verdict can be root-caused later without re-running anything.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import config as C
import git_fetcher as gf
import ecosystem_adapters as ea
from ecosystem_adapters import _find_pip_root
import sandbox_executor as sx

from regression_pipeline.execution_detector import detect_execution_plan, ExecutionPlan
from regression_pipeline.repo_lock import repo_lock
from regression_pipeline.install_planner import plan_install


_NOT_PACKAGEABLE = re.compile(
    r"is not installable|neither 'setup\.py' nor 'pyproject\.toml'|"
    r"automatic discovery|Multiple top-level packages discovered|"
    r"flat-layout|To modify pip, please run",
    re.I,
)


def _is_not_packageable(text: str) -> bool:
    """True when an install failure means 'this repo is not pip-installable
    the way we tried', rather than 'this project is broken'."""
    return bool(text and _NOT_PACKAGEABLE.search(text))


_NETWORK_INSTALL = re.compile(
    r"read timed out|connection (?:reset|aborted|error)|max retries exceeded|"
    r"failed to establish a new connection|temporary failure in name resolution|"
    r"network is unreachable|proxyerror|newconnectionerror|"
    r"could not fetch url|ssl(?:error|certificate|: )",
    re.I,
)


def _is_index_unreachable(text: str) -> bool:
    """
    True when an install failed because pip could not see the package index —
    not because the project or the dependency is broken.

    The giveaway is "(from versions: none)": pip found ZERO candidate versions.
    For packages like django, flask, fastapi or setuptools that is impossible
    unless the index was unreachable, yet 30 of 49 recent baseline install
    failures said exactly that, and every one was being recorded as "project
    did not install BEFORE the update - already broken, not Dependabot's
    fault". That is a false statement in the dataset, and it also produced
    false TIER_2 install regressions when it struck the AFTER snapshot.

    One important exception: pip also reports "from versions: none" when it
    DID reach the index but filtered every version out by requires-python. It
    says so explicitly when that happens, and that case is a real interpreter
    mismatch which the interpreter-retry path handles, so it must not be
    mistaken for a network fault.
    """
    if not text:
        return False
    if _NETWORK_INSTALL.search(text):
        return True
    if re.search(r"from versions:\s*none", text, re.I):
        if re.search(r"ignored the following versions?.{0,80}?requires? a different python",
                     text, re.I | re.S):
            return False
        return True
    return False


# Stdlib modules that simply do not exist on Windows. A project importing one
# of these cannot run here at all — that is a platform gap in our harness, not
# the project being broken, and must not be recorded as "already broken".
_UNIX_ONLY_STDLIB = {
    "fcntl", "resource", "termios", "grp", "pwd", "posix", "curses", "syslog",
    "spwd", "crypt", "nis", "ossaudiodev", "readline", "tty", "pty",
}

_MODNOTFOUND = re.compile(r"(?:ModuleNotFoundError|ImportError):\s*No module named ['\"]([^'\"]+)['\"]")


def _missing_modules(text: str) -> list[str]:
    """Top-level module names pytest could not import, in first-seen order."""
    seen = []
    for m in _MODNOTFOUND.findall(text or ""):
        top = m.split(".")[0]
        if top and top not in seen:
            seen.append(top)
    return seen


def _heal_missing_test_deps(py_root: Path, venv_dir: Path, missing: list[str],
                            protected_dep: Optional[str], already: set) -> list[str]:
    """
    Best-effort `pip install` of support packages a test suite needs to be
    collected at all (pytest-mock, responses, requests, jsonschema, ...).

    51 of our baseline execution failures are a bare ModuleNotFoundError for an
    ordinary installable package — the project's own test dependency that our
    install step did not capture (it lived in an undeclared extra, a tox env,
    or a CI-only file). That is our incomplete environment, not the project
    failing, and it leaves the baseline with zero passing tests so the per-test
    comparison has nothing to work with.

    Safety rules that keep this from ever manufacturing a false regression:
      * only *top-level* distribution names are installed (a dotted name like
        'mcp.server' is a dependency's own submodule reorganisation — exactly
        the breakage we are hunting — and is never touched);
      * the dependency under test is never installed here, so a genuine
        import-time break in the updated package is preserved;
      * healing is applied identically to BEFORE and AFTER, so it can only ever
        make the two snapshots more comparable, never less.

    Returns the module names actually attempted this round.
    """
    py = _venv_python(venv_dir)
    env = sx._safe_env(venv_dir=venv_dir)
    prot = (protected_dep or "").strip().lower().replace("_", "-")
    attempted = []
    for mod in missing:
        if mod in already or mod in _UNIX_ONLY_STDLIB:
            continue
        if mod.strip().lower().replace("_", "-") == prot:
            continue
        try:
            subprocess.run([py, "-m", "pip", "install", "--quiet", "--prefer-binary",
                            "--no-input", "--disable-pip-version-check", mod],
                           cwd=str(py_root), capture_output=True, timeout=180, env=env)
        except Exception:
            pass
        attempted.append(mod)
    return attempted


def parse_junit(xml_path: Path) -> dict:
    """
    Map every test the suite reported to pass | fail | error | skip.

    Whole-suite exit codes throw away most of what a test run knows. 41% of our
    rejected baselines are projects whose suite was *already* partly red before
    Dependabot touched them, and under an exit-code comparison every one of
    those is unusable — even when 200 tests passed before the update and 3 of
    those exact tests fail after it. Per-test outcomes turn that into the
    strongest evidence in the dataset: a named test, green at the parent
    commit, red at the Dependabot commit, with its traceback.
    """
    out: dict[str, str] = {}
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(str(xml_path)).getroot()
    except Exception:
        return out
    # pytest emits <testsuites><testsuite>, older versions just <testsuite>.
    suites = root.iter("testsuite") if root.tag != "testcase" else []
    for suite in suites:
        for case in suite.iter("testcase"):
            cls = (case.get("classname") or "").strip()
            name = (case.get("name") or "").strip()
            if not name:
                continue
            node_id = f"{cls}::{name}" if cls else name
            outcome = "pass"
            for child in case:
                tag = child.tag.lower()
                if tag == "failure":
                    outcome = "fail"
                elif tag == "error":
                    outcome = "error"
                elif tag == "skipped":
                    outcome = "skip"
            out[node_id] = outcome
    return out


def _tail(path: Path, n: int = 800) -> str:
    """
    Last `n` characters of a log file, or "" if it cannot be read.

    Used to capture a short failure excerpt for the results database without
    storing an entire build log. Deliberately swallows read errors: a missing
    or unreadable log must not abort the run that produced it.
    """
    try:
        t = path.read_text(encoding="utf-8", errors="ignore").strip()
        return t[-n:] if t else ""
    except Exception:
        return ""


def _venv_python(venv_dir: Path) -> str:
    """
    Path to the Python interpreter inside `venv_dir`, per platform.
    """
    return str(venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


_TEST_EXTRA_NAMES = ("test", "tests", "testing", "dev", "develop", "development")
_TEST_REQ_FILES = ("requirements-dev.txt", "requirements-test.txt", "requirements_dev.txt",
                   "requirements_test.txt", "test-requirements.txt", "dev-requirements.txt")


def _declared_test_extras(py_root: Path) -> list[str]:
    """Extras declared in pyproject.toml [project.optional-dependencies] that
    look like test/dev extras."""
    pyproject = py_root / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            return []
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    extras = ((data.get("project") or {}).get("optional-dependencies") or {})
    return [k for k in extras if k.lower() in _TEST_EXTRA_NAMES]


def _install_test_extras(py_root: Path, venv_dir: Path) -> None:
    """
    Best-effort install of the project's *declared* test/dev dependencies.

    Without this, a project whose test deps live in an extra (pytest-asyncio,
    pytest-mock, responses, ...) fails at pytest collection with exit code 2
    in under a second — and the pipeline would previously record that as
    "project already failed its execution BEFORE the update", blaming the
    project for our own incomplete environment. Failures here are ignored on
    purpose: these are optional, and an install error must not be mistaken
    for project breakage.
    """
    py = _venv_python(venv_dir)
    env = sx._safe_env(venv_dir=venv_dir)

    for extra in _declared_test_extras(py_root)[:3]:
        try:
            subprocess.run([py, "-m", "pip", "install", "--quiet", "--prefer-binary", f".[{extra}]"],
                           cwd=str(py_root), capture_output=True, timeout=300, env=env)
        except Exception:
            pass

    # Every dev/test requirements file, not just the first match: projects
    # routinely split them (requirements-test.txt for the runner, -dev.txt for
    # everything else), and stopping at the first one leaves pytest unable to
    # import the test modules. 38% of our baseline execution failures are
    # collection errors, which is overwhelmingly this.
    for fname in _TEST_REQ_FILES:
        if (py_root / fname).exists():
            try:
                subprocess.run([py, "-m", "pip", "install", "--quiet", "--prefer-binary", "-r", fname],
                               cwd=str(py_root), capture_output=True, timeout=300, env=env)
            except Exception:
                pass

    # Same files one directory down (requirements/dev.txt, requirements/test.txt).
    req_dir = py_root / "requirements"
    if req_dir.is_dir():
        for cand in ("dev.txt", "test.txt", "testing.txt", "local.txt"):
            if (req_dir / cand).exists():
                try:
                    subprocess.run([py, "-m", "pip", "install", "--quiet", "--prefer-binary",
                                    "-r", f"requirements/{cand}"],
                                   cwd=str(py_root), capture_output=True, timeout=300, env=env)
                except Exception:
                    pass


def run_snapshot(
    repo: str,
    pr_number: int,
    snapshot: str,             # "BEFORE" | "AFTER"
    sha: str,
    py_bin: str,
    work_root: Path,
    log_dir: Path,
    exec_plan_override: Optional[ExecutionPlan] = None,
    protected_dep: Optional[str] = None,
) -> dict:
    """
    Returns:
      install_result: PASS | FAIL | TIMEOUT
      install_excerpt: str
      execution_strategy: pytest_real | import_smoke | none
      execution_detail: str
      execution_result: PASS | FAIL | TIMEOUT | NOT_RUN
      execution_excerpt: str
      failure_stage: CHECKOUT | VENV | INSTALL | EXECUTION | None
      duration_seconds: float
      exec_plan: ExecutionPlan | None   (pass to the AFTER call so both
                                          snapshots use the identical
                                          exercise mechanism)
    """
    safe_repo = repo.replace("/", "__")
    work_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=f"{safe_repo}_pr{pr_number}_{snapshot}_", dir=str(work_root)))
    venv_dir = work_dir / "_venv"
    log_dir.mkdir(parents=True, exist_ok=True)

    res = {
        "install_result": "FAIL", "install_excerpt": "",
        "execution_strategy": "none", "execution_detail": "",
        "execution_result": "NOT_RUN", "execution_excerpt": "",
        "evidence_strength": "none",
        "failure_stage": None, "duration_seconds": 0.0,
        "exec_plan": None,
        "tests": {},          # node_id -> pass | fail | error | skip
        "platform_incompatible": False,
    }
    start_t = time.time()
    try:
        # ── 1. Checkout (serialised per repo — shared bare cache) ───────
        with repo_lock(repo):
            ok, err = gf.checkout_snapshot_worktree(repo, sha, work_dir)
        if not ok:
            res["failure_stage"] = "CHECKOUT"
            res["install_excerpt"] = f"git checkout failed: {err}"
            return res

        # ── 2. Fresh isolated venv ──────────────────────────────────────
        try:
            # 180s (was 60s): under 3 concurrent workers the disk I/O for
            # venv creation contends heavily and 60s produced spurious
            # RUNTIME_UNAVAILABLE rejections that had nothing to do with the
            # project under test.
            v_proc = subprocess.run([py_bin, "-m", "venv", str(venv_dir)],
                                     capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=180)
            if v_proc.returncode != 0:
                res["failure_stage"] = "VENV"
                res["install_excerpt"] = f"venv creation failed: {v_proc.stderr[:300]}"
                return res
        except Exception as e:
            res["failure_stage"] = "VENV"
            res["install_excerpt"] = f"venv creation exception: {e}"
            return res

        # ── 3. Install project dependencies ─────────────────────────────
        iplan = plan_install(work_dir)
        if iplan is None:
            # No requirements file and no package manifest anywhere. Previously
            # this fell through to a bogus `pip install .`, which failed and
            # was blamed on the project. Report it honestly instead.
            res["install_result"] = "NO_PLAN"
            res["failure_stage"] = "INSTALL"
            res["install_excerpt"] = ("No installable manifest found (no requirements*.txt, "
                                       "pyproject.toml, setup.py, Pipfile or poetry.lock)")
            return res

        slug = f"{safe_repo}__pr{pr_number}__{snapshot}__INSTALL"

        # A failed install is retried once when it looks like the package index
        # was unreachable. These faults are transient and cluster in bursts, so
        # a single extra attempt after a short pause recovers most of them —
        # and every one recovered is a candidate that would otherwise have been
        # libelled as an already-broken project.
        for attempt in range(2):
            install_sr = sx.run_stage(
                stage_name="INSTALL", command=iplan.command, work_dir=work_dir,
                stdout_path=log_dir / f"{slug}.stdout.txt", stderr_path=log_dir / f"{slug}.stderr.txt",
                # EXEC_TIMEOUT_INSTALL, not EXEC_TIMEOUT_TOTAL: config defines a
                # dedicated install budget and this call site was overriding it
                # with the per-snapshot wall-clock limit, doubling the cost of
                # every hung install (and run_stage retries once on top).
                timeout=C.EXEC_TIMEOUT_INSTALL, venv_dir=venv_dir,
            )
            if install_sr["exit_code"] == 0 or install_sr["result"] == "TIMEOUT":
                break
            peek = _tail(Path(install_sr["stderr_path"])) or _tail(Path(install_sr["stdout_path"]))
            if not _is_index_unreachable(peek) or attempt == 1:
                break
            time.sleep(20)

        if install_sr["exit_code"] != 0:
            excerpt = _tail(Path(install_sr["stderr_path"])) or _tail(Path(install_sr["stdout_path"]))
            res["install_excerpt"] = excerpt
            res["failure_stage"] = "INSTALL"
            if install_sr["result"] == "TIMEOUT":
                res["install_result"] = "TIMEOUT"
            elif _is_index_unreachable(excerpt):
                # Our connectivity, not the project. Must never be reported as
                # baseline breakage, and must never become a TIER_2 install
                # regression when it happens on the AFTER snapshot.
                res["install_result"] = "NETWORK"
            elif _is_not_packageable(excerpt):
                # The repo has a manifest but is not actually pip-installable
                # (flat layout setuptools can't auto-discover, etc). CI never
                # ran `pip install .` on these — it used requirements/tox — so
                # this is our method failing, not the project being broken.
                res["install_result"] = "NO_PLAN"
            else:
                res["install_result"] = "FAIL"
            return res
        res["install_result"] = "PASS"

        # Same directory the install actually ran in (e.g. a "backend/" subdir)
        # — tests and importable packages must be looked for there too.
        py_root = iplan.project_dir

        # Ensure the *harness* being missing is never mistaken for the
        # project being broken (many repos don't declare pytest themselves).
        subprocess.run(
            [_venv_python(venv_dir), "-m", "pip", "install", "--quiet", "--prefer-binary", "pytest"],
            capture_output=True, timeout=120,
        )
        _install_test_extras(py_root, venv_dir)

        # ── 4. Determine / reuse execution plan ─────────────────────────
        exec_plan = exec_plan_override or detect_execution_plan(py_root, venv_dir)
        res["exec_plan"] = exec_plan
        res["execution_strategy"] = exec_plan.strategy
        res["execution_detail"] = exec_plan.detail
        res["evidence_strength"] = getattr(exec_plan, "evidence_strength", "strong")

        if exec_plan.strategy == "none":
            res["execution_result"] = "NOT_RUN"
            return res

        slug_x = f"{safe_repo}__pr{pr_number}__{snapshot}__EXEC"
        stdout_x = log_dir / f"{slug_x}.stdout.txt"
        stderr_x = log_dir / f"{slug_x}.stderr.txt"

        # ── 5a. Real test suite ──────────────────────────────────────────
        if exec_plan.strategy == "pytest_real":
            junit_path = log_dir / f"{slug_x}.junit.xml"
            pytest_cmd = f'python -m pytest -q -p no:cacheprovider --junitxml="{junit_path.as_posix()}"'
            # -p no:cacheprovider keeps pytest from writing .pytest_cache into
            # the worktree, which would differ between the two snapshots.
            exec_sr = sx.run_stage(
                stage_name="EXEC", command=pytest_cmd, work_dir=py_root,
                stdout_path=stdout_x, stderr_path=stderr_x,
                timeout=C.EXEC_TIMEOUT_TOTAL, venv_dir=venv_dir,
            )

            # Heal a collection-time failure caused by a missing support package
            # (pytest exit code 2 = collection error), then re-run. See
            # _heal_missing_test_deps for why this cannot create a false
            # regression. Bounded to 3 rounds so a genuinely unsatisfiable
            # import can never loop.
            healed: set = set()
            for _round in range(3):
                if exec_sr["exit_code"] != 2 or exec_sr["result"] == "TIMEOUT":
                    break
                text = _tail(Path(exec_sr["stderr_path"]), 4000) + "\n" + _tail(Path(exec_sr["stdout_path"]), 4000)
                missing = _missing_modules(text)
                platform_gap = [m for m in missing if m in _UNIX_ONLY_STDLIB]
                if platform_gap:
                    res["platform_incompatible"] = True
                    res["execution_excerpt"] = (f"test collection requires Unix-only module(s) "
                                                 f"unavailable on this OS: {', '.join(platform_gap)}")
                    break
                to_try = [m for m in missing if m not in healed and "." not in m]
                if not to_try:
                    break
                attempted = _heal_missing_test_deps(py_root, venv_dir, to_try, protected_dep, healed)
                healed.update(attempted)
                if not attempted:
                    break
                exec_sr = sx.run_stage(
                    stage_name="EXEC", command=pytest_cmd, work_dir=py_root,
                    stdout_path=stdout_x, stderr_path=stderr_x,
                    timeout=C.EXEC_TIMEOUT_TOTAL, venv_dir=venv_dir,
                )

            passed = exec_sr["exit_code"] == 0
            res["execution_result"] = "TIMEOUT" if exec_sr["result"] == "TIMEOUT" else ("PASS" if passed else "FAIL")
            res["tests"] = parse_junit(junit_path)
            if not passed:
                res["failure_stage"] = "EXECUTION"
                res["execution_excerpt"] = res.get("execution_excerpt") or (
                    _tail(Path(exec_sr["stderr_path"])) or _tail(Path(exec_sr["stdout_path"])))

        # ── 5b. Import-smoke fallback ────────────────────────────────────
        elif exec_plan.strategy == "import_smoke":
            failures = []
            env = sx._safe_env(venv_dir=venv_dir)
            for target in exec_plan.import_targets:
                p = subprocess.run(
                    [_venv_python(venv_dir), "-c", f"import {target}"],
                    cwd=str(py_root), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60, env=env,
                )
                if p.returncode != 0:
                    failures.append((target, (p.stderr or p.stdout or "").strip()[-400:]))
            stdout_x.write_text(f"import targets attempted: {exec_plan.import_targets}\n", encoding="utf-8")
            if failures:
                res["execution_result"] = "FAIL"
                res["failure_stage"] = "EXECUTION"
                res["execution_excerpt"] = "\n".join(f"{t}: {e}" for t, e in failures)[-800:]
                stderr_x.write_text(res["execution_excerpt"], encoding="utf-8")
            else:
                res["execution_result"] = "PASS"

    finally:
        res["duration_seconds"] = round(time.time() - start_t, 2)
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

    return res
