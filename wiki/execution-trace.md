# 任务执行轨迹

任务详情抽屉的「执行轨迹」是只读投影。它不调度 Agent，不推进工作流，也不写入第二套历史。

## 数据从哪来

1. 任务文档里的创建时间、状态历史、开始时间和提交 SHA。
2. 状态库里的任务行和 `events` 表。任务身份与该 `task_id` 且 `run_id` 匹配的事件都通过 `get_readonly_db_connection` 读取，不打开可写连接，也不做表结构迁移。
3. 能证明属于该任务的 Codex、Claude Code 或 OpenCode 原生会话。

投影代码在 `herdr/execution_trace.py`。原生解析在 `herdr/native_session_trace.py`。控制台入口是 `GET /api/task/trajectory`。

## 归属

`agent_session_id` 是 HAFlow 启动适配器记下的会话标识，不是 Codex、Claude Code 或 OpenCode 的原生会话 ID。不能拿它去对原生记录。

Pane ID 会回收，也不能当作历史归属。

没有记录 `native_session_id` 时，只接受工作目录与该任务独占 `clone_path` 完全一致的原生会话。隔离目录被多个任务共用时，拒绝合并原生记录。已经写在任务或 `.herdr-launch-identity.json` 里的 `native_session_id` 必须与原生会话 ID 一致。

对不上的原生记录直接丢弃，页面仍显示已经归属的任务事件。

## 不推断的事

执行过测试命令不是测试通过。任务状态变成 completed 不是全部工具成功。两条相似命令不是重试。最终工作区里的文件不是某一时刻的读取或编辑。没有原始时间就保持为空，不用当前时间填。

## 页面

打开工作流，点开任务，再点「执行轨迹」。这一页才请求轨迹。时间正序。失败事件用红色，通过用绿色，编辑用蓝色，读取用空心灰点。点一条事件展开脱敏后的工具、相对路径、命令名、退出码、来源和证据标识。
