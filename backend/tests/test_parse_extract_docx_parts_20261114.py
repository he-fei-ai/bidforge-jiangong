# -*- coding: utf-8 -*-
"""R238 解析提取模块：DOCX 内容控件与页眉/页脚/脚注/尾注部件完整性。

聚焦【解析提取模块】（import）的两个真实 BUG：
  - BUG-1：``w:sdt``（内容控件）整段静默丢失 —— 旧实现只遍历 ``w:body``
    的直接子元素 ``p``/``tbl``，``w:sdt`` 被整体跳过；
  - BUG-2：页眉/页脚（及脚注/尾注）部件静默丢失 —— 旧实现从不读取
    header/footer/footnote/endnote parts。

护栏目标：
  - ``w:sdt`` 内段落/表格保留（含嵌套 sdt 递归）；
  - 页眉/页脚经 document.xml.rels 解析后保留；
  - 脚注/尾注按 id 取对应条目保留；
  - 无 rels / 缺 part / 损坏 rels 时不崩溃（正文仍解析）；
  - 既有普通正文/表格输出不受影响（旧行为不回归）；
  - 部件压缩炸弹 ParseError 仍直达用户（安全闸门不被静默吞掉）；
  - PARSE_DOCX_PARTS=0 可整体回退旧行为。
"""

import io
import zipfile

import pytest
from app.services import file_parser as fp

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"

_W = f'xmlns:w="{_W_NS}"'
_R = f'xmlns:r="{_OFFICE_RELS}"'
_REL = f'xmlns="{_PKG_RELS}"'


def _p(text: str, style: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _tc(text: str) -> str:
    return f'<w:tc><w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:tc>'


def _tbl(rows: list[list[str]]) -> str:
    inner = "".join("<w:tr>" + "".join(_tc(c) for c in row) + "</w:tr>" for row in rows)
    return f"<w:tbl>{inner}</w:tbl>"


def _document(body_inner: str, refs: str = "") -> str:
    sect = f"<w:sectPr>{refs}</w:sectPr>" if refs else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W} {_R}><w:body>{body_inner}{sect}</w:body></w:document>'


def _rels(pairs: list[tuple[str, str, str]]) -> str:
    inner = "".join(
        f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{typ}" Target="{target}"/>'
        for rid, typ, target in pairs
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Relationships {_REL}>{inner}</Relationships>'


def _part_root(tag: str, text: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><w:{tag} {_W}>'
        f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:{tag}>'
    )


def _notes(tag: str, entries: dict[str, str]) -> str:
    inner = "".join(
        f'<w:{tag} w:id="{nid}"><w:p><w:r><w:t xml:space="preserve">{txt}</w:t></w:r></w:p></w:{tag}>'
        for nid, txt in entries.items()
    )
    plural = "footnotes" if tag == "footnote" else "endnotes"
    return f'<?xml version="1.0" encoding="UTF-8"?><w:{plural} {_W}>{inner}</w:{plural}>'


def _build_docx(document_xml: str, extra: dict[str, str] | None = None) -> bytes:
    files = {"word/document.xml": document_xml}
    if extra:
        files.update(extra)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# BUG-1：w:sdt 内容控件
# ---------------------------------------------------------------------------


def test_docx_sdt_paragraph_and_table_preserved():
    """sdtContent 内的段落/表格必须保留；嵌套 sdt 递归下沉。"""
    inner = (
        '<w:sdt><w:sdtContent>'
        + _p("合同编号")
        + _tbl([["甲", "乙"], ["1", "2"]])
        + '<w:sdt><w:sdtContent>' + _p("嵌套控件") + "</w:sdtContent></w:sdt>"
        + "</w:sdtContent></w:sdt>"
        + _p("正文后")
    )
    out = fp._parse_docx(_build_docx(_document(inner)))
    assert "合同编号" in out
    assert "| 甲 | 乙 |" in out
    assert "| --- | --- |" in out
    assert "嵌套控件" in out  # 嵌套 sdt 亦递归
    assert "正文后" in out


def test_docx_sdt_plain_body_unchanged_when_no_sdt():
    """不含 sdt 时正文输出与旧口径一致（表格/段落原样）。"""
    out = fp._parse_docx(_build_docx(_document(_p("标题正文") + _tbl([["a", "b"], ["1", "2"]]))))
    assert "标题正文" in out
    assert "【表格】" in out
    assert "| a | b |" in out
    assert "| 1 | 2 |" in out
    assert "【页眉】" not in out
    assert "【页脚】" not in out
    assert "【脚注】" not in out
    assert "【尾注】" not in out


# ---------------------------------------------------------------------------
# BUG-2：页眉/页脚
# ---------------------------------------------------------------------------


def test_docx_header_footer_preserved():
    """headerReference/footerReference 经 rels 解析，部件内容带标记追加。"""
    doc = _document(
        _p("正文"),
        '<w:headerReference w:type="default" r:id="rId1"/>'
        '<w:footerReference w:type="default" r:id="rId2"/>',
    )
    files = {
        "word/_rels/document.xml.rels": _rels([("rId1", "header", "header1.xml"), ("rId2", "footer", "footer1.xml")]),
        "word/header1.xml": _part_root("hdr", "XX单位名称"),
        "word/footer1.xml": _part_root("ftr", "第 1 页"),
    }
    out = fp._parse_docx(_build_docx(doc, files))
    assert "【页眉】" in out
    assert "XX单位名称" in out
    assert "【页脚】" in out
    assert "第 1 页" in out
    assert "正文" in out


def test_docx_header_footer_same_part_deduped():
    """同一部件被多个 section 引用时只追加一次（标记不重复）。"""
    doc = _document(
        _p("正文"),
        '<w:headerReference w:type="default" r:id="rId1"/>'
        '<w:headerReference w:type="first" r:id="rId1"/>',
    )
    files = {
        "word/_rels/document.xml.rels": _rels([("rId1", "header", "header1.xml")]),
        "word/header1.xml": _part_root("hdr", "单位名称"),
    }
    out = fp._parse_docx(_build_docx(doc, files))
    assert "【页眉】" in out
    assert out.count("【页眉】") == 1


# ---------------------------------------------------------------------------
# 脚注/尾注
# ---------------------------------------------------------------------------


def test_docx_footnote_endnote_preserved():
    """footnoteReference/endnoteReference 按 id 取对应条目，分隔符条目跳过。"""
    doc = _document(
        _p("正文")
        + '<w:r><w:footnoteReference w:id="2"/></w:r>'
        + '<w:r><w:endnoteReference w:id="3"/></w:r>'
    )
    files = {
        "word/footnotes.xml": _notes("footnote", {"-1": "分隔符", "2": "详见附件"}),
        "word/endnotes.xml": _notes("endnote", {"3": "尾注内容"}),
    }
    out = fp._parse_docx(_build_docx(doc, files))
    assert "【脚注】" in out
    assert "详见附件" in out
    assert "分隔符" not in out  # w:id=-1 是脚注分隔符，不属于被引用条目
    assert "【尾注】" in out
    assert "尾注内容" in out


# ---------------------------------------------------------------------------
# 健壮性：缺失/损坏 rels 与部件
# ---------------------------------------------------------------------------


def test_docx_missing_rels_or_part_does_not_crash():
    """无 rels 文件 / rels 指向缺失部件时静默跳过，正文仍解析。"""
    doc = _document(_p("正文"), '<w:headerReference w:type="default" r:id="rId9"/>')
    # 无 rels
    out = fp._parse_docx(_build_docx(doc))
    assert "正文" in out
    assert "【页眉】" not in out
    # rels 存在但目标部件缺失
    files = {"word/_rels/document.xml.rels": _rels([("rId9", "header", "missing.xml")])}
    out2 = fp._parse_docx(_build_docx(doc, files))
    assert "正文" in out2
    assert "【页眉】" not in out2


def test_docx_malformed_rels_does_not_crash():
    """rels 文件损坏（非法 XML）时按空表处理，正文仍解析。"""
    doc = _document(_p("正文"), '<w:headerReference w:type="default" r:id="rId1"/>')
    files = {"word/_rels/document.xml.rels": b"<Relationships><broken"}
    out = fp._parse_docx(_build_docx(doc, files))
    assert "正文" in out
    assert "【页眉】" not in out


# ---------------------------------------------------------------------------
# 安全闸门不被静默吞掉 / 环境开关
# ---------------------------------------------------------------------------


def test_docx_part_bomb_propagates_parse_error(monkeypatch):
    """部件条目超限必须抛 ParseError（不能像普通部件缺失那样静默跳过）。"""
    monkeypatch.setattr(fp, "MAX_ARCHIVE_ENTRY_BYTES", 512)
    doc = _document(_p("正文"), '<w:headerReference w:type="default" r:id="rId1"/>')
    files = {
        "word/_rels/document.xml.rels": _rels([("rId1", "header", "header1.xml")]),
        "word/header1.xml": _part_root("hdr", "x" * 2000),
    }
    with pytest.raises(fp.ParseError) as e:
        fp._parse_docx(_build_docx(doc, files))
    assert "条目过大" in str(e.value)


def test_docx_parts_env_switch_disables(monkeypatch):
    """PARSE_DOCX_PARTS=0 时页眉/页脚不读取（正文不受影响）。"""
    monkeypatch.setattr(fp, "_PARSE_DOCX_PARTS", False)
    doc = _document(_p("正文"), '<w:headerReference w:type="default" r:id="rId1"/>')
    files = {
        "word/_rels/document.xml.rels": _rels([("rId1", "header", "header1.xml")]),
        "word/header1.xml": _part_root("hdr", "单位名称"),
    }
    out = fp._parse_docx(_build_docx(doc, files))
    assert "单位名称" not in out
    assert "正文" in out


def test_docx_heading_level_in_sdt_preserved():
    """sdt 内标题样式（Heading1）仍提升为 markdown 标题。"""
    inner = '<w:sdt><w:sdtContent>' + _p("第一章 总则", style="Heading1") + "</w:sdtContent></w:sdt>"
    out = fp._parse_docx(_build_docx(_document(inner)))
    assert "# 第一章 总则" in out


# ---------------------------------------------------------------------------
# BUG-3：mc:AlternateContent 与修订（w:ins/w:del）/自定义 XML 透明容器
# ---------------------------------------------------------------------------


_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MC = f'xmlns:mc="{_MC_NS}"'


def _document_mc(body_inner: str) -> str:
    """带 mc 命名空间的 document.xml（AlternateContent 需要）。"""
    return f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W} {_R} {_MC}><w:body>{body_inner}</w:body></w:document>'


def test_docx_alternate_content_choice_preserved():
    """mc:AlternateContent 内正文不再静默丢失；优先取 Choice（现代表示）。"""
    inner = (
        _p("前")
        + '<mc:AlternateContent><mc:Choice Requires="w14">'
        + _p("现代内容")
        + "</mc:Choice><mc:Fallback>"
        + _p("回退内容")
        + "</mc:Fallback></mc:AlternateContent>"
        + _p("后")
    )
    out = fp._parse_docx(_build_docx(_document_mc(inner)))
    assert "前" in out
    assert "现代内容" in out
    assert "回退内容" not in out  # Choice 优先，不回退重复收集
    assert "后" in out


def test_docx_alternate_content_fallback_when_no_choice():
    """AlternateContent 无 Choice 时取 Fallback（不丢内容）。"""
    inner = '<mc:AlternateContent><mc:Fallback>' + _p("仅回退内容") + "</mc:Fallback></mc:AlternateContent>"
    out = fp._parse_docx(_build_docx(_document_mc(inner)))
    assert "仅回退内容" in out


def test_docx_tracked_changes_ins_del_preserved():
    """修订 w:ins / w:del 包装的段落不再静默丢失。"""
    inner = (
        _p("正文")
        + '<w:ins w:id="1"><w:p><w:r><w:t xml:space="preserve">新增段落</w:t></w:r></w:p></w:ins>'
        + '<w:del w:id="2"><w:p><w:r><w:t xml:space="preserve">删除段落</w:t></w:r></w:p></w:del>'
    )
    out = fp._parse_docx(_build_docx(_document(inner)))
    assert "正文" in out
    assert "新增段落" in out
    assert "删除段落" in out


def test_docx_custom_xml_container_preserved():
    """w:customXml 数据岛内正文不再静默丢失。"""
    inner = '<w:customXml><w:p><w:r><w:t xml:space="preserve">数据岛内容</w:t></w:r></w:p></w:customXml>'
    out = fp._parse_docx(_build_docx(_document(inner)))
    assert "数据岛内容" in out


def test_docx_transparent_container_env_switch_applies(monkeypatch):
    """PARSE_DOCX_PARTS=0 不影响透明容器下沉（正文级能力独立于部件开关）。"""
    monkeypatch.setattr(fp, "_PARSE_DOCX_PARTS", False)
    inner = '<mc:AlternateContent><mc:Choice Requires="w14">' + _p("现代内容") + "</mc:Choice></mc:AlternateContent>"
    out_off = fp._parse_docx(_build_docx(_document_mc(inner)))
    assert "现代内容" in out_off
