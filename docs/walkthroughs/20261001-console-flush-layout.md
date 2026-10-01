# Console 外壳铺满窗口

目标：按用户确认去除外围灰底、16px 留白、整体圆角、边框和阴影。内部卡片及业务逻辑保持原行为；底部抽屉随 228px 侧栏贴边定位。

源码位于独立 clone `/Users/user/.herdr-controller/clones/console-flush-1001`，分支 `fix/console-flush-1001`，基于已 fetch 的 origin/main `1d27282`。原 ui-upgrade 的未跟踪收尾文件保留；verify-baseline ui-upgrade 返回 Task not found，不宣称 CoW Task 验收通过。

验证：专项 37 passed；compileall、diff --check 通过；浏览器 1646×831 视口中 shell 为 [0,0,1646,831]，padding/border/radius 为 0，shadow none，无水平溢出。自审记录 `.omc/review-console-flush.md`，非独立评审。

用户单独确认热更新 Console 后，读取实际 LaunchAgent，发现当前程序在 release `38b975d99478235c7aa02d77df0654f9cb0629f4`，已非旧记忆中的 .herdr-console 部署方式。保留 release 原件，将其 Console 和静态资源复制到 `.herdr-console`，仅应用同样四处 CSS 替换，保留 HERDR_ROOT 及原核心运行版本，更新 Console 的 plist 首个脚本参数并重新加载该服务。未更新其他服务。

回滚备份：`/Users/user/.herdr-console-backups/20261001-194605-console-flush`，包含原 plist 和 Console 脚本。第一次 bootstrap 返回 5，第二次成功；HTTP 恢复。当前 HTTP HTML 与部署模块 HTML.encode() 全字节相同，6 个静态资源均全字节一致。实际仪表板数据完成加载，统计显示 40/343、待处理问题 11、卡住 10。布局截图 `/tmp/haflow-flush-live.png`。

代码交付与本机热更新分开：部署采用运行版本上的最小 CSS 补丁，未将最新主干的其他业务变更带入运行环境。PR 创建不代表合并。

全量最终结果：`python3.13 -m pytest -q`：2885 passed，157 subtests passed，577.67s。全部验证绑定本 clone 当前源码。
