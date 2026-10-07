"""AI 生成正文的落库清洗与质量审计（商业化交付标准）

解决的问题
----------
1. **口语化/AI 腔残留**：大模型输出常带"说白了""咱们""搞定""需要注意的是""希望对你有帮助"
   等口语与客套表述，与专项方案的技术文件属性不符，交付前必须清除。
2. **AI 身份披露与空话套话**：如"作为人工智能…""以上是本章内容"，不得出现在正式文档中。
3. **过期标准引用**：生成后扫描，命中已废止编号即返回告警项，供前端质量面板标记。

设计约束
--------
- 清洗只作用于 **AI 生成的正文**，不改动用户手工编辑内容（调用方控制）。
- 清洗**跳过 ``` 与 ~~~ 围栏内的图表代码块**（Mermaid / chart-json / ai_image），
  避免破坏图表语法。两种围栏均与正文生成 / 登记 / 导出三侧的唯一围栏口径
  （content_utils._FENCE_LINE_RE）保持一致 —— 模型可能输出 ``~~~mermaid`` 围栏，
  旧实现只识别 ``` 围栏，会把 ~~~ 围栏里的图表代码当散文清洗（"咱们"→"施工项目部"
  等），直接破坏成稿图表。
- 规则均为"保守替换"：不改变技术含义，不删减数据，不做整句重写。

使用方式：
    from app.services.content_polish import sanitize_ai_content, quality_issues
    content = sanitize_ai_content(raw_content)
"""
from __future__ import annotations

import re

from app.services.standards_registry import find_abolished_codes

# ---------------------------------------------------------------------------
# 一、口语化 / 宣传腔 → 规范技术用语
# ---------------------------------------------------------------------------
# 说明：按"先长后短"排列，避免短规则抢先命中导致长规则失效。
COLLOQUIAL_RULES: list[tuple[str, str]] = [
    # 语气与总结
    (r"一句话概括(一下)?[,，]?", "概括而言，"),
    (r"总的来说[,，]?", "综上，"),
    (r"总的来讲[,，]?", "综上，"),
    (r"总而言之[,，]?", "综上，"),
    (r"说白了[,，]?", "即"),
    (r"换个话说[,，]?", "换言之，"),
    (r"其实呢?[,，]?", ""),
    (r"那么[,，]我们", "因此，"),
    (r"^那么[,，]", ""),
    # 口语动词
    (r"咱们", "施工项目部"),
    (r"搞定", "完成"),
    (r"弄好", "完成"),
    (r"搞好", "落实"),
    (r"狠抓", "强化"),
    (r"差不多", "基本一致"),
    (r"一大堆", "大量"),
    (r"很多很多", "大量"),
    (r"挺好", "良好"),
    (r"特别特别", "尤为"),
    # 仅替换口语化的"估计"，排除造价专业词（估算/概算/预算）与统计术语
    # （估计量/估计值/估计误差）：后顾排除造价词词尾（前面是 估算/概算/预算），
    # 前向排除统计术语后缀。
    (r"(?<!估算)(?<!概算)(?<!预算)估计(?![算量值误差])", "预计"),
    (r"也许", "可能"),
    (r"可能吧", "可能"),
    # 提示语书面化
    (r"需要注意的是[,，]?", "应注意，"),
    (r"值得注意的是[,，]?", "应予重视的是，"),
    (r"我们要(注意|做好|加强)", r"应\1"),
    (r"大家(要|应)", "作业人员应"),
    (r"请大家", "作业人员"),
    (r"各位(领导|同事)[,，]?", ""),
    # 主观表述 → 客观表述
    (r"我(们)?认为", "经分析"),
    (r"个人(认为|建议)", "建议"),
    # 夸张修辞
    (r"完美(地|的)?", "有效"),
    (r"极大(地)?", "显著"),
    (r"非常(大|高|好)", r"较\1"),
    # "做好…工作" 套话
    (r"做好([^，。；：\s]{2,8})工作", r"落实\1要求"),
]

# ---------------------------------------------------------------------------
# 二、AI 身份披露与客套话（整句删除）
# ---------------------------------------------------------------------------
AI_TONE_RULES: list[tuple[str, str]] = [
    (r"^\s*好的[，。！]?\s*", ""),
    (r"作为(一个)?(AI|人工智能|语言模型|助手)[^。！？\n]{0,30}[，。！？]?", ""),
    (r"我(只)?是一个(AI|人工智能助手|语言模型)[^。！？\n]{0,30}[，。！？]?", ""),
    (r"[^。！？\n]{0,15}由\s*AI\s*(生成|撰写)[^。！？\n]{0,15}[。！？]?", ""),
    (r"希望(以上|上述)?(内容|回答)?(对您|对你)?(有所|有)?帮助[。！？]?", ""),
    (r"如有(任何)?疑问[，,]?请?(随时)?(提问|联系|告知)[。！？]?", ""),
    (r"以上(就)?是(本章|本节|该章节)?(的)?(全部)?内容[。！？]?", ""),
    (r"感谢(您|你)的?(阅读|使用)[。！？]?", ""),
]

# 句末语气助词（位于句读标点之后，口语残留）
_TAIL_PARTICLE_RE = re.compile(r"(?<=[。！？])[哦啦呀嘛呗呢喽]+(?=[，。！？\s]|$)")

# 围栏代码块（Mermaid / chart-json / ai_image）保护：反引号与波浪号两种围栏
# ✅ 2026-10-03 修复（正文生成·图表清洗口径分叉）：旧正则只认 ``` 围栏，
#    模型输出的 ~~~mermaid 围栏会被当普通文本清洗，破坏成稿图表语法。
#    两种围栏同口径受保护（与 sse_handlers / chart_pipeline / export 三侧一致）。
_FENCE_SPLIT_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")
# GFM 表格行（行首可选空白 + |）：表格单元格内的专业数据/造价词
# （估算/概算/预算/估计值等）是结构化数据，清洗规则不得改写。
# 兼容 CRLF（split("\n") 后行尾残留 \r）。
_TABLE_LINE_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*\r?$")


def _table_ranges(text: str) -> list[tuple[int, int]]:
    """收集连续 GFM 表格行的字符区间（已在围栏代码块内的行不重复计入）。

    表格按"连续表格行"合并为一个区间（含表头分隔行），避免跨行命中绕过。
    基于 \\n 切分、把 \\r 留在行内，故对 CRLF / LF 行尾同样正确。
    """
    ranges: list[tuple[int, int]] = []
    lines = text.split("\n")
    offset = 0
    tbl_start = -1
    for line in lines:
        line_len = len(line) + 1  # +1 为 \n（末行多算无害：区间仅用于包含判断）
        if _TABLE_LINE_RE.match(line):
            if tbl_start < 0:
                tbl_start = offset
            tbl_end = offset + len(line)
        else:
            if tbl_start >= 0:
                ranges.append((tbl_start, tbl_end))
                tbl_start = -1
        offset += line_len
    if tbl_start >= 0:
        ranges.append((tbl_start, tbl_end))

    if not _FENCE_SPLIT_RE.search(text):
        return ranges
    # 排除落在围栏代码块内的区间（代码块里以 | 开头的行不是 Markdown 表格）
    fences = [(m.start(), m.end()) for m in _FENCE_SPLIT_RE.finditer(text)]
    return [(s, e) for s, e in ranges
            if not any(s >= fs and e <= fe for fs, fe in fences)]


def _protected_ranges(text: str, *, include_tables: bool = True) -> list[tuple[int, int]]:
    """全部受保护区间：围栏代码块 + GFM 表格（合并排序、不重叠）。

    ``include_tables=False`` 时只保护围栏代码块 —— 供「编号类审计」使用：
    代码块（chart-json / mermaid）里的编号是图表数据，正文并未引用；
    而表格单元格里的编号是交付件本身的内容（编制依据表等），仍需审计。
    """
    ranges = [(m.start(), m.end()) for m in _FENCE_SPLIT_RE.finditer(text)]
    if include_tables:
        ranges.extend(_table_ranges(text))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _apply_rules_outside_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    """仅对保护区间之外的普通文本应用清洗，保护区间逐字保留（原位拼接，
    不改变任何空白/换行）。"""
    if not ranges:
        return _apply_rules(text)
    out: list[str] = []
    cursor = 0
    for s, e in ranges:
        if s > cursor:
            out.append(_apply_rules(text[cursor:s]))
        out.append(text[s:e])
        cursor = e
    if cursor < len(text):
        out.append(_apply_rules(text[cursor:]))
    return "".join(out)

_COMPILED_COLLOQUIAL: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p), r) for p, r in COLLOQUIAL_RULES]
_COMPILED_AI_TONE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.MULTILINE), r) for p, r in AI_TONE_RULES]




def _apply_rules(segment: str) -> str:
    """对非代码文本片段依次应用清洗规则。"""
    text = segment
    for pattern, repl in _COMPILED_AI_TONE:
        text = pattern.sub(repl, text)
    for pattern, repl in _COMPILED_COLLOQUIAL:
        text = pattern.sub(repl, text)
    text = _TAIL_PARTICLE_RE.sub("", text)

    # 清理因删除产生的异常空白（不动换行结构）
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+(?=[\n])", "", text)
    text = re.sub(r"^[,，、；;]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def sanitize_ai_content(text: str) -> str:
    """清洗 AI 生成正文：消除口语化、AI 腔与客套话，保留图表代码块原样。

    Args:
        text: AI 原始生成的章节正文（Markdown）。

    Returns:
        清洗后的正文。输入为空或纯空白时原样返回。
    """
    if not text or not text.strip():
        return text

    has_protected = bool(_FENCE_SPLIT_RE.search(text)) or any(
        _TABLE_LINE_RE.match(ln) for ln in text.split("\n"))
    if not has_protected:
        return _apply_rules(text)

    return _apply_rules_outside_ranges(text, _protected_ranges(text))


def _strip_fences(text: str, *, include_tables: bool = True) -> str:
    """剔除围栏代码块与表格（其中的英文拼音/注释/专业数据会被误判口语化）。

    ``include_tables=False``：只剔除围栏代码块、保留表格行（编号类审计用）。
    """
    has_fence = bool(_FENCE_SPLIT_RE.search(text))
    has_table = any(_TABLE_LINE_RE.match(ln) for ln in text.split("\n"))
    if not has_fence and (not include_tables or not has_table):
        return text
    ranges = _protected_ranges(text, include_tables=include_tables)
    out: list[str] = []
    cursor = 0
    for s, e in ranges:
        out.append(text[cursor:s])
        cursor = e
    out.append(text[cursor:])
    return "".join(out)


def find_colloquial_hits(text: str) -> list[str]:
    """返回文本中仍存在的口语化/AI 腔命中项（供质量审计展示）。

    ✅ 审计只对围栏外文本执行：旧实现对全文（含 mermaid/chart-json 代码块）
    跑正则，代码注释里的普通词汇会产生误报。
    """
    if not text:
        return []
    scan = _strip_fences(text)
    hits: list[str] = []

    def _first_effective(pattern: re.Pattern[str], repl: str, strip: bool) -> str:
        # 含边界断言的规则：search 命中不代表该点替换生效（"估算"中的"估计"
        # 被断言否决，但同段后面有效的"估计"仍会被 sub 替换），逐命中点验证。
        for m in pattern.finditer(scan):
            replaced = scan[:m.start()] + m.expand(repl) + scan[m.end():]
            if replaced != scan:
                return m.group(0).strip() if strip else m.group(0)
        return ""

    for pattern, repl in _COMPILED_COLLOQUIAL:
        if h := _first_effective(pattern, repl, False):
            hits.append(h)
    for pattern, repl in _COMPILED_AI_TONE:
        if h := _first_effective(pattern, repl, True):
            hits.append(h)
    return [h for h in hits if h]


def quality_issues(text: str) -> dict:
    """生成后质量审计（轻量、纯函数）。

    Returns:
        {
          "colloquial_hits": [...],   # 口语化/AI 腔残留（清洗后应为空）
          "abolished_standards": [...]  # 命中的已废止标准编号
        }
    """
    # ✅ 2026-10-06 修复（正文生成·质量审计口径分叉）：废止标准扫描此前直接
    #    扫**原文**，而同文件的口语化判据走 _strip_fences —— 两条判据对同一份
    #    正文的保护范围不一致，违背本模块设计约束第 2 条「跳过围栏内的图表
    #    代码块」。实测后果：AI 生成的 ```chart-json 对比图（common 形态是
    #    「规范版本对比」，data 里必然出现被替代的旧编号）与 ~~~mermaid 围栏
    #    会让章节被误报「引用了已废止标准」，而该编号在正文里根本不存在 ——
    #    用户按告警逐段排查却找不到，质量面板失去可信度。该告警随 section_done
    #    与手工保存两条路径下发到前端日志区，是用户可见的假告警。
    #    现与口语化判据共用 _strip_fences 单一出口；include_tables=False 让
    #    **表格单元格里的编号仍可审计**（编制依据表引用废止编号是交付件缺陷，
    #    不能被豁免），只豁免代码块这一种「正文并未引用」的形态。
    return {
        "colloquial_hits": find_colloquial_hits(text),
        "abolished_standards": find_abolished_codes(
            _strip_fences(text or "", include_tables=False)),
    }
