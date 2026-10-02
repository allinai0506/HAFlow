# 工作流可靠性：人工升级复核材料

状态：六项实现及本地验收完成，人工边界复核已确认；最终全量 3134 passed、2 skipped、157 subtests passed，0 failed。尚未推送、合并、部署或重启。

## 升级依据

`RULES.md` S6：**“若审查发现问题，进入 S6 ➔ S4 ➔ S5 ➔ S6 修复闭环；连续 3 轮无法收敛强制升级人工介入。”**

组合审查的三轮依次覆盖：Standards 实际反例；两种模型的全调用链反例；最终恢复/关闭并发复核。每轮问题均已加入负向回归；第三轮新增的续签与关闭交错说明全局交付仍需人工介入。本材料不以自审或专项绿色替代该要求。

## 最后反例与修复

实际临时 SQLite + CLI：续签已准备 → 原完成回执消费 → 任务完成 → 工作流关闭完成 → 续签继续发送旧 Pane。修复前发送 1 次，错误返回 renewal_dispatched；原始证据保留 `.omc/interrogate-b-renew-close-red.json`。

修复后：投递的发送阶段和关闭/重开使用同一 workflow 生命周期跨进程锁；发送前再次核对 executing 状态、工作流开启、task/run/epoch/凭据文件/Pane 和原生实例所有权。检测到关闭、完成、身份变化或锁被占用时返回 unknown，保留持久意图，不发送旧提示、不盲目重发。

核心代码：`herdr/supervisor_delivery.py#deliver`、`#current_delivery_task`；所有首次派发、返工、RETRY、VERIFY 和续签的生产发送接缝均使用此检查。回归：`tests/test_delivery_lifecycle.py`。作者专项 96 passed；独立复现的最终结果和全量日志将写入正式交付记录。

## 需要人工确认的实际边界

- 原生 API 不支持“原子检查实例身份并发送提示”。当前在 I/O 前立即验证，不能保证外部进程在探测与发送之间绝不替换工位，也不能保证外部 exactly-once。
- 同 UID 工位共享操作系统权限；0600/0700 私有凭据不构成敌对工位沙箱。工具临时 HOME 防止默认数据库误写，不声称强制禁止任意显式路径访问。
- 无 stop/kill 或 Tab 实例所有权证明时保留资源及恢复意图；无运行证明时 doctor 标 unknown；未执行真实模型、生产业务或部署验收。
- 完成声明、测试通过、Git 集成、工作流关闭及生产验收是独立事实。没有生产回执始终 unknown。

人工确认的是以上已实现、已测试的交付边界，不是再次授权编码或允许推送/部署。未获得人工复核前，不标 MERGE_READY 或全局验收完成。

## 人工复核结果

用户已在本会话明确回复：**“认可该边界，继续完成本地验收”。** 该确认解除本次 S6 人工升级复核门禁；仍仅授权 working_tree 本地验收，不授予推送、合并、部署或服务重启。

独立 S5 原始反例复跑：workflow completed、task cleaned、close receipt completed、续签返回 DeliveryUnknown，发送次数 0。新增生命周期 6 passed；直接相关回归 70 passed。原始 RED 和修复 GREEN 均保留于 `.omc/interrogate-b-final.md` 引用的证据文件。
