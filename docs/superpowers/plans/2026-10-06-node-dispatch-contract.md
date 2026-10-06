# Node Dispatch Contract Implementation Plan

Goal: 首节点投递到真实任务登记有持久责任、固定期限和恢复出口。
Architecture: 纯 node_dispatch 判定 + 使用 recovery 表的 node_dispatch_store 事务；Controller 是唯一发送者，CLI 绑定 intent 和 Task；既有恢复按 kind 分流。
Tech Stack: Python 标准库、SQLite、pytest。
Global Constraints: working_tree；不调用真实 Agent/模型，不修改生产状态；unknown 不重发；不建平行状态库；代次/config CAS。

- [x] Task 1 RED: tests/test_node_dispatch_contract.py，临时库真实 Controller，模拟外部 rc0 未 launch，四轮和到期核验。`pytest -q tests/test_node_dispatch_contract.py` 保存失败日志。
- [x] Task 2 GREEN: node_dispatch.py 定义 identity/eligibility/registration evidence；node_dispatch_store.py 同库注册与 claim/started/核验。recovery_store.ensure_obligations 保留 node kind，claim/expire/verify 分流；workflow_recovery.drive_recovery 不运行返工 executor。测试持久 deadline、独立连接竞争、重启、跨代次。
- [x] Task 3 CLI: begin_launch_intent 新可选 operation ID，与真实 launch event 同事务校验；state_db.save_task 首次登记校验 intent/Run/operation；bin/herdr-task 参数及成功/失败记录携带绑定。生产 CLI 测试替换 Pane/Worker 物理资源，验证真实 intent 与 DB 读取。
- [x] Task 4 Controller: sweep 在 pane lookup 前登记/核验；首节点发送前 claim+started；消息透传 operation ID；发送结果只有真实登记可 advanced，其他等待且不清闩重发；缺 pane/忙碌有界延期；异常由持久 lease 核验。
- [x] Task 5 Projection: zero Task Workflow 从 operation 读取等待/负责人/期限，Console 待办 label 分流。legacy notified 为未知交付核验；paused/closed/变代不得发送。
- [x] Task 6 清理/验证/审查: ai-slop-cleaner Mode B 限定变更文件，合并重复分支；全量 pytest/compileall/CLI/diff-check。独立 Standards/Spec 审查和 Google code review，最多三轮。落盘 verify/review，知识/Wiki 与真实实现同步。

每项先写能证伪错误行为的用例、确认 RED、最小实现、相关 GREEN。未写源码前自查：scope 覆盖所有批准验收，首节点候选未知不能复用 fix-loop gate；旧 JSON 闩只是兼容投影；新 store 是该生命周期事务边界，不是泛化框架。


## 人工升级检查点

核心派发链已实现；Task 3 身份损坏兼容边界和 Task 6 最终全量/审查尚未完成。累计三轮 NEEDS_FIXES 后按 Unified Dev Flow 停止实现循环。详见 .omc/escalation-node-dispatch-20261006.md。必须修复残缺对象丢失 Run/execution/intent 的实际反例，并重新清理、验证、审查后才能完成。本任务没有提交、推送、合并或部署。


## 人工批准后的继续实施

用户明确“继续”。已新增8项真实失败回归：部分绑定允许原样修复、缺任一字段拒绝擦除、保留非空冲突拒绝自证。修正及清理后 node_dispatch_contract/state_db_v2 共68 passed。原升级记录保留，当前重新进入S5/S6；最终全量和独立结论仍以新证据为准。

## 最终验收

继续后的残缺身份与未登记Workflow兼容缺陷已修正并经独立复核。最终当前源码 `pytest -q`：3632 passed、157 subtests passed，554.13s，exit 0；compileall、CLI/AST与diff检查exit 0。历史失败及升级记录保留。交付为隔离working_tree，无提交、推送、部署或生产服务操作。
