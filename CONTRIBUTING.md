# Contributing to tab-conductor

Thanks for considering a contribution. tab-conductor follows a small,
focused approach: keep PRs minimal, well-tested, and easy to review.

## Setup

```bash
git clone https://github.com/hinanohart/tab-conductor.git
cd tab-conductor
make install     # uv venv + uv pip install -e ".[dev]"
make test        # pytest -m "not e2e"
make lint        # ruff check + mypy --strict
```

Required system tools: `tmux`, `jq` (for some integration tests), `git`.
Python 3.11+.

## Workflow

1. Open an issue first for non-trivial changes (architecture, new guards,
   schema changes). Small fixes / typos can skip this.
2. Branch off `main`, name `<type>/<short-slug>` (e.g. `fix/state-flock-timeout`).
3. Add tests. We expect:
   - unit tests for any new module
   - integration test for any orchestrator / runner change
   - hypothesis property test for any new invariant
4. Run `make lint && make test` locally. Keep coverage at or above 80 %.
5. Update `CHANGELOG.md` under the `[Unreleased]` section.
6. Open a PR. The PR template walks you through the checklist.

## Coding conventions

- Type hints on every public function (`mypy --strict` must pass).
- Google-style docstrings on every module and public function.
- No `print()`. Use the structured logger in `tab_conductor.logging_config`.
- No `bare except`. Catch the narrowest exception you can.
- All file I/O on `Path`, never raw strings, except where stdlib forces it.
- New JSON state fields require a schema update + migration note in
  `skill/references/ipc-protocol.md`.

## Tests

- `pytest -q -m "not e2e"` is the contract for CI.
- e2e tests (`@pytest.mark.e2e`) hit a real `claude` binary and are
  skipped by default. Run them locally with `pytest -m e2e` if you want.
- Hypothesis tests live alongside their unit-test peers and use
  `deadline=None` because flock + fsync exceeds the default budget.

## Commit messages

We do not enforce a strict format, but please:
- Use the imperative mood ("Add foo", not "Added foo").
- First line ≤ 72 chars.
- Body explains the *why*, not only the *what*.

## Security

Please do **not** open public issues for security vulnerabilities. Use
GitHub's [Security Advisories](https://github.com/hinanohart/tab-conductor/security/advisories/new)
flow. See `SECURITY.md` for the disclosure timeline.

## License

By submitting a contribution you agree to license your work under the
[MIT License](LICENSE) used by this project.
