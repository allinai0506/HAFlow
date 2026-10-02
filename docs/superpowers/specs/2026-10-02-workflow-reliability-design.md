# 工作流可靠性六项改进设计

状态：待设计确认，尚未实现。
基线：origin/main ef3964539d93a8d27c1f820418040b7bee6ee998。
隔离分支：feat/workflow-reliability-1002。
来源：wf-project-1001-01 实际运行中的完成漏识别、预检与运行差异、重复派发、长报告无检查点、release 参数累积、重复关闭及收尾证据口径错误。
交付边界：实现及本地验证；不自动推送、合并、部署、重启服务，不调用生产模型完成自动测试。

## 1. 选择与原则

推荐增量改进既有 CLI → kernel/核心 → SQLite → Controller/读取方调用链。六个可独立验收单元；不建设新调度平台或第二事实库。终端文本保留为旧任务兼容输入，新协议任务以结构化回执为权威完成声明。

备选 A：只增强文本解析和提示。成本低，但仍依赖 viewport 与自然语言，不能满足本次目标，拒绝。
备选 B：增量协议与事务保护（推荐）。复用现有 SQLite、events、Artifact/Observation、启动适配器、工作流锁。可逐项验证与回退。
备选 C：统一替换所有 Agent 外部 CLI。范围过大、生态不受 HAFlow 控制，本次不采用。

前次监控的 f21f74a/21199c4 未合入本基线；实现时逐项核对必要依赖，不能假设已具备，不能整体复制旧 Controller 覆盖 PR134 的改进。节点并发预算/Pane 复用已在 ef39645，复用而非重复建设。

## 2. 完成回执与生命周期

新增 herdr-task report-completion，由启动装配层注入 task_id、run_id、completion_epoch 和该 Run 独有的回执凭证；凭证不出现在公开日志或上下文证据中，仅传给所属 Worker 的受管环境/私有文件。不得从 task_id 猜测身份，不以易失 Pane ID 授权。

完成回执是 Agent 的“本轮执行已结束”声明，不代表测试通过、需求满足、允许合并或部署。服务端从 Task Store 取得 workflow/node/run/version，拒绝跨任务、跨 Run、旧返工轮次、关闭工作流及过期凭证。产物引用要验证归属、内容哈希和受管路径；文本不作为成功判定。

回执与去重记录通过既有 SQLite 持久化契约原子写入；重试返回同一权威回执。Controller 通过事件消费及有界恢复查询驱动既有 agent_done → Supervisor/协调器验收链，仍保留完成最小时长、返工/干预阻断及状态 CAS。新协议不再要求终端出现标记或可读 idle。

重复提交不重复发通知；落库后通知前崩溃可由恢复扫描补投；状态更新后的旧回执不可推动新 Run。新任务若回执缺失保持执行/attention，不能自动降级为文本完成。旧任务无协议字段时保留文本判据。

范围：bin/herdr-task、services/herdr-worker.py、services/herdr-controller.py、herdr/completion.py、herdr/state_db.py、herdr/state_store.py、herdr/kernel.py；必要的小型纯策略模块按职责新增于 herdr/。

## 3. 真实可执行预检

已有 deep_preflight 真实 smoke_probe、认证/配额/Provider 分类、TTL；继续复用。区分 binary_present、request_verified、interactive_ready 三种事实。非交互请求成功不能自动等同真实工位可用。

smoke 必须包含可程序验证的响应证据；退出0但无有效响应、仅帮助/启动告警或输入回显应为 unknown，而非 READY。Agent 特定结构化输出使用已支持的适配器；不凭空增加未知 CLI 参数。

预检身份覆盖实际二进制/版本、项目及启动模式、可获取的有效模型/配置指纹；配置指纹不包含明文密钥。身份改变或 TTL 过期需重新验证；不可查询的配额仅标 unknown，不伪造剩余额度。

Worker 启动后，在派发业务提示前核验真实实例、信任对话、交互就绪；失败进入既有有界回收与路由恢复。不得自动批准信任或改全局认证。探针有总时长/重试上限，脱敏输出，不自动测试调用收费模型。

范围：herdr/deep_preflight.py、herdr/agent_router.py、herdr/agent_adapter.py、services/herdr-worker.py、相关 preflight CLI。

## 4. 派发配置与幂等

不能粗暴禁止 docs+git：有文档提交的正常任务仍需要 Git 集成。为节点明确交付模式 repository_changes 或 shared_artifacts；shared_artifacts 必须 integration_mode=none。software-development-v1 的纯报告 wrapup 使用 shared_artifacts/none，其他文档节点保留原合同。

在模板校验及 launch 共同入口进行一致性检查，先于创建 Pane、Clone 和 Router 预留。旧配置不静默猜测；纯报告明确配置迁移，存在歧义给出可恢复错误。

复用 PR134 workflow_launch_lock 与节点预算。在同一跨进程锁中重新读取计划/注册表、校验同 workflow/node/role/candidate/派发轮次是否已有任务，再预占并派发。自动与手动入口共享去重语义；合法并行角色区分，显式返工必须绑定 supersedes 和新轮次。

若创建资源后未注册即崩溃，持久 launch intent 及有界租约必须允许核对真实资源后恢复；不得仅按超时再建一个 Worker，也不得回收别人的 Pane。

范围：herdr/workflow.py、herdr/direct_dispatch.py、herdr/task_resources.py、herdr/node_capacity.py、bin/herdr-task、services/herdr-controller.py、workflow_templates/software-development-v1.yaml。

## 5. 长任务检查点与工具边界

现有 workflow checkpoint 是状态快照，不能宣称恢复 Agent 未落盘的报告或内存。新增 task/run/epoch 绑定的产物检查点入口，复用 Artifact/Observation 与 Trajectory：记录已完成步骤、下一步骤、有界摘要和受管分段文件哈希，不保存隐私推理/凭据。

提供受管报告分段发布：临时写入 → 校验/脱敏 → 原子发布不可变分段 → 追加引用；最终聚合生成新文件，不修改已引用分段。重启从最后已验证分段续做；部分文件、缺失哈希、旧 Run 或被改写分段拒绝作为恢复成功依据。

HAFlow 自己执行的工具调用校验 NUL、参数/输入输出预算，并设置超时；超时不能被当作无副作用，须记录执行不确定性及 owned 进程恢复边界。现有已受控适配器复用，避免全项目机械替换 subprocess。

外部 Agent 的自有 MCP/shell 工具无法由 HAFlow 全面强制拦截。本轮通过启动契约要求阶段性调用检查点；不能声称 HAFlow 已限制所有外部模型输出或能恢复未提交的内存工作。

范围：herdr/trajectory.py、herdr/observation.py、bin/herdr-task、services/herdr-worker.py、herdr/agent_adapter.py；必要有界工具/分段产物逻辑置于 herdr/，同步 Agent 契约文档。

## 6. 版本一致性与关闭竞争

安装器逐项 plutil 更新 ProgramArguments 的方式有累积风险；改为 plistlib 构造完整、明确的 ProgramArguments 数组及对应 HERDR_ROOT，验证所有目标 plist 后原子替换，并保留备份。不能移除未了解用途的用户参数：声明支持的参数合同，未知参数阻止更新并给出迁移说明。

安装/doctor 报告配置快照 SHA、运行进程启动 SHA、import 根、相关组件实际版本；无法确认运行态就标 unknown，不能把 plist 更新或 HTTP200 当作加载成功。先核对实际服务布局，当前 Sentinel 已指向 ef39645 快照，不能沿用安装脚本旧注释称它必定运行工作区源码。

关闭必须取得跨进程、workflow 范围的唯一所有权，不能将 running→closing 的普通状态写入当作排他锁。重复调用返回 in_progress 或同一 completed 回执，不再次删除 Pane/Tab，不重复追加 completed 事件；dry-run 不取得执行完成语义。

关闭进程崩溃后，以同一关闭操作恢复，逐资源核对所有权与已完成动作，避免回收被重新分配的 Pane；锁等待与外部操作均有界，不能在 SQLite 写事务中等待原生 UI。保留现有门禁、escalation、非 purge 和未合并交付保护。

范围：scripts/install-herdr-console.sh、bin/herdr-task、herdr/task_resources.py、herdr/state_db.py、herdr/state_store.py、herdr/kernel.py、服务启动日志/doctor 与相关运维文档。

## 7. 验收证据与最终状态绑定

复用 events、eval_results、execution_outcome 与 Artifact 作为权威事实。增加统一只读交付投影：任务/run/epoch、冻结 candidate SHA、验证命令与退出码、pass/fail/skip/unknown、产物哈希、执行模式、部署环境和 release SHA。

验收和报告引用具体回执，程序校验同 Run/候选及是否失效。candidate 改变、产物内容改变或新验证失败时不能继续复用旧绿色结论。dry-run 完成不等于动作执行；agent_done 不等于 gate pass；workflow delivered 不等于 merge/deploy；没有环境回执就保持 production_verified=unknown。

收尾 CLI 根据事实生成六步状态表与限制，Agent 可增加解释但不能替代程序事实。门禁存在失败必须保留，允许既有明确非阻塞裁决独立列出，不能以其他测试绿抵充。最终关闭回执与投影版本绑定，读者能复核历史与当前差异。

范围：herdr/execution_outcome.py、herdr/supervisor/evidence.py、herdr/workflow_docs.py、herdr/state_db.py、bin/herdr-task、相关 CLI/收尾协议文档。

## 8. 实施顺序与验收

顺序：完成回执与身份 → 预检 → 派发/配置 → 分段检查点 → 安装/关闭 → 最终证据投影。每项 RED/最小实现/GREEN，再联合专项与全量；不是一次大规模重写。

| 单元 | 必须先失败后通过的验收 |
|---|---|
| 完成 | 无终端标记/极窄 viewport 仍从真实 CLI 回执推进；跨 Run/旧 epoch/重放拒绝；落库后崩溃恢复；不自动 gate pass |
| 预检 | exit0无响应不READY；401/quota/model错误准确归类；配置漂移失效；启动信任对话拒绝派发 |
| 派发 | 独立进程同时自动/手动同角色只产生一个真实任务；多角色并行及合法返工保持；错误配置无副作用 |
| 检查点 | 分段发布各中断窗口可恢复；NUL执行前拒绝；超时记录未知副作用；内容改写与跨 Run引用拒绝 |
| 安装/关闭 | 同一 plist 重复更新不累积；参数与import根一致；独立进程竞争只有一个关闭执行者；中断后不重删外来资源 |
| 证据 | dry-run不能冒充执行；不同SHA/过期证据不能pass；真实失败/skip保留；无生产回执不声称生产验收 |

至少一条真实 CLI→核心→临时 SQLite→Controller/读取方链；仅替换外部 Provider/UI transport，不替换验收能力。并发使用独立连接/进程+Barrier/Event，禁止 sleep 猜竞争。所有自动测试在临时 DB/HOME/目录中，不启动真实 Agent、不写生产数据、不执行 launchctl。

最终执行 pytest -q、python3 -m compileall -q herdr services bin tests、无扩展名 CLI语法及帮助检查、git diff --check。结果绑定当前完整源码指纹。独立评审若不可用如实记录，不以自审冒充；不允许无强制验证仍标 MERGE_READY。

## 9. 现场与工具限制

unified-computer-use 已尝试 cua.getState 两次，分别30秒/10秒超时并重置内核；当前不能提供界面验证。后续可用时读取 Console 实际投影，失效时保留 UI未验证，不因此妨碍源码/CLI测试。

现有 ui-upgrade 未跟踪 walkthrough 保留；前次监控隔离分支及补丁保留。最新运行 plist 指向 ef39645，由外部现场操作更新，本次未重启服务。
