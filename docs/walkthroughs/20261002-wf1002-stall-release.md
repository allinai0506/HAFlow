# wf-project-1002-01 集成发布与存量恢复

用户确认授权本地集成最新主干、热更新 Controller / Console / Sentinel，以及恢复指定 Workflow。本记录续接 [原修复验证](20261002-wf1002-stall-fixes.md)，原文是发布前历史快照。

## 候选与验证

隔离分支 `fix/wf-project-1002-stalls`，实现提交 `7df743f82360bcb16cf660d6b72e52cbc795e21e`，合并最新主干 `c0cf2a00caf3a306bb3ce7d3b4a2c1ffa9adf21a` 后候选 `81dc9a39b9dba34f01ca56a165ba99aba04dd8d6`。主干预检、路由及 Worker 修复保留；Worker 测试只补入实际生产使用的启动身份参数，原断言不变。教训保留主干 §122/123，本轮为 §124。

- 全量：3197 passed、2 skipped、157 subtests passed，0 failed，exit 0；pytest 562.51s，进程564.1s。临时 HOME，无生产状态/模型调用；两个 skip 为隔离 HOME 无实际 LaunchAgent 安装目录。
- 集成专项：RED 1 failed → GREEN 141 passed、2 skipped、6 subtests。独立发布专项59 passed、2 skipped、6 subtests，组间重叠不相加。
- compileall、五个无扩展名 Python CLI 的 AST/help、supersede --help、diff check 全通过。Ruff F 基线18/current18，无新增诊断。
- 321 源码输入指纹 `4dcbb4bda8321f374fd4a74d62b62293a0be2a073cbe1ebaa4765b6880a83018`，全量前后不变。git archive 制品逐文件/执行位校验通过，manifest SHA256 `0e9ec244c51ea092e8c7d6a7bc41c6db330c3a6bd63d4ecb8ee7b246953cd84c`。

审查和操作证据保存在本沙盒 `.omc/stall-release-*`、`stall-production-risk-review.md`、`stall-requirements-recovery-review.md`；固定三 plist 备份和目标状态快照在 `.omc/stall-release-backup/`，不依赖可能被覆盖的通用 `.bak`。

## 运行合同与恢复范围

现有 Run 的 implementation 必需任务误指向 wf-project-1001-01；恢复将其绑定到真实同 Workflow/同节点的 `impl-compliance-race-json-truth`，并冻结 Run 私有定义。共享项目配置不改，所有预算/工位规则保留。原 requirements 没有 required_task_ids，不采纳未应用草案新增的义务。

独立复核按 SQLite 原始 requirement、两份核心产物哈希、固定候选源码及真实日志，撤销“后端缺ID必须用户补充”“9对10”“严格三文件”等假阻塞，确认 A4 已由原需求明确。附录以人授权恢复的 human 来源发布，无假 Task receipt；采纳真实规格与对抗清单后，只显式 abandon 旧理由中为补生命周期记账提出的 r2 等价重跑。旧 Task 保持 superseded、原 Run/首次理由保留，不开第三 Task，不跨节点替代，不伪造 completed/PASS。

实现任务在本次生产操作前已由外部运行参与者进入 integrated，真实 task ref 为 `8be3099a61327c69b6bd77ae089ab73de0b949e0`；不归为本修复的集成成果，不等于源仓主干合入或 PR 交付。只处理已解决原因后的旧 finalize escalation，由正常 Controller 收尾。

## 发布与回滚

只更新 `com.user.herdr-controller`、`com.user.herdr-factory-console`、`com.user.herdr-sentinel`；三个 plist 全验证成功后 bootout/bootstrap。Notifier、全局主仓 CLI 不修改。后续恢复调用新 release 内绝对 CLI。

回滚前必须暂停涉及新 replacement_pending 义务的自动推进 Run；旧 scheduler 忽略新 pending 语义，不能直接切旧服务后自动 resume。固定备份恢复仅三 plist/旧制品，保留 Task/events，不恢复整个 DB；恢复推进前重新核对 required IDs 和替代链。

实际三服务已加载 `81dc9a3`：Controller PID43154、Console PID43156、Sentinel PID43158；启动 fingerprint 的完整 SHA/import root/component hashes 与制品一致。Notifier 仍旧 PID38505、旧 c0cf release，plist 字节不变。页面、ops-center、workflow API 均200，新增 stderr 无启动 traceback。证据 `.omc/stall-release-runtime.json`。

首次 Console bootstrap exit5，已按计划回滚三份固定备份；过滤 launchd 日志仅证明旧服务移除，不足以确认错误原因。第二次三 bootstrap 成功，主控立即读取 PID 过早；随后发现主控检查进程导入 release 时写入34份字节码，使严格 startup attestation 显示 unknown。重新对 fresh archive 校验内容后清除缓存，所有后续恢复导入设置 `sys.dont_write_bytecode=True`，三服务再次有界等待并重载，得到上述真实版本通过。失败与回滚记录保留，不称首轮成功；没有因此修改业务源码。

恢复实际完成：共享 implementation ID 在保护检查期间已由外部参与者修正，本主控没有覆盖它；核对除该ID外定义不变后，冻结 `/Users/user/.herdr-controller/workflows/wf-project-1002-01/workflow.json` 并通过 Kernel/Store绑定。需求复核附录 `n-1790949258577-d209`、采纳决定 `n-1790949258586-ba70` 为human来源、无Task假回执。canonical abandon 后旧对抗Task为superseded v9，replacement_pending=false，原Run/首次理由保持，需求Task总数仍2。实现Task清除旧escalation后integrated v18，实际task ref仍8be。目标从paused恢复running。证据 `.omc/stall-recovery-applied.json`。

最后现场观察：协调器 w13:p1 正在响应另一条用户handoff请求，真实working，并出现“Timed out waiting for response headers”模型重试；不是陈旧DB working。Controller因此等待，没有强改状态或中断。下游另出现test_baseline_rejected/delivery_missing记录，需要明确绑定8be冻结candidate的合法测试派发；没有伪造delivery/test/review门禁，不把running宣称工作流全部完成。

## 业务验收边界

NexusArchive 固定候选历史日志有 4139 tests passed，但 Task/Run/epoch 为空，不是正式下游门禁。本次原需复核保留 A1 页面顺序、A5 导出组合、A9 error/owner 分项、非法 JSON、JSON字面 null/数组、JSON纯空白、URL/anchor 副作用探针、typecheck、旧实现 RED 及正式 test/review 等未验证项。没有修改业务实现、自动合并或部署 NexusArchive。
