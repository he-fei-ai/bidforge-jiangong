# -*- coding: utf-8 -*-
"""R122: 扫描所有已导出 docx，定位 WPS「文字程序意外中断」的结构性诱因。

检查项（按 WPS/Word 对 OOXML 的真实约束）：
  C1  ZIP 完整性 / 必需部件存在（[Content_Types].xml, document.xml, rels）
  C2  所有 XML 部件 well-formed + 无 XML 非法控制字符
  C3  mc:Ignorable 前缀必须在根元素已声明命名空间中
  C4  OMML 结构：m:e/m:sub/m:sup/m:num/m:den/m:deg 空内容、m:chr 空或非单字符
  C5  w:tbl 网格：gridSpan > gridCol 数 / gridCol 数 0 / 一行 cell 数 > 网格列数
  C6  关系完整性：r:embed/r:id 引用的 rId 必须在 .rels 中存在且 Target 部件存在
"""
import sys
import zipfile
from pathlib import Path
from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
REQUIRED = ("[Content_Types].xml", "word/document.xml",
            "word/_rels/document.xml.rels")

# XML 1.0 合法字符：#x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
def _illegal_chars(data: bytes):
    bad = set()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b < 0x20 and b not in (0x09, 0x0A, 0x0D):
            bad.add(b)
            i += 1
        elif b < 0x80:
            i += 1
        elif b < 0xC0:
            i += 1  # stray continuation
        elif b < 0xE0:
            i += 2
        elif b < 0xF0:
            i += 3
        else:
            i += 4
    return bad


def scan(path: Path) -> list:
    issues = []
    name = path.name
    # C1 zip
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                issues.append(f"C1 zip 损坏成员: {bad}")
            names = set(zf.namelist())
            for req in REQUIRED:
                if req not in names:
                    issues.append(f"C1 缺部件: {req}")
            parts = {}
            for nm in names:
                if nm.endswith((".xml", ".rels")):
                    try:
                        parts[nm] = zf.read(nm)
                    except Exception as e:  # noqa
                        issues.append(f"C1 读取失败 {nm}: {e}")
    except zipfile.BadZipFile as e:
        return [f"C1 非 zip 文件: {e}"]

    # C2 well-formed + 非法字符
    trees = {}
    for nm, data in parts.items():
        bad = _illegal_chars(data)
        if bad:
            issues.append(f"C2 {nm} 含非法控制字符: {sorted(hex(b) for b in bad)}")
        try:
            trees[nm] = etree.fromstring(data)
        except etree.XMLSyntaxError as e:
            issues.append(f"C2 {nm} XML 语法错误: {e}")

    # C3 mc:Ignorable
    for nm, root in trees.items():
        ig = root.get(f"{{{NS['mc']}}}Ignorable")
        if ig:
            declared = set(root.nsmap.values())
            want = {f"http://schemas.openxmlformats.org/wordprocessingml/2006/{s}"
                    for s in ig.split() if s.startswith("w")}
            want |= {f"http://schemas.microsoft.com/office/word/{s}"
                     for s in ig.split() if s.startswith("w")}
            declared_uris = set(root.nsmap.values())
            for s in ig.split():
                uri = root.nsmap.get(s)
                if uri is None:
                    issues.append(f"C3 {nm} Ignorable 前缀未声明: {s}")
            # 显式核对已知 wps/office 前缀
            for s, uri in root.nsmap.items():
                if s and s.startswith("w") and "schemas" not in uri and "microsoft" not in uri:
                    pass  # 自定义 w 前缀仅记录

    # C4 OMML
    doc = trees.get("word/document.xml")
    if doc is not None:
        M = NS["m"]
        for om in doc.iter(f"{{{M}}}oMath", f"{{{M}}}oMathPara"):
            if not om.xpath("descendant::m:*", namespaces=NS):
                issues.append(f"C4 空 oMathPara/oMath 节点")
        for tag in ("e", "sub", "sup", "num", "den", "deg"):
            for el in doc.iter(f"{{{M}}}{tag}"):
                if len(el) == 0 or not el.xpath("string(.)").strip():
                    parent = el.getparent()
                    # ✅ R126（2026-10-21）：Word 原生「无界 nary」形态白名单 ——
                    #   Word 对 `\sum` 类无上下限 nary 的标准输出是
                    #   naryPr 带 subHide/supHide + 自闭合 `<m:sub/>`/`<m:sup/>`，
                    #   空被和式则为 `<m:e><m:r><m:t/></m:r></m:e>`（空参 run）。
                    #   该形态 schema 合法、WPS 大规模实机兼容，不算崩溃违规；
                    #   其余空 num/den/deg 及非 nary 空 e/sub/sup 仍然拦下。
                    if tag in ("sub", "sup") and parent is not None \
                            and parent.tag == f"{{{M}}}nary":
                        pr = parent.find(f"{{{M}}}naryPr")
                        if pr is not None and pr.find(
                                f"{{{M}}}{tag}Hide") is not None:
                            continue
                    if tag == "e" and parent is not None \
                            and parent.tag == f"{{{M}}}nary" and len(el) >= 1:
                        continue  # nary 空被和式的规范空参 run 形态
                    issues.append(f"C4 m:{tag} 空内容")
        for el in doc.iter(f"{{{M}}}chr"):
            v = el.get(f"{{{M}}}val")
            if not v or len(v) != 1:
                issues.append(f"C4 m:chr val 非单字符: {v!r}")
        for el in doc.iter(f"{{{M}}}nary"):
            e = el.find(f"{{{M}}}e")
            if e is None or len(e) == 0:
                issues.append("C4 m:nary 缺/空 m:e")

    # C5 表格网格
    if doc is not None:
        for ti, tbl in enumerate(doc.iter(f"{{{NS['w']}}}tbl")):
            grid = tbl.find(f"{{{NS['w']}}}tblGrid")
            ncols = len(grid.findall(f"{{{NS['w']}}}gridCol")) if grid is not None else 0
            if ncols == 0:
                issues.append(f"C5 表{ti} gridCol=0")
                continue
            for ri, tr in enumerate(tbl.findall(f"{{{NS['w']}}}tr")):
                cells = tr.findall(f"{{{NS['w']}}}tc")
                if len(cells) > ncols:
                    issues.append(f"C5 表{ti} 行{ri} cell 数 {len(cells)} > 网格 {ncols}")
                span_sum = 0
                for tc in cells:
                    tcPr = tc.find(f"{{{NS['w']}}}tcPr")
                    if tcPr is not None:
                        gs = tcPr.find(f"{{{NS['w']}}}gridSpan")
                        if gs is not None:
                            try:
                                span_sum += int(gs.get(f"{{{NS['w']}}}val"))
                            except (TypeError, ValueError):
                                issues.append(f"C5 表{ti} 行{ri} gridSpan 非整数")
                        else:
                            span_sum += 1
                    else:
                        span_sum += 1
                if span_sum > ncols:
                    issues.append(f"C5 表{ti} 行{ri} gridSpan 合计 {span_sum} > 网格 {ncols}")

    # C6 关系完整性
    rels_name = "word/_rels/document.xml.rels"
    if rels_name in trees:
        rels = trees[rels_name]
        ids = {rel.get("Id") for rel in rels}
        for rel in rels:
            tgt = rel.get("Target") or ""
            if rel.get("TargetMode") == "External":
                continue
            part = "word/" + tgt.lstrip("/")
            if tgt.startswith(".."):
                continue
            if part not in parts and part not in names:
                issues.append(f"C6 rels Target 缺部件: {tgt}")
        if doc is not None:
            used = set()
            for el in doc.iter():
                for attr, val in el.attrib.items():
                    if attr.startswith(f"{{{NS['r']}}}") and val not in (None,):
                        used.add(val)
            for rid in sorted(used):
                if rid not in ids:
                    issues.append(f"C6 document.xml 引用未定义 rId: {rid}")
    return issues


def main():
    d = Path(r"j:\编程\专项方案工具箱\backend\data\_exports")
    files = sorted(d.glob("*.docx"), key=lambda p: p.stat().st_mtime, reverse=True)
    total_bad = 0
    for f in files:
        iss = scan(f)
        if iss:
            total_bad += len(iss)
            print(f"\n### {f.name}  ({f.stat().st_size} bytes, mtime={f.stat().st_mtime})")
            for i in iss[:40]:
                print(f"   - {i}")
            if len(iss) > 40:
                print(f"   ... 共 {len(iss)} 条")
        else:
            print(f"OK  {f.name}")
    print(f"\n==== 合计问题条目: {total_bad} / 文件 {len(files)} 个 ====")


if __name__ == "__main__":
    main()
