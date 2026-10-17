# 执行轨迹 V1 方案对抗审查

## 结论与审查边界

结论：六项修复技术可行，采用现有三层只读链路即可；当前不能签署计划门禁通过。共享上下文没有主架构师技术方案，也没有主规格工位最终确认规格，只有 requirements 对抗审查及原始任务。以下审查继承原始六项验收，不重新定义需求；是源码约束下的条件性审查，不能冒充对不存在的技术方案已完成独立评审。主方案到达后须逐项映射并关闭下述阻塞条件。

本工位 HEAD 为 812f451d50f9e5b0a4d6b0e69629106c6d7e748c。受管 WorkingContext 为 wc_78630e1879404d1e93356e7add220ec7，fingerprint 为 1814afd12d4297a954cb7bc53e1f82baf75a99ef519f55febc14ec90476d0335。运行工具路径的 22c1a96 不是代码 HEAD。未核实最新远端 main，不宣称此基线最新；集成主控在实现派发前确认版本，Worker 不切分支、不推送、不提 PR。

## 真实调用链与最小实现

```text
原生会话 → load_native_actions → 各 Agent 解析器 → project_native_actions
只读任务行和 events → read_trace_tasks/read_stored_events → assemble_trace
Task 状态历史 → project_task_history ────────────────────────┘
GET /api/task/trajectory → api_task_trajectory → query_task_trace
→ loadTaskTrace/loadMoreTaskTrace → applyTraceResponse → renderExecutionTrace
```

保留现有接口和数据结构。只在 native_session_trace.py 补事实提取，在 execution_trace.py 校验事实及展示语义，在 Console 补统一路径、请求代际与图标。拒绝新事实表、后台采集服务、Shell 解释器、全量索引、WebSocket、通用事件总线和新的图标依赖。

## 阻塞条件与最小修正

| 编号 | 位置与触发 | 实际后果 | 最小修正及关闭证据 |
| --- | --- | --- | --- |
| C1 | native_session_trace.py:521-561，Codex 输出未提供成功结果；execution_trace.py:501-504 无失败检查 | 失败或未完成补丁被称为修改代码 | 绑定 call_id 的结果证据先于分类。失败、缺结果和歧义保持工具事件。补丁仅提取受控格式路径，不输出正文；多文件部分成功不能扩展成全文件成功。失败及无结果测试均不含 file_edit |
| C2 | execution_trace.py:349-387 全部 ref 预建 known 集合 | 将未来事件及跨会话引用当成重试；标题虚构修复 | 按输入因果顺序维护同 session 已见引用；双端均为同一明确测试执行关系才称再次测试，其他有效关联称重试工具调用；无有效关系不生成 retry。重复命令不推断关联 |
| C3 | execution_trace.py:218-278，committed 状态和 SHA 各生成一条 | 时间未知且重复，状态时间冒充 Git 创建时间 | 使用真实提交事件优先，SHA 相同且证据关联才合并；状态时间仅标已提交；只有 SHA 保持时间未知。不能用当前 Git 工作区、文件 mtime 或当前时间补历史 |
| C4 | Console:10972-11010，seq 只随 Task 切换增加 | 同任务旧刷新或分页覆盖新页，失败清空旧数据 | 每次整页刷新递增代际；分页捕获代际及 cursor 并限一个在途；刷新令旧分页失效；失败保留旧列表和游标并显示错误；校验请求及响应 Task，按稳定事件 ID 去重 |
| C5 | Console:1893 自行拼路径 | 详情与轨迹读不同库，同名 Task 串源 | 单次请求捕获 state_db_path() 并贯穿任务、siblings、events；保持只读连接。双库同 task_id 不同 run_id 的 HTTP 测试验证无串源，无库不创建 |
| C6 | native_session_trace.py:346-367 找到 pinned 后仍扫描 | 旧精确会话被扫描耗尽预算 | pinned 身份验证后立即受限读取并返回可用结果，不为无关扫描延迟。找不到时才有界降级；检索自身也必须计时，不取消读取上限 |
| C7 | session_belongs:30-49 仅有独占 clone 或已记录 ID | 独占路径不能证明同 Task 重跑的 Run；投影不能将未知归属盖成当前 Run | 不放松既有 run 冲突拒绝规则；旧 Run、复用 Task、同 clone 多任务和身份文件冲突测试。无法证明 Run 时 unknown 或拒绝，禁止借“完整性”越界 |
| C8 | 自动化六类事件与真实 Agent 验证被合并 | 自造 retry_of 或本机 skip 被宣传为真实支持 | A 层受控原始格式夹具走真实解析/API/渲染；B 层逐 Agent 原生记录→API→DOM，对缺 retry 字段如实未覆盖。不能启动收费模型补样本 |

## 死锁、复杂度与性能质询

本修复没有业务写事务，不需要数据库写锁、跨进程锁或后台队列。只读 DB 连接在原生文件扫描前关闭，避免持有事务等待大文件；不要将扫描移入全局锁。刷新使用代际丢弃旧结果，无需等待旧请求完成，避免刷新与分页相互等待；禁用分页必须在成功或失败路径恢复。

解析按受限记录数线性处理，引用查找用当前会话已见索引；不能每条事件全表回扫。多文件补丁只在现有单条输入预算内解析。现有 MAX_FILES=8、MAX_SCAN=800、MAX_BYTES=8MiB、MAX_ACTIONS=2000 不得因精确匹配被取消。_find_recorded_codex 两遍遍历虽有 deadline，文件数仍可能超 800；不能把结果页上限冒充发现成本上限。先证明精确匹配成功不调用无关扫描，再测精确未命中的截止路径。deadline 不等于 OS 层硬超时，报告需说明这一限制。

前端每页至多使用现有 200 条 API 上限，不引入长轮询。生成图标由 kind 到固定 SVG/CSS 白名单映射，禁止把事件文本拼为 SVG。标题、路径、错误消息和详情仍走现有 esc。路径仅作为显示字符串规范化，不读取 workspace 判断过去行为，不执行会话里的 Shell。

## 实现拆分与依赖拓扑

推荐两个实现 Task，沿职责拆分而不是按六项机械拆六个工位。这里只规划，不创建 Task 或 Pane。

| Task | 独占文件 | 独立验收 | 依赖 |
| --- | --- | --- | --- |
| I1 事实提取与投影 | herdr/native_session_trace.py、herdr/execution_trace.py、tests/test_execution_trace.py | C1/C2/C3/C6/C7 的 RED→GREEN，Claude/OpenCode 不回归，原始输入不泄密 | 共同确认兼容事件字段与身份规则 |
| I2 Console 读取与交互 | console/herdr_factory_console.py、tests/test_execution_trace_api.py、tests/test_console_execution_trace.py | C4/C5；图标、转义、时间未知、失败保留及 HTTP 双库验证 | 可基于既有接口并行开发；六类集成必须在 I1 合入后重跑 |

文件无重叠才允许两个 Task 并发。I2 不得修改 I1 的测试夹具文件，使用自己的临时原始会话。共享 Wiki 与交付文档由集成主控串行维护，不能两边同时追加。若需改共享 helper 或接口字段，停止相关并行，先更新文件所有权，再派发。集成方向为 I1、I2 → 六类链路验收 → 三 Agent 真实验证 → 独立复审；无反向等待，不形成依赖环。

## 门禁与未决项

本工位两份审查产物完整，不代表 plan 节点两个角色的产物齐全。主架构方案、requirements 主规格是否存在由主控确认，禁止用本报告代替缺失文件。C1-C8 对应既有正确性验收，修正后可进入实现；新架构偏好和无关问题不得升级为阻塞。

本次只读源码检查及文档检查，不执行功能修复、全量测试或生产验证；不签署 MERGE_READY。专项结果与真实支持能力全部留待实现验证，禁止使用前阶段历史统计作为本轮新证据。
