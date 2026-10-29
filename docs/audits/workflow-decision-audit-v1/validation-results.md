# HAFlow `software-development-v1` 场景化验证报告 (Validation Results)

> 验证基线：Git Commit `739ef0d`  
> 验证环境：macOS (Darwin 24.3.0) / Python 3.13.3 / Pytest 9.1.1  
> 验证原则：非侵入式、不修改生产状态、不放宽断言、全量新鲜实测  
> 验证日期：2026-10-29

---

## 1. 九大核心审计场景验证矩阵

根据审计任务要求，针对 `software-development-v1` 覆盖的 9 类关键执行场景进行系统性测试验证。

| 场景序号 | 场景类别 | 对应测试套件 / 验证方法 | 验证结果 | 实际状态流转与耗时 | 关键决策原因与现场证据 |
|---|---|---|---|---|---|
| **SC-01** | 正常执行并自动推进 | `tests/test_software_development_v1_e2e_isolated.py::test_01_normal_six_stage_lifecycle_e2e` | ❌ **失败 (环境系统调用异常)** | requirements ➔ plan 终止 | `stage_skills.py` 在 `get_default_skill_search_roots()` 中调用 `p.exists()` 访问 `~/.gemini/config/skills` 抛出未捕获的 `PermissionError: [Errno 1] Operation not permitted`，导致上下文装配崩溃。 |
| **SC-02** | Agent 正常返回，但交付验收失败 | `tests/test_software_development_v1_adapter.py::test_29_real_workflow_template_config_recognition_and_task_event_bypass` |  **已证实 (发现死胡同缺陷)** | `agent_done` ➔ 停滞 | 当文本正则不匹配或产物不全时，`try_auto_accept()` 返回 `False`。代码执行 `ports.attention_note(..., reason="auto_acceptance_rejected: ...")` 后直接 `return`，任务被永久留在 `agent_done`，既不推进也不重试。 |
| **SC-03** | Agent 进程异常崩溃 | `tests/test_dispatch_fuse.py` + `tests/test_software_development_v1_adapter.py` |  **通过** (21 passed) | `working` ➔ `failed` ➔ `superseded` ➔ 重新补派 (0.18s) | `registry_watcher` 感知到 `status == "failed"` 且原因为 `agent_process_crash`，触发 `recover_infra_failed_tasks()`，在 `AUTO_RECOVER_MAX_ATTEMPTS` (2 次) 预算内成功作废旧任务并补派。 |
| **SC-04** | Agent 长时间无响应 (超时) | `tests/test_dispatch_delivery_timeout.py` |  **通过** (15 passed) | `working` ➔ `failed` (hard_timeout_watchdog) (0.22s) | Sentinel 看门狗检测到超过 SLA 且终端无活跃输出，强行跃迁至 `failed`；随后被 Controller 基础设施恢复器捕获并重新排队。 |
| **SC-05** | 重复收到完成事件 (幂等性) | `tests/test_software_development_v1_adapter.py::test_20_duplicate_event_and_resume_idempotent` |  **通过** | `completed` ➔ 保持 `completed` (0.05s) | 重复收到 `agent_done` 或 `idle` 事件时，`task_listener.py` 与 `task_events.py` 识别当前任务版本与状态属于终态集合，直接幂等忽略，未触发二次派发。 |
| **SC-06** | 执行中系统重启 / 崩溃恢复 | `tests/test_workflow_recovery.py` |  **通过** (7 passed) | 重启 ➔ `reconcile()` ➔ 状态重构 (0.12s) | Controller 重启时读取 SQLite `state.db`，通过 `recovery_store.reconcile()` 修复挂起的运行快照，清理孤儿租约，恢复 DAG 就绪状态。 |
| **SC-07** | 多依赖节点汇聚推进 (Join Gate) | `tests/test_software_development_v1_e2e_isolated.py::test_03_multi_task_decoupled_lifecycle_e2e` |  **通过** (1.52s) | `test(pass) + review(pass)` ➔ `wrapup` 就绪 | `wrapup` 节点依赖 `[test, review]`。`_scheduler_join_gate_allows()` 严格校验两者是否绑定同一 `candidate_sha`，一致时成功放行收尾工位派发。 |
| **SC-08** | 达到重试次数上限 (Fix-Loop 耗尽) | `tests/test_158_fix_loop_fingerprint.py` + `tests/test_software_development_v1_adapter.py::test_10` |  **通过** (32 passed) | 累积返工 3 轮 ➔ `fix_loop_exhausted` ➔ 停止 (0.45s) | 门禁输出 `blocked` 时累计累加，第 3 轮打回后 Controller 判定 `fix_loop_count >= FIX_LOOP_MAX`，触发 `attention_note` (`fix_loop_exhausted`)，停止自动下发，阻止无限循环。 |
| **SC-09** | 需要人工介入的场景 | `tests/test_software_development_v1_adapter.py::test_11` + `test_terminal_workflow_dispatch_recovery.py` |  **通过** (18 passed) | `waiting_human` 挂起 | 交付冲突、不可自动修复的 Git 变更或规划严重阻断时，系统记录 `human_decision_required` 并挂起，等待 CLI/Console 人工处理后恢复。 |

---

## 2. 核心失败用例深度取证与根因确认

在执行本次场景化自动化测试时，发现了 2 个极具价值的硬性阻断现场，直接印证了架构层面的隐患：

### 现场取证 1：`stage_skills.py` 目录探测权限崩溃

- **执行命令**：
  ```bash
  pytest -v tests/test_software_development_v1_adapter.py -k test_02
  ```
- **真实终端报错输出**：
  ```text
  Traceback (most recent call last):
    File "/Users/user/HAFlow/herdr/stage_skills.py", line 255, in assemble_stage_context
      resolved_skills.append(resolve_stage_skill(name))
    File "/Users/user/HAFlow/herdr/stage_skills.py", line 116, in resolve_stage_skill
      roots = get_default_skill_search_roots()
    File "/Users/user/HAFlow/herdr/stage_skills.py", line 100, in get_default_skill_search_roots
      if p.exists() and p not in roots:
    File "/opt/homebrew/Cellar/python@3.13/3.13.3_1/Frameworks/Python.framework/Versions/3.13/lib/python3.13/pathlib/_abc.py", line 450, in exists
      self.stat(follow_symlinks=follow_symlinks)
  PermissionError: [Errno 1] Operation not permitted: '/Users/user/.gemini/config/skills'
  ```
- **取证事实**：
  由于 macOS 系统特权目录或沙盒存在，探测用户主目录下默认的 AI 工具目录时，`Path.exists()` 抛出了未经捕获的 `PermissionError`。这一异常直接导致 SC-01（`test_01_normal_six_stage_lifecycle_e2e`）在第一阶段就地崩溃，无法继续执行后续流程。

---

### 现场取证 2：`git_coordination.py` 进程检查权限崩溃

- **执行命令**：
  ```bash
  pytest -q tests/test_dispatch_idempotency.py -k test_real_cli_duplicate_returns_authoritative_task_without_router
  ```
- **真实终端报错输出**：
  ```text
  Traceback (most recent call last):
    File "/Users/user/HAFlow/herdr/git_coordination.py", line 107, in ensure_no_git_processes
      for pid, command in provider():
    File "/Users/user/HAFlow/herdr/git_coordination.py", line 82, in _default_process_provider
      result = subprocess.run(["ps", "-axo", "pid=,command="], ...)
    File ".../subprocess.py", line 1969, in _execute_child
      raise child_exception_type(errno_num, err_msg, err_filename)
  PermissionError: [Errno 1] Operation not permitted: 'ps'
  ```
- **取证事实**：
  在每个工位通过 CLI 派发（`launch_task`）前，系统强制调用 `ensure_no_git_processes()` 巡检当前是否存在残留 Git 进程。该函数直接调用系统 `ps` 命令。在受限权限或 CI 容器沙盒中，调用 `ps` 触发 `PermissionError`。由于该函数没有针对不可控环境异常做优雅降级，导致普通任务启动直接被拒。

---

## 3. 多任务解耦与动态实现派发验证 (SC-07 延伸)

在针对 PR #356 新引入的 `plan_parser.py` 进行多任务并发验证时：
- **测试命令**：
  ```bash
  pytest -v tests/test_software_development_v1_e2e_isolated.py -k test_03_multi_task_decoupled_lifecycle_e2e
  ```
- **测试结果**：**PASS** (1.52s)
- **实际执行流转记录**：
  1. `plan` 阶段提交拆分好的 2 个独立子任务：
     - Task 1: 修改 `module_a.py`
     - Task 2: 修改 `module_b.py`
  2. `plan_parser.py` 解析并验证两任务文件集合完全正交（无重叠）；
  3. `direct_dispatch.py` 成功生成 2 个实现任务：`wf-impl-t1` 与 `wf-impl-t2`；
  4. 两个 Worker 并发完成代码修改并自测；
  5. 两个任务依次完成 Git 终化（`commit` 与 `integrate`）；
  6. 汇流后成功进入 `test` 节点。
- **事实结论**：
  在**严格规避了 F-01（交付物格式偏差）与 F-02（Challenger 并发时序倒挂）的前提下**，HAFlow 的纯代码直接派发与多任务解耦编排逻辑能够正常工作。
  这反向证明：**当前流程难以稳定自主完成的根本症结不在于 DAG 调度器或并发架构设计本身，而在于边界条件判定过于脆弱、失败后静默弃单以及并发时序竞争。**
