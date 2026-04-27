"""Structured JSONL logging configuration for tab-conductor.

Provides a factory for loggers that emit one JSON object per line, suitable
for machine parsing and log aggregation.  Each record includes an RFC 3339
timestamp, level, logger name, message, and any extra key-value pairs passed
via the ``extra`` keyword argument.

Rotation is handled by :class:`logging.handlers.RotatingFileHandler` at 10 MiB
with up to 5 backup files.  A parallel stream handler writes to stdout.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON with RFC 3339 timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise *record* to a compact JSON string.

        Args:
            record: The log record produced by a :class:`logging.Logger`.

        Returns:
            A single-line JSON string ending without a newline.
        """
        ts = (
            datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        payload: dict[str, Any] = {
            "ts": ts,
            "lvl": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }

        # Collect extra fields: anything not in standard LogRecord attributes.
        _standard_keys = {
            "args", "created", "exc_info", "exc_text", "filename", "funcName",
            "levelname", "levelno", "lineno", "message", "module", "msecs",
            "msg", "name", "pathname", "process", "processName", "relativeCreated",
            "stack_info", "thread", "threadName", "taskName",
        }
        extra: dict[str, Any] = {
            k: v for k, v in record.__dict__.items() if k not in _standard_keys
        }
        if extra:
            payload["extra"] = extra

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str, log_path: Path | None = None) -> logging.Logger:
    """Return a structured JSONL logger, creating handlers on first call.

    If the logger already has handlers attached (e.g., called twice with the
    same *name*), no duplicate handlers are added.

    Args:
        name: Logger hierarchy name (e.g. ``"tab_conductor.state"``).
        log_path: Optional filesystem path for the rotating file handler.
            If ``None``, only the stdout handler is attached.

    Returns:
        A configured :class:`logging.Logger` instance.

    Raises:
        OSError: If *log_path* cannot be opened for writing.

    Example:
        >>> import tempfile, pathlib
        >>> from tab_conductor.logging_config import get_logger
        >>> log = get_logger("test", pathlib.Path(tempfile.mktemp(suffix=".log")))
        >>> log.info("hello", extra={"worker": "w1"})
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = _JsonFormatter()

    # Stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    stdout_handler.setLevel(logging.DEBUG)
    logger.addHandler(stdout_handler)

    # Rotating file handler (optional)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_path),
            maxBytes=10 * 1024 * 1024,  # 10 MiB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)

    # Prevent propagation to root logger to avoid duplicate output
    logger.propagate = False
    return logger


def structured_event(
    logger: logging.Logger,
    event: str,
    **kwargs: Any,
) -> None:
    """Emit a structured log record with a named *event* and arbitrary fields.

    Wraps :meth:`logging.Logger.info` with a fixed ``event`` key so that log
    aggregation systems can group and filter by event type rather than parsing
    free-form message strings.

    Args:
        logger: The :class:`logging.Logger` to emit through.
        event: Short, dot-namespaced event name (e.g. ``"secret.denied"``).
        **kwargs: Additional key-value pairs merged into the ``extra`` dict.

    Example:
        >>> from tab_conductor.logging_config import get_logger, structured_event
        >>> log = get_logger("tab_conductor.example")
        >>> structured_event(log, "cost.warn", worker_id="w1", usd=1.05)
    """
    extra: dict[str, Any] = {"event": event, **kwargs}
    logger.info(event, extra=extra)
