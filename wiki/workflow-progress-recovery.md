# Workflow 进展与持久恢复

## FACT：恢复独立于正向 DAG

阻塞事实来自已有 Task/Workflow/Candidate 记录。`cleaned + blocked` 仍为阻塞；正式 `superseded_by` 排除历史任务。Controller 快速 sweep 补登记，后台独立恢复先于 continuation；Console 与手工推进复用同一个纯评估。缺席的并行 review 不会使 test 的恢复义务不可见。

Evidence:
- `herdr/workflow_progress.py#assess_workflow`
- `herdr/state_db.py#save_task`
- `services/herdr-controller.py#check_workflow_stage_advance`
- `services/herdr-controller.py#schedule_workflow_continuations`
- `tests/test_recovery_entrypoints.py#test_controller_records_failure_even_without_coordinator_or_ready_join`

## FACT：同库义务、租约与未知交付

`workflow_recovery_operations` 在原 SQLite 数据库中保存语义身份、版本、执行租约、步骤回执与下一次检查时间。阻塞事实和义务同事务；跨连接 CAS 防止同一义务并发执行。执行前重查当前代次、候选和受影响范围。副作用开始后失联进入人工核验，不能因超时盲目重发。

人工决策需 operation ID、当前 version、operator、reason。`retry` 只用于已知安全前置条件；`hold` 有到期时间；`verify` 核对既有持久交付，不重新派发未知副作用。未知身份、源仓库 WIP 与不完整回执保留待处理义务。

Evidence:
- `herdr/recovery_store.py#claim_operation`
- `herdr/recovery_store.py#record_step`
- `herdr/recovery_store.py#decide_operation`
- `tests/test_recovery_store.py`

## FACT：committed 修复保留历史

committed 前驱保持原状态和 SHA。新的修复轮次从失败 SHA 派生独立分支。提交可能仅存在旧 Task clone；`choose_recovery_source` 优先项目仓库，再检查登记的前驱 clone 是否包含精确 SHA 且干净。Worker 的 CoW 源路径与项目集成根分别保留，候选不可读或源码 WIP 转人工；仅在注册 intent 和当前 Run 的 INITIAL 交付回执确认后原子连接谱系。快速完成的后继仍可验证同一回执。每个原受影响目标均须有一对一持久修复映射，并绑定 source/target Run。rework 还绑定派发前持久化的本轮 request ID、completion epoch 与身份文件；receipt-v1 重新核对本轮实际交付事件。完整确认修复交付后才作废旧门禁；最终结案要求当前任务 Run、真实交付、新候选以及全部配置验收节点的新 PASS。

Evidence:
- `herdr/recovery_successor.py#link_committed_successor`
- `herdr/workflow_recovery.py#successor_launch_command`
- `herdr/workflow_recovery.py#result_status`
- `services/herdr-controller.py#execute_workflow_recovery`
- `tests/test_recovery_successor.py`
- `tests/test_workflow_recovery.py`

## FACT：快照、发布与已送达动作的闭环

SQLite 的统一快照同时提供 Workflow、固定配置和 Task 身份；候选只读取当前代次、`critical-path-scheduler` 的明确冻结事件。未知 execution_id / Run、配置缺失、测试预算不足必须在派发前拒绝。历史身份迁移只使用同 Workflow/Task/Run 的 INITIAL 与当前 epoch 交付回执；apply 和 rollback 使用原行与事件水位 CAS，审计仅保存迁移字段的逆操作，禁止复制任意 Task 内容。外部 Git/Artifact 校验在写事务前完成，事务内重查身份和版本。

恢复先消费已登记 rework request、已送达 successor，或当前受影响对象自身的在途 successor。Run、epoch、request、双向 lineage 与一对一映射缺一即 unknown；未知交付不得补发。只有当前候选的有效失败参与返工，已发送动作继续剩余步骤。Task 使用自己的分支和 `refs/herdr/tasks/<task_id>`；onto 只定义基线，rebase 后实际 HEAD 必须先持久化，再输出集成引用。已集成重试仍清除升级标记。

发布事件携带生产者固定的候选 episode，迟到发布不能覆盖后来候选；清理和报告更新不产生发布事件。业务验收回执独立于通用评分，绑定当前候选、Run、epoch、全部配置 AC-N 和校验后的 checkpoint。预算增加须显式有界授权、配置哈希 CAS 与审计，不能重置历史计数。

Evidence:
- `herdr/recovery_store.py#read_snapshot`
- `herdr/workflow_repair_migration.py#apply`
- `herdr/workflow_recovery.py#existing_delivery_details`
- `herdr/scheduler_facts.py#publish_integrated_candidate`
- `herdr/task_checkpoint.py#record_business_acceptance`
- `tests/test_workflow_repair_contracts.py`

## FACT：业务回执同时授权前进

恢复结案校验不是全部前进入口。生产复跑曾在 review 最新业务 blocked 时，仅靠 stage pass 派发 wrapup（事件26838/26846）。统一 `task_checkpoint.business_gate_blockers` 在 Controller join/sweep/direct、CLI launch intent 前、kernel step、PR publication/merge、正常 close claim 前拒绝缺失或失效的 receipt-v1 业务证明。完整当前候选、execution、Run/epoch、AC覆盖、最新回执、artifact校验后重新读取版本/episode；unknown 只等待，不产生新的实现返工。单一依赖也需校验，Controller 不信任空/旧 Task 投影。没有业务 completion 协议的 legacy 任务继续旧阶段策略，但不能追认业务 PASS。

Task 容量也必须读取固定 config；生产曾显示 SQL review预算6，而 launch 仍读旧 workflow.json 的4，导致已批准预算不能使用。固定配置优先于可变文件，回归使用损坏旧文件证明不会回退读取。

## UNKNOWN：现场业务验收与发布

本页描述工作树实现，不能证明已部署或原 NexusArchive 业务测试已通过。发布需独立不可变 release、只读 shadow 对比、单执行者切换及授权后的现场复跑。源仓库 WIP 的归属与业务修复范围需由负责人确认。

相关页面：[[dag-workflow-engine]]、[[task-lifecycle]]、[[index]]。

## FACT：首节点派发到任务登记的持久责任（本地实现）

默认总指挥接单首节点在 SQLite `workflow_recovery_operations` 登记 `node_dispatch`。Workflow 零 Task 时也能读取待办。队列发送前预占同库租约，队列项携带 operation/owner；内存队列丢失后通过租约恢复，不让 JSON queued 永久压住派发。配置或代次变化拒绝旧队列，缺少当前 obligation 不回落到无跟踪发送。

外部发送前持久化 started 与固定 900 秒任务登记截止；Agent 命令成功只记录等待登记，不证明推进。CLI `launch --dispatch-operation-id` 把当前代次、Run 与 operation 绑定到真实 intent 和 Task，写事务拒绝伪造/跨代次登记。显式 required_task_ids 必须齐全；没有清单时只证明首个有效 Task 登记，不证明拆分完整、Worker 开始工作或工作流交付完成。同一已落实派发在原截止内可登记后续并行 Task，不能再发送该派发。

超时/非零/发送后失联先核验，不盲目重发。没有任务登记证据到期转 waiting_human；缺 Pane/忙碌等发送前问题最多三次，有期限暂缓不被普通扫描提前结束。历史 notified 空节点以明确 migration origin 登记未知交付，首次迁移建立核验期限，不虚构历史发送时间。所有终态历史在扫描前过滤，防止容量上限挤掉当前义务。

Evidence:
- `herdr/node_dispatch.py#payload/result/wait_projection`
- `herdr/node_dispatch_store.py#reconcile_workflow/claim/start/validate_task_registration`
- `services/herdr-controller.py#reconcile_node_dispatches/check_workflow_stage_advance/_handle_coordinator_item`
- `herdr/task_resources.py#begin_launch_intent`
- `tests/test_node_dispatch_contract.py`

UNKNOWN：本节是本地工作树实现。真实 Agent 接单、生产部署和从新建到交付的无人干预完成尚未验证。
