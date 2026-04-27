# Performance — tab-conductor

> **Note**: This document contains **design targets**, not measured values.
> A consolidated `scripts/bench.sh` runner is planned for `v0.2`. Until then,
> reproduce individual measurements with the inline `time` commands shown
> below and record the values in your fork or in a release-notes appendix.

---

## Mock Worker Wall-Time (3 parallel tasks)

Measures end-to-end time for a 3-task DAG with no real Claude subprocess (mock mode).

```bash
time tab-conductor run examples/scenario_a_refactor/plan.yaml \
    --mock --max-parallel 3 --state-dir /tmp/bench_demo
```

| Python version | Wall time (mock, 3 workers) | Notes |
|---|---|---|
| 3.11 | TBD | Target: < 2s |
| 3.12 | TBD | |
| 3.13 | TBD | |

Expected: dominated by `subprocess.Popen` fork overhead + poll interval (1s). A 3-task DAG with mock workers should complete in ~1.5s (1× poll interval + fork startup).

---

## Subprocess Fork Overhead

`claude -p` is a Node.js binary. Subprocess fork overhead on typical hardware:

| Platform | Fork + first-line overhead | Notes |
|---|---|---|
| Linux (ext4, i9-12900K) | TBD | Target: < 300ms |
| macOS (APFS, M2) | TBD | |
| WSL2 (ext4 VolFS) | TBD | WSL2 fork ~2× slower than native Linux |

Measurement script:

```bash
time claude -p "echo ok" --output-format stream-json --no-pager 2>/dev/null | head -1
```

---

## State Write Throughput

Atomic state writes (`flock + os.replace`) under concurrent load. Measured by `tests/unit/test_property_state.py::test_concurrent_writes`.

| Scenario | Writes/s | Notes |
|---|---|---|
| 50 threads, single state file | TBD | Target: > 500 writes/s |
| 10 threads, no contention | TBD | |

On WSL2, `fcntl.flock` adds ~0.5ms overhead per lock acquisition. At 50 concurrent writers, throughput is expected to be ~200–400 writes/s.

---

## JSON Schema Validation Cost

`jsonschema` validation is called on every state read and write.

| Schema size | Validation time | Notes |
|---|---|---|
| 1 task, 1 worker (minimal) | TBD | Target: < 1ms |
| 10 tasks, 10 workers | TBD | |
| 50 tasks, 50 workers | TBD | |

Measurement:

```python
import timeit
from tab_conductor.schema import validate_state

# minimal state dict
result = timeit.timeit(lambda: validate_state(state_dict), number=1000)
print(f"{result/1000*1000:.3f} ms per call")
```

Expected: `jsonschema` with a ~2KB state dict takes approximately 0.3–1ms per call on modern hardware. For 50 tasks, expect < 5ms.

---

## Supervisor Loop Overhead

The supervisor loop runs every `POLL_INTERVAL_S = 1.0` seconds. Loop overhead (excluding worker spawn and state I/O):

| Operation | Time | Notes |
|---|---|---|
| Read state.json (flock) | TBD | Target: < 10ms |
| Merge 10 worker logs | TBD | |
| Write state.json (flock + os.replace) | TBD | |
| Total loop overhead (10 workers) | TBD | Target: < 50ms total |

At 10 workers and 1s poll interval, supervisor overhead is expected to be < 5% of wall time.

---

## Python 3.11 vs 3.12 vs 3.13

Key performance improvements across Python versions relevant to tab-conductor:

| Version | Relevant improvement |
|---|---|
| 3.11 | Adaptive interpreter (2x speedup on some workloads) |
| 3.12 | Per-interpreter GIL foundations; `subprocess` improvements |
| 3.13 | Experimental no-GIL (3.13t); `asyncio` improvements |

tab-conductor uses `subprocess.Popen` (not asyncio), so Python version impact on worker spawn is minimal. The largest gain between 3.11 and 3.13 is expected in JSON parsing (~10–20%).

---

## Benchmark Update Instructions

After measuring, update the `TBD` values in this file (or in a release-notes
appendix) and commit. A unified `scripts/bench.sh` driver is planned for
`v0.2` and will record results to `docs/bench_results.txt` automatically.
