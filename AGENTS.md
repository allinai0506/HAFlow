# HAFlow Agent Guide

> 上海共事智能科技有限公司 · 共事 · HAFlow
> 让人和多个 AI Agent 一起把事情做完。Human + Agent, in Flow.

本文件只保留仓库导航、常驻约束和验收入口；详细规则按任务风险查阅。
测试通过不等于任务完成：还必须验证真实调用链、关键不变量和失败边界。

## 0. 规则与阅读顺序

- 遵守平台与当前任务的更高优先级指令；本文件不授予额外工具或交付权限。
- 仓库红线与 S0–S8 流程以 [RULES.md](RULES.md) 为准；本文及详细协议补充执行方法。
- 操作命令参考 [CLAUDE.md](CLAUDE.md)；修改目录前检查适用的局部指引。
- 从 [Wiki 索引](wiki/index.md) 定位相关知识，只读本次调用链需要的页面和源码。
- 非简单代码修改，编码前阅读[完整工程协议](docs/engineering/agent-engineering-protocol.md)的前置检查及适用章节。
- 文档与源码不符时记录差异；源码说明实际行为，已确认的任务规格定义目标行为。
- 不用“更严格”或“更新”自行裁决授权冲突；只暂停受影响的操作并说明冲突。

## 1. 仓库地图

| 路径 | 职责 / 入口 |
| --- | --- |
| `bin/herdr-factory`、`bin/herdr-task` | Workflow / Task CLI；沿真实子命令追踪调用链。 |
| `services/herdr-controller.py` | 调度、协调器和完成事件入口；检查所有调用来源。 |
| `services/` | Worker 工位装配、Sentinel 存活巡检、Notifier 通知。 |
| `herdr/workflow.py`、`herdr/agent_router.py` | DAG 与路由决策。 |
| `herdr/projects.py`、`herdr/topology.py`、`herdr/pane_pool.py` | 项目与运行现场。 |
| `herdr/state_db.py`、`herdr/state_store.py`、`herdr/runtime_state.py` | 持久化、状态与运行身份。 |
| `herdr/trajectory.py`、`herdr/observation.py` | 执行历史与可引用证据。 |
| `herdr/decision/`、`herdr/supervisor/`、`herdr/observer/` | 判断接口、监督策略、旁路诊断。 |
| `herdr/workflow_docs.py`、`workflow_templates/` | Workflow 共享文档与 YAML 模板。 |
| `console/`、`scripts/`、`tests/` | 控制台源码、安装运维、自动化测试。 |

新能力是否存在，以当前分支源码为准；未合并 PR 和规划不是已交付事实。
Console 的规范源代码在 `console/`；部署脚本是 `scripts/install-herdr-console.sh`。

## 2. 按需文档入口

- 架构：[全局分层](docs/architecture/architecture-overview.md)、[空间模型](docs/architecture/tab-node-model.md)。
- Workflow：[使用手册](docs/guides/universal-workflow-guide.md)、[模板规范](docs/guides/template-authoring-guide.md)、[Schema](docs/product-specs/workflow-template-schema.md)。
- 路由：[Agent 策略](docs/product-specs/agent-policy-spec.md)。
- 运维：[服务管理](docs/operations/service-management.md)、[排障](docs/operations/troubleshooting-faq.md)、[深度探针](docs/operations/deep-preflight-playbook.md)。
- 命令：[CLI 参考](docs/references/cli-reference.md)；操作前核对当前实现与 `--help`。
- 诊断：[Supervisor](wiki/semantic-supervisor.md)、[Observer](wiki/trajectory-observer.md)。
- 知识：[Wiki 治理](wiki/WIKI.md)、[工程教训](docs/lessons/lessons-learned.md)、[交付记录](docs/walkthroughs/README.md)。

## 3. 开工与范围

- 先检查已有 Task、PR 和未完成现场；修复当前 PR 时继续其已授权隔离分支，不另起重复任务。
- 按 `RULES.md` 完成远端同步与 CoW / 沙盒隔离；不覆盖他人改动，不直接在主干开发。
- Task 派发使用既有 `herdr-task launch`；CoW 产出用 `verify-baseline` 验收，普通 Git 状态不能替代基线。
- 先写清：目标、非目标、真实入口、数据权威来源、适用不变量、验收测试。
- 小改动使用短计划；状态、并发、持久化、模型调用或长任务改动按详细协议展开。
- 抽象能力不匹配时先说明差异与最小兼容方案；禁止仅用测试替身补出生产环境不存在的功能。
- 单一主控负责推进；仅修改本任务需要的文件，不顺手增加新框架、依赖或未来功能。

## 4. 常驻工程约束

1. **权威来源**：复用现有状态、存储、Provider、脱敏和身份解析能力，不建立平行事实源。
2. **身份隔离**：核对 run / task / workflow 及证据归属；信息不足标记 unknown，不猜测、不跨 Run 拼接。
3. **语义分层**：Trajectory 是历史事实，Observation 是证据，Finding 是分析，ContextPack 是工作记忆。
4. **模型边界**：ID、状态、测试结果和校验值由程序产生；引用存在不等于模型结论已被证明。
5. **副作用顺序**：先验证、筛选、应用数量上限，再写证据或事件；不得为被丢弃结果制造副作用。
6. **幂等与恢复**：明确去重键、事务边界和中断后的恢复路径；重试不得永久漏写或重复记账。
7. **并发**：跨进程一致性依赖数据库 / 文件系统契约；内存锁只声明进程内保证。
8. **有界处理**：查询尽量在数据源层过滤、排序、限量；同时检查索引、内存、输入及输出预算。
9. **缓存与记忆**：缓存键覆盖相关可变输入；退出窗口不等于不存在，历史引用仍需归属与有效性检查。
10. **旁路隔离**：诊断、证据采集、压缩失败不得擅自改变执行状态；外部调用有超时，后台任务有并发上限。
11. **运行现场**：持久状态不等于实时存活；易失 Pane ID 不是所有权证明，读取现场前验证实例身份。
12. **安全**：敏感数据出站及受管证据落盘前脱敏；持久引用稳定，不依赖未来的工作目录。

例外、失败语义和测试样例见[工程协议](docs/engineering/agent-engineering-protocol.md#invariants)。
外部 Artifact 的校验回执不等于脱敏副本或不可变文件；不得把原件默认送给模型。

## 5. 按风险读取完整协议

| 变更涉及 | 编码前必读 |
| --- | --- |
| 所有非简单代码修改 | [前置检查](docs/engineering/agent-engineering-protocol.md#preflight)、[需求与验收](docs/engineering/agent-engineering-protocol.md#acceptance) |
| 身份、状态、模型判断 | [系统不变量](docs/engineering/agent-engineering-protocol.md#invariants)、[模型边界](docs/engineering/agent-engineering-protocol.md#models) |
| 持久化、幂等、文件 | [事务与恢复](docs/engineering/agent-engineering-protocol.md#persistence) |
| 线程、守护进程、异步触发 | [并发与异步](docs/engineering/agent-engineering-protocol.md#async) |
| 查询、缓存、上下文压缩 | [规模与快照](docs/engineering/agent-engineering-protocol.md#data) |
| 对外日志、证据、Provider 输入 | [安全边界](docs/engineering/agent-engineering-protocol.md#security) |
| 验证及请求评审 | [测试矩阵](docs/engineering/agent-engineering-protocol.md#testing)、[代码评审](docs/engineering/agent-engineering-protocol.md#review) |

只读文档不代表已执行；交付需提供适用规则对应的测试或可核查证据。

## 6. 测试与环境隔离

- Bug 修复优先添加可复现失败的回归测试，再做最小实现；不得放宽断言掩盖缺陷。
- 至少验证一条真实 CLI / Controller → 核心 → 持久化 → 读取链；可替换外部依赖，不替换被验收能力。
- 测试使用临时数据库、目录和受控环境；禁止写生产状态、启动真实 Agent 或隐式调用收费模型。
- 并发测试使用独立连接 / 进程与可控交错；单线程重复调用不能证明并发安全。
- 修复后重跑相关专项与全量测试；结果绑定当前源码，旧提交的通过记录不能替代本轮验证。

在已隔离的仓库根目录执行，专项命令按实际受影响测试补充：

```bash
pytest -q
python3 -m compileall -q herdr services bin tests
git diff --check
```

`bin/` 的无扩展名 Python 脚本另做对应语法 / CLI 检查；`compileall` 不能替代它们。
按 `RULES.md`、`CLAUDE.md` 补充适用验收；缺环境或权限应报告未验证，不伪造 PASS。
运维命令只在授权环境执行；不得为验收擅自重启生产服务或使用 `kill -9`。

## 7. Code Review Rules

- 对最新提交及完整受影响调用链评审；检查修复回归、权限边界和历史兼容，而非只看新增 helper。
- 缺陷必须给出位置、触发条件、实际后果和验证方法；区分已确认缺陷、待验证风险、非阻塞建议。
- 不把代码风格、未要求功能或推测性重构标为阻塞；新增阻塞项必须对应既有验收或实际正确性风险。
- 尽量一次检查完整适用矩阵；不得把旧线程未 Resolve 当作缺陷仍存在的证明。
- 自审不是独立评审；无工具或未执行测试时明确说明，禁止编造审查与测试结果。
- 发现有效缺陷，按 S6 → S4 → S5 → S6 修复；连续三轮未收敛按 `RULES.md` 升级人工处理。

## 8. 收尾与交付

- 依据 `wiki/WIKI.md` 更新受影响知识；`wiki/log.md` 只追加，禁止覆盖历史条目。
- 通用教训按[知识沉淀技能](.agents/skills/knowledge-capture/SKILL.md)归档，与代码同 PR 提交；简单改动不写重复教训。
- 使用[交付模板](docs/engineering/agent-engineering-protocol.md#delivery)报告变更、验收证据、已知问题及未验证项。
- 未解决的正确性 / 安全性缺陷不得标记 `MERGE_READY`；低风险建议单独记录，由负责人决定。
- Push、合并、部署、服务重启分别遵循本次授权；“代码可合并”不等于“允许合并”。
- 完成条件：范围内功能可用、关键不变量有证据、生产调用链已验证、回归通过、无已知阻塞缺陷。
- 规则仅保存可复用约束；细节留在协议 / Wiki / 测试，不持续膨胀本文件。
