"""正文字数压缩（content_shrink）纯函数测试

覆盖：
- 保护区间收集（代码围栏 / GFM 表格 / 图片 / <img>）
- 操作解析（围栏剥离、非法操作过滤、空 operations 拒绝）
- 操作应用（逐字唯一校验、保护区间命中拒绝、delete / replace、倒序应用偏移正确）
"""

import pytest
from app.services.content_shrink import (
    apply_shrink_operations,
    collect_protected_ranges,
    parse_shrink_operations,
)
from app.services.content_utils import text_word_count


def test_protected_ranges_fence_table_image():
    content = (
        "前文\n```mermaid\nflowchart TD\nA-->B\n```\n"
        "| 列1 | 列2 |\n| --- | --- |\n| a | b |\n"
        '![图片](http://x/y.png) 文本 <img src="z" />\n'
    )
    ranges = collect_protected_ranges(content)
    # mermaid 围栏、表格块、图片、<img> 至少 4 个保护区
    assert len(ranges) >= 4
    # 围栏内容不在任何保护区之外
    fence_start = content.index("```mermaid")
    assert any(s <= fence_start < e for s, e in ranges)


def test_protected_ranges_plain_text_empty():
    assert collect_protected_ranges("") == []
    assert collect_protected_ranges("普通正文，没有保护区。") == []


def test_protected_ranges_tilde_fence():
    # ✅ 2026-09-28：~~~ 波浪线围栏也必须受保护（旧正则只认反引号）
    content = '前文\n~~~chart-json\n{"a": 1}\n~~~\n后文\n'
    ranges = collect_protected_ranges(content)
    fence_start = content.index("~~~chart-json")
    assert any(s <= fence_start < e for s, e in ranges)


def test_parse_operations_valid_and_fence_stripped():
    raw = '```json\n{"operations":[{"operation":"replace","target_text":"旧句","content":"新句"},{"operation":"delete","target_text":"废话"}]}\n```'
    ops = parse_shrink_operations(raw)
    assert len(ops) == 2
    assert ops[0] == {"operation": "replace", "target_text": "旧句", "content": "新句"}
    assert ops[1]["operation"] == "delete"


def test_parse_operations_rejects_unauthorized_and_empty():
    # 越权操作（rewrite_full / insert）被过滤；剩余为空 → ValueError
    raw = '{"operations":[{"operation":"rewrite_full","target_text":"x","content":"整篇"},{"operation":"insert","target_text":"y"}]}'
    with pytest.raises(ValueError):
        parse_shrink_operations(raw)
    # 无 operations
    with pytest.raises(ValueError):
        parse_shrink_operations('{"ok":true}')
    # 空输入
    with pytest.raises(ValueError):
        parse_shrink_operations("")


def test_apply_operations_replace_and_delete():
    content = "第一段。\n第二段重复表述。\n第三段。\n空泛套话收尾。"
    ops = [
        {"operation": "replace", "target_text": "第二段重复表述。", "content": "精简句。"},
        {"operation": "delete", "target_text": "\n空泛套话收尾。"},
    ]
    out = apply_shrink_operations(content, ops)
    assert out == "第一段。\n精简句。\n第三段。"
    # 字数确实下降（口径与全项目一致：剔除围栏）
    assert text_word_count(out) < text_word_count(content)


def test_apply_operations_rejects_duplicate_target():
    content = "重复句。中间。重复句。"
    ops = [{"operation": "delete", "target_text": "重复句。"}]
    with pytest.raises(ValueError):
        apply_shrink_operations(content, ops)


def test_apply_operations_rejects_missing_target():
    with pytest.raises(ValueError):
        apply_shrink_operations("正文A", [{"operation": "delete", "target_text": "不存在的片段"}])


def test_apply_operations_rejects_protected_overlap():
    content = "前文\n```mermaid\nflowchart TD\nA-->B\n```\n后文。"
    # target 跨越整个围栏 → 命中保护区，必须拒绝
    ops = [{"operation": "delete", "target_text": content[content.index("```mermaid") : content.index("后文")]}]
    with pytest.raises(ValueError):
        apply_shrink_operations(content, ops)
    # 表格行同样受保护
    table_content = "文字。\n| a | b |\n| - | - |\n结尾。"
    ops2 = [{"operation": "delete", "target_text": "| a | b |\n| - | - |"}]
    with pytest.raises(ValueError):
        apply_shrink_operations(table_content, ops2)


def test_apply_operations_multi_offsets_stable():
    """倒序应用：两个 delete 的偏移互不干扰"""
    content = "AAA BBB CCC DDD"
    ops = [
        {"operation": "delete", "target_text": "AAA "},
        {"operation": "delete", "target_text": " CCC"},
    ]
    out = apply_shrink_operations(content, ops)
    assert out == "BBB DDD"


# ============================================================
# ✅ R179（2026-11-05 · 列表样式被拍平）：列表块纳入压缩禁区
#   旧实现只保护围栏/表格/图片，AI 压缩操作可以整块删除/替换列表项，
#   导致成稿里的列表被压成一段散文（层级丢失）。以下用例锁定新口径。
# ============================================================


def test_protected_ranges_cover_unordered_list():
    content = "前言。\n- 第一项\n- 第二项\n- 第三项\n结论。"
    ranges = collect_protected_ranges(content)
    item_start = content.index("- 第一项")
    assert any(s <= item_start < e for s, e in ranges), "无序列表行必须落在保护区内"


def test_protected_ranges_cover_ordered_and_bracket_numbering():
    content = "前言。\n1、甲项\n2、乙项\n（3）丙项\n结论。"
    ranges = collect_protected_ranges(content)
    for token in ("1、甲项", "（3）丙项"):
        start = content.index(token)
        assert any(s <= start < e for s, e in ranges), f"{token} 必须被保护"


def test_protected_ranges_cover_list_continuation_line():
    content = "前言。\n- 第一项，说明文字\n  折行续写的补充说明\n结论。"
    ranges = collect_protected_ranges(content)
    cont = content.index("  折行续写")
    assert any(s <= cont < e for s, e in ranges), "缩进续行属于列表块，必须同受保护"


def test_apply_operations_rejects_list_delete_and_replace():
    content = "前言。\n- 第一项\n- 第二项\n- 第三项\n结论。"
    # 1) 直接删除列表项 → 拒绝（否则列表被拍平成散文）
    with pytest.raises(ValueError, match="列表"):
        apply_shrink_operations(content, [{"operation": "delete", "target_text": "- 第二项\n"}])
    # 2) 跨列表的整段替换 → 拒绝
    with pytest.raises(ValueError, match="列表"):
        apply_shrink_operations(
            content,
            [{"operation": "replace", "target_text": "- 第一项\n- 第二项\n- 第三项", "content": "合并成一句话。"}],
        )


def test_apply_operations_still_allows_plain_paragraph_around_list():
    content = "前言段落比较冗长需要压缩。\n- 第一项\n- 第二项\n结论段落可以压缩。"
    ops = [
        {"operation": "replace", "target_text": "前言段落比较冗长需要压缩。", "content": "前言精简。"},
        {"operation": "replace", "target_text": "结论段落可以压缩。", "content": "结论精简。"},
    ]
    out = apply_shrink_operations(content, ops)
    # 列表行逐字保留，只压缩了列表前后的普通段落
    assert "- 第一项\n- 第二项" in out
    assert "前言精简。" in out and "结论精简。" in out


# ===========================================================================
# ✅ R180（2026-11-05）：target_text 空白/全半角差异不再误报（规范化回退）
# ===========================================================================


def test_apply_operations_normalizes_whitespace_mismatch():
    """AI 复述 target 时换了换行/多空格 → 规范化后唯一命中，正常应用压缩。"""
    content = "第一段。\n\n第二段   有一些   冗余表述，可以删减。\n\n第三段。"
    out = apply_shrink_operations(
        content,
        [
            {
                "operation": "replace",
                "target_text": "第二段 有一些 冗余表述,可以删减。",  # 空白折叠 + 全角逗号变半角
                "content": "第二段已精简。",
            }
        ],
    )
    assert "第二段已精简。" in out
    assert "冗余表述" not in out
    # 区间外的段落必须逐字保留
    assert out.startswith("第一段。") and out.endswith("第三段。")


def test_apply_operations_normalizes_fullwidth_parentheses():
    content = "按（可选）方式处理该段落。"
    out = apply_shrink_operations(
        content, [{"operation": "replace", "target_text": "按(可选)方式处理该段落。", "content": "按需处理。"}]
    )
    assert out == "按需处理。"


def test_apply_operations_normalized_ambiguity_still_rejected():
    """规范化后出现多次 → 仍按「不唯一」拒绝（不因回退而放松唯一性）。"""
    content = "同一句话。\n\n同一句话。"
    with pytest.raises(ValueError, match="不唯一"):
        apply_shrink_operations(
            content, [{"operation": "replace", "target_text": "同一句话。", "content": "替换。"}]
        )


def test_apply_operations_normalized_protected_still_rejected():
    """规范化回退的偏移必须映射回原文，保护区重叠仍拒绝。"""
    content = "前文\n| 列1 | 列2 |\n| --- | --- |\n| a | b |\n后文"
    with pytest.raises(ValueError, match="表格"):
        apply_shrink_operations(
            content,
            [
                {
                    "operation": "delete",
                    "target_text": "|  列1  |  列2  |\n| --- | --- |\n| a | b |",  # 多空格差异
                }
            ],
        )
