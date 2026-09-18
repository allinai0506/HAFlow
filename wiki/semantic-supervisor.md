# Semantic Supervisor（语义监督层）

> 状态：V1 已落地（观察模式）。核心原则：**Agent 做事，Herdr 运行，Supervisor 观察，Jev 判断，Policy 决定，HAFlow 控制。**

## 1. 定位：事实层之上的语义层

HAFlow 事实层（`herdr/runtime_state.py`、`transitions.py`、`state_db.py`）已经确定性地知道：进程/会话存活、runtime.status、task.status、pane/workspace 身份、测试结论、git 状态。这些问题**永不外交给任何 LLM/Provider 回答**。

Semantic Supervisor 只回答事实层答不出的语义问题：有没有**有效进展**、是否**陷入循环**、是否**偏离目标**、结果是否**基本达标**、验证是否**充分**、是否**需要人工**。

```text
Runtime Facts ──► SupervisorState(有界/脱敏快照)
                      │
              DecisionProvider(judge/score/choose)
                ├── jev(第一版, HTTP /v1/systemone)
                └── rule(确定性离线)
                      │
              Semantic Signals(9 个概率)
                      │
              SupervisorEvaluation(+delta/trend) ──► events 表
                      │
              Policy Engine(signals + facts + 裕度)
                      │
     CONTINUE/VERIFY/RETRY/REROUTE/PAUSE/FINISH/ESCALATE
                      │
     enforcement 默认关闭；开启时动作只能经编排层既有回调(rework/attention)
```

## 2. 模块地图

| 文件 | 层 | 职责 |
| :--- | :--- | :--- |
| `herdr/decision/models.py` | Core | `DecisionResult`(value/confidence/probabilities/provider/latency_ms)；Noul 无 confidence 则留 None，禁止伪造 |
| `herdr/decision/base.py` | Core | `DecisionProvider`：judge/score/choose + `judge_many`(批量) |
| `herdr/decision/registry.py` | Core | provider 名称注册表，域代码不 import 具体后端 |
| `herdr/decision/providers/jev.py` | Shell-lite | Jev HTTP 契约映射（stdlib urllib，transport 可注入）；401/429/522/529→错误分类 |
| `herdr/decision/providers/rule.py` | Core | 确定性规则 provider（离线/测试替身） |
| `herdr/supervisor/config.py` | Core | 默认值 < `~/.herdr-controller/supervisor.json` < `HERDR_SUPERVISOR_*`/`JEV_API_KEY` env |
| `herdr/supervisor/signals.py` | Core | V1 九个 noul 信号定义 |
| `herdr/supervisor/state.py` | Core | SupervisorState：截断、事件行数封顶、密钥形状脱敏、总大小预算 |
| `herdr/supervisor/evaluation.py` | Core | SupervisorEvaluation 记录 + delta/trend；持久化为 WorkflowEvent |
| `herdr/supervisor/engine.py` | Core | checkpoint→0/1 次评估；RateGate(interval/cooldown/max_calls)；provider 异常永不外抛 |
| `herdr/supervisor/policy.py` | Core | 唯一"信号→动作"决策点；确定性事实一票否决；高风险动作需阈值裕度 |
| `herdr/supervisor/harness.py` | Shell | 控制器装配：读 events、写 `supervisor_evaluation`/`supervisor_policy` 事件、enforce 时回调编排层 actions |
| `services/herdr-controller.py#supervisor_checkpoint` | Shell | 挂点（V1：三处 agent_done 转换后）；整体 try/except |

## 3. 红线（架构不变量）

1. Jev/Provider **绝不**写 task.status、runtime.status、dispatch/workflow state；
2. Supervisor 失败 ≠ Task 失败：所有入口 fail-safe，最坏只留一条 `[SUPERVISOR SKIPPED]` 日志；
3. 删除 `JEV_API_KEY` 或 `supervisor.enabled=false` 时，HAFlow 行为与引入前完全一致；
4. 每条 stdout 不调用 Provider：checkpoint 驱动 + RateGate 聚合；
5. 发送给 Provider 的只有有界结构化摘要，凭证形状内容在 state 构建期即被脱敏；
6. 监督属于 HAFlow 控制层，不是 Agent 可调用 Tool，Agent 无权开关监督。

## 4. 配置速查

见 `herdr/supervisor/config.py::DEFAULTS`。要点：`enabled`、`provider`、`interval`(默认 300s)、`cooldown`、`max_calls_per_task`(12)、`max_context_size`(8000 字符)、`thresholds.*`、`policy.min_margin`(高风险动作置信裕度)、`enforce`(V1 默认 false=只观察记录)。密钥仅 `JEV_API_KEY`/`TYPESAFE_API_KEY` 环境变量，不落盘。

## 5. V1 未实现（有意留白）

tests_completed/meaningful_change/timeout 等更多 checkpoint、REROUTE 落地、execution_strategy/model_tier 选择、Verifier Agent 自动派发、趋势算法（现为逐次 delta）、Console 监督面板可视化。
