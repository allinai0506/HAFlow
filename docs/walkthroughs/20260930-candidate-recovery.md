# wf-project-0929-01 Candidate 身份恢复

## 目标与根因
恢复实现推进，完整实现才冻结 Candidate，不绕过 test/review。基线 HAFlow `3b557bcb`，隔离分支 `fix/implementation-candidate-readiness`。

真实链路：T5 completed→commit hook失败→任务分支未持久化到source→最近实现Task的branch无法resolve→Candidate冻结返回空→test/review每轮defer。同时计划剩余Task未派发，但完成判据只统计已有Task。旧 `gate_overrides.implementation` 人工放行是历史事实，不代表缺失实现已交付。

## 最小修复
- Worktree独立metadata导入保留source local heads，保持HEAD/index隔离；不修改source。
- Git模式节点完成等integrated/cleanup_ready/cleaned；非Git保持兼容。
- 显式required_task_ids清单校验：缺项/缺替代/环不完成；无配置兼容。Controller/CLI/ops-center共享判据且投影保留字段。
- Candidate选择忽略superseded和superseded_by旧记录。
- Selective replan即使节点已因未集成而不完成，仍据持久化待补派事实清除阶段闩。

## 现场恢复证据
T5 staged binary patch保存在 `~/.herdr-controller/workflows/wf-project-0929-01/recovery/20260930-121148-candidate/t5-staged.patch`。恢复source local dev到T5后，另一独立origin/dev祖先门禁仍拦截，故正常rebase最新dev而非改ref伪装/跳hook。source保留 `recovery/wf-project-0929-pre-dev-sync`（b9cb1a036）再rebase最新dev，保留T1/T6/T2真实成果。

T5刷新基线的前端全量在默认Node出现真实SSO失败，诊断router.navigate发现Node Request与DOM AbortSignal类型不兼容。改用项目Node22.16.0后4015/4015通过，原断言未改。随后hook发现T5自身两个复杂度/长度警告（HEAD无T5实测350，含T5=352，上限351）；仅在API与hook原文件拆分职责，接口行为不变。拆分后4015/4015再次通过，复杂度350<=351；完整commit hook通过，T5提交585a2bcb2aee11c7d22d2a3aa71c97991fb6f02d，控制器集成为a4a9909bee36579fc9d2d42664dd3a156a5c530d，source已包含该交付。

## 回归与边界
RED已证明source local branch不存在、completed/committed的Git任务过早完成、作废Task污染candidate、planned Task缺失、空reuse绕清单与UI投影丢清单。相邻selective replan的真实sweep退化已修正，断言未放宽。

真实临时链：Controller读取明确计划→未派发项不冻结→全部交付后resolve真实Git SHA→SQLite candidate_frozen唯一事实。无收费Agent替身或生产状态写入参与该探针。

最新全量：2440 passed、67 subtests passed、零失败零跳过（424.68秒）。相邻专项165 passed、16 subtests passed。隔离副本撤掉关键修复后6 failed、4 passed，证明回归会阻断该退化。独立评审覆盖最新完整调用链，无阻塞项；评审者未独立重跑全量。

运行代码以补丁加载到保留其他修改的HAFlow目录，并用launchctl热重启Controller。配置已保存9项required_task_ids，T3真实ID为impl-t3-abnormal-voucher-provider，基线d78809a32。恢复派发与协调器竞态产生的impl-t3-abnormal-provider已halt并supersede，未集成，保留现场（autosave报告git add失败，不宣称保存了其WIP）。当前实现节点in_progress、complete=false，原T3 runtime running；test/review尚未派发符合未完整交付的边界。配置与5个运行文件均有recovery目录回滚备份。当前不宣称产品工作流已完成；G1c/MATCH的DB证据与后续T4/T7仍属真实依赖，禁止补造验收结论。
