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

## 首派完成后的替代派发生命周期

首派resolved是历史登记证据，不能充当后续补派的准入锁。当前代次存在合法superseded且replacement_pending未被明确取消的谱系头时，沿用lineage_redispatch_candidates，建立独立派发记录；前序Task/Run集合纳入identity，原resolved不重置。扫描和协调器均查询当前节点最新记录，旧队列不能认领新记录。替代launch intent及Task必须携带本记录的 --supersedes，全部前序任务均有绑定的替代Task后才resolved。新记录仍保持固定期限、持久核验、unknown不重发和人工待办。

## P1补派修正验收（bcd5c5b后续）

当前补派/DB专项83 passed；独立Spec/Standards通过。完整回归实际结果3644 passed、2 failed、157 subtests passed；两项既有短预算用例未改源码或断言，随后主控及两位独立评审原样复验均2 passed。npm反例在未改bcd5c5b隔离基线同样出现；Git首次仅能确定清单未知且安全保留，不能断言具体超时根因。保留首次失败，不改写为单次全量PASS。交付为更新同一PR，不合并/部署，真实Agent全流程未验证。

## 存量、模式与显式替代关系

首节点责任按节点任务发现；其他起点已有Task不阻止空节点建立待办。存量当前代次superseded且仍需替代的任务，即使没有首派历史，也建立第一条绑定前序Task/Run的派发责任。关闭总指挥接单时，只结束尚未发送的待办，释放旧阶段锁并移交直接调度；已发送未知交付继续核验，不借开关重发。重新启用时追加新epoch并保留退役记录。替代谱系合并持久supersedes关系与旧-rN命名兼容，登记后的替代Task不依赖反向superseded_by落盘即可阻止重复补派。

模式移交仅适用于pending/running且未发送；人工hold及waiting_human不因切换模式失效。谱系同时处理显式、反向及传统隐式后继顺序，改名后再进入旧-rN命名仍选择最新后继，显式关系优先于有冲突的名称推断。

谱系选择不依赖数据库返回顺序：冲突环只舍弃进入持久后继的推断边；同rank时优先持久关系深度，再登记时间，最后Task ID稳定决胜。Task ID不证明时间先后；纯持久闭环继续拒绝补派。未发送首派遇到已登记的当前执行库存时，仅库存完整且身份明确才移交责任；部分库存和人工hold保留原义务。

## 授权恢复后的最终验证

第三轮排列缺陷升级后，用户授权继续。修正弱边剪除与同权叶子决胜，保留所有升级及历史失败事实。当前全量pytest -q：3667 passed、157 subtests passed，567.04s，exit 0；六文件专项273 passed、44 subtests passed，33.47s；compileall、两CLI AST、diff检查exit 0。独立Spec169 passed/23subtests，Standards209 passed/35subtests，两者19200排列一致且临时SQLite反例正确。交付更新PR #156，不合并部署。当前main新增#157/#158，完整结果绑定PR分支，未宣称合并后或真实Agent全流程通过。

## main冲突收口

推送后GitHub报告CONFLICTING，因此将origin/main9ac1d75合入当前分支；冲突仅lessons/wiki日志追加历史，完整保留两侧内容。整合全量3670 passed、1 failed、157subtests，559.10s，exit1；唯一Console间距失败在git archive main原样复现，与本轮派发改动无关，不改断言。独立集成Spec204pass/73sub、Standards244pass/108sub，均LGTM。另披露#158既有task_id排序note造成任意改名指纹变化的独立缺陷，未扩修。交付本轮修复和可合并PR分支，不宣称整个系统全绿或生产通过。
