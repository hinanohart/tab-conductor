---
name: Bug report
about: Something is not working as expected
labels: ["bug"]
---

## Description
<!-- Short description of the problem. -->

## Reproduction
<!-- Minimal plan.yaml + command. -->
```yaml
# plan.yaml
```
```bash
tab-conductor run plan.yaml --mock
```

## Expected vs actual
- Expected:
- Actual:

## Environment
- OS: (Ubuntu 24.04 / macOS 14 / WSL2 ...)
- Python: `python --version`
- tab-conductor: `tab-conductor version`
- tmux: `tmux -V` (if any)
- jq: `jq --version` (if any)
- claude CLI: `claude --version` (if any)

## State / logs
<!-- Paste `.orchestrator/<run_id>/state.json` (after redaction) and relevant
     `events/*.jsonl` lines. Use `tab-conductor bugreport <run_id>` to bundle. -->

## Have you tried alternatives?
- [ ] Anthropic Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=true`)
- [ ] [claude_code_agent_farm](https://github.com/Dicklesworthstone/claude_code_agent_farm)
- [ ] [claude-squad](https://github.com/smtg-ai/claude-squad)

If yes, why did they not fit?
