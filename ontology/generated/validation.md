# Validation Result — 候选本体 v0.1

日期：2026-10-19
基线：`origin/main @ 6b4d48b`
模式：BROWNFIELD_DISCOVERY / 首轮 READ_ONLY（仅新增 `ontology/**`，未改业务代码）
运行环境：CoW 沙盒 `~/.sandboxes/ontology-brownfield-discovery-v1`（APFS clonefile）

## 1. Schema Validation

```text
$ python3 <skill>/scripts/ontology_validate.py ontology
Ontology root: .../ontology
Counts:
  objects: 12
  relations: 12
  states: 4
  rules: 12
  actions: 14
  policies: 4
  workflows: 3
  connectors: 5
  mappings: 10
  projections: 3
PASSED
exit=0
```

## 2. Index Generation

```text
$ python3 <skill>/scripts/ontology_index.py ontology
.../ontology/generated/index.json
exit=0
```

## 3. Cross-check against code facts（本轮新增校验脚本）

```text
[PASS] task_lifecycle edges == herdr/transitions.py: code=53 doc=53 missing=[] extra=[]
[PASS] task_lifecycle states == code keys: code=15 doc=15
[PASS] workflow_lifecycle edges == herdr/transitions.py: code=24 doc=24 missing=[] extra=[]
[PASS] golden cases reference declared rule/action ids: all refs ok
[PASS] action targets/preconditions/policies/connectors resolve: all ok
[PASS] every candidate item carries evidence: all ok
[PASS] bin/herdr-task exposes referenced subcommands: all found
ALL PASSED
```

说明：脚本从 `herdr/transitions.py` 直接导入矩阵，逐边比对本体声明，防止人工转录漂移。

## 4. Limitation / 未验证项

- `observed` 层未采集：本轮未在生产环境运行、未读取生产状态库；所有冲突的 Observed 列标记 `not_collected`。
- Golden Cases 为声明式用例，当前无 runner 执行；已校验其引用的 rule/action id 全部存在。
- `worker_policy.permissions` 的生产接线、`gate.retry_target` 运行时消费等未验证项记录在 `proposals/OEP-0001` 的 Open Questions。
- 全部 items `lifecycle: experimental`：不构成运行时依赖，进入 active 需评审 + 测试 + Merge。
