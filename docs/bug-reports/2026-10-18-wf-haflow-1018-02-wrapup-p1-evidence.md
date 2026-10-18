# PR #267 机器审核 P1：截断事件预算吞没配对失败回执（复现与修复证据）

- 来源：Codex Code Review bot finding（PR #267，"Preserve supplemented failures across the event cap"），wrapup-auto-r2 独立复现确认。
- 触发条件：历史窗口饱和（2000 行）+ 补读回执使解码行数超过 `_EVENT_CAP=2000`；配对失败回执时间序靠后，其 `tests_completed` 基行在投影层已被抑制。
- 实际后果：`assess_quality` 的 `scoped[:_EVENT_CAP]` 丢弃靠后的失败回执 → `test_anomaly` 消失且 `verification_missing`（已确认失败被静默吞没，违反"真实失败仍上报"红线）。
- 盲区成因：r6 九场景（A-I）总事件数均未越过 cap；1999/2000 边界测试的配对回执均为 passed=True。

## 复现（真实临时 SQLite + assess_task_run，无 mock）

```
$ python3 probe-p1.py   # 修复前
{"scenario": "cap-swallow(2000win+2 receipts)", "process_quality": "unknown",
 "problem_codes": ["historical_run_trace_limited","outcome_absent","trace_truncated","verification_missing"], "n_anomalies": 0}
{"scenario": "small-trace(control, same pairing)", "process_quality": "improvement_opportunity",
 "problem_codes": ["historical_run_trace_limited","outcome_absent","test_anomaly"]}
```

## 修复与复验（f167581）

- `assess_quality` 截断分支改为溢出段 `test_failed` 事件保留进预算（严格有界，先失败按时间序）。
- RED→GREEN：`test_event_cap_preserves_test_failures_beyond_the_cap`、`test_supplemented_failed_receipt_survives_event_cap`（真实链）。
- 修复后 cap-swallow：`["historical_run_trace_limited","outcome_absent","test_anomaly","trace_truncated"]`，process=improvement_opportunity。
- 回归：`pytest -q tests/test_quality_assessment.py tests/test_execution_trace.py` → 105 passed + 13 subtests；r6 九场景 A-I 重跑 ALL_PASS（无误报回归）；compileall/diff-check 干净。
- 复现脚本留存：本工位 `artifacts/wrapup-p1/probe-p1.py`（与 r6 probe 同构：init_db + 原生 INSERT + assess_task_run）。

## 附：第二轮 Codex P2（非有限 JSON 常量）复现与修复

- 复现（修复前）：`decode_stored_payload('{"a": NaN}')` → `corrupt=False`；真实 SQLite 中 `{"verification": {"passed": true, "evidence_id": "ev1", "w": NaN}}` 伪装权威回执抑制 failing `tests_completed`。
- 修复：`decode_stored_payload` 经 `parse_constant=_reject_nonfinite` 拒绝 `NaN/Infinity/-Infinity`（与 SQLite `json_valid` 语义对齐，单点解码）。
- 复验（修复后）：该场景 codes=`['historical_run_trace_limited','outcome_absent','stored_payload_corrupt','test_anomaly']`，process=improvement_opportunity；影响面 106 passed+13 subtests；r6 九场景 ALL_PASS。
