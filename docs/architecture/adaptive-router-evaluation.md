# Adaptive Router Shadow Evaluation v1

Read-only retrospective over frozen shadow recommendations. Answers
"is the Adaptive Router's prediction trustworthy?" without ever
claiming the recommended agent "would have won".

## 1. Shadow Evaluation 不是 A/B Test

For one task, only `actual_agent` ever executed. `recommended_agent`
never ran, so its outcome is unobserved. Comparing
`recommended predicted success > actual observed outcome` proves
nothing about the world where the recommendation ran. This module
therefore reports three separate things and never mixes them:

```text
Observed Actual Outcome      <- agent_execution_outcomes (facts)
Predicted Recommended Outcome<- route_decision.candidate_rankings (frozen belief)
Calibration                  <- frozen actual_agent prediction vs settled outcome
```

There is deliberately no `recommended_agent_win_rate` and no
"success would increase X%". Uplift is reported as
`predicted_uplift` with a `counterfactual estimate, not observed
fact` note.

## 2. Observed vs Predicted 的区别

| | Observed | Predicted |
|---|---|---|
| What | `qualified_success`, wall time, rework/blocked/human counts | `blended_success_rate`, `etqs_seconds`, `p50`, `sample_count`, `confidence` |
| Source | `agent_execution_outcomes` row settled at finalization | `route_decision.candidate_rankings` entry frozen at decision time |
| Identity | `(task_id, run_id)` + `workflow_id` cross-check | same identity, read from the event payload |
| Missing | decision without outcome counts toward coverage only | agent absent from rankings means unavailable, never guessed |

Evaluation rows (`herdr/shadow_evaluation.py::build_evaluation_row`)
carry both sides but are not a new source of truth.

## 3. Prediction Calibration

For `actual_agent` we own both sides: the frozen prediction and the
later outcome. Bucket the predicted probability and compare against
the observed qualified-success rate:

```text
Predicted Success   Samples   Observed Success
0.4-0.6              18          50.0%
0.6-0.8              49          73.5%
0.8-1.0              75          89.3%
```

Buckets are `[low, high)` over `blended_success_rate` (last bucket
includes 1.0). Rows without a frozen actual prediction or without a
settled outcome are excluded, never imputed.

## 4. Brier Score

```text
mean((predicted_probability - actual_0_or_1)^2)
```

with `predicted_probability = blended_success_rate` (the
prior-smoothed belief the router prices into ETQS; always present
when the ranking entry exists) and `actual = 1/0` from
`qualified_success`. Lower is better; 0 is perfect, 0.25 is
chance-level at p=0.5. Example: `p=0.8,y=1` and `p=0.2,y=0` gives
`((0.04)+(0.04))/2 = 0.04`.

## 5. ETQS 评估限制

Frozen `etqs_seconds` is compared against the observed wall time of
the same attempt:

```text
median_absolute_error = median(|predicted_etqs - observed_wall|)
```

Observed wall time is dispatch-to-finish for one attempt. It is NOT
a full time-to-qualified-success: queueing before dispatch and
retries after this outcome are invisible. The report therefore names
the section "ETQS Approximation" and carries the caveat in
`etqs.note`. Never label observed wall time `actual_ttqs`.

## 6. Data Sufficiency

Per `agent x node x task_type`, counted from settled actual
executions in this evaluation (frozen `sample_count`/`confidence`
ride along as context only):

```text
cold       < 10
warming    10-29
sufficient >= 30
```

`canary_readiness` reports `eligible_bucket_count` /
`insufficient_bucket_count` plus the bucket detail. It is facts
only: no automatic promotion, no traffic change.

## 7. 为什么本版不自动接管生产

1. No counterfactual data exists: agreement + calibration + predicted
   uplift cannot prove the recommended agent executes better.
2. Calibration must first show predicted probabilities mean what they
   say (Brier + buckets); ETQS approximation must look sane.
3. Only `sufficient` buckets may enter a future canary, and canary
   entry is a separate human decision with real A/B traffic.
4. Production selection stays untouched: `choose_agent` returns the
   legacy decision; shadow only appends `route_decision` events.

## Data flow

```text
route_decision events (state_db.query_route_decisions, bounded, newest-first)
  + agent_execution_outcomes (state_db.batch_get_execution_outcomes, chunked)
  -> herdr/shadow_rows.py (frozen decision x outcome join, read-only)
  -> herdr/shadow_metrics.py (pure aggregations + deterministic report)
  -> herdr/shadow_render.py (text rendering)
  -> herdr/shadow_evaluation.py (pipeline composition + stable public API)
  -> herdr-task shadow-eval [--node/--task-type/--agent/--since/--limit/--json]
```

CLI and module are read-only: no inserts, no updates, no router or
outcome writes. Filters apply in Python after bounded reads so
`limit` always bounds storage I/O.
