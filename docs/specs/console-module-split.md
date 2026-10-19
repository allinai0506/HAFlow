# Console 模块拆分规格

## 目标与边界
将 13150 行入口改为薄启动层，按职责拆分 Console 装配；保持所有现有 HTTP 路由、JSON envelope、状态码、超时、错误映射和页面字节内容。纯业务决策继续复用 herdr 核心，不在本次重新实现。SQLite 仍是既有权威来源，现有 JSON 兼容行为不改。

## 验收
- INV-HTTP：真实 HTTP 请求的成功、失败、鉴权、下载与静态文件防越界契约不变。
- INV-PAGE：HTML_TEMPLATE 的 UTF-8 字节与基线常量完全相同，品牌替换只作用于原占位符；原页面 JS 语法/行为专项保留。
- INV-IMPORT：原脚本可从任意 cwd 启动；普通包导入和 SourceFileLoader 导入仍可调用旧导出。模块采用显式、单向依赖，禁止循环导入、exec、globals 注入或代理模块。
- INV-STATE：HTTP→既有核心/临时 SQLite→HTTP 读回一条真实写读链；跨 workflow/run 拒绝和现有边界测试保持。
- INV-SCOPE：不改生产状态，不调用真实 Agent/收费模型，不部署/重启/推送/提交。
- INV-STRUCTURE：入口只保留启动与兼容导出；Python 装配模块依职责保持约 800 行以内，不机械拆小函数；页面模板作为独立资源。

## 信任边界与残余风险
沿用现有本机 Console 安全模型和核心校验，不扩大信任边界。仅保持行为的职责搬迁；现有缺陷需与新增回归区分。前端完整模板暂不拆 CSS/JS，保留页面行为与加载时序。部署由现有不可变 git archive 快照机制负责，本任务不激活生产版本。
