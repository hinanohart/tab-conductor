# Scenario C: Multi-Provider / Multi-Strategy Evaluation

Run the same analytical task with three different model configurations in parallel, then compare outputs with a dedicated comparison worker.

Useful for: model selection, prompt evaluation, A/B testing prompts across quality tiers.

## Run

```bash
tab-conductor validate examples/scenario_c_multi_provider/plan.yaml
tab-conductor run examples/scenario_c_multi_provider/plan.yaml --mock --max-parallel 3
```

## Expected result

- 3 evaluation workers run simultaneously
- `compare_outputs` runs after all evaluations complete
- Run status: `completed` with 4 tasks done

## Production notes

In real usage, pass different `--model` flags or system prompts per worker by using separate wrapper scripts. The `prompt` field can contain model-specific instructions when the claude CLI is invoked with `-p`.
