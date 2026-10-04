# -*- coding: utf-8 -*-
"""正文同步图表链路单测：
- extract_inline_charts：内联 mermaid/chart-json 提取、%% 注释跳过、未闭合块截断（BUG 回归）、同类型去重
- has_inline_charts / _rewrite_code_block / _validate_inline_chart
- register_inline_charts：登记、mermaid 修复回写正文、修复失败删块、无内联不动作、不 commit（由调用方事务提交）
"""
import asyncio
import json

from app.routers._chart_pipeline import (
    _rewrite_code_block,
    _validate_inline_chart,
    extract_inline_charts,
    has_inline_charts,
    register_inline_charts,
)

# ---------- 工具 ----------

class FakeDb:
    """最小异步 db mock：记录 execute/commit 调用"""

    def __init__(self):
        self.rows = []
        self.commits = 0

    async def execute(self, sql, params=None):
        self.rows.append((sql, tuple(params or ())))
        # 真实 aiosqlite INSERT 成功时返回 Cursor（有 rowcount 属性），
        # 只有异常/事务冲突才返回 None。测试桩必须与真实语义一致，
        # 否则 F-3 修复（INSERT 返回 None → 视为失败降级 skipped）会让本文件
        # 的历史断言误判为「图块被删」。
        class _FakeCur:
            rowcount = 1
            lastrowid = 1
        return _FakeCur()

    async def commit(self):
        self.commits += 1


def run(coro):
    return asyncio.run(coro)


VALID_FLOWCHART = "flowchart TD\n    A --> B\n    B --> C"
VALID_SEQUENCE = "sequenceDiagram\n    A->>B: hi"


# ---------- extract_inline_charts ----------

def test_extract_mermaid_flowchart():
    content = ("本章方案总体流程如下：\n```mermaid\nflowchart TD\n    A --> B\n```\n其余正文。")
    charts = extract_inline_charts(content)
    assert len(charts) == 1
    assert charts[0][0] == "flowchart"
    assert "A --> B" in charts[0][1]


def test_extract_skips_comment_first_line():
    # AI 可能在 mermaid 首行输出 %% 注释 → 应跳过注释行再推断类型
    content = ("```mermaid\n%% 流程图\nflowchart LR\n    A --> B\n```")
    charts = extract_inline_charts(content)
    assert len(charts) == 1
    assert charts[0][0] == "flowchart"


def test_extract_chart_json_layout():
    content = ("总平面布置：\n```chart-json\n{\"type\": \"layout\", \"areas\": [{\"id\": \"a\", \"label\": \"x\", \"x\": 0, \"y\": 0, \"w\": 10, \"h\": 10}]}\n```")
    charts = extract_inline_charts(content)
    assert len(charts) == 1
    assert charts[0][0] == "layout"


def test_extract_chart_json_unknown_type_ignored():
    content = "```chart-json\n{\"type\": \"nonsense\", \"x\": 1}\n```"
    assert extract_inline_charts(content) == []


def test_extract_unclosed_block_truncated_then_next_block():
    # BUG 回归：未闭合的代码块会吞掉全部后续文本；截断保护（>500 行）后应继续扫描后续完整块
    content = (
        "```mermaid\nflowchart TD\n    A --> B\n"  # 未闭合围栏
        + ("正文继续……\n" * 600)  # 超过 _MAX_CODE_BLOCK_LINES 截断阈值
        + "```mermaid\nsequenceDiagram\n    A->>B: hi\n```"
    )
    charts = extract_inline_charts(content)
    assert len(charts) == 1  # 只提取到第二个完整块（第一个因未闭合被截断丢弃）
    assert charts[0][0] == "sequence"


def test_extract_same_type_dedup_keeps_first():
    content = ("```mermaid\nflowchart TD\n    A --> B\n```\n"
               "```mermaid\nflowchart LR\n    C --> D\n```")
    charts = extract_inline_charts(content)
    assert len(charts) == 1
    assert "A --> B" in charts[0][1]


def test_extract_empty_and_no_fence():
    assert extract_inline_charts("") == []
    assert extract_inline_charts("纯文本，没有图表") == []


def test_has_inline_charts():
    assert has_inline_charts("```mermaid\nflowchart TD\n```") is True
    assert has_inline_charts("```chart-json\n{}\n```") is True
    assert has_inline_charts("没有图表") is False
    assert has_inline_charts("") is False


def test_has_inline_charts_includes_ai_image():
    """```ai_image 同样是一等图表块（会登记 chart_predictions），不得漏判。

    BUG 回归：旧实现只认 mermaid / chart-json，仅含 AI 配图占位的章节
    会被判定为"无内联图表"，与登记/清单口径分叉。
    """
    content = '配图如下：\n```ai_image\n{"prompt": "剖面图"}\n```'
    assert has_inline_charts(content) is True


def test_has_inline_charts_unclosed_fence_not_counted():
    """口径收紧（2026-10-03 · R38）：未闭合围栏不算有图。

    「未闭合 = 不是图」在登记/导出/改写三侧收口后的第四处统一：
    eof/truncated 残片不产生任何图表登记、导出也不渲染，若本函数仍按
    「围栏存在」返回 True，未来被用于补生成粗判时会把坏块误计为已有图。
    """
    assert has_inline_charts("前文\n```mermaid\nflowchart TD\n    A --> B") is False
    assert has_inline_charts('```chart-json\n{"type": "gantt"') is False
    # 闭合对照：同样内容补齐收尾围栏即为 True（判据只取决于闭合态）
    assert has_inline_charts("前文\n```mermaid\nflowchart TD\n    A --> B\n```") is True


# ---------- _rewrite_code_block ----------

def test_rewrite_replaces_and_deletes():
    content = "前文\n```mermaid\nflowchart TD\n    A --> B\n```\n后文"
    replaced = _rewrite_code_block(content, "flowchart TD\n    A --> B", "flowchart LR\n    X --> Y")
    assert "```mermaid\nflowchart LR\n    X --> Y\n```" in replaced
    assert "A --> B" not in replaced
    deleted = _rewrite_code_block(content, "flowchart TD\n    A --> B", None)
    assert "mermaid" not in deleted
    assert "前文" in deleted and "后文" in deleted


def test_rewrite_no_match_unchanged():
    content = "```mermaid\nflowchart TD\n    A --> B\n```"
    assert _rewrite_code_block(content, "其他代码", "新代码") == content


# ---------- _validate_inline_chart ----------

def test_validate_mermaid_valid():
    ok, code = _validate_inline_chart("flowchart", VALID_FLOWCHART)
    assert ok is True
    assert code == VALID_FLOWCHART


def test_validate_mermaid_repair_success():
    # 中文标签含特殊字符但缺双引号 → 校验失败 → 正则修复（补引号）后通过
    bad = 'flowchart TD\n    A["准备"] --> B[开挖(第一层)]\n    B --> C["验收"]'
    ok, code = _validate_inline_chart("flowchart", bad)
    assert ok is True
    assert 'B["开挖(第一层)"]' in code
    assert validate_mermaid_again(code) is True


def test_mermaid_statement_semicolon_is_legal_and_not_repaired():
    """`;` 是 Mermaid 合法的语句分隔符（与换行等价）→ 直接放行、不做任何改写。

    旧行为：行末/行内分号被判「代码包含分号」→ 校验失败 → 修复 →（单行分号写法
    修不掉）→ **整块从正文删除**。现与渲染侧共用同一归一实现
    （chart_validators.normalize_mermaid_statements），不再误删合法图表。
    """
    ok, code = _validate_inline_chart("flowchart", "flowchart TD\n    A --> B;\n    B --> C")
    assert ok is True and code == "flowchart TD\n    A --> B;\n    B --> C"

    # 单行分号写法（旧实现在此把节点数算成 0 → 判"节点数不足"→ 删块）
    single = 'flowchart TD; A["准备"] --> B["施工"]; B --> C["验收"]'
    ok2, code2 = _validate_inline_chart("flowchart", single)
    assert ok2 is True and code2 == single

    # 分号只归一为换行，不进入节点标签：引号内的分号必须原样保留
    from app.services.chart_validators import normalize_mermaid_statements
    assert normalize_mermaid_statements('A["准备;验收"] --> B') == 'A["准备;验收"] --> B'


def test_validate_mermaid_repair_fail():
    ok, code = _validate_inline_chart("flowchart", "graph TD\nA-[完全坏掉的语法")
    assert ok is False
    assert code == ""


def test_validate_layout_json():
    good = json.dumps({"type": "layout", "areas": [{"id": "a", "label": "x", "x": 0, "y": 0, "w": 10, "h": 10}]})
    ok, code = _validate_inline_chart("layout", good)
    assert ok is True and code == good
    bad = json.dumps({"type": "layout", "areas": []})
    ok, _ = _validate_inline_chart("layout", bad)
    assert ok is False


def test_validate_timeline_json():
    good = json.dumps({"type": "timeline", "milestones": [
        {"date": "2026-01-01", "title": "开工"},
        {"date": "2026-03-01", "title": "竣工"},
    ]})
    ok, code = _validate_inline_chart("timeline", good)
    assert ok is True and code == good
    bad = json.dumps({"type": "timeline", "milestones": [{"date": "2026-01-01", "title": "只有一件"}]})
    ok, _ = _validate_inline_chart("timeline", bad)
    assert ok is False


def test_validate_bad_json():
    ok, _ = _validate_inline_chart("layout", "{not json")
    assert ok is False


# ---------- register_inline_charts ----------

def test_register_valid_inserts_and_returns_same_content():
    db = FakeDb()
    content = "前文\n```mermaid\n" + VALID_FLOWCHART + "\n```\n后文"
    n, new_content = run(register_inline_charts(db, "scheme-1", "sec-1", content))
    assert n == 1
    assert new_content == content  # 校验通过不改正文
    inserts = [r for r in db.rows if r[0].startswith("INSERT INTO chart_predictions")]
    deletes = [r for r in db.rows if r[0].startswith("DELETE FROM chart_predictions")]
    assert len(inserts) == 1
    # 全量同步：先按 section_id 清理历史登记（而非按类型逐条）
    assert deletes[0][0] == "DELETE FROM chart_predictions WHERE section_id=?"
    assert deletes[0][1] == ("sec-1",)
    payload = json.loads(inserts[0][1][7])
    assert payload["mermaid_code"] == VALID_FLOWCHART
    # 状态口径：已生成完毕写 "generated"（与 fix-mermaid / generate-ai_image 统一，
    # 导出预检与 CHART_DONE_STATUSES 同时接纳 'done' 与 'generated'）
    assert inserts[0][1][6] == "generated"
    assert db.commits == 0  # 不 commit，由调用方与正文更新同事务提交


def test_register_repairs_and_rewrites_content():
    db = FakeDb()
    # 中文标签含特殊字符但缺双引号（校准：行末分号是合法语法，不再触发修复）
    bad = 'flowchart TD\n    A["准备"] --> B[开挖(第一层)]\n    B --> C["验收"]'
    content = "```mermaid\n" + bad + "\n```"
    n, new_content = run(register_inline_charts(db, "scheme-1", "sec-2", content))
    assert n == 1
    assert 'B["开挖(第一层)"]' in new_content
    inserts = [r for r in db.rows if r[0].startswith("INSERT INTO chart_predictions")]
    payload = json.loads(inserts[0][1][7])
    assert 'B["开挖(第一层)"]' in payload["mermaid_code"]
    assert validate_mermaid_again(payload["mermaid_code"]) is True


def test_register_keeps_semicolon_flowchart_block():
    """行内/行末分号的合法流程图**不再被修复或删除**（回归锁定）。"""
    db = FakeDb()
    bad = "flowchart TD\n    A --> B;\n    B --> C"
    content = "前文\n```mermaid\n" + bad + "\n```\n后文"
    n, new_content = run(register_inline_charts(db, "scheme-1", "sec-2b", content))
    assert n == 1
    assert new_content == content
    inserts = [r for r in db.rows if r[0].startswith("INSERT INTO chart_predictions")]
    assert json.loads(inserts[0][1][7])["mermaid_code"] == bad


def test_register_repair_fail_removes_block():
    db = FakeDb()
    content = "前文\n```mermaid\ngraph TD\nA-[坏\n```\n后文"
    n, new_content = run(register_inline_charts(db, "scheme-1", "sec-3", content))
    assert n == 0
    assert "```mermaid" not in new_content
    assert "前文" in new_content and "后文" in new_content
    inserts = [r for r in db.rows if r[0].startswith("INSERT INTO chart_predictions")]
    assert inserts == []


def test_register_no_charts_noop():
    db = FakeDb()
    content = "纯文本章节，无图表。"
    n, new_content = run(register_inline_charts(db, "scheme-1", "sec-4", content))
    assert n == 0
    assert new_content == content
    assert db.commits == 0
    # ✅ 仍须清理该章节历史登记（重新生成后正文不再含图 → 不留僵尸图）
    deletes = [r for r in db.rows if r[0].startswith("DELETE FROM chart_predictions")]
    assert deletes and deletes[0][1] == ("sec-4",)
    assert [r for r in db.rows if r[0].startswith("INSERT")] == []


def test_register_all_invalid_still_clears_stale_rows():
    """块存在但全部校验/修复失败（被删块）→ 同样清理历史登记，避免残留僵尸图。"""
    db = FakeDb()
    content = "前文\n```mermaid\ngraph TD\nA-[坏\n```\n后文"
    n, new_content = run(register_inline_charts(db, "scheme-1", "sec-6", content))
    assert n == 0
    assert "```mermaid" not in new_content
    deletes = [r for r in db.rows if r[0].startswith("DELETE FROM chart_predictions")]
    assert deletes and deletes[0][1] == ("sec-6",)
    assert [r for r in db.rows if r[0].startswith("INSERT")] == []


def test_register_multiple_types_dedup():
    db = FakeDb()
    content = ("```mermaid\n" + VALID_FLOWCHART + "\n```\n"
               "```mermaid\nsequenceDiagram\n    A->>B: hi\n```\n"
               "```chart-json\n{\"type\": \"layout\", \"areas\": [{\"id\": \"a\", \"label\": \"x\", \"x\": 0, \"y\": 0, \"w\": 10, \"h\": 10}]}\n```")
    n, new_content = run(register_inline_charts(db, "scheme-1", "sec-5", content))
    assert n == 3  # flowchart / sequence / layout 三类各一个
    assert new_content == content


def validate_mermaid_again(code):
    from app.services.ai.image_engine import validate_mermaid
    return validate_mermaid(code)[0]


# ===========================================================================
# BUG 回归：同类型第 2 个图表块必须同样进入校验/修复/限额管线
# ---------------------------------------------------------------------------
# 旧实现用 `extract_inline_charts`（按类型去重）驱动登记，导致同类型的第 2、3 个
# 块**从不进入管线**：坏图不被删除（导出出红字占位）、"每章最多 1 个"上限失效、
# 图表清单少报。现登记消费 `_scan_inline_charts`（全部块），登记仍按类型去重。
# ===========================================================================

def test_scan_inline_charts_returns_all_blocks():
    """扫描层不去重：同类型两块都要返回（供校验/限额使用）。"""
    from app.routers._chart_pipeline import _scan_inline_charts
    content = ("```mermaid\n" + VALID_FLOWCHART + "\n```\n"
               "```mermaid\nflowchart LR\n    C --> D\n```")
    assert len(_scan_inline_charts(content)) == 2
    # 对外清单口径仍去重（保持既有语义）
    assert len(extract_inline_charts(content)) == 1


def test_register_limits_removes_same_type_extra_block():
    """enforce_limits=True 时，同类型第 2 块应被"每章≤1"上限裁剪出正文。"""
    db = FakeDb()
    second = "flowchart LR\n    C --> D"
    content = ("```mermaid\n" + VALID_FLOWCHART + "\n```\n正文\n"
               "```mermaid\n" + second + "\n```")
    n, new_content = run(register_inline_charts(
        db, "scheme-1", "sec-dup1", content, enforce_limits=True))
    assert n == 1                                   # 只登记一个
    assert second not in new_content                # 第 2 块已从正文移除
    assert VALID_FLOWCHART in new_content           # 第 1 块保留
    inserts = [r for r in db.rows if r[0].startswith("INSERT")]
    assert len(inserts) == 1


def test_register_validates_same_type_extra_block_when_no_limits():
    """enforce_limits=False 时，同类型第 2 块仍必须过校验：坏块被删除。"""
    db = FakeDb()
    bad_second = "graph TD\nA-[坏"       # 非法 Mermaid，修复也不可行
    content = ("```mermaid\n" + VALID_FLOWCHART + "\n```\n正文\n"
               "```mermaid\n" + bad_second + "\n```")
    n, new_content = run(register_inline_charts(
        db, "scheme-1", "sec-dup2", content, enforce_limits=False))
    assert n == 1
    # 旧实现会把这个坏块原样留在正文里 → 导出时变成「渲染失败」红字占位
    assert bad_second not in new_content
    assert new_content.count("```mermaid") == 1
    assert VALID_FLOWCHART in new_content


def test_register_same_type_second_block_repair_rewrites_content():
    """同类型第 2 块若可被正则修复，修复结果应回写正文。"""
    db = FakeDb()
    fixable = "flowchart TD\nA[开始] - B[结束]"     # 缺箭头闭合，repair 可修
    content = ("```mermaid\n" + VALID_FLOWCHART + "\n```\n正文\n"
               "```mermaid\n" + fixable + "\n```")
    n, new_content = run(register_inline_charts(
        db, "scheme-1", "sec-dup3", content, enforce_limits=False))
    assert n == 1
    assert new_content != content
    assert fixable not in new_content


# ===========================================================================
# BUG 回归：labor 载荷形状偏离规范时整块被删（唯一无归一容错的 JSON 图表）
# ---------------------------------------------------------------------------
# labor 此前在**校验侧与渲染侧都没有归一**，只认 phases/categories/data 三件套。
# AI 写 {"type":"labor","trades":[{"name":"木工","peak":30}]} 这类自然形态时，
# 校验判非法 → 本模块"校验失败 → 删块"策略把正文里的劳动力图整块删掉
# （正文凭空少一张图，且只剩一条 warning 日志）。
# 现与 architecture 同样走共享归一器 normalize_labor_data（校验/渲染两侧同口径）。
# ===========================================================================

def test_validate_labor_natural_shapes_pass():
    """labor 的自然形态应通过校验（旧实现一律判非法 → 删块）。"""
    shapes = [
        {"type": "labor", "trades": [{"name": "木工", "peak": 30},
                                     {"name": "钢筋工", "peak": 18}]},
        {"type": "labor", "data": {"木工": [10, 20], "钢筋工": [5, 8]}},
        {"type": "labor", "series": [{"name": "木工", "data": [10, 20]}]},
        {"type": "labor", "labels": ["基础", "主体"],
         "datasets": [{"label": "木工", "data": [10, 20]}]},
        {"type": "labor", "data": [{"phase": "基础", "木工": 10, "钢筋工": 5}]},
    ]
    for payload in shapes:
        code = json.dumps(payload, ensure_ascii=False)
        ok, fixed = _validate_inline_chart("labor", code)
        assert ok is True, (payload, fixed)
        assert fixed == code          # 校验通过不重写正文（归一只在判定时使用）


def test_register_labor_natural_shape_not_deleted():
    """自然形态的 labor 数据块应被登记，而不是从正文删除。"""
    db = FakeDb()
    code = json.dumps(
        {"type": "labor", "trades": [{"name": "木工", "peak": 30}]},
        ensure_ascii=False)
    content = "劳动力投入如下：\n```chart-json\n" + code + "\n```\n后续正文。"
    n, new_content = run(register_inline_charts(db, "scheme-1", "sec-labor1", content))
    assert n == 1
    assert new_content == content                    # 未被删块
    inserts = [r for r in db.rows if r[0].startswith("INSERT")]
    assert len(inserts) == 1
    assert inserts[0][1][3] == "labor"
    payload = json.loads(inserts[0][1][7])
    assert payload["data"]["type"] == "labor"        # 结构化数据走 data 分支
    assert payload["data"]["trades"][0]["name"] == "木工"


def test_validate_labor_repair_prompt_shape_passes():
    """修复提示词（chart_json_fix）教出的 labor 形态必须通过校验。

    这是**修复闭环的硬约束**：charts.py 的修复循环用 _validate_inline_chart 校验
    AI 的修复结果，判不过 → validated=False → 不写回正文（图永久坏着）。
    提示词教 ``phases[].workers[{"trade","count"}]``，而校验器若只认顶层三件套，
    则 labor 的 AI 修复**永远不可能成功**（3 轮全废）。归一器必须吃下这个形态。
    """
    code = json.dumps({
        "type": "labor",
        "title": "劳动力配置计划",
        "phases": [
            {"name": "施工准备", "workers": [{"trade": "普工", "count": 15},
                                            {"trade": "管理人员", "count": 6}]},
            {"name": "主体工程", "workers": [{"trade": "普工", "count": 35},
                                            {"trade": "管理人员", "count": 12}]},
        ],
    }, ensure_ascii=False)
    ok, fixed = _validate_inline_chart("labor", code)
    assert ok is True, fixed


def test_labor_prompt_shapes_aligned():
    """生成提示词与修复提示词必须教**同一种** labor 数据结构。

    历史缺陷：content_generation_system 教 ``phases/categories/data`` 矩阵，
    而 chart_json_fix 教 ``phases[].workers[]`` —— 同一图表类型两种目标结构，
    AI 生成/修复必然分叉，且修复结果会被校验器判非法（闭环失败）。
    """
    from app.services.ai.prompts import get_prompt

    gen = get_prompt("content_generation_system")
    fix = get_prompt("chart_json_fix")
    for name, text in (("content_generation_system", gen), ("chart_json_fix", fix)):
        labor_lines = [ln for ln in text.splitlines() if "labor" in ln]
        assert labor_lines, f"{name} 未声明 labor 数据结构"
        assert any("categories" in ln and "data" in ln for ln in labor_lines), (
            f"{name} 的 labor 说明未使用三件套矩阵（phases/categories/data）")
        assert not any("workers" in ln for ln in labor_lines), (
            f"{name} 的 labor 说明仍在使用 phases[].workers[] 形态")

