# 并行 Pane 修复计划

目标：用户授权 L1–L5，交付 working_tree，不部署、不真实回收。

- [x] 纯统计与 RED 回归：累计历史、并发、旧阈值确认及硬预算不可绕过。
- [x] launch 预检在路由/拓扑/supersede 之前，workflow 文件锁覆盖预检到注册，跨 source 同样受控；替换要求理由。
- [x] panes CLI 与图节点常驻计数高亮，引用数明确标注不等于实时存活。
- [x] kernel 归档事务标记 orphan；reap --apply 核验实例归属/活跃共享/动态所有权，unknown 保留，失败可恢复，不删 clone。
- [x] rework 保留 task_id/run/pane，CAS 与持久投递；blocked 默认返工。精确候选失效仍保留独立验收历史。
- [x] 模板迁移、文档/Wiki/教训、专项/全量/语法验证、独立评审。

用户已确认模板累计配额：需求/计划2、实现12、测试/评审4、收尾1。

L5人工介入后两项恢复边界已修复，独立评审通过；最终全量2942 passed、157 subtests passed。交付为working_tree，详见 .omc/review-parallel-pane.md。
