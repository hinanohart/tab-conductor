"""Unit tests for tab_conductor.cli using click.testing.CliRunner.

Covers: version, validate (valid/invalid), ls (empty), show (unknown id),
bugreport (tar.gz generation + secret redaction), watch (--exit-after).
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tab_conductor import __version__
from tab_conductor.cli import main

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_PLANS = Path(__file__).parent.parent / "fixtures" / "plans"


def _make_run_dir(tmp_path: Path, run_id: str = "TESTRUNID0000000000000000") -> Path:
    """Create a minimal run directory with a valid state.json."""
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    state: dict[str, Any] = {
        "run_id": run_id,
        "version": 3,
        "started_at": "2026-01-01T00:00:00Z",
        "ended_at": None,
        "status": "running",
        "cost_usd_total": 0.05,
        "workers": [{"id": "W1", "pid": 99999, "status": "running", "task_id": "t1"}],
        "tasks": [{"id": "t1", "status": "running"}],
        "events": [],
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "logs").mkdir()
    (run_dir / "events").mkdir()
    return run_dir


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def test_version_exits_zero() -> None:
    """``tab-conductor version`` must exit 0 and print the version string."""
    runner = CliRunner()
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


# ---------------------------------------------------------------------------
# validate — valid plan
# ---------------------------------------------------------------------------


def test_validate_valid_plan_exits_zero() -> None:
    """``tab-conductor validate <valid_plan>`` must exit 0."""
    runner = CliRunner()
    result = runner.invoke(main, ["validate", str(FIXTURES_PLANS / "sample_dag.yaml")])
    assert result.exit_code == 0
    assert "OK" in result.output


# ---------------------------------------------------------------------------
# validate — cycle plan (invalid)
# ---------------------------------------------------------------------------


def test_validate_cycle_plan_exits_nonzero() -> None:
    """``tab-conductor validate <cycle_plan>`` must exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(main, ["validate", str(FIXTURES_PLANS / "sample_invalid_cycle.yaml")])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# validate — duplicate id plan (invalid)
# ---------------------------------------------------------------------------


def test_validate_dup_id_plan_exits_nonzero() -> None:
    """``tab-conductor validate <dup_id_plan>`` must exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(main, ["validate", str(FIXTURES_PLANS / "sample_invalid_dup_id.yaml")])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ls — empty state root
# ---------------------------------------------------------------------------


def test_ls_empty_state_root_says_no_runs(tmp_path: Path) -> None:
    """``tab-conductor ls --state-root <empty>`` must output 'no runs'."""
    runner = CliRunner()
    result = runner.invoke(main, ["ls", "--state-root", str(tmp_path)])
    assert result.exit_code == 0
    assert "no runs" in result.output.lower()


def test_ls_nonexistent_state_root_says_no_runs(tmp_path: Path) -> None:
    """``tab-conductor ls`` on non-existent dir must output 'no runs'."""
    runner = CliRunner()
    nonexistent = tmp_path / "ghost"
    result = runner.invoke(main, ["ls", "--state-root", str(nonexistent)])
    assert result.exit_code == 0
    assert "no runs" in result.output.lower()


# ---------------------------------------------------------------------------
# ls — with a run
# ---------------------------------------------------------------------------


def test_ls_shows_runs(tmp_path: Path) -> None:
    """``tab-conductor ls`` lists runs that have state.json."""
    _make_run_dir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["ls", "--state-root", str(tmp_path)])
    assert result.exit_code == 0
    assert "TESTRUNID0000000000000000" in result.output


# ---------------------------------------------------------------------------
# show — unknown run_id
# ---------------------------------------------------------------------------


def test_show_unknown_run_exits_nonzero(tmp_path: Path) -> None:
    """``tab-conductor show <unknown_id>`` must exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["show", "DOESNOTEXIST", "--state-root", str(tmp_path)]
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# show — known run_id
# ---------------------------------------------------------------------------


def test_show_known_run_prints_json(tmp_path: Path) -> None:
    """``tab-conductor show <known_id>`` must exit 0 and print JSON."""
    _make_run_dir(tmp_path, "TESTRUNID0000000000000000")
    runner = CliRunner()
    result = runner.invoke(
        main, ["show", "TESTRUNID0000000000000000", "--state-root", str(tmp_path)]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["run_id"] == "TESTRUNID0000000000000000"


# ---------------------------------------------------------------------------
# bugreport — tar.gz generation + secret redaction
# ---------------------------------------------------------------------------


def test_bugreport_creates_tar_gz(tmp_path: Path) -> None:
    """``tab-conductor bugreport`` must produce a .tar.gz archive."""
    run_dir = _make_run_dir(tmp_path, "TESTRUNID0000000000000000")
    # Add a plain-text log file
    log_file = run_dir / "logs" / "worker.log"
    log_file.write_text("INFO something happened\n", encoding="utf-8")

    out = tmp_path / "report.tar.gz"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "bugreport",
            "TESTRUNID0000000000000000",
            "-o",
            str(out),
            "--state-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    with tarfile.open(str(out), "r:gz") as tar:
        names = tar.getnames()
    # state.json should be present
    assert any("state.json" in n for n in names)


def test_bugreport_redacts_secrets(tmp_path: Path) -> None:
    """Secret tokens in text files must be replaced with [REDACTED] in bugreport."""
    run_dir = _make_run_dir(tmp_path, "TESTRUNID0000000000000000")
    secret_token = "A" * 45  # 45-char base64-like → should be redacted
    secret_file = run_dir / "logs" / "env_dump.log"
    secret_file.write_text(f"TOKEN={secret_token}\n", encoding="utf-8")

    out = tmp_path / "report.tar.gz"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "bugreport",
            "TESTRUNID0000000000000000",
            "-o",
            str(out),
            "--state-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    with tarfile.open(str(out), "r:gz") as tar:
        for member in tar.getmembers():
            if "env_dump.log" in member.name:
                f = tar.extractfile(member)
                assert f is not None
                content = f.read().decode("utf-8")
                assert secret_token not in content
                assert "[REDACTED]" in content
                break
        else:
            pytest.fail("env_dump.log not found in archive")


# ---------------------------------------------------------------------------
# watch — --exit-after
# ---------------------------------------------------------------------------


def test_watch_exits_after_n_ticks(tmp_path: Path) -> None:
    """``tab-conductor watch --exit-after 2`` must exit without hanging."""
    _make_run_dir(tmp_path, "TESTRUNID0000000000000000")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "watch",
            "TESTRUNID0000000000000000",
            "--state-root",
            str(tmp_path),
            "--exit-after",
            "2",
        ],
    )
    # Should complete (not hang) with exit code 0
    assert result.exit_code == 0


def test_watch_unknown_run_exits_nonzero(tmp_path: Path) -> None:
    """``tab-conductor watch <unknown>`` must exit non-zero immediately."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["watch", "GHOST", "--state-root", str(tmp_path), "--exit-after", "1"],
    )
    assert result.exit_code != 0
