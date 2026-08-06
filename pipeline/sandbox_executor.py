"""
sandbox_executor.py
────────────────────
Execute repository build/test commands in a restricted subprocess environment.

Docker is NOT available on this host. We use subprocess with:
  - a disposable temporary directory as the working tree
  - strict environment variable sanitisation (no tokens, no SSH keys)
  - CPU/time limits via subprocess.run(timeout=...)
  - no network restrictions (needed for dependency installation)
  - all output captured to log files

Security note: this is a research tool. Running arbitrary project code on the
host is a security trade-off documented here. Mitigations applied:
  - No secrets in environment
  - Disposable work directory deleted after run
  - Wall-clock timeout enforced
  - Process limited to subprocess (no root, no socket mount)

If Docker becomes available, replace _run_subprocess with a Docker-based runner.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import config as C

logger = logging.getLogger(__name__)

# Variables to NEVER pass into executed subprocesses
_BLOCKED_ENV_KEYS = {
    "GITHUB_TOKEN", "GH_TOKEN", "NPM_TOKEN", "PYPI_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID",
    "DOCKER_HOST", "KUBECONFIG",
}


def _safe_env() -> dict:
    """Return a sanitised environment dict with secrets removed."""
    env = {}
    for k, v in os.environ.items():
        if k.upper() in _BLOCKED_ENV_KEYS:
            continue
        if "TOKEN" in k.upper() or "SECRET" in k.upper() or "PASSWORD" in k.upper():
            continue
        env[k] = v

    # Explicitly prepend Python 3.9 Conda environment to PATH
    conda_env_dir = r"C:\Users\jaspi\anaconda3\envs\diagnosis"
    conda_paths = [
        conda_env_dir,
        os.path.join(conda_env_dir, "Scripts"),
        os.path.join(conda_env_dir, "Library", "bin"),
        r"C:\tools\maven\bin",
    ]
    existing_path = os.environ.get("PATH", "")
    new_path = ";".join(conda_paths) + ";" + existing_path
    env["PATH"] = new_path

    env.setdefault("HOME", os.environ.get("HOME", str(Path.home())))
    # CI mode: prevents Jest/React/Angular from running in interactive watch mode
    env["CI"] = "true"
    env["NODE_ENV"] = "test"
    # Non-interactive execution flags: prevent hanging on terminal prompts
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PIP_NO_INPUT"] = "1"
    return env


def run_stage(
    stage_name: str,
    command: str,
    work_dir: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int = C.EXEC_TIMEOUT_TOTAL,
) -> dict:
    """
    Execute a single stage command. Returns a result dict:
        stage, command, exit_code, duration_seconds, result, stdout_path, stderr_path
    """
    logger.info(f"  [{stage_name}] {command}")
    start = time.time()
    env = _safe_env()

    exit_code = -1
    timed_out = False

    try:
        with open(stdout_path, "wb") as fout, open(stderr_path, "wb") as ferr:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(work_dir),
                stdout=fout,
                stderr=ferr,
                env=env,
            )
            try:
                proc.communicate(timeout=timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                # On Windows, kill the ENTIRE process tree (pip/mvn orphans survive
                # a plain proc.kill() because shell=True spawns cmd.exe as parent)
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                    )
                else:
                    try:
                        import signal, os as _os
                        _os.killpg(_os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        proc.kill()
                try:
                    proc.communicate(timeout=5)  # reap zombie without blocking forever
                except Exception:
                    pass
    except Exception as e:
        try:
            stderr_path.write_bytes(str(e).encode())
        except Exception:
            pass

    duration = time.time() - start

    if timed_out:
        result_str = "TIMEOUT"
    elif exit_code == 0:
        result_str = "PASS"
    else:
        result_str = "FAIL"

    logger.info(f"  [{stage_name}] exit={exit_code} ({result_str}) in {duration:.1f}s")

    return {
        "stage":            stage_name,
        "command":          command,
        "exit_code":        exit_code,
        "duration_seconds": round(duration, 2),
        "result":           result_str,
        "timed_out":        timed_out,
        "stdout_path":      str(stdout_path),
        "stderr_path":      str(stderr_path),
    }


def run_plan(
    plan,                    # ExecutionPlan from ecosystem_adapters
    work_dir: Path,
    log_dir: Path,
    snapshot: str,           # "BEFORE" or "AFTER"
    repo: str,
    pr_number: int,
    attempt: int = 1,
) -> tuple[str, list[dict]]:
    """
    Execute all stages in the plan.
    Stops at first failure.
    Returns (first_failure_stage_or_PASS, list_of_stage_results).
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    stage_results = []
    first_failure = None

    for stage in plan.stages:
        safe_repo = repo.replace("/", "__")
        slug = f"{safe_repo}__pr{pr_number}__{snapshot}__{stage.name}__att{attempt}"
        stdout_path = log_dir / f"{slug}.stdout.txt"
        stderr_path = log_dir / f"{slug}.stderr.txt"

        result = run_stage(
            stage_name=stage.name,
            command=stage.command,
            work_dir=work_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout=min(stage.timeout, C.EXEC_TIMEOUT_TOTAL),
        )
        stage_results.append(result)

        if result["result"] != "PASS":
            first_failure = stage.name
            break   # stop at first failure

    overall = first_failure or "PASS"
    return overall, stage_results
