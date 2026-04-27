"""Integration tests: DAG ordering and cycle detection."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tab_conductor.orchestrator import Orchestrator, OrchestratorConfig, OrchestratorError

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
        poll_max_s=0.5,
        max_parallel=3,
        cap_usd_per_worker=1.0,
        cap_usd_global=5.0,
    )


def _task(
    tid: str,
    depends_on: list[str] | None = None,
    priority: int = 5,
) -> dict[str, object]:
    return {
        "id": tid,
        "kind": "generic",
        "prompt": f"task {tid}",
        "status": "pending",
        "priority": priority,
        "depends_on": depends_on or [],
        "retries": 0,
        "max_retries": 0,
    }


def _read_state(run_dir: Path) -> dict[str, object]:
    import re
    ulid_re = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
    state_file = next(
        d / "state.json"
        for d in run_dir.iterdir()
        if d.is_dir() and ulid_re.match(d.name)
    )
    return json.loads(state_file.read_text())  # type: ignore[return-value]


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_linear_dag_order(tmp_path: Path) -> None:
    """t1 → t2 → t3 linear chain: all done, in order."""
    import os

    tasks = [
        _task("t1"),
        _task("t2", depends_on=["t1"]),
        _task("t3", depends_on=["t2"]),
    ]
    cfg = _base_config(tmp_path)
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
    assert task_statuses == {"t1": "done", "t2": "done", "t3": "done"}


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_parallel_then_join(tmp_path: Path) -> None:
    """t1 and t2 run in parallel; t3 waits for both."""
    import os

    tasks = [
        _task("t1"),
        _task("t2"),
        _task("t3", depends_on=["t1", "t2"]),
    ]
    cfg = _base_config(tmp_path)
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
    assert task_statuses == {"t1": "done", "t2": "done", "t3": "done"}


def test_cycle_detection_raises(tmp_path: Path) -> None:
    """Cycle in DAG raises OrchestratorError immediately (no subprocess needed)."""
    tasks = [
        _task("t1", depends_on=["t2"]),
        _task("t2", depends_on=["t1"]),
    ]
    with pytest.raises(OrchestratorError, match="DAG cycle detected"):
        Orchestrator(_base_config(tmp_path), tasks)
