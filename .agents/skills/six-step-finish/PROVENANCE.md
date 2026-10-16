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
| `SKILL.md` | `ae606b2bbe7349167c2d68e1bb8d970da115be5bdd0baf52bae69835281084c3` |
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
| 2026-10-16 | `2026.10.16-1` | 新增 **步骤 0.5：PR 审核闭环**——PR 创建后必须在申报完成前抓取 CI 与评审 findings（`gh pr checks` / `reviews` / `comments` 等），硬条件：CI 全绿、未决 P1=0、P2/P3 逐条处置；「常见借口」补两条（PR/审核属控制器职责、CI 没跑完先报完成）；`skill-metadata.yml` 补触发词「交付 PR」「PR 审核闭环」。模板侧同步：wrapup 节点新增『PR 审核闭环』『职责单一性』规则、六步执行规则 (8)(9)、required_outputs 增补审核闭环证据。背景：`wf-haflow-1015-01` 收尾未创建交付 PR、未检查 GitHub（Codex）审核 findings，P1/P2 于交付后才发现导致返工。随后按 Codex 复审补齐：① gh api 查询加 `--paginate`（聚合全部页，避免多页漏报）；② 修复提交再门禁（平台=重走门禁产生新的有效交付身份，forge=对新 head 重新触发审核）；③ HAFlow 新增 `herdr-task record-review-closure` 机器回执与 wrapup completed 转换的 fail-closed 校验（含 `--skip-review-closure --note` 审计豁免），行为级测试见 `tests/test_wrapup_review_closure.py`。该轮 Codex 复审（1387187）再提三条 P1 并已闭环：① 不变量上移至共享转换网关 `herdr.kernel.transition_task`（覆盖控制器 CAS auto-accept 等所有完成来源），控制器侧对网关拒绝改为 `[STATE TRANSITION REFUSED]` graceful 返回 False；② 回执内容校验（非 deferred 须 `--ci-status pass` 且 findings 非空）在写入时 fail-closed；③ 回执绑定 `--head-sha`，与工位克隆当前 HEAD 比对，head 变更即判 stale 并拒绝完成。第四轮（Codex 复审 e61537f）再提一条 P1 并已闭环：契约改为**显式 opt-in**——wrapup 节点须在 pin 配置声明 `review_closure_required: true`（`_normalize_workflow_body` 白名单放行、模板已启用、模板测试断言）；未启用的旧工作流/自定义模板保持原行为，升级不阻塞其完成路径。第五轮（Codex 复审 0fe9a1a）再提四条 P1 并已闭环：① 回执结构化计数（`--unresolved-p1` 必须为 0、`--unresolved-p2` 必填、findings 为处置文本）；② head 不可验证（clone 缺失/不可读）时完成转换 fail-closed 拒绝；③ 启用判定改读权威 pin 配置快照（SQLite `config_json`，文件为回退；存在但不可读 → fail-closed）；④ 审计豁免绑定当前 head，rework 后过期。第六轮（Codex 复审 11c3687）再提两条 P1 并已闭环：① 回执写入门禁增加 **forge 实证**——经 gh 查询 PR head 与 `--head-sha` 一致、`gh pr checks` 全绿、无未决 Codex P1/P0 评审线程（同 `scripts/codex-gate.py` 判定），自证字段不再被单独接受；② `bypass-gate` 对 wrapup 的 completed 直写路径补挂同一不变量（不再旁路共享网关）。第七轮（Codex 复审 850c740）再提两条 P1 并已闭环：① forge 实证**按 host 限定 GitHub**（非 GitHub PR host 明确拒绝——不再可能误查 github.com 上的无关仓库；非 GitHub forge 走 deferred 并附 remote 探测证据）；② deferred 回执须附**机器证据**（remote 探测：无 remote / 不受支持 forge + `git ls-remote` 探测结果），**存在 GitHub remote 时 deferred 被拒绝**。第八轮（Codex 复审 9dbf82d）再提两条 P1 并已闭环：① 回执 PR 须与**交付身份**一致（`pull_request_created` 交付记录含 candidate SHA，或项目 GitHub remote owner/repo），fork/第二仓库同提交不再能蒙混；② **完成转换时再次实证 forge 状态**（gh 复查 head/checks/未决 P1/P0），记录后 CI 转红或新增未决线程即失效须重录。第九轮（Codex 复审 820ab1b）再闭环 1×P1+1×P2：① 交付 PR 必须为 open 或**已合并**（closed 未合并且无 merged_at 一律拒绝）；② SKILL 六步总览重排为 知识沉淀/wiki(1-2) → 交付 PR(0) → 审核闭环(0.5)（回执之后不得再产生提交），与模板顺序一致。第十轮（Codex 复审 c6e7cf0）再提 1×P1 并已闭环：所有 gh 调用钉死 `--hostname github.com`（api/graphql）与 `--repo github.com/<owner>/<repo>`（pr checks），避免控制器环境 `GH_HOST` 指向 GitHub Enterprise 时误拒或误证。第十一轮（Codex 复审 f8e90ce）再闭环 2×P1+1×P2：① 回执要求**该 head 存在已完成的 Codex 审核记录**（codex-review-state=completed 且 commit_sha 匹配——空线程列表 ≠ 已审核）；② 应急豁免须**操作员身份与能力**（`--operator <id>` 独立于工位 agent + `HERDR_OPERATOR=1` 环境标记，工位环境不具备），事件记录 operator；③ deferred 回执在**完成转换时复查 deferral 依据**（remote 状态变化即拒绝）。第十二轮（Codex 复审 d5093a1）再提 1×P1 并已闭环：deferred 依据侦查先查工作流 `pull_request_created` 交付记录——**已存在交付 PR（即使本地 remote 被移除/不可读）时 deferred 一律拒绝**，须按 PR 路径验证并录正常回执。第十三轮（Codex 复审 f211c1d）再闭环 2×P1：① review-state 标记仅接受 **Codex App/bot 作者**（普通用户伪造标记不再能通过）；② 完成校验在 forge 复查后**再次核对 clone HEAD**（校验期间 head 前进即拒绝，收窄 TOCTOU）。第十四轮（Codex 复审 5acbda5）再闭环 2×P1：① 同一 head 的多条 review-state 标记取**最新时间戳**并要求 completed（新增 running 覆盖旧 completed 的用例）；② 交付身份核对**精确到 PR 编号**（交付记录为 /pull/1 时 /pull/999 即使同 sha/同 checks 也拒绝）。第十五轮（Codex 复审 0d77162）再提 1×P1 并已闭环：交付身份回退到 remote 时改为**收集全部 GitHub remote 身份做成员匹配**（fork 回退 origin=fork/upstream=target 不再因迭代序拒绝合法 upstream PR；未配置的第三方仓库仍拒绝）。 |
