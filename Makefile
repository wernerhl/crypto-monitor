# crypto-monitor — every target runs from a clean clone with `uv` installed.
.PHONY: all sync lint test test-live fetch compute site backfill clean size-check verify-sources

UV ?= uv
# PYTHONPATH makes the source importable even where the editable .pth is ignored
# (macOS marks files in .venv hidden and Python 3.12 skips hidden .pth files).
RUN := PYTHONPATH=$(CURDIR)/src $(UV) run --no-sync

all: sync compute site        ## rebuild every processed table from raw files and regenerate the site

sync:                         ## install pinned dependencies into .venv
	$(UV) sync --all-extras --frozen
	-chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

test:
	$(RUN) pytest

test-live:                    ## live adapter tests (network)
	LIVE=1 $(RUN) pytest -m live

fetch:                        ## fetch every daily dataset for today (idempotent)
	$(RUN) monitor fetch daily

compute:                      ## replay every raw file into the processed tables (no network)
	$(RUN) monitor compute all --rebuild

site:                         ## render static site into ./site
	$(RUN) monitor site render

backfill:                     ## usage: make backfill START=2020-01-01
	$(RUN) monitor backfill --start $(START)

verify-sources:               ## re-verify every endpoint in docs/data_sources.md
	$(RUN) python scripts/verify_sources.py

size-check:
	bash scripts/check_repo_size.sh

clean:
	rm -rf .venv .pytest_cache .ruff_cache site/*.html site/data/*.json
