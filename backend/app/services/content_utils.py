"""正文生成辅助纯函数（便于单元测试与复用）

从 sse_handlers.generate_content 中抽出的"目标章节筛选"逻辑：
范围（全部 / 指定子树）× 模式（all / missing）× 是否强制重写。
"""
from __future__ import annotations

import math
import re

# 围栏代码块（```lang ... ```）：图表（mermaid / chart-json）与普通代码块
# ✅ 判据唯一事实源（R76 · 2026-10-13）：本模块**不再自行判定闭合行**，全部委托
#   ``services/fence_common``（谓词 ``is_fence_close`` + 扫描 ``find_unclosed``）。
#   历史演进留档：
#     · 2026-09-28：旧 ``_FENCE_RE`` 只认反引号、按「最早出现的 ```」非贪婪闭合
#       → ``~~~mermaid`` 波浪围栏不剔除（图表代码计入字数）、4 反引号围栏闭合后
#         残留 1 个反引号计入字数。
#     · 2026-10-04（BUG-3）：与 ``content_blocks.parse_fence_line`` 统一为「同种字符
#       即可闭合」，消除「4 开 + 3 闭」被判未闭合后在文末追加假闭合 →
#       两个相邻闭围栏、图号虚跳、渲染失败占号。
#     · 本轮（R76）：那次统一只统一了**长度**维度，遗漏了**信息串**维度 ——
#       `read_fenced_block` 允许「反引号后带语言标签」的行当闭合，实测把后续正文吞进
#       代码块（导出少正文、前端 marked 却保留），并让 ``auto_fix`` 的「文末追加」
#       把缺陷固化成「事后检查通过」。现闭合判据定稿为
#       「同种字符 + 信息串必须为空 + 长度容错」，且长度错配在**写入前归一**
#       （见 ``auto_fix_unclosed_fences``），使严格口径（预检/字数/压缩禁区）与
#       容错口径（渲染）对落库正文结论恒等。
#   ⚠️ monkeypatch 提示：测试要替换围栏判定行为请 patch ``fence_common``，
#      patch 本模块的委托薄包装会被委托链吸收（与本仓 §运维惯例同族）。
FENCE_RE_DEPRECATED = None  # deprecated：请用 strip_fenced_code_blocks

# ---------- 字数口径常量（全项目唯一口径，避免魔法数字分散） ----------
# ✅ 2026-10-11（R66 字数偏差修复）：默认预算 1500 → 1200（四档之"标准"档）。
#    历史 1500 是散落在 8 处字面量里的"事实默认"，从未与任何档位对齐；
#    现统一收口到 DEFAULT_WORD_BUDGET，且其值等于 WORD_BUDGET_DEFAULT_TIER。
DEFAULT_WORD_BUDGET = 1200   # 未设置预算时的默认目标字数（= 标准档）
WORD_UNDER_RATIO = 0.8       # 低于预算 80% → under（同时是"是否需要续写"的门槛）
# ✅ 2026-10-06（R47 债-1）：WORD_OVER_RATIO 数值搬入
#    ``services/ai/prompts/_limits.py`` 单一事实源，本模块仍以同名可读。
from app.services.ai.prompts._limits import WORD_OVER_RATIO  # noqa: E402
# ✅ R76（2026-10-13）：围栏闭合判据唯一事实源。本模块只保留历史签名与用途口径，
#    「这一行是不是闭合行」的判定一律下沉到 fence_common（见其模块 docstring 的
#    三套语义分叉取证）。
from app.services import fence_common as _fence_common
from app.services.fence_common import (  # noqa: E402
    append_missing_closes,
    find_unclosed as _fence_common_find_unclosed,
    is_fence_close as _fence_common_is_close,
    normalize_fence_closes,
    FENCE_LINE_RE as _FENCE_LINE_RE,
)

# ---------- 四档字数控制（R66 · 2026-10-11） ----------
# 简短 800 / 标准 1200 / 详细 2000 / 超长 2500。
# 前端 WORD_BUDGET_OPTIONS 与后端档位解析共用这一组数值（前端只展示，
# 真源在此处；改档位只改这里，禁止再出现第二套字面量）。
WORD_BUDGET_TIERS = {"brief": 800, "standard": 1200, "detailed": 2000, "ultra": 2500}
WORD_BUDGET_DEFAULT_TIER = "standard"
# 三级目录（叶子节点）正文下限：除下级目录章节外，需生成约 300 字正文。
# 既是档位分配时的叶子预算下限（max(档位值, 300)），也是续写触发的最低门槛。
LEAF_MIN_WORD_BUDGET = 300


def resolve_word_budget_tier(raw) -> int | None:
    """把请求体里的 word_budget_tier 归一为目标字数（纯函数，便于单测）。

    接受：档位名（brief/standard/detailed/ultra，大小写/空格宽容）、
    档位数值本身（800/1200/2000/2500 直传也认，兼容脚本调用）。
    None / 空串 / 非法值 → None（不启用档位，沿用各章自身预算，行为不变）。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if not s:
            return None
        if s in WORD_BUDGET_TIERS:
            return WORD_BUDGET_TIERS[s]
        try:
            raw = float(s)
        except (TypeError, ValueError):
            return None
    if isinstance(raw, (int, float)):
        try:
            v = int(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        # 只认档位数值本身，不做区间吸附（800/1200/2000/2500 之外的值不算档位）
        if v in WORD_BUDGET_TIERS.values():
            return v
    return None


def word_deviation_of(
    word_count: int,
    word_budget: int,
    *,
    tolerance: float = 0.1,
) -> dict:
    """单章字数偏差三元组（实际-目标、比率、是否落在容差带内）。

    ``ratio = 实际/目标``（目标≤0 时按 ``DEFAULT_WORD_BUDGET`` 兜底，与
    ``word_status_for`` 同口径）。``within_band`` 按 ``1±tolerance`` 容差带
    判定（默认 0.1 即提示词承诺的 ``0.9X ~ 1.1X``），供可追溯记录与达标率统计。
    """
    wb = word_budget or DEFAULT_WORD_BUDGET
    wc = int(word_count or 0)
    ratio = wc / wb if wb > 0 else 0.0
    try:
        tol = abs(float(tolerance))
    except (TypeError, ValueError):
        tol = 0.1
    return {
        "deviation": wc - wb,
        "ratio": round(ratio, 4),
        "within_band": (1.0 - tol) <= ratio <= (1.0 + tol),
    }


def word_audit_summary(rows: list[dict], *, tolerance: float = 0.1) -> dict:
    """按章节行汇总字数达标情况（纯函数，审核预检与生成完成事件共用）。

    Args:
        rows: ``[{"section_id","title","level","word_budget","word_count"}, ...]``
            脏值宽容：缺失字段按 0/缺省处理，绝不抛异常。

    Returns:
        ``{"total","in_band","under_band","over","under","below_leaf_floor",
        "mean_ratio","max_ratio","items"}``；items 为逐章偏差明细（可追溯）。
    """
    items: list[dict] = []
    ratios: list[float] = []
    n_over = n_under = n_in_band = n_below_floor = 0
    for r in rows or []:
        try:
            wb = int(r.get("word_budget") or DEFAULT_WORD_BUDGET)
        except (TypeError, ValueError):
            wb = DEFAULT_WORD_BUDGET
        try:
            wc = int(r.get("word_count") or 0)
        except (TypeError, ValueError):
            wc = 0
        dev = word_deviation_of(wc, wb, tolerance=tolerance)
        status = word_status_for(wc, wb)
        if status == "over":
            n_over += 1
        elif status == "under":
            n_under += 1
        if dev["within_band"]:
            n_in_band += 1
        if wc < LEAF_MIN_WORD_BUDGET:
            n_below_floor += 1
        ratios.append(dev["ratio"])
        items.append({
            "section_id": r.get("section_id", ""),
            "title": r.get("title", ""),
            "level": int(r.get("level") or 0),
            "word_budget": wb,
            "word_count": wc,
            "deviation": dev["deviation"],
            "ratio": dev["ratio"],
            "within_band": dev["within_band"],
            "word_status": status,
        })
    total = len(items)
    # 中位数比率（sorted 取中间值，偶数取平均）；空输入 0.0
    srt = sorted(ratios)
    if srt:
        mid = len(srt) // 2
        median_ratio = srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2
    else:
        median_ratio = 0.0
    return {
        "total": total,
        "in_band": n_in_band,
        "under_band": total - n_in_band,
        "over": n_over,
        "under": n_under,
        "below_leaf_floor": n_below_floor,
        "mean_ratio": round(sum(ratios) / total, 4) if total else 0.0,
        "median_ratio": round(median_ratio, 4),
        "max_ratio": round(max(ratios), 4) if ratios else 0.0,
        "items": items,
    }


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


# ---------- 可读字数（R70 · 2026-10-12）**诊断专用，不参与任何判定** ----------
# ⚠️ 这不是第二把执行尺子。`word_count` 落库、`word_status`、续写门槛
#    （WORD_UNDER_RATIO）、超字数自动压缩（WORD_OVER_RATIO / _should_auto_shrink）
#    **全部仍走 text_word_count**。护栏
#    tests/test_word_caliber_diagnostic_r70_20261012.py 用反向 AST 锁死这一点：
#    任何判定路径引用本模块这几个函数即测试红 —— 免得「诊断读数」悄悄长成
#    第二口径（易标报告 §10 薄弱点 1 与本仓 R55/R62 反复警告的同族反例）。
#
# 为什么要有一个第二读数（R70 体检 F1）：text_word_count 的口径是
#    「剔围栏后的**字符数**」，中文标点、Markdown 结构符（| - # * >）、URL、
#    空白与换行**全部计入**。同一批 33 章真实语料实测：
#      · 字符口径 mean 偏差 136.9% → 可读口径 106.5%（虚高 30.4 个百分点）
#      · 缺口 15,050 字里标点占 8,744（58%）；换算系数 1.285
#      · 触发 >1.3× 自动压缩的章节 19/33 → 4/33
#    而预算数值（800/1200/2000/2500、DEFAULT_WORD_BUDGET）是按人的「字数」直觉
#    定的，用字符口径执行 ⇒ 每章真实目标被隐形放大近三成，且「该压的不该压、
#    该补的没补」两类相反错误同时发生（偏短章节在字符口径下提前满足续写门槛）。
#    把两个读数并列显示，是为了让「是否切口径、预算要不要同时下调」这个决策
#    有可对照的证据，而不是继续靠一条注释里的旧均值驱动默认值。
_RW_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")             # 图片整体不计（含图题语法）
_RW_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")           # 链接只留锚文字
_RW_REF_LINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")      # 引用式链接同上
_RW_URL_RE = re.compile(r"(?:https?|file|yibiao-asset)://\S+")  # 裸 URL 不计
_RW_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_RW_HTML_TAG_RE = re.compile(r"<br\s*/?>", re.I)
_RW_HTML_ANY_RE = re.compile(r"<[^>]{1,200}>")
_RW_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_RW_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")  # 表格分隔行整行剔除
_RW_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")     # 列表符号
_RW_MARKERS_RE = re.compile(r"[#>*_{}\\]|~~|\*\*|__|&nbsp;")    # 残留结构标记
_RW_HEADING_RE = re.compile(r"^\s*#{1,6}\s*")                  # 标题井号
_RW_QUOTE_RE = re.compile(r"^\s*>\s?")                        # 引用标记
# CJK 字符（含日文假名）：逐字计
_RW_CJK_IDEO_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
# 西文/数字 token：按词计（``1:1.2`` / ``GB50007`` 这类带连接符的视为一个词）
_RW_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*")


def _normalize_for_readable(content: str) -> str:
    """把正文归一到「用户实际读到的文字」：剔围栏/图片/URL/HTML/结构符。

    围栏剔除**复用** ``strip_fenced_code_blocks``（本模块唯一围栏事实源），
    不重造正则；行尾归一口径与 ``text_word_count`` 一致（CRLF/CR → LF），
    保证同一正文在 LF / CRLF 下读数相同。仅供本模块两个诊断函数共用。
    """
    t = content.replace("\r\n", "\n").replace("\r", "\n")
    t = strip_fenced_code_blocks(t)
    t = _RW_IMG_RE.sub(" ", t)
    t = _RW_LINK_RE.sub(r"\1", t)
    t = _RW_REF_LINK_RE.sub(r"\1", t)
    t = _RW_URL_RE.sub(" ", t)
    t = _RW_HTML_COMMENT_RE.sub(" ", t)
    t = _RW_HTML_TAG_RE.sub("\n", t)
    t = _RW_HTML_ANY_RE.sub(" ", t)
    t = _RW_INLINE_CODE_RE.sub(" ", t)
    lines = []
    for line in t.split("\n"):
        if _RW_TABLE_SEP_RE.match(line):
            continue
        line = _RW_LIST_MARK_RE.sub("", line)
        line = _RW_HEADING_RE.sub("", line)
        line = _RW_QUOTE_RE.sub("", line)
        lines.append(line)
    t = " ".join(lines).replace("|", " ")
    return _RW_MARKERS_RE.sub(" ", t)


def has_cjk(content: str | None) -> bool:
    """正文是否含 CJK 字符（可读字数读数的前在条件）。

    纯函数。用途：``readable_word_count`` 只对「人读的中文正文」有意义 ——
    测试语料与图表数据里常见的纯 ASCII 长串（``"x" * 3000``）按词合并后
    只有 1 个 token，把它当作「实际字数」显示会误导用户。调用方据此决定
    是否展示第二读数，**不得**用它改变任何判定结论。
    """
    return bool(content) and bool(_RW_CJK_IDEO_RE.search(content))


def readable_word_count(content: str | None) -> int:
    r"""可读字数：CJK 逐字 + 西文按词，剔标点/结构符/围栏/图片/URL（诊断专用）。

    ⚠️ **不参与判定**（详见上方常量块说明与 R70 护栏）。权威口径唯一，
    仍是 ``text_word_count``；本函数只提供「用户实际读到的文字量」这一对照读数。

    与 ``text_word_count`` 是**包含**关系：先剔围栏（共用同一事实源），再剔
    结构性噪声，最后按「字/词」而不是「字符」计数。

    Args:
        content: 章节正文（Markdown）。None / 空串 → 0。

    Returns:
        可读字数（int）。中文标点**不计**（与 Word 的「字数」不同，Word 计入，
        Word 档对照见 artifacts/r70_wordcount_audit 探针）。
    """
    if not content:
        return 0
    t = _normalize_for_readable(content)
    cjk = len(_RW_CJK_IDEO_RE.findall(t))
    # 先把 CJK 抠掉，剩下的才是西文/数字区；否则「第3章」里的 3 会与汉字并入同串
    rest = _RW_CJK_IDEO_RE.sub(" ", t)
    latin = len(_RW_LATIN_TOKEN_RE.findall(rest))
    return cjk + latin


# ---------- 字数收尾透明警告（R71 · 2026-10-12）**建议性，不参与任何判定** ----------
# 引入外部《易标文字生成数量控制规则与标准》R12「失败不硬裁 → 输出人工核对透明
# 警告」。与本仓既有红线天然一致（``content_shrink`` 的「宁可超字数，不可损坏正文」）：
# 当一章正文收尾时字数仍落在 word_status 的正常带之外（under / over），说明续写 /
# 压缩未能把它拉回预算，而系统**刻意不强行灌写 / 不强行裁剪**以保护内容 ——
# 此时应把这一事实透明告知用户，而不是静默放行。与 R70 诊断读数同族反例防控：
# 护栏 tests/test_word_caliber_transparency_r71_20261012.py 反向 AST 锁死本函数
# 绝不进入任何判定 / 落库路径（word_count 落库、word_status、续写门槛、压缩门槛
# 全部仍唯一走 text_word_count）。
#
# 复用唯一口径：入参 ``word_count`` 必须是调用方用 ``text_word_count`` 算好的字符
# 口径读数；本函数只做格式化，**不自算字数、不引入第三把尺子**。
def word_closeout_notice(
    word_count: int,
    word_budget: int,
    *,
    tolerance: float = 0.1,
) -> dict:
    """正文收尾字数透明警告（纯函数，仅建议）。见上方注释块的设计边界。

    Args:
        word_count: 调用方用 ``text_word_count`` 算好的正文字数（唯一口径）。
        word_budget: 本章目标字数（≤0 时按 ``DEFAULT_WORD_BUDGET`` 兜底，同口径）。
        tolerance: 容差带半宽，透传给 ``word_deviation_of``。

    Returns:
        ``{"advisory": bool, "kind": "under"|"over"|None, "message": str,
        "word_status": str, "ratio": float, "within_band": bool,
        "severity": "advisory"}``。脏值宽容：任何入参异常都不抛，退回 normal 带内、
        ``advisory=False``。``advisory`` 为真仅代表「建议人工核对」，绝不阻断落库。
    """
    wb = word_budget or DEFAULT_WORD_BUDGET
    try:
        wc = int(word_count or 0)
    except (TypeError, ValueError):
        wc = 0
    # 复用全项目唯一判定，不自造阈值：word_status_for 用同一组 0.8× / 1.3× 口径
    status = word_status_for(wc, wb)
    dev = word_deviation_of(wc, wb, tolerance=tolerance)
    message = ""
    if status == "under":
        message = (
            f"正文约 {wc} 字，低于目标 {wb} 字（续写未能补齐或已停止，"
            f"系统不强行灌写）——请人工核对是否需要补充。"
        )
    elif status == "over":
        message = (
            f"正文约 {wc} 字，超出目标 {wb} 字（系统按「宁可超字数，"
            f"不可损坏正文」未强行裁剪）——请人工核对是否需要精简。"
        )
    return {
        "advisory": status != "normal",
        "kind": status if status != "normal" else None,
        "message": message,
        "word_status": status,
        "ratio": dev["ratio"],
        "within_band": dev["within_band"],
        "severity": "advisory",
    }


# ---------- 未闭合围栏自动修复（2026-09-22） ----------
# 背景（DLV-07 缺口）：预检能检出未闭合围栏并提示用户「请补全 ``` 结束标记」，
# 但导出链路上 Markdown→DOCX 转换器遇到奇数个 ``` 时行为不定（吞掉后续正文 /
# 红字报错 / 图号虚跳），且用户在几百章正文里手工定位围栏成本极高。
# 现提供纯函数「在文档末尾补齐所有未闭合围栏」：确定性、幂等、不改动已闭合块，
# 供 preflight / autofix / 导出前置清洗共用，避免每处重造轮子。
# ✅ R76：正则本体上收到 ``fence_common.FENCE_LINE_RE``（本模块以别名 ``_FENCE_LINE_RE``
#    继续可用，供 ``count_fences`` 这类「是不是围栏行」的粗判使用）；
#    **「是不是某围栏的闭合行」一律走 ``_fence_common_is_close``，不得再自写判据**。


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
            if _fence_common_is_close(lines[j], ch, open_len, tolerant_length=False):
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
            if _fence_common_is_close(lines[j], ch, open_len, tolerant_length=False):
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
    闭」错配）只在 ``auto_fix_unclosed_fences`` 落库防线中生效，且该错配在写入前
    已由 ``fence_common.normalize_fence_closes`` 归一 —— 落库正文上严格与宽松恒等
    （R76 收口，函数本体委托 ``fence_common.find_unclosed(tolerant_length=False)``）。

    Args:
        content: Markdown 正文。

    Returns:
        未闭合围栏列表，每项包含
        ``{"line": 起始行号(1-based), "char": "`" 或 "~",
        "length": 围栏长度, "lang": 语言标签(可能为空), "marker": 原文标记}``。
        无未闭合围栏时返回 ``[]``。
    """
    return _fence_common_find_unclosed(content, tolerant_length=False)


# ✅ R76（2026-10-13）：旧的 `_find_unclosed_fences_lenient`（容错口径自写扫描循环）
#    已删除 —— 生产路径（auto_fix_unclosed_fences）改为直接消费
#    ``fence_common`` 的两个出口，容错扫描即 ``fence_common.find_unclosed(
#    content, tolerant_length=True)``。留着「委托薄包装但零调用方」就是新的
#    悬空副本（与本轮在 _chart_pipeline 清掉的 :197 同族），故不保留别名。


def auto_fix_unclosed_fences(
    content: str,
    *,
    separator: str = "\n",
) -> tuple[str, list[dict]]:
    """补齐正文中的代码围栏缺陷：**两步**归一 + 追加（落库防线唯一出口）。

    ✅ R76（2026-10-13）改造为两步，返回契约 ``(fixed_content, fixes_log)`` 与
    既有键**逐字不变**（加法式新增 ``action`` 区分两步）：

      1. ``fence_common.normalize_fence_closes`` —— 把「容错判为闭合、但标记长度
         短于开围栏」的闭合行补齐到开围栏长度（``action="normalize_close_length"``）。
         受开关 ``settings.content_fence_normalize_close``（默认 True）控制，
         置 False 逐字回退旧行为。
      2. ``fence_common.append_missing_closes`` —— 对归一后**仍真未闭合**的围栏，
         在文档末尾追加同种、等长的闭合标记（``action="append_close"``）。

    为什么必须先归一再追加（本轮最大踩坑，探针实测）：两步只走「文末追加」时，
    「4 开 + 3 闭」会被容错判为已闭合而**不进第二步日志**，但严格口径（预检
    DLV-07 / 字数剔除 / 压缩禁区 / 前端 marked）仍判未闭合 —— 于是同一份正文
    出现「自动修复说没东西修、预检说会吞正文、字数报 0」。归一让两种口径对
    落库正文**结论恒等**，分歧从文本源头消除，而不是各处各自兜底。

    两步都保持既有纪律：

      · 幂等：修复后的正文再跑一次返回空日志与不变内容；
      · 确定性：结果只由输入决定，不依赖随机/时间；
      · 无损：不改字、不改标点、不改已闭合块内部内容（第 1 步只改闭合行的
        **标记长度**，不增删行）；
      · 兼容：闭合标记用与开围栏**同种**字符（``` vs ~~~），长度对齐开围栏。

    ⚠️ 本函数**救不回**已被吞进块内的正文（未闭合意味着其后所有行都是代码，
    闭合只能补在末尾）。``append_close`` 日志项带 ``swallowed_prose`` 计数
    （块内以中文句读结尾的行数）—— 计数 > 0 时调用方**必须**提示人工核对，
    不得把「补齐 N 处」当成「已修复」（R76 · F4 可观测性）。

    Args:
        content: 原始 Markdown 正文。
        separator: 追加前的分隔符（默认空行，符合 CommonMark 段间隔）。

    Returns:
        ``(fixed_content, fixes_log)``

        fixes_log 每项结构：
          ``{"action": "normalize_close_length", "line": 闭合行行号, "lang": ...,
          "char": "`" 或 "~", "length": 补齐后长度, "old_length": 原长度}``
          ``{"action": "append_close", "line": 起始行号, "lang": 语言标签,
          "char": "`" 或 "~", "length": 闭合标记长度, "swallowed_prose": 疑似被吞正文行数}``
        无缺陷时返回 ``(原样, [])``。
    """
    if not content:
        return content, []
    log: list[dict] = []
    current = content
    if _fence_common.normalize_close_enabled():
        current, normalize_log = normalize_fence_closes(current)
        log.extend(normalize_log)
    current, append_log = append_missing_closes(current, separator=separator)
    log.extend(append_log)
    if not log:
        return content, []
    return current, log


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
