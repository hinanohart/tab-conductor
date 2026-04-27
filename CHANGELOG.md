# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-27

### Added

- Initial public release
- Subprocess-based worker spawning with `claude -p --output-format stream-json` parsing
- JSON state store with `flock` (Linux) / `lockf` (macOS) + `os.replace` atomic write
- JSON Schema 2020-12 strict validation (`unevaluatedProperties: false`) on every state read/write
- Cost cap enforcement: per-worker `$1.00` and global `$5.00` with SIGINT→SIGTERM→SIGKILL escalation
- Secret deny-list: regex scrub of `ANTHROPIC_API_KEY`, `sk-*`, `ghp_*`, `AKIA*`, `AWS_SECRET_ACCESS_KEY` before any write
- Stuck detection: 3-layer (heartbeat timeout / capture hash stall / pgrep liveness)
- Optional HMAC-SHA256 worker-write signing (`hmac_signing: true` in plan meta)
- DAG task plans (YAML) with cycle detection, topological sort, retry max 2 + exponential backoff (10s / 60s)
- Task kinds: `task` (default) and `verify` (retry once on fail)
- CLI with 8 subcommands: `run`, `ls`, `show`, `watch`, `kill`, `bugreport`, `validate`, `version`
- `bugreport` command: packages state + logs as tar.gz with secrets redacted
- Claude Code Skill bundle (`~/.claude/skills/tab-conductor/`) with progressive disclosure (SKILL.md ≤ 100 lines + `references/` subdirectory)
- 5 example scenarios: `scenario_a_refactor`, `scenario_b_translate`, `scenario_c_multi_provider`, `scenario_d_pipeline`, `scenario_e_repro`
- 165+ tests: unit, hypothesis property tests, concurrent stress tests (50-thread state write)
- 83% test coverage
- macOS support via `fcntl.lockf` fallback (`sys.platform == "darwin"`)
- WSL2 support with documented caveats (no `/mnt/c/` state, XDG_RUNTIME_DIR fallback)
- `--mock` flag for dry-run without spawning real `claude -p` subprocesses
- Structured JSON logging (`supervisor.jsonl`, `worker_<id>.jsonl`)
- tmux dashboard (optional; graceful fallback when tmux unavailable or `XDG_RUNTIME_DIR` unset)
- ULID-based run and worker IDs for lexicographic sorting

[Unreleased]: https://github.com/hinanohart/tab-conductor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hinanohart/tab-conductor/releases/tag/v0.1.0
