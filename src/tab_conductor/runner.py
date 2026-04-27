"""Worker subprocess runner for tab-conductor.

Spawns individual worker subprocesses, parses their NDJSON stdout stream,
maintains heartbeats, tracks cost/stuck status, and escalates kills safely
via process group signals.

Security:
    env allow-list prevents secret leakage — only enumerated variables are
    forwarded to the worker process (R11).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tab_conductor.cost_guard import CostGuard, CostStatus
from tab_conductor.logging_config import get_logger, structured_event
from tab_conductor.stuck_detector import StuckDetector, StuckStatus

_logger: logging.Logger = get_logger("tab_conductor.runner")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Environment variables forwarded to workers by default (allow-list)
_ENV_ALLOW_LIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "USER",
        "TZ",
        "TERM",
        "XDG_RUNTIME_DIR",
    }
)

# Number of recent stdout lines fed to StuckDetector
_CAPTURE_WINDOW = 50

# Heartbeat interval in seconds
_HEARTBEAT_INTERVAL_S = 5.0

# Kill escalation step durations (seconds between escalation levels)
_ESCALATION_WAIT_S = 5.0

# selector poll timeout per iteration
_POLL_TIMEOUT_S = 0.05


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class WorkerSpec:
    """Specification for launching a single worker subprocess.

    Attributes:
        id: Unique ULID identifier for this worker.
        task_id: ID of the task being executed.
        cmd: Command argv to launch the worker.
        env: Limited environment dictionary (allow-list already applied by caller
            or constructed here via :func:`build_worker_env`).
        cap_usd: Per-worker USD spending cap.
        state_dir: Root state directory (``<run_dir>/.orchestrator/<run_id>/``).
        allow_tools: Comma-separated tool allow string passed to Claude CLI.
        deny_tools: Comma-separated tool deny string passed to Claude CLI.
        session_id: ``--session-id`` value for Claude CLI continuation.
    """

    id: str
    task_id: str
    cmd: list[str]
    env: dict[str, str]
    cap_usd: float
    state_dir: Path
    allow_tools: str | None = None
    deny_tools: str | None = None
    session_id: str | None = None


@dataclass
class WorkerEvent:
    """A single event emitted by a worker or the runner.

    Attributes:
        ts: UTC-aware timestamp of the event.
        worker_id: ID of the originating worker.
        kind: Event classification.
        payload: Arbitrary structured data associated with the event.
    """

    ts: datetime
    worker_id: str
    kind: Literal[
        "spawned",
        "stdout_json",
        "stdout_raw",
        "stderr",
        "usage",
        "result",
        "exit",
        "heartbeat",
        "killed",
        "stuck",
    ]
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# WorkerHandle
# ---------------------------------------------------------------------------


class WorkerHandle:
    """Live handle for a running worker subprocess.

    Provides non-blocking poll, heartbeat tracking, cost guard integration,
    and kill escalation.

    Args:
        spec: The :class:`WorkerSpec` used to spawn this worker.
        proc: The running :class:`subprocess.Popen` process.
        cost_guard: Shared :class:`CostGuard` instance.
        stuck_detector: Shared :class:`StuckDetector` instance.
        logger: Optional override logger; defaults to module logger.
    """

    def __init__(
        self,
        spec: WorkerSpec,
        proc: subprocess.Popen[bytes],
        cost_guard: CostGuard,
        stuck_detector: StuckDetector,
        logger: logging.Logger | None = None,
    ) -> None:
        self.spec = spec
        self.proc = proc
        self.pid: int = proc.pid
        self.started_at: datetime = datetime.now(tz=UTC)
        self._cost_guard = cost_guard
        self._stuck_detector = stuck_detector
        self._log = logger or _logger
        self._recent_lines: list[str] = []
        self._last_heartbeat_ts: float = time.monotonic()
        self._stdout_buf = b""
        self._stderr_buf = b""
        self._sel: selectors.DefaultSelector = selectors.DefaultSelector()
        self._sel_registered = False
        self._exit_escalation_started: float | None = None
        self._killed = False
        self._events_dir = spec.state_dir / "events"
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._events_file = self._events_dir / f"{spec.id}.jsonl"

        # Register stdout/stderr with the selector (non-blocking)
        assert proc.stdout is not None
        assert proc.stderr is not None
        try:
            os.set_inheritable(proc.stdout.fileno(), False)
            os.set_inheritable(proc.stderr.fileno(), False)
        except OSError:
            pass
        try:
            self._sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
            self._sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")
            self._sel_registered = True
        except Exception as exc:
            self._log.warning(
                "Failed to register process streams with selector",
                extra={"worker_id": spec.id, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll_once(self) -> WorkerEvent | None:
        """Non-blocking poll: read a line from stdout/stderr and parse it.

        Also emits heartbeat events every :data:`_HEARTBEAT_INTERVAL_S` seconds,
        updates the StuckDetector capture window, and triggers kill escalation
        when cost or stuck thresholds are reached.

        Returns:
            A :class:`WorkerEvent` if new data was available, ``None`` otherwise.
        """
        event: WorkerEvent | None = None

        # --- heartbeat ---
        now_mono = time.monotonic()
        if now_mono - self._last_heartbeat_ts >= _HEARTBEAT_INTERVAL_S:
            self._last_heartbeat_ts = now_mono
            hb_ts = datetime.now(tz=UTC)
            with contextlib.suppress(KeyError):
                self._stuck_detector.update_heartbeat(self.spec.id, hb_ts)
            event = WorkerEvent(
                ts=hb_ts,
                worker_id=self.spec.id,
                kind="heartbeat",
                payload={},
            )
            self._append_event(event)
            return event

        # --- read data ---
        if not self._sel_registered:
            return None

        try:
            ready = self._sel.select(timeout=_POLL_TIMEOUT_S)
        except OSError:
            return None

        for key, _ in ready:
            assert self.proc.stdout is not None
            assert self.proc.stderr is not None
            if key.data == "stdout":
                chunk = os.read(key.fd, 4096)
                if chunk:
                    self._stdout_buf += chunk
                    while b"\n" in self._stdout_buf:
                        line_bytes, self._stdout_buf = self._stdout_buf.split(b"\n", 1)
                        line = line_bytes.decode("utf-8", errors="replace").rstrip()
                        if line:
                            event = self._handle_stdout_line(line)
            elif key.data == "stderr":
                chunk = os.read(key.fd, 4096)
                if chunk:
                    self._stderr_buf += chunk
                    while b"\n" in self._stderr_buf:
                        line_bytes, self._stderr_buf = self._stderr_buf.split(b"\n", 1)
                        line = line_bytes.decode("utf-8", errors="replace").rstrip()
                        if line:
                            event = WorkerEvent(
                                ts=datetime.now(tz=UTC),
                                worker_id=self.spec.id,
                                kind="stderr",
                                payload={"text": line},
                            )
                            self._append_event(event)

        # --- update stuck detector capture ---
        if self._recent_lines:
            capture_text = "\n".join(self._recent_lines[-_CAPTURE_WINDOW:])
            with contextlib.suppress(KeyError):
                self._stuck_detector.update_capture(self.spec.id, capture_text)

        # --- check stuck / cost and escalate if needed ---
        self._maybe_escalate()

        return event

    def is_alive(self) -> bool:
        """Return True if the worker process is still running."""
        return self.proc.poll() is None

    def graceful_terminate(self) -> None:
        """Send SIGINT to the process group (polite interrupt)."""
        self._send_pg_signal(signal.SIGINT)

    def force_terminate(self) -> None:
        """Send SIGTERM to the process group."""
        self._send_pg_signal(signal.SIGTERM)

    def kill(self) -> None:
        """Send SIGKILL to the process group."""
        self._send_pg_signal(signal.SIGKILL)
        self._killed = True

    def kill_escalation(self) -> None:
        """Three-stage kill escalation: SIGINT → SIGTERM → SIGKILL.

        Checks :attr:`CostGuard.escalation_step` or KILL_CANDIDATE stuck
        status to decide which signal to send.
        """
        cost_status = self._cost_guard.check(self.spec.id)
        stuck_status = StuckStatus.LIVE
        with contextlib.suppress(KeyError):
            stuck_status = self._stuck_detector.check(self.spec.id)

        if (
            cost_status not in (CostStatus.OVER_PER_WORKER, CostStatus.OVER_GLOBAL)
            and stuck_status != StuckStatus.KILL_CANDIDATE
        ):
            return

        if self._exit_escalation_started is None:
            self._exit_escalation_started = time.monotonic()

        since_over = time.monotonic() - self._exit_escalation_started
        if since_over < _ESCALATION_WAIT_S:
            self._send_pg_signal(signal.SIGINT)
        elif since_over < _ESCALATION_WAIT_S * 2:
            self._send_pg_signal(signal.SIGTERM)
        else:
            self._send_pg_signal(signal.SIGKILL)
            self._killed = True

    def collect_remaining_output(self) -> str:
        """Drain remaining stdout/stderr after process exit.

        Returns:
            Combined remaining output as a string.
        """
        remaining_parts: list[str] = []
        try:
            assert self.proc.stdout is not None
            rest = self.proc.stdout.read()
            if rest:
                remaining_parts.append(rest.decode("utf-8", errors="replace"))
        except OSError:
            pass
        try:
            assert self.proc.stderr is not None
            rest_err = self.proc.stderr.read()
            if rest_err:
                remaining_parts.append(rest_err.decode("utf-8", errors="replace"))
        except OSError:
            pass
        return "\n".join(remaining_parts)

    def close(self) -> WorkerEvent:
        """Drain output, wait for exit, emit exit event.

        Returns:
            A :class:`WorkerEvent` with kind ``"exit"``.
        """
        self._close_selector()
        remaining = self.collect_remaining_output()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

        returncode = self.proc.returncode or 0
        ev = WorkerEvent(
            ts=datetime.now(tz=UTC),
            worker_id=self.spec.id,
            kind="exit",
            payload={"returncode": returncode, "remaining_output": remaining},
        )
        self._append_event(ev)
        structured_event(
            self._log,
            "worker.exit",
            worker_id=self.spec.id,
            returncode=returncode,
        )
        return ev

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _handle_stdout_line(self, line: str) -> WorkerEvent:
        """Parse a single stdout line as JSON or raw text."""
        self._recent_lines.append(line)
        if len(self._recent_lines) > _CAPTURE_WINDOW * 2:
            self._recent_lines = self._recent_lines[-_CAPTURE_WINDOW:]

        try:
            parsed: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            ev = WorkerEvent(
                ts=datetime.now(tz=UTC),
                worker_id=self.spec.id,
                kind="stdout_raw",
                payload={"text": line},
            )
            self._append_event(ev)
            return ev

        kind: str = parsed.get("type", "")
        ts = datetime.now(tz=UTC)

        if kind == "result":
            total_cost = parsed.get("total_cost_usd", 0.0)
            usage = parsed.get("usage", {})
            tokens_in = int(usage.get("input_tokens", 0))
            tokens_out = int(usage.get("output_tokens", 0))
            self._cost_guard.add_usage(
                self.spec.id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                usd=total_cost if total_cost else None,
            )
            usage_ev = WorkerEvent(
                ts=ts,
                worker_id=self.spec.id,
                kind="usage",
                payload={"tokens_in": tokens_in, "tokens_out": tokens_out, "usd": total_cost},
            )
            self._append_event(usage_ev)
            result_ev = WorkerEvent(
                ts=ts,
                worker_id=self.spec.id,
                kind="result",
                payload=parsed,
            )
            self._append_event(result_ev)
            return result_ev

        ev = WorkerEvent(
            ts=ts,
            worker_id=self.spec.id,
            kind="stdout_json",
            payload=parsed,
        )
        self._append_event(ev)
        return ev

    def _maybe_escalate(self) -> None:
        """Check cost/stuck status and escalate if needed."""
        cost_status = self._cost_guard.check(self.spec.id)
        if cost_status in (CostStatus.OVER_PER_WORKER, CostStatus.OVER_GLOBAL):
            structured_event(
                self._log,
                "worker.cost_over",
                worker_id=self.spec.id,
                cost_status=cost_status.value,
            )
            self.kill_escalation()
            return

        try:
            stuck_status = self._stuck_detector.check(self.spec.id)
            if stuck_status == StuckStatus.KILL_CANDIDATE:
                structured_event(
                    self._log,
                    "worker.stuck_kill",
                    worker_id=self.spec.id,
                    stuck_status=stuck_status.value,
                )
                self.kill_escalation()
        except KeyError:
            pass

    def _send_pg_signal(self, sig: signal.Signals) -> None:
        """Send *sig* to the entire process group of this worker."""
        try:
            pgid = os.getpgid(self.pid)
            os.killpg(pgid, sig)
            structured_event(
                self._log,
                "worker.signal",
                worker_id=self.spec.id,
                signal=sig.name,
                pgid=pgid,
            )
        except ProcessLookupError:
            pass  # process already gone
        except OSError as exc:
            self._log.warning(
                "Failed to send signal to worker",
                extra={"worker_id": self.spec.id, "signal": sig.name, "error": str(exc)},
            )

    def _append_event(self, ev: WorkerEvent) -> None:
        """Append *ev* as a JSONL line to the worker's events file."""
        try:
            payload_copy = dict(ev.payload)
            record = {
                "ts": ev.ts.isoformat().replace("+00:00", "Z"),
                "worker_id": ev.worker_id,
                "kind": ev.kind,
                "payload": payload_copy,
            }
            with self._events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._log.warning(
                "Failed to append event to events file",
                extra={"worker_id": self.spec.id, "error": str(exc)},
            )

    def _close_selector(self) -> None:
        """Unregister all file descriptors from the selector and close it."""
        if not self._sel_registered:
            return
        with contextlib.suppress(Exception):
            assert self.proc.stdout is not None
            self._sel.unregister(self.proc.stdout)
        with contextlib.suppress(Exception):
            assert self.proc.stderr is not None
            self._sel.unregister(self.proc.stderr)
        with contextlib.suppress(Exception):
            self._sel.close()
        self._sel_registered = False


# ---------------------------------------------------------------------------
# Runner (factory)
# ---------------------------------------------------------------------------


def build_worker_env(
    extra: dict[str, str],
    *,
    pass_anthropic_key: bool = False,
    pass_hmac_key: bool = False,
) -> dict[str, str]:
    """Build an allow-listed environment dict for worker subprocesses.

    Only variables listed in :data:`_ENV_ALLOW_LIST` are forwarded from
    ``os.environ``.  ``LC_ALL`` is forced to ``C.UTF-8``.  Optional extras
    can be passed via *extra*.

    Args:
        extra: Additional variables to include verbatim (already validated by
            caller).
        pass_anthropic_key: If ``True``, forward ``ANTHROPIC_API_KEY`` from
            ``os.environ`` if present.
        pass_hmac_key: If ``True``, forward ``TAB_CONDUCTOR_HMAC_KEY`` from
            ``os.environ`` if present.

    Returns:
        A clean environment dict suitable for ``subprocess.Popen(env=...)``.
    """
    env: dict[str, str] = {}
    for key in _ENV_ALLOW_LIST:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val

    # Force safe locale
    env["LC_ALL"] = "C.UTF-8"

    if pass_anthropic_key:
        key_val = os.environ.get("ANTHROPIC_API_KEY")
        if key_val is not None:
            env["ANTHROPIC_API_KEY"] = key_val

    if pass_hmac_key:
        hmac_val = os.environ.get("TAB_CONDUCTOR_HMAC_KEY")
        if hmac_val is not None:
            env["TAB_CONDUCTOR_HMAC_KEY"] = hmac_val

    env.update(extra)
    return env


class Runner:
    """Factory that spawns :class:`WorkerHandle` instances.

    Args:
        cost_guard: Shared cost guard instance.
        stuck_detector: Shared stuck detector instance.
        secret_env_filter: If ``True`` (default), env allow-list is enforced
            even when caller provides ``spec.env`` directly.
        logger: Optional logger override.
    """

    def __init__(
        self,
        *,
        cost_guard: CostGuard,
        stuck_detector: StuckDetector,
        secret_env_filter: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self._cost_guard = cost_guard
        self._stuck_detector = stuck_detector
        self._secret_env_filter = secret_env_filter
        self._log = logger or _logger

    def spawn(self, spec: WorkerSpec) -> WorkerHandle:
        """Spawn the worker described by *spec* and return a live handle.

        Args:
            spec: Worker specification including command, env, and state dir.

        Returns:
            A :class:`WorkerHandle` wrapping the running subprocess.

        Raises:
            FileNotFoundError: If the command binary cannot be found.
            OSError: On other subprocess launch failures.
        """
        env = spec.env
        if self._secret_env_filter:
            # Re-apply allow-list to strip any disallowed vars from spec.env
            allowed_env: dict[str, str] = {}
            for k, v in env.items():
                if (
                    k in _ENV_ALLOW_LIST
                    or k == "LC_ALL"
                    or k == "ANTHROPIC_API_KEY"
                    or k == "TAB_CONDUCTOR_HMAC_KEY"
                    or k.startswith("MOCK_")
                    or k.startswith("TAB_CONDUCTOR_")
                ):
                    allowed_env[k] = v
            env = allowed_env

        # Force safe locale
        env["LC_ALL"] = "C.UTF-8"

        structured_event(
            self._log,
            "runner.spawn",
            worker_id=spec.id,
            task_id=spec.task_id,
            cmd=spec.cmd,
        )

        proc: subprocess.Popen[bytes] = subprocess.Popen(
            spec.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
            bufsize=0,
            start_new_session=True,  # creates new process group (setsid equivalent)
        )

        # Register with stuck detector immediately
        self._stuck_detector.register(spec.id, proc.pid)

        handle = WorkerHandle(
            spec=spec,
            proc=proc,
            cost_guard=self._cost_guard,
            stuck_detector=self._stuck_detector,
            logger=self._log,
        )

        # Emit spawned event
        spawned_ev = WorkerEvent(
            ts=handle.started_at,
            worker_id=spec.id,
            kind="spawned",
            payload={"pid": proc.pid, "task_id": spec.task_id},
        )
        handle._append_event(spawned_ev)

        return handle
