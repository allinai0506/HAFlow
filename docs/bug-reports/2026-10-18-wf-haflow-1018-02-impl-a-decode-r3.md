# D1/D2 r3 修复记录

## 理解和假设
基线 599c3bde；继续既有 HAFlow 工位与实现计划，仅本地提交。真实入口 bin/herdr-quality → assess_task_run → load_quality_inputs → _events_for_run → SQLite events；Outcome 仍为业务结果权威。目标是消除饱和误报与损坏吞批，不修改评分、当前运行、UI 或 P3。

## 最小方案 + 文件范围
修改 herdr/quality_sources.py、tests/test_quality_assessment.py 和本报告。保留 20 个 evidence、每个 10 条回执的预算；未完成精确核实的 tests_completed 降级 unresolved。补读 SQL 用 CASE json_valid 保护；另有界读取损坏回执进入既有 decode_stored_payload 聚合。

## 范围纪律
沿用指定冻结基线与分支，不同步 main、不 push/PR、不部署。用户范围锁优先，不创建 .omc、Wiki 或 lessons 工件；阶段证据记录在 docs/bug-reports 并经受管 checkpoint 发布。无 .codegraph。初始 verify-baseline 只有平台预建本报告。自审不能代替独立复验；CI/集成由控制器负责。

## 验证计划
TDD RED→GREEN：真实临时 SQLite，20/21 配对、预算外未配对、饱和坏 JSON、合法事件保留、跨 run/runless；保留既有 1999/2000/2001、混合回执、只读测试。执行两个指定测试模块、ruff、compileall、git diff --check、herdr-loop eval、verify-baseline。证据保存在本目录 r3-evidence，发布有界报告。

## 根因与修复
- D1：固定 `[:20]` 只限制补读次数，却让预算外 tests_completed 继续投影 test_failed。现在先标记未核实 evidence，仅在精确查询未达 10 行上限时解除；未核实 tests_completed 不进入已确认事件，单条 verification_receipts_unresolved note 记录数量。无 evidence_id 的饱和测试同样无法精确核实，保持未知。已读取的有效 verification_completed 仍保留权威结论。20 配对负对照不变。
- 同一 D1 边界：只有 evidence_id、没有布尔 verdict 的 schema 回执不构成已核实覆盖；复用 _verification_verdicts，避免漏掉后续权威回执。新增失败测试 red-schema.txt 后完成修复。
- D2：json_extract 在同范围坏 JSON 上抛异常，被整批异常分支吞掉已读事件。现在 CASE WHEN json_valid 守卫提取；另按严格 task/run 有界读取损坏回执（最多 STORED_LIMIT），按行 id 去重，统一交给 decode_stored_payload 和 _corrupt_payload_note。合法事件保留，非法 JSON 与非对象 JSON 同一路径；NULL/空串兼容语义不变。真正 SQLite 错误仍走原降级。

## 复现命令与前后对比
在当前仓库根目录执行：
```sh
python3 -m pytest -q tests/test_quality_assessment.py -k r3
PYTHONPATH=. python3 docs/bug-reports/r3-evidence/probe.py
python3 -m pytest -q tests/test_quality_assessment.py tests/test_execution_trace.py
```
CLI-before.json 的取得方法：保存当前 quality_sources.py，临时加载 `git show 599c3bde:herdr/quality_sources.py`，执行相同 probe，再在 finally 恢复当前源码。数据库全部由本工位测试构造，来源目录仅作参考。CLI 是独立进程重开 SQLite，全部场景数据库 SHA256 保持不变。

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 饱和20配对 | 0异常 / unknown | 0异常 / unknown |
| 饱和21配对 | 1异常 / improvement_opportunity | 0异常 / unknown |
| 饱和损坏回执、1个真实未配对失败 | 0异常 / trace_unavailable，合法事件丢失 | 1异常 / stored_payload_corrupt，合法事件保留 |
| 饱和坏回执+成功配对 | 缺陷由同一路径复现 | 0异常 / unknown / stored_payload_corrupt |

原始 RED：3 failed, 1 passed, 1 subtests passed（包括21配对子用例失败）；schema RED：1 failed。最终新增6测试专项：6 passed, 2 subtests passed。完整专项：**103 passed, 13 subtests passed**，33.10s（regression.txt）。相比既有97测试增加6项，原断言未放宽。覆盖20/21配对、未配对预算、每evidence10回执预算、schema-only、malformed/非对象、坏行去重、跨run/runless、合法事件保留；既有1999/2000/2001、普通窗外坏行、混合配对、SQL错误与只读专项全通过。

## 自审与静态验证
按 verification-before-completion 验证，按 google-code-review 的设计、复杂度、正确性维度做实现者自审；不是独立审查。ai-slop-cleaner 限定本次diff检查：无新死代码、依赖或包装层需要删除；保留单函数最小修复，不跨范围重构。沿用 unified-dev-flow 的 HAFlow 原生恢复与 S4→S5→S6→本地交付阶段，未建立第二控制器。
- compileall：`python3 -m compileall -q herdr services bin tests`，退出0；没有修改无扩展名CLI。
- diff：`git diff --check`，退出0。
- ruff touched：7 errors，基线同为7（I001×2、UP035×2、F401×1、UP006×2），无新增；未为消除旧问题扩大修复。
- ruff全仓：6480 errors，非零退出；不声称零lint。新增probe脚本ruff零错误。
- EXPLAIN QUERY PLAN：定向查询走 idx_events_type(event_type=?)；未新增索引。结果行有界，SQL扫描成本仍随既有数据规模增长；未进行生产规模性能基准。

## 修改文件与风险
- herdr/quality_sources.py：D1未核实隔离、D2安全查询/损坏补读，沿用现有持久化与判定通道。
- tests/test_quality_assessment.py：6个真实SQLite回归及其夹具，无新增Mock/能力替身。
- docs/bug-reports/本报告与r3-evidence：复现脚本、RED/GREEN、CLI前后输出、静态检查及阶段证据。
预算仍为20个evidence×10回执，额外损坏补读上限2000；超预算不承诺事件全集，显示unknown/unresolved。有效真实失败仍可令过程为 improvement_opportunity，即使完整性unknown；没有改评分阈值或Outcome结论。未部署、重启、push、PR、合并、删除现场。D3不在范围。CI合并结果全量、独立复验、生产业务验收未执行，等待控制器集成冻结新SHA。未配置机器交付契约，不将eval得分当作业务验收。

## 工位评估
release herdr-loop eval：`[HERDR_LOOP_SCORE] 100.0`、`[HERDR_LOOP_CONVERGED] True`。原始 EVALUATION.md 已保存在 r3-evidence/evaluation.md。仅代表工位自检通过，不代表独立验收/集成/上线。
