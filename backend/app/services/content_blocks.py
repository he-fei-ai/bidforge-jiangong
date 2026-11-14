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

# ✅ 2026-11-05（生成侧防回归第一道闸）：解析唯一入口的前置归一化 ——
#    字面 \n 粘行 / PDF 分页标记 / 表题与表头粘连行 / 确定性错别字，
#    在按行切分**之前**统一清除（见 services/content_sanitize.py）。
from app.services.content_sanitize import normalize_raw_content

# ✅ R76（2026-10-13）：围栏闭合判据唯一事实源。本模块的 parse_fence_line
#    保留历史签名与 lang 语义（图表三侧的上百处引用 + 既有测试的导入名不动），
#    但「这一行是不是闭合行」的判定一律下沉 fence_common —— 旧实现只比对围栏
#    字符，把「反引号后带语言标签」的行当作闭合，实测在块内提前闭合后让下一个
#    裸围栏行变成新开围栏、把其后正文整段吞进代码块（state=eof），而前端 marked
#    （CommonMark）判该行为正文保留 —— 预览与导出反向分叉（BUG-F2）。
from app.services.fence_common import (  # noqa: E402
    is_fence_close as _fence_is_close,
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
    re.compile(r"^\d+(?:[.．]\d+)*[、.．\s]+"),
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
LEAD_IN_HINT_RE = re.compile(r"(?:如下图|见下图|如下图示|如图|图示)\s*(?:所示)?\s*[:：。]?$")
# 保留私有名（既有本模块内 2 处消费 + 既有测试 import 零改动）。
_LEAD_IN_HINT_RE = LEAD_IN_HINT_RE
# 登记侧（_chart_pipeline）删除代码块时回收引导语的判据别名。
ORPHAN_LEAD_IN_RE = LEAD_IN_HINT_RE
# 私有常量别名（_chart_pipeline 既有 import 名保持不变）。
_LEAD_IN_MAX_CHARS = LEAD_IN_MAX_CHARS
# ✅ R183（2026-11-07）：图题候选专用门控。`_is_orphan_lead_in_text` 的宽判据
#    里 `如(?:下|后)(?:图|表)` 要求“如下”后紧跟“图/表”，覆盖不到
#    「主体施工工艺流程如下：」这类以“如下”收尾的引导语（仅紧邻图表块时调用本
#    函数，不会影响普通正文的孤儿引导语回收口径）。
_LEAD_IN_TITLE_HINT_RE = re.compile(
    r"(?:如(?:下|后)(?:图|表)?|见(?:下|上)(?:图|表)?|如图|图示)\s*(?:所示)?\s*[:：。]?\s*$"
)
# ✅ R183（2026-11-07）：去掉可选的 `(?:图|表)?` —— 它会把图题**末尾的"图"**
#    当作引导语的一部分剥掉（"施工流程图如下：" → "施工流程"）。只有明确的
#    引导短语后缀才应被剥除；"施工流程图""布置图"等名词短语必须保留。
_LEAD_IN_TAIL_RE = re.compile(
    r"(?:如下|如后|见下|见上|如图|图示)?\s*(?:所示|如下)?\s*[:：。]?\s*$"
)
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
#: 表题开头「表 X-Y」编号前缀（正文规范化侧；捕获剥离后的标题正文）。
#: ✅ R218（2026-11-11 · P3 · C-4）：与 `_export_docx_parts._LEADING_TABLE_NUM_RE`
#:   对齐 —— 旧 pattern 只认半角空格/半角数字/无「格」字/无零宽 ⇒
#:   「表６-１ 配置计划」「表格 6-1 配置计划」在正文预览不剥、成稿剥（同段
#:   正文两套表号叠写 / 预览与成稿不一致）。现补全角数字０-９、「格」字、
#:   零宽（Cf 格式字符 \u200b-\u2060\ufeff），与导出侧行为逐字一致。
_TABLE_CAPTION_RE = re.compile(
    r"^表[\s\u200b\u200c\u200d\u2060\ufeff]*格?[\s\u200b\u200c\u200d\u2060\ufeff]*"
    r"(?:[:：][\s\u200b\u200c\u200d\u2060\ufeff]*"
    r"|[0-9０-９一二三四五六七八九十][0-9０-９.\-–—]*[\s\u200b\u200c\u200d\u2060\ufeff]*"
    r"[、.：:\s\u200b\u200c\u200d\u2060\ufeff]+)"
    r"(\S.*)$"
)
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
_RE_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)")
_RE_TABLE_SEP = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$")
# ✅ R220（2026-11-12 · 导出 DOCX 图表渲染错误收口）：两类 AI 畸形围栏形态的唯一
#    修复出口（``normalize_chart_fence_shapes``）。生产库实证：
#      · 形态 A（3c150ba0）—— 图表被包进单列「图题假表格」：
#        ``| 图 9-1 井道垂直度检测示意图 |`` / ``| :---: |`` / ``| ```mermaid `` …
#        `` ``` |``。后果：围栏行以 ``|`` 开头 → ``parse_fence_line`` 判非围栏 →
#        图表**从不渲染**、源码行以表格单元格/裸段落形态泄漏进成稿；
#        闭合行 `` ``` |`` 信息串非空（R76 判据）→ 被当成 lang="|" 的代码围栏
#        开启，把图注段吞进代码块。
#      · 形态 B（a0b4093e）—— `` ```markdown `` 文档围栏包住**整段正文**并内嵌
#        `` ```chart-json `` 图表：外层围栏吞掉正文（成稿整段退化为 run-on 纯文本、
#        ``###`` 标记原样印刷），内嵌图表被外层围栏遮蔽 → 解析/登记两侧全部丢图。
#    修复原则：**行数不变**（只抹标记 / 原地重写）—— 保住 numbering 侧
#    ``src_line`` 行号契约（R192 FIX-R1 家族），且幂等。
#: 文档型围栏语言：内容是正文而非代码（仅在「内嵌图表围栏」时解包，见形态 B）。
_DOC_FENCE_LANGS = frozenset({"markdown", "md", "text", "txt", "plain"})
#: 图表家族围栏的**开启行**（行首即围栏 + 图表语言标签；用于形态 B 的嵌套探测）。
#:    ✅ R227：补 `~{3,}`（波浪线围栏）—— AI 可能输出 `~~~mermaid` 而非 ````` ```mermaid ````，
#:    markdown 两者都是合法围栏（fence_common.FENCE_LINE_RE 支持 `~{3,}`），
#:    旧版只认反引号，「波浪线图题假表格」形态（`| ~~~mermaid ~~~ |`）不被识别。
_RE_CHART_FENCE_OPEN_LINE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*(?:mermaid|chart-json|ai_image)\b")
#: 形态 A 开启行：整行就是「管道 + 围栏 + 图表语言标签（+ 可选尾管道）」。
#:    ✅ R227：围栏标记组（group 1）扩为反引号或波浪线，且**保持捕获**（调用点
#:    用它原样重写开启/闭合行，非捕获组会错位到语言标签）。
_RE_FIG_TABLE_CHART_OPEN = re.compile(r"^\s*\|\s*(`{3,}|~{3,})\s*(mermaid|chart-json|ai_image)\s*\|?\s*$")
#: 形态 A 闭合行：整行只有围栏标记（允许杂散的前导/尾随管道，无语言标签）。
_RE_FIG_TABLE_FENCE_CLOSE = re.compile(r"^\s*\|?\s*(`{3,}|~{3,})\s*\|?\s*$")
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
# ✅ R227（修复8）：无序列表「无空格变体」（`-材料进场`）被拍平 —— 旧正则要求
#    `\s+`，无空格形式落入普通段落。保守放宽：只对 **`-`/`•`/`·`** 三种（`*`
#    有 markdown 强调歧义，保持 `\s+` 不变），且**拒绝数字/空格/连字符后继**
#    （`-5℃` 是负温/数值、`--x` 是残破分隔线/连字符前缀，都不是列表项）。
_RE_UL_BULLET_TIGHT = re.compile(r"^[-•·](?=[^\s0-9\-])(.*)")
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

    # N.N.N 标题（优先级最高，先匹配）
    m = _RE_HEADING_NNN.match(s_inside)
    if m and _is_reasonable_heading_number(m.group(1)):
        return _plain_heading_result(4, m.group(2))

    # N.N.N.N(.N…) 深层标题（≥4 段）：层级 = 段数 + 1，封顶 7
    m = _RE_HEADING_DEEP.match(s_inside)
    if m and _is_reasonable_heading_number(m.group(1)):
        return _plain_heading_result(min(m.group(1).count(".") + 2, 7), m.group(2))

    # N.N 标题
    m = _RE_HEADING_NN.match(s_inside)
    if m and _is_reasonable_heading_number(m.group(1)):
        return _plain_heading_result(3, m.group(2))

    # N 标题
    m = _RE_HEADING_N.match(s_inside)
    if m and _is_reasonable_heading_number(m.group(1)):
        return _plain_heading_result(2, m.group(2))

    return None


#: 数量词/计量单位首字（`N 台挖掘机进场` 这类正文不得当标题）。
#: ✅ R218（2026-11-11 · P2 · C-1）：补齐「条/口/道/节/排/列/面/孔/眼/遍/趟」11 量词 ——
#:   此前 `3 条施工便道` 的正文首字「条」不在表内 ⇒ 整行被判成标题、数字 3 被
#:   静默丢弃（成稿标题「条施工便道」）；实测 `'3 条施工便道'` → `(2,'条施工便道')`。
#:   新增量词按「宁误判为正文不误判为标题」原则配豁免（见 `_UNIT_TAIL_EXEMPT_NEXT`）：
#:   正文误判为标题 = 数字丢 + 后续编号错乱（危害大）；标题误判为正文 = 内容保留
#:   （仅丢标题地位，可接受）。
_UNIT_TAIL_CHARS = "台人天米个套吨层根处项次月年周日时分号元组张栋座榀块份册页批车辆樘扇倍条口道节排列面孔眼遍趟"
#: 紧跟单位首字、能把该字组成普通词（而非量词）的下一字 —— 命中则**不**按数量词排除。
#: 例：`3 人力资源配置`（人力）、`3 项目划分`（项目）、`7.2.3 次生灾害…`（次生）
#: 都是正常标题，不能被误降级为正文。
_UNIT_TAIL_EXEMPT_NEXT = {
    "人": "力员数才均家口手",
    "天": "气",
    "台": "账帐",
    "处": "理置",
    "项": "目",
    "次": "生序数要日",
    "组": "织",
    "月": "度报",
    "年": "度",
    "周": "边期报",
    "日": "常期",
    "时": "间段期效",
    "分": "析阶段区布项工类层隔部仓包户支配散离化解享级数子外内中",
    "元": "件素",
    "张": "拉",
    "套": "管",
    "层": "站",
    "平": "面衡台均",
    "立": "面方杆",
    "根": "据",
    # ✅ R218（C-1）：新增量词的精准豁免 —— 只豁免「编号 + 明确普通名词」形态
    #    （`3 条件`/`3 节点` 是编号+名词标题，不得被误降级为正文）；「量词+实物名词」
    #    （`3 条施工便道`/`3 节电池`）的后续字不在豁免集 ⇒ 仍判正文（数字保留）。
    #    其余新量词（口/道/排/列/面/孔/眼/遍/趟）保守不豁免（宁正文不标题）。
    "条": "件款目文例",
    "节": "点能日气奏拍",
}
#: ASCII / 符号计量单位（`3 mm 厚`、`3 m 高`、`5 % 掺量`、`30 ℃`）。
_UNIT_TAIL_ASCII_RE = re.compile(
    r"^(?:mm|cm|km|kg|min|ml|m2|m3|m\u00b2|m\u00b3|\u33a1|m|t|h|d|s|L|[%\uff05\u2103\u00b0])(?![A-Za-z0-9])"
)


def _tail_starts_with_quantity(title: str) -> bool:
    """标题正文是否为「数量词 + 名词」形态（`3 台挖掘机进场` / `3.5 米厚底板浇筑`）。

    ✅ R182（2026-11-07 · 标题误判吞数字）：`_is_reasonable_heading_number` 只拦
    年份/超两位数编号（2023、100），`3 台挖掘机进场` 的编号 3 合法 → 被提升为
    level-2 标题且 **"3 " 被静默丢弃**（成稿标题变成「台挖掘机进场」，
    正文段落消失）；`3.5 米厚底板浇筑` 更会被判成 level-3（"米厚底板浇筑"）。
    这类「数字 + 空格 + 量词」行是正文陈述（工程量/规格），不是章节标题，
    故在四个编号分支出口统一排除；排除后整行保留为普通段落，内容零丢失。
    """
    t = (title or "").strip()
    if not t:
        return False
    if _UNIT_TAIL_ASCII_RE.match(t):
        return True
    if t[0] not in _UNIT_TAIL_CHARS:
        return False
    exempt = _UNIT_TAIL_EXEMPT_NEXT.get(t[0], "")
    return not (len(t) > 1 and t[1] in exempt)


def _plain_heading_result(level: int, title: str):
    """纯文本编号标题出口：命中数量词形态时返回 None（降级为正文段落）。"""
    title = (title or "").strip()
    if _tail_starts_with_quantity(title):
        return None
    return level, title


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
_FIGURE_CAPTION_RE = re.compile(r"^图\s*(?:[:：]\s*" r"|[0-9一二三四五六七八九十][0-9.\-–—]*[\s、.：:]+)" r"(\S.*)$")


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
    text = _RE_BOLD_WRAP.sub(r"\1", str(prev.get("text") or "").strip()).strip()
    m = _FIGURE_CAPTION_RE.match(text)
    if not m:
        return ""
    title = m.group(1).strip()
    if not _looks_like_figure_title(title):
        return ""
    blocks.pop()  # 已升格为图题，正文中不再重复输出
    return title


def _attach_trailing_figure_captions(blocks: list[dict]) -> list[dict]:
    """把图表块**下方**紧邻的「图 X-Y 题名」段落升格为该图图题并移除该段落。

    ✅ R179（G-08）：GB/T 交付规范要求图题位于图**下方**，AI 也常按此形态书写。
    旧实现只吸收块**上方**的题注，块下方的图题随后被
    ``_drop_dangling_figure_captions`` 当作「有题无图」整段删除 —— 成稿图题
    回落到载荷/引导语/通用名，用户写的具体图名丢失。

    判据与上方题注吸收完全同源（``_FIGURE_CAPTION_RE`` + ``_looks_like_figure_title``）；
    显式图题优先于引导语推导出的候选，故命中时直接覆盖块标题。
    """
    if not blocks:
        return blocks
    out: list[dict] = []
    i = 0
    while i < len(blocks):
        blk = blocks[i]
        out.append(blk)
        # ✅ R215（2026-11-11 · P3）：白名单扩 image —— AI 配图块（``![题](url)``）
        #   **下方**的「图 X-Y 题名」段此前流到 _drop_dangling_figure_captions 被
        #   按「有题无图」整段删除（上方题注由 _extract_figure_caption 吸收，
        #   下方只有 chart 块有吸收通道）。GB/T 规范图题在图**下方**（R179 G-08
        #   对 chart 块已修，image 块同族漏改），命中时覆盖 alt（与 chart 块
        #   「显式图题优先于引导语推导」同口径）。
        #   ✅ R224（F12a · 同族第 3 处）：白名单补 ``ai_image`` —— AI 配图块
        #   （:1385 产出 type="ai_image"，title 消费链与 chart 块同为
        #   「源文题注 > 载荷 title > 引导语」R196 链）**下方**的「图 X-Y 题名」
        #   段此前流到 _drop_dangling_figure_captions 被整段删除。
        if blk.get("type") in ("chart", "image", "ai_image") and i + 1 < len(blocks):
            nxt = blocks[i + 1]
            if nxt.get("type") == "paragraph":
                text = _RE_BOLD_WRAP.sub(r"\1", str(nxt.get("text") or "").strip()).strip()
                m = _FIGURE_CAPTION_RE.match(text)
                if m:
                    title = m.group(1).strip()
                    if _looks_like_figure_title(title):
                        if blk.get("type") == "image":
                            blk["alt"] = title  # image 块消费 alt（chart/ai_image 块消费 title）
                        else:
                            blk["title"] = title
                        i += 2  # 题注已升格，正文中不再重复输出
                        continue
        i += 1
    return out


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
            text = _RE_BOLD_WRAP.sub(r"\1", str(b.get("text") or "").strip()).strip()
            m = _FIGURE_CAPTION_RE.match(text)
            if m and _looks_like_figure_title(m.group(1).strip()):
                logger.warning("移除无图可挂的残留图题行（有题无图，不占图号）: %s", text[:50])
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
        # ✅ 2026-11-05（第1轮报告 §10「管道符表残留为文本」形态 B）：
        #    AI 生成的表漏写 GFM 分隔行（``| --- |``），旧判据整块降级为
        #    裸文本段落 → 成稿出现 "|测试系统|测试项目|…" 管道符残留。
        #    补齐条件（保守，不 Fabrication 数据）：
        #      · ≥2 行、每行列数一致且 ≥2 列 —— 行首已是 ``|``，结构可信；
        #      · 合成分隔行只是补齐 GFM 语法标记，表头=首行、数据=其余行，
        #        不发明任何单元格内容；列数不齐或只有 1 行 → 维持降级。
        if n >= 2:
            _col_counts = {ln.strip().strip("|").count("|") + 1 for ln in tbl_lines}
            if len(_col_counts) == 1 and next(iter(_col_counts)) >= 2:
                _cols = next(iter(_col_counts))
                logger.info("表格块缺分隔行：已按首行作表头合成分隔行（%d 列 × %d 行）", _cols, n)
                return [[tbl_lines[0], "|" + " --- |" * _cols, *tbl_lines[1:]]]
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
    t = (
        t.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
    )

    def _num(m: "re.Match") -> str:
        try:
            return chr(int(m.group(1)))
        except (ValueError, OverflowError):
            return m.group(0)

    t = _HTML_DEC_NUM_RE.sub(_num, t)
    t = _HTML_DEC_HEX_RE.sub(lambda m: (chr(int(m.group(1), 16)) if len(m.group(1)) <= 6 else m.group(0)), t)
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
    gfm = ["| " + " | ".join(norm[0]) + " |", "| " + " | ".join(["---"] * ncols) + " |"]
    gfm.extend("| " + " | ".join(r) + " |" for r in norm[1:])
    return gfm


# ✅ R183（2026-11-07）：图题自带「图 3-1 / 图 2-1.1 / 图一」前缀时，导出会再拼一次
#    「图 {章号}-{序号}」，产出「图 3-1 图 3-1 主体阶段劳动力配置」的重复编号。
#    该前缀只允许紧跟数字/中文数字/X（可带 `1.2` / `1-2` 小节号）并带分隔符，
#    因此「图示化」「图纸会审」等正常词不会被误剥。
_RE_FIGURE_NUMBER_PREFIX = re.compile(
    r"^\s*(?:图|Figure|Fig\.?)\s*([0-9一二三四五六七八九十Xx]+(?:[.\-–—][0-9一二三四五六七八九十Xx]+)?)"
    r"\s*(?:[、.：:\-–—]\s*|\s+)",
    re.I,
)


def _strip_leading_figure_number(raw) -> str:
    """去掉图题开头的「图 X-Y」自编号（唯一实现，图题/指令标题两处共用）。"""
    return _RE_FIGURE_NUMBER_PREFIX.sub("", str(raw or ""), count=1)


def _clean_chart_title(raw, limit: int = 40) -> str:
    """图题清洗：**折叠**空白（连续空白压成一个空格、保留词间分隔）、限长。

    ★ R175（BUG-175-04）：旧实现 ``_RE_WS_RUN.sub("", ...)`` 是**删除**空白，
    中英混排图题会被粘成一串（``"Site Layout Plan 施工平面布置"`` →
    ``"SiteLayoutPlan施工平面布置"``），导出后图题不可读、也不可检索。
    现保留单个分隔空格（全角空格同样归一为半角单空格）。

    ✅ R202（2026-11-09 · G2 · 模型可见静默截断）：超限时追加省略号 ——
    图题会随图表 JSON 一起送进模型（图表生成指令），静默截断会让模型把
    半截标题当成完整标题（例如「深基坑支护与」被当成完整图题）。
    改动仅在超长时发生，未超长逐字节不变。
    """
    cleaned = _RE_WS_RUN.sub(" ", _strip_leading_figure_number(raw)).strip()
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


def _title_candidate(raw: str) -> str:
    """引导语剥尾后的图题候选校验：长度 4~30、不含句读（否则不是名词短语图题）。

    ★ R175（BUG-175-04）：同 ``_clean_chart_title``，空白**折叠**为单空格而非删除。
    """
    t = _RE_WS_RUN.sub(" ", _strip_leading_figure_number(raw)).strip()
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
    text = _RE_BOLD_WRAP.sub(r"\1", str(prev.get("text") or "").strip()).strip()
    # ✅ R183（2026-11-07）：门控放宽为「孤儿引导语宽判据 ∪ 专用引导语后缀」，
    #    覆盖「主体施工工艺流程如下：」「施工流程图如下：」等不含
    #    「如下图/见下图/如图」词序的变体 —— 否则该图题被丢弃，图题退化为
    #    类型通用名（"流程图"），违反图名提取优先级。
    if not text or len(text) > LEAD_IN_MAX_CHARS or not (
        _is_orphan_lead_in_text(text) or _LEAD_IN_TITLE_HINT_RE.search(text)
    ):
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

    ✅ 2026-11-05：判据放宽为 :func:`_is_orphan_lead_in_text`（覆盖
    "施工流程图如下："这类**不含**"如下/见图/如图"命中词序的变体），
    修复第1轮报告 §9「施工流程图如下：后图缺失/仅有占位」的悬空引用残留。
    """
    if not blocks:
        return False
    prev = blocks[-1]
    if prev.get("type") != "paragraph":
        return False
    text = _RE_BOLD_WRAP.sub(r"\1", str(prev.get("text") or "").strip()).strip()
    if not _is_orphan_lead_in_text(text):
        return False
    blocks.pop()
    logger.info("图表围栏被跳过：已同步移除其孤儿引导语「%s」", text[:30])
    return True


# ✅ 2026-11-05：孤儿引导语判据（宽于 LEAD_IN_HINT_RE）。
#    旧正则要求句尾恰为「如下图/见下图/如图/图示」+可选"所示"+标点，
#    漏掉「施工流程图如下：」「工艺流程如下图。」等**词序不同**的变体
#    （第1轮报告 Page102-103 实测）。宽判据必须仍含"图/表"字样 ——
#    「要求如下：」「步骤如下：」是引出**列表**的正当行文，绝不可删。
_RE_LEADIN_BROAD = re.compile(
    r"(?:"
    r"如(?:下|后)(?:图|表)"  # 如下图 / 如下表 / 如后图
    r"|见(?:下|上)(?:图|表)"  # 见下图 / 见上图
    r"|如图"  # 如图（含"如图所示"）
    r"|图示"
    r"|流程图如下"  # 词序变体：施工流程图如下：
    r")\s*(?:所示)?\s*[:：。]?\s*$"
)


def _is_orphan_lead_in_text(text: str) -> bool:
    """判定一段文本是否是「图表引导语」（LEAD_IN_HINT_RE 宽化版，单一出口）。

    宽判据 = 既有 :data:`LEAD_IN_HINT_RE`（句尾"如下图/见下图/如图/图示"）
    ∪ :data:`_RE_LEADIN_BROAD`（"流程图如下："等词序变体）。
    长度上限沿用 LEAD_IN_MAX_CHARS（引导语是短句，长段落只可能在行文里
    恰好以这些词收尾，删除会毁正文）。
    """
    t = str(text or "").strip()
    if not t or len(t) > LEAD_IN_MAX_CHARS:
        return False
    return bool(LEAD_IN_HINT_RE.search(t) or _RE_LEADIN_BROAD.search(t))


def _drop_leadin_without_visual(blocks: list[dict]) -> list[dict]:
    """收尾清理：引导语段落之后**没有任何图表/图片**时，删除悬空引导语。

    ✅ 2026-11-05（第1轮报告 §9「施工流程图如下：后无图」根因收口）：
    既有 ``_pop_orphan_lead_in_block`` 只在「图表围栏被跳过」时触发；
    源文里压根没有图表块（AI 只写了引导语、忘写围栏，或图表在生成期被
    上游丢弃）时，引导语照样原样印进成稿 —— 读者看到"流程图如下"翻遍
    全文无图。现解析收尾统一兜底：
      · 段落是**纯引导语**（短、以引导语收尾）且其后一块不是
        chart / image / ai_image / table → 整段删除（与删图删引导语同口径）；
      · 长段落（正文里恰好以"如下："收尾）绝不触碰 —— 不做句内裁剪，
        避免误删实质内容；
      · 绝不静默：每次删除记 INFO。

    ✅ R194（FIX-9 · 白名单补 table）：判据源 ``_RE_LEADIN_BROAD`` 明确含
    「如(?:下|后)(?:图|表)」与「见(?:下|上)(?:图|表)」—— 即「如下表 /
    见下表」也命中引导语判据，但本函数的后继白名单只有 chart / image /
    ai_image，**表格不在其中**。「预算安排如下表：」+ 紧邻 GFM/HTML 表格块
    （工程文档极高频形态）被整段删除 —— 删除的是正当行文而非悬空引用：
    引导语的视觉对象（表格）明明存在。且与同文件
    ``_pop_orphan_lead_in_block`` 的 docstring 契约（"表格前的引导语绝不
    触碰"）及导出侧 ``_export_docx_parts._pop_orphan_lead_in`` 守卫直接矛盾，
    只删导出渲染面（本函数仅供导出/预检消费）→ 前端预览有这句、导出成稿
    没有。白名单补 "table" 后与本函数自身 docstring「没有任何图表/图片」
    的"图表"语义对齐。
    """
    out: list[dict] = []
    for idx, blk in enumerate(blocks):
        if blk.get("type") != "paragraph":
            out.append(blk)
            continue
        nxt = blocks[idx + 1] if idx + 1 < len(blocks) else None
        if nxt is not None and nxt.get("type") in ("chart", "image", "ai_image", "table"):
            out.append(blk)
            continue
        text = _RE_BOLD_WRAP.sub(r"\1", str(blk.get("text") or "").strip()).strip()
        if _is_orphan_lead_in_text(text):
            logger.info("无图引导语已回收（其后无图表块）：「%s」", text[:30])
            continue
        out.append(blk)
    return out


# ---------------------------------------------------------------------------
# ✅ R220（2026-11-12）：AI 畸形图表围栏形态修复（唯一出口，幂等，行数不变）
# ---------------------------------------------------------------------------
def _unwrap_figure_table_chart(lines: list[str]) -> None:
    """（原地）形态 A：图表被包进单列「图题假表格」→ 解包为正常图表围栏。

    生产形态（sections 3c150ba0 实证）::

        | 图 9-1 井道垂直度检测示意图 |      ← 图题行（单格）
        | :---: |                            ← 分隔行
        | ```mermaid                         ← 开启行（管道前缀 → 判非围栏）
        flowchart TD
         A[...] --> B[...]
        ``` |                               ← 闭合行（信息串「|」非空 → lang="|" 围栏）

    处理：① 开启行剥前导管道 → 正常 ```` ```mermaid ````；② 闭合行归一为裸围栏；
    ③ 图题行去管道还原为普通段落（导出侧 ``_extract_figure_caption`` 会把它
    吸收为该图图题，避免源文自写图号与导出图号重号）；④ 分隔行置空。
    **只重写、不增删行**（保住 src_line 行号契约）；找不到闭合行（形态不完整）
    一律保持原样 —— 不冒险重写半个形态。
    """
    n = len(lines)
    i = 0
    while i < n:
        m = _RE_FIG_TABLE_CHART_OPEN.match(lines[i])
        if m is None:
            i += 1
            continue
        marker, lang = m.group(1), (m.group(2) or "").strip().lower()
        j = i + 1
        while j < n and not _RE_FIG_TABLE_FENCE_CLOSE.match(lines[j]):
            j += 1
        if j >= n:
            i += 1
            continue
        lines[i] = f"{marker}{lang}" if lang else marker
        lines[j] = marker
        # ③ 向上回收图题行 + 分隔行（可选；两者形态齐备才动，避免误吞真实表格行）
        k = i - 1
        if k >= 0 and _RE_TABLE_SEP.match(lines[k].strip()):
            cand = lines[k - 1].strip() if k - 1 >= 0 else ""
            if cand.startswith("|") and cand.endswith("|") and "`" not in cand:
                inner = cand.strip("|").strip()
                if 0 < len(inner) <= 60:
                    lines[k - 1] = inner
                    lines[k] = ""
        elif k >= 0:
            cand = lines[k].strip()
            if cand.startswith("|") and cand.endswith("|") and "`" not in cand:
                inner = cand.strip("|").strip()
                if 0 < len(inner) <= 60:
                    lines[k] = inner
        logger.warning(
            "R220 图表被包进单列假表格（第 %d 行），已解包为正常图表围栏", i + 1
        )
        i = j + 1


def _unwrap_doc_fence_wrapping_chart(lines: list[str]) -> None:
    """（原地）形态 B：文档型围栏（```` ```markdown ```` 等）包住正文并内嵌图表 → 解包。

    生产形态（sections a0b4093e 实证）：AI 把整段正文包进 ```` ```markdown ````，
    段中还有 ```` ```chart-json ```` 图表 —— 外层围栏让全部正文退化为一个代码块
    （成稿 run-on 纯文本、``###`` 标记原样印刷），内嵌图表被外层围栏遮蔽而丢失。

    判据（**刻意收窄**）：仅当文档型围栏的围栏体内**确实嵌有图表家族开启行**
    才解包 —— 纯 ```` ```markdown ```` 代码样例（无图表）保持既有「按代码块
    渲染」行为（numbering 侧「围栏内标题不参与重编号」契约不受影响）。

    解包实现：只把外层开/闭围栏行**置空**（行数不变 → src_line 对齐；空行在
    解析中被跳过）。外层闭合行的定位带**嵌套穿透**：体内带语言标签的围栏行
    使深度 +1、裸围栏行在嵌套层使深度 -1，深度归零的裸围栏才是外层闭合 ——
    这样内嵌图表自己的闭合行不会被误认成外层闭合而抹掉（否则图表反而变成
    未闭合残片）。外层真未闭合（EOF）时只抹开启行：内嵌图表的闭合行自然
    成为图表自身的闭合。
    """
    n = len(lines)
    i = 0
    while i < n:
        pf = parse_fence_line(lines[i])
        if pf is None or pf[2].strip().lower() not in _DOC_FENCE_LANGS:
            i += 1
            continue
        open_char, open_len, lang = pf
        # 找外层闭合（嵌套穿透）
        j = i + 1
        depth = 0
        outer_close = None
        while j < n:
            if depth == 0 and _fence_is_close(lines[j], open_char, open_len, tolerant_length=True):
                outer_close = j
                break
            pf_j = parse_fence_line(lines[j])
            if pf_j is not None:
                if pf_j[2].strip():
                    depth += 1
                elif depth > 0:
                    depth -= 1
            j += 1
        body = lines[i + 1 : outer_close if outer_close is not None else n]
        if not any(_RE_CHART_FENCE_OPEN_LINE.match(ln) for ln in body):
            # 非图表场景：保持原样（文档围栏照旧按代码块渲染），整块跳过
            i = (outer_close + 1) if outer_close is not None else n
            continue
        lines[i] = ""
        if outer_close is not None:
            lines[outer_close] = ""
        logger.warning(
            "R220 文档型围栏（lang=%s）内嵌图表围栏，已解包为正文渲染（原第 %d 行）",
            lang,
            i + 1,
        )
        i += 1


def normalize_chart_fence_shapes(content: str) -> str:
    """R220：修复两类 AI 畸形图表围栏形态的**唯一出口**（幂等，行数不变）。

    - 形态 A（图题假表格包裹图表）→ :func:`_unwrap_figure_table_chart`
    - 形态 B（文档围栏包裹正文+内嵌图表）→ :func:`_unwrap_doc_fence_wrapping_chart`

    消费方（三类一致，杜绝「登记侧修了、导出侧没修」的口径分叉）：
      - 导出/预览解析：``_parse_content_blocks``（本模块，在 normalize_raw_content
        之后调用）；
      - 登记侧扫描：``_chart_pipeline.iter_inline_chart_fences``（图表清单 /
        chart_predictions / 配图上限 / 导出预检的唯一扫描器）；
      - 围栏改写定位：``_chart_pipeline._apply_chart_fence_edits`` /
        ``_rewrite_code_block``（ordinal 锚点与扫描侧同源的前提）。

    行数不变是**硬约束**：``renumber_section_body_subheadings`` 以
    ``_parse_content_blocks`` 产出的 ``src_line`` 回写源行（R192 FIX-R1），
    任何增删行都会让重写落到错误的行上。
    """
    if not content or ("`" not in content and "~" not in content):
        return content
    lines = content.split("\n")
    _unwrap_figure_table_chart(lines)
    _unwrap_doc_fence_wrapping_chart(lines)
    return "\n".join(lines)


def _parse_content_blocks(content: str, diagnostics: dict | None = None) -> list[dict]:
    """解析正文内容为 block 列表。

    支持的 block 类型：
      heading（``#``~``######`` 与纯文本编号标题）/ paragraph / list_item /
      code / table（GFM）/ chart（mermaid 围栏、chart-json、``[CHART_TYPE: x]``）/
      image（AI 配图：``![说明](http...)``）/ quote（``>`` 引用）/
      hr（``---`` ``***`` ``___`` 分隔线）

    ✅ 2026-11-05（第二轮日志驱动分析 P1）：`diagnostics` 为**可选**汇总字典。
    传非 None 时，被跳过的 chart-json 块会累计到
    ``chart_json_skipped`` / ``chart_json_skipped_unclosed``。
    旧调用方不传 → 行为逐字节不变。为什么需要：这些块被跳过时只写一行
    INFO（生产日志里 2 处「跳过不可渲染的 chart-json 块」），而导出响应头
    ``X-Chart-Render-Stats`` 的 failed 仍是 0 —— 成稿缺图却报「失败 0」，
    用户与排障者都看不到。

    ✅ R233（2026-11-13 · 统计口径对称收口）：mermaid / ai_image 两个图表家族
    分支的解析期跳过此前**零机器信号**（只有 WARNING/INFO + 引导语回收），
    而这两个分支同样是「成稿缺图」：未闭合块（max_tokens 截断，注释自证高频）
    与首关键字不在映射表内的 mermaid 块整块消失、ai_image 占位块整块消失。
    该缺口使这类缺图绕过全部降级判据 → 缺图产物照常写 export_cache 永久命中
    （R222/R181 已对 chart-json 侧闭合同一缺口，本侧漏网）。现补：
    ``chart_mermaid_skipped`` / ``chart_ai_image_skipped`` 总数与按形态细分键，
    由 export._merge_chart_json_skip_stats 统一并入 failed（坏缓存守卫随之生效）。
    """
    if not content:
        return []

    def _note_chart_json_skip(reason: str) -> None:
        if diagnostics is None:
            return
        diagnostics["chart_json_skipped"] = int(diagnostics.get("chart_json_skipped") or 0) + 1
        diagnostics[reason] = int(diagnostics.get(reason) or 0) + 1

    def _note_chart_mermaid_skip(total_key: str, reason: str) -> None:
        """mermaid / ai_image 家族解析期跳过的诊断（键族独立于 chart_json_skipped，
        既有键语义保持「只含 chart-json」不变，由消费侧合并计入 failed）。

        total_key / reason 分离：mermaid 与 ai_image 各有独立总数键
        （chart_mermaid_skipped / chart_ai_image_skipped），细分键挂具体形态。
        """
        if diagnostics is None:
            return
        diagnostics[total_key] = int(diagnostics.get(total_key) or 0) + 1
        diagnostics[reason] = int(diagnostics.get(reason) or 0) + 1

    # ✅ 2026-11-05（防回归第一道闸）：解析前统一归一化 —— 字面 \n 粘行 /
    #    PDF 分页标记 / 「第X页共Y页」页脚 / 表题与 GFM 表头粘连行 /
    #    确定性错别字（峻工→竣工、0.5MQ→0.5MΩ 等）。必须在 split("\n")
    #    之前执行：任何被转义的换行都会让多行正文以「一整行」形态绕过
    #    下方全部结构识别，原样印进交付 DOCX。
    content, _sanitize_stats = normalize_raw_content(content)
    # ✅ R220（2026-11-12）：AI 畸形图表围栏形态修复（图题假表格包裹 / 文档围栏
    #    包裹正文+内嵌图表）。行数不变 → src_line 行号契约（R192 FIX-R1）不受影响；
    #    幂等 → 二次解析零改动。
    content = normalize_chart_fence_shapes(content)
    blocks = []
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
        # ✅ R231（B8）：视觉缩进（tab 折算 4 空格）—— 列表项层级与续行合并判据
        #    的**单一口径**（旧双口径已收口：list_item.indent 曾记原始字符数
        #    （tab=1）、被导出侧 ``// 2`` 换算把 tab 缩进的嵌套列表拍平到 0 级；
        #    原始字符口径 ``indent`` 变量随之无消费方，已删除）。
        _vis_indent = len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())
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
                blocks.append(
                    {
                        "type": "heading",
                        "level": len(m_h.group(1)),
                        "text": _h_text,
                        # ✅ 编号统一（2026-09-25）：源行号（0 基）—— 落库前正文子标题
                        #    编号规范化（services/numbering）据其精确重写源行；
                        #    渲染链路不消费该字段，纯增量信息。
                        "src_line": i,
                    }
                )
            i += 1
            continue
        # ✅ 增强：纯文本编号子标题（AI 输出 `2 标题` / `3.1 标题` 格式）
        # 必须在 Markdown heading 之后、有序列表识别之前 —— 因为有序列表
        # 正则 `^(\d+)\s*[.)、]\s+` 不匹配"编号后是空格"的格式
        plain_h = _detect_plain_heading(line)
        if plain_h:
            lv, txt = plain_h
            blocks.append({"type": "heading", "level": lv, "text": txt, "src_line": i})
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
            i += 1
            # ✅ 三侧口径统一（2026-09-23，导出幽灵图根因修复）：围栏读取改用与登记侧
            #    （_scan_inline_charts）、改写侧（_rewrite_code_block）同源的共用扫描器
            #    read_fenced_block。旧实现本函数缺少登记侧 2026-09-22 补入的"超长但闭合"
            #    有界前视恢复：一份 500<行数≤1000 且首尾围栏齐全的合法图表块，登记侧提取入库、
            #    导出侧却因超行数上限 break 后判为"未闭合"整块跳过 → 出现在图表清单/预览、
            #    却在导出 DOCX 里凭空消失（幽灵图）。共用扫描器后：closed/recovered 均视为闭合，
            #    正常解析渲染；eof/truncated 才走下方"以句读行还原正文"的未闭合降级。
            code_lines, _fence_state, i = read_fenced_block(lines, i, open_char=_open_char, open_len=_open_len)
            _fence_closed = _fence_state in ("closed", "recovered")
            if not _fence_closed:
                logger.warning("检测到未闭合的代码块（lang=%s，state=%s）", lang, _fence_state)
                # ✅ 修复（2026-09-19）：围栏被截断时，"消费至文档末尾"会把截断点
                #    之后的**正文段落**一并吞进残片（残片随后被跳过 → 正文静默丢失）。
                #    图表代码行（mermaid 语句 / JSON 片段）与代码行不会以中文句读收尾，
                #    故以第一个以"。！？"结尾的行作为正文起点，把其后的内容还原给
                #    段落解析，既不渲染残片、也不丢它后面的正文。对**所有**围栏语言生效；
                #    其中图表家族围栏仍需保留完整残片交给下游按"解析失败 → 跳过"处理
                #    （不截断 code_lines），普通代码围栏则只输出真正的代码行，不夹带正文。
                _cut = next((j for j, _cl in enumerate(code_lines) if _cl.strip().endswith(("。", "！", "？"))), None)
                if _cut is not None:
                    _give_back = len(code_lines) - _cut
                    logger.warning(
                        "围栏未闭合（lang=%s），残片 %d 行已跳过，" "其后 %d 行正文已还原为段落", lang, _cut, _give_back
                    )
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
                        "已整块跳过 %d 行，避免把正文当代码块输出",
                        lang,
                        len(code_lines),
                    )
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
                        "章节代码块未闭合（lang=mermaid，已消费 %d 行），" "疑似生成被截断，已跳过（不渲染、不落代码）",
                        len(code_lines),
                    )
                    _pop_orphan_lead_in_block(blocks)
                    # ✅ R233：解析期跳过补机器信号（成稿缺图 → failed → 不写缓存）
                    _note_chart_mermaid_skip("chart_mermaid_skipped", "chart_mermaid_skipped_unclosed")
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
                        "与登记侧同口径，避免幽灵图绕过配图上限"
                    )
                    _pop_orphan_lead_in_block(blocks)
                    # ✅ R233：解析期跳过补机器信号（成稿缺图 → failed → 不写缓存）
                    _note_chart_mermaid_skip("chart_mermaid_skipped", "chart_mermaid_skipped_unrenderable")
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
                blocks.append(
                    {
                        "type": "chart",
                        "chart_type": chart_type,
                        "code": code_text,
                        "inline": True,
                        "title": _extract_figure_caption(blocks)
                        or _mermaid_directive_title(code_text)
                        or _lead_in_title(blocks),
                    }
                )
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
                        "章节代码块未闭合（lang=chart-json，state=%s），" "疑似生成被截断，已跳过（不渲染、不落代码）",
                        _fence_state,
                    )
                    _pop_orphan_lead_in_block(blocks)
                    _note_chart_json_skip("chart_json_skipped_unclosed")
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
                                "chart-json 的 type=%r 非法/缺失，按载荷结构推断为 %r", _cj_type or "(无)", _inferred
                            )
                        _cj_type = _inferred
                if not _cj_type or _cj_type not in PIL_RENDERABLE_CHART_TYPES:
                    logger.info(
                        "导出跳过不可渲染的 chart-json 块（type=%r），避免渲染失败占位", _cj_type or "(非JSON对象)"
                    )
                    _pop_orphan_lead_in_block(blocks)
                    _note_chart_json_skip("chart_json_skipped_unrenderable")
                    continue
                blocks.append(
                    {
                        "type": "chart",
                        "chart_type": _cj_type,
                        "code": code_text,
                        "inline": True,
                        # ✅ 2026-10-17（文档质量报告 图号问题）：源文自写图题优先
                        #    （吸收图表块上方「图 X-Y 题名」行，避免图号重号）；
                        #    载荷 title 次之（AI 按本章语义撰写），缺失时退回引导语
                        "title": _extract_figure_caption(blocks)
                        or _clean_chart_title(_cj_obj.get("title"))
                        or _lead_in_title(blocks),
                    }
                )
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
                        "章节代码块未闭合（lang=ai_image，state=%s），" "疑似生成被截断，已跳过（不占图号）",
                        _fence_state,
                    )
                    _pop_orphan_lead_in_block(blocks)
                    # ✅ R233：解析期跳过补机器信号（成稿缺配图 → failed → 不写缓存）
                    _note_chart_mermaid_skip("chart_ai_image_skipped", "chart_ai_image_skipped_unclosed")
                    continue
                ai_title = ""
                try:
                    _ai_obj = json.loads("\n".join(code_lines).strip())
                    if isinstance(_ai_obj, dict):
                        ai_title = str(_ai_obj.get("title") or "").strip()
                except (json.JSONDecodeError, TypeError):
                    pass
                blocks.append(
                    {
                        "type": "ai_image",
                        # ✅ R196（2026-11-09）：图题兜底链与 chart-json 分支同口径 ——
                        #    源文题注 > 载荷 title > 引导语。旧实现只取载荷 title
                        #    （空则空串），导出端只剩类型通用名「配图」可兜底，
                        #    图题信息层级缺失。
                        "title": _extract_figure_caption(blocks) or ai_title or _lead_in_title(blocks),
                        "code": "\n".join(code_lines).strip(),
                        "inline": True,
                    }
                )
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
                        len(tbl_lines),
                        len(segments),
                    )
                for seg in segments:
                    # ✅ 2026-10-17（文档质量报告 表号问题根因 B）：剥离首行首格 AI 自带的
                    #    「表 X-Y」编号前缀（表号由导出统一生成，否则「表 6-3 表 6-1 …」叠写）
                    table_block = {"type": "table", "lines": _normalize_table_first_cell(seg)}
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
                    _tbl = {"type": "table", "lines": _normalize_table_first_cell(_gfm)}
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
            #    ✅ R224（F12b）：显式图题行**优先于** Markdown alt —— 旧实现
            #    alt 非空时把已 pop 的题注行静默丢弃（用户图题不可恢复丢失），
            #    而下方题注通道是「题注覆盖 alt」（显式图题优先）——同族两条
            #    通道优先级相反。与 R196 ai_image 题注兜底链「源文题注第一」同口径。
            _img_cap = _extract_figure_caption(blocks)
            blocks.append(
                {
                    "type": "image",
                    "alt": _img_cap or (m_img.group("alt") or "").strip(),
                    "url": m_img.group("url").strip(),
                }
            )
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
            logger.warning("图表标记行格式异常（疑似机器标记，已整块跳过、不原样输出）: %s", _stripped[:80])
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
        # ✅ R231（2026-11-12 · B8）：列表项缩进记录**视觉缩进**（tab 折算 4 空格，
        #    与下方 :1588 续行合并判据同一折算）—— 旧实现记录原始字符数（tab=1），
        #    解析层内部两口径分叉且导出侧 ``// 2`` 换算把 **tab 缩进的嵌套列表
        #    拍平到 0 级**（indent=1 → 0，与父级同级、编号序列错乱）。视觉折算后
        #    2 空格→1 级、1 个 tab→2 级、4 空格→2 级（保序保嵌套；4 空格比
        #    Markdown 常规深一级属保序代价，混用 2/4 空格的 CommonMark 宽度
        #    算法与 numPr 语义列表一并归版式专项，见 R230 §九 B8）。
        m_ul = _RE_UL_BULLET.match(stripped) or _RE_UL_BULLET_TIGHT.match(stripped)
        if m_ul:
            blocks.append(
                {
                    "type": "list_item",
                    "ordered": False,
                    "text": m_ul.group(1).strip(),
                    "indent": _vis_indent,
                }
            )
            i += 1
            continue
        # 有序列表: 1. / 1) / 1、 / 1） / （1） / （一）
        # ✅ 优化：统一交由 _match_ordered_item 识别（含中文枚举符无空格、
        #    「（一）」「（1）」等工程文档常用层级标记），并记录标记样式，
        #    导出时按原样式渲染连续序号。
        matched = _match_ordered_item(stripped)
        if matched:
            num, text, marker = matched
            blocks.append(
                {
                    "type": "list_item",
                    "ordered": True,
                    "num": num,
                    "text": text,
                    "indent": _vis_indent,
                    "marker": marker,
                }
            )
            i += 1
            continue
        # ✅ R182（2026-11-07 · 列表样式被拍平）：列表项的缩进续行不得降级为普通段落。
        #    Markdown 语义（以及前端预览）中，缩进续行属于上一列表项的内容；旧实现
        #    落到 paragraph 分支，导出按 Normal 样式渲染（无列表左缩进/悬挂缩进），
        #    与预览不一致且层级丢失。此处并入上一 list_item 的 text（换行保留）。
        #    · 仅当续行视觉缩进（tab=4）≥2 且比上一列表项更深时合并，避免误吞
        #      与列表无关的正文段；
        #    · 表格行/标题/围栏在上方分支已先行识别，不会进入本分支。
        if _vis_indent >= 2 and blocks and blocks[-1].get("type") == "list_item":
            _prev_indent = int(blocks[-1].get("indent") or 0)
            if _vis_indent > _prev_indent:
                _prev = blocks[-1]
                _prev["text"] = f"{_prev.get('text', '')}\n{stripped}"
                i += 1
                continue
        blocks.append({"type": "paragraph", "text": line})
        i += 1
    # ✅ 2026-10-17（文档质量报告 图号问题）：收尾清理「有题无图」的残留图题段落 ——
    #    源文自写的「图 X-Y 题名」若无对应图表块（渲染失败/压根未生成），它既不进
    #    图表清单也不占图号，却与导出为真实图表生成的图号**重号**，且成稿呈现「有题
    #    无图」。已挂图的图题在解析期被吸收；此处只删无图可挂的残留（绝不静默：记 WARNING）。
    # ✅ 2026-11-05（防回归收尾双清理）：
    #    ① _drop_dangling_figure_captions —— 删「有题无图」残留图题（既有）；
    #    ② _drop_leadin_without_visual —— 删「有话无图」悬空引导语（新增，
    #       覆盖"施工流程图如下："后压根无图表块的形态）。
    return _drop_leadin_without_visual(_drop_dangling_figure_captions(_attach_trailing_figure_captions(blocks)))


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


# ✅ 2026-11-05（"a、a、"双重前缀根因 · 第1轮报告 §4）：
#    AI 正文的子标题自带字母序前缀（"a、吊顶与内装接口"），而 E3 降级 /
#    编号重排路径会**重新分配**同层级序号 —— 源前缀不剥离就会叠出
#    "a、a、吊顶与内装接口"。strip_outline_numbering 只认数字与中文数字
#    分支，字母序（a、 / b. / c））不在其列，此处补齐为独立 helper
#    （不改全局 _STRIP_NUMBER_RE，避免影响目录树等其他消费方）。
# ✅ R181（2026-11-06）：正则由单字母放宽到 1~2 个字母，并循环剥离重复前缀，
#    与导出侧原有循环行为对齐后**下沉为唯一实现**（导出不再保留第二套正则）。
_RE_SUBITEM_ORD_PREFIX = re.compile(r"^\s*[a-zA-Z]{1,2}\s*[、.．)）]\s*")
# ✅ R230（2026-11-12 · B3）：剥后剩余若以「字母+分隔符」形态之外的字母开头
#    （"A、B 两案对比" → "B 两案对比" / "a. Basic" → "Basic"），视为正文并列
#    枚举/英文实词，停止剥离。预编译为模块级常量（R227 修复：原内联
#    re.match 破坏 test_no_inline_re_calls_in_module，且每次调用重复编译）。
_RE_STOP_ORD_STRIP_TAIL = re.compile(r"^[A-Za-z](?![a-zA-Z]?\s*[、.．)）])")


def strip_subitem_ordinal_prefix(text: str) -> str:
    """剥离正文子标题/列表项开头残留的字母序前缀（"a、吊顶" → "吊顶"）。

    仅供「编号将被程序重新分配」的路径使用（正文子标题重编号 / 列表项
    连续重编号）；剥后为空则原样返回（标题本身就是编号时绝不吞正文）。
    幂等。重复前缀（``a、a、正文``）循环剥离，语义与导出侧旧循环一致。
    """
    s = str(text or "").strip()
    if not s:
        return s
    stripped = s
    while _RE_SUBITEM_ORD_PREFIX.match(stripped):
        rest = _RE_SUBITEM_ORD_PREFIX.sub("", stripped, count=1).strip()
        if not rest:
            break
        # ✅ R230（2026-11-12 · B3）：剥后剩余若以「字母+分隔符」形态之外的
        #    字母开头（"A、B 两案对比" → "B 两案对比" / "a. Basic" → "Basic"），
        #    视为正文并列枚举/英文实词，**停止剥离** —— 旧实现无条件循环剥离，
        #    把枚举首字母当残留编号静默删掉（内容篡改）。而「字母+分隔符」
        #    形态（"a、a、吊顶"）仍是连续残留前缀，继续循环剥离（R181 语义不变）。
        if _RE_STOP_ORD_STRIP_TAIL.match(rest):
            break
        stripped = rest
    return stripped


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
    d = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
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


def _compute_subheading(
    section_prefix: str,
    section_level: int,
    md_lv: int,
    sub_counters: dict,
    pure: str,
    sec_id: str = "",
    has_children: bool = False,
):
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
    第一层 rel=0 → ``N） 标题``（L6），更深 rel≥1 → ``字母、 标题``（L7）。
    这样正文子标题（L6+）与 DB 子章节（L3+）在同一文档内完全隔离，不再出现
    「1.1 正文标题」与「1.1 DB 子章节」成对撞号。
    """
    try:
        md_lv = max(1, min(int(md_lv or 1), 6))
    except (TypeError, ValueError):
        md_lv = 1

    counts: dict[int, int] = sub_counters.setdefault("c", {})
    if "base" not in sub_counters:
        sub_counters["base"] = md_lv
    base = sub_counters["base"]

    # 更浅的标题回跳 → 与基准层同级，续接兄弟序列（旧实现重启计数 → 重复编号）
    if md_lv < base:
        logger.warning(
            '正文子标题层级不齐（section=%s）：基准层级 H%d 之后出现更浅的 H%d 标题 "%s"，'
            "已按基准层同级续排（避免与本节已编号的子标题重复）。"
            "建议在正文中统一同级标题的 # 数量。",
            sec_id or "?",
            base,
            md_lv,
            pure or "(空)",
        )
        md_lv = base

    # 深度跳跃收敛：不得超过「已有最深非零层级 + 1」，避免中间出现 0 段
    deepest = max([d for d, n in counts.items() if n > 0], default=-1)
    rel = max(0, min(md_lv - base, deepest + 1))

    counts[rel] = counts.get(rel, 0) + 1
    for d in range(rel + 1, 7):
        counts[d] = 0
    if not pure:
        # 降级空标题时仍返回 L6/L7 样式（与有文本时一致）
        if has_children:
            style = 6 if rel == 0 else 7
        else:
            style = min(section_level + 1 + rel, 7)
        return "", style

    # ✅ E3：has_children=True → 降级为节内 body 命名空间
    if has_children:
        # ✅ R209（2026-11-10 · L7 字母塌缩）：旧实现用 52 项 ``ALPHABET`` 钳制
        #    （``min(l7 - 1, len(ALPHABET) - 1)``）⇒ 正文子标题的 L7 字母自
        #    **第 53 项起永久塌缩为 "az"**（成稿出现多个同号子标题、交叉引用失效）。
        #    R203b #18 统一了目录侧三处 L7 实现，**漏了正文侧这一处**（同族漏改）。
        #    现改用 services.numbering.alpha_label（Excel 续编，唯一实现）：
        #    1..52 与旧输出逐字相同，53 起为 ba、bb、…（数学上不重复）。
        from app.services.numbering import alpha_label

        if rel == 0:
            # 第一层：数字+）（1）/ 2）/ …），Heading L6。
            # rel=0 计数器沿既有的 counts[0] 递增（相对深度 0 的兄弟序列各自占一个数字）。
            # ✅ 编号统一（2026-09-26）：分隔符与 HEADING_STYLE_CONFIG[6] 单一事实源一致
            #    （顿号），产出 "1）、标题"，与导出行级 L6 标题（format_heading）严格同口径，
            #    修复「L6 缺顿号 / 多空格」的偏差。
            num = counts[rel]
            text = f"{num}）{_heading_punct(6)}{pure}"
            style = 6
        else:
            # 更深层：字母+、（a、/ b、/ …），Heading L7。
            # ✅ 所有 rel≥1 共享同一个 L7 字母计数器——避免 rel=1 与 rel=2 各自从 a、
            # 起算而撞号。例如 rel1→a、、rel1→b、、rel2→c、… 单调递增。
            l7 = sub_counters.setdefault("l7_count", 0)
            l7 += 1
            sub_counters["l7_count"] = l7
            # ✅ R209：Excel 续编（alpha_label），不再用 52 项 ALPHABET 钳制
            letter = alpha_label(l7)
            # ✅ 编号统一（2026-09-26）：分隔符与 HEADING_STYLE_CONFIG[7] 一致（顿号、无空格），
            #    产出 "a、标题"，与导出行级 L7 标题同口径，修复「L7 多空格」偏差。
            text = f"{letter}{_heading_punct(7)}{pure}"
            style = 7
        return text, style

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


def read_fenced_block(
    lines: list[str],
    body_start: int,
    max_lines: int = MAX_INLINE_CODE_BLOCK_LINES,
    open_char: str = "`",
    open_len: int = 3,
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
            while (
                _j < len(lines)
                and not _fence_is_close(lines[_j], open_char, open_len, tolerant_length=True)
                and _extra <= max_lines
            ):
                _j += 1
                _extra += 1
            if _j < len(lines) and _fence_is_close(lines[_j], open_char, open_len, tolerant_length=True):
                _extended = code_lines + lines[i:_j]
                # 防误吞守卫：图表代码行（Mermaid 语句 / JSON）不会以中文句读收尾；
                # 围栏内混入句读行 = "未闭合块吞正文"，仍按未闭合处理。
                # ✅ R76：判据下沉 fence_common.looks_like_prose，与落库补齐告警
                #    （auto_fix 的 swallowed_prose）同一实现，不再两份字面量。
                if not any(_looks_like_prose(ln) for ln in _extended):
                    return _extended, "recovered", _j + 1
            _pipe_logger.warning("内联代码块超过 %d 行且窗口内无干净闭合，判为未闭合（截断点行号=%d）", max_lines, i)
            return code_lines, "truncated", i
    # 循环正常结束：要么命中结束围栏（break 退出），要么到达 EOF。
    if i < len(lines) and _fence_is_close(lines[i], open_char, open_len, tolerant_length=True):
        return code_lines, "closed", i + 1
    return code_lines, "eof", len(lines)
