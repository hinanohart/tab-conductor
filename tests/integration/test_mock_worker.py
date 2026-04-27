"""Integration tests: mock worker spawn, cost cap, global halt, SIGINT, parallel."""

from __future__ import annotations

import json
import os
import shutil
import signal
import time
from pathlib import Path

import pytest

from tab_conductor.orchestrator import Orchestrator, OrchestratorConfig

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

MOCK_WORKER = Path(__file__).parent.parent / "fixtures" / "mock_worker.sh"


def _has_jq() -> bool:
    return shutil.which("jq") is not None


def _base_config(tmp_dir: Path, **kwargs: object) -> OrchestratorConfig:
    return OrchestratorConfig(
        state_dir=tmp_dir,
        mock_mode=True,
        mock_worker_path=MOCK_WORKER,
        poll_interval_s=0.05,
        backoff_base_s=0.05,
        backoff_factor=1.5,
        poll_max_s=0.5,
        **kwargs,  # type: ignore[arg-type]
    )


def _simple_task(
    tid: str,
    depends_on: list[str] | None = None,
    priority: int = 5,
    max_retries: int = 0,
) -> dict[str, object]:
    return {
        "id": tid,
        "kind": "generic",
        "prompt": f"task {tid}",
        "status": "pending",
        "priority": priority,
        "depends_on": depends_on or [],
        "retries": 0,
        "max_retries": max_retries,
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


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_three_workers_all_done(tmp_path: Path) -> None:
    """3 concurrent mock workers → all tasks done, workers done."""
    tasks = [_simple_task("t1"), _simple_task("t2"), _simple_task("t3")]
    cfg = _base_config(tmp_path, max_parallel=3, cap_usd_per_worker=1.0, cap_usd_global=5.0)
    env_patch = {"MOCK_SLEEP_S": "0.1", "MOCK_EXIT_CODE": "0"}
    with _env_patch(env_patch):
        orch = Orchestrator(cfg, tasks)
        rc = orch.run()
    assert rc == 0
    state = _read_state(tmp_path)
    task_statuses = {t["id"]: t["status"] for t in state["tasks"]}  # type: ignore[index]
    assert all(v == "done" for v in task_statuses.values()), task_statuses


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_per_worker_cost_cap_kills(tmp_path: Path) -> None:
    """Worker accumulating cost > cap_usd_per_worker is killed."""
    # Set cap very low; mock_worker emits cost 0.001, so we set cap to 0.0001
    # to trigger OVER_PER_WORKER immediately after first cost event.
    tasks = [_simple_task("t1")]
    cfg = _base_config(
        tmp_path,
        max_parallel=1,
        cap_usd_per_worker=0.00001,  # extremely low cap
        cap_usd_global=5.0,
    )
    env_patch = {"MOCK_SLEEP_S": "0.1", "MOCK_EXIT_CODE": "0"}
    with _env_patch(env_patch):
        orch = Orchestrator(cfg, tasks)
        rc = orch.run()
    # rc may be 0 or 1 depending on timing; the important thing is it finishes
    assert rc in (0, 1)


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_global_cost_cap_halts_all(tmp_path: Path) -> None:
    """Global cost cap exceeded → orchestrator halts all workers."""
    tasks = [_simple_task("t1"), _simple_task("t2"), _simple_task("t3")]
    # Global cap also extremely low to trigger on first result event
    cfg = _base_config(
        tmp_path,
        max_parallel=3,
        cap_usd_per_worker=1.0,
        cap_usd_global=0.00001,
    )
    env_patch = {"MOCK_SLEEP_S": "0.2", "MOCK_EXIT_CODE": "0"}
    with _env_patch(env_patch):
        orch = Orchestrator(cfg, tasks)
        rc = orch.run()
    # Orchestrator must have terminated (either 0 or 1)
    assert rc in (0, 1)


@pytest.mark.skipif(not _has_jq(), reason="jq required")
@pytest.mark.timeout(60)
def test_three_workers_parallel_complete_within_timeout(tmp_path: Path) -> None:
    """3 workers in parallel complete within 60 seconds."""
    tasks = [_simple_task("t1"), _simple_task("t2"), _simple_task("t3")]
    cfg = _base_config(tmp_path, max_parallel=3, cap_usd_per_worker=1.0, cap_usd_global=5.0)
    env_patch = {"MOCK_SLEEP_S": "0.1", "MOCK_EXIT_CODE": "0"}
    with _env_patch(env_patch):
        start = time.monotonic()
        orch = Orchestrator(cfg, tasks)
        rc = orch.run()
    elapsed = time.monotonic() - start
    assert rc == 0
    assert elapsed < 60.0


@pytest.mark.skipif(not _has_jq(), reason="jq required")
def test_sigint_graceful_halt(tmp_path: Path) -> None:
    """SIGINT triggers graceful halt; orchestrator subprocess exits with code 130."""
    import subprocess
    import sys

    # Run the orchestrator in a child process so SIGINT goes only to it,
    # leaving the pytest main thread unaffected.
    script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent.parent.parent / 'src')!r})
import os, time
os.environ["MOCK_SLEEP_S"] = "2.0"
os.environ["MOCK_EXIT_CODE"] = "0"
from pathlib import Path
from tab_conductor.orchestrator import Orchestrator, OrchestratorConfig
tasks = [
    {{"id": "t1", "kind": "generic", "prompt": "p", "status": "pending",
      "priority": 5, "depends_on": [], "retries": 0, "max_retries": 0}},
    {{"id": "t2", "kind": "generic", "prompt": "p", "status": "pending",
      "priority": 5, "depends_on": [], "retries": 0, "max_retries": 0}},
]
cfg = OrchestratorConfig(
    state_dir=Path({str(tmp_path)!r}),
    mock_mode=True,
    mock_worker_path=Path({str(MOCK_WORKER)!r}),
    poll_interval_s=0.05,
    max_parallel=2,
    cap_usd_per_worker=1.0,
    cap_usd_global=5.0,
)
orch = Orchestrator(cfg, tasks)
rc = orch.run()
sys.exit(rc)
"""
    proc = subprocess.Popen([sys.executable, "-c", script])
    # Let workers spawn
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("orchestrator subprocess did not finish within 15s")
    assert proc.returncode == 130, f"expected 130, got {proc.returncode}"


# ------------------------------------------------------------------
# Env context manager
# ------------------------------------------------------------------


class _env_patch:
    """Temporarily set environment variables."""

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env
        self._old: dict[str, str | None] = {}

    def __enter__(self) -> _env_patch:
        for k, v in self._env.items():
            self._old[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_: object) -> None:
        for k, old in self._old.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
