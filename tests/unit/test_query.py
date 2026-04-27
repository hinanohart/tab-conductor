"""Unit tests for tab_conductor.query."""

from __future__ import annotations

from typing import Any

import pytest

from tab_conductor.query import query


def _sample_state() -> dict[str, Any]:
    return {
        "run_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "status": "running",
        "workers": [
            {"id": "w1", "status": "running"},
            {"id": "w2", "status": "idle"},
        ],
        "tasks": [
            {"id": "t1", "status": "done"},
            {"id": "t2", "status": "done"},
            {"id": "t3", "status": "pending"},
        ],
        "meta": {
            "nested": {
                "deep": {
                    "value": 42,
                }
            }
        },
    }


def test_array_index_and_field() -> None:
    """workers[0].status must resolve to 'running'."""
    state = _sample_state()
    assert query(state, "workers[0].status") == "running"


def test_filter_length() -> None:
    """tasks[?status=='done']|length must count items whose status=='done'."""
    state = _sample_state()
    result = query(state, "tasks[?status=='done']|length")
    assert result == 2


def test_missing_key_returns_none() -> None:
    """Querying a non-existent field must return None, not raise."""
    state = _sample_state()
    assert query(state, "nonexistent_field") is None


def test_deep_nested_access() -> None:
    """Dotted paths of depth > 2 must resolve correctly."""
    state = _sample_state()
    assert query(state, "meta.nested.deep.value") == 42


def test_invalid_path_raises_value_error() -> None:
    """Malformed path expressions must raise ValueError."""
    state = _sample_state()
    with pytest.raises((ValueError, TypeError)):
        query(state, "workers[unclosed")
