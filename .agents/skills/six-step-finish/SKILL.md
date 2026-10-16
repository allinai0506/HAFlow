---
name: six-step-finish
version: "2026.10.16-1"
description: Use when implementation is complete and a development branch needs to be finished — PR creation fallback, PR review closure (CI + findings), knowledge capture, wiki checkpoint, merge confirmation, anchor sync, branch validation, hygiene cleanup. Merge confirmation is forge-optional (Gitee / GitHub / GitLab / local-only) and the anchor branch is parameterized, so it works in any project. A project-specific finish script (e.g. xiyu scripts/agent-finish-task.sh) takes precedence when present.
---

# Six-Step Finish（六步收尾）

标准任务收尾流程，由 xiyu 的 `agent-finish-task.sh` 泛化而来，适用于任何项目。
它只做「已合入的核验与善后清理」，不负责合入动作本身——合入走 PR / 项目合入工具。

**前置条件（步骤 0：交付 PR）**：交付分支必须已推送到远端并已创建 PR——
按目标仓库的标准 PR 流程执行（平台化流程优先用平台 PR 入口，如 HAFlow：
`herdr-task create-pr`；不可用时回退项目/forge 入口，如 nexusarchive：
`npm run pr:create`、GitHub 仓 `gh pr create`），并记录所用入口与回退原因。
本技能不创建 PR、不做合入；若发现分支未推送或无 PR，先执行项目的 PR 流程
再运行本技能。知识沉淀（步骤 1-2）必须先于 PR 创建，保证 PR 包含知识提交。

**前置条件（步骤 0.5：PR 审核闭环）**：PR 创建后必须在申报完成前抓取并处理
机器审核结果——CI/必过检查 + 评审 findings（GitHub：`gh pr checks`、
`gh api --paginate repos/<owner>/<repo>/pulls/<n>/reviews`、
`gh api --paginate repos/<owner>/<repo>/pulls/<n>/comments`——两处必须带
`--paginate` 并聚合全部页，否则多页 PR 会漏报后页 findings；其余 forge 用
对应入口）。硬条件：必过 CI 全绿、无未决 P1 finding、P2/P3 逐条处置或明示
豁免；需要修复时在交付分支追加提交（不改写历史）并复验——**修复提交会使既有
认证失效**：平台路径须重走 test/review 产生新的有效交付身份，forge 回退路径
须对新 head 重新触发审核（如 `@codex review`）并重新核验；严禁以旧审核结果
放行已变更 head。CI 未跑完或存在未决 P1 时，收尾尚未完成，不得申报；findings
与处置必须写入收尾报告与状态表。
闭环完成后必须运行 `herdr-task record-review-closure <task-id> --pr-url <URL>
--ci-status <pass|fail|pending> --findings '<结论>'` 落**机器回执**（确实无法
创建 PR 时 `--deferred --reason '<证据>'`）；HAFlow 的 wrapup 任务在 completed
转换处会 fail-closed 校验该回执存在，缺失将被拒绝（应急豁免
`--skip-review-closure --note '<审计原因>'`，会记录豁免事件）。

**优先级规则：** 项目内有专属收尾脚本时（如 xiyu `scripts/agent-finish-task.sh`）
优先用项目脚本；否则用本技能的 `scripts/finish-task.sh`。

**Announce at start:** "我正在使用 six-step-finish 技能收尾这个任务。"

## 六步总览

| 步骤 | 内容 | 执行者 |
|---|---|---|
| 0 交付 PR（前置） | 按项目/平台 PR 入口推送交付分支并创建 PR（不合并；平台入口不可用时按回退路径执行并记录） | Agent / 项目脚本 |
| 0.5 PR 审核闭环（前置） | 抓取（`--paginate`）PR CI 检查与评审 findings 并逐条处置（未决 P1 或 CI 红 = 未完成）；落 `record-review-closure` 回执 | Agent |
| 1 知识沉淀 | Bug 根因追加到 lessons-learned，新知识回填 wiki/文档 | Agent 调用 `knowledge-capture` |
| 2 Wiki checkpoint | 判定本次是否产生了必须回填的新知识，逐项确认 | 脚本（`.wiki/` 存在时） |
| 3 合并确认 | 核验分支确实已合入 base：git 三重检查 + 可选 forge 复核 | 脚本 |
| 4 锚点同步 | 切回锚点/基准分支并 rebase 到最新 base | 脚本 |
| 5 分支核对 | 拒绝受保护分支、锚点分支；核对命名；删除本地/远端分支 | 脚本 |
| 6 卫生检查 | 未提交变更前置拦截；清理文件锁、任务 worktree、孤儿 worktree | 脚本 |

## Agent 职责（脚本之外）

1. **先沉淀再收尾。** 步骤 1 必须在运行脚本之前完成：调用了
   `knowledge-capture`、回填了 lessons-learned / wiki。脚本只做提醒与确认，
   不会替你写知识。
2. **先建 PR 再做核验（步骤 0）。** 未推送、未提 PR 的分支一律先按项目 PR
   流程推送 + 创建 PR；本技能只做"已合入的核验与清理"，绝不代替项目 PR 流程，
   也绝不自动合并（合入由作者/评审决定）。
3. **不要用本技能做合入。** 未推送、未提 PR、未合入的分支会被脚本拒绝清理
   （`⛔ 尚未合入`）。此时停下来，先走项目的 PR 流程，不要找绕过办法。
4. **丢弃工作没有脚本通道。** 若用户明确要求丢弃未合入的工作，逐字确认
   （让用户打出 "discard"）后手动执行 `git branch -D` / 删除 worktree，
   并在回复中列出将永久删除的提交清单。
5. **解读输出。** `✅ 任务收尾完成` 后报告：切到了哪个锚点分支、删了哪些
   分支/锁/worktree；`--dry-run` 输出仅作计划展示，不代表已执行。
6. **审核闭环后才能报完成（步骤 0.5）。** PR 创建后先抓取 CI 与评审
   findings，未决 P1 或 CI 红意味着还有修复工作——在交付分支追加提交并复验，
   不得把「CI 未跑完 / 有未决 findings」当作可交付状态申报，也不得以
   「PR/审核属控制器职责」为由跳过 PR 入口或其回退路径。修复提交会使既有
   认证失效：平台路径须重走门禁产生新的有效交付身份，forge 路径须对新 head
   重新触发审核；收尾完成前必须运行 `herdr-task record-review-closure` 落
   机器回执——HAFlow 的 wrapup 完成转换会 fail-closed 校验该回执。

## 脚本用法

```bash
bash <skills>/six-step-finish/scripts/finish-task.sh [branch-name] [options]
```

| 参数 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--base <branch>` | `FINISH_BASE_BRANCH` | 解析 `origin/HEAD`，否则 `main` | 基准分支（合入目标） |
| `--forge <f>` | `FINISH_FORGE` | `auto`（按 origin URL 识别） | `gitee` / `github` / `gitlab` / `none` |
| `--anchor <branch>` | `FINISH_ANCHOR` | 见 auto 规则 | 收尾后切回的锚点分支 |
| `--anchor-mode <m>` | `FINISH_ANCHOR_MODE` | `auto` | `auto` / `anchor` / `base` / `keep` |
| `--include-remote` | | 关 | 同时删除远端任务分支 |
| `--force` | | 关 | 跳过合并确认（仅用于确定已合入但检测失败） |
| `--dry-run` / `--yes` | | 关 | 同 xiyu 语义：仅检查不执行 / 自动确认（未提交变更将被丢弃，慎用） |

锚点 auto 规则：分支名匹配 `agent/<name>/<task>` 且存在 `agent/<name>-init`
→ 锚点为该 init 分支；否则锚点为 base 分支。`keep` 表示不切换、保留当前分支
（此时若任务分支即当前分支，则跳过本地分支删除）。

forge 复核凭据解析顺序：专用环境变量（`FINISH_GITEE_TOKEN` / `FINISH_GITHUB_TOKEN`
/ `FINISH_GITLAB_TOKEN`，或旧名 `GITEE_TOKEN` 等）→ `git credential fill` →
GitHub/GitLab 优先用 `gh` / `glab` CLI。取不到凭据时降级为纯 git 检查并告警，
不会因此失败退出。

## 示例

```bash
# 通用项目：默认 auto（forge 按远端识别，锚点回 base）
bash finish-task.sh

# 无远端 / 本地仓库：纯 git 三重检查
bash finish-task.sh --forge none

# agent 命名习惯 + 指定锚点与远端清理
bash finish-task.sh agent/claude/fix-login --anchor agent/claude-init --include-remote

# 先看计划不动手
bash finish-task.sh --dry-run
```

## 常见借口

| 借口 | 现实 |
|---|---|
| "分支还没推/没 PR，先跑脚本看看？" | 先执行步骤 0：按项目 PR 流程推送交付分支并创建 PR（创建即可，不需已合入），再运行本技能；脚本不代劳，也不会因为已建 PR 就清理未合入分支。 |
| "PR 还开着，分支先删了也无妨" | PR 未合入就删分支，反馈循环断了。等合入确认。 |
| "反正要合入，顺手帮我 merge 了吧" | 本技能严禁自动合并：合入由作者/评审决定。PR 创建是交付，合并是授权。 |
| "forge 检查拿不到 token，用 --force 跳过吧" | 先降级跑纯 git 检查；`--force` 只用于确定已合入但检测失败的场合。 |
| "知识沉淀等合入后再补" | 步骤 1 在最前。合入后你已经在别的分支上，上下文就冷了。 |
| "这个孤儿 worktree 顺手也清了" | 只清注册到本任务分支的 worktree 和 prunable 孤儿；其他是宿主环境的地盘。 |
| "PR 创建/审核是控制器（或平台）的事" | 收尾任务对 PR 入口与审核闭环负责：平台入口不可用就走项目/forge 回退（`gh pr create` 等）并记录；「属控制器职责」不构成跳过理由，DEFERRED 必须附实测失败证据（命令 + 错误输出）。 |
| "CI/评审还没出结果，先报完成" | 未跑完 = 未完成。先抓取 `gh pr checks` 与 review findings；未决 P1 须在交付分支修复、复验通过后才可申报。 |
