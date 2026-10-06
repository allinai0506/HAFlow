# Workflow恢复契约修复规格

状态：实施中；原现场证据位于 /Users/user/Documents/Codex/2026-10-06/wf-nexusarchive-1005-01-diagnosis/。基线 ee061373。

## 目标与非目标
修复用户明确的五步路径：权威快照/身份与历史迁移 → 既有修复/交付消费 → 任务专属集成/最终SHA → 显式候选发布/当前失败 → 业务证据与评分分离/有界预算/独立门禁。
不改Nexus业务权限，不改写历史blocked，不强制放行，不添加外部依赖或平行数据库。交付上限working_tree；生产激活、push、merge分别需要明确授权。

## 验收契约
1. 所有恢复入口读取同事务workflow/config/task/candidate快照；缺execution_id、Task/Run归属冲突、未知候选、预算不足均在prompt/clone/Pane前拒绝。新Workflow注册持久代次。遗留迁移要求已存在的同代次Task和本Run持久初始交付证据，dry-run报告逐条引用；冲突拒绝，apply有CAS与回滚回执，不猜缺失身份。
2. 已有repair_map/current request/Run交付存在时只消费、验证，不重发。正式后继的INITIAL回执与原受影响范围一对一绑定；正在working/rework不生成第二次失败义务。已有修复Task可等待结果，不能因等待恢复再发新任务。并发认领最多一个执行者，未知副作用只能verify。
3. 实际Git集成只发布refs/herdr/tasks/<task>与herdr/integration-<task>；源工作区当前分支及HEAD不变。onto是基线而非新Task分支所有权。rebase后最终SHA先持久化检查点，再发布引用和完整集成回执；中断可幂等恢复。
4. 既有候选episode是当前权威；cleanup/updated_at不得自动改变它。发布来自精确task/run/交付SHA或明确人工CLI，expected previous episode CAS拒绝迟到/并发。返工prompt仅用当前有效gate Task、Run、candidate的义务事实；历史失败保留引用，但不冒充当前阻断。集成升级随同受管成功更新收敛，latch投影维护不被更早return永久屏蔽。
5. 通用metrics只证明配置命令执行，不宣称验收DoD。必需验收项由结构化gate结果及现有Observation/checkpoint引用绑定task/run/epoch/candidate；前端绿不能抵消Java红。累计预算维持硬上限，增加显式一次有界预算配置入口与耗尽前置诊断，任何变更有operator/reason/expected状态审计，不删除历史计数。新SHA独立test/review才能结案，wrapup交付是后续义务。

## 验证边界
在临时SQLite、真实Git和受控外部Agent传输下覆盖完整CLI→核心→持久化→读取链。使用事故快照的脱敏投影做历史迁移dry-run/前进/回滚，不把这些测试称为生产修复。全量pytest、compileall、无扩展CLI语法、diff检查与独立规范/规格评审为本轮本地门禁。现场新SHA复测需服务激活门禁后执行，结果另记。

## S3评审补充：迁移与中断边界
- 权威配置：新注册Workflow同事务保存标准化config_json；恢复read_snapshot只读SQLite配置，不回退外部文件。旧workflow_file仅允许一次迁移导入，计划包含精确内容SHA256和原DB行摘要；apply重查二者，事务内写config_json并审计。外部文件随后改变不改变本轮快照。
- 唯一可接受代次组合：现有workflow.execution_id，或当前创建/reopened窗口内Task唯一非空execution_id；每个补字段Task必须具有同workflow/task/node/current run的INITIAL事件，并有当前epoch/path的INITIAL或rework dispatched回执。仅旧epoch、多个execution_id、全部未知、缺run、窗口外活动Task都拒绝自动迁移，不凭workflow名字猜代次。
- 既有operation连续性：迁移同事务改绑旧payload facts中已证明的execution_id、重算semantic identity/slot；保留operation id、started、attempts、request与repair_map、原身份/证据迁移审计。已started的未知交付仍waiting_human；仅补detail.execution_id后由显式verify消费原回执，不能新建effects-eligible副本。新slot冲突或operation事实Run不匹配时整个apply拒绝。回滚按迁移后行/事件水位CAS，正常进度发生后拒绝回滚。
- 已送达消费：不再发送prompt，但必须继续幂等完成剩余gate失效和latch维护；若已有新candidate episode，仅验证原回执并要求该episode新test/review，不作废新候选gate。gate失效逐步记录，已作废门禁重读视为成功；交付后、单gate后、全部gate后但回执前崩溃均能恢复。
