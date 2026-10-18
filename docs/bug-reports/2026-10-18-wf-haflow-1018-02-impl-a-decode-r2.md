# 缺陷复盘报告: wf-haflow-1018-02-impl-a-decode-r2

- 任务标识: wf-haflow-1018-02-impl-a-decode-r2
- 分支名称: agent/agy/fix-wf-haflow-1018-02-impl-a-decode-r2
- 执行者: agy
- 生成日期: 2026-10-18

## 根因
1. **直接原因**：历史运行（`task.run_id != run_id`）评估路径 `herdr/quality_sources.py:_events_for_run` 仅使用 `ORDER BY timestamp ASC, id ASC LIMIT STORED_LIMIT`（2000）获取最早事件。在 ≥2001 行事件窗口场景中，晚于 2000 行窗口的权威 `verification_completed` 回执未被读入，未进入 `_verification_verdicts` 映射。
2. **连锁后果**：窗口内失败的 `tests_completed` 事件因无配对回执抵销，经 `_verification_view` 兜底投影为 `test_failed`（`execution_trace.py`），被质量评估层收集为 `test_anomaly`，导致最终 `process_quality` 被误判为 `improvement_opportunity`。
3. **架构根因**：当前运行路径此前已在窗口满时通过 `_receipts_for_scope` 补读回执（PR #230），但历史运行路径遗漏了同等处理（Issue #234 P2-1）。

## 防复发
1. **精确补读**：在 `herdr/quality_sources.py:_events_for_run` 中，当 `len(rows) >= STORED_LIMIT`（窗口满）时，复用 `_receipts_for_scope` 针对严格等值 `run_id = ?` 独立补读 `verification_completed` 回执，按行 `id` 去重并入 `rows`。
2. **防跨 Run 泄漏**：历史补读坚持严格等值 `run_id = ?`，杜绝 runless（`NULL` 或空串）或其它 run 的回执泄漏抵销本 run 失败（R4）。
3. **饱和精准对齐（H1 闭环）**：若补读回执数亦达 `STORED_LIMIT` 饱和，针对窗口内仍未配对的 `tests_completed` 的 `evidence_id` 执行有界针对性精准补读（`json_extract` 匹配），确保窗口内失败能够与真正对应的回执配对；对于真正无回执或回执未通过的独立失败，保留 `test_failed` / `test_anomaly` 异常信号，不伪装成正常（H3）。
4. **旁路与安全**：全查询开启 `PRAGMA query_only=ON;`，异常捕获 `sqlite3.Error` / `FileNotFoundError` 统一降级为 `trajectory_unavailable`，绝不上抛异常。
5. **自动化测试防护**：在 `tests/test_quality_assessment.py` 中新增 7 项专项回归：
   - 2001 行历史窗口回执抵销测试（AC-B1）；
   - 2000 行（满）与 1999 行（未满）精确阈值边界测试（AC-B2）；
   - 跨 run / runless 隔离防泄漏测试（AC-B3）；
   - 多回执 passed=True 与 passed=False 配对测试（AC-B4）；
   - 不匹配 evidence_id 真实异常保留测试（AC-B5）；
   - 补读饱和场景下的针对性精准补读测试（AC-B6）；
   - SQLite 异常优雅降级为 `trajectory_unavailable` 测试（AC-B7）。

## 验证与回归
- **复现命令**：
  ```bash
  python3 bin/herdr-quality assess --task-id historical-2001 --run-id old-run --db-path /Users/user/.herdr-controller/clones/wf-haflow-1018-02-test-auto/docs/test-results/wf-haflow-1018-02/state.db
  ```
- **修复前误报证据（来自上一轮 test 门禁 cli-historical-2001.json）**：
  ```json
  {
    "correctness": "success",
    "problem_codes": [
      "historical_run_trace_limited",
      "test_anomaly",
      "trace_truncated"
    ],
    "process_quality": "improvement_opportunity",
    "test_anomalies": [
      {
        "code": "test_failed",
        "evidence_ref": "trace:trajectory:trajectory:4002"
      }
    ],
    "trace_completeness": "unknown"
  }
  ```
- **修复后正确输出（实测落盘）**：
  ```json
  {
    "assessor_version": "1",
    "blocked_count": 0,
    "corrective_human_count": 0,
    "correctness": "success",
    "evidence": [
      {
        "kind": "outcome",
        "ref": "outcome_9d7d0247e9011888a08f038d01ff643971fc84f5f3e414420ce6f0f5462e0f5e"
      }
    ],
    "human_intervention_count": 0,
    "outcome_id": "outcome_9d7d0247e9011888a08f038d01ff643971fc84f5f3e414420ce6f0f5462e0f5e",
    "problem_codes": [
      "historical_run_trace_limited",
      "trace_truncated"
    ],
    "process_quality": "unknown",
    "rework_count": 0,
    "run_id": "old-run",
    "task_id": "historical-2001",
    "test_anomalies": [],
    "trace_completeness": "unknown",
    "workflow_decision_count": 0
  }
  ```
- **单测验证**：`python3 -m pytest -q tests/test_quality_assessment.py tests/test_execution_trace.py`（97 passed, 11 subtests passed in 2.92s）。
- **相关子系统回归**：`python3 -m pytest -q tests/test_execution_trace.py tests/test_quality_assessment.py tests/test_execution_trace_api.py tests/test_console_execution_trace.py`（120 passed, 11 subtests passed in 4.58s）。
- **代码质量与语法**：`ruff check` 无新增错误，`python3 -m compileall -q herdr services bin tests` 零错误，`git diff --check` 零告警。
- **工位内循环自检**：`herdr-loop eval` 评分 100.0，`[HERDR_LOOP_CONVERGED] True`。

