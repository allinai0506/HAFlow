# 0004 Modeling Decisions — 为什么这样建模

1. **候选（全部 experimental）**：首轮面向评审，不把任何定义标为 active；active 需要评审 + 测试 + Merge。
2. **对象选择**：只建模真实业务闭环所需的最小集合（12 个对象）。Trajectory/Observation/Finding/ContextPack 是重要语义层，但属于下一批切片候选，本轮不纳入以免范围失控。
3. **数据库不是业务对象**：`tasks` 表一个对象承载了 canonical 列 + payload 扩展；本体按业务含义拆成 `task` 的 properties，而不是按表 1:1 建对象。
4. **workflow_definition 双载体**：模板与 Run 冻结快照在业务上都是“定义”；用一个对象 + identity 表达，避免把同一语义拆成两套事实源。
5. **artifact 是投影不是实体**：`deliverables.py` 明确其不构成已验证事实；因此 relation `task_produces_artifact` 标记 object_backed=false，并在描述中保留“不保证已验证”。
6. **event/checkpoint 建模为对象**：虽然偏基础设施，但它们是业务追溯与恢复的持久事实，先以 experimental 纳入，允许后续降为 Coupling。
7. **关系最小化**：只建 12 条有业务含义的关系；`workflow_id` 直连与 `node` 路径并存，因为恢复扫描依赖直连（FK），DAG 语义依赖节点路径。
8. **动作全部有 binding**：按技能要求，写操作必须给出 target/inputs/preconditions/authorization/effects/side_effects/idempotency/transaction/audit/failure_behavior/bindings/verification；未接线项在 open questions 中明示。
9. **权限策略保守**：`task_agent_scope_policy` 明确禁止 Agent 自我扩权；`gate_bypass_policy` 记录“无二次审批”为已知缺口而非隐藏假设。
