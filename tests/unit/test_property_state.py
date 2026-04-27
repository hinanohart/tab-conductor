"""Property-based tests for tab_conductor.state using Hypothesis.

Tests invariants that must hold for arbitrary valid inputs:
1. write → read round-trip equality
2. Sequential updates preserve schema validity at every step
3. version is monotonically increasing across sequential updates
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tab_conductor.schema import load_schema, validate
from tab_conductor.state import StateStore

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid ULID-like run_id: 26 chars from Crockford alphabet
_CROCKFORD_CHARS = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_run_id_st = st.text(alphabet=_CROCKFORD_CHARS, min_size=26, max_size=26)

_status_st = st.sampled_from(
    ["initializing", "running", "halting", "halted", "completed", "failed"]
)

_cost_st = st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False)


def _valid_state_st() -> st.SearchStrategy[dict[str, Any]]:
    """Strategy for a minimal schema-valid state dict."""
    return st.fixed_dictionaries(
        {
            "run_id": _run_id_st,
            "version": st.just(0),
            "started_at": st.just("2026-01-01T00:00:00Z"),
            "ended_at": st.none(),
            "status": _status_st,
            "cost_usd_total": _cost_st,
            "workers": st.just([]),
            "tasks": st.just([]),
            "events": st.just([]),
        }
    )


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

@given(initial=_valid_state_st())
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_write_read_round_trip(initial: dict[str, Any]) -> None:
    """Any valid state written via init() must be exactly reproduced by read()."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="tc_prop_", dir="/tmp"))
    try:
        store = StateStore(tmp_dir)
        store.init(initial)
        loaded = store.read()
        assert loaded["run_id"] == initial["run_id"]
        assert loaded["version"] == 0
        assert loaded["status"] == initial["status"]
        assert abs(loaded["cost_usd_total"] - initial["cost_usd_total"]) < 1e-9
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@given(
    initial=_valid_state_st(),
    new_statuses=st.lists(_status_st, min_size=1, max_size=5),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_sequential_updates_always_valid(
    initial: dict[str, Any], new_statuses: list[str]
) -> None:
    """Applying sequential update()s must leave state schema-valid after every step."""
    schema = load_schema("state")
    tmp_dir = Path(tempfile.mkdtemp(prefix="tc_prop_seq_", dir="/tmp"))
    try:
        store = StateStore(tmp_dir)
        store.init(initial)
        for new_status in new_statuses:
            store.update(lambda s, ns=new_status: {**s, "status": ns})
            current = store.read()
            validate(current, schema)  # must not raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@given(
    initial=_valid_state_st(),
    n_updates=st.integers(min_value=1, max_value=8),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_version_monotonically_increases(
    initial: dict[str, Any], n_updates: int
) -> None:
    """version must strictly increase by exactly 1 per update() call."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="tc_prop_ver_", dir="/tmp"))
    try:
        store = StateStore(tmp_dir)
        store.init(initial)
        for expected_version in range(1, n_updates + 1):
            result = store.update(lambda s: {**s})
            assert result["version"] == expected_version, (
                f"Expected version {expected_version}, got {result['version']}"
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
