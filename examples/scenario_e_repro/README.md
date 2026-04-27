# Scenario E: Multi-Strategy Bug Reproduction

Run three different bug reproduction strategies in parallel — minimal repro script, integration test suite, and git bisect — then triage the combined results with a single synthesis worker.

This pattern dramatically reduces the time to confirm and root-cause a regression compared to running strategies sequentially.

## Run

```bash
tab-conductor validate examples/scenario_e_repro/plan.yaml
tab-conductor run examples/scenario_e_repro/plan.yaml --mock --max-parallel 3
```

## Expected result

- 3 repro workers run in parallel
- `triage` runs after all three complete and synthesizes a report
- Run status: `completed` with 4 tasks done

## Production notes

Set `max_retries: 0` on `repro_bisect` — git bisect is stateful and retrying may produce inconsistent results. Update the prompt references (`issue #42`, `test_issue42.py`) to match your actual issue before running in production.
