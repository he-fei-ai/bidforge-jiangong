"""护栏：模糊生成规则表扩为八类 + 生成后自检/自动修复默认开启（2026-10-02 · 第二十六轮）。

依据《目录与正文生成模块增强》需求文档落地两项收口：
① 三.2「模糊生成规则」为 **八类**（数值/名称/时间/数量/承诺/技术参数/材料规格/
   工序流程），原实现只有六类 —— 本轮补 material / process 两类，
   规则表仍是提示词与内部标记的**唯一事实源**（护栏锁定表↔文案↔检测器三方同源）；
② 一.1/六.4「生成即完整、不出现任何占位标记、发现问题自动修复」为硬性目标 →
   content_selfcheck / content_selfcheck_autofix 由观察期默认关改为**默认开**
   （回退能力保留：设 False 逐字回到旧行为）。

判据指向「真正下发给模型/落库的那份数据」，不做碰巧含关键字的宽匹配（§4.22 教训）。
"""
from __future__ import annotations

import inspect
from pathlib import Path

from app.config import Settings
from app.services.content_fuzzy import (
    FUZZY_CATEGORIES,
    FUZZY_CATEGORY_ORDER,
    FUZZY_UNDETECTABLE,
    build_fuzzy_rules_block,
    build_fuzzy_rules_for_standard,
    detect_fuzzy_expressions,
)
from app.services.content_trace import GENERATION_FUZZY, build_trace

# ---------------------------------------------------------------------------
# 1. 八类规则表：词表封闭、与需求文档逐字对齐
# ---------------------------------------------------------------------------

#: 需求文档三.2 列出的八类（顺序即文档顺序），label 必须一字不差
_REQUIRED_LABELS: tuple[str, ...] = (
    "数值类", "名称类", "时间类", "数量类", "承诺类", "技术参数类",
    "材料规格类", "工序流程类",
)


class TestEightCategoryTable:
    def test_labels_match_requirement_doc(self):
        """表内容与需求文档的八类清单穷举一致（不多不少、label 逐字）。"""
        assert [c["label"] for c in FUZZY_CATEGORIES.values()] == list(_REQUIRED_LABELS)
        assert len(FUZZY_CATEGORIES) == 8

    def test_new_categories_fields_complete(self):
        """补的两类四要素齐备（precise/fuzzy/forbid + trace 用 data_source/reason）。"""
        for key in ("material", "process"):
            c = FUZZY_CATEGORIES[key]
            assert c["precise"].startswith("有明确"), key
            assert "禁止" in c["forbid"], key
            assert c["data_source"] and c["reason"], key

    def test_material_forbids_brand_grade(self):
        """材料规格类红线：禁止编造具体牌号（需求文档原文口径）。"""
        assert "牌号" in FUZZY_CATEGORIES["material"]["forbid"]

    def test_process_forbids_invented_steps(self):
        """工序流程类红线：禁止编造不存在工序（需求文档原文口径）。"""
        assert "不存在的工序" in FUZZY_CATEGORIES["process"]["forbid"]

    def test_order_and_undetectable_are_subsets(self):
        assert set(FUZZY_CATEGORY_ORDER) == set(FUZZY_CATEGORIES)
        assert set(FUZZY_UNDETECTABLE) <= set(FUZZY_CATEGORIES)


class TestPromptTableSingleSource:
    """提示词对照表必须由规则表渲染 —— 新增类别不可能漏渲染。"""

    def test_block_renders_all_eight_rows(self):
        s = build_fuzzy_rules_block()
        for c in FUZZY_CATEGORIES.values():
            assert f"| {c['label']} |" in s, c["label"]
        # 表体行数 = 类别数（不多渲染、不少渲染）
        body_rows = [ln for ln in s.splitlines()
                     if ln.startswith("| ") and "---" not in ln
                     and "类别 |" not in ln]
        assert len(body_rows) == len(FUZZY_CATEGORIES)

    def test_block_no_longer_hardcodes_category_tuple(self):
        """build_fuzzy_rules_block 不得回退为硬编码类别元组（表/文案分叉根因）。"""
        src = inspect.getsource(build_fuzzy_rules_block)
        assert 'for key, c in FUZZY_CATEGORIES.items():' in src

    def test_injected_into_both_standards(self):
        from app.services.content_standard import FUZZY, PRECISE
        for std in (PRECISE, FUZZY):
            s = build_fuzzy_rules_for_standard(std)
            for label in _REQUIRED_LABELS:
                assert label in s, f"{std}: {label}"


# ---------------------------------------------------------------------------
# 2. 新类别检测器：正/负样本与归类优先级
# ---------------------------------------------------------------------------

class TestMaterialDetector:
    def test_positive_samples(self):
        for text in (
            "材料品种、规格符合设计要求",
            "钢筋的强度应符合现行规范要求",
            "防水材料应符合设计文件规定",
            "选用符合规范要求的",
        ):
            hits = detect_fuzzy_expressions(text)
            assert hits and hits[0]["category"] == "material", text

    def test_no_material_prefix_not_grabbed(self):
        """无材料名词前缀的泛化表述仍归 tech —— 两类不得互相抢判。"""
        hits = detect_fuzzy_expressions("参数取值经专项计算确定")
        assert hits and hits[0]["category"] == "tech"

    def test_precise_spec_not_fuzzy(self):
        assert detect_fuzzy_expressions("采用 HRB400 钢筋、C30 混凝土") == []


class TestProcessDetector:
    def test_positive_samples(self):
        for text in (
            "按标准施工工艺组织作业",
            "参照常规工序流程执行",
            "按同类工程经验组织施工",
            "先搭设支架，后浇筑混凝土施工",
        ):
            hits = detect_fuzzy_expressions(text)
            assert hits and hits[0]["category"] == "process", text

    def test_plain_narration_not_flagged(self):
        """普通叙述不得被刷成模糊工序（保守判定）。"""
        assert detect_fuzzy_expressions("基坑开挖至设计标高后验槽") == []


class TestDetectorCoverage:
    def test_every_detectable_category_has_detector(self):
        """除登记在 UNDETECTABLE 的类别外，每类都必须有检测正则（防哑类别）。"""
        import app.services.content_fuzzy as cf
        for key in FUZZY_CATEGORIES:
            if key in FUZZY_UNDETECTABLE and key != "promise":
                continue
            assert cf._DETECTORS.get(key), f"{key} 无检测器"

    def test_multiple_hits_all_recorded(self):
        """回归：多处模糊表述并存时不得因重叠去重异常而**整体丢标记**。

        历史潜伏 BUG（本轮发现并修复）：旧去重用 `for as_, ae, _ in accepted`
        对 4 键字典解包必抛 ValueError → 被外层 except 吞掉 → 只要有第二处
        候选命中，detect_fuzzy_expressions 就返回空表，内部标记全部丢失。
        """
        body = ("钢筋强度应符合现行规范要求，底板厚度约 200mm，"
                "支架搭设按标准施工工艺组织。")
        cats = {h["category"] for h in detect_fuzzy_expressions(body)}
        assert {"material", "number", "process"} <= cats, cats

    def test_overlap_priority_keeps_specific_category(self):
        """重叠区间只归优先级更高的类别一次（同一起点不得刷两条）。"""
        hits = detect_fuzzy_expressions("材料应符合规范要求")
        starts = [h["char_start"] for h in hits]
        assert len(starts) == len(set(starts))


# ---------------------------------------------------------------------------
# 3. 内部标记：新类别进入 trace 口径（category/data_source/fuzzy_reason 同源）
# ---------------------------------------------------------------------------

class TestTraceCarriesNewCategories:
    def test_material_hit_traceable(self):
        body = "主体材料品种、规格符合设计要求。"
        trace = build_trace(scanned=body,
                            fuzzy_expressions=detect_fuzzy_expressions(body))
        items = [i for i in trace["items"]
                 if i["generation_type"] == GENERATION_FUZZY]
        assert items
        assert items[0]["category"] == "material"
        assert items[0]["data_source"] == FUZZY_CATEGORIES["material"]["data_source"]
        assert items[0]["fuzzy_reason"] == FUZZY_CATEGORIES["material"]["reason"]

    def test_summary_by_category(self):
        body = "按标准施工工艺组织作业，材料应符合规范要求。"
        fx = detect_fuzzy_expressions(body)
        trace = build_trace(scanned=body, fuzzy_expressions=fx)
        assert trace["summary"]["by_category"]


# ---------------------------------------------------------------------------
# 4. 自检 + 确定性自动修复默认开启（需求目标一「生成即完整」）
# ---------------------------------------------------------------------------

class TestSelfcheckDefaultsOn:
    def test_config_defaults(self):
        s = Settings()
        assert s.content_selfcheck is True
        assert s.content_selfcheck_autofix is True
        assert s.content_checkpoint_prepend is True
        assert s.outline_checkpoint_check is True

    def test_revert_available(self):
        """默认开是刻意行为变更，但必须可逐项回退（不删代码红线）。"""
        s = Settings(content_selfcheck=False, content_selfcheck_autofix=False)
        assert s.content_selfcheck is False and s.content_selfcheck_autofix is False

    def test_sse_fallback_defaults_match_config(self):
        """接线处 getattr 兜底值必须与 config 默认一致（两处分叉=静默失效）。"""
        src = (Path(__file__).resolve().parents[1]
               / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert 'getattr(settings, "content_selfcheck", True)' in src
        assert 'getattr(settings, "content_selfcheck_autofix", True)' in src

    def test_autofix_rewrites_placeholder_deterministically(self):
        """自动修复动作本身：占位标记改写后正文不再残留【待补充】，且幂等。"""
        from app.services.content_checkpoint import rewrite_placeholder_marks
        text = "支护桩直径【待补充】，桩间距按设计确定。"
        fixed, actions = rewrite_placeholder_marks(text)
        assert "【待补充" not in fixed
        assert actions, "应产生可追溯的修复动作记录"
        again, actions2 = rewrite_placeholder_marks(fixed)
        assert again == fixed and actions2 == []  # 幂等
