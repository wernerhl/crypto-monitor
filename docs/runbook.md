# Runbook

## Jobs
| job | schedule (UTC) | what it does | phase |
|---|---|---|---|
| `ci` | push / PR | ruff, pytest (live tests skipped), secret scan, repo-size check | 1 |
| `daily` | 01:25 | (phase 1) render placeholder site, deploy to Pages, verify live sha | 1 |
| `hourly` | :07 | funding, OI, books, liquidations, options → positioning, liquidity, fragility, rules | 3 |
| `weekly` | Sun 02:40 | tiers, factor model, IC, hit rates, raw rotation, size check, review issue | 2/7 |
| `manual` | dispatch | run any job or a backfill | 2 |

## When a job fails
* **daily / verify-pages step fails**: Pages propagation can lag; the step retries for 5 minutes.
  Re-run the workflow from the Actions tab. If `data-build-sha` still differs, check that the
  Pages source is "GitHub Actions" in repository settings.
* **CI secret scan fails**: the line it names looks like a key. Move the value to an environment
  variable; if it is a false positive add `# not-a-secret` to the line.
* **Repo-size check fails**: rotate raw files (below).

## Rotating raw files
Phase 7. The weekly job moves raw files older than 90 days into a monthly tarball on the
`data-archive` orphan branch.

## Adding a venue or asset
Phase 2/3.

## Replacing the example book
Edit `config/book.yaml`; positions are shares of NAV with the venue where each sits.

## Rate-limit budget
Phase 3 (per-venue request counts per hourly run with 50 % headroom).
