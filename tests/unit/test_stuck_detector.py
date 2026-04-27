"""Unit tests for tab_conductor.stuck_detector.

Covers:
- LIVE on fresh registration
- WARN_HEARTBEAT after heartbeat_timeout_s elapsed
- WARN_CAPTURE after capture_repeat_threshold identical captures
- STUCK when all three indicators fire simultaneously
- KILL_CANDIDATE after idle_kill_threshold_s total idle
- pgrep_alive fallback when subprocess is mocked
- pgrep FileNotFoundError → returns True (conservative fallback)
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from tab_conductor.stuck_detector import StuckDetector, StuckStatus


def _utc(dt: datetime) -> datetime:
    """Ensure *dt* is UTC-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


class TestLive:
    """Freshly registered workers should be LIVE."""

    def test_live_on_registration(self) -> None:
        """A newly registered worker with no issues is LIVE."""
        sd = StuckDetector()
        sd.register("w1", pid=99999)
        status = sd.check("w1", now=_now())
        assert status == StuckStatus.LIVE

    def test_unregistered_raises(self) -> None:
        """Checking an unregistered worker raises KeyError."""
        sd = StuckDetector()
        with pytest.raises(KeyError):
            sd.check("nobody")


# ---------------------------------------------------------------------------
# Heartbeat timeout
# ---------------------------------------------------------------------------


class TestHeartbeatTimeout:
    """WARN_HEARTBEAT fires when heartbeat is overdue."""

    def test_warn_heartbeat_after_timeout(self) -> None:
        """No heartbeat for 70 s → WARN_HEARTBEAT."""
        sd = StuckDetector(heartbeat_timeout_s=60)
        base = _now()
        sd.register("w1", pid=99999)
        # Manually set the last_heartbeat to 70 s ago
        sd._workers["w1"].last_heartbeat = base - timedelta(seconds=70)

        with patch.object(sd, "pgrep_alive", return_value=True):
            status = sd.check("w1", now=base)
        assert status == StuckStatus.WARN_HEARTBEAT

    def test_just_under_timeout_is_live(self) -> None:
        """59 s without heartbeat → still LIVE (below 60 s threshold)."""
        sd = StuckDetector(heartbeat_timeout_s=60)
        base = _now()
        sd.register("w1", pid=99999)
        sd._workers["w1"].last_heartbeat = base - timedelta(seconds=59)

        with patch.object(sd, "pgrep_alive", return_value=True):
            status = sd.check("w1", now=base)
        assert status == StuckStatus.LIVE

    def test_update_heartbeat_resets_warn(self) -> None:
        """Receiving a fresh heartbeat clears the WARN_HEARTBEAT condition."""
        sd = StuckDetector(heartbeat_timeout_s=60)
        base = _now()
        sd.register("w1", pid=99999)
        sd._workers["w1"].last_heartbeat = base - timedelta(seconds=70)

        # Send a fresh heartbeat
        fresh = base
        sd.update_heartbeat("w1", ts=fresh)

        with patch.object(sd, "pgrep_alive", return_value=True):
            status = sd.check("w1", now=fresh + timedelta(seconds=1))
        assert status == StuckStatus.LIVE


# ---------------------------------------------------------------------------
# Capture repeat detection
# ---------------------------------------------------------------------------


class TestCaptureRepeat:
    """WARN_CAPTURE fires when output is unchanged for N samples."""

    def test_warn_capture_after_threshold(self) -> None:
        """3 consecutive identical captures → WARN_CAPTURE."""
        sd = StuckDetector(capture_repeat_threshold=3)
        sd.register("w1", pid=99999)

        text = "Waiting for input..."
        for _ in range(3):
            sd.update_capture("w1", capture_text=text)

        with patch.object(sd, "pgrep_alive", return_value=True):
            status = sd.check("w1", now=_now())
        assert status == StuckStatus.WARN_CAPTURE

    def test_different_captures_reset_counter(self) -> None:
        """Changing capture text resets the repeat counter to 1 (new baseline)."""
        sd = StuckDetector(capture_repeat_threshold=3)
        sd.register("w1", pid=99999)

        for i in range(3):
            sd.update_capture("w1", capture_text=f"output-{i}")

        # Each text is unique so the counter reset to 1 each time.
        # After 3 different texts the count is 1 (just the last one seen once).
        assert sd._workers["w1"].capture_repeat_count == 1
        with patch.object(sd, "pgrep_alive", return_value=True):
            status = sd.check("w1", now=_now())
        assert status == StuckStatus.LIVE

    def test_two_repeats_below_threshold_is_live(self) -> None:
        """2 identical samples (count=2) when threshold=3 is still LIVE."""
        sd = StuckDetector(capture_repeat_threshold=3)
        sd.register("w1", pid=99999)

        text = "Still thinking..."
        # First call: count=1; second call (identical): count=2
        for _ in range(2):
            sd.update_capture("w1", capture_text=text)

        assert sd._workers["w1"].capture_repeat_count == 2
        with patch.object(sd, "pgrep_alive", return_value=True):
            status = sd.check("w1", now=_now())
        assert status == StuckStatus.LIVE


# ---------------------------------------------------------------------------
# STUCK: all three indicators
# ---------------------------------------------------------------------------


class TestAllThreeIndicators:
    """STUCK requires all three signals to fire simultaneously."""

    def test_stuck_all_three(self) -> None:
        """Heartbeat expired + capture repeating + pgrep dead → STUCK."""
        sd = StuckDetector(heartbeat_timeout_s=60, capture_repeat_threshold=3)
        base = _now()
        sd.register("w1", pid=99999)
        sd._workers["w1"].last_heartbeat = base - timedelta(seconds=70)

        text = "frozen output"
        for _ in range(3):
            sd.update_capture("w1", capture_text=text)

        with patch.object(sd, "pgrep_alive", return_value=False):
            status = sd.check("w1", now=base)
        assert status == StuckStatus.STUCK

    def test_two_indicators_not_stuck(self) -> None:
        """Heartbeat + capture bad but process alive → not STUCK."""
        sd = StuckDetector(heartbeat_timeout_s=60, capture_repeat_threshold=3)
        base = _now()
        sd.register("w1", pid=99999)
        sd._workers["w1"].last_heartbeat = base - timedelta(seconds=70)

        text = "frozen"
        for _ in range(3):
            sd.update_capture("w1", capture_text=text)

        with patch.object(sd, "pgrep_alive", return_value=True):
            status = sd.check("w1", now=base)
        # Should be WARN_HEARTBEAT (heartbeat fires before capture in check)
        assert status in (StuckStatus.WARN_HEARTBEAT, StuckStatus.WARN_CAPTURE)
        assert status != StuckStatus.STUCK


# ---------------------------------------------------------------------------
# KILL_CANDIDATE: total idle
# ---------------------------------------------------------------------------


class TestKillCandidate:
    """KILL_CANDIDATE fires after idle_kill_threshold_s of inactivity."""

    def test_kill_candidate_after_idle(self) -> None:
        """700 s idle (no heartbeat) → KILL_CANDIDATE."""
        sd = StuckDetector(idle_kill_threshold_s=600)
        base = _now()
        sd.register("w1", pid=99999)
        sd._workers["w1"].last_heartbeat = base - timedelta(seconds=700)

        with patch.object(sd, "pgrep_alive", return_value=False):
            status = sd.check("w1", now=base)
        assert status == StuckStatus.KILL_CANDIDATE

    def test_kill_candidate_takes_priority_over_stuck(self) -> None:
        """KILL_CANDIDATE has higher priority than STUCK."""
        sd = StuckDetector(
            heartbeat_timeout_s=60,
            capture_repeat_threshold=3,
            idle_kill_threshold_s=600,
        )
        base = _now()
        sd.register("w1", pid=99999)
        sd._workers["w1"].last_heartbeat = base - timedelta(seconds=700)

        text = "frozen"
        for _ in range(3):
            sd.update_capture("w1", capture_text=text)

        with patch.object(sd, "pgrep_alive", return_value=False):
            status = sd.check("w1", now=base)
        assert status == StuckStatus.KILL_CANDIDATE


# ---------------------------------------------------------------------------
# pgrep_alive: mocking subprocess
# ---------------------------------------------------------------------------


class TestPgrepAlive:
    """pgrep_alive interacts with subprocess correctly."""

    def test_pgrep_returns_true_when_children_found(self) -> None:
        """pgrep returncode=0 means children exist → alive=True."""
        sd = StuckDetector()
        with patch("tab_conductor.stuck_detector.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["pgrep", "-P", "1234"], returncode=0, stdout="5678\n", stderr=""
            )
            assert sd.pgrep_alive(1234) is True

    def test_pgrep_returns_false_when_no_children(self) -> None:
        """pgrep returncode=1 (no match) → alive=False."""
        sd = StuckDetector()
        with patch("tab_conductor.stuck_detector.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["pgrep", "-P", "1234"], returncode=1, stdout="", stderr=""
            )
            assert sd.pgrep_alive(1234) is False

    def test_pgrep_file_not_found_fallback(self) -> None:
        """If pgrep is not installed, pgrep_alive returns True (conservative)."""
        sd = StuckDetector()
        with patch(
            "tab_conductor.stuck_detector.subprocess.run",
            side_effect=FileNotFoundError("pgrep not found"),
        ):
            # Should not raise; should return True
            assert sd.pgrep_alive(1234) is True

    def test_pgrep_timeout_fallback(self) -> None:
        """If pgrep times out, pgrep_alive returns True (conservative)."""
        sd = StuckDetector()
        with patch(
            "tab_conductor.stuck_detector.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=5),
        ):
            assert sd.pgrep_alive(1234) is True
