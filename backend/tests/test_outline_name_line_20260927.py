"""目录生成「方案名称主线 + 连续性/全面性校验」回归测试（2026-09-27）

对应需求的四项核心要求：

1. 要求一（引用方案名称 + 解析提取内容）
   - ``_rank_sections_by_basis`` / ``_rank_facts_by_basis``：把与方案名称相关的
     提取项/事实**前置**（兑现一直零读取点的 ``outline_basis_relevance`` 配置，
     并激活 ``scheme_basis.rank_by_relevance`` 死代码），且**一条不丢**；
   - ``_scheme_is_dangerous``：危大判定 = type 命中 **或** 名称字面命中
     （config.outline_name_basis 注释早已承诺，实现此前缺失）。
2. 要求三（连续性）：``check_outline_continuity`` —— 层级跳级 / 有父无子 /
   同名章节 / 编号错位 四类问题均可检出。
3. 要求四（全面性）：``analyze_name_coverage`` / ``find_redundant_titles``，
   且缺口能并入 ``_check_requirements_coverage`` 的 missing → 复用外科式补齐。
"""
import pytest

import app.routers.sse_handlers as sh
from app.services.outline_quality import (
    analyze_name_coverage, check_outline_continuity, find_redundant_titles,
    render_continuity_notice, render_coverage_notice,
)
from app.services.scheme_basis import parse_scheme_basis

BASIS = parse_scheme_basis("基坑支护及土方开挖专项施工方案")


# ===========================================================================
# 要求一：方案名称主线
# ===========================================================================
class TestSchemeNameLine:
    def test_name_only_triggers_dangerous(self):
        """名称写「深基坑支护」而 type 选「其它」→ 必须判危大（修复前为 False）。"""
        assert sh._scheme_is_dangerous(
            {"name": "深基坑支护专项施工方案", "type": "其它"}) is True

    def test_type_still_wins(self):
        assert sh._scheme_is_dangerous(
            {"name": "装饰装修专项施工方案", "type": "深基坑"}) is True

    def test_no_false_positive_on_plain_name(self):
        assert sh._scheme_is_dangerous(
            {"name": "装饰装修专项施工方案", "type": "其它"}) is False

    def test_switch_off_falls_back_to_type_only(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_name_basis", False, raising=False)
        assert sh._scheme_is_dangerous(
            {"name": "深基坑支护专项施工方案", "type": "其它"}) is False

    def test_basis_obj_respects_switch(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_name_basis", False, raising=False)
        assert sh._scheme_basis_obj({"name": "深基坑支护专项施工方案"}) is None


class TestRelevanceFrontLoad:
    """相关性前置：顺序即见性（固定预算下相关项必须先被看到），但绝不丢数据。"""

    SECTIONS = (
        "# 提取项目结果（自动提取，供后续步骤参考）\n\n"
        "## 商务条款与报价\n甲\n\n"
        "## 施工工艺与技术\n本工程基坑支护采用钻孔灌注桩 + 止水帷幕\n\n"
        "## 质量管理与验收\n乙\n"
    )

    def test_related_section_moves_first(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_basis_relevance", True, raising=False)
        out, hit = sh._rank_sections_by_basis(self.SECTIONS, BASIS)
        assert hit >= 1
        assert out.index("施工工艺与技术") < out.index("商务条款与报价"), out

    def test_no_section_lost(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_basis_relevance", True, raising=False)
        out, _ = sh._rank_sections_by_basis(self.SECTIONS, BASIS)
        for kw in ("商务条款与报价", "施工工艺与技术", "质量管理与验收"):
            assert kw in out, kw

    def test_switch_off_keeps_original_order(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_basis_relevance", False, raising=False)
        out, hit = sh._rank_sections_by_basis(self.SECTIONS, BASIS)
        assert hit == 0
        assert out == self.SECTIONS

    def test_no_relation_signal_keeps_order(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_basis_relevance", True, raising=False)
        unrelated = "## A\n完全无关内容\n\n## B\n另一段无关内容\n"
        out, hit = sh._rank_sections_by_basis(unrelated, BASIS)
        assert hit == 0 and out == unrelated

    def test_fact_groups_front_loaded_without_splitting(self, monkeypatch):
        """事实按**组**前置：组内原序、组不重复渲染（避免重复组标题）。"""
        monkeypatch.setattr(sh.settings, "outline_basis_relevance", True, raising=False)
        rows = [
            ("商务信息", "报价", "投标报价 1000 万"),
            ("技术参数", "支护形式", "钻孔灌注桩 + 止水帷幕"),
            ("技术参数", "开挖深度", "6.5 m"),
            ("商务信息", "工期", "180 天"),
        ]
        out, hit = sh._rank_facts_by_basis(rows, BASIS)
        assert hit >= 1
        groups = [r[0] for r in out]
        assert groups[0] == "技术参数"
        assert len(groups) == len(rows), "一条都不能少"
        # 同一组必须连续（否则 _render_facts_text 会渲染出重复 "### 组名"）
        blocks = [groups[0]] + [groups[i] for i in range(1, len(groups))
                                 if groups[i] != groups[i - 1]]
        assert set(blocks) == {"技术参数", "商务信息"} and len(blocks) == 2, groups



# ===========================================================================
# 要求三：目录连续性
# ===========================================================================
class TestContinuity:
    CLEAN = [
        {"title": "工程概况", "id": "1", "level": 1,
         "children": [{"title": "概况与特点", "id": "1.1", "level": 2, "children": []}]},
        {"title": "施工工艺", "id": "2", "level": 1, "children": []},
    ]

    def test_clean_tree_passes(self):
        r = check_outline_continuity(self.CLEAN)
        assert r["ok"] is True, r
        assert r["nodes"] == 3 and r["max_level"] == 2

    def test_level_gap_detected(self):
        tree = [{"title": "A", "level": 1,
                 "children": [{"title": "B", "level": 3, "children": []}]}]
        r = check_outline_continuity(tree)
        assert r["ok"] is False
        assert r["level_gaps"][0]["declared"] == 3 and r["level_gaps"][0]["expected"] == 2

    def test_numbering_mismatch_detected(self):
        tree = [{"title": "A", "id": "7", "level": 1, "children": []}]
        r = check_outline_continuity(tree)
        assert r["numbering_mismatch"] == [
            {"path": "1", "id": "7", "expected": "1", "title": "A"}]

    def test_duplicate_titles_detected(self):
        tree = [{"title": "施工部署", "children": []},
                {"title": "施工准备", "children": [
                    {"title": "施工部署", "children": []}]}]
        r = check_outline_continuity(tree)
        assert len(r["duplicate_titles"]) == 1
        assert r["duplicate_titles"][0]["first"] == "1"

    def test_empty_parent_detected(self):
        tree = [{"title": "A", "children": [{"title": "", "children": []}]}]
        assert check_outline_continuity(tree)["empty_parents"]

    def test_empty_and_garbage_input_safe(self):
        assert check_outline_continuity([])["ok"] is True
        assert check_outline_continuity(["x", 1, None])["ok"] is True
        assert check_outline_continuity(None)["nodes"] == 0

    def test_notice_renders_chinese(self):
        tree = [{"title": "A", "level": 1,
                 "children": [{"title": "B", "level": 3, "children": []}]}]
        assert "层级跳级" in render_continuity_notice(check_outline_continuity(tree))
        assert render_continuity_notice({"ok": True}) == ""


# ===========================================================================
# 要求四：全面性（方案名称 → 目录）
# ===========================================================================
class TestNameCoverage:
    OUTLINE = [
        {"title": "工程概况", "children": []},
        {"title": "土方开挖及支护施工", "children": [
            {"title": "分层分段开挖", "children": []}]},
    ]

    def test_covered_when_keywords_land_anywhere(self):
        cov = analyze_name_coverage(BASIS, self.OUTLINE)
        assert cov["evaluated"] is True
        assert any(m["item"] == "土方开挖" and m["covered"] for m in cov["matrix"])

    def test_missing_items_reported_with_dimension_label(self):
        cov = analyze_name_coverage(BASIS, [{"title": "工程概况", "children": []}])
        assert cov["covered"] is False
        assert any(x.startswith("施工工序·") for x in cov["missing"])
        assert "施工工序" in render_coverage_notice(cov["missing"])

    def test_none_basis_is_not_evaluated(self):
        cov = analyze_name_coverage(None, self.OUTLINE)
        assert cov["evaluated"] is False and cov["covered"] is True
        assert cov["missing"] == []

    def test_unparseable_name_is_not_judged(self):
        """名称无任何可解析维度 → 不得判目录不合规（不得编造 / 不得误伤）。"""
        cov = analyze_name_coverage(parse_scheme_basis(""), self.OUTLINE)
        assert cov["evaluated"] is False and cov["covered"] is True

    def test_generic_chapters_not_redundant(self):
        assert find_redundant_titles(BASIS, self.OUTLINE) == []

    def test_unrelated_chapter_reported_as_redundant(self):
        tree = [{"title": "工程概况", "children": []},
                {"title": "精装包厢软包施工", "children": []}]
        assert [r["title"] for r in find_redundant_titles(BASIS, tree)] == \
            ["精装包厢软包施工"]



class TestCoverageWiredIntoRequirementsCheck:
    """要求四的落地出口：名称覆盖缺口并入既有外科式补齐的 missing。"""

    def test_name_gaps_merged_into_missing(self):
        covered, missing = sh._check_requirements_coverage(
            "", [{"title": "工程概况", "children": []}], False, basis=BASIS)
        assert covered is False
        assert any("施工工序" in m for m in missing)

    def test_fully_covered_skips_review(self):
        covered, missing = sh._check_requirements_coverage(
            "", [{"title": "工程概况", "children": []},
                 {"title": "基坑支护施工", "children": [
                     {"title": "土方开挖", "children": []}]}],
            False, basis=BASIS)
        assert covered is True, missing

    def test_basis_none_keeps_legacy_behaviour(self):
        covered, missing = sh._check_requirements_coverage(
            "必须包含监测方案", [{"title": "工程概况", "children": []}], False)
        assert covered is False and "必须包含监测方案" in missing

    def test_switch_off_disables_name_check(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_name_coverage_check", False,
                            raising=False)
        covered, missing = sh._check_requirements_coverage(
            "", [{"title": "工程概况", "children": []}], False, basis=BASIS)
        assert covered is True and missing == []


class TestQualityReportAttached:
    """要求三/四的「生成后自动校验 + 输出报告」出口。"""

    def test_report_written_into_review_obj(self):
        review = {"passed": True, "suggestions": ["原建议"]}
        rep = sh._attach_quality_report(
            [{"title": "工程概况", "level": 1, "children": [
                {"title": "概况", "level": 3, "children": []}]}], BASIS, review)
        assert set(rep) == {"continuity", "name_coverage", "redundant_titles"}
        assert rep["continuity"]["ok"] is False
        assert review["quality"] is rep
        assert review["suggestions"] == ["原建议"], "校验告警不得污染修复建议"

    def test_never_raises_on_garbage(self):
        assert isinstance(sh._attach_quality_report(["x", None], None, {}), dict)


# ===========================================================================
# 遗留项修复：P1-3 标准章节模板注入 / P1-4 全层级宽松匹配
#           / P2-2 节点上限配置化 / P2-3 关键词库维护约定
# ===========================================================================
"""services → routers 层级依赖护栏（P2-1 / T-2）

背景：2026-09-27 之前，`services/numbering.py` 为让「落库正文子标题编号」与
「导出成稿」逐字同源，在运行时执行 ``from app.routers.export import (...)`` ——
形成 **services 依赖 routers** 的层级倒置（AGENTS.md §2 目录结构中 services 层
不得依赖 routers），并让核心解析算法住在全仓最大的路由文件里。
现相关纯函数已整体下沉到 ``services/content_blocks.py``，依赖方向恢复为
routers → services。

本护栏用 AST 静态扫描 ``app/services/**``：任何 ``app.routers.*`` 的 import
（模块级或函数级）都会失败，防止倒置重新长出来。
"""
import ast
import pathlib

import app

ROOT = pathlib.Path(app.__file__).parent
SERVICES = ROOT / "services"

#: 已知的遗留层级倒置（services → routers）。**逐条登记，不做静默豁免**：
#: 新增同类 import 必须先修既有项或显式登记，避免护栏被"再加一条"架空。
KNOWN_LAYER_LEAKS: dict[str, str] = {
    "consistency_scanner.py": "L92 from app.routers.sse_handlers import _build_facts_text"
                              "（全局事实文本构建函数住在 sse_handlers；与本轮已修的"
                              " numbering→export 同族，待独立批次下沉到 services）",
}


def _router_imports(path: pathlib.Path) -> list[str]:
    """返回该文件里所有指向 app.routers.* 的 import（**AST 级，天然忽略注释**）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("app.routers"):
                names = ",".join(a.name for a in node.names)
                hits.append(f"L{node.lineno}: from {node.module} import {names}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("app.routers"):
                    hits.append(f"L{node.lineno}: import {a.name}")
    return hits


def test_services_never_import_routers():
    """services 层不得依赖 routers 层（层级倒置护栏，P2-1）。

    已登记的遗留项（KNOWN_LAYER_LEAKS）仍视为失败，除非显式更新登记表 ——
    这样"修一处、挪一条"的凑数做法无法通过。
    """
    leaks: list[str] = []
    known: list[str] = []
    for p in sorted(SERVICES.rglob("*.py")):
        rel = p.name
        for hit in _router_imports(p):
            if rel in KNOWN_LAYER_LEAKS and KNOWN_LAYER_LEAKS[rel].split()[0] in hit:
                known.append(f"{rel} {hit}")
            else:
                leaks.append(f"{p.relative_to(ROOT)} {hit}")
    assert not leaks, ("services 层不得新增对 routers 的依赖（层级倒置）：\n  "
                       + "\n  ".join(leaks))
    # 已登记项必须仍然存在（登记表不得过期/失效），数量与登记一致
    assert len(known) == len(KNOWN_LAYER_LEAKS), (
        f"KNOWN_LAYER_LEAKS 已过期：实际 {len(known)} 处，登记 {len(KNOWN_LAYER_LEAKS)} 处")


def test_content_blocks_is_the_single_implementation():
    """导出与编号两侧必须共用同一份实现（不得各写一份）。"""
    import app.routers._chart_pipeline as cp
    import app.routers.export as exp
    from app.services import content_blocks as cb

    assert exp._parse_content_blocks is cb._parse_content_blocks
    assert exp._compute_subheading is cb._compute_subheading
    assert exp._strip_title_number is cb._strip_title_number
    assert exp._strip_duplicate_leading_title is cb._strip_duplicate_leading_title
    assert cp.parse_fence_line is cb.parse_fence_line
    assert cp.read_fenced_block is cb.read_fenced_block


def test_numbering_imports_from_services_not_routers():
    """numbering 的子标题算法来源必须是 services.content_blocks（AST 级判定）。"""
    import app.services.numbering as n

    path = pathlib.Path(n.__file__)
    assert not _router_imports(path), "numbering 仍从 routers 取实现"
    src = path.read_text(encoding="utf-8")
    assert "from app.services.content_blocks import" in src


def test_moved_functions_behave_identically():
    """行为契约：下沉后编号算法输出与预期逐字一致（不依赖 export 即可调用）。"""
    from app.services.content_blocks import _compute_subheading, _parse_content_blocks

    content = "## 2.1 材料要求\n\n正文A\n\n## 2.2 设备配置\n\n正文B\n"
    blocks = _parse_content_blocks(content)
    assert [b["type"] for b in blocks if b["type"] == "heading"] == ["heading"] * 2
    counters: dict = {}
    text, style = _compute_subheading("2", 1, 2, counters, "材料要求", "",
                                      has_children=False)
    assert text.startswith("2.") and style, (text, style)
    # 有 DB 子章节时降级到 body 命名空间（与导出 parity 硬约束）
    text2, _ = _compute_subheading("2", 1, 2, {}, "材料要求", "", has_children=True)
    assert not text2.startswith("2."), text2


class TestTemplateInjection:
    """P1-3：`outline_templates` 的 24 套标准章节模板此前解析出却从未进提示词。"""

    def _text(self):
        return sh._outline_scheme_basis({"name": "深基坑支护专项施工方案", "type": "深基坑"})

    def test_template_name_injected(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_template_inject", True, raising=False)
        text = self._text()
        assert "标准章节模板：" in text, text
        assert "基坑" in text.split("标准章节模板：")[1][:20], text

    def test_switch_off_removes_template_line(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_template_inject", False, raising=False)
        assert "标准章节模板：" not in self._text()

    def test_general_template_still_named(self, monkeypatch):
        """未命中专项模板时也给出模板名（general 有名字，不静默）。"""
        monkeypatch.setattr(sh.settings, "outline_template_inject", True, raising=False)
        assert "标准章节模板：" in sh._outline_scheme_basis({"name": "装饰装修专项施工方案"})

    def test_unparseable_name_yields_no_block(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_template_inject", True, raising=False)
        assert sh._outline_scheme_basis({"name": ""}) == ""


class TestAllLevelRelaxedMatching:
    """P1-4：宽松规则（≥4 字公共子串）扩展到全层级；严格规则仍只看一级。"""

    def test_deep_title_satisfies_requirement(self):
        covered, missing = sh._check_requirements_coverage(
            "深基坑开挖支护专项方案", [{"title": "工程概况", "children": [
                {"title": "深基坑开挖支护要点", "children": []}]}])
        assert covered is True, missing

    def test_dangerous_chapter_still_requires_l1(self):
        """危大必备是**结构性**要求：二级标题提到「监测」不算有独立监测方案章。"""
        covered, missing = sh._check_requirements_coverage(
            "", [{"title": "工程概况", "children": [
                {"title": "监测数据采集", "children": []}]}], True)
        assert covered is False
        assert any("监测方案" in m for m in missing), missing

    def test_l1_hit_still_covers(self):
        covered, _ = sh._check_requirements_coverage(
            "必须包含计算书及图纸", [{"title": "计算书及相关图纸", "children": []}])
        assert covered is True


class TestNodeCapConfigurable:
    """P2-2：生成链路节点上限配置化（默认 500 = 原行为）。"""

    def test_default_matches_legacy(self):
        from app.config import Settings
        assert Settings().outline_generate_max_nodes == 500
        assert sh.OUTLINE_GENERATE_MAX_NODES == 500

    def test_bound_to_constant(self):
        assert sh.OUTLINE_GENERATE_MAX_NODES == int(
            sh.settings.outline_generate_max_nodes)

    def test_under_limit_passes(self):
        big = {"outline": [{"title": f"章{i}", "children": [
            {"title": f"节{i}-{j}", "children": []} for j in range(3)]}
            for i in range(20)]}      # 80 节点
        assert sh._outline_validate_fn(big) == []

    def test_over_limit_flagged(self):
        big = {"outline": [{"title": f"章{i}", "children": [
            {"title": f"节{i}-{j}", "children": []} for j in range(30)]}
            for i in range(20)]}      # 620 节点 > 500
        issues = sh._outline_validate_fn(big)
        assert issues, "超过 500 节点必须被判非法（与修复前一致）"


class TestKeywordLibraryDiscipline:
    """P2-3：把 `scheme_basis` 文件头的维护约定变成可执行护栏。"""

    def test_no_single_char_keywords(self):
        from app.services import scheme_basis as sb
        for lib_name in ("PROCESS_KEYWORDS", "TECHNIQUE_KEYWORDS", "OBJECT_KEYWORDS"):
            for kw in getattr(sb, lib_name):
                assert len(str(kw).strip()) >= 2, f"{lib_name} 含单字词：{kw!r}"

    def test_no_duplicates_within_library(self):
        from app.services import scheme_basis as sb
        for lib_name in ("PROCESS_KEYWORDS", "TECHNIQUE_KEYWORDS", "OBJECT_KEYWORDS"):
            kws = [str(k) for k in getattr(sb, lib_name)]
            assert len(kws) == len(set(kws)), f"{lib_name} 有重复条目"

    def test_plain_names_do_not_falsely_match(self):
        """非专项方案名不得误命中（文件头约定的「反例」护栏）。"""
        for name in ("", "   ", "公司管理制度", "员工手册", None, 123):
            b = parse_scheme_basis(name)  # type: ignore[arg-type]
            assert b.process_steps == [] and b.techniques == [] and b.objects == [], name

    def test_real_schemes_do_match(self):
        b = parse_scheme_basis("深基坑支护及土方开挖专项施工方案")
        assert b.process_steps and b.objects, b.as_dict()
        # 该名称字面不含工艺词（"支护" 属工序），techniques 为空是**正确**行为
        assert b.techniques == [], b.techniques

    def test_technique_bearing_name_matches_all_dimensions(self):
        b = parse_scheme_basis("地下连续墙深基坑支护专项施工方案")
        assert b.techniques and b.process_steps and b.objects, b.as_dict()

