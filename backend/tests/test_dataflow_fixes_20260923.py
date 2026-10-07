# -*- coding: utf-8 -*-
"""数据流完整性审计修复回归用例（2026-09-23）。

覆盖五项修复，逐项做「正向 + 反例回归」：
- P0-1 format_downstream_context：从只消费硬编码 7 项 → 消费全部 18 项（含未知项兜底）。
- P0-2 正文生成结构化摘要预算：500 → 2000（源码级断言，防回归）。
- P1-3 全局事实读取：SQL 收敛去重 + 低置信度标注（3/4 元组兼容）。
- P1-4 FactItem 溯源/语义扩展字段落库（含 persist_extraction 端到端）。
- P1-5 source_ref 引句静默截断：放宽到 120 字并补省略号。
"""
import inspect

import pytest
from app.routers.sse_handlers import _render_facts_text
from app.services.bid_analysis_service import ANALYSIS_ITEMS, format_downstream_context
from app.services.facts_extractor import (
    FACTS_GT_COLUMN,
    MAX_SOURCE_EXCERPT,
    ExtractionResult,
    FactGroup,
    FactItem,
    _clip_excerpt,
    build_injectable_facts_query,
    persist_extraction,
)


def _item(item_id, label, content, output_type="markdown", status="success"):
    return item_id, {
        "item_id": item_id, "label": label, "output_type": output_type,
        "status": status, "content": content,
    }


# --------------------------------------------------------------------------
# P0-1：format_downstream_context 消费全部 18 项
# --------------------------------------------------------------------------

def test_downstream_context_includes_all_non_whitelisted_items():
    """旧实现只认 7 个 item_id，另外 11 项（含 10 个必选）从未进入提示词。"""
    old_whitelist = {
        "projectBasicInfo", "schemeBasicInfo", "overviewParams", "compilationBasis",
        "siteConditions", "deploymentSchedule", "constructionTechnique",
    }
    non_wl = [d["item_id"] for d in ANALYSIS_ITEMS if d["item_id"] not in old_whitelist]
    assert non_wl, "应存在被旧白名单遗漏的项"

    items = dict(_item(iid, f"标签-{iid}", f"内容-{iid}-唯一标识串") for iid in non_wl)
    out = format_downstream_context(items)
    for iid in non_wl:
        assert f"标签-{iid}" in out, f"{iid} 的小节标题应出现在下游摘要中"
        assert f"内容-{iid}-唯一标识串" in out, f"{iid} 的正文内容应出现在下游摘要中"


def test_downstream_context_includes_project_json_and_unknown_fallback():
    """JSON 项平铺；不在权威清单内的未知 item_id 也要兜底追加，不得静默丢弃。"""
    items = {
        "projectBasicInfo": {
            "item_id": "projectBasicInfo", "label": "项目级基本信息",
            "output_type": "json", "status": "success",
            "content": '{"contractor":"某某建设公司"}',
        },
        "customUnknownItem": {
            "item_id": "customUnknownItem", "label": "未来新增自定义项",
            "output_type": "markdown", "status": "success",
            "content": "未知项内容必须保留",
        },
    }
    out = format_downstream_context(items)
    assert "某某建设公司" in out
    assert "未来新增自定义项" in out and "未知项内容必须保留" in out


def test_downstream_context_still_skips_empty():
    """反例回归：全「没有提及」的 JSON 项仍不得下发空小节。"""
    out = format_downstream_context({
        "projectBasicInfo": {
            "item_id": "projectBasicInfo", "label": "项目级基本信息",
            "output_type": "json", "status": "success",
            "content": '{"project_name":"没有提及"}',
        },
    })
    assert "项目级基本信息" not in out


# --------------------------------------------------------------------------
# P0-2：正文生成结构化摘要预算对齐（源码级防回归）
# --------------------------------------------------------------------------

def test_content_generation_brief_budget_raised():
    """正文生成的 project_brief 逐章注入，预算必须能容纳结构化提取全量成果。

    ✅ 四项依据改造（2026-09-23）：旧预算 2000 字在 18 项提取成果全量下发时
    常态化截断（与「完整调用、不丢失不截断」约束相悖），上调为 4000 对齐
    目录生成档；历史低预算档（500/2000 硬编码）不得回退。"""
    import app.routers.sse_handlers as sh
    src = inspect.getsource(sh)
    # 正文生成处应为 4000 预算，且不再残留旧的低预算截断
    assert "max_chars=4000" in src
    assert "max_chars=500" not in src
    assert "max_chars=2000" not in src


# --------------------------------------------------------------------------
# P1-3：SQL 收敛去重 + 低置信度标注（3/4 元组兼容）
# --------------------------------------------------------------------------

def test_render_facts_text_low_confidence_marker():
    # 低于阈值 → 带低置信度标注
    rows_low = [("基坑支护", "开挖深度", "8.6m", 0.3)]
    assert "低置信度" in _render_facts_text(rows_low)
    # 高置信度 → 无标注
    rows_hi = [("基坑支护", "开挖深度", "8.6m", 0.95)]
    assert "低置信度" not in _render_facts_text(rows_hi)
    # 3 元组（无 confidence 列）→ 无标注，向后兼容既有渲染口径
    rows_3 = [("基坑支护", "开挖深度", "8.6m")]
    assert "低置信度" not in _render_facts_text(rows_3)


def test_build_injectable_facts_query_shape_and_filter():
    # project_id 非空：带项目级并联条件，两个参数
    sql, params = build_injectable_facts_query(
        "s1", "p1", f"{FACTS_GT_COLUMN}, title, content, confidence")
    assert params == ("s1", "p1")
    assert "project_id=?" in sql
    # 过滤口径固定：剔除矛盾值与未确认事实
    assert "has_conflict=0" in sql and "is_resolved=1" in sql
    assert "ORDER BY gt, title" in sql

    # project_id 为空：安全回退仅方案级，单参数
    sql2, params2 = build_injectable_facts_query("s1", "", f"{FACTS_GT_COLUMN}, title, content")
    assert params2 == ("s1",)
    assert "project_id=?" not in sql2


# --------------------------------------------------------------------------
# P1-4：FactItem 扩展字段落库
# --------------------------------------------------------------------------

def test_to_db_row_carries_extended_fields():
    it = FactItem(
        name="塔吊数量", value="3", key="tower_crane", category="machinery",
        source="招标文件.pdf", source_ref="拟投入 3 台塔吊",
        value_unit="台", fact_type="machinery", evidence_kind="table",
        page_ref=42, zone_type="machinery_stat", is_safety_critical=True,
        norm_group="machinery",
    )
    row = it.to_db_row("g1", "p1", "s1", "机械设备")
    # ✅ 2026-09-24：九大章节四维标注追加在元组尾部，23 → 27 列。
    # ✅ 2026-10-06：is_stale 补入（GLOBAL_FACTS_INSERT_COLS 单一事实源），
    #    位置在四维**之前** → 27 → 28 列，尾部 4 列仍是四维。
    #    列数与顺序不再写死，统一由 GLOBAL_FACTS_INSERT_COLS 校验。
    from app.services.facts_extractor import GLOBAL_FACTS_INSERT_COLS
    assert len(row) == len(GLOBAL_FACTS_INSERT_COLS) == 28
    assert row[16] == "台"
    assert row[17] == "machinery"
    assert row[18] == "table"
    assert row[19] == 42
    assert row[20] == "machinery_stat"
    assert row[21] == 1
    assert row[22] == "machinery"
    # is_stale：提取管线产出的事实恒为「未过期」（过期只由资料重传/删除触发）
    assert GLOBAL_FACTS_INSERT_COLS[23] == "is_stale"
    assert row[23] == 0
    # 尾部 4 列：未显式标注的 FactItem 取默认值（章节/属性/来源为空、非共享）
    assert row[24] == ""
    assert row[25] == ""
    assert row[26] == ""
    assert row[27] == 0


@pytest.mark.asyncio
async def test_persist_extraction_writes_extended_columns(db_conn):
    """端到端：迁移补列 + INSERT 占位符与 to_db_row 元数一致，落库后可回读。"""
    it = FactItem(
        name="高支模搭设高度", value="12.4", key="formwork_height",
        category="design_param", source="施组.docx", source_ref="搭设高度 12.4m",
        fact_type="design_param", evidence_kind="text", page_ref=7,
        zone_type="structure_zone", norm_group=None,
    )
    result = ExtractionResult(
        groups=[FactGroup(title="设计参数", category="design_param", items=[it])],
        total_items=1,
        chunk_hashes_run={"h1"},
        chunk_hashes_ok={"h1"},
    )
    await persist_extraction(result, db_conn, "p1", "s1")

    cur = await db_conn.execute(
        "SELECT value_unit, fact_type, evidence_kind, page_ref, zone_type, "
        "is_safety_critical, norm_group, source_ref FROM global_facts "
        "WHERE project_id='p1' AND scheme_id='s1'")
    rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    r = rows[0]
    assert r["fact_type"] == "design_param"
    assert r["evidence_kind"] == "text"
    assert r["page_ref"] == 7
    assert r["zone_type"] == "structure_zone"
    assert r["is_safety_critical"] == 0
    assert r["norm_group"] == ""


# --------------------------------------------------------------------------
# P1-5：source_ref 引句截断可见化
# --------------------------------------------------------------------------

def test_clip_excerpt():
    assert _clip_excerpt("短引句", MAX_SOURCE_EXCERPT) == "短引句"
    long = "深" * 200
    clipped = _clip_excerpt(long, MAX_SOURCE_EXCERPT)
    assert clipped.endswith("…")
    assert len(clipped) == MAX_SOURCE_EXCERPT + 1
    # 放宽后不再截到 30 字
    assert MAX_SOURCE_EXCERPT >= 120


@pytest.mark.asyncio
async def test_to_db_row_long_quote_marked_truncated():
    """超长引句落库时以省略号明示截断，不再静默丢尾。"""
    it = FactItem(name="依据", value="x", source_ref="规" * 300)
    row = it.to_db_row("g", "p", "s")
    import json
    quote = json.loads(row[8])[0]["quote"]
    assert quote.endswith("…")
    assert len(quote) == MAX_SOURCE_EXCERPT + 1
