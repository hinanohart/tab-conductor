---
name: tab-conductor
description: Spawn and supervise N parallel Claude Code worker processes via tmux/subprocess; aggregate JSON state with cost cap, secret deny, and stuck detection. Use when the user asks for parallel workers, multi-tab orchestration, supervisor / worker tabs, DAG pipelines, parallel refactor across files, concurrent translation, multi-strategy bug repro, or fleet of claude -p instances.
argument-hint: "[run plan.yaml | ls | watch RUN_ID | kill RUN_ID | bugreport RUN_ID]"
allowed-tools: Bash(tab-conductor *) Bash(jq *) Bash(tmux *) Read Write Edit Glob Grep
model: inherit
effort: medium
---

## When to use this skill

- User asks for parallel workers or multi-tab orchestration across files / repos
- Tasks decompose into independent or loosely-dependent subtasks (DAG shape)
- Concurrent translation, refactoring, lint + type-check across modules
- Multi-strategy bug reproduction (try 3 approaches in parallel)
- Fleet of `claude -p` instances with shared cost / secret guardrails

## When NOT to use

- Tasks require tight sequential context sharing (one worker needs the prior's output inline)
- Single-task jobs where spawning overhead outweighs benefit
- Anthropic Agent Teams (experimental) already satisfies the use case
- Real-time interactive UX required — `watch` polling (1s) may be too coarse

## Core commands

| Command | What it does |
|---------|-------------|
| `run <plan.yaml>` | Parse plan, spawn workers, supervise to completion |
| `ls` | List all runs under the state root directory |
| `show <RUN_ID>` | Pretty-print current state.json for a run |
| `watch <RUN_ID>` | Live-refresh progress every 1 second |
| `kill <RUN_ID>` | Send SIGTERM to all workers of a run |
| `bugreport <RUN_ID>` | Package state + logs as tar.gz with secrets redacted |
| `validate <plan.yaml>` | Validate YAML plan without running (dry-run safe) |

## Quickstart

```bash
# 1. Install
uv venv && uv pip install -e ".[dev]"

# 2. Validate your plan
tab-conductor validate examples/scenario_a_refactor/plan.yaml

# 3. Dry-run with mock workers (no API key needed)
tab-conductor run examples/scenario_a_refactor/plan.yaml --mock --max-parallel 3 \
    --state-dir .orchestrator/demo
```

## Plan format

```yaml
name: "my_run"
max_parallel: 4           # concurrent workers cap
tasks:
  - id: "lint"
    kind: "refactor"      # free-form label
    prompt: "Fix ruff issues in src/"
    priority: 5           # 0 (low) – 9 (high)
    depends_on: []        # task IDs that must complete first
    max_retries: 1        # 0–10, default 2
```

## Safety guarantees

- **Secret deny**: state + logs never contain API keys, SSH paths, or `.env` content
- **Cost cap**: per-worker default $1.00; global default $5.00; halts run on breach
- **Stuck detection**: worker with no heartbeat for 120s is marked `stuck` and killed
- **Atomic state**: flock + `os.replace` — no partial writes, safe under concurrent reads
- **Failure museum**: failed run state persisted to `state_dir/` — never silently deleted

## See also

- [Architecture & IPC overview](references/architecture.md)
- [IPC protocol & JSON Schema](references/ipc-protocol.md)
- [Troubleshooting guide](references/troubleshooting.md)

## Memory backup reminder

After a session using this skill, run `bash ~/.claude/hooks/memory-backup.sh` (R10).
