"""export.py 单元测试

覆盖 _parse_content_blocks markdown 格式解析

测试策略：
- 逐分支覆盖：空内容/段落/标题/代码块/mermaid/表格/图表标记/列表
- 验证返回的 block 列表结构（type + 关键字段）
- 边界用例：空行跳过、表格缺分隔行降级为段落、mermaid 识别为 chart
"""
import pytest
from app.routers.export import _detect_duplicate_sections, _parse_content_blocks


# ============================================================
# 空内容 / 空行
# ============================================================
class TestParseContentBlocksEmpty:
    """空内容与空行处理"""

    def test_empty_string(self):
        assert _parse_content_blocks("") == []

    def test_none_input(self):
        assert _parse_content_blocks(None) == []

    def test_only_blank_lines(self):
        """纯空行：全部跳过"""
        assert _parse_content_blocks("\n\n\n") == []

    def test_leading_trailing_blank_lines(self):
        """首尾空行被跳过，中间段落保留"""
        blocks = _parse_content_blocks("\n\n正文内容\n\n")
        assert len(blocks) == 1
        assert blocks[0] == {"type": "paragraph", "text": "正文内容"}


# ============================================================
# 段落
# ============================================================
class TestParseContentBlocksParagraph:
    """普通段落"""

    def test_single_paragraph(self):
        blocks = _parse_content_blocks("这是一段文字")
        assert blocks == [{"type": "paragraph", "text": "这是一段文字"}]

    def test_multiple_paragraphs(self):
        blocks = _parse_content_blocks("第一段\n\n第二段\n\n第三段")
        assert len(blocks) == 3
        assert all(b["type"] == "paragraph" for b in blocks)
        assert blocks[0]["text"] == "第一段"
        assert blocks[1]["text"] == "第二段"
        assert blocks[2]["text"] == "第三段"


# ============================================================
# 标题
# ============================================================
class TestParseContentBlocksHeading:
    """markdown 标题 # ~ ######"""

    def test_h1(self):
        blocks = _parse_content_blocks("# 一级标题")
        # ✅ 编号统一（2026-09-25）：heading 块新增 src_line（0 基源行号，正文子标题规范化用）
        assert blocks == [{"type": "heading", "level": 1, "text": "一级标题", "src_line": 0}]

    def test_h2(self):
        blocks = _parse_content_blocks("## 二级标题")
        assert blocks == [{"type": "heading", "level": 2, "text": "二级标题", "src_line": 0}]

    def test_h3(self):
        blocks = _parse_content_blocks("### 三级标题")
        assert blocks == [{"type": "heading", "level": 3, "text": "三级标题", "src_line": 0}]

    def test_h6(self):
        blocks = _parse_content_blocks("###### 六级标题")
        assert blocks == [{"type": "heading", "level": 6, "text": "六级标题", "src_line": 0}]

    def test_heading_with_trailing_spaces(self):
        """标题文本 strip 后存储"""
        blocks = _parse_content_blocks("#  标题文字  ")
        assert blocks[0]["text"] == "标题文字"

    def test_sentence_like_markdown_heading_demoted(self):
        """✅ 2026-09-19：AI 把正文句写成井号标题（句末带句读标点）→ 降级为段落"""
        blocks = _parse_content_blocks("#### 4 作业完成后清理现场，做到工完场清。")
        assert blocks == [{
            "type": "paragraph",
            "text": "4 作业完成后清理现场，做到工完场清。",
        }]

    def test_numbered_list_item_not_promoted_to_heading(self):
        """✅ 2026-09-19：编号 + 句末分号的列举条目不得被提升为标题"""
        blocks = _parse_content_blocks("3.1.12 身份证复印件、照片；")
        assert blocks[0]["type"] != "heading"


# ============================================================
# 代码块
# ============================================================
class TestParseContentBlocksCode:
    """代码块 ```...```"""

    def test_plain_code_block(self):
        content = "```python\nprint('hello')\n```"
        blocks = _parse_content_blocks(content)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code"
        assert blocks[0]["lines"] == ["print('hello')"]

    def test_code_block_no_lang(self):
        content = "```\nsome code\n```"
        blocks = _parse_content_blocks(content)
        assert blocks[0]["type"] == "code"
        assert blocks[0]["lines"] == ["some code"]

    def test_mermaid_code_block_becomes_chart(self):
        """mermaid 代码块识别为 chart 类型"""
        content = "```mermaid\ngraph TD\nA-->B\n```"
        blocks = _parse_content_blocks(content)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "chart"
        assert blocks[0]["chart_type"] == "flowchart"

    def test_multiline_code(self):
        content = "```js\nconst a = 1;\nconst b = 2;\nconsole.log(a+b);\n```"
        blocks = _parse_content_blocks(content)
        assert blocks[0]["type"] == "code"
        assert blocks[0]["lines"] == ["const a = 1;", "const b = 2;", "console.log(a+b);"]


# ============================================================
# AI 配图占位块（```ai_image）
# ============================================================
class TestParseContentBlocksAiImage:
    """未生成的 ```ai_image 占位块必须单独成块，不得落进通用 code 分支。

    BUG 回归（2026-09-17）：旧实现把 ```ai_image 当普通代码块 →
    `_add_code_block` 把内部 JSON（prompt/style/title）原样印进交付文档，
    AI 绘图提示词泄漏到成稿。现改为 type="ai_image"，由 write_section
    跳过（不写裸 JSON、不做红字占位、不占用图号）。
    """

    _FENCE = (
        "本节配图示意如下：\n\n"
        "```ai_image\n"
        '{"prompt": "深基坑支护结构剖面", "style": "engineering_diagram",'
        ' "title": "支护剖面图"}\n'
        "```\n\n"
        "后续正文段落。\n"
    )

    def test_ai_image_fence_is_own_block_type(self):
        blocks = _parse_content_blocks(self._FENCE)
        assert [b["type"] for b in blocks] == ["paragraph", "ai_image", "paragraph"]
        ai_block = blocks[1]
        assert ai_block["title"] == "支护剖面图"
        assert "prompt" in ai_block["code"]

    def test_ai_image_never_becomes_code_block(self):
        """核心断言：不得出现 type=code 的块（否则提示词 JSON 会印进 DOCX）。"""
        blocks = _parse_content_blocks(self._FENCE)
        assert all(b["type"] != "code" for b in blocks)

    def test_ai_image_malformed_json_still_not_code(self):
        """JSON 坏掉（AI 输出截断）时也必须成 ai_image 块，不能退化成代码块。"""
        content = "```ai_image\n{not valid json\n```"
        blocks = _parse_content_blocks(content)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "ai_image"
        assert blocks[0]["title"] == ""

    def test_generated_ai_image_line_still_image_block(self):
        """生成成功后正文已被改写为 ![title](url)，仍走既有 image 路径。"""
        content = "![支护剖面图](https://cdn.example.com/a.png)"
        blocks = _parse_content_blocks(content)
        assert blocks[0]["type"] == "image"
        assert blocks[0]["alt"] == "支护剖面图"


# ============================================================
# 表格
# ============================================================
class TestParseContentBlocksTable:
    """GFM 表格"""

    def test_valid_table(self):
        """合法表格（含分隔行）→ type=table"""
        content = "| 列A | 列B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        blocks = _parse_content_blocks(content)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "table"
        assert len(blocks[0]["lines"]) == 4

    def test_table_with_alignment(self):
        """带对齐符的表格"""
        content = "| 左 | 右 |\n|:--|--:|\n| a | b |"
        blocks = _parse_content_blocks(content)
        assert blocks[0]["type"] == "table"

    def test_table_without_separator_degrades_to_paragraph(self):
        """缺分隔行 → 降级为段落"""
        content = "| 列A | 列B |\n| 1 | 2 |"
        blocks = _parse_content_blocks(content)
        # 两行都成为 paragraph
        assert all(b["type"] == "paragraph" for b in blocks)
        assert len(blocks) == 2

    def test_indented_table_recognized(self):
        """✅ 回归（2026-09-20）：整表缩进（前导空格）仍应识别为表格

        实测交付文档：AI 把验收内容表格缩进挂在「1. 验收内容」列表项下，
        旧实现用未 strip 的原行判断 startswith("|")，整张表识别失败 →
        竖线源码原样印进文档；分隔行以 "-" 开头还会被误判为无序列表。
        """
        content = (
            "1. 验收内容\n"
            "   | 序号 | 检查项目 | 允许偏差/控制要求 |\n"
            "   |------|----------|-------------------|\n"
            "   | 1 | 主梁锚固长度 | 不小于设计值的1.25倍 |\n"
            "   | 2 | 钢丝绳间距 | 偏差不大于±20mm |"
        )
        blocks = _parse_content_blocks(content)
        assert [b["type"] for b in blocks] == ["list_item", "table"]
        assert len(blocks[1]["lines"]) == 4
        # 行内不应残留前导缩进（否则单元格切分会多出空列）
        assert all(ln.startswith("|") for ln in blocks[1]["lines"])

    def test_indented_table_mixed_indent(self):
        """表格行缩进深度不一致时仍按同一张表收集"""
        content = "| A | B |\n  |---|---|\n    | 1 | 2 |"
        blocks = _parse_content_blocks(content)
        assert [b["type"] for b in blocks] == ["table"]
        assert len(blocks[0]["lines"]) == 3

    def test_caption_line_absorbed_as_table_title(self):
        """✅ 新增：表格上方「表 X-Y 表名」行升格为表题，并从正文块中移除"""
        content = "表3-1 主要施工机械设备表\n\n| 序号 | 名称 |\n|---|---|\n| 1 | 挖掘机 |"
        blocks = _parse_content_blocks(content)
        assert [b["type"] for b in blocks] == ["table"]
        assert blocks[0]["caption"] == "主要施工机械设备表"

    def test_caption_line_bold_accepted(self):
        """加粗包裹的表名同样可识别"""
        content = "**表1-1 劳动力计划表**\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        blocks = _parse_content_blocks(content)
        assert blocks[0]["caption"] == "劳动力计划表"

    def test_reference_sentence_not_taken_as_caption(self):
        """回归：正文引用（"表 1 中的数据如下所示："）不得被吞成表题"""
        content = "表 1 中的数据如下所示：\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        blocks = _parse_content_blocks(content)
        assert [b["type"] for b in blocks] == ["paragraph", "table"]
        assert "caption" not in blocks[1]

    def test_plain_paragraph_before_table_kept(self):
        """普通说明段落不会被当作表题"""
        content = "上述参数取自设计文件。\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        blocks = _parse_content_blocks(content)
        assert [b["type"] for b in blocks] == ["paragraph", "table"]
        assert "caption" not in blocks[1]

    def test_caption_starting_with_xia_accepted(self):
        """回归：表名以"下"开头（如「下卧层承载力参数表」）也应被接受为表题"""
        content = "表2-3 下卧层承载力参数表\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        blocks = _parse_content_blocks(content)
        assert [b["type"] for b in blocks] == ["table"]
        assert blocks[0]["caption"] == "下卧层承载力参数表"

    def test_caption_with_colon_separator(self):
        """「表1-1：主要设备表」也可识别"""
        content = "表1-1：主要设备表\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        blocks = _parse_content_blocks(content)
        assert blocks[0]["caption"] == "主要设备表"

    def test_cross_reference_without_separator_rejected(self):
        """回归：`表1和表2的参数`（编号后无分隔符）不得被吞成表题"""
        content = "表1和表2的参数应保持一致。\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        blocks = _parse_content_blocks(content)
        assert [b["type"] for b in blocks] == ["paragraph", "table"]
        assert "caption" not in blocks[1]


# ============================================================
# 图表标记
# ============================================================
class TestParseContentBlocksChartMarker:
    """[CHART_TYPE: xxx] 标记"""

    def test_flowchart_marker(self):
        blocks = _parse_content_blocks("[CHART_TYPE: flowchart]")
        assert blocks == [{"type": "chart", "chart_type": "flowchart"}]

    def test_gantt_marker(self):
        blocks = _parse_content_blocks("[CHART_TYPE: gantt]")
        assert blocks == [{"type": "chart", "chart_type": "gantt"}]

    def test_chart_marker_with_spaces(self):
        """标记前后有空格"""
        blocks = _parse_content_blocks("  [CHART_TYPE: architecture]  ")
        assert blocks == [{"type": "chart", "chart_type": "architecture"}]

    def test_chart_marker_mixed_with_text(self):
        """图表标记与正文混合"""
        content = "正文段落\n[CHART_TYPE: labor]\n另一段"
        blocks = _parse_content_blocks(content)
        assert len(blocks) == 3
        assert blocks[0]["type"] == "paragraph"
        assert blocks[1] == {"type": "chart", "chart_type": "labor"}
        assert blocks[2]["type"] == "paragraph"


class TestParseContentBlocksChartJsonEdge:
    """```chart-json 块渲染侧与登记侧口径统一（回归：红字占位根因）。

    畸形 JSON / 未知类型的块在登记侧被跳过（不删正文），导出侧旧实现仍会
    产出 chart 块 → 渲染 None → 写入「[图 x-y — 渲染失败]」红字占位。
    现整块跳过；合法载荷（含无 type 键回落 labor）仍正常出图。
    """

    def test_malformed_chart_json_skipped(self):
        """JSON 不闭合 → 不产出 chart 块（避免红字占位）"""
        blocks = _parse_content_blocks('```chart-json\n{"type":"labor", "phases":[\n```')
        assert blocks == []

    def test_unknown_type_chart_json_skipped(self):
        """type 不属于 7 类可渲染图表 → 跳过"""
        blocks = _parse_content_blocks('```chart-json\n{"type":"weirdchart","foo":1}\n```')
        assert blocks == []

    def test_no_type_key_defaults_labor(self):
        """合法 JSON 对象但无 type 键 → 回落 labor（保留既有出图行为）"""
        blocks = _parse_content_blocks(
            '```chart-json\n{"phases":["a","b"],"categories":["x"],"data":[[1],[2]]}\n```')
        assert len(blocks) == 1
        assert blocks[0]["type"] == "chart"
        assert blocks[0]["chart_type"] == "labor"

    def test_valid_layout_chart_json_renderable(self):
        """合法 layout 载荷 → 正常产出 chart 块"""
        content = ('```chart-json\n'
                   '{"type":"layout","zones":[{"id":"z1","name":"A","x":0,"y":0,"w":9,"h":9}]}\n```')
        blocks = _parse_content_blocks(content)
        assert len(blocks) == 1
        assert blocks[0]["chart_type"] == "layout"


# ============================================================
# 列表
# ============================================================
class TestParseContentBlocksList:
    """无序列表与有序列表"""

    def test_unordered_list_dash(self):
        blocks = _parse_content_blocks("- 项目一\n- 项目二")
        assert len(blocks) == 2
        assert all(b["type"] == "list_item" for b in blocks)
        assert all(b["ordered"] is False for b in blocks)
        assert blocks[0]["text"] == "项目一"
        assert blocks[1]["text"] == "项目二"

    def test_unordered_list_star(self):
        blocks = _parse_content_blocks("* 项目A\n* 项目B")
        assert all(b["ordered"] is False for b in blocks)

    def test_unordered_list_bullet_chars(self):
        """• · 等项目符号"""
        blocks = _parse_content_blocks("• 项目一\n· 项目二")
        assert len(blocks) == 2
        assert all(b["type"] == "list_item" for b in blocks)

    def test_ordered_list_dot(self):
        blocks = _parse_content_blocks("1. 第一步\n2. 第二步")
        assert len(blocks) == 2
        assert all(b["type"] == "list_item" for b in blocks)
        assert all(b["ordered"] is True for b in blocks)
        assert blocks[0]["num"] == 1
        assert blocks[1]["num"] == 2

    def test_ordered_list_paren(self):
        """1) 格式"""
        blocks = _parse_content_blocks("1) 第一步\n2) 第二步")
        assert all(b["ordered"] is True for b in blocks)

    def test_ordered_list_chinese_paren(self):
        """1、 格式（需空格分隔，正则要求 \\s+）"""
        blocks = _parse_content_blocks("1、 第一步\n2、 第二步")
        assert all(b["ordered"] is True for b in blocks)

    def test_ordered_list_chinese_paren_no_space(self):
        """✅ BUG 修复：`1、加强安全管理`（顿号后无空格）也应识别为列表项"""
        blocks = _parse_content_blocks("1、加强安全管理\n2、落实安全责任制")
        assert [b["type"] for b in blocks] == ["list_item", "list_item"]
        assert all(b["ordered"] is True for b in blocks)
        assert blocks[0]["num"] == 1
        assert blocks[0]["text"] == "加强安全管理"
        assert blocks[1]["num"] == 2

    def test_ordered_list_chinese_round_paren_no_space(self):
        """✅ BUG 修复：`1）内容`（中文右括号后无空格）也应识别为列表项"""
        blocks = _parse_content_blocks("1）混凝土强度等级\n2）钢筋保护层厚度")
        assert all(b["type"] == "list_item" and b["ordered"] for b in blocks)
        assert blocks[0]["text"] == "混凝土强度等级"

    def test_decimal_paragraph_stays_paragraph(self):
        """回归：ASCII 小数点后仍要求空格，`3.14 是圆周率` 不得误判为列表"""
        blocks = _parse_content_blocks("3.14 是圆周率。")
        assert blocks[0]["type"] == "paragraph"

    def test_ordered_marker_only_is_not_list(self):
        """回归：仅有枚举符、其后无内容时不应识别为列表项（避免空列表）"""
        blocks = _parse_content_blocks("1、")
        assert blocks[0]["type"] == "paragraph"

    def test_nested_list_indent(self):
        """缩进列表记录 indent"""
        blocks = _parse_content_blocks("- 顶层\n  - 缩进")
        assert len(blocks) == 2
        assert blocks[0]["indent"] == 0
        assert blocks[1]["indent"] == 2

    def test_full_width_paren_digit(self):
        """✅ 优化：`（1）内容`（工程文档常用层级标记）识别为列表项"""
        blocks = _parse_content_blocks("（1）测量放线\n（2）基坑开挖")
        assert [b["type"] for b in blocks] == ["list_item", "list_item"]
        assert blocks[0]["marker"] == "num_paren_lr"
        assert blocks[0]["num"] == 1
        assert blocks[0]["text"] == "测量放线"

    def test_full_width_paren_cn_numeral(self):
        """✅ 优化：`（一）设计标准` 识别为列表项，起始序号解析为 1"""
        blocks = _parse_content_blocks("（一）设计标准\n（二）材料要求")
        assert all(b["type"] == "list_item" for b in blocks)
        assert blocks[0]["marker"] == "cn_num_paren"
        assert blocks[0]["num"] == 1
        assert blocks[1]["num"] == 2
        assert blocks[0]["text"] == "设计标准"

    def test_marker_styles_recorded(self):
        """各枚举符 → 标记样式映射（导出时据此还原外观）"""
        cases = {
            "1. 内容": "ascii",
            "1) 内容": "paren_ascii",
            "1、内容": "num_dun",
            "1）内容": "num_paren_r",
            "（1）内容": "num_paren_lr",
            "(1)内容": "num_paren_lr",
            "（十）内容": "cn_num_paren",
        }
        for text, expect in cases.items():
            b = _parse_content_blocks(text)[0]
            assert b["type"] == "list_item", (text, b)
            assert b["marker"] == expect, (text, b["marker"], expect)

    def test_ordered_prefix_styles(self):
        """_ordered_prefix：按样式渲染连续序号"""
        from app.routers.export import _ordered_prefix
        assert _ordered_prefix(1, "ascii") == "1. "
        assert _ordered_prefix(2, "num_dun") == "2、"
        assert _ordered_prefix(3, "num_paren_r") == "3）"
        assert _ordered_prefix(4, "num_paren_lr") == "（4）"
        assert _ordered_prefix(2, "paren_ascii") == "2) "
        assert _ordered_prefix(1, "cn_num_paren") == "（一）"
        assert _ordered_prefix(11, "cn_num_paren") == "（十一）"
        assert _ordered_prefix(21, "cn_num_paren") == "（二十一）"


# ============================================================
# 混合内容
# ============================================================
class TestParseContentBlocksMixed:
    """混合多种 block 类型"""

    def test_heading_paragraph_code(self):
        """标题 + 段落 + 代码块"""
        content = (
            "## 施工方案\n"
            "\n"
            "本方案包含以下内容：\n"
            "\n"
            "```python\n"
            "print('hello')\n"
            "```"
        )
        blocks = _parse_content_blocks(content)
        assert len(blocks) == 3
        assert blocks[0]["type"] == "heading"
        assert blocks[0]["level"] == 2
        assert blocks[1]["type"] == "paragraph"
        assert blocks[2]["type"] == "code"

    def test_full_document(self):
        """完整文档：标题+列表+表格+图表标记+代码"""
        content = (
            "# 施工组织设计\n"
            "\n"
            "## 编制依据\n"
            "- 规范一\n"
            "- 规范二\n"
            "\n"
            "## 进度计划\n"
            "[CHART_TYPE: gantt]\n"
            "\n"
            "| 阶段 | 工期 |\n"
            "|---|---|\n"
            "| 基础 | 30天 |\n"
            "| 主体 | 60天 |\n"
            "\n"
            "```mermaid\n"
            "graph TD\n"
            "A-->B\n"
            "```"
        )
        blocks = _parse_content_blocks(content)

        types = [b["type"] for b in blocks]
        assert "heading" in types
        assert "list_item" in types
        assert "chart" in types
        assert "table" in types

        # mermaid 代码块 → chart
        chart_blocks = [b for b in blocks if b["type"] == "chart"]
        assert len(chart_blocks) == 2
        chart_types = {b["chart_type"] for b in chart_blocks}
        assert "gantt" in chart_types
        assert "flowchart" in chart_types


# ============================================================
# 章节编号前缀提取 + 正文内子标题嵌套编号
# ============================================================
from app.routers.export import _compute_subheading, _section_number_prefix


class TestSectionNumberPrefix:
    """_section_number_prefix：从已格式化章节标题提取编号前缀"""

    def test_chapter_chinese(self):
        assert _section_number_prefix("第一章 施工组织设计") == "1"

    def test_section_dotted(self):
        assert _section_number_prefix("1.2 进度计划") == "1.2"

    def test_subsection_triple(self):
        assert _section_number_prefix("1.1.1 立面设计") == "1.1.1"

    def test_chapter_two(self):
        assert _section_number_prefix("第二章 工程概况") == "2"

    def test_chinese_paren_no_prefix(self):
        # L5（一）中文序号无法嵌套，返回空串
        assert _section_number_prefix("（一） 设计标准") == ""

    def test_empty(self):
        assert _section_number_prefix("") == ""
        assert _section_number_prefix(None) == ""


class TestComputeSubheading:
    """_compute_subheading：正文内 Markdown 子标题嵌套在章节编号之下"""

    def test_nested_under_section(self):
        # 章节 "1.2 进度计划"（section_level=2, prefix='1.2'）
        c = {}
        t1, s1 = _compute_subheading("1.2", 2, 2, c, "总体安排")
        t2, s2 = _compute_subheading("1.2", 2, 3, c, "关键节点")
        t3, s3 = _compute_subheading("1.2", 2, 2, c, "资源配置")
        assert t1 == "1.2.1 总体安排"
        assert t2 == "1.2.1.1 关键节点"
        assert t3 == "1.2.2 资源配置"
        # Heading 样式随层级递增，且不超 7
        assert s1 == 3 and s2 == 4 and s3 == 3

    def test_no_prefix_plain_dotted(self):
        c = {}
        t1, _ = _compute_subheading("", 2, 2, c, "小节A")
        t2, _ = _compute_subheading("", 2, 3, c, "子A")
        assert t1 == "1 小节A"
        assert t2 == "1.1 子A"

    def test_empty_pure_skips_number(self):
        c = {}
        t, s = _compute_subheading("1.2", 2, 2, c, "")
        assert t == ""
        assert s == 3


# ============================================================
# 新增：引用块 / 分隔线 / 缩进标题 / 图片说明
# ============================================================
class TestParseContentBlocksQuoteAndRule:
    """引用块（>）与分隔线（--- / *** / ___）"""

    def test_blockquote_single_line(self):
        blocks = _parse_content_blocks("> 注意雨季施工")
        assert blocks == [{"type": "quote", "text": "注意雨季施工"}]

    def test_blockquote_multi_line_merged(self):
        """连续引用行合并为一个 block"""
        blocks = _parse_content_blocks("> 第一行\n> 第二行\n\n正文")
        assert blocks[0] == {"type": "quote", "text": "第一行\n第二行"}
        assert blocks[1]["type"] == "paragraph"

    def test_horizontal_rule_variants(self):
        for rule in ("---", "***", "___", "- - -"):
            blocks = _parse_content_blocks(rule)
            assert blocks == [{"type": "hr"}], f"{rule} 应识别为分隔线"

    def test_two_dashes_is_not_rule(self):
        """仅 2 个字符不构成分隔线"""
        blocks = _parse_content_blocks("--")
        assert blocks[0]["type"] == "paragraph"

    def test_table_separator_not_treated_as_rule(self):
        """表格分隔行在表格分支内被消化，不应产生 hr"""
        blocks = _parse_content_blocks("| A | B |\n|---|---|\n| 1 | 2 |")
        assert [b["type"] for b in blocks] == ["table"]

    def test_indented_heading(self):
        """GFM 允许最多 3 个前导空格"""
        blocks = _parse_content_blocks("   ## 缩进标题")
        assert blocks[0] == {"type": "heading", "level": 2, "text": "缩进标题", "src_line": 0}

    def test_image_block(self):
        """✅ AI 配图：独占一行的图片按 image 块解析（导出时下载为真实位图插入）

        旧行为是当段落处理（行内解析器剔除 URL，只留说明文字），成稿里丢图；
        现取 alt / url 两个字段交给导出链路。
        """
        blocks = _parse_content_blocks("![平面布置](http://x/y.png)")
        assert blocks[0] == {
            "type": "image", "alt": "平面布置", "url": "http://x/y.png"}

    def test_image_requires_http_url(self):
        """非 http(s) 的图片行（如相对路径）仍按段落处理，不做下载"""
        blocks = _parse_content_blocks("![图](./local/a.png)")
        assert blocks[0]["type"] == "paragraph"


# ============================================================
# 新增：下载文件名清洗与 Content-Disposition 构造
# ============================================================
from app.routers.export import (  # noqa: E402
    _attachment_disposition,
    _safe_filename,
)


class TestSafeFilename:
    """_safe_filename：剔除路径分隔符与 Windows 非法字符"""

    def test_illegal_chars_replaced(self):
        # / : * ? " < > | 共 8 个非法字符，全部替换为下划线
        assert _safe_filename('测试/方案:*?"<>|') == "测试_方案" + "_" * 7

    def test_extension_appended_once(self):
        assert _safe_filename("方案", "docx") == "方案.docx"
        assert _safe_filename("方案.docx", "docx") == "方案.docx"

    def test_empty_falls_back(self):
        assert _safe_filename("", "pdf") == "导出文档.pdf"
        assert _safe_filename("   ", "pdf") == "导出文档.pdf"

    def test_length_capped(self):
        assert len(_safe_filename("长" * 300, "docx")) <= 120

    def test_long_name_keeps_extension(self):
        """✅ BUG 修复：先拼扩展名再整体截断会截掉 '.docx'，必须完整保留"""
        name = "深基坑开挖及支护专项施工方案编制说明" * 10  # 远超 120 字符
        out = _safe_filename(name, "docx")
        assert out.endswith(".docx"), out
        assert len(out) <= 120
        # 已自带扩展名时不得重复拼接
        assert _safe_filename(name + ".docx", "docx").endswith(".docx")
        assert _safe_filename(name + ".docx", "docx").count(".docx") == 1


class TestAttachmentDisposition:
    """_attachment_disposition：HTTP 头只能承载 latin-1，中文必须走 RFC 5987"""

    def test_chinese_name_ascii_safe(self):
        header = _attachment_disposition("测试方案.pdf")
        # 旧实现直接把中文写进 filename="..."，编码响应头时抛 UnicodeEncodeError
        header.encode("latin-1")
        assert "%E6%B5%8B%E8%AF%95%E6%96%B9%E6%A1%88.pdf" in header
        assert 'filename="' in header and "filename*=UTF-8''" in header

    def test_ascii_name_kept(self):
        header = _attachment_disposition("plan.pdf")
        assert 'filename="plan.pdf"' in header


# ============================================================
# 新增：内容指纹（改标题 / 调顺序 必须失效缓存）
# ============================================================
from app.routers.export import _content_fingerprint  # noqa: E402


def _prep(sections, chart_fp=(), config=None, fe_codes=(), global_facts=()):
    return {"sections": sections, "chart_fp": list(chart_fp),
            "config": config or {}, "fe_codes": list(fe_codes),
            "global_facts": list(global_facts)}


class TestContentFingerprint:
    """✅ BUG 修复：指纹必须覆盖标题 / 层级 / 父级 / 排序，否则改标题会命中旧缓存"""

    SEC = [{"id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
            "title": "第一章 概述", "content": "正文"}]

    def test_same_content_same_hash(self):
        assert _content_fingerprint(_prep(self.SEC))[1] == \
            _content_fingerprint(_prep([dict(self.SEC[0])]))[1]

    def test_title_change_changes_hash(self):
        changed = [dict(self.SEC[0], title="第一章 总述")]
        assert _content_fingerprint(_prep(self.SEC))[1] != \
            _content_fingerprint(_prep(changed))[1]

    def test_sort_order_change_changes_hash(self):
        changed = [dict(self.SEC[0], sort_order=5)]
        assert _content_fingerprint(_prep(self.SEC))[1] != \
            _content_fingerprint(_prep(changed))[1]

    def test_parent_change_changes_hash(self):
        changed = [dict(self.SEC[0], parent_id="other")]
        assert _content_fingerprint(_prep(self.SEC))[1] != \
            _content_fingerprint(_prep(changed))[1]

    def test_config_change_changes_config_hash(self):
        a = _content_fingerprint(_prep(self.SEC, config={"font_size": 12}))
        b = _content_fingerprint(_prep(self.SEC, config={"font_size": 14}))
        assert a[0] != b[0]

    def test_frontend_render_signature_matters(self):
        assert _content_fingerprint(_prep(self.SEC, fe_codes=["graph TD;A-->B"]))[1] != \
            _content_fingerprint(_prep(self.SEC))[1]

    def test_chart_data_matters(self):
        assert _content_fingerprint(_prep(self.SEC, chart_fp=[("c1", "gantt", "{}")]))[1] != \
            _content_fingerprint(_prep(self.SEC))[1]

    def test_global_fact_change_changes_hash(self):
        """✅ 修复（2026-09-17）：事实变化必须使内容指纹改变（否则 invalidate_export_cache 形同空转）。"""
        base = _content_fingerprint(_prep(self.SEC))[1]
        with_fact = _content_fingerprint(
            _prep(self.SEC, global_facts=[dict(gt="工期", title="项目总工期", content="180 天")]))[1]
        assert with_fact != base
        # global_facts 缺省时 prep 仍应可计算指纹（兼容旧调用方，不抛 KeyError）
        assert _content_fingerprint(_prep(self.SEC))[1] is not None


# ============================================================
# 新增：导出修复统计的线程隔离（并发导出不得串号）
# ============================================================
import threading  # noqa: E402

from app.routers.export import (  # noqa: E402
    _accumulate_fix_stats,
    _log_fix_stats,
)


class TestFixStatsThreadIsolation:
    """✅ BUG 修复：_FIX_STATS 曾为模块级全局，DOCX 构建在 asyncio.to_thread
    的多个工作线程中并发执行时会互相污染（统计张冠李戴 / 计数丢失）。
    现改为 threading.local，各线程统计相互隔离。"""

    def test_worker_stats_do_not_leak_to_main_thread(self):
        _log_fix_stats()  # 先清零当前线程统计基线
        _accumulate_fix_stats("公式 $E=mc^2$")  # 主线程：1 处行内公式

        captured: dict = {}

        def worker():
            _accumulate_fix_stats("公式 $a^2+b^2$")
            captured["stats"] = _log_fix_stats()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # 工作线程只应看到自己的 1 处，而不是主线程已累加的那一处
        assert captured["stats"].get("formulas") == 1, captured
        # 主线程统计也不应被工作线程的 _log_fix_stats 清零
        assert _log_fix_stats().get("formulas") == 1

