"""解析模块单元测试

覆盖：
- file_parser.simple_parse_outline（多级编号、Markdown标题、中文数字、4级+编号）
- file_parser.parse_file_content（空内容、文本解码）
- json_response.extract_json（多代码块、括号配对、字符串内括号）
- json_response.renumber_outline（循环引用防护、children修复）
- heading_standard.HeadingNumberingGeneratorV2（level 8不越界、完整流程）
"""
import json
import pytest

from app.services.file_parser import (
    simple_parse_outline,
    parse_file_content,
    parse_file_content_ex,
    _mineru_image_fallback,
)
from app.services.ai.json_response import extract_json, renumber_outline, _extract_balanced
from app.services.ai.heading_standard import (
    HeadingNumberingGeneratorV2,
    HeadingNumberingGenerator,
    format_heading_by_id,
)


# ============================================================
# simple_parse_outline
# ============================================================

class TestSimpleParseOutline:
    def test_empty_text(self):
        assert simple_parse_outline("") == []
        assert simple_parse_outline("   \n  \n") == []

    def test_chapter_with_multilevel_decimal(self):
        text = "第一章 概述\n1.1 项目背景\n1.1.1 历史沿革\n1.1.2 现状分析\n1.2 项目目标\n"
        result = simple_parse_outline(text)
        assert len(result) == 1
        assert result[0]["title"] == "概述"
        assert result[0]["level"] == 1
        assert len(result[0]["children"]) == 2
        assert result[0]["children"][0]["title"] == "项目背景"
        assert result[0]["children"][0]["level"] == 2
        assert len(result[0]["children"][0]["children"]) == 2
        assert result[0]["children"][0]["children"][0]["title"] == "历史沿革"
        assert result[0]["children"][0]["children"][0]["level"] == 3

    def test_four_level_numbering(self):
        text = "第一章 概述\n1.1 项目背景\n1.1.1 历史沿革\n1.1.1.1 建国初期\n1.1.1.2 改革开放\n"
        result = simple_parse_outline(text)
        assert result[0]["children"][0]["children"][0]["children"][0]["title"] == "建国初期"
        assert result[0]["children"][0]["children"][0]["children"][0]["level"] == 4
        assert result[0]["children"][0]["children"][0]["children"][1]["title"] == "改革开放"
        assert result[0]["children"][0]["children"][0]["children"][1]["level"] == 4

    def test_chinese_number_with_paren(self):
        text = "第一章 概述\n一、项目背景\n（一）历史沿革\n（二）现状分析\n二、项目目标\n"
        result = simple_parse_outline(text)
        assert result[0]["children"][0]["title"] == "项目背景"
        assert result[0]["children"][0]["children"][0]["title"] == "历史沿革"
        assert result[0]["children"][0]["children"][0]["level"] == 3
        assert result[0]["children"][0]["children"][1]["title"] == "现状分析"
        assert result[0]["children"][0]["children"][1]["level"] == 3

    def test_single_level_decimal(self):
        text = "1. 概述\n2. 总体设计\n3. 详细设计\n"
        result = simple_parse_outline(text)
        assert len(result) == 3
        assert all(n["level"] == 1 for n in result)

    def test_markdown_headings(self):
        text = "# 概述\n## 项目背景\n### 历史沿革\n## 项目目标\n# 总体设计\n"
        result = simple_parse_outline(text)
        assert len(result) == 2
        assert result[0]["title"] == "概述"
        assert result[0]["level"] == 1
        assert len(result[0]["children"]) == 2
        assert result[0]["children"][0]["title"] == "项目背景"
        assert result[0]["children"][0]["level"] == 2
        assert result[0]["children"][0]["children"][0]["title"] == "历史沿革"
        assert result[0]["children"][0]["children"][0]["level"] == 3

    def test_id_uniqueness(self):
        text = "1.1 A\n1.2 B\n1.3 C\n"
        result = simple_parse_outline(text)
        all_ids = []
        def collect(nodes):
            for n in nodes:
                all_ids.append(n["id"])
                collect(n["children"])
        collect(result)
        assert len(all_ids) == len(set(all_ids)), f"ID 重复: {all_ids}"

    def test_mixed_formats(self):
        text = (
            "第一章 编制说明\n"
            "1.1 编制依据\n"
            "一、设计文件\n"
            "（一）施工图纸\n"
            "# 备注\n"
        )
        result = simple_parse_outline(text)
        assert len(result) == 2
        assert result[0]["title"] == "编制说明"
        assert result[1]["title"] == "备注"
        assert result[0]["children"][0]["title"] == "编制依据"
        assert result[0]["children"][1]["title"] == "设计文件"
        assert result[0]["children"][1]["children"][0]["title"] == "施工图纸"

    def test_body_lines_with_trailing_punctuation_not_nodes(self):
        """✅ 2026-09-19：句末带；。等句读标点的列举条目/正文行不得生成大纲节点
        （否则导出为"有标题无正文"的空章节）"""
        text = (
            "3.1 专职安全员职责\n"
            "3.1.12 身份证复印件、照片；\n"
            "3.1.13 安全教育培训记录、考核成绩；\n"
            "3.2 作业完成后清理现场，做到工完场清。\n"
        )
        result = simple_parse_outline(text)
        titles = [n["title"] for n in result]
        assert titles == ["专职安全员职责"]


# ============================================================
# parse_file_content
# ============================================================

class TestParseFileContent:
    def test_empty_content(self):
        assert parse_file_content(b"", "test.txt") == ""

    def test_txt_utf8(self):
        content = "你好世界".encode("utf-8")
        assert parse_file_content(content, "test.txt") == "你好世界"

    def test_txt_gbk(self):
        content = "你好世界".encode("gbk")
        assert parse_file_content(content, "test.txt") == "你好世界"

    def test_txt_gbk_warns_in_diag(self):
        # GBK 回退分支应把编码风险写入诊断（供前端透传展示）
        content = "你好世界".encode("gbk")
        _text, diag = parse_file_content_ex(content, "test.txt")
        assert any("gbk" in w.lower() for w in diag["warnings"])

    def test_txt_unknown_encoding_warns_in_diag(self):
        # ✅ 修复（2026-09-25）：Big5/Shift-JIS 等无法识别编码经末级
        # utf-8 errors="ignore" 兜底会产出乱码，但必须给出告警，否则污染
        # 下游事实库却无任何痕迹。
        content = "繁體中文測試".encode("big5")
        _text, diag = parse_file_content_ex(content, "test.txt")
        assert any("无法识别" in w or "编码" in w for w in diag["warnings"])
        # 容错解码不应抛异常（乱码可控，不崩）
        assert isinstance(_text, str)

    def test_md_file(self):
        content = "# 标题\n正文".encode("utf-8")
        assert parse_file_content(content, "test.md") == "# 标题\n正文"

    def test_unknown_extension(self):
        content = "测试内容".encode("utf-8")
        assert parse_file_content(content, "test.unknown") == "测试内容"

    # ============================================================
    # F1 / F5 防护（2026-09-26）
    # ============================================================

    def test_f1_binary_disguised_as_txt_rejected(self):
        # ✅ F1：.exe 改名为 .txt 应被二进制防护拒绝（避免乱码入库污染事实库）
        content = b"MZ" + b"\x00" * 64 + b"\x01\x02\x03"
        from app.services.file_parser import ParseError
        with pytest.raises(ParseError):
            parse_file_content_ex(content, "evil.txt")

    def test_f1_png_bytes_as_txt_rejected(self):
        # PNG 二进制头改名 .txt 同样应拒绝
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        from app.services.file_parser import ParseError
        with pytest.raises(ParseError):
            parse_file_content_ex(content, "img.txt")

    def test_f1_legit_gbk_text_allowed(self):
        # 合法 GBK 文本改名 .txt 不应被误杀
        _text, _diag = parse_file_content_ex("专项方案测试文本".encode("gbk"), "a.txt")
        assert "专项方案测试文本" in _text

    def test_f5_invisible_chars_normalized(self):
        # ✅ F5：零宽字符 / NBSP / 控制字符被归一，并给出告警
        raw = "工程名称\u200b：\u00a0测试\u0007结束".encode("utf-8")
        _text, diag = parse_file_content_ex(raw, "a.txt")
        assert "\u200b" not in _text
        assert "\u00a0" not in _text
        assert "\u0007" not in _text
        assert any("不可见" in w for w in diag["warnings"])

    def test_f5_normal_text_no_warning(self):
        _text, diag = parse_file_content_ex("正常中文文本 123".encode("utf-8"), "a.txt")
        assert _text == "正常中文文本 123"
        assert not any("不可见" in w for w in diag["warnings"])


# ============================================================
# OLE 容器细分（旧版 .xls / .doc 流名嗅探 + 扩展名双向纠错）
# ============================================================

_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _ole_with_stream(*stream_names: str) -> bytes:
    """构造带 UTF-16LE 流名的最小 OLE 头字节串（仅用于嗅探单测）。"""
    blob = _OLE_HEADER + b"\x00" * 64
    for name in stream_names:
        blob += name.encode("utf-16-le") + b"\x00" * 16
    return blob


class TestOleSniffing:
    def test_sniff_xls_by_workbook_stream(self):
        from app.services.file_parser import _sniff_type
        assert _sniff_type(_ole_with_stream("Workbook")) == "xls"

    def test_sniff_doc_by_worddocument_stream(self):
        from app.services.file_parser import _sniff_type
        assert _sniff_type(_ole_with_stream("WordDocument")) == "doc"

    def test_ambiguous_markers_fall_back_to_doc(self):
        from app.services.file_parser import _sniff_type
        # 双标记（如 .doc 正文里恰好含英文 Workbook）→ 维持 doc 兜底
        assert _sniff_type(_ole_with_stream("Workbook", "WordDocument")) == "doc"

    def test_renamed_xls_routed_to_xlrd_channel(self):
        # ✅ 回归：改名为 .doc 的旧版 .xls 不再走 LibreOffice→docx 次优转换
        from app.services import file_parser as fp
        diag: dict = {"file_type": "", "text_len": 0, "truncated": False, "warnings": []}
        captured = {}
        monkey_fp = fp

        orig = monkey_fp._parse_excel
        monkey_fp._parse_excel = lambda content, ftype, diag: (
            captured.__setitem__("ftype", ftype) or "SHEET")
        try:
            monkey_fp.parse_file_content_ex(_ole_with_stream("Workbook"), "fake.doc")
        finally:
            monkey_fp._parse_excel = orig
        assert captured["ftype"] == "xls"

    def test_renamed_doc_routed_to_legacy_word_channel(self):
        # ✅ 回归：改名为 .xls 的旧版 Word 不再误入 xlrd
        from app.services import file_parser as fp
        captured = {}
        orig = fp._parse_legacy_word
        fp._parse_legacy_word = lambda content, fname, diag: (
            captured.__setitem__("fname", fname) or "DOC")
        try:
            fp.parse_file_content_ex(_ole_with_stream("WordDocument"), "fake.xls")
        finally:
            fp._parse_legacy_word = orig
        assert captured["fname"] == "fake.xls"


# ============================================================
# CSV / XLSX → Markdown 表格（下游表格保护与高权重区分类依赖竖线语法）
# ============================================================

class TestStructuredTableExport:
    def test_csv_becomes_markdown_table(self):
        content = "设备名称,规格型号,数量\n塔吊,QTZ80,2\n挖掘机,PC220,3\n".encode("utf-8")
        out = parse_file_content(content, "equip.csv")
        assert "【表格】" in out
        assert "| 设备名称 | 规格型号 | 数量 |" in out
        assert "| 塔吊 | QTZ80 | 2 |" in out
        # split_into_chunks 的表格识别依赖竖线分隔行
        assert out.count("\n|") >= 3

    def test_csv_pipe_in_cell_is_escaped(self):
        content = "名称,备注\na,b|c\n".encode("utf-8")
        out = parse_file_content(content, "a.csv")
        assert "b／c" in out
        assert "b|c" not in out

    def test_csv_rows_truncated_to_limit(self, monkeypatch):
        import app.services.file_parser as fp
        monkeypatch.setattr(fp, "MAX_CSV_ROWS", 2)
        content = ("h\n" + "\n".join(f"r{i}" for i in range(5))).encode("utf-8")
        out = parse_file_content(content, "a.csv")
        # ✅ 表头 + Markdown 对齐分隔行 + 1 数据行
        #    （分隔行是 GFM 渲染表格的必要条件，见 file_parser._md_separator）
        assert out.count("\n|") == 3
        assert "| --- |" in out
        assert "| r1 |" not in out      # 第 3 行起被截断

    def test_xlsx_becomes_markdown_table_with_sheet_name(self):
        import io as _io
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "机械表"
        ws.append(["设备名称", "规格型号", "数量"])
        ws.append(["塔吊", "QTZ80", 2])
        buf = _io.BytesIO()
        wb.save(buf)
        out = parse_file_content(buf.getvalue(), "equip.xlsx")
        assert "【工作表：机械表】" in out
        assert "| 设备名称 | 规格型号 | 数量 |" in out
        assert "| 塔吊 | QTZ80 | 2 |" in out


# ============================================================
# extract_json / _extract_balanced
# ============================================================

class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_plain_array(self):
        assert extract_json('[1, 2, 3]') == '[1, 2, 3]'

    def test_code_block_json(self):
        text = '```json\n{"outline": []}\n```'
        assert extract_json(text) == '{"outline": []}'

    def test_code_block_without_lang(self):
        text = '```\n{"a": 1}\n```'
        assert extract_json(text) == '{"a": 1}'

    def test_multiple_code_blocks(self):
        text = "说明\n```python\nprint(1)\n```\n```json\n{\"outline\": []}\n```"
        result = extract_json(text)
        assert result == '{"outline": []}'

    def test_brace_in_string(self):
        text = '{"text": "包含}和{的字符串", "ok": true}'
        result = extract_json(text)
        assert json.loads(result)["text"] == "包含}和{的字符串"

    def test_nested_objects(self):
        text = '{"a": {"b": {"c": [1, 2, 3]}}}'
        result = extract_json(text)
        assert json.loads(result) == {"a": {"b": {"c": [1, 2, 3]}}}

    def test_array_before_brace(self):
        text = '一些文字 [{"a": 1}] 还有'
        result = extract_json(text)
        assert json.loads(result) == [{"a": 1}]

    def test_invalid_brace_then_valid_array(self):
        text = '说明 {非json} 然后 [{"outline": []}]'
        result = extract_json(text)
        assert json.loads(result) == [{"outline": []}]

    def test_no_json(self):
        assert extract_json("纯文本没有JSON") is None

    def test_code_block_no_json(self):
        text = "```python\nprint('hello')\n```"
        assert extract_json(text) is None

    def test_pure_array_code_block(self):
        text = '```json\n[{"id": "1"}, {"id": "2"}]\n```'
        result = extract_json(text)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_extract_balanced_no_brackets(self):
        assert _extract_balanced("纯文本") is None

    def test_extract_balanced_unclosed(self):
        assert _extract_balanced('{"a": ') is None


# ============================================================
# renumber_outline
# ============================================================

class TestRenumberOutline:
    def test_basic_renumber(self):
        nodes = [{"title": "A", "children": []}, {"title": "B", "children": []}]
        result = renumber_outline(nodes)
        assert result[0]["id"] == "1"
        assert result[0]["level"] == 1
        assert result[1]["id"] == "2"

    def test_nested_renumber(self):
        nodes = [{"title": "A", "children": [{"title": "B", "children": []}]}]
        result = renumber_outline(nodes)
        assert result[0]["id"] == "1"
        assert result[0]["children"][0]["id"] == "1.1"
        assert result[0]["children"][0]["level"] == 2

    def test_fix_missing_children(self):
        nodes = [{"title": "A"}]
        result = renumber_outline(nodes)
        assert result[0]["children"] == []

    def test_fix_none_children(self):
        nodes = [{"title": "A", "children": None}]
        result = renumber_outline(nodes)
        assert result[0]["children"] == []


# ============================================================
# _mineru_image_fallback 单图大小阈值（审计报告 §4-4，2026-09-23）
# ============================================================

class TestMineruImageFallbackSizeCap:
    def _patch_settings(self, monkeypatch, enabled=True, provider="agent", cap=100):
        import app.config as cfg
        monkeypatch.setattr(cfg.settings, "mineru_enabled", enabled)
        monkeypatch.setattr(cfg.settings, "mineru_provider", provider)
        monkeypatch.setattr(cfg.settings, "mineru_image_max_bytes", cap)

    def test_over_cap_skips_cloud_and_notes(self, monkeypatch):
        self._patch_settings(monkeypatch, cap=100)
        called = {"hit": False}

        def fake_parse(content, fname):
            called["hit"] = True
            return "云端结果"

        monkeypatch.setattr(
            "app.services.mineru_client.parse_with_mineru", fake_parse)
        diag: dict = {}
        out = _mineru_image_fallback(b"x" * 200, "png", diag)
        # 超过阈值 → 直接跳过云端兜底、不调用云端
        assert out == ""
        assert called["hit"] is False
        assert "超过云端单图上限" in str(diag)

    def test_under_cap_uses_cloud(self, monkeypatch):
        # cap=0 表示不限制
        self._patch_settings(monkeypatch, cap=0)
        # 注意：云端结果需 ≥5 个有效字符才会被判定为"可用"（_has_usable_image_text）
        monkeypatch.setattr(
            "app.services.mineru_client.parse_with_mineru",
            lambda content, fname: "云端解析得到的有效文本内容")
        diag: dict = {}
        out = _mineru_image_fallback(b"small", "png", diag)
        assert out == "云端解析得到的有效文本内容"

    def test_disabled_provider_returns_empty(self, monkeypatch):
        self._patch_settings(monkeypatch, provider="")
        monkeypatch.setattr(
            "app.services.mineru_client.parse_with_mineru",
            lambda content, fname: "云端结果")
        diag: dict = {}
        out = _mineru_image_fallback(b"small", "png", diag)
        assert out == ""

    def test_fix_non_list_children(self):
        nodes = [{"title": "A", "children": "invalid"}]
        result = renumber_outline(nodes)
        assert result[0]["children"] == []

    def test_deep_nesting_protection(self):
        node = {"title": "deep", "children": []}
        node["children"].append(node)
        result = renumber_outline([node])
        assert result is not None


# ============================================================
# HeadingNumberingGeneratorV2
# ============================================================

class TestHeadingNumberingGeneratorV2:
    def test_level_8_no_index_error(self):
        gen = HeadingNumberingGeneratorV2()
        result = gen.update_counter(8, "root")
        assert result == ""

    def test_level_7_no_index_error(self):
        # 2026-09 规范：L7 输出小写字母（HEADING_MANAGED_LEVELS 含 7）
        gen = HeadingNumberingGeneratorV2()
        result = gen.update_counter(7, "root")
        assert result == "a"

    def test_full_l1_to_l8_flow(self):
        gen = HeadingNumberingGeneratorV2()
        results = []
        for lvl in range(1, 9):
            r = gen.update_counter(lvl, f"parent_{lvl}")
            results.append(r)
        # 2026-09 规范：L5 十进制延续 / L6 括号数字 / L7 小写字母 / L8 不管理
        assert results[0] == "第一章"
        assert results[1] == "1"
        assert results[2] == "1.1"
        assert results[3] == "1.1.1"
        assert results[4] == "1.1.1.1"
        assert results[5] == "1）"
        assert results[6] == "a"
        assert results[7] == ""

    def test_parent_change_resets_counter(self):
        gen = HeadingNumberingGeneratorV2()
        gen.update_counter(1, "root")
        gen.update_counter(2, "ch1")
        r1 = gen.update_counter(2, "ch1")
        assert r1 == "2"
        r2 = gen.update_counter(2, "ch2")
        assert r2 == "1"

    def test_reset(self):
        gen = HeadingNumberingGeneratorV2()
        gen.update_counter(1, "root")
        gen.reset()
        assert gen.counters == [0, 0, 0, 0, 0, 0, 0, 0]
        assert gen.parent_ids == [None] * 8

    def test_invalid_level(self):
        gen = HeadingNumberingGeneratorV2()
        assert gen.update_counter(0, "root") == ""
        assert gen.update_counter(9, "root") == ""


# ============================================================
# HeadingNumberingGenerator (静态版)
# ============================================================

class TestHeadingNumberingGenerator:
    def test_next_heading_level_8(self):
        counters = [0] * 8
        counters, num, formatted = HeadingNumberingGenerator.next_heading(8, counters, "测试")
        assert formatted == "测试"

    def test_next_heading_l1_to_l4(self):
        counters = [0] * 8
        counters, num, f1 = HeadingNumberingGenerator.next_heading(1, counters, "总则")
        assert "第一章" in f1
        counters, num, f2 = HeadingNumberingGenerator.next_heading(2, counters, "编制依据")
        assert "1" in f2
        counters, num, f3 = HeadingNumberingGenerator.next_heading(3, counters, "设计规范")
        assert "1.1" in f3
        counters, num, f4 = HeadingNumberingGenerator.next_heading(4, counters, "质量管控")
        assert "1.1.1" in f4
        counters, num, f5 = HeadingNumberingGenerator.next_heading(5, counters, "设计标准")
        assert "1.1.1.1、" in f5  # 2026-09 对齐：L5 十进制（旧「（一）」中文括号）


# ============================================================
# format_heading_by_id
# ============================================================

class TestFormatHeadingById:
    def test_level_1(self):
        result = format_heading_by_id("1", 1, "总则")
        assert "第一章" in result
        assert "总则" in result

    def test_level_4(self):
        result = format_heading_by_id("1.1.1.1", 4, "质量管控")
        assert "1.1.1" in result
        assert "质量管控" in result

    def test_empty_title(self):
        assert format_heading_by_id("1", 1, "") == ""
        assert format_heading_by_id("1", 1, "   ") == "   "