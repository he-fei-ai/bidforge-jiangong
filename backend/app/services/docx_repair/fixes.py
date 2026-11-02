# -*- coding: utf-8 -*-
"""两轮次导出 · 第二轮修复器（程序精确修改，R147）。

与检查器（inspector.py）按 finding.code 对偶。全部函数的原语是
**部件字典** ``{name: bytes}``：读取 → 字符串层改写 → 写回字典。
每个 fix 返回 ``(changed: bool, actions, unresolved)``；复杂排版错乱
（编号方案无法识别等）**不在此强行修改**，标 unresolved 交由
pipeline 决定是否走 Agent 辅助（agent_fix.py）。

修复原则（用户指令 §三）：
- 能用程序精确修改的直接改；每处修改留 RepairAction；
- 修改一律按 CT_* 严格序列插入（_xmlutil），不做 python-docx 往返。
"""
from __future__ import annotations

import re
import zipfile
from xml.sax.saxutils import escape

import app.services.docx_repair._xmlutil as _xu
from app.services.docx_repair._xmlutil import (
    ensure_ppr,
    insert_ppr_child,
    insert_rpr_child,
    iter_paragraphs,
    para_heading_level,
    para_ppr,
    para_text,
    ppr_child,
)
from app.services.docx_repair.inspector import (
    _CAPTION_RE,
    _CJK_FONT_RE,
    _DRAWING_RE,
    _EXTENT_RE,
    plan_heading_renumber,
)
from app.services.docx_repair.model import RepairAction, RepairFinding

PART_DOCUMENT = "word/document.xml"
PART_RELS = "word/_rels/document.xml.rels"
PART_CONTENT_TYPES = "[Content_Types].xml"

#: 中文硬编号前缀（剥离用；含尾随分隔空白）
_HARD_NUM_STRIP_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百\d]+[章节][\s、.:：]*|[一二三四五六七八九十百]+、\s*"
    r"|\d+(?:\.\d+)*[.、]\s*|\(\d+\)\s*|（\d+）\s*)")


def _document(parts: dict[str, bytes]) -> str:
    return parts.get(PART_DOCUMENT, b"").decode("utf-8", errors="replace")


def _set_document(parts: dict[str, bytes], xml: str) -> None:
    parts[PART_DOCUMENT] = xml.encode("utf-8")


def _para_spans(xml: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in _xu._PARA_RE.finditer(xml)]


def _numbering_levels(numbering_xml: str) -> dict[str, set[int]]:
    """numbering.xml → {numId: {已定义 ilvl}}（多级列表重绑定的匹配依据）。"""
    abs_levels: dict[str, set[int]] = {}
    for am in re.finditer(r"<w:abstractNum(?:\s[^>]*)?>.*?</w:abstractNum>",
                          numbering_xml, re.S):
        aid = re.search(r'w:abstractNumId="(\d+)"', am.group(0))
        if aid:
            abs_levels[aid.group(1)] = {
                int(x) for x in re.findall(r'<w:lvl\s+w:ilvl="(\d+)"', am.group(0))}
    out: dict[str, set[int]] = {}
    for nm in re.finditer(r"<w:num(?:\s[^>]*)?>.*?</w:num>", numbering_xml, re.S):
        nid = re.search(r'w:numId="(\d+)"', nm.group(0))
        aid = re.search(r'<w:abstractNumId\s+w:val="(\d+)"', nm.group(0))
        if nid and aid:
            out[nid.group(1)] = abs_levels.get(aid.group(1), set())
    return out


def _numpr_ilvl(p_xml: str) -> int | None:
    ppr = para_ppr(p_xml)
    if not ppr:
        return None
    m = re.search(r"<w:numPr(?:\s[^>]*)?>.*?</w:numPr>", ppr, re.S)
    if not m:
        return None
    im = re.search(r'<w:ilvl\s+w:val="(\d+)"', m.group(0))
    return int(im.group(1)) if im else None


# ---------------------------------------------------------------------------
# 文本替换原语
# ---------------------------------------------------------------------------

def _replace_text_token(p_xml: str, old_token: str, new_token: str
                        ) -> tuple[str, int]:
    """在段落的 w:t 文本流中替换首个 old_token（按**反转义后**文本匹配）。

    - 单 w:t 完整命中 → 直接替换；
    - 跨 w:t → 归并全文进首个非空 w:t（保留其 rPr，其余 w:t 清空）。
    写入侧一律 escape，防止图名/标题含 & < > 破坏 XML。
    返回 (新 p_xml, 替换次数 0/1)。
    """
    if not old_token:
        return p_xml, 0
    ts = list(re.finditer(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p_xml, re.S))
    if not ts:
        return p_xml, 0
    # 快路径：任一 w:t（原始转义态）包含 old_token 或其转义形态
    for m in ts:
        txt = m.group(1)
        for cand_old, cand_new in (
                (old_token, escape(new_token)),
                (escape(old_token), escape(new_token))):
            if cand_old and cand_old in txt:
                return (p_xml[:m.start(1)] + txt.replace(cand_old, cand_new, 1)
                        + p_xml[m.end(1):], 1)
    # 慢路径：跨 w:t —— 拼接反转义全文定位 token，重写进首个 w:t
    full = "".join(_xu.unescape_xml_text(t) for t in
                   re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p_xml, re.S))
    idx = full.find(old_token)
    if idx < 0:
        return p_xml, 0
    new_full = escape(full[:idx] + new_token + full[idx + len(old_token):])
    out = p_xml
    for m in reversed(ts[1:]):  # 其余 w:t 清空（用原偏移，倒序安全）
        out = out[:m.start(1)] + out[m.end(1):]
    m0 = ts[0]
    out = out[:m0.start(1)] + new_full + out[m0.end(1):]
    return out, 1


# ---------------------------------------------------------------------------
# C1 目录编号与层级
# ---------------------------------------------------------------------------

def fix_numbering(parts: dict[str, bytes], findings: list[RepairFinding],
                  ) -> tuple[bool, list[RepairAction], list[RepairFinding]]:
    """C1 修复：悬空 numPr 摘除 / outlineLvl 对齐 pStyle / 编号虚跳重排。

    编号重排的安全阀：文档内存在**任何**方案不可识别的标题时整体跳过
    （计数器无法对齐，强行重排会改错正确标题）→ 标 unresolved。
    """
    actions: list[RepairAction] = []
    unresolved: list[RepairFinding] = []
    xml = _document(parts)
    codes = {f.code for f in findings}
    changed = False

    # 1) 悬空 numPr 绑定 → 优先按 ilvl 重映射到同层级的有效 numId；
    #    无同层级可匹配时再摘除 w:numPr（保留字面文本，R149 多级列表策略）
    if "num_broken_binding" in codes:
        broken_ids = {str(f.meta.get("num_id")) for f in findings
                      if f.code == "num_broken_binding"}
        numbering_xml = parts.get("word/numbering.xml", b"").decode(
            "utf-8", errors="replace")
        levels = _numbering_levels(numbering_xml) if numbering_xml else {}
        n_removed = n_remapped = 0
        new_xml = xml
        for _s, _e, p in _para_spans(xml):
            nid = _xu.numpr_numid(p)
            if nid not in broken_ids:
                continue
            ppr = para_ppr(p)
            ilvl = _numpr_ilvl(p)
            candidates = sorted(
                (v for v, lv in levels.items()
                 if v not in broken_ids and ilvl is not None and ilvl in lv),
                key=lambda x: int(x))
            if candidates:
                new_ppr = re.sub(
                    r'(<w:numId\s+w:val=")' + re.escape(nid) + r'(")',
                    rf"\g<1>{candidates[0]}\g<2>", ppr, count=1)
                n_remapped += 1
            else:
                new_ppr = re.sub(
                    r"<w:numPr(?:\s[^>]*)?>.*?</w:numPr>|<w:numPr\s*/>", "",
                    ppr, flags=re.S)
                n_removed += 1
            new_p = p.replace(ppr, new_ppr, 1)
            new_xml = new_xml.replace(p, new_p, 1)
        if n_remapped or n_removed:
            xml = new_xml
            changed = True
            actions.append(RepairAction(
                category="numbering", code="num_broken_binding",
                location="document.xml",
                message=(f"多级列表重绑定：按 ilvl 重映射 {n_remapped} 个段落，"
                         f"摘除 {n_removed} 个无同层级可匹配的悬空绑定"),
                detail={"remapped": n_remapped, "removed": n_removed,
                        "num_ids": sorted(x for x in broken_ids if x != "None")}))

    # 2) outlineLvl 与 pStyle 级别对齐（层级跳变项：仅对齐元数据，不改结构）
    if "num_level_jump" in codes or "num_outline_mismatch" in codes:
        n_aligned = 0
        new_xml = xml
        for _s, _e, p in _para_spans(xml):
            lvl = para_heading_level(p)
            style = _xu.para_style(p) or ""
            m = re.match(r"^Heading([1-9])$", style)
            if not lvl or not m:
                continue
            want = int(m.group(1))
            ppr = para_ppr(p)
            om = ppr_child(ppr, "outlineLvl") if ppr else None
            if om:
                cur_m = re.search(r'w:val="(\d+)"', om.group(0))
                cur = int(cur_m.group(1)) + 1 if cur_m else None
                if cur == want:
                    continue
                new_ppr = re.sub(r'<w:outlineLvl\s+w:val="\d+"\s*/>',
                                 f'<w:outlineLvl w:val="{want - 1}"/>', ppr)
            else:
                new_ppr = insert_ppr_child(ppr, f'<w:outlineLvl w:val="{want - 1}"/>')
            new_p = p.replace(ppr, new_ppr, 1)
            new_xml = new_xml.replace(p, new_p, 1)
            n_aligned += 1
        if n_aligned:
            xml = new_xml
            changed = True
            actions.append(RepairAction(
                category="numbering", code="num_level_jump", location="document.xml",
                message=f"对齐 {n_aligned} 个标题的 outlineLvl 与标题样式层级",
                detail={"aligned": n_aligned}))

    # 3) 字面编号虚跳重排（逐链重排：父编号变则后代前缀随之改写；
    #    不可识别项只遗留自身，不再「有一颗坏标题就整篇放弃」）
    seq_findings = [f for f in findings if f.code == "num_sequence"]
    if seq_findings:
        headings: list[tuple[int, int, str]] = []
        for seq, (_s, _e, p) in enumerate(iter_paragraphs(xml)):
            lvl = para_heading_level(p)
            if lvl:
                headings.append((seq, lvl, para_text(p).strip()))
        plan = plan_heading_renumber(headings)
        n_fixed = 0
        fixed_seqs: set[int] = set()
        for item in plan["renumber"]:
            spans = _para_spans(xml)
            seq = item["para_seq"]
            if seq >= len(spans):
                continue
            s, _e, p = spans[seq]
            new_p, n = _replace_text_token(
                p, item["old_token"].strip(), item["new"].strip())
            if n:
                xml = xml[:s] + new_p + xml[s + len(p):]
                n_fixed += 1
                fixed_seqs.add(seq)
        if n_fixed:
            changed = True
            actions.append(RepairAction(
                category="numbering", code="num_sequence",
                location="document.xml",
                message=f"逐链重排 {n_fixed} 个标题的编号前缀"
                        f"（L1 方案：{plan['scheme'] or 'arabic'}；"
                        f"各级深度：{plan.get('depths')}）",
                detail={"fixed": n_fixed, "depths": plan.get("depths")}))
        remaining = [f for f in seq_findings
                     if int(f.meta.get("para_seq", -1)) not in fixed_seqs]
        if remaining:
            unresolved.extend(remaining)
            actions.append(RepairAction(
                category="numbering", code="num_sequence", location="document.xml",
                message=f"{len(remaining)} 处编号无法安全重排（定位失配/中间层缺计数）",
                result="unresolved"))

    if changed:
        _set_document(parts, xml)
    return changed, actions, unresolved


# ---------------------------------------------------------------------------
# C2 正文排版
# ---------------------------------------------------------------------------

_LINE_TARGET = 'w:line="360" w:lineRule="auto"'  # 1.5 倍行距（修复口径）


def fix_paragraph(parts: dict[str, bytes], findings: list[RepairFinding],
                  ) -> tuple[bool, list[RepairAction], list[RepairFinding]]:
    """C2 修复：补首行缩进 / 行距 / 对齐 / 剥离硬编号冲突。"""
    actions: list[RepairAction] = []
    unresolved: list[RepairFinding] = []
    xml = _document(parts)
    codes = {f.code for f in findings}
    changed = False
    stats: dict[str, int] = {}

    target_codes = ("para_no_indent", "para_no_linespacing", "para_line_too_tight",
                    "para_bad_align", "para_num_conflict")
    if codes & set(target_codes):
        seq_map: dict[str, set[int]] = {}
        for f in findings:
            if f.code in target_codes:
                seq_map.setdefault(f.code, set()).add(
                    int(f.meta.get("para_seq", -1)))
        targets = set().union(*seq_map.values()) - {-1}

        def transform(p: str, _seq: int) -> str | None:
            if para_heading_level(p):
                return None
            modified = False
            if _seq in seq_map.get("para_num_conflict", set()):
                text = para_text(p)
                hm = _HARD_NUM_STRIP_RE.match(text)
                if hm:
                    p2, n = _replace_text_token(p, hm.group(0), "")
                    if n:
                        p, modified = p2, True
                        stats["num_conflict"] = stats.get("num_conflict", 0) + 1
            if _seq in seq_map.get("para_no_indent", set()):
                p, _ = ensure_ppr(p)
                ppr = para_ppr(p)
                ind = ppr_child(ppr, "ind")
                if not ind or "w:firstLine" not in ind.group(0):
                    if ind:
                        new_ind = ind.group(0)
                        if "w:firstLineChars" not in new_ind:
                            new_ind = re.sub(r"<w:ind\s",
                                             '<w:ind w:firstLineChars="200" ',
                                             new_ind, count=1)
                        new_ppr = ppr.replace(ind.group(0), new_ind, 1)
                    else:
                        new_ppr = insert_ppr_child(
                            ppr, '<w:ind w:firstLineChars="200"/>')
                    p = p.replace(ppr, new_ppr, 1)
                    modified = True
                    stats["indent"] = stats.get("indent", 0) + 1
            if (_seq in seq_map.get("para_no_linespacing", set())
                    or _seq in seq_map.get("para_line_too_tight", set())):
                p, _ = ensure_ppr(p)
                ppr = para_ppr(p)
                sp = ppr_child(ppr, "spacing")
                new_sp = f"<w:spacing {_LINE_TARGET}/>"
                new_ppr = (ppr.replace(sp.group(0), new_sp, 1) if sp
                           else insert_ppr_child(ppr, new_sp))
                p = p.replace(ppr, new_ppr, 1)
                modified = True
                stats["spacing"] = stats.get("spacing", 0) + 1
            if _seq in seq_map.get("para_bad_align", set()):
                ppr = para_ppr(p)
                jc = ppr_child(ppr, "jc")
                if jc:
                    new_ppr = re.sub(r'<w:jc\s+w:val="[^"]+"\s*/>',
                                     '<w:jc w:val="both"/>', ppr)
                    p = p.replace(ppr, new_ppr, 1)
                    modified = True
                    stats["align"] = stats.get("align", 0) + 1
            return p if modified else None

        for seq in sorted(targets):
            spans = _para_spans(xml)
            if seq >= len(spans):
                continue
            s, e, p = spans[seq]
            new_p = transform(p, seq)
            if new_p is not None and new_p != p:
                xml = xml[:s] + new_p + xml[e:]
                changed = True
        if stats:
            det = "、".join(f"{k}×{v}" for k, v in sorted(stats.items()))
            actions.append(RepairAction(
                category="paragraph", code="para_format", location="document.xml",
                message=f"修正正文段排版（{det}）", detail=dict(stats)))

    if changed:
        _set_document(parts, xml)
    return changed, actions, unresolved


# ---------------------------------------------------------------------------
# C3 字号字体
# ---------------------------------------------------------------------------

_RPR_FULL_RE = re.compile(r"<w:rPr>.*?</w:rPr>|<w:rPr\s*/>", re.S)
_RUN_FULL_RE = re.compile(r"<w:r(?:\s[^>]*)?>.*?</w:r>", re.S)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


#: 导出标准西文字体（与 export.py FONT_LATIN 同口径）
_LATIN_FONT = "Times New Roman"


def _rebuild_rfonts(tag: str, latin: str, east: str) -> str:
    """重建 w:rFonts：显式写 ascii/hAnsi/eastAsia/cs，剔除主题字体属性。"""
    attrs = dict(re.findall(r'w:([A-Za-z0-9]+)="([^"]*)"', tag))
    for k in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        attrs.pop(k, None)
    attrs["ascii"] = latin
    attrs["hAnsi"] = latin
    attrs["eastAsia"] = east
    attrs["cs"] = latin
    return "<w:rFonts " + " ".join(f'w:{k}="{v}"' for k, v in attrs.items()) + "/>"


#: 不参与字体强制的 run（公式 / 域代码 / 图片 / 嵌入对象）
_SKIP_RUN_RE = re.compile(
    r"<m:oMath|<w:drawing|<w:pict|<w:object|<w:fldChar|<w:instrText|<m:t")


def fix_fonts(parts: dict[str, bytes], findings: list[RepairFinding],
              east_asia_font: str = "宋体",
              heading_font: str | None = None
              ) -> tuple[bool, list[RepairAction], list[RepairFinding]]:
    """C3 修复：按角色（正文/标题/图题）统一 rFonts 与 sz/szCs。

    角色契约（R149）：
    - 正文/图题：eastAsia = ``east_asia_font``；西文 = Times New Roman；
    - 标题：eastAsia = ``heading_font``（缺省同正文）；清除倾斜；
    - 公式（m:oMath）、域代码、图片/对象 run 一律跳过，不动其排版。
    """
    actions: list[RepairAction] = []
    unresolved: list[RepairFinding] = []
    xml = _document(parts)
    codes = {f.code for f in findings}
    changed = False
    stats: dict[str, int] = {}
    targets = {"font_no_eastasia", "font_sz_odd", "heading_italic",
               "font_ascii_cjk"}
    if not (codes & targets):
        return changed, actions, unresolved

    def _fix_run(run: str, is_heading: bool) -> str:
        rpr_m = _RPR_FULL_RE.search(run)
        rpr = rpr_m.group(0) if rpr_m else ""
        if not rpr or _SKIP_RUN_RE.search(run):
            return run
        east = (heading_font or east_asia_font) if is_heading else east_asia_font
        new_rpr = rpr
        fm = re.search(r"<w:rFonts(?:\s[^>]*)?/?>", new_rpr)
        if fm:
            new_rpr = new_rpr.replace(
                fm.group(0), _rebuild_rfonts(fm.group(0), _LATIN_FONT, east), 1)
            fstats = stats
            if "font_ascii_cjk" in codes and any(
                    _CJK_FONT_RE.search(v) for v in
                    re.findall(r'w:(?:ascii|hAnsi)="([^"]*)"', fm.group(0))):
                fstats["asciiCjk"] = fstats.get("asciiCjk", 0) + 1
            else:
                fstats["rFonts"] = fstats.get("rFonts", 0) + 1
        else:
            new_rpr = insert_rpr_child(
                new_rpr,
                f'<w:rFonts w:ascii="{_LATIN_FONT}" w:hAnsi="{_LATIN_FONT}" '
                f'w:eastAsia="{east}" w:cs="{_LATIN_FONT}"/>')
            stats["rFonts"] = stats.get("rFonts", 0) + 1
        # sz / szCs 对齐 + 半点值合法性（有其一即补齐另一）
        sm = re.search(r'<w:sz\s+w:val="(\d+)"\s*/>', new_rpr)
        scm = re.search(r'<w:szCs\s+w:val="(\d+)"\s*/>', new_rpr)
        size = None
        if sm:
            size = int(sm.group(1))
        elif scm:
            size = int(scm.group(1))
        if size is not None and size % 2 == 1:
            size += 1
            stats["sz"] = stats.get("sz", 0) + 1
        if size is not None and (sm or scm):
            if sm and sm.group(1) != str(size):
                new_rpr = new_rpr.replace(
                    sm.group(0), f'<w:sz w:val="{size}"/>', 1)
            if scm:
                if scm.group(1) != str(size):
                    new_rpr = new_rpr.replace(
                        scm.group(0), f'<w:szCs w:val="{size}"/>', 1)
            else:
                after = re.search(r'<w:sz\s+w:val="\d+"\s*/>', new_rpr)
                if after:
                    new_rpr = insert_rpr_child(
                        new_rpr, after.group(0).replace("w:sz ", "w:szCs "))
                    stats["szCs"] = stats.get("szCs", 0) + 1
        if is_heading:
            r2, k = re.subn(
                r'<w:i\s*/>|<w:iCs\s*/>|<w:i\s+w:val="(?:1|true)"\s*/>'
                r'|<w:iCs\s+w:val="(?:1|true)"\s*/>',
                "", new_rpr)
            if k:
                new_rpr = r2
                stats["italic"] = stats.get("italic", 0) + k
        if new_rpr == rpr:
            return run
        return run.replace(rpr, new_rpr, 1)

    paras = list(iter_paragraphs(xml))
    pieces: list[str] = []
    last = 0
    for s, e, p in paras:
        pieces.append(xml[last:s])
        is_heading = bool(para_heading_level(p))
        pieces.append(_RUN_FULL_RE.sub(
            lambda m, _h=is_heading: _fix_run(m.group(0), _h), p))
        last = e
    pieces.append(xml[last:])
    new_xml = "".join(pieces)
    if new_xml != xml:
        changed = True
        xml = new_xml
    if stats:
        det = "、".join(f"{k}×{v}" for k, v in sorted(stats.items()))
        actions.append(RepairAction(
            category="fonts", code="font_fix", location="document.xml",
            message=f"按角色统一字体字号（{det}）", detail=dict(stats)))

    if changed:
        _set_document(parts, xml)
    return changed, actions, unresolved


# ---------------------------------------------------------------------------
# C4 图表渲染
# ---------------------------------------------------------------------------

def fix_figures(parts: dict[str, bytes], findings: list[RepairFinding],
                media_sources: dict[str, bytes] | None = None,
                ) -> tuple[bool, list[RepairAction], list[RepairFinding]]:
    """C4 修复：替换损坏图 / 居中 / 版心缩放 / 图号重排 / 图题格式归一。"""
    actions: list[RepairAction] = []
    unresolved: list[RepairFinding] = []
    changed = False
    media_sources = media_sources or {}
    stats: dict[str, int] = {}

    # 1) 损坏图片：有源字节则替换部件；无源 → unresolved（调用方决定回滚）
    for f in [x for x in findings if x.code == "fig_broken_media"]:
        media = str(f.meta.get("media", ""))
        src = media_sources.get(media)
        if src:
            parts[media] = src
            changed = True
            stats["media_replaced"] = stats.get("media_replaced", 0) + 1
            actions.append(RepairAction(
                category="figures", code="fig_broken_media", location=media,
                message=f"以源字节替换损坏图片部件（{len(src)} bytes）",
                detail={"size": len(src)}))
        else:
            unresolved.append(f)
            actions.append(RepairAction(
                category="figures", code="fig_broken_media", location=media,
                message="图片损坏且无源字节可替换（需回滚初稿或人工处理）",
                result="unresolved"))

    xml = _document(parts)
    seqs_center = {int(f.meta.get("para_seq", -1))
                   for f in findings if f.code == "fig_not_centered"} - {-1}
    seqs_size = {int(f.meta.get("para_seq", -1))
                 for f in findings if f.code == "fig_oversize"} - {-1}

    for seq in sorted(seqs_center | seqs_size):
        spans = _para_spans(xml)
        if seq >= len(spans):
            continue
        s, e, p = spans[seq]
        new_p = p
        if seq in seqs_center:
            p, _ = ensure_ppr(p)
            ppr = para_ppr(p)
            jc = ppr_child(ppr, "jc")
            if not (jc and 'w:val="center"' in jc.group(0)):
                new_ppr = (ppr.replace(jc.group(0), '<w:jc w:val="center"/>', 1)
                           if jc else insert_ppr_child(ppr, '<w:jc w:val="center"/>'))
                new_p = p.replace(ppr, new_ppr, 1)
                stats["centered"] = stats.get("centered", 0) + 1
        if seq in seqs_size and _DRAWING_RE.search(new_p):
            fmeta = next((f.meta for f in findings if f.code == "fig_oversize"
                          and int(f.meta.get("para_seq", -1)) == seq), {})
            avail_w = float(fmeta.get("avail_w") or 0)
            avail_h = float(fmeta.get("avail_h") or 0)
            drawing = _DRAWING_RE.search(new_p).group(0)
            new_drawing = drawing
            for em in _EXTENT_RE.finditer(drawing):
                cx, cy = int(em.group(1)), int(em.group(2))
                scale = 1.0
                if avail_w and cx > avail_w:
                    scale = min(scale, avail_w / cx)
                if avail_h and cy > avail_h:
                    scale = min(scale, avail_h / cy)
                if scale < 1.0:
                    ncx, ncy = int(cx * scale), int(cy * scale)
                    new_drawing = new_drawing.replace(
                        em.group(0), f'<wp:extent cx="{ncx}" cy="{ncy}"/>', 1)
                    new_drawing = re.sub(
                        r'<a:ext\s+cx="\d+"\s+cy="\d+"/>',
                        f'<a:ext cx="{ncx}" cy="{ncy}"/>', new_drawing, count=1)
                    stats["resized"] = stats.get("resized", 0) + 1
            new_p = new_p.replace(drawing, new_drawing, 1)
        if new_p != p:
            xml = xml[:s] + new_p + xml[e:]
            changed = True

    # 2) 图号重排（同章按文档顺序重编 1..n，含虚跳/格式归一；图名原样保留）
    #    门控：存在虚跳或格式不规范才动手（正常编号的图题一个字节都不动）。
    #    ⚠️ 偏移纪律：先按序号收集图题段，再**逐次重新定位**改写 ——
    #    前一个图题补空格会改变后续偏移，一次快照连续拼接必然撕裂 XML
    #    （实测：caption_fmt+seq_gap 组合产出非法 QName）。
    if any(f.code in ("fig_seq_gap", "fig_caption_format") for f in findings):
        cap_paras: list[tuple[int, str]] = []
        for seq, (_s, _e, p) in enumerate(iter_paragraphs(xml)):
            text = para_text(p).strip()
            if _CAPTION_RE.match(text):
                cap_paras.append((seq, text))
        counters: dict[int, int] = {}
        n_renum = 0
        ref_map: dict[tuple[int, int], tuple[int, int]] = {}
        for seq, text in cap_paras:
            cm = _CAPTION_RE.match(text)
            if not cm:
                continue
            ch = int(cm.group(1))
            old_no = int(cm.group(2))
            counters[ch] = counters.get(ch, 0) + 1
            if (ch, old_no) != (ch, counters[ch]):
                ref_map[(ch, old_no)] = (ch, counters[ch])
            title = cm.group(3).strip()
            new_text = f"图 {ch}-{counters[ch]}" + (f" {title}" if title else "")
            if text == new_text:
                continue
            spans = _para_spans(xml)  # 逐次重定位（前序修改会移动偏移）
            if seq >= len(spans):
                continue
            s, _e, p = spans[seq]
            new_p, n = _replace_text_token(p, text, new_text)
            if n:
                xml = xml[:s] + new_p + xml[s + len(p):]
                n_renum += 1
        if n_renum:
            changed = True
            stats["renumbered"] = n_renum

        # 2b) 同步正文静态引用「见图 X-Y」→ 新图号（R149）
        if ref_map:
            n_ref = 0
            body_texts: list[tuple[int, str]] = []
            for seq, (_s, _e, p) in enumerate(iter_paragraphs(xml)):
                txt = para_text(p)
                if txt and not _CAPTION_RE.match(txt.strip()):
                    body_texts.append((seq, txt))
            for seq, txt in body_texts:
                for (och, ono), (nch, nno) in ref_map.items():
                    for old_tok in (f"图 {och}-{ono}", f"图{och}-{ono}"):
                        if old_tok not in txt:
                            continue
                        spans = _para_spans(xml)
                        if seq >= len(spans):
                            break
                        s, _e, p = spans[seq]
                        new_tok = (f"图 {nch}-{nno}" if old_tok.startswith("图 ")
                                   else f"图{nch}-{nno}")
                        new_p, n = _replace_text_token(p, old_tok, new_tok)
                        if n:
                            xml = xml[:s] + new_p + xml[s + len(p):]
                            txt = txt.replace(old_tok, new_tok)
                            n_ref += 1
            if n_ref:
                changed = True
                stats["ref_synced"] = n_ref

    # 3) 缺图名 / 缺图题：不强行造内容 → unresolved
    for f in [x for x in findings
              if x.code in ("fig_caption_no_title", "fig_no_caption")]:
        unresolved.append(f)
        actions.append(RepairAction(
            category="figures", code=f.code, location=f.location,
            message=f"{f.message}（不强行造内容，留待人工/Agent）",
            result="unresolved"))

    if stats:
        det = "、".join(f"{k}×{v}" for k, v in sorted(stats.items()))
        actions.append(RepairAction(
            category="figures", code="fig_fix", location="document.xml",
            message=f"图表修复（{det}）", detail=dict(stats)))

    if changed:
        _set_document(parts, xml)
    return changed, actions, unresolved


# ---------------------------------------------------------------------------
# C5 兼容性安全
# ---------------------------------------------------------------------------

_REL_MIME_DEFAULTS = {
    "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg",
    "gif": "image/gif", "bmp": "image/bmp", "tiff": "image/tiff",
    "tif": "image/tiff", "emf": "image/x-emf", "wmf": "image/x-wmf",
}


def fix_compatibility(parts: dict[str, bytes], findings: list[RepairFinding],
                      ) -> tuple[bool, list[RepairAction], list[RepairFinding]]:
    """C5 修复：补断裂关系 / 补 Content-Types Default（w14/w15 由
    docx_namespace.sanitize_docx_file 在 pipeline 落盘后统一清理）。"""
    actions: list[RepairAction] = []
    unresolved: list[RepairFinding] = []
    changed = False
    codes = {f.code for f in findings}

    if "compat_missing_rel" in codes:
        rels = parts.get(PART_RELS)
        dangling = [f for f in findings if f.code == "compat_missing_rel"]
        if rels:
            rels_xml = rels.decode("utf-8", errors="replace")
            media = [n for n in parts if n.startswith("word/media/")
                     and f'Target="media/{n.rsplit("/", 1)[-1]}"' not in rels_xml]
            added = 0
            for f in dangling:
                rid = str(f.meta.get("rid") or "")
                if not rid or not media:
                    break
                target = media.pop(0).rsplit("media/", 1)[-1]
                new_rel = (f'<Relationship Id="{rid}" Type="http://schemas.'
                           f'openxmlformats.org/officeDocument/2006/relationships/'
                           f'image" Target="media/{target}"/>')
                rels_xml = rels_xml.replace("</Relationships>",
                                            new_rel + "</Relationships>")
                added += 1
            if added:
                parts[PART_RELS] = rels_xml.encode("utf-8")
                changed = True
                actions.append(RepairAction(
                    category="compatibility", code="compat_missing_rel",
                    location=PART_RELS,
                    message=f"补建 {added} 条断裂关系（指向未登记媒体部件）",
                    detail={"added": added}))
            remaining = [f for f in dangling
                         if f'Id="{f.meta.get("rid")}"' not in rels_xml]
            unresolved.extend(remaining)
        else:
            unresolved.extend(dangling)

    if "compat_missing_content_type" in codes:
        ct = parts.get(PART_CONTENT_TYPES)
        if ct:
            ct_xml = ct.decode("utf-8", errors="replace")
            added = 0
            for f in [x for x in findings
                      if x.code == "compat_missing_content_type"]:
                ext = str(f.meta.get("ext") or "").lower()
                mime = _REL_MIME_DEFAULTS.get(ext)
                if mime and f'Extension="{ext}"' not in ct_xml:
                    ct_xml = ct_xml.replace(
                        "</Types>",
                        f'<Default Extension="{ext}" ContentType="{mime}"/></Types>')
                    added += 1
            if added:
                parts[PART_CONTENT_TYPES] = ct_xml.encode("utf-8")
                changed = True
                actions.append(RepairAction(
                    category="compatibility", code="compat_missing_content_type",
                    location=PART_CONTENT_TYPES,
                    message=f"补齐 {added} 条媒体扩展名 Default 声明",
                    detail={"added": added}))

    # 关系目标缺失：未被正文引用的悬空关系可直接删除（安全）；
    # 被正文引用的（删了会丢图）→ unresolved 交人工/回滚
    ghosts = [x for x in findings if x.code == "compat_broken_rel_target"]
    if ghosts:
        rels = parts.get(PART_RELS)
        doc_xml = parts.get(PART_DOCUMENT, b"").decode("utf-8", errors="replace")
        if rels:
            rels_xml = rels.decode("utf-8", errors="replace")
            n_removed = 0
            for f in ghosts:
                rid = str(f.meta.get("rid") or "")
                if rid and re.search(
                        rf'\sr:(?:embed|id|link)="{re.escape(rid)}"', doc_xml):
                    unresolved.append(f)
                    actions.append(RepairAction(
                        category="compatibility", code="compat_broken_rel_target",
                        location=str(f.meta.get("target") or "?"),
                        message="关系目标缺失且被正文引用（删关系会丢图，交人工处理）",
                        result="unresolved"))
                    continue
                new_rels = re.sub(
                    rf'<Relationship\s[^>]*\bId="{re.escape(rid)}"[^>]*/>', "",
                    rels_xml, count=1)
                if new_rels != rels_xml:
                    rels_xml = new_rels
                    n_removed += 1
            if n_removed:
                parts[PART_RELS] = rels_xml.encode("utf-8")
                changed = True
                actions.append(RepairAction(
                    category="compatibility", code="compat_broken_rel_target",
                    location=PART_RELS,
                    message=f"删除 {n_removed} 条未被正文引用的悬空关系",
                    detail={"removed": n_removed}))
        else:
            unresolved.extend(ghosts)

    return changed, actions, unresolved


# ---------------------------------------------------------------------------
# 写盘
# ---------------------------------------------------------------------------

def write_parts(path: str, parts: dict[str, bytes]) -> None:
    """部件字典 → DOCX（保持原部件顺序；原子替换）。"""
    import os
    import tempfile
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
                for name, data in parts.items():
                    z.writestr(name, data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
