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
| C09 | 补派未串接旧Task，failed/blocked占位和required清单永久阻断 | 他人提交 cff721b --supersedes 修复；24Task状态 | 现场运行release已含cff721b；保留并审查，不重复实现，核对组合回归/替代链 |
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
| C22 | 评审声明pass同时承认P1权限缺陷 | review-r2报告§1与P1说明 | 源码确认：无type跳过权限，聚合所有Provider，审批Provider仅fonds过滤；违反本轮权限DoD，必须返工 |
| C23 | 真实DB门禁/MATCH语义未完成，cannot claim full green | T7 note、RealDbGatesIT与test/review任务 | DU-10人类已裁决三Provider/MATCH后续增量；只验登记与降级，不将MATCH缺口作为阻断；真实DB仍须按本轮判据验证 |
| C24 | Provider jev422 / context reducer失败、stderr缺运行归属 | controller.err尾部、observer/context记录 | 待归属；旁路失败不得阻断主状态，不宣称属于本Workflow |
| C25 | console BrokenPipe/转义警告、STARTUP WAIT、Git busy | console.err、startup8次、GIT BUSY75后成功 | 分类核对；网络断开/正常等待不冒充bug，确认重试有界和成功后解除 |
| C26 | 全局JSON投影滞后，UI/诊断可能读到不同状态 | SQLite latest_dispatch != workflows.json；tasks projection缺Task | 已证实投影差异；检查权威读取/导出刷新，不另建事实源 |
| C03b | 运行信号抹掉等待仲裁的blocked状态，重启也自动恢复working | status_history；handle_event/reconcile_task_state | 已复现；当前修复保留仲裁事实、恢复队列，强历史优先于遗留metadata，正常恢复作对照 |
| C03c | 显式恢复后合法旧BLOCKER仍可被新采样再次消费 | Sentinel当前屏幕重复读取；process_blocked_observations | 未关闭；需核对恢复epoch及程序生成的本轮评估事实，不能把旧屏幕标记当新耗尽 |
| C27 | 评估超时只终止直接shell，后代进程可继续运行 | bin/herdr-loop subprocess.run；隔离就绪子进程探针 | 隔离实验确认；处理本次创建的进程组，验证超时/中断和正常返回 |
| C28 | 外层脚本exit17/缺lint回执，仍100分converged | 隔离真实run_evaluation写EVAL_DONE=true | 已复现；完整检查runner退出、步骤回执和新鲜日志，防缺失默认成功 |
| C29 | 同工位并发eval共享日志，读到另一个进程结果并假绿 | 独立进程/受控交错探针，2 lint errors被覆盖为1 | 已复现；跨进程锁覆盖eval/init/基线写入，busy不改他人快照；专门验证中断恢复 |
| C05b | 多栈仓库根package.json优先，Java子任务误跑前端 | auto_init_task_loop；T2/T4b/T7及test-r4 GOAL命令 | 已证实命令误选；显式--test-cmd现有契约优先，缺契约必须有可恢复拒绝，不能猜Java默认 |
| C27b | auto-init的lint基线采集仍只超时直接shell | issue27b-descendant-probe.json，就绪后超时仍late-write | 已复现；单命令exec正常对照无残留；复用C27生命周期覆盖真实基线入口，独立提交 |

| C30 | 仲裁卡/人工升级提示要求blocked→rework，状态机禁止 | build_coordinator_message；_notify_blocked_human_upgrade；临时SQLite非法转换 | 已证实协议矛盾；命令须复用现有合法恢复边，不扩展状态机或force放行 |
| C31 | re-init新契约后EVAL_DONE旧绿快照仍被当本轮证据 | issue31-probe.json，初始化0但extract返回旧converged/evidence_id | 已复现；原子失效当前快照，保留历史事实；属C03c/C08恢复身份的必要依赖 |

| C32 | 分类type仅用于权限判断，授权后丢失，Tab返回全部Provider | test-r4 Blocker-1；当前冻结5d3d615 Controller/TaskQueryContext/Service | 已确认；与C22同一选择边界有关，但类型筛选独立验收，不能仅前端过滤已分页数据 |
| C33 | 前端ASC页内二次重排破坏后端DESC全局分页顺序 | test-r4 Blocker-2与test-r6 D-01；当前源5d3d615仍保留反向比较 | 已确认同源一张卡；以后端C3裁决为准，前端保留服务端分页顺序，跨页回归 |

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

C03本地验证：派发内循环协议输入含自身BLOCKER字面量，DONE-only清洗未覆盖，导致提示词回显可被Sentinel误读。共享清洗扩展DONE/BLOCKER，覆盖真实dispatch→临时SQLite及steering→临时SQLite出口，保留ORCH身份与真实Agent标记检测。修前及撤销关键修复反证均3 failed/1 passed；修后专项62 passed，全量2598 passed/145 subtests passed、0 failed/0 skipped（335.82s）；compileall/diff-check通过。仅关闭输入回显路径，历史合法blocker跨恢复epoch的重复读取仍需独立处理（C03b），没有修生产历史状态。

C04本地验证：旧实现50轮询输出25条，回归1 failed/15 passed；修后专项43 passed/9 subtests passed。基于cff721b采用他人谱系修复并重放本轮独立提交后，全量2606 passed/145 subtests passed、0 failed/0 skipped（366.29s）。同一样本仅一日志，样本版本变更再次记录；相邻真实SQLite仲裁链与supersede链专项通过。compileall/diff-check通过；无运行现场变更。重放后本轮前三提交为8708460(C01)、ac4a1ee(C21)、e2281a0(C03)，旧证据仍对应原验证基线。

C26本地验证：隐式投影/opt-in迁移统一跟随所选SQLite父目录，保留显式环境、模块、调用参数路径；默认workflow.json仍供配置读取，不冒充显式选库。修前及撤销三处关键修复均5 failed/2 passed；修后专项59 passed，最终靶向7 passed；隔离全量2613 passed/145 subtests passed、0 failed/0 skipped（355.47s）。真实临时宿主文件未被写穿；compileall、bin/herdr-task AST与diff-check通过。仅自审、未部署，损坏的生产历史JSON投影尚未重建。

C06本地验证：三步复合命令完整重定向、独立cwd、exit不跳过后续步骤且记录真实退出码。修前及撤销关键修复均5 failed；修后专项53 passed/10 subtests passed，最终靶向5 passed；全量2618 passed/145 subtests passed、0 failed/0 skipped（372.30s）。真实Bash执行边界，无外部依赖替身；compileall/diff-check通过。仅自审，未部署；外层失败/缺失回执/日志新鲜性由C28独立处理。

C28本地验证：完整单次执行契约校验runner退出、唯一有效步骤回执、所需日志新鲜性；复用程序生成GOAL的repro配置，不用缺失默认成功。失败字段程序生成并进入METRICS/EVAL_DONE/EVALUATION及耗尽BLOCKER，保留实际绿色测试数、有效历史lint基线和旧日志原件；弃用已移除契约的旧repro日志。关键修复撤销反证18 failed/2 passed，修后20靶向通过；专项76 passed/10 subtests passed；全量2638 passed/145 subtests passed、0 failed/0 skipped（366.56s）。旧outer-loop成功夹具补本轮lint/repro日志，不改断言。compileall、CLI AST/diff-check通过。仅自审，未部署；C27进程残留、C29并发生产者身份、C07单位测试数和C08业务交付门禁仍未关闭。

C29本地验证：复用既有内核文件锁，在工位评估命名空间覆盖eval/init/基线写入；竞争者明确busy退出75，不改持有者日志、配置或快照。独立进程受控交错撤销修复3 failed/2正常对照passed，修后5靶向通过；专项43 passed；全量2643 passed/145 subtests passed、0 failed/0 skipped（336.77s）。异常及持有者进程退出后可以重新获取锁。compileall、CLI AST/diff-check通过。仅自审、未部署；锁不清理后代进程，C27仍独立待修。

C27本地验证：只回收本次新session进程组，在锁释放前覆盖超时/中断/异常/正常返回，TERM有限等待、残留KILL；保留124及真实回执。核心反证5 failed/1 passed，修后7靶向passed；专项45 passed；全量2650 passed/145 subtests、0 failed/0 skipped（390.39s）。两次专项试验分别发现就绪前超时夹具问题及宿主空组EPERM差异，均保留失败日志并用就绪屏障/原生组列表修正；不放宽清理断言，新增活组EPERM失败防护。compileall/CLI AST/diff-check通过；仅自审、未部署，没有杀生产进程。不可捕获SIGKILL及主动脱离session的后代未覆盖，不能宣称无限进程树保证。

C05a本地验证：默认npm命令CI=1，保留显式任务命令。旧1 failed/1正常对照passed，修后2靶向passed；专项18 passed，全量2652 passed/145 subtests、0 failed/0 skipped（381.91s）。实际安装Vitest 3.2.6真实PTY旧命令测试通过后watch、exit124，修后exit0收敛；无TTY旧命令正常退出，保留负触发对照。compileall/CLI AST/diff-check通过；仅自审、未部署；Java任务误选根前端测试独立列C05b，基线后代残留列C27b，不冒充全部关闭。

C27b本地验证：基线采集复用受管进程组执行，覆盖命令→输出→基线写入锁边界；保留/bin/sh -c与正常已有债务，超时不制造基线；SIGTERM处理限主线程命令作用域并恢复handler。关键Task调用撤销反证4 failed/1正常对照passed，修后5靶向；专项39 passed，全量2657 passed/145 subtests、0 failed/0 skipped（360.47s）。原C27测试保留，仅将权限拒绝helper定位到其新源码位置，不改断言。初次测试导入失败、宽限期内完成观察均保留；用超过宽限期的子进程覆盖强制回收。compileall/两CLI AST/diff-check通过；仅自审、未部署，非主线程收到进程SIGTERM及不可捕获终止/脱离session仍不在保证内。

C03b本地验证：实时working及重启working/done保留内循环blocked/版本，重启恢复队列；最新blocked转换reason优先于遗留sentinel_reason，合法显式返工及普通blocked保持恢复。撤销关键Controller实现6 failed/2正常对照passed，修后8靶向；专项60 passed，全量2665 passed/145 subtests、0 failed/0 skipped（352.76s）。真实临时SQLite观察/CAS/读取/仲裁队列链，只替换外部传输；HERDR_CONTROLLER_TEST在事件路径取消，防CLI返回值冒充持久化。初版对照发现非法blocked→rework，保留为C30并改为合法恢复后返工；没有force放行。compileall/diff-check通过，仅自审、未部署。C03c显式恢复后旧屏幕标记、C30命令矛盾仍未关闭。

C30本地验证：仲裁卡及人工升级提示采用现有合法blocked→working恢复；保留failed换策略，不扩展状态机、不force。真实提示参数→本Candidate CLI→临时SQLite→读取状态及来源历史，旧及撤销2 failed/1正常对照passed，修后3靶向；专项47 passed，全量2668 passed/145 subtests、0 failed/0 skipped（358.35s）。旧文本测试明确断言实际合法命令，不放宽新执行断言。通知出口替换，未实际发升级通知；compileall/diff-check通过，仅自审、未部署。命令中的运行版本路径C20、重新初始化证据C31仍待独立修复。


C31追加验证：初始化在替换输入前原子失效当前EVAL_DONE；旧原始快照以SHA256归档至history/EVAL_DONE-<sha>.json，历史日志与BLOCKER保留但不作为本轮事实。证据ID绑定读取的单份快照SHA，避免重置后相同计数/iteration复用旧身份；同字节跨进程重启仍去重，未传SHA的旧API保持兼容。升级前后同一旧快照可能被重新观察一次，部署需核对既有ledger；不宣称此SHA证明Task/run归属。撤销关键实现4 failed/1正常对照passed，修后5靶向passed；专项80 passed/10 subtests，全量2673 passed/145 subtests、0 failed/0 skipped（385.00s）。失败初始化也不能留下旧绿证据。仅自审、未部署，C03c恢复epoch和C08业务交付仍未关闭。

C12实施中：显式完整候选SHA匹配本地分支才免远端fetch；源/独立Worker Clone两边校验，真实CoW保留源WIP；远端普通续接及所有权继续原规则。专项最终127 passed，反证7 failed/2对照passed，最终全量2682 passed/145 subtests、0 failed/0 skipped（420.72s）；compileall、CLI AST/help与diff-check通过。中间全量1 failed/2681 passed源于原fixture用ok冒充原生Git SHA，已改真实临时Git候选/远端，保留原拒绝/资源回收/不登记/审计断言与失败日志。仅自审，以独立local commit交付，未部署。初次全量2679 passed/145 subtests对应补齐不可变pin边界之前，不能替代本轮最终验收。

2026-10-01真实状态刷新：wf仍running；test-r6 cleaned但stage_verdict=blocked，与test-r4排序缺陷互证，review-r2 completed/pass不覆盖测试阻断。test-r6报告完整逐行检查，列5项未覆盖而标题称3项；结构门禁59 run中7 skipped，不写59项实际执行全绿；空表EXPLAIN rows1不证明规模门禁。原报告保留，不改写历史。

2026-10-01后续现场：C33已由既有impl-t8-sort-order-fix接手（agy working，candidate与baseline均5d3d615e07ab2ae9c5f9f04a6254026c420b282b，独立run_6a488dee5db04c979d5d1efa5066b9f7）。不创建重复业务任务或修改活跃Clone，后续核对真实提交/重验，不因启动即关闭C33。
