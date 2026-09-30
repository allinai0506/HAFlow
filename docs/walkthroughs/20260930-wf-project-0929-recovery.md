# wf-project-0929-01 卡点恢复

## 范围与路线
修复当前实现阶段反复阻塞，保留原 Controller 和任务分支；独立修复沙盒 `fix/workflow-runtime-stall-recovery`。Bug 路线 S0→debug→TDD→验证→独立评审→本机恢复；用户后续授权提交PR；合并与产品生产部署不在本轮范围。

## 已证实根因
1. Worktree 来源 CoW 复制 `.git` 指针，三个 Clone 共享 Gemini HEAD/index；T6 切分支使 Barrier-0 提交被 agent mismatch 拒绝。
2. 评估器此前把绿色用例名中的 FAIL 误当失败（本轮PR纳入已有 evaluator 修复及10个subtests回归）。T1 第6轮、T6第5轮真实 eval 各3961/3961、lint0。T6中间出现1个SSO waitFor超时，定向3次均通过，后续全量通过；未改该用例或放宽断言。
3. Herdr runtime done 是可输入的完成态，纯策略及SQLite observation/CAS仅认idle，导致EARLY busy误判。
4. 本地 `agent/gemini-init` 被integrate误当origin远端分支，完成后卡在committed。
5. T6-r2派发前TOKEN_EXHAUSTED，无Clone/commit，遗留failed占位；已superseded且不建立跨Run replacement。

## 恢复证据
- 原Git指针、共享index/HEAD、源暂存/未暂存patch、运行代码副本：`~/.herdr-controller/workflows/wf-project-0929-01/recovery/20260930-103536/`。
- 三个Clone恢复独立.git与原任务分支，保留工作文件并恢复提交钩子配置。源恢复`agent/gemini-init`；之前评估器workaround的一行测试改名已备份并恢复，真实业务未改。
- Barrier-0方案真实提交`9238a557`。
- T1真实提交`cdd1c117`，集成`760d2b02863380e4a03cf719e9712c05a534693f`。
- T6真实提交`8655e5d9`，集成`2e4d1d46e877c2fef89cdddd037377ac08636127`。
- Controller/Sentinel使用launchctl kickstart优雅重载；新Sentinel记录COMPLETION READY，真实状态committed→integrated，不以force-pass恢复完成。
- shared evidence `n-1790736033754-a6aa`、`n-1790736751737-a020`；共享文件归属decision `n-1790736895633-7658`。
- 下一波：T2独占FondsPredicate，T3等待真实合流，其他无重叠任务可并行。

## 验证
RED：Worktree `.git.is_dir()`失败、done被分类early、远端anchor不存在，均修复前复现。GREEN：完成/身份/并发/CAS/worker相关119 passed；集成51 passed。首次现场恢复版本全量：2429 passed、50 subtests passed，0 failed/0 skipped。PR追加评估器修复后，最新全量 `python3.13 -m pytest -q`：2432 passed、60 subtests passed，0 failed/0 skipped，312.60s。受影响专项188 passed、10 subtests passed（19.58s）。独立评审覆盖最新四项完整差异，未发现阻塞缺陷。compileall、无扩展名CLI AST/--help、diff-check通过。独立只读评审无已确认阻塞。

## 边界与回滚
集成refs不是远端dev合并，工作流test/review/wrapup尚未验收。本轮按用户授权提交PR；未合并、未进行产品生产部署。当前源明确使用core.hooksPath=.husky/_；默认.git/hooks迁移及非当前local base离线fallback未验证，不声明兼容。源码回滚用runtime-code副本恢复五个文件并优雅重载服务；保留已产生任务/证据与真实成果，不把状态强行倒退。

## 恢复后真实节点观察
T2 `impl-t2-archive-ocr-provider` 与 T5 `impl-t5-frontend-workbench` 均已 working，基线 `4ccbbf331b8420006fd480eb1e8ceb185e2daae1`。现场核验两个 `.git` 均为独立目录，分支与 Task 元数据一致；T2 可读真实 T1 契约及方案，hook配置 `.husky/_` 保留，源工作区 tracked clean。T3 按共享文件依赖等待 T2 合流；test/review/wrapup 尚未执行，不声明整个工作流完成。
