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

## 首派完成后的替代派发生命周期

首派resolved是历史登记证据，不能充当后续补派的准入锁。当前代次存在合法superseded且replacement_pending未被明确取消的谱系头时，沿用lineage_redispatch_candidates，建立独立派发记录；前序Task/Run集合纳入identity，原resolved不重置。扫描和协调器均查询当前节点最新记录，旧队列不能认领新记录。替代launch intent及Task必须携带本记录的 --supersedes，全部前序任务均有绑定的替代Task后才resolved。新记录仍保持固定期限、持久核验、unknown不重发和人工待办。

发送和launch前再次核验前序Task/Run及待补派资格；部分前序撤销时未发送计划终止，下一轮为剩余合法谱系建立新责任，prior_operation_id关联旧计划，避免取消后恢复撞旧终态。已登记supersedes身份不可擦除或重绑定。

## 存量、模式与显式替代关系

首节点责任按节点任务发现；其他起点已有Task不阻止空节点建立待办。存量当前代次superseded且仍需替代的任务，即使没有首派历史，也建立第一条绑定前序Task/Run的派发责任。关闭总指挥接单时，只结束尚未发送的待办，释放旧阶段锁并移交直接调度；已发送未知交付继续核验，不借开关重发。重新启用时追加新epoch并保留退役记录。替代谱系合并持久supersedes关系与旧-rN命名兼容，登记后的替代Task不依赖反向superseded_by落盘即可阻止重复补派。

模式移交仅适用于pending/running且未发送；人工hold及waiting_human不因切换模式失效。谱系同时处理显式、反向及传统隐式后继顺序，改名后再进入旧-rN命名仍选择最新后继，显式关系优先于有冲突的名称推断。

谱系选择不依赖数据库返回顺序：冲突环只舍弃进入持久后继的推断边；同rank时优先持久关系深度，再登记时间，最后Task ID稳定决胜。Task ID不证明时间先后；纯持久闭环继续拒绝补派。未发送首派遇到已登记的当前执行库存时，仅库存完整且身份明确才移交责任；部分库存和人工hold保留原义务。


## FACT：下游节点派发也保留持久责任（隔离源码）

默认接单首节点和依赖已满足的 Agent 节点复用同库 node_dispatch。下游身份绑定候选 episode/SHA、固定配置及上游 Task/Run 或当前 reuse 证据。排队租约是恢复权威，JSON queued/notified 不得永久吞掉下游派发。Controller 直接 launch 和总指挥 prompt 均带 operation ID，真实 CLI intent/Task 登记核验当前依赖、候选及执行身份。

外部返回成功但未登记 Task 只进入 awaiting_result。直接派发的全部计划 Task 都须登记；部分角色缺席不得宣称推进。发送后未知交付不转第二条传输；只有同 operation 的完整 resources_absent 回执且无登记任务，才按既有三次预算重试。历史 notified 空节点迁移为有期限的未知交付核验。明确取消补派的节点有人工范围确认责任；hold/retry 不能擦除当前取消事实或旧未结案交付。目标已由当前 reuse 满足时不创建空责任，未发送责任转移给 reuse；已发送未知仍保留核验。

Evidence:
- `herdr/node_dispatch.py#dependencies_ready`
- `herdr/node_dispatch.py#result`
- `herdr/node_dispatch_store.py#direct_finished`
- `herdr/node_dispatch_store.py#validate_launch`
- `services/herdr-controller.py#check_workflow_stage_advance`
- `services/herdr-controller.py#_handle_coordinator_item`
- `tests/test_downstream_dispatch_contract.py`

UNKNOWN：此节描述隔离工作树源码。原工作流恢复、真实 Agent 接单、服务部署和业务测试审核结论未在本轮执行。

## 用户可操作的派发恢复（隔离源码）

主流程图、节点详情、底栏与 Controller 都投影同一恢复责任；读取失败显示“状态未确认”，不能显示“无卡点”。节点详情或 Controller 的恢复卡说明当前候选、卡点原因、核查结果和可用动作。

- **恢复验收范围并重新派发**：检查旧工位与任务列表后，填写处理人、依据并确认无旧任务执行。旧取消头必须有 Run；历史 execution 缺失时额外展示 Task/Run，要求确认归属。提交绑定精确 Task/Run/version，旧责任退休，新责任待 Controller 派发。
- **核查启动现场**：有未结案启动记录时先核查。复用既有资源 inventory，只结束证明缺席的 intent；核查期间版本或身份变化拒绝，intent 回执、待办结果和审计在最终事务一起提交；存在、归属不明、超时保持阻止重发，卡片显示分项原因。
- **确认旧任务未运行并重新派发**：未登记 Task 的未知交付，经人工检查、说明依据后创建新授权。既有任务、未结束 intent、旧未知责任、候选变化或上游身份缺失均拒绝。
- **确认旧启动仅停留在启动提示并重新派发**：对 stale generation 的动态 Pane，操作员须绑定 transcript SHA-256 并确认仅有启动提示；系统重新核验 Pane/terminal/cwd、Agent 缺席、私有 clone tag、Task 与 intent 身份后关闭 Pane、归档 clone 并落 `resources_absent` 回执。完成外部清理后若 dispatch CAS 失败，重试先验证归档回执及 Pane 缺席，再继续 CAS；不得重复关闭或归档。此动作只处理经确认的旧启动，不代表真实工作流已验收。

恢复成功提示只证明“已建立恢复待办”。Controller 仍沿 claim→send→launch intent→Task 登记核验；只有当前责任有真实登记证据，界面才显示“派发已确认”。人工新授权后，旧 operation 和无绑定迟到启动/登记不能接管新责任。通用核验及暂缓仍保留。未知现场并非可安全强制重发，界面明确保留卡点。

Evidence: `herdr/dispatch_recovery.py`、`herdr/dispatch_recovery.py#abandon_partial_launch`、`herdr/task_resources.py#begin_launch_intent`、`herdr/task_resources.py#retire_partial_launch`、`herdr/state_db.py#save_task`、`tests/test_dispatch_recovery_ui.py#test_audited_partial_launch_retirement_unblocks_only_current_dispatch`、`tests/test_launch_reconcile_cli.py#test_partial_launch_retirement_recovers_after_archive_before_receipt`。
本节描述实现契约，不替代部署和原工作流业务验收。

## 未提交工程产物的恢复责任

`FACT` delivery_incomplete携带真实delivery_checked拒绝回执时产生delivery类型责任。此责任不同于冻结候选修复，不因candidate_sha为空默认candidate_unknown。范围冲突和未知身份仍等待人工；auto_rework授权且原Task/Run未提交时，Controller复用原工位及receipt-v1安排有限返工。

`FACT` 责任记录真实source_runs、request及新完成epoch；结果只有原Task真实集成后才resolved。没有新增候选不妨碍工程交付返工，但测试派发仍受原冻结门禁。旧恢复身份在未包含交付字段时保持原哈希，升级不使既有责任失效。

Evidence:
- `herdr/workflow_progress.py#assess_workflow`
- `herdr/recovery_store.py#_validate_step`
- `herdr/delivery_rework.py#execute_delivery_recovery`
- `herdr/workflow_recovery.py#result_status`
- `tests/test_delivery_rework.py#test_existing_recovery_identities_survive_delivery_extension`
