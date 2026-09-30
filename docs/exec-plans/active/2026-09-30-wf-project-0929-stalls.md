# wf-project-0929-01 自主闭环卡点台账

## 目标、事实源与范围
目标：完整追踪每条相关日志与 Task/Run 身份，使工作流通过真实验收自主推进到结束。先建立全量清单，再按依赖逐项修复、逐项提交、逐项验证。原工作区 ui-upgrade 与 /Users/user/HAFlow 有其他任务产物，不覆盖。隔离分支 fix/wf-project-0929-stalls，基线 4d8e177。只读运行状态；本轮交付上限 local commit，未自动合并、部署、重启或触发收费模型。

权威来源：~/.herdr-controller/state.db 只读事务快照（24 Task），全体服务日志、各 Task clone 的 .herdr-loop 日志、共享 notes.jsonl、运行配置、已有 recovery 记录。tasks.json/workflows.json 是可能滞后的投影，不能代替 SQLite。

证据目录：/Users/user/.sandboxes/wf-project-0929-evidence-0930。原始日志保留 SHA256 与行号，含完整上下文，不进入 Git。初次快照 controller.out.log 共 645251 行、直接关联 5656 行；sentinel 4584 行、关联 4123 行；console.out 19325 行、关联 282 行。stderr 没有统一时间/Workflow ID，不能把共享错误全归本工作流；无归属项保留 UNKNOWN。统计是机械扫描覆盖，不冒充每行已完成根因闭环。

## 规格与不变量
- 不伪造 pass、不把评分等同验收，不降低原需求 DoD。
- 每条 Run 证据只归其 Task；重试/补派必须保留替代谱系。
- 实现交付、Candidate 冻结、独立测试、独立评审、收尾分别满足自身门禁。
- 外部失败须有有限恢复路径；不支持的能力不能无限轮询。
- 一个卡点一个提交；所需根因依赖允许同提交，但禁止无关重构。
- 验收链包含真实 CLI/Controller → 核心 → 临时持久化 → 读取；生产状态恢复需另列真实证据。

## 问题卡片与修复顺序
状态区分：历史恢复待复核 / 已证实待修 / 待验证风险 / 正常保护 / 本地验证，均不等同线上关闭。

| ID | 卡点 / 触发与后果 | 证据 | 当前结论 / 下一验证 |
|---|---|---|---|
| C01 | 绿色测试摘要 + exit124，仍100分/converged/DoD满足 | test-r4 test.log/metrics；evaluator.calculate_metrics | 已复现；第一修复：退出成功是收敛必要条件，保留真实用例数 |
| C02 | 普通插话不支持，pending不退出，每轮重发失败 | 2881 steering.steer_delivery_failed，soft_steer_not_supported | 已证实；能力失败应持久化为需处理而非无限重试，不冒充送达 |
| C03 | blocker仍在屏幕，blocked被working恢复，再被同标记阻塞 | review-r2 status_history，blocked_marker_observed 8230/8236 | 已证实症状；区分新工作与旧标记、恢复epoch，禁止靠普通working抹除阻塞 |
| C04 | stale sample用pop去重，隔轮重报 | controller.out 544条 BLOCKED OBSERVATION STALE | 源码缺陷；get比较并在真正恢复时清理，测试连续50次仅一日志 |
| C05 | 默认npm test进入watch，Java/评审任务跑无关全量前端 | test-r4 EVALUATOR.sh/test.log；T3/T4a历史修正 | 已证实；命令由任务契约决定，非交互运行；不得编造Java默认命令 |
| C06 | test命令cd Java后lint在Java目录执行；命令串未完整重定向 | review-r2 EVALUATOR.sh/test.log/lint.log | 待边界实验；每一步独立cwd、完整日志、真实exit |
| C07 | Maven -q无摘要，被泛化为1/1，不能证明实际覆盖 | review-r2 EVAL_DONE=1/1，Surefire报告 | 已证实计数不足；使用真实测试报告/可信摘要，不能把命令成功当1个测试 |
| C08 | Agent自报done但无指定交付报告、0改动 | test-r4/review-r1；human steering历史 | 已证实；产物契约在完成边界验证，评分不能代替交付 |
| C09 | 补派未串接旧Task，failed/blocked占位和required清单永久阻断 | main未提交 --supersedes 修复；24Task状态 | 他人现有修复，保留并审查；不重复实现，核对配置/替代链 |
| C10 | 显式codex/claude派发被健康/隔离门禁拒绝 | events6250/8127/8155，TOKEN_EXHAUSTED/ERROR | 保护正确；错误码empty_review_pool对实现也使用需修；恢复路由选实际合格Agent，不擅自绕隔离 |
| C11 | delivery note多条候选，test-r2/r3启动被拒 | test_baseline_rejected8129/8131 | 门禁正确；唯一交付登记需由主控自动收敛，旧历史不能删除来凑pass |
| C12 | 本地Task分支不存在origin，--onto派发失败 | controller.out643878/643921 | 已证实；核对launch和candidate快照边界，临时Git无网络回归 |
| C13 | onto==base被判无新提交，反复退回协调器 | DIRECT DISPATCH CANDIDATE EMPTY 10次 | 待验证；SHA身份与差异基线不可混淆，尤其文档节点与已合流节点 |
| C14 | CoW从worktree复制.git指针，Clone共享HEAD/index | recovery20260930-103536与既有恢复文档 | 历史已修复；逐Task检查独立Git元数据与branch归属 |
| C15 | 用例名FAIL被误判真实失败，耗尽内循环 | 历史evaluator修复 / 恢复文档 | 已进入main；保留定向回归，不重复打补丁 |
| C16 | runtime done未当settled导致early/busy误判 | 1223 early_done_signal、PR119恢复 | 历史已修复；区分修复前事件与当前done/working真实切换 |
| C17 | local anchor被当remote，committed无法integrate | controller.out639552..639722 | 历史已修复；T1/T6真实integrated/cleaned，核对当前实现 |
| C18 | 完成统计只看已派发Task，漏required计划与未集成 | Candidate恢复文档；freeze deferred2794 | 历史已修复；核对配置normalize、CLI/Controller读链及replacement |
| C19 | hook拒绝wrong Agent/local dev/origin dev/复杂度/Node版本 | COMMIT ERROR10次、FINALIZE耗尽2次 | 历史已恢复；未跳过hook，检查当前成果refs与source祖先；运行环境应匹配.nvmrc |
| C20 | 集成ref存在而source HEAD未合流，主控停止推进 | continuation PR124、stage wait1720 | 历史能力已合入；需现场核对是否运行最新版及obligation是否生效 |
| C21 | Queue判inner_loop_exhausted也要求agent_done，旧事件被丢弃 | QUEUE STALE12次，review-r2耗尽事件 | 已证实症状；检查event expected_status契约，防仲裁通知永久丢失 |
| C22 | 评审声明pass同时承认P1权限缺陷 | review-r2报告§1与P1说明 | 待独立源码核验；不能按文字pass越过权限DoD，有效缺陷必须返工 |
| C23 | 真实DB门禁/MATCH语义未完成，cannot claim full green | T7 note、RealDbGatesIT与test/review任务 | 产品验收依赖；不能由编排修复替代，需要固定Candidate实际测试与需求对齐 |
| C24 | Provider jev422 / context reducer失败、stderr缺运行归属 | controller.err尾部、observer/context记录 | 待归属；旁路失败不得阻断主状态，不宣称属于本Workflow |
| C25 | console BrokenPipe/转义警告、STARTUP WAIT、Git busy | console.err、startup8次、GIT BUSY75后成功 | 分类核对；网络断开/正常等待不冒充bug，确认重试有界和成功后解除 |
| C26 | 全局JSON投影滞后，UI/诊断可能读到不同状态 | SQLite latest_dispatch != workflows.json；tasks projection缺Task | 已证实投影差异；检查权威读取/导出刷新，不另建事实源 |

## 执行计划与检查点
1. 固定日志快照、归属与覆盖账本；逐卡补触发、因果、正常对照、代码边界及验证。
2. C01：RED→最小实现→专项→全量→自审→local commit。附带补齐Python3.13真实CLI缺失注解导入，这是验证入口依赖。
3. C02：先确定永久能力失败的持久状态与人工可恢复出口；临时SQLite证明重启后不重复送失败，真实成功仍可投递。
4. C03/C21：分别复现恢复抹掉仲裁事实、仲裁队列状态契约，分别提交；C04独立处理。
5. C05/C06/C07/C08：评估契约、执行隔离、实际覆盖、交付边界逐项处理，不合并为大补丁。
6. C09/C12/C13/C18/C20：审查已有修复与实际运行版本，必要缺口独立提交；不能覆盖主仓未提交修复。
7. C10/C11/C22/C23/C26：按实际门禁判据检查路由/唯一交付/权限/真实DB/读取链；不能强行放行。
8. 所有确认卡点关闭后，授权运行版本恢复需独立记录；观察 Workflow完整test→review→wrapup→completed，并验证过程不依赖人工force-pass。当前未达到此终态。

## 进度与验证
C01本地验证：旧实现同一断言10 failed，修后专项48 passed、10 subtests passed；全量2592 passed、145 subtests passed，0 failed/0 skipped（378.38s）。CLI --help/AST、compileall、diff-check通过。机械证据记录 issue01-evidence.json 在本机证据目录；自审完成，无独立评审，不标MERGE_READY。剩余卡片尚未关闭。没有生产部署/重启/运行数据改写，没有工作流完成声明。

C21本地验证：真实临时SQLite blocked observation→CAS→Controller queue→通知出口；反证恢复旧Controller为1 failed/1 passed，修后两场景通过。专项109 passed、9 subtests passed；最终隔离全量2594 passed、145 subtests passed、0 failed/0 skipped（330.41s）。两个隔离方案试运行中止，第三次发现原有测试夹具漏绑定Controller TASKS_FILE（1 failed），修正夹具并恢复变量后全量通过；没有放宽断言。Path.home与默认Controller expanduser仅在测试进程重定向至临时目录。compileall、diff-check通过；仅自审，尚未部署，历史队列事件未修复。
