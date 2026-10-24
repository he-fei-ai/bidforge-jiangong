"""R119 · 六模块跨模块收口（2026-10-21）· 字数口径分叉（TD-4）修复护栏 + 刻意不改护栏（TD-5）。

【缺陷来源】R118 §九 遗留 L1 / §三 TD-4：
``services/content_utils.py::strip_fenced_code_blocks`` 不区分 lang，把
`` ```markdown `` 家族围栏整段当代码剔除；而 R117 起导出侧
（``content_blocks._parse_content_blocks``）会把 markdown 围栏内部递归解析成
**真实标题 / GFM 表格 / 列表**渲染进成稿。⇒「统计口径 / 落库 word_count / 报告」与
「成稿内容」分叉。

【A/B 取证（本轮，真实生产库 backend/data/scheme_assistant.db，只读复算）】
`artifacts/r119/_probe_td4_wordcount.py` 实测 258 章中 3 章含 markdown 家族围栏，
其内部正文被现口径漏计：
  - sec=9eaee667《地基承载力特征值确定》现口径 1636 / 漏计 1495 / 实为 3131（缺 48%）
  - sec=11ebe09e《电梯井道墙体砌筑与抹灰》2400 / 1861 / 4261（缺 44%）
  - sec=a0b4093e《专职安全员现场巡查职责》1847 / 2017 / 3864（缺 52%）
后果：under/over 判定与续写/压缩门槛对这几章失真；UI/报表显示的字数比成稿少近一半。

【修法（加法式、默认零变更）】
① 判据唯一事实源上收 ``fence_common.MARKDOWN_FENCE_LANGS``（叶子模块，零依赖）；
② ``content_blocks._MARKDOWN_FENCE_LANGS`` 改为引用该源（保留模块内名，既有引用/
   测试导入点零改动）；
③ ``strip_fenced_code_blocks`` 新增 ``keep_markdown_fences``（**默认 False = 逐字
   等价旧行为**）；仅字数路径（``text_word_count`` / ``_normalize_for_readable``）传 True；
④ ``fence_spans``（压缩禁区）**不改** —— markdown 围栏内容计入字数但仍受压缩保护，
   与 mermaid 同款保守（刻意取舍，见本文件 D 组与收尾文档）。

【TD-5 · 刻意不改（R118 L2 要求本轮补的护栏）】
``read_fenced_block`` 用 ``tolerant_length=True``（content_blocks 多处）：一个更短的
同字符围栏行（如 3 反引号）**会**闭合更长的外层围栏（如 5 反引号），于是**同字符嵌套
围栏不成立**。这是为兼容「AI 把围栏长度写错」的**有意取舍，不是缺陷**；本文件 D 组把它
锁成显式契约，防止后人误当 BUG「修好」。
"""
from app.services import content_blocks as cb
from app.services import fence_common as fc
from app.services.content_blocks import (
    _parse_content_blocks,
    read_fenced_block,
)
from app.services.content_utils import (
    fence_spans,
    readable_word_count,
    strip_fenced_code_blocks,
    text_word_count,
)


# ============================================================
# A 组：markdown 家族围栏判据必须**三处同源**（单一事实源契约）
# ============================================================
class TestSingleSourceOfTruth:
    """TD-4 根因之一是「导出侧按 markdown 解析、字数侧按 markdown 剔除」两处 lang 列表各自漂移。"""

    def test_fence_common_exposes_the_constant(self):
        assert fc.MARKDOWN_FENCE_LANGS == ("markdown", "md", "gfm", "markdown-gfm")

    def test_content_blocks_alias_is_the_same_object(self):
        """content_blocks 不再自造字面量，而是引用 fence_common 同一对象。"""
        assert cb._MARKDOWN_FENCE_LANGS is fc.MARKDOWN_FENCE_LANGS

    def test_text_family_intentionally_excluded(self):
        """```text / ```plaintext 必须仍在集合外（纯文本代码块的既有契约）。"""
        for bad in ("text", "plaintext", "mdx", ""):
            assert bad not in fc.MARKDOWN_FENCE_LANGS, bad


# ============================================================
# B 组：字数口径 —— markdown 围栏内部计入，其余逐字不变
# ============================================================
class TestWordCountCaliber:
    MD_DOC = ("外层甲\n\n```markdown\n## 内标题\n内正文乙乙\n```\n\n外层丙")
    MERMAID = "正文\n```mermaid\nflowchart TD\n  SECRET_MERMAID-->B\n```"

    def test_markdown_inner_now_counted(self):
        kept = strip_fenced_code_blocks(self.MD_DOC, keep_markdown_fences=True)
        assert "内正文乙乙" in kept and "## 内标题" in kept
        assert "```markdown" not in kept  # 开围栏行被丢弃

    def test_markdown_count_strictly_greater_than_old_caliber(self):
        """修复的意义就是「比旧口径多算了 markdown 内部」，否则等于没修。"""
        old = len(strip_fenced_code_blocks(self.MD_DOC))  # 默认 keep=False
        assert text_word_count(self.MD_DOC) > old

    def test_mermaid_still_excluded_under_new_flag(self):
        """keep_markdown_fences=True 只放行 markdown 家族，mermaid 仍整段剔除。"""
        assert "SECRET_MERMAID" not in strip_fenced_code_blocks(
            self.MERMAID, keep_markdown_fences=True)

    def test_mixed_doc_counts_markdown_only(self):
        combo = ("文A\n```mermaid\nSECRET_M\n```\n"
                 "```markdown\nVISIBLE_MD\n```\n文Z")
        kept = strip_fenced_code_blocks(combo, keep_markdown_fences=True)
        assert "VISIBLE_MD" in kept
        assert "SECRET_M" not in kept

    # ---- 向后兼容：默认参数必须逐字节等于旧行为 ----
    _NON_MD_SAMPLES = [
        "正文\n```mermaid\nflowchart TD\nA-->B\n```\n尾",
        "```\nplain code line\n```",
        "```text\n### 这不该是标题\n纯文本\n```",
        "````\nfour-tick block\n````",
        "未闭合 ```python\n后续整段视为代码",
        "~~~python\nprint(1)\n~~~",
        "无围栏纯正文。",
    ]

    def test_default_option_is_byte_identical_to_legacy(self):
        """keep=False（默认）与「所有围栏一律剔除」的旧语义逐字一致（零回归锚）。"""
        for s in self._NON_MD_SAMPLES:
            assert strip_fenced_code_blocks(s) == strip_fenced_code_blocks(
                s, keep_markdown_fences=False)

    def test_option_only_moves_markdown(self):
        """不含 markdown 围栏的输入：keep=True 与 keep=False 输出恒等（开关被局限在 markdown）。"""
        for s in self._NON_MD_SAMPLES:
            assert strip_fenced_code_blocks(s) == strip_fenced_code_blocks(
                s, keep_markdown_fences=True)

    def test_readable_stays_le_text_with_markdown(self):
        """诊断读数 readable ≤ 权威读数 text 的既有不变式在 markdown 场景仍成立。"""
        assert readable_word_count(self.MD_DOC) <= text_word_count(self.MD_DOC)


# ============================================================
# C 组：端到端 —— 导出解析（渲染）与字数（统计）必须同源不漂移
# ============================================================
class TestRenderAndCountAgree:
    """TD-4 的病根是「导出把 markdown 当正文渲染、字数却把 markdown 当代码剔除」；
    本组断言两侧对同一 markdown 围栏的结论一致（都认它是正文）。"""

    def test_markdown_heading_is_body_block_and_is_counted(self):
        md = "```markdown\n## 真实标题\n这是真实正文内容。\n```"
        types = [b.get("type") for b in _parse_content_blocks(md)]
        assert "heading" in types, "R117：markdown 围栏内的 ## 必须成为标题块（渲染进成稿）"
        # 既然渲染进成稿，字数口径就不能再把这段正文剔掉
        assert text_word_count(md) >= len("这是真实正文内容。")
        assert "这是真实正文内容。" in strip_fenced_code_blocks(
            md, keep_markdown_fences=True)

    def test_text_fence_is_code_on_both_sides(self):
        """反向锁：```text 内 ### 不成标题（渲染侧）且不计字数（统计侧）—— 两侧同认代码。"""
        t = "```text\n### 不是标题\n纯文本X\n```"
        assert all(b.get("type") != "heading" for b in _parse_content_blocks(t))
        assert "纯文本X" not in strip_fenced_code_blocks(t, keep_markdown_fences=True)

    def test_compression_forbidden_zone_unchanged(self):
        """刻意取舍：fence_spans（压缩禁区）不改判据 —— markdown 围栏仍被框住，
        故 markdown 内部正文「计入字数但不被 AI 压缩改写」（与 mermaid 同款保守）。"""
        md = "正文\n```markdown\n内正文占位内容\n```\n尾"
        spans = fence_spans(md)
        assert spans, "fence_spans 必须仍框住 markdown 围栏（不被压缩改写）"
        start, end = spans[0]
        assert "内正文占位内容" in md[start:end]


# ============================================================
# D 组：TD-5 刻意不改 —— 同字符嵌套围栏不成立（tolerant_length 有意取舍）
# ============================================================
class TestTolerantLengthIsIntentional:
    """``read_fenced_block`` 用 ``tolerant_length=True``：更短的**同字符**围栏行也会
    闭合更长的外层围栏 ⇒ 同字符嵌套**不成立**。这是为兼容「AI 写错围栏长度」的
    **有意取舍，不是缺陷**；本组把它锁成显式契约，防止被误「修好」。"""

    def test_shorter_same_char_fence_closes_longer_fence(self):
        # 外层 5 反引号，内层裸 3 反引号 → 容错闭合（3 个 < 5 个仍算闭合）
        lines = ["`````", "inner1", "```", "after"]
        code, state, nxt = read_fenced_block(
            lines, 1, open_char="`", open_len=5)
        assert state == "closed"
        assert code == ["inner1"]
        assert nxt == 3, "3 反引号被当成 5 反引号外层的闭合 → 同字符嵌套不成立"

    def test_different_char_does_not_close(self):
        # 波浪号不能闭合反引号围栏 → 只有换不同围栏字符才能构造真嵌套（与 R118 D-1 同源）
        lines = ["````", "~~~", "inner", "````"]
        code, state, nxt = read_fenced_block(
            lines, 1, open_char="`", open_len=4)
        assert state == "closed"
        assert code == ["~~~", "inner"], "不同字符围栏行必须被当作内容而非闭合"
        assert nxt == 4

    def test_tolerant_source_documented(self):
        """根因锚：read_fenced_block 的闭合判定确实用 tolerant_length=True（若被改成
        False，同字符嵌套会变「成立」，本用例即应失败 —— 逼改动者显式面对该取舍）。"""
        import inspect
        src = inspect.getsource(read_fenced_block)
        assert "tolerant_length=True" in src
