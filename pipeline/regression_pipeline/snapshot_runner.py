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


def _tail(path: Path, n: int = 800) -> str:
    try:
        t = path.read_text(encoding="utf-8", errors="ignore").strip()
        return t[-n:] if t else ""
    except Exception:
        return ""


def _venv_python(venv_dir: Path) -> str:
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

    for extra in _declared_test_extras(py_root)[:2]:
        try:
            subprocess.run([py, "-m", "pip", "install", "--quiet", "--prefer-binary", f".[{extra}]"],
                           cwd=str(py_root), capture_output=True, timeout=300, env=env)
        except Exception:
            pass

    for fname in _TEST_REQ_FILES:
        req = py_root / fname
        if req.exists():
            try:
                subprocess.run([py, "-m", "pip", "install", "--quiet", "--prefer-binary", "-r", fname],
                               cwd=str(py_root), capture_output=True, timeout=300, env=env)
            except Exception:
                pass
            break


def run_snapshot(
    repo: str,
    pr_number: int,
    snapshot: str,             # "BEFORE" | "AFTER"
    sha: str,
    py_bin: str,
    work_root: Path,
    log_dir: Path,
    exec_plan_override: Optional[ExecutionPlan] = None,
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
        install_sr = sx.run_stage(
            stage_name="INSTALL", command=iplan.command, work_dir=work_dir,
            stdout_path=log_dir / f"{slug}.stdout.txt", stderr_path=log_dir / f"{slug}.stderr.txt",
            timeout=C.EXEC_TIMEOUT_TOTAL, venv_dir=venv_dir,
        )
        if install_sr["exit_code"] != 0:
            excerpt = _tail(Path(install_sr["stderr_path"])) or _tail(Path(install_sr["stdout_path"]))
            res["install_excerpt"] = excerpt
            res["failure_stage"] = "INSTALL"
            if install_sr["result"] == "TIMEOUT":
                res["install_result"] = "TIMEOUT"
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
            exec_sr = sx.run_stage(
                stage_name="EXEC", command="python -m pytest -q", work_dir=py_root,
                stdout_path=stdout_x, stderr_path=stderr_x,
                timeout=C.EXEC_TIMEOUT_TOTAL, venv_dir=venv_dir,
            )
            passed = exec_sr["exit_code"] == 0
            res["execution_result"] = "TIMEOUT" if exec_sr["result"] == "TIMEOUT" else ("PASS" if passed else "FAIL")
            if not passed:
                res["failure_stage"] = "EXECUTION"
                res["execution_excerpt"] = _tail(Path(exec_sr["stderr_path"])) or _tail(Path(exec_sr["stdout_path"]))

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
