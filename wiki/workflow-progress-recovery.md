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

## UNKNOWN：现场业务验收与发布

本页描述工作树实现，不能证明已部署或原 NexusArchive 业务测试已通过。发布需独立不可变 release、只读 shadow 对比、单执行者切换及授权后的现场复跑。源仓库 WIP 的归属与业务修复范围需由负责人确认。

相关页面：[[dag-workflow-engine]]、[[task-lifecycle]]、[[index]]。
