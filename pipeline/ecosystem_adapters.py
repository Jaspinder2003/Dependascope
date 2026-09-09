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
    """
    One command in a project's build or test sequence.

    `source` records where the command was inferred from (a CI workflow, a
    manifest, or a built-in default) and `confidence` how reliable that
    inference is, so a plan assembled from weak guesses can be told apart from
    one read directly out of the project's own CI configuration.
    """
    name: str           # INSTALL / BUILD / TEST / etc.
    command: str
    source: str         # where this command was inferred from
    confidence: str     # high / medium / low
    timeout: int = 900  # seconds


@dataclass
class ExecutionPlan:
    """
    The full sequence of stages needed to build and test a project.

    Alongside the stages themselves it records the ecosystem and the runtime
    version the project expects, plus where that version was found. The runtime
    matters because both snapshots of a candidate must run on the same one for
    the comparison to be valid.
    """
    ecosystem: str
    runtime_version: Optional[str]
    runtime_source: str
    stages: list[Stage] = field(default_factory=list)
    notes: list[str]    = field(default_factory=list)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _read_text(path: Path) -> str:
    """
    Read a file as text, returning "" if it cannot be read.

    Detection walks over many optional files, most of which will not exist in
    any given repository, so an unreadable file is an expected outcome rather
    than an error worth propagating.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _find_ci_workflows(repo_root: Path) -> list[Path]:
    """
    Return the repository's GitHub Actions workflow files, if any.

    Workflows are the most reliable source of a project's real build and test
    commands, since they are what the maintainers actually run.
    """
    wf_dir = repo_root / ".github" / "workflows"
    if not wf_dir.exists():
        return []
    return list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))


def _extract_node_version(text: str) -> Optional[str]:
    """Extract node-version from CI config text."""
    m = re.search(r"node-version['\"]?\s*[:=]\s*['\"]?([0-9.x*]+)", text)
    return m.group(1) if m else None


def _extract_python_version(text: str) -> Optional[str]:
    """
    First Python version pinned in the given CI workflow text, or None.

    Note this takes the first match anywhere in the file. In a build matrix, or
    where a lint job pins an older interpreter, that may be an arbitrary choice
    among several supported versions, so the result is treated as a preference
    rather than a hard requirement.
    """
    m = re.search(r"python-version['\"]?\s*[:=]\s*['\"]?([0-9.]+)", text)
    return m.group(1) if m else None


def _extract_java_version(text: str) -> Optional[str]:
    """
    First Java version pinned in the given CI workflow text, or None.
    """
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

# Common subdirectory names where JS code lives in non-JS-primary repos
_NPM_SUBDIR_CANDIDATES = [
    "client", "frontend", "app/javascript", "app/assets/javascripts",
    "web", "ui", "static", "assets", "src",
]


def _find_npm_root(repo_root: Path) -> Path:
    """Return the directory that actually contains package.json.
    Checks repo root first, then common JS subdirectories."""
    if (repo_root / "package.json").exists():
        return repo_root
    for subdir in _NPM_SUBDIR_CANDIDATES:
        candidate = repo_root / subdir
        if (candidate / "package.json").exists():
            return candidate
    return repo_root  # fall back to root even if not found


def plan_npm(repo_root: Path) -> ExecutionPlan:
    """
    Build an execution plan for a Node project.

    Prefers commands and the Node version declared in CI workflows, falling
    back to the scripts declared in package.json and then to conventional
    defaults.
    """
    plan = ExecutionPlan(ecosystem="npm", runtime_version=None, runtime_source="default")
    node_version = None

    # Try workflows
    for wf in _find_ci_workflows(repo_root):
        text = _read_text(wf)
        node_version = node_version or _extract_node_version(text)
        cmds = _workflow_commands(wf)
        if cmds:
            plan.notes.append(f"CI commands from {wf.name}: {cmds[:5]}")

    # Find the actual JS root (may be a subdirectory for mixed-language repos)
    js_root = _find_npm_root(repo_root)
    pkg_json = js_root / "package.json"
    if not pkg_json.exists():
        # No package.json anywhere — mark as unrunnable by returning empty plan
        plan.notes.append("No package.json found at root or common subdirs")
        return plan

    if js_root != repo_root:
        plan.notes.append(f"package.json found in subdirectory: {js_root.name}")
        plan.runtime_source = f"subdir:{js_root.name}"

    # Read package.json scripts
    test_script = None
    build_script = None
    try:
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        scripts = pkg.get("scripts") or {}
        test_script  = scripts.get("test")
        build_script = scripts.get("build") or scripts.get("compile")
        # Skip repos whose test script is literally just an error placeholder
        if test_script and 'no test specified' in test_script.lower():
            test_script = None
    except Exception:
        pass

    # Node version fallback from .nvmrc / .node-version
    for nvf in [".nvmrc", ".node-version"]:
        nvpath = js_root / nvf
        if not nvpath.exists():
            nvpath = repo_root / nvf
        if nvpath.exists():
            node_version = _read_text(nvpath).strip()
            plan.runtime_source = nvf
            break

    plan.runtime_version = node_version or "lts"

    # Choose install command — prefer yarn if yarn.lock exists AND yarn is available
    # Fall back to npm install --legacy-peer-deps if yarn not found
    import shutil
    if (js_root / "pnpm-lock.yaml").exists():
        install_cmd = "pnpm install --frozen-lockfile"
    elif (js_root / "yarn.lock").exists():
        if shutil.which("yarn"):
            install_cmd = "yarn install --frozen-lockfile"
        else:
            # yarn.lock present but yarn not installed: use npm with legacy deps
            install_cmd = "npm install --legacy-peer-deps"
            plan.notes.append("yarn.lock found but yarn not installed; using npm install")
    elif (js_root / "package-lock.json").exists():
        install_cmd = "npm ci --legacy-peer-deps"
    else:
        install_cmd = "npm install --legacy-peer-deps"

    # If JS is in a subdirectory, prefix commands with cd into that subdir
    if js_root != repo_root:
        rel = js_root.relative_to(repo_root).as_posix()
        def _prefix(cmd: str) -> str:
            """
            Prefix a command so it runs in the project subdirectory, if there is one.

            Many repositories keep the project below the repository root, so commands
            must be run from there rather than from the top level.
            """
            return f"cd {rel} && {cmd}"
    else:
        def _prefix(cmd: str) -> str:
            """
            Prefix a command so it runs in the project subdirectory, if there is one.

            Many repositories keep the project below the repository root, so commands
            must be run from there rather than from the top level.
            """
            return cmd

    plan.stages = [
        Stage("INSTALL", _prefix(install_cmd), "package_manager_detection", "high"),
    ]
    # Skip BUILD — build tools (vsce, webpack, esbuild CLI, etc.) are rarely installed
    # globally and cause false FAIL->FAIL noise. The research signal is at INSTALL and TEST.
    if test_script:
        plan.stages.append(Stage("TEST", _prefix("npm test"), "package_json_scripts", "high"))
    # Don't add a test stage if there's no real test script — avoids guaranteed failures

    return plan


# ─── pip/Python adapter ───────────────────────────────────────────────────────

_PYTHON_SUBDIR_CANDIDATES = [
    "backend", "api", "server", "src", "app", "tools", "pipeline", "service", "python"
]


def _find_pip_root(repo_root: Path) -> Path:
    """Return directory containing Python manifest (requirements.txt, pyproject.toml, etc.)."""
    manifests = ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "poetry.lock"]
    for m in manifests:
        if (repo_root / m).exists():
            return repo_root
    for subdir in _PYTHON_SUBDIR_CANDIDATES:
        candidate = repo_root / subdir
        for m in manifests:
            if (candidate / m).exists():
                return candidate
    return repo_root


def plan_pip(repo_root: Path) -> ExecutionPlan:
    """
    Build an execution plan for a Python project.

    Prefers the interpreter version and commands declared in CI workflows,
    falling back to the project's manifests (requirements files, pyproject,
    Pipfile) and then to conventional defaults.
    """
    plan = ExecutionPlan(ecosystem="pip", runtime_version=None, runtime_source="default")
    python_version = None

    for wf in _find_ci_workflows(repo_root):
        text = _read_text(wf)
        python_version = python_version or _extract_python_version(text)

    py_root = _find_pip_root(repo_root)

    # .python-version
    pv_file = py_root / ".python-version"
    if not pv_file.exists():
        pv_file = repo_root / ".python-version"
    if pv_file.exists():
        python_version = _read_text(pv_file).strip()
        plan.runtime_source = ".python-version"

    plan.runtime_version = python_version or "3.x"

    # Determine install command
    # --prefer-binary: use pre-built wheels instead of building from source.
    # This is the biggest single speed improvement — source builds can take
    # 5-10 minutes while downloading a wheel takes seconds.
    PIP_FAST = "pip install --prefer-binary"

    has_poetry = (py_root / "poetry.lock").exists()
    if (py_root / "pyproject.toml").exists():
        pyproj_text = _read_text(py_root / "pyproject.toml")
        if "[tool.poetry]" in pyproj_text:
            has_poetry = True

    if has_poetry:
        install_cmd = "pip install poetry && (poetry install --no-root --no-interaction || poetry install --no-interaction)"
    elif (py_root / "Pipfile.lock").exists() or (py_root / "Pipfile").exists():
        install_cmd = "pip install pipenv && pipenv install --dev"
    elif (py_root / "requirements.txt").exists():
        install_cmd = f"{PIP_FAST} -r requirements.txt"
        req_dev = py_root / "requirements-dev.txt"
        if req_dev.exists():
            install_cmd += f" && {PIP_FAST} -r requirements-dev.txt"
    elif (py_root / "setup.py").exists() or (py_root / "pyproject.toml").exists():
        install_cmd = f"{PIP_FAST} ."
    else:
        install_cmd = f"{PIP_FAST} ."

    if py_root != repo_root:
        rel = py_root.relative_to(repo_root).as_posix()
        plan.notes.append(f"Python manifest found in subdirectory: {rel}")
        def _prefix(cmd: str) -> str:
            """
            Prefix a command so it runs in the project subdirectory, if there is one.

            Many repositories keep the project below the repository root, so commands
            must be run from there rather than from the top level.
            """
            return f"cd {rel} && {cmd}"
    else:
        def _prefix(cmd: str) -> str:
            """
            Prefix a command so it runs in the project subdirectory, if there is one.

            Many repositories keep the project below the repository root, so commands
            must be run from there rather than from the top level.
            """
            return cmd

    # Smart test command detection: tox > nox > pytest > unittest > none
    test_cmd = None
    has_pytest = (
        (py_root / "pytest.ini").exists() or (repo_root / "pytest.ini").exists() or
        (py_root / "setup.cfg").exists() or (repo_root / "setup.cfg").exists() or
        (py_root / "tests").exists() or (repo_root / "tests").exists() or
        (py_root / "test").exists() or (repo_root / "test").exists() or
        bool(list(py_root.glob("test_*.py"))) or bool(list(repo_root.glob("test_*.py")))
    )

    if (py_root / "tox.ini").exists() or (repo_root / "tox.ini").exists():
        test_cmd = "tox"
        plan.notes.append("tox.ini found")
    elif (py_root / "noxfile.py").exists() or (repo_root / "noxfile.py").exists():
        test_cmd = "nox"
        plan.notes.append("noxfile.py found")
    elif has_pytest:
        test_cmd = "python -m pytest"
    else:
        unittest_files = list(py_root.glob("**/test*.py")) or list(repo_root.glob("**/test*.py"))
        if unittest_files:
            test_cmd = "python -m unittest discover"
        else:
            test_cmd = None

    plan.stages = [
        Stage("INSTALL", _prefix(install_cmd), "manifest_detection", "high"),
    ]
    if test_cmd:
        plan.stages.append(Stage("TEST", _prefix(test_cmd), "test_runner_detection", "medium"))

    return plan


# ─── Maven adapter ────────────────────────────────────────────────────────────

def _find_pom_root(repo_root: Path) -> Path:
    """Return the directory containing pom.xml, searching root then one level deep."""
    if (repo_root / "pom.xml").exists():
        return repo_root
    # Search one level of subdirectories
    for child in sorted(repo_root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            if (child / "pom.xml").exists():
                return child
    return repo_root  # fall back even if not found


def plan_maven(repo_root: Path) -> ExecutionPlan:
    """
    Build an execution plan for a Maven project, including the Java version.
    """
    plan = ExecutionPlan(ecosystem="maven", runtime_version=None, runtime_source="default")
    java_version = None

    for wf in _find_ci_workflows(repo_root):
        text = _read_text(wf)
        java_version = java_version or _extract_java_version(text)

    plan.runtime_version = java_version or "11"

    # Find where pom.xml actually lives
    mvn_root = _find_pom_root(repo_root)
    if not (mvn_root / "pom.xml").exists():
        plan.notes.append("No pom.xml found at root or immediate subdirectories")
        return plan  # empty stages = UNRUNNABLE

    if mvn_root != repo_root:
        rel = mvn_root.relative_to(repo_root).as_posix()
        plan.notes.append(f"pom.xml found in subdirectory: {rel}")
        def _mvn(cmd: str) -> str:
            """
            Prefix a Maven command so it runs in the project subdirectory, if there is one.
            """
            return f"cd {rel} && {cmd}"
    else:
        def _mvn(cmd: str) -> str:
            """
            Prefix a Maven command so it runs in the project subdirectory, if there is one.
            """
            return cmd

    plan.stages = [
        Stage("INSTALL", _mvn("mvn dependency:resolve -q --no-transfer-progress"), "maven_default", "high"),
        Stage("BUILD",   _mvn("mvn compile -q --no-transfer-progress"),            "maven_default", "high"),
        Stage("TEST",    _mvn("mvn test -q --no-transfer-progress"),               "maven_default", "high"),
    ]
    return plan


# ─── Gradle adapter ───────────────────────────────────────────────────────────

def plan_gradle(repo_root: Path) -> ExecutionPlan:
    """
    Build an execution plan for a Gradle project, including the Java version.
    """
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
    """
    Build an execution plan for a Go project, taking the version from go.mod.
    """
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
    """
    Build an execution plan for a Rust project.

    The toolchain is read from rust-toolchain.toml or rust-toolchain when either
    is present, since projects often pin a specific compiler release.
    """
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
    """
    Build an execution plan for a Ruby project, honouring .ruby-version.
    """
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

# Ordered by priority — check more specific files first
_AUTO_DETECT = [
    ("package.json",       "npm"),
    ("pom.xml",            "maven"),
    ("build.gradle",       "gradle"),
    ("build.gradle.kts",   "gradle"),
    ("requirements.txt",   "pip"),
    ("pyproject.toml",     "pip"),
    ("setup.py",           "pip"),
    ("Pipfile",            "pip"),
    ("Gemfile",            "gem"),
    ("go.mod",             "go"),
    ("Cargo.toml",         "cargo"),
    ("composer.json",      "composer"),
]


def detect_ecosystem(repo_root: Path) -> Optional[str]:
    """Auto-detect ecosystem from manifest files on disk."""
    for filename, eco in _AUTO_DETECT:
        if (repo_root / filename).exists():
            return eco
    return None


def get_execution_plan(ecosystem: str, repo_root: Path) -> Optional[ExecutionPlan]:
    """
    Return the execution plan for a repository, detecting the ecosystem if needed.

    When `ecosystem` is empty or unknown, it is inferred from the files present
    in the repository. Returns None when no adapter matches.

    Only the pip path is exercised by this research; the other ecosystems are
    retained from an earlier, multi-ecosystem version of the pipeline and are
    not validated.
    """
    eco = (ecosystem or "").lower()
    # Auto-detect if unknown
    if eco in ("", "unknown", "none"):
        eco = detect_ecosystem(repo_root) or ""
    adapter = _ADAPTERS.get(eco)
    if adapter is None:
        return None
    try:
        return adapter(repo_root)
    except Exception as e:
        return None
