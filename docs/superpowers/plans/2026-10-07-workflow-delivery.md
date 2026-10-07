# 工作流交付实施计划

Goal: 以固定到Task的交付契约、真实检查回执和受限返工完成D1–D10。
Architecture: 节点delivery_contract固定到Task；检查绑定源码、暂存区、分支、契约和运行身份；复用SQLite事件、恢复operation及receipt-v1，不新增调度器。
Tech Stack: Python标准库、Git、SQLite、pytest。
Route: match / bug / medium / state_machine, concurrency, permission, data_integrity / unified / controller=none / RED-first / adversarial / working_tree。用户已批准设计并授权直接实施，无提交或推送。

## Task 1: 交付检查与完成边界
- [x] 新建tests/test_task_delivery.py，验证缺文档、范围冲突、章节缺失、实际命令失败、内容变化失效、只读权限与真实CLI回执。
- [x] 运行pytest -q tests/test_task_delivery.py记录RED。
- [x] 实现herdr/task_delivery.py：validate_contract(contract)、instruction_block(task)、check_delivery(task_id,store)、require_delivery(task,store)。schema version=1，allowed_paths、required_files(path/headings)、checks(id/argv/timeout)、auto_rework。
- [x] bin/herdr-task新增delivery-check，launch从节点固定契约，supervisor_delivery统一追加说明；completion_receipt拒绝未满足的配置契约。无契约任务兼容旧完成流程。
- [x] 重跑专项并检查实际CLI → SQLite回执 → 完成拒绝/成功。

## Task 2: 失败回执及有限返工
- [x] tests/test_delivery_rework.py先复现普通rc=1误重试、末尾错误丢失、completed不能返工、无候选恢复阻塞与发送未知。
- [x] 实现失败分类和有界脱敏尾部；可靠检查拒绝不盲重试，原hook仍生效。未知rc=1保留既有重试。
- [x] herdr/delivery_rework.py复用事务和receipt派发；准入绑定未提交Task/Run/执行代次、范围、3轮预算、原工位所有权、提交活动及稳定HEAD。
- [x] workflow_progress/recovery_store/workflow_recovery/Controller支持delivery kind的原Task恢复，不要求candidate_sha，不改变测试节点冻结门禁；结果仅真实集成后resolve。
- [x] 重跑专项，独立连接交错验证旧Run/版本变化/重复请求/传输未知。

## Task 3: 清理、验证与文档评审
- [x] ai-slop-cleaner Mode B删除式清理，不加未来框架。
- [x] 专项和pytest -q全量、compileall、CLI AST、git diff --check；源码变化重跑受影响验证。
- [x] 更新docs/guides/task-delivery-contract.md、Wiki及教训，不写生产恢复成功。
- [x] google-code-review及code-review只读审查当前差异和D1–D10，最多三轮；写.omc/verify与review证据。
- [x] working_tree交付，列实际通过数、未验证项和风险。

## 测试示例
```python
receipt = check_delivery('t', store=store)
assert receipt['status'] == 'blocked'
assert receipt['issues'] == [{'code': 'required_file_missing', 'path': 'docs/postmortem.md'}]
```

## 基线证据
原NexusArchive门禁在临时Git仓库退出1，缺少复盘；源项目实际提示词仅允许三Java文件。health/codehealth工具不可用，记录测量缺口并使用AST结构快照与差异审查，不制造PASS。

## 最终执行证据
专项252 passed；全量3855 passed、157 subtests passed，exit0。compileall、无扩展名CLI AST、diff检查通过，最终源码哈希未变。两角色第三轮及不同模型最终评审无已确认阻塞；不同模型独立专项47 passed。health/codehealth工具缺口沿用显式测量豁免。证据见.omc/verify-01a1159f-693c-7453-820c-9d6e2e1719ff.md及对应review记录。交付限working_tree，未执行生产复跑。
