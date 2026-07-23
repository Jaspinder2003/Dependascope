# Dependabot Failure Research Pipeline

A complete pipeline to identify, reproduce, and classify Dependabot dependency-update failures.

---

## What This Pipeline Does

**Research question:** When Dependabot updates a project dependency, at what stage does the project first fail?

The pipeline:
1. **Ingests** the ScienceDB dataset of Dependabot PRs (363k+ rows across Excel + JSON files)
2. **Extracts** all Dependabot-authored PRs into a local SQLite database
3. **Enriches** each PR with GitHub API data (SHAs, files changed, CI status)
4. **Validates** that exactly one direct dependency changed (STRICT cohort)
5. **Prioritises** PRs most likely to show a PASS→FAIL transition
6. **Fetches** only the required Git commits (shallow clone)
7. **Detects** how the project was built/tested at each historical commit
8. **Runs** build/test stages for BEFORE and AFTER the Dependabot commit
9. **Classifies** each PR as PASS→PASS, PASS→FAIL, FAIL→FAIL, etc.
10. **Reports** confirmed PASS→FAIL cases as the primary research deliverable

---

## Quick Start

### 1. Install dependencies

```powershell
pip install duckdb polars pyarrow openpyxl requests pyyaml tqdm packaging
```

### 2. Set your GitHub token (required for Stage 3+)

```powershell
$env:GITHUB_TOKEN = "ghp_your_token_here"
```

> **Never** paste your token into a script or commit it. Use the environment variable only.

### 3. Run a small test (20 PRs, no reproduction)

```powershell
cd c:\Users\jaspi\OneDrive\Desktop\dependabot-failing\pipeline

# Inspect the data files
python stage1_inspect.py

# Extract candidates (first 500 rows for testing)
python stage2_extract_candidates.py --limit 500

# Enrich via GitHub API (first 20 PRs)
python stage3_enrich_github.py --limit 20 --resume

# Validate single-dep classification
python stage4_validate_single_dep.py --resume

# Score and queue
python stage5_prioritize.py

# View top candidates
python -c "
import sqlite3
conn = sqlite3.connect('output/research.sqlite')
rows = conn.execute('SELECT repo, pr_number, priority_score, title, ecosystem, strict_or_complex FROM pull_requests ORDER BY priority_score DESC LIMIT 20').fetchall()
for r in rows: print(r)
"
```

### 4. Run reproduction on a small sample

```powershell
python stage6789_reproduce.py --limit 5 --ecosystem npm --only-strict --dry-run
```

### 5. Full pipeline

```powershell
# Stage 2: Extract all candidates
python stage2_extract_candidates.py

# Stage 3: Enrich (may take hours — resume-safe)
python stage3_enrich_github.py --resume --limit 2000

# Stage 4: Validate
python stage4_validate_single_dep.py --resume

# Stage 5: Prioritize
python stage5_prioritize.py

# Stages 6-9: Reproduce (start with npm, limit 50)
python stage6789_reproduce.py --ecosystem npm --limit 50 --only-strict --resume --max-per-repo 3

# Stage 10: Report
python stage10_report.py
```

---

## Command Reference

### `run_pipeline.py` (master runner)

```
python run_pipeline.py [OPTIONS]

Options:
  --dry-run              Do not write to DB or files
  --limit N              Max rows/PRs per stage
  --workers N            Parallel workers for reproduction (default: 1)
  --ecosystem NAME       Filter to one ecosystem: npm, pip, maven, go, cargo, gem
  --only-strict          Only process STRICT single-dep PRs
  --resume               Skip already-completed records
  --max-per-repo N       Max PRs per repository (default: 5)
  --timeout N            Per-stage timeout in seconds (default: 900)
  --skip-stages "1,2"    Skip specific stages
  --only-stage "3"       Run only one stage
```

### Individual stage scripts

| Script | Purpose | Key flags |
|--------|---------|-----------|
| `stage1_inspect.py` | Inspect dataset files | `--dry-run` |
| `stage2_extract_candidates.py` | Extract Dependabot PRs | `--limit` |
| `stage3_enrich_github.py` | Fetch GitHub metadata | `--limit`, `--resume` |
| `stage4_validate_single_dep.py` | Classify STRICT/COMPLEX | `--limit`, `--resume` |
| `stage5_prioritize.py` | Score execution queue | `--dry-run` |
| `stage6789_reproduce.py` | Run before/after experiments | `--limit`, `--workers`, `--ecosystem`, `--only-strict`, `--resume`, `--max-per-repo`, `--timeout` |
| `stage10_report.py` | Generate final report | `--dry-run` |

---

## Output Files

All outputs go to `pipeline/output/`:

| File | Description |
|------|-------------|
| `research.sqlite` | Main database (all tables) |
| `dataset_schema_report.json` | Machine-readable schema report |
| `dataset_schema_report.md` | Human-readable schema report |
| `dependabot_candidates.parquet` | All extracted Dependabot PRs |
| `strict_single_dependency_prs.parquet` | STRICT cohort PRs |
| `complex_or_grouped_prs.parquet` | COMPLEX cohort PRs |
| `prioritized_execution_queue.parquet` | Ranked execution queue |
| `execution_results.parquet` | Per-stage run results |
| `confirmed_pass_to_fail.csv` | **Primary deliverable** |
| `pass_to_pass.csv` | Control group |
| `baseline_failures.csv` | Already-broken baselines |
| `unrunnable_cases.csv` | Could not reproduce |
| `final_report.md` | Summary report |

Logs go to `pipeline/logs/`. Execution logs (stdout/stderr) go to `pipeline/execution_logs/`.

---

## Database Tables

```sql
-- Main PR registry
SELECT * FROM pull_requests LIMIT 5;

-- Dependency change details
SELECT * FROM dependency_changes LIMIT 5;

-- All changed files per PR
SELECT * FROM changed_files WHERE repo='owner/repo' AND pr_number=123;

-- Per-stage execution results
SELECT repo, pr_number, snapshot, stage, result, exit_code FROM executions LIMIT 20;

-- Final classification
SELECT * FROM final_results WHERE classification='PASS->FAIL' AND reproduced=1;
```

---

## Data Sources

| Source | Format | Rows | Priority |
|--------|--------|------|----------|
| `Partition (1)/PRs.xlsx` | Excel | ~363,000 | **Primary** |
| `Partition (1)/Repos.xlsx` | Excel | ~36,000 repos | Repository metadata |
| `Partition (2)/PRs.xlsx` | Excel | ~46,000 | Secondary |
| `Partition (2)/Dataset/Dependabot/` | JSON (GitHub API) | ~10,000 | Tertiary |
| `Derived Sample/2. Extracted sub-sample.xlsx` | Excel | ~3,000 | Validation |
| `Partition (1)/Dataset/Part 0.rar` | RAR (compressed JSON) | Large | Requires extraction |

The **primary source** is `Partition (1)/PRs.xlsx` because it is already structured with author, title, state, and repo metadata.

---

## Classification Schema

### STRICT (primary research cohort)
- Dependabot author ✓
- Exactly 1 direct dependency changed ✓
- Only manifests & lockfiles changed ✓
- No source code, test code, or CI changes ✓
- Valid head_sha and before_sha retrieved ✓

### COMPLEX (secondary cohort, preserved for future study)
- Grouped updates (multiple deps)
- Source code also changed
- Test code changed
- PR has multiple commits from different authors

---

## Supported Ecosystems

| Ecosystem | Install command | Test command |
|-----------|----------------|--------------|
| npm | `npm ci` / `yarn install` / `pnpm install` | `npm test` |
| pip | `pip install -r requirements.txt` / `poetry install` | `pytest` / `tox` |
| maven | `mvn dependency:resolve` | `mvn test` |
| gradle | `./gradlew dependencies` | `./gradlew test` |
| go | `go mod download` | `go test ./...` |
| cargo | `cargo fetch` | `cargo test` |
| gem | `bundle install` | `bundle exec rspec` |

---

## Important Security Notes

- Repository code runs in a **subprocess** on your host (Docker is not available)
- **Secrets are stripped** from the subprocess environment before execution
- The `GITHUB_TOKEN` is never passed to subprocesses
- Each PR gets a **disposable temp directory** that is deleted after the run
- A **wall-clock timeout** is enforced per stage (default: 900 seconds)
- You are running **untrusted third-party code** — be aware of this risk

---

## Resuming After Interruption

Every operation uses SQLite upserts and a `processing_status` column:

```
PENDING → ENRICHED → VALIDATED → QUEUED → DONE
```

To resume from where you left off:

```powershell
python stage3_enrich_github.py --resume  # skips ENRICHED rows
python stage4_validate_single_dep.py --resume
python stage6789_reproduce.py --resume
```

---

## Primary Deliverable Format

`output/confirmed_pass_to_fail.csv` columns:

```
repo, pr_number, pr_url, dependency, old_version, new_version,
ecosystem, before_sha, after_sha, before_result, after_result,
first_failure_stage, reproduction_attempts, log_paths
```

Each row represents a **confirmed PASS→FAIL** case where:
- The BEFORE commit passed all stages
- The AFTER commit failed at `first_failure_stage`
- The result was **reproduced on a second independent run**

---

## Architecture

```
run_pipeline.py          ← Master runner (calls all stages)
├── config.py            ← All paths, weights, constants
├── db.py                ← SQLite schema + helper functions
├── github_client.py     ← Authenticated GitHub REST client with caching
├── ecosystem_adapters.py← Per-ecosystem plan detection (npm/pip/maven/…)
├── git_fetcher.py       ← Shallow Git fetch by SHA; bare repo cache
├── sandbox_executor.py  ← Subprocess runner with sanitised environment
├── stage1_inspect.py    ← Dataset file inspection + schema report
├── stage2_extract_candidates.py ← Extract Dependabot PRs from all sources
├── stage3_enrich_github.py      ← Enrich with GitHub API data
├── stage4_validate_single_dep.py← Classify STRICT vs COMPLEX
├── stage5_prioritize.py         ← Score and rank execution queue
├── stage6789_reproduce.py       ← Git fetch + plan detection + before/after run
└── stage10_report.py            ← Final CSV/Parquet/Markdown report
```
