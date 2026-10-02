# 六项工作流可靠性改进：本地交付记录

状态：六项代码实现及本地验收完成，人工复核已确认。无已知范围内阻塞缺陷；未推送、合并、部署或重启，生产验证 unknown。

## 范围与版本

任务：将监控中提出的六项优化落到软件工程契约与防复发回归。旧业务工作流已结束，本次没有创建新业务运行或改其交付 PR。

- CoW 工作树：`/Users/user/.herdr-controller/clones/workflow-reliability-1002`
- 分支：`feat/workflow-reliability-1002`
- 基线：`ef3964539d93a8d27c1f820418040b7bee6ee998`
- HEAD：`a2a2290a114a4f0982bb28885c5a9f3448a3d94b`，包含设计；实现保留在未提交工作树。
- 验收输入：56 个修改/新增的 Python、CLI、Bash/YAML 源码与测试文件，逐文件 SHA-256 映射见 `.omc/final-source-freeze.json`；组合指纹 `b050d3cb9f930087988e70d02c38b0c983c5eec2ad6edc72e59f36411b68071b`。
- 不自动复制此前监控补丁，保留当前主干的预算/Pane 复用能力；不添加新依赖、调度平台或第二事实库。

## 真实调用链与实现

| 改进 | 实际调用链及结果 | 关键验收 |
|---|---|---|
| 完成通知 | CLI report-completion → 既有 SQLite 中 completion_receipts → Controller → 既有 CAS/执行门禁/完成通知链 | task/run/epoch/bearer、24h 有效期、60 秒下限、重复消费、拒绝跨身份/关闭；无终端文本依赖 |
| 真实预检 | deep_preflight → 适配器验证响应 → Worker 实际请求/实例/交互就绪 | 空/回显/告警不 READY；认证/模型/配额分类；未知配额不编造；信任对话不自动批准 |
| 配置与派发 | workflow 验证 → 自动 direct_dispatch / 手动 CLI → 既有跨进程锁 → launch intent/资源归属 | 明确交付模式；同角色/候选/轮次去重；合法多角色与返工；资源未知时保留，launch-reconcile 仅证明不存在才释放 |
| 长任务 | checkpoint-publish/read/aggregate → Observation/events；tool-run → 有界进程 → 脱敏 Observation/执行回执 | 当前身份源查询限量；原子不可变分段、哈希/路径复核；NUL/超时/截断及后代进程清理；临时 HOME 防默认 DB 误写 |
| 版本与关闭 | 新鲜 git archive → release 内容/执行权限/缓存检查 → 完整 plist；close/reopen → 生命周期锁 → 逐资源动作日志 | 不累积参数；未知参数拒绝；缓存不自证 SHA；独立进程关闭竞争；重用 Pane 按实例区分；dry-run 不写已删除 |
| 验收绑定 | Worker 装配 → 真实 herdr-loop 执行 → EVAL_DONE → evidence/Controller → SQLite/Observation → delivery-report/关闭回执 | 真实候选/源码变化/epoch/命令/退出码/skip 保留；旧身份或失效产物 unknown；观察到的失败不消失；未有生产回执不宣称上线 |

首次派发、返工、监督 RETRY/VERIFY 和显式续签使用持久 prepared / transport_started / dispatched 阶段。准备与凭据/状态同事务，尚未发送可恢复；发送开始但缺回执为 unknown，不自动重发。显式 `renew-completion --operation-id` 保留 epoch/检查点，轮换 bearer。完成恢复扫描按公平游标推进，前 100 个阻塞声明不会永久挡住后续任务。

内部上下文、loop 和私有 launch identity 使用既有内部文件分类，不进入业务 autosave，也不让干净 Worker 候选变成 unknown。受管 tool-run 的子进程不继承操作员 HOME、状态库及投影路径；执行标识不是数据库授权。

## 不变量与风险检查

| 不变量 | 结果与真实证据 |
|---|---|
| INV-ID-01：task/run/epoch/候选/实例归属 | receipt、checkpoint、actual loop chain、原生所有权漂移负向测试；没有用当前任务字段补造旧执行身份 |
| INV-CAS-01：完成不绕过执行/验收门禁 | VERIFY 未执行的真实临时数据库反例；共用 completion.py 策略；agent_done 不是 gate pass |
| INV-RECOVERY-01：事务与外部投递分开 | 凭据/状态/准备同事务，独立崩溃窗口；传输未知保留；续签与关闭原始反例修复后发送 0 次 |
| INV-CONCURRENCY-01：跨进程所有权 | 独立进程/Event/Barrier 竞争；关闭/重開/发送共享 lifecycle lock；不在 SQLite 写事务内等待 UI |
| INV-BOUND-01：预算及公平恢复 | 当前 epoch SQL 过滤再 LIMIT；分段/工具/投影上限；任务投影截断禁止 all_verifications_passed；101 声明真实 Controller 轮询得到 0/1 |
| INV-EVIDENCE-01：实际生产者证据 | 真实 Git/Worker/loop/CLI/Controller/报告 pass/fail/skip、执行中源码变化；报告回执/哈希绑定最终关闭 |
| INV-SAFETY-01：明确外部能力与用户边界 | 未证明实例身份则保留；无 Tab/stop API 不模拟能力；凭据权限只在受信任 UID 边界；工具隔离不声称 OS 沙箱 |

异步与模型边界沿用现有 Controller/Supervisor；诊断失败不改执行成功事实。自动测试替换外部 Provider/native UI 传输，真实核心、SQLite、文件发布、原生子进程及读取方不替换。未测试真实收费模型、Agent 启动、服务加载或生产业务 E2E。

## 验证与执行结果

最终执行目录均为上述 CoW 根目录。完整 suite 使用临时 HOME，允许每个测试自己选择临时 SQLite；移除 shell 的状态路径覆盖及模型密钥，Observer 自动开销关闭。

| 检查 | 退出码/结果 | 证据 |
|---|---|---|
| 最终 `pytest -q -ra` | 0；3134 passed、2 skipped、157 subtests passed，0 failed；449.15s | `.omc/full-suite-final.log`、`full-suite-final-result.json` |
| compileall | 0 | `.omc/final-static-checks.json` |
| 全部 5 个无扩展名 CLI AST/help | 0 | 同上 |
| 8 个新增 CLI 子命令 help | 0 | 同上 |
| 安装器 bash -n、diff-check | 0 | 同上 |
| Ruff F 对照不可变基线 | 基线 36 / 当前 36，新增 0 | `.omc/health-final-{baseline,current,result}.json`，相对文件+规则+消息多重集比较 |
| 最后生命周期作者专项 | 96 passed | `.omc/delivery-lifecycle-final-green.log` |
| 独立原始生命周期反例 | 发送 0；DeliveryUnknown | `.omc/interrogate-b-renew-close-{red,green}.json` |
| 独立新增生命周期与相关回归 | 6 passed；70 passed，非可相加总数 | `.omc/interrogate-b-lifecycle-{new-green,green}.log` |

最终两项 skipped 是隔离 HOME 中没有已安装 LaunchAgent 目录的只读检查（测试行 197/219）；临时 HOME 的安装器 --no-restart、缓存与参数测试均已执行。静态检查全部退出 0；源码漂移 0，新文档链接检查通过。

初始全量 10 failed/3026 passed 已用于定位兼容夹具与实现。后来一轮全局状态路径覆盖与测试自有夹具冲突，19 failed/732 passed 后停止；修正启动环境后的全量因最后并发缺陷停止于 1076 passed。两轮中止日志保留，不作为通过证据；最终全量独立重跑。

## 评审与人工介入

Standards/Spec 及两模型 adversarial 实际审查发现并修复：不完整响应误 READY、大二进制误 unknown、旧身份完成/VERIFY 门禁、真实 EVAL 生产者字段断链、旧 epoch 占用预算、重复任务覆盖、截断泄露与残留子进程、错误检查点参数、凭据/投递崩溃窗口、过期续签与公平扫描、release 内容/字节码/执行权限、默认数据库误写、历史 Pane 日志冲突和关闭后旧投递。

独立模型为 gpt-5.6-sol 与 gpt-6-astra；作者的专项自审不算独立批准。Claude/Grok 未作为本次可调用 reviewer，不伪造其审查结果。CodeGraph 已用于导航，但未提供 numeric code-health，Ruff 基线对照不是虚构综合评分。

三轮组合修复触发 RULES S6 的人工升级。已提交[具体复核材料](workflow-reliability-1002-manual-review.md)，用户明确回复“认可该边界，继续完成本地验收”。最后修复后只做原反例与直接回归的独立 S5 验证，不把第四轮广泛评审当作跳过人工的替代。

评审证据：`.omc/standards-review.md`（历史缺陷）、`.omc/interrogate-a.md`、`interrogate-b.md`、`interrogate-a-final.md`、`interrogate-b-final.md`。历史缺陷状态以本次实际修复和新鲜验证为准；不改写旧报告。

## 尚未验证与授权边界

- 原生 API 不支持原子实例检查+prompt。发送前立即探测与生命周期锁仍不保证外部进程绝不在探测和发送之间替换实例，也不保证外部 exactly-once；这是用户已人工确认的边界。
- 同 UID 进程并非隔离安全主体；私有凭据不是敌对工位沙箱。外部 Agent 自有 MCP/shell 不能由本轮全面拦截，未提交的内存工作不能由检查点恢复。
- 没有真实运行证明，doctor 的 running_* 保持 unknown；无 Tab 实例身份或停止 API 时保留资源与恢复意图。
- 本次测试使用临时状态和受控依赖，未完成当前安装服务的新版本验收、生产重跑或新行为 UI 验收。旧工作流 Console DOM 是观察基线；截图工具超时不算 UI 通过。
- 注册 Worker CoW 任务应使用 verify-baseline；本次主控开发 CoW 没有注册或启动生产 Worker，不伪造该 Task 验收。主控开发基线由锁定 Git 对象、源码映射和专项/全量绑定。
- 允许交付：working_tree。本次不自行推送、合并、部署、重启、删除分支/沙盒，或收尾其他业务 PR。

## 知识与后续

更新 `wiki/task-lifecycle.md`、`wiki/preflight-and-health.md`、CLI 参考、Wiki 日志与工程教训 #120。工作树保留代码、设计、计划和实际证据，后续若提交 PR，知识与代码一并提交。最终全量通过，执行前后源码映射一致；本地验收完成，生产验收独立保持 unknown。

## 2026-10-02 PR 与本地重启阶段

用户追加授权“创建PR 然后重启本地服务”，允许提交、推送隔离分支、创建 PR 和本地服务更新/重启；未授权合并。前文 working_tree 边界描述的是此前阶段，生产业务验收仍单独保持 unknown。

发布前实查发现 PATH 上的旧 `/Users/user/HAFlow/bin/herdr-task` 不支持 report-completion。复用既有 workflow_docs.cli_path，将完成/续签、检查点、工作上下文和 herdr-loop 提示绑定到生成提示的同一发布快照，并进行错误 HERDR_ROOT、旧 PATH CLI 和带空格/引号路径的实际命令验证。独立专项审查无发现，61 项相关测试通过。旧固定文本断言随绝对命令合同更新，实际派发测试继续验证快照路径。

本地版本核验另发现 Console 的旧 ef39645 发布目录存在本任务之外的界面热补丁，文件既不同于 ef39645 Git 对象，也不同于 HAFlow 工作区 HEAD/脏文件。该现场已留存哈希、plist、日志位置与只读 SQLite 备份；不把目录名当作运行版本证明，不把其他界面改动吸收到本 PR。用户已确认保留当前 Console 界面；仅 Controller/Sentinel 更新到 PR 快照，Console/Notifier 原版本重启。这是明确授权的混合版本运行，Console 新版本指纹与全服务同版本验收不宣称通过。

本轮全量第一次结果：1 failed、3136 passed、2 skipped、157 subtests passed（459.41s）；失败是旧提示源码字符串断言，不能作为通过证据。修正后专项通过，最终全量与发布结果以以下追加证据为准。

最终 PR 阶段验收：pytest -q -ra 退出 0，3137 passed、2 skipped、157 subtests passed，0 failed（582.00s）。两项 skipped 仍为临时 HOME 无真实 LaunchAgent 的只读检查。22 项 inner-loop/命令绑定专项及独立两项复核通过。源码冻结指纹 `5fed3b1d1540dcdbeaf913d8da3fa99729f10462449b618afe596ad4b9e218b8`，58 个实现/测试输入执行前后一致。compileall、5 个 Python CLI AST/help、8 个新增子命令 help、安装器 Bash 语法和 diff-check 通过；Ruff F 基线/当前 36/36，新增 0。证据保留在 `.omc/pr-full-suite-final{,-result}.json`（日志为 `.log`）、`pr-source-freeze-final.json`、`pr-loop-binding-subset.log`、`pr-health.json` 与 `pr-release-command-binding-review.md`。

下一步按授权创建 PR（目标 main，不合并），随后更新 Controller/Sentinel 并按原版本重启 Console/Notifier。实际服务结果追加到本地运行证据及 PR 正文，不提前记作成功。
