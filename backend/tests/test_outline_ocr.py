"""simple_parse_outline 的 OCR 鲁棒性回归测试

覆盖 2026-09 增强：编号关键位置允许插入空格（"第 1 章" / "1 . 1" /
"（ 一 ）" / "一 、 工程概况" 等真实 OCR / 扫描件噪声），
以及"部分"等双字章节标记被正确整体消费。
"""
from app.services.file_parser import simple_parse_outline


class TestOcrSpacingTolerance:
    def test_spaced_chapter_cn(self):
        out = simple_parse_outline("第 1 章 工程概况")
        assert out[0]["title"] == "工程概况"
        assert out[0]["level"] == 1

    def test_spaced_section_cn(self):
        out = simple_parse_outline("第 2 节 边坡支护")
        assert out[0]["title"] == "边坡支护"
        assert out[0]["level"] == 1

    def test_spaced_digit_chapter(self):
        out = simple_parse_outline("第 3 章 施工计划")
        assert out[0]["title"] == "施工计划"

    def test_spaced_part(self):
        out = simple_parse_outline("第 3 部分 施工部署")
        # ✅ 必须整体消费"部分"，不能残留下一个"分"字
        assert out[0]["title"] == "施工部署"

    def test_cn_spaced_part(self):
        out = simple_parse_outline("第一部分 总则")
        assert out[0]["title"] == "总则"

    def test_dotted_with_spaces(self):
        out = simple_parse_outline("1 . 1 测量放线")
        # "1 . 1" 去空格后按点号数定层级 = 2（不能是 3）
        assert out[0]["title"] == "测量放线"
        assert out[0]["level"] == 2

    def test_deep_dotted_with_spaces(self):
        out = simple_parse_outline("1 . 1 . 1 深层标题")
        assert out[0]["level"] == 3

    def test_spaced_bracket_cn(self):
        out = simple_parse_outline("（ 一 ） 地质条件")
        assert out[0]["title"] == "地质条件"

    def test_spaced_cn_enum(self):
        out = simple_parse_outline("一 、 工程概况")
        assert out[0]["title"] == "工程概况"
        assert out[0]["level"] == 2

    def test_mixed_ocr_sample(self):
        """综合 OCR 噪声样例：各级编号混用空格，结构应正确。"""
        doc = (
            "第 1 章 工程概况\n"
            " 1 . 1 项目背景\n"
            " 1 . 2 地质条件\n"
            " 二 、 施工计划\n"
            " （ 一 ） 组织机构\n"
            "第三章 安全保证措施"
        )
        out = simple_parse_outline(doc)
        titles = [n["title"] for n in out]
        assert titles == ["工程概况", "安全保证措施"]
        gk = out[0]
        child_titles = [c["title"] for c in gk["children"]]
        # 1.1 / 1.2 / 二、 都正确挂到"工程概况"下
        assert "项目背景" in child_titles
        assert "地质条件" in child_titles
        assert "施工计划" in child_titles
        # （一）组织机构 应挂到"施工计划"（level 2）下，成为 level 3
        plan = next(c for c in gk["children"] if c["title"] == "施工计划")
        assert plan["level"] == 2
        assert any(gc["title"] == "组织机构" for gc in plan["children"])
