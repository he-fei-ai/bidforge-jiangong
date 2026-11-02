# -*- coding: utf-8 -*-
"""两轮次导出 · 第二轮检查器（只读，R147）。

五类检查（C1~C5），全部只读：输入 zip 部件字典 ``{name: bytes}``，
输出 :class:`RepairFinding` 列表。修复器按 ``finding.code`` 定向处理
（见 fixes.py）；每类检查在修复后**复跑**以验证收敛（pipeline.py）。

数量护栏：每个 code 最多上报 ``_MAX_PER_CODE`` 条 finding（超出聚合为
一条「另有 N 处」），防止异常初稿（全篇无缩进）把报告与 SSE 打爆。
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from app.services.docx_repair._xmlutil import (
    has_numpr,
    iter_paragraphs,
    numpr_numid,
    para_heading_level,
    para_ppr,
    para_style,
    para_text,
    ppr_child,
)
from app.services.docx_repair.model import SEV_ERROR, SEV_WARNING, RepairFinding

_MAX_PER_CODE = 200

#: 中文硬编号前缀（段首）：与自动编号共存即冲突（C2）。
_HARD_NUM_RE = re.compile(r"^\s*(第[一二三四五六七八九十百\d]+[章节、.]|[一二三四五六七八九十百]+、|\d+(?:\.\d+)*[.、]|\(\d+\)|（\d+）)")
#: 括号序号列表项：（1）/（2）/… 连续段序列（C2 中文列表序号检查）。
_PAREN_SEQ_RE = re.compile(r"^\s*[（(](\d{1,3})[)）]")
_ARABIC_DOT_SEQ_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){0,4})[.、]?\s")
#: 图题（C4）：图 {章}-{序} {图名}
_CAPTION_RE = re.compile(r"^\s*图\s*(\d{1,3})\s*[-–—]\s*(\d{1,3})\s*(.*)$")
#: 段内含图片（w:drawing）。
_DRAWING_RE = re.compile(r"<w:drawing(?:\s[^>]*)?>.*?</w:drawing>", re.S)
_EXTENT_RE = re.compile(
    r'<wp:extent\s+cx="(\d+)"\s+cy="(\d+)"\s*/>')
_BLIP_RE = re.compile(r'<a:blip[^>]*\sr:(?:embed|link)="([^"]+)"')
_DOC_REL_RE = re.compile(r'\sr:(?:embed|id|link)="([^"]+)"')


def _add(out: list[RepairFinding], bucket: dict[str, int], finding: RepairFinding) -> None:
    n = bucket.get(finding.code, 0)
    if n >= _MAX_PER_CODE:
        return
    bucket[finding.code] = n + 1
    if n == _MAX_PER_CODE - 1:
        out.append(RepairFinding(
            category=finding.category, code=finding.code, severity=SEV_WARNING,
            location="全文", message=f"「{finding.code}」同类问题较多，仅上报前 {_MAX_PER_CODE} 条",
            meta={}))
        return
    out.append(finding)


# ---------------------------------------------------------------------------
# C1 目录编号与层级
# ---------------------------------------------------------------------------

#: 大纲标题最大级别：Heading6/7（真实稿中的「1）、」「a、」列表项伪标题）不参与
#: 编号计算 —— 它们既不算层级跳变，也不进入编号重排（R149）。
OUTLINE_MAX_LEVEL = 5

def inspect_numbering(document_xml: str, numbering_xml: str | None,
                      expected_outline: list[int] | None = None
                      ) -> list[RepairFinding]:
    """C1：层级跳变 / 与目录树不一致 / 多级列表错绑 / 字面编号虚跳。"""
    out: list[RepairFinding] = []
    bucket: dict[str, int] = {}

    valid_num_ids: set[str] = set()
    if numbering_xml:
        valid_num_ids = set(re.findall(r'<w:num\s+(?:[^>]*\s)?w:numId="(\d+)"', numbering_xml))

    headings: list[tuple[int, int, str]] = []  # (para_seq, level, text) ≤5 级
    paras = iter_paragraphs(document_xml)
    for seq, (_s, _e, p) in enumerate(paras):
        lvl = para_heading_level(p)
        if not lvl or lvl > OUTLINE_MAX_LEVEL:
            continue
        text = para_text(p).strip()
        # 列表枚举伪标题（（1）/1）/a、/①…）：不参与大纲编号、跳变与目录比对
        if lvl >= 2 and _LIST_ENUM_RE.match(text):
            continue
        headings.append((seq, lvl, text))

    prev_lvl = 0
    for seq, lvl, _text in headings:
        if prev_lvl and lvl > prev_lvl + 1:
            _add(out, bucket, RepairFinding(
                category="numbering", code="num_level_jump", severity=SEV_ERROR,
                location=f"段落#{seq}", message=f"标题层级跳变 L{prev_lvl}→L{lvl}（不连续）",
                meta={"para_seq": seq, "level": lvl, "prev_level": prev_lvl}))
        prev_lvl = lvl

    if expected_outline is not None:
        actual = [lvl for _s, lvl, _t in headings if lvl <= OUTLINE_MAX_LEVEL]
        want = [int(x) for x in expected_outline
                if 1 <= int(x) <= OUTLINE_MAX_LEVEL]
        if actual != want:
            first_bad = next((i for i, (a, b) in enumerate(zip(actual, want)) if a != b),
                             min(len(actual), len(want)))
            _add(out, bucket, RepairFinding(
                category="numbering", code="num_outline_mismatch", severity=SEV_ERROR,
                location=f"第 {first_bad + 1} 个标题",
                message="标题层级序列与目录树不一致（"
                        f"文档 {'-'.join(map(str, actual)) or '空'} vs "
                        f"目录树 {'-'.join(map(str, want)) or '空'}）",
                meta={"actual": actual, "expected": want, "first_bad": first_bad}))

    for seq, (_s, _e, p) in enumerate(paras):
        if not has_numpr(p):
            continue
        nid = numpr_numid(p)
        if numbering_xml and nid and nid not in valid_num_ids:
            _add(out, bucket, RepairFinding(
                category="numbering", code="num_broken_binding", severity=SEV_ERROR,
                location=f"段落#{seq}",
                message=f"多级列表绑定悬空：w:numId={nid} 在 numbering.xml 中不存在",
                meta={"para_seq": seq, "num_id": nid}))

    plan = plan_heading_renumber(headings)
    for item in plan["renumber"]:
        _add(out, bucket, RepairFinding(
            category="numbering", code="num_sequence", severity=SEV_ERROR,
            location=f"段落#{item['para_seq']}",
            message=f"标题编号虚跳/错序：「{item['old_token'].strip()}」应为「{item['new'].strip()}」",
            meta=dict(item)))

    # 编号方案无法识别的标题（Agent 辅助候选）
    for item in plan["unrecognized"]:
        _add(out, bucket, RepairFinding(
            category="numbering", code="num_scheme_unknown", severity=SEV_WARNING,
            location=f"段落#{item['para_seq']}",
            message=f"标题编号方案无法识别：「{item['text'][:40]}」",
            meta=dict(item)))
    return out


_CN_NUMS = "一二三四五六七八九十"
_CN_RE = re.compile(r"^[一二三四五六七八九十百]+$")

#: 列表枚举前缀（1）/（1）/a、/① …）：在 ≤5 级标题里出现说明是**列表项被误设
#: 为标题样式**，不参与大纲编号（R149；真实稿 Heading6 已由级别上限排除）。
_LIST_ENUM_RE = re.compile(
    r"^\s*(?:[（(]\d{1,3}[）)]|\d{1,3}[）)]|[a-zA-ZＡ-Ｚａ-ｚ][、.）)]|[①-⑳])")


def _cn_from_int(n: int) -> str:
    """1..99 → 中文数字（工程文件编号场景足够）。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_NUMS[n - 1]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_NUMS[n - 11]
    tens, ones = divmod(n, 10)
    return _CN_NUMS[tens - 1] + "十" + (_CN_NUMS[ones - 1] if ones else "")


def _int_from_cn(s: str) -> int:
    if s.isdigit():
        return int(s)
    table = {c: i + 1 for i, c in enumerate(_CN_NUMS)}
    if "十" not in s:
        return table.get(s, 0) or 0
    left, _, right = s.partition("十")
    tens = table.get(left, 1) if left else 1
    ones = table.get(right, 0) if right else 0
    return tens * 10 + ones


#: L1 编号方案（按文档内多数票识别）
_L1_SCHEMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dizhang", re.compile(r"^第([一二三四五六七八九十百\d]+)章[\s、.:：]?")),
    ("dun", re.compile(r"^([一二三四五六七八九十百\d]+)、\s*")),
    ("arabic", re.compile(r"^(\d{1,3})(?:[.、]\s*|\s+)")),
)
_L1_SCHEME_KEYS: tuple[str, ...] = tuple(k for k, _p in _L1_SCHEMES)
_L1_SCHEMES_BY_KEY: dict[str, re.Pattern[str]] = dict(_L1_SCHEMES)
#: L2+ 数字链前缀（1 / 1.1 / 1.2.1 …；允许尾随「、」或空白分隔）
_SUB_NUM_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,4})(?:[、.．]\s*|\s+)")


def parse_heading_number(text: str, level: int) -> tuple[str, list[int]] | None:
    """解析标题字面编号 → (scheme, 层级号列表)；不匹配返回 None。

    L1 用 ``_L1_SCHEMES``（第X章 / 一、 / 1）；L2+ 只认数字链，**深度由
    文档内多数票推断**（见 ``plan_heading_renumber``）——真实工程文件的
    L2 常写成「1」（单段），L3 才是「1.1」，不能假定 token 数 == 级别。
    """
    if level == 1:
        for scheme, pat in _L1_SCHEMES:
            m = pat.match(text)
            if m:
                raw = m.group(1)
                n = int(raw) if raw.isdigit() else _int_from_cn(raw)
                return scheme, [n]
        return None
    m = _SUB_NUM_RE.match(text)
    if m:
        return "arabic", [int(x) for x in m.group(1).split(".")]
    return None


def gen_heading_number(scheme: str, counters: list[int]) -> str:
    """按方案生成编号前缀（counters 为 L1..Lcurrent 计数）。"""
    if scheme == "dizhang" and len(counters) == 1:
        return f"第{_cn_from_int(counters[0])}章 "
    if scheme == "dun" and len(counters) == 1:
        return f"{_cn_from_int(counters[0])}、"
    return ".".join(str(x) for x in counters)


def _infer_depths(heads: list[tuple[int, int, str]],
                  parsed: dict[int, tuple[str, list[int]] | None]) -> dict[int, int]:
    """各标题级别的编号深度（token 数）按文档内多数票推断。

    真实稿实测两种形态并存：L2 用「1」（深度 1）、L2 用「1.1」（深度 2）。
    固定 ``token 数 == 级别`` 会把前者整篇误判为编号错序（200 处假阳性）。
    """
    votes: dict[int, dict[int, int]] = {}
    for seq, lvl, text in heads:
        if lvl < 2:
            continue
        pr = parsed.get(seq)
        if not pr:
            continue
        n = len(pr[1])
        if 1 <= n <= lvl:  # 深度不可能超过级别
            d = votes.setdefault(lvl, {})
            d[n] = d.get(n, 0) + 1
    depths: dict[int, int] = {}
    for lvl in range(2, OUTLINE_MAX_LEVEL + 1):
        d = votes.get(lvl) or {}
        depths[lvl] = max(d, key=lambda k: d[k]) if d else max(1, lvl - 1)
    return depths


def plan_heading_renumber(headings: list[tuple[int, int, str]],
                          scheme: str | None = None) -> dict[str, list]:
    """计算标题字面编号重排计划（不改文本，供检查与修复共用）。

    返回 ``{"renumber": [...], "unrecognized": [...], "level_jumps": [...],
    "scheme": str|None, "depths": {...}}``。重排逐条进行（父编号变则后代
    前缀随之改写），**不再全有或全无**：不可识别项只遗留自身，其余照常
    收敛（R149）。renumber 条目：``{para_seq, level, old_token, new,
    old_token_len}``，old_token/new 均为数字链/编号前缀（不含分隔空白）。
    """
    heads = [(s, l, t) for s, l, t in headings
             if 1 <= int(l) <= OUTLINE_MAX_LEVEL]
    parsed: dict[int, tuple[str, list[int]] | None] = {}
    l1_votes: dict[str, int] = {}
    for seq, lvl, text in heads:
        pr = parse_heading_number(text, lvl)
        parsed[seq] = pr
        if pr and lvl == 1:
            l1_votes[pr[0]] = l1_votes.get(pr[0], 0) + 1
    if scheme not in _L1_SCHEME_KEYS:
        scheme = max(l1_votes, key=lambda k: l1_votes[k]) if l1_votes else None
    depths = _infer_depths(heads, parsed)

    renumber: list[dict] = []
    unrecognized: list[dict] = []
    level_jumps: list[dict] = []
    counters: dict[int, int] = {}
    prev_lvl = 0
    for seq, lvl, text in heads:
        pr = parsed.get(seq)
        # 列表枚举伪标题（1）/（1）/a、/①…：不参与大纲编号，也不产生 finding
        if lvl >= 2 and _LIST_ENUM_RE.match(text):
            continue
        if prev_lvl and lvl > prev_lvl + 1:
            # 结构性层级跳变：不能凭空造中间标题 → 只遗留该标题自身，
            # 计数器仍推进一级，保证后续同/上级标题的编号继续可算。
            level_jumps.append({"para_seq": seq, "level": lvl,
                                "prev_level": prev_lvl})
            prev_lvl = lvl
            counters[lvl] = counters.get(lvl, 0) + 1
            for k in range(lvl + 1, OUTLINE_MAX_LEVEL + 1):
                counters[k] = 0
            continue
        prev_lvl = lvl
        counters[lvl] = counters.get(lvl, 0) + 1
        for k in range(lvl + 1, OUTLINE_MAX_LEVEL + 1):
            counters[k] = 0
        if pr is None:
            unrecognized.append({"para_seq": seq, "level": lvl, "text": text})
            continue
        if lvl == 1:
            old_scheme, old_toks = pr
            if old_toks[0] != counters[1]:
                m = _L1_SCHEMES_BY_KEY[old_scheme].match(text)
                old_token = m.group(0) if m else ""
                new_num = gen_heading_number(old_scheme, [counters[1]])
                if old_token and old_token.strip() != new_num.strip():
                    renumber.append({
                        "para_seq": seq, "level": lvl,
                        "old_token": old_token.strip(), "new": new_num,
                        "old_token_len": len(old_token.strip())})
            continue
        depth = depths.get(lvl) or max(1, lvl - 1)
        start = max(1, lvl - depth + 1)
        expected = [counters.get(k, 0) for k in range(start, lvl + 1)]
        if any(x <= 0 for x in expected):
            # 中间层缺计数（如跳变后无父标题）：无法可靠重排，跳过本项
            continue
        actual = pr[1]
        new_num = ".".join(str(x) for x in expected)
        old_token = ".".join(str(x) for x in actual)
        if actual != expected:
            renumber.append({
                "para_seq": seq, "level": lvl,
                "old_token": old_token, "new": new_num,
                "old_token_len": len(old_token)})
    return {"renumber": renumber, "unrecognized": unrecognized,
            "level_jumps": level_jumps, "scheme": scheme, "depths": depths}


# ---------------------------------------------------------------------------
# C2 正文排版
# ---------------------------------------------------------------------------

def _style_props(styles_xml: str | None) -> dict[str, dict]:
    """解析 styles.xml → {styleId: {...}}；另记 ``__default__``（默认段落样式）。"""
    props: dict[str, dict] = {}
    if not styles_xml:
        return props
    for m in re.finditer(r'<w:style\s([^>]*)>(.*?)</w:style>', styles_xml, re.S):
        attrs, body = m.group(1), m.group(2)
        sid_m = re.search(r'w:styleId="([^"]+)"', attrs)
        if not sid_m:
            continue
        sid = sid_m.group(1)
        bo = re.search(r'<w:basedOn\s+w:val="([^"]+)"', body)
        ind = re.search(r"<w:ind\s[^>]*/>", body)
        sp = re.search(r"<w:spacing\s[^>]*/>", body)
        jc = re.search(r'<w:jc\s+w:val="([^"]+)"', body)
        props[sid] = {
            "based_on": bo.group(1) if bo else None,
            "indent": bool(ind and re.search(
                r'w:firstLine(Char)?s?="\d+"|w:hanging="\d+"', ind.group(0))),
            "line": (lambda lm: int(lm.group(1)) if lm else None)(
                re.search(r'w:line="(\d+)"', sp.group(0)) if sp else None),
            "jc": jc.group(1) if jc else None,
            "_is_para_default": ('w:type="paragraph"' in attrs
                                 and 'w:default="1"' in attrs),
        }
    for sid, p in props.items():
        if p.pop("_is_para_default", False):
            props["__default__"] = sid
            break
    return props


def _style_chain(props: dict[str, dict], style_id: str | None,
                 depth: int = 0) -> dict:
    """沿 basedOn 链合并样式属性（段落级缺省时逐级回溯）。"""
    if depth > 8 or not style_id or style_id not in props:
        return {}
    p = props[style_id]
    parent = _style_chain(props, p.get("based_on"), depth + 1)
    return {
        "indent": p["indent"] or parent.get("indent", False),
        "line": p["line"] if p["line"] is not None else parent.get("line"),
        "jc": p["jc"] if p["jc"] is not None else parent.get("jc"),
    }


def inspect_paragraph(document_xml: str,
                      styles_xml: str | None = None) -> list[RepairFinding]:
    """C2：首行缩进 / 行距 / 对齐 / 中文硬编号与自动编号冲突。

    段落级缺省时回溯样式 basedOn 链（真实导出稿的缩进/行距定义在样式层，
    段落级 w:ind 缺省是**正常形态**，不回溯会海量误报）。
    """
    out: list[RepairFinding] = []
    bucket: dict[str, int] = {}
    sp = _style_props(styles_xml)
    paras = iter_paragraphs(document_xml)
    for seq, (_s, _e, p) in enumerate(paras):
        if para_heading_level(p):
            continue
        ppr = para_ppr(p)
        text = para_text(p)
        has_drawing = bool(_DRAWING_RE.search(p))
        is_caption = bool(re.match(r"^\s*[图表]\s*\d", text))
        is_list = has_numpr(p)
        is_body = (not has_drawing and not is_caption and not is_list
                   and len(text) >= 30)
        if not is_body:
            _check_hard_num_conflict(out, bucket, seq, p, text, is_list)
            continue
        style = para_style(p) or sp.get("__default__")
        sprop = _style_chain(sp, style)
        ind = ppr_child(ppr, "ind") if ppr else None
        para_ind = bool(ind and re.search(
            r'w:firstLine(Char)?s?="\d+"|w:hanging="\d+"', ind.group(0)))
        if not para_ind and not sprop.get("indent"):
            _add(out, bucket, RepairFinding(
                category="paragraph", code="para_no_indent", severity=SEV_ERROR,
                location=f"段落#{seq}",
                message=f"正文段缺少首行缩进（前 20 字：{text[:20]}…）",
                meta={"para_seq": seq}))
        lm = re.search(r'w:line="(\d+)"',
                       (ppr_child(ppr, "spacing").group(0) if ppr and ppr_child(ppr, "spacing") else ""))
        para_line = int(lm.group(1)) if lm else sprop.get("line")
        if para_line is None:
            _add(out, bucket, RepairFinding(
                category="paragraph", code="para_no_linespacing", severity=SEV_WARNING,
                location=f"段落#{seq}", message="正文段未设置行距",
                meta={"para_seq": seq}))
        elif para_line < 240:
            _add(out, bucket, RepairFinding(
                category="paragraph", code="para_line_too_tight", severity=SEV_ERROR,
                location=f"段落#{seq}",
                message=f"行距过密（w:line={para_line} < 240）",
                meta={"para_seq": seq, "line": para_line}))
        jcm = ppr_child(ppr, "jc") if ppr else None
        jc_val = (re.search(r'w:val="([^"]+)"', jcm.group(0)).group(1)
                  if jcm else sprop.get("jc"))
        if jc_val in ("right", "end", "distribute"):
            _add(out, bucket, RepairFinding(
                category="paragraph", code="para_bad_align", severity=SEV_ERROR,
                location=f"段落#{seq}",
                message=f"正文段对齐异常（jc={jc_val}）",
                meta={"para_seq": seq, "jc": jc_val}))
        _check_hard_num_conflict(out, bucket, seq, p, text, is_list)

    _check_paren_sequence(out, bucket, paras)
    return out


def _check_hard_num_conflict(out, bucket, seq, p, text, is_list) -> None:
    if is_list and _HARD_NUM_RE.match(text):
        _add(out, bucket, RepairFinding(
            category="paragraph", code="para_num_conflict", severity=SEV_ERROR,
            location=f"段落#{seq}",
            message="段落同时存在自动编号（w:numPr）与中文硬编号前缀",
            meta={"para_seq": seq}))


def _check_paren_sequence(out, bucket, paras) -> None:
    """连续的（N）中文列表段：序号必须 1..n 递增（虚跳/回退即报）。"""
    expect = 1
    in_run = False
    for seq, (_s, _e, p) in enumerate(paras):
        text = para_text(p)
        m = _PAREN_SEQ_RE.match(text)
        if m:
            n = int(m.group(1))
            if in_run and n != expect:
                _add(out, bucket, RepairFinding(
                    category="paragraph", code="para_seq_gap", severity=SEV_WARNING,
                    location=f"段落#{seq}",
                    message=f"中文列表序号不连续：期望（{expect}）实为（{n}）",
                    meta={"para_seq": seq, "expect": expect, "actual": n}))
            expect = n + 1
            in_run = True
        else:
            in_run = False
            expect = 1


# ---------------------------------------------------------------------------
# C3 字号字体
# ---------------------------------------------------------------------------

_RUN_RE = re.compile(r"<w:r(?:\s[^>]*)?>.*?</w:r>", re.S)
_RPR_RE = re.compile(r"<w:rPr>(.*?)</w:rPr>|<w:rPr\s*/>", re.S)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
#: 被误写进西文槽（w:ascii/w:hAnsi）的中文字体名：数字/英文会跟着宋体走。
_CJK_FONT_RE = re.compile(
    r"宋体|黑体|仿宋|楷体|微软雅黑|等线|华文中宋|方正|隶书|幼圆")


def inspect_fonts(document_xml: str) -> list[RepairFinding]:
    """C3：中文字体缺失 / 字号非法 / 标题倾斜 / 中英字体混杂。"""
    out: list[RepairFinding] = []
    bucket: dict[str, int] = {}
    paras = iter_paragraphs(document_xml)
    for seq, (_s, _e, p) in enumerate(paras):
        lvl = para_heading_level(p)
        for rm in _RUN_RE.finditer(p):
            run = rm.group(0)
            rpr_m = _RPR_RE.search(run)
            rpr = rpr_m.group(0) if rpr_m else ""
            text = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", run, re.S))
            if not _CJK_RE.search(text) or not rpr:
                # 无 run 级 rPr：字体由样式层决定（不判，防样式级继承误报）
                continue
            fonts = re.search(r"<w:rFonts(?:\s[^>]*)?/?>", rpr)
            if not fonts or "w:eastAsia=" not in fonts.group(0):
                _add(out, bucket, RepairFinding(
                    category="fonts", code="font_no_eastasia", severity=SEV_ERROR,
                    location=f"段落#{seq}",
                    message=f"中文 run 缺少 eastAsia 字体声明（文本：{text[:12]}）",
                    meta={"para_seq": seq}))
            if fonts:
                latin = re.search(r'w:(?:ascii|hAnsi)="([^"]+)"', fonts.group(0))
                if latin and _CJK_FONT_RE.search(latin.group(1)):
                    _add(out, bucket, RepairFinding(
                        category="fonts", code="font_ascii_cjk", severity=SEV_ERROR,
                        location=f"段落#{seq}",
                        message=(f"中英字体混杂：西文槽写成中文字体"
                                 f"「{latin.group(1)}」（文本：{text[:12]}）"),
                        meta={"para_seq": seq, "font": latin.group(1)}))
            sz = re.search(r'<w:sz\s+w:val="(\d+)"', rpr) if rpr else None
            if sz and int(sz.group(1)) % 2 == 1:
                _add(out, bucket, RepairFinding(
                    category="fonts", code="font_sz_odd", severity=SEV_ERROR,
                    location=f"段落#{seq}",
                    message=f"字号半点值非法（w:sz={sz.group(1)}）",
                    meta={"para_seq": seq, "sz": int(sz.group(1))}))
            if lvl and re.search(r"<w:i\s*/>|<w:iCs\s*/>", rpr):
                _add(out, bucket, RepairFinding(
                    category="fonts", code="heading_italic", severity=SEV_ERROR,
                    location=f"段落#{seq}",
                    message=f"标题 run 含倾斜属性（文本：{text[:12]}）",
                    meta={"para_seq": seq}))
    return out


# ---------------------------------------------------------------------------
# C4 图表渲染
# ---------------------------------------------------------------------------

_EMU_PER_TWIP = 635


def _page_geometry(document_xml: str) -> tuple[int, int]:
    """版心可用 (宽, 高) EMU（取末个 sectPr；缺省 A4 默认边距）。"""
    sect = None
    for m in re.finditer(r"<w:sectPr(?:\s[^>]*)?>.*?</w:sectPr>", document_xml, re.S):
        sect = m.group(0)
    w, h, ml, mr, mt, mb = 11906, 16838, 1800, 1800, 1800, 1800
    if sect:
        sz = re.search(r'<w:pgSz\s+w:w="(\d+)"\s+w:h="(\d+)"', sect)
        if sz:
            w, h = int(sz.group(1)), int(sz.group(2))
        mar = re.search(r"<w:pgMar\s+([^>]*?)/>", sect)
        if mar:
            attrs = mar.group(1)
            ml = int(re.search(r'w:left="(\d+)"', attrs).group(1)) \
                if re.search(r'w:left="(\d+)"', attrs) else ml
            mr = int(re.search(r'w:right="(\d+)"', attrs).group(1)) \
                if re.search(r'w:right="(\d+)"', attrs) else mr
            mt = int(re.search(r'w:top="(\d+)"', attrs).group(1)) \
                if re.search(r'w:top="(\d+)"', attrs) else mt
            mb = int(re.search(r'w:bottom="(\d+)"', attrs).group(1)) \
                if re.search(r'w:bottom="(\d+)"', attrs) else mb
    return ((w - ml - mr) * _EMU_PER_TWIP, (h - mt - mb) * _EMU_PER_TWIP)


def inspect_figures(document_xml: str, parts: dict[str, bytes]) -> list[RepairFinding]:
    """C4：图片损坏 / 居中 / 超页 / 图题格式 / 图号虚跳 / 图题缺失。"""
    out: list[RepairFinding] = []
    bucket: dict[str, int] = {}
    avail_w, avail_h = _page_geometry(document_xml)

    media_names = [n for n in parts if n.startswith("word/media/")]
    try:
        from PIL import Image as PILImage  # 惰性导入（与导出主链同惯例）
    except Exception:  # noqa: BLE001 —— 无 PIL 时不判解码（fail-soft，防环境误报）
        PILImage = None
    for name in media_names:
        data = parts[name]
        if len(data) < 32:
            _add(out, bucket, RepairFinding(
                category="figures", code="fig_broken_media", severity=SEV_ERROR,
                location=name, message=f"图片部件过小（{len(data)} bytes），疑似损坏",
                meta={"media": name, "size": len(data)}))
            continue
        if PILImage is not None:
            try:
                with PILImage.open(io.BytesIO(data)) as im:
                    im.verify()
            except Exception:
                _add(out, bucket, RepairFinding(
                    category="figures", code="fig_broken_media", severity=SEV_ERROR,
                    location=name, message="图片部件无法解码（PIL verify 失败）",
                    meta={"media": name, "size": len(data)}))

    paras = iter_paragraphs(document_xml)
    fig_seqs: dict[int, list[tuple[int, int, str]]] = {}
    for seq, (_s, _e, p) in enumerate(paras):
        dm = _DRAWING_RE.search(p)
        if dm:
            ppr = para_ppr(p)
            jc = ppr_child(ppr, "jc") if ppr else None
            jv = re.search(r'w:val="([^"]+)"', jc.group(0)) if jc else None
            if not (jv and jv.group(1) == "center"):
                _add(out, bucket, RepairFinding(
                    category="figures", code="fig_not_centered", severity=SEV_ERROR,
                    location=f"段落#{seq}", message="图片段落未居中",
                    meta={"para_seq": seq}))
            em = _EXTENT_RE.search(dm.group(0))
            if em:
                cx, cy = int(em.group(1)), int(em.group(2))
                if cx > avail_w or cy > avail_h:
                    _add(out, bucket, RepairFinding(
                        category="figures", code="fig_oversize", severity=SEV_ERROR,
                        location=f"段落#{seq}",
                        message=f"图片超出版心（{cx}×{cy} EMU > 可用 {avail_w}×{avail_h}）",
                        meta={"para_seq": seq, "cx": cx, "cy": cy,
                              "avail_w": avail_w, "avail_h": avail_h}))
            # 图题缺失：下一段不是图题
            nxt = paras[seq + 1][2] if seq + 1 < len(paras) else ""
            if not _CAPTION_RE.match(para_text(nxt)):
                _add(out, bucket, RepairFinding(
                    category="figures", code="fig_no_caption", severity=SEV_WARNING,
                    location=f"段落#{seq}", message="图片后未紧跟图题段",
                    meta={"para_seq": seq}))

        text = para_text(p)
        cm = _CAPTION_RE.match(text)
        if cm:
            ch, no, title = int(cm.group(1)), int(cm.group(2)), cm.group(3).strip()
            canonical = f"图 {ch}-{no}" + (f" {title}" if title else "")
            if text.strip() != canonical:
                _add(out, bucket, RepairFinding(
                    category="figures", code="fig_caption_format", severity=SEV_ERROR,
                    location=f"段落#{seq}",
                    message=f"图题格式不规范：「{text.strip()[:24]}」"
                            f"应为「{canonical[:24]}」（图{{章号}}-{{序号}} {{图名}}）",
                    meta={"para_seq": seq, "chapter": ch, "seq_no": no,
                          "title": title}))
            if not title:
                _add(out, bucket, RepairFinding(
                    category="figures", code="fig_caption_no_title", severity=SEV_ERROR,
                    location=f"段落#{seq}", message=f"图题缺少图名（{text.strip()[:20]}）",
                    meta={"para_seq": seq, "chapter": ch, "seq_no": no}))
            fig_seqs.setdefault(ch, []).append((seq, no, text))

    for ch, items in sorted(fig_seqs.items()):
        expected = 1
        for seq, no, _text in items:
            if no != expected:
                _add(out, bucket, RepairFinding(
                    category="figures", code="fig_seq_gap", severity=SEV_ERROR,
                    location=f"段落#{seq}",
                    message=f"第 {ch} 章图号虚跳：期望 图{ch}-{expected}，实际 图{ch}-{no}",
                    meta={"para_seq": seq, "chapter": ch, "expect": expected,
                          "actual": no}))
            expected = no + 1
    return out


# ---------------------------------------------------------------------------
# C5 兼容性安全
# ---------------------------------------------------------------------------

def _rels_map(rels_xml: str) -> dict[str, dict[str, str]]:
    """Id -> {type, target, external}。"""
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"<Relationship\s+([^>]*?)/?>", rels_xml):
        attrs = m.group(1)
        rid = re.search(r'Id="([^"]+)"', attrs)
        tgt = re.search(r'Target="([^"]+)"', attrs)
        if not rid:
            continue
        out[rid.group(1)] = {
            "target": tgt.group(1) if tgt else "",
            "external": 'TargetMode="External"' in attrs,
        }
    return out


def inspect_compatibility(parts: dict[str, bytes]) -> list[RepairFinding]:
    """C5：2010+ 专有扩展残留 / 断裂关系文件 / Content-Types 缺口。"""
    out: list[RepairFinding] = []
    bucket: dict[str, int] = {}
    from app.services.docx_namespace import _BANNED_ATTR_RE, _BANNED_PREFIXES, _OPEN_TAG_RE

    for name, data in parts.items():
        if not re.match(r"^word/(?:document|styles|settings|numbering|fontTable"
                        r"|header\d*|footer\d*|footnotes|endnotes)\.xml$", name):
            continue
        xml = data.decode("utf-8", errors="replace")
        m = _OPEN_TAG_RE.search(xml)
        if m:
            for attr in re.findall(r'xmlns:([A-Za-z0-9]+)="', m.group(2)):
                if attr in _BANNED_PREFIXES:
                    _add(out, bucket, RepairFinding(
                        category="compatibility", code="compat_ns_decl",
                        severity=SEV_ERROR, location=name,
                        message=f"根元素声明专有命名空间 xmlns:{attr}",
                        meta={"part": name, "prefix": attr}))
        if _BANNED_ATTR_RE.search(xml):
            _add(out, bucket, RepairFinding(
                category="compatibility", code="compat_w14_attr",
                severity=SEV_ERROR, location=name,
                message="存在 w14/wp14/w15 注释性属性（paraId 等）",
                meta={"part": name}))

    doc_xml = parts.get("word/document.xml", b"").decode("utf-8", errors="replace")
    rels_xml = parts.get("word/_rels/document.xml.rels", b"").decode(
        "utf-8", errors="replace")
    rels = _rels_map(rels_xml) if rels_xml else {}
    for rid in dict.fromkeys(_DOC_REL_RE.findall(doc_xml)):
        if rels and rid not in rels:
            _add(out, bucket, RepairFinding(
                category="compatibility", code="compat_missing_rel",
                severity=SEV_ERROR, location=f"rId={rid}",
                message=f"document.xml 引用 rId={rid}，但 .rels 中不存在该关系",
                meta={"rid": rid}))
    for rid, info in rels.items():
        if info["external"] or not info["target"]:
            continue
        raw = info["target"].replace("\\", "/").lstrip("/")
        target = raw if raw.startswith("word/") else "word/" + raw
        # 规范化相对段（如 Target="../customXml/item1.xml"）
        parts_path = re.sub(r"(?<=/)\./|(?<=^)\./", "", target)
        while True:
            m2 = re.search(r"[^/]+/\.\./", parts_path)
            if not m2:
                break
            parts_path = parts_path[:m2.start()] + parts_path[m2.end():]
        if parts_path not in parts:
            _add(out, bucket, RepairFinding(
                category="compatibility", code="compat_broken_rel_target",
                severity=SEV_ERROR, location=f"rId={rid}",
                message=f"关系目标部件缺失：{parts_path}",
                meta={"rid": rid, "target": parts_path}))

    ct = parts.get("[Content_Types].xml", b"").decode("utf-8", errors="replace")
    for name in parts:
        if not name.startswith("word/media/"):
            continue
        ext = name.rsplit(".", 1)[-1].lower()
        if ext and f'Extension="{ext}"' not in ct:
            _add(out, bucket, RepairFinding(
                category="compatibility", code="compat_missing_content_type",
                severity=SEV_ERROR, location="[Content_Types].xml",
                message=f"媒体扩展名 {ext} 缺少 Default 声明",
                meta={"ext": ext}))
    return out


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------

def inspect_parts(parts: dict[str, bytes], *,
                  expected_outline: list[int] | None = None) -> list[RepairFinding]:
    """对部件字典跑 C1~C5（文件级命名空间回潮校验由 pipeline 落盘后兜底）。"""
    doc = parts.get("word/document.xml", b"").decode("utf-8", errors="replace")
    numbering = parts.get("word/numbering.xml")
    numbering_xml = numbering.decode("utf-8", errors="replace") if numbering else None
    styles = parts.get("word/styles.xml")
    styles_xml = styles.decode("utf-8", errors="replace") if styles else None
    findings: list[RepairFinding] = []
    findings += inspect_numbering(doc, numbering_xml, expected_outline)
    findings += inspect_paragraph(doc, styles_xml)
    findings += inspect_fonts(doc)
    findings += inspect_figures(doc, parts)
    findings += inspect_compatibility(parts)
    return findings


def group_findings(findings: list[RepairFinding]) -> dict[str, list[RepairFinding]]:
    g: dict[str, list[RepairFinding]] = {}
    for f in findings:
        g.setdefault(f.category, []).append(f)
    return g


def zip_to_parts(path: str | Any) -> dict[str, bytes]:
    """DOCX → {name: bytes}（全部部件；顺序保持原 zip 次序）。"""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            out[name] = z.read(name)
    return out
