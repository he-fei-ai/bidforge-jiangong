"""正文生成辅助纯函数（便于单元测试与复用）

从 sse_handlers.generate_content 中抽出的"目标章节筛选"逻辑：
范围（全部 / 指定子树）× 模式（all / missing）× 是否强制重写。
"""
from __future__ import annotations

import math
import re

# 围栏代码块（```lang ... ```）：图表（mermaid / chart-json）与普通代码块
# ✅ 2026-09-28 修复（围栏剔除语义与 find_unclosed_fences 对齐）：
#   旧 `_FENCE_RE` 只认反引号、且按「最早出现的 ```」非贪婪闭合，导致：
#      · `````~~~mermaid````` 波浪线围栏完全不剔除 → 图表代码被计入正文字数，
#        超长章节误判 over / 续写触发阈值失真；
#      · `````` 4 反引号围栏只有前 3 个反引号参与匹配，闭合后残留 1 个反引号
#        字符被计入字数。
# ✅ 2026-10-04 修复（BUG-3 · 与 content_blocks.parse_fence_line 语义统一）：
#   旧实现按 CommonMark §4.7 严格判闭合（同种字符 + 长度 ≥ 开围栏 + 无标签），
#   与 `content_blocks.parse_fence_line` / `read_fenced_block` 的**宽松**语义
#   （同种字符即可闭合，不要求长度、允许带标签）不一致，导致 AI 输出高频的
#   「4 反引号开 + 3 反引号闭」模式：
#     · `content_blocks` 侧判为已闭合 → 图表正常登记、正常导出；
#     · 本模块 `find_unclosed_fences` 侧判为未闭合 → `auto_fix_unclosed_fences`
#       在 `_persist_section`（`sse_handlers.py:5314`）追加一个假闭合围栏，
#       落库后出现两个相邻闭围栏、图号虚跳、渲染失败占号。
#   现统一为宽松语义（与 content_blocks 一致），从根上消除两侧分歧。
#   注：宽松语义下同种字符的短围栏会被视为闭合，未闭合围栏扫描时不再向后
#   解析——这与 `read_fenced_block` 完全一致，是本模块设计取舍。
_FENCE_RE = None  # deprecated：请用 strip_fenced_code_blocks

# ---------- 字数口径常量（全项目唯一口径，避免魔法数字分散） ----------
DEFAULT_WORD_BUDGET = 1500   # 未设置预算时的默认目标字数
WORD_UNDER_RATIO = 0.8       # 低于预算 80% → under（同时是"是否需要续写"的门槛）
# ✅ 2026-10-06（R47 债-1）：WORD_OVER_RATIO 数值搬入
#    ``services/ai/prompts/_limits.py`` 单一事实源，本模块仍以同名可读。
from app.services.ai.prompts._limits import WORD_OVER_RATIO  # noqa: E402

# ---------- 输出 token 上限折算（P1-2 · 2026-09-17） ----------
# 背景：章节生成此前**不传 max_tokens**，落到配置值（实测 32768）—— 无输出上限
# 时模型会把"目标字数"当参考持续扩写（实测 62% 章节 over），既浪费时间又触发
# 额外的续写/压缩调用。此处按目标字数折算输出上限：既给足空间，又给"跑飞"设
# 物理上限（触顶即结束，不会为了多写字而拉长耗时）。
WORD_TOKENS_PER_CHAR = 0.8    # 中文正文约 0.7~0.8 token/字（取保守上界）
MAX_TOKENS_OVER_FACTOR = 1.25  # 相对目标字数的上浮（提示词硬上限 1.2X 留一档余量）
MAX_TOKENS_FLOOR = 1536        # 下限保护：避免短章节被截断


def max_tokens_for_budget(word_budget: int, *, chars: int | None = None) -> int:
    """按目标字数折算输出 token 上限（P1-2）。

    Args:
        word_budget: 章节目标字数（预算单元分配后的值）。
        chars: 本次需要覆盖的字数；默认取 ``word_budget × MAX_TOKENS_OVER_FACTOR``。
            续写场景传"剩余待补字数"，使上限随缺口收敛。

    Returns:
        输出 token 上限（不小于 ``MAX_TOKENS_FLOOR``）。
    """
    try:
        budget = int(word_budget or 0)
    except (TypeError, ValueError):
        budget = 0
    if chars is not None:
        try:
            target_chars = int(chars)
        except (TypeError, ValueError):
            target_chars = 0
    else:
        target_chars = int(budget * MAX_TOKENS_OVER_FACTOR)
    if target_chars <= 0:
        target_chars = DEFAULT_WORD_BUDGET
    return max(MAX_TOKENS_FLOOR, int(target_chars * WORD_TOKENS_PER_CHAR))


def normalize_word_budget_override(raw) -> int | None:
    """归一化请求体里的 word_budget_override（纯函数，便于单测）。

    背景（BUG 修复 2026-09-23 · B4）：前端/脚本可能把该字段以字符串
    （"2000"）、浮点（1500.0）、甚至非法值（"abc"、负数、bool）传入。
    旧实现直接把原值参与 int 运算与比较，字符串入参在 f"预算 {x+200}" 之类
    的拼接处抛 TypeError，把**整批**正文生成任务炸挂。

    归一规则（向后兼容：None/缺省 → None，即沿用各章自身预算，行为不变）：
      · None / 空串 → None（不覆盖）
      · bool → None（True/False 不是合法字数，防止 int(True)=1 静默生效）
      · int / float / 数字字符串 → 取整；≤0 视为无效 → None
      · 其它非数值（"abc"、dict、list…）→ None（绝不抛异常）
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            if not math.isfinite(float(raw)):
                return None
            v = int(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return v if v > 0 else None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            parsed = float(s)
            if not math.isfinite(parsed):
                return None
            v = int(parsed)
        except (TypeError, ValueError, OverflowError):
            return None
        return v if v > 0 else None
    return None


def text_word_count(content: str) -> int:
    r"""正文字数（剔除 ``` 与 ~~~ 代码围栏后的字符数）—— 全项目唯一口径。

    背景（"图表与正文一体生成"带来的口径问题）：正文 AI 会把 ```mermaid /
    ```chart-json 图表代码块直接内嵌在章节正文里。若沿用 ``len(content)`` 统计，
    图表代码（常达数百字符）会被算作"正文字数"：

      · ``word_status`` 被虚高的字数掩盖 —— 正文实际只有 1000 字（低于
        ``budget*0.8`` 的 under 阈值）却因多出数百字符图表代码而判为 normal；
      · 续写判定（``wc < budget*0.8``）提前满足，正文偏短也不再补写。

    图表代码不是"正文文字"，故剔除后再计数，统计的是用户实际阅读到的文字量。

    ✅ 2026-09-28 修复（剔除口径与 ``find_unclosed_fences`` 对齐）：
      旧实现用正则 ````` ```[\s\S]*?``` ````` 剔除，只认反引号、且 4 反引号
      围栏会残留字符、`````~~~````` 波浪线围栏完全不剔除 —— 波浪线图表代码
      被误计入字数；现改用 ``strip_fenced_code_blocks``（逐行状态机，两种
      围栏同口径剔除），未闭合围栏仍按"从围栏处截断"处理。

    Args:
        content: 章节正文（Markdown，可能含 ```mermaid / ```chart-json / 普通代码块）。

    Returns:
        正文字符数（int，空输入返回 0）。
    """
    if not content:
        return 0
    # ✅ 修复（2026-10-09 · CRLF 行尾 \r 被计入字数）：
    #    strip_fenced_code_blocks 按 \n split/join，CRLF 输入的每行尾随 \r
    #    会保留在剔除后的输出里并被 len() 计入。对 50 行章节约多计 50 字
    #    （~3% 偏差），在 under(0.8×budget) / over(1.3×budget) 边界条件下
    #    可能把 under 误判为 normal（不续写）或 normal 误判为 over（触发
    #    不必要的压缩）。正文字数应只计用户实际阅读到的文字量，\r 不可见。
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    return len(strip_fenced_code_blocks(content))


# ---------- 未闭合围栏自动修复（2026-09-22） ----------
# 背景（DLV-07 缺口）：预检能检出未闭合围栏并提示用户「请补全 ``` 结束标记」，
# 但导出链路上 Markdown→DOCX 转换器遇到奇数个 ``` 时行为不定（吞掉后续正文 /
# 红字报错 / 图号虚跳），且用户在几百章正文里手工定位围栏成本极高。
# 现提供纯函数「在文档末尾补齐所有未闭合围栏」：确定性、幂等、不改动已闭合块，
# 供 preflight / autofix / 导出前置清洗共用，避免每处重造轮子。
_FENCE_LINE_RE = re.compile(
    # 独立成行的围栏标记：至少 3 个反引号或波浪号，后面可选语言标签
    r"^[ \t]{0,3}(`{3,}|~{3,})([^`~\n]*)$"
)


def fence_spans(content: str | None) -> list[tuple[int, int]]:
    """返回正文中**已闭合**代码围栏的字符区间（含开/闭围栏行）。

    语义与 ``strip_fenced_code_blocks`` / ``find_unclosed_fences`` 完全一致
    （CommonMark §4.7）：反引号（```）与波浪线（~~~）分别处理、闭合需同种字符
    且长度 ≥ 开围栏、围栏内不解析新围栏、未闭合的围栏**不产出区间**。

    注：本函数与 ``strip_fenced_code_blocks`` / ``find_unclosed_fences`` 保持
    **严格** CommonMark 语义。宽松闭合（AI「4 反引号开 + 3 反引号闭」错配）
    只在 ``auto_fix_unclosed_fences`` 落库防线中生效，详见其 docstring。

    用途：需要**按字符区间**保护代码围栏的调用方（如
    ``content_shrink.collect_protected_ranges`` —— "压缩禁区"必须知道围栏的
    确切起止位置，而非只剔除文本）。

    Args:
        content: 原始 Markdown 正文。

    Returns:
        ``[(start, end), ...]`` 列表，end 为闭围栏行尾（不含行尾换行符）。
        空输入 / 无闭合围栏返回 ``[]``。
    """
    if not content:
        return []
    lines = content.split("\n")
    # 预计算每行起始偏移（含行尾换行符）
    offsets: list[int] = []
    off = 0
    for ln in lines:
        offsets.append(off)
        off += len(ln) + 1
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _FENCE_LINE_RE.match(lines[i])
        if m is None:
            i += 1
            continue
        ch = m.group(1)[0]
        open_len = len(m.group(1))
        open_start = offsets[i]
        j = i + 1
        close_idx = -1
        while j < n:
            cm = _FENCE_LINE_RE.match(lines[j])
            if cm is not None and cm.group(1)[0] == ch \
                    and len(cm.group(1)) >= open_len \
                    and not cm.group(2).strip():
                close_idx = j
                break
            j += 1
        if close_idx >= 0:
            close_end = offsets[close_idx] + len(lines[close_idx])
            spans.append((open_start, close_end))
            i = close_idx + 1
        else:
            # 未闭合：其后内容均视为代码，不产出区间
            break
    return spans


def strip_fenced_code_blocks(content: str | None) -> str:
    """剔除正文中的全部（已闭合）代码围栏块，保留围栏外正文。

    语义与 ``find_unclosed_fences`` 严格一致（CommonMark §4.7）：
      · 反引号（```）与波浪线（~~~）两种围栏分别处理；
      · 开围栏是独立成行、≤3 个前导空格、≥3 个同种字符、后接可选语言标签；
      · 闭合围栏必须与开围栏同种字符、长度 ≥ 开围栏、且不带语言标签；
      · 围栏内部不再解析新围栏；
      · 到达末尾仍未闭合时，从该开围栏起「截断」（其后内容均视为代码）。

    注：本函数保持**严格** CommonMark 语义；宽松闭合（AI「4 反引号开 + 3 反引号
    闭」错配）只在 ``auto_fix_unclosed_fences`` 落库防线中生效。

    结果按换行拼接，围栏外正文的换行结构整体保留（不吞行、不折叠）。
    注意：紧邻围栏行的空行/正文换行会因围栏整段移除而小幅变化 —— 这是
    有意为之（代码不参与正文字数口径），回归见 ``test_content_utils``。

    Args:
        content: 原始 Markdown 正文（可含 mermaid / chart-json / 普通代码块）。

    Returns:
        剔除代码围栏后的正文（未闭合围栏作为截断点，其后内容不保留）。
    """
    if not content:
        return ""
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _FENCE_LINE_RE.match(lines[i])
        if m is None:
            out.append(lines[i])
            i += 1
            continue
        ch = m.group(1)[0]
        open_len = len(m.group(1))
        # 向前寻找闭合围栏：同种字符、长度 ≥ open_len、且不带标签
        j = i + 1
        closed_idx = -1
        while j < n:
            cm = _FENCE_LINE_RE.match(lines[j])
            if cm is not None and cm.group(1)[0] == ch \
                    and len(cm.group(1)) >= open_len \
                    and not cm.group(2).strip():
                closed_idx = j
                break
            j += 1
        if closed_idx >= 0:
            # 已闭合：跳过整段围栏（含开闭行）
            i = closed_idx + 1
        else:
            # 未闭合：从开围栏处截断，其余视为代码
            break
    return "\n".join(out)


def find_unclosed_fences(content: str) -> list[dict]:
    """扫描正文，返回所有「尚未闭合」的代码围栏。

    语义（与 CommonMark / GFM 严格一致）：
      · 反引号围栏（``` / ```` …）与波浪围栏（~~~ / ~~~~ …）分别处理；
      · 闭合围栏的字符与开围栏**同种**且**长度不小于**开围栏长度；
      · 开围栏后可选语言标签；闭合围栏后不允许带标签；
      · **围栏内部不解析新围栏**：一旦进入开围栏，遇到任何短于开围栏的反引号/
        其它字符围栏均视为**代码内容**，只有匹配的开围栏闭合算数（CommonMark §4.7）；
      · 多个围栏按线性扫描处理，未闭合状态在到达文档末尾时结束。

    注：本函数保持**严格** CommonMark 语义；宽松闭合（AI「4 反引号开 + 3 反引号
    闭」错配）只在 ``auto_fix_unclosed_fences`` 落库防线中生效。

    Args:
        content: Markdown 正文。

    Returns:
        未闭合围栏列表，每项包含
        ``{"line": 起始行号(1-based), "char": "`" 或 "~",
        "length": 围栏长度, "lang": 语言标签(可能为空), "marker": 原文标记}``。
        无未闭合围栏时返回 ``[]``。
    """
    if not content:
        return []
    lines = content.split("\n")
    unclosed: list[dict] = []
    i = 0
    n_lines = len(lines)
    while i < n_lines:
        m = _FENCE_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        open_marker = m.group(1)
        rest = m.group(2).strip()
        ch = open_marker[0]
        n = len(open_marker)
        lang = rest.split()[0].lower() if rest else ""
        open_line = i + 1
        # 向前寻找闭合围栏：同种字符、长度 ≥ n、且不带标签
        close_idx = -1
        j = i + 1
        while j < n_lines:
            cm = _FENCE_LINE_RE.match(lines[j])
            if not cm:
                j += 1
                continue
            cm_marker = cm.group(1)
            cm_rest = cm.group(2).strip()
            if cm_marker[0] == ch and len(cm_marker) >= n and cm_rest == "":
                close_idx = j
                break
            j += 1
        if close_idx < 0:
            # 到达文档末尾仍未闭合
            unclosed.append({
                "line": open_line, "char": ch, "length": n,
                "lang": lang, "marker": open_marker,
            })
            break  # 后面的行都在围栏内，不再是新围栏
        i = close_idx + 1
    return unclosed


def _find_unclosed_fences_lenient(content: str) -> list[dict]:
    """宽松扫描未闭合围栏（仅用于 ``auto_fix_unclosed_fences`` 落库防线）。

    语义与 ``content_blocks.parse_fence_line`` / ``read_fenced_block`` 完全
    一致：**同种字符即可视为闭合**（不要求长度 ≥ 开围栏、也不排除带标签）。

    用途：AI 输出高频出现「4 反引号开 + 3 反引号闭」错配。严格 CommonMark
    语义下这被判为未闭合，但图表登记/导出侧（``read_fenced_block``）判为已
    闭合、图表正常渲染。若 ``auto_fix_unclosed_fences`` 沿用严格判据，会在此
    类块尾追加一个假闭合围栏，落库后出现「两个相邻闭围栏、图号虚跳、渲染
    失败占号」。用宽松语义扫描即从根源消除这一分歧。

    注：只在本函数内部使用，不改变 ``find_unclosed_fences``（对外仍提供严格
    CommonMark 语义，用于字数/区间/内容保护等场景，那些场景需要严格语义
    把「4 反引号开 + 3 反引号闭 + 4 反引号」正确识别为「内容 + 闭合」）。
    """
    if not content:
        return []
    lines = content.split("\n")
    unclosed: list[dict] = []
    i = 0
    n_lines = len(lines)
    while i < n_lines:
        m = _FENCE_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        open_marker = m.group(1)
        rest = m.group(2).strip()
        ch = open_marker[0]
        n = len(open_marker)
        lang = rest.split()[0].lower() if rest else ""
        open_line = i + 1
        close_idx = -1
        j = i + 1
        while j < n_lines:
            cm = _FENCE_LINE_RE.match(lines[j])
            if cm is not None and cm.group(1)[0] == ch:
                close_idx = j
                break
            j += 1
        if close_idx < 0:
            unclosed.append({
                "line": open_line, "char": ch, "length": n,
                "lang": lang, "marker": open_marker,
            })
            break  # 后面的行都在围栏内，不再是新围栏
        i = close_idx + 1
    return unclosed


def auto_fix_unclosed_fences(
    content: str,
    *,
    separator: str = "\n",
) -> tuple[str, list[dict]]:
    """在文档末尾补齐所有未闭合的 Markdown 代码围栏。

    仅「追加」操作，不改动已有正文与已闭合块，保证：

      · 幂等：修复后的正文再跑一次返回空日志与不变内容；
      · 确定性：结果只由输入决定，不依赖随机/时间；
      · 无损：不改字、不改标点、不改已闭合块内部；
      · 兼容：闭合标记用与开围栏**同种**字符（``` vs ~~~），长度对齐开围栏。

    ✅ 2026-10-04 修复（BUG-3）：本函数改用**宽松**闭合扫描
    （``_find_unclosed_fences_lenient``），与图表登记/导出/预检侧的
    ``content_blocks.parse_fence_line`` / ``read_fenced_block`` 口径一致。
    原因：AI 输出高频出现「4 反引号开 + 3 反引号闭」错配，图表三侧判为已
    闭合、正常渲染，但本函数若沿用严格 CommonMark（要求闭围栏长度 ≥ 开围栏）
    会将其误判为未闭合、追加一个假闭合围栏，落库后出现「两个相邻闭围栏、
    图号虚跳、渲染失败占号」。放宽到「同种字符即可闭合」即从根源消除两侧
    分歧。

    其它扫描函数（``fence_spans`` / ``strip_fenced_code_blocks`` /
    ``find_unclosed_fences``）仍保持严格 CommonMark 语义，因为它们的用途
    （压缩禁区定位、字数口径、内容保护）需要严格识别「4 反引号开 + 3 反引号
    短围栏行 + 4 反引号闭」里的短围栏行为代码内容。

    Args:
        content: 原始 Markdown 正文。
        separator: 追加前的分隔符（默认空行，符合 CommonMark 段间隔）。

    Returns:
        ``(fixed_content, fixes_log)``

        fixes_log 每项结构：
          ``{"action": "append_close", "line": 起始行号, "lang": 语言标签,
          "char": "`" 或 "~", "length": 闭合标记长度}``
        无未闭合围栏时返回 ``(原样, [])``。
    """
    if not content:
        return content, []
    unclosed = _find_unclosed_fences_lenient(content)
    if not unclosed:
        return content, []
    # 从原始内容剥离末尾空白，避免 ``` 与正文粘连
    stripped = content.rstrip()
    parts: list[str] = []
    log: list[dict] = []
    for u in unclosed:
        marker = u["char"] * u["length"]
        parts.append(separator + marker)
        log.append({
            "action": "append_close",
            "line": u["line"],
            "lang": u["lang"],
            "char": u["char"],
            "length": u["length"],
        })
    # 修复后统一以换行结尾，符合 Markdown 段落约定
    fixed = stripped + "".join(parts) + "\n"
    return fixed, log


def count_fences(content: str) -> int:
    """统计正文中所有 ``` / ~~~ 围栏标记的**总出现次数**（含未闭合）。

    用于回归校验：修复前后偶数/奇数一致性、图号是否虚跳。
    与 CommonMark 的「独立成行的围栏」口径一致，不把行内 ``` 计入。
    """
    if not content:
        return 0
    n = 0
    for raw in content.split("\n"):
        if _FENCE_LINE_RE.match(raw):
            n += 1
    return n


def word_status_for(word_count: int, word_budget: int) -> str:
    """按字数与预算判定状态：``under`` / ``normal`` / ``over``。

    全项目唯一口径：正文生成落库（`sse_handlers._persist_section`）与
    单章手工保存（`sections.update_section`）必须使用同一判定，
    否则同一章节"自动生成"与"手工保存"会出现不同状态。
    """
    wb = word_budget or DEFAULT_WORD_BUDGET
    if word_count < wb * WORD_UNDER_RATIO:
        return "under"
    if word_count > wb * WORD_OVER_RATIO:
        return "over"
    return "normal"


# ---------- 并发档位归一（P0 修复 2026-09-19 · 严格遵循用户前端选择） ----------
CONCURRENCY_LEVELS = {"slow": 2, "balanced": 3, "fast": 5}
CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 5


def resolve_concurrency(raw, default: int = 3) -> int:
    """把用户并发参数归一为 [1,5] 整数档位（绝不抛异常）。

    接受：档位字符串（slow/balanced/fast，大小写/空格宽容）、整数、
    整数值浮点、数字字符串（"3"）。超范围数值钳到边界（用户显式要 8 → 5）；
    非法值/布尔/None 回退 default。

    旧实现（sse_handlers）只认精确匹配的档位串与 int，"3"、3.0 这类前端
    序列化变体会被静默丢弃 → 实际并发落到全局残留值上，与用户选择脱节。
    """
    d = max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, int(default or 3)))
    if isinstance(raw, bool) or raw is None:
        return d
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in CONCURRENCY_LEVELS:
            return CONCURRENCY_LEVELS[s]
        raw = s
    try:
        raw_f = float(raw)
    except (TypeError, ValueError, OverflowError):
        return d
    if not math.isfinite(raw_f):
        return d
    v = int(raw_f)
    if v < CONCURRENCY_MIN:
        return d
    return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, v))


def leaf_word_budget(leaf: dict, default: int = DEFAULT_WORD_BUDGET) -> int:
    """读取章节目标字数（对 NULL/字符串/浮点等脏值稳健，纯函数可单测）。

    与目录生成 word_budget 脏值修复同源：旧表达式 `leaf.get("word_budget")
    or DEFAULT` 会把非数值脏值（如 "1500"）原样漏过，续写判定
    `wc < budget * 0.8` 对 str 乘浮点抛 TypeError → 整章被误报"生成失败"。
    """
    raw = (leaf or {}).get("word_budget")
    if isinstance(raw, bool):
        return default
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def order_sections_dfs(sections: list[dict]) -> list[dict]:
    """把扁平章节行按目录树**前序 DFS** 排序（父先于子、同级按 sort_order）。

    背景（P0 修复 2026-09-19）：sections.sort_order 是「同级内序号」——目录
    落库（sections._process_nodes）按层级 enumerate 从 0 赋值，不是全局序号。
    SQL `ORDER BY sort_order` 会把所有层级的同序号节点排在一起
    （1 → 1.1 → 1.1.1 → 2 → 2.1 …按"列"展开而非"文档序"），正文生成任务
    若按该顺序排队，启动顺序与目录层级不一致：后置章节可能先于前置章节
    开跑，「前序同级结尾参考」落空、进度序号跳乱，破坏全文结构递进性。

    规则：孤儿节点（parent_id 悬空/自引用）按根节点处理，绝不丢行；
    环保护：parent 链成环导致不可达的节点按原相对序补挂在尾部。
    """
    if not sections:
        return []
    ids = {s.get("id") for s in sections}
    children: dict[str, list[dict]] = {}
    for s in sections:
        pid = s.get("parent_id", "") or ""
        key = pid if (pid and pid in ids and pid != s.get("id")) else ""
        children.setdefault(key, []).append(s)
    for v in children.values():
        v.sort(key=lambda x: (x.get("sort_order") or 0, str(x.get("id") or "")))
    out: list[dict] = []
    stack: list[dict] = list(reversed(children.get("", [])))
    while stack:
        node = stack.pop()
        out.append(node)
        kids = children.get(node.get("id") or "", [])
        if kids:
            stack.extend(reversed(kids))
    if len(out) < len(sections):
        # 父子成环（脏数据）时环上节点不可达 → 按原序补挂，保证不丢行
        seen = {id(s) for s in out}
        out.extend(s for s in sections if id(s) not in seen)
    return out


def select_target_leaves(
    all_sections: list[dict],
    *,
    section_id: str = "",
    mode: str = "all",
    force_rewrite: bool = False,
) -> list[dict]:
    """筛选本次需要生成的【叶子】章节（保持入参顺序，调用方应先过
    order_sections_dfs 保证入参为目录树前序 DFS 序）。

    规则：
    1. section_id 有值 → 取该节点及其全部后代；否则取全部章节。
    2. mode == "missing" → 只保留 status ∈ {empty, failed, pending} 的章节。
       mode == "continue" → 只保留【已有非空正文】的章节（续写的对象，
       空白章节应走"生成"而非"续写"）。
    3. 非 force_rewrite → 跳过【已有非空正文】的章节。
       ✅ 修复点：旧实现只跳过 status=='generated' 且 word_count>0 的章节，
       导致 status 为 reviewed/expanded 等已人工处理的章节会被默认生成覆盖。
       现以"是否已有正文内容"为唯一判据，语义更安全。
    4. 在候选集合中只保留叶子（自身不是任何候选节点的父节点）。
    """
    if not all_sections:
        return []

    # 父 → 子 映射（一次构建，避免 O(N²) 反复扫描）
    children_map: dict[str, list[str]] = {}
    for s in all_sections:
        children_map.setdefault(s.get("parent_id", ""), []).append(s["id"])

    # 1) 范围
    if section_id:
        target_set = {section_id}
        queue = [section_id]
        while queue:
            pid = queue.pop(0)
            for cid in children_map.get(pid, []):
                if cid not in target_set:
                    target_set.add(cid)
                    queue.append(cid)
    else:
        target_set = {s["id"] for s in all_sections}

    # 2) 模式 / 跳过策略
    if mode == "missing":
        # ✅ BUG 修复：missing 模式除了检查 status，还需确认 content 确实为空。
        # 旧实现只看 status，status='empty' 但 content 非空（数据不一致）的章节
        # 会被选中重新生成，覆盖已有正文。
        target_set = {
            s["id"] for s in all_sections
            if s["id"] in target_set
            and s.get("status") in ("empty", "failed", "pending")
            and not (s.get("content") or "").strip()
        }
    elif mode == "continue":
        # ✅ 续写模式：只保留已有非空正文的章节（与 missing 互为补集）。
        target_set = {
            s["id"] for s in all_sections
            if s["id"] in target_set
            and (s.get("content") or "").strip()
        }
    elif not force_rewrite:
        target_set = {
            s["id"] for s in all_sections
            if s["id"] in target_set
            and not ((s.get("content") or "").strip() and s.get("word_count", 0) > 0)
        }

    # 3) 叶子
    target_sections = [s for s in all_sections if s["id"] in target_set]
    parent_ids_in_target = {s["parent_id"] for s in target_sections if s.get("parent_id")}
    return [s for s in target_sections if s["id"] not in parent_ids_in_target]


def build_sibling_context(
    all_sections: list[dict],
    leaf: dict,
    *,
    generated_contents: dict[str, str] | None = None,
    children_by_parent: dict[str, list[dict]] | None = None,
    max_siblings: int = 8,
    summary_chars: int = 300,
) -> tuple[str, str]:
    """构建同级章节上下文（正文生成提示词用）。

    解决"分布生成导致同层次内容雷同 / 前后不衔接"的两条需求：
    1. 同级清单：给出**标题 + 描述**，让 AI 明确本层还有哪些兄弟章节、
       各自负责什么，从而主动避免内容重复（旧实现只给标题，AI 无从判断边界）。
    2. 前序同级摘要：取当前章节**之前**、离它最近且已有正文的同级章节的
       **结尾片段**，作为行文风格与逻辑衔接的参考。

    与旧实现的差异：
    - 旧实现只看 DB 里已落库的 content（同批新生成时 `all_sections` 快照为空，
      摘要恒为空，功能形同虚设）；本函数优先读 `generated_contents`
      （本次生成过程中已完成章节的实时正文），使摘要真正生效。
    - 旧实现取正文**开头** 300 字，用作"续接"参考不如**结尾**片段贴切。

    Args:
        all_sections: 方案全部章节（含 parent_id / sort_order）。
        leaf: 当前目标章节。
        generated_contents: {section_id: 本次生成的最新正文}，可为 None。
        children_by_parent: 预构建的 parent_id → [章节] 索引。传入时用 O(1) 查表，
            避免在逐章生成中退化为 O(N²) 扫描（与 _build_parent_chain 同一优化）。
        max_siblings: 同级清单最多展示的条数（避免提示词过长）。
        summary_chars: 前序摘要截取字数。

    Returns:
        (同级章节清单文本, 前序同级正文摘要)。均可能为空字符串。
    """
    parent_id = leaf.get("parent_id", "")
    if children_by_parent is not None:
        pool = children_by_parent.get(parent_id, [])
    else:
        pool = [s for s in all_sections if s.get("parent_id", "") == parent_id]
    siblings = [s for s in pool if s.get("id") != leaf.get("id")]
    siblings.sort(key=lambda s: (s.get("sort_order", 0), s.get("id", "")))

    lines: list[str] = []
    for s in siblings[:max_siblings]:
        desc = (s.get("description") or "").strip()
        lines.append(f"- {s.get('title', '')}" + (f"：{desc}" if desc else ""))

    # 前序同级摘要：sort_order 小于当前、离当前最近的一个已有正文者
    prev_summary = ""
    cur_order = leaf.get("sort_order", 0)
    for s in reversed([x for x in siblings if x.get("sort_order", 0) < cur_order]):
        content = (generated_contents or {}).get(s["id"]) or s.get("content") or ""
        content = content.strip()
        if content:
            prev_summary = content[-summary_chars:]
            break

    return "\n".join(lines), prev_summary
