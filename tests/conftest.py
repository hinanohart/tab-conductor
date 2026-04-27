"""Shared pytest fixtures for tab-conductor tests."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_run_dir() -> Path:  # type: ignore[misc]
    """Create a temporary directory for state store tests and clean it up.

    Returns:
        Path to a freshly-created temporary directory under ``/tmp``.
        The directory and all its contents are removed after the test.

    Example:
        >>> def test_something(tmp_run_dir: Path) -> None:
        ...     assert tmp_run_dir.is_dir()
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="tab_conductor_test_", dir="/tmp"))
    yield tmp_dir  # type: ignore[misc]
    shutil.rmtree(tmp_dir, ignore_errors=True)
