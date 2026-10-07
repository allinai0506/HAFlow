# 下游持久派发实施计划
**Goal:** 修复下游零任务 notified 死锁。
**Architecture:** node_dispatch 纯判定 + 原 SQLite 事务 + Controller 装配；现有 scheduler 和 reuse 证据复用。
**Tech Stack:** Python 标准库、SQLite、pytest。
## Global Constraints
仅隔离工作树；不写生产状态、不启动真实 Agent、不部署；未知交付不重发；候选绑定 episode/SHA；显式取消不自动补派。
## Task 1: 身份和存储
- [x] 新增临时数据库回归：implementation 已完成时 test/review 各有独立 pending 责任；legacy notified 有期限核验；取消谱系 waiting_human。
- [x] 执行 pytest -q tests/test_downstream_dispatch_contract.py 保留 RED。
- [x] 修改 herdr/node_dispatch.py / node_dispatch_store.py，复用 scheduler 依赖判定和同快照 reuse；候选变化、上游回退拒绝旧请求，真实 intent/task SHA 强绑定。
## Task 2: Controller 接线
- [x] 下游 sweep claim 不受旧 JSON 闩阻断；所有发送路径带 operation ID；直接 launch 无登记不得 advanced，也不得跌入第二次协调器发送。
- [x] 外部发送前持久 started；返回后真实同库核验；resources_absent 仅在完整 intent 集合否定交付时允许受限重试。
- [x] Controller 集成回归覆盖重复 sweep、timeout、空登记、候选轮换、上下游变化与取消。
## Task 3: 验证与交付
- [x] 完成固定源码的隔离全量 pytest：3709 passed、1 failed、2 skipped、157 subtests；唯一失败是主仓已复现的Console存量间距检查。专项339 passed/32 subtests、compileall、CLI ast/help、diff check 已执行。
- [x] 独立只读评审（技能要求）、修复反馈并重新验证；更新 wiki/log 和四段教训，落盘新鲜证据。

## 真实回归接口
新增回归使用 `nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)`，再从 `nd.operation_for_node` 和 `read_wait_projection` 读取持久状态。缺 Task 的成功传输必须为 awaiting_result，不能输出 STAGE ADVANCED。真实 CLI 集成保留 `cli.launch_task(args)`、生产候选校验、`begin_launch_intent` 和 `store.save_task`。

## 评审与兼容修正
独立评审第一轮确认 hold 覆盖 reason 和 reuse 空责任两项缺陷，均已补事实校验/回归；第二轮及最终复查未发现新阻塞。候选复用测试替身补齐真实 intent、Task、登记后前序链接和当前 review Task 完成，原17个业务断言全部保留并通过。

全量发现另外三处旧夹具仅在模拟接口持有模板，已补齐临时SQLite配置和上游身份；独立复核确认原断言未放宽，11项定向重跑通过。
