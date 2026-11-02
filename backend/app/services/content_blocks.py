"""正文内容块解析 / 子标题编号 / Markdown 围栏读取（2026-09-27 · 自 routers 下沉）

为什么下沉（架构债 T-2）
----------------------
`services/numbering.py` 为让「落库正文子标题编号」与「导出成稿」**逐字同源**，
此前运行时 ``from app.routers.export import (...)`` —— 形成 **services → routers
的层级反向依赖**（AGENTS.md §2 目录结构中 services 不得依赖 routers），且让
"核心算法住在最大的路由文件里"。`routers/_chart_pipeline.py` 里的
``parse_fence_line`` / ``read_fenced_block`` 同样是纯 Markdown 工具，却被放在
routers 层被 services 复用。

本模块把这些**纯函数**整体下沉，成为唯一实现：
- ``routers/export.py`` / ``routers/_chart_pipeline.py`` 改为从本模块导入
  （名字保持不变，其内部上百处引用与既有测试的
  ``from app.routers.export import _parse_content_blocks`` 全部照常工作）；
- ``services/numbering.py`` 改为从本模块导入，**反向依赖彻底消除**。

⚠️ 行为契约：函数**逐行搬迁、不做任何逻辑修改** —— 导出成稿渲染结果必须与
下沉前完全一致（由 tests/test_export_*.py、tests/test_numbering_*.py、
tests/test_inline_charts.py 等锁定）。

对外入口：
- ``_parse_content_blocks`` —— Markdown 正文 → 块序列
- ``_compute_subheading`` —— 块级子标题编号（与导出 write_section 同一算法）
- ``_strip_title_number`` / ``_strip_duplicate_leading_title`` —— 标题清洗
- ``parse_fence_line`` / ``read_fenced_block`` —— 代码围栏解析/读取
"""
from __future__ import annotations

import json
import logging
import re

from app.services.ai.heading_templates import HEADING_STYLE_CONFIG
from app.services.chart_validators import (
    PIL_RENDERABLE_CHART_TYPES,
    detect_mermaid_chart_type,
    infer_chart_type_from_payload,
)

# ✅ R76（2026-10-13）：围栏闭合判据唯一事实源。本模块的 parse_fence_line
#    保留历史签名与 lang 语义（图表三侧的上百处引用 + 既有测试的导入名不动），
#    但「这一行是不是闭合行」的判定一律下沉 fence_common —— 旧实现只比对围栏
#    字符，把「反引号后带语言标签」的行当作闭合，实测在块内提前闭合后让下一个
#    裸围栏行变成新开围栏、把其后正文整段吞进代码块（state=eof），而前端 marked
#    （CommonMark）判该行为正文保留 —— 预览与导出反向分叉（BUG-F2）。
from app.services.fence_common import (  # noqa: E402
    MARKDOWN_FENCE_LANGS as _MARKDOWN_FENCE_LANGS_SHARED,
)
from app.services.fence_common import (
    is_fence_close as _fence_is_close,
)

# ✅ R165（2026-11-01 · 4 空格缩进图表围栏）：缩进归一的「保护区」判定复用
#    fence_common 的**同一份**遍历（块内不解析新围栏），不另写状态机。
from app.services.fence_common import (
    iter_all_blocks as _iter_all_blocks,
)
from app.services.fence_common import (
    looks_like_prose as _looks_like_prose,
)
from app.services.fence_common import (
    parse_fence_line as _fence_parse_line,
)
from app.services.numbering import strip_outline_numbering_all

logger = logging.getLogger("content_blocks")
# 围栏工具沿用原 logger 名，日志来源保持不变（不改变运维检索习惯）
_pipe_logger = logging.getLogger("chart_pipeline")

MAX_INLINE_CODE_BLOCK_LINES = 500



INLINE_CHART_FENCE_LANGS = ("mermaid", "chart-json", "ai_image")

# ---------- 自 routers/export.py 一同下沉的模块级常量 ----------
# （逐行搬运，未做任何修改；导出与编号两侧共用同一批正则）
_CHART_FENCE_LANGS = INLINE_CHART_FENCE_LANGS
_HEADING_NUM_PREFIX_RES = (
    re.compile(r"^#{1,6}\s*"),
    re.compile(r"^第[一二三四五六七八九十百千零\d]+[章节][、\s]*"),
    re.compile(r"^[（(][一二三四五六七八九十百]+[)）][、\s]*"),
    re.compile(r"^\d+(?:\.\d+)*[、.\s]+"),
    re.compile(r"^\d+[）)][、\s]*"),
    re.compile(r"^[a-zA-Z]{1,2}[、.\s]+"),
)
_IMAGE_LINE_RE = re.compile(r"^!\[(?P<alt>[^\]\n]*)\]\((?P<url>https?://[^)\s]+)\)$")
# ---------- 图表引导语判据（**唯一事实来源**，2026-10-06 · R48 收敛） ----------
# 引导语 = 图表块上方那句「专为引出该图而写」的短段落（"施工工艺流程如下图所示："）。
# 删除/跳过图表时必须同步回收它，否则成稿出现"见下图"却无图的悬空引用。
#
# ⚠️ 历史分叉（本次收口根因）：本判据曾有**三份副本**——
#   · 本模块 `_LEAD_IN_HINT_RE`（5 个备选）：导出侧 `export._pop_orphan_lead_in` 经
#     import 共用；
#   · `_chart_pipeline._ORPHAN_LEAD_IN_RE`（8 个备选：多出 如下图所示|见下图所示|详见下图）
#     与 `_chart_pipeline._LEAD_IN_MAX_CHARS`；
#   · 长度上限 `60` 另有 3 处裸字面量（本模块 2 处 + `export.py` 1 处）。
# 实测两份正则**判定完全等价**（8 个备选全部被 5 个备选经子串匹配覆盖：
# `详见下图`⊃`见下图`、`如下图所示`⊃`如下图`、`见下图所示`⊃`见下图`），但按 AGENTS.md
# 反复记录的「同一判据多处字面量」教训，任一侧被单独修改都会让「删图删不删引导语」
# 在登记侧与导出侧再次分叉（一处删、一处留 → 正文出现新的悬空引用）。
# 现收敛：正则与上限各只有一份，全部消费方 import，不再有本地副本。
LEAD_IN_MAX_CHARS = 60
# 对外公开别名：判定「一行文本是否是图表引导语」。
# ✅ R164（2026-11-01 · 图题残字）：备选**必须按「长词在前」排列**——
#    `re.sub` 用第一条**命中**的备选取代，旧顺序把 `详见下图：` 里的 `见下图：`
#    当引导语剥掉，只剩一个悬空的「详」字，图题被写成「图 3-1 各项内容详」
#    （实测 `各项内容详见下图：` → title=`各项内容详`）。把 `详见下图` 提到
#    最前即可整词剥离；检测语义（`search` + `$`）不变，删图回收引导语的判据
#    与改动前逐例等价（旧式本就靠子串匹配命中同一行）。
LEAD_IN_HINT_RE = re.compile(
    r"(?:详见下图|见下图|如下图示|如下图|如图|图示)\s*(?:所示)?\s*[:：。]?$")
# 保留私有名（既有本模块内 2 处消费 + 既有测试 import 零改动）。
_LEAD_IN_HINT_RE = LEAD_IN_HINT_RE
# 登记侧（_chart_pipeline）删除代码块时回收引导语的判据别名。
ORPHAN_LEAD_IN_RE = LEAD_IN_HINT_RE
# 私有常量别名（_chart_pipeline 既有 import 名保持不变）。
_LEAD_IN_MAX_CHARS = LEAD_IN_MAX_CHARS
# 引导语剥尾后的**残留连接词**：`详见下图` / `如图所示` 之外，AI 还会写
# 「具体详见下图」「各项内容详见下图」，剥掉引导语后可能剩一个悬空字
# （详 / 如 / 见 / 所 / 下 / 示 / 后）。这类单字不构成名词短语图题，
# 必须一并剥除，否则会沿用上面的残字缺陷。
_LEAD_IN_TAIL_RE = re.compile(
    r"(?:具体|详见|如下|如后|见下)?\s*(?:图|表)?\s*(?:所示|如下)?"
    r"(?:详|如|见|所|下|示|后)?\s*[:：。]?\s*$")
_MERMAID_TITLE_RES = (
    re.compile(r"^\s*%%\s*(?:图题|标题|title)\s*[:：]\s*(.+?)\s*$", re.M),
    re.compile(r"^\s*(?:title|图题)\s*[:：]?\s*(.+?)\s*$", re.M),
    re.compile(r"^\s*pie\s+title\s+(.+?)\s*$", re.M),
)
_ORDERED_MARKER_RES: tuple = (
    # （一） / (一) —— 中文括号数字（与「第X章 / 1.1」并列的层级标记）
    (re.compile(r"^[（(]([一二三四五六七八九十]+)[)）]\s*(\S.*)$"), "cn_num_paren"),
    # （1） / (1)
    (re.compile(r"^[（(](\d+)[)）]\s*(\S.*)$"), "num_paren_lr"),
    # 1、 —— 顿号（中文文档最常用）
    (re.compile(r"^(\d+)\s*、\s*(\S.*)$"), "num_dun"),
    # 1） —— 中文右括号
    (re.compile(r"^(\d+)\s*）\s*(\S.*)$"), "num_paren_r"),
    # 1) —— ASCII 右括号（无空格也识别）
    (re.compile(r"^(\d+)\s*\)\s*(\S.*)$"), "paren_ascii"),
    # 1. / 1) —— ASCII 且必须跟空格，避免 "3.14 是圆周率" 被误切成列表项
    (re.compile(r"^(\d+)\s*[.)]\s+(.+)$"), "ascii"),
)
_TABLE_CAPTION_RE = re.compile(
    r"^表\s*(?:[:：]\s*"
    r"|[0-9一二三四五六七八九十][0-9.\-–—]*[\s、.：:]+)"
    r"(\S.*)$")
# ✅ 2026-10-10（文档质量报告 修复6 · 格式问题7）：AI 常返回 HTML `<table>` 而非
#    GFM 表格，旧实现无法识别，整张表以裸文本（含 <table>/<tr>/<td> 标签）印进交付
#    DOCX。现把 HTML 表格解析为 GFM 行、复用既有 `type: table` 块与
#    `export._add_table_from_markup` 渲染路径（逐字节无损、不引入新依赖）：
#    源正文里仍是 HTML（审计侧 html_tag 仍会提示用户改源文），但成稿已是真实表格。
_HTML_TABLE_OPEN_RE = re.compile(r"^\s*<table\b", re.I)
_HTML_TABLE_CLOSE_RE = re.compile(r"</table\s*>", re.I)
_HTML_TR_RE = re.compile(r"<tr\b.*?</tr>", re.I | re.S)
_HTML_TD_RE = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", re.I | re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_NBSP_RE = re.compile(r"&nbsp;|&#160;", re.I)
_HTML_DEC_NUM_RE = re.compile(r"&#(\d+);")
_HTML_DEC_HEX_RE = re.compile(r"&#x([0-9a-fA-F]+);")
# ✅ 2026-10-05（D4 · 编号剥离分叉收口）：`_TITLE_NUM_STRIP_RES` 已删除 ——
#    标题编号剥离的唯一实现是 numbering.strip_outline_numbering（见 _strip_title_number）。


_RE_BOLD_WRAP = re.compile(r"^\*\*(.*?)\*\*$")
_RE_SENTENCE_TAIL_FULL = re.compile(r"[。！？.!?；;，,、]\s*$")
_RE_SENTENCE_TAIL = re.compile(r"[。！？；;，,]\s*$")
_RE_HEADING_NNN = re.compile(r"^(\d+\.\d+\.\d+)\s+([\u4e00-\u9fa5A-Za-z].*)$")
# ✅ 深层点分编号（≥4 段，2026-10-07）：AI 在正文里写 4~7 段点分伪标题
#    （如 7.3.3.1.1 材料计划）时旧实现只识别到 3 段 —— 更深的行被当普通段落，
#    既不参与编号规范化（父章节重排后前缀错位残留成稿）也不按标题样式渲染。
#    现按「段数 + 1」映射层级（N.N→3、N.N.N→4、N.N.N.N→5…，封顶 7）。
_RE_HEADING_DEEP = re.compile(r"^(\d+(?:\.\d+){3,6})\s+([\u4e00-\u9fa5A-Za-z].*)$")
_RE_HEADING_NN = re.compile(r"^(\d+\.\d+)\s+([\u4e00-\u9fa5A-Za-z].*)$")
_RE_HEADING_N = re.compile(r"^(\d+)\s+([\u4e00-\u9fa5A-Za-z].*)$")
# ✅ R117（2026-10-20 · 导出成稿取证 · 加粗中文枚举伪标题）：
#    模型高频输出整行加粗的中文枚举伪标题 —— ``**1）、工程基本信息**`` /
#    ``**（1）施工准备**``。编号后跟的是**顿号/右括号**，而 ``_RE_HEADING_N`` 系列
#    一律要求「编号 + 空格」，故全部落空 ⇒ 退化成普通段落：既保留源文的 ``1）、``
#    编号（与 ``_compute_subheading`` 产出的 L6 编号 ``1）、`` 直接撞号，实测同一节
#    出现 2 个以上 ``1）、``），又永不进目录。
#    判据刻意收紧为「**整行**被一对 ``**`` 包裹」（行尾多一个字符都不算 —— 那正是
#    ``**1. 吊装构件设计图**：明确…`` 这类「加粗列表项 + 冒号接正文」的形态）。
_RE_BOLD_ENUM_HEADING = re.compile(
    r"^\*\*\s*(?:\d+\s*[)）、]|[（(]\s*\d+\s*[)）])\s*、?\s*"
    r"(?P<title>[^*\n]{2,}?)\s*\*\*$")
# ✅ R117（2026-10-20 · 导出成稿取证 · markdown 围栏按 Markdown 解析）：
#    ```markdown / ```md / ```gfm 是「这段内容本身就是 Markdown」的**显式声明**，
#    不是代码。旧实现一律落到通用 code 分支 ⇒ 成稿出现一个 Normal 段（实测
#    53 runs / 33 个 w:br）内含 GFM 表格与 $W_{max}$ 原始 LaTeX：表格不渲染、
#    表号与真实表格重号、公式泄漏、子标题层级与编号全部消失。
#    ⚠️ 口径刻意**不含** ```text / ```plaintext —— 那是「纯文本代码块」，
#    既有契约（test_numbering_unification::test_fence_and_list_and_paragraph_untouched）
#    明确要求其中的 ``### …`` 不得被当成标题；把 text 纳入本集合会让围栏真正
#    失去「保护内容不被解析」的语义。
#    ✅ R119（2026-10-21 · TD-4 字数口径与 markdown 判定同源）：本元组不再是本模块
#    自造字面量，改为引用 ``fence_common.MARKDOWN_FENCE_LANGS`` 唯一事实源 ——
#    否则「导出侧按 markdown 递归解析」与「字数侧按 markdown 计入内部正文」两处
#    lang 列表会各自漂移（一处加 ```mdx``、另一处忘加 → 成稿渲染但字数仍漏计）。
#    保留模块内名 ``_MARKDOWN_FENCE_LANGS`` 不变，既有引用/测试导入点零改动。
_MARKDOWN_FENCE_LANGS = _MARKDOWN_FENCE_LANGS_SHARED
_RE_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)")
_RE_TABLE_SEP = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$")
# ✅ R99（2026-10-17 · 机器标记不得泄漏进交付文档）：
#    历史图表标记块 ``[CHART_TYPE: xxx]`` 的唯一判据。旧正则只认**半角方括号 +
#    半角冒号**，而中文语境里模型/用户极可能写成全角 ``【CHART_TYPE: xxx】`` 或
#    ``[CHART_TYPE：xxx]`` —— 实测（artifacts/exp94/probe_blocks.py 场景 A）该行
#    退化为普通段落，**机器标记原文被原样印进交付 DOCX**，且该图不进图表清单、
#    不占图号，等同「成稿出现内部标记 + 静默丢图」。现按「半角/全角皆可」收口。
_RE_CHART_TYPE_TAG = re.compile(r"^[\[【]\s*CHART_TYPE\s*[:：]\s*(\w+)\s*[\]】]")
#: 形似机器标记、但类型不合法的行（类型为空 / 含非 \w 字符 / 右括号不匹配）。
#: 此类行**绝不**按普通段落原样输出（那等于把内部标记印进成稿），改为整块跳过。
_RE_CHART_TYPE_TAG_LOOSE = re.compile(r"^[\[【]\s*CHART_TYPE\s*[:：]")


def chart_type_tags(text: str) -> set[str]:
    """正文内嵌的 ``[CHART_TYPE: x]`` 历史图表标记类型集合（**单一事实源**）。

    ``routers/charts.py::list_charts`` 的「落点」判定与本模块的块解析曾各写一份
    字面串（``f"[CHART_TYPE: {ct}]" in content`` vs ``_RE_CHART_TYPE_TAG``），
    两侧对全角/异常写法不一致 ⇒ 「清单显示已落位、导出却按普通段落处理」。
    现统一由本函数供给，半角与全角形态在两侧必然一致。
    """
    out: set[str] = set()
    for raw_line in str(text or "").splitlines():
        m = _RE_CHART_TYPE_TAG.match(raw_line.strip())
        if m:
            out.add(m.group(1))
    return out
_RE_QUOTE_PREFIX = re.compile(r"^\s*>\s?")
_RE_HR_LINE = re.compile(r"^([-*_])(\s*\1){2,}$")
_RE_UL_BULLET = re.compile(r"^[-*•·]\s+(.*)")
_RE_WS_RUN = re.compile(r"\s+")
_RE_INLINE_MD_NOISE = re.compile(r"[*_`#]")
_RE_TITLE_TAIL_PUNCT = re.compile(r"[\s、.。：:；;，,]+$")

# ============================================================
# 预编译正则（2026-09-27 性能轮）
# ------------------------------------------------------------
# 现象：`_parse_content_blocks` 是**导出与正文落库共用的最热路径**（每章必跑，
# 200 章方案 = 200 次），cProfile 显示 `re.__init__._compile` 单次解析被调用
# 278 次 —— 即下面这些**内联** `re.match(r"...", s)` 每次都要走一次
# 模块级正则缓存的字典查找。
# 实测 A/B（同机 timeit 20 万次）：
#     内联 re.match 0.871 us/op  vs  预编译 .match 0.285 us/op  → 3.06x
#     内联 re.sub   0.911 us/op  vs  预编译 .sub   0.516 us/op  → 1.77x
# 做法：把热路径上的字面量模式上提到模块级 `_RE_*` 预编译，
# **pattern 字符串逐字不变**（调用点只去掉 `re.` 前缀），行为完全等价。
# ============================================================


def _detect_plain_heading(line: str) -> tuple[int, str] | None:
    """检测纯文本编号子标题（AI 输出的 `2 标题` / `3.1 标题` / `**2 标题**` 格式）。

    处理两种常见场景：
      1. 裸编号标题：`2 法律法规依据`、`3.1 国家标准`
      2. 加粗编号标题：`**1 施工条件分析**`、`**2.1 深基坑开挖**`（先剥加粗再检测）

    与 Markdown heading (#) 和有序列表 (1. / 1、) 的区别：
      1. 编号后必须是**空格**（不是 .、、、)、) 等标点）
      2. 句末不能有句号/感叹号/问号（正文句子以标点结尾）
      3. 标题不含冒号（冒号接正文是列表项特征）
      4. 长度 ≤ 50 字（标题短正文长）

    Returns:
        (level, pure_title) 或 None
        level 语义同 Markdown heading 的井号数量：
          2  ← `2 标题`   （等价于 ## 标题）
          3  ← `2.1 标题` （等价于 ### 标题）
          4  ← `2.1.1 标题`（等价于 #### 标题）
    """
    s = line.strip()
    if not s:
        return None

    # ✅ 先剥离 Markdown 加粗 **xxx** 标记 —— AI 常用加粗包裹纯文本标题
    s_inside = _RE_BOLD_WRAP.sub(r"\1", s, count=1).strip()

    # 1. 句末标点排除（正文句子特征）。
    #    ✅ 修复（2026-09-19）：旧集合漏了全角分号/逗号 —— 工程文档大量列举条目
    #    以"；"收尾且恰带 "3.1.12" 式编号（如 "3.1.12 身份证复印件、照片；"），
    #    会被误提升为四级标题且标题下无正文（导出成稿目录错乱）。
    #    故补齐 ；;，,（顿号收尾同理为列举残留）。
    if _RE_SENTENCE_TAIL_FULL.search(s_inside):
        return None
    # 2. 冒号排除（"1 xxx：yyyy" — 列表项接正文）
    if "：" in s_inside or ":" in s_inside:
        return None
    # 3. 长度排除（标题短正文长）
    if len(s_inside) > 50:
        return None
    # 4. ✅ R117：整行加粗的中文枚举伪标题（**1）、工程基本信息** / **（1）施工准备**）
    #    —— 编号后是顿号/右括号而非空格，下面四条 N.N 判据必然全部落空。整行加粗
    #    + 枚举编号 + 短标题三条件同时成立才提升为二级标题，编号交给调用方的
    #    `_compute_subheading` 统一重排（源文的 `1）、` 前缀由 _strip_title_number 剥除）。
    m_enum = _RE_BOLD_ENUM_HEADING.match(s)
    if m_enum:
        return 2, m_enum.group("title").strip()

    # N.N.N 标题（优先级最高，先匹配）
    m = _RE_HEADING_NNN.match(s_inside)
    if m and _is_reasonable_heading_number(m.group(1)):
        return 4, m.group(2).strip()

    # N.N.N.N(.N…) 深层标题（≥4 段）：层级 = 段数 + 1，封顶 7
    m = _RE_HEADING_DEEP.match(s_inside)
    if m and _is_reasonable_heading_number(m.group(1)):
        return min(m.group(1).count(".") + 2, 7), m.group(2).strip()


    # N.N 标题
    m = _RE_HEADING_NN.match(s_inside)
    if m and _is_reasonable_heading_number(m.group(1)):
        return 3, m.group(2).strip()

    # N 标题
    m = _RE_HEADING_N.match(s_inside)
    if m and _is_reasonable_heading_number(m.group(1)):
        return 2, m.group(2).strip()

    return None


def _is_reasonable_heading_number(num_str: str) -> bool:
    """判断编号是否为合理的标题编号（每段 <= 99，防止年份/数量词误判）。

    ✅ BUG 修复：旧实现仅匹配数字格式，"2023 年完成" / "3D 打印" / "100 人团队"
    会被误判为 N 级标题，导致正文段落被提升为标题、目录结构错乱。
    章节编号通常不超过两位数（第99章已极罕见），故限制每段 <= 99。
    """
    if not num_str:
        return False
    for seg in num_str.split("."):
        try:
            n = int(seg)
        except ValueError:
            return False
        if n < 1 or n > 99:
            return False
    return True


def _match_ordered_item(stripped: str):
    """匹配行首有序列表标记，返回 ``(起始序号, 正文, 标记样式)`` 或 ``None``。"""
    for rx, marker in _ORDERED_MARKER_RES:
        m = rx.match(stripped)
        if not m:
            continue
        raw_num, text = m.group(1), (m.group(2) or "").strip()
        if not text:
            continue
        if marker == "cn_num_paren":
            num = _cn_pure_to_int(raw_num) or 1
        else:
            try:
                num = int(raw_num)
            except ValueError:
                num = 1
        return num, text, marker
    return None


def _looks_like_table_title(title: str) -> bool:
    """判断候选文本是否像表题（而非"表中/表所示"之类的正文引用）。"""
    t = (title or "").strip()
    if not t or len(t) > 40:
        return False
    # 句读符号 → 正文句子，不是表题。
    # ✅ 2026-10-17（文档质量报告 表号问题根因 A）：**放行顿号「、」** ——
    #    实测真实表题大量含顿号（「预埋件、预留孔洞允许偏差」「项目、允许偏差（mm）、
    #    检验方法」），旧实现把「、」与句末标点一视同仁地拒绝，导致这些表题**不被吸收**
    #    → 源文表题行残留为正文段 + 导出又自动生成表题 → 双表题、表号错位。
    #    顿号是中文名词短语的并列分隔符，不构成「这是正文句子」的信号；句末标点
    #    （。！？；）与逗号/冒号才指示完整句子，故仅保留后者的拦截。
    if any(ch in t for ch in "：:，,；;。？！?!…"):
        return False
    # "表中的数据…" / "表所列…" / "表如下…" 等正文指代（注意不含"上/下"，
    # 否则「下卧层承载力参数表」这类真实表名会被误排除）
    if t[0] in "中所如为内里":
        return False
    # ✅ 修复：谓语动词特征 → 正文句子（如「表3-1 列出了主要设备参数」）。
    # 真实表题是名词短语（"主要施工机械设备表"），不会含"了"字；
    # 命中这些特征说明上一段是无句末标点的正文句，绝不能从正文中删除。
    if "了" in t:
        return False
    if t[:2] in ("列出", "给出", "汇总", "统计", "展示", "对比", "说明", "如下"):
        return False
    return True


def _extract_table_caption(blocks: list[dict]) -> str:
    """吸收紧邻表格上方的「表 X-Y 表名」行作为表题，并把它从正文块中移除。

    ✅ 新增：GB/T 交付规范要求表格有「表 {章号}-{序号} 表名」题注且位于表格
    **上方**。GFM 表格本身无法携带题注，实际正文里表名通常写成表格上一行的
    独立段落（如"表3-1 主要施工机械设备表"）。此处只在形态高度可信时才吸收
    （必须以"表"+编号/冒号开头、短、无句读），避免误吞正文说明段落。
    """
    if not blocks:
        return ""
    prev = blocks[-1]
    if prev.get("type") != "paragraph":
        return ""
    text = str(prev.get("text") or "").strip()
    text = _RE_BOLD_WRAP.sub(r"\1", text).strip()  # 去 Markdown 加粗
    m = _TABLE_CAPTION_RE.match(text)
    if not m:
        return ""
    title = m.group(1).strip()
    if not _looks_like_table_title(title):
        return ""
    blocks.pop()  # 已升格为表题，正文中不再重复输出
    return title


#: 图题行「图 X-Y 题名」（与表题同构；编号由导出唯一生成）。
_FIGURE_CAPTION_RE = re.compile(
    r"^图\s*(?:[:：]\s*"
    r"|[0-9一二三四五六七八九十][0-9.\-–—]*[\s、.：:]+)"
    r"(\S.*)$")


def _looks_like_figure_title(title: str) -> bool:
    """判断候选文本是否像图题（名词短语，而非"如图所示"之类正文引用）。"""
    t = (title or "").strip()
    if not t or len(t) > 40:
        return False
    # 句读标点 → 正文句子，不是图题（顿号「、」放行，同表题口径）
    if any(ch in t for ch in "：:，,；;。？！?!…"):
        return False
    if t[0] in "中所如为内里":
        return False
    if "了" in t:
        return False
    if t[:2] in ("列出", "给出", "汇总", "统计", "展示", "对比", "说明", "如下", "所示"):
        return False
    return True


def _extract_figure_caption(blocks: list[dict]) -> str:
    """吸收紧邻图表块**上方**的「图 X-Y 题名」行作为该图图题，并移除该行。

    ✅ 2026-10-17（文档质量报告 图号问题）：实测交付文档出现两个「图 3-1」
    —— 一个由导出按章序号生成（「图 3-1 模板工程施工进度计划」），另一个是
    源文里 AI 自写的残留图题行（「图 3-1 机械设备管理流程」）。导出为图表
    统一编号，源文自写的图题行若不吸收，就会与导出图题**重号**。此处与表题
    同口径：图表块产出的**前一**段落若形似图题则升格为该图图题并移除该段落。
    """
    if not blocks:
        return ""
    prev = blocks[-1]
    if prev.get("type") != "paragraph":
        return ""
    text = _RE_BOLD_WRAP.sub(r"\1",
                            str(prev.get("text") or "").strip()).strip()
    m = _FIGURE_CAPTION_RE.match(text)
    if not m:
        return ""
    title = m.group(1).strip()
    if not _looks_like_figure_title(title):
        return ""
    blocks.pop()  # 已升格为图题，正文中不再重复输出
    return title


def _drop_dangling_figure_captions(blocks: list[dict]) -> list[dict]:
    """移除**无图可挂**的残留图题段落（"有题无图"）。

    ✅ 2026-10-17（文档质量报告 图号/图片问题）：AI 常在正文里只写一行
    「图 X-Y 题名」而不产出任何图表块（渲染失败或本就没生成），该行既不进
    图表清单也不占图号，却与导出为**真实图表**生成的同名图号**重号**，且成稿
    呈现"有题无图"。已挂图的图题在 `_extract_figure_caption` 吸收阶段移除；
    走到这里的都是**无图可挂**的残留，按"宁缺毋滥"（不给不存在的图留题）
    整段移除，并记 WARNING 供排障（绝不静默）。
    """
    if not blocks:
        return blocks
    kept: list[dict] = []
    for b in blocks:
        if b.get("type") == "paragraph":
            text = _RE_BOLD_WRAP.sub(r"\1",
                                    str(b.get("text") or "").strip()).strip()
            m = _FIGURE_CAPTION_RE.match(text)
            if m and _looks_like_figure_title(m.group(1).strip()):
                logger.warning(
                    '移除无图可挂的残留图题行（有题无图，不占图号）: %s',
                    text[:50])
                continue
        kept.append(b)
    return kept


def _strip_leading_table_number(text: str) -> str:
    """剥离文本开头 AI 自带的「表 X-Y」编号前缀（表题编号由导出唯一生成）。

    ✅ 2026-10-17（文档质量报告 表号问题根因 B）：实测 AI 把表题连同编号写进
    表格**首行首格**（如「表 6-1 木工班组劳动力配置计划」），导出自动表题取首行
    前 3 列拼接后前置统一编号 → 「表 6-3 表 6-1 …」叠写、且表头单元格重复出现
    表题文字。此处剥离首格自带的编号前缀；剥离后为空则保留原文（不制造空单元格）。
    """
    t = str(text or "").strip()
    if not t.startswith("表"):
        return t
    m = _TABLE_CAPTION_RE.match(t)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return t


def _table_cell_count(line: str) -> int:
    """GFM 表格行的单元格数（剥离首尾竖线后按 ``|`` 切分，含空单元格）。

    ✅ R145（2026-10-29）：供 `_split_table_block` 判「表头前短行」，与分隔行
    的列数比较；含空单元格（``| 项目 | |`` 计 2 格），与导出端
    ``export._split_row`` 的口径一致。
    """
    s = line.strip()
    if not s:
        return 0
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return len(s.split("|"))


def _table_row_has_content(line: str) -> bool:
    """GFM 表格行是否至少有一个非空单元格（区分空行与真表头带）。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return any(c.strip() for c in s.split("|"))


def _split_table_block(tbl_lines: list[str]) -> list[list[str]]:
    """把连续收集的 `|` 行块切分为规范序 ``[表头, 分隔行, *数据行]`` 表格段。

    ✅ R104（2026-10-18 · 导出乱码收口）：AI 生成的 GFM 表格有两种**实测坏形态**，
    旧判据（分隔行必须落在 ``tbl_lines[1]``）对它们**整块失效** → 管道语法原样印进
    交付 DOCX（用户报障「导出文档内容乱码」；生产取证 scheme_assistant.db
    sections rowid 140 / 227）：
      A. **分隔行前置**：``| --- |`` 写在表头行**之前**。GFM 规范序是
         「表头 → 分隔行 → 数据」，AI 把分隔行提到最前 ⇒ ``tbl_lines[1]`` 是中文
         表头而非分隔行 ⇒ ``sep_ok`` 为假 ⇒ 整表降级为裸文本段落。
      B. **多表粘连**：两张表之间**无空行**（上表末行紧接下表表头/分隔行）。
         所有 ``|`` 行被并为一个块，``tbl_lines[1]`` 同样不是分隔行 ⇒ 同样整块
         降级（rowid 140 的「模板选型配置表」+「允许偏差表」即此形态）。
    本函数按**分隔行**切分（GFM 语义：分隔行永远紧跟表头），把坏形态重排为
    规范序；**规范序块（分隔行在 index 1）产出与旧实现逐字节一致**。

    判据（保守，宁可不改，不 Fabrication）：
      · 块内无分隔行 → 返回 ``[]``（调用方按旧行为整块降级为段落）；
      · 分隔行在 index 0（前置形态）→ 表头取其**后一行**，重排为规范序；
      · 分隔行在 index k≥1（规范/粘连形态）→ 表头取其**前一行**；
      · 块内 ≥2 个分隔行（粘连）→ 每个分隔行各切一张表；
      · 表头缺失或自身是分隔行（如连续两个分隔行）→ 该段不成立；
      · **任一行未被任何表格段覆盖**（病态形态，如前导孤儿数据行）→ 整体返回
        ``[]`` 走旧降级路径，绝不半识别半 Fabrication（内容不丢，全部回落段落）。
    """
    n = len(tbl_lines)
    sep_idx = [k for k in range(n) if _RE_TABLE_SEP.match(tbl_lines[k])]
    if not sep_idx:
        return []
    segments: list[list[str]] = []
    covered = [False] * n
    for pos, s in enumerate(sep_idx):
        # 表头位置：前置形态取分隔行**后**一行；规范/粘连形态取分隔行**前**一行
        h = 1 if s == 0 else s - 1
        if h >= n or _RE_TABLE_SEP.match(tbl_lines[h]):
            continue  # 表头缺失 / 表头自身是分隔行 → 该段形态不可信
        data_start = h + 1 if s == 0 else s + 1
        # 数据区终点 = 下一张表表头的前一行（前置形态的下一表头在下一分隔行后）
        if pos + 1 < len(sep_idx):
            nxt = sep_idx[pos + 1]
            data_end = nxt + 1 if nxt == 0 else nxt - 1
        else:
            data_end = n
        if data_end < data_start:
            data_end = data_start
        segments.append([tbl_lines[h], tbl_lines[s], *tbl_lines[data_start:data_end]])
        covered[s] = True
        for k in range(h, data_end):
            covered[k] = True
    if segments and not all(covered):
        # ✅ R145（2026-10-29 · 导出后校验检出 Markdown 残留）：表头**前**的短行
        #    （单元格数 < 分隔行列数、至少一格有内容）是「表头带/列组标签」行，
        #    属于表格的一部分，不是孤儿数据行。旧判据「任一行未覆盖 → 整块拒绝」
        #    把它当病态形态 → 整张表降级为裸文本段落，管道语法/分隔行原样印进
        #    交付 DOCX（生产取证：精装修方案「抹灰参数」章表 4-1，首行
        #    ``| 项目 | 技术参数 |`` 2 列 + 表头 3 列）。现把这类短行前置进首个
        #    表段并标记覆盖；短行由导出端 `_add_table_from_markup` 按 gridSpan 合并。
        #    若仍不全部覆盖（真·孤儿行）→ 照旧整体拒绝（保守，宁可不改不 Fabrication）。
        s0 = sep_idx[0]
        if s0 > 0:
            sep_cols = _table_cell_count(tbl_lines[s0])
            preamble: list[str] = []
            for k in range(s0):
                if covered[k]:
                    continue
                if (_table_cell_count(tbl_lines[k]) < sep_cols
                        and _table_row_has_content(tbl_lines[k])):
                    preamble.append(tbl_lines[k])
                    covered[k] = True
            if preamble:
                segments[0] = preamble + segments[0]
    if not all(covered):
        # 病态形态：有行未构成任何表格段 → 整体不识别，交旧降级路径（宁可裸文本，
        # 不半识别——半识别会把孤儿行静默丢掉或 Fabrication 出一张结构错误的表）。
        return []
    return segments


def _normalize_table_first_cell(lines: list[str]) -> list[str]:
    """剥离表格首行首格自带的「表 X-Y」编号前缀（见 `_strip_leading_table_number`）。"""
    if not lines:
        return lines
    first = lines[0]
    if not first.lstrip().startswith("|"):
        return lines
    parts = first.split("|")
    if len(parts) < 3:
        return lines
    cell = parts[1].strip()
    new_cell = _strip_leading_table_number(cell)
    if new_cell == cell:
        return lines
    parts[1] = " " + new_cell + " "
    return ["|".join(parts)] + list(lines[1:])


def _html_unescape(text: str) -> str:
    """还原常见 HTML 实体（不引入 html 库，覆盖表格单元格实际会出现的形态）。"""
    t = _HTML_NBSP_RE.sub(" ", text)
    t = (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", "\"").replace("&#39;", "'").replace("&apos;", "'"))

    def _num(m: "re.Match") -> str:
        try:
            return chr(int(m.group(1)))
        except (ValueError, OverflowError):
            return m.group(0)

    t = _HTML_DEC_NUM_RE.sub(_num, t)
    t = _HTML_DEC_HEX_RE.sub(
        lambda m: (chr(int(m.group(1), 16))
                   if len(m.group(1)) <= 6 else m.group(0)),
        t)
    return t


def _html_cell_text(raw: str) -> str:
    """单元格原文 → 纯文本（去标签、还原实体、折叠空白）。"""
    t = _HTML_TAG_RE.sub("", raw)
    t = _html_unescape(t)
    return _RE_WS_RUN.sub(" ", t).strip()


def _html_table_to_gfm(html_text: str) -> list[str] | None:
    """把一段 HTML `<table>…</table>` 解析为 GFM 表格行列表。

    无法解析（无 `<tr>`/无单元格）返回 None，由调用方降级为纯文本段落。
    首行视为表头（与 GFM 表格语义、导出端首行加粗一致）；列数取各行最大值，
    短行右侧补空，保证 `_add_table_from_markup` 不越界。
    """
    trs = _HTML_TR_RE.findall(html_text)
    if not trs:
        return None
    rows: list[list[str]] = []
    for tr in trs:
        cells = _HTML_TD_RE.findall(tr)
        if not cells:
            continue
        rows.append([_html_cell_text(c) for c in cells])
    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    norm = [r + [""] * (ncols - len(r)) for r in rows]
    gfm = ["| " + " | ".join(norm[0]) + " |",
           "| " + " | ".join(["---"] * ncols) + " |"]
    gfm.extend("| " + " | ".join(r) + " |" for r in norm[1:])
    return gfm


def _clean_chart_title(raw, limit: int = 40) -> str:
    """图题清洗：折叠空白、限长（载荷 title 原样保留语义，不做激进裁剪）。"""
    return _RE_WS_RUN.sub("", str(raw or "").strip())[:limit]


def _title_candidate(raw: str) -> str:
    """引导语剥尾后的图题候选校验：长度 4~30、不含句读（否则不是名词短语图题）。"""
    t = _RE_WS_RUN.sub("", str(raw or "").strip())
    t = t.strip("“”\"'（）()【】[]：:。，,、;；-—")
    if not (4 <= len(t) <= 30):
        return ""
    if any(ch in t for ch in "。！？，；、：:！?"):
        return ""
    return t


def _mermaid_directive_title(code: str) -> str:
    """从 Mermaid 代码的 `title` / `%% 图题：` 指令抽取图题。"""
    if not code:
        return ""
    for rx in _MERMAID_TITLE_RES:
        m = rx.search(code)
        if m:
            cand = _title_candidate(m.group(1))
            if cand:
                return cand
    return ""


def _lead_in_title(blocks: list[dict]) -> str:
    """从紧邻图表块上方的引导语段落抽取图题（如「…如下图所示：」）。"""
    if not blocks:
        return ""
    prev = blocks[-1]
    if prev.get("type") != "paragraph":
        return ""
    text = _RE_BOLD_WRAP.sub(r"\1",
                            str(prev.get("text") or "").strip()).strip()
    if not text or len(text) > LEAD_IN_MAX_CHARS or not _LEAD_IN_HINT_RE.search(text):
        return ""
    return _title_candidate(_LEAD_IN_TAIL_RE.sub("", _LEAD_IN_HINT_RE.sub("", text)))


def _pop_orphan_lead_in_block(blocks: list[dict]) -> bool:
    """图表块在**解析期**被跳过时，回收紧邻其上方的孤儿引导语段落。

    ✅ 修复（2026-10-03 · 未闭合=不是图 · 三侧引导语回收口径补齐）：
    导出侧 ``export._pop_orphan_lead_in`` 只在「chart 块已产出、但渲染/插入被
    跳过」的分支触发回收；而围栏因**未闭合 / 类型不可识别 / JSON 不可渲染**
    在解析期就被 `continue` 跳过的，chart 块从未产出，write_section 无从得知
    它上方那句专为引出该图而写的引导语（"施工工艺流程如下图所示："）——
    成稿出现"见下图"却无图的悬空引用。此处按同一判据在解析期回收：
      · 末块是普通段落（标题/列表/表格/引用前的引导语绝不触碰）；
      · 文本 ≤60 字且以"如下图/见下图/如图所示"类引导语收尾。
    与 ``export._pop_orphan_lead_in``、``_chart_pipeline._drop_dangling_lead_in``
    同口径、同一目的：删图必须删引导语。
    """
    if not blocks:
        return False
    prev = blocks[-1]
    if prev.get("type") != "paragraph":
        return False
    text = _RE_BOLD_WRAP.sub(r"\1",
                            str(prev.get("text") or "").strip()).strip()
    if not text or len(text) > LEAD_IN_MAX_CHARS or not _LEAD_IN_HINT_RE.search(text):
        return False
    blocks.pop()
    logger.info("图表围栏被跳过：已同步移除其孤儿引导语「%s」", text[:30])
    return True


def _parse_content_blocks(content: str, *, _md_depth: int = 0,
                          _line_offset: int = 0) -> list[dict]:
    """解析正文内容为 block 列表。

    支持的 block 类型：
      heading（``#``~``######`` 与纯文本编号标题）/ paragraph / list_item /
      code / table（GFM）/ chart（mermaid 围栏、chart-json、``[CHART_TYPE: x]``）/
      image（AI 配图：``![说明](http...)``）/ quote（``>`` 引用）/
      hr（``---`` ``***`` ``___`` 分隔线）

    ``_md_depth`` 为内部递归深度（R117）：`` ```markdown `` 家族围栏会被**按
    Markdown 递归解析**，深度上限 2 层，防止畸形输入无限递归。

    ``_line_offset`` 为内部递归的行号基准（R118）：本层 ``content`` 的首行在
    **最外层原文**中的 0 基行号。递归（围栏内；/嵌套围栏）时必须传入，否则
    子块上的 ``src_line`` 会退回「围栏内相对行号」。

    ⚠️ R118（2026-10-21 · P0 数据丢失回归）为什么必须传：``src_line`` 的唯一
    消费方 ``numbering.renumber_section_body_subheadings`` 把它当作
    ``content.split("\n")`` 的索引**直接覆盖源行**。R117 引入递归时未做偏移换算，
    实测输入 `` ```markdown␊＃＃ 围栏内全角井号␊## 围栏内标题␊``` `` 时内层块
    src_line=1，外层 ``lines[1]`` 恰是全角井号行 ⇒ **该行被整行覆盖删除**
    （原文丢失），且围栏内标题被错误占号（真标题顺延成 2.2）。
    递归产出的块会额外打 ``_from_fence=True``，编号侧据此不占号（见 numbering 注释）。
    """
    if not content:
        return []
    blocks = []
    # ✅ R165（2026-11-01 · 4 空格缩进图表围栏不被识别）：AI 偶尔把图表围栏缩进
    #    4 个空格（列表项续行习惯）。围栏判据是全仓唯一事实源
    #    ``fence_common.FENCE_LINE_RE``（≤3 前导空白，对齐 CommonMark），4 空格因此
    #    **不是围栏** → 整块降级为普通段落，交付成稿里出现裸的 ```mermaid / JSON
    #    源码（实测探针 ``[indent4]``）。放宽判据风险更大（CommonMark 中 4 空格
    #    缩进本是代码块，会误判真代码样例）；改用「读取前把图表围栏去缩进」的
    #    窄口径归一：只动「语言属于图表家族、且外层无其它围栏包住」的行，
    #    行数/行序完全不变（``src_line`` 契约不受影响），幂等。
    if _INDENTED_FENCE_RE.search(content):
        content, _indent_log = normalize_chart_fence_indent(content)
        for _it in _indent_log:
            logger.info(
                "图表围栏缩进归一：第 %d 行（lang=%s，去掉 %d 个前导空白），"
                "避免源码泄漏进成稿", _it["line"], _it["lang"], _it["indent"])
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # ✅ D-8c 修复（2026-10-08 · BOM 不再阻塞首行标题）：Windows 记事本 /
        #    部分 OCR 输出常在文首带 U+FEFF，它**不属于空白字符**（str.strip()
        #    不剥），黏在 "#" 前会让首行标题被解析成普通段落 —— 后果：
        #    导出少渲染一级标题、正文子标题编号整体错位一格。BOM 是零宽标记、
        #    绝不是内容，故在**唯一解析入口**统一剥除行首 BOM（仅剥行首，
        #    不触碰行内数据），导出 / 预览 / 编号规范化三条链路同时受益。
        if stripped.startswith("\ufeff"):
            stripped = stripped.lstrip("\ufeff").strip()
        if not stripped:
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        # Markdown 标题 # ~ ######（允许最多 3 个前导空格，与 GFM 一致）
        m_h = _RE_MD_HEADING.match(stripped)
        if m_h:
            _h_text = m_h.group(2).strip()
            # ✅ 修复（2026-09-19）：AI 偶尔把正文句子写成井号标题
            #    （如 "#### 4 作业完成后清理现场，做到工完场清。"）——
            #    句末带句读标点的"标题"实为正文行，照常产出会变成
            #    "多级标题下无正文"。降级为普通段落。
            if _RE_SENTENCE_TAIL.search(_h_text):
                blocks.append({"type": "paragraph", "text": _h_text})
            else:
                blocks.append({
                    "type": "heading",
                    "level": len(m_h.group(1)),
                    "text": _h_text,
                    # ✅ 编号统一（2026-09-25）：源行号（0 基）—— 落库前正文子标题
                    #    编号规范化（services/numbering）据其精确重写源行；
                    #    渲染链路不消费该字段，纯增量信息。
                    # ✅ R118：必须是**全文绝对行号**（= _line_offset + 本层下标），
                    #    递归层要用 _line_offset 把围栏内相对下标抬起。
                    "src_line": _line_offset + i,
                })
            i += 1
            continue
        # ✅ 增强：纯文本编号子标题（AI 输出 `2 标题` / `3.1 标题` 格式）
        # 必须在 Markdown heading 之后、有序列表识别之前 —— 因为有序列表
        # 正则 `^(\d+)\s*[.)、]\s+` 不匹配"编号后是空格"的格式
        plain_h = _detect_plain_heading(line)
        if plain_h:
            lv, txt = plain_h
            blocks.append({"type": "heading", "level": lv, "text": txt,
                           # ✅ R118：同 # 标题分支，必须是全文绝对行号
                           "src_line": _line_offset + i})
            i += 1
            continue
        # 代码块 ```lang ... ``` / ~~~lang ... ~~~（图表三侧口径统一，2026-09-24 修复）
        # ✅ 2026-09-24 修复（导出侧幽灵图 + Mermaid 源码泄漏根因）：旧实现用
        #    `line.strip().startswith("```") + lang = line.strip()[3:]` 提取语言标签，
        #    与登记侧 parse_fence_line 的修复口径分叉：
        #      · 4 反引号围栏 ````mermaid → lang 变成 "`mermaid" → 不被识别为 mermaid
        #        → 落到通用 code 分支 → **Mermaid 源码原样印进交付 DOCX**（既不渲染成图、
        #          也不进图表清单/预检），正是本模块注释反复强调要杜绝的"幽灵图/三侧口径分叉"；
        #      · 波浪号围栏 ~~~mermaid 完全检测不到（startswith("```") 为 False）→
        #        围栏内容被当普通段落解析、源码泄漏进正文。
        #    现改用 _chart_pipeline.parse_fence_line（与登记/改写侧同源），按围栏字符/长度
        #    原样提取 lang 并透传给 read_fenced_block，三侧必然同口径。
        _pf = parse_fence_line(line)
        if _pf is not None:
            _open_char, _open_len, lang = _pf
            lang = lang.strip().lower()
            # ✅ R118：围栏**开启行**在本层 lines 中的下标（+1 即围栏内容首行）。
            #    递归解析围栏内部时必须以此换算出内容首行的全文绝对行号，
            #    否则子块 src_line 是围栏内相对行号（详见本函数 docstring 的 P0 说明）。
            _fence_open_i = i
            i += 1
            # ✅ 三侧口径统一（2026-09-23，导出幽灵图根因修复）：围栏读取改用与登记侧
            #    （_scan_inline_charts）、改写侧（_rewrite_code_block）同源的共用扫描器
            #    read_fenced_block。旧实现本函数缺少登记侧 2026-09-22 补入的"超长但闭合"
            #    有界前视恢复：一份 500<行数≤1000 且首尾围栏齐全的合法图表块，登记侧提取入库、
            #    导出侧却因超行数上限 break 后判为"未闭合"整块跳过 → 出现在图表清单/预览、
            #    却在导出 DOCX 里凭空消失（幽灵图）。共用扫描器后：closed/recovered 均视为闭合，
            #    正常解析渲染；eof/truncated 才走下方"以句读行还原正文"的未闭合降级。
            # ✅ R165（2026-11-01 · markdown 外层围栏包裹内嵌图表被整块丢弃）：
            #    markdown 家族围栏的闭合行必须**让内层围栏配平**才算闭合。旧实现
            #    用通用扫描器，外层 ```markdown 里第一个内层 ` ``` `（内嵌 mermaid
            #    的闭合行）被当成外层闭合 → 内嵌图表整块丢失（正文段落保住），
            #    且外层真正的闭合行反向变成「新开围栏」把其后正文吞进代码块。
            #    CommonMark 只规定「闭合序列不得带信息串」，不要求配平 —— 因此
            #    这不是解析器缺陷，而是「markdown 家族按正文递归解析」这一既有
            #    约定（R117）必须自带的内层感知。非 markdown 围栏仍走通用扫描器。
            _reader = (_read_markdown_fenced_block
                       if lang in _MARKDOWN_FENCE_LANGS else read_fenced_block)
            code_lines, _fence_state, i = _reader(
                lines, i, open_char=_open_char, open_len=_open_len)
            _fence_closed = _fence_state in ("closed", "recovered")
            # ✅ R117（2026-10-20 · 导出成稿取证）：`` ```markdown `` / ```md / ```text
            #    是「这段内容**本身就是 Markdown**」的显式声明 —— 必须按 Markdown
            #    解析，绝不能落到通用 code 分支原样印进成稿。
            #    实测（室外汽车吊吊装施工专项方案 第 1 轮导出）：模型把整段正文包进
            #    ```markdown（含 GFM 表格、$W_{max}$ 公式、`##### 3.1.3 …` 子标题），
            #    成稿里变成**一个 Normal 段落**（53 runs / 33 个 w:br）：
            #      · GFM 表格不渲染 → 与后续真实表格共用「表 1-1」编号（重号）；
            #      · LaTeX 以 ```$W_{max}$``` 原文泄漏（不走 OMML 转换）；
            #      · 子标题层级消失 → 该节 4 个子标题既无 Heading 样式也不进目录；
            #      · 源文 ``1.2.2 汽车吊选型与性能校核`` 与父章节 ``2.2`` 编号冲突。
            #    未闭合时按「已交出多少解析多少」处理（宁可少一节代码块，也不丢正文）。
            if lang in _MARKDOWN_FENCE_LANGS:
                if not _fence_closed:
                    logger.warning(
                        "markdown 围栏未闭合（lang=%s，state=%s，已消费 %d 行），"
                        "已按 Markdown 解析其内容，不当作代码块输出",
                        lang, _fence_state, len(code_lines))
                _inner_md = "\n".join(code_lines)
                # 围栏内容首行的**全文绝对行号**（+1 跳过开启行自身）
                _inner_offset = _line_offset + _fence_open_i + 1
                if _inner_md.strip():
                    if _md_depth >= 2:
                        logger.warning(
                            "markdown 围栏嵌套超过 2 层（lang=%s），余下内容按普通段落输出",
                            lang)
                        blocks.append({"type": "paragraph", "text": _inner_md,
                                       "_from_fence": True})
                    else:
                        _inner_blocks = _parse_content_blocks(
                            _inner_md, _md_depth=_md_depth + 1,
                            _line_offset=_inner_offset)
                        for _b in _inner_blocks:
                            # ✅ R118：围栏派生标记（含嵌套）。编号侧据此
                            #    **不占号、不重写源行** —— 见 numbering 侧注释。
                            _b["_from_fence"] = True
                        blocks.extend(_inner_blocks)
                continue
            if not _fence_closed:
                logger.warning('检测到未闭合的代码块（lang=%s，state=%s）', lang, _fence_state)
                # ✅ 修复（2026-09-19）：围栏被截断时，"消费至文档末尾"会把截断点
                #    之后的**正文段落**一并吞进残片（残片随后被跳过 → 正文静默丢失）。
                #    图表代码行（mermaid 语句 / JSON 片段）与代码行不会以中文句读收尾，
                #    故以第一个以"。！？"结尾的行作为正文起点，把其后的内容还原给
                #    段落解析，既不渲染残片、也不丢它后面的正文。对**所有**围栏语言生效；
                #    其中图表家族围栏仍需保留完整残片交给下游按"解析失败 → 跳过"处理
                #    （不截断 code_lines），普通代码围栏则只输出真正的代码行，不夹带正文。
                _cut = next((j for j, _cl in enumerate(code_lines)
                             if _cl.strip().endswith(("。", "！", "？"))), None)
                if _cut is not None:
                    _give_back = len(code_lines) - _cut
                    logger.warning(
                        "围栏未闭合（lang=%s），残片 %d 行已跳过，"
                        "其后 %d 行正文已还原为段落", lang, _cut, _give_back)
                    if lang.lower() not in _CHART_FENCE_LANGS:
                        code_lines = code_lines[:_cut]
                    i -= _give_back
                elif lang.lower() not in _CHART_FENCE_LANGS:
                    # ✅ BUG 修复（2026-10-14 · P1 未闭合围栏吞噬正文）：
                    #    旧实现只在**找到**句读行时才回退 code_lines；找不到时
                    #    （截断点之后是项目符号列表 / 表格 / 英文 / 无标点内容——
                    #    AI 生成被 max_tokens 截断时非常常见）code_lines 保持
                    #    "消费至文件末尾"的整段，随后落入通用 code 分支被**当成
                    #    代码块原样印进交付 DOCX**：正文以等宽字体输出、失去段落
                    #    缩进与标题层级，而用户端只有一条 logger.warning，
                    #    完全无从得知正文已被吞。
                    #    图表家族围栏本来就走"未闭合 → 跳过"（不产出图），普通围栏
                    #    无可渲染的代码语义；宁可不输出，也不把正文当代码印。
                    logger.warning(
                        "围栏未闭合（lang=%s）且其后无中文句读行，无法定位正文起点："
                        "已整块跳过 %d 行，避免把正文当代码块输出", lang, len(code_lines))
                    _pop_orphan_lead_in_block(blocks)
                    continue
            if lang.lower() == "mermaid":
                # ✅ 修复（2026-09-19）：未闭合围栏（实测由 max_tokens 截断产生，
                #    如 `flowchart LR` 只写到 `B --> C{`）不得产出图表块 ——
                #    残片必然渲染失败（红字占位），而落到通用 code 分支时更会把
                #    Mermaid 源码原样印进交付文档。此处直接整块跳过并告警，
                #    与登记侧 `_scan_inline_charts` 的"未闭合块不提取"口径一致。
                if not _fence_closed:
                    logger.warning(
                        "章节代码块未闭合（lang=mermaid，已消费 %d 行），"
                        "疑似生成被截断，已跳过（不渲染、不落代码）", len(code_lines))
                    _pop_orphan_lead_in_block(blocks)
                    continue
                code_text = "\n".join(code_lines).strip()
                # ✅ 修复 P0：首行为空行时 "".split() 为空列表，[0] 抛 IndexError 导致导出 500
                # ✅ 统一映射：detect_mermaid_chart_type 会跳过 `%%` 注释行再到首个
                #    有效关键字，登记侧（_chart_pipeline）用的是同一张表与同一套
                #    跳过规则，两端必然一致。
                #
                # ✅ 判据收敛（2026-09-30 · 三侧口径分叉 / 幽灵图）：本行此前写死
                #    ``default="flowchart"``，而登记侧（_chart_pipeline
                #    ._scan_chart_fences_full）与 charts.py 两处都用 ``default=""``。
                #    后果：**首关键字不在映射表内**的 mermaid 块（如 AI 写错关键字、
                #    出现映射表未覆盖的新图形）在本侧被当成 flowchart 产出 chart 块，
                #    在登记侧却因 `if ct:` 为假而**整块跳过** —— 于是
                #    「成稿里有一张要渲染的图、chart_predictions 里查无此图」，
                #    既不进图表清单/导出预检（用户无法定位、无法 AI 修复），
                #    又绕过「每章 ≤1 / 同类型全方案 ≤3」的配图上限（上限判定全在登记侧），
                #    即 AGENTS.md §4.3 记载的「幽灵图 / 限额失效」三重错配，
                #    且与紧邻的 chart-json 分支（下方 `_cj_type not in
                #    PIL_RENDERABLE_CHART_TYPES` → continue）自相矛盾。
                #    修法：与登记侧**同一 default、同一个跳过语义**（unknownDiagram
                #    等本就过不了 image_engine.validate_mermaid，早晚会被删块，
                #    提前跳过只是省掉一次注定失败的渲染，不改变交付结果）。
                chart_type = detect_mermaid_chart_type(code_text, default="")
                if not chart_type:
                    logger.info(
                        "导出跳过不可渲染的 mermaid 块（首关键字不在映射表内），"
                        "与登记侧同口径，避免幽灵图绕过配图上限")
                    _pop_orphan_lead_in_block(blocks)
                    continue
                # ✅ 图表同步生成：内联块自带代码，直接携带（不再依赖 chart_predictions 查码）
                # ✅ 图题优先级（v16，对齐图表需求规格「载荷 title > Mermaid title >
                #    引导语 > 类型通用名」）：Mermaid 语法块按 **Mermaid title 指令 >
                #    引导语** 取题；chart-json 块见下方分支（载荷 title > 引导语）。
                #    历史注：2026-09-19 曾反转为"引导语优先"（Mermaid title 常是通用词），
                #    与 _EXPORTER_VERSION v11 规格注释及需求规格相悖，现统一回规格口径；
                #    引导语在 Mermaid title 缺失时仍兜底。
                # ✅ 2026-10-17（文档质量报告 图号问题）：图题优先级补入**源文自写图题**
                #    —— 若图表块**上方**紧邻段落形似「图 X-Y 题名」，升格为本图图题并
                #    移除该段落（否则与导出统一生成的图号重号，实测「图 3-1」两处）。
                blocks.append({"type": "chart", "chart_type": chart_type,
                               "code": code_text, "inline": True,
                               "title": _extract_figure_caption(blocks)
                               or _mermaid_directive_title(code_text)
                               or _lead_in_title(blocks)})
            elif lang.lower() == "chart-json":
                # ✅ 图表同步生成：JSON 数据型图表块（labor/layout 等），
                # 类型取 JSON 的 "type" 字段，载荷交渲染引擎（PIL 为其唯一路径）
                # ✅ BUG 修复（渲染失败红字占位根因）：本分支此前对**畸形/未知类型**
                #    的 chart-json 块仍无条件产出 chart 块——JSON 解析失败时默认落到
                #    "labor"、type 为任意字符串时原样透传。而登记侧 _scan_inline_charts
                #    对同类块是「解析失败/类型不在白名单 → 跳过登记、也不删正文」。
                #    两侧口径分叉导致：这些块进不了图表清单、却在导出时被当图表渲染，
                #    渲染器对畸形 JSON / 未知类型必然返回 None → 写入
                #    「[图 x-y — 渲染失败]」红字占位（正是日志『内联 chart-json 解析
                #    失败，跳过』的下游后果）。现与登记侧同用渲染类型白名单收口：
                #    载荷不是合法 JSON 对象、或类型不属于 7 类可渲染图表 → 整块跳过
                #    （不占图号、不写红字，与 ai_image 未生成的跳过语义一致）。
                # ✅ 未闭合=不是图（2026-10-03 · 三侧口径收口）：本分支此前只看
                #    「JSON 可解析 + 类型白名单」而**不检查闭合状态**——EOF 截断残片
                #    若恰好是完整 JSON（闭合围栏行缺失不参与解析）照样被产出渲染、
                #    占用图号，与 mermaid 分支「未闭合一律跳过」及 read_fenced_block
                #    文档承诺「eof/truncated 走未闭合降级」相悖（登记侧现已同口径跳 eof）。
                if not _fence_closed:
                    logger.warning(
                        "章节代码块未闭合（lang=chart-json，state=%s），"
                        "疑似生成被截断，已跳过（不渲染、不落代码）", _fence_state)
                    _pop_orphan_lead_in_block(blocks)
                    continue
                code_text = "\n".join(code_lines).strip()
                _cj_obj = None
                _cj_type = ""
                try:
                    _cj_obj = json.loads(code_text)
                except (json.JSONDecodeError, TypeError, ValueError):
                    _cj_obj = None
                if isinstance(_cj_obj, dict):
                    # ✅ BUG 修复（2026-09-19）：旧实现无 "type" 键时**静默兜底 labor**，
                    #    而 labor 渲染器对非 labor 结构必然返回 None → 交付文档出现
                    #    「[图 X-Y 劳动力配置计划 — 渲染失败]」红字占位（图题还与实际
                    #    内容完全不符，实测第 4 章 3 处）。现与登记侧同口径：
                    #    按载荷**结构**确定性推断类型（root→架构 / steps→流程 /
                    #    phases+categories→劳动力 …），推断不出即视为不可渲染块跳过。
                    _cj_type = str(_cj_obj.get("type", "") or "").strip().lower()
                    if _cj_type not in PIL_RENDERABLE_CHART_TYPES:
                        _inferred = infer_chart_type_from_payload(_cj_obj)
                        if _inferred and _inferred != _cj_type:
                            logger.info(
                                "chart-json 的 type=%r 非法/缺失，按载荷结构推断为 %r",
                                _cj_type or "(无)", _inferred)
                        _cj_type = _inferred
                if not _cj_type or _cj_type not in PIL_RENDERABLE_CHART_TYPES:
                    logger.info(
                        "导出跳过不可渲染的 chart-json 块（type=%r），避免渲染失败占位",
                        _cj_type or "(非JSON对象)")
                    _pop_orphan_lead_in_block(blocks)
                    continue
                blocks.append({"type": "chart", "chart_type": _cj_type,
                               "code": code_text, "inline": True,
                               # ✅ 2026-10-17（文档质量报告 图号问题）：源文自写图题优先
                               #    （吸收图表块上方「图 X-Y 题名」行，避免图号重号）；
                               #    载荷 title 次之（AI 按本章语义撰写），缺失时退回引导语
                               "title": _extract_figure_caption(blocks)
                               or _clean_chart_title(_cj_obj.get("title"))
                               or _lead_in_title(blocks)})
            elif lang.lower() == "ai_image":
                # ✅ BUG 修复（2026-09-17）：``ai_image`` 占位块此前落到通用 code 分支 ——
                #    `{"prompt": "...", "style": "...", "title": "..."}` 被 `_add_code_block`
                #    原样印进交付文档（AI 的绘图提示词泄漏到成稿，且观感极差）。
                #    该围栏是「待生成态」（生成成功后正文会被改写为 ![title](url) → image 块），
                #    此处单独成块，由 write_section 决定降级方式（当前：跳过 + 告警）。
                # ✅ 未闭合=不是图（2026-10-03 · 与 mermaid/chart-json 同口径）：
                #    未闭合的占位块说明生成被截断，提示词 JSON 大概率残缺，跳过。
                if not _fence_closed:
                    logger.warning(
                        "章节代码块未闭合（lang=ai_image，state=%s），"
                        "疑似生成被截断，已跳过（不占图号）", _fence_state)
                    _pop_orphan_lead_in_block(blocks)
                    continue
                ai_title = ""
                try:
                    _ai_obj = json.loads("\n".join(code_lines).strip())
                    if isinstance(_ai_obj, dict):
                        ai_title = str(_ai_obj.get("title") or "").strip()
                except (json.JSONDecodeError, TypeError):
                    pass
                blocks.append({
                    "type": "ai_image",
                    "title": ai_title,
                    "code": "\n".join(code_lines).strip(),
                    "inline": True,
                })
            else:
                # ✅ BUG 修复（2026-09-22）：未闭合围栏经正文还原后可能已无真实代码行
                #    （残片全部落在被还原的正文里）——此时不产出空代码块，避免交付
                #    文档出现一个只有底纹、没有内容的空框。
                if code_lines:
                    blocks.append({"type": "code", "lang": lang, "lines": code_lines})
            continue
        # 表格块
        # ✅ 修复（2026-09-17）：旧实现要求每行都必须以 "|" 结尾，AI 生成的表格行
        #    常漏掉行尾竖线（`| a | b`），只要有一行不带尾竖线，**整张表**就从渲染中
        #    消失、降级为裸文本段落。现放宽为"以 | 开头即视为表格行"（单元格切分用
        #    strip("|")，尾竖线缺失不影响解析）；仅表头+分隔行（无数据行）也按表格渲染，
        #    不再静默降级。是否真为表格仍由分隔行正则兜底，不会误伤普通段落。
        # ✅ 修复（2026-09-20）：旧实现用未 strip 的原行判断 startswith("|")，AI 常把
        #    表格缩进挂在有序列表项下（"1. 验收内容" + 缩进的 | 行 |），整张表识别失败
        #    → 竖线源码原样印进交付文档；且分隔行以 "-" 开头还会被误判为无序列表。
        #    现与其他所有块类型口径一致，用 stripped 判断并存行。
        if stripped.startswith("|"):
            tbl_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl_lines.append(lines[i].strip())
                i += 1
            # ✅ R104（2026-10-18 · 导出乱码收口）：旧判据要求分隔行必须落在
            #    ``tbl_lines[1]``，对「分隔行前置」与「多表粘连（表间无空行）」两种
            #    实测坏形态整块失效 → 管道语法原样印进交付 DOCX（生产取证
            #    scheme_assistant.db sections rowid 140/227）。现按分隔行切分重排
            #    为规范序（见 _split_table_block）：规范序块与旧行为逐字节一致，
            #    坏形态重排为一张/多张规范表，切分失败才走旧降级路径（整块段落）。
            segments = _split_table_block(tbl_lines)
            if segments:
                if len(segments) > 1 or segments[0] != tbl_lines:
                    logger.info(
                        "表格块形态重排：源文为分隔行前置/多表粘连等非规范 GFM"
                        "（%d 行 → %d 张表），已按规范序重组渲染",
                        len(tbl_lines), len(segments))
                for seg in segments:
                    # ✅ 2026-10-17（文档质量报告 表号问题根因 B）：剥离首行首格 AI 自带的
                    #    「表 X-Y」编号前缀（表号由导出统一生成，否则「表 6-3 表 6-1 …」叠写）
                    table_block = {"type": "table",
                                   "lines": _normalize_table_first_cell(seg)}
                    # ✅ 新增：吸收紧邻表格上方的「表 X-Y 表名」行作为表题
                    caption = _extract_table_caption(blocks)
                    if caption:
                        table_block["caption"] = caption
                    blocks.append(table_block)
            else:
                for tl in tbl_lines:
                    blocks.append({"type": "paragraph", "text": tl})
            continue
        # ✅ 修复6（文档质量报告 · 格式问题7）：AI 常返回 HTML `<table>` 而非 GFM 表格。
        #    旧实现无法识别 → 整张表（含 <table>/<tr>/<td> 标签）以裸文本印进交付 DOCX。
        #    现把 HTML 表格解析为 GFM 行、产出与上方同款的 `type: table` 块，复用
        #    `export._add_table_from_markup` 渲染为真实 Word 表格（源正文仍保留 HTML，
        #    审计侧 html_tag 仍提示用户改源文，渲染侧则已无损转化）。
        if _HTML_TABLE_OPEN_RE.match(stripped):
            _html_lines = [line]
            _close_idx = None
            _k = i + 1
            while _k < len(lines):
                _html_lines.append(lines[_k])
                if _HTML_TABLE_CLOSE_RE.search(lines[_k]):
                    _close_idx = _k
                    break
                _k += 1
            if _close_idx is not None:
                _gfm = _html_table_to_gfm("\n".join(_html_lines))
                if _gfm:
                    # ✅ 2026-10-17（根因 B）：与 GFM 表格同口径剥离首格「表 X-Y」编号前缀
                    _tbl = {"type": "table",
                            "lines": _normalize_table_first_cell(_gfm)}
                    # ✅ 与 GFM 表格同口径：吸收紧邻上方的「表 X-Y 表名」行作表题
                    _cap = _extract_table_caption(blocks)
                    if _cap:
                        _tbl["caption"] = _cap
                    blocks.append(_tbl)
                else:
                    # 解析失败（无 <tr>/无单元格）→ 降级为纯文本段落，不静默丢表
                    for _hl in _html_lines:
                        blocks.append({"type": "paragraph", "text": _hl})
                i = _close_idx + 1
                continue
            # 未找到闭合标签 → 仅当前行当普通段落，继续推进（避免死循环/重复消费）
            blocks.append({"type": "paragraph", "text": line})
            i += 1
            continue
        # ✅ AI 配图（文生图，正文里的 Markdown 图片）：独占一行时按"图"渲染。
        #    与图表（mermaid/chart-json 代码）属不同模态：这里消费的是真实位图。
        #    下载失败仍降级为图题文本，不会在成稿里残留裸 Markdown 语法。
        m_img = _IMAGE_LINE_RE.match(stripped)
        if m_img:
            # ✅ 2026-10-17（文档质量报告 图号问题）：吸收图**上方**的「图 X-Y 题名」行
            #    作为该图图题（alt 缺失时兜底），避免源文自写图题与导出图号重号。
            _img_cap = _extract_figure_caption(blocks)
            blocks.append({
                "type": "image",
                "alt": (m_img.group("alt") or "").strip() or _img_cap,
                "url": m_img.group("url").strip(),
            })
            i += 1
            continue
        # 图表标记 [CHART_TYPE: xxx]（R99：半角/全角方括号与冒号皆可）
        _stripped = line.strip()
        m = _RE_CHART_TYPE_TAG.match(_stripped)
        if m:
            blocks.append({"type": "chart", "chart_type": m.group(1)})
            i += 1
            continue
        if _RE_CHART_TYPE_TAG_LOOSE.match(_stripped):
            # ✅ R99：形似机器标记但类型不合法（缺类型 / 类型含非法字符 / 右括号不匹配）。
            #    旧实现按普通段落原样输出 ⇒ 「【CHART_TYPE：】」这类内部标记直接印进
            #    交付 DOCX（AGENTS 反复登记的「机器痕迹泄漏进成稿」同族）。此处整块跳过：
            #    该行本就无图可画，用户从成稿里看到内部标记毫无价值，只会以为方案有瑕疵。
            logger.warning(
                '图表标记行格式异常（疑似机器标记，已整块跳过、不原样输出）: %s',
                _stripped[:80])
            _pop_orphan_lead_in_block(blocks)
            i += 1
            continue
        # 引用块 > ...（连续行合并为一个 block）
        if stripped.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(_RE_QUOTE_PREFIX.sub("", lines[i]).rstrip())
                i += 1
            text = "\n".join(quote_lines).strip()
            if text:
                blocks.append({"type": "quote", "text": text})
            continue
        # 分隔线 --- / *** / ___（同一种符号至少 3 个，可含空格）
        if _RE_HR_LINE.match(stripped):
            blocks.append({"type": "hr"})
            i += 1
            continue
        # 列表项
        # 无序列表: - * • ·
        m_ul = _RE_UL_BULLET.match(stripped)
        if m_ul:
            blocks.append({
                "type": "list_item",
                "ordered": False,
                "text": m_ul.group(1).strip(),
                "indent": indent,
            })
            i += 1
            continue
        # 有序列表: 1. / 1) / 1、 / 1） / （1） / （一）
        # ✅ 优化：统一交由 _match_ordered_item 识别（含中文枚举符无空格、
        #    「（一）」「（1）」等工程文档常用层级标记），并记录标记样式，
        #    导出时按原样式渲染连续序号。
        matched = _match_ordered_item(stripped)
        if matched:
            num, text, marker = matched
            blocks.append({
                "type": "list_item",
                "ordered": True,
                "num": num,
                "text": text,
                "indent": indent,
                "marker": marker,
            })
            i += 1
            continue
        blocks.append({"type": "paragraph", "text": line})
        i += 1
    # ✅ 2026-10-17（文档质量报告 图号问题）：收尾清理「有题无图」的残留图题段落 ——
    #    源文自写的「图 X-Y 题名」若无对应图表块（渲染失败/压根未生成），它既不进
    #    图表清单也不占图号，却与导出为真实图表生成的图号**重号**，且成稿呈现「有题
    #    无图」。已挂图的图题在解析期被吸收；此处只删无图可挂的残留（绝不静默：记 WARNING）。
    return _drop_dangling_figure_captions(blocks)


def _norm_heading_text(text: str) -> str:
    """标题归一化：剥离 Markdown 标记 / 编号前缀 / 全部空白 / 首尾标点，用于查重。"""
    s = str(text or "").strip()
    for _ in range(3):  # 最多剥 3 层（如 "# 1.1 标题"）
        before = s
        for rx in _HEADING_NUM_PREFIX_RES:
            s = rx.sub("", s, count=1)
        s = s.strip()
        if s == before:
            break
    s = _RE_INLINE_MD_NOISE.sub("", s)
    s = _RE_TITLE_TAIL_PUNCT.sub("", s)
    return _RE_WS_RUN.sub("", s)


def _strip_duplicate_leading_title(blocks: list[dict], *titles: str) -> list[dict]:
    """删除正文开头与章节标题重复的块。

    ✅ BUG 修复：AI 偶尔在正文首行再次输出章节标题（如"施工总体顺序与流程规划"），
    导出时与 write_section 写入的编号标题（"1 施工总体顺序与流程规划"）连成两行，
    文档出现"标题重复"。此处仅剥离【开头连续】的重复标题块，
    不影响正文其余小标题与列表内容。
    """
    targets = {_norm_heading_text(t) for t in titles}
    targets.discard("")
    if not targets:
        return blocks
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.get("type") not in ("heading", "paragraph"):
            break
        if _norm_heading_text(b.get("text", "")) not in targets:
            break
        i += 1
    return blocks[i:]


def _strip_title_number(title: str) -> str:
    """从章节标题中剥离编号前缀（唯一实现 = numbering.strip_outline_numbering）。

    DB 中的 title 可能已经带了 AI 生成的编号（如 "第一章 工程概况"、"1.1 项目基本信息"），
    而 export.py 又会用 heading_gen 重新生成标准编号 —— 必须先剥离再生成，
    否则会出现 "第一章 第一章 工程概况" / "1 1.1 项目基本信息" 等双重编号。

    ✅ 2026-10-16（编号剥离收敛）：改为委托 numbering.strip_outline_numbering_all
    （唯一有界循环实现），删除本地循环——此前本函数与 numbering.py 各自维护一份
    循环逻辑，属同族「同一判据多份副本」反模式。行为逐字不变。
    """
    return strip_outline_numbering_all(str(title or ""))


def _cn_pure_to_int(cn: str) -> int | None:
    """纯中文数字解析（"一"~"九千九百九十九"）。解析失败返回 None。"""
    d = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
         "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000}
    if not cn:
        return None
    total = 0
    cur = 0
    i = 0
    n = len(cn)
    while i < n:
        c = cn[i]
        if c in d:
            cur = d[c]
            # "零" 通常表示中间留空（如"一百零五"），cur=0 时不加入
        elif c in units:
            u = units[c]
            # "十X" 隐含 "一十X"（如 "十一" = 11）
            if cur == 0:
                cur = 1
            total += cur * u
            cur = 0
        else:
            return None
        i += 1
    total += cur
    return total if total > 0 else None


def _heading_punct(level: int) -> str:
    """返回指定层级的编号后缀分隔符（与 HEADING_STYLE_CONFIG 单一事实源一致）。

    用于正文子标题编号：L2~L4 用空格、L5~L7 用顿号，与导出行级标题（format_heading）
    严格同口径，杜绝「L5 顿号缺失」「L6/L7 缺顿号或右括号」等格式漂移。
    """
    return HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3]).get("punctuation", " ")


# ✅ R137（2026-10-28 · 正文子标题字母前缀非幂等）：
#    E3 降级模式（本节有 DB 子章节）下，rel≥1 的子标题由 _compute_subheading 产出
#    "a、标题"（L7 形态）。该形态**不在** numbering._STRIP_NUMBER_RE 的任何分支内
#    （那里只覆盖 第X章 / （三） / 1） / 点分路径 / 单段数字 / 中文数字），于是落库
#    正文里这一层字母前缀永远剥不掉 —— 下一次规范化把 "a、关键节点" 当纯标题再套
#    一层字母，实测 4 轮逐层膨胀（a、关键节点 → a、a、关键节点 → a、a、a、关键节点
#    → 四层），且模式切回非降级后残留 "2.1.1 a、关键节点"。任何结构变更都会跑
#    _renormalize_all_section_contents，故这是**持续污染**（正文不可逆膨胀）。
#    收口点选在 _compute_subheading 内部：它是 numbering 落库、export 预检、export
#    写稿三处的唯一算法本体，一处修复即三处同源，不必各自补剥。
# ✅ 2026-10-31（目录编号与等级修正）：除本模块产出的 "a、" 之外，AI 原文常自带
#    "a）" / "a)" 枚举前缀（实测正文 "## a、a）材料与环保控制参数" —— 旧实现只剥
#    "a、" 后剩 "a）…" 再拼回 "a、" ⇒ 双重编号被落库固化）。分隔符字符类扩到
#    「顿号 / 半角右括号 / 全角右括号」，与 numbering._STRIP_NUMBER_RE 的字母分支同口径。
_LETTER_PREFIX_RE = re.compile(r"^\s*[a-z]{1,2}\s*[、)）]\s*")


def _strip_body_letter_prefix(text: str) -> str:
    """剥掉正文子标题开头由本模块自己产出的字母前缀（"a、" / "az、"）。

    - 有界 8 轮：既保证幂等，也能自愈历史上已膨胀的多层前缀；
    - 剥空则保留原文：标题本身只剩 "a、" 时不能变成空标题；
    - 只认 a-z（= numbering.ALPHABET 的字符集），中文 / 数字前缀一概不动
      （那由 numbering._STRIP_NUMBER_RE 负责，避免与之重复或冲突）。
    """
    s = str(text or "")
    if not s:
        return s
    for _ in range(8):
        new = _LETTER_PREFIX_RE.sub("", s, count=1).strip()
        if not new or new == s:
            return new or s
        s = new
    return s


def _compute_subheading(section_prefix: str, section_level: int,
                        md_lv: int, sub_counters: dict, pure: str,
                        sec_id: str = "", has_children: bool = False):
    """计算正文内 Markdown 子标题的编号文本与 Heading 样式。

    sub_counters 为可变 dict（跨多次调用保持状态），记录本节的编号状态：
      - ``base``：本节正文中出现过的**最浅** Markdown 标题层级（首个标题即基准）；
      - ``c``：{相对深度 rel -> 计数}，rel = md 层级 - base。

    例：章节 "1.2 进度计划"（section_level=2, section_prefix='1.2'）内：
        '## 总体安排'  -> ('1.2.1 总体安排', Heading 3)
        '### 关键节点' -> ('1.2.1.1 关键节点', Heading 4)
        '## 资源配置'  -> ('1.2.2 资源配置', Heading 3)

    ✅ BUG 修复（2026-09-20，第 8 轮交付文档取证）：AI 正文的标题层级经常
    不齐——同一逻辑层级混用 ##/###/####，且会「先深后浅」回跳。旧实现按
    **原始 md 层级**直接计数，产生两类必然出现的坏编号（实测脚手架专项方案
    第 4 章「构造节点与连墙件做法」，425 个正文子标题）：

      1. **重复编号**：`### 剪刀撑设置` 已占用 "2.1"，随后回跳的
         `## 连墙件应` 又重新从 "2.1" 起算 → 成稿出现两个 "2.1"；
         其后 `### 施工质量控制` 又得 "2.1.1"，与更早的 "2.1.1 剪刀撑设置" 撞号。
      2. **0 段虚编号**：`## ` 回跳后计数器被清零，`#### ` 再深入时
         中间段取到 0 → "2.1.0.1 扣件紧固力矩控制"。

    现改为**相对深度 + 单调收敛**算法：
      - 以本节最浅标题层级为基准，所有标题按「相对深度」计数；
      - 出现比基准更浅的标题 → 视为与基准同级，**续接基准层的兄弟序列**
        （2.1 之后回跳的 ## 得 "2.2"，而不是重启为 "2.1"）；
      - 出现比上一标题深超过 1 级的跳跃 → 收敛到「上一级 + 1」
        （## 之后直接 #### 得 "2.2.1"，而不是 "2.2.1.0.1" 或与兄弟撞号）。
    由此编号序列严格递增、无 0 段、无重复，且与 Heading 样式深度一致。

    ✅ E3 降级（2026-09-25）：has_children=True 时，正文子标题改走节内 body 命名空间——
    第一层 rel=0 → ``N） 标题``，更深 rel≥1 → ``字母、 标题``。
    这样正文子标题与 DB 子章节（X.X）在同一文档内使用两个独立命名空间，不再出现
    「1.1 正文标题」与「1.1 DB 子章节」成对撞号。

    ✅ 层级修正（2026-10-31）：降级只换**编号命名空间**，不再把标题压到 L6/L7。
    旧实现固定 style=6/7，导出稿导航窗格/目录出现 H2 → H6 断级跳跃（实测
    「1 技术参数」下的正文子标题被顶到第 6 级缩进）。现按「父级 + 相对深度」
    与未降级路径同一套深度规则（min(section_level+1+rel, 7)），编号文本仍是
    ``1）/ a、``，命名空间隔离不变。
    """
    try:
        md_lv = max(1, min(int(md_lv or 1), 6))
    except (TypeError, ValueError):
        md_lv = 1

    # ✅ R137：先剥掉「上一轮本函数自己产出的字母前缀」再参与编号 —— 保证
    #    降级模式幂等（a、关键节点 再跑一轮仍得 a、关键节点），并让模式切换
    #    （降级 → 非降级）不残留字母。详见 _strip_body_letter_prefix 说明。
    pure = _strip_body_letter_prefix(pure)

    counts: dict[int, int] = sub_counters.setdefault("c", {})
    if "base" not in sub_counters:
        sub_counters["base"] = md_lv
    base = sub_counters["base"]

    # 更浅的标题回跳 → 与基准层同级，续接兄弟序列（旧实现重启计数 → 重复编号）
    if md_lv < base:
        logger.warning(
            "正文子标题层级不齐（section=%s）：基准层级 H%d 之后出现更浅的 H%d 标题 \"%s\"，"
            "已按基准层同级续排（避免与本节已编号的子标题重复）。"
            "建议在正文中统一同级标题的 # 数量。",
            sec_id or "?", base, md_lv, pure or "(空)")
        md_lv = base

    # 深度跳跃收敛：不得超过「已有最深非零层级 + 1」，避免中间出现 0 段
    deepest = max([d for d, n in counts.items() if n > 0], default=-1)
    rel = max(0, min(md_lv - base, deepest + 1))

    counts[rel] = counts.get(rel, 0) + 1
    for d in range(rel + 1, 7):
        counts[d] = 0
    if not pure:
        # 降级空标题时样式与有文本时一致：均为「父级 + 相对深度」
        return "", min(section_level + 1 + rel, 7)

    # ✅ E3：has_children=True → 降级为节内 body 命名空间
    if has_children:
        from app.services.numbering import ALPHABET
        if rel == 0:
            # 第一层：数字+）（1）/ 2）/ …），Heading L6。
            # rel=0 计数器沿既有的 counts[0] 递增（相对深度 0 的兄弟序列各自占一个数字）。
            # ✅ 编号统一（2026-09-26）：分隔符与 HEADING_STYLE_CONFIG[6] 单一事实源一致
            #    （顿号），产出 "1）、标题"，与导出行级 L6 标题（format_heading）严格同口径，
            #    修复「L6 缺顿号 / 多空格」的偏差。
            num = counts[rel]
            text = f"{num}）{_heading_punct(6)}{pure}"
        else:
            # 更深层：字母+、（a、/ b、/ …）。
            # ✅ 所有 rel≥1 共享同一个字母计数器——避免 rel=1 与 rel=2 各自从 a、
            # 起算而撞号。例如 rel1→a、、rel1→b、、rel2→c、… 单调递增。
            l7 = sub_counters.setdefault("l7_count", 0)
            l7 += 1
            sub_counters["l7_count"] = l7
            idx = min(l7 - 1, len(ALPHABET) - 1)
            letter = ALPHABET[max(idx, 0)]
            # ✅ 编号统一（2026-09-26）：分隔符与 HEADING_STYLE_CONFIG[7] 一致（顿号、无空格），
            #    产出 "a、标题"，与导出行级 L7 标题同口径，修复「L7 多空格」偏差。
            text = f"{letter}{_heading_punct(7)}{pure}"
        # ✅ 层级修正（2026-10-31）：降级只换编号命名空间，Heading 深度与未降级
        #    路径同规则（父级 + 相对深度）——旧实现固定 6/7，导航窗格/目录断级。
        return text, min(section_level + 1 + rel, 7)

    # Heading 样式跟随**实际编号深度**（而非原始 md 层级），避免 "2.2.1" 却用 H6
    style = min(section_level + 1 + rel, 7)
    # ✅ 编号统一（2026-09-26）：分隔符与 HEADING_STYLE_CONFIG[style] 单一事实源一致
    #    （L2~L4 空格、L5+ 顿号）——保证正文子标题与导出行级标题同口径，
    #    4 段编号（L5 等价）也带顿号，消除「L5 顿号缺失」偏差。
    sep = _heading_punct(style)
    # 相对深度 0..rel 的计数（单调收敛保证此处每段均 > 0）
    dotted = ".".join(str(counts.get(d, 0)) for d in range(0, rel + 1))
    text = f"{section_prefix}.{dotted}{sep}{pure}" if section_prefix else f"{dotted}{sep}{pure}"
    return text, style


def number_body_subheadings(blocks: list[dict], *, section_prefix: str,
                            section_level: int, sec_id: str = "",
                            has_children: bool = False,
                            with_style: bool = False) -> dict:
    """按统一算法为本节正文的 heading 块写入编号文本（``_fixed_text``）。

    ✅ R138（R137-O 报告 L-3 收口）：本循环此前在**三处各自实现**并各自拼装
    参数 —— ``numbering.renumber_section_body_subheadings``（落库规范化）、
    ``export._detect_duplicate_sections``（导出预检）、``export.write_section``
    （导出写稿）。三者共用 ``_compute_subheading``，但「谁传 has_children、谁传
    sec_id、谁写 _heading_style」全靠人工保持一致：任一处漏传 ``has_children``
    就会让「前端预览 = 落库正文 = 导出成稿」三处同源契约分叉（预检按点分算、
    渲染按降级算，实测会报出成稿里根本不存在的重复编号）。现收口为单一
    helper：循环体与参数拼装只此一份。

    行为等价约束（务必保持）：
    - 只处理 ``type == "heading"`` 的块，其余原样跳过；**不**做围栏过滤
      （围栏派生块的取舍统一走 :func:`select_numbering_blocks` —— 三处调用方
      都传它选出的子集，R149 起导出两侧不再传全量）；
    - ``sub_counters`` 由本函数内新建：每次调用即「本节」作用域；
    - ``with_style=False`` 时不写 ``_heading_style``（落库与预检不需要该键）。

    Args:
        blocks: 已解析的正文块列表（原地写入 ``_fixed_text``）。
        section_prefix: 本节展示态编号前缀（``_section_number_prefix`` 结果）。
        section_level: 本节层级（仅影响 Heading 样式深度）。
        sec_id: 本节 ID，**仅用于日志定位**（层级不齐告警）。不传时告警显示 "?"。
        has_children: 是否走 E3 降级（节内 body 命名空间 1）/ a、）。
        with_style: 是否同时回写 ``_heading_style``（仅导出写稿需要）。

    Returns:
        sub_counters（调用方后续如需可续用；当前三处均不再使用）。
    """
    sub_counters: dict = {}
    for block in blocks:
        if block.get("type") != "heading":
            continue
        text, style = _compute_subheading(
            section_prefix, section_level, block.get("level", 1), sub_counters,
            _strip_title_number(block.get("text", "")), sec_id,
            has_children=has_children)
        block["_fixed_text"] = text
        if with_style:
            block["_heading_style"] = style
    return sub_counters


def select_numbering_blocks(blocks: list) -> list:
    """编号宇宙块选择唯一实现：``heading`` 且非围栏派生（``_from_fence``）。

    ✅ R149（2026-10-31 · 三侧同源的最后一块）：R138 收口了循环体，但
    「传哪些块」仍由调用方各自决定 —— 落库侧（numbering）排除围栏派生标题
    （D-8g 契约：围栏内内容一概不动、不占号），而导出预检 / 导出写稿两处传
    **全量** blocks ⇒ 围栏派生标题在成稿里占号、在预览/落库不占号，同一份
    正文两侧编号错位（真实形态：`` ```markdown `` 围栏内含 ``## 子标题``，
    成稿「资源配置 2.3」vs 预览「资源配置 2.2」）。现三处共用本选择器，
    导出两侧与落库侧对齐：围栏派生标题在成稿保留原文、不占号（与
    ``numbering.renumber_section_body_subheadings`` 的 D-8g 口径一致）。
    """
    return [b for b in blocks
            if b.get("type") == "heading" and not b.get("_from_fence")]


# ✅ R76（2026-10-13）：_FENCE_LINE_RE / _CJK_TAIL_RE 两份本地副本已删除，
#    闭合判据与「疑似正文」判据改由顶部 app.services.fence_common 提供（别名
#    _fence_is_close 与 _looks_like_prose）。留下本地字面量不仅重复，还会**遮蔽**
#    同名导入（导入在前、赋值在后 → 委托失效），属本仓「同一判据两份取值」高危形态。


def parse_fence_line(line: str) -> tuple[str, int, str] | None:
    """解析一行是否为代码围栏。

    返回 ``(围栏字符, 围栏长度, 语言标签)``；非围栏行返回 None。

    ✅ BUG 修复（2026-09-24 · P0 幽灵图 + Mermaid 源码泄漏进成稿）：
    登记/改写/导出三侧此前一律用 ``stripped[3:]`` 取语言标签。对 **4 个及以上
    反引号**开头的围栏（````mermaid），第 4 个反引号会被算进标签，得到
    ``lang="`mermaid"``：

      · 登记侧 lang 既不是 "mermaid" 也不是 "chart-json"/"ai_image" → 该块
        **从不登记**进 chart_predictions（图表清单 / 导出预检 / AI 修复都查不到，
        即反复出现的"幽灵图"）；
      · 改写侧同样匹配不到 → 每章≤1、全方案同类型上限、"校验失败删块"对该类块
        全部失效（只要把围栏多写一个反引号就能绕过全部程序级配图上限）；
      · 导出侧 lang 不匹配 → 落到通用 code 分支，把 **Mermaid/JSON 源码原样印进
        交付 DOCX**（而不是渲染成图）。

    同时围栏长度必须原样返回：CommonMark 规定闭围栏长度不小于开围栏，
    改写代码块时若把开围栏统一写回 3 个反引号，4 反引号围栏会被降级 →
    正文出现新的未闭合围栏、其后正文被整段吞进代码块。

    长度容错（不要求 ≥ 开围栏）不在本函数、而在闭合判定
    ``fence_common.is_fence_close``；信息串维度的口径已于 R76 收紧 —— 带标签的
    围栏行是**内容**，不得当闭合（旧实现把带标签行当闭合，实测在块内提前闭合后
    让下一个裸围栏行变成新开围栏、把其后正文整段吞进代码块，见 BUG-F2）。
    """
    return _fence_parse_line(line)


def read_fenced_block(lines: list[str], body_start: int,
                      max_lines: int = MAX_INLINE_CODE_BLOCK_LINES,
                      open_char: str = "`", open_len: int = 3,
                      ) -> tuple[list[str], str, int]:
    """从开围栏行的下一行 ``body_start`` 起读取一个代码围栏块 —— 图表三侧共用唯一扫描器。

    ✅ 根因修复（2026-09-23，超长闭合围栏幽灵图）：登记侧 `_scan_inline_charts` 在
    2026-09-22 引入了"超长但闭合"的有界前视恢复，而导出侧 `export._parse_content_blocks`
    与改写侧 `_rewrite_code_block` 未同步该恢复 —— 一份 500<行数≤1000 且首尾围栏齐全的
    合法图表块会被登记侧提取入库、却被导出侧判为"未闭合"跳过 → 出现在图表清单/预览、
    却在导出 DOCX 里凭空消失（正是本模块注释里反复强调要杜绝的"幽灵图/三侧口径分叉"）。
    现把围栏读取的**全部判定逻辑**收敛到本函数，登记/改写/导出三侧共用同一实现，
    从结构上杜绝再次分叉。

    返回 ``(code_lines, state, next_index)``，``state`` 取值：
      - ``"closed"``    ：常规闭合（命中结束围栏）；``next_index`` 指向结束围栏的下一行。
      - ``"recovered"`` ：超上限但闭合 —— 结束围栏位于再 ``max_lines`` 行之内、且块内未
                          混入中文句读正文行（有界前视恢复），按合法块交出；
                          ``next_index`` 指向恢复到的结束围栏的下一行。
      - ``"eof"``       ：到达正文末尾仍未见结束围栏（把剩余行作为块内容交出）。
      - ``"truncated"`` ：超上限且前视窗口内无干净闭合 —— 判为未闭合；
                          ``code_lines`` 为已消费的截断内容，``next_index`` 停在截断点，
                          由调用方决定如何处理其后正文（导出侧据此还原段落）。
    """
    code_lines: list[str] = []
    i = body_start
    while i < len(lines):
        if _fence_is_close(lines[i], open_char, open_len, tolerant_length=True):
            # 命中闭合围栏：同种字符 + **信息串必须为空**（R76 BUG-F2）+ 长度容错
            break
        code_lines.append(lines[i])
        i += 1
        if len(code_lines) > max_lines:
            # 命中行数上限：做**有界前视**（再扫最多 max_lines 行）寻找结束围栏。
            _j = i
            _extra = 0
            while (_j < len(lines)
                   and not _fence_is_close(lines[_j], open_char, open_len,
                                           tolerant_length=True)
                   and _extra <= max_lines):
                _j += 1
                _extra += 1
            if _j < len(lines) and _fence_is_close(lines[_j], open_char, open_len,
                                                   tolerant_length=True):
                _extended = code_lines + lines[i:_j]
                # 防误吞守卫：图表代码行（Mermaid 语句 / JSON）不会以中文句读收尾；
                # 围栏内混入句读行 = "未闭合块吞正文"，仍按未闭合处理。
                # ✅ R76：判据下沉 fence_common.looks_like_prose，与落库补齐告警
                #    （auto_fix 的 swallowed_prose）同一实现，不再两份字面量。
                if not any(_looks_like_prose(ln) for ln in _extended):
                    return _extended, "recovered", _j + 1
            _pipe_logger.warning(
                "内联代码块超过 %d 行且窗口内无干净闭合，判为未闭合（截断点行号=%d）",
                max_lines, i)
            return code_lines, "truncated", i
    # 循环正常结束：要么命中结束围栏（break 退出），要么到达 EOF。
    if i < len(lines) and _fence_is_close(lines[i], open_char, open_len,
                                          tolerant_length=True):
        return code_lines, "closed", i + 1
    return code_lines, "eof", len(lines)


# ============================================================
# R165（2026-11-01）· 围栏容错两件：缩进归一 + markdown 外层包裹内层配平
# ------------------------------------------------------------
# 两处都是「文本形态」问题而非「判据」问题，故都在**读取前**把文本改成两种口径
# 结论一致的形态（与 R76 ``normalize_fence_closes`` 同一纪律：只改形态、行数不变、
# 幂等、保守）。判据本身（``fence_common.FENCE_LINE_RE`` ≤3 前导空白）**不动** ——
# 放宽判据会误判 CommonMark 里合法的 4 空格缩进代码块。
# ============================================================

#: 4+ 前导空白的围栏行：``FENCE_LINE_RE`` 判为非围栏的「缩进越界」形态。
#: ``re.M`` 只服务 ``.search(content)`` 的快速预检；逐行仍用 ``.match(line)``。
_INDENTED_FENCE_RE = re.compile(r"^([ \t]{4,})(`{3,}|~{3,})([^`~\n]*)$", re.M)
#: 任意前导空白的围栏行（定位缩进块的闭合行时用；闭合判据仍由 is_fence_close 给）。
_ANY_INDENT_FENCE_RE = re.compile(r"^([ \t]*)(`{3,}|~{3,})([^`~\n]*)$")


def _dedent_line(line: str, width: int) -> str:
    """去掉至多 ``width`` 个前导空白（不足则全剥，不含其它字符）。"""
    k = 0
    while k < width and k < len(line) and line[k] in " \t":
        k += 1
    return line[k:]


def normalize_chart_fence_indent(content: str) -> tuple[str, list[dict]]:
    """把**图表家族围栏**的 4+ 空格缩进去掉（含块内各行与闭合行）。

    动机（R165 遗留项）：提示词已硬性要求围栏「前导 ≤3 空格」，但 AI 仍偶发
    按列表续行写成 4 空格。此时围栏行不满足 ``fence_common.FENCE_LINE_RE``
    ⇒ 整块退化为普通段落 ⇒ **裸源码印进交付 DOCX**（探针 ``[indent4]`` 实测）。

    ✅ 为什么归一文本而不是放宽判据：CommonMark 中「4 空格缩进」本身是缩进代码块
    语义，放宽 ``FENCE_LINE_RE`` 会连真正的代码样例一起当图表围栏解析；而只对
    「信息串属于 ``mermaid`` / ``chart-json`` / ``ai_image``」的行去缩进，作用域
    收敛且与提示词一致。

    性质（与 ``normalize_fence_closes`` 同纪律）：
      · **行数、行序完全不变**（``src_line`` / 围栏 ordinal 契约不受影响）；
      · 只动缩进，不改任何字符内容；缩进块内每行按同一宽度去缩进（保持相对对齐）；
      · 幂等（去缩进后不再命中 4+ 空格判据）；
      · 保守：① 被其它围栏块（``iter_all_blocks``）包住的区域一律不动
        （那是别人块内的示例文本）；② 找不到闭合行时**原样保留**
        （不制造新的未闭合围栏）。

    Returns:
        ``(normalized_content, log)``；log 项
        ``{"action": "dedent_chart_fence", "line": 1-based, "lang", "indent",
           "close_line": 1-based}``。
    """
    if not content:
        return content, []
    lines = content.split("\n")
    # 保护区：所有 ≤3 空格围栏块（含未闭合块到文末）内部的每一行。
    covered: set[int] = set()
    for b in _iter_all_blocks(content):
        end = b["close_line"] if b["close_line"] >= 0 else len(lines) - 1
        covered.update(range(b["open_line"], end + 1))
    log: list[dict] = []
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if i in covered:
            out.append(line)
            i += 1
            continue
        m = _INDENTED_FENCE_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        info = m.group(3).strip()
        lang = info.split()[0].lower() if info else ""
        if lang not in INLINE_CHART_FENCE_LANGS:
            out.append(line)
            i += 1
            continue
        ch = m.group(2)[0]
        close_j = -1
        for j in range(i + 1, n):
            if j in covered:
                break
            cm = _ANY_INDENT_FENCE_RE.match(lines[j])
            if cm and cm.group(2)[0] == ch and not cm.group(3).strip():
                close_j = j
                break
        if close_j < 0:
            out.append(line)
            i += 1
            continue
        width = len(m.group(1))
        out.append(line.lstrip(" \t"))
        out.extend(_dedent_line(ln, width) for ln in lines[i + 1:close_j])
        out.append(lines[close_j].lstrip(" \t"))
        log.append({"action": "dedent_chart_fence", "line": i + 1,
                    "lang": lang, "indent": width,
                    "close_line": close_j + 1})
        i = close_j + 1
    if not log:
        return content, []
    return "\n".join(out), log


def _read_markdown_fenced_block(lines: list[str], body_start: int,
                                max_lines: int = MAX_INLINE_CODE_BLOCK_LINES,
                                open_char: str = "`", open_len: int = 3,
                                ) -> tuple[list[str], str, int]:
    """``markdown`` 家族围栏的读取器：闭合行必须让**内层围栏配平**。

    动机（R165 遗留项）：``content_blocks._parse_content_blocks`` 把
    `` ```markdown`` 的内部**按 Markdown 递归解析**（R117），因此它必须是
    「容器」而不是「代码」。但通用扫描器 ``read_fenced_block`` 只按 CommonMark
    判闭合 —— 内层 `` ```mermaid `` 的**裸闭合行**（信息串为空）会被当成外层闭合，
    于是 ① 内嵌图表整块丢失（探针 ``_probe_caption.py`` 实测：日志
    「未闭合 state=eof」，图表被丢弃、正文段落保住）；② 外层真正的闭合行摇身
    变成「新开围栏」，把其后正文吞进代码块。

    判据（仅本读取器）：遇到信息串为空的同种围栏行时，
      · 若当前**不在**内层块内 → 这是外层闭合，正常返回（与 CommonMark 一致）；
      · 若当前**在**内层块内 → 这是内层的闭合，吞进内容、内层出栈。
    内层块 = 任何非闭合的围栏行（带信息串，或裸行但已在内层）。

    ✅ 保守回退：读到文末内层仍未配平（例如正文里孤立 ``` 作为示例）→ 回退
    通用扫描器，保持**畸形输入的历史行为逐字不变**（宁可少解析一层，也不让
    整段正文被吞进围栏）。
    """
    code_lines: list[str] = []
    inner: tuple[str, int] | None = None
    i = body_start
    n = len(lines)
    while i < n:
        pf = _fence_parse_line(lines[i])
        if pf is not None:
            ch, ln, info = pf
            if inner is not None:
                if (ch == inner[0] and not info
                        and _fence_is_close(lines[i], inner[0], inner[1],
                                            tolerant_length=True)):
                    inner = None
            elif (not info
                  and _fence_is_close(lines[i], open_char, open_len,
                                      tolerant_length=True)):
                return code_lines, "closed", i + 1
            else:
                # 非闭合的围栏行 = 内层块开启（`` ```mermaid `` / 裸 ` ``` ` 都算）
                inner = (ch, ln)
        code_lines.append(lines[i])
        i += 1
    _pipe_logger.debug(
        "markdown 围栏内层未配平（已读 %d 行），回退通用扫描器保持历史语义",
        len(code_lines))
    return read_fenced_block(lines, body_start, max_lines=max_lines,
                             open_char=open_char, open_len=open_len)
