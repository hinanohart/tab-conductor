"""Orchestrator: DAG-aware supervisor loop for tab-conductor workers.

Resolves task dependency graphs, maintains a ready queue, spawns worker
subprocesses up to the configured parallel limit, aggregates events, enforces
cost caps, retries failed tasks with exponential backoff, and handles graceful
shutdown on SIGINT/SIGTERM.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tab_conductor import ulid
from tab_conductor.cost_guard import CostGuard, CostStatus
from tab_conductor.exceptions import TabConductorError
from tab_conductor.logging_config import get_logger, structured_event
from tab_conductor.runner import Runner, WorkerSpec, build_worker_env
from tab_conductor.state import StateStore
from tab_conductor.stuck_detector import StuckDetector, StuckStatus

_logger: logging.Logger = get_logger("tab_conductor.orchestrator")

# Valid terminal task statuses
_TERMINAL_TASK_STATUSES = frozenset({"done", "terminal"})

# Valid terminal worker statuses
_TERMINAL_WORKER_STATUSES = frozenset({"done", "killed", "failed"})


class OrchestratorError(TabConductorError):
    """Raised for orchestrator-level configuration or DAG errors."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    """Configuration for the :class:`Orchestrator`.

    Attributes:
        state_dir: Root directory for state files (events/, logs/, transcripts/).
        max_parallel: Maximum number of concurrent worker processes.
        cap_usd_per_worker: Per-worker USD spending cap.
        cap_usd_global: Global USD spending cap across all workers.
        poll_interval_s: Supervisor loop poll interval in seconds.
        backoff_factor: Exponential backoff multiplier for retries.
        backoff_base_s: Base backoff duration in seconds.
        poll_max_s: Maximum backoff sleep time in seconds.
        require_hmac: Whether to pass HMAC key to workers.
        mock_mode: If True, use mock_worker_path instead of claude CLI.
        mock_worker_path: Path to the mock worker shell script.
        worker_command_template: CLI argv template for building worker commands.
        deny_tools_default: Default ``--disallowedTools`` string.
        allow_tools_default: Default ``--allowedTools`` string.
    """

    state_dir: Path
    max_parallel: int = 3
    cap_usd_per_worker: float = 1.0
    cap_usd_global: float = 5.0
    poll_interval_s: float = 0.5
    backoff_factor: float = 1.5
    backoff_base_s: float = 1.0
    poll_max_s: float = 5.0
    require_hmac: bool = False
    mock_mode: bool = False
    mock_worker_path: Path | None = None
    worker_command_template: list[str] | None = None
    deny_tools_default: str | None = (
        "Read(.env*) Read(*credentials*) Read(*token*)"
        " Bash(sudo *) Bash(curl * | sh) Bash(rm -rf /*)"
    )
    allow_tools_default: str | None = None


# ---------------------------------------------------------------------------
# Internal task record
# ---------------------------------------------------------------------------


@dataclass
class _TaskRecord:
    """Internal mutable record for tracking a task through its lifecycle."""

    id: str
    kind: str
    prompt: str
    status: str
    priority: int
    depends_on: list[str]
    retries: int
    max_retries: int
    assigned_to: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    result: dict[str, Any] | None = None
    last_error: str | None = None
    retry_at: float = 0.0  # monotonic timestamp; 0 = immediately ready
    worker_id: str | None = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """DAG-aware supervisor loop.

    Args:
        config: Orchestrator configuration.
        tasks: List of task dicts conforming to the task schema.
        logger: Optional logger override; defaults to module logger.
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        tasks: list[dict[str, Any]],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._log = logger or _logger
        self._run_id = ulid.new()

        # Create sub-directories
        self._run_dir = config.state_dir / self._run_id
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "events").mkdir(exist_ok=True)
        (self._run_dir / "logs").mkdir(exist_ok=True)
        (self._run_dir / "transcripts").mkdir(exist_ok=True)

        # Parse and validate task list
        self._tasks: dict[str, _TaskRecord] = {}
        for td in tasks:
            rec = _TaskRecord(
                id=td["id"],
                kind=td.get("kind", "generic"),
                prompt=td["prompt"],
                status=td.get("status", "pending"),
                priority=int(td.get("priority", 5)),
                depends_on=list(td.get("depends_on", [])),
                retries=int(td.get("retries", 0)),
                max_retries=int(td.get("max_retries", 3)),
                assigned_to=td.get("assigned_to"),
                last_error=td.get("last_error"),
            )
            self._tasks[rec.id] = rec

        # Validate DAG (no cycles)
        self._check_dag_cycles()

        # Shared guards
        self._cost_guard = CostGuard(
            per_worker_usd=config.cap_usd_per_worker,
            global_usd=config.cap_usd_global,
        )
        self._stuck_detector = StuckDetector(
            heartbeat_timeout_s=60,
            capture_repeat_threshold=3,
            idle_kill_threshold_s=600,
        )
        self._runner = Runner(
            cost_guard=self._cost_guard,
            stuck_detector=self._stuck_detector,
            secret_env_filter=True,
            logger=self._log,
        )

        # State store
        self._store = StateStore(self._run_dir)
        self._init_state()

        # Runtime tracking
        self._active_handles: dict[str, Any] = {}  # worker_id -> WorkerHandle
        self._task_worker_map: dict[str, str] = {}  # task_id -> worker_id

        # Shutdown flag
        self._halt_event = threading.Event()
        self._halt_lock = threading.Lock()
        self._exit_code: int = 0

        structured_event(
            self._log,
            "orchestrator.init",
            run_id=self._run_id,
            task_count=len(self._tasks),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Run the supervisor loop until all tasks complete or halt.

        Returns:
            Exit code: 0 = all done, 1 = partial failure, 130 = SIGINT.
        """
        # Signal handlers can only be set from the main thread.
        is_main = threading.current_thread() is threading.main_thread()
        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)
        if is_main:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            self._update_state_status("running")
            structured_event(self._log, "orchestrator.run_start", run_id=self._run_id)
            self._supervisor_loop()
        finally:
            if is_main:
                signal.signal(signal.SIGINT, original_sigint)
                signal.signal(signal.SIGTERM, original_sigterm)

        return self._exit_code

    # ------------------------------------------------------------------
    # Internal: supervisor loop
    # ------------------------------------------------------------------

    def _supervisor_loop(self) -> None:
        """Main supervisor loop."""
        while True:
            if self._halt_event.is_set():
                self._do_halt()
                break

            # Check if all tasks are terminal
            if self._all_tasks_terminal():
                self._finalize()
                break

            # 1. Spawn ready tasks
            self._spawn_ready_workers()

            # 2. Poll active workers
            completed_workers: list[str] = []
            for worker_id, handle in list(self._active_handles.items()):
                handle.poll_once()

                # Check global cost
                cost_status = self._cost_guard.check(worker_id)
                if cost_status == CostStatus.OVER_GLOBAL:
                    structured_event(
                        self._log, "orchestrator.global_cost_over", run_id=self._run_id
                    )
                    handle.kill_escalation()

                # Check stuck
                try:
                    stuck_status = self._stuck_detector.check(worker_id)
                    if stuck_status == StuckStatus.KILL_CANDIDATE:
                        handle.kill_escalation()
                        self._record_stuck_event(worker_id, handle)
                except KeyError:
                    pass

                # Detect exit
                if not handle.is_alive():
                    completed_workers.append(worker_id)

            # 3. Process completed workers
            for worker_id in completed_workers:
                self._process_worker_exit(worker_id)

            time.sleep(self._config.poll_interval_s)

    def _spawn_ready_workers(self) -> None:
        """Spawn workers for ready tasks up to the parallel limit."""
        if len(self._active_handles) >= self._config.max_parallel:
            return

        ready = self._get_ready_tasks()
        # Sort by priority desc, then stable order
        ready.sort(key=lambda t: -t.priority)

        for task in ready:
            if len(self._active_handles) >= self._config.max_parallel:
                break
            if task.id in self._task_worker_map:
                continue  # already assigned

            worker_id = ulid.new()
            cmd = self._build_worker_cmd(
                {"id": task.id, "prompt": task.prompt, "kind": task.kind},
                worker_id,
            )
            env = build_worker_env(
                {"MOCK_SLEEP_S": os.environ.get("MOCK_SLEEP_S", "0.3")},
                pass_anthropic_key=True,
                pass_hmac_key=self._config.require_hmac,
            )
            spec = WorkerSpec(
                id=worker_id,
                task_id=task.id,
                cmd=cmd,
                env=env,
                cap_usd=self._config.cap_usd_per_worker,
                state_dir=self._run_dir,
                deny_tools=self._config.deny_tools_default,
                allow_tools=self._config.allow_tools_default,
            )

            try:
                handle = self._runner.spawn(spec)
            except (FileNotFoundError, OSError) as exc:
                self._log.error(
                    "Failed to spawn worker",
                    extra={"worker_id": worker_id, "task_id": task.id, "error": str(exc)},
                )
                task.status = "failed"
                task.last_error = str(exc)
                self._maybe_retry_task(task)
                continue

            task.status = "running"
            task.assigned_to = worker_id
            task.started_at = datetime.now(tz=UTC)
            task.worker_id = worker_id
            self._active_handles[worker_id] = handle
            self._task_worker_map[task.id] = worker_id

            self._update_state_worker(
                worker_id=worker_id,
                task_id=task.id,
                pid=handle.pid,
                status="running",
            )
            self._update_state_task(task)

            structured_event(
                self._log,
                "orchestrator.worker_spawned",
                run_id=self._run_id,
                worker_id=worker_id,
                task_id=task.id,
            )

    def _process_worker_exit(self, worker_id: str) -> None:
        """Handle a worker that has exited."""
        handle = self._active_handles.pop(worker_id, None)
        if handle is None:
            return

        exit_ev = handle.close()
        returncode = exit_ev.payload.get("returncode", 0)
        task_id = handle.spec.task_id

        # Find the task
        task = self._tasks.get(task_id)
        if task is None:
            return

        # Remove task->worker mapping
        self._task_worker_map.pop(task_id, None)

        if returncode == 0:
            task.status = "done"
            task.ended_at = datetime.now(tz=UTC)
            structured_event(
                self._log,
                "orchestrator.task_done",
                run_id=self._run_id,
                task_id=task_id,
                worker_id=worker_id,
            )
        else:
            task.last_error = f"Worker exited with code {returncode}"
            task.status = "failed"
            task.ended_at = datetime.now(tz=UTC)
            self._maybe_retry_task(task)

        self._update_state_task(task)
        self._update_state_worker(
            worker_id=worker_id,
            task_id=task_id,
            pid=handle.pid,
            status="done" if returncode == 0 else "failed",
        )

        cost_summary = self._cost_guard.summary()
        self._update_state_cost(cost_summary.get("total_usd", 0.0))

    def _maybe_retry_task(self, task: _TaskRecord) -> None:
        """Retry task if below max_retries, else mark terminal."""
        if task.retries < task.max_retries:
            task.retries += 1
            task.status = "pending"
            task.assigned_to = None
            task.worker_id = None
            # Exponential backoff
            backoff = min(
                self._config.backoff_base_s
                * (self._config.backoff_factor ** (task.retries - 1)),
                self._config.poll_max_s,
            )
            task.retry_at = time.monotonic() + backoff
            structured_event(
                self._log,
                "orchestrator.task_retry",
                run_id=self._run_id,
                task_id=task.id,
                retries=task.retries,
                backoff_s=backoff,
            )
        else:
            task.status = "terminal"
            self._exit_code = 1
            structured_event(
                self._log,
                "orchestrator.task_terminal",
                run_id=self._run_id,
                task_id=task.id,
                retries=task.retries,
            )

    def _get_ready_tasks(self) -> list[_TaskRecord]:
        """Return tasks that are pending, deps done, and backoff elapsed."""
        now_mono = time.monotonic()
        done_ids = {
            t.id for t in self._tasks.values() if t.status == "done"
        }
        ready: list[_TaskRecord] = []
        for task in self._tasks.values():
            if task.status != "pending":
                continue
            if task.id in self._task_worker_map:
                continue
            if not all(dep in done_ids for dep in task.depends_on):
                continue
            if now_mono < task.retry_at:
                continue
            ready.append(task)
        return ready

    def _all_tasks_terminal(self) -> bool:
        """Return True when every task is in a terminal status."""
        return all(t.status in _TERMINAL_TASK_STATUSES for t in self._tasks.values())

    def _finalize(self) -> None:
        """Mark run as completed."""
        all_done = all(t.status == "done" for t in self._tasks.values())
        final_status = "completed" if all_done else "failed"
        self._update_state_status(final_status)
        structured_event(
            self._log,
            "orchestrator.finalized",
            run_id=self._run_id,
            status=final_status,
        )

    def _do_halt(self) -> None:
        """Graceful halt: SIGINT all workers, wait, then SIGTERM."""
        structured_event(self._log, "orchestrator.halting", run_id=self._run_id)
        self._update_state_status("halting")

        for handle in list(self._active_handles.values()):
            handle.graceful_terminate()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(not h.is_alive() for h in self._active_handles.values()):
                break
            time.sleep(0.1)

        for handle in list(self._active_handles.values()):
            if handle.is_alive():
                handle.force_terminate()

        # Close all
        for worker_id in list(self._active_handles.keys()):
            handle = self._active_handles.pop(worker_id)
            handle.close()

        self._update_state_status("halted")
        structured_event(self._log, "orchestrator.halted", run_id=self._run_id)

    def _handle_signal(self, signum: int, frame: object) -> None:
        """SIGINT / SIGTERM handler: trigger graceful halt."""
        if signum == signal.SIGINT:
            self._exit_code = 130
        else:
            self._exit_code = 1
        self._halt_event.set()

    # ------------------------------------------------------------------
    # Worker command builder
    # ------------------------------------------------------------------

    def _build_worker_cmd(self, task: dict[str, Any], worker_id: str) -> list[str]:
        """Build the subprocess argv for a worker.

        Args:
            task: Task dict with ``id`` and ``prompt`` fields.
            worker_id: ULID of the worker (used for session-id).

        Returns:
            Argv list for :class:`subprocess.Popen`.
        """
        if self._config.mock_mode:
            mock_path = self._config.mock_worker_path
            if mock_path is None:
                raise OrchestratorError("mock_mode=True but mock_worker_path is not set")
            return [
                str(mock_path),
                task["id"],
                task.get("prompt", ""),
            ]

        if self._config.worker_command_template:
            return list(self._config.worker_command_template)

        cmd = [
            "claude",
            "-p",
            task.get("prompt", ""),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--max-budget-usd",
            str(self._config.cap_usd_per_worker),
            "--session-id",
            worker_id,
        ]
        deny = self._config.deny_tools_default
        allow = self._config.allow_tools_default
        if deny:
            cmd += ["--disallowedTools", deny]
        if allow:
            cmd += ["--allowedTools", allow]
        return cmd

    # ------------------------------------------------------------------
    # DAG validation
    # ------------------------------------------------------------------

    def _check_dag_cycles(self) -> None:
        """Topological sort with gray/black coloring to detect cycles.

        Raises:
            OrchestratorError: If a cycle is detected.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {tid: WHITE for tid in self._tasks}
        path: list[str] = []

        def dfs(node: str) -> None:
            color[node] = GRAY
            path.append(node)
            for dep in self._tasks[node].depends_on:
                if dep not in self._tasks:
                    raise OrchestratorError(
                        f"Task '{node}' depends on unknown task '{dep}'"
                    )
                if color[dep] == GRAY:
                    cycle_path = " -> ".join(path + [dep])
                    raise OrchestratorError(f"DAG cycle detected: {cycle_path}")
                if color[dep] == WHITE:
                    dfs(dep)
            path.pop()
            color[node] = BLACK

        for tid in list(self._tasks.keys()):
            if color[tid] == WHITE:
                dfs(tid)

    # ------------------------------------------------------------------
    # State management helpers
    # ------------------------------------------------------------------

    def _init_state(self) -> None:
        """Write initial state.json to the state store."""
        now_iso = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        tasks_list = [self._task_to_dict(t) for t in self._tasks.values()]
        initial: dict[str, Any] = {
            "run_id": self._run_id,
            "version": 0,
            "started_at": now_iso,
            "ended_at": None,
            "status": "initializing",
            "cost_usd_total": 0.0,
            "cost_cap_usd_global": self._config.cap_usd_global,
            "workers": [],
            "tasks": tasks_list,
            "events": [],
        }
        self._store.init(initial)

    def _update_state_status(self, status: str) -> None:
        """Update the top-level run status in state.json."""
        def mutator(s: dict[str, Any]) -> dict[str, Any]:
            s["status"] = status
            if status in ("completed", "failed", "halted"):
                s["ended_at"] = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
            return s

        try:
            self._store.update(mutator)
        except Exception as exc:
            self._log.warning("State update failed", extra={"error": str(exc)})

    def _update_state_task(self, task: _TaskRecord) -> None:
        """Update a single task's record in state.json."""
        task_dict = self._task_to_dict(task)

        def mutator(s: dict[str, Any]) -> dict[str, Any]:
            tasks: list[dict[str, Any]] = s.get("tasks", [])
            for i, t in enumerate(tasks):
                if t["id"] == task.id:
                    tasks[i] = task_dict
                    break
            else:
                tasks.append(task_dict)
            s["tasks"] = tasks
            return s

        try:
            self._store.update(mutator)
        except Exception as exc:
            self._log.warning("Task state update failed", extra={"error": str(exc)})

    def _update_state_worker(
        self,
        *,
        worker_id: str,
        task_id: str,
        pid: int,
        status: str,
    ) -> None:
        """Upsert a worker record in state.json."""
        now_iso = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        worker_dict: dict[str, Any] = {
            "id": worker_id,
            "status": status,
            "pid": pid,
            "started_at": now_iso,
            "heartbeat_ts": now_iso,
            "task_id": task_id,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "retries": 0,
            "last_error": None,
        }

        def mutator(s: dict[str, Any]) -> dict[str, Any]:
            workers: list[dict[str, Any]] = s.get("workers", [])
            for i, w in enumerate(workers):
                if w["id"] == worker_id:
                    workers[i]["status"] = status
                    workers[i]["heartbeat_ts"] = now_iso
                    return s
            workers.append(worker_dict)
            s["workers"] = workers
            return s

        try:
            self._store.update(mutator)
        except Exception as exc:
            self._log.warning("Worker state update failed", extra={"error": str(exc)})

    def _update_state_cost(self, total_usd: float) -> None:
        """Update the global cost total in state.json."""
        def mutator(s: dict[str, Any]) -> dict[str, Any]:
            s["cost_usd_total"] = total_usd
            return s

        try:
            self._store.update(mutator)
        except Exception as exc:
            self._log.warning("Cost state update failed", extra={"error": str(exc)})

    def _record_stuck_event(self, worker_id: str, handle: object) -> None:
        """Append a stuck event to the state event list."""
        now_iso = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        ev_ref: dict[str, Any] = {
            "ts": now_iso,
            "kind": "stuck",
            "worker_id": worker_id,
            "task_id": None,
            "payload": {},
        }

        def mutator(s: dict[str, Any]) -> dict[str, Any]:
            evs: list[dict[str, Any]] = s.get("events", [])
            evs.append(ev_ref)
            s["events"] = evs
            return s

        try:
            self._store.update(mutator)
        except Exception as exc:
            self._log.warning("Stuck event state update failed", extra={"error": str(exc)})

    @staticmethod
    def _task_to_dict(task: _TaskRecord) -> dict[str, Any]:
        """Serialize a _TaskRecord to a state-schema-compatible dict."""
        return {
            "id": task.id,
            "kind": task.kind,
            "prompt": task.prompt,
            "status": task.status,
            "priority": task.priority,
            "depends_on": task.depends_on,
            "retries": task.retries,
            "max_retries": task.max_retries,
            "assigned_to": task.assigned_to,
            "started_at": (
                task.started_at.isoformat().replace("+00:00", "Z")
                if task.started_at
                else None
            ),
            "ended_at": (
                task.ended_at.isoformat().replace("+00:00", "Z")
                if task.ended_at
                else None
            ),
            "result": task.result,
            "last_error": task.last_error,
        }
