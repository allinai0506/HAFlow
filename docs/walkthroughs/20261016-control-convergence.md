# Workflow 自动推进控制链根因与修复（2026-10-16）

## 状态与范围

本地修复位于 `fix/workflow-control-convergence-20261016`，基线 `b4c3a11a848f139a985a25506ae65788173b4775`。独立 CoW 沙盒：`/Users/user/.herdr-controller/clones/haflow-control-convergence-20261016`。原工作区已有任务，因此不在原分支开发。本次不推送、合并、部署、重启服务或修改生产数据库。

目标是修复候选、派发、启动意图、恢复消费之间可重复触发的失配；不扩大为调度器重写，不承诺任意未知外部副作用均可自动恢复。SQLite 是运行事实权威来源，JSON 投影不作为结案依据。

## 根因结论

这是有可复现证据的系统性控制契约缺口。几个局部模块各自拒绝危险操作，但拒绝产生的事实未被下一环消费，导致安全拒绝累积为无法收敛的等待。过去修一个入口后出现另一个卡点，反映了候选完整性、启动副作用和恢复责任之间缺少贯通验证；不能简单归为 Agent 环境问题，也不能仅归为“测试不足”。

### 已观察的致错链

1. 并行实现有多个独立交付，但冻结读取单个最新发布，将 Hub 单任务提交 `06b2fa2f…` 当作整体候选；Collector 未被覆盖。之后才出现联合候选 `68e5c5ca…`。
2. 旧测试启动在 22:38:34 已写 allocating；22:41:21 候选变化，22:41:42 启动前置校验正确拒绝旧候选。失败返回没有结束尚未分配资源的自身意图，旧 allocating 阻挡新授权。
3. 22:41:58 review 启动遇到锁冲突。退出码不足以证明无副作用，旧消费者统一归为 unknown；生产者没有类型化、按 attempt 绑定的缺席证据供同步与重启恢复消费。
4. 与当前候选相符的外部完成测试未被尚未启动的 waiting 责任接管；任务结果与派发责任各自保持有效状态，却不能共同结案。
5. 对同一个等待状态重复 claim 仍增加版本和事件，单工作流约 11,858 条等待事件，10 月 1 日以来九个工作流约 36,699 条。它放大噪声与读写负担，但不是前四项死锁的唯一原因。

以上来自本次先前诊断阶段的 SQLite、Controller 日志及任务事实快照；不是当前实时状态声明。其他操作者随后轮换至 `c9dee79a…` 并通过 review 的进展，不归因于本沙盒修复。原始诊断证据路径记于沙盒 `.omc/control-convergence-context.json`。

### 为什么局部测试会漏过

原有测试分别验证冻结、拒绝、等待和登记，却未覆盖“多交付 → 候选变化 → 已写 intent → 前置拒绝 → 重启 → 新授权推进”的组合；也未要求同步返回与后台恢复读取同一持久回执。退出码、已登记 Task 和未产生资源的失败属于不同证据等级，混为一类会导致误重发或永久等待。

## 实现与约束

| 边界 | 修复 | 保留的拒绝条件 |
| --- | --- | --- |
| 完整交付候选 | `candidate_assembly.py` 读取当前执行的有界 Task/Run/commit/ref inventory，用 Git 对象合成，不 checkout；冻结事务复核 inventory、发布及 episode | 冲突、引用缺失、Run 变化、无当前发布授权不得覆盖显式候选 |
| CLI 启动前失败 | `herdr-task` 在资源开始前结束自身意图，释放自身 reservation，记录 `launch_rejected` | 资源边界之后的失败、不归属自身或已登记 Task 不证明缺席 |
| 派发恢复 | `node_dispatch_store.py` 同步与重启共用缺席消费者，绑定 operation/attempt/Task/Run，确认缺席后有限重试 | 旧 attempt 回执、未结案 allocating/registered、批次证据不完整保持 unknown；最多三次后转人工 |
| 完成任务接管 | `node_dispatch.py` 允许未启动且仅等待 prior-delivery-unknown 的责任接管完整匹配 PASS | execution、candidate、episode、verified SHA、状态、角色不匹配，以及显式暂缓/取消均拒绝 |
| 扫描幂等 | `_write` 比较完整有效状态，无变化不写事件或增加版本 | 真正状态、租约、attempt 等改变仍持久化 |

新增内部 `--dispatch-attempt` 由 Controller 传入并绑定意图；旧记录缺少证明时继续 fail closed，不把历史未知记录批量改为缺席。候选合成有数量和总超时预算；跨事务竞争由持久事实 CAS 防护。

## 验证证据

- 原始回归：5 failed、2 passed；失败断言分别命中上述五个机制。
- 最终专项：354 passed、12 subtests，52.38 秒。覆盖真实 CLI 独立进程锁、真实 Git 合成、SQLite 登记与读取、候选变化、Controller 重启、连续两次 busy、部分批次和错身份负例。
- 因果反证：在独立副本逐项撤回五个机制，对应业务断言五次均重新失败，均为 exit 1；不是导入或环境失败。
- 独立评审：前两轮发现并修复显式候选授权、重启消费及旧已结案意图挡住第二次 busy 的组合。第三轮 LGTM；评审者独立执行相关测试 106 passed。
- 静态检查：compileall、无扩展名 CLI 语法解析、新文件未使用导入检查、CLI launch --help 和 diff --check 通过。
- 只读 doctor：FAIL（exit 1），Herdr 与 Controller、HAFlow workspace/coordinator 均 PASS；已有 agency-agents、ctx-e2e.AwMlsT、数凭的 workspace/coordinator 不存在。未修改这些注册或现场，不把环境体检伪报为 PASS。服务指纹读取的 running SHA 为 unknown，配置指向旧 release。
- 首次完整全量：4390 passed、1 failed、2 skipped、163 subtests，703.50 秒。唯一失败是测试 HOME 被放在仓库允许读取范围，破坏了 OS 沙盒 canary 的前提；未修改基线在同类嵌套 HOME 下同样失败。将测试 HOME 移到仓库外的 `/Users` 独立目录后，该用例 1 passed（0.12 秒）。没有改源码或断言。正确环境全量重跑：**4391 passed、2 skipped、3 warnings、163 subtests passed，714.24 秒，exit 0**。警告为拒绝清理不可信临时路径的测试及多线程进程 fork 的弃用提示，日志完整保留。

专项命令：

```bash
python3.13 -m pytest -q tests/test_control_convergence.py tests/test_node_dispatch_contract.py tests/test_downstream_dispatch_contract.py tests/test_dispatch_recovery_ui.py tests/test_terminal_workflow_dispatch_recovery.py tests/test_scheduler_facts.py tests/test_scheduler_dispatch_e2e.py tests/test_workflow_repair_contracts.py tests/test_dispatch_idempotency.py tests/test_launch_reconcile_cli.py
```

全量命令为 `python3.13 -m pytest -q`；最终全量测试子进程 HOME 指向仓库外的 `/Users/user/.herdr-controller/test-homes/haflow-control-convergence-20261016` 独立目录（原专项使用沙盒 `.omc/test-home`），不使用生产状态，不启动真实 Agent 或收费模型。完整命令、日志和反证摘要保留于 `.omc/evidence/`。已中止的旧全量运行因期间修改源码而作废，不计入通过结果。

## 发布与剩余边界

当前仅本地工作树可审阅，没有提交或部署。原 workflow 的历史未知现场需要逐个核验，不能仅因安装新代码便批量清理或声明推进完成。真实 Agent 接单、原工作流业务验收及线上稳定性均未验证。

发布时应保留上一不可变 release，部署目标提交后核验服务实际路径/指纹；仅 kickstart 可能仍运行旧快照。观察当前 operation 的 claim → typed receipt / intent → Task 登记 → resolved，以及 candidate inventory 和重复扫描事件量。回滚应恢复上一 release，并保留新旧启动证据，不复活旧授权或删除未知意图。

本沙盒未登记为原生 Task，因此没有 `herdr-task verify-baseline` 回执；固定基线 diff 只作辅助审阅。没有可调用的自动复杂度评分工具，采用新文件静态检查与独立调用链审查，不伪报自动健康门禁通过。

## PR #222 Bot 审查追加修复

`chatgpt-codex-connector` 对提交 `e645a90` 指出两项有效缺陷：P1 为调用方 `--dispatch-attempt` 未进入 begin_launch_intent，而 record_intent 将其覆盖为当前轮次，迟到旧命令可能被新授权接纳；P2 为 PASS 接管复制了不完整的生命周期集合，遗漏 committed/integrated。

P1 修复贯穿直派 CLI、恢复后再次 begin、协调器提示与持久意图，事务比较精确 attempt 并保留原值。资源更新和 Task 登记也检查同一意图的 attempt，旧轮次不得更新证据或登记。历史省略 attempt 的调用最多表示第一轮 1，不允许取当前重试轮次；旧未知现场仍须核验。P2 复用 transitions.COMPLETED_TASK_STATUSES，保留 execution/candidate/episode/verified SHA/verdict/角色校验。

新增回归在修复前出现 6 个业务断言失败：committed/integrated 接管、三种 started 状态的旧 attempt、旧意图资源更新。另补真实 CLI 在 Router 前拒绝重放、当前第二轮正常登记、缺省/非法 attempt、协调器透传及旧意图登记拒绝。旧测试只覆盖回执消费与第一轮意图，未验证输入 attempt 是否保持；仅对 cleaned 状态的正例也掩盖了生命周期集合分叉。独立复查和全量结果由本次提交前补证，不能沿用上一提交通过结果。

兼容复查另覆盖历史无 attempt 的 allocating 意图：资源更新与登记均只固定解释为首轮 1。既有合法 supersedes 替代允许在尚未 claim、计数为 0 时启动，此狭窄入口也固定对应首轮 1；不根据当前重试轮次补身份。普通未启动责任仍拒绝，第二轮及以后精确比较当前授权轮次。原替代流程测试保持不变，新增首轮替代登记与错误第二轮的对照。

进一步覆盖旧实现确曾持久化的合法替代首轮 `dispatch_attempt=0`：只从持久意图恢复此历史值并固定解释为首轮 1，不接纳新命令的 0；在计数 0/1 可恢复资源与登记，计数 2 起拒绝。新增 counter 0/1/2 × 资源更新/登记/恢复入口九项对照；第三轮独立复查 LGTM，无已确认剩余阻塞。分别撤回 P1 校验与 P2 统一集合后，对应断言重新失败（exit 1），证据 `bot-counterfactual.json`。最终专项和全量结果待本轮运行结束补录。

实际发布链补证：协调器原提示硬编码 `~/HAFlow/bin/herdr-task`，而部署的 Controller 运行于不可变 release，原工作区 CLI 不一定支持新 attempt 参数。现复用已有 TASK_MANAGER，并为 list/launch 路径加引号；真实 Controller 消息回归修前失败、修后通过，独立增量复查 LGTM。期间已启动的旧版全量因源码变化中止并作废，最终验证重新运行。

最终验证：专项 384 passed、12 subtests（67.13 秒）；全量 4421 passed、2 skipped、3 warnings、163 subtests（777.48 秒，exit 0）。compileall、CLI AST、导入检查、diff --check 通过。两组撤回修复反证均为目标断言失败。当前结果绑定 `.omc/evidence/bot-source-manifest.json`，旧中止运行不计入通过。
