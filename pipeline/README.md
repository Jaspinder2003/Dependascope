# DependaScope

An empirical pipeline for measuring whether automated dependency updates break
Python projects, and at which stage they break.

## Research question

When Dependabot opens a pull request to update a dependency, does the project
still build and pass its tests? The pipeline answers this by *reproducing* each
update locally: it checks out the commit immediately before the pull request,
installs and runs the project's test suite, then checks out the commit produced
by the pull request and repeats the identical procedure. A regression is
recorded only when the project demonstrably worked before the update and
demonstrably failed after it.

This is deliberately stronger evidence than reading a pull request's CI badge. A
red CI run can mean the project was already broken, that an unrelated job
failed, or that infrastructure was briefly unavailable. Rebuilding both states
locally, on the same interpreter, isolates the dependency change as the only
variable.

## Results

Measured over 4,415 processed candidate pull requests:

| Outcome | Count |
| --- | ---: |
| Confirmed build/test regressions (cases) | 101 |
| Independent findings (repository x dependency) | 85 |
| Distinct projects affected | 70 |
| Install-only regressions | 93 |
| Controls (update verified harmless) | 860 |
| Excluded as non-reproducible | 13 |

Of the 1,067 candidates that produced a usable before/after comparison:

- **9.5%** broke the build or test suite
- **18.2%** broke something (build, tests, or installation)
- **80.6%** were harmless

### Notable observations

**Version numbers did not predict risk.** Break rates by change type were
statistically indistinguishable at this sample size: patch 9.5%, minor 11.7%,
major 10.4%. Constraint changes, where the pull request widens a version range
rather than pinning a new version, were the worst category at 14.6% and also the
largest. Confirmed regressions include single patch releases such as
`aiohttp 3.14.0 -> 3.14.1` and `gitpython 3.1.55 -> 3.1.57`.

**One breaking release propagated widely.** A single SDK's major-version
transition (`mcp` 1.x to 2.x) produced confirmed regressions in 17 independent
repositories. Other dependencies affecting multiple projects were `ruff` (4
projects), and `gdsfactory`, `pytest` and `fastapi` (3 each).

## Methodology

### 1. Collection

`stage2_fetch_live_github.py` queries the GitHub search API for closed, unmerged,
failing pull requests authored by Dependabot, partitioned into monthly windows.
Because the search API caps each query at 1,000 results per window, coverage
scales with the number of distinct search terms rather than the width of the
date range; the collector therefore issues 93 terms across 45 monthly windows.

Two filters are applied to every result:

- **Green-to-red CI verification.** The CI status of the parent commit and of the
  pull request head commit are fetched individually. Only pull requests whose
  parent was passing and whose head was failing are retained. This removes
  projects that were already broken, where a failure says nothing about the
  update.
- **Project quality gates.** The repository must have at least 5 stars, 100
  commits and 11 contributors, excluding personal and toy repositories.

Genuine green-to-red transitions are scarce. In a representative sweep, roughly
29,000 pull requests were discarded because the parent commit had no recorded CI
status at all, and a further 34,000 because CI was already failing.

### 2. Candidate selection

`regression_pipeline/candidates.py` admits a collected pull request only if it
satisfies all of the following:

| Filter | Rule | Rationale |
| --- | --- | --- |
| Ecosystem | Confirmed `pip` from the manifest files the pull request changed | Python-only scope. An unset ecosystem is never assumed to be Python |
| Single dependency | Grouped multi-dependency updates are excluded | A failure in a grouped update cannot be attributed to one dependency |
| Manifest-only | The pull request changes only manifests or lockfiles, never source | Guarantees the dependency is the sole variable |
| Commit pair | Both parent and head SHAs must resolve | Required to check out both states |
| Per-repository cap | At most 10 pull requests per repository | Prevents a single project dominating the dataset |

### 3. Reproduction

For each candidate, `regression_pipeline/snapshot_runner.py` performs two
independent snapshots inside a disposable git worktree:

1. **Before** - check out the parent commit, create a virtual environment,
   install dependencies, run the project's build and test suite.
2. **After** - check out the pull request head commit and repeat the identical
   procedure.

Both snapshots run on the same interpreter, selected from the project's declared
`requires-python` and substituting the nearest available version when the exact
one is absent. Holding the interpreter constant keeps the comparison valid.

Stage timeouts are enforced per snapshot (600s install, 120s test phase, 600s
wall clock). Each stage runs in its own process group, so a timeout terminates
the build subtree without affecting the worker that launched it.

### 4. Classification

`regression_pipeline/classifier.py` assigns one tier per candidate:

| Tier | Meaning |
| --- | --- |
| `TIER_3_CONFIRMED` | Installed and passed before; installed but failed the suite after |
| `TIER_3_TEST_REGRESSION` | As above, with specific regressed tests identified by name |
| `TIER_2_INSTALL_REGRESSION` | Dependencies no longer install after the update |
| `CONTROL` | Passed both before and after; the update was harmless |
| `TIER_3_UNSTABLE` | Appeared to regress but did not reproduce; excluded |
| `REJECTED` | No baseline could be established |

### 5. Reproduction check

Every apparent regression is run a second time, independently, from a fresh
checkout. Only candidates marked `REPRODUCED_2_OF_2` enter the dataset. Thirteen
candidates failed this check and were excluded, five of them from a single
project whose test suite proved nondeterministic across five unrelated
dependencies.

Without this check those thirteen would have entered the dataset as false
positives, inflating the confirmed count by roughly 11%.

## Repository layout

```
pipeline/
  stage2_fetch_live_github.py   Collection: GitHub search, CI verification, quality gates
  config.py                     Paths, timeouts, API settings
  db.py                         Research database schema and helpers
  github_client.py              Cached, rate-limit-aware GitHub API client
  git_fetcher.py                Shallow commit fetching and worktree checkout
  sandbox_executor.py           Stage execution with timeouts and output capture
  ecosystem_adapters.py         Manifest detection per ecosystem
  purified_reproduce.py         Shared reproduction helpers

  regression_pipeline/
    candidates.py               Candidate selection and filtering
    run_regression_pipeline.py  Worker entry point and run loop
    snapshot_runner.py          Before/after snapshot execution
    install_planner.py          Chooses the install command for a project
    execution_detector.py       Chooses the test command for a project
    classifier.py               Verdict and tier assignment
    db.py                       Work queue and results storage
    repo_lock.py                Cross-worker repository locking

  build_revalidation/
    build_detector.py           Build-system detection
    build_executor.py           Isolated build execution
    run_build_revalidation.py   Build-focused before/after comparison

  old_files/                    Superseded stages, one-off diagnostics, backups
  output/                       Research database and exports
  logs/                         Per-stage execution logs
```

## Setup

Requires Python 3.9 or newer. Additional interpreters (3.9, 3.11, 3.12, 3.13,
3.14) should be installed if broad coverage is wanted, since projects declare
different `requires-python` constraints and a candidate is skipped when no
compatible interpreter is available.

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=your_token_here
export DEPBOT_WORK_ROOT=/path/to/work/directory
```

The token is read from the environment only; it is never written to disk or
stored in the database.

## Usage

Collect candidate pull requests:

```bash
python3 stage2_fetch_live_github.py --ecosystem pip --since 2023-01-01 --limit 4000
```

Move collected pull requests into the work queue:

```bash
python3 regression_pipeline/run_regression_pipeline.py discover --limit 6000
```

Process the queue. Several workers may run concurrently against the same
database; claims are atomic, so each candidate is processed once:

```bash
python3 regression_pipeline/run_regression_pipeline.py run --batch 0
```

`--batch 0` drains the queue; a positive value stops after that many candidates.

## Outputs

Results are stored in `regression_pipeline/regression.sqlite`. The `results`
table holds one row per processed candidate with 36 columns, including both
commit SHAs, install and execution results for each snapshot, captured failure
output, the assigned tier and classification, evidence strength, reproduction
status, the names of any regressed tests, and the path to the full logs.

Exports are written as CSV: one file per tier, plus a complete dataset
containing every row and every column.

## Limitations

- **Baseline attrition.** Only about a quarter of candidates yield a usable
  comparison. The remainder fail because the project was already broken (27%),
  exposes no installable manifest (17%), or fails to install at the parent
  commit (17%). This is honest attrition rather than selection bias: those
  projects cannot answer the research question either way.
- **Clustering.** The 101 cases collapse to 85 independent findings across 70
  projects. One repository contributed five pull requests for a single
  underlying dependency break. Case counts should always be reported alongside
  finding and project counts.
- **Isolation.** Candidates are built and tested directly on the host rather
  than inside a per-candidate container. Since the pipeline executes code from
  arbitrary repositories, containerised execution with no host network access
  and no access to the invoking user's home directory is the appropriate next
  step.
- **Ecosystem coverage.** Python only. The collection and adapter layers retain
  support for other ecosystems, but no other ecosystem has been validated.
