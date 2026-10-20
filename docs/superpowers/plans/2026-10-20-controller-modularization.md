# Controller 模块化实施计划

**目标：** `services/herdr-controller.py` 不超过 500 行，保留现有功能、函数签名、环境变量和工作流执行顺序，按职责降低维护成本。

**基线：** `6417047aa9b78c013bda5f12d7f943792dcc4ef0`；继续 `refactor/split-controller-20261019` CoW 现场，原文件 10,946 行，本轮开始时 10,238 行、231 个顶层函数。

2026-10-20 同步后隔离分支快进至 `68c61d8`：上游增量仅为本体元数据，未覆盖本任务源码。主工作区保持原状。

**授权：** 实现阶段为修改、测试、文档；2026-10-20 用户追加“创建PR”，授权提交、推送和创建 PR，不包含合并或部署。

## 依赖分析与设计

实际入口为 main → coordinator worker / registry watcher → 调度、事件、监督、终化 → 既有 StateStore / node dispatch store → 公开状态读取。SQLite 为权威来源，JSON、Pane、进程内集合保留既有职责。

顶层函数调用图未发现递归环。耦合集中在可变配置、队列、锁、运行资源和跨职责函数引用。保留每次入口加载各自独立的共享对象，迁出模块不创建第二份状态。兼容装配使用明确声明、限制访问的依赖视图，以保留现有调用方替换函数和配置的行为；禁止 exec、源码拼接加载、函数 globals 复制和对入口的反向导入。

模块按以下变更原因归组：持久化读取与状态转换；attention 与 blocked SLA；节点策略与阶段状态；候选冻结、reverification、join gate；直派、恢复、阶段推进；选择性返工与 merged fix-loop；协调器传输、阶段投递、普通 Task event；验收与 verdict 读取；Git 终化与重试；监督动作与证据；协作投递与节点交接；任务 listener、运行对账与 registry sweep。

## 分批迁移与检查点

- [x] 固定源码快照，记录原始签名与函数语法树；添加入口行数、依赖边界和兼容性验收，先确认当前状态不满足 500 行。
- [x] 提取任务读取、attention、blocked SLA、验收/verdict、completion；运行 completion、blocked、auto acceptance、liveness 专项。
- [x] 提取候选、阶段状态、直派、恢复、返工和推进；运行 scheduler dispatch E2E、node dispatch、downstream、fix-loop、continuation 专项。
- [x] 将 stage admission/wait/delivery/receipt 与普通 Task event 分开；保留各分支语句和顺序，运行 stale、episode、decision timeout、coordinator intake 专项。
- [x] 提取 Git 终化、监督、协作、listener、registry watcher；运行对应真实入口/临时 SQLite 调用链专项。
- [x] 收缩入口为配置、资源初始化、兼容绑定和 main；检查 500 行门槛、反向导入、逻辑依赖环、签名、共享状态和可独立调用性。
- [x] 当前版本静态检查、完整受影响 Controller 专项；记录测试环境/失败归因，更新架构与证据。

## 行为验收

1. 所有原有函数保留位置兼容和签名；独立加载入口的状态、锁、队列互不污染。
2. `_handle_coordinator_item` 对 continuation、fix-loop、stage advance、Task event 的分派顺序不变。
3. stage dispatch 的 admission → lease → wait → launch → receipt 顺序不变；遗留未注册 workflow 分支不增加持久化调用。
4. Task event 的自动验收、stale 检查、blocker episode 重投、等待和队列清理顺序不变。
5. 现有测试从真实入口调用，保持核心调度和临时数据库；只替换 Agent/终端/外部传输。
6. `python3 -m compileall -q herdr services bin tests`、`git diff --check` 通过；本地不启动真实服务和 Agent。

## 风险与证据

迁移容易遗漏嵌套作用域、global 写入、默认参数、运行路径和猴子补丁语义。以冻结源码的 AST 归一比较辅助检查语句未变，以现有行为测试证明生产入口接线，二者不互相替代。新模块使用正常 Python 导入，可直接传入其声明依赖独立运行。

本轮将当前版本的源码扫描测试与本体 evidence/logic_ref 迁到实际 owner；Finding Verifier 的历史 SHA 夹具仍固定历史版本。宽回归中暴露的源码位置失败保留原断言修复，没有修改业务行为来使测试通过。兼容绑定保留普通函数类型与原签名，不使用 partial 作为旧接口对象。

已有工程教训 §177 已覆盖实时 namespace/deps 与源码 owner 迁移，本轮复用该约束，不重复新增相同教训。

## 最终验证与自审

- 入口最终 474 行，迁出实现最大 474 行；保留原始状态生命周期注释。原 231 个函数的参数、默认值与函数类型兼容；声明依赖图无环，所有依赖可解析，实现可脱离服务入口独立调用。
- 分批专项依次通过 170、475、314 项；宽回归最终 92 个文件为 2,214 passed、3 skipped、81 subtests passed，测试期间源码摘要未变化。记录见 `.omc/controller-modularization-final-verification.json` 和对应 pytest 日志。
- 随后自审发现上一轮 blocked-SLA helper 使用模块级资源，导致独立入口相同 task_id 相互去重。已用可控排队 RED 复现，将原线程池/锁/集合恢复到入口，并把原调度函数直接收敛到 blocked_actions，删除重复 helper。资源修正后专项 202 passed、9 subtests passed；覆盖独立实例、异步执行前回调替换和提交失败释放。其他调度/终化/监督/协作操作语法树未变，其宽回归证据可复用。
- 此次业务操作迁移的归一语法树与冻结源一致；blocked 调度函数单独对照原仓库 HEAD 实现完全一致。registry 的最终清理仅删除空行缩进，AST 完全相同。
- 编译、tracked/untracked whitespace 检查和本体审计通过；本体审计为 MAJOR=0、MINOR=0，既有 CLI coverage INFO 保留。
- 这是本地自审与隔离测试证据。实现验收时未执行生产服务冒烟、CI 或独立评审，也未提交、推送、合并或部署；主工作区业务源码未改。

## PR 准备与可复现验证

- 创建 PR 前同步至 `a6b315aee410638574f422de660de78c0d32a314`，包含上游 #280 状态转换网关修改；在该基线上重跑最终实现，追加 D1 特权转换专项。
- 专项清单为同目录 `2026-10-20-controller-modularization-tests.txt`（93 个文件），从隔离仓库根目录运行：

```bash
python3 - <<'PY'
from pathlib import Path
import subprocess
paths = Path("docs/superpowers/plans/2026-10-20-controller-modularization-tests.txt").read_text().splitlines()
raise SystemExit(subprocess.call(["pytest", "-q", "-ra", *paths]))
PY
```

- 原生 `herdr-task create-pr` 要求注册 Task ID；现有手工隔离现场 `verify-baseline split-controller-20261019` 返回 `Task not found`，按仓库回退规则使用 `gh pr create`，不声称获得 Task 基线回执。

- 同步后最终回归：**2,222 passed、3 skipped、81 subtests passed**（497.22 秒）；测试期间实现源码摘要无变化。跳过为本地 HAFlow 状态缺失 1 项及 launchd agent 目录不可用 2 项；4 条警告为多线程进程使用 fork 的 DeprecationWarning。编译、暂存差异空白检查和本体审计均通过。
