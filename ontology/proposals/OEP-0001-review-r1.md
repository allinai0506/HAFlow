# OEP-0001 评审记录 R1

- 被评审对象：提交 `1b2fe9a`（候选本体 v0.1，95 个文件，全部 experimental）
- 评审结论（对 1b2fe9a）：**REQUEST_CHANGES**
- 当前状态：R1 修正已应用于后续提交；**未批准**。R1 修正由同一代理完成，因此修正本身仍需独立复审与人工批准。
- 评审方法：
  1. 独立只读评审子代理（新上下文，未提供作者的怀疑清单），核对 44 条发现与 17 条冲突。
  2. 机械审计 `ontology/tests/audit_ontology.py`（本轮新增）：证据定位、绑定解析、授权双向、状态机边与代码矩阵、终态吸收性、CLI 覆盖。
  3. 作者人工核对：逐条读取代码行号，证据列给出位置。
- 边界：审计只证明“符号存在于引用位置附近”与“边集相等”，不证明运行时行为；行为类结论以人工核对为准。

## 1. 结论摘要

- 1b2fe9a 通过 Skill 自带 schema 校验，但存在 5 个阻断项：绑定指向不存在的入口、授权双向不一致、隐藏跳转（bypass 被写成审计迁移）、只读投影含写动作且控制台写路由未鉴权、验证记录循环。
- R1 修正后：审计 MAJOR=0、MINOR=0；绑定 28/28 可解析；连接器操作 26/26 可解析；状态机 77/77 边与代码矩阵相等；授权双向一致。
- 仍有 9 项属于设计决策（§5），不是可由证据直接修正的事实错误，需人工决定。

## 2. 发现与处置

级别：BLOCKER（阻断批准）、MAJOR（需修正或明确处置）、MINOR（描述或格式问题）。

| ID | 级别 | 发现 | 证据 | 处置 |
|---|---|---|---|---|
| B-01 | BLOCKER | 候选把 bypass 写成“经审计的状态迁移（transaction=atomic）”。实际 `bypass_task_gate` 直接 `save_task`，不经 `transition_task`；边 blocked→completed 不在矩阵中；代码中的 `suspended` 不是任务状态。 | herdr/kernel.py L832-957；herdr/transitions.py L19-35 | 已修正：建模为 out_of_matrix 写入路径，transaction 改为 non_atomic。待决策 D1。 |
| B-02 | BLOCKER | 绑定指向不存在的入口：`herdr-factory provision`、controller 的 `step_workflow`、“CAS arbitration”。 | bin/herdr-factory 子命令表 L805-886；services/herdr-controller.py 无 `def step_workflow` | 已修正：改为 `herdr-factory project`、`herdr.projects.ensure_project`、`check_workflow_stage_advance`。审计 28/28。 |
| B-03 | BLOCKER | 验证记录“Action bindings resolve: all ok”是循环验证（只比对连接器自身列表）；所引用的交叉检查脚本不在仓库中。 | 旧版 generated/validation.md；仓库内无该脚本 | 已撤回。新增可复现审计脚本 `ontology/tests/audit_ontology.py`。 |
| B-04 | BLOCKER | 控制台投影声称“只读”，却列出 transition_task、close_workflow、bypass_gate。控制台写路由（`/api/kernel/force-pass`、`/api/task/force-review`）无鉴权，监听 127.0.0.1。 | console/http_server.py L279、L290；`get_auth_token` 仅用于 experience 路由 | 候选层已修正：投影移除写动作。代码层安全缺口不属于本体修正范围，待决策 D6。 |
| B-05 | BLOCKER | 授权双向不一致：动作引用的策略未列出该动作，策略授予的动作未引用该策略。controller 被授予 reserve/release，但其调用 choose_agent 时不传 reservation_key，不写预占。 | 审计 A-not-listed、A-grant-not-cited；bin/herdr-task L3697-3705（launch 传 reservation_key）；controller L1651-1672 | 已修正：authorization 为唯一来源，策略 allowed_actions 由其派生；controller 不再授予预占动作。 |
| M-01 | MAJOR | 状态机之外存在 8 类写入路径，与 wiki 中“任何越权状态变更将被 CLI 直接拦截”（wiki/task-lifecycle.md L15）相矛盾：completed→rework（delivery-repair）、rollback 批量强制 superseded、close-workflow --force、console force-review、save_task 覆盖写、checkpoint restore 删除任务。 | state_db.py L6734-6738、L7740；kernel.py L553-571；bin/herdr-task L7274-7287；console/task_api.py L266-290 | 已记录为 out_of_matrix_writers（SC-018）。是否收敛到网关：待决策 D1。 |
| M-02 | MAJOR | 工作流初始状态：模型写 pending，但注册直接写 running；占位工作流以 pending 创建。 | herdr/projects.py L593；herdr/state_db.py L1617 | 已修正：initial=running（观测），pending 保留为矩阵声明。SC-011 待决策 D3。 |
| M-03 | MAJOR | cleanup_task 被描述为物理清理，实际为逻辑清理（保留窗格与克隆）；缺少物理删除动作；finalize 对 integrated/superseded 删除克隆；“failed 需 --force”属于 finalize。 | bin/herdr-task cleanup_task L6526-6557；purge_task L6560；clone_deletable L6720-6739；finalize --force L8958-8964 | 已修正；新增 purge_task 与 task_teardown_allowed。 |
| M-04 | MAJOR | launch_task 迁移写为 None→dispatched。实际 launch 先登记 pending，再由 dispatch 迁移到 dispatched。 | bin/herdr-task L4197、L4244、L4446 | 已修正为 pending→dispatched。 |
| M-05 | MAJOR | close_workflow 的起始状态错误：pending→closing 在矩阵中不合法（仅 --force 可过），in_progress→closing 合法却未列出。 | bin/herdr-task L7274-7287；herdr/transitions.py L89-97 | 已修正。 |
| M-06 | MAJOR | 预占状态模型错误：磁盘上不存在 expired；注册任务的预占也会被清理丢弃；原模型只写了 TTL 与显式释放两条路径；黄金用例期望 expired。 | herdr/agent_router.py L260-280（registered 分支 continue 即丢弃）、L283-297 | 已修正为 reserved→removed（三种触发）；黄金用例改为“记录不存在”。 |
| M-07 | MAJOR | record_gate_verdict、bypass_gate 的目标写为 gate，实际写入的是 task 行（stage_verdict、status）。 | bin/herdr-task set_status L2338；herdr/kernel.py L832-957 | 已修正：目标改为 task。 |
| M-08 | MAJOR | 门禁身份混合：配置按节点（workflow_id:node_id），结论按任务（tasks.stage_verdict；verdict 文件按 task_id）；结论存在双重权威（任务行与文件）。 | herdr/kernel.py bypass 段写 gate_verdict_path(task_id)；mappings/gate_verdict_file_mapping.yaml（旧） | 部分修正：权威说明已写入映射；模型拆分待决策 D2（SC-012）。 |
| M-09 | MAJOR | 门禁结论生命周期不完整：rework 与 delivery-repair 清空结论；completed 任务可在 pass 与 blocked 间改写；bypass 与 force-pass 可在任意前态写 pass。 | herdr/delivery_rework.py L102-108；bin/herdr-task L2429-2457；kernel.py L903-905 | 已修正：补充 8 条转移与触发。 |
| M-10 | MAJOR | 执行者缺失：controller 的 try_auto_verdict 运行 `herdr-task set --verdict`；recover_router_isolation_tasks 调用 supersede --allow-pending。 | services/herdr-controller.py L8302-8312、L1651-1680 | 已修正（授权与绑定）。 |
| M-11 | MAJOR | 覆盖不足：CLI 写子命令约 45 个未绑定动作（启发式分类，含少量只读名称）。典型包括 add、dispatch、halt、steer、pause、resume、rework、reap、checkpoint-restore（删除任务）、create-pr、merge-pr（外部写）、node-config-set、note-add、record-delivery；factory 侧 unregister、slot-bind、slot-unbind、step、checkpoint save/restore。 | 审计 C-coverage | 部分修正：新增 purge_task、supersede_task、force_pass_gate、rollback_workflow。覆盖范围待决策 D7。 |
| M-12 | MAJOR | 词表不合规：agent_execution.mode 使用了 references/05 之外的取值（service_account、require_human、deny_agent_escalation）。动作的 idempotency、transaction、failure_behavior 为自拟，references/04 未定义枚举。 | 审计 A-vocab；references/04、05 | 策略取值已修正为 inherit_user / service_identity。动作词表在 ontology.yaml 标注为描述性，待决策 D8。 |
| M-13 | MAJOR | 连接器不符合模板：操作为裸字符串，缺 timeout 与 retry。Phase 4 要求 Active 动作具备超时与重试策略。 | assets/templates/connector.yaml | 已结构化；timeout 与 retry 标注 unknown。候选为 experimental，本项不阻断。 |
| M-14 | MAJOR | 证据定位错误 4 处：required_outputs 注入位于 controller L7398-7420 而非 bin/herdr-task；AgentAdapterCapability 应为 AgentCapability；get_ready_nodes 调用在 controller L4972、定义在 herdr/workflow.py；遥测投影位于 herdr/projection.py L270。另有 1 处 freeze_run_definition 行号指向错误函数。 | 审计 E-symbol、E-drift（修正前） | 已修正。修正后审计 E 类 0 项。 |
| M-15 | MAJOR | 任务类型三套词表：节点 default_task_type（explore/plan/code/test/review/docs）、launch --task-type（fix/feat/...）、domain 注册表 _SOFTWARE_TASK_TYPES（code/test/fix/bugfix/...）。 | 节点 schema（docs/product-specs/workflow-template-schema.md L53）；bin/herdr-task L8823-8835；herdr/domain/registry.py L25 | 记录为 SC-013。待决策 D4。 |
| M-16 | MAJOR | fix-loop 可作废集合缺少 paused、interrupted，但矩阵允许二者转为 superseded。 | services/herdr-controller.py L948-958；herdr/transitions.py L24、L27 | 记录为 SC-014。代码修正属实现问题，待决策 D5。 |
| M-17 | MAJOR | 豁免守卫不一致：控制台路径带 expected_version，CLI 与 factory 的 force-pass、bypass 不带。 | console/control_api.py L409；bin/herdr-factory L652-656 | 记录为 SC-015。待决策 D6 的实现部分。 |
| M-18 | MAJOR | 提案与决策文档不符合模板（proposal.md 九节、decision.md 六节）。 | assets/templates/proposal.md、decision.md | 已按模板重写。 |
| M-19 | MAJOR | DoD 未满足：黄金用例未执行（无 runner）；无运行时消费者；多个写动作无审计事件；无 Active 动作。 | SKILL.md §10；tests/golden；generated/validation.md | 保持 experimental，本项不构成批准。 |
| M-20 | MAJOR | 高风险变更须人工评审：bypass 与 force-pass 属于“改变状态机绕过路径”；purge 与 rollback 属于高风险写入；动作 effects 变化须人工评审。 | references/05-security-governance.md“高风险变更” | 待人工批准（不由 Agent 自批）。 |
| m-01 | MINOR | `terminal` 与 `classification.terminal` 语义冲突：completed 有出边却被称为终态。 | states/workflow_lifecycle.yaml（旧） | 已修正：terminal 仅表示吸收态；分类改为 lane_closed、failed_lane。 |
| m-02 | MINOR | `herdr-task reopen` 不是子命令，正确为 reopen-workflow。 | bin/herdr-task L9020-9021 | 已修正。 |
| m-03 | MINOR | paused→in_progress 无生产触发方（resume_workflow 只写 running）。 | herdr/kernel.py L373-379 | 已标注“无观测触发方”。 |
| m-04 | MINOR | rule.returns 使用映射，模板要求 boolean。 | assets/templates/rule.yaml | 已修正；细节保留在 outputs。 |
| m-05 | MINOR | task_transition_allowed 未说明 old == new 合法。 | herdr/transitions.py L73-74 | 已修正。 |
| m-06 | MINOR | 关系 workflow_instance_groups_task 的“恢复查询依赖该直连”为推断。 | — | 已删除推断，标注为未逐一核对。 |
| m-07 | MINOR | gate_verdict_valid 的 logic_ref 指向 CLI 子命令而非函数。 | bin/herdr-task L2338 | 已修正为 set_status。 |
| m-08 | MINOR | 连接器元数据列出 user_tokens 凭证表。 | — | 已移除。 |
| m-09 | MINOR | 计数错误：旧文档写 78 个正式项，实际 79；r1 后为 85。 | — | 已修正。 |
| m-10 | MINOR | workflow_dag_acyclic 合并了 5 类检查，违反“一规则一问题”。 | rules/workflow_dag_acyclic.yaml | 已注明；拆分待提案。 |
| m-11 | MINOR | node_type 枚举未做代码校验，仅观测到 agent 与 gate。 | herdr/workflow.py L341；controller L4108-4116 | 已注明。 |
| m-12 | MINOR | current_stage 写入方未找到。 | herdr/state_db.py L1495-1513 | 保持 unknown。 |

### 撤回与更正（作者自我纠错）

- X-01：SC-003 撤回。`_clean_reservations` 对已注册任务执行 `continue`，即从结果中丢弃；wiki 与代码一致。原“冲突”不成立。真实问题是状态模型（M-06）。
- X-02：B-03 所指的“bindings resolve: all ok”撤回，它是循环验证。
- X-03：作者先前怀疑 pause/resume 不是 herdr-task 子命令，被推翻：二者在 herdr-task（L9137、L9141）与 herdr-factory（L852、L855）中均存在。
- X-04：“preferred 只影响顺序，不影响准入”经核对成立：自动路径在 agent_router.py L634-637 过滤 allowed 与 disabled。
- X-05：计数 78 更正为 79（m-09）。

### 核对正确（Verified correct）

- 任务状态机 53/53 边、工作流状态机 24/24 边与代码矩阵相等（审计 T 检查）。
- 绑定 28/28 可解析；连接器操作 26/26 可解析；授权双向一致；每个动作至少一个策略；只读投影不含动作。
- 终态为吸收态：superseded；预占模型的 removed。
- 标识格式（子代理核对）：artifact sha256[:20]、checkpoint cp_<wf>_<ts>_<uuid6>、project slug-sha1[:8]、workflow wf-…-NN、task 默认 {wf}-{node}-auto。
- 预占 TTL 300 秒与文件名 agent-reservations.json（代码与 wiki/architecture.md 一致）。
- wrapup 闭环闸门、anchor 与 coordinator 排除、克隆已存在拒绝、DAG 环与重复 id 的错误文本（子代理核对）。
- 提交范围仅限 `ontology/`（git diff 核对）。

## 3. 冲突登记状态（decisions/0003）

确认：SC-001、SC-002、SC-004、SC-005、SC-006、SC-007、SC-008、SC-009、SC-010、SC-011、SC-012、SC-014、SC-015、SC-018、SC-019。
部分确认：SC-013（词表确认，模板取值未核对）。
撤回：SC-003（见 X-01）。
未验证：SC-016（resume 对非法边的行为）、SC-017（add 覆盖写的竞态结果）。

## 4. 审计结果（R1 修正后）

```text
Stats: bindings=28, bindings_ok=28, connector_ops=26, evidence_items=202,
       logic_ref=14, logic_ref_ok=14, out_of_matrix_writers=8, state_edges_equal_code=77,
       symbol_refs_ok=110, line_refs_in_range=185
Totals: MAJOR=0, MINOR=0, INFO=2（C-coverage：task 35 项、factory 10 项，启发式）
exit=0
```

## 5. 需要人工决策（不由 Agent 自决）

- D1：bypass、rollback、completed→rework、force-review 等越权写入，收敛到转移网关，还是在状态机中显式建模为受控例外？（高风险，references/05）
- D2：门禁是否拆分为“节点配置”与“任务结论”两个对象？结论的唯一权威是任务行还是 verdict 文件？
- D3：工作流初始状态以代码为准（running），还是修正注册路径使其先写 pending？
- D4：任务类型是否统一为单一枚举？
- D5：fix-loop 可作废集合是否补入 paused、interrupted（代码变更）？
- D6：控制台写路由是否增加鉴权（安全问题，代码变更，不属于本体）？豁免守卫是否统一（SC-015）？
- D7：本轮是否要求覆盖全部约 45 个写子命令，还是分批？
- D8：动作词表（idempotency、transaction、failure_behavior）采用枚举，还是保留描述性？
- D9：event 与 checkpoint 是否长期保留为 Object（它们偏基础设施）？

## 6. 未验证项（诚实边界）

- 运行时行为与 observed 证据未采集；所有 Observed 列标记 not_collected。
- 黄金用例未执行（无 runner）。
- SC-013 中第三方模板取值、SC-016、SC-017 未验证。
- controller 的 CAS 条件（L8524-8531、L8649-8656）与自动验收排除门禁节点，取自子代理，未逐行复核。
- 动作的幂等细节（launch intent、force_pass_gate 的 store 层实现）未读取。

## 7. 批准状态

- 对 1b2fe9a：REQUEST_CHANGES。
- 对 R1 修正：待独立复审（新上下文子代理或人工）。
- 批准：未授予。下一步是人工给出 D1–D9 决策，然后进入正式批准流程。
