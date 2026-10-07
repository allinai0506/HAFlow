# 用户可操作恢复闭环
目标：用户在主流程图看到真实卡点，经恢复表单确认范围或未交付事实后，由原Controller安全派发当前冻结候选。当前此前已确认的两个恢复入口为实施范围。
路由：compatible_drift；bug/medium；state_machine/concurrency/data_integrity/production；ui/api/database；unified；controller=none（单主控）；adversarial；交付延续已授权deploy后PR，不合并、不自动恢复生产工作流。

## 设计与约束
1. 单库权威：恢复UI投影同事务读取operation、workflow、任务及最新启动intent；按钮由有效前提产生，不仅由started决定。
2. 恢复测试：用户选择恢复已取消范围，填写处理人和理由，明确确认已检查旧工位无旧任务执行。仅当前候选、同代次、依赖就绪、无其他旧未知责任且无活跃任务/未结束intent可提交。取消头replacement_pending改为true并增加Task版本，旧operation superseded；同事务创建绑定前序Task/Run及prior_operation_id的新pending责任。
3. 审核核验：可先核对登记，不产生派发。用户检查工位后明确声明旧派发未建立执行，并授权重新派发；数据库有活动Task或未确认resources_absent intent时拒绝声明。人工回执单独记录，不冒充机器缺席证明。旧operation永久退休，当前候选新epoch pending。旧operation迟到登记拒绝。
4. 提交绑定operation version、当前候选、确认字段；双击/版本变化/暂停/回退/跨工作流拒绝且不产生部分状态。改动事务内无模型/进程/网络；Controller原claim/send/intent/task链负责接续，UI不直接launch。
5. 主图、节点Inspector、底栏和Controller统一显示等待人工或核验；待办面板写明发生什么、为何不能自动重试、具体下一步和候选。没有恢复待办才显示无卡点。成功提示仅责任重建，实际任务登记后显示派发已确认。

## 验收和计划
- [x] RED取消恢复、未交付确认、版本/候选/并发/旧登记拒绝；真实API到SQLite到Controller派发。
- [x] 最小实现与删除式cleanup。
- [x] 专项、全量、浏览器真实临时服务表单操作/持久化读取；生产只读验证显示。
- [x] 独立评审与反馈闭环，最多三轮。
- [ ] 知识更新，按授权部署及PR；门禁失败如实报告。

替代方案：仅翻译错误不解决恢复；放宽通用retry会绕过取消和未知交付保护。选择显式有审计的新责任，复用既有调度机制。

## 审查补充约束
旧取消头缺Run时拒绝恢复；已有Run但execution_id缺失时，表单展示Task/Run并要求显式确认归属本次执行，审计旧值后才绑定。迟到无operation启动及登记不得接管人工作出的新授权。恢复GET失败时显示状态未确认和刷新入口，不把失败当无阻塞。现场核查复用既有inventory并保留分项原因，不伪造机器缺席；旧资源仍存在时继续阻止重发。

## 第三轮评审断点
状态：NEEDS_FIXES，按 RULES S6 和 unified-dev-flow 的三轮规则升级人工。标准审查通过，规格审查复现现场核查期间 operation 被暂缓后仍返回成功：intent 已结束但当前待办无核查结果。尚未提交、部署或创建 PR。

下一步方案：外部核查只采集事实；最后单事务重查候选、operation version/status、intent 身份，拒绝变化时不写任何本请求状态；同事务完成 intent 回执、待办投影和审计。添加 probe→hold/verify 的独立连接交错回归，再做全量及独立评审。需要用户授权继续下一轮；保留现有分支及证据，不绕过三轮门禁。

## 人工升级后继续
用户明确授权继续完成修复、立即部署然后PR。现场核查改为只读采集，最终事务严格重查候选、operation version/status/action及精确intent快照；通过后复用连接内资源登记/缺席确认，同事务写待办结果与审计。CAS拒绝或审计失败不留本请求部分写入。旧三轮记录保留为历史，本轮重新独立验证与审查，不合并PR、不代用户提交生产恢复决定。

最终隔离验证：3755 passed、2 skipped、157 subtests passed（583.72s），两项此前全量失败均已核实并关闭；独立规格和规范复审通过。生产部署与原工作流业务复跑分别记录，不从测试通过推断业务通过。
