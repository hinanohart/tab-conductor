"""Secret-path deny filter for tab-conductor workers.

Prevents worker subprocesses from reading credential files by checking
candidate paths against a curated deny-list of patterns and directories.
Symlink traversal and ``..`` directory traversal are both blocked by
resolving the realpath before matching.

Design decisions:
- ``~/.ssh/known_hosts`` is explicitly **allowed** because SSH operations
  need to read it; all other ``~/.ssh/`` entries are denied.
- ``home`` is injectable for hermetic testing (pass ``tmp_path``).
- No external dependencies; pure stdlib.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from tab_conductor.exceptions import SecretAccessDenied
from tab_conductor.logging_config import get_logger, structured_event

_logger: logging.Logger = get_logger("tab_conductor.secret_filter")

# ---------------------------------------------------------------------------
# Deny-list: basename glob patterns (matched after resolve)
# ---------------------------------------------------------------------------

_DENY_BASENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    ".envrc",
    "*.pem",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*.key",
    "credentials*.json",
    "kaggle.json",
    ".netrc",
    "*.p12",
    "*.pfx",
    "*.jks",
    "gh-token*",
)

# ---------------------------------------------------------------------------
# Deny-list: home-relative directory prefixes
# Each entry is a *tuple* of path parts relative to $HOME so we can
# reconstruct the absolute prefix after expanding home.
# ---------------------------------------------------------------------------

_DENY_HOME_DIRS: tuple[tuple[str, ...], ...] = (
    (".aws",),
    (".kaggle",),
    (".ssh",),  # individually excluded: known_hosts is allowed below
    (".modal",),
    (".gnupg",),
)

# Specific file paths relative to $HOME that are denied regardless of
# directory-level rules (used for granular files inside allowed dirs).
_DENY_HOME_FILES: tuple[tuple[str, ...], ...] = (
    (".config", "gh", "hosts.yml"),
    (".aws", "credentials"),
    (".netrc",),
)

# Paths relative to $HOME that are explicitly ALLOWED even when their parent
# directory would otherwise be denied.
_ALLOW_HOME_FILES: tuple[tuple[str, ...], ...] = ((".ssh", "known_hosts"),)


def _resolve(path: str | Path) -> Path:
    """Expand user and resolve symlinks / ``..`` segments.

    Uses ``strict=False`` so the path need not exist on disk (guards should
    be proactive, not reactive).

    Args:
        path: Raw path string or :class:`Path` from worker request.

    Returns:
        Fully resolved absolute :class:`Path`.
    """
    return Path(path).expanduser().resolve(strict=False)


def denied_reason(path: str | Path, *, home: Path | None = None) -> str | None:
    """Return a human-readable deny reason, or ``None`` if the path is allowed.

    Performs resolution then tests against all deny rules in order:

    1. Explicit allow list (``~/.ssh/known_hosts``).
    2. Home-relative specific file deny list.
    3. Home-relative directory prefix deny list.
    4. Basename glob pattern deny list.

    Args:
        path: The raw path submitted by the worker.
        home: Override for the home directory (defaults to ``Path.home()``).
            Useful in tests: pass ``tmp_path`` to avoid touching real home.

    Returns:
        A short reason string if denied, ``None`` if the path is allowed.
    """
    effective_home: Path = home if home is not None else Path.home()
    resolved: Path = _resolve(path)

    # --- 1. Explicit allow list ---
    for parts in _ALLOW_HOME_FILES:
        allow_path = (effective_home / Path(*parts)).resolve(strict=False)
        if resolved == allow_path:
            return None

    # --- 2. Home-relative specific file deny ---
    for parts in _DENY_HOME_FILES:
        deny_path = (effective_home / Path(*parts)).resolve(strict=False)
        if resolved == deny_path:
            return f"matches home-relative secret file: ~/{'/'.join(parts)}"

    # --- 3. Home-relative directory prefix deny ---
    for dir_parts in _DENY_HOME_DIRS:
        dir_path = (effective_home / Path(*dir_parts)).resolve(strict=False)
        # resolved must be equal to or inside dir_path
        try:
            resolved.relative_to(dir_path)
        except ValueError:
            continue
        return f"inside secret directory: ~/{'/'.join(dir_parts)}/"

    # --- 4. Basename glob pattern deny ---
    name = resolved.name
    for pattern in _DENY_BASENAME_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return f"basename matches secret pattern: {pattern!r}"

    return None


def is_denied(path: str | Path, *, home: Path | None = None) -> bool:
    """Return ``True`` if *path* matches any deny rule.

    Resolves symlinks and ``..`` traversal before matching so that crafted
    paths cannot bypass the deny-list.

    Args:
        path: The raw path submitted by the worker.
        home: Override for the home directory.  Pass ``tmp_path`` in tests.

    Returns:
        ``True`` if access should be denied, ``False`` if the path is safe.

    Example:
        >>> is_denied("~/.env")
        True
        >>> is_denied("/tmp/output.json")
        False
    """
    reason = denied_reason(path, home=home)
    denied = reason is not None
    if denied:
        structured_event(
            _logger,
            "secret.denied",
            path=str(path),
            resolved=str(_resolve(path)),
            reason=reason,
        )
    return denied


def assert_allowed(path: str | Path, *, home: Path | None = None) -> None:
    """Assert that *path* is not a secret path, raising if it is.

    Args:
        path: The raw path submitted by the worker.
        home: Override for the home directory.  Pass ``tmp_path`` in tests.

    Raises:
        SecretAccessDenied: If the path matches any deny rule, with the
            human-readable reason attached to the exception.

    Example:
        >>> assert_allowed("/tmp/safe_output.txt")  # no-op
        >>> assert_allowed("~/.env")  # raises SecretAccessDenied
    """
    reason = denied_reason(path, home=home)
    if reason is not None:
        resolved = _resolve(path)
        structured_event(
            _logger,
            "secret.assert_denied",
            path=str(path),
            resolved=str(resolved),
            reason=reason,
        )
        raise SecretAccessDenied(resolved, reason)
