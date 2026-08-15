# Daily driver + environment rebuild. The point of this file is that nothing
# about the local Python environment lives only in someone's shell history.
#
# The `run`/`test`/`smoke` targets call .venv/bin/python directly, so they work
# whether or not you have activated anything.

VENV    := .venv
PY      := $(VENV)/bin/python
UV      := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)
export PYTHONPATH := src

.PHONY: help hint env test smoke run clean-env

help:
	@echo "make hint   - print the activation command for an interactive shell"
	@echo "make env    - build/rebuild .venv from requirements.txt (needs uv)"
	@echo "make test   - PYTHONPATH=src pytest"
	@echo "make smoke  - live Alpaca option-chain check (read-only)"
	@echo "make run    - python -m tradingagent --stage all"

# `make` runs in a subshell, so it cannot activate anything for you. This prints
# the line to paste; `make run` skips activation entirely.
hint:
	@echo 'source $(CURDIR)/$(VENV)/bin/activate && export PYTHONPATH=src'

env:
	@command -v $(UV) >/dev/null 2>&1 || { \
	  echo "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	$(UV) venv --python 3.11 $(VENV)
	$(UV) pip install -r requirements.txt
	@echo "Built $(VENV) with $$($(PY) -V). Next: make test"

test:
	$(PY) -m pytest

smoke:
	$(PY) scripts/smoke_option_chain.py

run:
	$(PY) -m tradingagent --stage all

clean-env:
	rm -rf $(VENV)
