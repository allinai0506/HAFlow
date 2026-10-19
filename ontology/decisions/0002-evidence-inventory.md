# 0002 Evidence Inventory — 证据清单（摘要）

证据规则：`intended` = 文档/规格宣称；`implemented` = 代码实际执行；`observed` = 运行证据（本轮未采集，不伪造）。
完整逐项证据见各 `objects/*.yaml`、`states/*.yaml`、`rules/*.yaml`、`actions/*.yaml` 的 `evidence` 字段。

## 核心对象

| 对象 | 权威来源 | 关键代码位置 |
|---|---|---|
| project | `~/.herdr-controller/projects.json` | `herdr/projects.py` L108-145, L877-949, L1011-1028 |
| workflow_definition | 模板文件 + Run 冻结快照 | `herdr/workflow.py` L104-510；`herdr/projects.py` L472-577 |
| workflow_instance | StateDB `workflows` 表（JSON 为投影） | `herdr/state_db.py` L368-377, L1491-1517；`herdr/projects.py` L551-603 |
| workflow_node | 定义 `nodes`（canonical）+ 运行 metadata | `herdr/workflow.py` L330-510；`herdr/projects.py` L288-362 |
| task | StateDB `tasks` 表 | `herdr/state_db.py` L385-402, L1591-1607 |
| agent | CLI binary 映射 + 能力矩阵 | `herdr/agent_binary.py` L19-50；`herdr/agent_adapter.py` L46-67 |
| agent_pool | `agent-pools.json` | `herdr/agent_router.py` L12, L181-201 |
| agent_reservation | `agent-reservations.json`（TTL 300s） | `herdr/agent_router.py` L16, L260-297, L809-819 |
| gate | 节点 gate 配置 + task verdict | `services/herdr-controller.py` L938-973；`bin/herdr-task` L2402-2419 |
| artifact | 读取期投影（无存储表） | `herdr/deliverables.py` L76-114, L777-821 |
| event | StateDB `events` 表 | `herdr/state_db.py` L443-456, L3205-3234, L3384-3458 |
| checkpoint | StateDB `checkpoints` 表 | `herdr/state_db.py` L428-440, L7579-7646 |

## 状态机

- Task：15 状态 / 53 条合法边，`herdr/transitions.py` L19-35；执行漏斗 `herdr/kernel.py` L133-253；CAS 与同事务事件 `herdr/state_db.py` L6658-6909
- Workflow：7 状态，`herdr/transitions.py` L89-97
- Reservation：reserved -> released / expired，`herdr/agent_router.py` L260-297
- Gate verdict：unset -> pass / blocked，`bin/herdr-task` L2402-2419

## 测试证据（只读引用，本轮未执行）

- `tests/test_state_transition_gateway.py`：合法/非法迁移、事件、CAS、force
- `tests/test_dynamic_workflow_schema.py`：模板 schema 与 retry_target/inputs 校验
- `tests/test_workflow_engine.py`：环、未知依赖、就绪计算
- `tests/test_task_bypass_gate.py`：豁免审计
