"""生成侧防回归测试（2026-11-05 · 对应《机电安装施工组织设计_20261104_第1轮》格式问题报告）。

背景
----
第1轮成稿暴露的缺陷（报告 §4/§9/§11/§12）已定位到**生成链路**三处根因：
  1. 解析前置归一化缺失：字面 ``\\n`` 粘行、PDF 分页标记、表题与 GFM 表头
     粘连行、确定性错别字（峻工/力矩矩/0.5MQ/``1.5% 0``）原样进入解析器；
  2. 编号重排不剥源前缀：E3 降级 + 有序列表重编号把 AI 双写的源序号叠加
     成 "a、a、吊顶" / "1）1）国家法律"；
  3. 悬空引用无回收：源文只有"施工流程图如下："引导语而无图表块时，引导
     语原样印进成稿；内容子标题与章节标题同文本重复印出。

本文件锁定上述三条防线 + 落盘前 lint 守卫的行为：
复跑：``python -m pytest tests/test_content_sanitize_regression_20261105.py -q``
"""

import sys

from app.services import content_blocks as cb
from app.services.content_sanitize import normalize_raw_content

# ---------------------------------------------------------------------------
# 端到端：第1轮报告全部缺陷形态经完整导出链路后的成稿校验
# ---------------------------------------------------------------------------

# 模拟第1轮《机电安装施工组织设计》实测坏形态（构造自报告 §4/§9/§10/§11）：
#   字面 \n 粘行 / 分页标记 / 错别字单位 / 无图引导语 / 字母序子标题双写 /
#   有序列表 "1）" 双写 / 表题与表头粘连且缺分隔行
_DEFECT_CONTENT = (
    "本节为机电安装总体部署。\n"
    "===== Page 3 =====\n"
    "第184 页共259 页\n"
    "峻工验收要求：绝缘电阻0.5MQ、坡度1.5% 0、温度40 °C。\n"
    "施工准备\\n1）人员进场\\n2）机械进场\n"
    "施工工艺流程图如下：\n"
    "## a、吊顶与内装接口\n"
    "吊顶安装正文。\n"
    "1）国家法律\n"
    "1）国家法律是工程建设的根本遵循\n"
    "表4-1 智能化系统联调测试项|测试系统|测试项目|\n"
    "|视频监控|图像切换|\n"
)


def _build_docx(tmp_path, parent_content: str, child_content: str) -> str:
    from app.routers.export import _build_docx_sync, _load_heading_styles

    sections = [
        {
            "id": "c1",
            "parent_id": "",
            "sort_order": 0,
            "level": 1,
            "title": "第一章 总体部署",
            "content": parent_content,
        },
        {
            "id": "c1-1",
            "parent_id": "c1",
            "sort_order": 0,
            "level": 2,
            "title": "1.1 工程概况",
            "content": child_content,
        },
    ]
    blocks = {s["id"]: cb._parse_content_blocks(s["content"]) for s in sections}
    out = str(tmp_path / "defect_e2e.docx")
    _build_docx_sync(
        out,
        {"id": "s1", "project_id": "p1", "name": "测试方案"},
        [sections[0]],
        {"c1": [sections[1]]},
        {},
        {},
        blocks,
        "宋体",
        12,
        "",
        "",
        False,
        False,
        False,
        bidder_name="",
        heading_styles=_load_heading_styles({}),
        page_break_before_chapter=True,
        line_spacing=1.15,
        page_number_style="simple",
        toc_depth=3,
    )
    return out


def test_export_e2e_no_defect_patterns_in_final_docx(tmp_path):
    """完整导出链路后，成稿不得再出现第1轮报告的任何缺陷形态。"""
    from docx import Document

    out = _build_docx(tmp_path, _DEFECT_CONTENT, "工程概况正文。\n")
    paras = [p.text for p in Document(out).paragraphs]
    joined = "\n".join(paras)
    tbl_texts = "\n".join(cell.text for t in Document(out).tables for row in t.rows for cell in row.cells)

    # ① 分页标记 / 页脚残留 → 成稿无痕
    assert "Page 3" not in joined and "共259" not in joined, paras
    # ② 错别字与单位符号 → 全部修复
    assert "峻工" not in joined, paras
    assert "0.5MΩ" in joined and "1.5‰" in joined and "40℃" in joined, paras
    # ③ 字面 \n 粘行 → 拆成独立段（"1）人员进场"不再与"施工准备"粘连）
    assert "施工准备\\n1）人员进场" not in joined, paras
    assert any(t == "2）机械进场" for t in paras), paras
    # ④ 无图引导语（其后无图表块）→ 回收
    assert not any("流程图如下" in t for t in paras), paras
    # ⑤ E3 降级字母序子标题 → "1）、吊顶与内装接口"（源 "a、" 前缀已剥，不双写）
    assert "1）、吊顶与内装接口" in joined, paras
    assert "a、a、" not in joined and "a、）" not in joined and "a、" not in joined, paras
    # ⑥ 有序列表 "1）" 双写 → 重编号递增，不叠加
    assert "1）1）" not in joined and "1）2）" not in joined, paras
    assert any(t == "2）国家法律是工程建设的根本遵循" for t in paras), paras
    # ⑦ 表题粘连 + 缺分隔行 → 真表格 + 表题，管道符不残留为段落
    assert any(t.startswith("表 1-1") for t in paras), paras
    assert "|视频监控" not in joined and "|测试系统" not in joined, paras
    assert "视频监控" in tbl_texts and "测试系统" in tbl_texts, tbl_texts
    # ⑧ 成稿任何段落不得残留 ≥2 个管道符（表格已在 w:tbl 内）
    assert not any(t.count("|") >= 2 for t in paras), paras
    # ⑨ 成稿不得残留字面 \n
    assert "\\n" not in joined, paras


def test_export_e2e_leaf_section_letter_prefix_renumbered(tmp_path):
    """叶子章节（无 DB 子章节）的字母序子标题 → X.N 重编号，源前缀剥离。"""
    from docx import Document

    out = _build_docx(tmp_path, "父章正文。\n", "概况正文。\n## b、风管安装要求\n风管正文。\n")
    joined = "\n".join(p.text for p in Document(out).paragraphs)
    assert "b、风管安装要求" not in joined, joined
    assert "风管安装要求" in joined, joined


# ---------------------------------------------------------------------------
# 防线一：解析前置归一化
# ---------------------------------------------------------------------------


class TestNormalizeRawContent:
    def test_literal_backslash_n_becomes_real_newline(self):
        # 第1轮报告：40 行内容粘进一个段落（源文换行被转义成字面 \n）
        src = "施工准备\\n1）人员进场\\n2）机械进场"
        out, stats = normalize_raw_content(src)
        assert "\\n" not in out
        assert out == "施工准备\n1）人员进场\n2）机械进场"
        assert stats["literal_newline"] == 2
        # 幂等
        out2, stats2 = normalize_raw_content(out)
        assert out2 == out and not any(stats2.values())

    def test_double_backslash_n_not_touched(self):
        # LaTeX/正则语义的 \\n 不受影响
        out, stats = normalize_raw_content(r"a\\nb")
        assert out == r"a\\nb" and stats["literal_newline"] == 0

    def test_page_mark_and_footer_removed(self):
        src = "正文段落A\n===== Page 12 =====\n正文段落B\n第184 页共259 页\n正文段落C"
        out, stats = normalize_raw_content(src)
        assert "Page 12" not in out and "共259" not in out
        assert "正文段落A" in out and "正文段落B" in out and "正文段落C" in out
        assert stats["page_marks"] == 2

    def test_caption_pipe_row_split(self):
        # 第1轮报告：表题与 GFM 表头粘连一行，管道符表残留为正文文本
        src = "表4-1 智能化系统联调测试项|测试系统|测试项目|验收标准|记录方式|\n|视频监控|图像切换|≤2s|人工记录|"
        out, stats = normalize_raw_content(src)
        lines = out.split("\n")
        assert lines[0] == "表4-1 智能化系统联调测试项"
        assert lines[1].startswith("|测试系统|")
        assert stats["caption_pipe_split"] == 1
        # 解析器应把拆出的行识别为表格（首行=表头，AI 漏写分隔行时自动合成）
        blocks = cb._parse_content_blocks(out)
        tables = [b for b in blocks if b["type"] == "table"]
        assert tables and tables[0].get("lines")
        assert tables[0]["lines"][0].startswith("|测试系统|")

    def test_no_caption_prefix_left_alone(self):
        # 普通正文含竖线（无表题前缀）不动，避免误伤
        src = "本节论述A与B|的关系。"
        out, stats = normalize_raw_content(src)
        assert out == src and stats["caption_pipe_split"] == 0

    def test_typos_fixed(self):
        out, stats = normalize_raw_content("峻工验收、施加力矩矩、绝缘电阻0.5MQ、坡度1.5% 0、温度40 °C")
        assert "峻工" not in out and "竣工" in out
        assert "矩矩" not in out
        assert "0.5MΩ" in out
        assert "1.5‰" in out
        assert "40℃" in out
        assert stats["typo_fixed"] == 5

    def test_normal_percent_not_touched(self):
        out, _ = normalize_raw_content("合格率95%、占比0.85")
        assert "95%" in out and "0.85" in out


# ---------------------------------------------------------------------------
# 防线二：编号重排不叠源前缀（"a、a、" / "1）1）"）
# ---------------------------------------------------------------------------


class TestSubitemPrefix:
    def test_strip_letter_prefix(self):
        assert cb.strip_subitem_ordinal_prefix("a、吊顶与内装接口") == "吊顶与内装接口"
        assert cb.strip_subitem_ordinal_prefix("B. 风管安装") == "风管安装"
        assert cb.strip_subitem_ordinal_prefix("c）保温施工") == "保温施工"

    def test_idempotent_and_safe(self):
        assert cb.strip_subitem_ordinal_prefix("吊顶安装") == "吊顶安装"
        assert cb.strip_subitem_ordinal_prefix("") == ""
        assert cb.strip_subitem_ordinal_prefix("a、") == "a、"  # 剥后为空 → 原样

    def test_digit_content_not_stripped(self):
        # "3、5号楼"是实质内容，字母序剥离绝不吞数字开头
        assert cb.strip_subitem_ordinal_prefix("3、5号楼立面") == "3、5号楼立面"


# ---------------------------------------------------------------------------
# 防线三：悬空引导语回收 + 重复内容标题剔除
# ---------------------------------------------------------------------------


class TestDanglingLeadIn:
    def test_lead_in_without_chart_dropped(self):
        # 第1轮报告 §9："施工流程图如下："后压根没有图表块
        src = "## 4.2 施工工艺\n施工工艺流程图如下：\n4.3 质量验收"
        blocks = cb._parse_content_blocks(src)
        lead = [b for b in blocks if b["type"] == "paragraph" and "如下" in b["text"]]
        assert not lead, "无图引导语必须被回收"

    def test_lead_in_before_chart_kept(self):
        src = "## 4.2 施工工艺\n施工工艺流程图如下：\n```mermaid\ngraph TD\nA-->B\n```"
        blocks = cb._parse_content_blocks(src)
        assert any("如下" in (b.get("text") or "") for b in blocks if b["type"] == "paragraph")

    def test_list_lead_in_not_dropped(self):
        # 「要求如下：」引出列表是正当行文（无"图/表"字样）→ 绝不删
        src = "## 3.1 要求\n具体要求如下：\n1）人员持证上岗\n2）机械定期保养"
        blocks = cb._parse_content_blocks(src)
        assert any("要求如下" in (b.get("text") or "") for b in blocks)

    def test_orphan_lead_in_helper_covers_word_order_variant(self):
        # 宽判据覆盖"流程图如下"词序变体（旧 LEAD_IN_HINT_RE 漏掉）
        assert cb._is_orphan_lead_in_text("施工工艺流程图如下：")
        assert cb._is_orphan_lead_in_text("如下图所示。")
        assert not cb._is_orphan_lead_in_text("具体要求如下：" * 20)  # 超长不删


class TestDuplicateSectionHeading:
    def _render_blocks(self, title, content):
        blocks = cb._parse_content_blocks(content)
        blocks = cb._strip_duplicate_leading_title(blocks, title, title)
        norms = {cb._norm_heading_text(t) for t in (title, title)}
        norms.discard("")
        return [b for b in blocks if not (b["type"] == "heading" and cb._norm_heading_text(b["text"]) in norms)]

    def test_content_heading_same_as_section_title_removed(self):
        # 第1轮报告 §12：章节"通风空调系统施工工艺"正文里又有内容标题
        # "1）、通风空调系统施工工艺" → 同页两标题
        blocks = self._render_blocks(
            "3.4 通风空调系统施工工艺",
            "## 通风空调系统施工工艺\n正文第一段。\n正文第二段。",
        )
        heads = [b["text"] for b in blocks if b["type"] == "heading"]
        assert heads == [], "与章节标题同文本的内容子标题必须剔除"
        assert any(b["type"] == "paragraph" and "正文第一段" in b["text"] for b in blocks)

    def test_other_content_headings_kept(self):
        blocks = self._render_blocks(
            "3.4 通风空调系统施工工艺",
            "## 风管安装要求\n正文A。\n## 保温施工要求\n正文B。",
        )
        heads = [b["text"] for b in blocks if b["type"] == "heading"]
        assert len(heads) == 2, "与章节标题不同文本的内容子标题不受影响"
