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
from app.services.numbering import strip_outline_numbering
from app.services.chart_validators import (
    PIL_RENDERABLE_CHART_TYPES,
    detect_mermaid_chart_type,
    infer_chart_type_from_payload,
)

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
LEAD_IN_HINT_RE = re.compile(r"(?:如下图|见下图|如下图示|如图|图示)\s*(?:所示)?\s*[:：。]?$")
# 保留私有名（既有本模块内 2 处消费 + 既有测试 import 零改动）。
_LEAD_IN_HINT_RE = LEAD_IN_HINT_RE
# 登记侧（_chart_pipeline）删除代码块时回收引导语的判据别名。
ORPHAN_LEAD_IN_RE = LEAD_IN_HINT_RE
# 私有常量别名（_chart_pipeline 既有 import 名保持不变）。
_LEAD_IN_MAX_CHARS = LEAD_IN_MAX_CHARS
_LEAD_IN_TAIL_RE = re.compile(
    r"(?:如下|如后|见下)?\s*(?:图|表)?\s*(?:所示|如下)?\s*[:：。]?\s*$")
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
_RE_CHART_TYPE_TAG = re.compile(r"^\[CHART_TYPE:\s*(\w+)\]")
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
    # 句读符号 → 正文句子，不是表题
    if any(ch in t for ch in "：:，,；;。？！?!、…"):
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


def _parse_content_blocks(content: str) -> list[dict]:
    """解析正文内容为 block 列表。

    支持的 block 类型：
      heading（``#``~``######`` 与纯文本编号标题）/ paragraph / list_item /
      code / table（GFM）/ chart（mermaid 围栏、chart-json、``[CHART_TYPE: x]``）/
      image（AI 配图：``![说明](http...)``）/ quote（``>`` 引用）/
      hr（``---`` ``***`` ``___`` 分隔线）
    """
    if not content:
        return []
    blocks = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
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
                    "src_line": i,
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
                           "src_line": i})
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
            code_lines, _fence_state, i = read_fenced_block(
                lines, i, open_char=_open_char, open_len=_open_len)
            _fence_closed = _fence_state in ("closed", "recovered")
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
                blocks.append({"type": "chart", "chart_type": chart_type,
                               "code": code_text, "inline": True,
                               "title": _mermaid_directive_title(code_text)
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
                               # 载荷 title 优先（AI 按本章语义撰写），缺失时退回引导语
                               "title": _clean_chart_title(_cj_obj.get("title"))
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
            sep_ok = (
                len(tbl_lines) >= 2
                and _RE_TABLE_SEP.match(tbl_lines[1].strip())
            )
            if len(tbl_lines) >= 2 and sep_ok:
                table_block = {"type": "table", "lines": tbl_lines}
                # ✅ 新增：吸收紧邻表格上方的「表 X-Y 表名」行作为表题
                caption = _extract_table_caption(blocks)
                if caption:
                    table_block["caption"] = caption
                blocks.append(table_block)
            else:
                for tl in tbl_lines:
                    blocks.append({"type": "paragraph", "text": tl})
            continue
        # ✅ AI 配图（文生图，正文里的 Markdown 图片）：独占一行时按"图"渲染。
        #    与图表（mermaid/chart-json 代码）属不同模态：这里消费的是真实位图。
        #    下载失败仍降级为图题文本，不会在成稿里残留裸 Markdown 语法。
        m_img = _IMAGE_LINE_RE.match(stripped)
        if m_img:
            blocks.append({
                "type": "image",
                "alt": (m_img.group("alt") or "").strip(),
                "url": m_img.group("url").strip(),
            })
            i += 1
            continue
        # 图表标记 [CHART_TYPE: xxx]
        m = _RE_CHART_TYPE_TAG.match(line.strip())
        if m:
            blocks.append({"type": "chart", "chart_type": m.group(1)})
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
    return blocks


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

    ✅ 2026-10-05（D4 · 编号剥离分叉收口）：旧实现自带 `_TITLE_NUM_STRIP_RES`
    （5 条正则、循环剥离），与 `services/numbering.py` 的 `strip_outline_numbering`
    分叉，实测差异（更差）：
        "1.2.3钢筋工程" → 旧 "3钢筋工程"（点分未吃满整条路径） / 规范 "钢筋工程"
        "1.1"           → 旧 "1"（纯编号标题被误剥半截）      / 规范 "1.1"
    现改为委托 numbering 的唯一实现 + **有界循环**（兼容 "第1章 1.1 工程概况"
    这类多重前缀；numbering 内部已保证年份/纯编号/正常标题不被误剥），
    并删除本地 `_TITLE_NUM_STRIP_RES`。
    """
    s = str(title or "").strip()
    for _ in range(8):  # 多重前缀最多 8 轮即可收敛；有界避免异常输入死循环
        new = strip_outline_numbering(s).strip()
        if new == s:
            break
        s = new
    return s


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
        # 降级空标题时仍返回 L6/L7 样式（与有文本时一致）
        if has_children:
            style = 6 if rel == 0 else 7
        else:
            style = min(section_level + 1 + rel, 7)
        return "", style

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
            style = 6
        else:
            # 更深层：字母+、（a、/ b、/ …），Heading L7。
            # ✅ 所有 rel≥1 共享同一个 L7 字母计数器——避免 rel=1 与 rel=2 各自从 a、
            # 起算而撞号。例如 rel1→a、、rel1→b、、rel2→c、… 单调递增。
            l7 = sub_counters.setdefault("l7_count", 0)
            l7 += 1
            sub_counters["l7_count"] = l7
            idx = min(l7 - 1, len(ALPHABET) - 1)
            letter = ALPHABET[max(idx, 0)]
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


_FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^`~\n]*)$")
_CJK_TAIL_RE = re.compile(r"[，。；：！？…]\s*$")


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

    语义上有意**比 CommonMark 宽松**：闭围栏只看同种字符、不要求长度 ≥ 开围栏、
    也不排除带标签（见 read_fenced_block）。原因：AI 输出高频出现
    ``4 反引号开 + 3 反引号闭`` 的错配，严格判未闭合会让整块图表被丢弃；
    而 content_utils.find_unclosed_fences 的严格实现只用于"文档末尾悬着半块围栏"
    的落库前自动补齐，两者用途不同、互不冲突。
    """
    m = _FENCE_LINE_RE.match(line)
    if not m:
        return None
    marker = m.group(1)
    return marker[0], len(marker), m.group(2).strip()


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
        pf = parse_fence_line(lines[i])
        if pf is not None and pf[0] == open_char:
            # 命中闭合围栏（同种字符；长度判据见 parse_fence_line 的宽松语义说明）
            break
        code_lines.append(lines[i])
        i += 1
        if len(code_lines) > max_lines:
            # 命中行数上限：做**有界前视**（再扫最多 max_lines 行）寻找结束围栏。
            _j = i
            _extra = 0
            while (_j < len(lines)
                   and not (parse_fence_line(lines[_j]) is not None
                            and parse_fence_line(lines[_j])[0] == open_char)
                   and _extra <= max_lines):
                _j += 1
                _extra += 1
            if _j < len(lines) and parse_fence_line(lines[_j]) is not None \
                    and parse_fence_line(lines[_j])[0] == open_char:
                _extended = code_lines + lines[i:_j]
                # 防误吞守卫：图表代码行（Mermaid 语句 / JSON）不会以中文句读收尾；
                # 围栏内混入句读行 = "未闭合块吞正文"，仍按未闭合处理。
                if not any(_CJK_TAIL_RE.search(ln) for ln in _extended):
                    return _extended, "recovered", _j + 1
            _pipe_logger.warning(
                "内联代码块超过 %d 行且窗口内无干净闭合，判为未闭合（截断点行号=%d）",
                max_lines, i)
            return code_lines, "truncated", i
    # 循环正常结束：要么命中结束围栏（break 退出），要么到达 EOF。
    if i < len(lines):
        _pf_end = parse_fence_line(lines[i])
        if _pf_end is not None and _pf_end[0] == open_char:
            return code_lines, "closed", i + 1
    return code_lines, "eof", len(lines)
