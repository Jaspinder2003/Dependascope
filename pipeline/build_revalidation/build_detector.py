"""
build_detector.py — Detect project directory and build configuration
══════════════════════════════════════════════════════════════════════
Scans a checked-out Python repository for project root (root or subdirectories)
and detects project build commands.

Build command rules:
  1. Standard `python -m build` (uses build isolation to respect project's declared build-system).
  2. `python setup.py build` for legacy setup.py.
  3. `make build` if Makefile with build target exists.
  4. `tox -e py` if tox.ini exists.
  5. Returns (project_dir, build_command, config_source) or (repo_root, None, None).
"""

from __future__ import annotations
import re
from pathlib import Path
from typing import Optional, Tuple

_SUBDIR_CANDIDATES = [
    "backend", "api", "server", "src", "app", "tools", "pipeline", "service", "python"
]

_BUILD_MANIFESTS = ["pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "poetry.lock", "requirements.txt"]


def find_project_root(repo_root: Path) -> Path:
    """
    Locate the actual Python project root directory.
    Checks repo_root first, then common subdirectories.
    """
    for m in _BUILD_MANIFESTS:
        if (repo_root / m).exists():
            return repo_root

    for sub in _SUBDIR_CANDIDATES:
        cand = repo_root / sub
        if cand.is_dir():
            for m in _BUILD_MANIFESTS:
                if (cand / m).exists():
                    return cand

    # 1-level deep directory scan
    for child in sorted(repo_root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            for m in _BUILD_MANIFESTS:
                if (child / m).exists():
                    return child

    return repo_root


def detect_build_config(repo_root: Path) -> Tuple[Path, Optional[str], Optional[str]]:
    """
    Detect project root and build configuration.

    Returns:
        (project_dir, build_command, config_source)
        If no build config found, returns (project_dir, None, None).
    """
    py_root = find_project_root(repo_root)

    # 1. pyproject.toml with [build-system]
    pyproject = py_root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            if "[build-system]" in text:
                return py_root, "pip install build && python -m build", "pyproject.toml"
        except Exception:
            pass

    # 2. setup.py
    setup_py = py_root / "setup.py"
    if setup_py.exists():
        return py_root, "pip install build && python -m build", "setup.py"

    # 3. setup.cfg with [metadata] or [options]
    setup_cfg = py_root / "setup.cfg"
    if setup_cfg.exists():
        try:
            text = setup_cfg.read_text(encoding="utf-8", errors="replace")
            if "[metadata]" in text or "[options]" in text:
                return py_root, "pip install build && python -m build", "setup.cfg"
        except Exception:
            pass

    # 4. Makefile with a 'build' target
    makefile = py_root / "Makefile"
    if not makefile.exists():
        makefile = py_root / "makefile"
    if makefile.exists():
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^build\s*:", text, re.MULTILINE):
                return py_root, "make build", "Makefile"
        except Exception:
            pass

    # 5. tox.ini
    tox_ini = py_root / "tox.ini"
    if tox_ini.exists():
        return py_root, "pip install tox && tox -e py", "tox.ini"

    return py_root, None, None
