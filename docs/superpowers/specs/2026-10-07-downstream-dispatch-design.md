# 下游节点持久派发闭环
用户已批准实施；交付隔离 working_tree，不部署。
现有 node_dispatch 仅覆盖首节点。扩展到依赖已满足的 Agent 节点，复用 SQLite 同表、CAS 租约与 launch intent 登记。下游身份绑定当前 candidate episode/SHA；发送和登记重新核对依赖与代次。直接派发与协调器派发均纳入持久责任，消息成功没有 Task 不得宣称 STAGE ADVANCED。
未知外部交付进入 awaiting_result，900 秒固定期限后 waiting_human；不凭非零返回或超时重发。发送前确定性失败与 CLI 已证明 resources_absent 才允许有界重试。历史 notified 迁移为未知交付核验；明确取消的无后继谱系输出人工范围确认，不自动替换。
不采用自动 TTL 清锁、不建立通用 Action 框架。保持 existing reuse、业务门禁、候选一致性、已运行任务和首节点模式切换契约。

实现兼容说明：已有活跃 Task 的节点由该 Task 承担执行责任，不重新排队空转通知。取消判断以当前谱系头为准，已链接历史不遮住最新取消。其回归保留任务状态、任务清单和审核不得推进的断言。
