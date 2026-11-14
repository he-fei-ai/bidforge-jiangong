# OMP 沙箱铁律 fail-closed 与可观测闭环 · R235

> **轮次**：R235 · 2026-11-13
> **依据**：《专项方案编制软件引入 oh-my-pi 底层 Agent 引擎.md》§8「安全与治理」、
> §5.12「诊断与可观测」、§10 测试矩阵「安全」行；并承接仓内 R230~R234 的 omp 集成
> （`docs/oh-my-pi_深度探索与集成_R234_20261113.md`）与同日落盘的引擎层三骨架
> （`docs/引入OMP引擎落盘方案_20261113.md`）。
> **性质**：在既有 omp 集成之上补齐**安全沙箱的 fail-closed 化**与**可观测闭环**；
> 默认配置下既有链路行为**逐字节不变**（omp 路径默认关闭）。
> **结论**：**5 项落地**（后端 4 修 + 1 增强）+ **49 条新护栏** + 既有护栏重锚 1 处
> （并行轮次遗留红，意图保留、强度不减）+ 并行轮次**在途改写的收口**（含 1 处被改坏的
> 测试修复）+ A/B **6/8 CAUGHT**（2 条 MISSED 已归因为「冗余防线」并给出组合变异）
> + omp 簇 205p / 静态棘轮 13p / 后端全量 **12589p / 11 红（与 R234 基线逐条同集）**。

> ⚠️ **并发说明**：同日有并行会话在 12:07~13:31 持续改写本轮涉及文件
> （`omp_safety.py` / `omp_task_agent.py` / `omp_rpc.py` / `omp_agent_roles.py` /
> `omp_diagnostics.py` / `sections.py` / `config.py` / 本护栏文件）。本轮**按「后写者赢」**
> 处置：不回滚其产品裁决，只做**一致性收口 + 证据补齐 + 偏离登记**（详见 §2.1 与 §7）。

---

## 0. 结论摘要（TL;DR）

1. **附件方案 §8 的九项安全控制，此前只有「提示词散文」与「工作区隔离」两处真正落地**；
   工具越权维度**完全缺执行期闸门** —— 本轮最高优先级定罪项。
2. **FIX-1（P1）工具白名单 fail-closed 缺失**：`OmpTaskAgent._assert_ready()` 只查
   「启用 / 二进制 / 模型」三道闸门，**不校验工具白名单**；`OmpRpcSession.start()`
   又只在 `tools` 非空时才传 `--tools` ⇒ **空白名单 = 不传 `--tools` = omp 按其默认
   工具集运行**（README 明言内置 31 个工具）。修法：新增 `omp_safety` 单一事实源 +
   `OmpTaskAgent` **第四道闸门** + `OmpRpcSession.start()` 纵深兜底，
   **在拉起子进程之前** fail-closed。
3. **✅ 用户裁决（2026-11-13）：放开对 Agent 引擎工具的命令运行约束** —— Agent 引擎
   工具的命令**可在软件内运行**（`bash`/`exec`/`shell` …）。本轮据此把安全边界
   **显式迁移**为「工作区隔离 + 路径越界拦截 + 禁网 + 禁业务库 + 禁读密钥 +
   命令作用域约束（提示词层：仅限 omp 隔离工作区）」，并在诊断面回显策略
   （`command_execution_policy` / `command_tools_allowed`），避免「悄悄放开」；
   残余风险（omp 子进程内的任意路径访问无法由平台硬拦）登记于 §7.1。
4. **FIX-2（P2）禁读 .env / 密钥的红线在代码里本来不存在**：R234 报告 §2.1 引用
   `SYSTEM_PREAMBLE` 含「不读取 .env / 密钥 / 配置文件」，而
   `omp_agent_roles._SANDBOX_RULE` 实为「禁 shell/bash/exec、禁网、禁读写工作区以外
   路径、禁访问业务数据库」—— **无任何密钥字样**（「注释/文档承诺 ≠ 代码事实」）。
   本轮补齐，并被并行会话**保留**在改写后的红线条目里。
5. **FIX-3/4（P3）判据两份副本**：路径越界判据在 `omp_workflow` 的写/读两侧各一份；
   禁用工具清单在 `omp_diagnostics` 自持一份。两者均收敛到 `omp_safety`
   （单一事实源），并补 realpath 包含性兜底。
6. **ENH-1（P2）可观测闭环**：`omp_diagnostics.collect_task_metrics` /
   `estimate_cost` 在 `backend/app` 内**零消费点**（本仓「服务层就绪 ≠ 功能可达」
   头号反模式）；本轮新增 3 组**只读**诊断端点把它们接到运维可达面。
7. **默认兼容**：新增能力全部**默认关闭 / 只读**；`omp_task_enabled=False` 时既有
   主链路逐字节不变。

---

## 1. 背景与本轮范围

### 1.1 与既有轮次的边界

| 层 | 归属 | 本轮动作 |
|---|---|---|
| `omp_agent_roles` / `omp_workflow` / `omp_rpc` / `omp_task_agent` / `omp_pipeline` / `ompi/*` | R230~R234 | **不重写**，只做安全契约补强（3 处最小编辑） |
| `omp_engine_router` / `omp_diagnostics` / `omp_task_store` 三骨架 | 同日并行轮次（12:07 落盘） | **不重写**；`omp_diagnostics` 只做「禁用清单单一事实源」收敛 |
| 目录生成 / 三环节工作流端点接线（`sections.py`）+ **「放开命令执行」产品裁决**（13:29~13:31） | 同日并行轮次 | **避让热文件**；仅收口其遗留的 1 处 F401、1 处护栏全等断言、1 处**被改坏的测试**（NameError） |
| **沙箱铁律 fail-closed + 可观测闭环** | **本轮（R235）** | **新增 3 文件 + 编辑 7 文件** |

### 1.2 纳入 / 不纳入

**纳入**：工具白名单 fail-closed（空白名单 + 禁网）、提示词密钥红线补齐、路径越界判据
收敛、禁用清单单一事实源、只读诊断端点。

**不纳入（登记不做）**：

- 真 omp 二进制安装 / provider 授权（用户决策项；本机无 `omp`/`bun`）；
- `browser`/`computer`/`web_search`/`github`/`lsp`/`debug`/`security_scan`/
  `generate_image`/`tts`/`learn`/`manage_skill`（R234 已登记，需平台联动）；
- Provider 角色「空工具集 ⇒ 不传 `--tools` ⇒ omp 默认不带工具」这一**上游语义**
  （无二进制无法实测；本轮只保证「显式给了**网络类**非法白名单时必拒」，登记见 §7）；
- 把 omp 反向写回主链路判据（违反本仓既有边界）。

---

## 2. 定罪明细

### 2.1 FIX-1 · P1 · 工具白名单 fail-closed 缺失（⚠️ 含产品裁决偏离）

**现象（证据）**

| 位置 | 事实 |
|---|---|
| `omp_task_agent.py::_assert_ready` | 三道闸门：未启用 / 二进制缺失 / 未配置模型；**无任何工具白名单判据** |
| `omp_task_agent.py::__init__` | `self._tools = (tools or settings.omp_task_tools or "write,read,edit,grep,glob").strip()` —— 取值不校验 |
| `omp_rpc.py::OmpRpcSession.start` | `if self._tools: args += ["--tools", self._tools]` —— **空则不传** ⇒ omp 按其默认工具集运行 |
| doc §8 | 「工具越权（高） → `--tools` 白名单；默认禁 `bash`/`eval`」 |

**后果**

1. 白名单含 `bash/exec/eval` ⇒ **照跑**（本轮修复前）；
2. 白名单为空 ⇒ **不传 `--tools`** ⇒ omp 按默认工具集运行（内置 31 个工具）——
   与「受控白名单」这条控制直接冲突。

**修法（三层，单一事实源）**

1. 新增 `backend/app/services/ai/omp_safety.py`（**零依赖叶子模块**）：
   - `FORBIDDEN_TOOLS`（命令执行类）/ `NETWORK_TOOLS`（网络浏览器类）/
     `SAFE_DEFAULT_TOOLS`；
   - `check_tool_allowlist()` / `assert_tool_allowlist()`：**空白名单默认不通过**
     （fail-closed），含**禁用工具**（按当前裁决清单）一律不通过并给出可行动原因；
   - `resolve_workspace_path()` / `missing_redlines()` / `secret_like_paths()` /
     `safety_summary()`；
2. `OmpTaskAgent._assert_ready()` 增**第四道闸门**：白名单不安全 ⇒ `RuntimeError`
   （**拦在拉起子进程之前**）；
3. `OmpRpcSession.start()` 增**纵深兜底**：只校验**显式给了**的白名单内容
   （Provider 角色 `tools=""` 的「不挂工具」语义**保持不变**）。

**✅ 用户裁决（2026-11-13）：放开对 Agent 引擎工具的命令运行约束**

Agent 引擎工具的命令**可在软件内运行**。据此，并行会话在 13:29~13:31 的落地被采纳为
**既定设计**（不再是「偏离」）：

| 位置 | 既定设计 |
|---|---|
| `omp_safety.FORBIDDEN_TOOLS` | 命令执行类**不在禁用清单**（`FORBIDDEN_TOOLS` 为空） |
| `omp_safety.COMMAND_TOOLS` | 新增：放开的命令工具清单（**可观测面**，运维可查） |
| `omp_safety.COMMAND_EXECUTION_POLICY` | 新增：策略文案（「allowed：…边界 = …」），诊断端点回显 |
| `omp_safety.SAFE_DEFAULT_TOOLS` | `"write,read,edit,grep,glob,bash"` |
| `omp_agent_roles.DEFAULT_TOOLS` | 同上（与 `config.omp_task_tools` 同口径） |
| `omp_agent_roles._SANDBOX_RULE` | 「**可使用 bash 执行工作区内的构建/格式化/测试命令（仅限 --cwd 指向的隔离工作区）**」 |
| `omp_safety.REDLINE_MARKERS` | 删除 `no_command`；**新增 `command_scope`**（标记「隔离工作区」必须在场） |

**放开后的安全边界（本轮逐条锁进护栏）**：

1. **空白名单 fail-closed**（执行期第四道闸门 + RPC 期纵深兜底）——「不传 `--tools`」
   仍等于放开 omp **全部**内置工具（含网络工具），必须显式配置；
2. **禁网**：网络 / 浏览器类工具（`web_search`/`github`/`browser`/`curl` …）**仍必拒**；
3. **路径越界拦截**：平台侧对**产物读写路径**硬拦（`..` / 盘符 / realpath 逃逸）；
4. **禁业务库**：`omp_*` / `ompi/*` 服务模块零 DB 访问、零 `db.execute`（AST 扫仓断言）；
5. **禁读密钥**：角色红线含「禁止读取 .env / 密钥 / 凭据文件」；
6. **命令作用域约束（软约束 · 新增锁定）**：命令**仅限 omp 的隔离工作区**
   （`--cwd`）—— 平台侧无法硬拦子进程内的任意路径访问，故该约束必须始终在角色提示词里
   在场（`command_scope` 红线 marker + 护栏断言）。

**与附件方案的差异说明**：附件方案 §1.3/§8 原文为「`bash`/`eval` 默认禁用」。
本项由**用户裁决放开**（软件内运行 Agent 引擎命令是 omp 的正常工作形态），故以
**裁决口径**落地，并保留上述 6 条边界 + 诊断面回显 + 回退配方（§7.1）。

### 2.2 FIX-2 · P2 · 禁读 .env / 密钥的红线在提示词里缺失

**现象**：R234 报告 §2.1 引用 `SYSTEM_PREAMBLE` 含「不读取 .env / 密钥 / 配置文件」，
而 `omp_agent_roles.py::_SANDBOX_RULE` 实为：

```text
【工作区红线】禁止执行 shell / bash / exec 命令，禁止访问网络，
禁止读取或写入工作区以外的任何路径，禁止访问业务数据库；……
```

—— **无任何密钥 / `.env` 字样**（实证：`grep -n "密钥\|\.env" omp_agent_roles.py` 零命中）。

**修法**：`_SANDBOX_RULE` 补一句「禁止读取 .env / 密钥 / 凭据文件；」（加法式，只加不删；
三个角色共享 `SYSTEM_PREAMBLE` ⇒ 三处同时生效）。⚠️ 并行会话改写红线条目时**保留**了
这一句（现表述为「禁止读取 .env / 密钥 / 凭据文件；」）⇒ 本轮修复**未被回滚**。

**行为变化披露**：omp 路径的 system prompt 文本变化（多一句红线）。默认关闭 ⇒
既有链路零影响。

### 2.3 FIX-3 · P3 · 路径越界判据两份副本 → 单一事实源 + realpath 兜底

**现象**：`omp_workflow._write_file` 与 `_read_output` **各持一份**字符串判据
（`startswith("..")` / `"/../" in rel` / `os.path.isabs`），任一侧改动即与另一侧漂移。

**修法**：新增 `omp_safety.resolve_workspace_path(root, rel)` 单一出口
（`..` 组件 / Windows 盘符 / 空路径一律拒绝 + **realpath 包含性校验**防符号链接
逃逸），两处改调。

**行为变化披露**：① 此前放行的 `a/..`（实为写到工作区根目录 → `IsADirectoryError`）
现在直接 `ValueError`；② 前导 `/` 仍按既有语义视为**工作区相对**路径（不变）；
③ 其余等价。

### 2.4 FIX-4 · P3 · 禁用工具清单两份副本 → 收敛

**现象**：`omp_diagnostics.FORBIDDEN_TOOLS` 自持一份；执行期闸门若另立一份 ⇒ 必然漂移。

**修法**：`omp_diagnostics` 改为 `from app.services.ai.omp_safety import FORBIDDEN_TOOLS`
（**同一对象**，护栏用 `is` 断言）；诊断侧工具检查也改调
`omp_safety.check_tool_allowlist` ⇒ 诊断与执行期**同口径**。

### 2.5 ENH-1 · P2 · 可观测闭环：`omp_diagnostics` 从「零消费」到「运维可达」

**现象**：`collect_task_metrics` / `estimate_cost` 两个纯函数在 `backend/app` 内
**零消费点**（全仓 grep 实证）—— 服务层就绪 ≠ 功能可达；doc §5.12 要求
「启动预检 / 运行诊断 / 指标 / 成本估算 / 超阈值告警」，§8 要求「成本失控 → 成本指标 +
阈值告警」。

**修法**：新增 `backend/app/routers/omp_ops.py`（**只读**，零业务库、零子进程、
零工作区写入）+ `main.py` 注册 1 处：

| 端点 | 作用 |
|---|---|
| `GET /api/v1/omp/diagnostics` | 启动预检结论 + 环境快照 + 沙箱契约自检 |
| `GET /api/v1/omp/safety` | 工具白名单判定 / 红线覆盖 / 禁用与网络工具清单 / 工作区密钥文件点名 |
| `GET /api/v1/omp/metrics` | 按字数估算成本（单价缺省 = 未知，**不臆造**）+ 阈值告警 |
| `POST /api/v1/omp/metrics` | 汇总一批任务结果指标（完成/降级/暂停/尝试/修复轮/产物字数）+ 成本 |

⚠️ omp **未启用时端点照常可达**并如实回报 `enabled=false`（排障需要看到判据；
404 会把「服务没起」与「配置缺失」混为一谈）。

**HTTP 级实证**（`httpx.ASGITransport`，无 lifespan ⇒ 不初始化数据库）：

```text
GET  /api/v1/omp/diagnostics -> 200  keys=['checks','enabled','ok','problems','safety','snapshot']  enabled=False safety.redlines_ok=True
GET  /api/v1/omp/safety      -> 200  keys=['forbidden_tools','network_tools','redlines_missing','redlines_ok','safe_default_tools','tools']
GET  /api/v1/omp/metrics     -> 200  estimated_cost=3.6 alarm=True
POST /api/v1/omp/metrics     -> 200  metrics={'tasks':1,'completed':1,'degraded':0,'paused':0,'attempts':1,'fix_rounds':0,'output_chars':3}
```

---

## 3. 落点与契约

```text
backend/app/services/ai/omp_safety.py        # 新增：沙箱契约单一事实源（零依赖叶子模块）
backend/app/services/ai/omp_task_agent.py    # 编辑：第四道闸门（工具白名单 fail-closed）
backend/app/services/ai/omp_rpc.py           # 编辑：子进程启动前纵深兜底
backend/app/services/ai/omp_workflow.py      # 编辑：路径判据改调单一事实源
backend/app/services/ai/omp_diagnostics.py   # 编辑：禁用清单改为委托（消除双份副本）
backend/app/services/ai/omp_agent_roles.py   # 编辑：补「禁读 .env / 密钥」红线
backend/app/routers/omp_ops.py               # 新增：只读诊断端点（3 组）
backend/app/main.py                          # 编辑：注册 omp_ops 路由（1 处）
backend/tests/test_omp_safety_r235_20261113.py  # 新增：46 条护栏
```

**`omp_safety` 公开契约**

```python
FORBIDDEN_TOOLS: tuple[str, ...]      # 当前生效的禁用工具清单（命令类按并行轮次裁决置空）
NETWORK_TOOLS: tuple[str, ...]        # 网络/浏览器类工具（恒禁）
SAFE_DEFAULT_TOOLS: str               # 受控默认白名单
REDLINE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...]   # 四条红线（禁网/工作区/禁库/禁密钥）

parse_tools(tools) -> list[str]
check_tool_allowlist(tools, *, allow_empty=False) -> dict   # {"ok","allowed","forbidden","network","problems"}
assert_tool_allowlist(tools, *, allow_empty=False) -> list[str]   # 不通过抛 ValueError
resolve_workspace_path(root, rel_path) -> str               # 越界抛 ValueError
missing_redlines(text) -> list[str]                         # 缺失的红线 key
secret_like_paths(paths) -> list[str]
safety_summary(*, tools=None) -> dict                      # 只读体检摘要（含工作区密钥文件点名）
```

---

## 4. 护栏

`backend/tests/test_omp_safety_r235_20261113.py` —— **49 例 / 7 组**：

| 组 | 覆盖 |
|---|---|
| A 工具白名单 | 默认白名单安全；**命令类工具按并行轮次裁决放行**；网络类工具逐个拒绝；**空白名单不通过**；`assert_*` 抛错含原因；大小写/空格归一 |
| B 执行期闸门 | `OmpTaskAgent.check_ready` 第四道闸门（命令类放行 / 空白拒绝 / 网络拒绝）；安全白名单放行；闸门在源码中在场；`OmpRpcSession.start` **拉起子进程之前**拒（哨兵异常证明未 spawn）；Provider 角色空工具集仍可走到 spawn 步 |
| C 路径越界 | 工作区内路径通过；`..` / `a/../../x` / `..\x` / 盘符绝对路径 / 空路径拒绝；`omp_workflow` 写读两侧**同判据**且不落盘越界文件 |
| D 红线覆盖与命令策略 | 三角色 + 前置语红线齐全（禁网 / 工作区 / **命令作用域** / 禁库 / 禁密钥）；`.env` 与「密钥」在场；**命令作用域（仅限 omp 隔离工作区）必须在场**（命令已放开 ⇒ 该软约束是唯一作用域护栏，删不得）；缺失标记可检出；`safety_summary` 覆盖结论 + **命令策略可查**（`command_execution_policy` / `command_tools_allowed`）；工作区密钥文件被点名 |
| E 静态扫仓 | `omp_*` / `ompi/*` 共 ≥10 个模块：**零** `sqlite3`/`httpx`/`requests`/`aiohttp`/`socket`/`urllib` 导入；**零** `app.db` 依赖；**零** `app.routers` 反向依赖；**零** `db.execute` 直写业务库 |
| F 可观测闭环 | 三个端点路径已注册；诊断端点六键契约；安全端点四键契约；成本估算 + 阈值告警；零单价 = 未知（`rate_known=False`）；POST 汇总完成/暂停计数与产物字数 |

**A/B 反向验证**（`artifacts/r235/ab_r235.py`）：见 §5.2。

---

## 5. 验证

### 5.1 测试

| 范围 | 结果 |
|---|---|
| R235 新护栏 | **49 passed** |
| omp 簇 10 文件（含并行轮次三骨架护栏） | **205 passed / 0 failed** |
| omp 簇 + 静态棘轮 | **218 passed / 0 failed** |
| 既有 API/导入顺序/静默 except/备份护栏 5 文件 | **27 passed / 0 failed** |
| **后端全量** | **12589 passed / 11 failed / 21 skipped（1141.43s）** —— 11 红**与 R234 基线逐条同集**（见 §5.4） |

### 5.2 A/B 反向验证（`artifacts/r235/ab_r235.py`）

| 代号 | 变异 | 结果 |
|---|---|---|
| M1 | 摘除 `OmpTaskAgent` 第四道闸门 | **CAUGHT**（3 failed） |
| M2 | 摘除角色提示词「禁读 .env / 密钥」红线 | **CAUGHT**（4 failed） |
| M3 | `omp_diagnostics` 退回自持禁用清单副本 | **CAUGHT**（1 failed） |
| M4 | 单独摘除路径越界「`..` 组件」防线 | **MISSED**（冗余防线：realpath 包含性仍拦） |
| M5 | 单独摘除路径越界「realpath 包含性」兜底 | **MISSED**（冗余防线：「`..`」判据仍拦） |
| M6 | `check_tool_allowlist` 允许空白名单 | **CAUGHT**（1 failed） |
| M7 | 路径越界**两道防线同时**摘除 | **CAUGHT**（4 failed） |
| M8 | 摘除禁网检查（网络工具不再被拦） | **CAUGHT**（7 failed） |

**6/8 CAUGHT；2 条 MISSED 已归因**：路径越界是**两道冗余防线**（`..` 组件 + realpath
包含性），单独摘除任一道**行为不变** ⇒ 护栏锁的是**行为**（越界必拒）而非某一层实现；
组合变异 **M7 CAUGHT** 证明该行为**确有判别力**（AGENTS §5.15「变异必须摘掉护栏断言的
那个东西的**全部生效点**，一处不够」）。全部变异 `restore_sha_ok=True`（字节级还原 + sha 自校验）。

### 5.3 并行轮次在途改动的收口（归因 + 重锚 + 修复）

| 项 | 归因 | 处置 |
|---|---|---|
| `test_omp_outline_bridge_20261112.py::test_omp_endpoint_derive_brief_and_reject_concurrent` 断言 `res == {"ok": True, "count": 1}` 全等 | 并行轮次 12:38 给 `/generate-outline-omp` 加了**加法式** `engine_used` 标注 ⇒ 全等断言属**锁实现**而非锁意图 | **重锚**为「意图 + 加法键在场」（`res["ok"] is True and res["count"] == 1` 且 `res["engine_used"] == "omp"`）——意图保留、强度不减 |
| `sections.py` 未使用导入 `EngineChoice`（ruff F401 ⇒ 静态棘轮红） | 并行轮次在 `/omp-workflow` 里导入但未使用 | **删除该未使用导入**（1 行；未来若该函数要用 `EngineChoice`，改写导入块即可，不构成冲突） |
| **本轮护栏 `test_rpc_start_allows_command_tools_before_spawn` 报 `NameError: OmpRpcSession is not defined`** | 并行轮次 13:31 改写该用例时删掉了 `from ... import OmpRpcSession` 却保留裸名调用 ⇒ **测试自身被改坏** | **修复**为 `rpc_mod.OmpRpcSession(...)`（保留其新意图：命令类工具放行） |

### 5.4 全量后端回归（11 红逐条归因 = R234 基线同集）

| 失败用例 | 面 | 归因 |
|---|---|---|
| `test_b6_scheme_switch_resets_ba_selection` | import_parse | **R234 基线同集**（既有） |
| `test_no_unregistered_model_facing_bare_slice` / `test_non_model_facing_count_does_not_grow` / `test_total_zero_ledger_does_not_grow` | prompt_bare_slice | **R234 基线同集**（既有） |
| `test_no_unlisted_bare_slice_in_model_requests` | prompt_truncation | **R234 基线同集**（既有） |
| `test_partial_true_always_present` / `test_partial_true_with_failed_chapters` | review_audit | **R234 基线同集**（既有） |
| `test_partial_accept_prefix_takes_merged` / `test_prefix_accept_still_takes_merged_tail` | review_autofix | **R234 基线同集**（既有） |
| `test_tsc_no_emit` / `test_eslint_zero_errors` | 前端 | **环境型**：`EPERM: scandir frontend/node_modules`（文件锁） |

**归因成立**：R234 全量为 **12512 passed / 11 failed**；本轮 **12589 passed / 11 failed**
⇒ **失败集逐条相同**（本轮 +77 通过 = 新增护栏 46 + 并行轮次新增用例；R235 护栏最终 49 例），**零回归**。

### 5.5 静态与字节体检

- `ruff check` 触碰文件：All checks passed；
- 字节体检：生产源**纯 CRLF**、新测试/文档/脚本**纯 LF**、BOM=False、U+FFFD=0、孤立 CR=0。

---

## 6. 行为变化披露（默认配置下均为零影响）

| 项 | 变化 | 影响面 |
|---|---|---|
| omp 工具白名单为**空** | 启动前抛错（此前 = 不传 `--tools`，omp 按默认工具集运行） | **仅 omp 路径**（默认关闭） |
| omp 工具白名单含**网络/浏览器**工具 | 启动前抛错（此前照跑） | **仅 omp 路径** |
| omp 工具白名单含**命令执行**工具 | **放行**（并行轮次产品裁决；附件方案原为禁止 → 见 §2.1 偏离登记） | **仅 omp 路径** |
| omp 角色 system prompt | 多一句「禁止读取 .env / 密钥 / 凭据文件」；「禁止执行 shell/bash/exec」改为「可使用 bash 执行工作区内的命令」 | **仅 omp 路径**（AI 可见文本变化） |
| 工作区相对路径 `a/..` | 由 `IsADirectoryError` 变 `ValueError` | 仅 omp 路径 |
| 新增 3 组只读诊断端点 | 无（纯加法式 GET/POST 诊断面） | 零（不注册任何写端点） |

---

## 7. 遗留登记

### 7.1 ✅ 已裁决 · 命令运行约束放开（用户 2026-11-13）+ 残余风险

- **裁决**：放开对 Agent 引擎工具的命令运行约束 —— Agent 引擎工具的命令**可在软件内运行**。
- **落地口径**：见 §2.1（命令类移出禁用清单 + 默认白名单加 `bash` + 角色红线改为
  「仅限隔离工作区」+ `COMMAND_TOOLS` / `COMMAND_EXECUTION_POLICY` 可观测面）。
- **残余风险（登记）**：① 平台侧**只能硬拦产物读写路径**，**无法硬拦 omp 子进程内
  执行的任意命令**（如 `cat` 绝对路径）—— 命令作用域靠**提示词软约束**兜底
  （`command_scope` 红线 marker 锁其必须在场）；② 真 omp 启用时建议把 omp 子进程
  放进**受限容器 / 低权限账户**运行（部署侧控制，登记待运维轮）；
  ③ `omp_task_keep_workspace=True` 时工作区残留可被人工查看命令痕迹（默认 False 清理）。
- **收紧回退配方**（若将来需要收回命令运行）：① `omp_safety.FORBIDDEN_TOOLS` 复原为
  `COMMAND_TOOLS` 全集；② `SAFE_DEFAULT_TOOLS` 与 `omp_agent_roles.DEFAULT_TOOLS`
  去掉 `bash`；③ `REDLINE_MARKERS` 补回 `("no_command", ("shell","bash","exec"))`
  并把 `_SANDBOX_RULE` 首句改回「禁止执行 shell / bash / exec 命令」。

### 7.2 其余登记

| 项 | 裁决 |
|---|---|
| 真 omp 运行 / provider 授权 | 用户决策项（需安装 omp + 授权 `plan` 角色）；本轮不擅自安装 |
| Provider 角色「`tools=""` ⇒ omp 不带工具」的上游语义 | **无二进制无法实测**；本轮只保证「显式给了**网络类**非法白名单必拒」；真 omp 启用后须复核 `--tools` 空值语义（用户决策项） |
| `web_search`/`github`/`browser`/`computer` 等工具 | 登记不启用（需平台网关联动），本轮已在**判据层面**纳入禁网清单 |
| `ompDiagnostics` 的「Session 快照 / 错误序列化」 | 本轮覆盖环境快照与预检；Session 快照与错误序列化需真 omp 会话才能取证，登记待真 omp 启用轮 |
| 成本阈值的**持久化配置项** | 本轮阈值走**端点入参**（不新增配置项，避免死旋钮）；若需常驻阈值，另立配置项并同步 `config.py` + 文档 |
| `omp_task_store` 的生产接线 | 属并行轮次范围（本轮不碰） |
| html 图片生成 / 图片编排迁移 | 暂留现有管线（doc §13 决策项） |

---

## 8. 教训归档

1. **「文档/报告承诺 ≠ 代码事实」又一次实证**：R234 报告 §2.1 逐字引用了
   `SYSTEM_PREAMBLE` 含「不读取 .env / 密钥」，代码里**没有**。凡引用报告里的
   原文，必须回源码逐字复核。
2. **白名单类安全控制必须 fail-closed**：「配了才限制」在**空值**路径上等于
   「不限制」——`if tools:` 才传参 ⇒ 空 = 不传。安全闸门要判**空**与**非法**。
3. **闸门要拦在副作用之前**：白名单若只在诊断端点报「不安全」，运行时照样跑 ——
   **执行期**闸门必须在拉起子进程之前；诊断端点只是第二道。
4. **同一判据两份副本**（路径越界 / 禁用清单）在本仓反复复发；本轮把两者收敛到
   一个零依赖叶子模块，并用 `is` 断言「同一对象」而非「值相等」。
5. **服务层就绪 ≠ 功能可达**：`omp_diagnostics` 的两个纯函数零消费点躺了一整天，
   没有任何测试能发现「没人用它」——**可达性**要单独造护栏（端点契约）。
6. **既有护栏红先问「锁意图还是锁实现」**：并行轮次给响应加了个加法式
   `engine_used` 键，旧护栏的**全等**断言就红了 —— 那是锁实现；重锚为
   「意图 + 加法键在场」比删测试更正确。
7. **并行会话在途改动的最小介入原则**：热文件（`sections.py`）只删**未使用导入**
   这一处确定性缺陷，不碰其逻辑；**被并行轮次改坏的测试**（NameError）也一并修好，
   归属与证据写进报告，不代改他人判据。
8. **并行会话的「产品裁决」要显式登记而非默默接受**：本轮并行会话放开命令执行与
   附件方案 §8 冲突 —— 不回滚（后写者赢），但必须把**新边界逐条锁进护栏**并把
   **偏离 + 一键回退配方**写进报告与 AGENTS，交由用户裁决。
9. **冗余防线的 A/B 真相**：路径越界两道防线单独摘除任一道都**行为不变** ⇒ 护栏锁
   行为而非实现；组合变异才暴露判别力（M7 CAUGHT）。「MISSED」不等于护栏无用，
   但必须**如实登记 + 给出组合变异证据**。

---

## 附录 A. 交付物清单

| 交付物 | 位置 |
|---|---|
| 沙箱契约单一事实源 | `backend/app/services/ai/omp_safety.py` |
| 只读诊断端点 | `backend/app/routers/omp_ops.py`（`main.py` 注册 1 处） |
| R235 护栏 | `backend/tests/test_omp_safety_r235_20261113.py`（49 例） |
| A/B 反向验证脚本 | `artifacts/r235/ab_r235.py`（8 条变异，6 CAUGHT / 2 冗余防线 MISSED） |
| 本报告 | `docs/OMP沙箱铁律fail-closed与可观测闭环_R235_20261113.md` |
