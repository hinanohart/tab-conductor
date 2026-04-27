"""Stuck-worker detector for tab-conductor.

Implements a three-pronged check to determine whether a worker subprocess
has become unresponsive:

1. **Heartbeat timeout** — no heartbeat received for more than
   ``heartbeat_timeout_s`` seconds.
2. **Capture repeat** — the text captured from the worker's stdout/stderr
   has been identical for ``capture_repeat_threshold`` consecutive samples.
3. **Process liveness** — ``pgrep`` reports no child processes under the
   worker's PID (indicating the subprocess silently exited).

If **all three** signals fire simultaneously the worker is classified as
:attr:`StuckStatus.STUCK`.  After ``idle_kill_threshold_s`` seconds of
inactivity (no heartbeat updates) the worker becomes
:attr:`StuckStatus.KILL_CANDIDATE`.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import threading
from datetime import UTC, datetime
from enum import Enum

from tab_conductor.logging_config import get_logger, structured_event

_logger: logging.Logger = get_logger("tab_conductor.stuck_detector")


class StuckStatus(Enum):
    """Status returned by :meth:`StuckDetector.check`.

    Values progress from healthy to terminal.
    """

    LIVE = "LIVE"
    """Worker is responsive and healthy."""

    WARN_HEARTBEAT = "WARN_HEARTBEAT"
    """No heartbeat for more than ``heartbeat_timeout_s`` seconds."""

    WARN_CAPTURE = "WARN_CAPTURE"
    """Captured output has been identical for ``capture_repeat_threshold``
    consecutive samples."""

    STUCK = "STUCK"
    """All three indicators (heartbeat, capture, pgrep) fired simultaneously."""

    KILL_CANDIDATE = "KILL_CANDIDATE"
    """No activity for more than ``idle_kill_threshold_s`` seconds;
    supervisor should force-kill."""


def _now_utc() -> datetime:
    """Return the current UTC-aware datetime.

    Returns:
        Current time as a timezone-aware :class:`datetime` (UTC).
    """
    return datetime.now(tz=UTC)


def _sha256(text: str) -> str:
    """Return the hex SHA-256 digest of *text* encoded as UTF-8.

    Args:
        text: Arbitrary string to hash.

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _WorkerRecord:
    """Internal mutable record for a single tracked worker.

    Attributes:
        pid: OS process ID of the worker subprocess.
        last_heartbeat: UTC-aware timestamp of the most recent heartbeat.
        last_capture_hash: SHA-256 of the most recently seen capture text.
        capture_repeat_count: Number of consecutive identical captures.
        registered_at: UTC-aware timestamp when the worker was registered.
    """

    __slots__ = (
        "pid",
        "last_heartbeat",
        "last_capture_hash",
        "capture_repeat_count",
        "registered_at",
    )

    def __init__(self, pid: int, now: datetime) -> None:
        """Initialise with PID and registration time.

        Args:
            pid: OS process ID of the worker subprocess.
            now: Current UTC-aware timestamp (injected for testability).
        """
        self.pid: int = pid
        self.last_heartbeat: datetime = now
        self.last_capture_hash: str | None = None
        self.capture_repeat_count: int = 0
        self.registered_at: datetime = now


class StuckDetector:
    """Three-pronged stuck-worker detector.

    Args:
        heartbeat_timeout_s: Seconds without a heartbeat before
            :attr:`StuckStatus.WARN_HEARTBEAT` fires.  Defaults to 60.
        capture_repeat_threshold: Number of consecutive identical captures
            before :attr:`StuckStatus.WARN_CAPTURE` fires.  Defaults to 3.
        idle_kill_threshold_s: Seconds without any activity before
            :attr:`StuckStatus.KILL_CANDIDATE` fires.  Defaults to 600.

    Example:
        >>> from datetime import datetime, timezone
        >>> sd = StuckDetector()
        >>> sd.register("w1", pid=12345)
        >>> sd.check("w1").value
        'LIVE'
    """

    def __init__(
        self,
        *,
        heartbeat_timeout_s: int = 60,
        capture_repeat_threshold: int = 3,
        idle_kill_threshold_s: int = 600,
    ) -> None:
        """Initialise with detection thresholds.

        Args:
            heartbeat_timeout_s: Seconds without heartbeat → WARN_HEARTBEAT.
            capture_repeat_threshold: Consecutive identical captures →
                WARN_CAPTURE.
            idle_kill_threshold_s: Total idle seconds → KILL_CANDIDATE.
        """
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._capture_repeat_threshold = capture_repeat_threshold
        self._idle_kill_threshold_s = idle_kill_threshold_s
        self._workers: dict[str, _WorkerRecord] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, worker_id: str, pid: int) -> None:
        """Register a new worker for monitoring.

        Args:
            worker_id: Unique identifier for the worker subprocess.
            pid: OS process ID of the worker subprocess.
        """
        now = _now_utc()
        with self._lock:
            self._workers[worker_id] = _WorkerRecord(pid=pid, now=now)
        structured_event(_logger, "stuck.registered", worker_id=worker_id, pid=pid)

    def update_heartbeat(self, worker_id: str, ts: datetime) -> None:
        """Record a heartbeat for *worker_id*.

        Args:
            worker_id: The worker whose heartbeat was received.
            ts: UTC-aware timestamp of the heartbeat (usually ``datetime.now(UTC)``).

        Raises:
            KeyError: If *worker_id* has not been registered.
        """
        with self._lock:
            rec = self._workers[worker_id]
            rec.last_heartbeat = ts
        structured_event(_logger, "stuck.heartbeat", worker_id=worker_id)

    def update_capture(self, worker_id: str, capture_text: str) -> None:
        """Record a new capture sample and update repeat counter.

        If the SHA-256 of *capture_text* matches the previous sample the
        repeat counter is incremented; if it differs the counter resets to 0.

        Args:
            worker_id: The worker whose output was captured.
            capture_text: Raw text from the worker's stdout/stderr.

        Raises:
            KeyError: If *worker_id* has not been registered.
        """
        digest = _sha256(capture_text)
        with self._lock:
            rec = self._workers[worker_id]
            if rec.last_capture_hash == digest:
                rec.capture_repeat_count += 1
            else:
                rec.last_capture_hash = digest
                # count=1: this is the first observation of a new text; if the
                # *next* sample is also identical the counter becomes 2, and so
                # on.  Threshold of N means N consecutive identical samples.
                rec.capture_repeat_count = 1
        structured_event(
            _logger,
            "stuck.capture",
            worker_id=worker_id,
            repeat_count=rec.capture_repeat_count,
        )

    def check(self, worker_id: str, *, now: datetime | None = None) -> StuckStatus:
        """Evaluate the stuck status of *worker_id*.

        Checks are performed in this order (most severe first):

        1. Idle kill threshold exceeded → :attr:`StuckStatus.KILL_CANDIDATE`
        2. All three signals active → :attr:`StuckStatus.STUCK`
        3. Heartbeat timed out only → :attr:`StuckStatus.WARN_HEARTBEAT`
        4. Capture repeating only → :attr:`StuckStatus.WARN_CAPTURE`
        5. Otherwise → :attr:`StuckStatus.LIVE`

        Args:
            worker_id: The worker to evaluate.
            now: Override for current time (UTC-aware).  Defaults to
                ``datetime.now(UTC)``.  Inject in tests to avoid wall-clock
                dependency.

        Returns:
            The most severe :class:`StuckStatus` that applies.

        Raises:
            KeyError: If *worker_id* has not been registered.
        """
        effective_now = now if now is not None else _now_utc()

        with self._lock:
            rec = self._workers[worker_id]
            pid = rec.pid
            last_hb = rec.last_heartbeat
            repeat = rec.capture_repeat_count

        elapsed_hb = (effective_now - last_hb).total_seconds()
        elapsed_idle = elapsed_hb  # idle = time since last heartbeat

        # 1. Idle kill threshold
        if elapsed_idle >= self._idle_kill_threshold_s:
            status = StuckStatus.KILL_CANDIDATE
            structured_event(
                _logger,
                "stuck.check",
                worker_id=worker_id,
                status=status.value,
                elapsed_idle_s=elapsed_idle,
            )
            return status

        hb_bad = elapsed_hb >= self._heartbeat_timeout_s
        cap_bad = repeat >= self._capture_repeat_threshold
        proc_bad = not self.pgrep_alive(pid)

        # 2. All three signals → STUCK
        if hb_bad and cap_bad and proc_bad:
            status = StuckStatus.STUCK
        # 3. Heartbeat timeout
        elif hb_bad:
            status = StuckStatus.WARN_HEARTBEAT
        # 4. Repeated capture
        elif cap_bad:
            status = StuckStatus.WARN_CAPTURE
        else:
            status = StuckStatus.LIVE

        if status is not StuckStatus.LIVE:
            structured_event(
                _logger,
                "stuck.check",
                worker_id=worker_id,
                status=status.value,
                elapsed_hb_s=elapsed_hb,
                capture_repeats=repeat,
                proc_alive=not proc_bad,
            )
        return status

    def pgrep_alive(self, pid: int) -> bool:
        """Return ``True`` if *pid* has living child processes.

        Runs ``pgrep -P <pid>`` (list children of *pid*) and treats any
        output as evidence the worker is still alive.  If ``pgrep`` is not
        installed the method falls back to ``True`` (conservative: assume
        alive) so that heartbeat and capture checks remain the sole guards.

        Args:
            pid: OS process ID to check.

        Returns:
            ``True`` if child processes were found or ``pgrep`` is unavailable.
            ``False`` if ``pgrep`` found no children.
        """
        try:
            result = subprocess.run(
                ["pgrep", "-P", str(pid)],
                shell=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except FileNotFoundError:
            # pgrep not available; fall back to assuming alive
            _logger.warning(
                "pgrep not found; skipping process liveness check",
                extra={"pid": pid},
            )
            return True
        except subprocess.TimeoutExpired:
            _logger.warning(
                "pgrep timed out; assuming process alive",
                extra={"pid": pid},
            )
            return True
