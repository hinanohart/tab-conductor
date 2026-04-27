"""Unit tests for tab_conductor.state.StateStore.

Covers:
- Atomic write round-trip
- kill -9 simulation (abandoned temp file leaves state intact)
- CAS: concurrent threads / version mismatch detection
- Schema validation on write
- Platform lock path switching (darwin vs linux) via monkeypatching
- flock timeout (StateLockTimeout)
- read_summary compact output
- Atomic update: exception in mutator leaves state unchanged
"""

from __future__ import annotations

import fcntl
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tab_conductor.schema import SchemaValidationError
from tab_conductor.state import (
    _FLOCK_TIMEOUT_S,
    StateAlreadyExists,
    StateCorrupted,
    StateLockTimeout,
    StateStore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_state(run_id: str = "01ARZ3NDEKTSV4RRFFQ69G5FAV") -> dict[str, Any]:
    """Return a minimal schema-valid state dict."""
    return {
        "run_id": run_id,
        "version": 0,
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": None,
        "status": "running",
        "cost_usd_total": 0.0,
        "workers": [],
        "tasks": [],
        "events": [],
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_init_and_read_round_trip(tmp_run_dir: Path) -> None:
    """init() then read() must return an equal dict with version==0."""
    store = StateStore(tmp_run_dir / "run1")
    initial = _make_valid_state()
    store.init(initial)

    loaded = store.read()
    assert loaded["run_id"] == initial["run_id"]
    assert loaded["version"] == 0
    assert loaded["status"] == "running"


def test_atomic_write_survives_abandoned_tempfile(tmp_run_dir: Path) -> None:
    """A leftover .state_tmp_*.json must NOT corrupt state.json.

    Simulates kill -9 mid-write by leaving an invalid temp file in the
    state directory, then verifying that state.json is still readable.
    """
    state_dir = tmp_run_dir / "run_crash"
    store = StateStore(state_dir)
    initial = _make_valid_state()
    store.init(initial)

    # Simulate an abandoned temp file (e.g., from a SIGKILL mid-write)
    abandoned = state_dir / ".state_tmp_abandoned.json"
    abandoned.write_text("{ INVALID JSON !!!!", encoding="utf-8")

    # state.json must still be readable and valid
    loaded = store.read()
    assert loaded["version"] == 0
    assert loaded["run_id"] == initial["run_id"]


def test_update_increments_version(tmp_run_dir: Path) -> None:
    """update() must increment version by 1 each call."""
    store = StateStore(tmp_run_dir / "run_ver")
    store.init(_make_valid_state())

    new_state = store.update(lambda s: {**s, "status": "halting"})
    assert new_state["version"] == 1
    assert new_state["status"] == "halting"

    new_state2 = store.update(lambda s: {**s, "status": "halted"})
    assert new_state2["version"] == 2


def test_concurrent_update_version_mismatch(tmp_run_dir: Path) -> None:
    """CAS: a mutator that illegally modifies version must trigger StateVersionMismatch.

    StateVersionMismatch is raised when the mutator returns a dict whose
    'version' field differs from the pre-lock snapshot — i.e., when something
    outside the store mutates the version field.  We simulate this by
    monkeypatching a mutator that increments version by 1 (pretending another
    writer already did so).
    """
    from tab_conductor.state import StateVersionMismatch

    state_dir = tmp_run_dir / "run_cas"
    store = StateStore(state_dir)
    store.init(_make_valid_state())

    def bad_mutator(s: dict[str, Any]) -> dict[str, Any]:
        """Incorrectly bumps version — simulates external tampering."""
        return {**s, "version": s["version"] + 1, "status": "halting"}

    with pytest.raises(StateVersionMismatch):
        store.update(bad_mutator)


def test_schema_validation_failure_on_init(tmp_run_dir: Path) -> None:
    """init() must raise SchemaValidationError for an invalid state dict."""
    store = StateStore(tmp_run_dir / "run_schema")
    bad = _make_valid_state()
    bad["status"] = "not_a_valid_status"
    with pytest.raises(SchemaValidationError):
        store.init(bad)


def test_lock_path_darwin_vs_linux(tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both the darwin (lockf) and linux (flock) lock paths must work end-to-end."""
    for platform in ("darwin", "linux"):
        state_dir = tmp_run_dir / f"run_{platform}"
        monkeypatch.setattr(sys, "platform", platform)
        store = StateStore(state_dir)
        store.init(_make_valid_state())
        loaded = store.read()
        assert loaded["version"] == 0


def test_flock_timeout_raises(tmp_run_dir: Path) -> None:
    """StateLockTimeout must be raised when the exclusive lock is held too long."""
    state_dir = tmp_run_dir / "run_timeout"
    store = StateStore(state_dir)
    store.init(_make_valid_state())

    # Hold the exclusive lock in a background thread for longer than the timeout
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_lock() -> None:
        with store._lock_path.open("r+", encoding="utf-8") as fh:
            if sys.platform == "darwin":
                fcntl.lockf(fh.fileno(), fcntl.LOCK_EX)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            lock_held.set()
            release_lock.wait(timeout=_FLOCK_TIMEOUT_S + 3)
            if sys.platform == "darwin":
                fcntl.lockf(fh.fileno(), fcntl.LOCK_UN)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    lock_held.wait(timeout=2)

    try:
        with pytest.raises(StateLockTimeout):
            store.update(lambda s: {**s, "status": "halting"})
    finally:
        release_lock.set()
        holder.join(timeout=5)


def test_read_summary_is_compact(tmp_run_dir: Path) -> None:
    """read_summary() output must serialise to ≤ 1 KB JSON."""
    store = StateStore(tmp_run_dir / "run_summary")
    store.init(_make_valid_state())
    summary = store.read_summary()
    serialised = json.dumps(summary)
    assert len(serialised.encode("utf-8")) <= 1024, (
        f"read_summary JSON is {len(serialised)} bytes (must be ≤ 1024)"
    )
    assert "workers_count" in summary
    assert "cost_usd_total" in summary


def test_atomic_update_exception_leaves_state_unchanged(tmp_run_dir: Path) -> None:
    """If mutator raises, state.json must remain at its previous version."""
    store = StateStore(tmp_run_dir / "run_atomic")
    store.init(_make_valid_state())

    pre_state = store.read()
    assert pre_state["version"] == 0

    def bad_mutator(s: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("simulated crash in mutator")

    with pytest.raises(RuntimeError, match="simulated crash"):
        store.update(bad_mutator)

    # State must be unchanged
    post_state = store.read()
    assert post_state["version"] == 0
    assert post_state["status"] == pre_state["status"]


def test_init_raises_if_state_exists(tmp_run_dir: Path) -> None:
    """Calling init() twice on the same directory must raise StateAlreadyExists."""
    store = StateStore(tmp_run_dir / "run_dup")
    store.init(_make_valid_state())
    with pytest.raises(StateAlreadyExists):
        store.init(_make_valid_state())


def test_read_corrupted_state_raises(tmp_run_dir: Path) -> None:
    """Placing invalid JSON in state.json must raise StateCorrupted on read()."""
    state_dir = tmp_run_dir / "run_corrupt"
    store = StateStore(state_dir)
    store.init(_make_valid_state())

    # Overwrite state.json with garbage
    (state_dir / "state.json").write_text("{ BROKEN JSON", encoding="utf-8")

    with pytest.raises(StateCorrupted):
        store.read()
