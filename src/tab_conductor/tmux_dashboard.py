"""Optional tmux dashboard integration for tab-conductor (Phase 5 placeholder).

Provides a minimal availability check and a placeholder attach function.
Full implementation is deferred to Phase 5.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tab_conductor.logging_config import get_logger

_logger = get_logger("tab_conductor.tmux_dashboard")


def is_available() -> bool:
    """Return True if tmux is installed and on PATH.

    Returns:
        ``True`` if ``shutil.which("tmux")`` finds the binary, ``False`` otherwise.
    """
    return shutil.which("tmux") is not None


def attach_window(run_id: str, state_dir: Path) -> None:
    """Attach (or create) a tmux window for the given run.

    .. note::
        This is a Phase 5 placeholder.  The live dashboard is not yet
        implemented.  The function logs the state directory path so the
        caller can inspect progress manually.

    Args:
        run_id: ULID identifying the orchestrator run.
        state_dir: Root state directory containing state.json.
    """
    _logger.info(
        "tmux dashboard not yet implemented; see state at %s",
        state_dir / run_id / "state.json",
        extra={"run_id": run_id, "state_dir": str(state_dir)},
    )
