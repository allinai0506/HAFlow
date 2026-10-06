# 节点派发到任务登记的持久闭环设计

状态：用户批准继续后实施完成；当前源码全量与独立审查通过，交付为隔离working_tree。基线：defa6078e58a1c3b9c031aa82d73133741a1d4c8；origin/main 为 034cf32bdfd88a694e1b9a55977ef4046a16e31a。本地额外上下文提交保留，不重置主干。

## 目标与范围

覆盖默认首节点总指挥接单：running Workflow 即使零任务，也必须有正在执行的派发义务、有期限的核验，或明确的人工决定。消息投递成功不证明任务登记，不输出 STAGE ADVANCED。沿用现有 SQLite workflow_recovery_operations、事务与版本 CAS，不建立平行数据库或泛化所有控制动作。

本轮也处理首节点派发前没有 coordinator、启动后重启、历史 notified 空节点。其他返工/新候选/业务关闭契约只做兼容回归，不重写。未授权部署、推送、合并、真实 Agent 或收费模型调用。

## 当前源码证据

- services/herdr-controller.py:mark_stage_advance_queued / mark_stage_advance_notified：JSON queued/notified 是无期限字符串。
- _handle_coordinator_item：首节点默认 intake；subprocess returncode=0 即 mark notified 和 STAGE ADVANCED。
- reconcile_stage_advance_states：上游回退或全部旧任务作废可解锁，节点从未建任务不解锁。
- herdr/workflow_continuation.py:pending_continuations：无 owned Task 直接返回。
- herdr/projection.py:detect_workflow_stalls：空任务未提供首节点派发证据。
- herdr/recovery_store.py：同库 operation、next_due_at、owner/lease、版本 CAS、started 与 waiting_human 可复用；ensure_obligations、_validate_step、settle_result 当前具有 fix-loop 假设，必须按 kind 分流，不能直接塞入新的 payload。

用户的隔离复现与 110 项通过记录是输入证据，本轮没有重跑该复现，不能写成已独立复现。

## 方案比较

1. 给 notified 加 TTL，过期清除并重发：修改小，但超时可能已经创建任务，存在重复副作用；不采用。
2. 在原 recovery 表增加 node_dispatch 类型并绑定任务登记：可复用持久义务、CLI 人工决策和展示，需明确类型分流；推荐。
3. 所有控制动作统一为新的通用 Action 引擎：范围过大，本轮不采用。

## 状态、身份与证据

在第一次发送前，事务登记 node_dispatch operation。语义键包含 workflow_id、execution_id/创建代次、reopened_at、node_id 和固定配置指纹；唯一约束保证两个 Controller/连接只能登记同一个义务。首次无候选 SHA 是正常首节点场景，不套用 fix-loop 的 candidate_unknown 判定。

业务负责人是总指挥，恢复责任属于 Controller；operation 记录 owner 的执行租约，payload 记录目标节点和逻辑负责人，不把 Pane ID 当作所有权证明。检查节点 readiness 与当前固定配置后才能 claim；外部调用前持久化 started。

状态沿用 pending → running → awaiting_result → resolved / waiting_human。人工暂缓使用 waiting + next_due_at。等待任务登记的初始预算固定为发送开始后 900 秒，核验间隔 30 秒；900 秒覆盖现有总指挥命令 600 秒等待预算。截止时间从持久发送事实计算，轮询、Controller 重启和普通日志不得刷新它。

CLI launch 新增可选 --dispatch-operation-id；首节点总指挥提示词携带此参数。实际生产 launch intent / Task 登记写入 operation ID，并在写事务中校验该 operation 的 Workflow 代次、node、配置和未终结状态。迟到的旧代次请求拒绝，不能认领新 operation；兼容旧 launch 不带参数，但不得自动证明新派发已落实。

resolved 只证明派发落实，不证明工作流完成或 Worker 已 working。至少存在一个属于当前 operation 的真实有效 Task/launch intent，并包含有效 Task ID 与 Run/Workflow 身份；失败 Task 交接给既有失败恢复路径。若有 required_task_ids，应核对全部要求，避免部分登记被当作全部落实。没有显式任务清单的节点，本轮只证明首个有效任务登记；任务拆分完整性明确不在本轮证明范围。

## 超时和恢复

- rc=0 无任务：awaiting_result，日志 STAGE DISPATCH AWAITING_TASK；持久 next_due_at 与 deadline 继续有效。
- 超时、非零返回、崩溃发生在 started 之后：交付未知。定期重新读取 SQLite 任务和 launch intent；有同 operation 证据才落实，截止后 waiting_human。不得把非零返回当作安全重发证据。
- 调用前已知失败：没有 started，可按现有最多三次策略恢复；无 coordinator 应显示可定位原因与下一次检查，而不是单纯 return。耗尽转 waiting_human。
- Controller 重启：从 SQLite 恢复义务，不依赖内存队列。started 且租约失效进入核验/人工路径，不重新发送。
- 历史 notified、零任务：为当前可执行首节点登记 migration-origin 的未知交付义务，保留原 notified 的防重复作用；不虚构原发送时间。给首次迁移核验明确截止并记录迁移事实。历史任务不能自动绑定此次新义务。
- 工作流 paused/closed、代次改变、上游回退：禁止副作用；未发送义务可以 superseded，已开始义务保留未知交付说明并避免跨代次落实。
- waiting_human 说明需要决定什么：核对是否已有外部派发、补足需求或批准新的派发。复用 recovery-status / recovery-decide 的 version、operator、reason；verify 只核验，retry 不绕过 started 的未知交付保护。

## 文件范围与真实链

- NEW: herdr/node_dispatch.py：纯身份、登记证据和截止判定；不做 subprocess 或 DB I/O。
- herdr/recovery_store.py：node_dispatch 注册、claim、核验、结案的事务分支；generic reconcile 不把这种义务当作消失的 fix-loop 自动 supersede。
- herdr/workflow_recovery.py：drive_recovery 按 kind 分流，避免把 node_dispatch 送进 successor/rework。
- services/herdr-controller.py：首节点 sweep 在 Pane 检查前登记；发送前 claim/started；发送后核验；重启和后台 bounded sweep 恢复。旧 stage latch 仅兼容，SQLite 为新义务权威。
- bin/herdr-task：参数、真实 launch intent 登记绑定与可核查输出。
- herdr/projection.py 及当前 recovery Console 展示入口：从 Workflow 读取义务，零任务也报告等待任务/截止/人工关注。展示不得依靠 Task 推导 Workflow 存在。
- NEW: tests/test_node_dispatch_contract.py；现有 tests/test_stage_advance_and_supersede.py、tests/test_recovery_store.py、tests/test_recovery_entrypoints.py：回归与兼容。
- wiki/workflow-progress-recovery.md、wiki/log.md、docs/lessons/lessons-learned.md：实现和验证完成后更新；设计阶段不写已交付事实。

实际入口：Controller Workflow sweep → 同库义务注册 → coordinator queue → 发送前持久 claim → Agent 调用 → herdr-task launch intent/Task 绑定 → 后台核验 → 同库结案 → recovery-status/Console 读取。

## 验收矩阵与命令

用临时 HOME/SQLite/目录、受控时钟和外部 Agent 替身，保留真实 Controller、CLI、launch intent 和持久化；禁止启动真实 Agent、服务或收费模型。

1. rc=0 未 launch：当轮不是 advanced；重复四轮仍只有一个未完成义务；到期有人工决定及原因。
2. 正常 launch：真实 CLI 将 operation ID 写入持久 intent/Task；核验成功，重复扫描不重复发送。
3. 无关 Workflow、同节点旧 Run、旧 operation、部分 required_task_ids：不能满足派发义务。
4. 超时但 Task 已登记、稍后登记、始终未登记：分别正确核验/等待/到期人工；都不盲目重发。
5. 崩溃窗口：登记后发送前、started 后、Task 登记后回执前、结案后响应前；重启从库恢复。
6. 两个独立 SQLite 连接用 Barrier/Event 控制并发 claim，只有一个外部调用；跨进程重启不依赖内存锁。
7. 无 coordinator、startup_ready=false、paused/closed、配置或代次变化、旧 notified：保留准确有界后续动作，不跨代次认领。
8. projection 对零任务 Workflow 输出等待内容/负责人/期限；人工决定有 CAS，hold 到期可见。
9. #152–154 恢复、业务验收和直接派发专项仍通过，不能改变业务 blocked 的门禁含义。

RED：pytest -q tests/test_node_dispatch_contract.py
专项：pytest -q tests/test_node_dispatch_contract.py tests/test_stage_advance_and_supersede.py tests/test_recovery_store.py tests/test_recovery_entrypoints.py tests/test_workflow_recovery.py tests/test_workflow_repair_contracts.py
全量：pytest -q
语法：python3 -m compileall -q herdr services bin tests；bin/herdr-task 另用 ast.parse 检查并隔离 HOME 运行 --help。
差异：git diff --check

实现前保留 RED 输出，S4 完成后执行 ai-slop-cleaner Mode B；S5 对清理后的源码执行上述命令。S6 独立审查覆盖规格、真实链、并发与崩溃矩阵并落盘 review；不足不得写 MERGE_READY。health 工具可用性尚未核实，不自行豁免。

## 授权与待确认

交付上限 working_tree，主控为本会话，spec_backend=unified，proof_mode=red-first，assurance=adversarial。S0/recon 已进行；设计批准后补齐 Entry Gate、实施计划和正式基线，进入 S4–S6。

本设计采用“未知交付核验，截止转人工”的默认策略。它可以消除无人负责的 running，但不承诺所有业务必然无人干预完成。要自动重发，需额外的外部执行否定证据与幂等契约；本轮不凭没有 Task 推断未执行。

## 本轮只读基线记录

在上述 CoW 沙盒、defa607 基线执行：
`pytest -q tests/test_stage_advance_and_supersede.py tests/test_recovery_store.py tests/test_recovery_entrypoints.py`
退出码 0：64 passed、9 subtests passed，3.19 秒。
`git diff --check` 退出码 0。仅新增本设计文档，未修改业务源码、测试或生产状态。未执行全量测试、停滞复现、真实 Agent 和部署验收，未宣称修复完成。


## 实施细化（授权范围内）

新事务函数放在 herdr/node_dispatch_store.py，复用现有 recovery 表与事务入口，避免把首节点生命周期混进返工证明。队列入队时即领取持久租约，携带 operation/owner；租约过期恢复丢失队列。resolved 允许原发送截止内补齐同一派发的并行 Task，但始终禁止再次发送。tasks 历史必须先按 execution 过滤；未完成义务先于历史容量限制被读取。当前基线已对齐 origin/main 91f084c；与原 defa607 的源码内容一致。
