"""正文生成质量保障 单元测试

覆盖三项商业化交付要求：
1. 现行有效标准清单（standards_registry）：按方案类型命中、禁止废止编号；
2. 正文落库清洗（content_polish）：口语化/AI 腔清除、图表代码块不被破坏；
3. 提示词渲染（content_generation_system）：新增变量正确注入，无残留占位符。
"""
from app.services.ai.prompts._registry import render
from app.services.content_polish import (
    find_colloquial_hits,
    quality_issues,
    sanitize_ai_content,
)
from app.services.standards_registry import (
    find_abolished_codes,
    get_standards_text,
    is_known_standard,
    match_categories,
)


# ---------------------------------------------------------------------------
# 一、标准库
# ---------------------------------------------------------------------------
class TestStandardsRegistry:
    def test_match_基坑类(self):
        cats = match_categories("深基坑土方开挖专项施工方案", "深基坑")
        assert "基坑" in cats

    def test_match_脚手架类(self):
        cats = match_categories("悬挑脚手架专项施工方案", "脚手架", "脚手架搭设工艺")
        assert "脚手架" in cats

    def test_不命中时返回空(self):
        assert match_categories("项目管理大纲", "") == []

    def test_文本包含现行强制规范(self):
        text = get_standards_text("悬挑脚手架专项施工方案", "脚手架")
        assert "GB 55023-2022" in text
        assert "施工脚手架通用规范" in text

    def test_文本包含禁止引用清单(self):
        text = get_standards_text("临时用电专项施工方案", "临时用电")
        assert "JGJ/T 46-2024" in text          # 现行版本在库
        assert "JGJ 46-2005" in text            # 已废止版本列入禁止清单

    def test_无命中仍返回通用标准(self):
        text = get_standards_text("项目管理大纲", "")
        assert "GB 50300-2013" in text

    def test_扫描废止编号(self):
        assert find_abolished_codes("本工程临时用电按 JGJ 46-2005 执行") == ["JGJ 46-2005"]
        assert find_abolished_codes("按 JGJ/T 46-2024 执行") == []

    def test_现行标准判定(self):
        assert is_known_standard("JGJ 130-2011") is True
        assert is_known_standard("JGJ 46-2005") is False


# ---------------------------------------------------------------------------
# 二、正文清洗
# ---------------------------------------------------------------------------
class TestSanitizeAiContent:
    def test_消除口语化(self):
        raw = "首先呢，咱们要把模板搞定，需要注意的是支撑间距，说白了就是不能太大。"
        out = sanitize_ai_content(raw)
        assert "咱们" not in out
        assert "搞定" not in out
        assert "需要注意的是" not in out
        assert "说白了" not in out
        assert "应注意" in out
        assert "完成" in out

    def test_清除AI身份表述(self):
        raw = "作为人工智能助手，本节说明如下。\n希望对你有帮助。\n以上是本章内容。"
        out = sanitize_ai_content(raw)
        assert "人工智能" not in out
        assert "希望对你有帮助" not in out
        assert "以上是本章内容" not in out

    def test_保留技术数据不被改动(self):
        raw = "混凝土强度等级不低于C30，立杆间距不大于1.2m，扫地杆距地200mm。"
        assert sanitize_ai_content(raw) == raw

    def test_保护图表代码块(self):
        raw = ("施工工艺流程如下图所示：\n\n"
               "```mermaid\nflowchart TD\n  A[测量放线] --> B[土方开挖]\n```\n\n"
               "上述工序说白了就是先放线后开挖。")
        out = sanitize_ai_content(raw)
        assert "```mermaid" in out
        assert "A[测量放线] --> B[土方开挖]" in out
        assert "说白了" not in out

    def test_空值原样返回(self):
        assert sanitize_ai_content("") == ""
        assert sanitize_ai_content("   ") == "   "

    def test_套话书面化(self):
        out = sanitize_ai_content("项目部要做好安全生产工作。")
        assert "落实安全生产要求" in out

    def test_造价专业词不被误替换(self):
        # 估算/概算/预算是造价专业词，只有独立的"估计"才书面化为"预计"
        raw = "投资估算100万元，概算已批复，预算50万元，估计3天完成。"
        out = sanitize_ai_content(raw)
        assert "投资估算" in out
        assert "概算" in out
        assert "预算" in out
        assert "预计3天完成" in out
        assert "估计" not in out

    def test_统计术语不被误替换(self):
        raw = "该参数的估计量、估计值与估计误差需复核，但估计需要3天。"
        out = sanitize_ai_content(raw)
        assert "估计量" in out and "估计值" in out and "估计误差" in out
        assert "预计需要3天" in out

    def test_GFM表格内容不被清洗(self):
        raw = ("| 项目 | 说明 |\n|---|---|\n"
               "| 投资估算 | 估计值待核 |\n\n正文估计三天完成。")
        out = sanitize_ai_content(raw)
        assert "| 投资估算 | 估计值待核 |" in out
        assert "正文预计三天完成" in out

    def test_表格前后空行不被吞掉(self):
        raw = "| a | b |\n|---|---|\n| 1 | 2 |\n\n后续正文"
        assert sanitize_ai_content(raw) == raw

    def test_CRLF表格同样受保护(self):
        raw = "前文\r\n\r\n| 估算 | 估计 |\r\n|---|---|\r\n正文搞定\r\n"
        out = sanitize_ai_content(raw)
        assert "| 估算 | 估计 |" in out
        assert "正文完成" in out

    def test_围栏代码块内的竖线行不当作表格(self):
        raw = "```\n| 这不是表格 | 估计 |\n```\n搞定"
        out = sanitize_ai_content(raw)
        assert "| 这不是表格 | 估计 |" in out
        assert out.endswith("完成")


class TestQualityIssues:
    def test_命中口语化(self):
        issues = quality_issues("咱们今天搞定它")
        assert issues["colloquial_hits"]

    def test_清洗后无残留命中(self):
        issues = quality_issues(sanitize_ai_content("咱们今天搞定它，说白了很简单"))
        assert issues["colloquial_hits"] == []

    def test_审计废止标准(self):
        issues = quality_issues("临时用电执行 JGJ 46-2005 的规定。")
        assert issues["abolished_standards"] == ["JGJ 46-2005"]

    def test_空文本不报错(self):
        assert quality_issues("") == {"colloquial_hits": [], "abolished_standards": []}

    def test_造价词与表格不误报口语化(self):
        assert find_colloquial_hits("投资估算100万，概算已批复") == []
        assert find_colloquial_hits("| 估算 | 估计值 |\n|---|---|") == []
        assert "估计" in find_colloquial_hits("估计3天完成")


# ---------------------------------------------------------------------------
# 三、提示词渲染
# ---------------------------------------------------------------------------
class TestContentPromptRendering:
    def _render(self):
        return render(
            "content_generation_system",
            scheme_name="深基坑土方开挖专项施工方案",
            scheme_type="深基坑",
            section_number="3.2",
            chart_plan="无",
            standards_text=get_standards_text("深基坑土方开挖专项施工方案", "深基坑"),
        )

    def test_无残留占位符(self):
        prompt = self._render()
        assert "{standards_text}" not in prompt
        assert "{chart_plan}" not in prompt

    def test_包含数据红线与规范引用规则(self):
        prompt = self._render()
        assert "数据真实性红线" in prompt
        assert "标准与图集引用规范" in prompt
        assert "语言规范" in prompt
        assert "GB 50497-2019" in prompt           # 基坑监测现行标准
        assert "JGJ 46-2005" in prompt             # 废止版本列入禁止项

    def test_续写提示词注入标准清单(self):
        prompt = render(
            "content_continue_system",
            scheme_name="悬挑脚手架专项施工方案",
            scheme_type="脚手架",
            standards_text=get_standards_text("悬挑脚手架专项施工方案", "脚手架"),
        )
        assert "数据真实性红线" in prompt
        assert "GB 55023-2022" in prompt
        assert "{standards_text}" not in prompt


def test_find_colloquial_hits_对空文本安全():
    assert find_colloquial_hits("") == []


# ---------------------------------------------------------------------------
# 四、质量审计路由
# ---------------------------------------------------------------------------
async def test_sections_quality_路由(db_conn):
    from app.routers.sections import sections_quality

    await db_conn.execute(
        "INSERT INTO projects (id, name) VALUES (?,?)", ("p1", "测试项目"))
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name, type) VALUES (?,?,?,?)",
        ("s1", "p1", "临时用电专项施工方案", "临时用电"))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, title, level, sort_order, content)"
        " VALUES (?,?,?,?,?,?)",
        ("sec1", "s1", "配电线路敷设", 1, 0, "本工程按 JGJ 46-2005 执行，咱们要搞定。"))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, title, level, sort_order, content)"
        " VALUES (?,?,?,?,?,?)",
        ("sec2", "s1", "接地与防雷", 1, 1, "接地电阻不应大于4Ω，采用TN-S接零保护系统。"))
    await db_conn.commit()

    result = await sections_quality("s1", db_conn)

    assert result["summary"]["checked"] == 2
    assert result["summary"]["problem_sections"] == 1
    assert result["summary"]["abolished_standards"] == 1
    assert result["items"][0]["section_id"] == "sec1"
    assert "JGJ 46-2005" in result["items"][0]["abolished_standards"]
    assert result["standard_db_version"]
