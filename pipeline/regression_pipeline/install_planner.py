"""
install_planner.py — Work out how a Python project actually installs.

Replaces ecosystem_adapters.plan_pip for this pipeline. That function ends
with an unconditional `pip install .` fallback, which fails instantly on any
repo that is an *application* rather than an installable package:

    ERROR: Directory '.' is not installable.
           Neither 'setup.py' nor 'pyproject.toml' found.

86 of 152 baseline install failures (57%) were that exact message, and each
one was recorded as "project did not install BEFORE the update — already
broken", blaming the project for a command we should never have run. A
further 18 (12%) failed because the interpreter we picked violated the
project's own requires-python.

Rules here:
  * `pip install .` is emitted ONLY when a real package manifest exists.
  * requirements files are searched broadly (root, common subdirs, and a
    requirements/ directory) before giving up.
  * if neither exists we return None, so the caller can record an honest
    "no install plan" instead of manufacturing a failure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_SUBDIRS = ["backend", "api", "server", "src", "app", "service", "python", "web", "worker"]
_REQ_DIR_NAMES = ["requirements", "reqs", "deps"]
_PRIMARY_REQ = ["requirements.txt", "requirements-base.txt", "base.txt", "prod.txt", "main.txt"]
_EXTRA_REQ = ["requirements-dev.txt", "requirements-test.txt", "dev-requirements.txt",
              "test-requirements.txt", "requirements_dev.txt", "requirements_test.txt",
              "dev.txt", "test.txt"]

# "python -m pip", never bare "pip": if a requirements file pins pip itself,
# the pip.exe wrapper cannot overwrite its own running executable on Windows
# and dies with "ERROR: To modify pip, please run the following command...".
PIP = "python -m pip install --prefer-binary"


@dataclass
class InstallPlan:
    command: str
    project_dir: Path
    kind: str                      # "requirements" | "package" | "poetry" | "pipenv"
    requires_python: Optional[str] = None


def _is_installable_package(d: Path) -> bool:
    if (d / "setup.py").exists() or (d / "pyproject.toml").exists():
        return True
    cfg = d / "setup.cfg"
    if cfg.exists():
        try:
            t = cfg.read_text(encoding="utf-8", errors="replace")
            return "[metadata]" in t or "[options]" in t
        except Exception:
            return False
    return False


def _find_requirements(d: Path) -> list[str]:
    """Return requirements files (relative to d), primary first."""
    found = []
    for name in _PRIMARY_REQ:
        if (d / name).exists():
            found.append(name)
            break
    for rd in _REQ_DIR_NAMES:
        sub = d / rd
        if sub.is_dir():
            for name in _PRIMARY_REQ:
                if (sub / name).exists():
                    found.append(f"{rd}/{name}")
                    break
            break
    if not found:
        for p in sorted(d.glob("requirements*.txt")):
            found.append(p.name)
            break
    for name in _EXTRA_REQ:
        if (d / name).exists():
            found.append(name)
            break
    return found


def _read_requires_python(d: Path) -> Optional[str]:
    """Extract requires-python so we don't run a project on an interpreter it
    explicitly excludes (the 'Requires-Python >=3.7,<3.11' failures)."""
    pp = d / "pyproject.toml"
    if pp.exists():
        try:
            m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']',
                          pp.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1)
        except Exception:
            pass
    for fname in ("setup.py", "setup.cfg"):
        f = d / fname
        if f.exists():
            try:
                m = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']',
                              f.read_text(encoding="utf-8", errors="replace"))
                if m:
                    return m.group(1)
            except Exception:
                pass
    return None


def _candidate_dirs(repo_root: Path) -> list[Path]:
    dirs = [repo_root]
    for s in _SUBDIRS:
        p = repo_root / s
        if p.is_dir():
            dirs.append(p)
    return dirs


def plan_install(repo_root: Path) -> Optional[InstallPlan]:
    """Return an InstallPlan, or None when the repo offers no usable way to
    install dependencies (caller should record NO_INSTALL_PLAN, not a failure)."""
    for d in _candidate_dirs(repo_root):
        rel = "" if d == repo_root else d.relative_to(repo_root).as_posix()
        prefix = f"cd {rel} && " if rel else ""
        req_python = _read_requires_python(d)

        if (d / "poetry.lock").exists() or (
            (d / "pyproject.toml").exists()
            and "[tool.poetry]" in (d / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        ):
            cmd = "python -m pip install poetry && (poetry install --no-root --no-interaction || poetry install --no-interaction)"
            return InstallPlan(prefix + cmd, d, "poetry", req_python)

        if (d / "Pipfile.lock").exists() or (d / "Pipfile").exists():
            return InstallPlan(prefix + "python -m pip install pipenv && pipenv install --dev --system", d, "pipenv", req_python)

        reqs = _find_requirements(d)
        if reqs:
            cmd = " && ".join(f"{PIP} -r {r}" for r in reqs)
            # Installing the package itself on top is a bonus, never required.
            if _is_installable_package(d):
                cmd += f" && ({PIP} . || echo 'package install skipped')"
            return InstallPlan(prefix + cmd, d, "requirements", req_python)

        if _is_installable_package(d):
            return InstallPlan(prefix + f"{PIP} .", d, "package", req_python)

    return None


def python_version_ok(requires_python: Optional[str], version: str) -> bool:
    """Cheap check of an interpreter 'X.Y' against a requires-python spec.
    Conservative: unparseable specs are treated as compatible."""
    if not requires_python:
        return True
    try:
        major, minor = (int(x) for x in version.split(".")[:2])
    except Exception:
        return True
    for clause in requires_python.split(","):
        clause = clause.strip()
        m = re.match(r'(>=|<=|==|!=|<|>|~=)\s*(\d+)(?:\.(\d+))?', clause)
        if not m:
            continue
        op, cmaj, cmin = m.group(1), int(m.group(2)), m.group(3)
        cmin = int(cmin) if cmin is not None else 0
        cur, ref = (major, minor), (cmaj, cmin)
        if op == ">=" and not cur >= ref:
            return False
        if op == ">" and not cur > ref:
            return False
        if op == "<=" and not cur <= ref:
            return False
        if op == "<" and not cur < ref:
            return False
        if op == "==" and cur != ref:
            return False
        if op == "!=" and cur == ref:
            return False
        if op == "~=" and not (cur >= ref and cur[0] == ref[0]):
            return False
    return True
