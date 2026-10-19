# Validation Result — 候选本体（OEP-0002 D1–D3 收敛版）

日期：2026-10-19
基线：origin/main @ 8d8759f（分支 feat/ontology-oep-0002-d1-d3）
模式：BROWNFIELD_DISCOVERY，首轮 READ_ONLY（仅新增与修改 `ontology/**`；未改业务代码）

## 1. Skill 自带校验器（schema 与引用完整性）

```text
$ python3 <skill>/scripts/ontology_validate.py ontology
Counts: objects 14, relations 14, states 4, rules 14, actions 19, policies 4,
        workflows 3, connectors 5, mappings 10, projections 3
PASSED   exit=0
```

局限：校验器只检查字段存在与引用可解析，不检查符号、绑定、授权一致性或状态机与代码的一致性。

## 2. 索引生成

```text
$ python3 <skill>/scripts/ontology_index.py ontology  -> generated/index.json
```

## 3. 机械审计（本轮新增，`ontology/tests/audit_ontology.py`）

```text
$ python3 ontology/tests/audit_ontology.py <repo-root>
Stats: bindings=30, bindings_ok=30, connector_ops=26, evidence_items=213,
       logic_ref=14, logic_ref_ok=14, out_of_matrix_writers=8,
       state_edges_equal_code=77, symbol_refs_ok=127, line_refs_in_range=186
Totals: MAJOR=0, MINOR=0, INFO=2
exit=0
```

检查项：
- E：证据符号须定义于引用行附近、位于函数体内，或在 20 行内被提及（Markdown 为 5 行）。
- R：规则的 logic_ref 须解析为定义或提及。
- B：绑定须解析为 herdr-task 或 herdr-factory 的 argparse 子命令、herdr/ 下的 Python 函数，或 controller 的函数。
- A：动作与策略的授权双向一致；只读投影不含动作；每个动作至少一个策略。
- T：状态机的边集与 `herdr/transitions.py` 矩阵逐边相等；声明的终态须为吸收态。
- C：CLI 写子命令中未绑定动作者（INFO，启发式分类）。

## 4. 撤回与更正

- 旧版 validation.md 写有“cross-check ALL PASSED”与“Action bindings resolve: all ok”。前者的子命令检查为子串匹配，后者为循环验证（只比对连接器自身列表）。两者均不可靠，已撤回。
- 旧版引用的交叉检查脚本不在仓库中；现以 `ontology/tests/audit_ontology.py` 取代，并可复现。

## 5. 黄金用例

- 声明 7 例，位于 `tests/golden/`。
- **未执行**：无 runner。引用的 rule 与 action id 已由审计解析通过。
- 状态：不能作为回归证据。

## 6. 未验证项

- observed（生产运行证据）未采集。
- 行为类结论（如 controller CAS 条件、自动验收排除门禁节点）取自子代理，未逐行复核。
- 动作幂等细节（launch intent、force_pass_gate 的 store 层）未读取。
- CLI 写子命令中约 45 个未绑定动作（INFO），覆盖范围待决策 D7。

## 7. 状态

- 对 OEP-0001 (1b2fe9a)：PR #275 已合并人工批准。
- 对 OEP-0002 D1–D3 演化：schema PASSED，索引已同步，审计 MAJOR=0, MINOR=0，待人工批准合并。
