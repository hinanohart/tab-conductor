# Troubleshooting Guide

## Known Issues

### WSL2 tmux SIGTERM bug (upstream issue #14142)

**Symptom**: Workers spawned inside tmux panes occasionally ignore SIGTERM under WSL2. The process remains in the process table but produces no output.

**Root cause**: WSL2 kernel 5.15 has a known signal delivery race for processes in tmux session contexts. Fixed in kernel 6.1+.

**Resolution**: tab-conductor uses tmux as an *optional* dashboard only. All worker management (spawning, killing, heartbeat) uses `subprocess.Popen` directly — not tmux `send-keys`. This makes the system safe even when the bug is present. The tmux pane is display-only; killing a worker via `tab-conductor kill` uses `os.kill(pid, SIGTERM)` on the recorded PID, bypassing tmux entirely.

---

### flock timeout: `StateLockTimeout`

**Symptom**: `StateLockTimeout: could not acquire state.lock within 5s`

**Causes and fixes**:

1. **Stale lock from crashed supervisor**: The lock file remains but no process holds it. Solution: `rm ~/.tab-conductor/runs/<RUN_ID>/state.lock` — safe because flock is advisory.

2. **State dir on `/mnt/c/` (NTFS)**: `fcntl.flock` is not supported on Windows NTFS filesystem mounted via 9P. Move `--state-dir` to a Linux ext4 path (e.g. `~/`).

3. **Zombie supervisor process**: Run `lsof ~/.tab-conductor/runs/<RUN_ID>/state.lock` to find the lock holder and kill it.

---

### State corruption recovery

**Symptom**: `json.JSONDecodeError` on `tab-conductor show` or `watch`.

**Procedure**:
```bash
RUN_ID=<your_run_id>
STATE_DIR=~/.tab-conductor/runs/$RUN_ID

# 1. Rename corrupt file (failure museum — never delete)
mv $STATE_DIR/state.json $STATE_DIR/state.json.bad

# 2. Restore from tmp (written atomically before replace)
ls $STATE_DIR/state.json.tmp 2>/dev/null && \
  mv $STATE_DIR/state.json.tmp $STATE_DIR/state.json && \
  echo "Restored from .tmp"

# 3. If .tmp also missing, reconstruct from events log
tab-conductor bugreport $RUN_ID  # package what we have
# Then inspect events/*.jsonl manually to determine last known good state
```

---

### `pgrep` not available

**Symptom**: `FileNotFoundError: [Errno 2] No such file or directory: 'pgrep'`

**Cause**: Minimal container images may omit `pgrep` (part of `procps`).

**Fix**: `sudo apt-get install -y procps` or `apk add procps`.

**Note**: tab-conductor uses `pgrep` only for the `ls` and `watch` commands to detect if a supervisor is still running. The core orchestrator uses `psutil` (Python) for process detection if available, falling back to `/proc/<pid>/status` on Linux.

---

### `jq` not available — mock worker skips validation

**Symptom**: `tests/fixtures/mock_worker.sh` runs but skips JSON output validation. This is expected behavior when `jq` is not installed.

**Fix** (optional, for development): `sudo apt-get install -y jq`

The mock worker still functions for dry-run testing; it just won't validate its own JSON output structure.

---

### `claude` CLI not in PATH

**Symptom**: `FileNotFoundError: claude` when running `tab-conductor run`.

**Fix**: Override the binary path via environment variable:
```bash
export CLAUDE_BIN=/path/to/claude
tab-conductor run plan.yaml
```

Or use mock mode (no claude binary needed):
```bash
tab-conductor run plan.yaml --mock
```

---

### `ANTHROPIC_API_KEY` not set

**Symptom**: Workers immediately fail with `AuthenticationError`.

**Recommended approach**: Use mock mode during development:
```bash
tab-conductor run plan.yaml --mock
```

For production, set `ANTHROPIC_API_KEY` in your environment. tab-conductor never reads or logs the key value — it is passed to worker subprocesses via environment inheritance only.

---

### Huge `state.json` causing slow `watch`

**Symptom**: `tab-conductor watch` becomes sluggish after many tasks complete.

**Cause**: The `events` array in `state.json` grows unbounded in long runs.

**Diagnosis**:
```bash
tab-conductor bugreport <RUN_ID>  # includes state.json size in report
wc -c ~/.tab-conductor/runs/<RUN_ID>/state.json
```

**Fix**: Events are also stored separately in `events/*.jsonl`. The `state.json` events array is a convenience cache. In a future release, completed events will be pruned from state.json after archival. For now, use `jq '.events | length'` to check event count.

---

### macOS: `fcntl.flock` semantics differ

**Symptom**: On macOS, `flock` on NFS-mounted directories may silently succeed without actually locking (POSIX advisory lock semantics on NFS are undefined).

**Fix**: Keep `state_dir` on a local filesystem. macOS native ext/APFS volumes work correctly. Avoid NFS or SMB for state storage.

---

### WSL2: Never place `state_dir` under `/mnt/c/`

The Windows DrvFs filesystem (exposed as `/mnt/c/` etc.) does not support `fcntl.flock`. Attempting to use it will produce immediate `StateLockTimeout` errors on every operation.

Always use a Linux-native path:
```bash
# Good
tab-conductor run plan.yaml --state-dir ~/my-runs

# Bad — will fail on WSL2
tab-conductor run plan.yaml --state-dir /mnt/c/Users/me/runs
```

---

## FAQ

**Q: Can I resume a run that was interrupted?**
A: Not yet in v0.1.0. Planned for v0.2.0. For now, `tab-conductor bugreport` the interrupted run and re-submit with a modified plan that skips already-completed tasks.

**Q: Can multiple supervisors share the same `state_dir`?**
A: No. Each run gets its own subdirectory under `state_dir` (keyed by `RUN_ID`). Two supervisors for the *same* RUN_ID would conflict. Different runs are safe to co-locate.

**Q: How do I change the stuck-detection threshold?**
A: Set `--stuck-threshold-s <seconds>` on `tab-conductor run`. Default is 120 seconds.

**Q: Workers keep getting marked `stuck` even though they're working.**
A: The worker's heartbeat interval may be slower than the stuck threshold. Increase `--stuck-threshold-s` or ensure your workers emit progress output regularly.

**Q: Can I use tab-conductor without tmux?**
A: Yes. tmux is entirely optional. Without it you lose the live dashboard, but all functionality (run/watch/kill/show) works via the CLI.

**Q: How do I see real-time worker output?**
A: `tail -f ~/.tab-conductor/runs/<RUN_ID>/logs/worker_<id>.jsonl | jq .`

**Q: The `validate` command passes but `run` fails immediately. Why?**
A: `validate` checks schema and DAG structure only. Runtime failures (missing `claude` binary, bad env vars, filesystem permissions) are not checked at validate time.

**Q: How do I set per-worker cost caps?**
A: Use `--cost-cap-usd-worker <float>` on `tab-conductor run`. Default is $1.00 per worker. Workers exceeding their cap are killed and the task marked `failed`.

**Q: Is there a way to watch multiple runs at once?**
A: Not built-in. Use `tab-conductor ls` to list runs, then open separate terminals for `tab-conductor watch` on each. A future tmux layout helper is planned.

**Q: How do I clean up old run state?**
A: Runs are never auto-deleted (failure museum policy). Manually remove old run directories: `rm -rf ~/.tab-conductor/runs/<RUN_ID>`. Consider archiving important runs with `tab-conductor bugreport` first.
