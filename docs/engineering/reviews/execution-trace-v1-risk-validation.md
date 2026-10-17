# 执行轨迹 V1 风险与验证方案

## 执行顺序与证据归属

按[方案对抗审查](execution-trace-v1-plan-challenge.md)的 I1/I2 文件所有权执行。每条证据保存源码 SHA、命令、临时环境、退出码和断言摘要。先在未修复基线上记录 RED，再在候选上记录 GREEN；此文档是验证计划，不是测试通过报告。

## 风险矩阵

| 风险 | 输入与交错 | 必须观察到的结果 | 责任 |
| --- | --- | --- | --- |
| R1 读取误判 | cat、sed 只读、head、tail 的明确静态文件；搜索、目录、管道、复合 Shell、动态路径 | 前者相对路径 file_read；后者中性工具，无正文或敏感命令参数 | I1 |
| R2 修改伪造 | 原生 apply_patch 受控格式与绑定结果：成功、失败、缺失、部分成功、多文件、路径带恶意文本 | 只对明确成功的目标给 file_edit；未知不猜；命令返回失败不能显示成功修改 | I1 |
| R3 输出错绑 | 并列 exec、多 call_id、输出乱序、重复输出、缺一条退出码 | 不按偶然输出位置把一个命令的成功赋给另一个；不明确就 exit_code unknown | I1 |
| R4 Retry 造假 | 失败测试→显式关联测试→通过；工具失败→工具重试；同命令无关联；不存在、自引、未来、跨会话引用 | 仅第一项再次测试，第二项重试工具调用，非法关联无 retry；不出现修复宣称 | I1 |
| R5 提交时间混淆 | SHA+真实提交事件；SHA+committed；仅 SHA；无 SHA；多次 committed；不同 SHA | 时间分别为真实时间、状态时间、未知；无 SHA 无生成提交；同事实只一条，不合并不同提交 | I1 |
| R6 身份泄漏 | 双 Task 同 clone、旧 Run、复用 Task、身份文件冲突、pinned ID 正确但 cwd 错 | 不读取或不投影错误归属；unknown 不盖当前 run_id | I1/I2 |
| R7 扫描饥饿 | 旧精确会话+超过 800 无关文件；精确未命中；受控单调时钟耗尽 | 已定位且归属正确者优先解析；无关扫描不影响它；未命中有界降级，超预算有说明 | I1 |
| R8 数据源分裂 | 临时 A/B 库同 task_id 不同 Run；默认、HERDR_STATE_DB、TASKS_FILE、WORKFLOWS_FILE 和优先级组合 | 详情与轨迹同源；只读、无迁移、无新文件；另一库事件不可见 | I2 |
| R9 旧响应覆盖 | 刷新 A→刷新 B→B 先返回；分页 A→刷新 B；切 Task 后 A 返回；刷新失败 | 旧代际丢弃；分页不能混入 B；失败保留旧数据、游标、可重试错误；重复刷新不重复 | I2 |
| R10 页面真实性 | 六种事件、缺时间、恶意标题/路径/详情、错误文案、未知 kind | 不同受控图标、细竖线、中文标题、实际第二行、红绿蓝；未知时间；展开脱敏；无 HTML 执行 | I2 |

## 六类事件集成验收

A 层从临时目录的真实 Codex JSONL 格式输入开始，创建临时只读来源状态库和 Task 身份。读取、成功补丁、失败测试、带原始关联字段的重试测试及通过结果，连同已有格式的真实提交证据进入同一链路。启动隔离 HTTP Console，使用实际 GET /api/task/trajectory，不替换解析器、投影或 API。把 HTTP 结果交由真实 Console JavaScript 渲染，并在浏览器点击详情。

必须断言 file_read、file_edit、test_failed、retry、test_passed、commit 六类的来源引用、时间、相对路径、退出码、关联与 SHA。状态转换时间样例额外断言标题已提交，不能为凑“生成提交”改成 Git 创建时间。缺一条原始证据的负例只出现剩余事实。禁止直接注入最终 UI JSON 替代整条调用链。夹具的 retry_of 只证明兼容处理，不证明某个真实 Agent 原生会话会产出它。

B 层逐个选择可证明归属的 Codex、Claude Code、OpenCode 已有任务，记录 task/run/session 的脱敏引用和原生来源校验信息，再对照只读 API 与真实 DOM。每个 Agent 独立填表：会话可定位、读取、编辑、测试退出码、真实 retry、提交 SHA/时间、缺失字段原因。没有样本或没有操作写“未覆盖”，不得用另一 Agent 的结果填充。原生记录本身不足则保留缺失，不能收费执行补事件。生产服务不重启，使用受授权只读接口或隔离 Console；不能访问真实界面时明确未验证。

## 命令与门禁

实现阶段执行用户指定的三份专项测试、受影响详情回归、Console JavaScript 语法及受控 Promise 交错检查、HTTP 接口测试，再执行 pytest -q、python3 -m compileall -q herdr services bin tests 和 git diff --check。无扩展名 Python CLI 若修改则另做 ast.parse，compileall 不代替它。解释器先确认有 pytest，不访问生产运行状态。

单独在同等隔离环境对最新实现基线及候选执行 tests/test_task_loop_noninteractive.py::test_default_npm_evaluation_finishes_without_watch。基线失败与候选失败都记录，不能通过排除测试声称全量通过，也不修无关模块。用户明确要求本地全量，因此不能用仓库默认“本地专项、CI 全量”省略本任务指定命令。

集成主控负责最新 main 核对及非文档 PR 当前合并结果 CI；Worker 只本地验证，不 push、提 PR、合并、部署或重启。缺主规格或主方案阻止 plan 节点签署，不妨碍保存本次审查。实现专项失败定位到 I1/I2；修改共享文件时撤销并行假设并更新计划。连续三轮未收敛按 RULES 升级，不自行扩围。

## 本次实际验证状态

本工位未修改业务代码，未执行上述产品测试。完成内容为源码定位、方案风险审查、文档落盘和受管发布。没有任何产品 PASS 或三 Agent 能力已实测的声明。最终文件范围以 herdr-task verify-baseline 输出为准。
