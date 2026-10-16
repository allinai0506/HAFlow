# 控制面收敛修复计划

基线 b4c3a11，范围和授权见 .omc/entry-gate-control-convergence.md。原始五个缺陷已在本沙盒RED重现（5 failed、2 passed）。方案沿用现有SQLite、launch intent和Git交付引用。

1. 候选完整性：新增candidate_assembly，按有效implementation Task/Run/integrated_commit形成稳定库存；使用git merge-tree和commit-tree合成，绝不checkout源工作区。正常单任务兼容原发布路径；多个任务必须全部交付、无blocked/缺身份/未知commit。发布事务复验库存和原episode，event附库存；冲突不发布。先新增独立Git/线性对照、冲突、重放、库存变化测试，再实现。
2. 启动失败：CLI写launch_rejected回执，绑定operation/attempt/task；进入资源创建前失败可abort本次intent并确认缺席，进入资源边界后保持unknown。Controller只消费数据库证明，不根据stdout或exit75盲重试。direct_finished确认缺席时当前代有界重试，旧代退休，已登记/部分角色不重发。
3. 完成事实消费：只扩展未发送且因旧派发未知等待的责任；完成库存必须同execution/node/candidate/episode、verified SHA匹配、PASS且完整角色/required任务。人工hold和明确取消不自动解除，已发送未知独立保留。
4. 扫描幂等：将幂等检查放在共用_write，只有状态/详情/租约/预算真实变化才写版本和事件。验证100轮claim和重复reconcile，终态不复活。
5. 组合验证：真实Controller/CLI边界→SQLite→公开读回；仅替换外部Agent，覆盖锁失败后释放→重试→唯一Task、候选轮换→旧意图缺席→新代可认领。专项、全量pytest、compileall、无扩展名AST、diff check。
6. 独立只读审查与修复闭环；按ai-slop-cleaner删除重复/无用代码后重跑。更新Wiki和四段式教训，记录未部署/未真实生产冒烟，不Push/合并/重启。

方案自审：无第二事实源；慢Git不放SQLite写事务；候选合成结果绑定库存并CAS，迟到旧启动不能接管新代；未知副作用仍fail-closed；无用户业务需求扩展。上一轮诊断测试的轮换用例只调用preflight helper，本轮改用实际launch_task异常收口边界，避免测试替身遗漏应由CLI负责的补偿。
