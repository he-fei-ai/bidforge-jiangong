"""目录生成质量校验 —— 围绕方案名称的**全面性** + 目录**连续性**（2026-09-27）

为什么需要本模块（探查结论，均有代码依据）
------------------------------------------
1. ``services.scheme_basis`` 已把方案名称确定性解析为「主要施工内容 / 工序 / 工艺 /
   对象 / 危大分类 / 标准章节模板 key」，但目录生成**从未用它做过覆盖校验**：
   名称里的工序/工艺/对象是否在目录里有落点，全靠提示词 0.1 条款自觉，
   生成完即无人复核 → 要求四「全面性校验」在代码里完全缺实现。
2. 目录连续性只有「生成时裁剪到三级 + renumber 重排」这一层，生成**之后**没有
   任何自动校验（层级跳级 / 编号错位 / 同名章节）→ 要求三「生成后自动校验」
   缺实现。
3. 本模块**纯函数、零 IO、零 AI**，可在生成前/生成后/单测任意调用；校验只产出
   报告，由调用方决定"外科补齐"还是"仅告警"。

设计红线（对齐 AGENTS.md §3.1 与需求「不得编造」）
------------------------------------------------
- 校验**只读**：不修改、不删除任何目录节点；
- 「冗余章节」只**报告**不删除（删章节是不可逆的用户数据变更）；
- 命中判定一律**偏宽**：宁可少报缺失，也不误触发一次外科补齐 AI 调用。
"""
from __future__ import annotations

import logging
import re
import unicodedata

from app.services.duplicate_detection import find_similar_titles

logger = logging.getLogger("outline_quality")

#: 与方案名称无关但属"通用必要章节"的标题特征（命中即视为围绕名称合理存在）。
#: 与 ``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS``（危大 10 章）互补而不重复：
#: 本表只用于"冗余候选"报告，不参与缺失判定。
GENERIC_CHAPTER_HINTS: tuple[str, ...] = (
    "工程概况", "工程概述", "编制依据", "施工计划", "施工部署", "施工总平面",
    "施工工艺", "工艺技术", "安全保证", "安全保障", "人员分工", "组织机构",
    "验收", "应急", "计算书", "图纸", "监测", "质量保证", "质量管理",
    "环保", "文明施工", "成本", "进度计划", "风险管理", "新技术", "BIM",
)

#: 名称维度 → 中文标签（报告与提示词共用，避免两处各写一份）
DIMENSION_LABELS: dict[str, str] = {
    "scope_items": "主要施工内容",
    "process_steps": "施工工序",
    "techniques": "施工工艺",
    "objects": "施工对象",
}

_DOT_ID_RE = re.compile(r"\d+(?:\.\d+)*")
_STRIP_RE = re.compile(r"[\s、，,。；;：:（）()【】\[\]]+")
#: F4 标题中的破折号长尾分隔符（单/双 em dash、双连字符、双 en dash）。
#: 命中即说明把描述性长尾拼进了标题（如「门窗安装工程 — 成品门窗安装…」）。
_DASH_TITLE_RE = re.compile(r"\s(?:—{1,2}|──|–{2}|--)\s")


def _has_dash_title(title: str) -> bool:
    """判断标题是否用破折号拼接了描述性长尾（F4）。"""
    return bool(_DASH_TITLE_RE.search(str(title or "")))


#: ✅ 2026-10-16（R87 FIX-4）：**零宽 / 不可见字符族**。
#: 一个不可见字符即可让**全部**判据同时失效 —— 实测 `措\u200b施1`、`措施1\u200b`、
#: `\ufeff措施1` 三种放置下，`is_generic_title` 与 `detect_generic_titles` 全部
#: **逃逸**（连 R85 之前就有的 `GENERIC_TITLE_PATTERNS` 字面量枚举一并被打穿）。
#: 来源真实：AI 输出偶发零宽字符、从 Word / 网页复制粘贴的标题也常带
#: U+200B / U+FEFF / U+00AD（软连字符）。
#: ⚠️ 这些字符对**用户完全不可见** —— 用户在目录树里看到的是「措施1」，
#: 系统却判它不是占位名。
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad\u180e\u034f]+")


def _canon(text: str) -> str:
    """标题**判据用**的规范形（仅供比较，绝不用于展示/落库）。

    两步，缺一不可：
      1. **剥零宽族** —— 否则一个不可见字符就让全部判据失效（R87 FIX-4）；
      2. **NFKC 归一** —— 把全角数字/字母/括号折成 ASCII
         （「措施１」→「措施1」、「监测措施（2）」→「监测措施(2)」）。
         顺带统一同形异码（CJK 兼容字），这是「同一判据两份取值」的另一条漂移路径。

    ⚠️ 只在**比较语境**使用（`_norm` / `has_trailing_counter_suffix`），
    其返回值全部用作 dict 键 / 正则入参 / 比较基准，**从不出现在任何用户
    可见文本里** ⇒ 归一不会改写用户数据。
    """
    s = _INVISIBLE_RE.sub("", str(text or ""))
    try:
        return unicodedata.normalize("NFKC", s)
    except (TypeError, ValueError):  # pragma: no cover - normalize 对 str 不会抛
        return s


#: ✅ 2026-10-16（R85 BUG-B）：标题尾部「（N）」计数后缀（整组剥除）。
#: AI 编号歧义把重复章节记成「专项6（3）」（第 3 次出现）——去重必须归一为
#: 「专项6」才能与本体碰撞；旧实现只剥括号留数字（「专项63」），与本永不
#: 存在的标题互不碰撞，重复全部漏报。
#:
#: ⚠️ **R85 值域拓宽（修 BUG-B）**：旧值域只收 1~3 位阿拉伯数字，于是
#: 「总体实施策划（三）」（中文数字）与「（1234）」两个形态**同时逃过检测侧
#: 与回写红线**——AI 换一种写法即可绕过全部占位闸门，用户报的症状原样复发。
#: 现补中文数字（一~十，规范方案里「第N章」用汉字，故 AI 消歧用汉字同样常见）
#: 与 4 位数字；判定刻意**只认「整组位于标题末尾」**的形态，绝不匹配正文中
#: 出现的括号数字（如「按第（3）条执行」不在标题末尾 ⇒ 不命中）。
#: ✅ R89（2026-10-17）：值域由「一~十」扩到「一~千」——原值域使
#: 「专项一百」「总体实施策划（一百）」这类百位/千位中文序号**同时逃过**
#: 结构判据（`GENERIC_STEM_ORDINAL_RE`）与冲突后缀判据
#: （`_TRAILING_COUNTER_SUFFIX_RE` 共用同一字符表）。两个判据声明的意图就是
#: 「中文数字序号」，补全值域只是兑现声明，不改变任何已命中形态的结论。
_CN_ORDINAL_CHARS = "一二三四五六七八九十零百千两"
_TRAILING_COUNTER_SUFFIX_RE = re.compile(
    rf"[（(]\s*(?:\d{{1,4}}|[{_CN_ORDINAL_CHARS}]{{1,3}})\s*[)）]\s*$")

def _norm(text: str) -> str:
    return _STRIP_RE.sub("", _TRAILING_COUNTER_SUFFIX_RE.sub("", _canon(text)))


def has_trailing_counter_suffix(title: str) -> bool:
    """标题尾部是否带「（N）」/「(N)」类编号冲突后缀（形态判据，单一事实源）。

    ✅ R79 C2：`sse_handlers._apply_title_updates`（外科式标题重写）据此**拒收**
        AI 回传的新标题。背景：AI 消歧会把重复章节写成「总体实施策划（2）」，
        而 `detect_generic_titles` 的 `conflict_suffix` 判定要求「去后缀基名与
        其它节点碰撞」才报（刻意保守，防误伤「深基坑支护（2）」这类合法带部位
        限定标题）⇒ 单个后缀样本复检恒过 ⇒ 「专项6」被改写成「钢内支撑安装（2）」
        后一路落库，用户报的症状经外科式重写复发。

    ⚠️ 与 `detect_generic_titles` 的 conflict_suffix 是**两个不同用途的判据**，
       勿合并：检测侧必须防误报（要看碰撞），回写侧必须防复现（只看形态）。
       导出的 re-export 文案不参与任何「ok」结论，仅用于回写拒因。

    ✅ R87 FIX-4：入参先过 `_canon`（剥零宽族 + NFKC 归一）—— 否则
    「监测措施（2）\\u200b」这类**用户完全看不见**的尾随零宽字符会让本判据
    返回 False，占位名原样落库。
    """
    return bool(_TRAILING_COUNTER_SUFFIX_RE.search(_canon(title)))


#: ✅ 2026-10-10（目录占位/泛化标题收口）：三级标题大量出现占位式泛化名称
#: （生产实证「专项6」「总体实施策划（2）」「总体实施策划（3）」），且带
#: 「（N）」编号冲突后缀。这类标题不是合法目录，必须程序化检出并交由修复链路重写。
#: ✅ 2026-10-13（遗留项①收口）：补全「有关要求」「等等」「若干」等纯语义空话标题
#: 及英文等价模式（etc/misc/other），覆盖 AGENTS.md 记录的检出率上限缺口。
#: 判定刻意偏保守（精确锚定，绝不按子串模糊匹配），避免误伤「专项施工方案」
#: 等合法标题。
GENERIC_TITLE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^专项\d+$"),                       # 专项6 / 专项12
    re.compile(r"^总体实施策划"),                     # 总体实施策划（2）
    re.compile(r"^主要内容$"), re.compile(r"^主要内容[（(]"),
    re.compile(r"^其他$"), re.compile(r"^其他内容$"), re.compile(r"^其他章节$"),
    re.compile(r"^其它$"), re.compile(r"^其它内容$"),
    re.compile(r"^通用$"), re.compile(r"^通用要求$"), re.compile(r"^通用内容$"),
    re.compile(r"^未命名$"), re.compile(r"^无名$"), re.compile(r"^无标题$"),
    re.compile(r"^空白$"), re.compile(r"^占位$"),
    re.compile(r"^示例$"), re.compile(r"^样例$"), re.compile(r"^模板$"),
    re.compile(r"^待定$"), re.compile(r"^暂定$"),
    re.compile(r"^补充$"), re.compile(r"^补充内容$"), re.compile(r"^相关章节$"),
    re.compile(r"^未填写$"), re.compile(r"^默认章节?$"), re.compile(r"^临时$"),
    re.compile(r"^测试章节?$"), re.compile(r"^新建章节?$"), re.compile(r"^新建$"),
    re.compile(r"^第\d+[章节](?:占位|示例|测试)?$"),
    re.compile(r"^章节\d+$"), re.compile(r"^小节\d+$"),
    re.compile(r"^无标题章节?$"),
    # ✅ 2026-10-13：纯语义空话标题（「有关要求」类）—— AGENTS.md 遗留项①收口。
    #    这类标题不携带任何具体施工内容信息，AI 用来填充目录结构，命中即视为占位。
    re.compile(r"^有关要求$"),          # 有关要求 / 有关要求（2）
    re.compile(r"^要求$"),               # 仅「要求」无修饰（技术要求/安全要求 不算）
    re.compile(r"^等等$"),               # 等等
    re.compile(r"^若干"),                # 若干 / 若干要求 / 若干说明
    re.compile(r"^其他要求$"), re.compile(r"^其他措施$"),
    re.compile(r"^其他说明$"), re.compile(r"^其他补充$"),
    re.compile(r"^相关措施$"), re.compile(r"^相关说明$"),
    re.compile(r"^相关补充$"), re.compile(r"^相关内容$"),
    re.compile(r"^补充说明$"), re.compile(r"^补充要求$"),
    re.compile(r"^补充措施$"),
    # 英文等价（AI 偶发英文占位标题）
    re.compile(r"^etc", re.IGNORECASE),
    re.compile(r"^misc", re.IGNORECASE),
    re.compile(r"^other", re.IGNORECASE),
)


#: ✅ 2026-10-16（R85 BUG-A）：「泛化词干 + 纯序号」结构化占位判据。
#:
#: **问题（用户症状的直接复发路径）**：上方 ``GENERIC_TITLE_PATTERNS`` 是
#: **字面量枚举**，`^专项\d+$` 只覆盖「专项 + 阿拉伯数字」这一种形态。AI 只要
#: 换一种同义写法即可让占位名**完全逃过检测**，实测（`sse_handlers`
#: 外科式标题重写端到端）把「专项6」改写成下列任一形态时，
#: ``applied=1 / rejected=0`` 且复检 ``generic_residual=0``（闸门报「干净」）：
#:     专项方案6 / 专项六 / 专项措施6 / 措施3 / 内容2
#: ⇒ 占位闸门形同虚设，用户报的症状原样复发，而 6 份既有护栏全绿。
#:
#: **解法**：把枚举升级为**结构判据** —— 标题整体等于「泛化词干（可选）+ 序号」
#: 时视为占位。两处刻意的保守约束：
#:   ① **全串锚定**（``^…$``）：词干表是「纯占位词」闭集，故真实复合标题
#:      （「专项施工方案」「监测措施」「施工要求」）结构上不可能命中
#:      ——「专项」后面跟的是「施工方案」既非空也非序号，直接不匹配；
#:   ② **裸词干**（无序号）只在**窄白名单**内成立（「专项/措施/内容/要点/
#:      其它/其他」）：这些词单独成句即为空话；「方法/步骤/流程/要求/说明」
#:      等可能构成正常章节名，**必须带序号**才判占位。
GENERIC_STEM_ORDINAL_RE = re.compile(
    r"^(?:专项方案|专项措施|专项|措施|内容|要点|方法|步骤|流程|工序|要求|说明"
    r"|保障|安排|其它|其他)"
    # ✅ R87 FIX-3：值域由 `[0-9]`（**ASCII 显式范围**）改为 `\d`，与同模块兄弟
    #   判据 `_TRAILING_COUNTER_SUFFIX_RE` 的 `\d{1,4}` 对齐 —— Python 的 `\d`
    #   匹配 Unicode Nd（含全角）。否则同一模块里出现「括号路径收全角、序号
    #   路径不收」的值域分叉：`_norm("措施（１）")=="措施"`（被结构判据收）
    #   而 `is_generic_title("措施１")==False`（逃逸）。
    #   （主防线仍是 `_canon` 的 NFKC 归一，此处是让本谓词**自身**自洽、
    #   独立调用时也不分叉。）
    rf"(?:\d{{1,3}}|[{_CN_ORDINAL_CHARS}]{{1,3}})?$"
)
#: 裸词干白名单（无序号也判占位的**封闭**集合；刻意不含「要求/说明/方法」等）
GENERIC_BARE_STEMS: frozenset[str] = frozenset(
    {"专项", "措施", "内容", "要点", "其它", "其他"})

# ============================================================================
# ✅ R89（2026-10-17 · 用户投诉形态复发收口）
# ----------------------------------------------------------------------------
# 用户反馈：目录三级标题仍是「专项6」「总体实施策划（2）」「总体实施策划（3）」
# 等占位泛化名称并带（N）编号冲突后缀。核查结论分两类，需分别修：
#
# ① **真漏检**（下方 R89 判据补齐）：
#     - 「总体方案」「总体部署」等**裸占位词**——旧字面枚举只列了
#       `^总体实施策划`（前缀），「总体方案」不匹配；
#     - 「章节」「小节」等裸词——不在 `GENERIC_BARE_STEMS`（该集合刻意保守，
#       R85 护栏 test_bare_stem_whitelist_is_narrow 锁定为 6 词精确相等，
#       故本轮**不改它**，另立本表，两层语义不混）；
#     - 「专项6：土方开挖」「专项6-降水」「专项6、土方」——旧结构判据
#       `GENERIC_STEM_ORDINAL_RE` 要求**全串**等于「词干+可选序号」，
#       序号后带任何描述性长尾即整条逃逸（R85 文档 §2 记录过同类逃逸，
#       但当时只补了词干，没补长尾方向）。
#
# ② **检出但未阻断**（不在本表范围内，见 config.outline_autofix_generic_on_save）：
#     「总体实施策划（2）」已被 `^总体实施策划` 命中为 generic，
#     但写路径只加法式回传 `generic_residual` 不阻断（R79 起的设计纪律），
#     用户忽略告警即原样落库。本轮在写路径补一道「落库前自动外科式重写」。
#
# ⚠️ 全部经 `outline_generic_extended_patterns`（默认 True）门控：置 False
#    即逐字回到 R89 之前（新增判据整体不生效），既有判据一字未动。
# ============================================================================

#: 裸占位词（整串等于该词即视为占位；与 `GENERIC_BARE_STEMS` 分层——后者是
#: 「结构判据里词干+可选序号」的裸词白名单，两者判据语义不同、不得合并）。
#: 只收「单独成句即不含任何施工信息」的词，刻意不收「部署/方案/要求」等
#: 可能构成正常章节名的通用词。
GENERIC_BARE_WORDS_R89: frozenset[str] = frozenset(
    {"章节", "小节", "总体方案", "总体部署", "专项部署",
     "总体策划", "部署安排", "总体部署安排"})

#: 「总体…」族泛化前缀（与既有 `^总体实施策划` 同族：只写「总体+泛化词」，
#: 不含具体施工对象/工序）。
GENERIC_TITLE_PATTERNS_R89: tuple[re.Pattern, ...] = tuple(
    re.compile(rf"^{w}$") for w in sorted(GENERIC_BARE_WORDS_R89)) + (
    re.compile(r"^总体实施策划"),          # 既有 R2，此处显式复述以便整族一起门控
    re.compile(r"^总体方案"), re.compile(r"^总体部署"),
    re.compile(r"^总体策划"), re.compile(r"^总体安排"),
    re.compile(r"^总体布置"), re.compile(r"^总体技术"),
    re.compile(r"^专项部署"), re.compile(r"^专项安排"),
    re.compile(r"^专项内容"), re.compile(r"^部署安排"),
)

#: ✅ R89：「泛化词干 + 序号 + 描述性长尾」逃逸形态。
#:
#: 在 `_norm` 的输出上判定（`_STRIP_RE` 已剥空白与常见标点），故
#: 「专项6：土方开挖」→「专项6土方开挖」、「专项6、土方」→「专项6土方」
#: 都归一到「词干+序号+长尾」同一形态；破折号（—/–/-）不在 `_STRIP_RE` 内
#: （F4 另有破折号长尾判据，不在此处改 `_STRIP_RE`），故长尾前允许至多 3 个
#: 非 CJK/数字/字母的分隔字符。
#:
#: 三处刻意的保守约束（防误伤真实复合标题）：
#:   ① **必须带序号**（\d{1,3} 或中文序号）——「专项6土方开挖」有数字前缀，
#:      说明它是「第 6 个专项」的编号式命名，而非正常章节名；
#:   ② **长尾必须是 ≥2 个汉字**——「方法100人」「措施2023年度」这类
#:      长尾首字符是数字即不命中（正常数量/年份型标题）；
#:   ③ **全串锚定**——与 `GENERIC_STEM_ORDINAL_RE` 同纪律，绝不子串模糊匹配，
#:      故「专项6土方开挖措施及验收」命中，而「基坑专项6土方开挖方案」不命中。
GENERIC_STEM_ORDINAL_TAIL_RE = re.compile(
    r"^(?:专项方案|专项措施|专项|措施|内容|要点|方法|步骤|流程|工序|要求|说明"
    r"|保障|安排|其它|其他)"
    rf"(?:\d{{1,3}}|[{_CN_ORDINAL_CHARS}]{{1,3}})"
    r"[^\u4e00-\u9fff0-9A-Za-z]{0,3}"
    r"[\u4e00-\u9fff]{2,}$")

#: R89 判据总闸默认值（实际读取 config.outline_generic_extended_patterns）
_R89_GENERIC_DEFAULT = True


def _r89_extended_on() -> bool:
    """R89 扩展判据是否启用（配置不可用时按**开启**处理 —— 补齐漏检，不静默放过）。"""
    try:
        from app.config import settings
        return bool(getattr(settings, "outline_generic_extended_patterns",
                            _R89_GENERIC_DEFAULT))
    except Exception:
        return _R89_GENERIC_DEFAULT


def _is_generic_r89(norm: str) -> bool:
    """R89 扩展判据（内部纯函数，零 IO）。入参必须是 :func:`_norm` 的输出。"""
    if not norm:
        return False
    if norm in GENERIC_BARE_WORDS_R89:
        return True
    if GENERIC_STEM_ORDINAL_TAIL_RE.match(norm):
        return True
    return any(pat.search(norm) for pat in GENERIC_TITLE_PATTERNS_R89)



def _is_stem_ordinal_generic(norm: str) -> bool:
    """「泛化词干（可选序号）」结构化占位判定（内部纯函数，零 IO）。

    入参必须是 :func:`_norm` 的输出（已剥「（N）」后缀与空白标点）。
    """
    if not norm:
        return False
    if norm in GENERIC_BARE_STEMS:
        return True
    return bool(GENERIC_STEM_ORDINAL_RE.fullmatch(norm))


def is_generic_title(title: str) -> bool:
    """单个标题是否命中「泛化/占位」模式（纯函数，零 IO）。

    与 :func:`detect_generic_titles` 用**同一份** ``GENERIC_TITLE_PATTERNS``、
    同一个 ``_norm``（先剥「（N）」计数后缀再匹配）与同一个
    ``_is_stem_ordinal_generic``（R85 结构判据）—— 单一判据事实源，
    严禁在调用方另抄一套正则。供「标题改写」一类需要**逐条校验新标题**
    的场景使用：改写结果若仍是泛化名，就地拒绝落回目录。
    """
    norm = _norm(str(title or "").strip())
    if not norm:
        return False
    # ✅ R85 BUG-A：结构判据与字面量枚举**或**关系（任一命中即占位）。
    #    放在同一出口 ⇒ 外科式重写回写红线与检出闸门同时收紧，不会分叉。
    if _is_stem_ordinal_generic(norm):
        return True
    if _r89_extended_on() and _is_generic_r89(norm):
        return True
    return any(pat.search(norm) for pat in GENERIC_TITLE_PATTERNS)


def detect_generic_titles(outline: list) -> list[dict]:
    """检出目录中的泛化/占位标题与「（N）」编号冲突后缀（纯函数，零 IO）。

    返回 ``[{path, title, kind}]``，``kind`` 为 ``generic``（泛化/占位名称）
    或 ``conflict_suffix``（带「（N）」类后缀且去后缀基名与其它节点碰撞）。
    判定偏保守：仅精确锚定，不按子串模糊匹配，避免误伤「专项施工方案」
    等合法标题；``（N）`` 后缀仅当基名本身泛化、或与其它节点碰撞（即
    AI 用来消歧的编号冲突后缀）时才报，避免「深基坑支护（2）」这类合法
    带部位限定标题被误报。

    两处消费：
    - ``check_outline_continuity`` 计入 ``generic_titles`` issue（影响质量报告 ok）；
    - ``sse_handlers._review_and_fix_outline`` 据此强制进入修复链路重写。
    """
    entries = collect_titles(outline)
    # 归一化基名频次：用于「（N）编号冲突后缀」的碰撞判定（去后缀后基名出现 ≥2 次）
    norm_count: dict[str, int] = {}
    for e in entries:
        t = _norm(str(e.get("title") or "").strip())
        if t:
            norm_count[t] = norm_count.get(t, 0) + 1

    hits: list[dict] = []
    for e in entries:
        title = str(e.get("title") or "").strip()
        if not title:
            continue
        norm = _norm(title)  # 已去除（N）后缀
        kind = None
        if is_generic_title(title):
            kind = "generic"
        # ✅ R87 FIX-6：改调 `has_trailing_counter_suffix` **单一出口**。
        #    旧实现直接拿 `_TRAILING_COUNTER_SUFFIX_RE.search(title)` 判**原文**，
        #    绕过了该函数内部的 `_canon`（剥零宽族 + NFKC）⇒
        #    「钢内支撑安装（三）\u200b」检测侧 MISS、而回写红线收 ⇒ 两侧分叉。
        #    本仓最高频反模式「同一判据两份副本」在此处的第四份副本。
        if kind is None and has_trailing_counter_suffix(title):
            # 仅当去后缀基名与其它节点碰撞（编号冲突后缀）才报，
            # 排除「深基坑支护（2）」这类合法带部位限定标题
            if norm and norm_count.get(norm, 0) >= 2:
                kind = "conflict_suffix"
        if kind:
            hits.append({"path": e.get("path"), "title": title, "kind": kind})
    return hits


#: 连续性各类问题的中文标签（**唯一事实源**，跨侧共享口径）。
#: 两处消费必须与此保持一致：
#:   · 后端日志摘要 :func:`render_continuity_notice`（引用本常量，不再各写副本）；
#:   · 前端 `SchemeWorkbenchPage.tsx::summarizeOutlineQuality` 的 labels 表
#:     （跨语言无法共享代码，由 R73b 护栏做键集合 parity 锁定）。
#: ⚠️ `check_outline_continuity` 的 issues **新增一类必须同步这里**：否则该类计数
#:    会在后端日志与前端界面**双双被静默丢弃** —— R73b F5 实证 `generic_titles`
#:    加入 issues 时两处标签表都没跟上，结果占位标题「检出了却谁也没看见」。
#: ⚠️ 只收录**缺陷类**（进 issues、影响 `ok`）。`similar_titles` 这类**组织建议**
#:    附录键（不进 issues、不改 ok）**不得**登记于此，否则会被当成缺陷播报。
CONTINUITY_ISSUE_LABELS: dict[str, str] = {
    "level_gaps": "层级跳级",
    "empty_parents": "有父无子",
    "duplicate_titles": "同名章节",
    "numbering_mismatch": "编号错位",
    "dash_titles": "破折号长尾标题",
    "empty_titles": "空标题节点",
    "generic_titles": "占位/泛化标题",
}


def collect_titles(outline: list, *, max_level: int | None = None) -> list[dict]:
    """展平目录为 ``[{level, path, title, text}]``（text = 标题 + 描述）。

    ``path`` 为点分**位置**路径（与 outline_json.id 同构）；AI 生成结果的 id 会被
    renumber 覆写，用位置路径做报告定位更稳定。
    """
    out: list[dict] = []

    def _walk(nodes: list, level: int, parent_path: str) -> None:
        if not isinstance(nodes, list) or level > 12:
            return
        # ✅ R79：非法项（非 dict）不占用位置编号 —— 与
        #    `numbering.renumber_outline_nodes`（valid_idx 只数 dict）和
        #    `sse_handlers._outline_path_index`（k 只数 dict）严格同口径。
        #    旧实现 enumerate 全项：脏数组 `[dict, "X", dict]` 报出 path '3'，
        #    而外科式标题重写侧 `_outline_path_index` 只认 '1'/'2' ⇒
        #    「检出 path 在修复侧不存在」⇒ 占位标题检出了却修不掉
        #    （detect_generic_titles 的冲突后缀判定同样受影响）。
        valid_idx = 0
        for n in nodes:
            if not isinstance(n, dict):
                continue
            valid_idx += 1
            path = f"{parent_path}.{valid_idx}" if parent_path else str(valid_idx)
            title = str(n.get("title") or "").strip()
            if (max_level is None or level <= max_level) and title:
                out.append({"level": level, "path": path, "title": title,
                            "text": f"{title} {str(n.get('description') or '')}"})
            _walk(n.get("children") or [], level + 1, path)

    _walk(outline or [], 1, "")
    return out


def _common_run(a: str, b: str) -> int:
    """最长公共子串长度（标题/关键词都很短，DP 足够，无性能顾虑）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _keyword_hits(kw: str, entries: list[dict]) -> list[str]:
    """关键词在目录中的落点（返回命中的 path）。判定刻意偏宽：
    - 子串包含（"土方开挖" ⊂ "土方开挖及支护"）；
    - 或与标题/描述有足够长的公共子串（"开挖" 也能命中"基坑土方开挖"）。
    """
    k = _norm(kw)
    if len(k) < 2:
        return []
    hits: list[str] = []
    for e in entries:
        t = _norm(e.get("title") or "")
        d = _norm(e.get("text") or "")
        if (k and (k in t or (d and k in d))) or _common_run(k, t) >= 2 \
                or (d and _common_run(k, d) >= 3):
            hits.append(e["path"])
    return hits


# ---------------------------------------------------------------------------
# 要求四：围绕方案名称的全面性
# ---------------------------------------------------------------------------
def analyze_name_coverage(basis, outline: list) -> dict:
    """方案名称关键词 → 目录章节 的覆盖分析（**只读**）。

    Args:
        basis: ``scheme_basis.SchemeBasis``（None / 无任何维度 → 返回"未评估"）。
        outline: 目录树（AI 生成 / 目录库 / 上传识别 / 手动编辑 皆可）。

    Returns::

        {evaluated, dimensions, matrix:[{dim,item,hits,covered}],
         missing:[str], covered:bool, total_items:int}

    ``missing`` 是「方案名称里有、目录里无任何落点」的关键词（中文化标签前缀），
    供调用方喂给外科式补齐（``_try_outline_patch``）——这是要求四的**唯一出口**。
    """
    if basis is None:
        return {"evaluated": False, "dimensions": dict(DIMENSION_LABELS),
                "matrix": [], "missing": [], "covered": True, "total_items": 0}
    dims = {k: list(getattr(basis, k, None) or []) for k in DIMENSION_LABELS}
    total = sum(len(v) for v in dims.values())
    if total == 0:
        # 名称里没有可解析维度 → 无法评估（**不得**据此判目录不合规）
        return {"evaluated": False, "dimensions": dict(DIMENSION_LABELS),
                "matrix": [], "missing": [], "covered": True, "total_items": 0}

    entries = collect_titles(outline)
    matrix: list[dict] = []
    missing: list[str] = []
    for dim, items in dims.items():
        for it in items:
            hits = _keyword_hits(it, entries) if entries else []
            matrix.append({"dim": dim, "item": it, "hits": hits,
                           "covered": bool(hits)})
            if not hits:
                missing.append(f"{DIMENSION_LABELS[dim]}·{it}")
    return {"evaluated": True, "dimensions": dict(DIMENSION_LABELS), "matrix": matrix,
            "missing": missing, "covered": not missing, "total_items": total}


def find_redundant_titles(basis, outline: list) -> list[dict]:
    """列出"与方案名称零关联且非通用必要章节"的标题候选（**只报告不删除**）。"""
    if basis is None:
        return []
    try:
        kws = {k for k in basis.keywords() if len(k) >= 2}
    except Exception:  # pragma: no cover - 防御
        kws = set()
    if not kws:
        return []
    out: list[dict] = []
    for e in collect_titles(outline, max_level=2):
        t = str(e.get("title") or "")
        if not t or any(h in t for h in GENERIC_CHAPTER_HINTS):
            continue
        tn = _norm(t)
        if not any(kw in tn for kw in kws):
            out.append({"path": e["path"], "title": t, "level": e["level"]})
    return out


# ---------------------------------------------------------------------------
# 要求三：目录连续性
# ---------------------------------------------------------------------------
def check_outline_continuity(outline: list) -> dict:
    """目录连续性校验（层级 / 编号 / 重复章节 / 空壳 / 标题格式），返回结构化报告。

    检查项：
    - ``level_gaps``：子节点声明的 ``level`` ≠ 父层级 + 1（跳级）；
    - ``empty_parents``：children 非空但其下无任何有效标题（有父无子壳）；
    - ``duplicate_titles``：同名标题（跨层也记，附首次出现路径便于定位）；
    - ``numbering_mismatch``：节点 id 为点分路径却与位置编号不一致（编号错位）；
    - ``dash_titles``（F4）：标题用破折号拼接描述性长尾；
    - ``empty_titles``（F5）：title 为空白的 dict 节点（旧逻辑静默跳过）；
    - ``max_level`` / ``nodes``：规模指标。

    F4/F5 受配置 ``outline_check_title_format``（默认 True）门控；关闭时空节点
    仍跳过、破折号不查，逐字回到旧口径。

    附加项（不进 ``issues``、不影响 ``ok``）：
    - ``similar_titles``：**近似**雷同标题对（2-gram Dice ≥ 0.70 且不完全同名，
      如「基坑降水与支护施工」vs「基坑降水及支护施工」）。这是**目录组织建议**
      而非结构缺陷 —— 真正的同名章节由 ``duplicate_titles`` 判为缺陷，
      此处若也计入 ``issues`` 会让 ``ok`` 误变 False、进而误判目录不合格。
    """
    # ✅ BUG 修复（2026-10-11 · 跨模块数据链 R70 · fail-soft 反噬成 NameError）：
    #    旧实现只在这一段 try 里 import settings，except 分支**仅**设置了
    #    check_fmt 而没有绑定 settings；而下方（泛化标题检测处）仍在 try 之外
    #    `getattr(settings, "outline_check_generic_titles", True)` ——
    #    一旦 `from app.config import settings` 失败（配置模块初始化异常 / 被
    #    单测隔离替换），这里不抛、下方必抛 NameError，整个目录质量校验崩掉；
    #    且该 NameError 发生在「防御性 fail-soft」之后，属典型的容错反噬。
    #    修法：所有配置读取集中在同一 try 内完成并各自给默认值，
    #    下方只读局部变量，不再引用可能未绑定的模块名。
    try:
        from app.config import settings
        check_fmt = bool(settings.outline_check_title_format)
        check_generic = bool(getattr(settings, "outline_check_generic_titles", True))
    except Exception:  # 配置不可用时按开启处理（补齐漏检）
        check_fmt = True
        check_generic = True
    issues: dict[str, list] = {
        "level_gaps": [], "empty_parents": [],
        "duplicate_titles": [], "numbering_mismatch": [],
        "dash_titles": [], "empty_titles": [], "generic_titles": [],
    }
    seen: dict[str, str] = {}
    counter = {"nodes": 0, "max_level": 0}
    # ✅ 新增（2026-10-01）：收集 (path, title) 供标题近似雷同检测使用
    titles: list[dict] = []

    def _visit(node: dict, level: int, path: str) -> None:
        title = str(node.get("title") or "").strip()
        if not title:
            # F5：空标题 dict 节点。旧逻辑直接静默跳过，现按配置登记。
            if check_fmt:
                issues["empty_titles"].append({"path": path})
            children = node.get("children")
            if isinstance(children, list) and children:
                _visit_children(children, level + 1, path, title)
            return
        counter["nodes"] += 1
        counter["max_level"] = max(counter["max_level"], level)
        titles.append({"path": path, "title": title})
        if check_fmt and _has_dash_title(title):
            issues["dash_titles"].append({"path": path, "title": title})
        key = _norm(title)
        if key and key not in seen:
            seen[key] = path
        elif key:
            issues["duplicate_titles"].append(
                {"path": path, "title": title, "first": seen[key]})
        nid = str(node.get("id") or "").strip()
        if nid and _DOT_ID_RE.fullmatch(nid) and nid != path:
            issues["numbering_mismatch"].append(
                {"path": path, "id": nid, "expected": path, "title": title})
        children = node.get("children")
        if isinstance(children, list) and children:
            _visit_children(children, level + 1, path, title)

    def _visit_children(children: list, level: int, parent_path: str,
                        parent_title: str) -> None:
        valid = 0
        # ✅ BUG 修复（2026-10-07 · 编号错位误报）：位置路径必须与
        #    numbering.renumber_outline_nodes 的占号口径一致 —— 后者只对
        #    isinstance(node, dict) 的项连续编号，**非 dict 项跳过且不占号**。
        #    旧实现用 enumerate 的原始下标 j+1 拼路径，非 dict 项也占了一个位置，
        #    于是 normalize（renumber 不删除非 dict 项、只跳过）后再做连续性校验时，
        #    脏数据数组（如 [dict, "字符串残片", dict]）里后续合法节点的真实 id
        #    （"2"）与路径（"3"）永久不等，被误报 numbering_mismatch，进而把一份
        #    编号完全正确的目录判成 ok=False。现按 dict 项连续计数。
        dict_idx = 0
        for c in children:
            if not isinstance(c, dict):
                continue
            dict_idx += 1
            cpath = f"{parent_path}.{dict_idx}" if parent_path else str(dict_idx)
            ctitle = str(c.get("title") or "").strip()
            if ctitle:
                valid += 1
            declared = c.get("level")
            if isinstance(declared, int) and declared and declared != level:
                issues["level_gaps"].append(
                    {"path": cpath, "title": ctitle, "parent": parent_title,
                     "declared": declared, "expected": level})
            _visit(c, level, cpath)
        if valid == 0 and children:
            issues["empty_parents"].append(
                {"path": parent_path, "count": len(children)})

    # 顶层同样按 dict 项连续计数（旧实现 str(i+1) 含非 dict 位置，与 renumber 漂移）
    top_idx = 0
    for n in outline or []:
        if isinstance(n, dict):
            top_idx += 1
            _visit(n, 1, str(top_idx))

    # ✅ 2026-10-10：泛化/占位标题与「（N）」编号冲突后缀检测（偏保守，默认开启）。
    #    不进 issues 的其它项：这是独立于层级/编号/重复的「标题内容质量」维度，
    #    命中即视为目录不合规（影响 ok），交由修复链路重写为具体标题。
    # ✅ R70：改读上方 try 内已解析好的局部变量（原为直接 getattr(settings, …)，
    #    配置导入失败时 settings 未绑定 → NameError，见上方注释）。
    if check_generic:
        try:
            issues["generic_titles"].extend(detect_generic_titles(outline))
        except Exception as exc:  # 防御：检测异常绝不改变连续性结论
            logger.warning("目录泛化标题检测异常（忽略，不阻断校验）: %s", exc, exc_info=True)

    # ✅ 新增（2026-10-01）：标题近似雷同（2-gram Dice）。
    # 加法式 —— 只作为顶层附加键返回，**不进入 issues**：近似标题属于
    # 「建议合并或明确区分」的组织建议，不是结构缺陷，因此不得改变
    # ``ok`` / ``issue_counts`` 的既有契约（调用方按 ok 决定是否放行）。
    # 完全同名的对由 ``duplicate_titles`` 负责，find_similar_titles 内部已跳过。
    try:
        similar_titles = find_similar_titles(titles)
    except Exception as exc:  # 防御：标题检测异常不得影响连续性结论
        logger.warning("目录标题近似雷同检测异常: %s", exc, exc_info=True)
        similar_titles = []

    return {
        "ok": not any(issues.values()),
        "nodes": counter["nodes"],
        "max_level": counter["max_level"],
        "issue_counts": {k: len(v) for k, v in issues.items()},
        "similar_titles": similar_titles,
        **issues,
    }


def render_coverage_notice(missing: list[str]) -> str:
    """把缺失的名称关键词渲染成一句话（写进审核建议 / 日志，用户可直接行动）。"""
    if not missing:
        return ""
    head = "、".join(missing[:6])
    more = f"等 {len(missing)} 项" if len(missing) > 6 else ""
    return f"方案名称涉及但目录未覆盖：{head}{more}"


def render_continuity_notice(report: dict) -> str:
    """连续性问题的一句话摘要（无问题返回空串）。

    ✅ R73b（2026-10-12 · F5「日志不准确」实证）：标签表此前是本函数内的私有
    字面量副本，且**停在 6 类旧口径** —— ``generic_titles`` 于 2026-10-10 加入
    issues 却没进标签表，于是「目录唯一问题就是占位标题」（正是生产实证的场景）
    时 ``parts`` 为空串，WARNING 渲染成「目录连续性校验未通过：」——后面什么都没有。
    现改为引用唯一事实源 :data:`CONTINUITY_ISSUE_LABELS`，并以 **`issue_counts`**
    （由 issues 派生、与前端完整同一份数据）驱动拼接：对**未登记**的问题类追加
    兜底分句，``ok=False`` 却渲染空摘要这种「静默失败」从此结构上不可能。

    ⚠️ 故意不扫「report 里所有列表键」：`similar_titles` / `redundant_titles` 这类
    附录键是**组织建议**、不是缺陷（不影响 ok），混进缺陷摘要会误导用户。
    """
    if not report or report.get("ok"):
        return ""
    labels = CONTINUITY_ISSUE_LABELS
    counts = report.get("issue_counts")
    if not isinstance(counts, dict):
        # 兼容手造/旧式报告（只带 issues 列表、不带 issue_counts）
        counts = {k: len(report.get(k) or []) for k in labels}
    parts = [f"{labels[k]} {int(counts.get(k) or 0)} 处"
             for k in labels if counts.get(k)]
    # 兜底：新增缺陷类若忘了登记标签，绝不等于可以不播报（防「检出却零呈现」）
    unlabeled = [k for k, v in counts.items() if k not in labels and v]
    if unlabeled:
        parts.append("其它问题 " + "、".join(
            f"{k} {int(counts[k])} 处" for k in unlabeled) + "（标签待补，请反馈）")
    if not parts:
        # 理论上不可达（ok=False 必然有非空 issues）；兼做诚实现式，绝不空转
        return "目录连续性校验未通过（未提供问题明细，请查看完整报告）"
    return "目录连续性问题：" + "、".join(parts)


# ---------------------------------------------------------------------------
# 要求五：目录结构规范（《文档生成规则分析与软件标准建立》第一部分 1.1）
# ---------------------------------------------------------------------------
#: 章节数量规范：危大专项方案法定九章骨架（CMP-01~09）叠加专业章后，
#: 常见成稿落在 5-10 章；推荐值仅作报告展示，不参与任何判定。
OUTLINE_CHAPTER_MIN, OUTLINE_CHAPTER_MAX = 5, 10
OUTLINE_CHAPTER_RECOMMENDED = 7
#: 每章二级节数规范（文档 3-15；本仓提示词契约「每个一级目录 ≥ 2 个二级」
#: 取更严的 2 作下限，上限沿用文档 15）。
OUTLINE_SECTION_MIN, OUTLINE_SECTION_MAX = 2, 15
#: 层级深度规范：文档标准最多 4 级；本仓提示词契约「严禁第四级」为更严口径，
#: 此处按文档 4 级做程序化兜底（模型越界输出由这道程序闸门检出）。
#:
#: ⚠️ 2026-10-12（技术债 P2-15 · 与硬上限的语义边界显式化）：
#:   本常量与 ``outline_utils.MAX_OUTLINE_DEPTH``（=3）**不是同一条判据的两份
#:   副本，而是两条不同语义的线，此前无任何交叉说明，极易被后人「顺手统一」：
#:     · ``MAX_OUTLINE_DEPTH`` = 3 —— **落库硬上限**。`normalize_outline` /
#:       `clamp_outline_depth` 会真地把第四级裁掉（标题并入父节点 description），
#:       `sections.py` 的手工新增/移动两条路径也按它 400 拦截。改它=改数据形状。
#:     · ``OUTLINE_MAX_DEPTH``  = 4 —— **规范告警线**，只产 advisory warning，
#:       不进 `issues`、不翻转 `ok`、不拦截任何写入。
#:   不变式：**告警线必须 ≥ 硬上限**。若把告警线调到 3 以下，规范建议会与
#:   落库实现直接冲突（合法目录被判违规），属误报；保持 4 则意味着本告警只在
#:   **裁剪之前的原始目录**上才有意义 —— 在 `normalize_outline` 之后调用时
#:   `max_depth` 恒 ≤ 3，该告警恒不触发（已知限制，见护栏用例注释）。
#:   ⚠️ 修改任一常量前请先读 `tests/test_cross_module_chain_r70_20261011.py`
#:      ::TestOutlineDepthSemantics 里的不变式断言。
OUTLINE_MAX_DEPTH = 4


def check_structure_standards(outline: list) -> dict:
    """目录结构规范**建议性**校验（纯函数、零 IO、绝不抛）。

    与 ``check_outline_continuity`` 的结构性缺陷（跳级/重名/错位）不同，
    本函数核对的是「规模是否落在规范区间」：章数 5-10、每章二级节数 2-15、
    总深度 ≤ 4。这些都是**组织建议**而非硬缺陷 —— 章数少于 5 的微型方案
    （如单一吊装工程）完全合法 —— 因此 warnings 只作报告呈现，
    **绝不进 issues、绝不改变连续性 ok 结论**（与 similar_titles 同一口径）。
    """
    warnings: list[dict] = []
    chapters: list = []
    max_depth = 0
    try:
        chapters = [n for n in (outline or []) if isinstance(n, dict)]
        max_depth = _max_depth(chapters)
    except Exception:  # pragma: no cover - 防御：脏结构不得炸掉质量报告
        return {"evaluated": False, "chapters": 0, "max_depth": 0,
                "recommended_chapters": OUTLINE_CHAPTER_RECOMMENDED,
                "warnings": []}

    if not chapters:
        return {"evaluated": False, "chapters": 0, "max_depth": 0,
                "recommended_chapters": OUTLINE_CHAPTER_RECOMMENDED,
                "warnings": []}

    n_ch = len(chapters)
    if not (OUTLINE_CHAPTER_MIN <= n_ch <= OUTLINE_CHAPTER_MAX):
        warnings.append({
            "kind": "chapter_count", "count": n_ch,
            "allowed": [OUTLINE_CHAPTER_MIN, OUTLINE_CHAPTER_MAX],
            "message": f"一级章节共 {n_ch} 章，超出规范区间 "
                       f"{OUTLINE_CHAPTER_MIN}-{OUTLINE_CHAPTER_MAX} 章"})
    if max_depth > OUTLINE_MAX_DEPTH:
        warnings.append({
            "kind": "max_depth", "depth": max_depth,
            "allowed": OUTLINE_MAX_DEPTH,
            "message": f"目录最深 {max_depth} 级，超出规范上限 "
                       f"{OUTLINE_MAX_DEPTH} 级"})
    for idx, ch in enumerate(chapters, 1):
        children = ch.get("children")
        n_sec = len(children) if isinstance(children, list) else 0
        if 0 < n_sec and not (OUTLINE_SECTION_MIN <= n_sec <= OUTLINE_SECTION_MAX):
            warnings.append({
                "kind": "section_count", "path": str(idx),
                "title": str(ch.get("title") or "").strip(),
                "count": n_sec,
                "allowed": [OUTLINE_SECTION_MIN, OUTLINE_SECTION_MAX],
                "message": f"第 {idx} 章二级节数 {n_sec}，超出规范区间 "
                           f"{OUTLINE_SECTION_MIN}-{OUTLINE_SECTION_MAX} 节"})
    return {
        "evaluated": True, "chapters": n_ch, "max_depth": max_depth,
        "recommended_chapters": OUTLINE_CHAPTER_RECOMMENDED,
        "warnings": warnings,
    }


def _max_depth(nodes: list, level: int = 1) -> int:
    """递归求目录最大深度（非 dict 子项跳过，与 renumber 占号口径一致）。"""
    best = level if nodes else 0
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        children = n.get("children")
        if isinstance(children, list) and any(isinstance(c, dict) for c in children):
            best = max(best, _max_depth(children, level + 1))
    return best


def render_structure_notice(report: dict) -> str:
    """结构规范建议的一句话摘要（无建议返回空串）。"""
    if not report or not report.get("warnings"):
        return ""
    return "目录结构规范建议：" + "；".join(
        str(w.get("message") or "") for w in report["warnings"][:6])


# ============================================================================
# R86（L5）· 写路径共用占位闸门 —— **单一出口**
# ============================================================================

def detect_generic_titles_for_write(outline: list, *, context: str = "") -> list[dict]:
    """目录**写路径**共用的占位/泛化标题检测（加法式，单一出口，fail-soft）。

    ✅ R86 L5（2026-10-16）：占位检测此前只有 ``sections._save_outline_to_db``
    一处接线，``create_section`` / ``update_section`` /
    ``upload_outline.save_as_outline`` 三条入口**零闸门** —— 而它们正是用户报
    的「三级标题大量专项6 / 总体实施策划（2）」的另一批来源：手工新增/改名、
    上传目录识别（该路径连 ``_save_outline_to_db`` 都不走）。

    三条语义约束（刻意与 R79 C3 保持同口径）：
      1. **只报不拒**：调用方把结果放进响应体供前端提示，**不拒收、不改写**
         任何标题 —— 静默改写用户已输入的标题是本仓红线；
      2. 受既有 ``settings.outline_check_generic_titles`` 总闸控制（置 False
         即逐字回到本项引入前：不检测、不回传该键），**零新增配置项**；
      3. 检测自身异常一律 fail-soft（返回 ``[]`` 并记 WARNING）——
         **绝不阻断保存**（占位检测是提示性增强，不是数据完整性闸门）。
    """
    try:
        from app.config import settings
        if not getattr(settings, "outline_check_generic_titles", True):
            return []
    except Exception:  # noqa: BLE001 - 配置读不到即按默认开，不阻断写路径
        pass
    try:
        return detect_generic_titles(outline)
    except Exception:  # noqa: BLE001 - 检测失败不得阻断保存
        logger.warning("目录占位标题检测失败（不影响保存）%s",  # 单占位符 + exc_info
                       f"[{context}]" if context else "",
                       exc_info=True)
        return []


def attach_generic_residual(result: dict, outline: list, *, scheme_id: str = "",
                            context: str = "") -> dict:
    """把占位残留**加法式**挂到写路径响应体（唯一回传形态）。

    与 ``_save_outline_to_db`` 的回传形态逐字一致：仅当确有残留时才加
    ``generic_residual`` 键（明细清单），并记一条 WARNING —— 前端据此提示
    「请人工核对」，用户不被静默改写，也不会被静默丢失。
    """
    hits = detect_generic_titles_for_write(outline, context=context)
    if hits:
        result["generic_residual"] = hits
        logger.warning(
            "目录写入%s：检出 %d 个占位/泛化标题（已保存，建议人工核对）：%s",
            f"（{context} scheme={scheme_id[:8]}）" if scheme_id else f"（{context}）",
            len(hits),
            "、".join(str(h.get("title") or "")[:20] for h in hits[:10]))
    return result
