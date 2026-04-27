"""Centralised exception hierarchy for tab-conductor.

All custom exceptions raised by any tab-conductor sub-module are defined here
so that callers can catch them with a single import site.  Each exception
carries structured attributes that allow supervisory code to make policy
decisions without string-parsing the message.

Hierarchy::

    TabConductorError (base)
    ├── StateLockTimeout
    ├── StateCorrupted
    ├── StateVersionMismatch
    ├── SchemaValidationError
    ├── SecretAccessDenied
    ├── BudgetExceeded
    └── HmacKeyMissing
"""

from __future__ import annotations

from pathlib import Path


class TabConductorError(Exception):
    """Base class for all tab-conductor runtime errors.

    Catching this class will catch every custom exception raised by the library.
    """


# ---------------------------------------------------------------------------
# State / schema (previously defined inline in state.py / schema.py)
# ---------------------------------------------------------------------------


class StateLockTimeout(TabConductorError, TimeoutError):
    """Raised when an advisory lock cannot be acquired within the timeout.

    Attributes:
        timeout: Seconds waited before giving up.
        lock_path: Filesystem path of the contested lock file.
    """

    def __init__(self, timeout: float, path: Path) -> None:
        """Initialise with timeout duration and lock path.

        Args:
            timeout: Seconds waited before giving up.
            path: Filesystem path of the contested lock file.
        """
        super().__init__(f"Could not acquire lock on '{path}' within {timeout:.1f}s")
        self.timeout = timeout
        self.lock_path = path


class StateCorrupted(TabConductorError, RuntimeError):
    """Raised when state.json cannot be parsed as valid JSON.

    Attributes:
        state_path: Filesystem path of the corrupted state file.
        reason: Human-readable description of the parse failure.
    """

    def __init__(self, path: Path, reason: str) -> None:
        """Initialise with file path and parse error description.

        Args:
            path: Filesystem path of the corrupted state file.
            reason: Human-readable description of the parse failure.
        """
        super().__init__(f"State file '{path}' is corrupted: {reason}")
        self.state_path = path
        self.reason = reason


class StateVersionMismatch(TabConductorError, RuntimeError):
    """Raised when a concurrent writer incremented version before us.

    Attributes:
        expected: The version we read before the update attempt.
        actual: The version found when we re-read inside the lock.
    """

    def __init__(self, expected: int, actual: int) -> None:
        """Initialise with expected and actual version numbers.

        Args:
            expected: The version we read before the update attempt.
            actual: The version found when we re-read inside the lock.
        """
        super().__init__(
            f"State version mismatch: expected {expected}, found {actual}. "
            "A concurrent writer modified state before this update could commit."
        )
        self.expected = expected
        self.actual = actual


class SchemaValidationError(TabConductorError, ValueError):
    """Raised when a JSON object fails schema validation.

    Attributes:
        message: Human-readable description of the first (best-match) error.
        path: JSON Pointer indicating where the violation occurred.
    """

    def __init__(self, message: str, path: str = "") -> None:
        """Initialise with a human-readable message and optional JSON path.

        Args:
            message: Concise description of the validation failure.
            path: Dotted or slash-separated path to the offending field.
        """
        super().__init__(message)
        self.path = path


# ---------------------------------------------------------------------------
# Secret filter
# ---------------------------------------------------------------------------


class SecretAccessDenied(TabConductorError, PermissionError):
    """Raised when a worker attempts to access a secret path.

    Attributes:
        denied_path: The resolved absolute path that was denied.
        reason: Human-readable explanation of why the path is denied.
    """

    def __init__(self, path: Path | str, reason: str) -> None:
        """Initialise with the denied path and a reason string.

        Args:
            path: The path (resolved or raw) that triggered the deny rule.
            reason: Short explanation of the matching deny rule.
        """
        self.denied_path = Path(path)
        self.reason = reason
        super().__init__(f"Secret access denied for '{path}': {reason}")


# ---------------------------------------------------------------------------
# Cost guard
# ---------------------------------------------------------------------------


class BudgetExceeded(TabConductorError, RuntimeError):
    """Raised (optionally) when cost limits are exceeded.

    The :class:`~tab_conductor.cost_guard.CostGuard` itself returns a status
    enum rather than raising; callers may raise this exception when they
    decide to abort based on the status.

    Attributes:
        reason: Human-readable summary of which limit was breached.
    """

    def __init__(self, reason: str) -> None:
        """Initialise with a description of the exceeded budget.

        Args:
            reason: Description of which limit was breached and by how much.
        """
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# HMAC signer
# ---------------------------------------------------------------------------


class PlanParseError(TabConductorError, ValueError):
    """Raised when a YAML plan file fails validation.

    Attributes:
        message: Human-readable description of the parse failure.
        path: Filesystem path of the plan file (if available).
        line: Line number of the offending content (if available).
    """

    def __init__(
        self,
        message: str,
        path: Path | str | None = None,
        line: int | None = None,
    ) -> None:
        """Initialise with a message, optional file path, and optional line number.

        Args:
            message: Concise description of the validation failure.
            path: Filesystem path of the plan file, if known.
            line: Line number where the error occurred, if known.
        """
        loc = ""
        if path is not None:
            loc += f" in '{path}'"
        if line is not None:
            loc += f" at line {line}"
        super().__init__(f"Plan parse error{loc}: {message}")
        self.message = message
        self.path: Path | str | None = path
        self.line = line


class HmacKeyMissing(TabConductorError, RuntimeError):
    """Raised when an HMAC operation is attempted with no key configured.

    Attributes:
        env_var: The environment variable that was consulted for the key.
    """

    def __init__(self, env_var: str = "TAB_CONDUCTOR_HMAC_KEY") -> None:
        """Initialise with the name of the missing environment variable.

        Args:
            env_var: Name of the environment variable expected to hold the key.
        """
        super().__init__(
            f"HMAC key not set.  Provide it via the '{env_var}' environment variable "
            "or pass key= explicitly to HmacSigner."
        )
        self.env_var = env_var
