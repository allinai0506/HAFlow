# Controller 服务职责

`services/herdr-controller.py` 是进程入口和旧接口兼容门面。它保留环境配置、可选依赖导入、进程内队列/锁/线程池，以及启动顺序。实现模块不导入入口、不创建另一份运行状态。

| 修改目标 | 实现入口 |
| --- | --- |
| 任务/Workflow 权威读取、状态转换与完成样本 | `persistence.py`、`task_state.py` |
| 阻塞 SLA、attention 和升级回执 | `attention.py`、`blocked_actions.py`、`blocked_claims.py`、`blocked_records.py` |
| 节点策略、阶段闩和就绪推进 | `node_policy.py`、`stage_state.py`、`advancement.py` |
| 候选冻结、复验、join gate 和直接派发 | `candidate.py`、`reverification.py`、`join_gates.py`、`direct_dispatch.py` |
| 普通/已合并返工、选择性归因与补投 | `fix_loop.py`、`fix_invalidation.py`、`merged_fix_loop.py`、`selective_plan.py`、`selective_facts.py`、`fix_delivery.py` |
| 协调器队列和事件分派 | `coordinator_queue.py`、`coordinator_events.py` |
| 阶段投递与普通 Task event | `stage_delivery.py`、`task_events.py` |
| 协调器传输、文档、提示词与返工通知 | `coordinator_transport.py`、`coordinator_prompts.py`、`fix_notification.py` |
| 自动验收、verdict 证据与落盘 | `auto_acceptance.py`、`verdict_evidence.py`、`auto_verdict.py` |
| Git 终化、重试、回执和工作流关闭 | `git_finalize.py`、`finalize_retry.py`、`finalize_support.py`、`workflow_close.py` |
| 监督动作、执行证据和中断恢复 | `supervisor_actions.py`、`supervisor_evidence.py`、`supervisor_execution.py` |
| 监督观察点、测试完成、done 与旁路压缩 | `supervisor_checkpoints.py`、`task_testing.py`、`done_events.py`、`context_compaction.py` |
| 协作消息与节点交接 | `collaboration.py`、`handoffs.py` |
| Socket、运行状态对账与基础设施恢复 | `task_listener.py`、`runtime_reconciliation.py`、`infra_recovery.py` |
| Workflow continuation 与 durable recovery | `continuation.py`、`workflow_recovery.py` |
| 常驻巡检、批量推进和唤醒守卫 | `registry.py` |

## 依赖与兼容

每个实现函数的首参数 `ports` 提供该操作的依赖；模块末尾的 `DEPENDENCIES` 精确声明这些名称。`bindings.py` 为每个操作建立受限的实时依赖视图，并以原函数名、签名和函数类型导出到旧入口。配置、锁和队列的对象身份保留；已有调用方替换入口函数或配置时，后续调用仍读取替换值。

实现也可直接调用，不要求启动 Controller。例如，持久化读取只需要已有 StateStore：

```python
from types import SimpleNamespace
from services.controller.persistence import get_task

task = get_task(SimpleNamespace(_get_store=lambda: store), task_id)
```

依赖视图用于兼容装配，不承担业务判断。DAG、状态转换、派发规划和持久化继续复用 `herdr/` 的已有能力。新逻辑应放入对应职责模块；跨职责调用同时声明依赖。`tests/test_controller_modularity.py` 检查入口规模、实现模块规模、声明依赖环、独立调用、状态隔离和旧接口。

## 顺序契约

- `coordinator_queue._handle_coordinator_item` 按 continuation → fix-loop → stage advance → Task event 分派。
- `stage_delivery._handle_stage_advance_item` 保留 admission/lease、等待、投递和 receipt 的原有语句顺序；admission 的存储契约仍由 `herdr/coordinator_stage_admission.py` 与 node dispatch store 执行。
- `task_events._handle_task_event` 保留自动验收、状态/episode stale 检查、协调器等待与最终队列清理顺序。
- `registry.registry_watcher` 和 `task_listener` 保留原巡检顺序、节流、监听与失败恢复入口。

验证须从旧服务入口经过实际实现和临时 SQLite，再读取持久化结果；仅验证兼容适配函数被调用不能证明工作流行为。
