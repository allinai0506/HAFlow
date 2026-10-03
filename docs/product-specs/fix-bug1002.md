# FIX_BUG1002 修复与验收记录

基线：b83e26e；独立 CoW，分支 fix/FIX_BUG1002。用户 2026-10-03 授权决策与实现。仅本地 working_tree。

## 决策与验收
- PR 创建职责放在已验收、集成后的平台交付，禁止任务预推送；保留 H-2。外部仓库helper在独立Nexus CoW修复，保持交付对象独立。
- required_task_ids 只属于同 workflow 同节点；未来尚未注册的合法任务不得被误拒。显示缺失/跨 workflow/跨节点原因，绝不静默跨域解析。
- 自动验收仅为受控变更验收，不代表评审通过。blocked 不自动完成；评审任务明确路由验收。生产写入必须绑定快照版本，证据记录于已有事件 metadata。
- Supervisor 观察模式与 enforced 模式区分；观察结论不能描述为已执行的 PAUSE。
- pane/intent 恢复必须实例归属验证。无持久完成证据不得由文件存在推定成功。
- 累计预算与并发预算按既有明确契约，不能为了复跑绕过预算。

## 任务与检查点
- [x] 1. 分支 mismatch 文案 expected/actual，避免未枚举时 commits=0 误导。
- [x] 2. AUTO ACCEPT blocked/评审任务隔离、CAS 与显式自动验收事件证据。
- [x] 3. review note kind 从 CLI→共享存储→公开读取可用。
- [x] 4. required ID 诊断与项目配置校验入口，不改变 missing candidate 判决。
- [x] 5. launch pane/intent 故障与 reconcile 归属恢复。
- [x] 6. preflight 过期探测/刷新，限制真实探针及费用。
- [x] 7. 完成恢复与 dispatch 状态、容量和多样性逐项复现。
- [x] 8. PR 交付能力与 prompt 契约、ops/current_stage 可观测性。
- [x] 9. 清理、自审、全量回归、知识同步与每项状态报告。

每个修复：先写失败回归 → 确认预期失败 → 最小实现 → 专项通过 → 记录证据。共同边界集中修复。不机械采用 handoff 建议，也不通过新增 auto verdict 枚举改变既有 pass/blocked 协议。


## 当前20项验收状态

这里的20项沿用首批规格的编号集合；“本地验收”只说明当前实现及受控测试，未部署、未重启、未在外部平台发布PR。26项本地处理和验收已完成，未标记生产交付完成。

| 项 | 当前状态 | 实现、验收与边界 |
|---|---|---|
| 1.1 | 本地修复/专项验收 | expected/actual分支与commits=unknown区分未枚举；分配context说明任务分支；`tests/test_fix_bug1002.py` |
| 1.2 | 本地能力实现/专项验收 | `create-pr`由平台在集成后发布明确SHA；同候选独立review/test pass、task-owned refs、remote/push仓库匹配；不放宽H-2。受控真实HTTP provider与临时Git远端测试，未外部发PR |
| 1.3 | 本地修复/专项验收 | known跨workflow/节点拒绝；缺失可作为未来义务但不计完成；谱系/配置诊断贯穿CLI及图投影 |
| 1.4 | 本地修复/专项验收 | blocked及review/adversarial不自动验收；agent_done/version CAS及auto证据；受控变更验收不写虚假review pass |
| 2.1 | 事实澄清/日志修复 | observe建议与enforced PAUSE区分；生产历史事件enforced=false解释该现场；enforced回归阻断完成，不把观察建议说成已执行干预 |
| 2.2 | 本地修复/专项验收 | split回执绑定terminal_id；未尝试start故障按私有身份/空agent证明rollback动态pane；prebuilt、未知、已尝试start保留 |
| 2.3 | 本地恢复闭环/专项验收 | reconcile --apply核验intent/task/run/tag/terminal_id；认证未启动pane回收、clone归档；缺token需显式认证，ABA拒绝；role mismatch准确诊断。原生无close CAS边界见下 |
| 2.4 | 本地修复/专项验收 | failed allocation立即recovery_required；同dispatch重试只在安全回收或完整absence后重claim；lease超时不自动销毁曾启动实例 |
| 2.5 | 本地修复/专项验收 | stale软故障候选先复用真实deep请求探针，只有READY+request_verified+可验证identity可选择；首选失败尝试其余兼容候选；显式Agent不暗中换身份 |
| 2.6 | 本地能力实现/专项验收 | deep-preflight --workflow-id --agent --deep --apply回写StateStore；部分刷新只更新该Agent时间/身份，全池完整结果才刷新全局时间 |
| 2.7 | 本地恢复闭环/专项验收 | authorize-completion以显式run/version/reason签发receipt-v1；不改working、不认定成功；后续report-completion与Controller消费仍核验task/run/epoch |
| 2.8 | 契约澄清/诊断补全 | max_tasks_per_node累计注册含superseded，max_concurrency当前并发；新增registered/superseded计数与来源，硬限制不变；241专项通过 |
| 2.9 | 既有能力补可发现性/专项验收 | working/paused/interrupted走同task/pane rework且核验实例；dispatch错误给出修复命令，不添加working→pending |
| 3.1 | 本地修复/专项验收 | 同节点不同dispatch_role排除非superseded历史任务和在途reservation使用的Agent；独立进程竞争验收；显式复用仍需reason及持久审计 |
| 3.2 | 本地修复/专项验收 | review纳入共享NOTE_KINDS与context；真实note-add→ledger→note-list读回 |
| 3.3 | 外部隔离修复/专项验收 | NexusArchive独立CoW的gitee-pr.sh在认证/API/push前核对当前分支与--head，按解析SHA推送；新5 passed、既有160 passed，均0 fail/skip；未应用原工作树、未外部发PR |
| 3.4 | 本地能力实现/专项验收 | node-config读取config_sha；node-config-set按expected-sha/reason校验scope，生成不可变本workflow快照，指针与audit同事务，保留共享项目原件 |
| 3.5 | 本次范围校验完成 | required_task_ids唯一非空列表及64上限、已知归属、节点变更入口校验；未知未来ID不误拒；不声称所有workflow字段完整schema已实现 |
| 3.6 | 本地读取/呈现修复 | ops卡片/图投影提供completion_issues，Console checklist显示具体原因；坏配置不得默认为completed；隔离Console真实浏览器已显示中文跨workflow原因及阻塞图；生产页面未验证 |
| 3.7 | 本地派生投影完成 | DAG提供current_nodes/ready_nodes；仅frontier唯一才派生stage；Console新增derived_current_stage保留legacy current_stage，Context标明derived_context_scope避免冒充全量；241专项通过 |

## 可操作入口

```bash
herdr-task create-pr <integrated-task> --title '<title>' --body-file <body-file> --draft
herdr-task node-config <workflow> <node>
herdr-task node-config-set <workflow> <node> --expected-sha <config_sha> --reason '<reason>' --required-task-id <task>
herdr-task node-config-set <workflow> <node> --expected-sha <config_sha> --reason '<reason>' --clear-required-tasks
herdr-task authorize-completion <task> --run-id <run> --expected-version <version> --reason '<evidence>'
herdr-task report-completion <task> --identity-file <returned-path>
herdr-task rework <task> --prompt '<repair instructions>'
herdr-task launch-reconcile --workflow-id <workflow> --node <node> --dispatch-role <role> --candidate-sha <sha> --dispatch-round <round> --apply
herdr-task launch-reconcile --workflow-id <workflow> --node <node> --dispatch-role <role> --candidate-sha <sha> --dispatch-round <round> --authorize-terminal-id <terminal_id> --confirm-agent-never-started --reason '<evidence>'
herdr-deep-preflight --workflow-id <workflow> --agent <agent> --deep --apply
```

认证pane命令不close，需另外执行--apply；操作者须有“未尝试Agent start”的证据，native agent=null/idle与产物文件不能自动证明。authorize-completion只授权声明契约，返回可执行report_command，不代替声明或验收。create-pr有外部push/API副作用，只在明确授权的交付阶段执行。

## 关键边界与证据

- 原生pane close仅接受pane_id，不提供expected terminal/CAS。HAFlow的managed动态分配/回收由跨进程全局锁串行，回收前再次检查terminal_id；人类直接native操作不受此锁约束，仍存在最终检查与close之间的人工并发竞态。不能将此称为原生原子实例close。
- 配置mutation使用本workflow不可变快照；unknown required ID是未来义务，不是已完成证明。生产配置未改。
- 预检局部刷新不能提高全局快照年龄；刷新前后binary/config/root/mode身份可验证才采用。真实探针可能发起收费模型请求，本轮测试只使用受控transport，未执行生产探针。
- 用户续修范围扩展为handoff全部26项；原20项编号保留，新增六项单列如下。不会把外部仓库修复当作HAFlow源码变更。

## 新增六项验收状态

| 项 | 当前状态 | 实现、验收与边界 |
|---|---|---|
| 1.5 | 本地修复/专项验收 | direct/Coordinator test/review只传精确candidate_sha，各自任务分支；CLI→Worker实际checkout同SHA，未知SHA拒绝，ownership及空候选检查保留；194专项及38子测试通过 |
| 2.10 | 本地修复，归入2.4 | allocation_failed即进入可恢复状态；同dispatch intent在安全回收后重claim，未知副作用不重复分配 |
| 2.11 | 本地修复，归入2.5 | Router兼容候选先真实请求预检，失败换兼容候选；显式指定身份不静默换Agent |
| 3.8 | 外部隔离修复/专项验收 | 已认证CoW integration分支复用Agent白名单/branch/worktree全归属校验；保护分支、detached、错身份仍拒绝；新8、既有merge8/stale11通过 |
| 3.9 | 本地修复/专项验收 | completed保留待commit语义；physical finalize/close-workflow检查真实Git输出，转写及close/delete动作前重核；未保存产出保留pane/clone/status，审计与退出2，workflow不标记完成；105专项通过 |
| 3.10 | 本地修复/专项验收 | local-only onto缺SHA在intent前拒绝并显示当前SHA；CLI透传完整SHA，Worker验明commit；同1.5真实Git/CLI回归 |

验收入口：`tests/test_fix_bug1002.py`、`tests/test_fix_bug1002_lifecycle.py`、`tests/test_fix_bug1002_routing.py`、`tests/test_fix_bug1002_config.py`、`tests/test_fix_bug1002_delivery.py`。生命周期最终组合57 passed；路由扩大专项114 passed，容量/Context/Console241 passed，冻结候选194 passed及38子测试，产出收尾105 passed。最终全量：**3244 passed、2 skipped、157 subtests passed，0 failed，退出0，501.18秒**。两个跳过均为隔离HOME下没有launchd agent目录的Console安装测试，未绕过业务回归。

未执行：部署、服务重启、生产workflow重跑、生产浏览器验收、真实GitHub/Gitee PR发布。当前源码自审、同模型独立Agent评审和全量回归已收敛；交付目标为两个隔离沙盒工作树。

## 冻结后复核

生命周期增量于2026-10-03 09:39:53冻结；浏览器发现绿色“无阻塞”与required红色原因冲突后，追加真实JS执行RED→GREEN并再次冻结全部源码/测试，全量使用临时HOME及测试自有数据库，模型凭据移除，记录代码文件SHA256前后对比。独立审查修复了PR push目标、门禁角色与真实verified SHA、rollback terminal ABA、多pane inventory、preflight旧workflow覆盖和转写后新增staged产出竞态；当前未发现剩余已确认阻塞。Reviewer为同模型不同agent，不称跨模型；生命周期作者复查不代替其他agent独立审查。

浏览器证据：隔离Console `http://127.0.0.1:18876` 使用真实API→graph→JS；截图 `.omc/browser-required-task.png` 显示两个blocked节点及“必需任务属于其他工作流 · foreign”；截图后最后修正文案避免同时显示绿色“无失败、无阻塞”，新文案由执行真实flowChecklist的回归验证（24专项通过），未将旧截图冒充最终文案截图。验证后关闭仅本任务临时server与TaskSpace，未重启生产。

物理收尾依赖原生close/文件系统删除，不能声称对绕过HAFlow受管锁的外部写入或人工native操作提供原子CAS。明确discard授权保持已有契约并记录审计；默认未知或未保存输出保留现场。

## 最终交付证据

主沙盒 `/Users/user/.herdr-controller/clones/FIX_BUG1002`，分支 `fix/FIX_BUG1002`，基线HEAD `b83e26e69b06f290c4e96bb99e1962bfb399982d`。原20项及最新补充6项均已完成本地处理；各项性质、入口和不变量见上表。

`pytest -q -ra` 在临时HOME、去除全局状态路径覆盖和模型凭据后执行：3244通过、2跳过、157子测试通过，退出0。日志 `/tmp/FIX_BUG1002-pytest-full-final-26-v3.log`。代码/测试内容SHA256前后相同：`2c8c6e30ddb053243b2e4fe721e756af200ca6cb7b11f4c6dfc7d4b29efb47e0`，结果绑定本轮工作树而非旧HEAD通过记录。

外部Nexus CoW `/Users/user/.herdr-controller/clones/FIX_BUG1002-nexus`，基线origin/dev `5980996a42908b86059fe9f0d6ba121bc4ee69f2`。脚本专项192通过、0失败、0跳过；测试对象四个脚本合并SHA256 `8a4ad2ab297e47a112db7c735de31364422d3af488bba6782e7f3c4c6f7f3a88`。交付说明位于该沙盒 `docs/bug-reports/2026-10-03-fix-bug1002-herdr-delivery.md`，未修改原业务工作树。

主仓库compileall、无扩展Python CLI AST/帮助入口、两沙盒diff检查及Nexus bash语法检查通过。独立评审最后对全量3个兼容失败的收敛复核109通过，真实输出保护不放宽；相关实现专项203通过。没有剩余已确认阻塞缺陷。

主沙盒为独立CoW而非注册Herdr Task，没有伪称本任务verify-baseline已运行；受控CLI/Controller→核心→临时SQLite/文件→公开读回的链路由测试验收。修改尚未提交、推送、合并、部署或重启生产；真实外部PR发布、生产重跑和最终文案新截图未执行。原生无CAS边界如上保留。

## PR阶段同步与复核（2026-10-03）

以上最终交付证据记录上一轮本地阶段；用户随后明确授权提交PR。主沙盒同步origin/main `c52a41a`，保留上游Run快照、替换任务义务及Worker推送保护；Nexus同步origin/dev `272c97915`。跨模型独立审查为gpt-6-astra，补齐预检全局锁网络等待、同Agent旧探针覆盖新结果及合法context目录误拒绝的并发/兼容回归。合法context纯目录完成物理收尾后默认保留Artifact；未知身份和Git未保存产出继续阻止清理。最新冻结候选的测试与评审证据由下方PR阶段验收记录说明。

### PR候选最终验收

最新全量 `pytest -q -ra`：3308 passed、2 skipped、157 subtests passed，0 failed，退出0，563.01秒。两项跳过均为临时HOME没有launchd安装目录的环境用例。代码/测试SHA256前后相同：`0a08a795f140bbc3d0eff09c710462146816370c83859b5874ba690f6e49adbe`；日志 `/tmp/FIX_BUG1002-pr-pytest-v2.log`。跨模型独立路由76项、收尾93项通过，作者专项121/110项通过；compileall、无扩展CLI AST、diff检查通过。评审结论MERGE_READY，用户授权交付为GitHub待审查PR；Nexus Gitee PR #1518已open且源SHA核对一致。先前3244计数及未提交陈述为本地阶段历史记录，当前验收以本节为准。未合并、部署、重启或生产复跑。

### 合并前P1审查闭环

GH PR144自动审查两条P1经真实实证成立：split返回后杀Worker遗漏持久分配回执，以及runtime topology写入改变node-config审计snapshot。补修在managed锁内持久split回执，写盘失败仍保留已分配现场；实际launch/topology与projects统一将运行拓扑保存到StateStore，teardown/reap/pool/CLI读取仅叠加所属workflow的runtime，原始snapshot/config_sha不变。234专项及12子测试通过，Worker41专项通过；独立复审和最新全量结果待本节后续记录。原生跨系统非原子边界继续明确保留。

合并候选最终验收：3312 passed、2 skipped、157 subtests passed，0 failed，退出0，552.14秒；两skip为隔离HOME无launchd安装目录。代码SHA256前后一致：`dcd26b383b723b8fe7e7b940d21a26b5ec3b2192d411b070953bda4f6813bf50`。日志 `/tmp/FIX_BUG1002-pr-pytest-v4.log`；独立reviewer gpt-6-astra 126专项+3子测试、Worker30专项通过，MERGE_READY。历史v3在681通过时因补全topology调用链主动停止，不作为最终结果。
