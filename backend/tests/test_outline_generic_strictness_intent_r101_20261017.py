"""目录泛化判据「裸词干意图」锁（R101 · 遗留 4 · 2026-10-17）。

背景（本轮探针核对，均有代码依据）
----------------------------------
`outline_quality.GENERIC_STEM_ORDINAL_RE` 的序号分组**刻意写成可选**
（AGENTS R85 记录在案的设计）⇒ 裸词干「专项」「措施」「步骤」「流程」
「要求」「说明」… 单独成句即判占位。而 `GENERIC_BARE_STEMS` 作为
「被护栏锁定的封闭白名单」只列 6 词（既有
`test_outline_closeout_r85_20261016.py::test_bare_stem_whitelist_is_narrow`
锁精确相等）。

⚠️ 两者**不是等号关系**：白名单是正则裸词干分支的**真子集**（6 ⊂ 16）。
既有 R85 护栏只锁了「白名单窄」，**未**锁「裸词干整体判占位」这一意图 ⇒
后人极易朝两个**减少检出 / 引入误伤**的方向「修正」：

  · 把正则序号分组改**必选**（去掉结尾的可选 `?`）—— 裸词干即刻逃逸，
    检出**减少**，与「占位名不得进成品」的目标相反；
  · 把 `GENERIC_BARE_STEMS` 扩到与正则词干表**相等** —— 收进
    「要求/说明/方法/流程/工序/安排/保障」等**可能构成正常章节名**的词。

本文件把**双向意图**锁死：裸词干整体判占位（含正则词干表 16 词）、
真实复合标题绝不误伤、白名单保持真子集（既不缩到空也不扩到相等）。

⚠️ 判据纪律（AGENTS §5.14）：静态锁锚**正则形态**与**真实判据调用**，
不锁「源码含字符串」；行为锁每条都配反例。
"""
from __future__ import annotations

import pytest
from app.services.outline_quality import (
    GENERIC_BARE_STEMS,
    GENERIC_STEM_ORDINAL_RE,
    _norm,
    detect_generic_titles,
    is_generic_title,
)

#: 正则词干表闭集（与 GENERIC_STEM_ORDINAL_RE 源码逐字对齐；护栏 `test_stem_
#: table_matches_regex` 锁其与正则一致）。**裸词干**（无序号）全部判占位。
STEM_WORDS = (
    "专项方案", "专项措施", "专项", "措施", "内容", "要点", "方法", "步骤",
    "流程", "工序", "要求", "说明", "保障", "安排", "其它", "其他",
)

#: 必须判为占位的**裸词干**（用户症状 + 「AI 换写法逃逸」的同族变体）。
BARE_STEMS_MUST = (
    "专项", "措施", "内容", "要点", "其它", "其他",   # 白名单 6 词
    "步骤", "流程", "工序", "要求", "说明", "方法", "保障", "安排",  # 白名单外的裸词干
)

#: 必须**不**误伤的合法复合标题（真实生产语料/既有护栏出现过）。
LEGIT_MUST_NOT = (
    "施工要求", "技术要求", "监测措施", "工序流程", "施工工艺流程",
    "质量保障措施", "进度安排", "内容简介说明", "深基坑支护", "专项施工方案",
)

#: 「词干 + 确定性序号」必经占位（序号值域含阿拉伯/中文/全角）。
STEM_ORDINAL_MUST = ("专项6", "专项方案6", "措施三", "内容2", "措施１")


class TestBareStemIsGenericByDesign:
    """裸词干（无序号）判占位是**刻意设计** —— 序号分组为可选。"""

    @pytest.mark.parametrize("title", BARE_STEMS_MUST)
    def test_bare_stem_is_generic(self, title):
        assert is_generic_title(title) is True, (
            f"裸词干 {title!r} 逃逸检出 —— 序号分组若被改必选即会减少检出，"
            "与「占位名不得进成品」目标相反（R85 记录在案的设计）")

    def test_regex_ordinal_group_is_optional(self):
        """静态锁：序号分组必须是可选（`)?$`），这是裸词干判占位的**唯一形态依据**。

        锚**正则字符串形态**而非注释 —— 注释提及不算判据（AGENTS §5.14）。
        """
        pat = GENERIC_STEM_ORDINAL_RE.pattern
        assert pat.startswith("^") and pat.endswith("?$"), (
            "序号分组不再是可选 ⇒ 裸词干逃逸（检出减少）")
        # 行为实证：裸词干能 fullmatch，且带序号同样能
        assert GENERIC_STEM_ORDINAL_RE.fullmatch(_norm("步骤"))
        assert GENERIC_STEM_ORDINAL_RE.fullmatch(_norm("步骤3"))

    def test_stem_table_matches_regex(self):
        """词干表逐词必须被正则**裸词干分支**接纳（表与正则同源，防两处分叉）。"""
        for w in STEM_WORDS:
            assert GENERIC_STEM_ORDINAL_RE.fullmatch(_norm(w)), (
                f"词干表词 {w!r} 不被正则裸词干分支接纳 ⇒ 表/正则分叉")


class TestLegitTitlesNeverFlagged:
    """反例锁：判据拓宽**不得**误伤合法复合标题（否则护栏会被后人改松）。"""

    @pytest.mark.parametrize("title", LEGIT_MUST_NOT)
    def test_legit_title_not_generic(self, title):
        assert is_generic_title(title) is False, (
            f"合法复合标题 {title!r} 被误判为占位（全串锚定被破坏？）")


class TestStemOrdinalDeterministic:
    """词干 + 序号（阿拉伯/中文/全角）必判占位，且经检出出口端到端可见。"""

    @pytest.mark.parametrize("title", STEM_ORDINAL_MUST)
    def test_stem_ordinal_is_generic(self, title):
        assert is_generic_title(title) is True, f"{title!r} 逃逸检出"

    def test_detect_reports_bare_and_ordinal(self):
        """端到端：检出出口必须与逐条判据同源（防 is_generic_title 与 detect 分叉）。"""
        tree = [{"title": t, "children": []}
                for t in ("专项", "步骤", "专项6", "施工要求", "监测措施")]
        hits = {h["title"] for h in detect_generic_titles(tree)}
        assert hits == {"专项", "步骤", "专项6"}, hits


class TestWhitelistIsProperSubset:
    """白名单必须是正则裸词干分支的**真子集** —— 既不相等也不为空。

    这是本轮要锁的**核心关系**：R85 只锁了「白名单窄」，本组补锁
    「白名单 ⊂ 正则」，防止后人朝任一方向靠拢。
    """

    def test_whitelist_exact_six_words(self):
        assert GENERIC_BARE_STEMS == {"专项", "措施", "内容", "要点", "其它", "其他"}

    def test_whitelist_is_subset_of_regex_stems(self):
        assert GENERIC_BARE_STEMS <= set(STEM_WORDS), (
            "白名单出现正则词干表之外的词 ⇒ 白名单被额外放宽")

    def test_whitelist_is_strict_subset(self):
        """存在「正则判占位但不在白名单」的裸词干 ⇒ 证明是真子集（非相等）。"""
        extra = {w for w in ("要求", "说明", "方法", "步骤", "流程", "工序",
                             "安排", "保障") if is_generic_title(w)}
        assert extra, "白名单外的裸词干一个都不判占位 ⇒ 正则可选序号分组失效"
        assert not (extra & set(GENERIC_BARE_STEMS)), (
            "白名单外的裸词干混入白名单 ⇒ 真子集关系被破坏")


class TestNoWhitespaceEscapes:
    """空/纯空白标题不得进入判据（防 `_norm` 退化后空串误判）。"""

    def test_empty_and_blank_not_generic(self):
        for t in ("", "   ", "\t", None):
            assert is_generic_title(t) is False
