# 工作流交付与仓库门禁闭环

状态：用户已批准，隔离工作区实现及回归、独立评审完成；尚未提交、部署或恢复生产工作流。任务：auto-workflow。分支：feat/auto-workflow。基线：704264a0cd1bd7f2e7871834ac2a4503a252e9e8。

## 已验证的问题

原分支 agent/opencode/feat-wf-nexusarchive-1007-01-impl-atomic-fix 命中 NexusArchive bugfix 门禁。实际 initial_dispatch_prepared 提示词明确“只改 AdminOrgController.java、AdminOrgWriteMethodSecurityGuardTest.java、AdminOrgControllerPermissionMatrixTest.java”，但门禁要求额外提交复盘文档。任务范围与工程交付要求冲突，不能自动扩权。

临时 Git 仓库使用同一分支，暂存源码及测试，运行原 scripts/git-hooks/bugfix-engineering-gate.sh，退出1并输出“缺少缺陷复盘文档”。未修改原Task或生产状态。

Controller日志670008–670331记录重复提交及耗尽。finalize_completed_task把普通提交失败当retryable；finalize_commit_error只保留开头500字，实际存的是Node版本检查，末尾门禁原因丢失。

本次只读查询时，operation27965已经superseded；Task仍completed、commit为空、finalize_escalated已清除。历史waiting_human不是当前状态。

## 方案比较

| 方案 | 收益 | 局限 |
| --- | --- | --- |
| 仅补提示词 | 修改少，提醒工程产物 | 无程序检查，收尾才发现冲突 |
| 交付要求、检查、有限返工（推荐） | 前置暴露冲突，按真实失败补齐产物 | 需要身份、状态、持久化验证 |
| 更名或放宽门禁 | 降低表面阻塞 | 不符合任务目标，不采用 |

## 推荐设计

### 派发前明确交付

复用节点rules、required_outputs、Task goal/acceptance与仓库配置入口，明确工程产物、文件范围、验证命令及规则来源。仓库或协调者登记可执行要求，不在HAFlow硬编码NexusArchive正则、目录或profile，不通过模型解析shell取得权限。

首次、直接、返工、恢复派发使用相同交付说明边界。仓库要求与明确文件范围冲突时，开工前报告最小调整，协调者明确授权后再执行；不能默认文档是范围例外。只读评审、测试、context任务不获得工程写入权限。

### 完成前验证

Agent声明、业务验收、Git交付、冻结候选保持独立语义。完成声明前执行已配置且授权的交付检查，记录真实命令、退出码、缺失项和内容指纹。缺产物、环境未知、范围冲突不能取得交付就绪。

复用Task基线及检查设施，git status和自述不作为通过证据。回执绑定Task/Run/Workflow执行代次及源码、配置、分支、暂存内容；变化后旧通过失效。仓库没有安全preflight时标记未验证，实际commit hook继续作为最终权威。不擅自运行有副作用的hook或借preflight自动提交。

### 失败诊断与恢复

复用HERDR_COMMIT_RESULT、事件和恢复operation，保存命令阶段、退出码、脱敏后有界输出尾部及证据引用。不能只保存开头成功日志。

可靠结构化检查拒绝转入工程交付恢复责任，内容未变时不重复同一commit五次。普通rc=1不自动认作hook缺失；Git锁保留等待契约，未知错误保留有限重试及人工核查。

未提交交付返工与冻结候选测试分开。无commit时针对原工位补齐工程产物，不能伪造candidate_sha或交给test冒充冻结候选。现有cmd_rework不接受completed，需要明确受限入口，不能开放所有终态。条件包括未提交/未集成、同Task/Run/执行代次、工位所有权已验证、无活跃或未知提交、范围已授权、责任未转移。

复用claim/lease、request_id、owned_live_pane及receipt-v1：先持久登记责任，外部副作用前重验身份，传输未知保留核查而不换request_id重发。重复事件、重启及旧回执不能重复派发或覆盖新Run。自动返工最多三轮，仍失败转人工；范围冲突立即等待裁决。

补齐后重新验证并通过原commit → integrate → freeze candidate链。发出返工指令不代表交付成功，不自动推进阶段。

## 范围与真实入口

预计修改bin/herdr-task、herdr/supervisor_delivery.py、herdr/direct_dispatch.py、services/herdr-controller.py、herdr/workflow_recovery.py、herdr/recovery_store.py。仅必要时调整herdr/transitions.py。必要纯契约收敛于NEW: herdr/task_delivery.py，不新建调度框架或事实数据库。实施前固定最小schema及兼容策略，并收紧文件清单。

真实链路：CLI派发 → 持久派发回执 → Worker交付检查 → 完成声明 → Controller提交 → 仓库hook → 失败回执/恢复责任 → 同工位补齐 → 重新提交集成 → 冻结候选。权威仍是Task Store、仓库规则、实际Git结果及既有恢复operation。

当前交付授权为沙盒内修复与验证，不含Push、PR、合并、部署、重启或原生产工作流恢复。不修改NexusArchive门禁及原Task。宿主Entry Gate固定查源仓库.omc，本次在那里仅写入口元数据；设计和后续业务修改均在沙盒。

## 验收矩阵

| 编号 | 场景 | 必须观察到 |
| --- | --- | --- |
| D1 | 三文件范围与复盘要求冲突 | 开工前报告冲突，不自动扩权 |
| D2 | 首次/直接/返工/恢复 | 相同说明，权限按角色区分 |
| D3 | 声明完成但缺产物 | 未交付就绪，不冻结候选 |
| D4 | 长成功日志后hook拒绝 | 回执保留实际失败原因 |
| D5 | 确定拒绝且内容未变 | 不盲重试，建立恢复责任 |
| D6 | rc=1/index.lock/超时 | 不误分类，不伪造PASS |
| D7 | 授权补齐产物 | 重新验证，真实hook通过后才冻结 |
| D8 | 旧Run/双驱动/重启/结果未知 | 无跨Run写入和重复派发 |
| D9 | 只读/context/已集成 | 不获得写入和返工权限 |
| D10 | 三轮失败 | 明确原因的人工待办，保留产物 |

## 实施与证据

先建立临时Git和SQLite回归RED，再最小实现，删除式清理后运行专项与全量pytest -q、python3 -m compileall -q herdr services bin tests、CLI AST及git diff --check。至少一条真实CLI → 核心 → SQLite → Controller → hook → 回执读取集成链。并发使用独立连接和受控交错，不启动真实Agent或收费模型，不写生产状态。

同步任务生命周期、恢复Wiki、追加wiki/log.md及通用教训。自审不称独立审查，源码变化后重验。生产部署与原工作流复跑另需授权及实际证据。
