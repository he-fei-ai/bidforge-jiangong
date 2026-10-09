"""测试 content_utils 的未闭合围栏自动修复。

覆盖：
  · find_unclosed_fences：奇/偶数、语言标签、长度对齐、波浪围栏、栈式配对
  · auto_fix_unclosed_fences：追加闭合、幂等、多围栏、混合场景
  · count_fences：与 find/auto_fix 口径一致性、行内不计数
  · 集成：修复后 DLV-07 判据清零；不干扰 text_word_count 与 word_status
"""
from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    auto_fix_unclosed_fences,
    count_fences,
    find_unclosed_fences,
    text_word_count,
    word_status_for,
)


class TestFindUnclosedFences:
    def test_empty(self):
        assert find_unclosed_fences("") == []
        assert find_unclosed_fences("   \n\n  ") == []

    def test_no_fences(self):
        assert find_unclosed_fences("普通正文。\n\n第二段。") == []

    def test_single_open(self):
        result = find_unclosed_fences("前言\n```\n未完成代码")
        assert len(result) == 1
        assert result[0]["line"] == 2
        assert result[0]["char"] == "`"
        assert result[0]["length"] == 3

    def test_pair_fences_balanced(self):
        assert find_unclosed_fences("```\nhello\n```") == []

    def test_lang_label_preserved(self):
        result = find_unclosed_fences("````mermaid\ngraph TD; A-->B")
        assert len(result) == 1
        assert result[0]["lang"] == "mermaid"
        assert result[0]["length"] == 4

    def test_tilde_fence(self):
        result = find_unclosed_fences("~~~python\ndef f(): pass")
        assert len(result) == 1
        assert result[0]["char"] == "~"
        assert result[0]["lang"] == "python"

    def test_char_kind_not_cross_matched(self):
        # 反引号开、波浪关 → 不同种，不算配对
        # 进入 ``` 后，~~~ 被视为代码内容（CommonMark 语义）
        result = find_unclosed_fences("```\nhello\n~~~")
        assert len(result) == 1
        assert result[0]["char"] == "`"

    def test_close_length_must_be_gte(self):
        # 4 反引号开 → 长度需 ≥4 才能闭合
        result = find_unclosed_fences("````\ncode")
        assert len(result) == 1
        assert result[0]["length"] == 4

    def test_inline_backticks_not_counted(self):
        assert find_unclosed_fences("使用 `code` 内联代码。") == []

    def test_nested_fence_semantics(self):
        # 3 反引号开，4 反引号关（长度 ≥ 即可闭合）
        assert find_unclosed_fences("```\nouter ``` inside\n````") == []

    def test_indented_fence(self):
        result = find_unclosed_fences("   ```\ncode here")
        assert len(result) == 1
        assert result[0]["line"] == 1

    def test_multi_byte_no_crash(self):
        assert len(find_unclosed_fences("前言\u200b\n```\n中文")) == 1

    def test_single_unclosed_after_multiple_closed(self):
        # 前两个已闭合，最后一个未闭合 → 只报最后一个
        src = "```\na\n```\n\n```\nb\n```\n\n```c\nunclosed"
        result = find_unclosed_fences(src)
        assert len(result) == 1
        assert result[0]["lang"] == "c"


class TestAutoFixUnclosedFences:
    def test_empty_returns_empty_log(self):
        fixed, log = auto_fix_unclosed_fences("")
        assert fixed == ""
        assert log == []

    def test_all_balanced_noop(self):
        src = "前言\n```\ncode\n```\n后续正文。"
        fixed, log = auto_fix_unclosed_fences(src)
        assert fixed == src
        assert log == []

    def test_single_unclosed_appended(self):
        src = "前言\n```\n未完成代码"
        fixed, log = auto_fix_unclosed_fences(src)
        assert count_fences(fixed) == 2
        assert len(log) == 1
        assert log[0]["action"] == "append_close"
        assert log[0]["line"] == 2

    def test_lang_preserved_in_log(self):
        _, log = auto_fix_unclosed_fences("````mermaid\ngraph TD; A-->B")
        assert log[0]["lang"] == "mermaid"
        assert log[0]["length"] == 4

    def test_idempotent(self):
        src = "```\n未完成"
        fixed_once, _ = auto_fix_unclosed_fences(src)
        fixed_twice, log2 = auto_fix_unclosed_fences(fixed_once)
        assert fixed_twice == fixed_once
        assert log2 == []

    def test_tilde_matches_tilde(self):
        src = "~~~python\nprint('x')"
        fixed, log = auto_fix_unclosed_fences(src)
        assert fixed.rstrip().endswith("~~~")
        assert log[0]["char"] == "~"
        assert find_unclosed_fences(fixed) == []

    def test_long_fences_preserve_length(self):
        fixed, _ = auto_fix_unclosed_fences("````\n内容")
        assert fixed.rstrip().endswith("````")

    def test_unclosed_after_closed_fences(self):
        # 前一个已闭合，最后一个未闭合
        src = "````a\ncode1\n````\n\n~~~\ncode2"
        fixed, log = auto_fix_unclosed_fences(src)
        assert len(log) == 1
        assert log[0]["char"] == "~"
        assert find_unclosed_fences(fixed) == []
        assert count_fences(fixed) == count_fences(src) + 1

    def test_preserves_original_text(self):
        src = "原文段落。\n```\ncode block"
        fixed, _ = auto_fix_unclosed_fences(src)
        assert src in fixed

    def test_no_duplicate_trailing_newline(self):
        src = "```\ncode\n"
        fixed, _ = auto_fix_unclosed_fences(src)
        assert fixed.endswith("```\n")
        assert not fixed.endswith("```\n\n")

    def test_balanced_input_untouched(self):
        src = "hello\n\n```\ncode\n```\n\nworld"
        fixed, log = auto_fix_unclosed_fences(src)
        assert fixed == src
        assert log == []

    def test_mixed_mermaid_and_chart_json(self):
        src = "```\n前言\n```\n\n```chart-json\n{\"type\":\"pie\",\"data\":[1,2,3]"
        fixed, log = auto_fix_unclosed_fences(src)
        assert len(log) == 1
        assert log[0]["lang"] == "chart-json"
        assert "```\n前言\n```" in fixed
        assert fixed.rstrip().endswith("```")


class TestCountFences:
    def test_empty(self):
        assert count_fences("") == 0

    def test_inline_ignored(self):
        assert count_fences("`code` inline text") == 0

    def test_balanced(self):
        assert count_fences("```\nfoo\n```") == 2

    def test_odd_unclosed(self):
        assert count_fences("```\n未完成") == 1

    def test_mixed_backtick_tilde(self):
        assert count_fences("```\n```\n\n~~~\n~~~") == 4

    def test_matches_postfix(self):
        src = "```a\ncode"
        assert count_fences(src) == 1
        fixed, _ = auto_fix_unclosed_fences(src)
        assert count_fences(fixed) == 2


class TestIntegration:
    def test_dlv07_parity_positive(self):
        src = "```\n未完成"
        assert src.count("```") % 2 == 1
        assert len(find_unclosed_fences(src)) == 1

    def test_dlv07_parity_negative(self):
        src = "```\nok\n```"
        assert src.count("```") % 2 == 0
        assert find_unclosed_fences(src) == []

    def test_fix_resolves_dlv07_flag(self):
        src = "```mermaid\ngraph TD; A-->B"
        assert src.count("```") % 2 == 1
        fixed, _ = auto_fix_unclosed_fences(src)
        assert fixed.count("```") % 2 == 0

    def test_word_count_not_polluted(self):
        body = "正文 100 字。" * 5
        src = body + "\n```\nchart-json\n{1,2,3,4,5,6}"
        wc_before = text_word_count(src)
        fixed, _ = auto_fix_unclosed_fences(src)
        wc_after = text_word_count(fixed)
        # 修复前后都代表正文字数（不含图表代码），允许 1 字符级差异
        # （源于围栏前后空行归属差异）
        assert abs(wc_after - wc_before) <= 1
        # 图表 JSON 中的字符不应被计入
        assert wc_after < len(body) + 5

    def test_word_status_unchanged_on_clean_body(self):
        short = "字" * 100
        fixed, log = auto_fix_unclosed_fences(short)
        assert fixed == short
        assert log == []
        assert word_status_for(text_word_count(short), DEFAULT_WORD_BUDGET) == "under"


# =============================================================================
# BUG-3 回归：AI 常见「4-backtick-open + 3-backtick-close」被误判未闭合
# =============================================================================
class TestAutoFixLenientFences:
    """auto_fix_unclosed_fences 使用宽松语义（同种字符即闭合）；
    而 find_unclosed_fences 保持严格 CommonMark（长度≥开围栏且无标签）。
    两者口径的差异是有意为之，分别满足落库防线与展示一致性的诉求。
    """

    def test_bug_3_lenient_auto_fix_does_not_append_close(self):
        """BUG-3：4-开 3-关的 AI 模式 → auto_fix 不应补闭合（视为已闭合）。"""
        src = "````mermaid\ngraph TD; A-->B\n```\n尾段"
        fixed, log = auto_fix_unclosed_fences(src)
        assert fixed == src, f"未被误判为未闭合，原文保留；实际：{fixed!r}"
        assert log == [], "不应产生修复日志"

    def test_bug_3_strict_still_reports_unclosed(self):
        """严格版 find_unclosed_fences 依然把 4-开 3-关 视为未闭合（向后兼容）。"""
        src = "````mermaid\ngraph TD; A-->B\n```"
        result = find_unclosed_fences(src)
        assert len(result) == 1
        assert result[0]["line"] == 1
        assert result[0]["char"] == "`"
        assert result[0]["length"] == 4

    def test_bug_3_lenient_matches_content_blocks_parser(self):
        """与 content_blocks.parse_fence_line/read_fenced_block 的宽松口径对齐：
        3 反引号即可闭合 4-反引号开围栏（同一模块内两套解析器结论一致）。"""
        from app.services.content_blocks import parse_fence_line, read_fenced_block

        src = "````mermaid\ngraph TD; A-->B\n```"
        lines = src.split("\n")
        # 第 0 行是 4-反引号开围栏
        open_pf = parse_fence_line(lines[0])
        assert open_pf is not None
        assert open_pf[0] == "`"
        # 第 2 行是 3-反引号，被宽松语义视为可闭合（同种字符即闭合）
        close_pf = parse_fence_line(lines[2])
        assert close_pf is not None
        assert close_pf[0] == "`"
        # read_fenced_block 从 body_start=1 起读，应在行 2 停止（返回 next_index=3）
        code_lines, state, next_idx = read_fenced_block(lines, body_start=1)
        assert state == "closed"
        assert next_idx == 3
        assert code_lines == ["graph TD; A-->B"]

    def test_bug_3_true_unclosed_still_repaired(self):
        """真未闭合仍会被 auto_fix 补上闭合围栏。"""
        src = "````mermaid\ngraph TD; A-->B"
        fixed, log = auto_fix_unclosed_fences(src)
        assert fixed != src
        assert fixed.rstrip().endswith("````") or fixed.rstrip().endswith("```")
        assert len(log) == 1
        assert log[0]["char"] == "`"

    def test_bug_3_idempotent_after_fix(self):
        """修复后再次 auto_fix 应无变化（幂等）。"""
        src = "````mermaid\ngraph TD; A-->B"
        fixed1, _ = auto_fix_unclosed_fences(src)
        fixed2, log2 = auto_fix_unclosed_fences(fixed1)
        assert fixed2 == fixed1
        assert log2 == []

    def test_bug_3_tilde_lenient_same_char_kind(self):
        """宽松语义对 ~~~ 围栏同样生效：3 波浪闭合 4 波浪开。"""
        src = "~~~~python\ndef f(): pass\n~~~\n尾段"
        fixed, log = auto_fix_unclosed_fences(src)
        assert fixed == src
        assert log == []

