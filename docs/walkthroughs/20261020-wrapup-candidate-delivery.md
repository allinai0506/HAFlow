# 收尾新增提交的候选与交付闭环

目标：默认开发工作流在已认证 X 后追加知识/文档/审核修复 Y，正常进入最终候选认证、平台 PR、同 head 回执、wrapup 与 workflow completed。终点为作者可审查的 PR，不合并、不部署。

## 根因与改动

旧入口形成依赖环：commit 需要 completed，integrate 需要 committed，PR 需要 integrated；wrapup completed 又需要与 clone HEAD 和有效交付 PR 完全一致的审核回执。新增 Y 后，X 的证明过期，Y 尚无合法集成和认证身份。两个默认模板的 RED 均在真实 CLI commit 被拒绝。

最小方案保留所有通用状态边，把 wrapup 的 Git 交付阶段独立记录在原 Task payload。`herdr/wrapup_delivery.py` 负责准入、Run/execution/父 episode 与三轮预算；现有 commit/adoption/integrate 保留真实 Git、基线、归属和重基逻辑。集成前 HEAD 必须等于正常 commit 登记的 Y（恢复时为已持久化 S），正常 Git rebase 负责保留提交内容；Git 副作用前以 Task version CAS 记录固定输入 HEAD/基线 SHA 的重基意图。成功重基到 S、尚未写库时中断，正常 commit/integrate 会在无网络、禁 hooks 的临时共享对象克隆中重放同一输入与固定基线；完整树一致后 CAS 收编 S，拒绝未登记推进或回退。主干再次前进时先恢复旧意图，再登记新基线重基。clone HEAD 自然落在重基结果 S。

| 步骤 | 归属 | 持久证据 | 推进条件 |
| --- | --- | --- | --- |
| 登记 Y | 原活跃 wrapup | registered/committed + 父 episode + source_sha | X 有有效认证，分支和身份可证 |
| 集成 S | 同一 wrapup | task-owned refs、integrated_commit、publication | 正常重基及交付内容校验 |
| 冻结 S | Controller | 新 freeze episode | 消费当前 execution 的合法 publication |
| 重新验证 | 新独立 test/review，SPEC acceptance | 新 Task/Run、verified SHA、业务验收 Artifact | 普通退役与 dispatch，旧异步结果不属于新轮次 |
| 交付身份 | wrapup 正常命令 | record-delivery 显式 supersedes | 本轮门禁与业务验收齐备 |
| PR | 平台 create-pr | 候选分支、PR receipt、前驱 URL | 精确 S 与 task-owned refs；库存查询恢复未知结果 |
| 完成 | 原 wrapup、既有 workflow close | 同 Run/execution/episode 的 S 回执、completed | 实时 forge CI/审核、HEAD 与原有关闭条件 |

`services/controller/candidate.py` 识别 wrapup publication；`advancement.py` 在就绪计算前冻结、规划强制双 fresh、恢复旧验证退役及 dispatch。无新增提交保持原路径。候选专属发布分支与集成分支分离，因此已有 X PR 不被覆盖；Y/Z 各通过平台合法创建/恢复替代 PR，保留原校验。

默认模板与仓库 six-step-finish 明确正常 commit/integrate、等待、record-delivery/create-pr 和恢复协议。属于 PR 的内容认证前固定，最终运行报告在 clone 外 checkpoint 发布，避免落回执后再提交报告的循环。

## 验证与边界

回归使用真实临时 Git（包括本地 bare remote）、SQLite、可执行 CLI、实际 Controller 冻结/调度、实际 Artifact/acceptance/verdict/交付/回执与 close 入口。合法历史 X 可以构造；新候选没有预填 integrated/pass/交付/PR/审核事件。仅外部 Agent 启动/执行智能与 forge 服务使用替身。

已观察 RED：`pytest -q tests/test_wrapup_candidate_delivery.py` 首轮 **2 failed**，分别对应两个默认模板，错误为 `Task cannot commit from status: working`。受影响基础专项曾 **180 passed**。最终验证命令、精确结果和候选对应关系在本次审查证据中记录；测试范围包括原路径、两种 PR 起点、Y→S 重基、Z、错误证据、CI/审核阻断与关键中断重试。

本次没有启动真实收费 Agent，没有修改生产任务/数据库或重启服务；假 forge 通过不代表生产 forge 已验收。仓库变更 PR、CI 和作者是否合并另行报告。

独立审查另补三组 RED→GREEN：同文件不同 hunk 的主干/Y 重基、SPEC 缺少或 blocked 新 acceptance、同 HEAD 回执后脏工作树。第三轮发现精确重基落库中断窗口后按仓库规则升级，作者批准限定修复；新增正常入口恢复、期间 main 再次前进、错误树拒绝回归。

## 可重跑的集中验证

```bash
pytest -q \
  tests/test_wrapup_candidate_delivery.py tests/test_wrapup_review_closure.py \
  tests/test_fix_1017_pr_delivery_joint_candidate.py tests/test_fix_bug1002_delivery.py \
  tests/test_task_cli_delivery.py tests/test_task_cli_integration.py tests/test_task_cli_launch_preflight.py \
  tests/test_reverification_controller.py tests/test_reverification_core.py tests/test_reverification_v1.py \
  tests/test_scheduler_facts.py tests/test_auto_acceptance.py tests/test_task_cli_workflow_close.py \
  tests/test_workflow_close_claim.py tests/test_delivery_lifecycle.py tests/test_task_delivery.py \
  tests/test_delivery_rework.py tests/test_controller_modularity.py
python3 -m compileall -q herdr services tests
python3 bin/herdr-task --help
git diff --check
```

测试覆盖错误 PR/旧 head/跨执行身份、fresh test/review 与业务验收拒绝、SPEC 新 acceptance 缺失或 blocked、CI pending/转红、P1 未决、Z 再认证、冻结/派发/集成发布中断、外部 PR 创建响应丢失及 rebase 落库前中断。软件模板不配置 acceptance，相关两个 SPEC 专属用例显式 skip；无新增提交对照仍走原路径。

## 本次隔离闭环对应关系

以下为本轮实际运行记录，SHA 显示前 12 位；每行独立临时数据库中的 workflow=`wf`、execution=`exec`，不是相互拼接的生产证据。`no-pr` / `existing-pr` 是 X 的两个起点；`rebase` 包含主干与 Y 对同文件不同位置的改动，S≠Y。重基用例完成后另经正常 `close-workflow wf` 验证 workflow completed。

| 默认模板 / 起点 | X | 源 Y | 最终 S | wrapup / workflow |
| --- | --- | --- | --- | --- |
| software-development-v1 / no-pr | `4c3dd8bf9688` | `56019083ff02` | `56019083ff02` | completed / completed |
| software-development-v1 / existing-pr | `d547cd2d0161` | `aea2ed62c31b` | `aea2ed62c31b` | completed / completed |
| software-development-v1 / rebase | `f333e9982545` | `7df159474f9a` | `903e15b62d83` | completed / completed |
| spec-development-v1 / no-pr | `aaf14fc88114` | `d7a7d53213a3` | `d7a7d53213a3` | completed / completed |
| spec-development-v1 / existing-pr | `facee4f9dbe6` | `db9c5917bb3e` | `db9c5917bb3e` | completed / completed |
| spec-development-v1 / rebase | `ad7052941bcb` | `d5bb1df5bd1f` | `1f72da85e8d4` | completed / completed |

每行均有：冻结 SHA = 新 test/review 的 candidate_sha = verified_candidate_sha = 有效交付身份 = 平台 PR head = forge 替身实证 head = 审核回执 head = 完成时 clone HEAD = S；SPEC 还包括同 episode 的新 `old-acceptance-r2` pass/业务证据。test/review 分别为新 Run 的 `old-test-r2`、`old-review-r2`，不是重写旧任务。运行记录完整 SHA 与事件 payload 保存在本地 `.omc/wrapup-candidate-evidence/`；PR 中只保存说明，临时运行产物不混入交付候选。

## 最终本地结果与独立审查

上述 18 文件集中专项：**430 passed，2 skipped，9 subtests passed，458.15 秒，exit 0**。两个 skip 为 software 模板不配置 acceptance 的 SPEC 专属用例。`ruff check --select F`（所有本次 Python 改动）、`compileall`、无扩展名 CLI `--help`、`git diff --check` 均通过。

精确重基落库中断新增回归在修复前 **4 failed**；修复后恢复/错误树/非法回退专项 **8 passed**。独立评审另以原反例确认正常重试退出序列 `[1,0,0,0]`，并运行扩展专项 **16 passed，2 skipped**。Standards/Google 与 Spec、独立不同会话复审均未发现剩余确认阻塞；没有把自审称为独立审查。

缺少可调用的 health/interrogate 工具已在开工门禁记录明确替代方式：静态检查、真实失败回归、受影响专项与独立审查；没有虚构评分。当前完成的是本地隔离调用链验证，真实 GitHub 变更 PR 的合并结果全量 CI 另行核对；没有生产任务、付费 Agent 或上线验证。
