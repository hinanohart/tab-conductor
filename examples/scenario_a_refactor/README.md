# Scenario A: Parallel Module Refactor

Refactor two modules in parallel, running lint and type-check workers concurrently, then verifying with a single pytest worker once all module work is done.

The DAG shape: `lint_a` and `lint_b` run simultaneously, then `type_a` and `type_b` unblock in parallel, then `verify` runs last.

## Run

```bash
tab-conductor validate examples/scenario_a_refactor/plan.yaml
tab-conductor run examples/scenario_a_refactor/plan.yaml --mock --max-parallel 4
```

## Expected result

- 4 workers active at peak (lint_a, lint_b + type_a or type_b)
- `verify` runs last after all module tasks complete
- Run status: `completed` with 5 tasks done

## Production notes

In real usage replace `src/module_a/` and `src/module_b/` with actual paths in your repo. Add `max_retries: 2` to type-check tasks if mypy errors may cascade across multiple files.
