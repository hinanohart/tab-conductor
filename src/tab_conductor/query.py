"""Dotted-path query engine for tab-conductor state dicts.

Provides a minimal jq-inspired query language sufficient for supervisor
decision-making:

* ``"workers[0].status"``   — index into array then access field
* ``"tasks[?status=='done']|length"``  — filter array by field equality, then count
* ``"meta.nested.key"``     — arbitrary dotted nesting
* Missing paths return ``None``.

Only a subset of jq syntax is supported.  Complex projections or multi-field
filters are intentionally out of scope for Phase 1.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Token patterns
# ---------------------------------------------------------------------------

# Matches the filter suffix: [?field=='value']|length  (with leading [?)
_FILTER_RE = re.compile(r"^\[\?(?P<field>[a-zA-Z_]\w*)==['\"](?P<value>[^'\"]*)['\"]]\|length$")
_INDEX_RE = re.compile(r"^\[(?P<idx>-?\d+)]$")
_SEGMENT_RE = re.compile(r"^[a-zA-Z_]\w*$")


def query(data: dict[str, Any], path: str) -> Any:
    """Evaluate a dotted-path query against *data*.

    Supported syntax:

    * ``"field"`` — top-level key access.
    * ``"a.b.c"`` — nested key access.
    * ``"arr[0]"`` — zero-based integer index into a list.
    * ``"arr[?field=='value']|length"`` — count list items whose *field*
      equals *value* (equality filter, string values only).
    * Combinations: ``"workers[0].status"``.

    Args:
        data: The root dictionary to query.
        path: Query expression string.

    Returns:
        The value at the resolved path, or ``None`` if any intermediate key
        or index is absent.

    Raises:
        ValueError: If the path expression is syntactically invalid (e.g.
            unmatched brackets, unknown operators).
        TypeError: If an intermediate value is not a dict or list when one
            is expected.

    Examples:
        >>> query({"a": {"b": 1}}, "a.b")
        1
        >>> query({"items": [{"x": 1}, {"x": 2}]}, "items[1].x")
        2
        >>> query({"ts": [{"s": "done"}, {"s": "pending"}]}, "ts[?s=='done']|length")
        1
        >>> query({}, "missing") is None
        True
    """
    path = path.strip()
    if not path:
        raise ValueError("Query path must not be empty.")

    # Check for the filter+length shorthand at the *end* of the path.
    # Split path into prefix.filter parts, e.g. "tasks[?status=='done']|length"
    if "[?" in path:
        bracket_start = path.rindex("[?")
        prefix = path[:bracket_start].rstrip(".")
        filter_suffix = path[bracket_start:]
        filter_match = _FILTER_RE.match(filter_suffix)
        if filter_match is None:
            raise ValueError(
                f"Invalid filter expression '{filter_suffix}' in path: {path!r}. "
                "Supported syntax: [?field=='value']|length"
            )
        field = filter_match.group("field")
        value = filter_match.group("value")

        container = _resolve_prefix(data, prefix) if prefix else data
        if container is None:
            return 0
        if not isinstance(container, list):
            raise TypeError(f"Expected a list at '{prefix}', got {type(container).__name__}")
        return sum(1 for item in container if isinstance(item, dict) and item.get(field) == value)

    # Standard dotted-path resolution
    return _resolve_prefix(data, path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_prefix(data: Any, path: str) -> Any:
    """Resolve a dotted path (without filter syntax) against *data*.

    Args:
        data: Current value (dict or list expected at each step).
        path: Remaining path string.

    Returns:
        Resolved value or ``None`` on missing intermediate keys/indices.

    Raises:
        ValueError: On malformed path segments.
        TypeError: On type mismatch (e.g. index into a non-list).
    """
    if not path:
        return data

    # Split on the first dot, but only dots that are not inside brackets
    segments = _tokenise(path)
    current: Any = data
    for segment in segments:
        if current is None:
            return None
        current = _apply_segment(current, segment, path)
    return current


def _tokenise(path: str) -> list[str]:
    """Split a dotted path into segments, preserving bracket groups.

    For example ``"workers[0].status"`` → ``["workers[0]", "status"]``.

    Args:
        path: The raw path string.

    Returns:
        List of segment strings.

    Raises:
        ValueError: If brackets are unmatched or the path is otherwise invalid.
    """
    segments: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in path:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            if depth < 0:
                raise ValueError(f"Unmatched ']' in path: {path!r}")
            current.append(ch)
        elif ch == "." and depth == 0:
            if current:
                segments.append("".join(current))
                current = []
            # else: leading/trailing dot — ignore silently
        else:
            current.append(ch)
    if depth != 0:
        raise ValueError(f"Unmatched '[' in path: {path!r}")
    if current:
        segments.append("".join(current))
    return [s for s in segments if s]


def _apply_segment(current: Any, segment: str, full_path: str) -> Any:
    """Apply a single path segment to *current*.

    Args:
        current: The value to index into.
        segment: A single segment, e.g. ``"workers"``, ``"[0]"``,
            ``"workers[0]"``.
        full_path: Original full path string, used only for error messages.

    Returns:
        The result of applying the segment, or ``None`` on missing keys.

    Raises:
        ValueError: If the segment syntax is invalid.
        TypeError: On type mismatch.
    """
    # Segment may be "field[idx]" or just "field" or just "[idx]"
    bracket_pos = segment.find("[")
    if bracket_pos == -1:
        # Plain field access
        return _dict_access(current, segment, full_path)

    field_part = segment[:bracket_pos]
    index_part = segment[bracket_pos:]

    if field_part:
        current = _dict_access(current, field_part, full_path)
        if current is None:
            return None

    # Now apply the index part, e.g. "[0]"
    idx_match = _INDEX_RE.match(index_part)
    if not idx_match:
        raise ValueError(
            f"Invalid bracket expression '{index_part}' in path: {full_path!r}. "
            "Only integer indices like [0] are supported in this position."
        )
    idx = int(idx_match.group("idx"))
    if not isinstance(current, list):
        raise TypeError(
            f"Expected a list for index '{index_part}' in path {full_path!r}, "
            f"got {type(current).__name__}"
        )
    try:
        return current[idx]
    except IndexError:
        return None


def _dict_access(current: Any, key: str, full_path: str) -> Any:
    """Access *key* from a dict, returning None on KeyError.

    Args:
        current: Expected to be a dict.
        key: The key to look up.
        full_path: Original full path for error messages.

    Returns:
        The value or ``None`` if *key* is absent.

    Raises:
        ValueError: If *key* is not a valid identifier.
        TypeError: If *current* is not a dict.
    """
    if not _SEGMENT_RE.match(key):
        raise ValueError(
            f"Invalid field name '{key}' in path: {full_path!r}. "
            "Field names must start with a letter or underscore."
        )
    if not isinstance(current, dict):
        raise TypeError(
            f"Expected a dict to access field '{key}' in path {full_path!r}, "
            f"got {type(current).__name__}"
        )
    return current.get(key)
