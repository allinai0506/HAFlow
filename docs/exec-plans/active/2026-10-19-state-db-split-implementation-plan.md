# state_db.py 拆分实现方案与任务拆解（Plan 节点产出）

> 配套架构文档：`docs/architecture/state-db-module-split.md`（架构基线与全部实测证据；本文不重复设计论证，只引用其编号 §F/§D/§G/§9/§10）。
> 基线：`6417047`。本文只定义**实现阶段**要开什么 Task、改哪些文件、怎么验收；Plan 节点不为子任务开 Pane，也不改业务代码。

## 0.0 承接 provenance 与本版差异（`wf-haflow-1020-01` plan 节点新增）

| 项 | 值 |
|---|---|
| 本文来源 | `wf-haflow-1019-02-plan-executor` 搬运，来源副本 SHA256 `50fd28c82da58ac8cc76f7572c15807d00f2d2e0d2d3b65d152e9afd348717e1`（与 requirements 节点 R-00 登记一致）；搬运后按本工位源码逐条复核，修正标 `〔承接核对〕` |
| 被测文件 | `herdr/state_db.py` SHA256 `3047c80f777d458e75745e15308d4c58c5245ccebc86e1bb5c6bd4052d5244e3`，8069 行；`git diff 6417047..68c61d8 -- herdr/state_db.py` 为空 ⇒ 架构基线与本工位 HEAD 之间**零漂移**，全部行号继续有效 |
| 本版唯一结构性变更 | **实现阶段执行形态由"T2 + 5 路并发 T3-*"改为"1 个 Task、1 个 pane 内 8 个顺序分段 S0..S7"**（§2）。依据是架构 §7.1 与本单 dispatch 的强制口径，不是保守偏好。 |
| 其余变更 | 消费 requirements 节点 R-00..R-28 的成立项（逐项落点见架构 §9.1）、修正 6 处数字口径、补 T1 资产承接段 S0 与 5 处门禁补丁（架构 §6.1 GA-1..GA-5） |

## 0. 前置事实与不变量（继承，不重做需求分析）

实测基线（架构 §4.8：全部数字可由 `scripts/check_state_db_split.py --report` 再生成）〔承接核对：以下每一项都在本工位独立复算，通过项不再标注；带 `→` 的是更正项，见架构 §9.1〕：

- 源文件：`herdr/state_db.py`，8069 行；顶层 **152 个 def/class + 14 个模块级常量绑定 = 166 个顶层绑定**；def/class 体合计 7629 行〔AST `lineno..end_lineno` 口径；段长口径为 7943〕。
- 外部使用面：133 个文件、230 处 import 行（生产/脚本 54 文件 104 行，tests 79 文件 126 行）；其中 **34 行是符号式 `from herdr.state_db import NAME`**（16 → **17** 文件：9 生产 24 行 + 8 测试 10 行）；私有符号 `_decode_task_row` 被 3 处直接 import。〔承接核对：34 行中仅 **8 行是模块级**（真正的名字副本、今天拦不到），其余 **26 行是函数级 import**（在桩窗口内执行 `IMPORT_FROM`，**能被拦截**）；`_decode_task_row` 的 3 处站点全是函数级。〕
- patch 面：**56 → 57 个计数点 / 22 个去重符号**（21 函数 + `CONTROLLER_DIR`），全部锚定 `herdr.state_db` 命名空间；另有 2 处 stdlib `time` 模块属性打桩（不受拆分影响，架构 §2 F-1）。〔承接核对：57 = 38 处 `state_db` 对象形式 + 12 处字符串形式 + 7 处别名 module 形式；22 个符号集合与原表逐名相同。〕
- INV-BIND 改写工作量：**111 处同模块/跨模块裸名调用 + 6 处 `CONTROLLER_DIR` 裸读**（逐点清单由脚本穷举，架构 §2 F-1 表）。〔承接核对：`--inventory` 在本工位输出同一数字；按"调用者所在模块"归属得分段配额 **S1(T2) 19 / S2 22 / S3 33 / S4 20 / S5 16 / S6 7 = 117**，已写入 §3 各段验收。〕

继承的硬不变量（不得破坏）：

- **INV-1** 表结构、索引、PRAGMA 与 DDL **执行序列**不变（全文件口径 25 CREATE TABLE / 43 索引 / 12 ALTER；`_ensure_schema` 文本口径 20/41/0，12 个 ALTER 全在被调助手内 → 门禁范围必须含助手，架构 §2 F-3）。合法切点仅限架构 §4.1.1 白名单：`362-366 / 367-550 / 551-907 / 908-978 / 979-1115`。
- **INV-2** `get_db_connection` → `_ensure_schema` → `_INITIALIZED_DBS`/`_schema_initialization_lock` 的"一次初始化 + 跨进程串行"语义不变；4 处 `_INITIALIZED_DBS.add`（1001/1029/1107/1115）与三条互斥成功路径 + 一条 `else` 路径**一一对应**保留。
- **INV-3** `get_readonly_db_connection` 不执行 DDL、失败抛 `ReadonlySchemaError` 的 fail-closed 语义不变（`_rollout_read_connection@4034`、`_open_shadow_read_connection@1463` 依赖它）。
- **INV-4** 15 个 `conn` 可选参数写 API 的 `should_close`（67 处分支）/ 嵌套 `BEGIN IMMEDIATE` 归属不变（谁开谁提交），判定顺序不改。
- **INV-5** 门面 **166** 个顶层绑定（含私有符号与常量）全部可 `from herdr.state_db import X`；`_INITIALIZED_DBS`/`SHADOW_READ_CAPABILITY` 为同一对象别名；`herdr.state_db.time` 存在。
- **INV-6** patch 拦截能力保持：对 22 个 patch 面符号，`setattr/patch(state_db, NAME, fake)` 必须仍能拦截**真实调用路径**，含符号自身所在叶模块的自调用（架构 §9 BLOCKER-1 派生项）。
- **INV-7** 不改语义、不加依赖、不动 `herdr/state_store.py` 与 `herdr/transitions.py` 契约；不改表结构。
- **INV-8** 语义分层不混装：Trajectory=历史事实、Observation=证据、Finding=分析、ContextPack=工作记忆（AGENTS.md §4.3）；不得为凑 band 合并跨层模块。

## 1. 技术架构与拆解结论（摘要，详情见架构文档）

- 布局：`herdr/state/`（22 个领域模块 + `_facade.py`/`__init__.py`）+ `herdr/state_db.py` 保持**模块**身份作门面（D-1）。
- 分层（**编号权威 = T1 门禁脚本内嵌的 `LAYER` 表**，架构 §3.2）：`constants`/`_facade`=0、`connection`=1、`schema_ddl_helpers`=2、`schema_ddl_txn`/`schema_v1`=3、`schema`=4、16 个数据域=5、门面与注册表豁免；静态 import 只许指向更低层（R-层），同层与上行一律 `_facade()` 晚绑定。**推论**：`connection`(1) → `schema`(4) 是上行边，`get_db_connection@1287` 调 `_ensure_schema` 只能走门面属性（架构 §3.3）。
- 需求 5 个领域组 = 归档视图；物理模块 **22** 个，band 命中 **14** 个、8 条偏离逐条登记（5 低于下沿 + 3 高于上限，架构 §4.7），其中 `context_source_validation.py`(702) 与 `working_context.py`(541) 为上限例外、`constants.py`(139) 为下限例外。
- 拆解的解耦依据：**§4 文件重叠矩阵**（模块集合两两不相交 + 两个共享写文件 `state_db.py`/`state/__init__.py` 用锚点段隔离），它在本承接版里的用途是**段级归因与回退边界**，不再是并发派发依据（§2、架构 §7.1）。

## 2. 任务拓扑与依赖拓扑（承接版：1 个实现 Task，pane 内 8 个顺序分段）

**派发形态已作废并重裁**（架构 §7.1）：原"T2 + 5 路并发 T3-*"不成立——6 个迁移任务全部写 `herdr/state_db.py`，另加共享写文件 `herdr/state/__init__.py` 注册表，违反节点规则"修改文件互不重叠"的并发前提；且本流水线实测在节点内已存在终态 task 时拿不到第二轮 `node_dispatch` 凭据（`herdr/node_dispatch_store.py:113-158`、`herdr/direct_dispatch.py:638` 之后 mode=wait）。因此：

```
实现节点 = 1 个 Task、1 个 pane，内部按序执行 8 个分段：

S0 承接 T1 门禁资产 + 门禁补丁 GA-1/GA-5        （不改 herdr/）
 └─> S1 T2 基座 + 组 A（constants/connection/schema*）+ GA-3/GA-4   （串行，schema 是不变量之根）
      └─> S2 T3-1 组 B：workflow_task_store / transition_task / transition_gate
           └─> S3 T3-2 组 D+F：completion / checkpoint / steering / intervention / collaboration
                └─> S4 T3-3 组 E：events / trajectory / outcome_route / rollout
                     └─> S5 T3-4 组 C-1：observation_finding / context_pack
                          └─> S6 T3-5 组 C-2：working_context / context_source_validation
                               └─> S7 T4 门面定稿、band 结算、--final 全量验证与知识沉淀
```

每段跑完本段验收才进下一段；**任一段不过 → 只回退该段**（判据与定位见 §5），不得带病进入下一段。段序 S2..S6 取 T3-1→T3-5 的原编号顺序，理由是 `transition_task`/`record_event`/`_decode_task_row` 被后续多段引用，先落定可减少门面中间态的 re-export 抖动；该顺序**不是**功能依赖（跨段调用边一律走 `_facade()`，架构 §3.3.1）。

分段（而非并发）的成立性判据不变，且更有用：**模块集合两两不相交 + 每段唯一写者** ⇒ 失败可精确归因到单一域，Selective Replan 只回退一段。架构 §4.0 的 Owner 列与 §7 的归属表继续作为**分段边界**依据，不再作为并发派发依据。

硬约束（决定 S1 必须在 S2..S6 之前）：

1. S2..S6 的每个叶模块都要 `from ._facade import _facade` 与 `from .constants import …`；S1 未落定前契约不存在。
2. `_route_decision_bucket_exprs@4425`（被 `_ensure_schema@704` 用于建表达式索引）与 `_OUTCOME_COLUMNS/_OUTCOME_COLUMN_NAMES@3838/3850`（被 `_resolve_shadow_read_capability@1411` 前向引用）是**跨域符号**，必须 S1 一次性落 `constants.py`，否则 S4 迁 outcome 时 `connection.py` 引用到已消失的名字（架构 §4.1）。
3. 全部 14 个模块级常量归 S1 独占，S2..S6 **禁止**新增/搬动模块级绑定 ⇒ 各段符号集不相交得以成立（架构 §7 第 3 条）。
4. S6 依赖 S0 的 characterization 用例表；表不完备则 S6 blocked 上报，不得自行发明拆法。
5. `connection`(L1) → `schema`(L4) 是**上行边**，必须 `_facade()._ensure_schema(...)`（架构 §3.2/§3.3.1）；把它写成静态下行 import 会在 S1 当场形成循环导入，是全案最容易踩的一脚。

## 3. 分段定义（目标 / 修改文件预估 / 独立验收）

> 段号 S0..S7 是**执行单位**；括号内 T1..T4 是架构文档 §4.0 Owner 列的**归属单位**，用于把失败定位到唯一域。每段的"验收"都是可直接执行的命令 + 门禁编号，不接受口头结论。

### S0（T1）— 承接门禁与 patch 拦截测试脚手架

**S0 是承接，不是重新实现。** T1 五件资产已由上游工位产出并通过实测，但**尚未进 main**（本仓库 `git ls-files scripts/check_state_db_split.py tests/test_state_db_facade_compat.py` 均为空，已复核）：

| 文件 | 行数 | SHA256（前 16） |
|---|---|---|
| `scripts/check_state_db_split.py` | 1703 | `a2e1d0af04e8f692` |
| `tests/test_state_db_facade_compat.py` | 170 | `25053fbe1e3a19e6` |
| `tests/test_state_db_patch_interception.py` | 391 | `cab9998c796a3db4` |
| `tests/test_state_db_characterization_context_source.py` | 515 | `3fc438800eaafad5` |
| `tests/test_state_db_ddl_sequence.py` | 277 | `6404e5821fc9eed0` |

来源 ref `refs/herdr/tasks/wf-haflow-1019-02-impl-t1`，候选 commit `de3d6a682bb6ae18a9da30431ab0b26b29797b6c`，修正 commit **`a1614e5`**（已裁决采用：`git diff --name-only 6417047..a1614e5` 实测只含上述 5 个文件，修的是 G-2(b)/G-2(c) 的假 BLOCK，见架构 §9.1 R-01/R-26）。

**目标**：把五件资产纳入本实现 Task 基线，落地 GA-1/GA-5 两个门禁自身的洞，并建立后续各段的共同判据。**禁止重新实现一套门禁脚本**（dispatch 原文约束；重实现会引入第二事实源，违反 AGENTS.md §4.1）。

**修改文件**：上表 5 个文件（迁入 + 按 GA-1/GA-5 打补丁），全部新增，与 S1..S7 零重叠。

**门禁补丁判据**（架构 §6.1）：
1. **GA-1**：`_resolve_import_edges` 从只遍历 `tree.body` 改为 `ast.walk` 整棵树，函数体内 `from .schema import _ensure_schema` 这类**上行静态边**同样受 R-层约束；新增用例：叶模块函数体内上行静态 import → G-5 必须 BLOCK。修法只改脚本内该函数的遍历方式，不改判定语义。
2. **GA-5**：`test_state_db_patch_interception.py` 从"每符号一条代表路径"升级为**按调用点登记命中矩阵**——22 个 patch 面符号的**全部**真实调用点由 `python3 scripts/check_state_db_split.py --inventory` 枚举行号，矩阵行数 **≥ 117**；并按架构 §2 F-1 的两栏区分：模块级 from-import 副本（8 站点）标"既有失效"，函数级 import（26 行）在桩窗口内执行 `IMPORT_FROM`，标"**应命中**"。
3. **GA-2 流程固化**：S0 起每段验收都跑 `python3 scripts/check_state_db_split.py --final`；段内允许 `G-3 PENDING`/`G-2 WARN`，但**必须把该段 PENDING 集合写进台账**，禁止以 `OVERALL: PASS` 代替 G-3 结论（`blocked = any(status=="BLOCK")`）。

**验收**：
1. `python3 -m pytest -q tests/test_state_db_facade_compat.py tests/test_state_db_patch_interception.py tests/test_state_db_characterization_context_source.py tests/test_state_db_ddl_sequence.py` 全绿，且 `herdr-task verify-baseline` 显示**只新增 tests/ + scripts/**（未改任何 `herdr/` 文件）。
2. `python3 scripts/check_state_db_split.py --snapshot-baseline` 落盘 166 符号基线；`--report` 输出与架构 §4.0 表一致（`covered=8029/8069`，未覆盖仅 `1-40`），逐文件 SHA 与上表一致。
3. GA-1 用例与 GA-5 矩阵（≥117 行）落地，`--final` 输出原文进台账。
4. 若某 patch 面符号在**单体阶段**就无法命中，登记为既有事实并上报，不得跳过或放宽断言（架构 §2 F-1：仅 §2 F-1 列出的 8 个模块级站点允许标为既有失效）。

**不做**：不改 `state_db.py`，不建 `herdr/state/`。

### S1（T2）— 基座 + 组 A（6 模块，L0-L2）

**目标**：完成 `constants/connection/schema*`，把 `state_db.py` 切换为"门面 + 未迁移域"的中间态，并落地 6 个域 re-export 锚点与 `herdr/state/__init__.py` 注册表，使 S2..S6 有稳定契约可用。

**新增文件**：`herdr/state/__init__.py`、`_facade.py`、`constants.py`(114)、`connection.py`(349)、`schema.py`(272)、`schema_ddl_txn.py`(357)、`schema_ddl_helpers.py`(406)、`schema_v1.py`(241)。
**修改文件**：`herdr/state_db.py`（删组 A **43 个顶层符号**：`constants` 15 + `connection` 15 + `_ensure_schema` + `_schema_initialization_lock` + `_ensure_*`/`_migrate_rollout_state_key` 10 + `migrate_v1_to_v2`，其中 `_ensure_schema` 按 5 个行段分片；**建立 6 个域 re-export 锚点块**，见架构 §7）；组 A 相关测试仅在有明确理由时改 patch 目标（候选：`tests/test_state_db_v2.py`、`test_replay.py`、`test_eval.py`、`test_critical_reads_fail_closed.py`、`test_state_projection_namespace.py`），**不得删断言**。

**实现约束**：
- `_facade()` 唯一实现：`def _facade(): from herdr import state_db; return state_db`；函数体内调用，禁止 import 期取门面（R-leaf / R-4）。
- `get_db_connection@1260` 内对 `_ensure_schema@362` 的调用是 **L1 → L4 上行边**，必须写成 `_facade()._ensure_schema(...)`；写成静态 import 即循环导入（架构 §3.2）。
- 编排器 `ensure_schema` 按原顺序调用 3 个内容段（`362-366` / `367-550` / `908-978`）+ 事务段 + V1 尾巴；`BEGIN IMMEDIATE`(551)/`COMMIT`(907) 的相对位置不改；`_ensure_rollout_schema`(977) 必须保持在 907 之后（legacy 库上它经 `_migrate_rollout_state_key@331` 自开事务，架构 §4.1.1）。`1116-1117` 是两个空行，不是切片段，实现期不得为它生成代码。
- V1 尾巴 `979-1115` 整段原子搬迁到 `schema_v1.py` 的单个函数，含 4 处 `_INITIALIZED_DBS.add`（1001/1029/1107/1115）的路径对应；禁止再拆函数。
- `_ensure_rollout_schema`/`_migrate_rollout_state_key` 唯一宿主 = `schema_ddl_helpers.py`（架构 §9 MAJOR-7）；`rollout.py`（S4）下行静态导入。
- **INV-BIND 改写配额 = 19 处**（架构 §2 F-1 分段配额）：组 A 内 `get_db_connection`/`get_default_db_path` 全部裸调用点、`init_db@1297`（体内 2 处）、`_open_shadow_read_connection@1454`（体内 1 处）、`_get_credentials_dir` 的 3 处裸调（位于 `save_user_token@8076`/`get_user_token@8114`/`delete_user_token@8146`，R-05 补项）、以及 **6 处 `CONTROLLER_DIR` 裸读全部**（80、1339、7869-7872×4，均在组 A）。漏改由 G-2(a) 报行号定位。
- GA-3/GA-4 在本段落地（改测试，不改被测代码）：G-4 增第 (v) 条建库路径（先建库→写 rollout 行触发 `_migrate_rollout_state_key@331` 自开事务→再次初始化），并要求"把 977 前移到 COMMIT 之前"必须使该路径失败；路径 (iv) 增导入后**行值**断言（workflow/task/steer/steering_history 各 ≥1 条关键字段），并要求"故意改一个字段映射"必须使该用例失败。
- R-21 补强：并发 spawn 后断言 `_INITIALIZED_DBS` 命中且第二进程**不再执行 DDL 语句**（不能只看 spawn 不报错）；R-3 补强：`_rollout_read_connection@4018 → get_readonly_db_connection@4034` 专项断言"不做 DDL、失败抛 `ReadonlySchemaError`"。

**验收**：
1. `python3 -m compileall -q herdr services tests` 通过，**且**对 9 个无扩展名入口脚本逐个 `python3 -m py_compile bin/<file>`（`compileall -q bin` 是空跑，架构 §9 MINOR-8 / R-11）。
2. `python3 scripts/check_state_db_split.py` 的 G-1/G-2/G-5/G-7 通过，**且** `--final` 输出原文进台账；`python3 -m pytest -q tests/test_state_db_ddl_sequence.py` **五条**路径 trace 与拆分前逐项相等。
3. `python3 -m pytest -q tests/test_state_db_v2.py tests/test_state_store.py tests/test_replay.py tests/test_eval.py tests/test_critical_reads_fail_closed.py tests/test_state_projection_namespace.py` 全绿。
4. 并发开库与 schema 锁专项：`tests/test_state_db_v2.py:127-180`（`_run_concurrent_state_db_open`，`multiprocessing` spawn 真实多进程）通过——单线程重复调用不得替代。
5. 真实链路：临时 db → 写 workflow/task → 重开走 `init_db` → 读回一致（AGENTS.md §6）。
6. INV-BIND 配额：G-2(a) 报告组 A 残留裸名 = 0，且 `--inventory` 的组 B..F 计数从 98 起不变（证明未误改他段配额）。
7. `herdr-task verify-baseline <task-id>` 的 `TASK_CHANGED` 仅含上述文件。

**风险处置**：G-4/G-7 任一非等价即停手回退到 S1 起点，不带病进入 S2（schema 是全部不变量的根）。

### S2（T3-1）— 组 B：workflow / task / transition

- **新增**：`herdr/state/workflow_task_store.py`(387)、`transition_task.py`(450)、`transition_gate.py`(452)
- **修改**：`herdr/state_db.py`（删符号：`save_workflow/get_workflow/list_workflows/delete_workflow/save_task/_decode_task_row/get_task/get_tasks/list_tasks/delete_task/transition_task/compare_and_set_task_transition/compare_and_set_completion_transition/transition_workflow/update_task_metadata/update_workflow_metadata/force_pass_gate`；**按符号名定位，不用绝对行号**）；`tests/test_state_transition_gateway.py`、`tests/test_task_checkpoint.py`（仅 patch 目标）；`herdr/state/__init__.py` 只在本段自己的锚点区间追加模块名（注册表也是共享写文件，架构 §7.1）
- **不变量**：INV-4（`save_task` 的 `should_close` 分支与嵌套 BEGIN）；本域内 `save_task@1611` 裸调 `get_db_connection`、`save_task@1617` 裸调 `save_workflow`、`transition_task@6658` 裸调 `get_db_connection`、`@6807` 裸调 `save_task`、`@6820` 裸调 `record_event`、`@6721/6736/6811` 裸调 `_decode_task_row` 全部改门面属性（INV-BIND）；`PROTECTED_*` 只从 `constants` 静态引用（白名单常量，G-2(c)）。
- **INV-BIND 配额 = 22 处**：`--inventory` 报告本段残留裸名必须归零，且其余段计数不变。
- **验收**：`python3 -m pytest -q tests/test_state_transition_gateway.py tests/test_recovery_entrypoints.py tests/test_supervisor_delivery.py tests/test_workflow_repair_contracts.py tests/test_dispatch_recovery_ui.py` 全绿；`test_state_db_patch_interception.py` 对本域 patch 面符号（`get_task`/`_decode_task_row`/`record_event`/`get_db_connection`）命中；G-1/G-2/G-3 通过并把 `--final` 的 PENDING 集合写进台账（GA-2）；verify-baseline 文件集合与上面一致。

### S3（T3-2）— 组 D+F：completion / checkpoint / steering / intervention / collaboration

- **新增**：`completion.py`(479，上限临界)、`checkpoint.py`(301)、`steering.py`(239▼)、`intervention.py`(312)、`collaboration.py`(292)
- **修改**：`herdr/state_db.py`（删本域 39 个符号：completion 8 + checkpoint 6 + steering 6 + intervention 9 + collaboration 10）；`tests/test_experience_collector.py`、`tests/test_blocker_queue_episode.py`、`tests/test_eval_engine.py`、`tests/test_eval_replay_e2e.py`（仅 patch 目标）；`tests/test_task_checkpoint.py` **由 S2 持有写权**，本段只跑不改
- **不变量**：INV-4；`observe_completion`/`observe_blocker_marker` 的 version 单调与 `_blocker_sample_consumed` 判定不变；`_record_intervention_event@2608 → record_event` 晚绑定；`claim_intervention@2756` 乐观并发语义不变；`compare_and_set_completion_transition@6924 → transition_task`（S2 域）晚绑定——**该跨段边只依赖 S1 契约，不构成 S2↔S3 的执行顺序前提**。
- **INV-BIND 配额 = 33 处**（本域为 6 段中最大改写量，`_decode_*_row`/`get_db_connection` 裸调集中）。
- **band 处置**：`completion.py` 实测 >500 时只允许下沉已确认为纯函数的 `_coerce_version`(8)/`_blocker_sample_consumed`(29)；`steering.py` 不做跨域合并（实测 steering+collaboration=531、steering+intervention=551 均超上限，架构 §4.6）。
- **验收**：`python3 -m pytest -q tests/test_experience_collector.py tests/test_blocker_queue_episode.py tests/test_eval_engine.py tests/test_task_checkpoint.py` 全绿；`state_db.time.time` patch 路径仍生效（`tests/test_blocker_queue_episode.py:53`、`tests/test_execution_trace.py:528`）——**34 处 `time.*` 不在改写范围**（架构 §2 F-1，R-9）；`list_steers` patch 在 `tests/test_eval_engine.py:307` 命中；G-2 无违规。

### S4（T3-3）— 组 E：events / trajectory / outcome+route / rollout

- **新增**：`events.py`(445)、`trajectory.py`(244▼)、`outcome_route.py`(403)、`rollout.py`(346)
- **修改**：`herdr/state_db.py`（删本域 33 个符号：events 4 + trajectory 6 + outcome_route 11 + rollout 12）；`herdr/execution_outcome.py:37`、`herdr/shadow_rows.py:318` 的 import 期常量绑定**只核对不改**；`tests/test_rollout_policy.py`、`tests/test_execution_outcome.py`、`tests/test_console_interaction_api.py`
- **不变量**：INV-3（`_rollout_read_connection@4018 → get_readonly_db_connection@4034` 走只读、不做 DDL）；`transact_rollout_stage`(102 行) 的事务与审计写入顺序；`OUTCOME_PER_AGENT_LIMIT`/`OUTCOME_LOOKUP_CHUNK`/`ROUTE_DECISION_*` 数值与语义；`rollout.py` 对 `_ensure_rollout_schema` 用**下行静态导入**（L3→L2a）。
- **INV-BIND 配额 = 20 处**；另须核对 `OUTCOME_SCHEMA_VERSION@3822` 的 import 期求值只发生一次（S1 已把常量落 `constants.py`）。
- **band 处置**：`trajectory.py` 不并入 `events.py`（合并 690 超限且违反 INV-8），维持登记 ▼ 例外。
- **验收**：`python3 -m pytest -q tests/test_rollout_policy.py tests/test_execution_outcome.py tests/test_observation.py tests/test_trajectory_observer.py` 全绿；`read_rollout_snapshot`/`get_rollout_stage`/`rollout_stage_known`/`query_execution_outcomes`/`artifact_ref_exists`/`record_observation_receipt` patch 命中；verify-baseline 若含 `herdr/execution_outcome.py` 必须给出理由（默认不应改动）。

### S5（T3-4）— 组 C-1：observation / finding / context pack

- **新增**：`observation_finding.py`(433)、`context_pack.py`(314)
- **修改**：`herdr/state_db.py`（删本域 24 个符号：observation_finding 16 + context_pack 8）；`tests/test_context_compact.py`、`tests/test_trajectory_observer.py`（`test_observation.py` 划归 S4 所有，本段只跑不改）
- **不变量**：`find_observation_by_dedup` 的 dedup 键与幂等语义；`_finding/_observation_source_fingerprint` 结果稳定；`read_context_compact_snapshot` 内 4 处跨域调用（`_trajectory_rows_in_conn@5198`、`_decode_finding_row@5237`、`_context_source_version_in_conn@5254`、`_decode_context_pack_row@5276`）晚绑定。
- **INV-BIND 配额 = 16 处**。
- **验收**：`python3 -m pytest -q tests/test_context_compact.py tests/test_trajectory_observer.py` 全绿；`artifact_ref_exists`/`get_trajectory_finding_by_id`/`upsert_trajectory_finding` patch 拦截在 `tests/test_context_compact.py:1274-1291` 路径上仍生效；G-1/G-2/G-3 通过。

### S6（T3-5）— 组 C-2：working context / context source（最高风险，单段独占）

- **新增**：`working_context.py`(516▲)、`context_source_validation.py`(702▲)
- **修改**：`herdr/state_db.py`（删 `_decode_working_context_row/_valid_context_source_ref/_validate_context_source_existence/save_working_context/register_working_context_source/working_context_source_is_current/record_working_context_metric/get_working_context/get_latest_working_context/list_working_contexts`）；`tests/test_context_compiler.py`、`tests/test_console_view_state.py`
- **实现约束（与初稿不同，已由实测否决内部切片）**：`_validate_context_source_existence`(段长 667 / AST 函数体 666) **整块搬迁**，不做 helper 抽取——15 个嵌套 def 存在跨阶段闭包读，且 `relevant_peer_tasks` **不是 def 而是局部变量**（赋值@5915 被 ~5790 的闭包调用读取）：把 5790 之前的语句抽成独立函数会直接 `UnboundLocalError`；另有跨阶段可变状态（`taskless_allowed_runs` 写入@6098）、17 处 raise 集中 6054-6128（架构 §9 MAJOR-5 / D-5、§4.3 承接核对）。`save_working_context`(段长 289 / 函数体 287) 同样不得拆函数。
- **INV-BIND 配额 = 7 处**（本段真实改写量远小于初稿印象，但风险在"切片"而非"改写"）。
- **验收**：S0 的 characterization 用例 **100% 通过且用例数不少于拆分前**（禁止删用例）；`python3 -m pytest -q tests/test_context_compiler.py` 全绿（含 `:1162` 的 `save_working_context` patch 命中）；G-3 对本域两个模块判定为"已登记 ▲ 例外"；`working_context_source_is_current`/`register_working_context_source` 与 heads/clock 的一致性契约有测试覆盖。

### S7（T4）— 门面定稿、band 结算、全量验证与知识沉淀

- **修改**：`herdr/state_db.py`（收口 **166** 绑定 re-export + `__all__`，目标 ≤400 行）、`herdr/state/__init__.py`（注册表收口为按层顺序的 22 个 import）、`docs/architecture/state-db-module-split.md`（把实现期 band 实测值与例外登记写实）、`wiki/`（按 `wiki/WIKI.md` 更新 + `wiki/log.md` 只追加）、`docs/lessons/lessons-learned.md`（如产生通用教训）
- **验收**：
  1. `python3 -m compileall -q herdr services tests` + 对 9 个 `bin/*` **逐个** `python3 -m py_compile bin/<file>`；
  2. `python3 scripts/check_state_db_split.py`（G-1/G-2/G-3/G-5/G-7 全通过；G-3 例外必须逐条命中 §4.7 登记表）；**`--final` 必须 PASS**——本段起 `--final` 把缺失模块/`__all__` 升为 BLOCK，不允许再出现 PENDING（GA-2 的收口点）；
  3. `python3 -m pytest -q tests/test_state_db_facade_compat.py tests/test_state_db_patch_interception.py tests/test_state_db_ddl_sequence.py tests/test_state_db_characterization_context_source.py`；
  4. 真实 CLI/Controller 链路：`bin/herdr-task` / `bin/herdr-factory` → `services/herdr-controller.py` → `herdr/state/*` → 临时 SQLite → 读回；
  5. 全量 pytest 由 CI 在合并结果上执行；CI 不可用时按工程协议 §9.4 补证并写明未验证项；
  6. `herdr-task verify-baseline` 报告与 §4 文件清单一致；`git diff --check` 干净；
  7. INV-BIND 结算：`--inventory` 对 `herdr/state/**` 报告 117 处改写点全部归零（裸名残留 = 0），`time.*` 34 处保持原样。

## 4. 文件重叠矩阵（分段边界与 Replan 定位的事实依据）

> 本矩阵原本用来证明"可并发"，承接版用它证明另一件事：**每段有唯一写者**，因此失败可精确归因到一段。并发派发已作废（§2、架构 §7.1）。

| 共享资源 | 归属 | 冲突处理 |
|---|---|---|
| `herdr/state_db.py` | S1 建中间态与锚点；S2..S6 各删**自己符号集**；S7 收口 | 6 个 `# ---- re-export: domain=<X> ----` 锚点块各占连续行，Sx 只改自己块；符号→行号由脚本提供，不硬编码绝对行号。单 pane 顺序执行 ⇒ 这里不再是并发冲突面，而是**回退边界** |
| `herdr/state/__init__.py`（叶模块注册表） | S1 建立；S2..S6 各自追加本段模块名；S7 收口排序 | **原 §7 未登记的共享写文件**（架构 §7.1 R-14）；顺序执行下由同一段独占写入时机，注册表 import 顺序必须按层（L0→L3）以保证 G-5 图与运行时一致 |
| 模块级常量（14 个绑定）+ 2 个跨域符号 | **仅 S1 写** | S2..S6 禁新增/搬动模块级绑定 ⇒ 各段符号集不相交得以成立 |
| `herdr/state/_facade.py`、`constants.py`、`connection.py`、`schema*.py` | 仅 S1 | S2..S6 只读 |
| 22 个领域模块 | 每模块唯一 Owner（架构 §4.0 Owner 列） | 新增文件两两不相交；Owner 列 = 段归属列 |
| `scripts/check_state_db_split.py` | 仅 S0（迁入 + GA-1/GA-5 补丁） | S1..S7 只调用；实现期发现门禁本身的洞 → 回到 S0 修，禁止在业务段顺手改门禁 |
| `tests/test_state_db_facade_compat.py`、`_patch_interception.py`、`_ddl_sequence.py`、`_characterization_context_source.py` | 仅 S0 写 | S1..S7 只跑；GA-3/GA-4 新增路径虽针对组 A，仍**落在 S0 的文件里、由 S1 段验收执行**，改动记入 S1 台账 |
| `tests/test_task_checkpoint.py` | 仅 S2 写 | S3（checkpoint 域）只跑不改，消除与 S2 的交叉 |
| 域测试文件 | 每域唯一 owner；`tests/test_observation.py` 划归 S4，S3/S5 只跑不改 | 消除唯一已知交叉 |
| `docs/`、`wiki/` | 仅 S7 | 无冲突 |

**跨段的调用边**（`compare_and_set_completion_transition → transition_task`、`read_context_compact_snapshot → 4 个 helper`、`_record_intervention_event → record_event`，全量 41 条见架构 §3.3.1，其中 21 条同层/上行必须晚绑定）一律走 `_facade()`：它们只依赖 S1 已定型的门面契约，**不构成段间执行顺序前提**——段序 S2→S6 只是减少门面中间态抖动，不是功能依赖。

## 5. 派发约定与段级 Selective Replan 支撑

- 实现节点**只开 1 个 Task**（`--agent auto`，由 Agent Router 选择；本节点给出"纯 Python 结构搬迁 + 测试，Pool 可胜任"的理由），pane 内顺序执行 S0..S7。**严禁计划阶段为子任务预开 Pane**；本节点未开任何实现 Pane。
- 每段的验收标准独立（§3 各段"验收"均为可直接执行的命令 + 门禁编号 + INV-BIND 配额），因此失败可精确定位到单一域：
  - band 例外未登记 → 只 rework 该段的模块边界（G-3 未登记偏离即 BLOCK）；
  - patch 拦截失败（G-2a）→ 只 rework 该段漏改的裸调用点，`--inventory` 给行号，配额数字（19/22/33/20/16/7）用来确认漏改规模；
  - DDL trace 不等价（G-4，5 路径）→ 只 rework S1，且必须先回退再重进（不带病进入 S2）；
  - characterization 用例失败 → 只 rework S6，并回到 S0 补用例；
  - 门禁自身判据缺陷（GA-1..GA-5 类）→ 回到 S0 修脚本/测试，再重跑受影响段，**不得在业务段顺手放宽门禁**。
- 前置门禁：S0 未产出 characterization 表 ⇒ S6 blocked；S1 未通过 G-4/G-7 ⇒ S2..S6 全部不启动；每段结束时 `--final` 输出原文与该段 PENDING 集合进台账（GA-2），否则下一段不得开工。
- 回退动作只允许 `git restore`/重跑本段，**禁止**用 `git push`、分支切换或重命名绕开失败段；现场与凭据由流水线控制器收编。

## 6. 风险登记册

| ID | 风险 | 触发条件 | 后果 | 缓解 | Owner |
|---|---|---|---|---|---|
| R-1 | schema 有序切片顺序漂移 | DDL/索引/ALTER 跨片段、977 调用点被提前 | 新库建表失败或旧库静默少索引、嵌套事务报错 | G-4 **5 路径**（GA-3）trace 等价 + G-7 dump 比对 + §4.1.1 白名单切点 | S1 |
| R-2 | `_validate_context_source_existence` 搬迁改变校验顺序/错误文本 | 未看 characterization 表就手拆 | context 校验漏判（第二事实源） | 整块搬迁 + S0 用例表 + 禁止删用例；`relevant_peer_tasks` 是局部变量而非 def，抽段必 `UnboundLocalError` | S0/S6 |
| R-3 | 同模块裸调用搬迁后桩静默失效（假通过） | **117** 处改写（111 裸调 + 6 `CONTROLLER_DIR`）中漏改任一处 | 测试绿但真实缺陷逃逸 | INV-BIND/R-bare + G-2(a) AST 门禁 + 拦截命中矩阵 ≥117（GA-5）+ 分段配额 19/22/33/20/16/7 | S0/S1/S2..S6 |
| R-4 | 门面↔叶模块循环导入 | 叶模块 import 期访问门面符号；或 `connection→schema` 上行边写成静态 import | ImportError / 部分初始化 | `_facade()` 只在函数体内调用；G-5 无环 + 方向合法（GA-1 后含函数级 import） | S1 |
| R-5 | `_INITIALIZED_DBS` 出现第二宿主或被拷贝 | 常量复制到多模块 | 重复 DDL 执行或漏初始化 | 单一宿主 + 别名 + G-2(b) `is` 断言（8 处 `.discard` 站点：7 tests + `state_db.py:1301`） | S1 |
| R-6 | band 与内聚硬冲突 | 强行凑 300-500 | 过度设计 / 语义撕裂 | 已裁决 **Variant A**：8 条偏离逐条登记（14 OK / 5 BELOW / 3 ABOVE，架构 §4.7）；G-3 未登记偏离即 BLOCK | S7 |
| R-7 | 外部私有符号 import 断链 | `_decode_task_row` 未进 `__all__` | `console/read_api.py:111` 等 3 处 ImportError | G-1 以 166 为基线 | S0/S7 |
| R-8 | 只读通道误触发 DDL | 迁移后 `get_readonly_db_connection` 调用顺序变化 | fail-closed 失效、生产库被写 | INV-3 专项断言（架构 §6.1 R-3 补强）+ `tests/test_ops_center_read_path.py`、`test_workflow_repair_contracts.py:322` | S1/S4 |
| R-9 | `state_db.time.time` patch 路径理解错误导致无谓改写 | 把 34 处 `time.*` 纳入 INV-BIND | 噪声改动、掩盖真实风险 | 架构 §2 F-1 明确排除 + S0 专门用例证明 stdlib 属性打桩全局生效 | S0 |
| R-10 | verify-baseline 与段间累积改动不一致 | 单 pane 内多段连续改同一文件 | 改动范围判定失真、把上一段的残留算进本段 | **每段结束时**跑 `verify-baseline` 并把 `TASK_CHANGED` 快照进台账；符号名而非行号删除 | 全段 / S7 核对 |
| R-11 | `compileall -q bin` 空跑给出虚假语法 PASS | 沿用旧验收命令 | 9 个 CLI 入口脚本语法错误逃逸 | 验收命令固定为逐个 `python3 -m py_compile bin/<file>`（§3 S1/S7） | S0/S1/S7 |
| R-12 | 门禁"绿"被误读为拆分结论成立 | 只跑默认模式，看 `OVERALL: PASS` 就进下一段 | 迁移中途 G-3 PENDING / G-2 WARN 被静默吞掉 | GA-2：每段必跑 `--final` 且 PENDING 集合进台账；S7 起 `--final` 必须 PASS | 全段 |
| R-13 | 重实现门禁造成第二事实源 | S0 未纳入 `a1614e5` 的五件资产而另写脚本 | 判据漂移、上游资产作废 | 逐文件 SHA256 fail-closed preflight（§3 S0 表）；dispatch 明令禁止重实现 | S0 |

## 7. 对抗性质询

本节结论已全部落盘到架构文档 §9（10 条裁定：6 成立 / 2 不成立 / 2 部分成立并派生新约束）与 §9.1（承接单 requirements 节点 R-00..R-28 的消费登记表：成立项全部落到本文或架构文档，不成立项标为既有事实），每条附 `文件:行号` 或实验输出证据；派发缺口见 §8 Q-1。本文不重复罗列，避免两处口径漂移。

## 8. 未决问题（承接版：三项已全部裁决，保留原始口径供追溯）

1. **Q-1 challenger 工位派发缺口 — 已消解**。上一单 `wf-haflow-1019-02` 的 requirements 节点已由独立 challenger 工位完成对抗性质询（R-00..R-28），本单直接消费其结论并登记于架构 §9.1，不再重复同进程评审。上一单 plan 节点当时未获得可 `started` 的 challenger dispatch operation，故由 plan executor 以只读评审角色执行并落盘——该替代形态本身未违反红线（未自建 Clone/Pane/分支）。
2. **Q-2 band 硬约束的最终口径 — 已裁决为 Variant A**：`_ensure_schema` 按白名单 5 段有序切片；`_validate_context_source_existence`(667) 与 `save_working_context`(289) 整块搬迁不拆函数；8 条偏离逐条登记例外（架构 §4.7）。验收文字固定为"**300-500 行目标 + 登记例外**"，不得表述为"每模块严格 300-500"。Variant B（8-10 个模块、两个巨型函数原样保留、band 改"依职责 ≤800"，先例见 `docs/specs/console-module-split.md` INV-STRUCTURE）作为已否决的备选保留记录（架构 §10 Q-2），若需求方改判则整体 rework 为 S1/S6 的模块边界，不影响其余段。
3. **Q-3 小模块配对 — 已裁决不强配对**：`constants`(139)/`schema_v1`(266)/`steering`(264)/`trajectory`(269) 维持 ▼ 例外；实测 3 组候选均超 500 或跨语义层（架构 §4.6），且配对会违反 INV-8 语义分层。若后续给出新配对约束，只局部 rework S1/S3/S4。
4. **仍待需求方裁决（不阻塞，按默认口径继续）**：300-500 硬 band 与两巨型函数不可同时满足这一冲突，本单默认 Variant A；平台缺陷 D-1（关单凭据与 `workflows.status` 漂移）/D-2（节点内已有终态 task 时拿不到第二轮派发凭据）**不在本单修**，本单只把其后果固化为 §2 的单 Task 串行形态。

## 9. 本节点交付边界

- 本文与 `docs/architecture/state-db-module-split.md` 为 Plan 节点两份核心产出，均已**搬运进本仓库**（不再只引用外部路径）并逐条对源码复核：基线 `6417047..68c61d8` 之间 `git diff -- herdr/state_db.py` 为空，`herdr/state_db.py` 仍 8069 行（SHA256 `3047c80f…`），故架构全部行号证据在本工位仍然有效。
- 本版相对来源的变更：§0.0 记录的 6 处数字修正、GA-1..GA-5 消费、架构 §3.3.1/§6.1/§7.1/§9.1/§11 新增，以及**唯一结构性变更**——实现阶段执行形态改为 1 个 Task 内 S0..S7 顺序分段。
- **未修改任何业务代码**（`herdr/`、`services/`、`bin/` 零改动）；实现阶段的文件归属见 §3/§4。
- **T1 五件门禁资产不在 main**，本节点未擅自迁入（保持交付边界干净）；实现段 S0 必须以 §3 的逐文件 SHA 承接，禁止重实现。
- 改动范围以 `herdr-task verify-baseline` 为准；不执行远程 push / PR / 分支切换或重命名，交付由流水线控制器统一收编。
- **delivery_contract 缺口如实披露**：本节点的 plan 交付为两份文档 + 台账决策，不含代码 PR；上一单 implementation 节点只完成 T1 即死锁（现场已 pause 保留），因此"实现已交付"不成立，不应被本文档的存在误读为拆分已完成。



## 实施工位已裁决基线（2026-10-20）

需求方授权保留 a6b315a 的全部已有行为，当前实施契约为 **8080 行 @ a6b315aee410638574f422de660de78c0d32a314**，源码 SHA256 `a09da46e0130c37d0bb3be8b418464d6946c63a9d1a63292038e48a5a5df931d`。原 8069 行 / `3047c80f…` 零漂移判据作废；这是授权重绑定，不是待修缺陷。原文保留来源历史，本文以下补充优先。

- ≤6667 的白名单锚点不动；后续位置以脚本 symbol→line 输出为准。`compare_and_set_completion_transition` 新基线 def=6924，原 @7074 是来源文档精度错误，不能机械加11。
- §4.0 `transition_task.py` body=461、est=486（原450/475加11），仍在300–500目标内；八条band例外集合与理由不变。覆盖率=8040/8080，未覆盖1–40。最终以实际模块行数结算。
- S4=events/trajectory/outcome_route/rollout（20）；S5=observation_finding/context_pack（16）；S6=working_context/context_source_validation（7）。S6两巨型函数整块搬迁，characterization不完备即blocked。
- GA-5仍 NOT certified：117引用表达式矩阵与8模块级副本/26函数级import登记，不等于117真实路径全部命中。重绑定不放宽验收。
- S2前增量复核新intent参数、delivery-repair推导与校验、CAS及事件intent持久化，重跑四文件和transitions影响面。
- D-3保留接口原始拒绝回执：`[CHECKPOINT REJECTED] step exceeds checkpoint budget`。需求方规定后续各段不再调用checkpoint-publish，以`note-add --kind evidence`发布有界checkpoint。此前step1..3成功回执仅作历史，不替代后续台账。


### S7 实测分配与 INV-BIND 结算

验收口径为“300–500 行目标 + 登记例外”。轻量导入使实际行数低于原估算，实际为15个在区间内、5个已登记下沿例外、2个已登记上沿例外。原八条例外登记保持；completion现为区间内，ARCH-4.7-completion本次未使用，不新增例外。

| 模块 | 实际行数 | band |
|---|---:|---|
| `checkpoint.py` | 304 | OK |
| `collaboration.py` | 302 | OK |
| `completion.py` | 481 | OK |
| `connection.py` | 348 | OK |
| `constants.py` | 114 | BELOW（已登记） |
| `context_pack.py` | 315 | OK |
| `context_source_validation.py` | 683 | ABOVE（已登记） |
| `events.py` | 450 | OK |
| `intervention.py` | 313 | OK |
| `observation_finding.py` | 427 | OK |
| `outcome_route.py` | 407 | OK |
| `rollout.py` | 345 | OK |
| `schema.py` | 284 | BELOW（已登记） |
| `schema_ddl_helpers.py` | 400 | OK |
| `schema_ddl_txn.py` | 366 | OK |
| `schema_v1.py` | 252 | BELOW（已登记） |
| `steering.py` | 242 | BELOW（已登记） |
| `trajectory.py` | 246 | BELOW（已登记） |
| `transition_gate.py` | 459 | OK |
| `transition_task.py` | 470 | OK |
| `workflow_task_store.py` | 387 | OK |
| `working_context.py` | 519 | ABOVE（已登记） |

S1→S6 的117条patch面引用核销为19/22/33/20/16/7，逐段门面剩余98/76/43/23/7/0。S6的7条是7个`get_db_connection`调用点（同一个patch符号），另有6个跨模块校验调用改成晚绑定，后6条不计入117。原§3.3.1将S6说成“1个connection调用+6个校验调用=7”属来源计数混淆，以上述逐调用点矩阵与实际脚本为准；S6 checkpoint的括注同样由本条更正。

门面保留166个顶层绑定、`time`及原导入对象别名；共享mutable对象不复制。151个非schema顶层def/class与授权基线做规范化AST逐函数对比；schema按五条运行路径验证trace与行值；34个time调用表达式逐项一致。完整真实调用命中证据另绑最终候选文件哈希，不以引用表达式探针代替业务路径。


### 最终业务实现文件的真实路径回执

透明门面spy在真实业务frame中记录调用，最终实现文件候选的专项运行结果为1453 passed / 12 subtests passed，117/117内部patch调用点均命中。`tests/fixtures/state_db_real_path_receipt.json`绑定门面、注册表、helper和所有领域模块的SHA256，并逐点登记当前owner/行号/原始行号/命中测试；`test_real_path_receipt_covers_current_implementation`拒绝缺失或与当前文件不符的回执。8个模块级副本保留既有不拦截事实，26个函数级import窗口仍须命中。此前NOT certified中间态记录由此补证，不能反向改写历史结论。

结构验收`--final`通过；兼容面、五路径DDL、完整characterization、AST等价与真实CLI/SQLite读回均有专项。通用inner-loop评分与业务验收分开记录，最终运行回执见工作流共享台账。全量Ruff非零，不宣称lint全绿；既有`Iterable`字符串注解未在拆分中修正。自审不是独立评审；本单不包含Push、PR、合并或部署，也无机器delivery_contract覆盖仓库门禁。


### 2026-10-20 承接基线与证据更新

FACT：本次承接基线为 `a56739e`，单体 8169 行、167 绑定（153 def/class + 14 Assign/AnnAssign 名称）、35 个 `time.*` 调用点；GA-5 本轮矩阵 119 = 113 bare + 6 CONTROLLER_DIR。授权写集为 45 = 24 state + 1 门面 + 1 门禁 + 11 tests + 3 docs + 5 wiki。历史验证数字仅描述旧候选，不能证明本次验收。

`register_workflow` 的唯一宿主为 `herdr/state/transition_gate.py`，门面显式 import 与 `__all__` 再导出；删除内联相对导入后复用顶层绝对导入，事务与 fallback 契约保持上游行为。300-500 行目标 + 登记例外：第 9 条 `above=ARCH-4.7-transition-gate-port`，实际 546 行。G-5 静态图为 25 模块 / 74 边，与历史动态调用边口径分开。

Evidence：`scripts/check_state_db_split.py --report --symbols` 为 symbol→line 权威；`--snapshot-baseline` 为 live 167 绑定且拒绝缺失绑定与未知 owner。`tests/test_state_db_split_source_equivalence.py` 验证搬迁 AST 与 35 个 time 表达式；`tests/test_state_db_real_patch_paths.py` 验证注册事务边界；`tests/test_state_db_ddl_sequence.py` 五路径运行 trace 与 schema 93 对象等价分证。共享台账 wf-haflow-1020-02 / implementation 保存本次命令原文，独立洁净室验收待 test 节点执行。

当前基线的迁移锚点（由 `--snapshot-baseline` 的 symbol→line 生成；正文历史切片不再用于执行）：

| symbol | line | end | next | owner |
|---|---:|---:|---:|---|
| `compare_and_set_task_transition` | 6889 | 6921 | 6924 | transition_task |
| `compare_and_set_completion_transition` | 6924 | 7116 | 7119 | transition_task |
| `transition_workflow` | 7119 | 7238 | 7241 | transition_gate |
| `register_workflow` | 7241 | 7327 | 7330 | transition_gate |
| `update_task_metadata` | 7330 | 7401 | 7404 | transition_gate |
| `update_workflow_metadata` | 7404 | 7465 | 7468 | transition_gate |
| `force_pass_gate` | 7468 | 7657 | 7660 | transition_gate |
| `create_checkpoint` | 7660 | 7771 | 7774 | checkpoint |
| `list_checkpoints` | 7774 | 7800 | 7803 | checkpoint |
| `get_checkpoint` | 7803 | 7816 | 7819 | checkpoint |
| `restore_checkpoint` | 7819 | 7865 | 7868 | checkpoint |
| `fork_workflow_from_checkpoint` | 7868 | 7939 | 7942 | checkpoint |
| `get_checkpoint_lineage` | 7942 | 7958 | 7961 | checkpoint |
| `migrate_v1_to_v2` | 7961 | 8062 | 8065 | schema_v1 |
| `_get_credentials_dir` | 8065 | 8073 | 8076 | connection |
| `save_user_token` | 8076 | 8111 | 8114 | connection |
| `get_user_token` | 8114 | 8143 | 8146 | connection |
| `delete_user_token` | 8146 | 8169 | 8170 | connection |
