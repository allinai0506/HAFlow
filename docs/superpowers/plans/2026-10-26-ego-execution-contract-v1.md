# ego-browser 执行义务 V1 实施计划

Goal：基于冻结契约和可信事实作只观察核验，不能证明就明确记录。
Architecture：复用 workflow/task SQLite payload、TrajectoryLedger、ObservationStore。纯核心不读文件或更新状态；原生适配器不把 shell 成功等同浏览器 E2E。
Tech Stack：Python 标准库与现有 SQLite 存储，无新依赖。

## 边界与选型
用户任务规格为验收基线。保留 Controller、code-craft-v1 及现有门禁。不新增 Agent、阶段、状态、自动恢复或 UI。原生事件优先；仅命令文本、技能读取与报告均不能 satisfied。托管浏览器执行器需要额外运行时加载证明能力，超出本 PR，不用弱证明替代。V1 暴露缺口而非假装完整覆盖。

信任 HAFlow 与原生会话记录；防疏漏、误归属、跨 Run/候选复用；不防同 UID 恶意篡改宿主。当前适配器不具备完整 E2E 可信事实来源，生产入口不可生成 satisfied。纯函数的 satisfied 用受控事实夹具验证，仅是契约测试。

## 步骤
- [x] 新增验收反例并运行 RED：四类输出、身份隔离、调用失败、技能读取、自述、旧模板兼容、核验不可用、门禁不重派。
- [x] workflow 创建时冻结各节点 required_executions；Task 首次创建时由冻结声明绑定 workflow/task/run/candidate，不允许后续 upsert 替换；历史无契约保持无契约。
- [x] 实现纯核验函数及 ego-browser 原生 shell 调用适配。仅精确单命令原生成功/失败记录可作调用层事实；包装脚本/命令链不推断。结果内容不从 Agent 报告采纳。
- [x] 自动裁决入口复用 ObservationScheduler 的有界后台旁路采集，在 ObservationStore 保存脱敏事实，在 TrajectoryLedger 引用；所有错误旁路，不改 verdict 或状态。
- [x] 专项回归、真实工具探针、清理与审查，记录真实集成能力缺口与未来强制接入边界。

## 验收
pytest -q tests/test_execution_obligations.py tests/test_trajectory.py tests/test_observation.py tests/test_completion_receipt.py tests/test_execution_context_contract.py
其他受影响调用方按搜索定位补充，不本地重复全量。未来 enforce 必须进入事务内 transition_task，覆盖手工 set、completion receipt、sentinel CAS、auto-verdict；当前不得启用。

审查修复：直接原生 shell 回执采用来源标记，Codex exec 包装器推断不作实际调用；原生 stdout 不作退出码证明；引号内 JS 与外层 shell 运算符分开处理；不扫描 Ledger 历史进行去重。透明探针对受影响的 state_db 119 个调用位点重新取证。
