"""列表缩进续行解析回归（R182 · 2026-11-07 · 列表样式被拍平）

缺陷（修复前实测）：
    ``1. 施工准备：`` 后面缩进 3 空格的续行被解析成 ``paragraph`` 块。
    导出侧 paragraph 走 ``doc.add_paragraph()``（仅首行缩进，无列表左缩进/
    悬挂缩进），而成稿预览按 Markdown 语义把缩进续行算作上一列表项内容
    —— 同一条正文在预览里属于列表、在 DOCX 里被拍平成普通段落。

修复：解析期把「视觉缩进（tab=4）≥2 且比上一列表项更深」的续行并入
上一 ``list_item`` 的 text（换行保留），不再产出独立 paragraph。
"""

from app.services.content_blocks import _parse_content_blocks


def _blocks(content: str) -> list[dict]:
    return [b for b in _parse_content_blocks(content) if isinstance(b, dict)]


class TestListContinuationMerged:
    def test_ordered_item_continuation_merged(self):
        content = "1. 施工准备：\n   检查场地，确认水电接入。\n   清点材料与机具。\n2. 测量放线。"
        blocks = _blocks(content)
        assert [b["type"] for b in blocks] == ["list_item", "list_item"]
        assert blocks[0]["text"] == "施工准备：\n检查场地，确认水电接入。\n清点材料与机具。"
        assert blocks[1]["text"] == "测量放线。"

    def test_unordered_item_continuation_merged(self):
        content = "- 安全管理\n  建立三级教育台账。\n- 质量管理"
        blocks = _blocks(content)
        assert [b["type"] for b in blocks] == ["list_item", "list_item"]
        assert blocks[0]["text"] == "安全管理\n建立三级教育台账。"

    def test_tab_indented_continuation_merged(self):
        content = "1. 第一条\n\t制表符续行"
        blocks = _blocks(content)
        assert [b["type"] for b in blocks] == ["list_item"]
        assert blocks[0]["text"] == "第一条\n制表符续行"

    def test_nested_item_continuation_merged(self):
        content = "1. 外层\n  1. 内层\n     内层续行\n2. 外层二"
        blocks = _blocks(content)
        assert [b["type"] for b in blocks] == ["list_item"] * 3
        assert blocks[1]["indent"] == 2
        assert blocks[1]["text"] == "内层\n内层续行"
        assert blocks[2]["text"] == "外层二"


class TestListContinuationNonTargets:
    def test_indented_paragraph_without_list_stays_paragraph(self):
        content = "  开头的缩进正文，前面没有列表项。"
        blocks = _blocks(content)
        assert [b["type"] for b in blocks] == ["paragraph"]

    def test_single_space_continuation_not_merged(self):
        content = "1. 第一项\n 一个空格不算列表缩进"
        blocks = _blocks(content)
        assert [b["type"] for b in blocks] == ["list_item", "paragraph"]

    def test_blank_line_between_does_not_break_list_item(self):
        content = "1. 第一项\n\n   空行后的缩进续行"
        blocks = _blocks(content)
        assert [b["type"] for b in blocks] == ["list_item"]
        assert blocks[0]["text"] == "第一项\n空行后的缩进续行"

    def test_indented_table_under_list_still_table(self):
        content = "1. 验收内容\n   | 项目 | 标准 |\n   | 强度 | C30 |"
        blocks = _blocks(content)
        assert [b["type"] for b in blocks] == ["list_item", "table"]
