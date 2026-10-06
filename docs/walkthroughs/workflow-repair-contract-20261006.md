# Workflow 恢复修复交付与激活预案

工作树：`/Users/user/.sandboxes/haflow-recovery-contract-1006`；基线 `ee061373a6d1dbc3e8641d6ff7746d6f606d9ef2`。授权交付上限 working_tree。规格见 `docs/product-specs/workflow-repair-contract-spec.md`，实现计划见 `docs/superpowers/plans/2026-10-06-workflow-repair-contract.md`。

## 变更与证据

按用户五项顺序实现共享快照和副作用前拒绝、历史证据迁移、现有交付消费、专属集成引用和最终 SHA、显式候选发布、当前失败过滤、业务回执与有界预算。`.omc/evidence/` 保留 RED/GREEN、独立审查、源码摘要及完整测试输出。历史生产备份只在本地独立 shadow 修改，线上状态未写入。

## 授权后的激活顺序

1. 将审核通过的冻结源码提交成明确 Git SHA；创建不可变 release，核对 Controller/Worker/Sentinel 的实际启动路径。停止旧执行者再切换，不能用 kickstart 代替读取变更 plist 的 bootout/bootstrap。
2. 新建生产只读 backup，在该 backup 重新运行 migration plan/apply/rollback。复核配置文件 SHA、INITIAL/current epoch、Task 专属 Git 引用；审核计划不得直接使用旧 shadow 的 fingerprint。
3. 在暂停自动派发、单执行者条件下备份生产 DB，然后以新计划 CAS apply。保留操作 ID/attempt/request；如有新事件或版本变化，重新生成计划，禁止覆盖。
4. 核对 implementation-04 当前 Run、clone HEAD 和已有 task ref。重新执行幂等 integration，持久化真实 post-rebase SHA、清升级标记；已经明确冻结同 SHA 的候选不得因重试再次轮转。
5. 对已送达 request/operation 做 verify，先确认同代 Run/epoch/请求的一对一 coverage，再继续剩余 gate invalidation/latch；不得重新发修复。测试/review 预算按最新配置哈希分别显式增加，建议每节点 +2，不重置已有 4 次历史计数。
6. 派发独立 test/review 到当前明确候选（当前只读现场为 `6e47cedc03994073ec461cd26d13908a866c93e8`，激活时必须重新核验）。验证实际 HEAD、Task 分支和所有配置验收项；checkpoint 记录端点/授权等业务测试，随后登记全覆盖业务回执。只有新候选全部真实门禁 PASS 才结案。

## 回滚与停止条件

代码切换失败恢复原不可变 release；生产迁移回滚只能在 after fingerprint 未变化时使用 receipt，后续执行已经改变状态则停止人工核验，不能逆写覆盖新结果。unknown 身份、未知交付、CAS 冲突或业务失败均保留待办，不宣布结案。本地绿灯不证明生产复跑成功。
