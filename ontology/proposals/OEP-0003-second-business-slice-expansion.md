# OEP-0003：第二业务切片本体扩展——Console UI、Observer Findings 与 Experience Hub

状态：proposed
类型：业务扩展（新增对象/关系/规则/动作/策略/投影，全部 experimental）
范围：`ontology/` 第二切片资产，不触碰生产运行时代码与第一切片状态机
关联决策：基于 OEP-0001 候选本体与 OEP-0002 D1–D3 收敛版，扩展至三个新业务子域

## 1. Observation

HAFlow 第一业务切片（OEP-0001 + OEP-0002）已完成内核调度闭环：
- Project → Workflow → Node → Task → Gate 五层实体
- 15 状态 Task 状态机与 7 状态 Workflow 状态机
- 19 项语义冲突（SC-001～SC-019）100% 裁决收敛

然而，以下三个业务子域尚未纳入企业本体统一管理：

1. **Console UI 交互层**：前端控制台的视图状态、操作权限计算与状态机约束未在本体中建模，导致前后端状态解释脱节（`workflow_policy._validate_force_pass_params` 与本体动作契约孤立）；
2. **Observer / Trajectory Findings 旁路诊断**：Agent 故障诊断的结构化实体（`TrajectoryFinding`、`Observation`）未纳入统一本体，`upsert_trajectory_finding` 作为纯 DB 调用存在，缺乏语义化动作契约；
3. **Experience Hub 经验规则枢纽**：AI 执行教训无法以版本化 `ExperienceRule` 实体在本体层统一管理，晋升与撤销动作缺乏正式授权契约。

## 2. Evidence

**Console UI**：
- `console/herdr_factory_console.py` L37：控制台主入口
- `console/workflow_policy.py` L34：`_validate_force_pass_params`（CAS 版本校验 + 状态机守卫）
- `console/http_server.py` L73：HTTP Handler（写路由无鉴权，见 F-07、F-22）

**Observer / Findings**：
- `herdr/observer/models.py` L72：`TrajectoryFinding` 结构体（finding_key 去重合约）
- `herdr/observer/engine.py` L374：`_persist`（upsert 持久化 + fallback 读）
- `herdr/state_db.py` L4726：`upsert_trajectory_finding`
- `herdr/observation.py` L115：`Observation` 结构体 / `ObservationStore` L152

**Experience Hub**：
- `herdr/experience/models.py` L20：`ExperienceStatus` 枚举（pending_review|approved|published|rejected|revoked）
- `herdr/experience/hub_server.py` L398：`review_candidate` / L461：`revoke_rule`
- `console/http_server.py` L46：`api_experience_rules` / L58：`api_experience_review` / L66：`api_experience_revoke`

## 3. Current Ontology Gap

- `ontology/` 无任何 Observer / Finding / Observation 实体
- `ontology/` 无任何 ExperienceRule 实体与晋升/撤销动作契约
- `console_operator_projection` 为只读投影，未建模控制台操作舱上下文与允许操作集
- Observer 服务与 Experience Hub 管理员角色缺乏对应的权限策略（Policy）

## 4. Proposed Diff

### 4.1 新增对象（Objects）
| 对象 | 主键 | 证据绑定 |
|------|------|----------|
| `finding` | `finding_id` / dedup: `finding_key` | `herdr/observer/models.py:TrajectoryFinding` |
| `observation` | `observation_id` | `herdr/observation.py:Observation` |
| `console_view` | `view_id` | `console/herdr_factory_console.py:main` |
| `experience_rule` | `experience_id` | `herdr/experience/models.py:BusinessRuleExperience` |

### 4.2 新增关系（Relations）
| 关系 | 方向 | 基数 |
|------|------|------|
| `finding_evaluates_task` | finding → task | N:1 |
| `finding_references_observation` | finding → observation | N:M |
| `experience_rule_originates_from_task` | experience_rule → task | N:1 |
| `console_view_projects_workflow` | console_view → workflow_instance | 1:1 |

### 4.3 新增规则（Rules）
| 规则 | 逻辑引用 |
|------|----------|
| `finding_severity_actionable` | `herdr/observer/signals.py:detect_signals` |
| `experience_rule_promotable` | `herdr/experience/hub_server.py:review_candidate` |
| `console_action_permitted` | `console/workflow_policy.py:_validate_force_pass_params` |

### 4.4 新增动作（Actions）与双向授权
| 动作 | 绑定 | 授权策略 |
|------|------|----------|
| `publish_finding` | `herdr_core_lib:herdr.state_db.upsert_trajectory_finding` | `observer_service_policy` |
| `promote_experience_rule` | `console_http_api:api_experience_review` | `experience_hub_policy` |
| `revoke_experience_rule` | `console_http_api:api_experience_revoke` | `experience_hub_policy` |

### 4.5 新增 Connector
- `console_http_api`（http_api 类型）：operations `api_experience_review` + `api_experience_revoke`

### 4.6 新增策略（Policies）
- `observer_service_policy`：Observer 旁路服务身份，allowed_actions: `[publish_finding]`
- `experience_hub_policy`：Experience Hub admin 角色，allowed_actions: `[promote_experience_rule, revoke_experience_rule]`

### 4.7 新增投影（Projections）
- `console_cockpit_view`：控制台操作舱上下文投影（含 finding 旁路诊断视图）
- `experience_hub_context`：经验枢纽审核投影（不暴露 content 字段）

### 4.8 新增金标测试（Golden Tests）
- `finding_actionable_remediation`：critical severity + request_human → finding_severity_actionable=true
- `experience_rule_promotion`：pending_review + admin reviewer → experience_rule_promotable=true → status=approved

## 5. Breaking Change

- [x] No
- 说明：全量模型处于 `experimental` 候选阶段，无线上运行时读取 `ontology/`；第一切片状态机（`TASK_TRANSITIONS` / `WORKFLOW_TRANSITIONS`）未被触碰。

## 6. 约束纪律

- 不修改 `herdr/transitions.py`、`herdr/kernel.py` 中已合入 main 的状态机
- 不引入无证据的推测性实体或冗余包装
- 所有 action 双向授权：action.authorization.policies ↔ policy.allowed_actions 闭环已验证

## 7. 资产变化汇总

| 类别 | 第一切片基线 | 第二切片后 | 增量 |
|------|------------|-----------|------|
| objects | 14 | 18 | +4 |
| relations | 14 | 18 | +4 |
| states | 4 | 4 | 0 |
| rules | 14 | 17 | +3 |
| actions | 19 | 22 | +3 |
| policies | 4 | 6 | +2 |
| workflows | 3 | 3 | 0 |
| connectors | 5 | 6 | +1 |
| mappings | 10 | 10 | 0 |
| projections | 3 | 5 | +2 |
| golden_cases | 7 | 9 | +2 |

## 8. 机械审计验证结果

```
python3 /Users/user/.gemini/config/skills/enterprise-ontology-as-code/scripts/ontology_validate.py ontology
→ PASSED

python3 ontology/tests/audit_ontology.py .
→ Totals: MAJOR=0, MINOR=0, INFO=2（INFO 保持不变，均为 C-coverage 提示）
```

所有 E/R/B/A 检查均绿灯通过：
- E（evidence 定位）：symbol_refs_ok=184（+6）
- R（logic_ref 解析）：logic_ref_ok=17（+3）
- B（binding 解析）：bindings_ok=33（+3）
- A（authorization 双向）：无新增 MAJOR/MINOR

## 9. 后续建议（非本提案范围）

- **Console 写路由鉴权**：`/api/task/force-review` 等端点仍无鉴权（见 F-07、F-22），建议独立 ADR 决议
- **console_view 实体持久化**：当前为投影实体，建议评估是否需要 session 级持久化
- **Experience Rule 第三切片**：`sync-experience` CLI 子命令与 `ExperienceCollector` 收集逻辑可在第三切片中建模
