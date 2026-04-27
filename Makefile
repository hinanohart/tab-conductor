install:
	uv venv && uv pip install -e ".[dev]"
test:
	. .venv/bin/activate && pytest -q -m "not e2e"
lint:
	. .venv/bin/activate && ruff check src/ tests/ && mypy --strict src/tab_conductor/
fmt:
	. .venv/bin/activate && ruff format src/ tests/
clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build *.egg-info

demo:
	. .venv/bin/activate && tab-conductor run tests/fixtures/plans/sample_dag.yaml --mock --state-dir .orchestrator/demo

install-skill:
	bash scripts/install_skill.sh

validate:
	. .venv/bin/activate && tab-conductor validate tests/fixtures/plans/sample_dag.yaml

.PHONY: install test lint fmt clean demo install-skill validate
