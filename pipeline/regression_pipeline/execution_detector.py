"""
execution_detector.py — Determine a meaningful execution strategy for a
checked-out Python project.

"Meaningful execution" means a command that actually exercises the project's
code, not merely a command that happens to exit 0. Two strategies:

  1. pytest_real  — a real test suite exists AND pytest can actually collect
                     at least one test item from it (verified via
                     `pytest --collect-only`, not assumed from a directory name).
  2. import_smoke — no usable test suite; fall back to importing every
                     top-level importable module/package. This still executes
                     real code (import-time execution) and catches broken
                     APIs, removed symbols, incompatible signatures, etc. —
                     the most common way a dependency bump breaks a project
                     that has no tests.

If neither applies, "none" is returned. Per the research requirement, a
command that exits 0 without exercising anything (e.g. pytest collecting zero
tests) must NEVER be treated as proof the project works — callers must
exclude "none" cases from the confirmed dataset while still recording them.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import sandbox_executor as sx

_TEST_DIR_NAMES = {"tests", "test"}
_SKIP_DIR_NAMES = {
    ".git", ".github", "__pycache__", "_venv", "venv", ".venv", "env",
    "node_modules", "build", "dist", ".tox", ".pytest_cache", ".mypy_cache",
    "site-packages", "docs", "examples", "example",
}


@dataclass
class ExecutionPlan:
    strategy: str                          # "pytest_real" | "import_smoke" | "none"
    command: Optional[str]                 # shell command (pytest_real only)
    detail: str                            # human-readable justification
    import_targets: Optional[list] = None  # import_smoke only
    # "strong" = a real test suite actually exercised the project.
    # "weak"   = import-smoke only; a passing import is a low bar (it proves
    #            the package imports, not that it works), though a *failing*
    #            import after an update is still solid breakage evidence.
    #            Recorded so the dataset can separate the two in analysis.
    evidence_strength: str = "strong"


def _venv_python(venv_dir: Path) -> str:
    return str(venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def _iter_py_files(root: Path):
    for p in root.rglob("*.py"):
        try:
            rel_parts = p.relative_to(root).parts[:-1]
        except ValueError:
            continue
        if any(part in _SKIP_DIR_NAMES for part in rel_parts):
            continue
        yield p


def _count_pytest_collected(work_dir: Path, venv_dir: Path, timeout: int = 90) -> int:
    """Run `pytest --collect-only -q` and count collected test items.
    Returns -1 if collection itself errored (untrustworthy, don't use pytest_real)."""
    py_venv = _venv_python(venv_dir)
    try:
        proc = subprocess.run(
            [py_venv, "-m", "pytest", "--collect-only", "-q"],
            cwd=str(work_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env=sx._safe_env(venv_dir=venv_dir),
        )
    except Exception:
        return -1

    out = proc.stdout or ""
    m = re.search(r"(\d+)\s+tests?\s+collected", out)
    if m:
        return int(m.group(1))
    if "no tests collected" in out.lower() or "no tests ran" in out.lower():
        return 0
    if proc.returncode not in (0, 5):
        return -1
    return 0


# Directories that are never "the project" for the purposes of proving the
# project works. Importing a repo's own `tests` package and calling that
# evidence of health is exactly the failure mode this research must avoid:
# a command that exits 0 without meaningfully exercising the project.
_NON_PROJECT_DIRS = {
    "tests", "test", "testing", "scripts", "script", "bin", "benchmarks",
    "bench", "docs", "doc", "examples", "example", "samples", "migrations",
    "fixtures", "data", "assets", "notebooks",
}


def _is_substantive_package(pkg_dir: Path) -> bool:
    """
    True if a package looks like it contains real code rather than being an
    empty namespace shell. An `import foo` against a package whose only
    content is a blank __init__.py executes essentially nothing, so it must
    not count as evidence the project works.
    """
    init = pkg_dir / "__init__.py"
    try:
        if any(p.name != "__init__.py" for p in pkg_dir.glob("*.py")):
            return True
        if any(p.is_dir() and (p / "__init__.py").exists() for p in pkg_dir.iterdir()):
            return True
        body = [
            ln.strip() for ln in init.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        return len(body) >= 3
    except Exception:
        return False


def _discover_import_targets(repo_root: Path) -> list[str]:
    """Top-level importable packages (dirs with __init__.py) and standalone
    modules at repo root, excluding test/tooling scaffolding and empty
    namespace shells."""
    targets = []
    for child in sorted(repo_root.iterdir()):
        if child.name.startswith(".") or child.name in _SKIP_DIR_NAMES:
            continue
        if child.name.lower() in _NON_PROJECT_DIRS:
            continue
        if child.is_dir() and (child / "__init__.py").exists():
            if _is_substantive_package(child):
                targets.append(child.name)
        elif child.is_file() and child.suffix == ".py":
            if child.stem in ("setup", "conftest", "manage", "noxfile", "tasks"):
                continue
            if child.stem.startswith("test_") or child.stem.endswith("_test"):
                continue
            try:
                if child.stat().st_size < 200:   # trivial stub module
                    continue
            except Exception:
                continue
            targets.append(child.stem)
    return targets


def detect_execution_plan(repo_root: Path, venv_dir: Path) -> ExecutionPlan:
    has_test_signal = (
        any((repo_root / d).exists() for d in _TEST_DIR_NAMES)
        or any(f.name.startswith("test_") or f.name.endswith("_test.py") for f in _iter_py_files(repo_root))
    )
    if has_test_signal:
        n = _count_pytest_collected(repo_root, venv_dir)
        if n > 0:
            return ExecutionPlan("pytest_real", "python -m pytest -q", f"{n} tests collected",
                                  evidence_strength="strong")
        # n == 0 (empty/stub tests) or -1 (collection errored) -> fall through

    targets = _discover_import_targets(repo_root)
    if targets:
        return ExecutionPlan(
            "import_smoke", None,
            f"{len(targets)} top-level modules: {', '.join(targets[:5])}",
            import_targets=targets,
            evidence_strength="weak",
        )

    return ExecutionPlan("none", None, "no real test suite and no importable top-level package found",
                          evidence_strength="none")
