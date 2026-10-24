# -*- coding: utf-8 -*-
"""测试辅助：按 Word 规则还原 DOCX 列表编号的可见文本。

R127 起列表前缀不再写入 run，而由 numbering.xml 渲染。断言列表编号的测试
需要把「lvlText + startOverride + 计数器」还原成 Word 实际显示文本。
"""
from app.routers.export import _cn_ordinal
from docx.oxml.ns import qn


def numbering_maps(document):
    """读取 numbering.xml：abstractNum 的 lvl 定义 + num 的 startOverride。"""
    root = document.part.numbering_part.element
    abstracts = {}
    for a in root.findall(qn("w:abstractNum")):
        aid = a.get(qn("w:abstractNumId"))
        lvls = {}
        for lvl in a.findall(qn("w:lvl")):
            i = int(lvl.get(qn("w:ilvl")))
            text_el = lvl.find(qn("w:lvlText"))
            fmt_el = lvl.find(qn("w:numFmt"))
            suff_el = lvl.find(qn("w:suff"))
            lvls[i] = (
                fmt_el.get(qn("w:val")) if fmt_el is not None else "decimal",
                text_el.get(qn("w:val")) if text_el is not None else "",
                suff_el.get(qn("w:val")) if suff_el is not None else "tab",
            )
        abstracts[aid] = lvls
    nums = {}
    for n in root.findall(qn("w:num")):
        nid = n.get(qn("w:numId"))
        aid = n.find(qn("w:abstractNumId")).get(qn("w:val"))
        overrides = {}
        for o in n.findall(qn("w:lvlOverride")):
            lv = int(o.get(qn("w:ilvl")))
            so = o.find(qn("w:startOverride"))
            if so is not None:
                overrides[lv] = int(so.get(qn("w:val")))
        nums[nid] = (aid, overrides)
    return abstracts, nums


def display_texts(document) -> list[str]:
    """返回「编号 + 正文」的可见文本（等价于 Word 的列表渲染结果）。"""
    abstracts, nums = numbering_maps(document)
    counters: dict[str, list[int]] = {}
    initialized: set[str] = set()
    out = []
    for p in document.paragraphs:
        num_pr = p._p.pPr.numPr if p._p.pPr is not None else None
        if num_pr is None:
            out.append(p.text)
            continue
        nid = num_pr.numId.val
        ilvl = int(num_pr.ilvl.val)
        aid, overrides = nums[str(nid)]
        c = counters.setdefault(str(nid), [0] * 9)
        if str(nid) not in initialized:
            for lv, sv in overrides.items():
                c[lv] = sv - 1
            initialized.add(str(nid))
        c[ilvl] += 1
        for lv in range(ilvl + 1, 9):
            c[lv] = 0
        fmt, text, suff = abstracts[aid][ilvl]
        label = text
        for lv in range(9):
            value = (_cn_ordinal(c[lv]) if fmt == "chineseCounting"
                     else str(c[lv]))
            label = label.replace(f"%{lv + 1}", value)
        if suff == "space" and not label.endswith(" "):
            label += " "
        out.append(label + p.text)
    return out
