# R176 性能基准工具

只新增文件，不修改产品源码。所有脚本不启动服务、不写生产库；端到端脚本只请求
**已经由人工启动**的本地服务，并且默认拒绝在并发会话/脏工作树中运行。

```
backend/tools/perf/
├── bench_micro.py          # 纯函数微基准（无 DB / 无网络）
├── bench_e2e.py            # 端到端分段基准（带并发守卫）
├── dataset/
│   ├── generate_dataset.py # S/M/L 确定性数据集生成器
│   ├── .gitignore          # 忽略 _generated/ 生成物
│   └── __init__.py
└── README.md
```

## 1. 数据集

```powershell
python backend/tools/perf/dataset/generate_dataset.py --tier all
```

| 档位 | 章节 | 全局事实 | 源文档 | 图表 |
| --- | --- | --- | --- | --- |
| S | 30 | 100 | 3 | 4 |
| M | 120 | 400 | 10 | 12 |
| L | 300 | 1200 | 30 | 30 |

- 同一 `--tier` + `--seed`（默认 `20261104`）生成内容逐字节一致；`manifest.json`
  记录每个文件的 sha256，可用于跨机器核对数据集是否同一份。
- 生成物写入 `dataset/_generated/`，已被同目录 `.gitignore` 忽略，不提交。
- 正文刻意包含 GFM 表格、mermaid 围栏、公式、占位符与口语化表达，使审核/解析
  微基准覆盖真实分支，而不是空跑正则。

## 2. 纯函数微基准

```powershell
python backend/tools/perf/bench_micro.py
python backend/tools/perf/bench_micro.py --sections 30,120,300 --repeat 9
python backend/tools/perf/bench_micro.py --only audit_content,content_fingerprint --json
```

覆盖项：

| bench | 被测函数 | 语义 |
| --- | --- | --- |
| `audit_content` | `_export_issue_scan.audit_content` | 导出前正文审核（全章节正则扫描） |
| `content_fingerprint` | `_export_cache._content_fingerprint` | 缓存指纹（JSON + md5） |
| `render_facts_text` | `facts_builder._render_facts_text` | 事实 → 提示词文本（预算分配） |
| `parse_content_blocks` | `content_blocks._parse_content_blocks` | 单节 Markdown 块解析 |
| `sanitize_ai_content` | `content_polish.sanitize_ai_content` | 单节正文清洗 |
| `dedup_continuation` | `sse_utils._dedup_continuation` | 续写去重 |

输出列：

- `best_ms` / `median` / `p95`：单次 op 的墙钟毫秒（内部自适应批量，摊薄计时噪声）。
- `ms/unit`：按单位摊薄。`audit_content`、`content_fingerprint` 为 `ms/section`；
  `render_facts_text` 为 `ms/fact`；其余为单节/单 op。
- 单函数单档累计测量受 `--budget`（默认 6s，硬上限 30s）保护。

测量方法约定：先预热 2 次；每档至少 `--repeat` 个样本；取 median 作为主口径，
best 用于判断下限，P95 用于判断抖动。跨会话对比时**只比较 median/P95 相对变化**，
不要拿单次 best 做结论。

## 3. 端到端分段基准

```powershell
# 只打印计划（不请求、不连库；仍会做只读守卫检查）
python backend/tools/perf/bench_e2e.py --dry-run --tier M

# 安静窗口（工作树干净、无并发 pytest/uvicorn）才允许真实运行
python backend/tools/perf/bench_e2e.py `
  --base-url http://127.0.0.1:8000 --scheme-id <scheme_id> `
  --tier M --repeat 5 --cases V1,V2,V6,V7,V8,V10
```

守卫规则（硬要求）：

- 检测到命令行含 `pytest` / `uvicorn` / `app.main:app` / `vitest` / `managed` /
  `run_perf` 的进程，**或** `git status --porcelain` 非空 → 退出码 2，拒绝启动。
- `--force` 可绕过，但结果不可用于正式基线（脚本会在结果 JSON 中保留
  `base_url/tier/scheme_id` 以便追溯）。
- 缺少 `--scheme-id` → 退出码 3；缺少数据集 → 退出码 3。

### 指标口径

| 指标 | 定义 | 说明 |
| --- | --- | --- |
| sections/s | `section_done` 事件数 / 墙钟秒 | 目录与正文生成均可用；正文另记首章延迟 |
| chars/s | 完成正文总字符 / 墙钟秒 | 需从 `section_done` 的 data 汇总；脚本给出事件计数与占位说明 |
| 首 token 延迟 | 首个 SSE `data:` 到达时间 | **后端正文非流式**，此值只反映任务启动/排队，不是模型首 token；正文场景应看"首章完成延迟" |
| 导出 页/s | 输出页数 / 导出墙钟秒 | 页数由产物解析或人工登记，脚本只记 ms/次 |
| 冷/热缓存 | 同一指纹连续导出两次 | 冷 = 首次或清缓存后；热 = 紧随其后 |
| 事实 段/s | 事实 SSE 事件 / 墙钟秒 | 事件口径可能因版本变化，需在报告中注明 |
| 审核 规则/s | 扫描章节数 × 规则数 / 墙钟秒 | 由 `/compliance/check`、`consistency-audit` 的 ms/次与规则数换算 |
| API P50/P95/P99 | 单向延迟分位 | 样本数 <20 时 P99 不稳定，仅作参考 |

### V1–V10 覆盖

| ID | 场景 | 自动 | 前置条件 |
| --- | --- | --- | --- |
| V1 | 导出 DOCX 冷缓存 | 是 | `scheme_id`，建议先清缓存 |
| V2 | 导出 DOCX 热缓存 | 是 | 紧接 V1 |
| V3 | AI 并发退档 | 否 | mock provider / 代理注入 429 |
| V4 | DB 池探活影响 | 否 | 切换探活开关各跑一轮 V1/V2 |
| V5 | 上传期间事件循环尖刺 | 否 | `--upload-url` + 样本文档 |
| V6 | 正文生成吞吐 | 是 | 已保存目录的 `scheme_id` |
| V7 | 事实抽取吞吐 | 是 | 已上传资料 |
| V8 | 审核/一致性扫描 | 是 | `scheme_id` |
| V9 | 前端首屏/操作延迟 | 否 | Chrome DevTools Performance trace |
| V10 | 目录生成未保存 + checkpoint | 是 | `scheme_id`，不调用 save-outline |

V3/V4/V5/V9 需要人工前置条件。脚本会明确打印 SKIP 原因，**不会伪造数字**。

## 4. 如何解读

1. **先看 P95，再看 median**：P95 反映排队/锁竞争；median 反映典型成本。
2. **纯函数微基准只回答"这段计算本身多贵"**：它不代表端到端墙钟，端到端受
   AI 延迟、DB IO、导出渲染支配。
3. **冷热缓存必须成对测**：只报热缓存会把 `_prepare_export` 的全量审核成本藏起来。
4. **跨会话对比冻结工作树**：同一 commit/工作树、同一机器、同一数据集 seed；
   否则数字不可比。
5. **正文"首 token"不可达**：当前后端按章整段返回（非 token 流），应改用
   "首章完成延迟"与"章均完成间隔"作为体验指标。
