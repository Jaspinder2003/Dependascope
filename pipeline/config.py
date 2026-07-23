"""
config.py – Central configuration for the Dependabot Security PR failure pipeline.

Work root is intentionally placed OUTSIDE OneDrive to avoid syncing high-churn
data (SQLite WAL, repo caches, logs, execution scratch).

Override work root:  set env var DEPBOT_WORK_ROOT before running any stage.
"""

import os
from pathlib import Path

# ─── Source data (read-only; stays in OneDrive) ───────────────────────────────
PIPELINE_DIR = Path(__file__).parent
DATA_DIR     = PIPELINE_DIR.parent          # dependabot-failing/

PARTITION1   = DATA_DIR / "Partition (1)"
PARTITION2   = DATA_DIR / "Partition (2)"
DERIVED      = DATA_DIR / "Derived Sample"

# ─── Work root (high-churn; must NOT be inside OneDrive) ─────────────────────
_default_work_root = Path("C:/depbot-work")
WORK_ROOT = Path(os.environ.get("DEPBOT_WORK_ROOT", str(_default_work_root)))

OUTPUT_DIR       = WORK_ROOT / "output"
LOG_DIR          = WORK_ROOT / "logs"
GITHUB_CACHE_DIR = WORK_ROOT / "github_api_cache"
REPO_CACHE_DIR   = WORK_ROOT / "repository_cache"
EXEC_LOG_DIR     = WORK_ROOT / "execution_logs"
SNAPSHOT_DIR     = WORK_ROOT / "repo_snapshots"

DB_PATH = OUTPUT_DIR / "research.sqlite"

# ─── Dataset label ────────────────────────────────────────────────────────────
# The primary source is the ScienceDB Dependabot *Security* PR dataset.
DATASET_LABEL    = "dependabot-security-prs"
DATASET_CITATION = "ScienceDB DOI:10.57760/sciencedb.07938"

# ─── GitHub API ───────────────────────────────────────────────────────────────
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API_BASE    = "https://api.github.com"
GITHUB_RATE_LIMIT_PAUSE   = 65      # seconds to sleep when rate-limited
GITHUB_RETRY_MAX          = 5
GITHUB_RETRY_BACKOFF      = 2.0     # exponential base
GITHUB_REQUEST_TIMEOUT    = 30      # seconds per request
GITHUB_PER_PAGE           = 100     # items per paginated request

# ─── API estimate thresholds ──────────────────────────────────────────────────
# Warn and require --allow-large-api-run when projected requests exceed this.
API_ESTIMATE_WARN_THRESHOLD = 500   # requests

# Pagination overhead multiplier for "likely" estimate
API_PAGINATION_FACTOR = 1.5

# ─── Local prefilter cohort labels ────────────────────────────────────────────
COHORT_TOP          = "TOP"           # Primary enrichment queue
COHORT_LOW_PRIORITY = "LOW_PRIORITY"  # Parseable but weak signals
COHORT_DIGEST       = "DIGEST"        # SHA digest updates (abc123→def456)
COHORT_ACTION       = "ACTION"        # GitHub Actions uses: updates
COHORT_RANGE        = "RANGE"         # Version specifier/range changes
COHORT_UNPARSED     = "UNPARSED"      # Title present but no version extractable
COHORT_GROUPED      = "GROUPED"       # Multi-dep grouped updates → COMPLEX

# Ecosystems the reproduction stage supports
SUPPORTED_ECOSYSTEMS = {"npm", "pip", "maven", "gradle", "go", "cargo", "gem"}

# ─── Local prefilter scoring weights ─────────────────────────────────────────
LOCAL_SCORE = {
    "author_modern_dependabot":  3,   # dependabot[bot] (not preview)
    "author_preview_dependabot": 1,   # dependabot-preview[bot]
    "title_bump_from_to":        4,   # "Bump X from A to B"
    "title_security_prefix":     2,   # "[Security]" in title
    "single_commit":             5,   # commits_count == 1
    "files_lte3":                4,   # changed_files <= 3
    "files_eq1":                 2,   # changed_files == 1 (bonus)
    "closed_not_merged":         3,   # state=closed, merged_at empty
    "has_comments":              2,   # comments_count >= 1
    "many_comments":             1,   # comments_count >= 3 (bonus)
    "major_bump":                3,   # version major change
    "minor_bump":                2,   # version minor change
    "patch_bump":                0,   # version patch change
    "supported_ecosystem":       2,   # ecosystem in SUPPORTED_ECOSYSTEMS
    "not_archived":              1,   # repo not archived
}

# Minimum local score to qualify for TOP cohort
LOCAL_TOP_MIN_SCORE   = 8
LOCAL_LOW_PRIORITY_MIN = 3

# ─── Strict classification requirements ───────────────────────────────────────
STRICT_REQUIRE_SINGLE_COMMIT        = True
STRICT_REQUIRE_SINGLE_PARENT        = True
STRICT_MIN_PARSE_CONFIDENCE         = {"high", "medium"}

# ─── Docker execution backend ─────────────────────────────────────────────────
DOCKER_IMAGE_DEFAULT = "ubuntu:22.04"   # override per ecosystem in adapters

# Container resource limits
DOCKER_MEMORY_LIMIT    = "2g"
DOCKER_CPU_COUNT       = "2"
DOCKER_PIDS_LIMIT      = 256
DOCKER_TMP_SIZE        = "2g"           # /workspace tmpfs size

# Timeouts
EXEC_TIMEOUT_INSTALL   = 600            # seconds – install phase (network on)
EXEC_TIMEOUT_TEST      = 600            # seconds – build/test phase
EXEC_TIMEOUT_TOTAL     = 1200           # hard wall-clock limit per snapshot

# Network policy values (stored in executions.network_policy)
NETWORK_POLICY_PHASED  = "phased_install_net_test_isolated"
NETWORK_POLICY_FULL    = "full_run_networked"
NETWORK_POLICY_NONE    = "full_run_isolated"

# Initial implementation uses full_run_networked (Option B fallback)
DOCKER_DEFAULT_NETWORK_POLICY = NETWORK_POLICY_FULL

MAX_WORKERS     = 2
MAX_PRS_PER_REPO = 5

# ─── Git / shallow fetch ──────────────────────────────────────────────────────
GIT_SHALLOW_DEPTH  = 10
GIT_FETCH_TIMEOUT  = 300

# ─── Manifest / lockfile name sets ───────────────────────────────────────────
MANIFEST_NAMES = {
    "package.json", "requirements.txt", "requirements-dev.txt",
    "requirements-test.txt", "pyproject.toml", "pipfile",
    "setup.py", "setup.cfg", "pom.xml", "build.gradle",
    "build.gradle.kts", "go.mod", "cargo.toml", "gemfile",
    "composer.json", "directory.packages.props",
}

LOCKFILE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "pipfile.lock", "cargo.lock", "gemfile.lock", "go.sum",
    "composer.lock", "packages.lock.json",
}

SOURCE_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".go",
    ".rs", ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".swift",
    ".scala", ".clj", ".ex", ".exs",
}

TEST_PATH_FRAGMENTS = {
    "test", "tests", "spec", "specs", "__tests__",
}

CI_PATH_PREFIXES = {
    ".github/workflows/", ".travis", ".circleci/", "Jenkinsfile",
    ".gitlab-ci", "azure-pipelines", ".appveyor",
}

# ─── Failure-signal keywords ──────────────────────────────────────────────────
FAILURE_KEYWORDS = [
    "fail", "failing", "failed", "broken", "breaks", "break",
    "regression", "incompatible", "peer dependency", "conflict",
    "type error", "does not work", "revert", "build error",
    "compile error", "test failure", "cannot install",
]

# ─── Audit gate ───────────────────────────────────────────────────────────────
AUDIT_SAMPLE_SIZE      = 20    # STRICT samples
AUDIT_COMPLEX_SAMPLE   = 20    # COMPLEX samples
AUDIT_GATE_NAME        = "audit_review"
