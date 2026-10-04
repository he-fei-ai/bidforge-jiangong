"""scheme_classification 单元测试（确定性、零 AI/零 DB）。

覆盖：方案名称解析、危大/超规模阈值判定、九大章节字段映射、完整性校验。
"""
import pytest
from app.services.scheme_classification import (
    HAZARD_CATEGORIES,
    NINE_CHAPTERS,
    SchemeClassification,
    build_chapter_extraction_text,
    build_classification_hint,
    classify_scheme,
    classify_scheme_name,
    evaluate_hazard_level,
    format_classification_hint,
    map_extraction_to_chapters,
    match_category_keywords,
    match_chapter_by_title,
    required_fields_for_chapter,
    top_ancestor,
    validate_chapter_fields,
)


# ---------------------------------------------------------------------------
# 1. 分类体系完整性
# ---------------------------------------------------------------------------
def test_six_categories_present():
    ids = [c["id"] for c in HAZARD_CATEGORIES]
    assert ids == [
        "foundation_pit", "formwork", "hoisting",
        "scaffold", "demolition", "other",
    ]
    # 每个大类至少 1 个子类
    for c in HAZARD_CATEGORIES:
        assert c["subs"], f"{c['id']} 缺少子类"


def test_nine_chapters_present():
    chapters = [ch["chapter"] for ch in NINE_CHAPTERS]
    assert chapters == [1, 2, 3, 4, 5, 6, 7, 8, 9]


# ---------------------------------------------------------------------------
# 2. 方案名称关键词解析
# ---------------------------------------------------------------------------
def test_classify_compound_name():
    hits = classify_scheme_name("基坑支护及土方开挖安全专项施工方案")
    sub_ids = {h["sub_id"] for h in hits}
    assert "fp_support_drain" in sub_ids
    assert "fp_earthwork" in sub_ids
    cat_ids = {h["category_id"] for h in hits}
    assert "foundation_pit" in cat_ids


def test_classify_tall_formwork():
    hits = classify_scheme_name("高大模板支撑体系工程专项施工方案")
    sub_ids = {h["sub_id"] for h in hits}
    assert "fw_tall" in sub_ids or "fw_support" in sub_ids


def test_classify_no_hit_returns_empty():
    assert classify_scheme_name("普通装饰装修施工方案") == []


def test_match_keywords_case_insensitive():
    hits = match_category_keywords("深基坑 DEPTH 开挖")
    assert any(h["sub_id"] == "fp_earthwork" for h in hits)


# ---------------------------------------------------------------------------
# 3. 阈值判定
# ---------------------------------------------------------------------------
def test_threshold_pit_oversize():
    lvl = evaluate_hazard_level("fp_earthwork", {"depth": 6.2})
    assert lvl["is_hazardous"] is True
    assert lvl["is_oversize"] is True


def test_threshold_pit_3m_is_hazardous():
    # 建办质〔2018〕31号 附件一：开挖深度≥3m 即属危大工程，≥5m 才属超规模。
    # 临界值 3.0m 恰好落在分水岭上 —— 旧实现按 >=5 判定会漏判（P0 安全缺陷）。
    lvl = evaluate_hazard_level("fp_earthwork", {"depth": 3.0})
    assert lvl["is_hazardous"] is True
    assert lvl["is_oversize"] is False
    assert lvl["missing_params"] == []


def test_threshold_pit_between_3_and_5m():
    # 3m < depth < 5m：危大但不超规模
    lvl = evaluate_hazard_level("fp_support_drain", {"depth": 4.2})
    assert lvl["is_hazardous"] is True
    assert lvl["is_oversize"] is False


def test_threshold_pit_below_3m_not_hazardous():
    lvl = evaluate_hazard_level("fp_earthwork", {"depth": 2.8})
    assert lvl["is_hazardous"] is False
    assert lvl["is_oversize"] is False


def test_threshold_tall_formwork_by_height():
    lvl = evaluate_hazard_level("fw_tall", {"height": 9})
    assert lvl["is_oversize"] is True


def test_threshold_tall_formwork_boundary_closed():
    # 「及以上」为闭区间：8m / 18m / 15kN/m² / 20kN/m 这几个临界值必须命中
    assert evaluate_hazard_level("fw_tall", {"height": 8})["is_oversize"] is True
    assert evaluate_hazard_level("fw_tall", {"span": 18})["is_oversize"] is True
    assert evaluate_hazard_level("fw_tall", {"total_load": 15})["is_oversize"] is True
    assert evaluate_hazard_level("fw_tall", {"line_load": 20})["is_oversize"] is True


def test_threshold_non_param_is_always_hazardous():
    lvl = evaluate_hazard_level(None)
    assert lvl["is_hazardous"] is True
    assert lvl["is_oversize"] is True


def test_threshold_attached_scaffold_is_hazardous():
    lvl = evaluate_hazard_level("sc_attached")
    assert lvl["is_hazardous"] is True


def test_threshold_missing_param_conservative():
    # 阈值型子类缺参 → 保守视为危大，并提示补全
    lvl = evaluate_hazard_level("fp_earthwork", {})
    assert lvl["is_hazardous"] is True
    assert "depth" in lvl["missing_params"]


# ---------------------------------------------------------------------------
# 4. 综合分类
# ---------------------------------------------------------------------------
def test_classify_scheme_aggregates():
    res = classify_scheme("钢结构网架安装专项施工方案", {"span": 40})
    assert "other" in res.category_ids
    assert "ot_steel" in res.sub_ids
    assert res.is_hazardous is True   # 跨度≥36m → 危大
    assert res.is_oversize is True


def test_classify_scheme_demolition():
    res = classify_scheme("爆破拆除工程专项施工方案")
    assert "dm_blast" in res.sub_ids
    assert res.is_hazardous is True


# ---------------------------------------------------------------------------
# 5. 九大章节字段映射
# ---------------------------------------------------------------------------
def test_required_fields_base_and_category():
    base = required_fields_for_chapter("overview")
    assert "工程名称" in base and "工程地点" in base
    cat = required_fields_for_chapter("overview", "foundation_pit")
    assert "基坑周长/面积/深度" in cat
    assert "JGJ120" not in cat


def test_basis_category_fields():
    cat = required_fields_for_chapter("basis", "scaffold")
    assert "JGJ130" in cat


def test_map_extraction_source_items():
    extraction = {"projectBasicInfo": "项目经理：张三", "overviewParams": "基坑深度 6m"}
    mapping = map_extraction_to_chapters(extraction)
    assert mapping["overview"]["filled"] is True
    # 缺源提取项 → 该章节 unfilled
    mapping2 = map_extraction_to_chapters({"projectBasicInfo": "x"})
    assert mapping2["overview"]["missing_source_items"] == ["overviewParams"]


# ---------------------------------------------------------------------------
# 6. 字段完整性校验
# ---------------------------------------------------------------------------
def test_format_classification_hint_roundtrip():
    res = classify_scheme("基坑支护及土方开挖安全专项施工方案", {"depth": 6})
    hint = format_classification_hint(res)
    assert "基坑" in hint
    assert "危大级别" in hint
    assert "九大章节" in hint
    # 空分类 → 空串（调用方据此不注入）
    assert format_classification_hint(SchemeClassification()) == ""


def test_build_classification_hint_from_name():
    hint = build_classification_hint("高大模板支撑体系工程专项施工方案")
    assert "高大模板" in hint or "模板" in hint
    assert "九大章节" in hint
    # 无名称无文本 → 空串
    assert build_classification_hint("") == ""


def test_build_system_prompt_includes_classification_hint():
    from app.services.bid_analysis_service import build_system_prompt
    # 旧版：section_hint/classification_hint 均为空 → 与基础 prompt 一致
    base = build_system_prompt()
    assert build_system_prompt("") == base
    assert build_system_prompt("", "") == base
    # 注入分类提示后，system 消息同时含标段与分类约束
    out = build_system_prompt("标段A施工范围", "本方案为基坑工程")
    assert "标段A施工范围" in out
    assert "本方案为基坑工程" in out


def test_validate_all_filled():
    extraction = {
        "projectBasicInfo": "工程名称：某项目 工程地点：某市 建设规模：10万 结构形式：框架 参建各方责任主体单位：建设/设计/施工 风险辨识与分级：高 气候特征：多雨",  # noqa: E501
        "overviewParams": "基坑周长 200m 面积 3000 深度 6m 支护形式 排桩 降水方式 管井 监测要求 沉降",  # noqa: E501
        "compilationBasis": "适用法规清单 适用标准清单 JGJ120 GB50497 GB50202 施工图编号 施工组织设计编号 施工合同编号",  # noqa: E501
        "deploymentSchedule": "计划开工日期 计划竣工日期 分项进度节点 材料需求清单 设备配置清单 劳动力配置表",  # noqa: E501
        "resourceAllocation": "管理人员名单及岗位 安全员名单 特种作业人员及证书编号 作业人员配置",  # noqa: E501
        "constructionTechnique": "材料选型 规格 技术参数 工艺流程步骤 施工方法描述 操作要求 质量检查标准",  # noqa: E501
        "safetyMeasures": "安全组织机构 安全职责分工 技术措施清单 监测方案参数 预警值",  # noqa: E501
        "qualityAcceptance": "验收标准编号 验收程序步骤 验收内容清单 验收人员组成",  # noqa: E501
        "emergencyResponse": "应急组织架构 应急联系人及电话 应急物资清单 救援线路 附近医院信息",  # noqa: E501
        "calcAndDrawings": "计算书类型 计算参数 图纸清单 图纸编号",
    }
    res = validate_chapter_fields(extraction, "foundation_pit")
    assert res["missing_chapters"] == []
    assert res["completeness"] == 1.0
    # 每个章节字段级应全覆盖
    for key, ch in res["chapters"].items():
        assert ch["missing_fields"] == [], f"{key} 缺失 {ch['missing_fields']}"


def test_validate_partial_missing():
    extraction = {
        # 仅提供工程概况，其余缺失
        "projectBasicInfo": "工程名称 工程地点 建设规模 结构形式 参建各方责任主体单位 风险辨识与分级 气候特征",  # noqa: E501
        "overviewParams": "基坑周长 面积 深度 6m",  # 不含支护形式/降水方式/监测要求
    }
    res = validate_chapter_fields(extraction, "foundation_pit")
    assert "basis" in res["missing_chapters"]
    assert res["completeness"] < 1.0
    # 工程概况基础字段已覆盖，但基坑专属字段（支护形式/降水方式/监测要求）缺失
    miss = res["chapters"]["overview"]["missing_fields"]
    assert "支护形式" in miss and "降水方式" in miss


def test_validate_category_appends_fields():
    # 基坑类应追加基坑专属必填字段；缺失时计入 missing_fields
    extraction = {
        "projectBasicInfo": "工程名称 工程地点 建设规模 结构形式 参建各方责任主体单位 风险辨识与分级 气候特征",  # noqa: E501
        "overviewParams": "深度 6m",  # 缺 基坑周长/面积、支护形式、降水方式、监测要求
    }
    res = validate_chapter_fields(extraction, "foundation_pit")
    miss = res["chapters"]["overview"]["missing_fields"]
    assert "基坑周长/面积/深度" in miss or "支护形式" in miss


# ---------------------------------------------------------------------------
# 7. 章节标题匹配 / 顶层祖先上溯 / 本章提取文本拼装（#10 正文按章注入）
# ---------------------------------------------------------------------------
def test_match_chapter_by_title():
    assert match_chapter_by_title("1 工程概况") == "overview"
    assert match_chapter_by_title("三、施工安全保证措施") == "safety"
    assert match_chapter_by_title("7 验收要求与标准") == "acceptance"
    assert match_chapter_by_title("计算书及相关施工图纸") == "calc_drawings"
    assert match_chapter_by_title("项目管理机构") is None


def test_top_ancestor():
    nodes_map = {
        "c1": {"id": "c1", "parent_id": ""},
        "c2": {"id": "c2", "parent_id": "c1"},
        "c3": {"id": "c3", "parent_id": "c2"},
    }
    assert top_ancestor("c3", nodes_map)["id"] == "c1"
    assert top_ancestor("c1", nodes_map)["id"] == "c1"
    assert top_ancestor("missing", nodes_map) is None


def test_build_chapter_extraction_text():
    items = [
        {"label": "方案级基本信息", "content": "深度 6m"},
        {"label": "施工工艺技术", "content": ""},  # 空内容应剔除
        {"item_id": "x", "content": "工艺要点"},
    ]
    text = build_chapter_extraction_text(items, "overview")
    assert "深度 6m" in text and "工艺要点" in text
    assert "施工工艺技术" not in text
    # 截断
    assert build_chapter_extraction_text(items, "overview", max_chars=5) == "### 方案级基本信"[:5]


async def test_build_category_reference_outline():
    import json as _json

    from app.services.outline_reference import build_category_reference_outline

    outline = [{"title": "1 工程概况", "children": [{"title": "1.1 开挖深度"}]}]
    rows = [{
        "id": "L1", "name": "基坑标准目录", "type": "foundation_pit",
        "version": "v1.0", "outline_json": _json.dumps(outline),
    }]
    # 极简假 db：仅实现 execute → fetchall
    class _Cur:
        def __init__(self, r): self.r = r
        async def fetchall(self): return self.r
    class _Db:
        async def execute(self, sql, params=()):
            # 模拟 outline_library.type IN (?) 过滤
            return _Cur([r for r in rows if r.get("type") in (params or ())])
    text, hits = await build_category_reference_outline(_Db(), ["foundation_pit"])
    assert "基坑标准目录" in text and "工程概况" in text
    assert hits == ["L1"]

    # 类别不匹配 → 空
    empty, empty_hits = await build_category_reference_outline(_Db(), ["scaffold"])
    assert empty == "" and empty_hits == []


async def test_build_category_reference_outline_swallows_db_error():
    """目录库查询异常时必须降级为空参考且不抛异常（不影响目录生成）。

    回归护栏（2026-09-24 F821）：降级分支调用了 logger.warning，
    但 outline_reference 模块此前未定义 logger，异常分支自身会再抛 NameError，
    使「不影响生成」承诺失效。
    """
    from app.services.outline_reference import build_category_reference_outline

    class _BrokenDb:
        async def execute(self, sql, params=()):
            raise RuntimeError("模拟数据库故障")

    text, hits = await build_category_reference_outline(_BrokenDb(), ["foundation_pit"])
    assert text == "" and hits == []

