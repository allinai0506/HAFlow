# 执行质量评估

质量评估回答一次 Agent 执行好不好。它不改写任务结果，也不发布经验规则。

## 读什么

一次评估只看一个 `task_id` 加一个 `run_id`。

1. `agent_execution_outcomes` 里已经结算的 Outcome。业务是否合格、返工次数、阻塞次数、人工介入次数都直接复制这行，不从状态历史再算一遍。
2. 现有执行轨迹投影：账本事件，以及 Codex、Claude Code、OpenCode 能归属到当前运行的原生动作。
3. 同一任务上、载荷里写明本次 `run_id` 的人工插话，以及同一 `task_id` 的 `RETRY` intervention。`herdr-task steer` 会把任务当时的 `run_id` 写进新插话。没有这份身份的旧插话仍不记成纠错。

读取使用 `get_readonly_db_connection` 和 `read_execution_outcome`。不建库，不迁移，不调用会打开可写连接的 `get_execution_outcome`。

## 两个结论

`correctness` 是业务结果：

- Outcome 的 `qualified_success` 为真：`success`
- 为假：`failure`
- 没有 Outcome，或合格结论不是布尔值：`unknown`

`process_quality` 是过程：

- `acceptable`：业务成功，轨迹完整，没有已确认的返工、阻塞、测试异常、重试或纠错
- `improvement_opportunity`：业务失败，或上面任一信号有证据
- `unknown`：还不能判断过程。未知不是失败

工作流要求的人工决策不进入过程问题。判定只认插话载荷里的 `decision_id` 或 `kind=decision`，并且 `run_id` 必须对得上。没有运行身份的人工次数保持 `human_intervention_unclassified`，不当成纠错。

## 不推断的事

没有 Outcome，不能用轨迹里的测试失败补成业务失败。原生测试失败也不是业务验收回执：没有 Outcome、账本里又没有验收事件时，仍记 `verification_missing`。没有退出码的测试命令记 `exit_code_unknown`，不记成测试异常。原生会话对不上记 `native_unattributed`。非 Codex、Claude Code、OpenCode 的任务没有原生会话可归属，不因此记为未归属。

显式重试来自原生动作的 `retry_of`、轨迹里的 `retry` 事件，或本次运行的 `RETRY` intervention。非显式重试只在同一条完整命令先失败、随后又执行，且中间没有显式重试链接时成立。缺时间戳的动作保持读入顺序，不把它排到时间零点。命令文本不同不算。返工次数仍以 Outcome 为准，重试不会改写它。人工插话和 intervention 只读本次 `run_id`，超过 200 条记 `human_signals_truncated`，过程不能判成 acceptable。

其他运行的事件直接丢弃，并记下 `foreign_run_dropped`。这个编码只说明隔离生效，不表示这次运行本身有问题。任务当前运行和被评估的运行不同时，只读该运行自己的账本事件，不读取当前工作区里的原生会话，也不把没有 `run_id` 的旧行算进来。同一 `evidence_id` 已有验收回执时，配对的 `tests_completed` 不再另记一条测试异常。

评估器异常时返回 `assessment_unavailable`。结算、经验采集和 Controller 不调用它。

## 命令

```bash
bin/herdr-quality assess --task-id <task_id> --run-id <run_id> [--db-path <state.db>]
```

标准输出是一份 JSON。退出码 0 表示报告已经打印，不表示业务成功。报告里的 `problem_codes` 才是判断。

Evidence:
- `herdr/quality_assessment.py#assess_quality`
- `herdr/quality_sources.py#load_quality_inputs`
- `herdr/state_db.py#read_execution_outcome`
- `bin/herdr-quality`
- `tests/test_quality_assessment.py`
