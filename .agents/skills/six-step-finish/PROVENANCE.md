# Provenance — six-step-finish（技能来源登记）

本目录是 `six-step-finish` 技能的仓库 canonical 副本（vendoring）。
所有文件均从上游**逐字复制**，未追加、未删减、未修改任何内容；
与上游的 sha256 一致性由 `tests/test_six_step_skill_provenance.py` 守护。

| 字段 | 值 |
|---|---|
| 上游路径 | `/Users/user/.agents/skills/six-step-finish/` |
| 上游版本 | `2026.10.16-1`（本地修订链：步骤 0 交付 PR 前置 → 步骤 0.5 PR 审核闭环；`skill-metadata.yml` 另行声明 `1.0.0`） |
| 导入日期 | `2026-09-13` |
| 导入方式 | 逐字复制（verbatim copy）+ 本地修订（见下方修订记录），脚本权限保持 `755` |

## 文件清单与校验（sha256）

| 文件 | sha256 |
|---|---|
| `SKILL.md` | `7bf03e6b2acdb84a9d094411dd1161077f224ea2cd6d74526a6ca9ed0647559d` |
| `scripts/finish-task.sh` | `51c73e5bd8b91b3132b20158b13f26511b3d209ba9735de49084f8bf4da835b6` |
| `skill-metadata.yml` | `a0c02d9d7a7cf981fa69a5f472073e44c577e8f8e77a1485e1563730c5054c54` |

## 复验命令

```bash
shasum -a 256 .agents/skills/six-step-finish/SKILL.md \
  .agents/skills/six-step-finish/scripts/finish-task.sh \
  .agents/skills/six-step-finish/skill-metadata.yml
python3 -m pytest tests/test_six_step_skill_provenance.py -q
```

## 同步与升级策略

- 仓库内本目录为**单一事实源**；用户级副本通过
  `scripts/install-herdr-skills.sh` 从本目录同步到 `~/.agents/skills/`。
- 上游技能升级时：逐字更新本目录文件 → 同步更新本登记表的 sha256 →
  运行上述校验测试 → 在同一个 PR 内提交。

## 本地修订记录（Local Amendments）

| 日期 | 版本 | 修订内容 |
|---|---|---|
| 2026-09-17 | `2026.09.17-1` | 新增 **步骤 0：交付 PR 前置**——交付分支必须先按目标仓库 PR 流程推送 + 创建 PR（如 nexusarchive `npm run pr:create`），本技能不做合入；同步在「常见借口」表补充三条（先建 PR / 严禁自动合并 / 不代劳 PR 创建）。背景：`wf-nexusarchive-0917-01` 收官时 wrapup 只做只读合并确认即 DEFERRED，全链路无 push/PR 动作，PR 只能由人工/总指挥补交（lessons §64）。 |
| 2026-10-16 | `2026.10.16-1` | 新增 **步骤 0.5：PR 审核闭环**——PR 创建后必须在申报完成前抓取 CI 与评审 findings（`gh pr checks` / `reviews` / `comments` 等），硬条件：CI 全绿、未决 P1=0、P2/P3 逐条处置；「常见借口」补两条（PR/审核属控制器职责、CI 没跑完先报完成）；`skill-metadata.yml` 补触发词「交付 PR」「PR 审核闭环」。模板侧同步：wrapup 节点新增『PR 审核闭环』『职责单一性』规则、六步执行规则 (8)(9)、required_outputs 增补审核闭环证据。背景：`wf-haflow-1015-01` 收尾未创建交付 PR、未检查 GitHub（Codex）审核 findings，P1/P2 于交付后才发现导致返工。随后按 Codex 复审补齐：① gh api 查询加 `--paginate`（聚合全部页，避免多页漏报）；② 修复提交再门禁（平台=重走门禁产生新的有效交付身份，forge=对新 head 重新触发审核）；③ HAFlow 新增 `herdr-task record-review-closure` 机器回执与 wrapup completed 转换的 fail-closed 校验（含 `--skip-review-closure --note` 审计豁免），行为级测试见 `tests/test_wrapup_review_closure.py`。 |
