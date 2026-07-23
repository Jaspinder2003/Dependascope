"""
stage4_validate_single_dep.py
──────────────────────────────
Inspect changed files for each ENRICHED PR and classify as STRICT or COMPLEX.

STRICT: exactly one direct dependency changes; only manifests & lockfiles touch;
        no application source or test code changed.

COMPLEX: anything else.

Stores dependency_changes rows and updates pull_requests.strict_or_complex.
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import db

LOG_DIR = C.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "stage4_validate.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


# ─── Ecosystem/manifest → direct-dep parser dispatch ─────────────────────────

def detect_ecosystem_from_files(file_list: list[str]) -> Optional[str]:
    """Best-effort ecosystem from filenames."""
    fset = {Path(f).name.lower() for f in file_list}
    if "package.json" in fset:
        return "npm"
    if any(f in fset for f in ("requirements.txt", "pyproject.toml", "pipfile", "setup.py")):
        return "pip"
    if "pom.xml" in fset:
        return "maven"
    if "build.gradle" in fset or "build.gradle.kts" in fset:
        return "gradle"
    if "go.mod" in fset:
        return "go"
    if "cargo.toml" in fset:
        return "cargo"
    if "gemfile" in fset:
        return "gem"
    if "composer.json" in fset:
        return "composer"
    if any(f.endswith(".csproj") for f in file_list):
        return "nuget"
    if any(".github/workflows/" in f for f in file_list):
        return "github_actions"
    return None


def _count_direct_changes_from_patch(filename: str, patch: Optional[str]) -> tuple[int, int]:
    """
    Parse the patch text and count:
    - direct_dependency_count: number of direct dep lines that changed
    - transitive_change_count: number of transitive dep lines that changed (lockfile heuristic)

    Returns (direct, transitive).
    """
    if not patch:
        return 0, 0

    fname = Path(filename).name.lower()

    # package.json – count changed "dependencies"/"devDependencies" keys
    if fname == "package.json":
        return _count_json_dep_changes(patch)

    # requirements.txt / pyproject.toml [dependencies] – each changed line = one dep
    if fname in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
        return _count_line_dep_changes(patch)

    # go.mod
    if fname == "go.mod":
        return _count_line_dep_changes(patch)

    # Cargo.toml
    if fname == "cargo.toml":
        return _count_line_dep_changes(patch)

    # Gemfile
    if fname == "gemfile":
        return _count_line_dep_changes(patch)

    # pom.xml – count changed <version> or <dependency> blocks (approximate)
    if fname == "pom.xml":
        changed = len(re.findall(r"^[+-]\s*<version>", patch, re.M))
        return max(1, changed), 0

    # Lockfiles – all changes are considered transitive
    lockfile_names = {
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "poetry.lock", "pipfile.lock", "cargo.lock",
        "gemfile.lock", "go.sum", "composer.lock",
    }
    if fname in lockfile_names:
        added   = len(re.findall(r"^\+", patch, re.M))
        removed = len(re.findall(r"^-", patch, re.M))
        return 0, max(added, removed)

    # GitHub Actions workflow
    if ".github/workflows/" in filename.lower():
        return _count_line_dep_changes(patch, pattern=r"^\s*uses:")

    return 1, 0   # default: assume single direct dep change


def _count_json_dep_changes(patch: str) -> tuple[int, int]:
    """Count changed dep entries in package.json patch."""
    # Lines like: +    "lodash": "^4.17.21",
    pattern = re.compile(r'^[+-]\s*"[^"]+"\s*:\s*"[^"]*"', re.M)
    matches = pattern.findall(patch)
    # Filter out metadata keys
    dep_keys = {m.strip()[1:].split('"')[1] for m in matches
                if not m.strip()[1:].split('"')[1].startswith("_")}
    return len(dep_keys), 0


def _count_line_dep_changes(patch: str, pattern: Optional[str] = None) -> tuple[int, int]:
    """Count unique dependency-looking changed lines."""
    if pattern:
        changed = re.findall(pattern + r".+", patch, re.M)
    else:
        changed = re.findall(r"^[+-][^-+#\s].*", patch, re.M)
    return len(set(changed)), 0


# ─── Validator ────────────────────────────────────────────────────────────────

def validate_pr(conn, repo: str, pr_number: int,
                dry_run: bool = False) -> tuple[str, str]:
    """
    Returns (strict_or_complex, reason).
    """
    # Fetch changed files from DB
    files = conn.execute(
        "SELECT * FROM changed_files WHERE repo=? AND pr_number=?",
        (repo, pr_number)
    ).fetchall()

    if not files:
        # Files not in DB (enrichment may have been skipped or had 0 files listed)
        # Try to classify from PR info alone
        pr_row = conn.execute(
            "SELECT * FROM pull_requests WHERE repo=? AND pr_number=?",
            (repo, pr_number)
        ).fetchone()
        if not pr_row:
            return "UNKNOWN", "pr_not_found"
        title = pr_row["title"] or ""
        if re.search(r"(?i)group|\d+\s+(update|package)", title):
            return "COMPLEX", "grouped_title_no_files"
        return "UNKNOWN", "no_file_data"

    filenames  = [f["filename"] for f in files]
    has_source = any(f["is_source"] for f in files)
    has_test   = any(f["is_test"] for f in files)
    has_ci     = any(f["is_ci"] for f in files)
    manifest_files = [f for f in files if f["is_manifest"]]
    lockfile_files  = [f for f in files if f["is_lockfile"]]
    other_files     = [f for f in files if not f["is_manifest"] and not f["is_lockfile"]]

    ecosystem = detect_ecosystem_from_files(filenames)

    # Reject: source code changes
    if has_source and not has_test:
        return "COMPLEX", "source_code_changed"

    if has_test:
        return "COMPLEX", "test_code_changed"

    # Reject: CI-only changes accompanying a dependency update
    non_ci_non_manifest = [
        f for f in other_files if not f["is_ci"] and not f["is_lockfile"] and not f["is_manifest"]
    ]
    if non_ci_non_manifest:
        return "COMPLEX", f"unclassified_files_changed:{','.join(f['filename'] for f in non_ci_non_manifest[:3])}"

    # Now count direct dependency changes across manifests
    total_direct_changes  = 0
    total_transitive_changes = 0
    primary_manifest_path = None
    dep_name: Optional[str] = None
    old_version: Optional[str] = None
    new_version: Optional[str] = None

    for mf in manifest_files:
        fname = mf["filename"]
        patch = None  # patches not stored in local DB; use title as fallback
        direct, transitive = _count_direct_changes_from_patch(fname, patch)

        if direct == 0 and mf["additions"] + mf["deletions"] > 0:
            direct = 1  # at least one change, assume 1 dep

        total_direct_changes  += direct
        total_transitive_changes += transitive

        if primary_manifest_path is None:
            primary_manifest_path = fname

    # Add lockfile transitive counts
    for lf in lockfile_files:
        total_transitive_changes += (lf["additions"] or 0) + (lf["deletions"] or 0)

    # If no manifests at all, this is odd
    if not manifest_files:
        if lockfile_files:
            # Only lockfile changed – could be a dependabot PR that only touched lockfile
            total_direct_changes = 1
        else:
            return "COMPLEX", "no_manifest_or_lockfile_changed"

    # Classify
    if total_direct_changes > 1:
        return "COMPLEX", f"multiple_direct_deps_changed:{total_direct_changes}"

    # Extract dep info from title
    pr_row = conn.execute(
        "SELECT title, head_sha, before_sha FROM pull_requests WHERE repo=? AND pr_number=?",
        (repo, pr_number)
    ).fetchone()

    title = (pr_row["title"] if pr_row else "") or ""
    dep_name, old_version, new_version = _extract_dep_from_title(title)
    bump_type = _classify_version_bump(old_version, new_version)

    # GitHub Actions workflow update
    if ecosystem == "github_actions" or (
        primary_manifest_path and ".github/workflows" in str(primary_manifest_path)
    ):
        ecosystem = "github_actions"
        bump_type = "github_action"

    # Persist dependency_changes row
    if not dry_run and dep_name:
        db.upsert_dep_change(conn, {
            "repo": repo,
            "pr_number": pr_number,
            "manifest_path": primary_manifest_path or "",
            "dependency": dep_name,
            "old_version": old_version or "",
            "new_version": new_version or "",
            "version_change_type": bump_type,
            "direct_dependency_count": total_direct_changes,
            "transitive_change_count": total_transitive_changes,
        })

    # Update pull_requests
    if not dry_run:
        conn.execute(
            """UPDATE pull_requests
               SET strict_or_complex='STRICT', ecosystem=?, processing_status='VALIDATED'
               WHERE repo=? AND pr_number=?""",
            (ecosystem, repo, pr_number)
        )
        conn.commit()

    return "STRICT", f"single_dep:{dep_name}:{old_version}->{new_version}"


def _extract_dep_from_title(title: str):
    title = title or ""
    m = re.search(
        r"(?i)(?:bump|update|upgrade)\s+(\S+)\s+from\s+(\S+)\s+to\s+(\S+)",
        title
    )
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = re.search(r"(?i)(?:bump|update|upgrade)\s+(\S+)\s+to\s+(\S+)", title)
    if m:
        return m.group(1), None, m.group(2)
    return None, None, None


def _classify_version_bump(old: Optional[str], new: Optional[str]) -> str:
    if not old or not new:
        return "unknown"
    old_c = old.lstrip("v")
    new_c = new.lstrip("v")
    try:
        op = [int(x) for x in old_c.split(".")[:3]]
        np = [int(x) for x in new_c.split(".")[:3]]
        while len(op) < 3: op.append(0)
        while len(np) < 3: np.append(0)
        if np[0] != op[0]: return "major"
        if np[1] != op[1]: return "minor"
        if np[2] != op[2]: return "patch"
        return "unknown"
    except Exception:
        if re.match(r"[0-9a-f]{7,}", old_c) and re.match(r"[0-9a-f]{7,}", new_c):
            return "digest"
        return "unknown"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace) -> None:
    conn = db.init_db(C.DB_PATH)

    if args.resume:
        sql = "SELECT repo, pr_number FROM pull_requests WHERE processing_status='ENRICHED'"
    else:
        sql = "SELECT repo, pr_number FROM pull_requests WHERE processing_status IN ('ENRICHED','VALIDATED')"

    if args.limit:
        sql += f" LIMIT {args.limit}"

    rows = conn.execute(sql).fetchall()
    logger.info(f"PRs to validate: {len(rows)}")

    strict_count  = 0
    complex_count = 0
    unknown_count = 0

    for i, row in enumerate(rows):
        repo = row["repo"]
        pr_num = row["pr_number"]
        try:
            classification, reason = validate_pr(conn, repo, pr_num, dry_run=args.dry_run)
            if classification == "STRICT":
                strict_count += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE pull_requests SET strict_or_complex='STRICT', "
                        "classification_reason=?, processing_status='VALIDATED' "
                        "WHERE repo=? AND pr_number=?",
                        (reason, repo, pr_num)
                    )
            elif classification == "COMPLEX":
                complex_count += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE pull_requests SET strict_or_complex='COMPLEX', "
                        "classification_reason=?, processing_status='VALIDATED' "
                        "WHERE repo=? AND pr_number=?",
                        (reason, repo, pr_num)
                    )
            else:
                unknown_count += 1

            if not args.dry_run:
                conn.commit()

        except Exception as e:
            logger.error(f"Error validating {repo}#{pr_num}: {e}")
            db.log_event(conn, repo, pr_num, "validate", str(e), "ERROR")

        if (i + 1) % 500 == 0:
            logger.info(
                f"Progress {i+1}/{len(rows)}: "
                f"strict={strict_count} complex={complex_count} unknown={unknown_count}"
            )

    logger.info(f"Stage 4 complete: strict={strict_count}, complex={complex_count}, unknown={unknown_count}")

    # Export Parquet files
    if not args.dry_run:
        _export_parquets(conn)

    conn.close()


def _export_parquets(conn) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        for label, where in [
            ("strict_single_dependency_prs", "strict_or_complex='STRICT'"),
            ("complex_or_grouped_prs",       "strict_or_complex='COMPLEX'"),
        ]:
            rows = conn.execute(f"SELECT * FROM pull_requests WHERE {where}").fetchall()
            if not rows:
                continue
            cols = [d[0] for d in conn.execute(f"SELECT * FROM pull_requests WHERE {where} LIMIT 0").description]
            data = {c: [] for c in cols}
            for r in rows:
                for c, v in zip(cols, r):
                    data[c].append(v)
            table = pa.table(data)
            out = C.OUTPUT_DIR / f"{label}.parquet"
            pq.write_table(table, str(out))
            logger.info(f"Exported {out} ({len(rows)} rows)")
    except Exception as e:
        logger.warning(f"Parquet export failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 4: Validate single-dependency PRs")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="Only process ENRICHED (not already VALIDATED)")
    args = parser.parse_args()
    main(args)
