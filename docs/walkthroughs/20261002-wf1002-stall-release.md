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

81dc 发布后的现场观察：协调器 w13:p1 正在响应另一条用户handoff请求，真实working，并出现“Timed out waiting for response headers”模型重试；不是陈旧DB working。Controller因此等待，没有强改状态或中断。下游另出现test_baseline_rejected/delivery_missing记录，需要明确绑定8be冻结candidate的合法测试派发；没有伪造delivery/test/review门禁，不把running宣称工作流全部完成。

## 后续：协调器恢复与无 onto 候选传递

用户随后明确要求修复模型超时及测试漏传 SHA。同一协调器会话/Provider 在22:03:47成功完成 handoff，22:03:49收到新的阶段事件，后续工具调用持续成功。handoff 文件真实存在，SHA256 `f277deab1a44feba9aa6667759b0072e6dd402ebf0f203b96b63fbcc959b3936`。本轮未改 Provider、放宽 timeout 或中断会话；缺请求 status/request-id，网络、网关排队或客户端 deadline 等细分原因仍 unknown。取证 `.omc/stall-coordinator-timeout-fix.md`。

SHA 缺口由真实调用链确认：实现8be已发布 task ref，但 source HEAD仍1638608；Controller 的已交付规划省略 onto 后，原回退只接受 HEAD==freeze，导致不传 pin，CLI正确拒绝 delivery_missing。直接使用实现的 owned branch 又会被所有权保护拒绝。修复只涉及三个生产文件：Controller用本Workflow当前full40冻结SHA解析真实commit，CLI无onto仍透传pin，Worker从本地候选commit创建验证任务自己的分支。无需远端fetch，不放宽onto所有权，短/缺失/blob对象在Pane前拒绝。

同断言控制回退分别复现 Controller 2 failed、真实CLI链1 failed；修复后专项144 passed、26 subtests passed。真实 CLI→Worker→Git→SQLite 的 candidate / baseline / clone HEAD 全部等于pin，source HEAD保持旧基线；追加受控清理隔离单测1 passed。首轮321输入指纹 `56483e2be7e5d66ee93a256e661c1b054b8aafe178f1c7aade1bbda45d93ca1d` 的全量3205 passed、1 failed、2 skipped、157 subtests，失败仅旧 `head_moved` 断言，记录保留 `.omc/stall-pin-initial-full*`。独立复查确认新Worker精确pin分支下不需要HEAD==freeze，保留未显式pin拒绝与五类负例，将该用例增强为实际派发/CLI身份证明，并将CLI→Worker→Store真链覆盖HEAD behind/ahead。最终321输入指纹 `7a0ac3d1e01f575001f3789f084d35cfa7fea9945cdba307b518753fc7126be4`；本轮完整回归与发布证据回填于下文。

22:33现场已有协调器自行恢复的真实测试Task `test-compliance-race-json-truth`，working，codex w13:p24，candidate8be；其恢复早于本轮新代码部署，不能归为本修复的部署成果，也不再重复启动test-auto。正式业务test/review结论仍按本Run证据门禁处理。

本轮最终全量：3207 passed、2 skipped、157 subtests passed，0 failed，exit0；pytest507.06s，进程507.9s。上述321输入前后无漂移，两个skip仍为隔离HOME缺真实LaunchAgent目录。强合同专项10 passed，独立审查确认HEAD移动不是freeze轮换；compileall/五CLI AST+help/diff共7项exit0，Ruff F仍18，无新增。证据 `.omc/stall-pin-full.log` / `stall-pin-full-result.json` / `stall-pin-source-freeze.json` / `stall-pin-static-checks.json` / `stall-pin-health-result.json`。制品/实际版本/本轮首次自动派发另行记录，不以本地全量伪称业务门禁PASS。

### 本轮实际发布与自动派发

实现提交/运行release为 `d93a3c807eea7427d34cf92730c446e1bb3c27c4`，git archive 448文件内容/执行位验证与321源码指纹一致；archive SHA256 `d45b4f5b459c6738c12f6278179e4bb1204a52736e2ae0692292e7041054af96`，manifest `b2b25b7bdde1df94a1f8fe296b0a4b39440634320af67bc7fba4f5f8cfc51bd2`。固定回滚为前一81dc三plist，不退到c0cf；证据 `.omc/stall-pin-release-prepared.json`、`stall-pin-release-backup.json`。

三服务一次bootout等待旧label/PID完全退出后bootstrap成功：Controller43390、Console43392、Sentinel43394；实际进程命令、启动SHA/import根/component hashes全为d93制品，三个关键HTTP均200，无新启动traceback。Notifier原PID/plist不变，未动正在执行的业务Pane。本主控读取/导入release设置禁止字节码缓存。证据 `.omc/stall-pin-release-runtime.json`。后续旁路Observer仍记录jev422，未改变执行状态，不作为这两项修复的PASS或新增业务阻塞。

旧review空候选intent `fd532f0970ab4ddeae9a1045cc731ec9` 经canonical CLI完整原生资源核查为absent，apply后为resources_absent（不是cancelled）。旧失败已通知协调器，遗留review notified闩但没有ReviewTask；短暂pause目标、核对没有ReviewTask且协调器done seq347后，通过现有Controller `clear_stage_advance` 仅删除本Run的review键，resume由正常新Controller重驱。所有其他stage键不变，未伪造业务结论。证据 `.omc/stall-pin-review-redrive.json`。

23:09真实新自动链已完成：Controller记录 `[STAGE ADVANCED DIRECT] ... node=review tasks=wf-project-1002-01-review-auto`；Task working v4，codex Pane `w13:p26`，Run `run_4c7508b036f04d6b84e6ebc1832dd013`，intent `5c5850ff94c244ad944540e8dc76bea2`。candidate、baseline和真实clone HEAD全部8be，source HEAD仍1638608；独立任务分支 `agent/codex/test-wf-project-1002-01-review-auto`，未借用实现分支。原测试completed v8，candidate/baseline/verified均8be。新review的实时原生身份与持久launch tag匹配，证据 `.omc/stall-pin-live-after-release.json`。

因此本轮候选传递缺陷已部署且真实自动调用链验收通过；旧模型超时已自然恢复，不宣称Provider根因被修复。业务review正在运行，wrapup/PR/业务验收/整个Workflow完成仍未宣称通过。

结案时严格制品attestation因59份新字节码显示unknown，写入者未确认；源码没有漂移。已从原d93提交重新生成git archive（摘要与原制品一致），先逐文件/执行位核对，再使用现有verify_snapshot清除缓存，attestation恢复d93。没有因此再重启服务或改业务代码；保留 `.omc/stall-pin-final-attestation.json` 原unknown及缓存时间证据，不将首次结案检查说成成功。

随后缓存再次生成（67份），单次清理不能维持只读制品。再次按原archive核对/清理后，仅移除本d93版本目录及文件的写权限，保留所有执行位/内容；原模式保存用于离线回退。以uid501、显式HERDR_ROOT=d93、去掉PYTHONDONTWRITEBYTECODE保护调用真实CLI --help成功，缓存仍0且attestation稳定d93；不改源码、不再重启。证据 `.omc/stall-pin-release-readonly.json` / `stall-pin-final-state.json`。

最新业务状态：原test和review均以blocked结论进入superseded，replacement_pending=true；Controller正常触发返工，新实现任务 `fix-compliance-display-mask-export-probes` 已working。test确认A3展示侧缺少resultOwner掩码，可能切档首帧串旧结论；review指出非法/空JSON以及真实createObjectURL/anchor.click导出副作用回归不足。保留这些真实业务门禁，不归为模型超时或SHA漏传，也不由本HAFlow修复伪造通过。上述23:09working/completed仅为当时快照。

## 业务验收边界

NexusArchive 固定候选历史日志有 4139 tests passed，但 Task/Run/epoch 为空，不是正式下游门禁。本次原需复核保留 A1 页面顺序、A5 导出组合、A9 error/owner 分项、非法 JSON、JSON字面 null/数组、JSON纯空白、URL/anchor 副作用探针、typecheck、旧实现 RED 及正式 test/review 等未验证项。没有修改业务实现、自动合并或部署 NexusArchive。
