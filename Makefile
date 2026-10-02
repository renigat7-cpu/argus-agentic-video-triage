SHELL := /bin/bash
PY ?= .venv/bin/python
PORT ?= 8080

.PHONY: help venv install lock run api eval test lint fmt docker clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtual environment
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip

install: ## Install runtime + dev dependencies
	$(PY) -m pip install -r requirements.txt -e ".[dev]"

lock: ## Freeze exact versions into requirements.lock
	$(PY) -m pip freeze > requirements.lock

api: ## Run the web endpoint
	$(PY) -m uvicorn argus.api:app --host 0.0.0.0 --port $(PORT)

eval: ## Run the evaluation harness
	$(PY) -m argus.eval --clips 12 --out var/eval

test: ## Run the test suite
	$(PY) -m pytest

lint: ## Static checks
	$(PY) -m ruff check src tests
	$(PY) -m mypy src || true

docker: ## Build the container image
	docker build -t argus-triage:0.1.0 .

clean: ## Remove generated artefacts
	rm -rf var .pytest_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +