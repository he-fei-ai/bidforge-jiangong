"""全局事实「九大章节分类体系」单元测试（纯函数 + DB 落库往返，零 AI 依赖）。

覆盖（对齐用户验收要求，建办质〔2018〕31号 九大章节）：
- 分类体系：22 类 category / fact_type → 九大章节映射完整性、九大章节顺序与标题；
- 章节归属：文本规则优先级、既有 category 无法表达的「计算书/图纸」历史缺口；
- 事实属性：定量 / 定性 / 关系 / 规范 四类判定（含测量值与条文编号的歧义消解）；
- 数据来源：项目文件 / 施工图 / 勘察报告 / 施工组织设计 / 用户补充 五类；
- 跨章节共性事实：共性识别 + 复用章节集合；
- 危大阈值参数抽取：单位换算、同参取最大、hazard_always 类规则；
- 方案类型识别 + 阈值判定：3m/5m 分水岭（此前误判 P0 缺陷）、闭区间阈值；
- 章节聚合与字段完整性差集；
- DB 往返：新增 4 列幂等补列、persist_extraction INSERT 列数一致、读路径惰性派生。
"""
import json

import app.services.facts_extractor as fe
import pytest
from app.services import facts_classification as fc
from app.services import scheme_classification as sc

# ---------------------------------------------------------------------------
# 1. 分类体系完整性
# ---------------------------------------------------------------------------

def test_nine_chapters_order_and_titles():
    assert fc.CHAPTER_ORDER == [
        "overview", "basis", "plan", "technique", "safety",
        "personnel", "acceptance", "emergency", "calc_drawings",
    ]
    assert fc.CHAPTER_TITLES["overview"] == "工程概况"
    assert fc.CHAPTER_TITLES["calc_drawings"] == "计算书及相关施工图纸"
    assert fc.CHAPTER_NUMBERS["emergency"] == 8


def test_category_map_covers_all_categories():
    """既有 category 值域必须全部有映射项（未分类回退空串也算已覆盖）。"""
    covered = set(fc.CATEGORY_TO_CHAPTER.keys())
    assert covered == {
        "basic", "basis", "personnel", "labor", "schedule", "equipment",
        "machinery", "material_mgmt", "tech_param", "scale", "risk",
        "safety_critical", "deployment", "temporary", "process", "execution",
        "monitoring", "quality", "acceptance", "commitment", "environment",
        "emergency", "other",
    }


def test_chapter_targets_all_valid():
    """所有映射目标必须是合法的九大章节码（或空串=未分类）。"""
    for tgt in list(fc.CATEGORY_TO_CHAPTER.values()) + list(fc.FACT_TYPE_TO_CHAPTER.values()):
        assert tgt in fc.CHAPTER_ORDER or tgt == ""
    for _kws, ch in fc.CHAPTER_TEXT_RULES:
        assert ch in fc.CHAPTER_ORDER


# ---------------------------------------------------------------------------
# 2. 章节归属判定
# ---------------------------------------------------------------------------

def test_chapter_from_category_fallback():
    assert fc.classify_chapter_from_text("项目经理", "张三", category="personnel") == "personnel"
    assert fc.classify_chapter_from_text("劳动力", "焊工8人", category="labor") == "plan"
    assert fc.classify_chapter_from_text("其他事实", "x", category="other") == ""


def test_chapter_text_rule_overrides_coarse_fact_type():
    """基坑开挖深度 → 第一章工程概况（专项工程特征），不被 design_param 误吞。"""
    ch = fc.classify_chapter_from_text(
        "基坑开挖深度", "4.2 m", category="tech_param", fact_type="design_param")
    assert ch == "overview"


def test_chapter_calc_drawings_gap_filled():
    """既有 category 中无「计算书/图纸」落点 —— 历史缺口由文本规则补上。"""
    for name in ("支护结构验算书", "平面布置图", "图纸编号", "图纸清单"):
        assert fc.classify_chapter_from_text(name, "见附件", category="other") == "calc_drawings"


def test_chapter_fact_type_as_second_signal():
    """文本规则未命中时回退到 fact_type（比 category 更细）。"""
    assert fc.classify_chapter_from_text(
        "某参数", "v", category="other", fact_type="monitoring") == "safety"
    assert fc.classify_chapter_from_text(
        "某参数", "v", category="other", fact_type="material") == "technique"


def test_chapter_concrete_examples():
    cases = {
        "施工工艺流程": "technique",
        "监测报警值": "safety",
        "应急演练": "emergency",
        "验收规范": "acceptance",
        "执行标准": "basis",
        "管理人员名单": "personnel",
        "总工期": "plan",
        "地质条件": "overview",
    }
    for name, expect in cases.items():
        assert fc.classify_chapter_from_text(name, "v", category="other") == expect, name


# ---------------------------------------------------------------------------
# 3. 事实属性判定
# ---------------------------------------------------------------------------

def test_attr_quantitative():
    assert fc.classify_fact_attr("基坑开挖深度", "4.2 m") == "quantitative"
    assert fc.classify_fact_attr("总工期", "240 日历天") == "quantitative"
    assert fc.classify_fact_attr("监测点数", "36") == "quantitative"


def test_attr_norm_standard_codes():
    assert fc.classify_fact_attr("执行标准", "GB 55003-2021") == "norm"
    assert fc.classify_fact_attr("依据规范", "JGJ 120-2012") == "norm"
    assert fc.classify_fact_attr("验收依据", "第5.1.2条") == "norm"
    assert fc.classify_fact_attr("设计图", "见 图 2-1") == "norm"


def test_attr_norm_does_not_eat_measurements():
    """4.2 / 1:3.5 这类测量值不得被判为条文编号（否则定量信号丢失）。"""
    assert fc.classify_fact_attr("开挖深度", "4.2 m") == "quantitative"
    assert fc.classify_fact_attr("坡度", "1:3.5") == "quantitative"


def test_attr_relation():
    assert fc.classify_fact_attr("基坑与邻近建筑关系", "位于南侧5m") == "relation"
    assert fc.classify_fact_attr("工序搭接顺序", "先撑后挖") == "relation"
    assert fc.classify_fact_attr("地下管线距离", "距坑边2.0m") == "relation"


def test_attr_qualitative():
    assert fc.classify_fact_attr("地质条件描述", "场地以粉质黏土为主") == "qualitative"
    assert fc.classify_fact_attr("施工方法", "采用逆作法") == "qualitative"


# ---------------------------------------------------------------------------
# 4. 数据来源判定
# ---------------------------------------------------------------------------

def test_source_kind_rules():
    assert fc.classify_source_kind(source="岩土工程勘察报告.pdf") == "survey"
    assert fc.classify_source_kind(source="结构设计施工图.pdf") == "drawing"
    assert fc.classify_source_kind(source="施工组织设计.doc") == "overall_plan"
    assert fc.classify_source_kind(source="手动录入") == "manual"
    assert fc.classify_source_kind(source="招标文件.pdf") == "bid_doc"
    assert fc.classify_source_kind() == "bid_doc"  # 默认


def test_source_kind_parses_json_ref():
    """source_ref 落库形态是 JSON 数组 [{"file":..,"quote":..}]，必须能解析匹配。"""
    ref = json.dumps([{"file": "地勘报告.pdf", "quote": "地下水位-3.2m"}], ensure_ascii=False)
    assert fc.classify_source_kind("", ref) == "survey"
    ref2 = json.dumps([{"file": "图纸会审记录", "quote": "结构层数"}], ensure_ascii=False)
    assert fc.classify_source_kind("", ref2) == "drawing"


# ---------------------------------------------------------------------------
# 5. 跨章节共性事实
# ---------------------------------------------------------------------------

def test_shared_chapters_detected():
    """共性事实 → 复用章节集合（按九大章节序号升序，稳定可展示）。"""
    assert fc.shared_chapters_for("工程名称", "XX小区") == (
        "overview", "basis", "acceptance", "emergency")
    assert fc.shared_chapters_for("混凝土强度等级", "C30") == (
        "technique", "acceptance", "calc_drawings")
    assert fc.shared_chapters_for("施工总工期", "240天") == (
        "plan", "safety", "emergency")
    assert fc.shared_chapters_for("管理人员名单", "张三") == (
        "personnel", "acceptance", "emergency")
    assert fc.shared_chapters_for("基坑开挖深度", "4.2m") == (
        "technique", "safety", "calc_drawings")


def test_non_shared_fact_returns_empty():
    assert fc.shared_chapters_for("监测点布置方式", "按网格布置") == ()


def test_classify_fact_dimensions_shape():
    d = fc.classify_fact_dimensions("施工总工期", "240")
    assert set(d.keys()) == {"chapter", "fact_attr", "source_kind", "is_shared",
                             "shared_chapters"}
    assert d["chapter"] == "plan"
    assert d["is_shared"] is True
    # shared_chapters 按九大章节序号升序（plan=3, safety=5, emergency=8）
    assert d["shared_chapters"] == fc.shared_chapters_for("施工总工期", "240")
    assert [fc.CHAPTER_NUMBERS[c] for c in d["shared_chapters"]] == sorted(
        fc.CHAPTER_NUMBERS[c] for c in d["shared_chapters"])
    # 非共性事实：shared_chapters 只含主归属章节（完整归属集合，不含复用扩展示例）
    d2 = fc.classify_fact_dimensions("监测点布置方式", "按网格布置")
    assert d2["is_shared"] is False
    assert d2["shared_chapters"] == (d2["chapter"],)
    # 共性事实：shared_chapters = 复用章节 ∪ 主归属章节
    d3 = fc.classify_fact_dimensions("施工总工期", "240")
    assert d3["shared_chapters"] == fc.shared_chapters_for("施工总工期", "240")
    assert d3["chapter"] in d3["shared_chapters"]


# ---------------------------------------------------------------------------
# 6. 危大工程阈值参数抽取
# ---------------------------------------------------------------------------

def test_extract_params_unit_conversion_and_max():
    rows = [
        {"name": "基坑开挖深度", "value": "4200", "value_unit": "mm"},
        {"name": "基坑开挖深度", "value": "3.5", "value_unit": "m"},
    ]
    assert fc.extract_danger_params(rows) == {"depth": 4.2}  # 同参取最大


def test_extract_params_loads_kept_as_is():
    rows = [
        {"name": "施工总荷载", "value": "15", "value_unit": "kN/m²"},
        {"name": "集中线荷载", "value": "20", "value_unit": "kN/m"},
        {"name": "单件起吊重量", "value": "10", "value_unit": "kN"},
    ]
    assert fc.extract_danger_params(rows) == {
        "total_load": 15.0, "line_load": 20.0, "single_weight": 10.0}


def test_extract_params_ignores_bad_rows():
    """非 dict / 无可提取数值的行被忽略（不做任何臆测）。"""
    assert fc.extract_danger_params([
        None, "x", {"name": "基坑开挖深度", "value": "深基坑"},
        {"name": "基坑开挖深度"}, {"value": "6.5"},
    ]) == {}


def test_extract_params_approximate_is_conservative():
    """「约6米多」这类模糊表述仍抽取数值（6.0m）—— 危大判定宁可保守不可漏判。"""
    got = fc.extract_danger_params([
        {"name": "基坑开挖深度", "value": "约6米多", "value_unit": ""}])
    assert got["depth"] == pytest.approx(6.0)


def test_extract_params_from_fact_key():
    assert fc.extract_danger_params([
        {"name": "设计参数", "value": "4.2", "value_unit": "m",
         "fact_key": "foundation_depth"}]) == {"depth": 4.2}


# ---------------------------------------------------------------------------
# 7. 方案类型识别 + 危大/超规模阈值判定（BUG 修复重点）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("depth,hazard,oversize", [
    (2.8, False, False),
    (3.0, True, False),    # ← 3m 即危大（旧实现误判为非危大）
    (4.2, True, False),
    (5.0, True, True),     # ← 5m 即超规模
    (6.2, True, True),
])
def test_pit_threshold_waterline(depth, hazard, oversize):
    """开挖深度分水岭：≥3m 危大，≥5m 超过一定规模（部文附件一）。"""
    r = fc.danger_check("基坑支护工程专项施工方案",
                        [{"name": "基坑开挖深度", "value": str(depth), "value_unit": "m"}])
    c = r["classification"]
    assert c["is_hazardous"] is hazard, f"depth={depth}"
    assert c["is_oversize"] is oversize, f"depth={depth}"


@pytest.mark.parametrize("param,val,expect_oversize", [
    ("height", 8, True), ("height", 7.9, False),
    ("span", 18, True), ("span", 17.9, False),
    ("total_load", 15, True), ("total_load", 14.9, False),
    ("line_load", 20, True), ("line_load", 19.9, False),
])
def test_formwork_threshold_closed_interval(param, val, expect_oversize):
    """「及以上」为闭区间：临界值必须命中（旧实现用 > 会漏判临界值）。"""
    lvl = sc.evaluate_hazard_level("fw_tall", {param: val})
    assert lvl["is_oversize"] is expect_oversize


@pytest.mark.parametrize("name,expect_hazardous", [
    ("基坑支护工程专项施工方案", True),
    ("高支模专项施工方案", True),
    ("塔式起重机安装拆卸专项施工方案", True),
    ("附着式升降脚手架专项施工方案", True),
    ("普通装饰装修施工方案", False),
])
def test_scheme_type_auto_recognition(name, expect_hazardous):
    """方案名称关键词 → 自动判定专项方案类型（六大类危大工程）。"""
    r = fc.danger_check(name, [])
    assert r["classification"]["is_hazardous"] is expect_hazardous


def test_hazard_always_has_no_missing_params():
    """起重机械安装拆卸：本身即危大，缺参不得被伪装成判定依据。"""
    lvl = sc.evaluate_hazard_level("ho_crane", {"crane_capacity": 500})
    assert lvl["is_hazardous"] is True
    assert lvl["is_oversize"] is True
    assert lvl["missing_params"] == []


def test_danger_check_returns_params():
    r = fc.danger_check("基坑支护工程专项施工方案",
                        [{"name": "基坑开挖深度", "value": "6.5", "value_unit": "m"}])
    assert r["threshold_params"] == {"depth": 6.5}
    assert r["classification"]["scheme_name"] == "基坑支护工程专项施工方案"


# ---------------------------------------------------------------------------
# 8. 章节聚合与字段完整性（差集分析）
# ---------------------------------------------------------------------------

def _row(name, value, chapter="", fact_attr="", source_kind="", is_shared=0,
         category="", fact_type=""):
    return {"name": name, "value": value, "chapter": chapter, "fact_attr": fact_attr,
            "source_kind": source_kind, "is_shared": is_shared,
            "category": category, "fact_type": fact_type}


def test_chapter_field_completeness_missing_chapters():
    rows = [_row("工程名称", "XX小区", "overview")]
    comp = fc.chapter_field_completeness(rows)
    assert comp["chapters"]["overview"]["fact_count"] == 1
    # 其余 8 章无事实 → 均列入缺失章节
    assert len(comp["missing_chapters"]) >= 8


def test_chapter_field_completeness_legacy_rows_lazy_derive():
    """历史行无 chapter 列 → 惰性派生兜底，不丢事实。"""
    rows = [{"name": "监测报警值", "value": "累计沉降20mm"}]
    comp = fc.chapter_field_completeness(rows)
    assert comp["chapters"]["safety"]["fact_count"] == 1


def test_chapter_field_completeness_normalizes_punctuation_and_labels():
    rows = [
        _row("适用标准清单", "JGJ 120-2012", "basis"),
        _row("工程名称", "某项目", "overview"),
    ]
    comp = fc.chapter_field_completeness(rows)
    assert "适用标准清单" in comp["chapters"]["basis"]["covered_fields"]
    assert "工程名称" in comp["chapters"]["overview"]["covered_fields"]



def test_nine_chapter_summary_shape():
    rows = [
        _row("工程名称", "XX小区", "overview"),
        _row("执行标准", "JGJ 120", "basis"),
        _row("监测报警值", "20mm", "safety"),
        _row("其它", "x", ""),
    ]
    s = fc.nine_chapter_summary(rows)
    assert len(s["chapters"]) == 9
    assert s["totals"]["facts"] == 4
    assert s["totals"]["uncategorized"] == 1
    assert 0.0 <= s["totals"]["coverage"] <= 1.0


def test_category_map_payload():
    p = fc.category_map_payload()
    assert len(p["chapters"]) == 9
    assert p["fact_attr_titles"]["quantitative"] == "定量事实"
    assert p["source_kind_titles"]["survey"] == "勘察报告提取"


# ---------------------------------------------------------------------------
# 9. 提取管线接入（确定性标注，不改动既有 category 口径）
# ---------------------------------------------------------------------------

def test_apply_fact_dimensions_is_idempotent_and_non_overwriting():
    items = [fe.FactItem(name="基坑开挖深度", value="4.2", value_unit="m",
                         category="tech_param", fact_type="design_param",
                         key="foundation_depth", source="招标文件.pdf")]
    fc.apply_fact_dimensions(items)
    got = (items[0].chapter, items[0].fact_attr, items[0].source_kind, items[0].is_shared)
    assert got == ("overview", "quantitative", "bid_doc", True)
    # 已标注的值不被二次标注覆盖（保留人工归类）
    items[0].chapter = "safety"
    fc.apply_fact_dimensions(items)
    assert items[0].chapter == "safety"


def test_run_post_extract_normalize_applies_dimensions(monkeypatch):
    monkeypatch.setattr(fe.settings, "facts_chapter_classification", True)
    items = [fe.FactItem(name="总工期", value="240", value_unit="日历天",
                         category="schedule", fact_type="schedule")]
    out = fe.run_post_extract_normalize(items)
    assert out[0].chapter == "plan"
    assert out[0].is_shared is True  # 工期为跨章节共性事实


def test_run_post_extract_normalize_disabled_switch(monkeypatch):
    """配置关闭时完全不标注（默认行为不变）。"""
    monkeypatch.setattr(fe.settings, "facts_chapter_classification", False)
    items = [fe.FactItem(name="总工期", value="240", value_unit="日历天",
                         category="schedule")]
    out = fe.run_post_extract_normalize(items)
    assert out[0].chapter == ""


def test_to_db_row_has_four_dimension_columns():
    it = fe.FactItem(name="施工总工期", value="240", value_unit="日历天",
                     category="schedule", source="招标文件.pdf")
    fc.apply_fact_dimensions([it])
    row = it.to_db_row("g", "p", "s", "工期安排")
    # 尾部 4 列 = chapter / fact_attr / source_kind / is_shared
    assert row[-4:] == ("plan", "quantitative", "bid_doc", 1)


# ---------------------------------------------------------------------------
# 10. DB 往返（新增 4 列幂等补列 + 插入 + 读路径惰性派生）
# ---------------------------------------------------------------------------

@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    import app.db as _appdb
    import app.routers.global_facts as gf
    _appdb.DB_PATH = tmp_path / "facts-chapter.sqlite"
    await _appdb.init_db()
    db = await _appdb.get_conn()
    uploads = tmp_path / "uploads" / "facts"
    uploads.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
    yield db
    await db.close()


async def test_migrate_adds_dimension_columns(ctx):
    """新增 4 列 + 章节索引：新库由 schema 建出，旧库由 _migrate 幂等补出。"""
    cur = await ctx.execute("PRAGMA table_info(global_facts)")
    cols = {dict(r)["name"]: dict(r)["dflt_value"] for r in await cur.fetchall()}
    assert cols["chapter"] == "''"
    assert cols["fact_attr"] == "''"
    assert cols["source_kind"] == "''"
    assert cols["is_shared"] == "0"
    # 章节索引（供正文生成按章节精选事实）
    cur = await ctx.execute("PRAGMA index_list(global_facts)")
    names = {dict(r)["name"] for r in await cur.fetchall()}
    assert "idx_global_facts_chapter" in names


async def test_insert_and_lazy_derive_roundtrip(ctx):
    """新写入的行带标注；历史行（无标注）由读路径惰性派生兜底。"""
    it = fe.FactItem(name="基坑开挖深度", value="4.2", value_unit="m",
                     category="tech_param", fact_type="design_param",
                     key="foundation_depth", source="招标文件.pdf")
    fc.apply_fact_dimensions(items := [it])
    row = items[0].to_db_row("g1", "p1", "s1", "技术参数")
    # ✅ 2026-10-06：改走 GLOBAL_FACTS_INSERT_SQL 单一事实源。
    #   本测试此前内联一份 27 列 INSERT 字面量 + 27 个手写占位符 ——
    #   to_db_row 补 is_stale（28 列）后立即 "Incorrect number of bindings"。
    #   这正是本轮要消灭的「同一列清单多份副本」：测试里的副本同样是漂移源。
    await ctx.execute(fe.GLOBAL_FACTS_INSERT_SQL, row)

    # 历史行：仅有 name/content/category（模拟 2026-09-24 之前的旧数据）
    await ctx.execute(
        "INSERT INTO global_facts "
        "(id, project_id, scheme_id, group_id, title, content, category, is_resolved) "
        "VALUES ('legacy-1', 'p1', 's1', 'g1', '监测报警值', "
        "'- **监测报警值**: 累计沉降20mm', 'monitoring', 1)")
    await ctx.commit()

    cur = await ctx.execute("SELECT * FROM global_facts ORDER BY title")
    rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 2

    new = [r for r in rows if r["fact_key"] == "foundation_depth"]
    assert new, "按 fact_key 查不到新写入的事实"
    n = new[0]
    assert n["chapter"] == "overview"
    assert n["fact_attr"] == "quantitative"
    assert n["source_kind"] == "bid_doc"
    assert n["is_shared"] == 1

    legacy = [r for r in rows if r["id"] == "legacy-1"][0]
    assert legacy["chapter"] == ""          # 历史行未标注
    # 读路径惰性派生：不写库，但章节视图/正文精选不会丢这条事实
    dims = fc.dimensions_for_row(legacy)
    assert dims["chapter"] == "safety"      # 监测报警值 → 第五章
    assert dims["fact_attr"] == "quantitative"


async def test_persist_extraction_writes_dimensions(ctx):
    """persist_extraction 的 INSERT 列数与 FactItem.to_db_row 严格一致（新增 4 列后）。"""
    sid, pid = "s1", "p1"
    await ctx.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await ctx.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await ctx.commit()

    items = [fe.FactItem(name="施工总工期", value="240", value_unit="日历天",
                         category="schedule", fact_type="schedule")]
    fc.apply_fact_dimensions(items)
    res = fe.ExtractionResult(
        groups=[fe.FactGroup(title="工期安排", category="schedule", items=items)],
        total_items=1)
    await fe.persist_extraction(res, ctx, pid, sid)
    await ctx.commit()

    cur = await ctx.execute("SELECT * FROM global_facts WHERE scheme_id=?", (sid,))
    rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["chapter"] == "plan"
    assert rows[0]["is_shared"] == 1


# ---------------------------------------------------------------------------
# 11. 正文生成章节优先注入（sse_handlers）
# ---------------------------------------------------------------------------
def test_chapter_key_of_title_matches_nine_chapters():
    from app.routers import sse_handlers as sh
    cases = {
        "1 工程概况": "overview",
        "工程概况": "overview",
        "2 编制依据": "basis",
        "3 施工计划": "plan",
        "3.2 施工部署": "plan",
        "4 施工工艺技术": "technique",
        "4.1 施工方法": "technique",
        "5 施工安全保证措施": "safety",
        "6 施工管理及作业人员配备和分工": "personnel",
        "7 验收要求": "acceptance",
        "8 应急处置措施": "emergency",
        "9 计算书及相关图纸": "calc_drawings",
    }
    for title, expect in cases.items():
        assert sh.chapter_key_of_title(title) == expect, title


def test_chapter_key_of_title_returns_empty_when_unknown():
    """识别不到时不猜（避免错误归类导致事实被挤到尾部而丢失）。"""
    from app.routers import sse_handlers as sh
    assert sh.chapter_key_of_title("") == ""
    assert sh.chapter_key_of_title(None) == ""
    assert sh.chapter_key_of_title("附录") == ""


def test_render_facts_text_chapter_prioritizes_without_dropping():
    """章节内事实前置，补充事实保留在尾部（顺序稳定、总量不丢）。"""
    from app.routers import sse_handlers as sh
    rows = [
        ("工期安排", "总工期", "- **总工期**: 240日历天", 0.9, "plan"),
        ("技术参数", "基坑深度", "- **基坑深度**: 12.5m", 0.9, "overview"),
        ("材料", "混凝土强度", "- **混凝土强度**: C30", 0.9, "technique"),
    ]
    text = sh._render_facts_text(rows, chapter="plan")
    i_plan = text.index("总工期")
    i_over = text.index("基坑深度")
    i_tech = text.index("混凝土强度")
    assert i_plan < i_over < i_tech  # plan 事实前置，其余按原顺序排在后


def test_render_facts_text_chapter_never_drops_facts():
    """预算收紧时，被舍弃的只能是尾部（非本章）事实。"""
    from app.routers import sse_handlers as sh
    rows = [
        ("A组", "A1", "- **A1**: " + "甲" * 200, 0.9, "plan"),
        ("B组", "B1", "- **B1**: " + "乙" * 200, 0.9, "overview"),
        ("C组", "C1", "- **C1**: " + "丙" * 200, 0.9, "technique"),
    ]
    text = sh._render_facts_text(rows, chapter="plan", max_total=400)
    assert "A1" in text           # 本章事实必须注入
    # ✅ 2026-09-27 契约变更：超预算时改为**按比例分配**，
    # 本章事实优先，但其余事实**不再整段丢弃**（旧实现为头部优先
    # break，尾部事实在提示词里完全不可见 → AI 误判「事实缺失」
    # → 读出假性【待补充】）。
    assert "B1" in text and "C1" in text
    assert len(text) <= 400
    # 不传 chapter 时行为完全不变（默认行为）
    text_default = sh._render_facts_text(rows, max_total=2000)
    assert "A1" in text_default and "B1" in text_default and "C1" in text_default


def test_render_facts_text_legacy_rows_ignored_but_kept():
    """历史 3/4 元组行（无 chapter 列）不参与章节分区，但仍完整保留。"""
    from app.routers import sse_handlers as sh
    from app.services.facts_builder import _row_chapter
    rows = [
        ("旧组", "旧事实", "- **旧事实**: 保留"),
        ("旧组2", "旧事实2", "- **旧事实2**: 保留", 0.8),
        ("新组", "新事实", "- **新事实**: 命中", 0.9, "plan"),
    ]
    text = sh._render_facts_text(rows, chapter="plan", max_total=2000)
    assert "新事实" in text and "旧事实" in text and "旧事实2" in text
    assert text.index("新事实") < text.index("旧事实")
    assert _row_chapter(("旧组", "旧事实", "- **旧事实**: 保留")) == ""


async def test_load_facts_rows_returns_chapter_column(ctx):
    """_load_facts_rows 需多读一列 chapter，行结构按长度自适应。"""
    from app.routers import sse_handlers as sh

    sid, pid = "s1", "p1"
    await ctx.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await ctx.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))

    it = fe.FactItem(name="施工总工期", value="240", value_unit="日历天",
                     category="schedule", fact_type="schedule")
    fc.apply_fact_dimensions([it])
    await ctx.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,group_id,group_title,title,content,category,"
        "is_resolved,chapter,fact_attr,source_kind,is_shared) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("new-1", pid, sid, "g1", "工期安排", "施工总工期",
         "- **施工总工期**: 240", "schedule", 1, "plan",
         "quantitative", "bid_doc", 1))
    await ctx.commit()

    rows = await sh._load_facts_rows(ctx, sid)
    assert rows, "未读到可注入事实"
    row = rows[0]
    assert len(row) >= 5
    assert row[4] == "plan"


# ---------------------------------------------------------------------------
# 12. 路由端点：/category-map、/chapters、/danger-check、list_facts 新字段
# ---------------------------------------------------------------------------
async def test_category_map_endpoint(ctx):
    import app.routers.global_facts as gf
    payload = await gf.get_fact_category_map()
    assert len(payload["chapters"]) == 9
    assert payload["chapters"][0]["key"] == "overview"
    assert payload["chapters"][-1]["key"] == "calc_drawings"
    assert "category_to_chapter" in payload
    assert payload["fact_attr_titles"]["quantitative"] == "定量事实"
    assert payload["source_kind_titles"]["survey"] == "勘察报告提取"
    # 九大章节全部有中文名；category 映射目标要么为空串（如 other，无对应章节），
    # 要么落在九大章节内
    keys = {c["key"] for c in payload["chapters"]}
    for ch in payload["chapters"]:
        assert ch["title"]
    for tgt in set(payload["category_to_chapter"].values()):
        assert tgt == "" or tgt in keys, tgt


def _insert_scheme(ctx, sid, pid, name="深基坑支护专项施工方案"):
    return ctx.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (sid, pid, name))


async def test_list_facts_returns_dimension_fields(ctx):
    """list_facts 必须回传四维标注；历史行由读路径惰性派生兜底。"""
    import app.routers.global_facts as gf
    sid, pid = "s1", "p1"
    await ctx.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await _insert_scheme(ctx, sid, pid)
    await ctx.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,group_id,group_title,title,content,category,"
        "is_resolved,chapter,fact_attr,source_kind,is_shared) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("n1", pid, sid, "g1", "技术参数", "基坑开挖深度",
         "- **基坑开挖深度**: 4.5m", "tech_param", 1, "overview",
         "quantitative", "bid_doc", 1))
    # 历史行：无四维标注列
    await ctx.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,group_id,title,content,category,is_resolved) "
        "VALUES (?,?,?,?,?,?,?,1)",
        ("n2", pid, sid, "g2", "混凝土强度等级",
         "- **混凝土强度等级**: C30", "material_mgmt"))
    await ctx.commit()

    res = await gf.list_facts(scheme_id=sid, project_id="", db=ctx)
    items = {it["name"]: it for g in res["groups"] for it in g["items"]}
    assert len(items) == 2

    assert items["基坑开挖深度"]["chapter"] == "overview"
    assert items["基坑开挖深度"]["chapter_title"] == "工程概况"
    assert items["基坑开挖深度"]["fact_attr"] == "quantitative"
    assert items["基坑开挖深度"]["source_kind"] == "bid_doc"
    assert items["基坑开挖深度"]["is_shared"] is True

    # 历史行：库列为空 → 惰性派生兜底，章节视图/正文精选不丢这条事实
    legacy = items["混凝土强度等级"]
    assert legacy["chapter"], "历史行必须有惰性派生的章节归属"
    assert legacy["chapter_title"] == "施工工艺技术"
    assert legacy["fact_attr"] == "quantitative"

    # 章节维度统计（供前端章节视图直接渲染）
    bc = res["stats"]["by_chapter"]
    assert len(bc["chapters"]) == 9
    assert bc["totals"]["facts"] == 2
    assert bc["by_fact_attr"]["quantitative"] == 2
    # 新行来源标注为 bid_doc；历史行未标注 → 按默认「项目文件解析提取」兜底
    assert bc["by_source_kind"]["bid_doc"] == 2


async def test_chapters_endpoint_groups_and_reports_missing(ctx):
    """/chapters 按章节重组事实，并给出应提取字段与缺失字段差集。"""
    import app.routers.global_facts as gf
    sid, pid = "s1", "p1"
    await ctx.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await _insert_scheme(ctx, sid, pid)
    await ctx.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,group_id,group_title,title,content,category,"
        "is_resolved,chapter) VALUES (?,?,?,?,?,?,?,?,1,?)",
        ("c1", pid, sid, "g1", "工程概况", "基坑开挖深度",
         "- **基坑开挖深度**: 4.5m", "tech_param", "overview"))
    await ctx.commit()

    res = await gf.list_facts_by_chapters(scheme_id=sid, project_id="", db=ctx)
    assert len(res["chapters"]) == 9
    overview = [c for c in res["chapters"] if c["key"] == "overview"][0]
    assert overview["order"] == 1
    assert overview["count"] == 1
    assert len(overview["items"]) == 1
    assert overview["items"][0]["name"] == "基坑开挖深度"
    assert overview["items"][0]["value"] == "4.5m"
    # 缺失字段差集：该章必填字段远多于已提取的 1 条
    assert overview["fields"] and overview["missing_fields"]
    assert 0.0 <= overview["coverage"] <= 1.0
    assert res["uncategorized"] == []
    assert res["totals"]["facts"] == 1


async def test_danger_check_endpoint_uses_fact_params(ctx):
    """/danger-check：从全局事实抽取定量参数再对照阈值判定。"""
    import app.routers.global_facts as gf
    sid, pid = "s1", "p1"
    await ctx.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await _insert_scheme(ctx, sid, pid, "深基坑支护与降水工程专项施工方案")
    await ctx.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,group_id,title,content,category,is_resolved,"
        "value_unit,fact_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("d1", pid, sid, "g1", "基坑开挖深度", "- **基坑开挖深度**: 4.5",
         "tech_param", 1, "m", "foundation_depth"))
    await ctx.commit()

    res = await gf.check_danger_scheme(
        {"scheme_name": "深基坑支护与降水工程专项施工方案"},
        scheme_id=sid, project_id="", db=ctx)
    assert res["category_id"] == "foundation_pit"
    assert res["threshold_params"] == {"depth": 4.5}
    c = res["classification"]
    assert c["is_hazardous"] is True      # ≥3m 即危大
    assert c["is_oversize"] is False      # <5m 未超规模
    assert res["missing_params"] == []


async def test_danger_check_unit_conversion_to_meter(ctx):
    """毫米/厘米统一换算为米，再对照阈值（4.2m → 危大不超规模）。"""
    import app.routers.global_facts as gf
    sid, pid = "s1", "p1"
    await ctx.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await _insert_scheme(ctx, sid, pid, "基坑支护专项施工方案")
    await ctx.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,group_id,title,content,category,is_resolved,"
        "value_unit,fact_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("d2", pid, sid, "g1", "基坑深度", "- **基坑深度**: 4200",
         "tech_param", 1, "mm", "foundation_depth"))
    await ctx.commit()

    res = await gf.check_danger_scheme(
        {"scheme_name": "基坑支护专项施工方案"},
        scheme_id=sid, project_id="", db=ctx)
    assert res["threshold_params"] == {"depth": 4.2}
    assert res["classification"]["is_hazardous"] is True
    assert res["classification"]["is_oversize"] is False


async def test_danger_check_requires_name(ctx):
    """无方案名且无补充文本时返回 400（不做无依据判定）。"""
    import app.routers.global_facts as gf
    sid, pid = "s1", "p1"
    await ctx.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await _insert_scheme(ctx, sid, pid, "")  # 空方案名
    await ctx.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        await gf.check_danger_scheme({}, scheme_id=sid, project_id="", db=ctx)
    assert ei.value.status_code == 400

async def test_danger_check_scheme_id_only_loads_name_from_db(ctx):
    """前端主路径（body 空对象 + 仅 query scheme_id）：从库中取方案名正常判定。

    ✅ 2026-10-01 P0 回归锁：400 检查此前位于「从库中取方案名」**之前**，
    只传 scheme_id 的调用恒 400（生产 backend_err.log 多次实证：
    同方案同时段 /chapters 为 200、/danger-check 恒 400）；
    且注释声明「前端只传 scheme_id 时从库中取方案名」与实现矛盾、分支不可达。
    """
    import app.routers.global_facts as gf
    sid, pid = "s1", "p1"
    await ctx.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await _insert_scheme(ctx, sid, pid, "基坑支护专项施工方案")
    await ctx.execute(
        "INSERT INTO global_facts "
        "(id,project_id,scheme_id,group_id,title,content,category,is_resolved,"
        "value_unit,fact_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("d3", pid, sid, "g1", "基坑深度", "- **基坑深度**: 4.5",
         "tech_param", 1, "m", "foundation_depth"))
    await ctx.commit()

    # body 为空对象（与前端 factsApi.dangerCheck({}, schemeId) 逐字一致）
    res = await gf.check_danger_scheme({}, scheme_id=sid, project_id="", db=ctx)
    assert res["category_id"] == "foundation_pit"
    assert res["threshold_params"] == {"depth": 4.5}
    assert res["classification"]["is_hazardous"] is True