"""Unit tests for tab_conductor.plan_parser.

Tests cover: happy-path parsing, duplicate id detection, unknown depends_on,
DAG cycle detection, priority range validation, invalid YAML rejection, size
limit enforcement, and empty task list rejection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tab_conductor.exceptions import PlanParseError
from tab_conductor.plan_parser import ParsedPlan, parse_plan, parse_plan_dict, redact_text

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent.parent / "fixtures" / "plans"


def _make_plan(**overrides: object) -> dict[object, object]:
    """Return a minimal valid plan dict, with optional overrides."""
    base: dict[object, object] = {
        "name": "test_plan",
        "tasks": [
            {
                "id": "t1",
                "prompt": "Do something",
            }
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_parse_valid_plan_dict_minimal() -> None:
    """A minimal plan dict with one task should parse without error."""
    plan = parse_plan_dict({"tasks": [{"id": "t1", "prompt": "Hello"}]})
    assert isinstance(plan, ParsedPlan)
    assert len(plan.tasks) == 1
    assert plan.tasks[0]["id"] == "t1"
    assert plan.tasks[0]["kind"] == "general"
    assert plan.tasks[0]["priority"] == 5
    assert plan.tasks[0]["max_retries"] == 2
    assert plan.tasks[0]["depends_on"] == []


def test_parse_valid_sample_dag_file() -> None:
    """The sample_dag.yaml fixture should parse cleanly with 5 tasks."""
    plan = parse_plan(FIXTURES / "sample_dag.yaml")
    assert len(plan.tasks) == 5
    task_ids = {t["id"] for t in plan.tasks}
    assert "lint_fix" in task_ids
    assert "integration_check" in task_ids
    assert plan.metadata["name"] == "scenario_a_refactor"
    assert plan.metadata["max_parallel"] == 4


def test_parse_valid_sample_fifo_file() -> None:
    """The sample_fifo.yaml fixture should parse 3 tasks with no deps."""
    plan = parse_plan(FIXTURES / "sample_fifo.yaml")
    assert len(plan.tasks) == 3
    for task in plan.tasks:
        assert task["depends_on"] == []
        assert task["priority"] == 5


def test_parse_metadata_defaults_filled_in() -> None:
    """Missing name/description/max_parallel in plan dict get defaults."""
    plan = parse_plan_dict({"tasks": [{"id": "x", "prompt": "p"}]})
    assert plan.metadata["name"] == ""
    assert plan.metadata["description"] == ""
    assert plan.metadata["max_parallel"] == 4


def test_parse_plan_priority_boundaries() -> None:
    """Priority 0 and 9 should both be accepted."""
    for pval in (0, 9):
        plan = parse_plan_dict({
            "tasks": [{"id": "t", "prompt": "p", "priority": pval}]
        })
        assert plan.tasks[0]["priority"] == pval


def test_parse_plan_max_retries_boundaries() -> None:
    """max_retries 0 and 10 should both be accepted."""
    for mr in (0, 10):
        plan = parse_plan_dict({
            "tasks": [{"id": "t", "prompt": "p", "max_retries": mr}]
        })
        assert plan.tasks[0]["max_retries"] == mr


# ---------------------------------------------------------------------------
# Duplicate id detection
# ---------------------------------------------------------------------------


def test_duplicate_id_raises() -> None:
    """Two tasks with the same id must raise PlanParseError."""
    data = {
        "tasks": [
            {"id": "dup", "prompt": "First"},
            {"id": "dup", "prompt": "Second"},
        ]
    }
    with pytest.raises(PlanParseError, match="Duplicate task id"):
        parse_plan_dict(data)


def test_duplicate_id_file_fixture() -> None:
    """The sample_invalid_dup_id.yaml fixture must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="Duplicate task id"):
        parse_plan(FIXTURES / "sample_invalid_dup_id.yaml")


# ---------------------------------------------------------------------------
# Unknown depends_on reference
# ---------------------------------------------------------------------------


def test_unknown_depends_on_raises() -> None:
    """A depends_on pointing to a non-existent id must raise PlanParseError."""
    data = {
        "tasks": [
            {"id": "t1", "prompt": "p", "depends_on": ["ghost_task"]},
        ]
    }
    with pytest.raises(PlanParseError, match="unknown id 'ghost_task'"):
        parse_plan_dict(data)


# ---------------------------------------------------------------------------
# DAG cycle detection
# ---------------------------------------------------------------------------


def test_dag_cycle_raises() -> None:
    """A two-node mutual dependency must raise PlanParseError."""
    data = {
        "tasks": [
            {"id": "a", "prompt": "p", "depends_on": ["b"]},
            {"id": "b", "prompt": "p", "depends_on": ["a"]},
        ]
    }
    with pytest.raises(PlanParseError, match="[Cc]ycle"):
        parse_plan_dict(data)


def test_dag_cycle_file_fixture() -> None:
    """The sample_invalid_cycle.yaml fixture must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="[Cc]ycle"):
        parse_plan(FIXTURES / "sample_invalid_cycle.yaml")


# ---------------------------------------------------------------------------
# Priority range validation
# ---------------------------------------------------------------------------


def test_priority_too_low_raises() -> None:
    """Priority below 0 must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="priority"):
        parse_plan_dict({"tasks": [{"id": "t", "prompt": "p", "priority": -1}]})


def test_priority_too_high_raises() -> None:
    """Priority above 9 must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="priority"):
        parse_plan_dict({"tasks": [{"id": "t", "prompt": "p", "priority": 10}]})


def test_priority_non_int_raises() -> None:
    """A non-integer priority must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="priority"):
        parse_plan_dict({"tasks": [{"id": "t", "prompt": "p", "priority": "high"}]})


# ---------------------------------------------------------------------------
# Invalid YAML
# ---------------------------------------------------------------------------


def test_invalid_yaml_file_raises(tmp_path: Path) -> None:
    """A file with invalid YAML must raise PlanParseError."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("tasks: [\n  {id: broken\n", encoding="utf-8")
    with pytest.raises(PlanParseError):
        parse_plan(bad)


# ---------------------------------------------------------------------------
# Size limit (> 1 MB)
# ---------------------------------------------------------------------------


def test_size_over_limit_raises(tmp_path: Path) -> None:
    """A plan file larger than 1 MB must raise PlanParseError."""
    big = tmp_path / "big.yaml"
    # Write a valid YAML header then pad with comment lines to exceed 1 MB
    big.write_text("tasks:\n  - id: t\n    prompt: p\n" + "# x" * 400_000, encoding="utf-8")
    assert big.stat().st_size > 1_024 * 1_024
    with pytest.raises(PlanParseError, match="bytes"):
        parse_plan(big)


# ---------------------------------------------------------------------------
# Empty tasks list
# ---------------------------------------------------------------------------


def test_empty_tasks_raises() -> None:
    """A plan with an empty tasks list must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="empty"):
        parse_plan_dict({"tasks": []})


def test_missing_tasks_key_raises() -> None:
    """A plan dict without a 'tasks' key must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="tasks"):
        parse_plan_dict({"name": "no_tasks"})


# ---------------------------------------------------------------------------
# Missing / empty prompt
# ---------------------------------------------------------------------------


def test_missing_prompt_raises() -> None:
    """A task without 'prompt' must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="prompt"):
        parse_plan_dict({"tasks": [{"id": "t1"}]})


def test_empty_prompt_raises() -> None:
    """A task with an empty string 'prompt' must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="prompt"):
        parse_plan_dict({"tasks": [{"id": "t1", "prompt": "   "}]})


# ---------------------------------------------------------------------------
# max_retries range
# ---------------------------------------------------------------------------


def test_max_retries_too_high_raises() -> None:
    """max_retries above 10 must raise PlanParseError."""
    with pytest.raises(PlanParseError, match="max_retries"):
        parse_plan_dict({"tasks": [{"id": "t", "prompt": "p", "max_retries": 11}]})


# ---------------------------------------------------------------------------
# redact_text helper
# ---------------------------------------------------------------------------


def test_redact_text_base64_like() -> None:
    """A 40+ char base64-like token must be redacted."""
    raw = "token: " + "A" * 45
    result = redact_text(raw)
    assert "[REDACTED]" in result
    assert "A" * 45 not in result


def test_redact_text_env_like() -> None:
    """An env-var-like TOKEN= assignment must be redacted."""
    raw = "API_KEY=supersecretvalue123"
    result = redact_text(raw)
    assert "[REDACTED]" in result
