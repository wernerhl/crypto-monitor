# crypto-monitor — every target runs from a clean clone with `uv` installed.
.PHONY: all sync lint test test-live fetch compute site backfill clean size-check verify-sources

UV ?= uv
RUN := $(UV) run

all: sync compute site        ## rebuild archive from raw files and regenerate the site

sync:                         ## install pinned dependencies into .venv
	$(UV) sync --all-extras --frozen

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

test:
	$(RUN) pytest

test-live:                    ## live adapter tests (network)
	LIVE=1 $(RUN) pytest -m live

fetch:                        ## fetch everything for the current timestamp (idempotent)
	$(RUN) monitor fetch all

compute:                      ## recompute every processed table from raw files
	$(RUN) monitor compute all

site:                         ## render static site into ./site
	$(RUN) monitor site render

backfill:                     ## usage: make backfill START=2020-01-01
	$(RUN) monitor backfill --start $(START)

verify-sources:               ## re-verify every endpoint in docs/data_sources.md
	$(RUN) python scripts/verify_sources.py

size-check:
	bash scripts/check_repo_size.sh

clean:
	rm -rf .venv .pytest_cache .ruff_cache site/*.html site/data
