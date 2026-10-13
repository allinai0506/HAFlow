# PR 交付物回执读取修复

基线：db90394e0b424dd6fb0715dbde7a93ae8d9f4c79。用户授权立刻修复缺少 PR 展示，并延续本地服务更新授权；不推送或合并远端。

原因：`herdr-task create-pr` 正式创建回执保存于 events，而 Console 交付物只读取任务 PR 字段。wf-nexusarchive-1012-01 的事件74013持有 PR #1611，任务本身无 PR 字段。

变更：Console 在交付物装配时按 workflow/task/event_type/source 查询最新一条正式回执，并投影给既有 PR 卡片；保留旧字段兼容，不写入任务或 PR 状态，无迁移。缺数据库不生成回执，读取异常保留旧字段并输出警告。历史创建事实不证明远端当前状态。

验收：RED 1 failed/4 passed；专项45 passed；同模型独立只读 agent 27 passed、未发现阻塞缺陷；全量4144 passed、163 subtests passed、0 failed、1 warning（测试中拒绝不可信路径清理的预期安全警告），exit0。compileall 与 diff check exit0。

真实链路：PR producer→SQLite正式事件→Console workflow/task API→既有git_pr/open卡片。修复版查询生产工作流取得 nx101201-impl-t1 的 #1611，未改写存储任务。服务更新与页面验证记录见本轮交付答复。

回滚：保留db90394快照，安装该SHA并重载。无需数据库回滚。
