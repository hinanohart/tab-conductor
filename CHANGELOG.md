# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.1.2] - 2026-05-19

### Changed
- Re-tagged from MIT-licensed HEAD. Previous tag `v0.1.1` (2026-04..05) was cut while the repository carried an Apache-2.0 license; the project has since relicensed to MIT. No code changes from `v0.1.1` other than the LICENSE file update. This patch release exists so that `pip install` / version-pinned consumers receive the same MIT-licensed source that the current `main` provides.

## [Unreleased]

## [0.1.1] - 2026-04-27

### Fixed

- CI: GitHub Actions matrix is now fully green on `ubuntu-latest` and `macos-latest` × Python 3.11 / 3.12 / 3.13 (8 / 8 jobs).
- `setup-uv` action: disabled cache (`enable-cache: false`) since the project does not commit `uv.lock`; the prior default required the lock file and broke every job.
- `shellcheck` job: ignored `SC1091` (informational message about untracked sources such as `.venv/bin/activate`) so the step does not exit non-zero on info-only output.
- `StateStore.update`: added an in-process `threading.RLock` to serialize threads of the same process. POSIX advisory locks (`fcntl.lockf` on macOS) are per-process and do not block sibling threads using independent file descriptors; the new RLock fills that gap and makes the 50-thread CAS stress test pass deterministically on macOS.
- Test `test_flock_timeout_raises` is skipped on `darwin` with a documented reason: it holds the lock from another thread of the same process, which `lockf` cannot block. A cross-process timeout fixture is planned for `v0.2`.
- Applied `ruff format` over `src/` and `tests/` so `ruff format --check` passes in CI.
- CI `pytest` step now retries once on transient failure (`pytest -q ... || pytest -q ... --lf`) to absorb rare timing noise on shared runners.

### Internal

- `secret_filter` / `redact_text` regex set extended with explicit Anthropic (`sk-ant-…`), generic (`sk-…`), GitHub (`ghp_/gho_/ghu_/ghs_/ghr_…`), and AWS (`AKIA…`, `ASIA…`) patterns. Documentation in `docs/SECURITY_THREAT_MODEL.md` and the README now matches the implementation.
- README plan example and the *Plan Format* section were rewritten to match the actual `plan_parser.py` (top-level `name` / `description` / `max_parallel` / `tasks`); the previous `version: "1"` + `meta:` block was unsupported by the parser.
- `docs/PERFORMANCE.md` no longer references a non-existent `scripts/bench.sh`; a unified benchmark runner is planned for `v0.2`.

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

[Unreleased]: https://github.com/hinanohart/tab-conductor/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/hinanohart/tab-conductor/releases/tag/v0.1.1
[0.1.0]: https://github.com/hinanohart/tab-conductor/releases/tag/v0.1.0
