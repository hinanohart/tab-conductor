"""JSON Schema validation helpers for tab-conductor.

Wraps ``jsonschema`` Draft202012Validator with human-readable error messages
via ``best_match``.  Schema files are resolved relative to the
``skill/references/schemas/`` directory in the repository root.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import jsonschema.validators
from jsonschema.exceptions import best_match

from tab_conductor.exceptions import SchemaValidationError

# Re-export for backward-compat (existing tests import from tab_conductor.schema)
__all__ = ["load_schema", "validate", "validate_named", "SchemaValidationError"]


def _repo_root() -> Path:
    """Resolve the repository root from this file's location.

    Returns:
        Absolute :class:`Path` to the repository root directory, i.e.
        ``tab_conductor/__file__`` → ``src/tab_conductor/schema.py``
        → ``../../../`` → repo root.
    """
    return Path(__file__).resolve().parent.parent.parent


def load_schema(name: str) -> dict[str, Any]:
    """Load a JSON Schema by name from the canonical schema directory.

    Schemas are stored under ``skill/references/schemas/<name>.schema.json``
    relative to the repository root.

    Args:
        name: Schema name without extension (e.g. ``"state"``, ``"task"``).

    Returns:
        The parsed schema as a Python dict.

    Raises:
        FileNotFoundError: If the schema file does not exist.
        json.JSONDecodeError: If the schema file contains invalid JSON.

    Example:
        >>> schema = load_schema("state")
        >>> schema["type"]
        'object'
    """
    schema_path = _repo_root() / "skill" / "references" / "schemas" / f"{name}.schema.json"
    if not schema_path.is_file():
        raise FileNotFoundError(
            f"Schema '{name}' not found at expected path: {schema_path}"
        )
    with schema_path.open(encoding="utf-8") as fh:
        return json.load(fh)  # type: ignore[no-any-return]


def validate(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate *data* against a JSON Schema draft 2020-12.

    Args:
        data: The object to validate.
        schema: The parsed JSON Schema dict (e.g. returned by
            :func:`load_schema`).

    Raises:
        SchemaValidationError: If *data* does not conform to *schema*, with
            a human-readable description of the first best-match violation.

    Example:
        >>> from tab_conductor.schema import load_schema, validate
        >>> schema = load_schema("event")
        >>> validate({"ts": "2026-01-01T00:00:00Z", "kind": "test"}, schema)
    """
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)

    errors = list(validator.iter_errors(data))
    if not errors:
        return

    best = best_match(errors)
    path_str = "/" + "/".join(str(p) for p in best.absolute_path) if best.absolute_path else "/"
    raise SchemaValidationError(
        f"Schema validation failed at '{path_str}': {best.message}",
        path=path_str,
    )


def validate_named(data: dict[str, Any], schema_name: str) -> None:
    """Load a schema by name and validate *data* against it.

    Convenience wrapper combining :func:`load_schema` and :func:`validate`.

    Args:
        data: The object to validate.
        schema_name: Schema name without extension (e.g. ``"state"``).

    Raises:
        FileNotFoundError: If the schema file is missing.
        SchemaValidationError: If validation fails.
    """
    schema = load_schema(schema_name)
    validate(data, schema)
