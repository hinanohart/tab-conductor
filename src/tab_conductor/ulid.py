"""ULID utilities for tab-conductor.

Thin wrapper around python-ulid providing monotonic ULID generation and
timestamp extraction with strict type safety.
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime

from ulid import ULID

# Crockford Base32 character set (uppercase only)
_CROCKFORD_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

# Lock ensuring monotonic generation within the same millisecond
_mono_lock = threading.Lock()
_last_ulid: ULID | None = None


def new() -> str:
    """Generate a new ULID string, guaranteeing monotonic ordering.

    Uses a process-local lock to ensure that two ULIDs generated in the
    same millisecond are still lexicographically ordered (monotonic).

    Returns:
        A 26-character Crockford Base32 ULID string.

    Example:
        >>> uid = new()
        >>> len(uid)
        26
    """
    global _last_ulid
    with _mono_lock:
        candidate = ULID()
        if _last_ulid is not None and candidate <= _last_ulid:
            # Increment the previous ULID to preserve monotonic ordering.
            # python-ulid ULID wraps a bytes-like value; we bump the integer.
            prev_int = int.from_bytes(_last_ulid.bytes, "big")
            candidate = ULID.from_bytes((prev_int + 1).to_bytes(16, "big"))
        _last_ulid = candidate
        return str(candidate)


def parse(s: str) -> datetime:
    """Extract the timestamp embedded in a ULID string.

    Args:
        s: A 26-character Crockford Base32 ULID string.

    Returns:
        A timezone-aware :class:`datetime` in UTC corresponding to the
        millisecond timestamp encoded in the ULID.

    Raises:
        ValueError: If *s* does not match the ULID alphabet or length.

    Example:
        >>> from tab_conductor.ulid import new, parse
        >>> dt = parse(new())
        >>> dt.tzinfo is not None
        True
    """
    if not _CROCKFORD_RE.match(s):
        raise ValueError(
            f"Invalid ULID string: {s!r}. "
            "Must be 26 characters from Crockford Base32 alphabet."
        )
    ulid_obj = ULID.from_str(s)
    # ulid_obj.datetime returns a timezone-aware UTC datetime
    ts: datetime = ulid_obj.datetime
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts
