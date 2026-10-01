# wf-project-0929-01 自主闭环卡点台账

## 目标、事实源与范围
目标：完整追踪每条相关日志与 Task/Run 身份，使工作流通过真实验收自主推进到结束。先建立全量清单，再按依赖逐项修复、逐项提交、逐项验证。原工作区 ui-upgrade 与 /Users/user/HAFlow 有其他任务产物，不覆盖。隔离分支 fix/wf-project-0929-stalls，初次基线4d8e177，后采用他人谱系修复并重放至cff721b；本轮首15提交固定5f69f072复核。只读运行状态；本轮交付上限 local commit，未自动合并、部署、重启或触发收费模型。

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

| C26b | 缺失迁移配套路径被变成None，回退宿主输入 | A独立复现；主控四类TEMP迁移/文件读取，issue26b-red.log | 本地验证：保留已解析路径；旧及反证4 failed/8 passed；12靶向/64相邻/2687全量+145子测试绿；独立复审无此项阻断，未部署 |
| C27c | 原生CLI init基线仍在锁外且未受管；Task初始化/采集也分离 | A/B/C/D各自原生并发探针；A/D另有SIGTERM晚写 | 本地验证：整个reset→采集→发布同一锁；旧baseline失效；9靶向/86相邻+10子测试/2696全量+145子测试，独立复审无此项阻断；未部署 |
| C31b | current回执替换早于history发布，失败重试永久丢原字节 | A中断/C目录故障；主控故障及正常对照 | 专项已验证：先归档后失效current再替换输入；故障/中断保持旧契约与原回执；12靶向/91专项+10子测试，11失败/1正常反证；全量2730 passed/145子测试，0失败/0跳过，360.26s；未部署 |
| C21b | 旧耗尽队列跨显式恢复、普通阻塞episode误投 | A/D真实TEMP SQLite queue出口 | 专项已验证：workflow/run/status_history episode核对；metadata更新保留，旧新队列独立去重；10靶向通过；最终反证9失败1正常；相邻验证见下文；全量2790 passed/145子测试，0失败/0跳过，392.05s；本地验证未部署 |
| C28b | 自由GOAL/DoD文本伪装repro配置，绿色评估被误阻塞 | B/C独立复现；主控quoted/正常对照 | 专项已验证：GOAL末尾program typed requirement；legacy唯一完整单行配置，歧义unknown需re-init；22靶向/89专项+10子测试通过，旧19失败3正常；独立复审闭合legacy假绿；全量2780 passed/145子测试，0失败/0跳过，415.65s；本地验证未部署 |
| C28c | linter未启动exit127被抵扣为历史债务并假绿 | B原生missing_linter脚本和原子回执，主控重执行一致 | 专项已验证：reserved执行失败独立veto且拒绝采集假债务；旧污染基线不能假绿；89专项/10子测试通过，19失败/3正常对照反证；全量2718 passed/145子测试，0失败/0跳过，387.92s；仅本地提交，未部署 |
| C31c | reset后Supervisor仍把历史BLOCKER作为当前tests evidence | B临时reset→summary→collect，主控重执行一致 | 专项已验证：modern摘要单份EVAL_DONE状态/计数；仅当前耗尽BLOCKER；invalid未知不fallback，absent legacy兼容；106专项通过/反证10失败2正常；全量2742 passed/145子测试，0失败/0跳过370.78s；未部署 |
| C15b | ANSI前缀让绿色FAIL标题被识别成失败 | B解析探针、主控1/1却score95复验 | C34独立提交后精准恢复：仅test parser复用ANSI清洗，原始日志保留；新基线13定向pass/反证11失败2正常；98专项/10子测试及2758全量/145子测试通过，0失败/0跳过385.08s；旧失败全量不算PASS |
| C34 | 测试假Jev key泄漏使确定性Observer误走模型，finding迟到全量失败 | C15b首轮full1失败/2754通过；tmp DB随后已有finding；受控key/transport2失败1正常 | 独立测试隔离修复中：准确restore key、每例移除继承凭据、默认Observer模型关闭、gateway明确零transport；155扩大专项/3定向通过；全量2745 passed/145子测试，0失败/0跳过405.16s；C15b完整暂存不混改 |

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

2026-10-01四份固定5f69f07对抗审查完成，合并8项Act on；A/C归档顺序、B/C自由文本、A/B/C/D原生基线、A/D旧队列均有共识。仅OpenAI模型多样性，不声称跨厂商。其他三项由B独立证实、主控待复验；所有发现保持TEMP与代码边界，不冒充本Workflow实际触发。旧健康基线cff721b独立复跑为1 failed/2588 passed/145子测试，410.19s，Controller latch fixture读取边界未闭合，不回写为全绿。

主控重执行B三项独立脚本：missing linter exit127/baseline1仍converged=true；reset后initialized0却blocker_report=true；ANSI绿色1/1却score95且failing_tests非空。仅TEMP原生评估与解析，脚本退出0表示其缺陷断言被复现，不是产品验收通过。材料固定于外部review-b及primary-*日志。

C26b最终本地验收：四个resolved companion paths不再按exists转None；仅存在性归reader判断，显式源继续优先。旧及撤销核心StateStore实现均4 failed/8 passed；恢复精准字节后12靶向；64相邻专项；全量2687 passed/145 subtests passed，0 failed/0 skipped（378.99s）。compileall/diff-check通过，独立只读复审无此项正确性/安全阻断；仅local commit，未部署，未重建实盘投影。checkpoint负例证明宿主文件不被读取，不冒充外键拒绝的宿主行曾持久化。

C27c最终本地验收：两个公开producer统一init_loop(capture_baseline=True)，同一次锁覆盖旧基线失效、新契约、受管采集和发布；standalone init默认不执行命令且失效旧债务。Task保留既有best-effort启动及超时阶段警告，CLI超时不再返回成功。旧原生init/eval竞争、SIGTERM晚写、首锁释放时基线不匹配、旧债务继承7 failed/1正常对照；增加实际CLI受控超时后，撤销三个核心文件8 failed/1正常对照；修后9靶向、86相邻/10子测试、全量2696 passed/145 subtests，0 failed/0 skipped（384.91s）。compileall、两CLI AST、init --help、diff-check通过；独立只读复审无此项阻断。初次专项命令误写不存在测试文件，exit4/no tests，日志保留，纠正路径后验证真实执行。仅local commit，未部署。此项不解决C28c缺linter语义、C31b归档中断、C28b配置文本与C03c屏幕epoch，均另卡。

T8现场只读复核：SQLite仍working/version3，但核对native instance name/cwd/workspace/tab/pane后实际Agent idle；当前Clone有前端2文件/2测试/计划及报告WIP，HEAD仍5d3d615未提交，不重复修改。最新物理Clone日志44前端tests通过，lint4行未见失败；报告后端59 run/7 skipped，即52实际执行。GOAL仍npm test，而物理runner执行44定向tests，需结合C05b/C20核对契约；日志自身无Run戳，标physical-clone归属，不拼接为其他Run。报告测试耗时与当前log不同，不当同一次执行证据。未提交/未独立重验，不关闭C33。

C28c验收中：原始20用例18 failed/2合法exit1/2对照；扩展至22用例后恢复核心实现19 failed/3对照。最终专项89 passed/10 subtests（27.82s）。初次相邻运行与反证阶段时间交叠，不作为最终证据；反证恢复精准源字节后重跑专项，最终全量期间冻结生产源码。独立只读复审无本卡新增阻断。保留Task best-effort警告，CLI采集执行失败返回非零，旧污染baseline仍独立否决。

C28c最终：全量2718 passed/145 subtests passed，0 failed/0 skipped（387.92s），exit0；compileall、两CLI AST、diff-check通过。生产源码全量期间冻结；本卡不部署、不修实盘历史数据，其余卡继续逐项。

C31b验收中：实际旧成功/耗尽回执×history mkdir/write/replace/SystemExit故障；native CLI history被文件阻挡和重试；同hash冲突及已存在相同回执。旧11 failed/1正常，修后12 passed（6.80s），撤销核心11 failed/1正常（13.47s）；恢复精准字节后91专项/10子测试（35.20s）。独立评审遇反证暂时撤销时明确停止当前候选结论，恢复后重新只读核对，无本卡新增阻断；不将历史BLOCKER过滤C31c混入本卡。全量运行，源码冻结。

C31b最终：源码冻结全量2730 passed/145 subtests passed，0 failed/0 skipped（360.26s），exit0；compileall、两CLI AST、diff-check通过。仅本地提交，不部署、不改live状态；下一项C31c。

C31c验收中：先真实eval/reset发现历史BLOCKER误报；首次测试误以为init保留METRICS，2项自测假设错误已纠正并保留旧日志，不计产品根因。实际METRICS写入失败会留下旧显示状态/计数；另有current单次read后replace可控交错。modern摘要与extract复用原子reader，坏/薄/深层/不可读快照unknown不回退，只有absent快照legacy保留兼容；BLOCKER只在当前exhausted报告存在。原版最终12用例10 failed/2正常（6.22s），相邻106 passed（18.67s）；源恢复冻结，靶向及全量进行中。

C31c最终：恢复精准源码后22定向passed（6.38s），106相邻passed（18.67s）；全量2742 passed/145 subtests passed，0 failed/0 skipped（370.78s），exit0；compileall/diff-check通过。独立最终只读复审无本卡新增阻断，未自行重跑；仅local提交，未部署。


C34诊断：C15b首轮full1 failed/2754 passed/145子测试（332.58s）；单独gateway1 pass0.15s、Observer整模块100 pass11.49s，不能因此丢弃失败。只读failed TEMP DB后来有run-gateway verification_failure唯一行，说明迟到；前序Jev unittest cleanup未清理原本absent测试key，默认Observer模型enable，可超过drain10秒。已中断未受控重跑，1469 passed/108子测试/KeyboardInterrupt195.93s，不算全量通过。真实是否HTTP出站/花费及具体阻塞时长未证明，不宣称从未发生。C15b tracked patch/newtest完整外部暂存回HEAD，先C34独立修复。受控spy无实际请求，新3例旧/撤销2 fail1正常，相关119 pass13.40s；显式本机provider故障测试原断言保持，suite默认禁Observer模型且每例不继承host凭据。源码冻结全量中。

C34最终专项：独立评审发现outer transport spy被inner覆盖且异常被吞的盲点；修为higher judge_many spy及inner transport各自可观测计数。旧最终2 failed/1正常（0.50s）；最终字节移除gateway禁用保护1 failed/2正常（0.47s），恢复3 passed（0.40s）；119专项（14.17s）及155扩大专项（13.70s）passed。首个C34 full为修复测试盲点主动中断：514 passed/85子测试/KeyboardInterrupt72.45s，不算通过。恢复源码后重新final full冻结运行；无生产源码修改，C15b外部完整暂存。

C34最终：全量2745 passed/145 subtests passed，0 failed/0 skipped（405.16s），exit0；compileall/diff-check通过；独立最终只读复审无新增阻断，未自行重跑。无生产源码修改，仅本地提交。C15b恢复后重新验证，旧失败及中断记录不当通过。迟到finding仅独立审查只读报告，原临时库已被pytest retention移除，主控无法再次读取，不伪造root查询。

C15b恢复验收：C34已单独commit16699f8，全量2745/145子测试通过。仅恢复herdr/evaluator.py3行patch及13用例，与原外部字节一致，不覆盖C34记录。新基线target13 passed（0.73s），撤销当前HEAD核心11 failed/2正常（0.48s），恢复fixed后冻结neighbors/full。此前1失败full由C34测试key泄漏及默认model路径独立处理，旧失败/中断永不冒充通过。

C15b最终新基线验收：13靶向passed（0.73s），撤销11 failed/2正常（0.48s），98相邻passed/10子测试（20.48s）；全量2758 passed/145 subtests passed，0 failed/0 skipped（385.08s），exit0。compileall/diff-check通过，独立只读核对恢复patch/test字节与C34护栏无变动。仅local提交，未部署；原失败/中断记录保留，C34已另提交。

C28b验收中：原scanner全GOAL误把自由goal/DoD例子当repro。旧19用例17 failed/2正常；新20原生CLI例target20，撤销18 failed/2正常。独立审查发现中间rpartition reader对合法multiline repro中的假配置区块会错误false；主控完整遗漏runner→EVAL_DONE回归1 failed证明中间假绿。最终new typed末尾bool由program repro_cmd产生，raw命令不改；legacy只唯一完整单行已知字段，重复/缺/坏配置goal_configuration_invalid，不能猜最后区块。添加Bash -n合法证明与modern同literal正向实际repro，新22 targetpassed9.87s、撤销19 failed/3正常9.53s；89专项/10子测试29.13s，compileall/CLI AST/diff通过，独立最终只读复审无新增阻断。冻结源全量2780 passed/145子测试，0失败/0跳过，415.65s；legacy歧义需re-init而非pretend全部旧手写Markdown兼容；未对live clone操作。

C21b审查闭合：首版7靶向/55相邻通过；评审发现消息build期间恢复仍投旧卡，新增2项失败后补发送前再读。legacy无history只version保守身份会因metadata save丢仲裁，新增1失败/9正常后补按当前持久blocked排队，去重仍按episode。最终10靶向0.84s，精确撤回production最终9失败/1正常1.32s。首次full因评审停在598 passed/85子测试、KeyboardInterrupt/exit2（71.57s），不计PASS；首次相邻误写不存在文件exit4/no tests保留。最终源恢复冻结全量2790 passed/145子测试，0失败/0跳过，392.05s；58相邻9.05s，独立最终复审无新增阻断。跨DB到外部prompt仍非原子事务，不声称消除所有最后读到发送窗口；本卡实证关闭等待和build期间失效，以及legacy丢失可达性。未部署。
