# Workflow 进展与持久恢复契约

目标：blocked 验收不依赖其他并行节点完成即可生成恢复义务。执行状态、验收、交付与候选归属分别判断；cleaned 不等于通过。

## 不变量与验收

1. 活跃事实排除 superseded 或 superseded_by，cleaned blocked 与 committed escalated 仍阻塞；Console、Controller、手工推进共用评估。
2. 原 SQLite 内阻塞事实和义务同事务；既有数据 sweep 补登记。原子故障完全回滚。
3. 身份包含 workflow generation、task/run、候选 SHA 与受影响谱系，活动时间不能产生新义务。同事实并发唯一；候选换代作废旧义务与旧回执。
4. 恢复先于正向 DAG，无 pane 或无法安全执行必须持久 waiting_human。
5. 数据库 CAS 租约控制执行，执行前重查身份。副作用开始后未知结果禁止盲目重发；超时转 delivery_unknown 待核对。外部系统不宣称 exactly-once。
6. committed 失败候选保留历史，通过正式 successor 谱系修复。不 force committed→rework，不 integrate 失败候选作为修复前置。新 SHA 必须重测复审。后继源码必须可读取精确失败 SHA；优先项目仓库，否则使用登记的干净前驱 clone，项目根仍是最终集成目标。候选缺失或 WIP 转人工，不推送、不集成失败候选来补对象。
7. 人工决策绑定 operation version、候选、操作者与理由。retry 必须前置条件有效；hold 带期限，届满返回待处理；无自动 stash/reset、跳门禁、无条件 pass。
8. closed/paused/startup-not-ready 不执行恢复；未知事实不猜测归属。
9. 每个恢复步骤有持久结果与 next_due_at/attempts；通知不算完成；successor 失败仍有义务。

同 retry_node 的并行 test/review blocked 合并，保存全部失败 gate 和 affected IDs。配置未知转人工。复用 StateStore、现有 launch intent、rework、supersede、实例校验、candidate freeze/reverification 契约；不重造调度框架。不自动扩展 NexusArchive SecurityConfig 修复范围。

仅沙盒 working_tree 交付。部署需 immutable release、先 shadow 对比再切单执行者；本任务不重启服务、不写线上 workflow、不宣称线上恢复。

## 完整覆盖与请求回执

每个原受影响 Task 必须有持久 `source_runs` 和 `repair_map`，一对一关联已确认后继或同 Run rework。原始目标缺失、重复目标、谱系/Run/候选不符时，保留 waiting_human 并返回缺失 IDs，禁止作废任何失败门禁或结案。

rework 的同 Run 不代表同请求。本轮 request ID 在外部调用前落盘，并显式传入现有 CLI 去重契约；核验必须匹配本轮 request ID、completion epoch、identity path。receipt-v1 必须有当前请求的实际 rework_dispatched 事件，绑定 workflow/node/task/run/source；gate 作废和结案事务重新核对。旧请求已送达不能证明本轮已送达。
