#!/usr/bin/env bash
# Run a 30-second mock demo with 3 parallel workers.
# Usage: bash scripts/demo.sh [extra tab-conductor flags]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
. .venv/bin/activate 2>/dev/null || { echo "run 'make install' first" >&2; exit 1; }
exec tab-conductor run tests/fixtures/plans/sample_dag.yaml --mock --max-parallel 3 --state-dir .orchestrator/demo "$@"
