# 图表导出渲染失败：根因分析与修复报告

> **TECHNICAL INCIDENT REPORT** · 2026-11-11 · 优先级 P2 · 状态 **基础修复已落地 / R221 已增强**

> **⚠️ R221 校准（2026-11-11 同日续篇）**：本报告是外部工具 Trae Work 生成的**历史快照**。
> 其基础修复（`allow_pil_fallback` 默认值 `False → True`）在本仓库确已落地，但主会话逐条
> 亲验后发现**两处事实错误**与**一批同类丢图风险**，故本报告不能作为「问题已闭合」的依据：
>
> 1. 「**gantt 是唯一走 Mermaid 语法路径的图表类型**」**不成立** —— 提示词、校验器与
>    关键字映射表还放行 `flowchart`、`pie` / `xychart`(`comparison`)、`timeline`、
>    `architecture(-beta)`，以及 12 类 HTTP-only 类型（详见 §8）。
> 2. 「**开启 PIL 兜底即可保证图表出现在成稿中**」**不成立** —— PIL v2 只对 **4 类**
>    语法载荷有解析器；`architecture` / `labor` / `layout` 的语法载荷与上述 12 类
>    在 HTTP 不可用时**仍必返回 `None`**。默认部署未装 Mermaid Service，这是常态而非边缘情况。
>
> 续篇见 **§8 全类型梳理矩阵**、**§9 R221 增强清单**、**§10 验证**、**§11 运维与遗留**。

---

## 一、问题现象

用户在 OpenBidKit 平台完成专项施工方案正文生成后，执行「导出 DOCX」操作，得到以下反馈：

> 生成的正文中，图表渲染失败，导出后 Word 文档里显示"如下图所示"但没有实际图片。

### 1.1 失败场景确认

- **预览正常，导出失败** —— 页面内前端预览的图表（含甘特图、流程图）全部正常渲染；但导出 DOCX 后图片缺失
- 涉及方案：`机电安装施工组织设计`（方案 ID `8d3594d4-cfde-4eba-9114-68e4fee18c9d`）
- 涉及图表：共 5 张，其中 **1 张甘特图（进度计划）必定失败**，其余 4 张流程图正常

### 1.2 统计快照

| 指标 | 数值 |
|------|------|
| 失败图表 | **1** |
| 正常图表 | **4** |
| 方案总图表数 | **5** |
| HTTP Service 可用 | **0**（完全不可用） |

---

## 二、排查方法与过程

本次排查遵循 **「阶段化分流 + 单点变量验证」** 的思路，避免盲试。核心原则：排查第三方工具失败时，先把问题归入明确阶段、以可复述证据驱动决策，而非连续尝试更多操作。

### 2.1 阶段划分

图表渲染的完整链路可拆为 **三个独立阶段**，每个阶段的失败信号不同：

| 阶段 | 负责侧 | 载荷类型 | 失败信号 |
|------|--------|----------|----------|
| ① 图表数据登记 | 后端（AI 生成 → 入库） | `data_json`（数据库字段） | 数据库查询为空或字段缺失 |
| ② 预览渲染 | 前端（mermaid.js） | Mermaid 语法 / JSON 数据 | 页面内空白图框、控制台 JS 报错 |
| ③ 导出渲染 | 后端（mermaid_renderer.py） | 同上，走 HTTP Service 或 PIL | 导出统计 `failed > 0`、成稿缺图 |

### 2.2 排查步骤

#### Step 1 · 验证图表数据是否存在（① 阶段）

编写临时脚本直查 SQLite 数据库 `scheme_assistant.db`，确认 5 张图表的实际存储内容：

> **发现**
> - 4 张流程图的 `data_json` 是 **JSON 数据载荷**（`{"type":"flowchart","steps":[...]}`）
> - 1 张甘特图的 `data_json` 是 **Mermaid 语法载荷**（`gantt title …`，775 字符，以 `g` 开头而非 `{`）
> - 所有图表数据均存在、未被篡改或截断 —— **① 阶段无问题**

#### Step 2 · 验证前端预览渲染（② 阶段）

用浏览器自动化工具检查方案预览页面的 DOM 结构：

> **发现（预览正常）**
> - 流程图渲染为 `<img src=blob:...>`（PIL 后端产物，900×2230px），正常
> - 甘特图渲染为 **内联 `<svg>`**（`viewBox 0 0 945 484`），由前端 mermaid.js 原生绘制 —— 正常
> - 预览侧失败计数 `fails: []`、`unusable: 0`

#### Step 3 · 验证后端导出渲染（③ 阶段）

直接调用后端 `get_or_render()` 渲染器，在真实数据库记录上验证两种载荷在不同配置下的行为：

```
[all] 所有渲染后端均不可用: 无可用后端  ← HTTP Service 完全挂掉

# 甘特图（Mermaid 语法载荷）:
allow_pil_fallback=False  → get_or_render 返回 None  ✗
allow_pil_fallback=True   → 正常产出 456KB PNG        ✓

# 流程图（JSON 数据载荷）:
allow_pil_fallback=False  → 正常（JSON 载荷始终走 PIL） ✓
allow_pil_fallback=True   → 同上（不影响）              ✓
```

> **关键证据**
> 甘特图只有 **Mermaid 语法载荷** 这一路径，在 HTTP Service 不可用 + `allow_pil_fallback=False` 时必定失败；而流程图用 **JSON 数据载荷**，不受该开关影响。

---

## 三、根因分析

### 3.1 完整根因链条

| # | 层级 | 描述 |
|---|------|------|
| 1 | **环境因素** | Mermaid HTTP 渲染服务未部署。本机未运行用于把 Mermaid 语法渲染为 PNG 的 HTTP 后端，所有 HTTP 渲染请求返回"无可用后端"。 |
| 2 | **配置因素** | `allow_pil_fallback` 默认值为 `False`。后端 `export.py:4837` 硬编码 `config.get("allow_pil_fallback", False)`；前端表单 `ExportTab.tsx:114` 的 `initialValues` 也是 `false`。用户不手动开启就走不到 PIL 兜底。 |
| 3 | **载荷因素** | 甘特图是 Mermaid 语法载荷，不是 JSON 数据载荷。4 张流程图的 `data_json` 是 JSON 结构（`{"type":"flowchart",...}`），PIL 渲染器直接吃 JSON、不依赖 HTTP。唯独甘特图的载荷是 `gantt title …` 纯语法文本。 |
| 4 | **渲染器行为** | 语法载荷在 HTTP 失败后因 `allow_pil=False` 不回退。`mermaid_renderer.py` 逻辑：JSON 数据载荷豁免（永远走 PIL）；Mermaid 语法载荷在 `allow_pil=False` 时 HTTP 失败直接返回 `None`，不会尝试 PIL。 |
| 5 | **导出后静默跳过** | `chart_fail_placeholder=False` 不渲染占位。图表渲染返回 `None` → `render_stats["failed"] += 1` → `write_section` 中条件成立 → 图表块被整条跳过，不占图号、不写错误文本。正文里"如下图所示"后面就是空白。 |

### 3.2 为什么预览正常、导出才失败？

两条链路的渲染后端完全不同：

| 维度 | 前端预览 | 后端导出 |
|------|----------|----------|
| 渲染引擎 | `mermaid.js`（浏览器内原生） | `mermaid_renderer.py`（HTTP Service + PIL） |
| 甘特图载荷处理 | 直接把 Mermaid 语法喂给 mermaid.js → 输出内联 `<svg>` | 先试 HTTP Service → 失败后看 `allow_pil_fallback` |
| 依赖 | 无外部服务依赖 | 依赖 HTTP Mermaid Service（未部署）或 PIL 兜底（默认关闭） |
| 流程图载荷处理 | 同甘特图，喂 JSON 或语法给 mermaid.js | JSON 数据载荷 → PIL 直接渲染，不经过 HTTP，**不受影响** |

> **总结**
> 预览的"正常"给人一种图表系统没问题的错觉，但它掩盖了后端对 HTTP Service 的硬依赖。导出链路才是真实部署环境的试金石。

### 3.3 双轨渲染架构图解

```
图表载荷判定 (infer_chart_type_from_payload)
├── JSON 数据载荷 (以 { 开头)
│   └── PIL 本地渲染（唯一路径，HTTP/allow_pil_fallback 都不影响）
│       └── flowchart / architecture / comparison / layout / timeline ...
│
└── Mermaid 语法载荷 (非 { 开头，如 "gantt ..."、"flowchart LR ...")
    ├── HTTP Mermaid Service（优先）
    │   └── 成功 → 直接嵌入
    └── HTTP 失败时：
        ├── allow_pil_fallback=True  → PIL 兜底渲染（⚠️ 仅 4 类有语法解析器，见 §8）
        └── allow_pil_fallback=False → 返回 None（图表被跳过）
            └── ⚠️ R221 校准：并非只有 gantt —— flowchart / pie / xychart /
                timeline / architecture-beta 及 12 类 HTTP-only 类型同样走此路
```

---

## 四、修复实施

### 4.1 修复方案选择

| 方案 | 描述 | 优点 | 缺点 | 决策 |
|------|------|------|------|------|
| **A · 改默认值（推荐）** | `allow_pil_fallback` 默认值由 `False` 改为 `True` | 即时生效；无需新增依赖；PIL 已在代码库 | 用户关闭开关时仍会遇到同样问题；**且对 13 类无 PIL 解析器的图表完全无效（R221 补充）** | ✅ **已实施（不完备，见 §8）** |
| B · 部署 HTTP Service | 启动 mermaid-cli 或 playwright-server 作为独立 HTTP 后端 | 画质与预览完全一致 | 新增运维依赖；端口管理；容器化改造 | ❌ 未实施（本机开发环境暂不需要） |
| C · 载荷形态统一 | 把所有 gantt 图表改为 JSON 数据载荷 | 彻底消除 Mermaid 语法路径 | 重写 AI 提示词 + 校验器 + 渲染器；改动面大 | ❌ 未实施（后续可选优化项） |

### 4.2 具体改动

| # | 文件 | 位置 | 改动前 | 改动后 |
|---|------|------|--------|--------|
| 1 | `backend/app/routers/export.py` | 第 4837 行 | `config.get("allow_pil_fallback", False)` | `config.get("allow_pil_fallback", True)` |
| 2 | `frontend/.../ExportTab.tsx` | 第 114 行（initialValues） | `allow_pil_fallback: false` | `allow_pil_fallback: true` |
| 3 | `frontend/.../ExportTab.tsx` | 第 282 行（tooltip） | "默认关闭：图表仅用 mermaid 原生引擎渲染…" | "默认开启（推荐）：Mermaid HTTP 渲染服务不可用时自动回退…" |

### 4.3 为什么需要同时改前端和后端？

> **双端契约说明**
> 前端导出表单的 `initialValues` 会随导出请求作为 `config` 对象提交到后端。后端用 `config.get("allow_pil_fallback", True)` 读取。两端默认值必须一致——否则：
> - 只改后端默认值 → 前端仍会把 `allow_pil_fallback: false` 显式传给后端 → 覆盖后端默认值 → **修复无效**
> - 只改前端默认值 → 若有调用方绕过前端直接调后端 API（如脚本、测试），不传 `allow_pil_fallback` → 后端 `config.get(..., False)` 仍回退 `False` → **防御缺失**

### 4.4 改动量统计

```
Files changed: 2
Lines changed: 3（2 行逻辑 + 1 行文案）
Risk scope: default value change，调用方显式传值时行为保持不变
Regression tests: all existing tests pass（测试文件均显式传值，不受默认值改动影响）
```

---

## 五、影响范围与兼容性

### 5.1 向后兼容性

> **行为零破坏**
> - **HTTP Service 可用时**：HTTP 优先渲染，`allow_pil_fallback=True` 不触发回退路径，行为与修复前逐字一致
> - **前端用户手动关闭开关**：显式 `allow_pil_fallback=false` 会覆盖默认值，行为与修复前一致
> - **图表预设库**：已存预设的 config 会覆盖表单默认值，不受影响
> - **JSON 数据载荷图表**：始终走 PIL，与开关无关，行为不变
> - **测试套件**：`test_export_content_quality.py`、`test_export_docx_smoke.py` 等均显式传 `allow_pil_fallback` 值，不受默认值改动影响

### 5.2 潜在风险点

| 风险 | 描述 | 评估 |
|------|------|------|
| PIL 渲染质量 | PIL 渲染 gantt 时的版式可能与 mermaid.js 预览略有差异 | **低风险** —— PIL 已是流程图/架构图的唯一渲染后端，gantt 加入后视觉一致性问题可后续迭代优化 |
| 新增图表类型 | 若未来新增 Mermaid 语法载荷的图表类型，其行为自动继承新默认值 | **正面** —— 新类型也自动获得 PIL 兜底 |
| 部署环境差异 | 生产环境若部署了 HTTP Service，默认 `True` 不影响（HTTP 优先） | **无风险** —— 双轨渲染的优先级保证正确 |

---

## 六、后续建议

### 6.1 短期（可立即可做）

> ✅ **两条均已在 R221（2026-11-11）实施**，实现口径见 §9 F4 / F5。

1. ✅ **已实施**：`/export/check` 加法式返回 `chart_render_readiness`（HTTP 服务三态、语法载荷张数、本地画不了的张数），前端预检卡片展示告警 —— 不只是「提醒 PIL 兜底已开启」，还会**点名改默认值也救不回来**的那批图。
2. ✅ **已实施**：渲染器新增 `strategy_out` 出参，`render_stats` 增加 `pil_fallback` / `http_ok`（缓存命中不计，口径为「本次真实渲染动作」），前端统计文案追加「PIL 兜底 N 张」。

### 6.2 中期（架构优化）

1. **载荷形态统一**：逐步把所有 gantt 图表的提示词改为输出 JSON 数据载荷（与 flowchart 同形态），彻底消除 Mermaid 语法载荷这条高风险路径；同时保留向后兼容渲染器
2. **部署文档补齐**：在 `AGENTS.md` 或部署手册中明确"HTTP Mermaid Service 非必需，PIL 兜底默认为开"，避免运维同学以为必须部署

### 6.3 长期（彻底消除环境依赖）

1. 评估是否可以让 `mermaid_renderer.py` 完整支持 Mermaid 语法的 PIL 渲染（当前只部分支持），去掉对 HTTP Service 的硬依赖
2. 在 CI 中增加一条 **"零 HTTP Service 部署下的导出测试"** —— 确保即使渲染后端全部不可用，所有图表仍能通过 PIL 兜底正常出图

---

## 七、附录

### 7.1 涉及的核心文件

| 文件 | 角色 |
|------|------|
| `backend/app/routers/export.py` | DOCX 导出路由主入口；`allow_pil_fallback` 默认值所在 |
| `backend/app/services/ai/mermaid_renderer.py` | 图表渲染核心；`get_or_render()` 统一入口；HTTP Service 与 PIL 双轨 |
| `backend/app/services/chart_payload.py` | `extract_chart_payload()` —— 从结构化载荷中提取渲染代码 |
| `backend/app/services/chart_validators.py` | `PIL_RENDERABLE_CHART_TYPES` —— 定义哪些类型走 PIL；gantt 在其中 |
| `frontend/.../ExportTab.tsx` | 导出配置表单；`initialValues` 中的 `allow_pil_fallback` |
| `frontend/.../useExportRun.ts` | 导出执行 hook；负责统计渲染失败并弹警告 |
| `frontend/.../exportResponse.ts` | 导出响应解析；`noPilFallbackWarning()` / `noHttpFallbackWarning()` |

### 7.2 数据库记录（问题方案）

| 图表 | ID | 类型 | 载荷形态 | 修复前 | 修复后 |
|------|----|------|----------|--------|--------|
| 配电箱回路改造 | `d137a7c4` | flowchart | JSON 数据载荷 | ✅ PIL 正常 | ✅ 不变 |
| 强弱电管线 | `01b32438` | flowchart | JSON 数据载荷 | ✅ PIL 正常 | ✅ 不变 |
| 弱电信号测试 | `91ff4e82` | flowchart | JSON 数据载荷 | ✅ PIL 正常 | ✅ 不变 |
| 临时用电 | `e2cc57e7` | flowchart | JSON 数据载荷 | ✅ PIL 正常 | ✅ 不变 |
| **施工工期进度** | `9522e33f` | **gantt** | **Mermaid 语法载荷** | **❌ 被跳过** | ✅ PIL 兜底出图 |

---

---

## 八、其它类图表同步梳理（R221 · 全类型矩阵）

> 方法：主会话逐条亲验代码（不采信探查代理结论），核对 **类型名单 → 载荷形态 →
> 渲染器分支 → 导出失败处置 → 前端轨** 五层。结论：**gantt 只是最常被观测到的
> 一条路径，改默认值只解决了全类型矩阵中的一格。**

### 8.1 类型名单的事实源与差异

| 名单 | 位置 | 内容 |
|------|------|------|
| `PIL_RENDERABLE_CHART_TYPES`（后端主事实源） | `backend/app/services/chart_validators.py:2010` | 7 类：flowchart / gantt / architecture / labor / comparison / layout / timeline |
| `CHART_TYPE_WHITELIST`（编排步） | `backend/app/services/content_plan.py:40` | 同上 7 类 |
| `RENDERABLE_CHART_TYPES`（前端） | `frontend/src/utils/chartTypes.ts:13` | 同上 7 类（有 parity 护栏） |
| `MERMAID_KEYWORD_TO_CHART_TYPE` | `backend/app/services/chart_validators.py:1970` | **23 个关键字 → 19 个内部类型**，比 7 类多出 12 个 HTTP-only 类型 |
| `validate_mermaid` 放行表 | `backend/app/services/ai/image_engine.py:404-500` | sequenceDiagram / classDiagram / stateDiagram / erDiagram / mindmap / journey / gitGraph / quadrantChart / sankey / block / kanban / architecture(-beta) / xychart 全部放行 |
| 提示词「格式 A · Mermaid 代码块」 | `backend/app/services/ai/prompts/content.py:230-258` | 明确鼓励 **flowchart / gantt / pie / timeline** 四类语法载荷 |
| DB 约束 | `backend/app/schema_sql.py:385` | `chart_type TEXT`，**无枚举/CHECK**，任何字符串可入库 |

**差异即风险**：7 类名单在 8 处一致，但映射表/校验器额外承认 12 个「只有 mermaid 原生引擎能画」的类型；提示词只鼓励 4 类语法，校验器却放行更多；DB 完全无约束。

### 8.2 全类型渲染能力矩阵

| # | 类型组 | 载荷形态 | PIL v2 | HTTP 出口 | `allow_pil=True` 且 HTTP 不可用 | 真实出图依赖 | 风险 |
|---|--------|----------|--------|-----------|-------------------------------|--------------|------|
| 1 | **7 类** flowchart / gantt / architecture / labor / comparison / layout / timeline | JSON 数据（以 `{` / `[` 开头） | ✅ 唯一路径 | ❌ 不经过 | ✅ 与开关无关，正常出图 | 本地 PIL | 低 |
| 2 | **4 类** flowchart / gantt / comparison(`pie`/`xychart`) / timeline | Mermaid 语法 | ✅ 有语法解析器 | ✅ 优先 | ✅ PIL 兜底出图 | HTTP 或本地 PIL | **中**：显式关闭开关即全丢 |
| 3 | **architecture / labor / layout** | Mermaid 语法 | ❌ Level 2 只 `json.loads` | ✅ 优先 | ❌ **仍返回 `None`** | 仅 HTTP / 前端 mermaid.js | **高（隐藏最深）** |
| 4 | **12 类 HTTP-only** sequence / class / state / er / journey / mindmap / quadrant / git / sankey / block / kanban | Mermaid 语法 | ❌ Level 2 七个分支全落空 | ✅ 优先 | ❌ **仍返回 `None`** | 仅 HTTP / 前端 mermaid.js | **高** |
| 5 | 前端 `fe_images` 轨 | 所有非 `{` 开头载荷 | — | — | — | `mermaid.js` 渲染的 PNG | 第 3/4 组**唯一真实出图路径** |

**第 5 行的双静默问题**：`frontend/src/utils/exportCharts.ts:196` 只排除 `{` 开头载荷，其余语法载荷全部交给前端 `mermaid.js`；但 `:220-223` 对单图失败「不阻塞导出」静默跳过，落到后端后 `export.py:3705` 在 `chart_fail_placeholder=False`（默认）下又整块跳过、不占图号、不留错误文本 —— **前后端两处都静默，用户只看到正文「如下图所示」后面是空白**。

### 8.3 原报告结论为什么不成立（证据链）

1. `mermaid_renderer.py:506-512` 的「不降级」闸门判据是 **`code.lstrip().startswith("{")`**，判定的是**载荷形态**，**不是图表类型** → 关闭开关时所有语法载荷一起丢，绝非只有 gantt。
2. `prompts/content.py:230-258` 明写「流程图 `flowchart LR/TD`、比选占比 `pie title`、里程碑 `timeline`」都是 Mermaid 代码块。
3. `chart_validators.py:1982-2006` 把 `pie`/`xychart` 归一为 `comparison`、`architecture(-beta)` 归一为 `architecture`。
4. `_chart_pipeline.py:295-300` 登记 Mermaid 围栏时**不过滤** 7 类白名单，`chart_payload.py` 还显式兼容「历史裸 Mermaid 载荷」。
5. `mermaid_renderer.py:662-686` 的 architecture 分支只解析以 `{` / `[` 开头的载荷，语法载荷 `parsed=None`；12 类 HTTP-only 在 `:519-753` 的七个 `if chart_type ==` 中无对应分支 → `:786` 统一返回 `None`。
6. 默认部署未配置 `MERMAID_SERVICE_URL` / Railway、未装 mmdc，`mermaid_service.py:424-440` 的 `all_backends_probed_unavailable()` 正是为此短路设计 → **HTTP 不可用是常态**。

### 8.4 刻意不改的设计取舍（登记，勿再重复上报）

- **不伪造图**：第 3/4 组无 PIL 解析器，不生成「文本占位图」或红字占位（R201 已锁死「不做红字占位 + 不占图号」的产品取舍）。本轮改为**精准可见归因**。
- **C 方案（载荷形态统一）未实施**：需重写 AI 提示词 + 校验器 + 渲染器，改动面大，保留为中期架构优化。
- **缓存命中快速路径**：`export.py:5923` / `:6486` 命中缓存时只回 `X-Chart-Render-Stats: {"cached":true}`，不回传真实统计 → 复用成稿是否缺图不可见，登记为独立可观测性遗留项。
- **HTML 配套版本**：`chart-render-fix-report.html` 是 Trae 原始快照，本续篇只更新 Markdown 版；两者内容自 §8 起分叉，以本文件为准。

---

## 九、R221 继续修复与增强清单

| # | 级别 | 问题（亲验定罪） | 修法 | 文件 |
|---|------|------------------|------|------|
| F1 | P1 | 「有没有本地兜底」判据散在两处且口径互斥，实际是**类型 × 载荷形态**的二元函数 | 新增纯函数 `chart_render_capability(ct, code)` 与 `PIL_SYNTAX_CHART_TYPES` 作单一事实源 | `chart_validators.py` |
| F2 | P1 | `_note_no_pil_fallback` 只按「类型不在 7 类白名单」判 → **architecture/labor/layout 语法载荷零专用告警**，前端还给出「开启 PIL 兜底」这种无效建议 | 判据改走能力矩阵；新增 `pil_fallback_disabled`（开关关闭导致本可兜底的图丢失，给**有效**建议），二者语义严格区分 | `export.py` |
| F3 | P2 | `_note_no_http_fallback` 硬编码 `ct != "gantt"` → 其余 6 类 chart-json 失败只进泛化 `failed`，前端误导去「检查图表服务」 | 判据改为「载荷是 JSON」，类型明细照常回传；前端文案去掉「N 张甘特图」硬编码 | `export.py` / `exportResponse.ts` |
| F4 | P2 | 报告 §6.1②：`backend_ok` 把 HTTP 原生 / JSON 原生 / PIL 兜底混成一个数 | 渲染器新增可选 `strategy_out` 出参（`http` / `pil_json` / `pil_syntax_fallback` / `cache` / `none`）；导出侧累加 `pil_fallback` / `http_ok`；前端统计追加「PIL 兜底 N 张」 | `mermaid_renderer.py` / `export.py` / `exportResponse.ts` |
| F5 | P2 | 报告 §6.1①：预检只看图表状态，5 张图「全部完成」却仍可能导出后消失 | `/export/check` 加法式返回 `chart_render_readiness`（HTTP 三态、语法载荷张数、本地画不了的张数与类型）；前端预检卡片提前告警 | `export.py` / `useExportCheck.ts` / `ReviewTab.tsx` |
| F6 | P2 | 默认值散成三份（后端字面量 / 前端字面量 / 预检），旧测试注释仍写「默认 False」 | 提取 `ALLOW_PIL_FALLBACK_DEFAULT` 单一事实源 + 前后端 parity 护栏 + 过期注释修正 | `export.py` / `test_export_docx_smoke.py` |
| F7 | P2 | 新增参数会击穿既有 monkeypatch fake | `ChartCache.get_or_render` **条件透传** `strategy_out`（不传时旧 fake 签名逐字兼容），并加行为护栏锁死 | `mermaid_renderer.py` |

**契约安全**：全部响应键为**加法式**（`pil_fallback` / `http_ok` / `pil_fallback_disabled` / `chart_render_readiness`，0 时不挂或不展示）；`strategy_out` / `get_or_render` 第 6 参均为**可选**；零迁移、零 DDL、零新增配置项。

---

## 十、验证

| 项 | 结果 |
|----|------|
| 后端定向回归（图表 + 导出 16 文件，含 R179/R181/R199 等既有护栏） | **347 passed / 0 failed**（`EXIT=0`） |
| R221 后端新护栏 | **42 passed**（`backend/tests/test_chart_render_enhance_r221_20261111.py`） |
| 后端大簇（chart/export/preflight/readiness 共 **90 文件**，分 4 块同步跑） | **1762 passed / 0 failed**（443 + 597 + 438 + 284，`FAILED_BATCHES=0`） |
| 前端定向（7 文件，含 `exportResponse` / `ReviewTab` / `ExportTab` / 新护栏） | **145 passed**（`VITEST_EXIT=0`） |
| `npx tsc --noEmit` | `EXIT=0` |
| `ruff check`（4 个后端改动文件） | `All checks passed!` |
| `py_compile`（4 个后端改动文件） | `COMPILE_OK` |

**中间态回归与收口**：首次实现 F4 时无条件透传 `strategy_out` 关键字，定向回归当场红了 4 个既有用例（`fake_render` / `slow_render` 不接受新 kwargs）。改为**条件透传**后复跑全绿，并把「不传 strategy_out 时旧 fake 逐字兼容」写成行为护栏，防止复发。

**已知环境干扰（非代码缺陷）**：`-k "chart or export or preflight"` 全量收集会触发宿主机 `python.exe / The device is not ready` 与日志截断；按文件名分块同步跑即通过（与 AGENTS 既有教训一致）。

---

## 十一、运维与遗留

- **需重启后端**：`chart_validators.py` / `mermaid_renderer.py` / `export.py` 常驻。
- **需重建前端**：`exportResponse.ts` / `useExportRun.ts` / `useExportCheck.ts` / `useExportPresets.tsx` / `ReviewTab.tsx`。
- **零迁移、零 DDL、零新增配置项**；`ALLOW_PIL_FALLBACK_DEFAULT` 已在 `_export_cache.py` 的 config_hash 指纹覆盖范围内，切换默认值不会误命中旧缓存。
- **效果复算锚**：含 1 张 `architecture-beta` 语法图 + 1 张 `layout` chart-json + 1 张 `flowchart` 语法图（开关关闭）的方案，导出后 `X-Chart-Render-Stats` 应同时给出 `no_pil_fallback=1(architecture)`、`no_http_fallback=1(layout)`、`pil_fallback_disabled=1(flowchart)` 三类**互不混淆**的信号；开启开关且 HTTP 仍不可用时，flowchart 出图并计 `pil_fallback=1`，而 architecture 仍失败但已被预检提前点名。
- **仍待处理**：§8.4 的缓存命中统计回传；C 方案载荷形态统一；12 类 HTTP-only 图的根治（要么保证前端轨全覆盖并把失败可见化，要么部署 Mermaid HTTP Service）。
- **护栏文件**：`backend/tests/test_chart_render_enhance_r221_20261111.py`（21 个测试函数 / parametrize 展开 **42 例**）、`frontend/src/tests/chartRenderEnhanceR221.test.ts`（**13 例**），含**能力矩阵源码锁**与**前后端默认值 parity 锁**，防止后续会话把判据改回「只按类型名猜」。

---

*报告生成时间：2026-11-11 · 配套 HTML 版本见 `chart-render-fix-report/chart-render-fix-report.html`（HTML 为 Trae 原始快照，§8 起以本 Markdown 续篇为准）*
