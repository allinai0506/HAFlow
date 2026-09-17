# Provenance — six-step-finish（技能来源登记）

本目录是 `six-step-finish` 技能的仓库 canonical 副本（vendoring）。
所有文件均从上游**逐字复制**，未追加、未删减、未修改任何内容；
与上游的 sha256 一致性由 `tests/test_six_step_skill_provenance.py` 守护。

| 字段 | 值 |
|---|---|
| 上游路径 | `/Users/user/.agents/skills/six-step-finish/` |
| 上游版本 | `2026.09.17-1`（本地修订：步骤 0 交付 PR 前置；`skill-metadata.yml` 另行声明 `1.0.0`） |
| 导入日期 | `2026-09-13` |
| 导入方式 | 逐字复制（verbatim copy），脚本权限保持 `755` |

## 文件清单与校验（sha256）

| 文件 | sha256 |
|---|---|
| `SKILL.md` | `1efd5aafb5b8226d363e903612433409a3492454dd993a150c1adaa77f213df1` |
| `scripts/finish-task.sh` | `51c73e5bd8b91b3132b20158b13f26511b3d209ba9735de49084f8bf4da835b6` |
| `skill-metadata.yml` | `92fbcc3abe8da97ef95a2251f545df890dd2147bb522c139d203629c99aa34ee` |

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
