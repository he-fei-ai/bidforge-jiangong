# -*- coding: utf-8 -*-
"""R29（2026-10-02）检查点反哺 · 目录生成与正文生成阶段护栏

背景（生产库 findings 实证：R27 收口 5 项后剩余的 P0/P1）
---------------------------------------------------------
- **TRC-01（block）**：生成侧自检只判「计算」这个**词**，而预检
  ``check_traceability`` 判的是**公式与参数代入过程**（``_FORMULA_RES``）。
  词过了而过程没有 → 生成侧自检全绿、预检照报 block。
- **CON-06（medium）**：跨章节段落搬运。**结构上无法在提示词预防** ——
  生成单章时模型看不到其他章节的正文。生产库 3 条 CON-06 全部是
  「骨架归一后相似度 100%」（整段照抄，连数字都没换），属评审硬伤。
- **STD-03（low）**：标准库覆盖不足 → 真实有效规范被报成「未收录」，
  属审核侧误报（补库即消除，不改判据强度）。

三条全部按「**判据同源**」落地：生成侧直接 import 预检侧的判据函数 /
常量，**不在生成侧重抄正则与阈值**。全部新增检查：零 AI、
fail-soft（异常只 warning 不上抛，绝不污染生成链路）。

⚠️ 判据方向约束（与第二十五轮同源）：生成侧判据必须**等于或略宽于**
审核侧，使「生成侧判无问题」蕴含「预检也判无问题」。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from app.services import content_checkpoint as cc
from app.services import duplicate_detection as dd
from app.services import preflight_engine as pe
from app.services import standards_registry as sr
from app.services.preflight_engine import (
    CALC_TITLE_KEYWORDS,
    PreflightContext,
    check_traceability,
    has_calc_process,
)

APP = Path(__file__).resolve().parents[1] / "app"
CC_PATH = APP / "services" / "content_checkpoint.py"
SSE_PATH = APP / "routers" / "sse_handlers.py"

#: 有「计算」字样、**无任何公式或参数代入** → 应报 TRC-01
NO_FORMULA = (
    "移动式脚手架稳定性计算书\n\n"
    "本节对移动式脚手架进行稳定性计算，结论详见附表。\n"
    "经验收合格后方可投入使用，满足规范要求。"
)
#: 含明确公式与参数代入 → 不应报 TRC-01
WITH_FORMULA = (
    "移动式脚手架稳定性计算\n\n"
    "站载 N = 12.5 × 0.8 = 10.0kN，小于允许值 18.0kN，满足要求。\n"
    "立杆受力验算：q = 0.6 × 1.2 = 0.72kN/m。"
)

COPY_TEXT = "本施工现场实施总体布置统筹管理，现场成本控制目标为不超过预算的95%。" * 4
OTHER_TEXT = "本章规定施工工艺流程与质量控制要点，进场材料须逐批报验。" * 4


def _selfcheck_trc01(title: str = "", chapter_key: str = "", content: str = "") -> list:
    """跑生成侧自检，只取 TRC-01 finding。

    ⚠️ 先断言「本节确实被识别为计算书章节」—— 否则用例会因判据判否而
    **空转通过**（``not []`` 恒真），测不出任何东西。
    """
    assert cc._is_calc_section(title, chapter_key), (
        f"样本前提不成立：「{title}」/「{chapter_key}」未被判为计算书章节")
    out = cc.checkpoint_selfcheck(
        content, chapter_key=chapter_key, section_title=title,
        is_hazardous_basis=False)
    return [f for f in out if f.get("rule_id") == "TRC-01"]


def _preflight_trc01(sections: list) -> list:
    """跑预检，只取 TRC-01 finding。"""
    ctx = PreflightContext(scheme_id="s", sections=sections)
    return [f for f in check_traceability(ctx) if f.get("rule_id") == "TRC-01"]


# ===========================================================================
# 一、TRC-01 判据同源（P0）
# ===========================================================================
class TestCalcProcessPredicateParity:
    """P0：生成侧自检与预检必须共用同一公式判据，不允许重抄正则。"""

    def test_has_calc_process_is_preflight_alias(self):
        """``has_calc_process`` 必须就是预检私有的 ``_has_calc_process``
        （同一函数对象），而非一份独立实现。"""
        assert pe.has_calc_process is pe._has_calc_process

    def test_calc_process_findings_calls_shared_predicate(self, monkeypatch):
        """判据同源的最强证据：换掉预检判据，生成侧结论随之改变。

        若生成侧重抄了正则，本用例会失败 —— 打补丁换掉预检判据后
        生成侧仍按自己的正则给出相反结论。
        """
        monkeypatch.setattr(pe, "has_calc_process", lambda text: False)
        # 打补丁后，即使正文含真实公式也应报 TRC-01
        assert _selfcheck_trc01("稳定性验算", "", WITH_FORMULA)
        assert _selfcheck_trc01("稳定性验算", "", NO_FORMULA)

    def test_no_local_formula_regex_in_content_checkpoint(self):
        """content_checkpoint 不得定义本地公式正则（分叉病根的静态锁）。"""
        src = CC_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tg in node.targets:
                    if isinstance(tg, ast.Name):
                        names.add(tg.id)
        assert "_FORMULA_RES" not in names, "生成侧重抄了公式正则清单"
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "compile"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            pat = node.args[0].value
            if not isinstance(pat, str):
                continue
            # 「数字 + 运算符」形态才是公式判据；[×xX]{2,} 是占位标记，不算
            if ("\u00d7" in pat and "\\d" in pat) or \
               ("\uff0f" in pat and "\\d" in pat):
                offenders.append(node.lineno)
        assert not offenders, f"生成侧重抄了公式判据正则：行 {offenders}"

    def test_calc_process_findings_calls_preflight_module(self):
        """静态锁：``_calc_process_findings`` 必须引用共享判据
        ``has_calc_process``。"""
        src = CC_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_calc_process_findings")
        assert "has_calc_process(" in ast.get_source_segment(src, fn)

    def test_trc01_reported_when_no_formula(self):
        # 标题通道（命中 CALC_TITLE_KEYWORDS）
        assert _selfcheck_trc01("稳定性验算", "", NO_FORMULA)
        # 章节归类通道（chapter_key）
        assert _selfcheck_trc01("计算书", "calc_drawings", NO_FORMULA)
        # 仅归类通道可用（标题无关键词）
        assert _selfcheck_trc01("平面布置图", "calc_drawings", NO_FORMULA)

    def test_trc01_not_reported_when_formula_present(self):
        assert has_calc_process(WITH_FORMULA)  # 样本本身确含公式
        assert not _selfcheck_trc01("稳定性验算", "", WITH_FORMULA)
        assert not _selfcheck_trc01("计算书", "calc_drawings", WITH_FORMULA)

    def test_trc01_finding_shape(self):
        f = _selfcheck_trc01("稳定性验算", "", NO_FORMULA)[0]
        assert f["rule_id"] == "TRC-01"
        assert f["severity"] == "medium"
        assert f["source"] == "selfcheck"
        assert f["checkpoint"]
        assert f["suggestion"]


class TestIsCalcSectionParity:
    """P0：「这节算不算计算书章节」也必须与预检同源。

    ⚠️ 实测分叉：预检 ``CALC_TITLE_KEYWORDS`` 含「承载力计算」「安全系数」，
    而章节归类补表只到「计算书/验算」—— 只按 chapter_key 判会让
    「承载力计算」这类章节漏检，与预检分叉。
    """

    @pytest.mark.parametrize("kw", CALC_TITLE_KEYWORDS)
    def test_every_preflight_keyword_recognized(self, kw):
        """预检认的每一个标题关键词，生成侧也必须认（零漏检）。"""
        assert cc._is_calc_section(kw, ""), f"「{kw}」未被生成侧识别"

    def test_chapter_key_shortcut(self):
        """章节归类已确认时，无需标题关键词（覆盖「平面布置图」类标题）。"""
        assert cc._is_calc_section("", "calc_drawings")
        assert cc._is_calc_section("平面布置图", "calc_drawings")

    def test_non_calc_title_rejected(self):
        for title in ("施工工艺流程", "安全保证措施", "验收要求",
                      "材料进场计划", ""):
            assert not cc._is_calc_section(title, "")

    def test_non_calc_chapter_key_rejected(self):
        for key in ("safety", "technique", "basis", "emergency", ""):
            assert not cc._is_calc_section("施工工艺流程", key)

    def test_union_not_intersection(self):
        """两个来源任一命中即判计算书章节（并集，非交集）。"""
        assert cc._is_calc_section("计算书", "")
        assert cc._is_calc_section("", "calc_drawings")
        assert cc._is_calc_section("计算书", "calc_drawings")

    def test_fail_soft_on_constant_read_failure(self, monkeypatch):
        """常量读取失败时 fail-soft 回落，绝不抛异常。"""
        import builtins

        real = builtins.__import__

        def _blocked(name, *a, **kw):
            if name == "app.services.preflight_engine":
                raise RuntimeError("boom")
            return real(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        assert cc._is_calc_section("计算书", "") is False
        assert cc._is_calc_section("", "calc_drawings") is True

    def test_shared_boundary_titiles_outside_keywords(self):
        """两侧**共同不认**的标题（如「稳定性计算」）—— 生成侧同样不认。

        这锁住的是「生成侧不得比审核侧更宽」的方向：若生成侧额外认
        「稳定性计算」，会出现生成侧自检报 TRC-01 而预检不认该章节的
        假缺项。
        """
        for title in ("稳定性计算", "计算", "受力分析", "抗风计算"):
            if any(k in title for k in CALC_TITLE_KEYWORDS):
                continue  # 命中关键词的标题不在此断言范围
            assert not cc._is_calc_section(title, ""), (
                f"「{title}」预检不认，生成侧不得更宽（会产生假缺项）")


class TestTrc01EndToEndParity:
    """P0 端到端：生成侧自检与预检对同一批章节给出一致的 TRC-01 结论。"""

    def test_both_report_when_one_calc_section_lacks_process(self):
        """预检 TRC-01 语义：整体有过程、个别章节缺失时逐章报。
        生成侧自检必须对同一章节同样报出。"""
        sections = [
            {"id": "s1", "title": "12.1 移动式脚手架稳定性验算",
             "content": NO_FORMULA},
            {"id": "s2", "title": "13 计算书", "content": WITH_FORMULA},
        ]
        pf = _preflight_trc01(sections)
        assert pf, "预检应报 TRC-01（样本前提）"
        assert pf[0]["section_id"] == "s1"
        # 生成侧对同一章节必须同样报出
        assert _selfcheck_trc01("12.1 移动式脚手架稳定性验算", "", NO_FORMULA)
        # 有公式的那章两侧都不报
        assert not _selfcheck_trc01("13 计算书", "", WITH_FORMULA)
        assert all(f["section_id"] != "s2" for f in pf)

    @pytest.mark.parametrize("title", [
        "稳定性验算", "承载力计算", "安全系数分析", "受力计算书",
    ])
    def test_generation_side_recognizes_every_preflight_calc_title(self,
                                                                    title):
        """核心不变量：**生成侧判据 ⊆ 审核侧判据（不漏检）**。

        预检按 ``CALC_TITLE_KEYWORDS`` 判「这节是不是计算书章节」，
        生成侧必须同样判 —— 否则出现「预检报 TRC-01 而自检静默」的漏检。
        """
        assert cc._is_calc_section(title, ""), (
            f"预检认「{title}」为计算书章节，生成侧漏判（TRC-01 会漏报）")


# ===========================================================================
# 二、CON-06 跨章节段落搬运（P1）
# ===========================================================================
class TestCrossSectionCopyFindings:
    """P1：CON-06 是唯一**结构上无法在提示词预防**的检查点。

    生成单章时模型看不到其他章节正文，system 级「禁止成段雷同」只能提高
    概率、无法保证。故该判据挂**生成后自检**，且必须复用预检的同一检测器。
    """

    def test_exact_copy_detected(self):
        secs = [{"id": "a", "title": "1.1", "content": COPY_TEXT},
                {"id": "b", "title": "4.2", "content": COPY_TEXT}]
        out = cc.cross_section_copy_findings(secs, new_section_id="b")
        assert len(out) == 1
        f = out[0]
        assert f["rule_id"] == "CON-06"
        assert f["severity"] in ("medium", "high")
        assert f["source"] == "selfcheck"
        assert f["checkpoint"]
        assert f["evidence"]

    def test_new_section_filter_excludes_history(self):
        """只报涉及本章的搬运组 —— 不把历史搬运重复报出。"""
        secs = [{"id": "a", "title": "1", "content": COPY_TEXT},
                {"id": "b", "title": "2", "content": COPY_TEXT},
                {"id": "c", "title": "3", "content": OTHER_TEXT}]
        assert cc.cross_section_copy_findings(secs, new_section_id="c") == []
        assert cc.cross_section_copy_findings(secs, new_section_id="b")

    def test_only_reports_once_per_new_section(self):
        """本章与多个历史章节雷同也只报一条（避免重复扣分）。"""
        secs = [{"id": "b", "title": "2", "content": COPY_TEXT},
                {"id": "h1", "title": "1", "content": COPY_TEXT},
                {"id": "h2", "title": "3", "content": COPY_TEXT}]
        out = cc.cross_section_copy_findings(secs, new_section_id="b")
        assert len(out) == 1

    def test_no_false_positive_on_unrelated_content(self):
        secs = [{"id": "a", "title": "1", "content": OTHER_TEXT},
                {"id": "b", "title": "2", "content": COPY_TEXT}]
        assert cc.cross_section_copy_findings(secs, new_section_id="b") == []

    def test_uses_preflight_detector_single_source(self, monkeypatch):
        """判据同源的最强证据：换掉预检检测器，生成侧结论随之改变。

        若生成侧重抄了相似度阈值 / 骨架归一规则，本用例会失败。
        """
        called = []

        def _sentinel(secs, limit=None, min_similarity=None):
            called.append(len(secs))
            return []

        monkeypatch.setattr(dd, "find_cross_section_copies", _sentinel)
        out = cc.cross_section_copy_findings(
            [{"id": "a", "content": COPY_TEXT},
             {"id": "b", "content": COPY_TEXT}], new_section_id="b")
        assert out == [], "应透传共享检测器结果，不得本地重算"
        assert called == [2], "必须调用共享检测器（否则是本地副本）"

    def test_limit_respected(self):
        secs = [{"id": "a", "content": COPY_TEXT},
                {"id": "b", "content": COPY_TEXT},
                {"id": "c", "content": COPY_TEXT}]
        assert len(cc.cross_section_copy_findings(
            secs, new_section_id="c", limit=1)) <= 1

    def test_fail_soft_on_bad_input(self):
        """坏输入一律返回空列表，绝不抛异常（fail-soft）。"""
        assert cc.cross_section_copy_findings(None) == []
        assert cc.cross_section_copy_findings([{}]) == []
        assert cc.cross_section_copy_findings(
            [{"id": "a"}, {"id": "b"}], new_section_id="b") == []

    def test_fail_soft_on_detector_failure(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(dd, "find_cross_section_copies", _boom)
        assert cc.cross_section_copy_findings(
            [{"id": "a", "content": COPY_TEXT},
             {"id": "b", "content": COPY_TEXT}], new_section_id="b") == []

    def test_finding_shape(self):
        secs = [{"id": "a", "title": "1", "content": COPY_TEXT},
                {"id": "b", "title": "2", "content": COPY_TEXT}]
        f = cc.cross_section_copy_findings(secs, new_section_id="b")[0]
        for k in ("rule_id", "checkpoint", "severity", "source",
                  "message", "suggestion", "evidence"):
            assert k in f, f"缺少字段 {k}"


# ===========================================================================
# 三、STD-03 标准库覆盖（P1 误报消除）
# ===========================================================================
class TestStandardsRegistryDecorationCoverage:
    """P1：装饰保温类此前缺 3 个现行规范，导致预检 STD-03 恒报「未收录」
    而实际标准真实有效 —— 属审核侧误报（库覆盖不足），补库即消除。
    """

    CORE_DECORATION_CODES = ("GB 50209-2010", "GB 50222-2017", "GB 50009-2012")

    def test_decoration_category_contains_core_standards(self):
        codes = {s.code for s in sr.CATEGORY_STANDARDS.get("装饰保温", [])}
        for code in self.CORE_DECORATION_CODES:
            assert code in codes, f"装饰保温类缺现行规范 {code}（STD-03 会误报）"

    def test_decoration_codes_are_current_not_abolished(self):
        """补入的编号必须是**现行有效**条目，不能与废止清单冲突。"""
        current = {s.code for s in sr.CATEGORY_STANDARDS.get("装饰保温", [])}
        for code in self.CORE_DECORATION_CODES:
            assert code in current

    def test_registry_version_bumped(self):
        # ✅ 2026-10-07（R51）：openstd 实证补入装饰装修材料限量 4 条 + 废止 2 条。
        assert sr.STANDARD_DB_VERSION == "2026.10.7"

    def test_get_standards_text_mentions_decoration_standards(self):
        """注入正文的编制依据清单必须包含补入的 3 个规范。"""
        text = sr.get_standards_text("装饰装修专项施工方案", "装饰保温")
        for code in self.CORE_DECORATION_CODES:
            assert code in text, f"编制依据清单缺 {code}"

    def test_still_allows_genuinely_unknown_code(self):
        """补库不等于放宽判据：杜撰编号仍应被 STD-03 判为未收录。"""
        bogus = "GB 99999-2099"
        all_codes = set()
        for group in sr.CATEGORY_STANDARDS.values():
            all_codes.update(s.code for s in group)
        all_codes.update(s.code for s in sr.BASE_STANDARDS)
        assert bogus not in all_codes


# ===========================================================================
# 四、接线静态锁（sse_handlers._persist_section）
# ===========================================================================
class TestCrosscheckWiring:
    """接线护栏：CON-06 必须挂在**生成后自检**、锁在开关后面、DB 读有判空。"""

# ===========================================================================
# 六、CON-04 占位符后的**中文**计量单位吸收（生产库证据驱动）
# ===========================================================================
class TestPlaceholderTrailingChineseUnits:
    """生产库 CON-04 实证：单位词表此前只有拉丁计量单位，中文单位全部漏收。

    改写后遗留孤立单位、句子读不通：
      「堆放区面积【待补充：堆放区面积】平方米」→「…确定平方米」
      「每周清运不少于【待补充：清运频次】次」  →「…确定次」
    应吸收为括号注记（与既有 ``深度【待定】m`` → ``（m）`` 同一机制）。
    """

    @pytest.mark.parametrize("text,expect", [
        ("堆放区面积【待补充：堆放区面积】平方米",
         "堆放区面积按设计文件及现场实际确定（平方米）"),
        ("每周清运不少于【待补充：清运频次】次",
         "每周清运不少于按设计文件及现场实际确定（次）"),
        ("开挖深度【待定】米", "开挖深度按设计文件及现场实际确定（米）"),
        ("基坑容积【待补充：容积】立方米",
         "基坑容积按设计文件及现场实际确定（立方米）"),
        ("钢筋重量【待定】公斤", "钢筋重量按设计文件及现场实际确定（公斤）"),
        ("养护时间【待定】小时", "养护时间按设计文件及现场实际确定（小时）"),
        ("墙面涂刷【待定】遍", "墙面涂刷按设计文件及现场实际确定（遍）"),
    ])
    def test_chinese_unit_absorbed_into_parentheses(self, text, expect):
        out, fixes = cc.rewrite_placeholder_marks(text)
        assert out == expect
        assert len(fixes) == 1 and fixes[0]["fixed"]

    def test_latin_units_not_regressed(self):
        """既有拉丁单位吸收行为必须逐字保持（新增分支不得挤掉旧分支）。"""
        cases = {
            "深度【待定】m": "深度按设计文件及现场实际确定（m）",
            "板厚【待定】mm": "板厚按设计文件及现场实际确定（mm）",
            "混凝土强度【待定】MPa": "混凝土强度按设计文件及现场实际确定（MPa）",
            "工期【待定】天": "工期按设计文件及现场实际确定（天）",
            "面积【待定】㎡": "面积按设计文件及现场实际确定（㎡）",
        }
        for text, expect in cases.items():
            out, _ = cc.rewrite_placeholder_marks(text)
            assert out == expect, text

    def test_no_orphan_unit_left_after_rewrite(self):
        """改写后不得遗留「条件短语 + 裸单位」的读不通形态。"""
        orphan = re.compile(
            r"按设计文件及现场实际确定"
            r"(平方米|立方米|公斤|小时|米|遍|次(?!日))(?![）)])")
        texts = [
            "堆放区面积【待补充：堆放区面积】平方米",
            "每周清运不少于【待补充：清运频次】次",
            "开挖深度【待定】米，宽度【待定】米",
            "基坑容积【待补充：容积】立方米",
        ]
        for t in texts:
            out, _ = cc.rewrite_placeholder_marks(t)
            assert not orphan.search(out), f"遗留孤立单位: {out}"

    def test_rewrite_idempotent_with_chinese_units(self):
        """新增单位吸收后仍须幂等（二次改写零变化）。"""
        for t in ("堆放区面积【待补充：堆放区面积】平方米",
                  "每周清运不少于【待补充：清运频次】次"):
            once, _ = cc.rewrite_placeholder_marks(t)
            twice, _ = cc.rewrite_placeholder_marks(once)
            assert once == twice

    def test_ci_next_day_word_not_absorbed(self):
        """``次日``（next day）是常用词，不得被当成单位吸收。"""
        out, _ = cc.rewrite_placeholder_marks("【待定】次日恢复施工")
        assert out == "按设计文件及现场实际确定次日恢复施工"
        assert "（次）" not in out

    def test_week_and_month_words_deliberately_not_absorbed(self):
        """``周边`` / ``月末`` 会以「周」「月」起首 —— 刻意不吸收。

        这是写入实现的取舍（误收风险高于收益），此处按**行为**锁定，
        不锁正则字面量（避免后人改写法即误伤）。
        """
        out, _ = cc.rewrite_placeholder_marks("影响范围【待定】周边区域")
        assert "（周" not in out
        out2, _ = cc.rewrite_placeholder_marks("完成时间【待定】月末")
        assert "（月" not in out2


# ===========================================================================
# 七、STD-03 基号豁免：只该报真正作废的编号
# ===========================================================================
class TestStd03BaseNumberExemption:
    """生产库 STD-03 实证：报出的 6 个「未收录编号」里有 5 个是真实现行规范。

    当前代码经基号豁免后**只报 ``GB 18581``**（该族已被 GB 30981-2014 替代，
    报它是对的）。本组锁死「真实现行规范一律放行、作废编号照报」两端。
    """

    @pytest.mark.parametrize("code", [
        "GB 55034", "GB 55032", "GB 50210", "GB 50325-2020", "GB 12523-2011",
    ])
    def test_current_codes_not_reported(self, code):
        assert sr.is_known_standard(code) or sr.is_known_base_number(code), code

    def test_obsolete_code_still_reported(self):
        """GB 18581 族已被 GB 30981-2014 替代 —— 报它是对的，不是误报。"""
        assert not sr.is_known_standard("GB 18581")
        assert not sr.is_known_base_number("GB 18581")

    def test_standalone_fabricated_code_still_reported(self):
        """库外、非已知基号、且非废止族的杜撰编号必须照报。"""
        for bad in ("GB 99999", "GB 55999-2099", "JGJ 9999"):
            assert not sr.is_known_standard(bad)
            assert not sr.is_known_base_number(bad), bad

    @classmethod
    def setup_class(cls):
        cls.sse = SSE_PATH.read_text(encoding="utf-8")

    def test_import_present(self):
        assert "cross_section_copy_findings" in self.sse

    def test_switch_read_with_default_true(self):
        assert '_crosscheck_dup_on' in self.sse
        assert '"content_crosscheck_duplicate", True' in self.sse

    def test_gated_by_selfcheck_and_switch(self):
        """必须双重门控：总自检开关 AND 本项独立开关（关闭完整回退）。

        R52（2026-10-07）：CON-01 数值一致性自检与搬运检测**共享快照构建**，
        外层门控为合取 ``if _crosscheck_dup_on or _crosscheck_values_on:``；
        搬运调用仍由 ``_crosscheck_dup_on``（+ 20 字性能门槛）**单独**门控 ——
        两个独立开关互不连带。
        """
        assert 'if _crosscheck_dup_on or _crosscheck_values_on:' in self.sse
        assert ('if _crosscheck_dup_on and len(content or "") >= 20:'
                in self.sse), "搬运调用必须仍由 _crosscheck_dup_on 单独门控"

    def test_called_after_checkpoint_selfcheck(self):
        """顺序锁：CON-06 必须在 checkpoint_selfcheck 之后执行
        （结果 append 到同一份 _ck_findings，顺序错会导致覆盖或漏合并）。"""
        i_sc = self.sse.find("checkpoint_selfcheck(")
        i_dx = self.sse.find("cross_section_copy_findings(")
        assert i_sc > 0 and i_dx > 0
        assert i_dx > i_sc, "CON-06 检测必须排在 checkpoint_selfcheck 之后"

    def test_result_merged_into_selfcheck_findings(self):
        """必须并入 _ck_findings（而不是丢弃或另开通道）。"""
        assert "_ck_findings.extend(cross_section_copy_findings(" in self.sse

    def test_db_read_has_none_guard(self):
        """R13：``db.execute()`` 可能返回 None，必须判空后再 fetchall。"""
        # P2 修复（2026-10-04）：跨章搬运检测已改为内存 snapshot 增量对比，
        # sentinel 从旧的 `_dup_secs.append` 更新为 `_dup_secs = list(...)`；
        # 但 None-guard 语义完全不变：`_dup_cur` 为 None 时仍回退到空列表。
        seg = self.sse[self.sse.find("_dup_cur"):
                       self.sse.find("_dup_secs = list")]
        assert "if _dup_cur is not None else []" in seg

    def test_fail_soft_guarded(self):
        """异常只打 WARNING，绝不阻断正文落库。"""
        seg = self.sse[self.sse.find("_crosscheck_dup_on"):
                       self.sse.find("if _ck_findings:")]
        assert "except Exception:" in seg
        assert "exc_info=True" in seg

    def test_current_section_content_overridden(self):
        """本章尚未落库，必须用内存最终正文参与比对（否则本章正文为空）。

        ✅ P2 修复（2026-10-04 · O(N²) 性能）：跨章搬运检测改为按 section_id
        对 snapshot 直接赋值 —— `_crossdup_snapshot[section_id] = {..., "content": content, ...}`
        —— 语义等价于旧的 `_d["content"] = content`（当前章节的最终正文
        会覆盖/写入到快照里参与比对）。这里改断言为新的快照赋值形式。
        """
        seg = self.sse[self.sse.find("_dup_rows"):
                       self.sse.find("if _ck_findings:")]
        assert "_crossdup_snapshot[section_id]" in seg
        # 快照字典里必须包含 content / id / title 三键，且 content 用当前最终正文
        _snap_idx = seg.find("_crossdup_snapshot[section_id]")
        _snap_block = seg[_snap_idx:_snap_idx + 400]
        assert '"content": content' in _snap_block
        assert '"id": section_id' in _snap_block

    def test_run_outside_write_lock(self):
        """必须在锁**外**做（锁外只读）—— 置于 ``async with _db_write_lock`` 之前。"""
        i_dup = self.sse.find("cross_section_copy_findings(")
        i_lock = self.sse.find("async with _db_write_lock", i_dup)
        assert 0 < i_dup < i_lock

    def test_config_switch_declared(self):
        """配置项必须声明且默认 True（自检家族默认开）。"""
        import app.config as _cfg

        assert getattr(_cfg.settings, "content_crosscheck_duplicate") is True


# ===========================================================================
# 五、约束映射表与 __all__ 回归
# ===========================================================================
class TestConstraintMapAndExports:
    """防止本轮改动打穿上一轮的约束映射表完整性。"""

    def test_constraint_map_still_valid(self):
        assert cc.validate_constraint_map() == []

    def test_calc_process_in_exported_api(self):
        assert "cross_section_copy_findings" in cc.__all__
        assert "has_calc_process" in pe.__all__

    def test_alias_contract_is_documented_in_source(self):
        """公开别名的**契约注释必须紧邻赋值行**（防止后人把它拆成独立实现）。"""
        src = (APP / "services" / "preflight_engine.py").read_text(encoding="utf-8")
        line = next((l for l in src.splitlines()
                     if l.startswith("has_calc_process = _has_calc_process")), "")
        assert line, "别名赋值行丢失"
        idx = src.find(line)
        head = src[max(0, idx - 600):idx]
        assert "TRC-01" in head, "别名上方注释必须写明与 TRC-01 同源的契约"

    def test_cross_section_findings_documented_as_fail_soft(self):
        doc = cc.cross_section_copy_findings.__doc__ or ""
        assert "fail-soft" in doc
        assert "CON-06" in doc