"""
ecosystem_adapters.py
──────────────────────
Per-ecosystem logic for:
  1. Detecting which runtime version to use (from CI config / README)
  2. Building a list of (stage_name, command) tuples
  3. Recognising the manifest/lockfile format

Each adapter returns a list of ExecutionPlan namedtuples.
"""

from __future__ import annotations
import re
import yaml
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Stage:
    name: str           # INSTALL / BUILD / TEST / etc.
    command: str
    source: str         # where this command was inferred from
    confidence: str     # high / medium / low
    timeout: int = 900  # seconds


@dataclass
class ExecutionPlan:
    ecosystem: str
    runtime_version: Optional[str]
    runtime_source: str
    stages: list[Stage] = field(default_factory=list)
    notes: list[str]    = field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _find_ci_workflows(repo_root: Path) -> list[Path]:
    wf_dir = repo_root / ".github" / "workflows"
    if not wf_dir.exists():
        return []
    return list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))


def _extract_node_version(text: str) -> Optional[str]:
    """Extract node-version from CI config text."""
    m = re.search(r"node-version['\"]?\s*[:=]\s*['\"]?([0-9.x*]+)", text)
    return m.group(1) if m else None


def _extract_python_version(text: str) -> Optional[str]:
    m = re.search(r"python-version['\"]?\s*[:=]\s*['\"]?([0-9.]+)", text)
    return m.group(1) if m else None


def _extract_java_version(text: str) -> Optional[str]:
    m = re.search(r"java-version['\"]?\s*[:=]\s*['\"]?([0-9.]+)", text)
    return m.group(1) if m else None


def _workflow_commands(wf_path: Path) -> list[str]:
    """Extract 'run:' commands from a GitHub Actions workflow file."""
    text = _read_text(wf_path)
    try:
        data = yaml.safe_load(text)
    except Exception:
        # Fallback: grep for run: lines
        return re.findall(r"run:\s*\|?\s*(.+)", text)

    cmds = []
    jobs = (data or {}).get("jobs") or {}
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for step in (job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if run:
                cmds.extend(run.strip().splitlines())
    return [c.strip() for c in cmds if c.strip()]


# ─── npm adapter ──────────────────────────────────────────────────────────────

def plan_npm(repo_root: Path) -> ExecutionPlan:
    plan = ExecutionPlan(ecosystem="npm", runtime_version=None, runtime_source="default")
    node_version = None

    # Try workflows
    for wf in _find_ci_workflows(repo_root):
        text = _read_text(wf)
        node_version = node_version or _extract_node_version(text)
        cmds = _workflow_commands(wf)
        if cmds:
            plan.notes.append(f"CI commands from {wf.name}: {cmds[:5]}")

    # Read package.json scripts
    pkg_json = repo_root / "package.json"
    test_script = None
    build_script = None
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts") or {}
            test_script  = scripts.get("test")
            build_script = scripts.get("build") or scripts.get("compile")
        except Exception:
            pass

    # Node version fallback from .nvmrc / .node-version
    for nvf in [".nvmrc", ".node-version"]:
        nvpath = repo_root / nvf
        if nvpath.exists():
            node_version = _read_text(nvpath).strip()
            plan.runtime_source = nvf
            break

    plan.runtime_version = node_version or "lts"

    # Choose install command
    if (repo_root / "pnpm-lock.yaml").exists():
        install_cmd = "pnpm install --frozen-lockfile"
    elif (repo_root / "yarn.lock").exists():
        install_cmd = "yarn install --frozen-lockfile"
    elif (repo_root / "package-lock.json").exists():
        install_cmd = "npm ci"
    else:
        install_cmd = "npm install"

    plan.stages = [
        Stage("INSTALL", install_cmd, "package_manager_detection", "high"),
    ]
    if build_script:
        plan.stages.append(Stage("BUILD", f"npm run build", "package_json_scripts", "medium"))
    if test_script:
        plan.stages.append(Stage("TEST", f"npm test", "package_json_scripts", "high"))
    else:
        plan.stages.append(Stage("TEST", "npm test", "default_npm", "low"))

    return plan


# ─── pip/Python adapter ───────────────────────────────────────────────────────

def plan_pip(repo_root: Path) -> ExecutionPlan:
    plan = ExecutionPlan(ecosystem="pip", runtime_version=None, runtime_source="default")
    python_version = None

    for wf in _find_ci_workflows(repo_root):
        text = _read_text(wf)
        python_version = python_version or _extract_python_version(text)

    # .python-version
    pv_file = repo_root / ".python-version"
    if pv_file.exists():
        python_version = _read_text(pv_file).strip()
        plan.runtime_source = ".python-version"

    plan.runtime_version = python_version or "3.x"

    # Determine install command
    if (repo_root / "poetry.lock").exists() or (repo_root / "pyproject.toml").exists():
        install_cmd = "pip install poetry && poetry install --no-interaction"
    elif (repo_root / "Pipfile.lock").exists():
        install_cmd = "pip install pipenv && pipenv install --dev"
    elif (repo_root / "requirements.txt").exists():
        install_cmd = "pip install -r requirements.txt"
    else:
        install_cmd = "pip install -e ."

    # Test command priority: tox > pytest > unittest
    test_cmd = None
    if (repo_root / "tox.ini").exists():
        test_cmd = "tox"
        plan.notes.append("tox.ini found")
    elif (repo_root / "noxfile.py").exists():
        test_cmd = "nox"
        plan.notes.append("noxfile.py found")
    else:
        # Check pyproject.toml [tool.pytest]
        ppt = repo_root / "pyproject.toml"
        if ppt.exists() and "pytest" in _read_text(ppt):
            test_cmd = "pytest"
        else:
            test_cmd = "python -m pytest || python -m unittest discover"

    plan.stages = [
        Stage("INSTALL", install_cmd, "manifest_detection", "high"),
        Stage("TEST",    test_cmd,    "test_runner_detection", "medium"),
    ]
    return plan


# ─── Maven adapter ────────────────────────────────────────────────────────────

def plan_maven(repo_root: Path) -> ExecutionPlan:
    plan = ExecutionPlan(ecosystem="maven", runtime_version=None, runtime_source="default")
    java_version = None

    for wf in _find_ci_workflows(repo_root):
        text = _read_text(wf)
        java_version = java_version or _extract_java_version(text)

    plan.runtime_version = java_version or "11"
    plan.stages = [
        Stage("INSTALL", "mvn dependency:resolve -q", "maven_default", "high"),
        Stage("BUILD",   "mvn compile -q",            "maven_default", "high"),
        Stage("TEST",    "mvn test -q",               "maven_default", "high"),
    ]
    return plan


# ─── Gradle adapter ───────────────────────────────────────────────────────────

def plan_gradle(repo_root: Path) -> ExecutionPlan:
    plan = ExecutionPlan(ecosystem="gradle", runtime_version=None, runtime_source="default")
    java_version = None

    for wf in _find_ci_workflows(repo_root):
        text = _read_text(wf)
        java_version = java_version or _extract_java_version(text)

    plan.runtime_version = java_version or "11"
    wrapper = repo_root / "gradlew"
    gradle_cmd = "./gradlew" if wrapper.exists() else "gradle"

    plan.stages = [
        Stage("INSTALL", f"{gradle_cmd} dependencies --quiet", "gradle_default", "high"),
        Stage("BUILD",   f"{gradle_cmd} assemble --quiet",     "gradle_default", "high"),
        Stage("TEST",    f"{gradle_cmd} test",                 "gradle_default", "high"),
    ]
    return plan


# ─── Go adapter ───────────────────────────────────────────────────────────────

def plan_go(repo_root: Path) -> ExecutionPlan:
    plan = ExecutionPlan(ecosystem="go", runtime_version=None, runtime_source="default")

    gomod = repo_root / "go.mod"
    if gomod.exists():
        m = re.search(r"^go\s+(\S+)", _read_text(gomod), re.M)
        if m:
            plan.runtime_version = m.group(1)
            plan.runtime_source = "go.mod"

    plan.stages = [
        Stage("INSTALL", "go mod download",  "go_default", "high"),
        Stage("BUILD",   "go build ./...",   "go_default", "high"),
        Stage("TEST",    "go test ./...",    "go_default", "high"),
    ]
    return plan


# ─── Cargo/Rust adapter ───────────────────────────────────────────────────────

def plan_cargo(repo_root: Path) -> ExecutionPlan:
    plan = ExecutionPlan(ecosystem="cargo", runtime_version=None, runtime_source="default")

    rtf = repo_root / "rust-toolchain.toml"
    rtf2 = repo_root / "rust-toolchain"
    for p in [rtf, rtf2]:
        if p.exists():
            plan.runtime_version = _read_text(p).strip()
            plan.runtime_source = str(p.name)
            break

    plan.stages = [
        Stage("INSTALL", "cargo fetch",     "cargo_default", "high"),
        Stage("BUILD",   "cargo build",     "cargo_default", "high"),
        Stage("TEST",    "cargo test",      "cargo_default", "high"),
    ]
    return plan


# ─── Gem/Ruby adapter ─────────────────────────────────────────────────────────

def plan_gem(repo_root: Path) -> ExecutionPlan:
    plan = ExecutionPlan(ecosystem="gem", runtime_version=None, runtime_source="default")

    rbv = repo_root / ".ruby-version"
    if rbv.exists():
        plan.runtime_version = _read_text(rbv).strip()
        plan.runtime_source = ".ruby-version"

    plan.stages = [
        Stage("INSTALL", "bundle install", "gem_default", "high"),
        Stage("TEST",    "bundle exec rspec || bundle exec rake spec || bundle exec rake test",
              "gem_default", "medium"),
    ]
    return plan


# ─── Dispatch ─────────────────────────────────────────────────────────────────

_ADAPTERS = {
    "npm":           plan_npm,
    "pip":           plan_pip,
    "maven":         plan_maven,
    "gradle":        plan_gradle,
    "go":            plan_go,
    "cargo":         plan_cargo,
    "gem":           plan_gem,
}


def get_execution_plan(ecosystem: str, repo_root: Path) -> Optional[ExecutionPlan]:
    adapter = _ADAPTERS.get((ecosystem or "").lower())
    if adapter is None:
        return None
    try:
        return adapter(repo_root)
    except Exception as e:
        return None
