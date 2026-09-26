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

Evaluation rows (`herdr/shadow_rows.py::build_evaluation_row`)
carry both sides but are not a new source of truth. Each row also
carries `agreement_status` (`same` / `different` / `unknown`) and a
lightweight `model_evidence` snapshot (every frozen candidate's
`{agent, sample_count, confidence}`).

## 2a. Agreement 是三态的

A missing `recommended_agent` is `unknown`, never defaulted. Unknown
rows are counted (`unknown_decision_count`) but excluded from the
disagreement denominator and from disagreement grouping and uplift:

```text
disagreement_rate = different / (same + different)
```

"Unknown recommendation" ≠ "router disagreed".

## 2b. Outcome 归属只认执行身份

`_outcome_matches` 是唯一的归属contract（fail-closed）：

```text
(task_id, run_id) 相等
+ workflow_id 双方非空时相等
+ decision.actual_agent == outcome.agent（无fallback）
+ node 双方非空时相等
+ task_type 双方非空时相等
```

Prediction属于哪个执行身份，Outcome必须由同一身份产生；
不确定一律不关联（保留decision覆盖计数，不进Outcome评估）。

## 2c. 一个 execution 最多一个评估样本

`select_authoritative_execution_rows` 按 `(task_id, run_id)` 去重：
仅保留带Outcome的行，取 `decision_at` 最大（`decision_event_id`
tie-break），即最后一次与真实执行者一致的决策。报告明确区分
`decision_rows`（coverage/agreement/disagreement/uplift）与
`execution_rows`（actual outcome/calibration/Brier/ETQS/
sufficiency/canary）；coverage同时给出 `total_route_decisions`、
`unique_executions`、`settled_executions`。

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

Headline P50s cover different populations (all frozen predictions vs
settled successes), so the report also publishes paired medians over
the exact same rows:

```text
paired_predicted_etqs_p50 / paired_observed_wall_time_p50
```

Compare paired-against-paired; the overall P50s are context only.
`_median` is the standard mathematical median (even count averages
the two middle values); `_percentile` stays nearest-rank for P90.

## 6. Data Sufficiency：模型证据与校准证据是两回事

Per `agent x node x task_type`, each bucket carries two independent
evidence sides (never conflated):

```text
Model evidence:      model_sample_count / model_confidence
                     (frozen history the router predicted with;
                      max over candidates across ALL decision rows,
                      no Outcome required)
Evaluation evidence: evaluation_sample_count (settled outcomes)
                     calibration_sample_count (frozen prediction
                       paired with a settled outcome)
```

Each side gets its own status (`cold < 10 / warming 10-29 /
sufficient >= 30`); a missing side is `unknown`, never zero-filled.
Example: 50 history samples but 1 calibrated outcome reports
`model_data_status = sufficient` alongside
`evaluation_data_status = cold` — the router had history, but the
prediction is not yet calibrated.

`canary_readiness` publishes fact counts only
(`model_sufficient_*`, `evaluation_sufficient_*`,
`sufficient_both_*`) with an explicit no-eligibility-verdict note.
It is facts only: no automatic promotion, no traffic change.

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
  -> herdr/shadow_rows.py (filter-then-limit paginated scan + frozen join)
  -> herdr/shadow_metrics.py (pure aggregations over rows)
  -> herdr/shadow_sufficiency.py (model vs evaluation evidence)
  -> herdr/shadow_render.py (text rendering)
  -> herdr/shadow_evaluation.py (pipeline composition + stable public API)
  -> herdr-task shadow-eval [--node/--task-type/--agent/--since/--limit/--json]
```

Filters apply DURING a newest-first paginated scan (exact keyset
cursor on `(timestamp, id)`), BEFORE the limit: each page reads at
most `min(page_size, scan_cap - scanned)` events, batch-joins that
page's Outcomes, builds rows, filters, and stops the moment matched
`limit` is reached. Exactly one stop reason is reported —
`matched_limit` / `scan_cap` / `exhausted` / `cursor_stalled` — in
`report["collection"]` (`source_window_size / matched_rows /
requested_limit / scan_cap / stop_reason / truncated / ...`), so a
filtered report can never be mistaken for "all of history".

CLI and module are read-only: no inserts, no updates, no router or
outcome writes. Filters apply in Python after bounded reads so
`limit` always bounds storage I/O.

## 8. True read-only DB access

Shadow Evaluation opens the HAFlow state database in SQLite read-only
mode and never initializes or migrates schema. The full chain is:

```text
herdr-task shadow-eval
  -> resolve_state_db_path() (pure path math, no StateStore, no init_db)
  -> shadow_evaluation.run_shadow_evaluation
    -> shadow_rows._collect_rows_with_meta
      -> state_db.query_route_decisions / batch_get_execution_outcomes
        -> state_db._open_shadow_read_connection
          -> get_readonly_db_connection (URI mode=ro + PRAGMA query_only=ON)
          -> assert_shadow_read_capability (SELECT-only sqlite_master /
             PRAGMA table_info check)
```

Consequences:

- The boundary is HAFlow persistent state: `shadow-eval` never creates
  the DB, its directory or the `state.db.schema.lock` file, and never
  runs `_ensure_schema` DDL, migrations, `schema_meta` writes or
  `journal_mode` changes. SQLite's own `-wal`/`-shm` coordination files
  are explicitly out of scope: opening a WAL-mode DB in `mode=ro` may
  let SQLite materialize them for the connection's lifetime and remove
  them again on a clean close. They are transient OS-level WAL
  coordination, not application data, and a pre-open check gating them
  would be a TOCTOU race anyway -- the contract tolerates them by
  design. Application rows, schema and journal mode stay invariant.
- An absent DB fails with `[SHADOW-EVAL] state database not found:
  ...` (nonzero exit), never with an auto-created empty database.
- A legacy DB missing a table or column fails with
  `[SHADOW-EVAL] database schema is not compatible with shadow
  evaluation: ...` (nonzero exit) -- schema incompatible always means
  fail, never migrate. A read-only diagnostic observes state; it never
  repairs the database it is observing.
- The production runtime (`get_db_connection`, `SQLiteStateStore`,
  `init_db`) is untouched: only the shadow evaluation read path uses
  the read-only connection.
