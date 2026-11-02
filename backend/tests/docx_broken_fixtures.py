# -*- coding: utf-8 -*-
"""两轮次导出测试基建：**程序化构造错误初稿**（R147 · 用户指令 §五）。

先用 python-docx 产出一份**合法**基础稿（章节树 / 正文 / 居中插图 / 规范图题），
再按缺陷清单在 zip 层注入 OpenXML 缺陷 —— 保证：
- 无缺陷时产物与真实导出稿同构（检查器零误报基线）；
- 逐类缺陷可独立开关（定向护栏与修复收敛共用同一构造器）。
"""
from __future__ import annotations

import io
import re
import shutil
import zipfile

#: 可注入缺陷全集（与 RepairFinding.code 对应）
ALL_DEFECTS = frozenset({
    "level_jump", "num_seq", "no_indent", "bad_align", "num_conflict",
    "no_eastasia", "sz_odd", "heading_italic",
    "broken_media", "not_centered", "oversize", "caption_fmt", "seq_gap",
    "ns_decl", "missing_rel", "content_type",
    # R149：真实工程稿高频形态（伪标题 / 深度错配 / 关系断链 / 列表拍平 / 中英混杂）
    "heading6_fake", "l3_depth", "rel_break", "inline_list", "mixed_font",
})

_TINY_PNG: bytes | None = None


def tiny_png() -> bytes:
    """16×16 纯色 PNG（PIL 生成，保证 python-docx 可插入、PIL 可解码）。"""
    global _TINY_PNG
    if _TINY_PNG is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), (200, 210, 220)).save(buf, format="PNG")
        _TINY_PNG = buf.getvalue()
    return _TINY_PNG


def build_base_docx(path: str) -> None:
    """合法基础稿：L1+L2 章节、正文（样式层缩进/行距）、居中插图、规范图题。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    doc = Document()
    body = doc.styles["Normal"]
    body.font.name = "宋体"
    body.font.size = Pt(12)
    body.paragraph_format.first_line_indent = Cm(0.74)
    body.paragraph_format.line_spacing = 1.5

    doc.add_paragraph("第一章 总体概述", style="Heading 1")
    doc.add_paragraph("1.1 编制依据", style="Heading 2")
    doc.add_paragraph(
        "本方案依据国家现行施工验收规范、设计图纸与现场踏勘资料编制，"
        "内容涵盖施工部署、进度计划、质量保证措施与安全文明施工要求。")
    doc.add_paragraph("1.2 施工部署", style="Heading 2")
    doc.add_paragraph(
        "施工总体安排按照先地下后地上、先结构后装修的原则组织，"
        "各专业交叉作业统一协调，确保工期与质量安全目标全面实现。")

    doc.add_paragraph("第二章 施工工艺", style="Heading 1")
    doc.add_paragraph("2.1 工艺流程", style="Heading 2")
    doc.add_paragraph(
        "土方开挖采用分层分段法施工，每层厚度控制在三米以内，"
        "边坡按设计坡率放坡并随挖随支护，严禁超挖与扰动基底原状土。")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_picture(io.BytesIO(tiny_png()), width=Cm(6))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.add_run("图 1-1 施工总体流程图")
    # 「BodyNoIndent」样式（不基于 Normal，无缩进/无行距）：
    #   供 no_indent 缺陷载体 —— 干净稿中该样式存在但无人使用（零误报）
    from docx.enum.style import WD_STYLE_TYPE
    s = doc.styles.add_style("BodyNoIndent", WD_STYLE_TYPE.PARAGRAPH)
    s.font.name = "宋体"
    s.font.size = Pt(12)
    doc.save(path)


def build_clean_docx(path: str) -> None:
    """零缺陷对照稿（检查器必须零 error 命中，防误报基线）。"""
    build_base_docx(path)


def _rewrite_zip(path: str, mutate: dict) -> None:
    """部件级重写：mutate = {name: bytes | callable(bytes)->bytes | None(删除)}。"""
    tmp = path + ".tmp"
    with zipfile.ZipFile(path) as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name in mutate:
                fn = mutate[name]
                if fn is None:
                    continue
                data = fn(data) if callable(fn) else fn
            zout.writestr(name, data)
        for name, val in mutate.items():
            if name not in zin.namelist() and val is not None \
                    and not callable(val):
                zout.writestr(name, val)
    shutil.move(tmp, path)


def build_broken_docx(path: str, defects: set[str]) -> list[str]:
    """构造含指定缺陷的初稿；返回实际注入的缺陷清单。"""
    build_base_docx(path)
    injected: list[str] = []

    def doc_xml(xml: str) -> str:
        if "level_jump" in defects:
            # 在 H1「第一章」之后直插 Heading3：H1→H3 结构性跳变
            xml = xml.replace(
                "<w:t>第一章 总体概述</w:t>",
                "<w:t>第一章 总体概述</w:t></w:r></w:p>"
                "<w:p><w:pPr><w:pStyle w:val=\"Heading3\"/></w:pPr>"
                "<w:r><w:t>1.0.1 越级标题</w:t>", 1)
            injected.append("level_jump")
        if "l3_depth" in defects:
            # L3 使用「1.2.1 / 1.3.2」三段深度：第二项与 L2 计数失配 → 应重排
            xml = xml.replace(
                "<w:t>1.2 施工部署</w:t>",
                "<w:t>1.2 施工部署</w:t></w:r></w:p>"
                "<w:p><w:pPr><w:pStyle w:val=\"Heading3\"/></w:pPr>"
                "<w:r><w:t>1.2.1 施工段划分</w:t></w:r></w:p>"
                "<w:p><w:pPr><w:pStyle w:val=\"Heading3\"/></w:pPr>"
                "<w:r><w:t>1.3.2 资源配置</w:t>", 1)
            injected.append("l3_depth")
        if "num_seq" in defects:
            xml = xml.replace("<w:t>1.2 施工部署</w:t>",
                              "<w:t>1.9 施工部署</w:t>", 1)
            injected.append("num_seq")
        if "no_indent" in defects:
            # 用无缩进的 BodyNoIndent 样式作载体（继承链无 indent，必命中）
            xml = xml.replace(
                "</w:body>",
                "<w:p><w:pPr><w:pStyle w:val=\"BodyNoIndent\"/></w:pPr>"
                "<w:r><w:t>安全文明施工目标为杜绝重大安全事故与环境污染事件发生，"
                "现场围挡、材料堆放与临时用电全部按照标准化要求布置到位并逐项验收合格。</w:t>"
                "</w:r></w:p></w:body>", 1)
            injected.append("no_indent")
        if "bad_align" in defects:
            xml = xml.replace(
                "边坡按设计坡率放坡并随挖随支护，严禁超挖与扰动基底原状土。",
                "</w:t></w:r></w:p><w:p><w:pPr><w:jc w:val=\"right\"/></w:pPr>"
                "<w:r><w:t>边坡按设计坡率放坡并随挖随支护，严禁超挖与扰动基底原状土。"
                "补充说明文字用于满足正文段最短长度判定要求，确保检查器稳定命中该缺陷类型。", 1)
            injected.append("bad_align")
        if "num_conflict" in defects:
            xml = xml.replace(
                "</w:body>",
                "<w:p><w:pPr><w:numPr><w:ilvl w:val=\"0\"/><w:numId w:val=\"1\"/>"
                "</w:numPr></w:pPr><w:r><w:t>一、安全生产管理措施"
                "包括安全教育、班前检查与隐患排查治理三项制度化安排，落实责任到人。</w:t>"
                "</w:r></w:p></w:body>", 1)
            injected.append("num_conflict")
        if "heading_italic" in defects:
            xml = xml.replace(
                "<w:t>第一章 总体概述</w:t>",
                "<w:t>第一章 总体概述</w:t></w:r></w:p>"
                "<w:p><w:pPr><w:pStyle w:val=\"Heading1\"/></w:pPr>"
                "<w:r><w:rPr><w:i/></w:rPr><w:t>斜体标题测试</w:t>", 1)
            injected.append("heading_italic")
        if "sz_odd" in defects:
            xml = xml.replace(
                "本方案依据国家现行施工验收规范",
                "</w:t></w:r><w:r><w:rPr><w:sz w:val=\"25\"/></w:rPr>"
                "<w:t>半点字号", 1)
            injected.append("sz_odd")
        if "no_eastasia" in defects:
            xml = xml.replace(
                "<w:r><w:t>1.1 编制依据</w:t>",
                "<w:r><w:rPr><w:rFonts w:ascii=\"Times New Roman\" "
                "w:hAnsi=\"Times New Roman\"/></w:rPr><w:t>1.1 编制依据</w:t>", 1)
            injected.append("no_eastasia")
        if "not_centered" in defects:
            # 精确剥离**图片所在段**的居中（段内含 w:drawing 的那个）
            def _strip_center(m: re.Match) -> str:
                return m.group(0).replace('<w:jc w:val="center"/>', "", 1)
            xml = re.sub(
                r"<w:p>(?:(?!</w:p>).)*?<w:drawing.*?</w:p>",
                lambda m: (_strip_center(m) if '<w:jc w:val="center"/>' in m.group(0)
                           else m.group(0)),
                xml, count=1, flags=re.S)
            injected.append("not_centered")
        if "oversize" in defects:
            xml = re.sub(r'<wp:extent cx="(\d+)" cy="(\d+)"\s*/>',
                         lambda m: f'<wp:extent cx="90000000" cy="{m.group(2)}"/>',
                         xml, count=1)
            injected.append("oversize")
        if "caption_fmt" in defects:
            xml = xml.replace("<w:t>图 1-1 施工总体流程图</w:t>",
                              "<w:t>图1-1施工总体流程图</w:t>", 1)
            injected.append("caption_fmt")
        if "seq_gap" in defects:
            xml = xml.replace(
                "</w:body>",
                "<w:p><w:pPr><w:jc w:val=\"center\"/></w:pPr>"
                "<w:r><w:t>图 1-9 基坑支护剖面图</w:t></w:r></w:p></w:body>", 1)
            injected.append("seq_gap")
        if "ns_decl" in defects:
            # 真实形态：Word 2013+ 文档带 w15 声明并列入 mc:Ignorable
            # （不重复注入 mc:Ignorable —— 重复属性本身即非法 XML，非真实缺陷形态）
            xml = xml.replace(
                "<w:document ",
                "<w:document "
                "xmlns:w15=\"http://schemas.microsoft.com/office/word/2012/wordml\" ", 1)
            xml = re.sub(r'mc:Ignorable="([^"]*)"',
                         lambda m: f'mc:Ignorable="{m.group(1)} w15"', xml, count=1)
            injected.append("ns_decl")
        if "missing_rel" in defects:
            xml = xml.replace("<a:blip r:embed=",
                              "<a:blip r:embed=\"rIdBroken\" r:embed2=", 1)
            injected.append("missing_rel")
        if "heading6_fake" in defects:
            # 真实稿形态：列表项被误设为 Heading6（1）、/ a、），不参与大纲编号
            xml = xml.replace(
                "</w:body>",
                "<w:p><w:pPr><w:pStyle w:val=\"Heading6\"/></w:pPr>"
                "<w:r><w:t>1）、设计文件类依据</w:t></w:r></w:p>"
                "<w:p><w:pPr><w:pStyle w:val=\"Heading6\"/></w:pPr>"
                "<w:r><w:t>a、现场踏勘记录</w:t></w:r></w:p></w:body>", 1)
            injected.append("heading6_fake")
        if "inline_list" in defects:
            # 列表枚举被拍平成标题（（1）…），不应产生编号 finding
            xml = xml.replace(
                "</w:body>",
                "<w:p><w:pPr><w:pStyle w:val=\"Heading4\"/></w:pPr>"
                "<w:r><w:t>（1）验收标准</w:t></w:r></w:p></w:body>", 1)
            injected.append("inline_list")
        if "mixed_font" in defects:
            # 中英字体混杂：西文槽被写成中文字体（数字/英文随宋体走）
            xml = xml.replace(
                "<w:r><w:t>2.1 工艺流程</w:t></w:r>",
                "<w:r><w:rPr><w:rFonts w:ascii=\"宋体\" w:hAnsi=\"宋体\" "
                "w:eastAsia=\"宋体\"/></w:rPr><w:t>2.1 工艺流程</w:t></w:r>", 1)
            injected.append("mixed_font")
        return xml

    mutate: dict = {"word/document.xml": (
        lambda d: doc_xml(d.decode("utf-8")).encode("utf-8"))}
    rel_mutators: list = []

    if "broken_media" in defects:
        mutate["word/media/image1.png"] = b"\x00\x01\x02broken" * 10
        injected.append("broken_media")
    if "missing_rel" in defects:
        def _rels(data: bytes) -> bytes:
            # 移除 image 关系制造断链；另附一个未登记媒体部件供补建
            s = re.sub(r'<Relationship [^>]*Target="media/image1\.png"[^>]*/>', "",
                       data.decode("utf-8"))
            return s.encode("utf-8")
        rel_mutators.append(_rels)
        mutate["word/media/spare.png"] = tiny_png()
    if "rel_break" in defects:
        def _rels_break(data: bytes) -> bytes:
            # 关系指向不存在的目标部件（真实稿中的 rel 目标缺失形态）
            s = data.decode("utf-8").replace(
                "</Relationships>",
                '<Relationship Id="rIdGhost" Type="http://schemas.openxmlformats.'
                'org/officeDocument/2006/relationships/image" '
                'Target="media/ghost.png"/></Relationships>')
            return s.encode("utf-8")
        rel_mutators.append(_rels_break)
        injected.append("rel_break")
    if rel_mutators:
        def _rels_all(data: bytes) -> bytes:
            for fn in rel_mutators:
                data = fn(data)
            return data
        mutate["word/_rels/document.xml.rels"] = _rels_all
    if "content_type" in defects:
        def _ct(data: bytes) -> bytes:
            return re.sub(r'<Default Extension="png"[^>]*/>', "",
                          data.decode("utf-8")).encode("utf-8")
        mutate["[Content_Types].xml"] = _ct
        injected.append("content_type")

    _rewrite_zip(path, mutate)
    return injected
