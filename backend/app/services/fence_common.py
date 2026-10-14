"""代码围栏判据唯一事实源（R76 · 2026-10-13）。

## 为什么要有这个模块

「这一行是不是某个围栏的闭合行」这一条判据，此前在本仓有**三套语义、九处实现**：

| 语义 | 原实现 | 闭合判据 | 消费方 |
|---|---|---|---|
| 严格 CommonMark | ``content_utils.find_unclosed_fences`` | 同种字符 + 长度 ≥ 开围栏 + 信息串为空 | 导出预检 DLV-07、``review_autofix._match_fence``、``strip_fenced_code_blocks``、``fence_spans`` |
| 宽松 | ``content_utils._find_unclosed_fences_lenient``（R76 已删）、``content_blocks.read_fenced_block`` | **同种字符即可**、不排除带标签 | 正文落库补齐、图表登记/改写/导出渲染 |
| 前端 | ``MarkdownRenderer.tsx`` 正则 | 仅三反引号 + 非贪婪任意闭合 | 预览 |

三套语义同时作用于**同一份正文**的直接后果（R76 探针 ``_probe_fence_matrix_20261013.py`` 实测）：

- 「4 反引号开 + 3 反引号闭」：导出渲染正常、预检报「会吞噬后续正文」、自动修复答
  「未发现未闭合围栏」、字数口径报 **0 字**、AI 压缩禁区为空 —— 四方互不相认；
- 「```` ```js ```` 这类带信息串的行被当作闭合」：导出**提前闭合**并把后续正文吞进
  代码块，而前端 CommonMark 判为正文保留 —— 预览与导出反向分叉，且自动修复在文末
  追加闭合会把该缺陷固化成「事后检查通过」。

本模块把判据收敛为**一个谓词 + 两个扫描**，所有消费方（预检 / 自动修复 / 落库 /
图表登记 / 改写 / 导出渲染）改为同一实现；长度维度保留刻意容错（见
``is_fence_close`` 的 ``tolerant_length``），并由 ``normalize_fence_closes`` 在**写入前**
把长度错配补齐，使「容错口径」与「严格口径」在落库正文上恒等 —— 分歧从源头消除，
而不是靠各消费方各自兜底。

依赖方向：本模块是叶子（仅 ``re``），可被 ``content_utils`` / ``content_blocks`` /
``_chart_pipeline`` / ``preflight_engine`` 自由引用，不构成循环导入。
"""
from __future__ import annotations

import re

#: 独立成行的代码围栏标记：≤3 前导空白 + 同种字符（反引号或波浪号）≥3 个 + 可选信息串。
#: 逐字沿用原 ``content_utils._FENCE_LINE_RE`` / ``content_blocks._FENCE_LINE_RE``
#: （两处字面量本就相同，合并即"三份副本变一份"）。
FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^`~\n]*)$")


def parse_fence_line(line: str) -> tuple[str, int, str] | None:
    """解析一行是否为围栏行，返回 ``(围栏字符, 长度, 信息串(已 strip))``；否则 None。

    长度必须**原样返回**：改写代码块时若把开围栏统一写回 3 个反引号，4 反引号围栏
    会被降级 → 正文出现新的未闭合围栏（见 R49 幽灵图根因）。
    """
    m = FENCE_LINE_RE.match(line)
    if not m:
        return None
    marker = m.group(1)
    return marker[0], len(marker), m.group(2).strip()


def is_fence_close(line: str, open_char: str, open_len: int, *,
                   tolerant_length: bool = True) -> bool:
    """``line`` 是否为 ``(open_char, open_len)`` 围栏的**闭合行** —— 全仓唯一判据。

    判据（R76 定稿）：
      1. 必须是围栏行（独立成行、≤3 前导空白、≥3 个标记字符）；
      2. 围栏字符与开围栏**同种**（``` 不能闭合 ~~~）；
      3. 信息串**必须为空** —— ````` ```js ````` 是**内容**不是闭合。
         ✅ BUG 修复（R76 · BUG-F2）：旧实现只判 2，``read_fenced_block`` 会在带标签行
         提前闭合，使下一行裸 ``` 变成新开围栏、把其后的正文吞进代码块（实测
         ``state='eof'``），而前端 marked（CommonMark）判为正文保留 —— 预览与导出
         反向分叉；``auto_fix_unclosed_fences`` 还会在文末追加闭合，把缺陷固化为
         「事后检查通过」的静默落库。CommonMark §4.7 明确「闭合序列不得带 info string」，
         本条与前端对齐。
      4. 长度：``tolerant_length=True`` 时不要求 ≥ 开围栏（**刻意容错**，AI 高频输出
         「4 开 + 3 闭」错配，严格判未闭合会让整块图表被丢弃）；
         ``tolerant_length=False`` 时要求 ≥（CommonMark 严格口径，供预检报警、
         字数剔除、压缩禁区使用）。

    ⚠️ 两种口径的分歧不在**判定**而在**文本**：写入前由 ``normalize_fence_closes``
    把闭合行标记补齐到开围栏长度，归一后两种口径对同一份正文**结论恒等**。
    """
    pf = parse_fence_line(line)
    if pf is None:
        return False
    char, length, info = pf
    if char != open_char or info:
        return False
    return True if tolerant_length else length >= open_len


def find_unclosed(content: str, *, tolerant_length: bool = True) -> list[dict]:
    """扫描正文，返回所有未闭合围栏。

    返回项结构与历史实现逐字一致：
    ``{"line": 1-based 行号, "char": "`"|"~", "length": 标记长度,
       "lang": 小写信息串首词, "marker": 原文标记}``；无未闭合返回 ``[]``。

    线性扫描语义（不变）：一旦进入开围栏，未找到闭合即**停止**扫描
    （其后的行都在围栏内，不再是新围栏）。
    """
    if not content:
        return []
    lines = content.split("\n")
    unclosed: list[dict] = []
    i = 0
    n_lines = len(lines)
    while i < n_lines:
        m = FENCE_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        open_marker = m.group(1)
        rest = m.group(2).strip()
        ch = open_marker[0]
        n = len(open_marker)
        lang = rest.split()[0].lower() if rest else ""
        close_idx = -1
        j = i + 1
        while j < n_lines:
            if is_fence_close(lines[j], ch, n, tolerant_length=tolerant_length):
                close_idx = j
                break
            j += 1
        if close_idx < 0:
            unclosed.append({
                "line": i + 1, "char": ch, "length": n,
                "lang": lang, "marker": open_marker,
            })
            break  # 后面的行都在围栏内，不再是新围栏
        i = close_idx + 1
    return unclosed


def iter_closed_blocks(content: str) -> list[tuple[int, int, str, int, str]]:
    """返回所有**已闭合**围栏块 ``(open_line0, close_line0, char, open_len, lang)``。

    闭合口径 = ``tolerant_length=True``（渲染侧口径）。供
    ``normalize_fence_closes`` 定位需要补齐的闭合行。
    """
    if not content:
        return []
    lines = content.split("\n")
    blocks: list[tuple[int, int, str, int, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        m = FENCE_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        marker = m.group(1)
        rest = m.group(2).strip()
        ch = marker[0]
        open_len = len(marker)
        lang = rest.split()[0].lower() if rest else ""
        close_idx = -1
        j = i + 1
        while j < n:
            if is_fence_close(lines[j], ch, open_len, tolerant_length=True):
                close_idx = j
                break
            j += 1
        if close_idx < 0:
            break
        blocks.append((i, close_idx, ch, open_len, lang))
        i = close_idx + 1
    return blocks


def normalize_fence_closes(content: str) -> tuple[str, list[dict]]:
    """把「容错判为闭合、但标记长度不足开围栏」的闭合行补齐到开围栏长度。

    动机（R76 · BUG-F1 / BUG-F3 的源头治理）：AI 高频输出「4 反引号开 + 3 反引号闭」。
    渲染侧容错 → 图和正文都正常；但严格口径（前端 marked、预检 DLV-07、字数剔除、
    压缩禁区）判为**未闭合** → 预览把后续正文吞进代码块、预检假报警、字数报 0、
    AI 压缩对 Mermaid 源码失去保护。四处各自兜底已被证明修不完，唯一收敛办法是
    **在写入前把文本本身改成两种口径结论一致**的形态。

    性质（与 ``auto_fix_unclosed_fences`` 同一纪律）：
      · 只改闭合行的**标记长度**，不增删行、不改正文、不改围栏内内容；
      · 幂等：归一后再跑返回 ``(原样, [])``；
      · 确定性：结果只由输入决定；
      · 保守：仅当该行确实被容错口径当作闭合、且长度确实不足时才动；
      · 缩进与信息串原样保留（闭合行按判据必然信息串为空）。

    Returns:
        ``(normalized_content, log)``，log 项结构
        ``{"action": "normalize_close_length", "line": 1-based, "lang": ...,
           "char": "`"|"~", "length": 补齐后长度, "old_length": 原长度}``。
    """
    if not content:
        return content, []
    lines = content.split("\n")
    log: list[dict] = []
    for _open0, close0, ch, open_len, lang in iter_closed_blocks(content):
        pf = parse_fence_line(lines[close0])
        if pf is None:
            continue
        _c, cur_len, _info = pf
        if cur_len >= open_len:
            continue
        m = FENCE_LINE_RE.match(lines[close0])
        if m is None:
            continue
        indent = m.group(0)[:m.group(0).index(m.group(1))]
        lines[close0] = f"{indent}{ch * open_len}{m.group(2)}"
        log.append({
            "action": "normalize_close_length",
            "line": close0 + 1,
            "lang": lang,
            "char": ch,
            "length": open_len,
            "old_length": cur_len,
        })
    if not log:
        return content, []
    return "\n".join(lines), log


def iter_all_blocks(content: str, *,
                    tolerant_length: bool = True) -> list[dict]:
    """返回**所有**围栏块（含未闭合），按出现顺序。

    项结构 ``{"open_line": 0-based, "close_line": 0-based 或 -1, "char", "length",
    "lang"}``。遍历语义与 ``iter_closed_blocks`` / ``find_unclosed`` 完全相同（块内
    不再解析新围栏，首个未闭合处即停止 —— 其后所有行都是该块的代码内容），差别只在
    未闭合块**也**返回（``close_line = -1``）。

    供「哪一段文本属于代码围栏」的保护/剔除类调用方使用（AI 重写切分、检查点区间、
    清洗保护区），使它们与渲染侧共用同一份遍历，而不是各写一份状态机。
    """
    if not content:
        return []
    lines = content.split("\n")
    blocks: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        m = FENCE_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        marker = m.group(1)
        rest = m.group(2).strip()
        ch = marker[0]
        open_len = len(marker)
        lang = rest.split()[0].lower() if rest else ""
        close_idx = -1
        j = i + 1
        while j < n:
            if is_fence_close(lines[j], ch, open_len, tolerant_length=tolerant_length):
                close_idx = j
                break
            j += 1
        blocks.append({"open_line": i, "close_line": close_idx,
                       "char": ch, "length": open_len, "lang": lang})
        if close_idx < 0:
            break  # 未闭合：其后都在块内
        i = close_idx + 1
    return blocks


def fence_char_ranges(content: str, *,
                      tolerant_length: bool = True) -> list[tuple[int, int]]:
    """围栏块的字符区间 ``[(start, end)]``（``start`` = 开围栏行首，``end`` = 闭合行末，
    均为切片语义）；未闭合块 ``end`` 截到文本末尾（其后的行确实都是代码内容）。

    唯一出口动机（R76）：``content_rewrite._split_fences`` / ``content_checkpoint
    ._fence_ranges`` / ``content_polish._FENCE_SPLIT_RE`` 此前各写一份「这块围栏到哪
    结束」—— 三份都**不看信息串**，于是块内 ````` ```python ````` 会被当成收尾，
    其后正文滑出保护区被 AI 改写/清洗（与 ``read_fenced_block`` 的 BUG-F2 同族）；
    而未闭合围栏在旧的非贪婪正则下**整块不匹配**，保护区反而为空。
    """
    if not content:
        return []
    lines = content.split("\n")
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    ranges: list[tuple[int, int]] = []
    for b in iter_all_blocks(content, tolerant_length=tolerant_length):
        start = offsets[b["open_line"]]
        if b["close_line"] < 0:
            ranges.append((start, len(content)))
        else:
            end = offsets[b["close_line"]] + len(lines[b["close_line"]])
            ranges.append((start, end))
    return ranges


def classify_fence_defects(content: str) -> dict:
    """把「严格口径判为未闭合」的正文分成两类 —— 预检文案与修复路径的唯一分派处。

    返回 ``{"unclosed": [..], "length_mismatch": [..], "kind": str}``：

      · ``unclosed``       ：**真未闭合**（容错口径也判未闭合）。块内后续行全部
        按代码处理，导出会吞正文；文末追加闭合只能让块合法结束，**救不回正文**，
        必须人工核对。
      · ``length_mismatch`` ：**闭合标记长度错配**（严格判未闭合、容错判已闭合）。
        图表三侧仍能正常渲染，只差把闭合行标记补齐到开围栏长度，
        ``normalize_fence_closes`` 一步即可修 —— 不该让用户手抄闭合标记。
      · ``kind``           ：``""`` / ``"unclosed"`` / ``"length_mismatch"``（无缺陷时
        为空串）；两类都不会同时出现（两个扫描都在首个未闭合处停止，故各至多一项）。

    为什么需要这个函数（R76 · BUG-F1）：预检 DLV-07 与一键修复此前共用「是不是
    闭合行」但**取不同口径**，导致同一份正文「预检报警 / 修复答无东西可修」，
    finding 永不闭环。分类判据放在调用方就是新的第二份副本，故与谓词同处一源。
    """
    strict = find_unclosed(content, tolerant_length=False)
    if not strict:
        return {"unclosed": [], "length_mismatch": [], "kind": ""}
    lenient = find_unclosed(content, tolerant_length=True)
    if lenient:
        return {"unclosed": list(lenient), "length_mismatch": [], "kind": "unclosed"}
    return {"unclosed": [], "length_mismatch": list(strict), "kind": "length_mismatch"}


def normalize_close_enabled() -> bool:
    """长度归一开关（R76，默认开）。惰性读取，避免模块级循环导入。

    置 ``False``（``settings.content_fence_normalize_close = False``，
    环境变量 ``CONTENT_FENCE_NORMALIZE_CLOSE=false``）时
    ``content_utils.auto_fix_unclosed_fences`` 逐字回退旧行为（只做文末追加）。
    取不到 settings（如独立脚本引用本模块）按默认开启处理 —— 归一是收敛口径，
    不是扩大判定，缺配置不应改变生产语义。

    ⚠️ monkeypatch 提示：本函数是开关的**唯一权威**。消费方（content_utils）
    经模块属性调用，测试须 patch ``fence_common.normalize_close_enabled``；
    patch 消费方侧的别名会被解析成另一个对象（本仓 monkeypatch 表面迁移同族）。
    """
    try:
        from app.config import settings  # 局部导入：config 依赖链较宽，禁止模块级
    except Exception:
        return True
    return bool(getattr(settings, "content_fence_normalize_close", True))


def append_missing_closes(content: str, *, separator: str = "\n") -> tuple[str, list[dict]]:
    """对**真未闭合**围栏在文档末尾追加闭合标记（历史行为，原样保留）。

    判据 = ``tolerant_length=True``（与渲染侧同源）。未闭合的含义是「其后所有行都是
    代码内容」，故闭合只能补在文档末尾 —— 这也是本函数**无法**救回已被吞进块内的
    正文的原因：块内若混入中文句读结尾行，说明输入本身歧义，调用方须据此提示人工
    核对（R76 · F4 可观测性），不得静默报「已修复」。
    """
    unclosed = find_unclosed(content, tolerant_length=True)
    if not unclosed:
        return content, []
    lines = content.split("\n")
    stripped = content.rstrip()
    parts: list[str] = []
    log: list[dict] = []
    for u in unclosed:
        marker = u["char"] * u["length"]
        parts.append(separator + marker)
        # ✅ R76 · F4（补齐动作的可见性）：「未闭合」意味着其后所有行都是代码内容，
        #    追加闭合只能让块合法结束，**救不回已被吞进块里的正文**。块内存在中文
        #    句读结尾行 = 正文大概率已被吞，调用方必须据此提示人工核对，不得把
        #    「补齐 N 处」当成「已修复」。判据与 content_blocks 的「防误吞守卫」同源
        #    （``looks_like_prose``）。
        open_line0 = u["line"] - 1
        swallowed = sum(1 for ln in lines[open_line0 + 1:]
                        if looks_like_prose(ln))
        log.append({
            "action": "append_close",
            "line": u["line"],
            "lang": u["lang"],
            "char": u["char"],
            "length": u["length"],
            "swallowed_prose": swallowed,
        })
    return stripped + "".join(parts) + "\n", log


def looks_like_prose(line: str) -> bool:
    """该行是否以中文句读结尾 —— 围栏块内出现即「疑似正文被吞」的信号。

    与 ``content_blocks`` 的「防误吞守卫」同一判据（此前那份内联实现改为复用本函数，
    见 R76 消费点改造），故「超长但闭合」的有界前视恢复与补齐告警不会分叉。
    """
    return bool(_CJK_TAIL_RE.search(line))


_CJK_TAIL_RE = re.compile(r"[，。；：！？…]\s*$")


__all__ = [
    "FENCE_LINE_RE",
    "append_missing_closes",
    "classify_fence_defects",
    "fence_char_ranges",
    "find_unclosed",
    "is_fence_close",
    "iter_all_blocks",
    "iter_closed_blocks",
    "looks_like_prose",
    "normalize_fence_closes",
    "normalize_close_enabled",
    "parse_fence_line",
]
