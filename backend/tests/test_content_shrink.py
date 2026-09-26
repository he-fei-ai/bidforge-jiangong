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
        "![图片](http://x/y.png) 文本 <img src=\"z\" />\n"
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


def test_parse_operations_valid_and_fence_stripped():
    raw = "```json\n{\"operations\":[{\"operation\":\"replace\",\"target_text\":\"旧句\",\"content\":\"新句\"},{\"operation\":\"delete\",\"target_text\":\"废话\"}]}\n```"
    ops = parse_shrink_operations(raw)
    assert len(ops) == 2
    assert ops[0] == {"operation": "replace", "target_text": "旧句", "content": "新句"}
    assert ops[1]["operation"] == "delete"


def test_parse_operations_rejects_unauthorized_and_empty():
    # 越权操作（rewrite_full / insert）被过滤；剩余为空 → ValueError
    raw = "{\"operations\":[{\"operation\":\"rewrite_full\",\"target_text\":\"x\",\"content\":\"整篇\"},{\"operation\":\"insert\",\"target_text\":\"y\"}]}"
    with pytest.raises(ValueError):
        parse_shrink_operations(raw)
    # 无 operations
    with pytest.raises(ValueError):
        parse_shrink_operations("{\"ok\":true}")
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
    ops = [{"operation": "delete", "target_text": content[content.index("```mermaid"):content.index("后文")]}]
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
