"""Integration tests: env allow-list and secret variable isolation."""

from __future__ import annotations

import os
from pathlib import Path

from tab_conductor.runner import build_worker_env

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_secret_token_not_in_worker_env(tmp_path: Path) -> None:
    """SECRET_TOKEN must not appear in allow-listed env dict."""
    os.environ["SECRET_TOKEN"] = "super_secret_value_12345"
    try:
        env = build_worker_env({})
        assert "SECRET_TOKEN" not in env, "SECRET_TOKEN leaked into worker env"
        assert "super_secret_value_12345" not in env.values()
    finally:
        os.environ.pop("SECRET_TOKEN", None)


def test_anthropic_api_key_explicit_passthrough(tmp_path: Path) -> None:
    """ANTHROPIC_API_KEY is forwarded only when pass_anthropic_key=True."""
    os.environ["ANTHROPIC_API_KEY"] = "sk-test-key"
    try:
        env_no_key = build_worker_env({}, pass_anthropic_key=False)
        assert "ANTHROPIC_API_KEY" not in env_no_key

        env_with_key = build_worker_env({}, pass_anthropic_key=True)
        assert env_with_key.get("ANTHROPIC_API_KEY") == "sk-test-key"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
