# Detailed Comparison: tab-conductor vs Alternatives

This document provides a feature-by-feature comparison of tab-conductor against three alternatives for multi-agent / parallel Claude Code orchestration.

---

## Projects Compared

| Project | Repo | Stability |
|---|---|---|
| **tab-conductor** | [hinanohart/tab-conductor](https://github.com/hinanohart/tab-conductor) | alpha (v0.1.0) |
| **Anthropic Agent Teams** | [docs.anthropic.com/en/docs/build-with-claude/agents](https://docs.anthropic.com/en/docs/build-with-claude/agents) | experimental |
| **claude_code_agent_farm** | [Dicklesworthstone/claude_code_agent_farm](https://github.com/Dicklesworthstone/claude_code_agent_farm) | stable |
| **claude-squad** | [smtg-ai/claude-squad](https://github.com/smtg-ai/claude-squad) | stable |

---

## Feature Matrix

| Feature | tab-conductor | Anthropic Agent Teams | claude_code_agent_farm | claude-squad |
|---|---|---|---|---|
| **Requires experimental flag** | No | Yes (`--dangerously-skip-permissions`) | No | No |
| **IPC mechanism** | JSON state file (flock + atomic rename) | In-process (SDK) | Shared files / sockets | tmux panes |
| **Persistent state across crash** | Yes (state.json survives) | No | Partial (log files only) | No |
| **Per-worker cost cap** | Yes (`--max-budget-usd`) | API-level enforcement | No | No |
| **Global cost cap** | Yes (SIGTERM escalation) | No | No | No |
| **Secret deny-list** | Yes (regex scrub before any write) | No | No | No |
| **Stuck detection** | Yes (3-layer: heartbeat/hash/pgrep) | No | No | No |
| **Failure museum** | Yes (`.orchestrator/` never deleted) | No | No | No |
| **bugreport command** | Yes (tar.gz, secrets redacted) | No | No | No |
| **Claude Code Skill** | Yes (`~/.claude/skills/tab-conductor/`) | N/A | No | No |
| **DAG task plans** | Yes (YAML, cycle detection) | No | No | Partial (sequential) |
| **HMAC state signing** | Optional (`hmac_signing: true`) | No | No | No |
| **Retry with backoff** | Yes (max_retries + 10s/60s backoff) | No | No | No |
| **OS support** | Linux, macOS, WSL2 | Linux (primarily) | Linux | Linux |
| **JSON Schema validation** | Yes (JSON Schema 2020-12, strict) | No | No | No |
| **tmux dashboard** | Optional (graceful fallback) | No | Yes (required) | Yes (required) |
| **git worktree support** | Planned (v0.2) | Unknown | Yes | No |
| **Multi-LLM** | Via prompt override | No | Partial | No |

---

## Narrative Analysis

### vs Anthropic Agent Teams

Anthropic Agent Teams is the **official** solution. It runs agents within the Claude Code process using the `--dangerously-skip-permissions` experimental flag. This gives it the deepest integration with Claude Code internals (tool access, permission model). The trade-off is that it requires opting into experimental behavior and provides no persistent state between runs.

tab-conductor is a **stable, observable fallback** for cases where:
- The experimental flag is not acceptable in production or CI
- Post-mortem analysis of agent behavior is required
- Cost guardrails must be enforced at the infrastructure level

### vs claude_code_agent_farm

claude_code_agent_farm demonstrates large-scale parallelism (10–50 workers) via Python multiprocessing. It focuses on throughput for batch tasks. It does not provide a cost cap, secret scrubbing, or stuck detection.

tab-conductor trades raw throughput for **observability and safety**: every state transition is logged, every secret is scrubbed, and every stuck worker is detected and killed within `stuck_threshold_s` seconds (default: 120s).

### vs claude-squad

claude-squad uses tmux panes as the IPC boundary — each worker is a tmux window. This gives a great interactive debugging experience. The downside is that tmux must be running, sessions are not crash-safe, and there is no programmatic state to query.

tab-conductor uses **file-based IPC** (state.json + flock) so it works in headless CI environments, survives process restarts, and exposes machine-readable state for monitoring and alerting.

---

## When to Use What

| Use case | Recommendation |
|---|---|
| Official, deepest Claude Code integration | Anthropic Agent Teams |
| Maximum raw throughput, batch processing | claude_code_agent_farm |
| Interactive debugging with tmux panes | claude-squad |
| Stable CI pipeline, cost enforcement, post-mortem | **tab-conductor** |
| Crash-safe, observable, secret-safe production workloads | **tab-conductor** |
