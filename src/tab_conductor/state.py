"""Atomic, schema-validated JSON state store for tab-conductor.

Implements a file-backed state store with POSIX advisory locking (flock/lockf),
atomic writes via tempfile + os.replace, and CAS versioning.  Supports both
Linux/WSL2 (flock) and macOS (lockf) transparently.

Thread-safety model:
  - Multiple readers are allowed concurrently (LOCK_SH).
  - Writers take an exclusive lock (LOCK_EX) and perform an atomic replace.
  - The ``version`` integer in state acts as an optimistic CAS counter.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tab_conductor.exceptions import StateCorrupted, StateLockTimeout, StateVersionMismatch
from tab_conductor.logging_config import get_logger
from tab_conductor.schema import load_schema, validate

_logger = get_logger("tab_conductor.state")

# Re-export for backward-compat (existing tests import from tab_conductor.state)
__all__ = [
    "StateStore",
    "StateAlreadyExists",
    "StateLockTimeout",
    "StateCorrupted",
    "StateVersionMismatch",
]

# Locking constants
_LOCK_SH = fcntl.LOCK_SH
_LOCK_EX = fcntl.LOCK_EX
_LOCK_NB = fcntl.LOCK_NB
_LOCK_UN = fcntl.LOCK_UN

# Timeout and polling configuration
_FLOCK_TIMEOUT_S = 5.0
_FLOCK_POLL_INTERVAL_S = 0.05


class StateAlreadyExists(FileExistsError):
    """Raised by :meth:`StateStore.init` when state.json already exists."""

    def __init__(self, path: Path) -> None:
        """Initialise with the existing state file path.

        Args:
            path: Filesystem path that already exists.
        """
        super().__init__(f"State file already exists at '{path}'. Use read() instead.")
        self.state_path = path


def _flock(fd: int, operation: int) -> None:
    """Apply an advisory flock or lockf depending on platform.

    Args:
        fd: Open file descriptor to lock.
        operation: Combination of ``fcntl.LOCK_*`` flags.

    Raises:
        BlockingIOError: If ``LOCK_NB`` is set and the lock is held.
        OSError: On other locking failures.
    """
    if sys.platform == "darwin":
        fcntl.lockf(fd, operation)
    else:
        fcntl.flock(fd, operation)


def _flock_with_timeout(fd: int, operation: int, lock_path: Path) -> None:
    """Acquire an exclusive advisory lock, polling until timeout.

    Polls every :data:`_FLOCK_POLL_INTERVAL_S` seconds for up to
    :data:`_FLOCK_TIMEOUT_S` seconds.

    Args:
        fd: Open file descriptor to lock.
        operation: Combination of ``fcntl.LOCK_EX | fcntl.LOCK_NB``.
        lock_path: Path used only for error messages.

    Raises:
        StateLockTimeout: If the lock cannot be acquired within the timeout.
    """
    deadline = time.monotonic() + _FLOCK_TIMEOUT_S
    while True:
        try:
            _flock(fd, operation)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise StateLockTimeout(_FLOCK_TIMEOUT_S, lock_path) from exc
            time.sleep(_FLOCK_POLL_INTERVAL_S)


def _fsync_dir(path: Path) -> None:
    """fsync the parent directory of *path* to flush directory entry updates.

    Args:
        path: Any filesystem path whose parent directory should be synced.

    Raises:
        OSError: If the directory cannot be opened or synced.
    """
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class StateStore:
    """Atomic, schema-validated JSON state store backed by a directory.

    Uses POSIX advisory locking (flock on Linux, lockf on macOS) for
    concurrent access safety.  All writes are performed atomically via a
    temporary file and ``os.replace``.  A ``version`` integer in the state
    document serves as an optimistic concurrency control (CAS) counter.

    Attributes:
        state_dir: Root directory containing ``state.json`` and ``state.lock``.

    Example:
        >>> import tempfile, pathlib
        >>> d = pathlib.Path(tempfile.mkdtemp())
        >>> store = StateStore(d)
        >>> initial = {"run_id": "01HV..." , "version": 0, ...}
        >>> store.init(initial)
        >>> state = store.read()
        >>> state["version"]
        0
    """

    def __init__(self, state_dir: Path) -> None:
        """Initialise the store pointing at *state_dir*.

        Args:
            state_dir: Directory that will contain ``state.json`` and
                ``state.lock``.  The directory is created if it does not exist.
        """
        self.state_dir = state_dir
        self._state_path = state_dir / "state.json"
        self._lock_path = state_dir / "state.lock"
        self._schema: dict[str, Any] = load_schema("state")
        state_dir.mkdir(parents=True, exist_ok=True)
        # Ensure the lock file exists so we can open it without CREAT races
        self._lock_path.touch(exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def init(self, initial: dict[str, Any]) -> None:
        """Persist the initial state, failing if state already exists.

        Validates *initial* against the state schema before writing.

        Args:
            initial: The initial state dictionary.  Must include all required
                fields defined in ``state.schema.json``.

        Raises:
            StateAlreadyExists: If ``state.json`` already exists.
            SchemaValidationError: If *initial* does not conform to the schema.
            OSError: On filesystem errors.
        """
        if self._state_path.exists():
            raise StateAlreadyExists(self._state_path)
        validate(initial, self._schema)
        self._atomic_write(initial)
        _logger.info(
            "StateStore initialised",
            extra={"run_id": initial.get("run_id"), "state_dir": str(self.state_dir)},
        )

    def read(self) -> dict[str, Any]:
        """Read and return the current state under a shared lock.

        Args:
            (none)

        Returns:
            The parsed state dictionary.

        Raises:
            FileNotFoundError: If ``state.json`` does not exist yet.
            StateCorrupted: If the file contains invalid JSON.
            SchemaValidationError: If the stored state fails schema validation.
        """
        with self._lock_path.open("r", encoding="utf-8") as lock_fh:
            _flock(lock_fh.fileno(), _LOCK_SH)
            try:
                state = self._read_raw()
            finally:
                _flock(lock_fh.fileno(), _LOCK_UN)
        validate(state, self._schema)
        return state

    def update(self, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """Apply *mutator* to the current state and persist atomically.

        Steps:
        1. Acquire exclusive lock with timeout.
        2. Read current state.
        3. Capture ``state["version"]`` as *pre_version*.
        4. Call ``mutator(state)`` → *new_state*.
        5. Assert ``new_state["version"] == pre_version`` (mismatch → raise).
        6. Increment ``new_state["version"]``.
        7. Validate *new_state* against schema.
        8. Write atomically (tempfile → fsync → os.replace → dir fsync).
        9. Release lock.

        Args:
            mutator: Pure function that receives the current state dict and
                returns the updated dict.  Must not modify ``version`` itself
                (the store manages version increments).

        Returns:
            The committed new state dictionary.

        Raises:
            StateLockTimeout: If the exclusive lock cannot be acquired.
            StateCorrupted: If the state file is unparseable.
            StateVersionMismatch: If *mutator* modified the ``version`` field.
            SchemaValidationError: If the post-mutation state is invalid.
            OSError: On filesystem errors.
        """
        with self._lock_path.open("r+", encoding="utf-8") as lock_fh:
            _flock_with_timeout(lock_fh.fileno(), _LOCK_EX | _LOCK_NB, self._lock_path)
            try:
                current = self._read_raw()
                pre_version: int = current.get("version", -1)

                new_state = mutator(current)

                post_version: int = new_state.get("version", -1)
                if post_version != pre_version:
                    raise StateVersionMismatch(pre_version, post_version)

                new_state["version"] = pre_version + 1
                validate(new_state, self._schema)
                self._atomic_write(new_state)
            finally:
                _flock(lock_fh.fileno(), _LOCK_UN)

        _logger.debug(
            "State updated",
            extra={"new_version": new_state["version"]},
        )
        return new_state

    def read_summary(self) -> dict[str, Any]:
        """Return a compact summary dict for supervisor consumption.

        Avoids loading the full state (which may grow large) by extracting
        only the fields needed for supervisory decisions.

        Returns:
            A dict with keys ``run_id``, ``version``, ``status``,
            ``cost_usd_total``, ``workers_count``, and a nested
            ``worker_status_counts`` mapping each worker status to its count.

        Raises:
            FileNotFoundError: If ``state.json`` does not exist.
            StateCorrupted: If the file contains invalid JSON.
        """
        with self._lock_path.open("r", encoding="utf-8") as lock_fh:
            _flock(lock_fh.fileno(), _LOCK_SH)
            try:
                state = self._read_raw()
            finally:
                _flock(lock_fh.fileno(), _LOCK_UN)

        workers: list[dict[str, Any]] = state.get("workers", [])
        status_counts: dict[str, int] = {}
        for w in workers:
            st: str = w.get("status", "unknown")
            status_counts[st] = status_counts.get(st, 0) + 1

        return {
            "run_id": state.get("run_id"),
            "version": state.get("version"),
            "status": state.get("status"),
            "cost_usd_total": state.get("cost_usd_total", 0.0),
            "workers_count": len(workers),
            "worker_status_counts": status_counts,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read_raw(self) -> dict[str, Any]:
        """Read and JSON-parse ``state.json`` without locking.

        Must be called with the advisory lock already held.

        Returns:
            The parsed state dictionary.

        Raises:
            FileNotFoundError: If ``state.json`` does not exist.
            StateCorrupted: If the JSON is invalid.
        """
        if not self._state_path.exists():
            raise FileNotFoundError(
                f"State file not found: {self._state_path}. "
                "Call init() before read()."
            )
        try:
            with self._state_path.open("r", encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)
        except json.JSONDecodeError as exc:
            raise StateCorrupted(self._state_path, str(exc)) from exc
        return data

    def _atomic_write(self, state: dict[str, Any]) -> None:
        """Write *state* atomically to ``state.json``.

        Uses a sibling temporary file in the same directory so that
        ``os.replace`` is guaranteed to be atomic on POSIX (same filesystem).
        Calls ``os.fsync`` on the temp file before replacing, and then syncs
        the parent directory entry.

        Args:
            state: The state dict to serialise.

        Raises:
            OSError: On any filesystem I/O error.
        """
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=self.state_dir, prefix=".state_tmp_", suffix=".json"
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_fh:
                json.dump(state, tmp_fh, ensure_ascii=False, indent=2)
                tmp_fh.flush()
                os.fsync(tmp_fh.fileno())
            os.replace(tmp_path_str, str(self._state_path))
            _fsync_dir(self._state_path)
        except Exception:
            # Clean up the temp file if anything goes wrong; suppress errors
            # from the cleanup itself so the original exception propagates.
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise
