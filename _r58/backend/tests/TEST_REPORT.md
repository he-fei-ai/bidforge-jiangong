# 单元测试报告

## 1. 概述

| 项目 | 值 |
|------|-----|
| 项目路径 | `E:\编程\专项方案工具箱\backend` |
| 测试框架 | pytest 8.3.3 + pytest-asyncio 1.4.0 |
| 覆盖率工具 | pytest-cov 7.1.0 |
| Python 版本 | 3.14.4 |
| 测试文件目录 | `backend/tests/` |
| 配置文件 | `backend/pytest.ini` |
| 公共 fixture | `backend/tests/conftest.py` |
| 测试用例总数 | **106** |
| 通过 | **106** |
| 失败 | **0** |
| 执行耗时 | ~6s |

---

## 2. 测试范围

为以下 5 项性能优化编写单元测试：

| 编号 | 目标文件 | 被测函数 | 优化点 | 测试文件 | 用例数 |
|------|----------|----------|--------|----------|--------|
| PB-2 | `sections.py` | `reorder_sections` | executemany 批量更新 sort_order | `test_sections.py` | 7 |
| PB-2 | `sections.py` | `delete_section` | 递归 CTE 一次性查后代 ID | `test_sections.py` | 7 |
| PB-4 | `provider_factory.py` | `_load_active_config` | 5 分钟 TTL 缓存 | `test_provider_factory.py` | 10 |
| PB-5 | `sse_utils.py` | `with_heartbeat` | Queue maxsize=100 背压 | `test_sse_utils.py` | 15 |
| - | `export.py` | `_parse_content_blocks` | markdown 格式解析 | `test_export.py` | 21 |
| - | `task_registry.py` | `register_task` / `update_progress` / `finish_task` 等 | 任务持久化 + SSE 广播 | `test_task_registry.py` | 46 |

---

## 3. 覆盖率分析

### 3.1 被测函数覆盖率（行覆盖）

| 模块 | 被测函数 | 行范围 | 覆盖率 |
|------|----------|--------|--------|
| `sse_utils.py` | `with_heartbeat` | 6-53 | **94%** (52-53 为 finally 中异常吞咽，无法触发) |
| `task_registry.py` | 全部公开函数 | 1-152 | **100%** |
| `sections.py` | `reorder_sections` | 237-247 | **100%** |
| `sections.py` | `delete_section` | 79-94 | **100%** |
| `provider_factory.py` | `_load_active_config` | 407-422 | **100%** |
| `export.py` | `_parse_content_blocks` | 69-141 | **100%** |

### 3.2 文件整体覆盖率

| 文件 | Stmts | Miss | Cover | 说明 |
|------|-------|------|-------|------|
| `sse_utils.py` | 36 | 2 | **94%** | ✅ 达标 |
| `task_registry.py` | 81 | 0 | **100%** | ✅ 达标 |
| `sections.py` | 161 | 116 | 28% | 被测函数 100%；未测函数为 `save_outline`/`export_tree` 等（不在本次范围） |
| `provider_factory.py` | 147 | 108 | 27% | 被测函数 100%；未测函数为 `_fallback_chain`/`get_working_provider` 等（不在本次范围） |
| `export.py` | 434 | 338 | 22% | 被测函数 100%；未测函数为 DOCX 生成相关（不在本次范围） |

---

## 4. 测试质量评估

### 4.1 断言稳健性

所有测试均包含**语义级断言**，验证被测代码的实际行为而非仅返回值：

- **DB 状态验证**：直接查询 SQLite 验证 `sort_order`、`status`、`progress` 等字段是否真正写入
- **缓存行为验证**：修改 DB 后验证缓存命中返回旧值、过期后返回新值（非仅检查非 None）
- **背压验证**：150/500 项数据全部到达且顺序正确，证明 Queue maxsize 不丢数据
- **级联删除验证**：构造 4 层树验证递归 CTE 删除所有后代，而非仅删父节点
- **跨 scheme 隔离**：验证 `AND scheme_id=?` 条件防止跨方案误更新
- **SSE 广播验证**：订阅者队列实际收到事件，验证 event 类型 + 字段值

### 4.2 测试设计亮点

1. **内存数据库隔离**：每个测试用 `aiosqlite.connect(":memory:")` 独立连接，零磁盘 IO，测试间互不干扰
2. **monkeypatch get_conn**：patch `app.db` 及 `provider_factory`/`task_registry` 中已绑定的 `get_conn` 引用，确保被测代码拿到测试连接
3. **autouse fixture 重置全局状态**：`_config_cache`、`_subscribers`、`_tasks` 在每个测试前自动清空，避免跨测试污染
4. **缓存 TTL 边界测试**：验证 `now - ts == TTL` 时视为过期（条件是 `< TTL` 而非 `<=`）
5. **None 不缓存行为**：发现并验证了 `_load_active_config` 的设计——None 不走缓存路径，每次查 DB，确保配置插入后立即可见

---

## 5. 发现的代码行为

测试过程中发现以下代码设计行为（非 bug，已通过测试固化）：

1. **`_load_active_config` None 不缓存**：缓存条件为 `data is not None and now - ts < TTL`，当无活跃配置时返回 None 但不缓存，每次都查 DB。这是合理设计——确保插入配置后立即可见，无需等 TTL 过期。

2. **`_parse_content_blocks` 有序列表要求空格**（✅ 已修复，见 v8）：正则 `(\d+)[.、)]\s+(.+)` 曾要求数字标记后必须有空格，`1、第一步`（无空格）不匹配为列表项而回退为段落。现对**中文枚举符**（`、` `）`）放宽为可无空格（`1、加强安全管理` / `3）混凝土强度` 均识别为列表项），ASCII 的 `.` / `)` 仍要求空格，以免 `3.14 是圆周率` 被误切为 `3. 14 是圆周率`。回归用例：`test_ordered_list_chinese_paren_no_space`、`test_ordered_list_chinese_round_paren_no_space`、`test_decimal_paragraph_stays_paragraph`、`test_ordered_marker_only_is_not_list`。

3. **`reorder_sections` 幽灵 ID**：order 中包含不存在的 ID 时，该条 UPDATE 影响 0 行，其余正常更新。幽灵 ID 占据 sort_order 序号但不报错。

4. **有序列表标记样式还原（v9 新增行为）**：`_parse_content_blocks` 为每个有序项记录 `marker`（`ascii` / `paren_ascii` / `num_dun` / `num_paren_r` / `num_paren_lr` / `cn_num_paren`），导出时按原标记样式渲染**重新计数后的连续序号**。判定规则：标记样式变化（如 `（一）` → `1、`）视为新序列从 1 重起；无序子项**不再**清零序号（`1. 顶层 / - 子项 / 2. 顶层` 保持 1 → 2）；非列表块仍重置序号。

5. **表格题注自动吸收（v9 新增行为）**：表格上方紧邻段落形如「表 X-Y 表名」（编号后必须有分隔符、无句读、≤ 40 字，且不以「中/所/如/为/内/里」开头）时，升格为表题并从正文块移除，导出为「表 {章号}-{序号} 表名」（居中加粗、位于表格上方、`keep_with_next` 防跨页拆分）。无表名行时不插入光秃秃的编号题注。回归用例见 `test_reference_sentence_not_taken_as_caption`、`test_cross_reference_without_separator_rejected`、`test_docx_table_caption_numbered_above_table`。

---

## 6. 文件清单

```
backend/
├── pytest.ini                          # pytest 配置（asyncio_mode=auto）
└── tests/
    ├── conftest.py                     # 公共 fixture（内存 DB + 全局状态重置）
    ├── test_sections.py                # reorder_sections + delete_section（14 用例）
    ├── test_provider_factory.py        # _load_active_config 缓存逻辑（10 用例）
    ├── test_sse_utils.py               # with_heartbeat Queue maxsize（15 用例）
    ├── test_export.py                  # _parse_content_blocks markdown 解析（21 用例）
    └── test_task_registry.py           # register/update/finish + 控制功能（46 用例）
```

---

## 7. 执行命令

```bash
# 运行全部测试
cd backend
python -m pytest tests/ -v

# 运行覆盖率分析
python -m pytest tests/ \
  --cov=app.routers.sections \
  --cov=app.services.ai.provider_factory \
  --cov=app.services.ai.sse_utils \
  --cov=app.routers.export \
  --cov=app.services.ai.task_registry \
  --cov-report=term-missing
```

---

## 8. 结论

- **106 个测试用例全部通过**，0 失败
- **所有被测函数行覆盖率 ≥ 94%**，达到 ≥80% 要求
- **断言均为语义级验证**（DB 状态、缓存行为、事件广播、数据完整性），能有效检测功能缺陷
- **测试隔离完备**：内存数据库 + 全局状态重置，无跨测试污染