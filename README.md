# DependaScope - code and dataset

This package contains the full pipeline and the dataset it produced.

## Contents

```
pipeline/   Source code. See pipeline/README.md for the methodology,
            setup instructions and repository layout.
data/       The dataset, as a SQLite database and as CSV exports.
```

## Data files

| File | Rows | Contents |
| --- | ---: | --- |
| `regression_results.sqlite` | - | The results database. Table `results` holds one row per processed candidate; `candidates` holds the work queue and its final status |
| `dependascope_FULL_dataset_20260903.csv` | 4,415 | Every processed candidate, every column. All other CSVs are filtered views of this file |
| `tier3_confirmed_20260903.csv` | 101 | Confirmed build/test regressions |
| `tier2_install_regressions_20260903.csv` | 93 | Updates that broke dependency installation |
| `controls_pass_to_pass_20260903.csv` | 860 | Updates verified as harmless |
| `tier3_unstable_excluded_20260903.csv` | 13 | Apparent regressions that failed to reproduce, excluded from the findings |
| `rejected_20260903.csv` | 3,348 | Candidates where no baseline could be established, with the reason for each |

The rejected file is worth keeping alongside the others: it is the record of
what was examined and discarded, and it is what makes the reported break rate
interpretable rather than a selected subset.

## Reading a single case

Each row in `results` carries the evidence behind its verdict:

- `repo`, `pr_number`, `dependency`, `old_version`, `new_version`
- `before_sha`, `after_sha`, `sha_pair_verified` - the exact commits compared
- `before_install_result`, `before_execution_result` - the state before the update
- `after_install_result`, `after_execution_result` - the state after it
- `before_execution_excerpt`, `after_execution_excerpt` - captured output, including
  the actual failure text
- `tier`, `classification`, `reason` - the verdict and its justification in words
- `evidence_strength` - whether a real test suite ran or only an import check
- `confirmation` - the independent second-run result
- `tests_passing_before`, `tests_regressed`, `regressed_tests` - the per-test delta
- `duration_seconds`, `log_dir` - timing and the path to the full logs

Example query:

```sql
SELECT repo, pr_number, dependency, old_version, new_version,
       reason, after_execution_excerpt
FROM results
WHERE tier IN ('TIER_3_CONFIRMED', 'TIER_3_TEST_REGRESSION')
ORDER BY repo;
```

## Headline results

Of 1,067 candidates that produced a usable before/after comparison:

- 101 confirmed build or test regressions, across 70 distinct projects
- 93 install-only regressions
- 860 controls where the update was harmless
- 13 excluded because they did not reproduce on an independent second run

That is a 9.5% build/test break rate, or 18.2% counting installation failures.

Note that the 101 cases collapse to 85 independent findings once repositories
contributing several pull requests for the same underlying dependency break are
counted once. Case, finding and project counts should be reported together.

## Reproducing the analysis

The pipeline is resumable and safe to stop at any point. To continue collecting
and processing:

```bash
cd pipeline
export GITHUB_TOKEN=your_token_here
export DEPBOT_WORK_ROOT=/path/to/work/directory
pip install -r requirements.txt

python3 stage2_fetch_live_github.py --ecosystem pip --since 2023-01-01 --limit 4000
python3 regression_pipeline/run_regression_pipeline.py discover --limit 6000
python3 regression_pipeline/run_regression_pipeline.py run --batch 0
```

Several `run` workers may execute concurrently against the same database; work
claims are atomic, so each candidate is processed exactly once.

## Note on old_files

`pipeline/old_files/` holds an earlier staged implementation, one-off
diagnostic scripts and superseded backups. None of it is imported by the
current pipeline; it is retained only for provenance and can be removed
without affecting anything.
