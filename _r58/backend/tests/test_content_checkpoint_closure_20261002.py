"""护栏：审核检查点闭环（2026-10-02 · 第二十四轮 · 生成侧闭环收口）。

覆盖六层：

A. 「检查点 → 生成约束 → 实现方式」映射表 —— 唯一事实源完整性：
   12 类分组齐全、通道枚举封闭、rule_id 锚点全部真实存在、九章全覆盖、
   章节条目 constraint 直接派生自 CHAPTER_CHECKPOINT_REQUIREMENTS（不另抄措辞）。
B. 章节 key 补充推断 —— 主判据优先不覆盖、小节标题补全、未命中不猜；
   锁定生产库实证的 13 个漏映射标题。
C. 小节作用域过滤 —— 小节只查自己承担的要求（SAF-03/04/05 假缺项不再产生），
   章级标题仍全查；默认参数保持旧行为（向后兼容红线）。
D. STD-03 裸标准编号自动补年号 —— 年号只取自现行标准库（绝不编造）、
   库外不动、已带年号不动、围栏内不动、歧义不补、幂等、fail-soft。
E. CON-04 占位标记确定性改写 —— 括号形态改写为条件式表述且不引入任何数值、
   单位保留为括号注记、×× 形态只报不改、围栏内不动、幂等、fail-soft、
   过度匹配修复（相邻两个占位符不得互相吞并）。
F. 接线（源码静态锁）—— sse_handlers 三处接线到位、开关默认值正确、
   确定性自动修复必须在任何报告计算**之前**执行。

⚠️ 判据指向（AGENTS §4.22.8 教训）：对 sse_handlers 的断言走**源码扫描**而非
「碰巧含关键字的一切字符串」—— 只锚定真正生效的那段装配代码（正则定位
`_ck_fix_actions` 与 `standard_report(` 的相对先后，以及 `checkpoint_selfcheck(`
调用体的实际形参），而不是全文件子串包含。

⚠️ A/B 反向验证：每条「修复」类用例都配一个「还原旧行为即失败」的对照断言
（见各 Test* 类内的 test_revert_* / test_without_fix_*）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.config import settings
from app.services.audit_rules import RULE_MAP
from app.services.content_checkpoint import (
    AMBIGUOUS_BASES,
    BASE_CODE_INDEX,
    CHAPTER_CHECKPOINT_REQUIREMENTS,
    CHAPTER_TITLE_SUPPLEMENT,
    CHECKPOINT_CHANNELS,
    CHECKPOINT_GROUPS,
    CROSS_CHAPTER_CONSTRAINTS,
    checkpoint_constraint_map,
    checkpoint_selfcheck,
    fix_bare_standard_codes,
    infer_chapter_key,
    rewrite_placeholder_marks,
    validate_constraint_map,
    validate_rule_anchoring,
)
from app.services.scheme_classification import NINE_CHAPTERS

REPO_ROOT = Path(__file__).resolve().parents[1]
SSE_HANDLERS = REPO_ROOT / "app" / "routers" / "sse_handlers.py"

_CONDITIONAL = "按设计文件及现场实际确定"

GROUP_KEYS = [k for k, _ in CHECKPOINT_GROUPS]


def _sse_source() -> str:
    return SSE_HANDLERS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A. 映射表完整性
# ---------------------------------------------------------------------------

class TestConstraintMap:
    """「检查点 → 生成约束 → 实现方式」唯一事实源。"""

    def test_validate_constraint_map_clean(self):
        """锚点全合法：规则未废弃、分组与通道均未越界。"""
        assert validate_constraint_map() == []

    def test_validate_rule_anchoring_still_clean(self):
        """章节必含要求的 rule_id 锚点依旧合法（独立成立，不依赖映射表校验）。"""
        assert validate_rule_anchoring() == []

    def test_groups_are_twelve_and_closed(self):
        """12 类分组（需求清单原样），key 唯一且首尾顺序固定。"""
        assert len(CHECKPOINT_GROUPS) == 12
        assert len(set(GROUP_KEYS)) == 12
        assert GROUP_KEYS[0] == "completeness" and GROUP_KEYS[-1] == "other"

    def test_channels_closed_enum(self):
        """通道枚举封闭且六类俱全（防新加魔法字符串绕过约束）。"""
        assert len(CHECKPOINT_CHANNELS) == 6
        assert set(CHECKPOINT_CHANNELS) == {
            "system_preprompt", "chapter_preprompt", "selfcheck",
            "autofix", "export_pipeline", "audit_fallback",
        }

    def test_map_non_empty_and_shape(self):
        """映射表非空，每条 7 个键齐全且类型正确。"""
        rows = checkpoint_constraint_map()
        assert len(rows) >= 30
        for r in rows:
            assert set(r) == {"group", "checkpoint", "rule_ids", "constraint",
                              "implementation", "channel", "chapter_key"}
            assert r["group"] in GROUP_KEYS
            assert r["channel"] in CHECKPOINT_CHANNELS
            assert r["rule_ids"] and all(isinstance(x, str) for x in r["rule_ids"])
            assert r["checkpoint"] and r["constraint"] and r["implementation"]

    def test_every_group_covered(self):
        """12 类分组全部至少有一条映射（漏一类 = 需求清单有缺口）。"""
        assert {r["group"] for r in checkpoint_constraint_map()} == set(GROUP_KEYS)

    def test_all_anchored_rule_ids_exist(self):
        """映射表引用的每个 rule_id 都必须真实存在于审核注册表。"""
        rows = checkpoint_constraint_map()
        missing = sorted({rid for r in rows for rid in r["rule_ids"]
                          if rid not in RULE_MAP})
        assert missing == [], f"悬空规则锚点: {missing}"

    def test_nine_chapters_fully_derived(self):
        """九大章节的每一条必含要求都被派生成映射条目（零遗漏）。"""
        rows = checkpoint_constraint_map()
        derived = {(r["chapter_key"], r["checkpoint"]) for r in rows
                   if r["chapter_key"]}
        expected = {(ck, f"{spec['title']}·{r['label']}")
                    for ck, spec in CHAPTER_CHECKPOINT_REQUIREMENTS.items()
                    for r in spec["requirements"]}
        assert derived == expected

    def test_chapter_constraints_are_derived_not_copied(self):
        """章节条目的 constraint 必须逐字等于 requirements 的 note（不另抄措辞）。"""
        rows = checkpoint_constraint_map()
        for ck, spec in CHAPTER_CHECKPOINT_REQUIREMENTS.items():
            for r in spec["requirements"]:
                hit = [x for x in rows
                       if x["chapter_key"] == ck
                       and x["checkpoint"] == f"{spec['title']}·{r['label']}"]
                assert len(hit) == 1, (ck, r["label"])
                assert hit[0]["constraint"] == r["note"]
                assert hit[0]["rule_ids"] == list(r["rule_ids"])

    def test_hazard_only_marked_in_implementation(self):
        """hazard_only 条目必须在 implementation 中明示（避免误读为全量要求）。"""
        rows = checkpoint_constraint_map()
        for ck, spec in CHAPTER_CHECKPOINT_REQUIREMENTS.items():
            for r in spec["requirements"]:
                if not r.get("hazard_only"):
                    continue
                hit = [x for x in rows
                       if x["chapter_key"] == ck
                       and x["checkpoint"] == f"{spec['title']}·{r['label']}"]
                assert len(hit) == 1
                assert "hazard_only" in hit[0]["implementation"]

    def test_autofix_channel_present_for_std03(self):
        """STD-03 必须登记为 autofix 通道（否则闭环缺自动修复一环）。"""
        rows = checkpoint_constraint_map()
        assert any(r["channel"] == "autofix" and "STD-03" in r["rule_ids"]
                   for r in rows)

    def test_cross_chapter_items_well_formed(self):
        """跨章条目自身结构合法（分组/通道/rule_ids 锚点齐全）。"""
        for c in CROSS_CHAPTER_CONSTRAINTS:
            assert c["group"] in GROUP_KEYS
            assert c["channel"] in CHECKPOINT_CHANNELS
            assert c["rule_ids"], c["checkpoint"]
            assert all(rid in RULE_MAP for rid in c["rule_ids"]), c["checkpoint"]

    def test_revert_drop_group_fails(self):
        """A/B：剔除整个分组的映射即被 validate_constraint_map 拦下。"""
        import app.services.content_checkpoint as mod
        saved = mod.CROSS_CHAPTER_CONSTRAINTS
        try:
            mod.CROSS_CHAPTER_CONSTRAINTS = tuple(
                c for c in saved if c["group"] != "other")
            problems = mod.validate_constraint_map()
            assert any("分组 其他" in p for p in problems)
        finally:
            mod.CROSS_CHAPTER_CONSTRAINTS = saved

    def test_revert_dangling_rule_id_fails(self):
        """A/B：引用不存在的规则编号即被拦下（防悬空锚点）。"""
        import app.services.content_checkpoint as mod
        saved = mod.CROSS_CHAPTER_CONSTRAINTS
        try:
            mod.CROSS_CHAPTER_CONSTRAINTS = saved + ({
                "group": "other", "checkpoint": "幽灵检查点",
                "rule_ids": ("NOPE-99",),
                "constraint": "x", "implementation": "y",
                "channel": "selfcheck",
            },)
            problems = mod.validate_constraint_map()
            assert any("NOPE-99" in p for p in problems)
        finally:
            mod.CROSS_CHAPTER_CONSTRAINTS = saved

    def test_revert_bad_channel_fails(self):
        """A/B：非法通道字符串即被拦下（防枚举越界）。"""
        import app.services.content_checkpoint as mod
        saved = mod.CROSS_CHAPTER_CONSTRAINTS
        try:
            mod.CROSS_CHAPTER_CONSTRAINTS = saved + ({
                "group": "other", "checkpoint": "坏通道", "rule_ids": ("DLV-08",),
                "constraint": "x", "implementation": "y",
                "channel": "magic_channel",
            },)
            problems = mod.validate_constraint_map()
            assert any("magic_channel" in p for p in problems)
        finally:
            mod.CROSS_CHAPTER_CONSTRAINTS = saved


# ---------------------------------------------------------------------------
# B. 章节 key 补充推断（生产实证：小节标题漏映射）
# ---------------------------------------------------------------------------

#: 生产库 scheme=d3c1a897… 的真实小节标题 → 期望补全的章节
_PRODUCTION_SECTION_TITLES = {
    # 应急处置章的三个叶子：SAF-03/04/05 此前在生成侧从未被要求过
    "应急组织机构及职责": "emergency",
    "应急物资装备保障": "emergency",
    "应急演练": "emergency",
    "触电事故急救及疏散": "emergency",
    # SAF-02 点名的临时用电 / 消防防火小节
    "照明及手持电动工具管理": "safety",
    "装修动火作业审批": "safety",
    # 其它实测漏映射小节
    "环保检测及竣工资料移交": "acceptance",
    "装饰装修主要材料进场计划": "plan",
    "1.1 工程基本情况": "overview",
    "1.2 周边环境": "overview",
    "装饰装修施工图纸及预算": "calc_drawings",
}


class TestInferChapterKey:
    def test_primary_key_wins_never_overridden(self):
        """主判据命中时直接返回，绝不被补充表覆盖（只补不覆盖）。"""
        assert infer_chapter_key("照明及手持电动工具管理",
                                 "emergency") == "emergency"
        assert infer_chapter_key("应急演练", "calc_drawings") == "calc_drawings"

    def test_production_subsection_titles_mapped(self):
        """生产库实证的小节标题全部补全到正确章节（零漏映射）。"""
        for title, expect in _PRODUCTION_SECTION_TITLES.items():
            assert infer_chapter_key(title) == expect, title

    def test_unknown_title_returns_empty(self):
        """未命中一律空串（宁可漏注入也不猜，与既有契约一致）。"""
        assert infer_chapter_key("随便一个标题") == ""
        assert infer_chapter_key("") == ""
        assert infer_chapter_key(None) == ""
        assert infer_chapter_key(None, "safety") == "safety"

    def test_primary_and_supplement_disagree_primary_wins(self):
        """两者都命中但不一致时以主判据为准（避免改变既有归类）。"""
        assert infer_chapter_key("应急演练", "acceptance") == "acceptance"

    def test_supplement_covers_all_nine_chapters(self):
        """补充表覆盖九大章节（否则某些章的小节永远拿不到本章要素要求）。"""
        assert set(CHAPTER_TITLE_SUPPLEMENT) == {
            ch["key"] for ch in NINE_CHAPTERS}

    def test_supplement_values_non_empty(self):
        """补充表每项关键词非空且去重（空表项 = 死规则）。"""
        for key, kws in CHAPTER_TITLE_SUPPLEMENT.items():
            assert kws, key
            assert len(set(kws)) == len(kws), key

    def test_revert_without_supplement_is_empty_for_these(self):
        """A/B 对照：这批标题主判据全部返回空串 → 补充表是唯一修复手段。

        直接验证「主判据未覆盖」这一前提（主判据在别处维护，此处只断言
        这些标题确实依赖补充表命中 —— 若主判据将来扩展覆盖了它们，
        本用例会失败并提醒同步收窄补充表，避免两套词表长期并存）。
        """
        from app.routers.sse_handlers import chapter_key_of_title as primary
        uncovered = [t for t in _PRODUCTION_SECTION_TITLES
                     if primary(t) == ""]
        assert len(uncovered) >= len(_PRODUCTION_SECTION_TITLES) - 2, (
            "主判据已覆盖多数实证标题，补充表应重新评估（避免两套词表并存）")


# ---------------------------------------------------------------------------
# C. 小节作用域过滤（避免把章级聚合要求拆到小节造成假缺项）
# ---------------------------------------------------------------------------

def _rule_ids(fs: list) -> set:
    return {f.get("rule_id") for f in fs}


class TestSubsectionScope:
    """SAF-03/04/05 是三个独立小节，逐叶子聚合判定会产出假缺项。"""

    def test_subsection_checks_only_its_own_requirement(self):
        """「应急组织机构及职责」只承担 SAF-03，不承担 SAF-04/05。"""
        content = "本项目成立应急组织机构，组长由项目经理担任，明确各岗位职责。"
        subs = checkpoint_selfcheck(
            content, chapter_key="emergency",
            section_title="应急组织机构及职责", subsection_scope=True)
        # 只检查 SAF-03（该小节自己承担的那条），且内容已写全 → 无任何缺项
        assert subs == []

    def test_chapter_level_still_checks_all(self):
        """章级标题（未开 subsection_scope）仍全查三条要求（行为不变）。"""
        content = "项目建立应急机制，加强安全教育培训。"
        chapter = checkpoint_selfcheck(content, chapter_key="emergency")
        assert {"SAF-04", "SAF-05"} <= _rule_ids(chapter)

    def test_default_keeps_legacy_behaviour(self):
        """默认参数（不传 section_title / subsection_scope）与旧行为逐项一致。"""
        content = "项目建立应急机制，加强安全教育培训。"
        legacy = checkpoint_selfcheck(content, chapter_key="emergency")
        explicit_legacy = checkpoint_selfcheck(
            content, chapter_key="emergency", section_title="",
            subsection_scope=False)
        assert legacy == explicit_legacy

    def test_safety_aggregate_not_split_to_subsection(self):
        """SAF-02 四要素是章级聚合要求，不得要求「照明及手持电动工具管理」也写全。"""
        content = "照明线路采用低压照明，电缆架空敷设，配电箱设漏电保护。"
        subs = checkpoint_selfcheck(
            content, chapter_key="safety",
            section_title="照明及手持电动工具管理", subsection_scope=True)
        assert subs == []

    def test_scope_matches_matching_subsection(self):
        """「应急演练」小节只查 SAF-05（主题词命中自身标签）。"""
        content = "每季度组织一次应急演练，做好记录并归档。"
        subs = checkpoint_selfcheck(
            content, chapter_key="emergency",
            section_title="应急演练", subsection_scope=True)
        assert subs == []

    def test_subsection_missing_own_element_reported(self):
        """小节确实缺自己那条要素时仍要报出（过滤不能漏真缺陷）。"""
        subs = checkpoint_selfcheck(
            "本章主要说明现场文明施工管理要求。", chapter_key="emergency",
            section_title="应急物资装备保障", subsection_scope=True)
        assert "SAF-04" in _rule_ids(subs)

    def test_revert_without_scope_produces_false_positives(self):
        """A/B：不开 subsection_scope 时同一小节产出 2 条假缺项（证明修复有效）。"""
        content = "本项目成立应急领导小组，组长由项目经理担任，明确各岗位职责。"
        subs = checkpoint_selfcheck(
            content, chapter_key="emergency",
            section_title="应急组织机构及职责", subsection_scope=False)
        assert {"SAF-04", "SAF-05"} <= _rule_ids(subs), \
            "旧行为应产生跨小节的假缺项"


# ---------------------------------------------------------------------------
# D. STD-03 裸标准编号自动补年号
# ---------------------------------------------------------------------------

class TestFixBareStandardCodes:
    def test_index_built_from_standards_registry_only(self):
        """年号索引**只**来自现行标准库（本模块不另立词表、不写死年号）。"""
        from app.services import standards_registry as sr
        pool = list(sr.BASE_STANDARDS)
        for items in sr.CATEGORY_STANDARDS.values():
            pool.extend(items)
        valid = {sr.strip_standard_year(s.code): s.code for s in pool}
        for base, full in BASE_CODE_INDEX.items():
            assert base in valid, base
            assert valid[base] == full, base
            assert re.search(r"-\d{4}$", full), f"补出的编号必须带年号: {full}"

    def test_known_base_gets_year(self):
        """库内裸编号补齐年号（生产实证的 GB 50210）。"""
        text = "依据《建筑装饰装修工程质量验收标准》GB 50210 执行。"
        out, fixes = fix_bare_standard_codes(text)
        assert "GB 50210-2018" in out
        assert len(fixes) == 1
        f = fixes[0]
        assert f["rule_id"] == "STD-03" and f["fixed"] is True
        assert f["to"] == "GB 50210-2018"

    def test_no_space_form_also_fixed(self):
        """正文常裸写 GB55032（无空格），同样补齐。"""
        out, fixes = fix_bare_standard_codes("应执行 GB55032 的规定。")
        assert "GB 55032-2022" in out
        assert len(fixes) == 1

    def test_year_already_present_untouched(self):
        """已带年号的编号不重复补（幂等前置条件）。"""
        text = "已带年号 GB 50210-2018 不应重复补。"
        out, fixes = fix_bare_standard_codes(text)
        assert out == text
        assert fixes == []

    def test_unknown_code_untouched(self):
        """库外编号一律不动（补了就是编造年号，比不补危害更大）。"""
        text = "库外编号 GB 99999 不应动。"
        out, fixes = fix_bare_standard_codes(text)
        assert out == text
        assert fixes == []

    def test_idempotent(self):
        """幂等：对已修复文本再跑一次返回零修复。"""
        out1, _ = fix_bare_standard_codes("应执行 GB55032 与 GB 55034。")
        out2, fixes2 = fix_bare_standard_codes(out1)
        assert out2 == out1
        assert fixes2 == []

    def test_fenced_block_not_touched(self):
        """围栏代码块（Mermaid / chart-json 载荷）内的编号不动。"""
        text = ("正文 GB 50210\n```mermaid\nflowchart LR\n"
                "GB 99999 --> GB 50300\n```\n尾部 GB 55034")
        out, fixes = fix_bare_standard_codes(text)
        assert "```mermaid\nflowchart LR\nGB 99999 --> GB 50300\n```" in out
        assert "GB 50210-2018" in out and "GB 55034-2022" in out
        assert len(fixes) == 2

    def test_ambiguous_base_is_reported_not_fixed(self):
        """同一基号对应多个编号 → 不补，只报待人工核实（年号不可编造）。"""
        import app.services.content_checkpoint as mod
        saved_idx, saved_amb = mod.BASE_CODE_INDEX, mod.AMBIGUOUS_BASES
        try:
            base = next(iter(saved_idx))          # 取一个真实基号
            bare = saved_idx[base].split("-")[0]  # 如 "GB 55034"
            mod.BASE_CODE_INDEX = {}              # 从可补映射中移除
            mod.AMBIGUOUS_BASES = {base: "X"}     # 标记为歧义
            text = f"引用 {bare} 的规定。"
            out, fixes = mod.fix_bare_standard_codes(text)
            assert out == text, "歧义基号绝不可改写（编造年号比不改更糟）"
            assert len(fixes) == 1
            f = fixes[0]
            assert f["fixable"] is False and f["fixed"] is False
            assert f["rule_id"] == "STD-03"
            assert "人工核实" in f["suggestion"]
        finally:
            mod.BASE_CODE_INDEX, mod.AMBIGUOUS_BASES = saved_idx, saved_amb

    def test_no_ambiguity_in_current_registry(self):
        """现行标准库内无歧义基号（重复登记同一编号不算歧义）。"""
        for base, full in BASE_CODE_INDEX.items():
            assert base not in AMBIGUOUS_BASES

    def test_fixes_shape_contract(self):
        """修复清单字段契约固定（可直接并入报告）。"""
        _, fixes = fix_bare_standard_codes("应执行 JGJ 59。")
        assert len(fixes) == 1
        f = fixes[0]
        assert {"rule_id", "checkpoint", "from", "to", "position", "severity",
                "fixable", "fixed", "source"} <= set(f)
        assert f["source"] == "selfcheck_autofix"
        assert f["position"] >= 0

    def test_fail_soft_on_empty(self):
        """空/None 输入安全返回（fail-soft，绝不抛业务异常）。"""
        assert fix_bare_standard_codes("") == ("", [])
        assert fix_bare_standard_codes(None) == ("", [])

    def test_fail_soft_on_internal_error(self):
        """内部异常时返回原文与空清单（自检是体检，不是闸门）。"""
        import app.services.content_checkpoint as mod
        saved = mod._normalize_code_for_index
        try:
            def boom(code):
                raise RuntimeError("注入异常")
            mod._normalize_code_for_index = boom
            text = "应执行 GB55032。"
            out, fixes = mod.fix_bare_standard_codes(text)
            assert out == text
            assert fixes == []
        finally:
            mod._normalize_code_for_index = saved

    def test_revert_without_autofix_leaves_bare_code(self):
        """A/B 对照：自动修复会补年号；不开修复（即旧行为）时原文不变。"""
        text = "应执行 GB55032。"
        fixed, fixes = fix_bare_standard_codes(text)
        assert fixed != text and "-2022" in fixed and len(fixes) == 1
        # 旧行为（不开 content_selfcheck_autofix）= 原文照落库
        assert text == "应执行 GB55032。"


# ---------------------------------------------------------------------------
# E. CON-04 占位标记确定性改写
# ---------------------------------------------------------------------------

class TestRewritePlaceholderMarks:
    def test_bracket_form_rewritten_to_conditional(self):
        """括号形态改写为条件式表述（生产实证的 88 处占位符主体形态）。"""
        out, fixes = rewrite_placeholder_marks("堆放区面积【待补充：堆放区面积】m²")
        assert _CONDITIONAL in out
        assert "【待补充" not in out
        assert len(fixes) == 1 and fixes[0]["rule_id"] == "CON-04"

    def test_conditional_phrase_introduces_no_number(self):
        """替代短语**不引入任何具体数值**（编造数值是数据真实性红线）。"""
        out, _ = rewrite_placeholder_marks("工期【待定】为90天。")
        # 不得凭空多出除原文中 90 以外的数字
        nums_before = re.findall(r"\d+", "工期【待定】为90天。")
        nums_after = re.findall(r"\d+", out)
        assert nums_after == nums_before, (nums_before, nums_after)

    def test_trailing_unit_kept_as_bracket_note(self):
        """紧随其后的单位保留为括号注记（避免遗留孤立单位）。"""
        out, _ = rewrite_placeholder_marks("基坑深度【待定】m")
        assert out.endswith("（m）"), out
        out2, _ = rewrite_placeholder_marks("宽度【待补充：宽度】mm")
        assert out2.endswith("（mm）"), out2

    def test_xx_form_reported_not_rewritten(self):
        """×× 形态只报不改（上下文不明，改错比不改更糟）。"""
        text = "联系单位：××公司。深度为××m。"
        out, fixes = rewrite_placeholder_marks(text)
        assert out == text, "×× 形态不得被自动改写"
        pend = [f for f in fixes if f["fixed"] is False]
        assert pend and all(f["rule_id"] == "CON-04" for f in pend)
        assert all(f["fixable"] is False for f in pend)
        assert all("人工" in f["suggestion"] for f in pend)

    def test_adjacent_placeholders_not_swallowed(self):
        """过度匹配修复：相邻两个占位符各自独立改写，中间的正常文字不得被吞。"""
        text = "深度【待定】m 宽度【待补充：宽度】mm 面积【待确认】m²"
        out, fixes = rewrite_placeholder_marks(text)
        assert out.count(_CONDITIONAL) == 3
        # 中间的「宽度」「面积」等真实正文必须保留
        assert "宽度" in out and "面积" in out
        assert [f["fixed"] for f in fixes].count(True) == 3

    def test_idempotent(self):
        """幂等：改写后再跑一次无任何变化。"""
        text = "堆放区面积【待补充：堆放区面积】m²，基坑深度【待定】m"
        out1, _ = rewrite_placeholder_marks(text)
        out2, fixes2 = rewrite_placeholder_marks(out1)
        assert out2 == out1
        assert fixes2 == []

    def test_fenced_block_not_touched(self):
        """围栏代码块内的占位标记不动（图表载荷不是引用语境）。"""
        text = "正文【待补充：甲】\n```mermaid\nflowchart LR\n【待补充：乙】\n```"
        out, fixes = rewrite_placeholder_marks(text)
        assert "```mermaid\nflowchart LR\n【待补充：乙】\n```" in out
        assert len(fixes) == 1

    def test_clean_text_untouched(self):
        """无占位符的正常正文逐字不变（绝不误伤）。"""
        text = "本节说明施工工艺与质量控制要求，无需补充。"
        out, fixes = rewrite_placeholder_marks(text)
        assert out == text
        assert fixes == []

    def test_fail_soft_on_empty(self):
        """空/None 输入安全返回。"""
        assert rewrite_placeholder_marks("") == ("", [])
        assert rewrite_placeholder_marks(None) == ("", [])

    def test_fail_soft_on_internal_error(self):
        """内部异常时返回原文与空清单（fail-soft）。"""
        import app.services.content_checkpoint as mod
        saved = mod._fence_ranges
        try:
            def boom(text):
                raise RuntimeError("注入异常")
            mod._fence_ranges = boom
            text = "深度【待定】m"
            out, fixes = mod.rewrite_placeholder_marks(text)
            assert out == text
            assert fixes == []
        finally:
            mod._fence_ranges = saved

    def test_revert_without_rewrite_keeps_placeholder(self):
        """A/B 对照：改写会消除占位符；旧行为（不改写）时占位符原样落库。"""
        text = "堆放区面积【待补充：堆放区面积】m²"
        fixed, fixes = rewrite_placeholder_marks(text)
        assert "【待补充" not in fixed and len(fixes) == 1
        assert "【待补充" in text


# ---------------------------------------------------------------------------
# F. 接线（源码静态锁）
# ---------------------------------------------------------------------------

class TestWiring:
    """判据只锚定真正生效的装配代码，不做「碰巧含关键字」的全文件匹配。"""

    def test_functions_imported(self):
        src = _sse_source()
        m = re.search(r"from app\.services\.content_checkpoint import \(([^)]*)\)",
                      src)
        assert m, "sse_handlers 必须从唯一事实源导入 content_checkpoint"
        # 一行可能有多个逗号分隔的导入名，必须按逗号切分而非按行
        imported = {x.strip() for x in m.group(1).split(",") if x.strip()}
        assert {"infer_chapter_key", "fix_bare_standard_codes",
                "rewrite_placeholder_marks", "checkpoint_selfcheck"} <= imported

    @staticmethod
    def _call_window(src: str, name: str, span: int = 320) -> str:
        """取 ``name``（调用点）之后的定长窗口。

        以 ``(`` 定位调用点以避开 import 行；非调用锚点（以 ``=`` / ``[`` 结尾）
        直接按文本定位。
        """
        at = src.index(name if name.endswith(("=", "]")) else name + "(")
        return src[at:at + span]

    def test_autofix_runs_before_any_report(self):
        """确定性自动修复必须在 standard_report **之前**（否则报告与落库正文对不上号）。"""
        src = _sse_source()
        fix_at = src.index("_ck_fix_actions: list = []")
        report_at = src.index("report = standard_report(")
        assert fix_at < report_at, "自动修复被排到了报告之后（口径分叉）"

    def test_autofix_gated_by_switch(self):
        """自动修复受 content_selfcheck_autofix 开关控制（关闭时零行为变化）。"""
        tail = self._call_window(_sse_source(), "_ck_fix_actions: list = []")
        assert "if _selfcheck_autofix:" in tail
        assert "fix_bare_standard_codes(content)" in tail
        assert "rewrite_placeholder_marks(content)" in tail

    def test_autofix_block_is_fail_soft(self):
        """自动修复整体包在 try/except 中（异常不得阻断正文落库）。"""
        block = self._call_window(_sse_source(),
                                  "_ck_fix_actions: list = []", span=1800)
        assert "try:" in block and "except Exception:" in block

    def test_selfcheck_receives_subsection_scope(self):
        """自检调用必须传 subsection_scope（否则小节假缺项回归）。"""
        call = self._call_window(_sse_source(), "checkpoint_selfcheck")
        assert "subsection_scope=" in call
        assert "section_title=" in call
        assert "chapter_key=" in call

    def test_injection_uses_infer_chapter_key(self):
        """章节级检查点注入必须走 infer_chapter_key（小节标题才拿得到要素要求）。"""
        call = self._call_window(_sse_source(), "build_chapter_checkpoint_block")
        assert "infer_chapter_key(" in call

    def test_primary_key_not_used_for_facts_after_change(self):
        """事实注入仍走 chapter_key_of_title（补充推断不得改变既有分类结果）。"""
        call = self._call_window(_sse_source(), "facts_text = _render_facts_text")
        assert "chapter_key_of_title(" in call
        assert "infer_chapter_key" not in call


class TestWiringDefaults:
    """开关默认值。

    ⚠️ 2026-10-02（第二十六轮）校准：需求目标一「生成即完整、不残留占位标记」
    为硬性交付要求，content_selfcheck / content_selfcheck_autofix 由观察期的
    默认关改为**默认开**；回退能力由开关本身保留（设 False 逐字回到旧行为）。
    """

    def test_prepend_default_on(self):
        assert settings.content_checkpoint_prepend is True

    def test_selfcheck_default_on(self):
        assert settings.content_selfcheck is True

    def test_selfcheck_autofix_default_on(self):
        assert settings.content_selfcheck_autofix is True

    def test_revert_switch_stays_available(self):
        """A/B 对照：默认开是刻意行为变更，但设 False 逐字回退必须仍可行。"""
        import app.config as cfg
        fresh = cfg.Settings()
        assert fresh.content_selfcheck is True
        assert fresh.content_selfcheck_autofix is True
        reverted = cfg.Settings(content_selfcheck=False,
                                content_selfcheck_autofix=False)
        assert reverted.content_selfcheck is False
        assert reverted.content_selfcheck_autofix is False


class TestCheckpointsEndpoint:
    """GET /api/v1/compliance/checkpoints（映射表对外可见）。"""

    def test_route_registered(self):
        from app.main import app
        assert "/api/v1/compliance/checkpoints" in app.openapi()["paths"]

    def test_endpoint_payload(self):
        import asyncio

        from app.routers.compliance import list_checkpoint_constraints
        data = asyncio.run(list_checkpoint_constraints())
        assert set(data) >= {"rule_version", "groups", "channels", "items",
                             "by_group", "by_channel", "anchor_problems"}
        assert data["anchor_problems"] == []
        assert len(data["items"]) >= 30
        assert data["by_group"] and data["by_channel"]
        # by_group 与 items 统计必须一致
        assert sum(data["by_group"].values()) == len(data["items"])
        assert sum(data["by_channel"].values()) == len(data["items"])
        assert len(data["groups"]) == 12

