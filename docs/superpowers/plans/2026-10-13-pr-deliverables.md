# PR 交付物回执读取修复计划
用户已批准最小方案并要求立刻修复。单一主控执行，无额外 Agent。
目标：正式 pull_request_created 回执在 workflow/task 交付物显示，兼容旧任务字段。
架构：Console 装配按 workflow/task/source/type 查询最新一条事件，传递临时 pull_request 字段；核心已有 git_pr 渲染不变。不写状态，无外部调用。
- [x] 临时 SQLite 回执到 API 回归测试，确认 RED。
- [x] 最小读取实现并验证 GREEN；身份隔离、intent、cleaned 负例。
- [x] 删除冗余检查，自审差异，专项和全量测试。
- [x] 记录验证、更新 Wiki，提交隔离分支。
- [ ] 安装该提交的快照，核对真实工作流 API 与浏览器 PR 链接。
