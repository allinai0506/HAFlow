# Agent 交付演进与 Walkthrough 归档 (docs/walkthroughs/)

> 本目录专门归档各类 AI Agent 在执行开发、重构、调优等任务后生成的 Walkthrough、交付演进说明与执行验收报告。

---

## 📁 文档收录规范

1. **命名建议**：
   - 具有明确里程碑的任务建议以日期或工单命名前缀，例如：`YYYYMMDD-<feature-or-fix-name>.md`。
   - 当前最新或全局性重构可归档为 `walkthrough.md`。
2. **内容结构**：
   - **任务目标与背景**：明确本次 Agent 执行的意图。
   - **改动范围与对比**：清晰列出文件变动、架构调整或模式演进。
   - **验证与测试数据**：提供真实的命令执行结果、测试用例通过率及日志截图。
3. **已归档文档**：
   - [`walkthrough.md`](file:///Users/user/HAFlow/docs/walkthroughs/walkthrough.md)：仓库目录架构重构与最佳实践整理 Walkthrough。
   - [`20260912-console-template-library.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260912-console-template-library.md)：HAFlow 控制台工作流模板库（页面编排）交付与实现决策记录。
   - [`20260912-agent-binary-resolution.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260912-agent-binary-resolution.md)：Agent CLI 二进制解析治本修复（执行者阵容误判"未安装"）的决策与权衡记录。
   - [`20260913-workflow-finalize.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-workflow-finalize.md)：工作流物理收尾机制（finalize / close-workflow）设计思维链、安全权衡与真实工作流端到端验证记录。
   - [`20260913-agentic-loop-five-pillars.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-agentic-loop-five-pillars.md)：自主循环五大支柱架构设计与落地报告。
   - [`20260913-phase1-kernel-control-primitives.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-phase1-kernel-control-primitives.md)：通用底座阶段一：内核控制原语与检查点机制。
   - [`20260913-phase2-steering-and-intervention.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-phase2-steering-and-intervention.md)：通用底座阶段二：双轨干预与工位插话网格 (Steering Mesh)。
   - [`20260913-phase3-telemetry-projection-engine.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-phase3-telemetry-projection-engine.md)：通用底座阶段三：语义提炼与白盒投影引擎 (Projection Engine)。
   - [`20260913-phase4-dynamic-config-and-mcp-mesh.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-phase4-dynamic-config-and-mcp-mesh.md)：通用底座阶段四：通用配置驱动与受控 MCP 能力容器。
   - [`20260913-phase5-universal-studio-ui.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-phase5-universal-studio-ui.md)：通用底座阶段五：通用人机对等协同工作舱 (Universal Studio UI)。
   - [`20260913-universal-runtime-e2e-dogfooding.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-universal-runtime-e2e-dogfooding.md)：通用底座全链路端到端集成实操与自动化演练报告。
   - [`20260913-checkpoint-store-v2-sqlite.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260913-checkpoint-store-v2-sqlite.md)：Checkpoint Store V2：嵌入式 SQLite 状态引擎、单事务原子快照、图谱谱系追踪与时间旅行分叉。
   - [`20260928-pr108-selective-reverification.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260928-pr108-selective-reverification.md)：Selective Reverification v1 —— 候选轮换后按可证明证据选择性跳过重复验证；含四轮评审闭环与变异验证记录。
   - [`20260929-pr110-selective-replan.md`](file:///Users/user/HAFlow/docs/walkthroughs/20260929-pr110-selective-replan.md)：Selective Replan v1 —— 门禁 blocked 后只重做被 Verifier 显式点名的实现 Task 谱系（A 保留 / B → B-r2 / C 保留）；含 Fail-Closed 全有或全无校验、episode 身份、6/6 变异验证与评审来源披露。
