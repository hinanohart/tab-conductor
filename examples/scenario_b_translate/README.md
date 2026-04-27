# Scenario B: Concurrent Document Translation

Translate one source document into three languages simultaneously, then review all outputs with a single verification worker.

Translation tasks have no dependencies between them — all three run at full parallelism. The review task waits until all translations complete.

## Run

```bash
tab-conductor validate examples/scenario_b_translate/plan.yaml
tab-conductor run examples/scenario_b_translate/plan.yaml --mock --max-parallel 3
```

## Expected result

- 3 translation workers run in parallel
- `review_translations` runs after all three complete
- Run status: `completed` with 4 tasks done

## Production notes

Adjust `max_parallel` to match your API concurrency quota. For large documents, add `max_retries: 1` to translation tasks to handle transient API errors gracefully.
