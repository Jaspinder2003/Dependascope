"""
build_executor.py — Execute build for a single BEFORE/AFTER snapshot
════════════════════════════════════════════════════════════════════
Creates an isolated worktree + venv, detects project directory,
installs dependencies, then runs the standard build command.

Completely isolated from the existing pipeline database.
"""

from __future__ import annotations

import logging
import os
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
import sandbox_executor as sx

from build_revalidation.build_detector import detect_build_config

logger = logging.getLogger("build_revalidation")


def run_build_snapshot(
    repo: str,
    pr_number: int,
    snapshot: str,       # "BEFORE" or "AFTER"
    sha: str,
    py_bin: str,
    work_root: Path,
    log_dir: Path,
    ecosystem: str = "pip",
) -> dict:
    """
    Run a full install + build for one snapshot (BEFORE or AFTER).

    Returns:
        {
            "install_result":    "PASS" | "FAIL" | "TIMEOUT" | "UNRUNNABLE",
            "build_result":      "PASS" | "FAIL" | "TIMEOUT" | "SKIPPED" | "NO_CONFIG",
            "build_command":     str or None,
            "config_source":     str or None,
            "project_rel_path":  str,
            "duration_seconds":  float,
            "failure_excerpt":   str or None,
            "failure_stage":     "CHECKOUT" | "VENV" | "INSTALL" | "BUILD" | None,
        }
    """
    safe_repo = repo.replace("/", "__")
    work_root.mkdir(parents=True, exist_ok=True)
    # Guarantee a fresh disposable directory per run
    work_dir = Path(tempfile.mkdtemp(prefix=f"{safe_repo}_pr{pr_number}_{snapshot}_", dir=str(work_root)))
    venv_dir = work_dir / "_venv"
    log_dir.mkdir(parents=True, exist_ok=True)

    res = {
        "install_result": "FAIL",
        "build_result": "NO_CONFIG",
        "build_command": None,
        "config_source": None,
        "project_rel_path": ".",
        "duration_seconds": 0.0,
        "failure_excerpt": None,
        "failure_stage": None,
    }

    start_t = time.time()
    try:
        # ── 1. Checkout ──────────────────────────────────────────────────
        ok, err = gf.checkout_snapshot_worktree(repo, sha, work_dir)
        if not ok:
            res["failure_stage"] = "CHECKOUT"
            res["failure_excerpt"] = f"git checkout failed: {err}"
            return res

        # ── 2. Create fresh venv ─────────────────────────────────────────
        try:
            v_proc = subprocess.run(
                [py_bin, "-m", "venv", str(venv_dir)],
                capture_output=True, text=True, timeout=60
            )
            if v_proc.returncode != 0:
                res["failure_stage"] = "VENV"
                res["failure_excerpt"] = f"venv creation failed: {v_proc.stderr[:300]}"
                return res
        except Exception as e:
            res["failure_stage"] = "VENV"
            res["failure_excerpt"] = f"venv creation exception: {e}"
            return res

        # ── 3. Detect project root & build config ───────────────────────
        py_root, build_cmd, config_src = detect_build_config(work_dir)
        res["build_command"] = build_cmd
        res["config_source"] = config_src
        if py_root != work_dir:
            res["project_rel_path"] = py_root.relative_to(work_dir).as_posix()

        # ── 4. Install dependencies ──────────────────────────────────────
        plan = ea.get_execution_plan(ecosystem, work_dir)
        if not plan or not plan.stages:
            res["failure_stage"] = "INSTALL"
            res["failure_excerpt"] = "No execution plan generated"
            return res

        install_stage = next((s for s in plan.stages if s.name == "INSTALL"), None)
        if not install_stage:
            res["failure_stage"] = "INSTALL"
            res["failure_excerpt"] = "No INSTALL stage in execution plan"
            return res

        slug = f"{safe_repo}__pr{pr_number}__{snapshot}__INSTALL"
        stdout_path = log_dir / f"{slug}.stdout.txt"
        stderr_path = log_dir / f"{slug}.stderr.txt"

        install_sr = sx.run_stage(
            stage_name="INSTALL",
            command=install_stage.command,
            work_dir=work_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout=min(install_stage.timeout, C.EXEC_TIMEOUT_TOTAL),
            venv_dir=venv_dir,
        )

        if install_sr["result"] != "PASS":
            res["install_result"] = install_sr["result"]  # FAIL or TIMEOUT
            res["failure_stage"] = "INSTALL"
            err_txt = _read_tail(stderr_path, 500) or _read_tail(stdout_path, 500)
            res["failure_excerpt"] = err_txt or f"Install exited with code {install_sr['exit_code']}"
            return res

        res["install_result"] = "PASS"

        # ── 5. Run build if config exists ────────────────────────────────
        if not build_cmd:
            res["build_result"] = "NO_CONFIG"
            return res

        slug_build = f"{safe_repo}__pr{pr_number}__{snapshot}__BUILD"
        stdout_build = log_dir / f"{slug_build}.stdout.txt"
        stderr_build = log_dir / f"{slug_build}.stderr.txt"

        build_sr = sx.run_stage(
            stage_name="BUILD",
            command=build_cmd,
            work_dir=py_root,
            stdout_path=stdout_build,
            stderr_path=stderr_build,
            timeout=300,  # 5 min limit for build execution
            venv_dir=venv_dir,
        )

        if build_sr["result"] == "PASS":
            res["build_result"] = "PASS"
        elif build_sr["result"] == "TIMEOUT":
            res["build_result"] = "TIMEOUT"
            res["failure_stage"] = "BUILD"
            res["failure_excerpt"] = "Build timed out after 300s"
        else:
            res["build_result"] = "FAIL"
            res["failure_stage"] = "BUILD"
            err_txt = _read_tail(stderr_build, 500) or _read_tail(stdout_build, 500)
            res["failure_excerpt"] = err_txt or f"Build exited with code {build_sr['exit_code']}"

    finally:
        res["duration_seconds"] = round(time.time() - start_t, 2)
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

    return res


def _read_tail(path: Path, max_chars: int = 500) -> str:
    """Read the last max_chars of a file, or empty string if not readable."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        return text[-max_chars:] if text else ""
    except Exception:
        return ""
