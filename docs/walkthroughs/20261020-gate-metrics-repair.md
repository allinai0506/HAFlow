# 2026-10-20 门禁与度量交接单修复

本地交付位于 `fix/gate-metrics-handoff-20261020` CoW 沙盒，基线 `df4b3cf`。实现验收阶段没有推送、合并、部署、重启服务或改写原抢救单状态。规格来源是本仓库 [D1–D11 交接单](../bug-reports/2026-10-20-herdr-gate-metrics-handoff.md)。

## 当前状态：限定修复与本地验收完成

第三轮审查曾因 reopen 等待代际缺陷停表；用户明确授权“修复”后继续同一隔离分支。等待身份与退役判定现统一复用 canonical workflow generation，包含 reopened_at；终态仅按当前状态判断，历史 completed_at/outcome 不再退役运行中的新一代等待。真实生命周期回归验证旧操作 superseded、新操作从 1 次开始，交错 reconcile 后仍能升级 waiting_human。[原失败证据](evidence/20261020-gate-metrics/reopened-blocker.log)保留。

独立 Standards、Spec 及跨模型对抗审查均无剩余阻塞。当前源码完整专项为 549 passed、22 subtests；候选兼容专项 502 passed，新增度量回归 11 passed，独立新探针 1275 passed、119/119。原停表记录保存在 evidence 中。

## 变更与裁决

| 条目 | 本次结果与边界 |
| --- | --- |
| D1 | 生产 close 在写 completed 收编凭证的事务中确认 workflow 已 completed；重复 close 拒绝状态漂移，reconcile 返回显式 inconsistencies 和人工处置要求。历史 paused 工作流不自动完成。 |
| D2 | 使用既有 rollback 重开节点，以下列命令与副作用明确运行契约；未新增一个隐藏撤销下游的“无损重跑”原语。 |
| D3 | 原预算假设未复现：源码已按 Task/Run/epoch 过滤，预算 1000 段，step 是 1–1000 的整数；新增满预算后换 epoch 的独立性回归。没有放宽预算。 |
| D4 | 增加显式 rebaseline CLI，保留原因、旧基线历史与 SHA，并绑定初次 lint 命令和 EVALUATOR.sh 摘要；不同命令、修改后的 evaluator 或未绑定的旧基线先拒绝。初次真实捕获也记录 SHA。捕获失败不覆盖旧值；历史最多 100 次，满额明确拒绝。 |
| D5 | workspace server_not_running 返回 503 和启动后重试提示；真实 HTTP 路径验证没有半成品项目注册。未自动启动 workspace 服务。 |
| D6 | 支持既有 ESLint/TypeScript 摘要和 Ruff JSON；未知非零结果 unavailable，不能被 baseline 抵消，Markdown 质量行也明确显示 unavailable 而非绿色零错误。零诊断却非零退出也拒绝收敛；保留原 exit code。 |
| D7 | fix_loop 初始等待、执行结束和结果结算进入 waiting_human 时，统一附替代对象、原因及 recovery-decide 指引。 |
| D8 | 采用交接单允许的 fail-closed 分支：receipt-v1 test/review 的 force-pass/bypass 在写入前明确拒绝。没有伪造、代签或自动生成 acceptance PASS。 |
| D9 | 等待写入既有恢复表，三次至少相隔 30 秒的观测升级 waiting_human 并记一次 stalled 事件；候选/执行及 canonical reopen generation 切换、workflow 当前状态终结或节点移除后退役；同一阻塞集合再次出现会重新开始有界观测。Console 通过既有 recovery 视图合同显示指引，不提供后端不支持的按钮。waiting_human 是恢复操作状态，workflow 不增加新状态。 |
| D10 | 原事件 behind_by/remediation 已完整，保留不删。task blocker 同时含漂移详情与处置；基线拒绝的未启动任务用既有 superseded/replacement_pending 表示，可沿补派谱系继续。launch intent 记 resources_absent。 |
| D11 | 新增文档三路合并导出工具：onto 原字节保持前缀，已完整存在的相同追加块去重，只顺延 incoming 新编号，输出合并文件、映射和可应用 patch。历史改写、删除、不完整末行拒绝；空文件与不存在文件分别处理。不是完整代码 rebase，也不自动裁决正文冲突。 |

## 操作入口

重开既有节点需要显式回溯：

```bash
bin/herdr-factory rollback <workflow_id> <node_id> --reason '<重开原因>'
bin/herdr-factory resume <workflow_id>
```

rollback 会使目标及下游相关任务/终态失效，保留审计历史并进入既有补派流程；不能承诺保留既有下游验收。执行前核对当前 DAG、候选、下游范围及节点累计预算。本次未对原 workflow 执行这些命令。D10 的新基线拒绝场景无需依赖 rollback 清理一个从未执行的节点代表。

授权 intake 完成后的度量重捕获：

```bash
bin/herdr-loop rebaseline --dir <clone> --reason '<intake完成原因>' --lint-cmd '<原度量命令>'
```

该操作明确改变度量基线，不能据此复用旧候选业务验收。lint 命令必须与初次捕获一致，EVALUATOR.sh 必须未变；未绑定合同的旧基线应先核对、归档并显式重建，不能借换命令抬高基线。

只导出文档冲突的安全解决结果：

```bash
python3 scripts/replay-append-only.py --repo <repo> --base <共同基线SHA> \
  --onto <目标SHA> --candidate <候选SHA> --output-dir <不存在且在源仓库外的新目录>
```

工具仅读取固定 Git 对象，输出 `manifest.json`、合并文档与 `append-only.patch`；源分支和索引不变。先检查 manifest 的 SHA 与编号映射，再在对应目标版本的隔离工作树应用 patch。编号映射不会擅自重写正文引用，引用语义需复核。实际 `a56739e → df4b3cf` / `b46b81a` 输入在 `wiki/log.md` 被拒绝：该历史不是纯 EOF 追加，退出 2 且不创建输出。不能称原抢救单重放已完成。

## 验收证据

所有主修复测试使用临时 HOME、临时 DB 和目录；保留同解释器包路径。外部 workspace transport 在 HTTP 测试中被替换，不启动真实 Agent 或收费模型。主调用链覆盖 Controller join → acceptance 查询 → recovery/events → 读取；基线拒绝 → 真实 launch intent → resources_absent → pending 补派；Console HTTP → 项目逻辑 → 注册检查；close → SQLite 事务 → 收编凭证。

- [当前完整专项原文](evidence/20261020-gate-metrics/main-tests-final.log)：549 passed，22 subtests，exit 0，覆盖 20 组真实影响面，绑定当前源码。
- [补充专项](evidence/20261020-gate-metrics/targeted-tests.log)：175 passed；最终主专项已覆盖更新后的所有修复。
- 新文件 Ruff 零诊断；[健康差分](evidence/20261020-gate-metrics/health-summary.json)按相同文件/规则/消息计数，745 → 744，无新增诊断，不把既有 lint 债称为清零。
- `compileall herdr/services/tests`、三个变更相关无扩展名入口的 `py_compile` 和 `git diff --check` 通过。
- [最终 verify-baseline 回执](evidence/20261020-gate-metrics/baseline-final.log)：exit 0，TASK_CHANGED 为隔离工作树内本任务产出；不是未修改基线的声明。
- Wiki/log、lessons、task-lifecycle 均为原文前缀加追加，未删除历史；新教训编号由程序从最大值派生为 179。

### 拆分候选的度量兼容性

主干没有 `scripts/check_state_db_split.py`，因此额外创建独立副本，加载 **b46b81a**，仅覆盖本次 `herdr/evaluator.py` 与 `bin/herdr-loop`（另有未选入测试的本次回归文件）。这不是主修复分支的代码身份，也不是原 test Run 的验收；两处度量源码 overlay 与当前主修复文件哈希一致；兼容证据仅覆盖度量改动，不覆盖主修复的其他模块。两个版本和 overlay 哈希见 [identity.json](evidence/20261020-gate-metrics/identity.json)。

- G1/G2/G3/G5/G7：[`--final` 原文](evidence/20261020-gate-metrics/candidate-static-gates.log)，167/167，PENDING=[]，静态边 25 模块/74 边，93 个 schema 对象。
- G4、GA1/GA3/GA4 的测试和 G6 真实 CLI→SQLite 回读：[候选 11 组专项原文](evidence/20261020-gate-metrics/candidate-tests-final.log)为 502 passed、10 subtests；GA2 由完整 final 及 PENDING 集合留证。[分配报告](evidence/20261020-gate-metrics/candidate-allocation-report.log)、[schema dump/check 原文](evidence/20261020-gate-metrics/candidate-schema.log)为 SCHEMA_MATCH。
- GA5：以原候选已登记的 40 组测试和原排除表达式，在全新目录重新执行透明调用点探针；[最终原文](evidence/20261020-gate-metrics/candidate-probe-final.log)为 1275 passed、7 deselected、12 subtests，**REAL_PATCH_SITES 119/119**，exit 0。未沿用旧 receipt 的命中结论。
- [新增度量专项](evidence/20261020-gate-metrics/candidate-new-metrics-final.log)：11 passed、19 deselected，包含变更 evaluator 后拒绝消费旧基线的真实 CLI 回归；既有度量测试包含在上述 502 项中。
- [探针首轮失败记录](evidence/20261020-gate-metrics/candidate-probe-first-failed.log)保留（仅去除行末空白）：额外全局 HERDR_STATE_DB 干扰旧夹具选择，7 个 Kernel 用例读不到它们在 workflows.json 播种的记录，只命中 118/119。恢复夹具自身 DB 选择、保留隔离 HOME/PYTHONPATH 后完整重跑通过；未删掉这 7 个用例。

## 未执行与交付边界

未给原 wf-haflow-1020-02 代签、重放或放行；未运行生产 E2E、部署或全仓 CI。工作树交付不代表允许合并或发布。D2 按交接单允许的文档化方案交付；D3 按实测校正原假设；D11 的正文改写冲突仍明确留给人工。

评审结果及最终源码指纹另见本工作树 `.omc/review-gate-metrics-20261020.md` 与 `.omc/verify-gate-metrics-20261020.md`。

## PR 交付入口

用户随后授权创建 PR，继续使用同一隔离分支。平台入口 `bin/herdr-task create-pr local-gate-metrics-verification --title ...` 在隔离验收 DB 返回 `[PR PUBLICATION REFUSED] task must be integrated before PR publication`（exit 2）。该记录用于本地基线验收，不是原工作流的 wrapup Task；因此按项目允许的 forge 回退使用 `gh pr create`。不制造 integrated/acceptance 状态，也不冒用原工作流身份。本次授权包含提交、推送与建 PR，未包含合并、部署或服务重启。

## PR #289 审查修复

Codex P1 指出升级数据库仍可能保留不含 `generation` 的旧版 `workflow_close_operations`。原 reconcile 只检查表存在便按新列查询，会以 `no such column: generation` 中断 Controller/CLI。修复后的只读路径先读取列集合：新表按当前 generation 精确匹配，旧表按 workflow/status 读取历史 completed receipt 并继续报告状态不一致，不在 reconcile 中执行迁移。

GitHub 全量 CI 同时暴露 9 个旧测试夹具与新合同不一致：close 测试 mock 掉了完成状态持久化，却仍要求发布 completed receipt；evaluator 测试直接写未绑定 baseline，却仍要求抵扣 lint 债。测试现保留原关注点，同时执行真实状态副作用或通过真实 lint capture 绑定命令/evaluator 合同。没有放宽生产门禁。首轮审查修复专项覆盖 16 个相关模块，229 passed、10 subtests；CI 报出的 10 个失败与新增 legacy schema 回归合计 13 项单独通过。

新 head 的 Codex 复审另报 P2：主线后缀为 `A + B (+ C)`、候选后缀为已存在的完整 `B` 时，原去重只检查主线后缀开头，会重复追加并重编号。现在仅在完整行边界匹配候选追加块，可识别其位于主线后缀开头、中部或末尾；正文中的非边界片段不作为已应用。新增编号文档中部幂等回归，更新后的 16 模块专项为 230 passed、10 subtests。最终源码指纹已再次更新，等待新提交对应的 GitHub 全量 CI 与 Codex 复审。

第三轮 Codex 复审进一步指出，编号冲突使第一次重放改变标题号后，下一次用原候选无法按原字节识别同一块。编号文档的去重比较现只归一化 Markdown 二级编号，其他标题和正文仍逐字匹配，并继续要求完整行边界；第一次重放产生的编号映射不变，重复重放返回原 main 且不产生新映射。新增 `## 2 → ## 3` 后再重放仍只保留一份正文的回归，16 模块专项更新为 231 passed、10 subtests。

用户明确授权越过三轮未收敛停表后，第四次修复处理两项 P2。编号文档现按代码围栏外的真实二级编号标题拆分条目，比较忽略条目自身编号但保留正文原字节；若候选连续前缀已经落入 main，只追加尚未应用的剩余条目，避免 `B` 已落地后重放 `B + C` 再次复制 `B`。标题发现和重编号均忽略 fenced code 内的示例标题，避免示例被改写或抬高后续编号。新增部分应用重放及 fenced heading 回归；16 模块专项更新为 233 passed、10 subtests，Ruff、compileall 与 `git diff --check` 通过。

当前 head 复审又识别出 main-only 条目穿插后的重试边界：`B + D + C` 中候选条目仍已全部落地，但连续匹配只会消费 `B`。候选前缀识别现允许 main-only 条目穿插，同时仍要求候选条目按原顺序、正文逐字匹配；因此首次在 `B + D` 后补入 `C`，再次重放不会复制 `C`。新增该交错重试回归，16 模块专项更新为 234 passed、10 subtests。

后续复审补出两项 P2。初次 lint baseline 捕获现在与显式 rebaseline 一样在命令前后读取 HEAD，期间提交变化会拒绝发布，避免把旧或混合工作树输出绑定到新 SHA。编号文档去重现单独保留首个真实标题前的非空字节；即使全部标题条目已存在，也不会静默丢弃前置正文，重复重放则按完整行边界去重该前缀。两项回归先失败后通过，16 模块专项更新为 236 passed、10 subtests。远端上一轮全量的单一失败位于未受本次变更影响的 wrapup 测试，原因是并行现场 `clone_git_inventory_unknown`；同一精确参数本地重跑通过，保留远端失败记录并在新 head 重跑全量。

下一轮复审发现前置正文已经落地、编号条目尚未落地的局部状态仍会复制前缀。suffix 重建现无论已应用条目数是否为零，都使用独立计算后的待追加前缀；新增 prefix-only partial application 回归，编号条目仍正常追加且 `IMPORTANT` 只保留一次。16 模块专项更新为 237 passed、10 subtests。

PR 最终复审绑定 `5e6bef4`，Codex 明确返回无 findings、无未决 P1/P2，远端 pytest、tests-required 与 P1 Gate 全部通过。随后为解除 GitHub `DIRTY`，将最新 `origin/main` 合入同一 PR 分支；唯一内容冲突位于 append-only `wiki/log.md`，解决结果完整保留双方追加段落。合并后专项额外覆盖 main 新增的 D5 状态与 wrapup 交付路径，共 334 passed、2 skipped、10 subtests。
