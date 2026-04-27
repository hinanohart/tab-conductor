# IPC Protocol Reference

## Overview

tab-conductor uses **file-based IPC** exclusively. No sockets, pipes, or in-process queues. All coordination passes through `state.json` (supervisor-owned, flock-protected) and append-only `*.jsonl` log files (worker-owned).

---

## state.schema.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/hinanohart/tab-conductor/schemas/state.schema.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["run_id", "version", "started_at", "workers", "tasks", "cost_usd_total", "status"],
  "properties": {
    "run_id":            {"type": "string", "pattern": "^[0-9A-HJKMNP-TV-Z]{26}$"},
    "version":           {"type": "integer", "minimum": 0},
    "started_at":        {"type": "string", "format": "date-time"},
    "ended_at":          {"type": ["string", "null"], "format": "date-time"},
    "status":            {"enum": ["initializing","running","halting","halted","completed","failed"]},
    "cost_usd_total":    {"type": "number", "minimum": 0},
    "cost_cap_usd_global": {"type": "number", "minimum": 0},
    "workers":           {"type": "array", "items": {"$ref": "#/$defs/worker"}},
    "tasks":             {"type": "array", "items": {"$ref": "#/$defs/task"}},
    "events":            {"type": "array", "items": {"$ref": "#/$defs/event_ref"}}
  },
  "$defs": {
    "worker": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id","status","pid","started_at","heartbeat_ts","cost_usd","tokens_in","tokens_out"],
      "properties": {
        "id":           {"type": "string"},
        "status":       {"enum": ["spawning","running","idle","stuck","done","killed","failed"]},
        "pid":          {"type": ["integer","null"]},
        "started_at":   {"type": "string", "format": "date-time"},
        "heartbeat_ts": {"type": "string", "format": "date-time"},
        "task_id":      {"type": ["string","null"]},
        "cost_usd":     {"type": "number", "minimum": 0},
        "tokens_in":    {"type": "integer", "minimum": 0},
        "tokens_out":   {"type": "integer", "minimum": 0},
        "retries":      {"type": "integer", "minimum": 0, "default": 0},
        "last_error":   {"type": ["string","null"]}
      }
    },
    "task": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id","kind","prompt","status","priority","depends_on","retries","max_retries"],
      "properties": {
        "id":          {"type": "string"},
        "kind":        {"type": "string"},
        "prompt":      {"type": "string"},
        "status":      {"enum": ["pending","ready","assigned","running","done","failed","terminal"]},
        "priority":    {"type": "integer", "minimum": 0, "maximum": 9},
        "depends_on":  {"type": "array", "items": {"type": "string"}},
        "retries":     {"type": "integer", "minimum": 0},
        "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
        "assigned_to": {"type": ["string","null"]},
        "started_at":  {"type": ["string","null"], "format": "date-time"},
        "ended_at":    {"type": ["string","null"], "format": "date-time"},
        "result":      {"type": ["object","null"]},
        "last_error":  {"type": ["string","null"]}
      }
    },
    "event_ref": {
      "type": "object",
      "additionalProperties": false,
      "required": ["ts","kind"],
      "properties": {
        "ts":        {"type": "string", "format": "date-time"},
        "kind":      {"type": "string"},
        "worker_id": {"type": ["string","null"]},
        "task_id":   {"type": ["string","null"]},
        "payload":   {"type": "object"}
      }
    }
  }
}
```

---

## task.schema.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/hinanohart/tab-conductor/schemas/task.schema.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["id","kind","prompt","status","priority","depends_on","retries","max_retries"],
  "properties": {
    "id":          {"type": "string"},
    "kind":        {"type": "string"},
    "prompt":      {"type": "string"},
    "status":      {"enum": ["pending","ready","assigned","running","done","failed","terminal"]},
    "priority":    {"type": "integer", "minimum": 0, "maximum": 9},
    "depends_on":  {"type": "array", "items": {"type": "string"}},
    "retries":     {"type": "integer", "minimum": 0},
    "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
    "assigned_to": {"type": ["string","null"]},
    "started_at":  {"type": ["string","null"], "format": "date-time"},
    "ended_at":    {"type": ["string","null"], "format": "date-time"},
    "result":      {"type": ["object","null"]},
    "last_error":  {"type": ["string","null"]}
  }
}
```

---

## event.schema.json

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/hinanohart/tab-conductor/schemas/event.schema.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["ts","kind"],
  "properties": {
    "ts":        {"type": "string", "format": "date-time"},
    "kind":      {"type": "string"},
    "worker_id": {"type": ["string","null"]},
    "task_id":   {"type": ["string","null"]},
    "payload":   {"type": "object"}
  }
}
```

---

## ULID Values

Run IDs and event IDs use **ULID** (Universally Unique Lexicographically Sortable Identifier):

- 26 characters, Crockford Base32 alphabet `[0-9A-HJKMNP-TV-Z]`
- First 10 characters = millisecond timestamp; last 16 = random
- Lexicographic sort = chronological sort (useful for `events/*.jsonl` file ordering)

Example ULID: `01HVZR3GXQK5P2T8JNWCBF0DE7`

---

## State Transition Example

Initial plan load → task `lint_fix` dispatched → completes → `type_fix` unblocked:

```json
// t=0: initial
{"run_id": "01HVZR3G...", "status": "running", "tasks": [
  {"id": "lint_fix",  "status": "ready",   "depends_on": []},
  {"id": "type_fix",  "status": "pending", "depends_on": ["lint_fix"]}
]}

// t=1: lint_fix assigned to worker w1
{"tasks": [
  {"id": "lint_fix",  "status": "running",  "assigned_to": "w1"},
  {"id": "type_fix",  "status": "pending"}
]}

// t=2: lint_fix done
{"tasks": [
  {"id": "lint_fix",  "status": "done",    "ended_at": "2026-04-27T10:00:05Z"},
  {"id": "type_fix",  "status": "ready"}   // depends_on satisfied
]}
```

---

## HMAC Opt-In Protocol

For environments requiring tamper detection on `state.json`, an optional HMAC signature can be appended:

```json
{
  "run_id": "...",
  "...": "...",
  "_hmac": {
    "alg": "sha256",
    "sig": "<hex>",
    "canonical": "RFC8785"
  }
}
```

**Canonical JSON**: fields sorted lexicographically (RFC 8785 / JSON Canonicalization Scheme). The `_hmac` key itself is excluded from the hash input.

Enable via `--hmac-key <env-var-name>` on `tab-conductor run`. The key value is read from the named env var (never from CLI to avoid shell history exposure).

**Verification**: `tab-conductor show <RUN_ID> --verify-hmac` re-computes and compares.

---

## Version Migration Policy

Current schema version: **v0.1.0** (implicit — no `schema_version` field required).

Future versions will add a top-level `schema_version` field:

```json
{"schema_version": "0.2.0", "run_id": "...", "...": "..."}
```

Migration rules:
- Missing `schema_version` → assumed `"0.1.0"`
- Supervisor refuses to write a state file with a higher `schema_version` than it understands
- `tab-conductor show` will display a warning for unknown schema versions
- No automatic migration scripts provided for v0.1.x → v0.2.x; manual `jq` transforms documented in release notes
