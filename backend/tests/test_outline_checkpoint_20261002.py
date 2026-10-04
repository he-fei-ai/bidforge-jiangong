"""护栏：目录侧审核检查点反向增强（2026-10-02 · 第二十五轮）。

背景（生产库 ``preflight_runs`` 实证，scheme=d3c1a897…「装饰装修专项施工方案」）
--------------------------------------------------------------------------
``CMP-01/02/03/04/05/09`` 六条全部命中（3 block），但都不是审核侧误报，而是
**目录侧从未被要求过**：

1. **判据分叉（第三份副本）**：``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS`` 是
   九大章节关键词的第三份手抄副本，与 ``audit_rules`` 注册表**双向分叉**：
   目录侧认「工程概述 / 施工部署 / 组织机构 / 图纸」，审核侧不认。
2. **门控不对齐**：预检 ``check_completeness`` 对 CMP-01~09 **无条件**检查；
   目录侧只在危大时检查，且整段程序化预检被「用户填了编制要求」门控。

本文件锁定的六层
----------------
A. **判据同源（核心不变量）**：目录侧判「缺失」的 rule_id 集合 ⊆ 审核侧
   ``check_completeness`` 判「缺失」的集合（穷举 2000 组标题子集）。
   这是「降低预检问题」的**充分条件** —— 目录侧放行的，预检不会报。
B. **分叉方向锁死**：旧目录侧独有的词（「工程概述」等）**不得**出现在判据里；
   一旦回流即报（本轮踩过的坑：并集方向搞反会让分叉固化）。
C. **门控对齐**：非危大专项方案也必须检查九大法定章节；
   ``outline_checkpoint_check=False`` 完整回退到旧行为。
D. **监测章节类别门控**：SAF-06 与 ``check_safety`` 同门控
   （``MONITOR_CATEGORIES`` 交集），不做「危大即要求」的粗判。
E. **接线（源码静态锁）**：三处渲染点注入、``**`` 置于关键字实参之后、
   开关默认值 True、变量契约已登记、提示词占位符独占行。
F. **内容侧 STD-02/STD-05 编号级自检**：编制依据章须含 GB 55xxx 与本类别
   现行标准**编号**（与预检同源），且不得用裸编号检测器误判。

⚠️ 判据指向（AGENTS §5.14）：对 ``sse_handlers`` 的断言走**源码扫描**且锚定
真正生效的那段装配代码，不做「碰巧含关键字的一切字符串」的全文件匹配。
"""
from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
from app.config import settings
from app.services import outline_checkpoint as ocp
from app.services.audit_rules import RULE_MAP, get_rule
from app.services.content_checkpoint import (
    _BARE_CODE_RE,
    _basis_standard_findings,
    checkpoint_selfcheck,
)
from app.services.preflight_engine import (
    MONITOR_CATEGORIES,
    SAF03_TITLE_KEYWORDS,
    SAF04_TITLE_KEYWORDS,
    SAF05_TITLE_KEYWORDS,
    SAF06_MONITOR_TITLE_KEYWORDS,
    TRC03_TITLE_KEYWORDS,
    PreflightContext,
    check_completeness,
    check_safety,
    check_traceability,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SSE_HANDLERS = REPO_ROOT / "app" / "routers" / "sse_handlers.py"

#: 旧目录侧「独有的词」（审核侧不认）。回流即分叉复现。
_LEGACY_ONLY_WORDS = ("工程概述", "施工部署", "组织机构")

_TITLE_POOL = [
    "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
    "人员分工", "验收要求", "应急处置措施", "计算书及相关图纸", "监测方案",
    "工程概述", "施工部署", "组织机构", "图纸", "附图与节点详图",
    "施工准备", "质量保证", "环保文明施工", "施工计划与进度", "现场平面布置",
    "人员配备与分工", "监测监控", "边坡支护", "计算书及验算", "应急预案",
]


def _audit_missing(titles: list[str], scheme_name: str) -> set[str]:
    """审核侧对同一组标题会报「章节缺失」的 rule_id 集合。"""
    secs = [{"id": str(i), "title": t, "content": "占位正文", "parent_id": None}
            for i, t in enumerate(titles)]
    ctx = PreflightContext(scheme_id="x", scheme_name=scheme_name,
                           scheme_type="施工组织设计", word_budget=30000,
                           sections=secs)
    out: set[str] = set()
    for f in check_completeness(ctx):
        # CMP-09 的追加条款（无计算过程）是**正文侧**判定，目录阶段无从判
        if f["rule_id"] == "CMP-09" and "未找到与" not in f["detail"]:
            continue
        out.add(f["rule_id"])
    for f in check_traceability(ctx):
        if f["rule_id"] == "TRC-03":
            out.add("TRC-03")
    for f in check_safety(ctx):
        # SAF-06 只取「未检出监测监控方案**章节**」子句；另一子句判的是正文
        if f["rule_id"] == "SAF-06" and "未检出监测监控方案章节" in f["detail"]:
            out.add("SAF-06")
    return out


def _outline_missing(titles: list[str], scheme_name: str) -> set[str]:
    ol = [{"title": t} for t in titles]
    return {f["rule_id"] for f in ocp.check_outline_chapters(
        ol, is_hazardous=True,
        need_monitor=ocp.needs_monitor_chapter(scheme_name, ""))}


def _sse_source() -> str:
    return SSE_HANDLERS.read_text(encoding="utf-8")
# ---------------------------------------------------------------------------
# A. 判据同源（核心不变量）
# ---------------------------------------------------------------------------

class TestParityWithAudit:
    """目录侧判据必须是审核侧判据的**子集**（不漏放行 = 能真正降低预检问题）。"""

    def test_no_over_report_in_exhaustive_search(self):
        """穷举 2000 组标题子集：目录侧判缺失 ⊆ 审核侧判缺失。

        这是本轮**最重要**的一条：反向（审核侧报而目录侧不报）说明目录侧
        放行了预检会报的目录，即分叉未消除；正向（目录侧报而审核侧不报）
        会产生假缺项、白跑外科补齐。
        """
        rnd = random.Random(20261002)
        nm = "深基坑支护专项施工方案"      # 基坑类 → 需监测（与预检同门控）
        over: list = []
        for _ in range(2000):
            titles = rnd.sample(_TITLE_POOL, rnd.randint(0, 13))
            extra = _outline_missing(titles, nm) - _audit_missing(titles, nm)
            if extra:
                over.append((titles, extra))
        assert not over, f"目录侧多报了审核侧不会报的规则（判据分叉）：{over[:3]}"

    def test_legacy_only_words_are_caught_as_missing(self):
        """旧目录侧放行的「工程概述 / 施工部署 / 组织机构」现在必须判缺失。

        A/B 对照：这正是生产库 CMP-01/03/06 high 的成因。
        """
        titles = ["工程概述", "编制依据", "施工部署", "组织机构", "施工工艺技术",
                  "安全保证措施", "验收要求", "应急处置措施", "计算书及相关图纸"]
        assert {"CMP-01", "CMP-03", "CMP-06"} <= _outline_missing(titles, "装饰装修")
        assert {"CMP-01", "CMP-03", "CMP-06"} <= _audit_missing(titles, "装饰装修")

    def test_production_scenario_non_hazardous_is_checked(self):
        """生产库场景：非危大专项方案同样受九章检查（门控对齐）。"""
        titles = ["施工准备", "质量保证措施", "环保文明施工"]
        missing = ocp.outline_coverage_missing(
            [{"title": t} for t in titles], is_hazardous=False)
        assert len(missing) >= 8, "非危大方案也必须检查九大法定章节"

    def test_complete_outline_passes(self):
        """完整目录零 findings（不得把正常目录判成缺失）。"""
        titles = ["工程概况", "编制依据", "施工计划", "施工工艺技术",
                  "安全保证措施", "人员分工", "验收要求", "应急处置措施",
                  "计算书及相关图纸", "监测方案", "附图与节点详图"]
        assert ocp.check_outline_chapters([{"title": t} for t in titles],
                                          is_hazardous=True,
                                          need_monitor=True) == []

    def test_second_level_titles_count(self):
        """二级/三级标题命中也算覆盖（与 preflight 全层级扫描同口径）。"""
        ol = [{"title": "一、总体说明", "children": [
            {"title": "1.1 工程概况与周边环境", "children": []}]}]
        rids = {f["rule_id"] for f in ocp.check_outline_chapters(
            ol, is_hazardous=False)}
        assert "CMP-01" not in rids, "二级标题含法定关键词即算覆盖"

    def test_empty_outline_reports_all(self):
        """空目录不静默通过（「不猜」原则：宁可多提醒）。

        非危大 = 九章 CMP + TRC-03（附图 / 节点详图）；SAF-06 监测章不参与。
        """
        rids = {f["rule_id"] for f in ocp.check_outline_chapters(
            [], is_hazardous=False)}
        assert rids == {f"CMP-{i:02d}" for i in range(1, 10)} | {"TRC-03"}

    def test_fail_soft_on_internal_error(self, monkeypatch):
        """内部异常降级为已收集结论，绝不抛业务异常。"""
        def boom(*_a, **_k):
            raise RuntimeError("注入异常")
        monkeypatch.setattr(ocp, "collect_titles", boom)
        assert ocp.check_outline_chapters([{"title": "工程概况"}]) == []

# ---------------------------------------------------------------------------
# B. 分叉方向锁死
# ---------------------------------------------------------------------------

class TestNoForkReintroduction:
    """旧目录侧独有的词不得回流进判据（并集方向搞反会让分叉固化）。"""

    @pytest.mark.parametrize("word", _LEGACY_ONLY_WORDS)
    def test_legacy_only_word_not_in_any_keywords(self, word):
        for spec in ocp.required_chapter_specs(True, need_monitor=True):
            assert word not in spec["keywords"], (
                f"「{word}」是旧目录侧独有的词（审核侧不认），不得进入判据")

    def test_keywords_superset_of_audit_registry(self):
        """每条必备章节的判据 ⊇ 审核注册表关键词（方向只能是「更宽」）。"""
        for spec in ocp.required_chapter_specs(True, need_monitor=True):
            rule = get_rule(spec["rule_id"])
            if rule is None or not rule.keywords:
                continue
            for kw in rule.keywords:
                assert kw in spec["keywords"], (
                    f"{spec['rule_id']} 判据漏了注册表关键词 {kw}")

    def test_engine_predicates_not_registered_for_cmp(self):
        """CMP-* 不得并入引擎常量（CMP-09 会因此被目录侧多报）。"""
        for rid in ocp._ENGINE_PREDICATE_KEYWORDS:
            assert not rid.startswith("CMP-"), (
                f"{rid} 的标题匹配由 check_completeness 直接读注册表 keywords，"
                f"并入引擎常量会让目录侧判据宽于审核侧")

    def test_audit_rules_keywords_cover_engine_predicates(self):
        """audit_rules 注册表已按引擎实际谓词补齐（防下游按注册表推导时分叉）。"""
        pairs = {
            "SAF-03": SAF03_TITLE_KEYWORDS, "SAF-04": SAF04_TITLE_KEYWORDS,
            "SAF-05": SAF05_TITLE_KEYWORDS, "SAF-06": SAF06_MONITOR_TITLE_KEYWORDS,
            "TRC-03": TRC03_TITLE_KEYWORDS,
        }
        for rid, engine_kw in pairs.items():
            rule_kw = set(get_rule(rid).keywords)
            missing = [k for k in engine_kw if k not in rule_kw]
            assert not missing, f"{rid} 注册表漏登记引擎谓词：{missing}"

# ---------------------------------------------------------------------------
# C. 门控对齐 + 开关回退
# ---------------------------------------------------------------------------

class TestGateAlignment:
    def test_default_switch_on(self):
        assert settings.outline_checkpoint_check is True

    def test_kwargs_empty_when_switch_off(self, monkeypatch):
        from app.routers import sse_handlers as sh
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)
        assert sh._outline_checkpoint_kwargs("深基坑", "深基坑", True) == {}

    def test_kwargs_non_empty_when_on(self):
        from app.routers import sse_handlers as sh
        kw = sh._outline_checkpoint_kwargs("深基坑支护专项施工方案", "深基坑", True)
        assert set(kw) == {"outline_checkpoint_block"}
        assert "审核检查点前置要求" in kw["outline_checkpoint_block"]

    def test_kwargs_fail_soft(self, monkeypatch):
        from app.routers import sse_handlers as sh
        def boom(**_k):
            raise RuntimeError("注入异常")
        monkeypatch.setattr(sh._ocp, "build_outline_checkpoint_block", boom)
        assert sh._outline_checkpoint_kwargs("深基坑", "深基坑", True) == {}

    def test_non_hazardous_still_checked_in_coverage(self):
        """九章检查对非危大方案恒定参与（不再要求填写编制要求）。"""
        from app.routers import sse_handlers as sh
        ol = [{"title": "施工准备"}, {"title": "质量保证"}]
        _cov, missing = sh._check_requirements_coverage("", ol, False, basis=None)
        assert any("法定必备章节" in m for m in missing)

    def test_switch_off_disables_nine_chapter_check(self, monkeypatch):
        from app.routers import sse_handlers as sh
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)
        ol = [{"title": "施工准备"}, {"title": "质量保证"}]
        _cov, missing = sh._check_requirements_coverage("", ol, False, basis=None)
        assert not any("法定必备章节" in m for m in missing)

    def test_dangerous_ten_chapter_check_preserved(self):
        """危大 10 章的**结构性**检查未被本轮削弱。"""
        from app.routers import sse_handlers as sh
        ol = [{"title": t} for t in (
            "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
            "人员分工", "验收要求", "应急处置措施", "计算书及相关图纸", "监测方案")]
        _cov, missing = sh._check_requirements_coverage(
            "", ol, True, basis=None)
        assert not any("危大工程必备章节" in m for m in missing)

    def test_dangerous_alias_derived_from_single_source(self):
        """_DANGEROUS_REQUIRED_KEYWORDS 已由唯一出口派生（不再是独立副本）。"""
        from app.routers import sse_handlers as sh
        assert sh._DANGEROUS_REQUIRED_KEYWORDS == sh._dangerous_required_keywords()


# ---------------------------------------------------------------------------
# D. 监测章节类别门控
# ---------------------------------------------------------------------------

class TestMonitorGate:
    def test_pit_scheme_requires_monitor(self):
        assert ocp.needs_monitor_chapter("深基坑支护专项施工方案", "") is True

    def test_decoration_scheme_does_not(self):
        """装饰装修不属 MONITOR_CATEGORIES → 不要求监测章节（否则是假缺项）。"""
        assert ocp.needs_monitor_chapter("装饰装修专项施工方案", "") is False

    def test_gate_matches_preflight_categories(self):
        """门控集合与 check_safety 用的 MONITOR_CATEGORIES 同一个常量。"""
        assert set(ocp._pf.MONITOR_CATEGORIES) == set(MONITOR_CATEGORIES)

    def test_non_monitor_category_no_saf06(self):
        ol = [{"title": t} for t in (
            "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
            "人员分工", "验收要求", "应急处置措施", "计算书及相关图纸")]
        rids = {f["rule_id"] for f in ocp.check_outline_chapters(
            ol, is_hazardous=True, need_monitor=False)}
        assert "SAF-06" not in rids

    def test_monitor_category_requires_saf06(self):
        ol = [{"title": t} for t in (
            "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
            "人员分工", "验收要求", "应急处置措施", "计算书及相关图纸")]
        rids = {f["rule_id"] for f in ocp.check_outline_chapters(
            ol, is_hazardous=True, need_monitor=True)}
        assert "SAF-06" in rids
# ---------------------------------------------------------------------------
# E. 接线（源码静态锁）
# ---------------------------------------------------------------------------

class TestWiring:
    def test_three_render_sites_inject(self):
        """三处渲染点（短方案 / 分步一级 / 目录审核）都注入检查点块。"""
        src = _sse_source()
        assert src.count("_outline_checkpoint_kwargs(") >= 4, (
            "短方案 / level1 / review 三处渲染 + 1 处定义")

    def test_kwargs_unpacked_last(self):
        """`**` 必须置于所有关键字实参之后（否则 SyntaxError）。"""
        src = _sse_source()
        for m in re.finditer(r"render\(\"outline_\w+\"", src):
            seg = src[m.start():m.start() + 1400]
            kw = seg.find("**_outline_checkpoint_kwargs(")
            if kw < 0:
                continue
            tail = seg[kw + len("**_outline_checkpoint_kwargs("):]
            close = tail.find("))")
            assert not re.search(r"\w+=", tail[:close]), (
                "检查点 kwargs 之后仍有具名实参 → 顺序非法")
            return
        pytest.fail("未找到 outline_* 的 render 调用点")

    def test_config_switch_declared(self):
        src = (REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
        assert "outline_checkpoint_check: bool = True" in src

    def test_variable_contract_registered(self):
        src = (REPO_ROOT / "app" / "services" / "ai" / "prompts"
               / "_registry.py").read_text(encoding="utf-8")
        for key in ("outline_short_system", "outline_level1_system",
                    "outline_review_system"):
            block = src.split(f'"{key}": [', 1)[1].split("]", 1)[0]
            assert "outline_checkpoint_block" in block, f"{key} 未登记变量契约"

    def test_prompt_placeholder_on_own_line(self):
        """占位符须独占一行（未传时整行丢弃 = 逐字回退）。"""
        src = (REPO_ROOT / "app" / "services" / "ai" / "prompts"
               / "outline.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            if "outline_checkpoint_block" in line:
                assert line.strip() == "{outline_checkpoint_block}", (
                    f"占位符未独占一行，回退时会留下孤行：{line!r}")

    def test_prompt_renders_and_block_absent_when_off(self, monkeypatch):
        from app.routers import sse_handlers as sh
        from app.services.ai.prompts import render
        base = dict(scheme_name="深基坑支护专项施工方案", scheme_type="深基坑",
                    construction_scope="", scheme_basis="", standards_text="",
                    project_facts="")
        on = render("outline_short_system", **base,
                    **sh._outline_checkpoint_kwargs(
                        base["scheme_name"], base["scheme_type"], True))
        assert "审核检查点前置要求" in on
        assert "{outline_checkpoint_block}" not in on
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)
        off = render("outline_short_system", **base,
                     **sh._outline_checkpoint_kwargs(
                         base["scheme_name"], base["scheme_type"], True))
        assert "审核检查点前置要求" not in off, "关闭开关后不得残留检查点段"
        assert "{outline_checkpoint_block}" not in off

    def test_review_prompt_shares_same_block(self):
        """目录审核提示词与生成提示词共用同一份清单（同一词表）。"""
        from app.routers import sse_handlers as sh
        from app.services.ai.prompts import render
        kw = sh._outline_checkpoint_kwargs("深基坑支护专项施工方案", "深基坑", True)
        p = render("outline_review_system", scheme_name="深基坑支护专项施工方案",
                   scheme_type="深基坑", construction_scope="", scheme_basis="",
                   is_dangerous="是", project_facts="", outline_json="[]", **kw)
        assert "审核检查点前置要求" in p
        for spec in ocp.required_chapter_specs(True, need_monitor=True):
            assert spec["rule_id"] in p
# ---------------------------------------------------------------------------
# F. 内容侧 STD-02 / STD-05 编号级自检
# ---------------------------------------------------------------------------

class TestBasisStandardSelfcheck:
    """编制依据章须含标准**编号**（词级检查不足，预检判的是编号）。"""

    def test_compliant_basis_passes(self):
        assert _basis_standard_findings(
            "依据 GB 55034-2022 与 GB 50210-2018 编制。",
            scheme_name="装饰装修专项施工方案", scheme_type="施工组织设计") == []

    def test_missing_mandatory_code_reported(self):
        rids = {f["rule_id"] for f in _basis_standard_findings(
            "依据 GB 50210-2018 编制。",
            scheme_name="装饰装修专项施工方案", scheme_type="施工组织设计")}
        assert "STD-02" in rids

    def test_missing_category_code_reported(self):
        rids = {f["rule_id"] for f in _basis_standard_findings(
            "依据 GB 55034-2022 编制。",
            scheme_name="装饰装修专项施工方案", scheme_type="施工组织设计")}
        assert "STD-05" in rids

    def test_no_context_skips_std05(self):
        """无方案上下文时跳过 STD-05（无法判类别就不猜），STD-02 照常。"""
        rids = {f["rule_id"] for f in _basis_standard_findings(
            "依据 GB 55034-2022 编制。")}
        assert "STD-05" not in rids
        assert "STD-02" not in rids

    def test_bare_code_regex_cannot_be_reused(self):
        """A/B 对照：裸编号检测器提取不到带年号编号（不能用于「有没有引用」）。"""
        assert not _BARE_CODE_RE.search("GB 55034-2022")
        assert _basis_standard_findings(
            "依据 GB 55034-2022 与 GB 50210-2018 编制。",
            scheme_name="装饰装修专项施工方案", scheme_type="施工组织设计") == []

    def test_wired_into_selfcheck_for_basis_chapter(self):
        f = checkpoint_selfcheck(
            "本章依据 GB 50210-2018 编制。", chapter_key="basis",
            scheme_name="装饰装修专项施工方案", scheme_type="施工组织设计")
        assert "STD-02" in {x["rule_id"] for x in f}

    def test_selfcheck_forwards_scheme_context_to_basis_check(self):
        """A/B 反向验证发现的缺口：STD-05 依赖 ``scheme_type`` 转发。

        ``TestBasisStandardSelfcheck`` 的全部用例都**直调** ``_basis_standard_findings``，
        而 ``test_wired_into_selfcheck_for_basis_chapter`` 只断言 STD-02 ——
        STD-02 不需要方案上下文，于是当 ``checkpoint_selfcheck`` 停止转发
        ``scheme_name`` / ``scheme_type`` 时，**没有任何用例会失败**（A/B A7 实测
        0 定向失败）。本用例走 selfcheck 端到端、专门断言必须转发上下文的
        STD-05，让断链必然暴露。
        """
        f = checkpoint_selfcheck(
            "依据 GB 55034-2022 编制。", chapter_key="basis",
            scheme_name="装饰装修专项施工方案", scheme_type="施工组织设计")
        assert "STD-05" in {x["rule_id"] for x in f}

    def test_not_wired_for_other_chapters(self):
        """仅编制依据章启用（其它章不报 STD-02/STD-05）。"""
        f = checkpoint_selfcheck(
            "本章依据 GB 50210-2018 编制。", chapter_key="technique",
            scheme_name="装饰装修专项施工方案", scheme_type="施工组织设计")
        assert not {"STD-02", "STD-05"} & {x["rule_id"] for x in f}

    def test_selfcheck_backward_compatible_defaults(self):
        """既有调用点逐字不变（可选参数向后兼容红线）。"""
        assert isinstance(checkpoint_selfcheck("占位正文", chapter_key="basis"), list)

    def test_call_site_passes_scheme_context(self):
        """接线：sse_handlers 必须传方案名/类型（否则 STD-05 永远跳过）。"""
        src = _sse_source()
        seg = src[src.find("checkpoint_selfcheck("):][:900]
        assert "scheme_name=" in seg and "scheme_type=" in seg

    def test_block_states_title_keyword_rule(self):
        """提示词必须明确告知「审核按标题关键词匹配」。"""
        block = ocp.build_outline_checkpoint_block("深基坑", "深基坑", True)
        assert "标题关键词" in block
        assert "不得改写掉法定词本身" in block
