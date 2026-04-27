"""Unit tests for tab_conductor.cost_guard.

Covers:
- per_worker threshold exceeded → OVER_PER_WORKER
- other workers unaffected by one worker's overage
- global threshold exceeded by combined usage → OVER_GLOBAL
- escalation_step timing: 0s/4s→SIGINT, 6s→SIGTERM, 11s→SIGKILL
- token-based USD estimation is monotonically increasing with token count
- reset() zeroes counters and allows re-accumulation
- WARN status fires below limit at 80 %
- summary() returns correct aggregated data
"""

from __future__ import annotations

import pytest

from tab_conductor.cost_guard import CostGuard, CostStatus


class TestPerWorkerLimit:
    """Per-worker USD cap enforcement."""

    def test_over_per_worker(self) -> None:
        """Exceeding per-worker limit returns OVER_PER_WORKER."""
        guard = CostGuard(per_worker_usd=1.0, global_usd=10.0)
        # Add usage that exceeds the $1.00 per-worker cap
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=1.01)
        assert guard.check("w1") == CostStatus.OVER_PER_WORKER

    def test_other_worker_unaffected(self) -> None:
        """Another worker's overage does not affect a within-limit worker."""
        guard = CostGuard(per_worker_usd=1.0, global_usd=10.0)
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=1.50)
        guard.add_usage("w2", tokens_in=0, tokens_out=0, usd=0.10)
        # w1 is over, w2 should be fine (global is 1.60 < 10.0)
        assert guard.check("w1") == CostStatus.OVER_PER_WORKER
        assert guard.check("w2") == CostStatus.OK

    def test_within_limit_is_ok(self) -> None:
        """Usage well below the per-worker cap returns OK."""
        guard = CostGuard(per_worker_usd=1.0, global_usd=10.0)
        guard.add_usage("w1", tokens_in=1000, tokens_out=100)
        assert guard.check("w1") == CostStatus.OK

    def test_unknown_worker_is_ok(self) -> None:
        """A worker with no recorded usage returns OK."""
        guard = CostGuard()
        assert guard.check("unknown") == CostStatus.OK


class TestGlobalLimit:
    """Global aggregate USD cap enforcement."""

    def test_over_global(self) -> None:
        """Combined usage exceeding global cap returns OVER_GLOBAL."""
        guard = CostGuard(per_worker_usd=3.0, global_usd=5.0)
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=2.60)
        guard.add_usage("w2", tokens_in=0, tokens_out=0, usd=2.60)
        # total = 5.20 > 5.0; each worker is below per-worker cap of 3.0
        assert guard.check("w1") == CostStatus.OVER_GLOBAL
        assert guard.check("w2") == CostStatus.OVER_GLOBAL

    def test_global_takes_priority_over_per_worker(self) -> None:
        """OVER_GLOBAL is returned even when per-worker is also over."""
        guard = CostGuard(per_worker_usd=1.0, global_usd=5.0)
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=1.50)
        # Add enough from other workers to push global over
        for i in range(5):
            guard.add_usage(f"wx{i}", tokens_in=0, tokens_out=0, usd=0.80)
        assert guard.check("w1") == CostStatus.OVER_GLOBAL


class TestWarnStatus:
    """WARN fires at 80 % of the limit."""

    def test_warn_per_worker_at_80_percent(self) -> None:
        """80 % of per-worker budget triggers WARN."""
        guard = CostGuard(per_worker_usd=1.0, global_usd=10.0)
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=0.80)
        assert guard.check("w1") == CostStatus.WARN

    def test_warn_global_at_80_percent(self) -> None:
        """80 % of global budget triggers WARN even if per-worker is fine."""
        guard = CostGuard(per_worker_usd=5.0, global_usd=10.0)
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=4.0)
        guard.add_usage("w2", tokens_in=0, tokens_out=0, usd=4.0)
        # total = 8.0 = 80 % of 10.0
        assert guard.check("w1") == CostStatus.WARN


class TestEscalationStep:
    """Escalation ladder timing."""

    def test_zero_seconds_sigint(self) -> None:
        """0 s since overage → SIGINT."""
        guard = CostGuard()
        assert guard.escalation_step("w1", since_over=0.0) == "SIGINT"

    def test_four_seconds_sigint(self) -> None:
        """4 s since overage → still SIGINT (boundary is at 5 s)."""
        guard = CostGuard()
        assert guard.escalation_step("w1", since_over=4.9) == "SIGINT"

    def test_six_seconds_sigterm(self) -> None:
        """6 s since overage → SIGTERM."""
        guard = CostGuard()
        assert guard.escalation_step("w1", since_over=6.0) == "SIGTERM"

    def test_eleven_seconds_sigkill(self) -> None:
        """11 s since overage → SIGKILL."""
        guard = CostGuard()
        assert guard.escalation_step("w1", since_over=11.0) == "SIGKILL"

    def test_boundary_five_seconds_sigterm(self) -> None:
        """Exactly 5.0 s → SIGTERM (boundary inclusive on upper range)."""
        guard = CostGuard()
        assert guard.escalation_step("w1", since_over=5.0) == "SIGTERM"

    def test_boundary_ten_seconds_sigkill(self) -> None:
        """Exactly 10.0 s → SIGKILL."""
        guard = CostGuard()
        assert guard.escalation_step("w1", since_over=10.0) == "SIGKILL"


class TestTokenEstimation:
    """Token-based USD estimation sanity checks."""

    def test_estimation_is_monotonic(self) -> None:
        """More tokens → higher estimated USD (monotonic)."""
        guard_small = CostGuard(per_worker_usd=100.0, global_usd=1000.0)
        guard_large = CostGuard(per_worker_usd=100.0, global_usd=1000.0)

        guard_small.add_usage("w1", tokens_in=10_000, tokens_out=1_000)
        guard_large.add_usage("w1", tokens_in=100_000, tokens_out=10_000)

        small_usd = guard_small.summary()["total_usd"]
        large_usd = guard_large.summary()["total_usd"]
        assert small_usd > 0.0
        assert large_usd > small_usd

    def test_explicit_usd_overrides_estimate(self) -> None:
        """Explicit usd= argument is used directly, ignoring token counts."""
        guard = CostGuard(per_worker_usd=100.0, global_usd=1000.0)
        guard.add_usage("w1", tokens_in=999_999, tokens_out=999_999, usd=0.01)
        assert guard.summary()["workers"]["w1"]["usd"] == pytest.approx(0.01)


class TestReset:
    """reset() clears per-worker counters."""

    def test_reset_clears_counters(self) -> None:
        """After reset() usage is zeroed and status returns to OK."""
        guard = CostGuard(per_worker_usd=1.0, global_usd=10.0)
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=2.0)
        assert guard.check("w1") == CostStatus.OVER_PER_WORKER

        guard.reset("w1")
        assert guard.check("w1") == CostStatus.OK
        assert guard.summary()["workers"]["w1"]["usd"] == pytest.approx(0.0)

    def test_reset_unknown_worker_is_noop(self) -> None:
        """reset() on an untracked worker does not raise."""
        guard = CostGuard()
        guard.reset("nonexistent")  # should not raise

    def test_recount_after_reset(self) -> None:
        """Counters accumulate correctly after a reset."""
        guard = CostGuard(per_worker_usd=1.0, global_usd=10.0)
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=2.0)
        guard.reset("w1")
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=0.30)
        assert guard.check("w1") == CostStatus.OK
        assert guard.summary()["workers"]["w1"]["usd"] == pytest.approx(0.30)


class TestSummary:
    """summary() returns correct aggregated data."""

    def test_summary_aggregates_all_workers(self) -> None:
        """summary() total_usd equals sum of per-worker costs."""
        guard = CostGuard(per_worker_usd=5.0, global_usd=20.0)
        guard.add_usage("w1", tokens_in=0, tokens_out=0, usd=1.0)
        guard.add_usage("w2", tokens_in=0, tokens_out=0, usd=2.0)

        s = guard.summary()
        assert s["total_usd"] == pytest.approx(3.0)
        assert s["workers"]["w1"]["usd"] == pytest.approx(1.0)
        assert s["workers"]["w2"]["usd"] == pytest.approx(2.0)
        assert "tokens_in" in s["workers"]["w1"]
        assert "tokens_out" in s["workers"]["w1"]
