"""Integration tests: retry logic and exponential backoff."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tab_conductor.orchestrator import Orchestrator, OrchestratorConfig

MOCK_WORKER = Path(__file__).parent.parent / "fixtures" / "mock_worker.sh"


def _has_jq() -> bool:
    return shutil.which("jq") is not None


def _base_config(tmp_dir: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        state_dir=tmp_dir,
        mock_mode=True,
        mock_worker_path=MOCK_WORKER,
        poll_interval_s=0.05,
        backoff_base_s=0.05,
        backoff_factor=1.5,
        poll_max_s=0.3,
        max_parallel=1,
        cap_usd_per_worker=1.0,
        cap_usd_global=5.0,
    )


def _task(
    tid: str,
    max_retries: int = 3,
) -> dict[str, object]:
    return {
        "id": tid,
        "kind": "generic",
        "prompt": f"task {tid}",
        "status": "pending",
        "priority": 5,
        "depends_on": [],
        "retries": 0,
        "max_retries": max_retries,
    }


def _read_state(run_dir: Path) -> dict[str, object]:
    # Find the ULID sub-directory (26-char Crockford Base32)
    import re

    ulid_re = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
    state_file = next(
        d / "state.json" for d in run_dir.iterdir() if d.is_dir() and ulid_re.match(d.name)
    )
    return json.loads(state_file.read_text())  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Fixture: fail-then-succeed mock worker
# We use a counter file to make mock_worker.sh exit non-zero for N calls
# then succeed. We create a wrapper script.
# ---------------------------------------------------------------------------


def _make_failing_worker(tmp_dir: Path, fail_count: int) -> Path:
    """Create a wrapper that fails *fail_count* times then succeeds."""
    counter_file = tmp_dir / "fail_counter"
    counter_file.write_text("0")
    real_mock = MOCK_WORKER

    script = tmp_dir / "failing_worker.sh"
    script.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
COUNT=$(cat "{counter_file}" 2>/dev/null || echo 0)
if [ "$COUNT" -lt "{fail_count}" ]; then
    echo $((COUNT + 1)) > "{counter_file}"
    # emit minimal JSON so runner doesn't choke
    printf '{{"type":"system","task_id":"%s"}}\n' "${{1:-unknown}}"
    exit 1
fi
exec "{real_mock}" "$@"
"""
    )
    script.chmod(0o755)
    return script


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_retry_success_on_third_attempt(tmp_path: Path) -> None:
    """Worker exits 1 twice, succeeds on 3rd → task done."""
    worker = _make_failing_worker(tmp_path, fail_count=2)
    tasks = [_task("t1", max_retries=3)]
    cfg = _base_config(tmp_path)
    cfg.mock_worker_path = worker

    old = os.environ.get("MOCK_SLEEP_S")
    os.environ["MOCK_SLEEP_S"] = "0.1"
    try:
        orch = Orchestrator(cfg, tasks)
        rc = orch.run()
    finally:
        if old is None:
            os.environ.pop("MOCK_SLEEP_S", None)
        else:
            os.environ["MOCK_SLEEP_S"] = old

    assert rc == 0
    state = _read_state(tmp_path)
    task_statuses = {t["id"]: t["status"] for t in state["tasks"]}  # type: ignore[index]
    assert task_statuses["t1"] == "done"


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_terminal_after_max_retries(tmp_path: Path) -> None:
    """Worker always fails; after max_retries=2 → terminal, rc=1."""
    worker = _make_failing_worker(tmp_path, fail_count=99)
    tasks = [_task("t1", max_retries=2)]
    cfg = _base_config(tmp_path)
    cfg.mock_worker_path = worker

    old = os.environ.get("MOCK_SLEEP_S")
    os.environ["MOCK_SLEEP_S"] = "0.05"
    try:
        orch = Orchestrator(cfg, tasks)
        rc = orch.run()
    finally:
        if old is None:
            os.environ.pop("MOCK_SLEEP_S", None)
        else:
            os.environ["MOCK_SLEEP_S"] = old

    assert rc == 1
    state = _read_state(tmp_path)
    task_statuses = {t["id"]: t["status"] for t in state["tasks"]}  # type: ignore[index]
    assert task_statuses["t1"] == "terminal"


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_exponential_backoff_delays_retry(tmp_path: Path) -> None:
    """Retry respects exponential backoff timing."""
    worker = _make_failing_worker(tmp_path, fail_count=1)
    tasks = [_task("t1", max_retries=2)]
    cfg = OrchestratorConfig(
        state_dir=tmp_path,
        mock_mode=True,
        mock_worker_path=MOCK_WORKER,
        poll_interval_s=0.05,
        backoff_base_s=0.2,
        backoff_factor=2.0,
        poll_max_s=2.0,
        max_parallel=1,
        cap_usd_per_worker=1.0,
        cap_usd_global=5.0,
    )
    cfg.mock_worker_path = worker

    old = os.environ.get("MOCK_SLEEP_S")
    os.environ["MOCK_SLEEP_S"] = "0.05"
    try:
        start = time.monotonic()
        orch = Orchestrator(cfg, tasks)
        rc = orch.run()
        elapsed = time.monotonic() - start
    finally:
        if old is None:
            os.environ.pop("MOCK_SLEEP_S", None)
        else:
            os.environ["MOCK_SLEEP_S"] = old

    assert rc == 0
    # With backoff_base_s=0.2, the first retry is delayed by ~0.2s
    assert elapsed >= 0.15  # at least the backoff delay
