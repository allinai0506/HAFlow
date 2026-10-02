# Workflow Reliability Implementation Plan

> 主控按 unified-dev-flow / executing-plans 推进；独立文件域按 dispatching-parallel-agents 分工，共享入口由主控整合。

目标：实现获批设计的六项可靠性改进。基线与授权见设计文件；交付目标 working_tree，不推送、合并、部署或重启。测试在临时 HOME/SQLite/Git 中执行，外部 Provider/UI 只替换传输边界，不启动收费模型或真实 Agent。

## 实施与真实接口

| 单元 | 实际入口与实现 | 关键反例与验收 |
|---|---|---|
| 完成 | report-completion → completion_receipt → 既有状态 CAS/执行门禁 → Controller 恢复 | 无终端标记、跨 run/epoch/token、60 秒下限、过期拒绝、VERIFY 未执行、独立消费者、凭据及投递崩溃窗口 |
| 预检 | deep_preflight / agent_adapter.startup_readiness → Worker 真实请求及实例就绪 | exit0 空/回显不 READY；认证/模型/配额分类；配置与大二进制身份；信任对话不派发，未知资源保留 |
| 派发 | workflow.validate_artifact_contract → CLI / direct_dispatch → launch intent + 既有跨进程锁 | 自动/手动相同角色去重；合法角色/返工；歧义 wrapup 配置提前拒绝；真实资源恢复核对 |
| 长任务 | checkpoint-publish/read/aggregate → task_checkpoint / Observation；tool-run → bounded_tools | 分段发布中断与内容改写；当前 epoch 源查询限量；NUL/超时/截断；受管子进程 HOME 与数据库路径隔离 |
| 版本/关闭 | install-herdr-console → service_release；close/reopen → workflow_close | 完整 plist 参数与根；archive 内容/执行权限/字节码校验；独立进程关闭竞争；Pane 重用；dry-run 不冒充执行 |
| 交付 | 实际 herdr-loop → evaluation_identity / EVAL_DONE → evidence / Controller → delivery-report | Worker 内部文件排除；源码前后变化及旧身份拒绝；真实 pass/fail/skip 与退出码保留；缺候选/产物/生产回执 unknown |

必要增量范围：completion_receipt、task_checkpoint、bounded_tools、service_release、workflow_close、delivery_report、evaluation_identity、supervisor_delivery。它们使用既有 SQLite/Observation/events；没有第二状态事实源。共享执行门禁在 completion.py；内部文件分类复用 repo_hygiene / git_adoption。

## 已执行步骤

- [x] 核对已有任务、基线、运行版本及授权；CoW 隔离，不覆盖原监控现场。
- [x] 用户确认设计与编码；锁定工作树交付边界。
- [x] 六单元分别 RED → 最小实现 → GREEN；失败日志保留，不将 parser 错误或旧绿色记录当作验收。
- [x] 真实 CLI / Worker 装配 / 临时 Git / SQLite / Controller / 报告联合专项。
- [x] 独立进程、Event/Barrier 并发与崩溃窗口；不以单线程重复调用证明并发安全。
- [x] 删除废弃自动信任写入与无用变量；不引入新框架或依赖。
- [x] Standards / Spec 同模型审查与两种模型 adversarial 审查；具体反例反馈到实现与回归。
- [x] 更新相关 Wiki、四段式教训与同 UID 信任边界。

## 最终门禁

- [x] 最后恢复路径源码冻结及独立修复复审。
- [x] 当前源码映射指纹绑定专项、全量 pytest、compileall、全部无扩展名 CLI AST/help、Bash 语法和 diff-check。
- [x] 同一规则集对照锁定基线和当前源码 Ruff F，新增问题清零；无数值 code-health 工具不伪造评分。
- [x] 交付记录逐项列结果、日志与未验证项；不把工作树验证写成部署/生产验收。

最终执行记录在 `.omc/`；可长期引用的报告存放于本次 walkthrough。缺环境的检查明确标记 skipped/unknown。最终 full 前的初始 10 个失败已用于定位兼容夹具与实现问题，不是最终通过证据。

最终证据：56 个源码/测试文件映射无漂移；全量 3134 passed、2 skipped、157 subtests passed（449.15s），退出 0。两项 skipped 为隔离 HOME 下已安装 LaunchAgent 只读检查；临时安装器测试已执行。全部 CLI/compile/Bash/diff 退出 0；Ruff F 基线36、当前36、新增0。三轮规则已升级人工，用户明确认可边界继续本地验收；完整记录见 walkthrough。
