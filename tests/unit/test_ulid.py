"""Unit tests for tab_conductor.ulid."""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from tab_conductor.ulid import new, parse

_CROCKFORD_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_new_returns_26_char_crockford() -> None:
    """new() must return exactly 26 characters from Crockford Base32 alphabet."""
    uid = new()
    assert len(uid) == 26, f"Expected 26 chars, got {len(uid)}: {uid!r}"
    assert _CROCKFORD_RE.match(uid), f"Invalid Crockford chars in: {uid!r}"


def test_monotonic_ordering_100_sequential() -> None:
    """100 consecutive new() calls must produce lexicographically non-decreasing values."""
    ulids = [new() for _ in range(100)]
    for i in range(len(ulids) - 1):
        assert ulids[i] <= ulids[i + 1], (
            f"Monotonic violation at position {i}: {ulids[i]} > {ulids[i + 1]}"
        )


def test_parse_returns_utc_datetime() -> None:
    """parse() must return a timezone-aware datetime in UTC."""
    uid = new()
    dt = parse(uid)
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None
    # Confirm it is UTC (offset zero)
    assert dt.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_parse_invalid_rejects_bad_chars() -> None:
    """parse() must raise ValueError for strings with non-Crockford characters."""
    bad_inputs = [
        "ILOILOILOILOILOILOILOILOIL",  # I, L, O forbidden
        "shortstring",
        "",
        "01ARZ3NDEKTSV4RRFFQ69G5FA",  # 25 chars
        "01ARZ3NDEKTSV4RRFFQ69G5FAXXX",  # 29 chars
    ]
    for bad in bad_inputs:
        with pytest.raises(ValueError, match="Invalid ULID"):
            parse(bad)
