"""YAML plan parser for tab-conductor.

Loads a YAML plan file (or dict), validates structure, and returns a
:class:`ParsedPlan` with a list of task dicts and plan-level metadata.

Validation rules
----------------
- ``id`` must be unique across tasks.
- ``depends_on`` references must all exist as task ids within the same plan.
- The dependency graph must be a DAG (no cycles); cycle detection uses
  gray/black DFS, identical to the orchestrator's internal check, so invalid
  plans are rejected early before reaching the orchestrator.
- ``priority`` must be an integer in [0, 9].  Default 5.
- ``max_retries`` must be an integer in [0, 10].  Default 2.
- ``kind`` defaults to ``"general"``.
- ``prompt`` is required and must not be empty.
- Invalid YAML and schema errors raise :class:`~tab_conductor.exceptions.PlanParseError`.
- Files larger than 1 MB are rejected (DoS protection).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tab_conductor.exceptions import PlanParseError

# Maximum file size accepted (bytes)
_MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB

# Valid range constants
_PRIORITY_MIN = 0
_PRIORITY_MAX = 9
_MAX_RETRIES_MIN = 0
_MAX_RETRIES_MAX = 10

# Required task fields
_REQUIRED_TASK_FIELDS = frozenset({"id", "prompt"})


@dataclass
class ParsedPlan:
    """Validated, parsed representation of a tab-conductor YAML plan.

    Attributes:
        tasks: List of task dicts ready for consumption by the orchestrator.
            Each dict contains: id, kind, prompt, priority, depends_on,
            max_retries (and any extra fields passed through unchanged).
        metadata: Plan-level settings: name, description, max_parallel, and
            any other top-level keys that are not ``tasks``.
    """

    tasks: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_plan(path: Path) -> ParsedPlan:
    """Parse and validate a YAML plan from a filesystem path.

    Args:
        path: Absolute or relative path to the ``.yaml`` plan file.

    Returns:
        A validated :class:`ParsedPlan` instance.

    Raises:
        PlanParseError: If the file exceeds 1 MB, contains invalid YAML,
            fails schema validation, has duplicate task ids, contains
            unknown ``depends_on`` references, or contains a dependency cycle.
        FileNotFoundError: If *path* does not exist.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PlanParseError(str(exc), path=path) from exc

    if size > _MAX_FILE_SIZE:
        raise PlanParseError(
            f"Plan file is {size} bytes, exceeds maximum of {_MAX_FILE_SIZE} bytes",
            path=path,
        )

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanParseError(str(exc), path=path) from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        line: int | None = None
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            line = exc.problem_mark.line + 1  # 1-based
        raise PlanParseError(str(exc), path=path, line=line) from exc

    if not isinstance(data, dict):
        raise PlanParseError(
            "Top-level YAML value must be a mapping, not "
            f"{type(data).__name__}",
            path=path,
        )

    return parse_plan_dict(data, _source_path=path)


def parse_plan_dict(
    data: dict[str, Any],
    *,
    _source_path: Path | None = None,
) -> ParsedPlan:
    """Validate and parse a plan from an already-loaded dict.

    Args:
        data: Python dict corresponding to the top-level YAML structure.
        _source_path: Optional originating file path, used in error messages.

    Returns:
        A validated :class:`ParsedPlan` instance.

    Raises:
        PlanParseError: On any validation failure.
    """
    p = _source_path  # short alias for error messages

    if not isinstance(data, dict):
        raise PlanParseError("Plan must be a YAML mapping", path=p)

    # -----------------------------------------------------------------------
    # Extract task list
    # -----------------------------------------------------------------------
    raw_tasks = data.get("tasks")
    if raw_tasks is None:
        raise PlanParseError("Plan must have a 'tasks' key", path=p)
    if not isinstance(raw_tasks, list):
        raise PlanParseError("'tasks' must be a list", path=p)
    if len(raw_tasks) == 0:
        raise PlanParseError("'tasks' must not be empty", path=p)

    # -----------------------------------------------------------------------
    # Validate individual tasks
    # -----------------------------------------------------------------------
    tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for idx, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise PlanParseError(
                f"Task at index {idx} must be a mapping, got {type(raw).__name__}",
                path=p,
            )

        task = _validate_task(raw, idx, seen_ids, p)
        seen_ids.add(task["id"])
        tasks.append(task)

    # -----------------------------------------------------------------------
    # Validate depends_on references
    # -----------------------------------------------------------------------
    for task in tasks:
        for dep in task["depends_on"]:
            if dep not in seen_ids:
                raise PlanParseError(
                    f"Task '{task['id']}' depends_on unknown id '{dep}'",
                    path=p,
                )

    # -----------------------------------------------------------------------
    # DAG cycle detection (gray/black DFS)
    # -----------------------------------------------------------------------
    _check_dag_cycles(tasks, p)

    # -----------------------------------------------------------------------
    # Build metadata (everything except 'tasks')
    # -----------------------------------------------------------------------
    metadata: dict[str, Any] = {k: v for k, v in data.items() if k != "tasks"}
    # Apply defaults for well-known metadata keys
    metadata.setdefault("name", "")
    metadata.setdefault("description", "")
    metadata.setdefault("max_parallel", 4)

    return ParsedPlan(tasks=tasks, metadata=metadata)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_task(
    raw: dict[str, Any],
    idx: int,
    seen_ids: set[str],
    path: Path | None,
) -> dict[str, Any]:
    """Validate and normalise a single raw task dict.

    Args:
        raw: Raw task dict from YAML.
        idx: Zero-based index in the tasks list (used in error messages).
        seen_ids: Set of already-accepted task ids (for duplicate detection).
        path: Source file path for error messages.

    Returns:
        Normalised task dict.

    Raises:
        PlanParseError: On any per-task validation failure.
    """
    # Required: id
    task_id = raw.get("id")
    if task_id is None:
        raise PlanParseError(f"Task at index {idx} is missing required field 'id'", path=path)
    if not isinstance(task_id, str) or not task_id.strip():
        raise PlanParseError(
            f"Task at index {idx}: 'id' must be a non-empty string", path=path
        )
    task_id = task_id.strip()
    if task_id in seen_ids:
        raise PlanParseError(f"Duplicate task id '{task_id}'", path=path)

    # Required: prompt
    prompt = raw.get("prompt")
    if prompt is None:
        raise PlanParseError(f"Task '{task_id}' is missing required field 'prompt'", path=path)
    if not isinstance(prompt, str) or not prompt.strip():
        raise PlanParseError(
            f"Task '{task_id}': 'prompt' must be a non-empty string", path=path
        )

    # Optional: kind (default "general")
    kind = raw.get("kind", "general")
    if not isinstance(kind, str):
        raise PlanParseError(f"Task '{task_id}': 'kind' must be a string", path=path)

    # Optional: priority (default 5, int in [0,9])
    priority_raw = raw.get("priority", 5)
    try:
        priority = int(priority_raw)
    except (TypeError, ValueError) as err:
        raise PlanParseError(
            f"Task '{task_id}': 'priority' must be an integer, got {priority_raw!r}",
            path=path,
        ) from err
    if not (_PRIORITY_MIN <= priority <= _PRIORITY_MAX):
        raise PlanParseError(
            f"Task '{task_id}': 'priority' must be in [{_PRIORITY_MIN}, {_PRIORITY_MAX}],"
            f" got {priority}",
            path=path,
        )

    # Optional: max_retries (default 2, int in [0,10])
    max_retries_raw = raw.get("max_retries", 2)
    try:
        max_retries = int(max_retries_raw)
    except (TypeError, ValueError) as err:
        raise PlanParseError(
            f"Task '{task_id}': 'max_retries' must be an integer, got {max_retries_raw!r}",
            path=path,
        ) from err
    if not (_MAX_RETRIES_MIN <= max_retries <= _MAX_RETRIES_MAX):
        raise PlanParseError(
            f"Task '{task_id}': 'max_retries' must be in"
            f" [{_MAX_RETRIES_MIN}, {_MAX_RETRIES_MAX}], got {max_retries}",
            path=path,
        )

    # Optional: depends_on (default [])
    depends_on_raw = raw.get("depends_on", [])
    if not isinstance(depends_on_raw, list):
        raise PlanParseError(
            f"Task '{task_id}': 'depends_on' must be a list", path=path
        )
    depends_on: list[str] = []
    for dep in depends_on_raw:
        if not isinstance(dep, str) or not dep.strip():
            raise PlanParseError(
                f"Task '{task_id}': each entry in 'depends_on' must be a non-empty string,"
                f" got {dep!r}",
                path=path,
            )
        depends_on.append(dep.strip())

    # Build normalised task (preserve unknown extra keys)
    task: dict[str, Any] = {
        **{k: v for k, v in raw.items() if k not in _REQUIRED_TASK_FIELDS
           and k not in {"kind", "priority", "max_retries", "depends_on"}},
        "id": task_id,
        "prompt": prompt.strip(),
        "kind": kind,
        "priority": priority,
        "max_retries": max_retries,
        "depends_on": depends_on,
    }
    return task


def _check_dag_cycles(tasks: list[dict[str, Any]], path: Path | None) -> None:
    """Gray/black DFS cycle detection on the task dependency graph.

    Args:
        tasks: List of normalised task dicts (each with 'id' and 'depends_on').
        path: Source file path for error messages.

    Raises:
        PlanParseError: If a cycle is detected.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {t["id"]: WHITE for t in tasks}
    adj: dict[str, list[str]] = {t["id"]: t["depends_on"] for t in tasks}
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in adj[node]:
            if dep not in color:
                # Unknown dep — already caught by reference check; skip here
                continue
            if color[dep] == GRAY:
                cycle = " -> ".join(stack + [dep])
                raise PlanParseError(
                    f"Dependency cycle detected: {cycle}",
                    path=path,
                )
            if color[dep] == WHITE:
                dfs(dep)
        stack.pop()
        color[node] = BLACK

    for tid in list(color.keys()):
        if color[tid] == WHITE:
            dfs(tid)


# ---------------------------------------------------------------------------
# Secret-pattern helper (re-exported for bugreport redaction)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(token|key|secret|password|passwd|api_key|auth)\s*=\s*\S+"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgho_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bghu_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bghs_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bghr_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
]


def redact_text(text: str) -> str:
    """Replace secrets in *text* with ``[REDACTED]``.

    Applies single-pass substitution of:
    - env-var-like assignments (e.g. ``TOKEN=abc123``)
    - base64-like strings of 40+ characters

    Args:
        text: Input text content (e.g. from a log or config file).

    Returns:
        Text with secrets replaced by ``[REDACTED]``.
    """
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text
