# Six-Step Finish 收尾报告 — wf-haflow-1015-01（Experience Hub V1 合并后 Hotfix）

- 收尾节点：wrapup（7 收尾）· 单工位（1 Task / 1 Pane）
- 收尾执行者：qodercli（独立视角，未复用实现阶段 opencode / codex Agent）
- 冻结候选：`c9dee79ac9b9c3659513e8f1de0e1468265236f1`
- 基准分支：`main`（`origin/main = b4c3a11a`，交付分支相对 base 4 个提交）
- 交付分支：`fix/experience-hub-postmerge-hardening`（== `c9dee79`）
- 收尾分支文档提交：`4f73a61`（在独立 docs 分支 `agent/qodercli/docs-wf-haflow-1015-01-wrapup-auto` 追加知识，候选 `c9dee79` 未被改写）
- 生成时间：2026-10-16

## 0. 门禁汇聚确认（同一 candidate_sha）

test 与 review 均基于同一候选 `c9dee79` 判定 pass：

| 阶段 | 任务 | 候选 SHA | 门禁 | 关键证据 |
|---|---|---|---|---|
| test | wf-haflow-1015-01-test-joint-r2 | `c9dee79` | pass | 全量 `pytest -q` 4409 passed / 0 failed（775s，exit 0）；边界抽查 8/8 PASS |
| review | wf-haflow-1015-01-review-joint | `c9dee79` | pass | 独立评审零阻塞缺陷；Ruff 零新增（collector 67→67，hub 51→53 均风格建议）；工作树只读 |

内容等价性（前阶段已证）：`git diff 68e5c5c c9dee79 -- <5文件>` 输出 0 字节（两轮同一内容）。

## 1. 六步状态表（步骤 / 执行者 / 命令 / 结果 / 证据）

| 步骤 | 内容 | 执行者 | 命令 | 结果 | 证据 |
|---|---|---|---|---|---|
| 0 | 交付 PR（前置） | 流水线控制器 | Worker 禁 push/PR（`origin` push=`DISABLED_FOR_WORKER_LOCAL_TEST_ONLY`） | ⛔ DEFERRED（控制器收编） | 见 §3、§4 |
| 1 | 知识沉淀 | Agent 调 knowledge-capture | 追加 `docs/lessons/lessons-learned.md` §160 + commit | ✅ 完成 | commit `4f73a61`（+45 行） |
| 2 | Wiki checkpoint | 脚本 + Agent 判断 | `finish-task.sh` 无 `.wiki/WIKI.md` 自动跳过；按 AGENTS.md 治理回填 `wiki/` | ✅ 完成 | `wiki/enterprise-experience-hub.md`(+3)、`wiki/log.md`(+7，append-only) |
| 3 | 合并确认 | 脚本（只读） | `finish-task.sh fix/experience-hub-postmerge-hardening --base main --forge none --dry-run` | ⛔ 尚未合入 → DEFERRED | 脚本输出：`⛔ 分支…尚未合入 main`（exit 1，预期） |
| 4 | 锚点同步 | 脚本 | `--dry-run` 未执行（合入前不得切锚点/删分支） | ⛔ 待 base 合入后执行 | close-workflow 职责 |
| 5 | 分支核对 | 脚本 | `--dry-run` 未执行（严禁收尾删分支） | ⛔ 待 base 合入后执行 | close-workflow 职责 |
| 6 | 卫生检查 | 脚本 | `--dry-run` 未提交变更仅 worker 身份文件（非交付物，不提交） | ✅ 工作树干净（业务文件） | `git status`：仅 `.agent-task-context`、`.herdr-launch-identity.json` 未跟踪 |

> 说明：本工作流为「单工位收尾」，且远端 push 被平台禁用、交付由控制器统一收编，故步骤 0/3/4/5 合规记为 ⛔/DEFERRED，不计为收尾失败；破坏性步骤（4-6 的删除）在 base 合入后由 close-workflow 执行。

## 2. 最终交付成果说明与修改文件清单

三项遗留问题均有修复代码 + 可复现回归测试（候选 `c9dee79`，相对 `b4c3a11a` 恰 5 文件）：

| 问题 | 根因 | 修复位置 | 对应测试 |
|---|---|---|---|
| P1-01 认证前读无限请求体 | `do_POST()` 先 `_parse_body()` 后鉴权；按客户端 `Content-Length` 无上限读 | `herdr/experience/hub_server.py`（+107）：先 401 后读体；`MAX_REQUEST_BODY_BYTES=1 MiB`；`_parse_body`/`_reject` TE→400/缺长→411/非法或重复→400/超预算→413/截断→400，显式关连接 | `tests/test_experience_pr211r24_hub_body_limit.py`（+783） |
| P2-01 相对 DB 路径跨进程错乱 | `_resolved_db_str()` 对显式/默认路径未绝对化 | `herdr/experience/collector.py`（48 行）：`_absolute_db_path`=`abspath(expanduser())`；入队持久化每行所属库路径；`_row_state_db_path` 按行恢复；缺字段兼容回退 | `tests/test_experience_pr211r25_collector_db_path.py`（+191） |
| P2-02 普通异常不限定 Run | `process_task_settlement()` 异常分支 `_note_collection_failure(task_id,…)` 无 Run ID，批量污染同 Task 其他 Run | `herdr/experience/collector.py`：`_note_collection_failure` 必填 Run 身份；`process_pending` 透传 `queued_run_id`；终止/异常/退避三分支一致只改 `(task_id, run_id)` | `tests/test_experience_pr211r26_collector_run_isolation.py`（+148） |

变更合计：5 files changed, 1252 insertions(+), 25 deletions(-)。新增依赖：无（仅 stdlib `socket`）。收尾追加：docs/wiki 3 文件 +55 行（commit `4f73a61`）。

## 3. 交付 PR URL 与合并指引

- **PR 状态：DEFERRED（控制器收编）** —— 本 Worker 克隆的 `origin` push 被平台禁用，且收尾节点红线禁止 Worker 执行 `git push`/自建 PR；远端推送与建 PR 一律经流水线控制器交付入口（`herdr-task create-pr <integrated-task_id>`）执行。故本工位未生成 PR URL，如实记为 DEFERRED，不伪造链接。
- **合并指引（供评审/作者执行）：**
  1. 由控制器对候选 `c9dee79`（交付分支 `fix/experience-hub-postmerge-hardening`）经 `herdr-task create-pr` 推送并创建待审查 PR → 目标 `main`（base `b4c3a11a`）。
  2. PR 描述须含：三项根因/修复位置/对应测试、测试结果（见 §4）、兼容性说明（HTTP API/SQLite 历史/Collector 恢复）、变更摘要（5 文件 +1252/-25，commit `c9dee79`/`15c57c0`/`06b2fa2`/`43dfbf4`）。
  3. 严禁自动合并；合入由作者/评审决定。合入后由 close-workflow 执行物理清理（步骤 4-6）。

## 4. 测试结果（本工位第一方独立复跑）

环境：`python3.13` + pytest 9.1.1（`.venv`/homebrew）。候选代码与 `c9dee79` 逐字节一致（`git diff c9dee79..HEAD -- herdr/ tests/` 空）。

| 命令 | 结果 |
|---|---|
| `pytest -q tests/test_experience_pr211r24_hub_body_limit.py …r25… …r26…` | **45 passed** / 0 failed（30.0s） |
| `pytest -q tests/test_experience_hub.py tests/test_experience_collector.py tests/test_experience_integration.py` | **24 passed** / 0 failed（8.0s） |
| `pytest -q tests/test_experience_pr211r*.py` | **119 passed** / 0 failed（49.0s） |
| `git diff --check b4c3a11..HEAD` | clean |
| `python3.13 -m compileall -q herdr services bin tests` | ok |

完整套件 `pytest -q`（4409 passed / 0 failed / 3 warnings / 163 subtests）由 test-joint-r2 在同一候选 `c9dee79` 上执行并 pass；本工位独立复跑覆盖全部验收强制子集（188 项）+ 静态检查，无回归、未删/跳测试。

## 5. 遗留问题与后续建议

- 无已知阻塞缺陷。三项修复行为级回归充分，HTTP/SQLite/Collector 兼容性未破坏。
- 建议（非阻塞）：hub_server Ruff 新增 2 条为风格建议（UP045 `Optional[...]`、UP041 `TimeoutError`），与既有全文件风格一致，可择机统一处理，不影响门禁。
- 411/413/400/401 拒绝路径依赖客户端 `Content-Length`/`Transfer-Encoding` 语义正确性；生产建议前置反代/网关统一限流兜底（V1 范围内不强制）。

## 6. 门禁结论

**HERDR_GATE_VERDICT: pass** —— 全流程交付物完备，test 与 review 基于同一候选 `c9dee79` 均 pass，本工位独立复跑验收子集全绿、静态检查干净；PR/合并属控制器职责，记 DEFERRED，非收尾阻塞。
