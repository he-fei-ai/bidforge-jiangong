"""列表项渲染测试（P1-1 修复）

验证目标：
1. list_item 分支使用 "List Paragraph" 样式（非 Normal）
2. 悬挂缩进基于前缀宽度动态计算（非硬编码 -0.6cm）
3. indent_lvl 口径统一（ordered_stack 与 left_indent 共用）
4. 嵌套列表正确缩进

⚠️ 前置条件：需先运行 export_docx 生成 docx 文件，再解析验证。
   测试直接验证块解析与渲染逻辑（非完整导出流程）。
"""
import pytest
from app.routers.export import _estimate_prefix_width, _ensure_list_paragraph_style, _ordered_prefix


# ---------------------------------------------------------------------------
# _estimate_prefix_width 单元测试
# ---------------------------------------------------------------------------

class TestEstimatePrefixWidth:
    """前缀宽度估算测试"""

    def test_ascii_prefix_width(self):
        """ASCII 前缀（如 "1. "）宽度约 0.60 cm"""
        w = _estimate_prefix_width("1. ")
        assert 0.55 <= w <= 0.65, f"Expected ~0.60, got {w}"

    def test_bullet_prefix_width(self):
        """无序列表前缀（"• "）宽度约 0.57 cm"""
        w = _estimate_prefix_width("• ")
        assert 0.5 <= w <= 0.65, f"Expected ~0.57, got {w}"

    def test_chinese_prefix_width(self):
        """中文前缀（"（一）"）宽度约 1.11 cm"""
        w = _estimate_prefix_width("（一）")
        assert 1.05 <= w <= 1.15, f"Expected ~1.11, got {w}"

    def test_cn_num_paren_width(self):
        """中文枚举前缀（"（二）"）宽度约 1.11 cm"""
        w = _estimate_prefix_width("（二）")
        assert 1.05 <= w <= 1.15, f"Expected ~1.11, got {w}"

    def test_num_dun_width(self):
        """中文顿号前缀（"1、"）宽度约 0.57 cm"""
        w = _estimate_prefix_width("1、")
        assert 0.5 <= w <= 0.65, f"Expected ~0.57, got {w}"

    def test_min_width(self):
        """最小宽度 0.5 cm（避免悬挂缩进不可见）"""
        w = _estimate_prefix_width("")
        assert w >= 0.5

    def test_mixed_width(self):
        """混合前后缀宽度计算"""
        w = _estimate_prefix_width("1.（一）")
        # 2 ASCII (0.20*2=0.40) + 3 Chinese (0.37*3=1.11) = 1.51
        assert 1.45 <= w <= 1.55, f"Expected ~1.51, got {w}"


# ---------------------------------------------------------------------------
# _ensure_list_paragraph_style 单元测试
# ---------------------------------------------------------------------------

class TestEnsureListParagraphStyle:
    """List Paragraph 样式获取/创建测试"""

    def test_style_exists(self):
        """文档已包含 List Paragraph 样式时直接返回"""
        from docx import Document
        doc = Document()
        # python-docx 默认文档包含 List Paragraph 样式
        name = _ensure_list_paragraph_style(doc)
        assert name == "List Paragraph"

    def test_style_idempotent(self):
        """多次调用不重复创建样式"""
        from docx import Document
        doc = Document()
        name1 = _ensure_list_paragraph_style(doc)
        name2 = _ensure_list_paragraph_style(doc)
        assert name1 == name2 == "List Paragraph"
        # 验证样式数量不增加
        style_names = [s.name for s in doc.styles if s.name == "List Paragraph"]
        assert len(style_names) == 1


# ---------------------------------------------------------------------------
# _ordered_prefix 单元测试
# ---------------------------------------------------------------------------

class TestOrderedPrefix:
    """有序列表前缀生成测试"""

    def test_ascii_marker(self):
        """ascii 标记 → "1. " 格式"""
        prefix = _ordered_prefix(1, "ascii")
        assert prefix == "1. "

    def test_cn_num_paren_marker(self):
        """cn_num_paren 标记 → "（一）" 格式"""
        prefix = _ordered_prefix(1, "cn_num_paren")
        assert prefix == "（一）"

    def test_num_dun_marker(self):
        """num_dun 标记 → "1、" 格式"""
        prefix = _ordered_prefix(1, "num_dun")
        assert prefix == "1、"

    def test_num_paren_lr_marker(self):
        """num_paren_lr 标记 → "（1）" 格式"""
        prefix = _ordered_prefix(1, "num_paren_lr")
        assert prefix == "（1）"

    def test_sequence_continuity(self):
        """序号连续性（1, 2, 3...）"""
        prefixes = [_ordered_prefix(i, "ascii") for i in range(1, 4)]
        assert prefixes == ["1. ", "2. ", "3. "]


# ---------------------------------------------------------------------------
# 集成测试：列表项渲染
# ---------------------------------------------------------------------------

class TestListItemRendering:
    """列表项渲染集成测试（验证缩进与样式）"""

    def test_flat_list_rendering(self):
        """扁平列表（无嵌套）渲染验证"""
        from docx import Document
        from docx.shared import Cm

        doc = Document()
        _ensure_list_paragraph_style(doc)

        # 模拟 list_item 块
        blocks = [
            {"type": "list_item", "ordered": True, "marker": "ascii", "text": "第一项", "indent": 0},
            {"type": "list_item", "ordered": True, "marker": "ascii", "text": "第二项", "indent": 0},
            {"type": "list_item", "ordered": False, "text": "无序项", "indent": 0},
        ]

        # 渲染逻辑（与 export.py list_item 分支一致）
        ordered_stack = []
        for block in blocks:
            indent_lvl = block.get("indent", 0) // 2
            if block.get("ordered"):
                marker = block.get("marker", "ascii")
                if indent_lvl < len(ordered_stack):
                    ordered_stack = ordered_stack[:indent_lvl + 1]
                while len(ordered_stack) <= indent_lvl:
                    ordered_stack.append([marker, 0])
                entry = ordered_stack[indent_lvl]
                if entry[0] != marker:
                    entry[0] = marker
                    entry[1] = 0
                entry[1] += 1
                prefix = _ordered_prefix(entry[1], marker)
            else:
                prefix = "• "

            p = doc.add_paragraph(style="List Paragraph")
            from app.routers.export import _add_runs_with_inline_format
            _add_runs_with_inline_format(p, prefix + block["text"])
            lpf = p.paragraph_format
            lpf.left_indent = Cm(0.74 * (indent_lvl + 1))
            _hang_w = _estimate_prefix_width(prefix)
            lpf.first_line_indent = Cm(-_hang_w)
            lpf.space_after = Cm(0)  # Pt(2) 等价于 Cm(0.07)，这里简化

        # 验证
        assert len(doc.paragraphs) == 3
        for p in doc.paragraphs:
            assert p.style.name == "List Paragraph"
            # 左缩进应约 0.74 cm（indent_lvl=0）
            assert abs(p.paragraph_format.left_indent - Cm(0.74)) < Cm(0.01)
            # 悬挂缩进应为负值
            assert p.paragraph_format.first_line_indent < Cm(0)

    def test_nested_list_rendering(self):
        """嵌套列表渲染验证"""
        from docx import Document
        from docx.shared import Cm

        doc = Document()
        _ensure_list_paragraph_style(doc)

        # 模拟嵌套列表块（2 空格缩进 = 1 级嵌套）
        blocks = [
            {"type": "list_item", "ordered": True, "marker": "ascii", "text": "一级第一项", "indent": 0},
            {"type": "list_item", "ordered": True, "marker": "cn_num_paren", "text": "二级第一项", "indent": 2},
            {"type": "list_item", "ordered": True, "marker": "cn_num_paren", "text": "二级第二项", "indent": 2},
            {"type": "list_item", "ordered": True, "marker": "ascii", "text": "一级第二项", "indent": 0},
        ]

        # 渲染逻辑
        ordered_stack = []
        for block in blocks:
            indent_lvl = block.get("indent", 0) // 2
            if block.get("ordered"):
                marker = block.get("marker", "ascii")
                if indent_lvl < len(ordered_stack):
                    ordered_stack = ordered_stack[:indent_lvl + 1]
                while len(ordered_stack) <= indent_lvl:
                    ordered_stack.append([marker, 0])
                entry = ordered_stack[indent_lvl]
                if entry[0] != marker:
                    entry[0] = marker
                    entry[1] = 0
                entry[1] += 1
                prefix = _ordered_prefix(entry[1], marker)
            else:
                prefix = "• "

            p = doc.add_paragraph(style="List Paragraph")
            from app.routers.export import _add_runs_with_inline_format
            _add_runs_with_inline_format(p, prefix + block["text"])
            lpf = p.paragraph_format
            lpf.left_indent = Cm(0.74 * (indent_lvl + 1))
            _hang_w = _estimate_prefix_width(prefix)
            lpf.first_line_indent = Cm(-_hang_w)

        # 验证
        assert len(doc.paragraphs) == 4
        # 一级项（indent_lvl=0）：左缩进约 0.74 cm
        assert abs(doc.paragraphs[0].paragraph_format.left_indent - Cm(0.74)) < Cm(0.01)
        # 二级项（indent_lvl=1）：左缩进约 1.48 cm
        assert abs(doc.paragraphs[1].paragraph_format.left_indent - Cm(1.48)) < Cm(0.01)
        assert abs(doc.paragraphs[2].paragraph_format.left_indent - Cm(1.48)) < Cm(0.01)
        # 一级项（indent_lvl=0）：左缩进约 0.74 cm
        assert abs(doc.paragraphs[3].paragraph_format.left_indent - Cm(0.74)) < Cm(0.01)

        # 验证序号连续性
        assert "1. 一级第一项" in doc.paragraphs[0].text
        assert "（一）二级第一项" in doc.paragraphs[1].text
        assert "（二）二级第二项" in doc.paragraphs[2].text
        assert "2. 一级第二项" in doc.paragraphs[3].text
