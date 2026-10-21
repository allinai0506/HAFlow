# state_db.py 拆分：技术架构与解耦任务拆解

> 状态：Plan 节点产出（架构基线）。承接 `wf-haflow-1019-02` requirements 已确认需求，不重做需求分析。
> 基线 commit：`6417047`。目标文件：`herdr/state_db.py`（8069 行 / 152 个顶层 def-class，已 AST 实测）。

## 0. 承接 provenance 与核对口径（wf-haflow-1020-01 plan 节点新增）

本文自 `wf-haflow-1019-02-plan-executor` 搬运，搬运后按本工位源码逐条复核并就地修正；修正一律标注 `〔承接核对〕`，保留原编号（§F/§4.x/§G/§9/§10）以免跨文档引用漂移。

| 项 | 值 | 复核方式 |
|---|---|---|
| 来源文档 SHA256 | `9418ae8c448253def7ebcefaf3adb33976ccaab6c667b528001ded9bcc5a43d4` | 与 requirements 节点 R-00 登记的来源副本一致（字节相同） |
| 文档架构基线 | `6417047` | `git diff 6417047..68c61d8 -- herdr/state_db.py` 为**空**：被测文件在本工位 HEAD 与架构基线之间零漂移 |
| 本工位 HEAD | `68c61d8`（含 #278/#279 本体学合并，未触 `herdr/state_db.py`） | `git rev-parse HEAD` |
| 目标文件 SHA256 | `3047c80f777d458e75745e15308d4c58c5245ccebc86e1bb5c6bd4052d5244e3` | `shasum -a 256 herdr/state_db.py`；与 T1 候选工位 `wf-haflow-1019-02-impl-t1` 内同名文件相同 ⇒ T1 门禁的 166 符号内嵌基线对本工位直接有效 |
| 权威复算工具 | `scripts/check_state_db_split.py`（T1 候选 `a1614e5`，SHA256 `a2e1d0af04e8f692c47e4b0ace61aa4f2656135ff119ac19576046882f3159aa`） | `python3 <script> --root <本工位> --report --symbols` / `--inventory`，本文 §4.0/§2 表以该输出为准 |

数字口径约定（避免"看似矛盾"）〔承接核对〕：

- **段长 vs AST 函数体**：段长 = `本符号 lineno .. 下一个顶层语句 lineno - 1`（含尾部空行/注释，用于行数归属与 band 判定）；AST 函数体 = `lineno .. end_lineno`。`_ensure_schema` 段长 756（362-1117）/ 函数体 754；`_validate_context_source_existence` 段长 667（5463-6129）/ 函数体 666；`save_working_context` 段长 289（6130-6418）/ 函数体 287。两组数字都对，R-13 的裁定是"不得把段长称作函数体"。
- **装饰器行 vs def 行**：`_schema_initialization_lock` 的 `@contextmanager` 在 87、`def` 在 88；`ExactDecisionBucket` 的 `@dataclass(frozen=True)` 在 4403、`class` 在 4404。本文其余 `符号@行号` 全部与 AST `lineno`（def/class 行）逐条核对通过，仅此两处按装饰器行书写。
- **索引计数两个口径**：源码文本 `CREATE (UNIQUE )?INDEX` = **43**；初始化后 `sqlite_master` 的 index 对象 = **44**（G-7 输出口径，含 UNIQUE 约束生成的隐式索引）。两者不是同一测量，G-7 的 44 不构成对 F-3 的 43 的反驳。

## 1. 需求基线承接与算术裁决

已确认需求：把 `herdr/state_db.py` 按 schema/connection、workflow+task、observation/finding/context、checkpoint/migration、rollout/outcome/eval 拆为高内聚模块，每模块 300-500 行，`state_db.py` 保留兼容 re-export，不改语义与表结构，验收 compileall + pytest + verify-baseline。

架构裁决（需求阶段证据 `obs_fbe08bdd5fa04813999512f11e29d7dd` 的收敛）：

- 5 个"领域组"是**语义分组**，不等于 5 个物理模块。152 个顶层 def/class + 14 个模块级常量绑定 = **166 个顶层绑定**；def/class 体合计 7629 行〔承接核对：7629 = AST `lineno..end_lineno` 合计，已复算通过；按"段长"口径为 7943，两者不得混写〕，加各模块 import/docstring 开销约 25 行/模块。
- 500 行/模块 ⇒ 至少 17 个物理模块；300 行/模块 ⇒ 至多 26 个。按 §4.8 脚本对真实代码段做穷举后取 **22 个物理模块**（含 8 处 band 例外，逐条登记理由），归入 6 个领域组（A-F，其中 F 是需求 5 组未命名的残差域）。
- 两处不可调和的单点：`_ensure_schema` 函数体 756 行〔承接核对：756 是段长（362-1117），AST 函数体 754；见 §0 口径〕（含 `BEGIN IMMEDIATE`→`COMMIT` 单事务与 V1 引导分支，只能按 §4.1.1 白名单切点做**有序切片 + 编排**）、`_validate_context_source_existence` 函数体 667 行〔承接核对：段长 667 / 函数体 666〕（实测**不可**安全切片，只能整块搬迁并登记 band 例外，见 §4.3）。这两点是"不改语义"约束下的唯一可行路径，也是本方案最高风险项（实现计划 §6 R-1/R-2）。

## 2. 决定架构的三条实测事实

拆分方案的形状不是风格选择，由以下仓库事实强制决定：

### F-1 全部 patch 锚定在 `herdr.state_db` 命名空间本身（→ 晚绑定契约）

tests/ 中 41+ 处 monkeypatch/patch 目标全部写成 `setattr(state_db, "X", …)`、`patch.object(state_db, "X")` 或 `patch("herdr.state_db.X")`，例如：

- `tests/test_context_compact.py:419` patch `get_db_connection`
- `tests/test_state_transition_gateway.py:330,340,727`、`tests/test_task_checkpoint.py:58` 等 7 个文件 patch `record_event`
- `tests/test_state_projection_namespace.py:26,125` patch 模块常量 `CONTROLLER_DIR`
- `tests/test_deliverable_read_latency.py:60,89`、`tests/test_ops_center_read_path.py:42`、`tests/test_workflow_repair_contracts.py:322` patch `get_readonly_db_connection`
- `tests/test_rollout_policy.py:1811-1815` patch `get_rollout_stage` / `read_rollout_snapshot`
- `tests/test_execution_outcome.py:624` patch `query_execution_outcomes`
- `tests/test_execution_trace.py:528` patch `herdr.state_db.time.time`

Python 语义（已在本地用 `unittest.mock.patch.object(module, name)` 实验证实）：patch 打在 **module 对象的属性**上，因此
- 经 `state_db.X(...)` 属性访问的调用（无论调用方在哪个模块，**包括 X 自己所在模块的自调用**）→ 被拦截；
- 经 `from .y import X` 得到的 global 名字副本 → **不被拦截**（桩失效，断言仍可能为真）。

后果：只把"跨模块调用"改为晚绑定是**不充分的**。`ast.walk` 实测的同模块裸调用即有三类典型：`connection.py` 迁移后同时容纳 `init_db`（1297，体内裸调 `get_default_db_path@1299` + `get_db_connection@1302`）与 `_open_shadow_read_connection`（1454，体内裸调 `get_readonly_db_connection@1463`；同处还裸调 `assert_shadow_read_capability@1465`，该符号不在 patch 面故不受门禁约束）；`workflow_task_store.py` 内 `save_task` 裸调 `get_db_connection@1611` 与 `save_workflow@1617`（后者被 3 个外部模块 from-import，见 F-2）——这些**同模块裸调用**在当前单体内可被 patch 拦截，搬迁后静默失效，而门面测试仍全绿。这是本任务最隐蔽的假通过路径（challenger BLOCKER-1，已核验成立）。

因此契约 **INV-BIND（v2，收紧）**：patch 面符号（含 patch 面模块常量）的**所有引用点**——包括其定义所在叶模块内部的自调用与同模块他调用——一律写成 `state_db.NAME(...)` / `state_db.CONST` 的属性访问形式；`herdr/state/` 内不得出现对 patch 面符号的裸名 `Load`，也不得出现对它的静态 `from … import NAME`。G-2 由"禁静态 import"升级为 AST 级"禁裸名引用"。

```python
# herdr/state/connection.py
def init_db(db_path=None) -> Path:
    state_db = _facade()
    ...
    conn = state_db.get_db_connection(path)   # 同模块内也必须走门面属性
```

被 patch 面清单（AST + 正则双口径实测，方法与结果一并固化给 T1 作门禁输入）：`patch.object/patch/monkeypatch.setattr(state_db, "X")` 44 处 + 字符串目标 `patch("…state_db.X")` 12 处 = **56 个计数点、22 个去重符号**（`setattr(...)` 形式会被两种正则同时命中，因此以**去重符号集合**为准，计数仅作规模参考；G-2 由 AST 自动重抽，不依赖该数字）。〔承接核对：本工位按"目标对象形 + 字符串形 + 别名 module 形"三分类重跑，得 **57 个计数点 / 22 个去重符号 / 27 个测试文件**——38 处 `state_db` 对象形式、12 处字符串形式、7 处别名对象形式（`rollout_policy.state_db`、`api.state_db`、`module.state_db`×2、`observer_engine.state_db`×2、`trajectory_module.state_db`）。原 56 漏计 `fault.setattr` 站点 1 处（R-12 成立）；**22 个符号集合与本节清单逐名相同**，且全部存在于 `state_db.py` 顶层，故 G-2 的判据不受该计数影响。复算命令见 §11。〕22 个符号为：`CONTROLLER_DIR`、`_decode_task_row`、`_get_credentials_dir`、`artifact_ref_exists`、`get_db_connection`、`get_default_db_path`、`get_readonly_db_connection`、`get_rollout_stage`、`get_task`、`get_trajectory_finding_by_id`、`init_db`、`list_rollout_audit`、`list_rollout_states`、`list_steers`、`query_execution_outcomes`、`read_rollout_snapshot`、`record_event`、`record_event_if_absent`、`record_observation_receipt`、`rollout_stage_known`、`save_working_context`、`upsert_trajectory_finding`（21 个函数 + 1 个模块常量）。

**搬迁工作量由此精确可算**（`ast.walk` 对 `herdr/state_db.py` 实测，逐调用点行号可由 T1 脚本再生成）：

| 需改写的裸名调用 | 处数 | 目标形态 |
|---|---|---|
| `get_db_connection` | 79 | `_facade().get_db_connection(path)` |
| `record_event` | 9 | 同上 |
| `_decode_task_row` | 8 | 同上 |
| `get_readonly_db_connection` | 5 | 同上 |
| `get_default_db_path` | 4 | 同上 |
| `_get_credentials_dir` | 3 | 同上 |
| `read_rollout_snapshot` | 2 | 同上 |
| `init_db` | 1 | 同上 |
| `CONTROLLER_DIR`（读） | 6 | `_facade().CONTROLLER_DIR` |
| **合计** | **111 + 6** | G-2 的机器可校验清单 |

〔承接核对：`--inventory` 在本工位输出 `111 bare patch-function calls + 6 CONTROLLER_DIR reads`，与上表逐名相同；6 处 `CONTROLLER_DIR` 裸读位于 80（`get_default_db_path`）、1339（`resolve_state_db_path`）、7869-7872（`migrate_v1_to_v2`，4 处），**全部落在组 A**，故由 T2 一次性改写。另按"改写点归属 = 调用者所在模块"重算得分段配额（T2 19 / T3-1 22 / T3-2 33 / T3-3 20 / T3-4 16 / T3-5 7 = 117），已写入实现计划 §3 各段验收，使"漏改"可被机器定位而非靠自觉。R-05 成立：`_get_credentials_dir` 的 3 处裸调位于 `save_user_token@8076`/`get_user_token@8114`/`delete_user_token@8146`，同属组 A，原计划 T2 清单未列出，已在实现计划 §3 T2 补上。〕

三个实测边界，直接影响 INV-BIND 的适用范围（不得扩大成无差别改写）：

- **`time.*` 不受影响**：函数体内 `time.time/strftime/localtime` 共 **34 处**，其 patch 形式是 `patch("herdr.state_db.time.time")` / `patch.object(state_db.time, "time")`，改的是 stdlib `time` **模块对象的属性**（全局唯一），叶模块各自 `import time` 后调用同样被拦截。⇒ INV-BIND **不**适用于 `time.*`，禁止为此改写 34 处；T1 须包含一条验证用例（门面外模块调用 `time.time()` 时桩生效）把该结论固化为事实而非推断。
- **`_INITIALIZED_DBS`（7 处读/原地 mutate）与 `SHADOW_READ_CAPABILITY`（1 处读）**：测试只做原地 `.discard()`（§2 F-2），不重绑定 ⇒ 同对象别名即可，允许静态 `from .constants import _INITIALIZED_DBS`。
- **只读/不可变常量**（`PROTECTED_*`×3、`OUTCOME_*`、`ROUTE_DECISION_*`、`_OUTCOME_COLUMNS`/`_OUTCOME_COLUMN_NAMES`，体内读合计 13 处）：全仓库无重绑定站点 ⇒ 允许静态 from-import，列入 G-2 白名单。

G-2 因此是**三条规则**而非一条：patch 面 21 函数 + `CONTROLLER_DIR` 禁裸名（含同模块自调用）；`_INITIALIZED_DBS`/`SHADOW_READ_CAPABILITY` 必须别名同一对象；白名单常量允许静态导入。

两点必须写进 T1 的判据，否则会出现"拆分前就存在的失效"被误判为拆分引入的回归：

- 全仓库未发现 `getattr(state_db, …)` / `importlib.reload(state_db)` / `dir(state_db)` 形式的动态引用（门面不必为魔法访问兜底）。
- 但已有 **34 行 `from herdr.state_db import NAME`（16 个文件：9 个生产文件 24 行 + 7 个测试文件 10 行）** 持有名字副本，生产侧集中在 `console/read_api.py`(4)、`console/http_server.py`(3)、`herdr/recovery_store.py`(5)、`herdr/dispatch_recovery.py`(4)、`herdr/task_resources.py`(4)、`herdr/console_queries.py`(1)、`herdr/quality_sources.py`(1)、`herdr/execution_trace.py`(1)、`herdr/kernel.py`(1)。这些调用点**今天就不受** `setattr(state_db, NAME, fake)` 拦截。〔承接核对：两处需修正。① **16 → 17 个文件**（tests 侧实为 8 文件 10 行，原表漏计 `tests/test_console_interaction_api.py:19`、`tests/test_trajectory_observer.py:51`，R-12 成立）。② 按 import **层级**重测后须收窄"桩拦不到"的范围：34 行中只有 **8 行是模块级**（`herdr/console_queries.py:4`、`herdr/execution_trace.py:17`、`herdr/quality_sources.py:31` + tests 5 行），只有这 8 处在 import 期取走名字副本、今天确实拦不到；其余 **26 行是函数级 import**（含 F-2 的三处私有符号站点 `console/read_api.py:111`(缩进 12)、`herdr/task_resources.py:392`(4)、`herdr/recovery_store.py:60`(4)），它们在桩生效窗口内才执行 `IMPORT_FROM`，**能被拦截**。最小实验：模块级副本在 `m.f = FAKE` 后仍返回 `REAL`；函数级 import 同窗口返回 `FAKE`，`patch.object` 窗口内返回 `FAKE2`。⇒ T1 拦截基线必须**按"模块级副本 / 函数级 late-binding"两栏分别登记**，把 34 行整体标为既有失效会把 26 条本应命中的路径误放（与 R-23"每符号一条代表路径 ≠ 全路径覆盖"同源）。〕因此 T1 的拦截基线必须**按调用路径分别登记**（区分"经属性访问的调用点"与"经 from-import 副本的调用点"），把后者的"桩未命中"标为既有事实而非拆分回归；拆分后的要求是"属性访问路径的命中集合不缩小"。

### F-2 133 个文件从门面导入，且有人直接 import 私有符号

实测（正则枚举 `import state_db` / `from … import … state_db` / `from …state_db import NAME` 三种形式，排除 `__pycache__` 与 docs）：**133 个文件、230 处 import 行**——生产/脚本侧 54 个文件 104 行，tests 侧 79 个文件 126 行。〔承接核对：本工位重跑得 **133 文件 / 230 行（prod 54/104 + tests 79/126）**，逐项相同。复算口径必须包含①跨行 `from herdr.state_db import (` 块、②`bin/`、`scripts/` 下无 `.py` 扩展名的入口脚本；缺任一项只得 121/196，会低估使用面。〕多数是模块式 `from . import state_db`（属性访问，门面友好）。但存在**符号式 import（34 行，见 F-1）**，re-export 必须逐名精确覆盖：

- 私有符号被外部直接 import：`herdr/task_resources.py:392`、`herdr/recovery_store.py:60`、`console/read_api.py:111` 均 `import _decode_task_row`
- 模块级常量绑定：`herdr/execution_outcome.py:37` = `state_db.OUTCOME_SCHEMA_VERSION`（import 期求值一次）、`herdr/shadow_rows.py:318` 使用 `ROUTE_DECISION_MAX_LIMIT`
- `herdr/state_store.py:76,97` 读取 `state_db.CONTROLLER_DIR`

后果：门面的 `__all__` 必须覆盖 **166 个顶层绑定**——AST 实测 152 个 `def/class` + **14 个模块级赋值绑定**（`HOME`、`CONTROLLER_DIR`、`_INITIALIZED_DBS`、`SHADOW_READ_CAPABILITY`、`OUTCOME_SCHEMA_VERSION`、`OUTCOME_PER_AGENT_LIMIT`、`_OUTCOME_COLUMNS`、`_OUTCOME_COLUMN_NAMES`、`ROUTE_DECISION_DEFAULT_LIMIT`、`ROUTE_DECISION_MAX_LIMIT`、`OUTCOME_LOOKUP_CHUNK`、`PROTECTED_TASK_METADATA_FIELDS`、`PROTECTED_WORKFLOW_METADATA_FIELDS`、`RESERVED_EVENT_METADATA_FIELDS`），且包括下划线符号；只按 152 建基线会让 G-1 在常量漏 re-export 时仍然通过（challenger MAJOR-6，已核验并登记于 §9：`ast.parse` 顶层 `Assign/AnnAssign` = 14）。共享可变对象（`_INITIALIZED_DBS`、`SHADOW_READ_CAPABILITY`）必须是同一对象的别名而非拷贝——`tests/test_replay.py:20`、`tests/test_eval.py:21`、`tests/test_eval_engine.py:21`、`tests/test_eval_replay_e2e.py:21`、`tests/test_replay_engine.py:20`、`tests/test_critical_reads_fail_closed.py:479,501` 共 **8 处直接 `state_db._INITIALIZED_DBS.discard(...)`** 做用例隔离（全部为原地 mutate，无重绑定；故别名方案充分，见 D-6）。〔承接核对：站点级重数为 **tests 7 处 + `herdr/state_db.py:1301`（`init_db` 体内）1 处 = 8 处**，R-12 的"8→7"指测试站点；两处口径合计仍为 8，且全仓 `_INITIALIZED_DBS` 引用共 8 个行点（定义 84 / 读 364、1276 / `add` 1001、1029、1107、1115 / `discard` 1301），无 `.clear()` 或重绑定 ⇒ D-6 与 MINOR-9 成立。分布对 T2 的含义：`add` 四点全在 V1 尾巴（→ `schema_v1.py`），两点读在 `schema.py`(364) 与 `connection.py`(1276)，`discard` 在 `connection.py`(1301) ⇒ **三个叶模块都要静态 from-import 同一个对象**，G-2(b) 的 `is` 断言须覆盖 `connection`/`schema`/`schema_v1` 三个宿主而非只对门面。〕

### F-3 schema 初始化是"跨进程串行 + 缓存 + 顺序敏感"的单体

- `_ensure_schema` 只在 `get_db_connection:1287` 一处被调用，受 `_INITIALIZED_DBS` 进程内缓存与 `_schema_initialization_lock`（`fcntl` 文件锁，def@87，行段 87-97）保护。
- DDL 计数分两个口径，门禁必须同时覆盖（challenger MAJOR-4，已核验）：
  - **全文件口径**：25 个 `CREATE TABLE`、43 个 `CREATE INDEX/UNIQUE`、12 个 `ALTER TABLE`。
  - **`_ensure_schema`(362-1116) 文本口径**：20 个 `CREATE TABLE`、41 个索引、**0 个 `ALTER TABLE`**——12 个 ALTER 全在它调用的 `_ensure_*_columns` 助手（1118-1259）里。
  - 结论：INV-1/G-4 的检查范围不能只锁 `_ensure_schema` 文本，必须把 `_ensure_*_columns`×6、`_ensure_working_context_source_heads_schema`、`…_clock_schema`、`_ensure_rollout_schema`、`_migrate_rollout_state_key` 一并纳入，否则**最易出错的 4 处穿插子调用（552/553、683-687、925、977）落在门禁盲区**。
- 事务与控制结构实况（决定合法切片边界，challenger BLOCKER-2 已核验成立）：
  - `BEGIN IMMEDIATE`(551) → `COMMIT`(907) 之间是同一事务，**660/661 落在事务内部**；
  - `if not cur.fetchone():`(981) 的 body 延伸到 1113，`else: _INITIALIZED_DBS.add(path_key)` 在 1114-1115，**985/986 落在 `if` 内部，不是合法切片点**；
  - V1 导入只在 legacy 分支内自开 `BEGIN TRANSACTION`(1032)/`COMMIT`(1107)/`ROLLBACK`，而 1001、1029 的 `schema_meta` 写入是 autocommit——原文"986-1115 是同事务内尾巴"的表述不成立，已更正；
  - `_INITIALIZED_DBS.add` 有 4 个标点（1001、1029、1107、1115）分属三条互斥成功路径 + 一条 `else` 路径，重构必须一一对应保留，任何"顺手统一"都会改变重复初始化语义。
- 只读通道 `get_readonly_db_connection`（1342）**刻意不做 DDL**（docstring 1352-1360），失败抛 `ReadonlySchemaError`(1307)；`_rollout_read_connection:4034` 与 `_open_shadow_read_connection:1465` 依赖它，后者是同模块裸调用（见 F-1 INV-BIND v2）。

〔承接核对：本节全部行点在本工位逐行验证通过——`BEGIN IMMEDIATE`@551、`COMMIT`@907（同一事务，660/661 是事务内的 `schema_meta` 建表：660 `conn.execute("""`、661 `CREATE TABLE IF NOT EXISTS schema_meta (`）、`if not cur.fetchone():`@981、`else:`@1114；`_INITIALIZED_DBS.add`@1001/1029/1107/1115；`_ensure_schema` 唯一调用点 `get_db_connection`@1287；提交前助手调用 552/553、683-687，提交后 925/977；`_migrate_rollout_state_key` 自开 `BEGIN IMMEDIATE`@331；`_rollout_read_connection` 定义@4018、其体内调用只读通道@4034；DDL 双口径 25/43/12（全文件）与 20/41/0（`_ensure_schema` 文本范围）复现一致。新增约束：G-7 读的是初始化后的 `sqlite_master`（25 表 / 44 索引 / 24 触发器），与文本口径 43 是不同测量，见 §0。〕

后果：拆分只能做"有序切片 + 编排"，不能重排、不能合并事务边界、不能让叶模块反向触发 DDL。`_INITIALIZED_DBS` 只能有一个宿主模块。

## 3. 目标架构

### 3.1 布局

```
herdr/state_db.py            # 门面（facade）：兼容 re-export，唯一公开导入入口
herdr/state/__init__.py      # 叶模块注册表（按层顺序 import，供门面消费）
herdr/state/_facade.py       # _facade()：延迟取 herdr.state_db 模块对象（INV-BIND 的唯一 helper）
herdr/state/<domain>.py      # 22 个领域模块（§4.0 权威表），band 目标 300-500，8 处登记例外
```

选 `herdr/state/` 作包名，`herdr/state_db.py` 保持**模块**（不是包）：

- 避免 `state_db.py` 与 `state_db/` 同名冲突；
- 保持 `herdr.state_db` 的 module 对象身份不变（所有既有 patch 目标、`herdr.state_db.time.time`、`state_db._INITIALIZED_DBS.discard` 语义连续）；
- `herdr/state/` 名称在仓库中未被占用（仅 `herdr/supervisor/state.py` 同名词，不同包，无冲突）。

### 3.2 分层与依赖方向（单向，无环）

```
L-1 既有     herdr/transitions.py  ←  仅被引用，不引用 state
L0  基座     _facade.py  constants.py（15 个模块级绑定：HOME/CONTROLLER_DIR/PROTECTED_*×3/OUTCOME_*/ROUTE_DECISION_*/_INITIALIZED_DBS/SHADOW_READ_CAPABILITY + 共享 SQL 片段 _route_decision_bucket_exprs）
L1  连接     connection.py → L0                    （15 符号：读写通道、shadow 能力、path 解析、user token）
L2a DDL 段   schema_ddl_helpers.py → L0            （10 个 _ensure_* / _migrate_rollout_state_key，只收 conn 形参，零 DB 连接获取）
L2b DDL 段   schema_ddl_txn.py → L0/L2a            （551-907 单事务段，只收 conn）
L2c DDL 段   schema_v1.py → L0/L2a（静态）+ L3（晚绑定写 API）（979-1115 V1 引导 + migrate_v1_to_v2）
L2d 编排     schema.py → L0/L1/L2a/L2b/L2c          （guard + 事务前 DDL + 有序调用 + 提交后 DDL）
L3  数据域   workflow_task_store / transition_task / transition_gate / completion / checkpoint /
             steering / intervention / collaboration / events / trajectory / outcome_route /
             rollout / observation_finding / context_pack / working_context / context_source_validation
             → 静态 import 只允许指向 L0-L2；同层跨域调用一律 _facade() 晚绑定
L4  门面     state_db.py = re-export(L0..L3 全部 166 顶层绑定)
```

L2 内部再分 a/b/c/d 是 R-层 规则的**必要条件**而非美化：`_ensure_rollout_schema`/`_ensure_*_columns` 被事务段(683-687)与提交后段(925/977)调用，若与调用者同层则必须走晚绑定，而 DDL 段是纯 `conn` 形参函数、无 patch 站点，晚绑定只会增加噪声。层级 a<b<c<d 由"被调用者更低"确定。

规则：
- **R-层**：静态 import 只允许指向更低的层。同层与上行调用一律 `_facade()` 晚绑定。〔承接核对：层的**编号权威是 T1 门禁脚本内嵌的 `LAYER` 表**，不是本文 §3.2 图示——`constants`/`_facade`=0、`connection`=1、`schema_ddl_helpers`=2、`schema_ddl_txn`/`schema_v1`=3、`schema`=4、16 个数据域=5、门面与注册表豁免。由此得出一条本文原图会误导执行的硬结论：**`connection`(1) → `schema`(4) 是上行**，`get_db_connection`@1287 调 `_ensure_schema` 必须写成 `_facade()._ensure_schema(conn, path_key)`（静态 `from .schema import …` 会被 G-5 BLOCK）。§3.3 原写"单向：connection → schema"未说明方向合法性，已更正（R-02/R-05 成立）。〕
- **R-bare**（由 F-1 INV-BIND v2 派生）：`herdr/state/**` 中不得出现对 21 个 patch 面函数与 `CONTROLLER_DIR` 的**裸名** `ast.Name`（含同模块自调用）；必须 `_facade().NAME(...)`。实测改写量 = 111 处调用 + 6 处常量读，清单由脚本穷举生成，不接受"顺手少改一处"。`time.*` 34 处**不在**改写范围（patch 改的是 stdlib 模块对象属性，全局生效）。
- **R-leaf**：叶模块不得在 import 期执行 SQL、读环境变量、或依赖门面已完成 re-export（防止 `herdr.state` 与 `state_db` 循环导入）。`tests/conftest.py:23-41` 在 import 期设置 `HOME`，模块级 `HOME`/`CONTROLLER_DIR` 求值时机不得改变。
- **R-conn**：`conn: Optional[sqlite3.Connection] = None` + `should_close` + 嵌套 `BEGIN IMMEDIATE` 的 15 个写 API 与 67 处 `should_close` 分支，原样搬迁，不改判定顺序（需求阶段已登记该契约）。
- **R-ddl**：DDL 语句序列与事务边界逐字节保持；拆分只允许把连续语句块移入按序调用的私有函数；合法切点白名单见 §4.1.1。

### 3.3 循环依赖清单与破环手段（实测边）

| 边 | 证据 | 处置 |
|---|---|---|
| schema → 上层写 API | `_ensure_schema` 的 V1 导入尾巴调 `save_workflow@1041/1045/1079`、`save_task@1053`、`save_steer@1063`、`record_steering_history@1067`、`_route_decision_bucket_exprs@704` | schema 叶模块内一律晚绑定调用；`_route_decision_bucket_exprs` 是纯 SQL 片段生成器（无 DB 访问、无 patch 记录），T2 先行下沉到 `constants.py`（该模块兼作常量与共享 SQL 片段宿主）消除反向依赖 |
| connection ↔ schema | `get_db_connection:1287` 调 `_ensure_schema`；schema 需要 path | 〔承接核对更正〕调用方向单向 connection → schema，但按门禁 `LAYER` 这是 **L1→L4 上行**：`_ensure_schema` 只能经 `_facade()` 晚绑定调用，`schema` 侧只收 `(conn, path_key)`、不得反向 import `connection`。原判据"schema 只收 conn 即无环"只证明了无静态环，未说明 import 方向合法性（R-05 成立）。 |
| transition → task/event/outcome | `transition_task` 调 `get_db_connection@1260`、`_decode_task_row@1761`、`save_task@1579`、`record_event@3185`、`_outcome_autofinalize_enabled@3829` | `save_task`/`record_event` 晚绑定（二者都是 patch 目标）；`_decode_task_row` 因 F-2 外部也 import，宿主唯一 |
| completion → transition | `compare_and_set_completion_transition@6924` 调 `transition_task` | 晚绑定 |
| context → 多域 | `read_context_compact_snapshot` 调 `_trajectory_rows_in_conn@5198`、`_decode_finding_row@5237`、`_context_source_version_in_conn@5254`、`_decode_context_pack_row@5276` | 同层跨域，晚绑定 |
| intervention → event | `_record_intervention_event@2608` 调 `record_event` | 晚绑定 |
| collaboration → context | `attach_working_context_ref` 调 `_decode_collaboration_row@3076` + context 引用校验 | 晚绑定 |
| 外部 38 处 lazy import | `state_db` 内部函数级 import `recovery_store`/`dispatch_recovery`/`node_dispatch_store`/`execution_outcome`/`completion`/`intervention`/`collaboration`/`trajectory`/`context_sources`/`observation`/`context_projection`/`context_models`/`delivery_rework`/`workflow` | 原样保留在其所属叶模块函数体内，不改位置 |

### 3.3.1 全量跨模块调用边（承接核对补算，取代上表的"清单"地位）

上表只列了 8 条命名边；按 §4.0 的模块归属对 `herdr/state_db.py` 做 AST 穷举（caller 体内对 166 个顶层符号的裸名调用，caller 模块 ≠ callee 模块）实测 **41 条模块对 / 139 处调用点**，其中 **21 条为同层或上行、必须 `_facade()` 晚绑定**，20 条为下行、可静态导入（R-03 成立）。层级取门禁脚本 `LAYER`（域=5）。

| 必须晚绑定（21 条，同层或上行） | 调用点 | 可静态下行（20 条） | 调用点 |
|---|---|---|---|
| `connection` → `schema` (L1→L4) | 2 | `observation_finding` → `connection` | 10 |
| `schema` → `workflow_task_store` (L4→L5) | 4 | `workflow_task_store` → `connection` | 8 |
| `schema` → `steering` (L4→L5) | 2 | `steering` / `trajectory` / `working_context` → `connection` | 6/6/7 |
| `schema_v1` → `workflow_task_store` (L3→L5) | 2 | `transition_gate`/`transition_task`/`checkpoint`/`completion`/`collaboration`/`context_pack`/`intervention`/`events`/`rollout`/`outcome_route` → `connection` | 4/2/5/5/5/5/5/4/4/6 |
| `schema_v1` → `steering` (L3→L5) | 2 | `schema` → `schema_ddl_helpers` | 9 |
| `transition_task` → `workflow_task_store` (L5→L5) | 5 | `schema` → `constants` | 1 |
| `transition_gate` → `workflow_task_store` (L5→L5) | 5 | `schema_v1` → `connection` | 2 |
| `working_context` → `context_source_validation` (L5→L5) | 6 | `rollout` → `schema_ddl_helpers` | 2 |
| `context_pack` → `observation_finding` (L5→L5) | 5 | `outcome_route` → `constants` | 1 |
| `checkpoint` → `workflow_task_store` (L5→L5) | 4 | | |
| `transition_task` → `events` / `completion` / `outcome_route` | 1/1/1 | | |
| `checkpoint`/`completion`/`intervention`/`steering`/`transition_gate` → `events` (L5→L5) | 3/1/1/1/1 | | |
| `completion` → `workflow_task_store`；`context_pack` → `workflow_task_store`；`workflow_task_store` → `outcome_route` | 1/1/1 | | |

三条执行期结论（写入实现计划 §3 各段验收）：

1. **上行边不止 §3.3 原列的 schema 一处**：`connection` → `schema` 与 `schema_v1`/`schema` → 数据域都必须晚绑定；只改"schema 层"会留下 5 条上行静态边，G-5 在扩展后（见 §6 承接门禁补丁）直接 BLOCK。
2. **`working_context` → `context_source_validation` 是 6 点同层边**（`_valid_context_source_ref`×5 + `_validate_context_source_existence`×1，位于 `save_working_context` 体内 6165/6186/6189/6192/6195/6295），加上 `save_working_context` 自身对 `connection.get_db_connection`@6271 的裸调 ⇒ T3-5 的真实改写量是 7 点（= §3 分段配额），原计划只登记了 `get_db_connection`（R-11 成立）。
3. `rollout` → `schema_ddl_helpers`(2) 与 `outcome_route`/`schema`/`schema_v1` → `constants`(4) 是**下行静态边**，按 §4.1/§4.5 的计划实现，不需晚绑定；若实现期把它们改成 `_facade()`，G-5 不会报错但会违反"下行优先静态"的可读性约定，属 MINOR 建议项。

## 4. 模块拆解表（领域组 → 物理模块）

**本表由脚本生成，不是手写估计。** 生成方法固化在 T1 的门禁脚本里（本节点的等价一次性脚本见 §4.8）：

- 用 `ast.parse` 取 `herdr/state_db.py` 全部顶层语句，每个符号的行段 = 本语句 `lineno` 到**下一个顶层语句前一行**（即含其尾部空行/注释，符号之间无空隙、无重叠）；
- 两个巨型函数体按 §4.1.1 白名单切片点手工切段后归段；
- `body` = 该行段合计（搬迁净行数），`est` = `body + 25`（每模块 import/docstring 开销的实测均值）；band 按 `est` 判定；
- 覆盖率：**8029/8069 行已归属**，唯一未归属段是 `1-40`（模块 docstring + `import` 前言），它不进任何叶模块，改为每模块自带所需 import。

结论：**22 个领域模块**（+ `_facade.py`/`__init__.py` 两个非领域脚手架），〔承接核对：按 `--report` 实测 band 分布为 **14 个 OK + 5 个 BELOW + 3 个 ABOVE = 8 条例外**（原文"15 落在区间、4 低于下沿"与 §4.7 的 8 条登记相加得 23，与 22 模块矛盾，R-07 成立；以下方 §4.0 逐行 band 为准）〕，全部偏离逐条登记为例外并给出理由（§4.7）。需求的 5 个领域组是**归档视图**，不是物理模块数。

### 4.0 全量模块表（唯一权威命名 / 行数 / Owner）

| # | 模块 | body | est | band | 组 | Owner | 符号数 |
|---|---|---|---|---|---|---|---|
| 1 | `constants.py` | 114〔承接核对：原 112〕 | 139 | ▼例外 | A | T2 | 15 |
| 2 | `connection.py` | 349 | 374 | ✓ | A | T2 | 15 |
| 3 | `schema.py` | 272〔承接核对：原 273〕 | 297 | ▼边界〔承接核对：按 `--report` 判为 BELOW，已登记 ARCH-4.7-schema〕 | A | T2 | 2 def + 4 个 `_ensure_schema` 段 |
| 4 | `schema_ddl_txn.py` | 357 | 382 | ✓ | A | T2 | 1 段 |
| 5 | `schema_ddl_helpers.py` | 406 | 431 | ✓ | A | T2 | 10 |
| 6 | `schema_v1.py` | 241 | 266 | ▼例外 | A | T2 | 1 def + 1 段 |
| 7 | `workflow_task_store.py` | 387 | 412 | ✓ | B | T3-1 | 10 |
| 8 | `transition_task.py` | 461 | 486 | ✓ | B | T3-1 | 3 |
| 9 | `transition_gate.py` | 452 | 477 | ✓ | B | T3-1 | 4 |
| 10 | `completion.py` | 479 | 504 | ▲边界 | D | T3-2 | 8 |
| 11 | `checkpoint.py` | 301 | 326 | ✓ | D | T3-2 | 6 |
| 12 | `steering.py` | 239 | 264 | ▼例外 | F | T3-2 | 6 |
| 13 | `intervention.py` | 312 | 337 | ✓ | F | T3-2 | 9 |
| 14 | `collaboration.py` | 292 | 317 | ✓ | F | T3-2 | 10 |
| 15 | `events.py` | 445 | 470 | ✓ | E | T3-3 | 4 |
| 16 | `trajectory.py` | 244 | 269 | ▼例外 | E | T3-3 | 6 |
| 17 | `outcome_route.py` | 404 | 429 | ✓ | E | T3-3 | 11 |
| 18 | `rollout.py` | 346 | 371 | ✓ | E | T3-3 | 12 |
| 19 | `observation_finding.py` | 433 | 458 | ✓ | C | T3-4 | 16 |
| 20 | `context_pack.py` | 314 | 339 | ✓ | C | T3-4 | 8 |
| 21 | `working_context.py` | 516 | 541 | ▲例外 | C | T3-5 | 8 |
| 22 | `context_source_validation.py` | 677 | 702 | ▲例外 | C | T3-5 | 2 |
| — | `state_db.py`（门面） | — | ≤400 | ✓ | — | T2 建 / T4 定稿 | 166 再导出 |

命名以本表为准；§3/§7/§9 与实现计划若与本表不符，以本表为准。`outcome_route.py` 合并 outcome(246) + route decision(158) 两段〔承接核对：脚本归并后 body=403（原写 404，差 1 行为段界归属），以 `--report` 为准；246/158 是手工子段估算值〕（拆成两个模块各 ≈180/≈180，双双低于 band，且同属"Adaptive Router 写时事实"这一语义域）；`_route_decision_bucket_exprs` 是纯 SQL 片段生成器（无 DB 访问），已下沉 `constants.py` 以消除 schema 层对上层的方向违规。

### 4.1 组 A：schema / connection（6 模块，T2 独占）

| 模块 | body | 内容（符号@起始行） |
|---|---|---|
| `constants.py` | 114〔承接核对：原 112，以 `--report` 为准〕 | `HOME@41`、`CONTROLLER_DIR@42`、`_INITIALIZED_DBS@84`、`SHADOW_READ_CAPABILITY@1400`、`OUTCOME_SCHEMA_VERSION@3822`、`OUTCOME_PER_AGENT_LIMIT@3826`、`_OUTCOME_COLUMNS@3838`、`_OUTCOME_COLUMN_NAMES@3850`、`ROUTE_DECISION_DEFAULT_LIMIT@4393`、`ROUTE_DECISION_MAX_LIMIT@4396`、`OUTCOME_LOOKUP_CHUNK@4400`、`_route_decision_bucket_exprs@4425`、`PROTECTED_TASK_METADATA_FIELDS@6626`、`PROTECTED_WORKFLOW_METADATA_FIELDS@6639`、`RESERVED_EVENT_METADATA_FIELDS@6646` |
| `connection.py` | 349 | `_db_path_for_tasks_file@45`、`_db_path_for_workflow_file@51`、`get_default_db_path@61`、`get_db_connection@1260`、`init_db@1297`、`ReadonlySchemaError@1307`、`resolve_state_db_path@1316`、`get_readonly_db_connection@1342`、`_resolve_shadow_read_capability@1411`、`assert_shadow_read_capability@1422`、`_open_shadow_read_connection@1454`、`_get_credentials_dir@8065`、`save_user_token@8076`、`get_user_token@8114`、`delete_user_token@8146` |
| `schema.py` | 272〔承接核对：原 273〕 | `_schema_initialization_lock`（`@contextmanager`@87 / `def`@88）+ 编排器 `_ensure_schema` 的 3 个内容段：head/guard `362-366`、事务前 DDL `367-550`、提交后 DDL `908-978`；〔承接核对〕原列的"函数尾 `1116-1117`"是**两个空行**（`_ensure_schema` AST 终点 1115），不构成切片段，仅作为段分配的行尾计入 `schema.py`，实现期不得为它生成代码。白名单切片以 §4.1.1 的 5 段（含 V1 尾巴 979-1115）为准。 |
| `schema_ddl_txn.py` | 357 | `551-907`：`BEGIN IMMEDIATE` → `COMMIT` 的**单事务**全段（含 heads/clock 调用@552-553、列迁移调用@683-687、`_route_decision_bucket_exprs` 表达式索引@704） |
| `schema_ddl_helpers.py` | 406 | `_ensure_working_context_source_heads_schema@98`、`…_clock_schema@180`、`_ensure_rollout_schema@258`、`_migrate_rollout_state_key@311`、`_ensure_task_columns@1118`、`_ensure_completion_columns@1135`、`_ensure_event_columns@1164`、`_ensure_intervention_columns@1190`、`_ensure_working_context_columns@1208`、`_ensure_eval_replay_columns@1218` |
| `schema_v1.py` | 241 | `migrate_v1_to_v2@7961` + `_ensure_schema` 的 V1 引导尾巴 `979-1115`（`schema_meta` 判定、`has_existing_state` 分支、legacy JSON 导入、4 处 `_INITIALIZED_DBS.add`） |

**`_ensure_rollout_schema` / `_migrate_rollout_state_key` 唯一宿主 = `schema_ddl_helpers.py`**（修正：此前 `rollout.py` 与 schema 组重复认领）。`rollout.py` 需要建表能力时按下行静态导入（L3 → L2 helper，方向合法）；实测这两个符号在 tests/ 与生产侧**无任何外部引用**，故静态导入不破坏 patch 语义。

#### 4.1.1 `_ensure_schema` 允许的切点（硬约束）

`_ensure_schema` 不是无结构 DDL 堆，函数体内有事务边界与状态标记，切片必须落在以下白名单位置：

| 原行号 | 语义 | 对切片的要求 |
|---|---|---|
| 362-366 | `def` + docstring + `if path_key in _INITIALIZED_DBS: return` | 留在 `schema.py` 编排器顶部，不得下沉到片段 |
| 367-550 | 建表/索引，**事务外** | `schema.py` 段1；段1 内部不得引入 `BEGIN` |
| **551** | `conn.execute("BEGIN IMMEDIATE;")` | 事务起点；551-907 是**一个事务**，整体归 `schema_ddl_txn.py`，内部禁止再切 |
| 552-906 | 事务内：heads(552)/clock(553)/列迁移(683-687)/schema_meta(661)/user_tokens(668)/eval_results(753) 等 | 编排器持有 conn，按序调用，不提前 COMMIT |
| **907** | `conn.execute("COMMIT;")` | 事务终点语句保持相对位置 |
| 908-978 | 提交后：`replay_specs`(912)、`_ensure_eval_replay_columns`(925)、`_ensure_rollout_schema`(977) | `schema.py` 段3；`_ensure_rollout_schema` → `_migrate_rollout_state_key` 在 legacy 库上会**自开 `BEGIN IMMEDIATE`**(331)，该调用点必须保持在 907 之后（当前靠 320-330 列检查在新库提前 return 才未暴露——真实脆弱点，禁止改动顺序） |
| 979-1115 | `schema_meta.v1_migration_done` 判定 + `has_existing_state` 分支 + legacy JSON 导入 + `COMMIT`/`ROLLBACK` + 4 处 `_INITIALIZED_DBS.add`（1001/1029/1107/1115） | **整段原子搬迁**到 `schema_v1.py` 单个函数，禁止再拆：`if not cur.fetchone():`(981) 的 body 延伸到 1113，`else:`(1114-1115) 与之一体；981 之内没有合法切点 |

补充实测依赖边（切片不能违反）：`_ensure_event_columns`(683) 依赖 `events` 表先建（443）；`_ensure_eval_replay_columns`(925) 依赖 `eval_results`(753)/`replay_specs`(913)；`_ensure_rollout_schema`(977) 依赖 `rollout_state`(275)；V1 尾巴调 `save_workflow@1041/1045/1079`、`save_task@1053`、`save_steer@1063`、`record_steering_history@1067`、`_route_decision_bucket_exprs@704`——schema 层反向上层写 API，由晚绑定（D-2）承接。G-4 门禁以**运行时 executed-statement trace 等价**覆盖以上全部约束（见 §6）。

> `_resolve_shadow_read_capability`(1411) 对 `_OUTCOME_COLUMN_NAMES`(3850) 存在**前向引用**（docstring 1414 明确按首次使用解析）。两个符号都归 `constants.py`（本表 #1）+ `connection.py`，即 T2 必须一次性落定常量层，否则 T3-3 迁移 outcome 时 `connection.py` 引用到已消失的名字。这是 D-2 之外"T2 必须先行、T3-* 只能在 T2 之后并发"的第二条硬理由。

### 4.2 组 B：workflow + task（3 模块，T3-1）

| 模块 | body | 内容 |
|---|---|---|
| `workflow_task_store.py` | 387 | workflow 4 API + task 6 API（`save_task@1579` 182 行为主体；`_decode_task_row@1761` 被 3 处外部 from-import，宿主唯一） |
| `transition_task.py` | 450 | `transition_task@6658`、`compare_and_set_task_transition@6889`、`compare_and_set_completion_transition@6924` |
| `transition_gate.py` | 452 | `transition_workflow@7119`、`update_task_metadata@7330`、`update_workflow_metadata@7404`、`force_pass_gate@7468` |

### 4.3 组 C：observation / finding / context（4 模块，T3-4 / T3-5）

| 模块 | body | 内容 |
|---|---|---|
| `observation_finding.py` | 433 | finding 6 + observation 5 + 2 个 fingerprint helper + `_compact_observation_metadata` + `_context_source_version_in_conn@4913`/`get_context_source_version@4966` |
| `context_pack.py` | 314 | pack CRUD 5 + `read_context_compact_snapshot@5184` + `_trajectory_rows_in_conn@5140` + `_task_matches_run@5174` |
| `working_context.py` | 516 | `_decode_working_context_row@5433`、`save_working_context@6130`(289)、`register_working_context_source@6419`、`working_context_source_is_current@6480`、`record_working_context_metric@6507`、`get/latest/list@6543/6556/6597` |
| `context_source_validation.py` | 677 | `_valid_context_source_ref@5453` + `_validate_context_source_existence@5463`(667) |

**`_validate_context_source_existence` 不做内部切片**（本节点初稿曾提出"三段 helper 抽取"，已被实测证据否决，见 §9 MAJOR-5）：函数体 667 行〔承接核对：段长 667（5463-6129）/ AST 函数体 666〕含 **16 个嵌套 def**〔承接核对：AST 实测 **15 个**嵌套 `def`（5463-6129 内缩进 `def` 行同为 15），原文 16 多计 1；不改变结论〕，存在跨阶段闭包读（`relevant_peer_tasks` 定义@5915 但被 ~5790 调用的闭包读取）〔承接核对：证据形式需更正且**危害更强**——`relevant_peer_tasks` 不是 def，而是**局部变量**：`relevant_peer_tasks: set[str] = set(verified_handoff_tasks)` 在 **5915** 赋值（5917 再 `update(_dependency_closure_peers())`），而 **5790** 的一个定义在前的嵌套函数体里就读它（`str(task_id) not in relevant_peer_tasks`），6095 又读一次。即"读取点源码位置早于赋值点"，正确性完全依赖**调用发生在赋值之后**；把它按段抽成两个 helper 会直接产生 `UnboundLocalError` 或读到半集合，属比"闭包读另一个 def"更硬的不可拆证据（R-17 的"整块搬迁"裁定因此更稳）。另实测 `taskless_allowed_runs` 行点为 5485/5770/5779/…/6098，**17 处 `raise` 全部落在 6054-6128**（AST 复算 17/17 命中）〕，而全部 **17 处 raise 集中在 6054-6128**——即"采集/分类/校验"三段之间不是数据流边界而是共享可变状态。按段抽取会改变 fail-closed 判定顺序，违反实现计划 §0 INV-1（不改语义）。故 `context_source_validation.py` 登记为 **702 行 band 上限例外**（全案唯一"必须整块搬迁"的模块），交总指挥裁决（§10 Q-2）。〔承接核对：Q-2 已由 `wf-haflow-1020-01` 需求方口径裁定为 Variant A + 例外登记，见 §10 与本目录实现计划 §1。〕

### 4.4 组 D：completion / checkpoint / migration（2 模块，T3-2）

| 模块 | body | 内容 |
|---|---|---|
| `completion.py` | 479 | completion observation 8 API（`observe_completion@1867` 230 行 + `observe_blocker_marker@2134` 122 行 + `_coerce_version@2097`/`_blocker_sample_consumed@2105` helper） |
| `checkpoint.py` | 301 | 6 个 checkpoint API（create/list/get/restore/fork/lineage） |

migration 的物理归属：V1 JSON 自动导入与 `migrate_v1_to_v2` 在组 A `schema_v1.py`——迁移代码与 DDL 同属"库结构演化"，与"快照时间旅行"不同生命周期，不与 checkpoint 合并。`completion.py` est=504 为**上限临界**：实现期若实测 >500，优先把 `_coerce_version`(8)/`_blocker_sample_consumed`(29) 中确认为纯函数者下沉 `constants.py`，而不是拆散 `observe_completion`。

### 4.5 组 E：rollout / outcome / eval / trajectory（4 模块，T3-3）

| 模块 | body | 内容 |
|---|---|---|
| `events.py` | 445 | `record_event@3185`、`record_event_if_absent@3240`、`list_events@3321`、`aggregate_run_metric_rows@3576`(246) |
| `trajectory.py` | 244 | `record_trajectory_event@3384`、`record_observation_receipt@3461`、`list_trajectory_events@3532`、`latest_trajectory_sequence@4634`、`trajectory_event_exists@4647`、`artifact_ref_exists@4665` |
| `outcome_route.py` | 403〔承接核对：原 404〕 | outcome 7（`_outcome_autofinalize_enabled@3829`、`_decode_outcome_row@3855`、`insert/get/read_execution_outcome@3882/3951/3969`、`query_execution_outcomes@4337`、`batch_get_execution_outcomes@4589`）+ route decision 4（`ExactDecisionBucket`：`@dataclass(frozen=True)`@4403 / `class`@4404、`_exact_bucket_sql@4453`、`_route_decisions_sql@4482`、`query_route_decisions@4521`） |
| `rollout.py` | 346 | 12 个 rollout API（`transact_rollout_stage@4235` 102 行为主体；`_rollout_read_connection@4018` 走只读通道） |

### 4.6 组 F：需求 5 组未覆盖的残差域（3 模块，T3-2）

需求的 5 个领域组未命名以下 3 个真实存在的域，显式登记，不硬塞进无关组：

| 模块 | body | 内容 | 归组理由 |
|---|---|---|---|
| `steering.py` | 239 | `save_steer@2342`…`list_steering_history@2550` 6 API | 人工转向指令，非 workflow/task 主记录 |
| `intervention.py` | 312 | intervention 9 API | 人工干预生命周期，`claim_intervention@2756` 含乐观并发语义 |
| `collaboration.py` | 292 | collaboration 10 API | 人机协同事件 + working_context 引用 |

band 下沿合并选项（实测）：`steering`(239) + `collaboration`(292) = 531 > 500，**不可合并**；`steering` + `intervention`(312) = 551，同样超限。因此三域各自成模块，`steering.py` est=264 登记为 ▼ 例外（`trajectory.py` est=269 同理：并入 `events.py` 达 690，且 Trajectory=历史事实 / Event=记录通道属 AGENTS.md §4 语义分层，禁止混装）。

### 4.7 band 例外登记表（G-3 白名单，逐条给理由）

| 模块 | est | 偏离 | 理由 | 若不成立的后果 |
|---|---|---|---|---|
| `constants.py` | 139〔承接核对：原 137〕 | ▼161（原 ▼163） | 纯常量 + 纯 SQL 片段宿主，无 DB 访问；与任何域合并即产生"常量反向依赖业务模块" | 拆散常量 → 多宿主 → 第二事实源 |
| `schema.py` | 297〔承接核对：原 298〕 | ▼3（原文写作"▼2 视为 ✓"） | 编排器 + 事务外 DDL 两段；事务段(551-907)不能并入否则单文件 730。〔承接核对：`--report` 判为 **BELOW 且已登记**，G-3 须按登记例外放行，不得当作 OK；band 计数因此是 14 OK / 5 BELOW / 3 ABOVE〕 | 并入 → 违反 R-ddl 且超限 |
| `schema_v1.py` | 266 | ▼34 | V1 引导分支原子体（`if/else` + 4 个初始化标记），与常规 DDL 生命周期不同 | 与 `schema_ddl_txn` 合并 → 640 超限且混淆事务边界 |
| `steering.py` | 264 | ▼36 | 见 §4.6，与任一组合法配对均超 500 | 强合 → 语义混装 |
| `trajectory.py` | 269 | ▼31 | AGENTS.md §4.3 语义分层（Trajectory≠Event） | 与 events 合并 → 690 超限 + 破坏分层 |
| `completion.py` | 504 | ▲4 | `observe_completion` 230 行为单函数不可拆 | 实现期 >500 则下沉 2 个纯 helper（§4.4） |
| `working_context.py` | 541 | ▲41 | `save_working_context` 289 行单函数；拆出的 source-注册三 API 只有 124 行，落入 ▼ | 二选一冲突，交 T3-5 按 G-3 实测择一并登记 |
| `context_source_validation.py` | 702 | ▲202 | §4.3 实测不可安全切片（闭包跨阶段读 + 跨阶段可变状态） | 强行拆 → 改变 fail-closed 顺序，违反 INV-1 |
| `transition_gate.py` | 实际 546 | ▲46 | `above=ARCH-4.7-transition-gate-port`：register_workflow 与 transition_workflow 同属事务状态网关；第 9 条登记例外 | 按行数改归属会违背 D-P1 的职责裁决 |

### 4.8 可复现性要求（T1 交付物）

`scripts/check_state_db_split.py` 必须内置本节的段分配表（符号 → 模块 → 行段），并支持 `--report` 输出与 §4.0 同形的表（模块 / body / est / band / 覆盖率）。任何后续对 §4 的手工改动都必须先让脚本重跑一致，否则以脚本为准。基线数据：`covered=8029/8069`，未覆盖仅 `1-40`。

## 5. 门面与兼容策略

- `herdr/state_db.py` 收敛为 re-export 层：`from .state import constants, connection, schema, …` + 显式列出**全部 166 个顶层绑定**到 `__all__`（含下划线符号），不做代理/`__getattr__` 魔法（对齐既有 INV-IMPORT 精神：显式、单向、禁 exec/globals 注入/代理模块，见 `docs/specs/console-module-split.md`）。
- 门面内保留 `import time/json/os/sqlite3`，维持 `herdr.state_db.time.time` 这类 patch 路径可用（实测该路径是 stdlib 模块属性打桩，全局生效，见 §2 F-1）。
- `_INITIALIZED_DBS`/`SHADOW_READ_CAPABILITY` 经 `from .state.constants import NAME` 形成**同一对象别名**；禁止拷贝或包装。
- 门面不得成为实现宿主（除 re-export 与 `_facade()` 自注册外不得新增逻辑），否则 `state_db.py` 无法收敛到目标规模。
- 目标规模：`state_db.py` ≤ ~400 行（166 行 import + `__all__` 块 + docstring）。
- 兼容验收不只做"符号存在"：F-2 实测 34 行 `from herdr.state_db import NAME`（16 文件）持有名字副本，拆分后这些副本必须仍指向同一实现；`herdr/execution_outcome.py:37` 的 import 期常量绑定（`OUTCOME_SCHEMA_VERSION`）求值时机不得改变。

## 6. 门禁（把口头约束变成可执行检查）

| 门禁 | 检查内容 | 失败即 |
|---|---|---|
| G-1 完整性 | 门面导出符号集合 == 拆分前 **166** 顶层绑定快照（AST 提取，含 `Assign`/`AnnAssign` 常量与私有符号） | BLOCK |
| G-2 绑定形式 | 三条子规则：(a) `herdr/state/**` 内对 21 个 patch 面函数 + `CONTROLLER_DIR` 不得出现裸名 `ast.Name`（`Call.func` 与 `Load` 均计，含同模块自调用），必须 `_facade().NAME`；(b) `_INITIALIZED_DBS`/`SHADOW_READ_CAPABILITY` 必须别名同一对象（`is` 比较）；(c) §4.7 白名单常量允许静态 from-import | BLOCK |
| G-3 band | 每模块**文件实行数** ∈ [300, 500]；例外必须命中 §4.7 登记表（模块名 + 偏离方向 + 理由 ID），未登记的偏离即失败 | WARN→BLOCK |
| G-4 DDL 等价 | **运行时 executed-statement trace 等价**，而非文本 diff：在临时目录分别对"拆分前/拆分后"跑 4 条建库路径 —— (i) 全新空库、(ii) 已存在且 `schema_meta.v1_migration_done='1'`、(iii) 已有 runtime state 的升级库（`has_existing_state=True`）、(iv) legacy JSON 存在的首次导入 —— 用 `set_trace`/`exec_driver_sql` 包装记录每条实际执行语句与事务事件顺序，两侧序列必须逐项相等 | BLOCK |
| G-5 环检测 | `herdr.state.*` 与 `herdr.state_db` 的静态 import 图无环，且每条静态边满足 R-层（低层方向） | BLOCK |
| G-6 真实链路 | 每个领域组至少 1 条 CLI/Controller → 核心 → 持久化 → 读回 链（AGENTS.md §6） | BLOCK |
| G-7 语义不变 | 表结构/索引/PRAGMA 集合与拆分前一致（`sqlite_master` dump 比对），"不改语义表结构"的机器化表达 | BLOCK |

G-4 的口径修正说明（challenger MAJOR-3 成立）：规范化文本 diff 只能作**预筛**——`_ensure_schema` 的 981/1114 `if/else`、1001/1029/1107/1115 四处 `_INITIALIZED_DBS.add`、以及 907 之后的自开事务调用点(977→331)属于**控制流与状态语义**，文本相等不能证明路径等价；只有按上述 4 条路径各跑一次并比较执行序列，才能覆盖 F-3 登记的全部不变量。文本 diff 保留为快速失败的第一道。

G-1/G-2/G-3/G-5/G-7 由 `scripts/check_state_db_split.py` 单文件实现（纯 stdlib AST + sqlite3，不引新依赖），G-4 由 `tests/test_state_db_ddl_sequence.py` 实现；既进 CI 也供本地跑。

### 6.1 承接门禁补丁（requirements 节点 R-02/R-09/R-10/R-15/R-21/R-23 成立项，必须在实现段 S0 落地）

继承的 T1 候选（`a1614e5`）在本基线上实跑结论：`--report` 与本节 §4.0 一致、`OVERALL: PASS`、`G-7: 25 tables / 44 indexes / 24 triggers`。但下列 5 处是**门禁自身的洞**，不修则"绿"不成立；全部为脚本/测试文件内的改动，不触碰被验收能力：

| ID | 洞（已实测确认） | 修法（最小） | Owner | 判据 |
|---|---|---|---|---|
| GA-1 | **G-5 只扫 `tree.body` 顶层 import**，函数体内 `from .schema import _ensure_schema` 这类上行静态边可绕过（R-02） | `_resolve_import_edges` 改遍历整棵树（`ast.walk`），函数级 import 同样受 R-层约束 | S0/T2 | 新增用例：叶模块函数体内上行静态 import → G-5 BLOCK |
| GA-2 | **G-3 `PENDING`、G-2 `WARN` 不参与 `OVERALL` 判定**（`blocked = any(status=="BLOCK")`），迁移中途可"假称 G-3 通过"（R-15） | 流程固化：S0 起每段验收必须跑 `--final`（脚本已实现 `--final` 把缺失模块/`__all__` 升为 BLOCK）；段内允许 PENDING 但**必须在台账记录该段 PENDING 集合**，禁止以 OVERALL=PASS 代替 G-3 结论 | 全段 | 段报告含 `--final` 输出原文 |
| GA-3 | **G-4 四条建库路径不含"legacy 库 + rollout `bucket_key` 已存在"**；把 `_ensure_rollout_schema`@977 误前移到 COMMIT 之前，四条路径仍全绿，真实 legacy 库报嵌套事务错（R-09） | `tests/test_state_db_ddl_sequence.py` 增第 (v) 条路径：先建库→写入 rollout 行（触发 `_migrate_rollout_state_key`@331 自开事务）→再次初始化 | T2（S1）| 5 路径 trace 逐项相等；前移 977 必须使第 (v) 路径失败 |
| GA-4 | **G-4 只比语句序列、不比值**；legacy JSON 字段映射错位可静默逃逸（R-10） | 对路径 (iv) 增加导入后的行值断言（workflow/task/steer/steering_history 各 ≥1 条关键字段），断言"语句序 + 值"双一致 | T2（S1）| 故意改一个字段映射 → 用例必须失败 |
| GA-5 | **拦截测试是"每符号一条代表路径"**，`_get_credentials_dir` 等仍有漏项；且需按 §2 F-1 的"模块级副本 / 函数级 late-binding"分栏（R-23/承接核对） | 22 个 patch 面符号逐一枚举其**全部**真实调用点（脚本 `--inventory` 已给行号），按调用点登记命中矩阵；函数级 from-import 站点必须标为"应命中" | S0 | 矩阵行数 ≥ 117；"既有失效"仅允许 §2 F-1 的 8 个模块级站点 |

另两条继承判据的强化（不改脚本，改测试）：R-21 要求"一次初始化"不能只靠 `tests/test_state_db_v2.py:127-180` 的 spawn 不报错——该段已实测存在（`_run_concurrent_state_db_open` 在 127 行窗口内），但需补一条断言：并发 spawn 后 `_INITIALIZED_DBS` 命中且第二进程不再执行 DDL 语句；R-3 的 rollout 只读通道（`_rollout_read_connection@4018` → `get_readonly_db_connection@4034`）需专项断言"不做 DDL、失败抛 `ReadonlySchemaError`"。

## 7. 解耦任务的并发可行性（文件重叠分析）

| 阶段 | 任务 | 写文件集合 | 重叠判定 |
|---|---|---|---|
| P0 | T1 测试与门禁脚手架 | `tests/test_state_db_facade_compat.py`、`tests/test_state_db_patch_interception.py`、`tests/test_state_db_characterization_context_source.py`、`tests/test_state_db_ddl_sequence.py`、`scripts/check_state_db_split.py` | 与后续无重叠（只读被测码） |
| P1 | T2 基座 + 组 A 迁移 | 新增 `herdr/state/{__init__,_facade,constants,connection,schema,schema_ddl_txn,schema_ddl_helpers,schema_v1}.py`；改 `herdr/state_db.py`（删组 A 符号 + 建立 6 个域锚点块）；组 A 相关 tests | `state_db.py` 此阶段唯一写入者 |
| P2 | T3-1..T3-5 领域迁移（5 路并发） | 各自新增 `herdr/state/<域模块>.py`（§4.0 表 Owner 列，两两不相交）+ 各自域测试 + `herdr/state_db.py` 的**本域专属锚点块** | 模块/测试零重叠；`state_db.py` 为唯一共享写文件 |
| P3 | T4 门面定稿、band 结算、全量验证 | `herdr/state_db.py`（收口）、`docs/`、`wiki/` | 依赖全部 P2 |

`herdr/state_db.py` 的共享写冲突如何消解（并发成立的前提，须在 T2 落地）：

1. T2 在门面中为 6 个域预留**固定锚点注释块**（`# ---- re-export: domain=<A|B|C|D|E|F> ----`），每个锚点独占一段连续行；T3-x 只在自己的锚点下追加 import 与 `__all__` 条目，不改动他人锚点。
2. T3-x 从 `state_db.py` **删除本域符号定义**，删除集合由 §4.0 符号列穷举界定（6 组符号集两两不相交，脚本可校验并集 = 166 − 组 A）；删除按符号名定位（T1 门禁脚本提供符号→行号映射），不以绝对行号硬编码，避免基线漂移。
3. 模块级常量全部由 **T2 一次性落 `constants.py`**（含 `PROTECTED_*`、`OUTCOME_*`、`ROUTE_DECISION_*`、`_OUTCOME_COLUMNS`/`_OUTCOME_COLUMN_NAMES`、`_route_decision_bucket_exprs`）。T3-x **不得**新增或搬动模块级常量，否则 5 路并发的符号集不再不相交。
4. 控制器按 T3-1 → T3-5 顺序串行收编。预期冲突面仅为锚点块相邻行与 `__all__` 列表尾部——属文本级可自动/半自动合并，不构成设计冲突；任一 Task 失败只 rework 该域，不阻塞其它域（Selective Replan 目标）。
5. 判据固化：**无文件冲突 + 无功能依赖**（T3-x 只依赖 T2 的 `constants/connection/schema*/_facade` 契约）⇒ 实现阶段可 5 路并发。计划阶段不为此预先开 Pane。

### 7.1 承接核对：本节"5 路并发"结论已被作废（作废的是派发形态，不是分解依据）

〔承接核对〕本小节是 `wf-haflow-1020-01` 与 `wf-haflow-1019-02` 口径分叉点，必须写明，否则实现阶段会按 §7.5 去开 5 个 Pane 并复现上一单死锁：

1. **"模块/测试两两不相交"成立，但"5 路并发"不成立**：6 个迁移任务（T2 + T3-1..T3-5）都写 `herdr/state_db.py`，唯一共享写文件；`# ---- re-export: domain=X ----` 锚点只把**冲突面**缩小到相邻行，不等于满足节点规则"修改文件互不重叠"的并发前提（R-08 成立）。另实测 `herdr/state/__init__.py` 注册表也是共享写文件，原 §7 未登记（R-14 成立），已并入 S0/T2 归属：注册表由 T2 建立、T3-x 只在自己的锚点区间追加、T4 收口。
2. **平台侧硬约束**：当前构建内节点一旦含 `completed/cleaned` task 就拿不到第二轮 `node_dispatch` 凭据（`herdr/node_dispatch_store.py:113-158` 只在有 redispatch 候选或节点无 task 时铸造；`herdr/direct_dispatch.py:638` 之后返回 `mode=wait`），上一单 `wf-haflow-1019-02` 的 implementation 节点正是由此在 T1 后死锁；同时 `max_concurrency=3` 使 5 路派发本身也不可用。
3. **裁定**：实现节点 = **1 个 Task、1 个 pane、内部顺序分段** S0→S1(T2)→S2..S6(T3-1..T3-5)→S7(T4)。§4.0 的 Owner 列与 §7 的文件归属表**继续作为分段边界与 Selective Replan 定位依据**（哪段失败只回退该段），但不再作为并发派发依据。权威表述见实现计划 §2/§5。

已知跨域调用（须晚绑定，不阻塞并发）：§3.3 全部 8 条实测边 + `transition_task` 体内 `get_db_connection@1260`/`_decode_task_row@1761,6736,6811`/`save_task@1579`/`record_event@3185`/`_outcome_autofinalize_enabled@3829`；`compare_and_set_completion_transition@6924` → `transition_task`；`read_context_compact_snapshot` → `_trajectory_rows_in_conn@5198`/`_decode_finding_row@5237`/`_context_source_version_in_conn@5254`/`_decode_context_pack_row@5276`；`_record_intervention_event@2608` → `record_event`。

## 8. 架构决策记录（ADR）

| ID | 决策 | 备选项与放弃理由 | 依据证据 |
|---|---|---|---|
| D-1 | 叶模块放 `herdr/state/` 包，`herdr/state_db.py` 保持**模块**身份 | 备选一：改成 `state_db/` 包 + `__init__.py` 作门面 → 放弃：改变 module 对象身份与 `__file__`，133 个使用方 230 处 import 的 patch/常量绑定语义需重证，风险无收益。备选二：`state_db_*.py` 平铺 → 放弃：22 个文件污染包根、失去分层表达 | §2 F-1/F-2；`herdr/state` 名称未占用 |
| D-2 | patch 面符号的**所有**引用点走 `_facade()` 属性访问（INV-BIND v2 / R-bare） | 备选甲：只改跨模块调用 → 放弃：同模块裸调用 111 处搬迁后静默失效（BLOCKER-1）。备选乙：静态 `from .x import name` + 批量改测试 patch 目标 → 放弃：把"测试锚点"当事实源改写，等于用测试替身补实现，改动面 >20 个测试文件 | §2 F-1 实测改写表；本地 `patch.object` 实验 |
| D-3 | 需求 5 领域组 → **22** 物理模块（+2 脚手架），组是归档视图 | 备选：严格 5 模块 → 算术不可行（5×500=2500 < 8069）；备选：机械按 300-500 均分 → 放弃：产生无语义配对（如 steering+intervention=551 超限） | §1 算术裁决、§4.0/§4.6/§4.7；需求阶段 `obs_fbe08bdd5fa04813999512f11e29d7dd` |
| D-4 | DDL 只做"连续片段有序搬迁 + 编排器"，不重排、不并事务；合法切点白名单化 | 备选：按表分域、各域自带 DDL → 放弃：`_ensure_schema` 单事务 + 索引依赖表存在 + V1 导入尾巴夹在同一函数中，重排会改变建库语义 | §2 F-3、§4.1.1；G-4 门禁 |
| D-5 | `_ensure_schema`(756) 按 4 段切片；`_validate_context_source_existence`(667) **整块搬迁**并登记 band 例外 | 备选：对后者做三段 helper 抽取（本节点初稿方案）→ 放弃：实测 16 个嵌套 def 存在跨阶段闭包读与跨阶段可变状态，17 处 raise 集中在尾部，抽取会改变 fail-closed 顺序（违反 INV-1）。band 冲突点显式登记交总指挥裁决，不静默放宽 | §4.3、§9 MAJOR-5 |
| D-6 | `_INITIALIZED_DBS`/`SHADOW_READ_CAPABILITY` 单一宿主（`constants.py`）+ 别名 re-export | 备选：各模块各自持有 → 放弃：直接产生第二事实源（AGENTS.md §4.1）。备选：为 `NAME = state_db.NAME` 式重绑定担心 → 排除：实测 8 处测试站点全为原地 `.discard()`，无重绑定 | §2 F-2；§9 MINOR-9 |
| D-7 | 门禁脚本 `scripts/check_state_db_split.py` 承担 G-1/G-2/G-3/G-5/G-7 与 §4 段分配表再生成 | 备选：靠 code review 保证 → 放弃：22 文件 × 166 符号 + 111 改写点不可人工穷举 | §4.8、§6 |
| D-8 | band 下沿（`constants`/`schema_v1`/`steering`/`trajectory`）与上限（`completion`/`working_context`/`context_source_validation`）保留为登记例外，不为凑行数混装语义 | 备选：强行合并到区间内 → 放弃：违反"高内聚"这一需求本意，制造下一次拆分成本；备选 Variant B（见 §10）→ 未采纳为本基线，但作为需求方裁决的备选方案保留 | §4.7、§10 Q-2 |
| D-9 | `outcome` 与 `route_decision` 合并为 `outcome_route.py`；`_route_decision_bucket_exprs` 下沉 `constants.py` | 备选：拆两个模块 → 各 ≈180 行双双重 ▼；备选：片段留在上层模块 → schema@704 反向依赖上层，破坏 R-层 | §4.0、§4.5、§3.3 |

## 9. 对抗性质询裁定（challenger → 主架构师）

规则：每条判定必须给"成立/不成立 + 可核查证据"，不因提出者身份而自动采纳。

| 编号 | 质询 | 裁定 | 证据与处置 |
|---|---|---|---|
| BLOCKER-1 | 门面晚绑定会丢失 patch 语义（晚绑定不如直接 import 可靠） | **不成立（结论方向错），但暴露了真缺陷 → 已采纳其派生项** | 本地 `patch.object(module, name)` 最小实验：`facade 直接调用 → FAKE`、`晚绑定(state_db.attr) → FAKE`、`静态 global 名字副本 → REAL`。晚绑定本身可被拦截；真正失效路径是**同模块裸调用**（`init_db@1302`、`_open_shadow_read_connection@1463`、`save_task@1611/1617`）。处置：INV-BIND v2 + R-bare + G-2(a)，改写清单 111 处 |
| BLOCKER-2 | `660/661`、`985/986` 作为切片点会切进事务/`if` 内部 | **成立** | `BEGIN IMMEDIATE`@551 → `COMMIT`@907 为同一事务；`if not cur.fetchone():`@981 body 延伸至 1113、`else:`@1114-1115。处置：§4.1.1 白名单切点表，段界改为 362-366 / 367-550 / 551-907 / 908-978 / 979-1115 |
| MAJOR-3 | G-4 用文本规范化 diff 不足以证明 DDL 语义等价 | **成立** | 控制流（4 条建库路径）与状态标记（4 处 `_INITIALIZED_DBS.add`）无法由文本相等覆盖。处置：G-4 升级为运行时 executed-statement trace 等价，文本 diff 降为预筛 |
| MAJOR-4 | DDL 计数口径混淆（`_ensure_schema` 内 0 个 `ALTER TABLE`） | **成立** | 全文件 25 CREATE TABLE / 43 索引 / 12 ALTER；`_ensure_schema` 文本口径 20 / 41 / 0，12 个 ALTER 全在被调助手内。处置：F-3 双口径 + G-4/G-7 覆盖助手集合 |
| MAJOR-5 | `_validate_context_source_existence` 的三段抽取不可行 | **成立** | 16 个嵌套 def；`relevant_peer_tasks` 定义@5915 却被 ~5790 处闭包调用读取；`taskless_allowed_runs` 于 6098 写入；17 处 raise 集中 6054-6128。处置：D-5 整块搬迁 + §4.7 band 例外 + §10 Q-2 上报 |
| MAJOR-6 | 门面只按 152 建基线会漏常量 | **成立** | AST 顶层 `Assign`+`AnnAssign` = 14，合计 166 绑定；`herdr/execution_outcome.py:37`、`herdr/shadow_rows.py:318`、`herdr/state_store.py:76,97` 依赖模块级绑定。处置：G-1 基线 166 |
| MAJOR-7 | `_ensure_rollout_schema` 宿主重复（组 A 与 `rollout.py` 同时认领） | **成立** | §4.0 修正：唯一宿主 `schema_ddl_helpers.py`；`rollout.py` 下行静态导入；实测该二符号在 tests/生产侧**无外部引用**，静态导入安全 |
| MINOR-8 | `compileall -q bin` 通过是假信号 | **成立** | `bin/` 下 9 个无扩展名 Python 脚本（`bin/` 排除 `__pycache__` 后共 9 个文件，全部以 `#!/usr/bin/env python3` 起头），`python3 -m compileall -q bin` 收集到 0 个 `.py` 文件即返回成功。处置：验收命令改 `python3 -m py_compile bin/<每个脚本>`（实现计划 §2 验收块已加） |
| MINOR-9 | `_INITIALIZED_DBS.clear()` 重绑定会使别名失效 | **不成立** | 实测 8 处站点全为 `state_db._INITIALIZED_DBS.discard(...)`（原地 mutate），无 `.clear()`/重绑定。处置：D-6 保留别名方案，G-2(b) 用 `is` 断言防回归 |
| MINOR-10 | `tests/test_context_compact.py:30` 是 alias import 证据 | **不成立** | 该行是另一符号的 import，非 `state_db` 别名绑定。F-2 的 34 行 from-import 清单另行给出（9 生产文件 + 7 测试文件） |

### 9.1 承接单 requirements 节点质询的消费登记（R-00..R-28 → 本文/实现计划）

〔承接核对〕`wf-haflow-1020-01-req-chal` 的 28 条 review 是本文的**上游输入**，不是重复质询。逐项处置（成立=已在文档修正或转为实现段判据；不重复列证据）：

| R-ID | 上游裁定 | 本节点处置 | 落点 |
|---|---|---|---|
| R-01/R-26 | T1 门禁不在 main，候选 `de3d6a6` 已被 `a1614e5` 修正 | **采纳并裁决用 `a1614e5`**：该 commit 只改这 5 个文件（`git diff --name-only 6417047..a1614e5` 实测），且修的是 G-2(b)/G-2(c) 的假 BLOCK；逐文件 SHA 写进实现计划 §1 作 fail-closed preflight | 实现计划 §1 S0 |
| R-02 | G-5 只查顶层 import | 采纳 → GA-1 | 本文 §6.1 |
| R-03 | §3.3 只列 8 条边，实际 41 / 21 上行 | 采纳，已补 §3.3.1 全量 41 条边表（本工位 AST 复算 41/21 独立复现） | 本文 §3.3.1 |
| R-04 | T2 删除清单 43 枚举只有 42，漏 `_schema_initialization_lock` | 采纳，实现计划 §3 S1 补为 43 | 实现计划 §3 |
| R-05 | T2 INV-BIND 漏 `_get_credentials_dir` 与 connection→schema 晚绑定 | 采纳：3 处 `_get_credentials_dir` 站点（7986/8020/8053）与 `_facade()._ensure_schema` 均写入 S1 判据 | 本文 §2 F-1、§3.2、§3.3 |
| R-06 | `--report` 与 §4.0 三行不一致 | 采纳，constants/schema/outcome_route 三行已按脚本改（114/272/403），并声明"脚本为准" | 本文 §4.0 |
| R-07 | band 15+8=23 与 22 矛盾，实为 14+8 | 采纳，§4 结论行更正为 14 OK / 5 BELOW / 3 ABOVE | 本文 §4 |
| R-08 | 6 任务共写 `state_db.py`，5 路并发不成立；`max_concurrency=3` | 采纳，§7.1 作废并发派发形态 | 本文 §7.1 |
| R-09/R-10 | G-4 缺 legacy rollout bucket_key 路径；只比语句不比值 | 采纳 → GA-3/GA-4 | 本文 §6.1 |
| R-11 | `save_working_context` 6 处跨模块调用未列入重写 | 采纳，已给出 6 站点行号并入 T3-5（S6）配额 7 点 | 本文 §3.3.1、实现计划 §3 S6 |
| R-12 | discard 8→7、from-import 16→17 文件、patch 56→57 | 采纳，两处已更正并解释 8=7 tests + 1 内部 | 本文 §2 F-1/F-2 |
| R-13 | 756/667/289 是段长非函数体 | 采纳，§0 建立"段长 vs AST 体"双口径约定 | 本文 §0 |
| R-14 | `herdr/state/__init__.py` 未入重叠矩阵 | 采纳，注册表归属并入 S0/S1 与 T4 | 本文 §7.1 |
| R-15 | G-3 PENDING / G-2 WARN 不阻塞 OVERALL | 采纳 → GA-2（`--final` 固化为每段必跑） | 本文 §6.1 |
| R-16 | `compileall -q bin` 空跑假 PASS | 采纳并**实测确认**：`bin/` 恰有 9 个无扩展名 `#!/usr/bin/env python3` 脚本；验收改为逐个 `py_compile` | 实现计划 §3 各段 |
| R-17 | "可安全切片"的质疑不成立，整块搬迁结论成立 | 采纳，并把证据改强为"局部变量被前置嵌套函数读取"（5915 赋值 / 5790 读）；嵌套 def 16→15 | 本文 §4.3 |
| R-19/R-24 | 核心数字（8069/166/14/25-43-12/111+6/22/67/34）与 `_decode_task_row`、`state_db.time`、前向引用成立 | 本工位独立复算**逐项相同**，未发现漂移 | 本文 §0/§2 |
| R-20 | 必查项 a..j 部分成立 | 已按各项落点消费（GA-1..GA-5、§7.1、§9.1） | 全文 |
| R-21 | spawn 测试只证不报错；rollout 只读无专项 | 采纳，列为 S1 测试补强判据 | 本文 §6.1 末段 |
| R-22 | 15 个 conn 可选 API 中 3 个只读；67 正确 | 采纳并**独立复现 15/67 与逐函数计数**；只读三为 `get_trajectory_finding`/`get_observation`/`find_observation_by_dedup`，措辞改为"12 写 + 3 读" | 实现计划 §0 INV-4 |
| R-23 | 22 符号全存在；"每符号一条路径"≠全路径；`_get_credentials_dir` 漏项 | 采纳 → GA-5 | 本文 §6.1 |
| R-25 | 5 路并发 vs 单 Task 串行待裁决 | 已裁决：**单 Task 串行**（§7.1） | 本文 §7.1 |
| R-27 | Variant A 未经签字不得默认 | 已由需求方口径签字为 A，并要求"验收文字=300-500 目标 + 登记例外"（G-3 对未登记偏离 BLOCK） | 本文 §10 Q-2 |
| R-28 | 台账字段完整性自检 | 无冲突，plan 节点台账沿用同一七要素格式 | 台账 |

未被采纳即"仅小偏差、不改结论"的两项：R-13 的数字口径（已用双口径表述解决）、R-17 的 667→666（已在 §4.3 标注）。R-00 由本次搬运本身消解：文档此前**未落盘于本仓库**，现两文件 SHA256 与来源副本相同（`9418ae8c…` / `50fd28c8…`），本文即权威副本。

## 10. 交总指挥 / Controller 的未决问题

〔承接核对：三项均已由 `wf-haflow-1020-01` 需求方口径消解，消解结论以本工位 dispatch 的"已裁决口径"为准，并已落 `kind=decision` 台账；原文保留以便追溯。〕

- **Q-1 质询者派发授权缺口**（已消解）：本节点执行规则限定"最多 2 个 Task / 2 个 Pane（主架构师 + 对抗性质询者）"。对抗性质询者已在本 Pane 内以同进程角色完成（§9 全部裁定附证据），但**未新开第 2 个 Task/Pane**。若要求独立 Pane 的第三模型评审，请由 Controller 决定；计划阶段不擅自扩 Pane。〔承接核对：`wf-haflow-1020-01` 的 requirements 节点已开出独立 challenger 工位（`wf-haflow-1020-01-req-chal`，agent=opencode，产出 R-00..R-28），Q-1 的"缺第二模型评审"已被上游补齐；本 plan 节点据此只做**消费与修正**，不重复质询、不再开第 2 个 Pane（受 §7.1 与平台 D-2 约束）。〕
- **Q-2 300-500 band 与两个巨型函数的硬冲突**（已消解 → **Variant A**）：需求"每模块 300-500 行"与"`_ensure_schema`(756)/`_validate_context_source_existence`(667) 不改语义"不可能同时满足。本基线选择：有序切片 + 整块搬迁 + 8 条例外登记（§4.7）。**Variant B**（若需求方坚持零例外）：门面 + **8-10 个模块**，两个巨型函数保持原样不拆，band 改为"依职责 800 行以内"——该口径与仓库既有先例一致（`docs/specs/console-module-split.md` INV-STRUCTURE："依职责保持约 800 行以内，不机械拆小函数"；〔承接核对：该行存在，实测在 `docs/specs/console-module-split.md:12`，先例成立〕），代价是把 166 符号的模块归属放宽、模块数与需求"高内聚小模块"意图偏离。需需求方裁决取哪一个，本节点不自行改需求。〔承接核对：**需求方已裁决取 Variant A**——`_ensure_schema` 按白名单 5 段有序切片（362-366/367-550/551-907/908-978/979-1115），`_validate_context_source_existence` 与 `save_working_context` 整块搬迁不拆函数，偏离 300-500 的模块逐条登记例外（§4.7 共 8 条）。验收口径因此是"300-500 目标 + 登记例外"：G-3 对未登记偏离必须 BLOCK，对已登记 8 条放行；若需求方后续改为零例外，才切 Variant B 并重写 §4/§7 与实现计划 T2-T4。〕
- **Q-3 小模块合并**（已消解 → 维持 22 模块 + 登记例外）：`steering`(264)/`trajectory`(269)/`schema_v1`(266)/`constants`(137) 四处 ▼ 是否有业务上可接受的配对（而非机械凑行数）？本节点已实测 3 组候选均超 500 或跨语义层（§4.6），倾向保持现状；如需求方给出新配对约束，T2/T3-2/T3-3 可局部 rework，不影响其他域（Selective Replan 边界成立）。〔承接核对：需求方口径为"小模块不强配对"，维持 5 条 ▼ 登记（含 `schema.py` 297）。〔承接核对：数字口径 `constants` 现为 139，非 137。〕〕

## 11. 复算入口（本文任何数字必须可被下列命令再生成）

门禁脚本本体不在 main，S0 承接前用来源工位路径直跑（只读，不改被测文件）：

```bash
T1=/Users/user/.herdr-controller/clones/wf-haflow-1019-02-impl-t1
REPO=/Users/user/.herdr-controller/clones/wf-haflow-1020-01-plan-executor
python3 $T1/scripts/check_state_db_split.py --root $REPO --report --symbols   # §4.0 模块表 / 166 符号表
python3 $T1/scripts/check_state_db_split.py --root $REPO --inventory          # 111 + 6 改写点逐行清单
python3 $T1/scripts/check_state_db_split.py --root $REPO --snapshot-baseline  # G-1 的 166 绑定基线
python3 $T1/scripts/check_state_db_split.py --root $REPO --final              # GA-2：缺失模块/__all__ 升 BLOCK
```

不依赖 T1 的最小复算（本节结论的原始来源，`python3 -` 直喂）：顶层绑定数、段长/函数体双口径、DDL 双口径 25/43/12 与 20/41/0、`should_close` 15 函数 67 处、`time.*` 34、patch 面 57/22/27 文件、import 面 133/230 与符号式 34 行/17 文件（须含跨行 import 与 `bin/`、`scripts/` 无扩展名脚本）、41/21 跨模块边、`_INITIALIZED_DBS` 8 行点、`_validate_context_source_existence` 15 嵌套 def 与 17 raise、V1 尾巴写站点 1041/1045/1053/1063/1067/1079。实现期若上述任一数字漂移，先改文档再改代码，并以 `--report` 输出为准（§4.8）。


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
