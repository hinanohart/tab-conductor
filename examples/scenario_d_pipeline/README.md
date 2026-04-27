# Scenario D: CI/CD Pipeline DAG

Model a typical CI/CD pipeline as a DAG: unit and integration tests run in parallel, then lint, then build, then deploy. Each stage waits for the previous to complete.

This pattern is useful for long-running automated workflows where each stage's output feeds the next.

## Run

```bash
tab-conductor validate examples/scenario_d_pipeline/plan.yaml
tab-conductor run examples/scenario_d_pipeline/plan.yaml --mock --max-parallel 2
```

## Expected result

- `unit_tests` and `integration_tests` run in parallel
- `lint_check` starts after both tests pass
- `build_artifact` then `deploy_staging` run sequentially
- Run status: `completed` with 5 tasks done

## Production notes

Set `max_retries: 0` on `deploy_staging` to prevent accidental double-deploys. Use cost caps to guard against runaway LLM usage on the deploy step.
