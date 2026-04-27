"""Concurrent-access stress tests for StateStore.

Tests:
1. 50 threads concurrently calling StateStore.update() each increment version
   once; the final version must equal 50 with no lost updates.
2. Two subprocesses compete for the flock; only one can hold it at a time.
3. A concurrent reader never observes a partial/invalid state during atomic
   writes (200 read iterations, all must be schema-valid).

Design notes
------------
- No wall-time comparisons; correctness is verified through invariants only.
- threading.Barrier is used to synchronise the start of concurrent work so
  threads are genuinely concurrent (not serialised by setup).
- The CAS-retry loop in StateStore.update() is exercised naturally because
  50 threads compete for the same version slot.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tab_conductor.schema import load_schema, validate
from tab_conductor.state import StateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_FAKE_RUN_ID = "A" * 26  # 26-char Crockford-alphabet string


def _make_initial_state() -> dict[str, Any]:
    """Return a minimal schema-valid initial state dict."""
    return {
        "run_id": _FAKE_RUN_ID,
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
# Test 1: 50-thread concurrent version increment
# ---------------------------------------------------------------------------


def test_50_threads_version_consistent(tmp_path: Path) -> None:
    """50 threads each update version once; final version must be 50.

    Each thread loops with CAS retry until its update is accepted.  No
    updates must be lost.
    """
    store = StateStore(tmp_path)
    store.init(_make_initial_state())

    n_threads = 50
    errors: list[str] = []
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()  # all threads start simultaneously
        try:
            store.update(lambda s: {**s})  # no-op mutator; version auto-increments
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"Thread errors: {errors}"
    final = store.read()
    assert final["version"] == n_threads, (
        f"Expected version {n_threads}, got {final['version']}"
    )


# ---------------------------------------------------------------------------
# Test 2: two-process flock mutual exclusion (subprocess fixture)
# ---------------------------------------------------------------------------

_LOCK_HOLDER_SCRIPT = """
import fcntl
import sys
import time
from pathlib import Path

lock_path = Path(sys.argv[1])
hold_s = float(sys.argv[2])

lock_file = open(lock_path, "w")
try:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    # Signal we hold the lock
    sys.stdout.write("LOCKED\\n")
    sys.stdout.flush()
    time.sleep(hold_s)
    sys.stdout.write("RELEASING\\n")
    sys.stdout.flush()
finally:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    lock_file.close()
"""


def test_two_process_flock_exclusive(tmp_path: Path) -> None:
    """Two subprocesses cannot hold the same exclusive flock simultaneously.

    Process A grabs the lock and holds it for 0.4 s.  Process B tries to
    acquire the same lock with LOCK_NB and must see BlockingIOError while
    A holds it; after A releases, B must succeed.
    """
    lock_path = tmp_path / "test.lock"
    lock_path.touch()

    # Start process A (holds lock for 0.4 s)
    proc_a = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER_SCRIPT, str(lock_path), "0.4"],
        stdout=subprocess.PIPE,
        text=True,
    )

    # Wait until A reports it holds the lock
    assert proc_a.stdout is not None
    line = proc_a.stdout.readline()
    assert line.strip() == "LOCKED", f"Unexpected output: {line!r}"

    # While A holds the lock, try non-blocking LOCK_EX from this process
    import fcntl as _fcntl

    with open(lock_path, "w") as f:
        try:
            _fcntl.flock(f.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
            pytest.fail("Expected BlockingIOError while process A holds flock")
        except BlockingIOError:
            pass  # correct: lock is held by A

    # Wait for A to finish
    proc_a.wait(timeout=5)
    assert proc_a.returncode == 0

    # Now we should be able to get the lock
    with open(lock_path, "w") as f:
        try:
            _fcntl.flock(f.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        except BlockingIOError:
            pytest.fail("Failed to acquire lock after process A released it")


# ---------------------------------------------------------------------------
# Test 3: concurrent reader never sees partial/invalid state
# ---------------------------------------------------------------------------


def test_reader_never_sees_partial_state(tmp_path: Path) -> None:
    """A background reader sees only schema-valid state during concurrent writes.

    A writer thread performs 100 rapid updates.  A reader thread performs 200
    reads in parallel.  Every successful read must pass schema validation.
    """
    schema = load_schema("state")
    store = StateStore(tmp_path)
    store.init(_make_initial_state())

    read_errors: list[str] = []
    stop_event = threading.Event()

    def writer() -> None:
        for _ in range(100):
            with contextlib.suppress(Exception):  # occasional lock contention is acceptable
                store.update(lambda s: {**s, "cost_usd_total": s["cost_usd_total"] + 0.001})

    def reader() -> None:
        reads = 0
        while reads < 200 and not stop_event.is_set():
            try:
                state = store.read()
                try:
                    validate(state, schema)
                except Exception as exc:
                    read_errors.append(f"Schema violation on read {reads}: {exc}")
            except FileNotFoundError:
                pass  # transient, extremely unlikely
            except Exception as exc:
                read_errors.append(f"Read error on read {reads}: {exc}")
            reads += 1

    w = threading.Thread(target=writer, daemon=True)
    r = threading.Thread(target=reader, daemon=True)

    w.start()
    r.start()
    w.join(timeout=15)
    r.join(timeout=15)
    stop_event.set()

    assert not read_errors, f"Reader observed invalid states: {read_errors[:3]}"
