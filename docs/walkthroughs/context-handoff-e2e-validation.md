# Context Handoff E2E Validation (Trajectory → Observation → Finding → ContextPack → Next Agent)

- Date: 2026-09-22 (UTC)
- Branch: `exp/context-handoff-e2e-validation` (from `origin/main` @ `8fea980`, includes PRs #73, #75, #76, #77)
- Verdict: **PASS**
- Production logic changed: **none** (new test file + this report only)

## 1. 实验任务

真实开发任务（低风险、仅测试代码）:创建 `tests/test_context_handoff_e2e.py` —— 一个端到端回归测试,
用隔离 tmp DB 模拟 writer→reader 交接:writer 记录 trajectory + observation + verification,
reader 仅凭 ContextPack + 按需证据读取还原工作契约(goal/completed/verified/open-issues/next-focus,
全部 ref 可解析)。任务天然多阶段:需求理解 → 代码定位 → writer 实现 → pytest green →
reader 桩测试 red → 再验证 red(稳定) → 交接 → B 补齐 reader → green 收尾。

## 2. Agent A(前半段,止于 red)

- 阅读 AGENTS.md 与 `tests/test_context_compact.py` 模式,定位 `TrajectoryLedger` / `ObservationStore` / `TrajectoryObserver` / `compact_run`。
- 实现 writer half + `build_reader_view` 桩(`raise NotImplementedError`)+ `test_handoff_reader_completes_contract`。
- 真实验证:writer-only 运行 `3 passed`;加桩后连续两次 `1 failed, 3 passed`(红色稳定,非人造失败)。
- 在实验 DB(`$EXP/exp.db`,显式 `db_path`,生产 DB 未碰,mtime 仍 Sep 21)记录:
  `evt_1 task_started`, `evt_2 artifact_created`(测试文件), `evt_3 observation_created`(自动回执),
  `evt_4 verification passed 3/3 exp-green-1`, `evt_5/evt_6 verification failed 3/4 exp-red-1/2`(真实计数)。
- 存 3 个人工 observation(红测输出尾 1631B、verification JSON 93B、部分测试文件 1800B)+1 个 artifact 自动回执(6595B)。
- 真实 observer(`observe_run(..., use_model=False)`,无模型调用)自然产生 `repeated_failure`(连续 2 次失败,无需 confirmation)。
- 真实 `compact_run(provider=None)` 生成 ContextPack,存 `$EXP/agent_a_context_pack.json`(3191B),然后停止。未写任何交接说明,未实现 reader。

## 3. 产生的数据(关键 ID,无大日志粘贴)

- Run: `run_handoff_exp_01` / Task: `handoff-exp-01` / Workflow: `wf-handoff-exp`
- Trajectory: `evt_1..evt_6`(A),`evt_7 progress`(B),`evt_8 verification passed 4/4 exp-green-final`(B)
- Observation: `obs_a2bb…`(artifact 回执 6595B)、`obs_fb89…`(红测尾 1631B)、`obs_e106…`(verification 93B)、`obs_bd75…`(部分文件 1800B)、`obs_399e…`(B 的 green-final 80B)
- Finding: `fnd_7c86f7ed1b344951`(`repeated_failure`/warning/conf 0.85/anchor `evt_5`/`recommended_action=replan`,状态 open)
- ContextPack A: `ctx_39ccade794c44010929b0c954f6b16aa`(`source_event_sequence=6`;completed 4,verified 4,findings 1,open_issues 1,next_focus 1,evidence 4,artifact 1)
- ContextPack B: `ctx_397cbb67…`(`source_event_sequence=8`;completed 5,verified 5 含 `exp-green-final`,3577B)—— 链条延续性证明

## 4. Agent B 初始理解(原样要点,出自 `agent_b_understanding.md`,仅基于 Pack A)

1. 目标:用真实双 Agent 实验验证交接链(run_handoff_exp_01)。2. 已完成:artifact_created(evt_2)+3 次 verification(evt_4/5/6);Pack 未说明每次验证对象。3. 已验证:evt_4 通过 3/3(exp-green-1);evt_5/6 未通过 3/4;task_status=in_progress;Pack 无测试名与失败细节。4. 未解决:连续 2 次 verification 未通过(最近 exp-red-2),引用 fnd_7c86…。5. Finding:fnd_7c86… repeated_failure/warning;Pack 未给 recommended_action 明细(仅 next_focus 见 replan)。6. 4 个 observation 均值得按需解析;artifact 指向测试文件;Pack 未标每个 observation 的证据角色。7. 下一步:Pack 字面是 replan,但 Pack 缺 reader/writer 分工与失败测试名,需先解析 finding + 最近失败 verification,再看工作树测试现状决定修复点。

## 5. Agent B 后续行为

- 按需读取 5 次(见 `agent_b_reads.jsonl`):observation×2、finding×1(读两次)、trajectory list×1;未碰 `$EXP` 下其他文件(红测尾原文未读),未读任何 transcript。
- 仅实现 `build_reader_view`(goal/completed/verified/open-issues/next-focus 透传 + `verified_facts→verified` 并回填 `refs=[event_id]` 使 ref 全可解析);writer half 一字未改。
- `pytest tests/test_context_handoff_e2e.py`:交接前 `1 failed, 3 passed` → 交接后 `4 passed`。
- 追加 `evt_7` 进展 + `evt_8` 通过验证 + 新 observation,并重跑 `compact_run` 生成 Pack B(同一 run 延续)。

## 6. 验收指标

| 指标 | 结果 |
| --- | ---: |
| 接班理解(7 问 + current_state) | 8/8 (100%) |
| 状态/验证结果理解 | 全对(含 evt_4~6 通过状态与计数、in_progress) |
| 重复劳动次数 | 0(writer half 未动;全文件最终运行属正常收尾验证) |
| 读取 Observation 数 | 2(4 个中按需取 2) |
| 读取 Finding 数 | 1(同一 finding 读两次:列表+全文) |
| 读取 Artifact(store 直读) | 0(经工作树文件 + obs 回执解析,符合允许输入) |
| 读取 Trajectory | 1(`list_events` 一次) |
| 读取完整 transcript | 0 |
| 最终任务是否完成 | 是(`4 passed`;全套件 1044 passed) |

## 7. Context 压缩效果(实测字节,估算处已注明)

- A 时刻持久化历史:6 个事件 payload 共 1993B + 4 个 observation 内容共 10119B = **12112B**。
- B 初始上下文 Pack A:3191B ≈ 历史的 **26%**,且经检查**零 observation 原文内联**(仅 id/excerpt 级元数据)。
- B 总摄入:Pack 3191B + 按需证据约 7301B ≈ 10492B。小规模下字节节省有限(值得读的证据本就占大头),但价值在导航性:5 次定向读取、0 transcript 即正确接班。
- Transcript 级对比无法直接测量(subagent 内部 token 不可见,特此声明):定性上 A 消费了 4+ 个源码/测试文件(数千行,含 1525 行的 `test_context_compact.py` 与 858 行的 `context_compact.py`)做定位,而 B 仅消费 Pack + 5 次读取 + 工作树测试文件。少传的是全部定位过程与思考历史,多传的为零(除 B 主动拉取的证据)。

## 8. 发现的问题

阻塞类(A):**无**。未出现:状态丢失、跨 Run 串数据、verification 被错误覆盖、Pack 误导致错误操作。Pack B 与 Pack A 的 goal 一致、序号单调(6→8)、finding 状态 open 延续。

非阻塞类(Future Improvements,本次不改):
1. Pack 的 `important_findings` 只有 id/type/severity/summary,B 需二次读取才拿到 `recommended_action`/`suspected_cause` —— 建议未来评估是否给高优 finding 附带一句话处置。
2. `evidence_refs` 无角色标注(obs 与 verification 的对应靠猜,B 猜中 obs_bd75 实为 artifact 内容)—— 建议未来给 observation 元数据加 role/source 提示。
3. `repeated_failure` 的 `recommended_action=replan` 对"补齐缺失实现即可收敛"的状态偏保守;信号准确、处方模糊。B 正确忽略了字面处方、采纳了信号事实—— 恰好证明 Finding 当分析用而非当事实用的分层是有效的。

四层边界抽查(§八 8 项):Finding 未被当事实(B 质疑了 replan);Observation 大文本未进 Pack(3191B 实测);Pack 未复述历史;当前态与历史一致(in_progress,无 terminal 事件);finding 引用可解析;evidence 可读且 `verify()` 有效(测试内断言);单 run 无串数据;B 明确知道下一步(先读证据再看工作树)。**8 项全部通过**。

## 9. 结论

**PASS**。事实依据:全新 Agent B 在零 transcript 下,仅凭 3191B 的 ContextPack + 5 次按需证据读取,8/8 理解正确、0 重复劳动、补齐唯一缺口并使测试转绿,同时用新事件与 Pack 证明链条可延续;Finding 真实、有据、被使用;四层边界无违规。压缩的字节意义在小规模下有限,导航意义已证实。已知缺口均为非阻塞优化,列入 Future Improvements,不在本次修改。
