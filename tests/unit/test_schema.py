"""Unit tests for tab_conductor.schema."""

from __future__ import annotations

from typing import Any

import pytest

from tab_conductor.schema import SchemaValidationError, load_schema, validate


def _valid_state() -> dict[str, Any]:
    """Return a minimal valid state document."""
    return {
        "run_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "version": 0,
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": None,
        "status": "running",
        "cost_usd_total": 0.0,
        "workers": [],
        "tasks": [],
        "events": [],
    }


def test_valid_state_passes() -> None:
    """A fully valid state document must pass without raising."""
    schema = load_schema("state")
    validate(_valid_state(), schema)  # must not raise


def test_additional_properties_rejected() -> None:
    """Extra top-level properties must be rejected (additionalProperties: false)."""
    schema = load_schema("state")
    bad = _valid_state()
    bad["unexpected_field"] = "oops"
    with pytest.raises(SchemaValidationError, match="additionalProperties|unexpected_field"):
        validate(bad, schema)


def test_invalid_run_id_pattern_rejected() -> None:
    """run_id not matching Crockford Base32 26-char pattern must be rejected."""
    schema = load_schema("state")
    bad = _valid_state()
    bad["run_id"] = "not-a-valid-ulid!"
    with pytest.raises(SchemaValidationError):
        validate(bad, schema)


def test_invalid_worker_status_enum_rejected() -> None:
    """A worker with an unknown status value must be rejected."""
    schema = load_schema("state")
    bad = _valid_state()
    bad["workers"] = [
        {
            "id": "w1",
            "status": "UNKNOWN_STATUS",
            "pid": None,
            "started_at": "2026-01-01T00:00:00Z",
            "heartbeat_ts": "2026-01-01T00:00:00Z",
            "task_id": None,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
        }
    ]
    with pytest.raises(SchemaValidationError):
        validate(bad, schema)


def test_validation_error_message_is_human_readable() -> None:
    """SchemaValidationError must include a non-empty, descriptive message."""
    schema = load_schema("state")
    bad = _valid_state()
    bad["status"] = "totally_invalid_status_string"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(bad, schema)
    msg = str(exc_info.value)
    assert len(msg) > 20, f"Error message too short: {msg!r}"
    # The message should name the offending value or field
    assert "status" in msg or "totally_invalid" in msg or "enum" in msg.lower()
