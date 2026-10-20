# 门禁与度量修复规格和实施计划

基线 df4b3cf，交付 working_tree，单一主控，无外部发布。

- [x] D8：receipt-v1 force-pass/bypass 在任何写操作前明确拒绝；legacy 保持兼容。测试数据库 task/workflow/events 均不变。
- [x] D9：等待持久化到现有恢复操作，按 workflow execution/candidate/node/reason 去重，三轮后 waiting_human 并写一次 stalled 事件；通过后 resolve。不得发业务放行凭证。
- [x] D10：基线拒绝派发的 task 采用既有 superseded/replacement_pending，带 launch_not_started 与 blocker；派发归属不把它当已执行。
- [x] D4/D6：明确已知 lint 格式；未知非零退出 unavailable 且不能因旧 baseline 通过。rebaseline 必须带原因，旧基线与新 SHA 一同原子写入；失败保留原值。
- [x] D1/D2/D3/D5/D7：按真实源码与最小复现裁决；不以猜测改变 workflow 生命周期。记录 restart/rollback 边界和可执行处置。
- [x] Cleanup：仅在已改文件内删除重复与无用分支；再跑专项与编译。
- [x] Review：向独立 reviewer 提供规格、diff、最新测试证据；修复有效问题并重验。

替代方案：人工制造 acceptance PASS 被拒绝，因为缺少门禁自身签署；直接将 failed 改 pending 被拒绝，因为会丢失拒绝审计；自动完成 paused workflow 被拒绝，因为生命周期事实不足。

- [x] D11：三路 EOF 追加校验、编号冲突顺延、原 main 前缀字节恒等、patch 可在 onto 工作树真实应用；历史删除/非 EOF 修改拒绝，输出目录必须不存在，不改原候选。

评审闭环：初轮退役遗漏、第二轮 D11 EOF/空文档边界均已以真实回归修复；跨模型审查追加的阻塞集合复现、度量合同绑定、Console 操作合同和相同追加去重亦有 RED→GREEN。最终第三轮只读审查在最新完整专项与 G/GA 原文全部齐备后执行。

用户授权恢复后的限定修复：复用 canonical generation（含 reopened_at）并按当前状态判终态；549 项专项及独立三方复核通过，候选兼容探针最终回执另存交付证据。
