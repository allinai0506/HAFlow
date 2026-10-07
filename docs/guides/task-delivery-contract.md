# 配置 Task 工程交付要求

当仓库要求测试、复盘或专用验证时，将这些要求写入 Workflow 节点的 delivery_contract。配置是明确的授权输入；HAFlow不会从shell或任意提示词自动推断文件权限。

## 在派发前对齐范围

读取源仓库AGENTS.md、适用局部规则、实际commit和CI门禁、复盘模板。逐项登记允许文件和必需产物。若需求写明只改三份Java文件而门禁要求复盘，先请协调者明确补充文档范围，再登记契约。不能换分支名、关闭hook或默认扩大范围。

契约示例（使用项目实际路径与命令替换本示例）：

```json
{
  "id": "implementation",
  "default_integration_mode": "git",
  "delivery_contract": {
    "version": 1,
    "allowed_paths": ["src/Feature.java", "tests/*", "docs/bug-reports/*"],
    "required_files": [{
      "path": "docs/bug-reports/2026-10-07-feature.md",
      "headings": ["## 根因", "## 防复发", "## 验证与回归"]
    }],
    "checks": [{"id": "regression", "argv": ["python3", "-m", "pytest", "-q", "tests/test_feature.py"], "timeout": 60}],
    "auto_rework": true
  }
}
```

配置存入工作流节点，launch固定到Task。新配置不追溯改写已派发Task的授权。当前版本的机器交付契约面向nodes格式；legacy stage_policies继续兼容旧任务语义，迁移时使用规范nodes。只读测试/评审、reviewer/adversarial角色和context任务不获得工程写入或执行此检查的权限。

## 字段及边界

- version必须为1；未知字段拒绝。
- allowed_paths为1–100个仓库相对路径或fnmatch模式，是授权范围，不是缺失时的自动猜测。required_files必须落在此范围，normalize/launch在创建工位前拒绝冲突。
- required_files最多50项，path必须是确切文件；必须属于相对Task基线的新增或修改产物。已有但本轮未交付的历史文档不算满足。headings最多20个精确二级标题，标题下必须有正文；这只检查结构，不证明正文结论正确。
- checks最多8项，每项含唯一id、argv列表和timeout。复用有界执行，单项上限300秒，总超时上限120秒，输出上限64KiB。登记安全验证命令，不将有副作用的完整pre-commit当作只读探针。命令不会继承控制器状态路径或常用模型密钥，HOME为临时目录；需要真实外部服务的检查应单独配置授权环境，不凭本检查宣称已覆盖。
- auto_rework默认false。true仅允许原Run原工位在原文件范围内补齐交付，最多三轮；不能扩大业务范围、跳过hook或把缺候选Task发给test。

## 执行并读取检查

在工位完成必要产物后运行当前HAFlow版本的绝对CLI路径：

```bash
/absolute/haflow/bin/herdr-task delivery-check <task-id>
```

stdout输出JSON。退出0表示ready，退出3表示blocked或unknown，退出2表示参数、身份、环境或之前未知执行未解决。回执存入既有SQLite events，event_type为delivery_checked，source为delivery-check，绑定Task/Run/execution/epoch及仓库指纹。检查命令先登记delivery_check_started；中断、超时、输出预算停止或检查改动仓库时结果为unknown，同epoch禁止默默重跑，需要协调者核验副作用并明确安排新执行。

指纹覆盖HEAD、分支、相对基线的文件内容、暂存blob、固定契约、根规则、Git配置及实际hook文件。拒绝超预算及不安全路径。源码或配置变化后重新检查，旧ready不能用于完成。SHA是检查的内容归属证据，不是源码不可变的永久保证；实际commit和下游冻结验收继续负责交付边界。

report-completion和Controller消费声明都检查最新ready，新的检查意图使旧通过失效。commit在暂存后再次检查，包括直接提交的收编路径；实际Git hook仍照常执行。缺契约的旧任务兼容原完成路径，提示词明确其仓库要求尚未被机器契约覆盖。

## 失败与恢复

明确的检查拒绝使用HERDR_COMMIT_RESULT、退出7和delivery_blocked，Controller在事务内校验真实检查回执及Task/Run/version再建立delivery恢复责任。权限或范围冲突、身份不足、预算耗尽进入waiting_human。普通Git rc=1没有可靠分类依据时继续原受限重试，保留脱敏后的有界输出尾部；不会把所有rc=1都当作缺文档。

恢复责任复用既有claim/lease、request_id、receipt-v1。未提交交付恢复不要求candidate_sha；业务测试仍要求冻结候选。completed到rework只在delivery-repair的事务准入条件满足时成立，普通set/rework不能打开终态。原HEAD已有未登记提交、工位归属未知、旧Run或发送未知时拒绝自动返工。

发送结果未知不重发；若已有同Run/epoch/request的rework_dispatched，只补齐delivered状态。补齐产物后重新检查、提交和集成，真实integrated结果才关闭恢复责任。生产升级与既有工作流恢复需另外授权。

## 验证入口

```bash
pytest -q tests/test_task_delivery.py tests/test_delivery_rework.py
```

测试包含临时Git/SQLite的实际commit hook、直接提交收编拒绝、同工位恢复及真实本地集成。传输替身只替换外部Agent，不替换交付检查、持久责任或Git结果。
