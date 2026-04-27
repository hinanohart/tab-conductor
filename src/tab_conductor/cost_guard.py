"""Token and USD cost guard for tab-conductor workers.

Tracks per-worker and global token/cost accumulation and returns a
:class:`CostStatus` enum so the caller can decide how to react (warn,
escalate, kill).  The guard itself never raises :class:`BudgetExceeded`;
that decision belongs to the caller.

Token-to-USD estimation (MVP, Sonnet pricing):
- Input:  $3.00 / 1 M tokens
- Output: $15.00 / 1 M tokens

These are overridable via the class-level constants
:attr:`CostGuard.USD_PER_M_IN` and :attr:`CostGuard.USD_PER_M_OUT`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from tab_conductor.logging_config import get_logger, structured_event

_logger: logging.Logger = get_logger("tab_conductor.cost_guard")


class CostStatus(Enum):
    """Status returned by :meth:`CostGuard.check`.

    Values are ordered from benign to critical so ``>`` comparisons work.
    """

    OK = "OK"
    """Usage is within all thresholds."""

    WARN = "WARN"
    """Usage has reached 80 % of either per-worker or global budget."""

    OVER_PER_WORKER = "OVER_PER_WORKER"
    """This worker has exceeded its individual budget."""

    OVER_GLOBAL = "OVER_GLOBAL"
    """The sum of all workers has exceeded the global budget."""


@dataclass
class _WorkerStats:
    """Accumulated statistics for a single worker.

    Attributes:
        tokens_in: Total prompt/input tokens consumed.
        tokens_out: Total completion/output tokens produced.
        usd: Total USD cost (estimated or reported).
        over_since: Monotonic timestamp when cost first exceeded per-worker
            budget, or ``None`` if not yet exceeded.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    over_since: float | None = None


class CostGuard:
    """Thread-safe guard that tracks token and USD costs across workers.

    Args:
        per_worker_usd: Per-worker USD spending cap.  Defaults to $1.00.
        global_usd: Global (sum of all workers) USD spending cap.  Defaults
            to $5.00.

    Attributes:
        USD_PER_M_IN: Cost in USD per million input tokens.
        USD_PER_M_OUT: Cost in USD per million output tokens.

    Example:
        >>> guard = CostGuard(per_worker_usd=0.50, global_usd=2.0)
        >>> guard.add_usage("w1", tokens_in=100_000, tokens_out=10_000)
        >>> guard.check("w1")
        <CostStatus.OK: 'OK'>
    """

    USD_PER_M_IN: float = 3.00
    USD_PER_M_OUT: float = 15.00

    # WARN threshold as a fraction of the limit
    _WARN_FRACTION: float = 0.80

    def __init__(
        self,
        *,
        per_worker_usd: float = 1.0,
        global_usd: float = 5.0,
    ) -> None:
        """Initialise with budget limits.

        Args:
            per_worker_usd: Per-worker spending cap in USD.
            global_usd: Global spending cap in USD for all workers combined.
        """
        if per_worker_usd <= 0:
            raise ValueError(f"per_worker_usd must be positive, got {per_worker_usd}")
        if global_usd <= 0:
            raise ValueError(f"global_usd must be positive, got {global_usd}")

        self._per_worker_usd = per_worker_usd
        self._global_usd = global_usd
        self._workers: dict[str, _WorkerStats] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_usage(
        self,
        worker_id: str,
        tokens_in: int,
        tokens_out: int,
        usd: float | None = None,
    ) -> None:
        """Accumulate token and cost usage for *worker_id*.

        If *usd* is provided (e.g. from a Claude stream-json ``usage.cost_usd``
        field), it is used directly.  Otherwise the cost is estimated from the
        token counts using :attr:`USD_PER_M_IN` / :attr:`USD_PER_M_OUT`.

        Args:
            worker_id: Unique identifier for the worker subprocess.
            tokens_in: Number of prompt/input tokens in this batch.
            tokens_out: Number of completion/output tokens in this batch.
            usd: Exact cost in USD if known, ``None`` to use token-based
                estimation.
        """
        if tokens_in < 0:
            raise ValueError(f"tokens_in must be non-negative, got {tokens_in}")
        if tokens_out < 0:
            raise ValueError(f"tokens_out must be non-negative, got {tokens_out}")

        cost_usd: float = (
            usd
            if usd is not None
            else (
                tokens_in * self.USD_PER_M_IN / 1_000_000.0
                + tokens_out * self.USD_PER_M_OUT / 1_000_000.0
            )
        )

        with self._lock:
            stats = self._workers.setdefault(worker_id, _WorkerStats())
            stats.tokens_in += tokens_in
            stats.tokens_out += tokens_out
            stats.usd += cost_usd

            # Record the first moment this worker went over budget
            if stats.usd > self._per_worker_usd and stats.over_since is None:
                stats.over_since = time.monotonic()

        structured_event(
            _logger,
            "cost.usage_added",
            worker_id=worker_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )

    def check(self, worker_id: str) -> CostStatus:
        """Return the :class:`CostStatus` for *worker_id*.

        Checks (in priority order):

        1. Global total exceeds ``global_usd`` → :attr:`CostStatus.OVER_GLOBAL`
        2. Worker total exceeds ``per_worker_usd`` → :attr:`CostStatus.OVER_PER_WORKER`
        3. Either is ≥ 80 % of limit → :attr:`CostStatus.WARN`
        4. Otherwise → :attr:`CostStatus.OK`

        Args:
            worker_id: The worker to check.  Unknown workers return OK with
                zero usage.

        Returns:
            The most severe applicable :class:`CostStatus`.
        """
        with self._lock:
            stats = self._workers.get(worker_id, _WorkerStats())
            global_total = sum(s.usd for s in self._workers.values())

        if global_total > self._global_usd:
            status = CostStatus.OVER_GLOBAL
        elif stats.usd > self._per_worker_usd:
            status = CostStatus.OVER_PER_WORKER
        elif (
            stats.usd >= self._per_worker_usd * self._WARN_FRACTION
            or global_total >= self._global_usd * self._WARN_FRACTION
        ):
            status = CostStatus.WARN
        else:
            status = CostStatus.OK

        if status not in (CostStatus.OK,):
            structured_event(
                _logger,
                "cost.check",
                worker_id=worker_id,
                status=status.value,
                worker_usd=stats.usd,
                global_usd=global_total,
            )
        return status

    def escalation_step(
        self, worker_id: str, since_over: float
    ) -> Literal["SIGINT", "SIGTERM", "SIGKILL"]:
        """Return the appropriate escalation signal given seconds since budget exceeded.

        Escalation ladder:
        - ``[0, 5)`` seconds → ``SIGINT``  (polite interrupt)
        - ``[5, 10)`` seconds → ``SIGTERM`` (termination request)
        - ``≥ 10`` seconds → ``SIGKILL``  (forced kill)

        Args:
            worker_id: The worker being escalated (used for logging only).
            since_over: Elapsed seconds since the worker first exceeded its
                budget.

        Returns:
            One of ``"SIGINT"``, ``"SIGTERM"``, or ``"SIGKILL"``.
        """
        if since_over < 5.0:
            signal: Literal["SIGINT", "SIGTERM", "SIGKILL"] = "SIGINT"
        elif since_over < 10.0:
            signal = "SIGTERM"
        else:
            signal = "SIGKILL"

        structured_event(
            _logger,
            "cost.escalation",
            worker_id=worker_id,
            since_over_s=since_over,
            signal=signal,
        )
        return signal

    def summary(self) -> dict[str, Any]:
        """Return a snapshot of all usage statistics.

        Returns:
            A dict with:
            - ``"total_usd"``: sum across all workers.
            - ``"total_tokens_in"``: sum of input tokens.
            - ``"total_tokens_out"``: sum of output tokens.
            - ``"workers"``: per-worker dict mapping *worker_id* to a
              sub-dict with ``usd``, ``tokens_in``, ``tokens_out``.
        """
        with self._lock:
            workers_snap = {
                wid: {
                    "usd": s.usd,
                    "tokens_in": s.tokens_in,
                    "tokens_out": s.tokens_out,
                }
                for wid, s in self._workers.items()
            }
            total_usd = sum(s.usd for s in self._workers.values())
            total_in = sum(s.tokens_in for s in self._workers.values())
            total_out = sum(s.tokens_out for s in self._workers.values())

        return {
            "total_usd": total_usd,
            "total_tokens_in": total_in,
            "total_tokens_out": total_out,
            "workers": workers_snap,
        }

    def reset(self, worker_id: str) -> None:
        """Reset usage counters for *worker_id* (e.g., before a retry).

        If *worker_id* is not tracked, this is a no-op.

        Args:
            worker_id: The worker whose counters should be zeroed.
        """
        with self._lock:
            if worker_id in self._workers:
                self._workers[worker_id] = _WorkerStats()
        structured_event(_logger, "cost.reset", worker_id=worker_id)
