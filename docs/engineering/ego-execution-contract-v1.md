# ego-browser 执行义务 V1

V1 记录可信调用层事实，并明确完整 E2E 是否可证明。它不强制工作流等待，不修改业务 verdict，不触发 craft 重派。

## 冻结与身份

新 Workflow 首次写入时，从配置的节点 `required_executions` 冻结 `execution_declarations_v1`；后续配置、模板或元数据更新不能替换该声明。Task 首次注册时，由服务器绑定 workflow_id、task_id、run_id、candidate_sha，保存 `execution_contract_v1` 与规范 JSON 的 SHA-256。契约模式固定为 `observe`。

```yaml
required_executions:
  - tool: ego-browser
    source: native-session-v1
```

仅支持上述单工具声明。无声明的新旧 Workflow/Task 不启用核验；历史记录不会因升级或模板变化自动启用。错误声明或缺失身份为 unverifiable，不阻止注册。Task 后续 upsert 保留原契约；元数据接口拒绝修改契约。摘要用于完整性检查，不是签名或防同 UID 篡改的安全边界。

## 真实证据来源及能力缺口

入口为宿主原生会话 `load_native_actions`，复用其 Codex/Claude/OpenCode 解析、会话 cwd、独占 clone、native session 与启动 Run 归属检查。采集上限为 10,000 个任务；原生读取使用现有预算、文件和字节限制，超出预算不能视为完整扫描。只识别带有直接 shell 原生回执标记的 `ego-browser nodejs` 命令（Codex exec_command、Claude Bash、OpenCode bash）；命令链、脚本包装、重定向、环境包装、技能读取、命令回显及 Agent 总结均不被推断为实际工具调用。Codex exec 包装器从代码文本推断出的命令仅记为未证实提示，结果为 unverifiable；stdout 中打印的退出码不作为结构化原生退出结果。拒绝执行、工具层错误或未知结果都不能证明进程启动，必须有结构化进程退出结果才能记 invoked=true。引号内 JS 分号按参数处理，外层 shell 链不采纳。

原生 shell 的调用/退出结果只能证明调用层事实，不能证明 JavaScript 真正执行浏览器动作，更不能证明服务实际加载的 SHA、全部需求场景与持久状态回读。没有这些证明时，调用返回零仍是 unverifiable。非零退出记录为 failed，表示工具调用层失败，**不表示代码测试失败**。同一次观察有成功与失败调用时，仅完整有效的 E2E 证明能满足义务。

**当前生产适配器不能产出 satisfied。** 它不解析 JS 文本来猜测动作，不采纳 Agent 报告中的“已验证”标记。纯函数 satisfied 测试注入完整可信事实，只证明核验政策，不能当作真实浏览器集成成功。当前不支持的执行方式保留能力缺口，不能把 missing 理解为“历史上绝对没用过工具”；missing 的准确含义是“可用来源中未发现受支持的可信调用记录”。

信任 HAFlow 数据库和宿主原生记录，防疏漏、自述误采纳及跨身份复用；不宣称防御同 UID 恶意修改原生记录、数据库、可执行文件或本机环境。不引入新的受信执行平台。

## 核验与存储

`verify_executions(contract, facts, available=...)` 是纯函数：

| 结果 | 含义 |
| --- | --- |
| satisfied | 同身份、同候选、同指定来源的成功执行，且可信证据完整验证浏览器动作、运行候选、E2E 结果和状态回读 |
| missing | 可用受支持来源没有匹配的可信调用事实 |
| failed | 匹配的可信工具调用有失败结果；不是业务门禁结论 |
| unverifiable | 来源不可用/不完整、契约无效、身份变化或完整 E2E 证明不足 |

核验函数不执行 I/O、启动 Agent、重试或更新状态。来源不可用时，即使输入缓存的完整事实也不能 satisfied。适配器按当前执行起点筛选原生事件，不将旧 Run、错误 Task、不同 candidate_sha 的事实绑定为本次成功。

装配链：自动裁决 `try_auto_verdict` → 复用已有 `ObservationScheduler`（进程内最多 1 个后台工人、同 Run 每 30 秒最多一次、最多 24 次） → `observe_task_best_effort` → 原生事实 → 纯核验 → `ObservationStore` 的脱敏不可变内容 → `TrajectoryLedger` 的 Observation 引用与 `execution_obligations_checked`。Observation 内容包含冻结契约、规范化调用事实与结果；不复制命令、脚本、原始输出、截图或敏感会话内容。使用 `ObservationStore.verify` 核对内容摘要；原生 source_ref 可供宿主来源回查。核验结果不进入 pass/blocked、重派或状态选择逻辑。

相同内容复用 Observation 与其原子索引回执；新内容或新索引才追加诊断结果事件，不为去重扫描历史。进程中断可能只留下原子索引引用，此时核验结果仍可在 Observation 内容中读取；并发观察允许重复诊断，不产生业务记账或重派副作用。后台满载时跳过本次采集，不增加队列或工作流等待；可用显式 CLI 补采。写证据失败时返回 unverifiable 并记录诊断日志，不伪造持久化成功；修复存储后可显式再次采集。

自动采集当前只覆盖自动裁决入口，不声称覆盖所有手工完成入口。可显式指定已有数据库和 Task 进行观察（写证据，不改任务）：

```bash
python3 -m herdr.ego_execution_evidence --db /absolute/path/to/isolated/state.db --task TASK_ID
```

无契约输出 `null`；数据库路径必须显式提供且文件已存在，不自动创建或选择生产数据库。

## 未来强制模式的权威边界

强制点应是**业务门禁被采纳为通过并持久化的事务边界**，而非 Agent 输出、完成回执声明或 `auto_verdict` 本身。普通路径在 `herdr/state/transition_task.py::transition_task` 的事务中，在写入 completed/pass 前，按相同事务快照校验 Task/Run、候选冻结事件、契约和证据完整性；不得把“执行结束”的 agent_done 直接视为义务满足。

当前不存在一个已证明覆盖所有写入者的强制原语：`save_task` 支持直接 upsert，checkpoint 恢复也会写任务，`force_pass_gate` 使用直接 SQL。因此未来 enforce 必须先统一这些状态写入者，或在其共享持久化边界施加同一检查，并明确人工授权的显式例外和审计。不完成这一步，就不能声称不被不同完成入口绕过。本 V1 不增加这些强制能力。

## 验证结论

- 自动化使用临时 SQLite、合成的原生事件文件和受控事实夹具，覆盖来源/Run/Task/候选隔离、来源不可用、技能读取、自述、调用失败、冻结兼容、Observation 完整性、CLI 读回与不重派。
- 2026-10-26 实际调用已安装的 ego-browser：在独立 TaskSpace 中打开 data URL，按钮点击前后快照显示 `0` → `1`，动作返回 `{}`，随后关闭 TaskSpace。它证明本机浏览器工具可用，也说明动作回执没有业务结果证明。
- 上述探针使用人工构造页面，不是冻结 HAFlow candidate 的业务 E2E，不计为业务集成通过。未执行真实 cleanroom Task/Run 的冻结候选 E2E；当前缺少可信浏览器动作、运行版本和业务回读的完整来源，集成结论为**不能证明完整义务满足**。
- 未修改生产任务，未部署、未重启、未启动真实 Agent。Ego Lite 提示有新版本；本任务未升级。

Evidence：`herdr/execution_obligations.py`、`herdr/ego_execution_evidence.py`、`herdr/state/workflow_task_store.py`、`tests/test_execution_obligations.py`。
