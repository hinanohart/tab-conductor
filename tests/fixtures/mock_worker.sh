#!/usr/bin/env bash
# Usage: mock_worker.sh <task_id> <prompt> [exit_code] [sleep_s]
# Environment overrides: MOCK_EXIT_CODE, MOCK_SLEEP_S
set -euo pipefail

TASK_ID="${1:-unknown}"
PROMPT="${2:-noop}"
EXIT_CODE="${MOCK_EXIT_CODE:-0}"
SLEEP_S="${MOCK_SLEEP_S:-0.3}"

emit() {
    jq -nc \
        --arg type "$1" \
        --arg ts "$(date -u +%FT%TZ)" \
        --argjson p "$2" \
        '{type:$type, ts:$ts}+$p'
}

emit system "{\"task_id\":\"${TASK_ID}\",\"prompt_excerpt\":\"${PROMPT:0:40}\"}"
sleep "$SLEEP_S"
emit assistant "{\"delta\":{\"text\":\"working on ${TASK_ID}...\"}}"
sleep "$SLEEP_S"
emit result "{\"total_cost_usd\":0.001,\"usage\":{\"input_tokens\":50,\"output_tokens\":100},\"output\":\"done:${TASK_ID}\"}"
exit "$EXIT_CODE"
