"""跨章节重复检测（骨架归一 + 双阈值 Dice，纯程序、零 AI、零成本）

专项施工方案的查重需要同时解决两类问题：整章级雷同（由 CON-05 的 4-gram
Jaccard 负责）与段落级搬运（由本模块负责）。本模块实现两条通用算法线：

1. **骨架归一**：把可变数字 / 日期 / 标准编号 / 百分比 / 页码替换为语义占位符
   后再比对。正文里「基坑开挖深度 3m」与「基坑开挖深度 5m」是同一模板段落
   改了数字，按原文比对必然漏报；
2. **字符 2-gram Dice + 双阈值分级**：Dice 对短文本比 Jaccard 敏感（交集占比
   而非并集占比），双阈值按长度分档，避免短句子误报。

判定口径：
  - 骨架门控关键词、图片哈希、多份投标文件间的分组聚合属于投标文件互查语境，
    专项方案没有对应的数据源与对立关系，本模块不实现；
  - 单位归一化不在本模块职责内。「6m」与「6000mm」在骨架归一后仍是不同键。
    全仓已存在三份互不相同的单位表（`preflight_engine._UNIT_CANON` /
    `facts_classification._UNIT_TO_METER` / `content_standard._UNIT_ALIASES`），
    本模块**故意不新增第四份** —— 单位口径收敛是独立缺口，需单独立项收敛后
    本模块自动受益，而不是在这里再写一份悄悄分叉的表。

⚠️ 与 CON-05 的分工（防重复告警）：
  - ``CON-05`` = 整章级 4-gram Jaccard（两章整体雷同）；
  - ``CON-06`` = 段落级骨架归一 + Dice（两章整体不同，但有成段照抄）。
  调用方须把 CON-05 已判定的章节对传入 ``exclude_pairs``，否则同一现象
  会被两条规则同时报出。

性能
----
第二级（近似匹配）**不用朴素两两**：80 章 × 600 句 = 4.8 万句时朴素实现
需 11 亿次 bigram 集合运算（实测 >30s 未完成）。改为**倒排索引 + 高 DF
剪枝**（标准 IR 做法），同规模实测 2.3s。这是**精确剪枝**而非近似采样：
真实照抄必然共享大量低频 bigram，跳过高频 gram 不损失任何真重复
（护栏 ``test_pairwise_pruning_is_lossless``）。

所有函数均为纯函数（无 DB、无 AI、无 IO），可直接单测。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict

__all__ = [
    "BIGRAM_SIZE",
    "MIN_SENTENCE_CHARS",
    "MIN_TITLE_CHARS",
    "TITLE_DICE_THRESHOLD",
    "MAX_COPY_GROUPS",
    "COPY_GROUP_LARGE_CHARS",
    "normalize_comparable",
    "build_skeleton_key",
    "char_bigrams",
    "dice",
    "containment",
    "split_sentences",
    "sentence_similarity",
    "sentences_near_match",
    "find_cross_section_copies",
    "title_similarity",
    "find_similar_titles",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 字符 n-gram 的 n（2-gram 是中文短文本比对的经验取值）
BIGRAM_SIZE = 2

#: 参与比对的句子最小**归一化后**字符数。
#: 过短的句子 2-gram 集合极小，任意两句的 Dice 都会偏高 → 误报。
MIN_SENTENCE_CHARS = 16

#: 标题比对的最小长度（归一化后）
MIN_TITLE_CHARS = 3

#: 标题近似的 Dice 阈值。
#: 标题比正文短，2-gram Dice 对「改一个字」的惩罚天然偏重：
#: 9 字标题改 1 字（「与」→「及」）Dice 只有 0.75。实测真实目录样本：
#:   0.769 质量验收标准 / 质量验收标准及程序   ← 应合并
#:   0.750 基坑降水与支护施工 / 基坑降水及支护施工 ← 应合并
#:   0.714 模板支撑体系施工 / 模板支撑体系拆除   ← 阶段不同，建议明确区分
#:   0.545 季节性施工措施 / 雨季施工措施         ← 正常区分
#: 取 0.70 可覆盖前两类真缺陷并把 0.5 量级的正常区分排除在外。
TITLE_DICE_THRESHOLD = 0.70

#: 最多报告的搬运组数（避免刷屏，按严重度排序取前 N 组）
MAX_COPY_GROUPS = 10

#: 近似匹配的两两比对上限（O(n²) 保护）；触顶即停止并置 ``truncated=True``
MAX_PAIRWISE_COMPARISONS = 60000

#: 单章节参与比对的句子数上限（超长章节取前 N 句）
MAX_SENTENCES_PER_SECTION = 600

#: 搬运组严重度升级阈值：单句归一化字数 ≥ 此值视为「大段照抄」
COPY_GROUP_LARGE_CHARS = 60

#: 长句档：≥30 字的主体段落，偶然重合多但比例低，阈值可稍宽
_TIER_LONG: tuple[int, float, float] = (30, 0.90, 0.82)
#: 短句档：<30 字，偶然重合比例高，阈值收紧
_TIER_SHORT: tuple[int, float, float] = (0, 0.95, 0.88)

#: 双阈值分级表：``(最小归一化长度, containment 阈值, dice 阈值)``。
#: 保留表结构便于调用方遍历说明分档口径。
_SIMILARITY_TIERS: tuple[tuple[int, float, float], ...] = (_TIER_LONG, _TIER_SHORT)

# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
#: 参与比对的文本只保留「字母 / 数字」类字符，剔除空白（Z）、标点（P）、
#: 符号（S）、控制符（C）。与 ``preflight_engine._shingles`` 同一口径
#: （避免查重两套清理逻辑再次分叉）。
_KEEP_CATEGORY: tuple[str, ...] = ("L", "N")

#: 骨架键额外保留的字符：占位符边界（花括号）。
#: 若按默认口径清理，``{num}`` 会退化成 ``num``、``{code}`` 退化成 ``code`` ——
#: 不仅可读性下降，占位符还会与正文里的真实英文单词混淆，排障时无法区分
#: 「这是被归一的数字」和「原文就有一个 num」。
_SKELETON_KEEP_EXTRA: frozenset = frozenset("{}")


def _keep_only(text: str, extra: frozenset = frozenset()) -> str:
    """剔除空白 / 标点 / 符号 / 控制符，只留字母与数字。

    ``extra`` 中的字符例外保留（骨架键传占位符花括号）。
    **唯一清理实现**：normalize_comparable 与 build_skeleton_key 共用，
    避免两套清理口径再次分叉。
    """
    hit = extra.__contains__
    return "".join(ch for ch in text if hit(ch) or unicodedata.category(ch)[0] in _KEEP_CATEGORY)


def normalize_comparable(text: str) -> str:
    """归一化可比较文本：NFKC 折叠 + 剔除空白 / 标点 / 符号。

    NFKC 把全角字母数字（``３ｍ``）折成半角（``3m``），是单位写法差异
    的主要噪声来源之一；剔除标点则让「3 m」「3m」「3．m」等价。
    """
    if not text:
        return ""
    return _keep_only(unicodedata.normalize("NFKC", str(text)))


# ---------------------------------------------------------------------------
# 骨架归一（可变字段 → 语义占位符）
# ---------------------------------------------------------------------------
# 顺序敏感：先长模式（日期 / 标准编号 / 百分数）后裸数字，
# 否则裸数字规则会先把日期里的数字吃掉、留下残缺日期。
_DATE_MASK = re.compile(
    r"(?:\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{4}\s*年\s*\d{1,2}\s*月"
    r"|\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})"
)
# 标准编号：GB 50202-2018 / JGJ 120-2012 / GB/T 50502 / DBJ …（含年号）
_CODE_MASK = re.compile(
    r"(?:GB(?:/T)?|JGJ(?:/T)?|JTG(?:/T)?|DBJ|CECS|CJJ(?:/T)?)\s*" r"\d{2,5}(?:\.\d{1,3})?(?:[—\-－]\s*\d{4})?",
    re.IGNORECASE,
)
#: 金额单位按「万元 → 元」的顺序匹配，避免只吃掉"元"而留下"万"
_AMOUNT_MASK = re.compile(r"\d+(?:\.\d+)?\s*(?:万元|元)")
_PERCENT_MASK = re.compile(r"\d+(?:\.\d+)?\s*%")
_PAGE_MASK = re.compile(r"P\s*\d+(?:\s*[-~至]\s*P?\s*\d+)?", re.IGNORECASE)
# 裸数字（含小数）—— 兜底，必须放最后
_NUMBER_MASK = re.compile(r"\d+(?:\.\d+)?")

_SKELETON_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (_DATE_MASK, "{date}"),
    (_CODE_MASK, "{code}"),
    (_PERCENT_MASK, "{percent}"),
    (_AMOUNT_MASK, "{money}"),
    (_PAGE_MASK, "{page}"),
    (_NUMBER_MASK, "{num}"),
)


def _mask_variable_fields(text: str) -> str:
    """按规则顺序把可变字段替换为语义占位符。"""
    masked = text
    for pattern, replacement in _SKELETON_RULES:
        masked = pattern.sub(replacement, masked)
    return masked


def build_skeleton_key(text: str) -> str:
    """把文本归一化为「骨架键」：可变字段替换为语义占位符后比对。

    这是查重真正有价值的一步 —— 段落照抄最常见的变形就是**改了数字**
    （深度 3m → 5m、工期 30 天 → 45 天、GB 50202 → GB 50204），
    按原文比对必然漏报，而骨架键能把它们判为同一模板。

    ⚠️ 处理顺序（关键）：**先 NFKC 折叠 → 再套字段规则 → 最后归一化**。
    字段规则依赖标点作为边界（百分数的 ``%``、日期的 ``年/月/日``、
    标准编号的 ``-`` 年号分隔），而 :func:`normalize_comparable` 会把
    标点全部剔除 —— 若先归一化再套规则，``95%`` 已变成 ``95``，
    百分数规则必然落空、只能被裸数字规则兜底。

    注意：只做**字面字段**归一，不做单位换算（见模块 docstring 的职责边界）。
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text))
    masked = _mask_variable_fields(folded)
    # 不用 normalize_comparable（它会剔除花括号）；
    # 同时这也让本函数**幂等** —— 传入已归一化的骨架键（内部两两比对走这条
    # 路径以省一次归一化）不会再把占位符吃掉。
    return _keep_only(masked, extra=_SKELETON_KEEP_EXTRA)


# ---------------------------------------------------------------------------
# 相似度度量
# ---------------------------------------------------------------------------
def char_bigrams(text: str) -> frozenset:
    """字符 n-gram 集合（默认 2-gram）。

    长度不足 n 的文本返回空集 —— 调用方据此跳过，而不是拿空集去算相似度
    （空集的 Dice / containment 都是 0，语义上是「不可比」而非「完全不相似」）。
    """
    n = BIGRAM_SIZE
    span_count = len(text) - n + 1
    if span_count <= 0:
        return frozenset()
    return frozenset(text[start : start + n] for start in range(span_count))


def dice(a: frozenset, b: frozenset) -> float:
    """Dice 系数 = 2|A∩B| / (|A|+|B|)。空集返回 0.0。

    相比 Jaccard（交集 / 并集），Dice 对**短文本**更敏感：
    两个短段落若有 8 个 2-gram、共享 5 个，Dice = 10/16 = 0.625 而
    Jaccard = 5/11 ≈ 0.45 —— 前者更贴近「有多少比例重合」的直觉。
    """
    total = len(a) + len(b)
    if not total:
        return 0.0
    shared = len(a & b)
    if not shared:
        return 0.0
    return 2.0 * shared / total


def containment(a: frozenset, b: frozenset) -> float:
    """Containment = |A∩B| / min(|A|,|B|)。空集返回 0.0。

    衡量「较短一方有多少比例被较长一方覆盖」—— 照抄场景里通常是
    「短段落被长章节包含」，因此 containment 比对称的 Dice 更贴合。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _tier_for(length: int) -> tuple[int, float, float]:
    """按（较短句子的）归一化长度选阈值分档。"""
    if length >= _TIER_LONG[0]:
        return _TIER_LONG
    return _TIER_SHORT


def sentence_similarity(a: str, b: str) -> dict:
    """计算两个句子的骨架相似度指标（只出数值，不做判定）。

    返回 ``{skeleton_a, skeleton_b, dice, containment, length_ratio,
    tier_cont, tier_dice}``。判定请用 :func:`sentences_near_match`。

    所有比较都基于**骨架键**（已归一化 + 数字/日期替换），
    故同一句子里「3m」与「5m」等价。
    """
    sk_a = build_skeleton_key(a)
    sk_b = build_skeleton_key(b)
    grams_a = char_bigrams(sk_a)
    grams_b = char_bigrams(sk_b)
    len_a, len_b = len(sk_a), len(sk_b)
    _, tier_cont, tier_dice = _tier_for(min(len_a, len_b))
    short_len, long_len = (len_a, len_b) if len_a <= len_b else (len_b, len_a)
    return {
        "skeleton_a": sk_a,
        "skeleton_b": sk_b,
        "dice": dice(grams_a, grams_b),
        "containment": containment(grams_a, grams_b),
        "length_ratio": (short_len / long_len) if long_len else 0.0,
        "tier_cont": tier_cont,
        "tier_dice": tier_dice,
    }


def _exact_match(sk_a: str, sk_b: str) -> dict:
    return {"reason": "exact", "skeleton_a": sk_a, "skeleton_b": sk_b}


def _skeleton_match(sk_a: str, sk_b: str) -> dict | None:
    """双阈值同时满足才算近似命中（只满足一个不足以排除误报）。"""
    metrics = sentence_similarity(sk_a, sk_b)
    if metrics["containment"] < metrics["tier_cont"] or metrics["dice"] < metrics["tier_dice"]:
        return None
    return {
        "reason": "skeleton",
        "dice": round(metrics["dice"], 4),
        "containment": round(metrics["containment"], 4),
        "length_ratio": round(metrics["length_ratio"], 4),
        # 带上分档阈值，便于调用方（预检报告 / 日志）说明
        # 「是按长句档还是短句档判定的」，而不是只给一个裸分值。
        "tier_cont": metrics["tier_cont"],
        "tier_dice": metrics["tier_dice"],
        "skeleton_a": sk_a,
        "skeleton_b": sk_b,
    }


def sentences_near_match(a: str, b: str) -> dict | None:
    """判定两个句子是否构成照抄。

    两级判定：

      1. **exact**：骨架键完全相同 → 直接判定，``reason="exact"``。
         零成本的索引级命中，也是精度最高的一级；
      2. **skeleton**：双阈值同时满足 → ``reason="skeleton"``。
         containment ≥ 分档阈值（较短者被覆盖的比例）**且**
         Dice ≥ 分档阈值（整体重合比例），两者都要求。

    不达标的返回 ``None``。

    ⚠️ 入参可以是**原始句子**，也可以是**已归一化的骨架键**（本模块内部的
    两两比对走后者以省一次归一化），两种都用同一个入口保证口径一致。
    """
    if not a or not b:
        return None
    sk_a = build_skeleton_key(a)
    sk_b = build_skeleton_key(b)
    if not sk_a or not sk_b:
        return None
    if min(len(sk_a), len(sk_b)) < MIN_SENTENCE_CHARS:
        return None
    if sk_a == sk_b:
        return _exact_match(sk_a, sk_b)
    return _skeleton_match(sk_a, sk_b)


def _near_match_from_grams(
    sk_a: str, sk_b: str, grams_a: frozenset, grams_b: frozenset, shared: int | None = None
) -> dict | None:
    """用**已算好的** bigram 集合做双阈值判定（与 :func:`sentences_near_match` 同口径）。

    ✅ R223（P1 · 预检 CPU 热点）：两两循环里对每个候选都调
    :func:`sentences_near_match`，而它会重跑 ``build_skeleton_key`` +
    ``char_bigrams`` —— 但入参本就是 ``_prepare_sections`` 用同一函数产出的
    **骨架串**，属纯重复劳动（实测是预检 6.4s 的主要成分）。``build_skeleton_key``
    对骨架串幂等（真实方案 2176 句 0 例不幂等，护栏
    ``test_r223_grams_reuse_equals_public_api`` 双向锁定），故直接复用预算集合，
    结论逐字不变。
    """
    if not sk_a or not sk_b:
        return None
    if min(len(sk_a), len(sk_b)) < MIN_SENTENCE_CHARS:
        return None
    if sk_a == sk_b:
        return _exact_match(sk_a, sk_b)
    n_a, n_b = len(grams_a), len(grams_b)
    total = n_a + n_b
    if not total:
        return None
    if shared is None:
        shared = len(grams_a & grams_b)
    if not shared:
        return None
    short_len, long_len = (len(sk_a), len(sk_b)) if len(sk_a) <= len(sk_b) else (len(sk_b), len(sk_a))
    _, tier_cont, tier_dice = _tier_for(short_len)
    dice_v = 2.0 * shared / total
    cont_v = shared / (n_a if n_a <= n_b else n_b)
    if cont_v < tier_cont or dice_v < tier_dice:
        return None
    return {
        "reason": "skeleton",
        "dice": round(dice_v, 4),
        "containment": round(cont_v, 4),
        "length_ratio": round(short_len / long_len, 4) if long_len else 0.0,
        "tier_cont": tier_cont,
        "tier_dice": tier_dice,
        "skeleton_a": sk_a,
        "skeleton_b": sk_b,
    }


# ---------------------------------------------------------------------------
# 句子切分
# ---------------------------------------------------------------------------
#: 句末分隔符（中英文 + 换行 + 全角分号）。Markdown 标记（``#`` / ``*`` /
#: `` ` ``）不在此列 —— 它们由 :func:`normalize_comparable` 剔除，无需在此拆分。
#:
#: ⚠️ **刻意不包含 ASCII 冒号 ``:``** —— 施工文本里 ``:`` 绝大多数是**比值**
#: （坡度 ``1:0.75``、配筋比 ``1:2``、时间 ``8:30``），把它当句末符会把
#: 「边坡坡度1:0.75放坡」切成两半，直接造出大量无意义短句。
#: 全角分号 ``；`` 是中文正常的分句符，保留。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；!?;\n\r]+")


def split_sentences(
    text: str, *, min_chars: int = MIN_SENTENCE_CHARS, max_sentences: int = MAX_SENTENCES_PER_SECTION
) -> list[str]:
    """按句末标点切分正文，返回**原句**（相似度计算内部再归一化）。

    过滤规则：
      - 空句丢弃；
      - 归一化后长度 < ``min_chars`` 丢弃（短句子相似度无统计意义）；
      - 归一化去重保序（同章节内重复句只比一次，避免自我重复刷屏）；
      - 单章节最多 ``max_sentences`` 句（超长章节取前 N 句，控 O(n²)）。

    注意**不按逗号/分号**切分：中文技术文本里的分句通常是同一句的并列成分
    （如「基坑深度 3m，边坡采用 1:0.75 放坡」），切开会造出大量无意义短句。
    """
    if not text:
        return []
    kept: list[str] = []
    seen_norms: set[str] = set()
    for fragment in _SENTENCE_SPLIT_RE.split(str(text)):
        sentence = str(fragment).strip()
        if not sentence:
            continue
        norm = normalize_comparable(sentence)
        if len(norm) < min_chars or norm in seen_norms:
            continue
        seen_norms.add(norm)
        kept.append(sentence)
        if len(kept) >= max_sentences:
            break
    return kept


# ---------------------------------------------------------------------------
# 跨章节搬运检测
# ---------------------------------------------------------------------------
def _iter_section_sentences(sections: list):
    """产出 ``(section_id, title, skeleton, bigrams)``，跳过脏数据与过短句。"""
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        sid = str(section.get("id") or "")
        title = str(section.get("title") or "").strip()
        content = str(section.get("content") or "")
        if not sid or not content.strip():
            continue
        for sentence in split_sentences(content):
            skeleton = build_skeleton_key(sentence)
            if len(skeleton) < MIN_SENTENCE_CHARS:
                continue
            yield sid, title, skeleton, char_bigrams(skeleton)


def _prepare_sections(sections: list):
    """预处理章节 → ``(骨架键索引, 扁平句子表)``。

    返回：
      - ``by_skeleton``：``{骨架键: [(section_id, title)]}``（跨章节索引）
      - ``flat``：``[(section_id, title, 骨架键, bigrams)]``，按骨架键长度升序
    """
    by_skeleton: dict[str, list[tuple[str, str]]] = {}
    flat: list[tuple[str, str, str, frozenset]] = []
    for sid, title, skeleton, grams in _iter_section_sentences(sections):
        by_skeleton.setdefault(skeleton, []).append((sid, title))
        flat.append((sid, title, skeleton, grams))
    # 按骨架键长度升序：便于用「长度差 2 倍」在双层循环里直接剪枝
    flat.sort(key=lambda entry: len(entry[2]))
    return by_skeleton, flat


def _sample_sentences(sk: str, flat: list, n: int = 3) -> list[str]:
    """取骨架键相等的代表句子（去重，最多 n 条）。

    展示的是**骨架键**而非原始句子 —— 骨架键已剔除标点空白、可读性足够，
    且省去为回查原句再建一遍索引。
    """
    samples: list[str] = []
    seen: set[str] = set()
    for _sid, _title, sentence_sk, _grams in flat:
        if sentence_sk != sk or sentence_sk in seen:
            continue
        seen.add(sentence_sk)
        samples.append(sentence_sk[:160])
        if len(samples) >= n:
            break
    return samples


#: 候选生成的最小共享 bigram 数。
#: 精判阈值是 Dice ≥ 0.82（长句档）→ 交集下界 = 0.82/2 × (|A|+|B|) ≥ 0.82×|A|，
#: 即两条**非短句**至少要共享自身 bigram 数的 82%。本阈值取 **2** 作为
#: 索引层预筛（只排除「交集必然为 0」的极端无关对），真正的判定仍在 Dice 精判。
#: 取 2 而非更小值是刻意的：既能把随机无关句（交集 0~1）挡在外面，又不会
#: 因阈值过大漏掉「改了几个字」的近重复（改 1 字只损失 2 个 bigram）。
_MIN_SHARED_BIGRAMS = 2

#: 倒排表的「高文档频率」上限：posting 长度超过此值的 gram 直接跳过。
#:
#: 施工文本高度模板化（「要求 / 工程 / 进行 / 验收」这类词几乎每句都有），
#: 其倒排表可达上万条；全量展开等于放弃剪枝、候选生成退化为朴素两两
#: （实测 80 章 × 600 句 = 4.8 万句时跑不完）。这是**标准 IR 高 DF 剪枝**：
#: 高频 gram 不具判别力，跳过后由更具体的低频 gram 决定候选。
#:
#: ⚠️ **不损失真重复**：被照抄的段落会整段重复，其中必然包含大量**低频**
#: bigram（如具体构件名 + 参数组合），这些 gram 的 posting 长度很小，
#: 足以把候选精确收窄。上界取 2000（远大于真实重复场景的倒排表长度，
#: 又远小于全库句子数），留足余量避免误剪。
_MAX_POSTING_LEN = 2000


def _build_bigram_inverted_index(flat: list) -> dict:
    """建 bigram → 句子下标 的倒排索引（候选生成用）。

    时间 O(总 bigram 数)，相比朴素两两的 O(n²) 是数量级改善。
    """
    inverted: dict = defaultdict(list)
    for idx, entry in enumerate(flat):
        for gram in entry[3]:
            inverted[gram].append(idx)
    return dict(inverted)


def _candidate_indices(a_bg: frozenset, inv: dict, self_idx: int, flat: list) -> list:
    """收集与 ``self_idx`` 共享 ≥ ``_MIN_SHARED_BIGRAMS`` 个 bigram 的**后向**候选下标。

    ✅ R223（P1 · 预检 CPU 热点）：只取 ``idx > self_idx``。``flat`` 已按骨架键
    长度升序，故候选满足 ``|B| ≥ |A|``；而判定对两个入参完全对称（Dice /
    containment / 分档均只依赖两者），有序对 (i, j) 与 (j, i) 结论相同，
    ``claimed`` 亦只认**无序章节对**。旧实现对同一对**双向各比一次**，等于把
    两两判定整轮做两遍（纯浪费）。只保留前向与旧结论逐字等价。

    ⚠️ **不得**在此按「分档阈值反导的交集下界」收紧：本函数的计数刻意跳过
    高文档频率 gram（``_MAX_POSTING_LEN``），故 ``计数 ≤ 真实交集``，用它在
    索引层收紧会漏判「共有 bigram 以高频词为主」的真实搬运（2026-11-12 实测
    真实方案漏 7 对）。下界必须在拿到**真实 bigram 集合**后施加，见
    ``_collect_near_groups``。

    返回**升序且去重**的下标列表（保持与朴素两两一致的遍历顺序 → 结论可复现）。
    """
    shared_counts: Counter = Counter()
    self_sid = flat[self_idx][0]
    for gram in a_bg:
        posting = inv.get(gram)
        if not posting or len(posting) > _MAX_POSTING_LEN:
            # ⚠️ 跳过「几乎每句都出现」的 gram（如「要求」「工程」「进行」）。
            #    这类高文档频率 gram 的倒排表可达上万条，全量展开会让候选生成
            #    退化成朴素两两。这是**标准 IR 剪枝**：高 DF 词本身不具判别力，
            #    跳过后由更具体的低频 gram 决定候选，故不损失任何「真重复」。
            continue
        for idx in posting:
            if idx <= self_idx or flat[idx][0] == self_sid:
                # idx <= self_idx：只保留前向对（见 docstring，结论对称、省一半）
                continue  # 同章节内不比（自我重复由 split 去重处理）
            shared_counts[idx] += 1
    return sorted(idx for idx, count in shared_counts.items() if count >= _MIN_SHARED_BIGRAMS)


def _to_exclude_set(exclude_pairs) -> set:
    """把章节 id 对集合归一为无序对集合（容忍列表/元组/反向序/脏数据）。"""
    excluded: set = set()
    for pair in exclude_pairs or []:
        try:
            if not pair or len(pair) != 2:
                continue
            first, second = str(pair[0]), str(pair[1])
            if first and second and first != second:
                excluded.add(frozenset((first, second)))
        except TypeError:
            continue
    return excluded


def _collect_exact_groups(by_skeleton: dict, flat: list, excluded: set, claimed: set) -> list[dict]:
    """第一级：exact 骨架匹配（索引分组，O(n)）。"""
    groups: list[dict] = []
    for skeleton, occurrences in by_skeleton.items():
        # 按章节去重（同章节多次出现只算一个章节）
        distinct: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for sid, title in occurrences:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            distinct.append((sid, title))
        if len(distinct) < 2:
            continue

        pairs: list[tuple[str, str, str, str]] = []
        for i in range(len(distinct)):
            for j in range(i + 1, len(distinct)):
                a_id, a_title = distinct[i]
                b_id, b_title = distinct[j]
                key = frozenset((a_id, b_id))
                if key in excluded or key in claimed:
                    continue
                pairs.append((a_id, b_id, a_title, b_title))
        if not pairs:
            continue

        claimed |= {frozenset((pair[0], pair[1])) for pair in pairs}
        groups.append(
            {
                "reason": "exact",
                "skeleton": skeleton[:120],
                "sentences": _sample_sentences(skeleton, flat),
                "section_pairs": pairs,
                "large": len(skeleton) >= COPY_GROUP_LARGE_CHARS,
                "dice": 1.0,
            }
        )
    return groups


def _collect_near_groups(flat: list, excluded: set, claimed: set) -> tuple[list[dict], bool, int]:
    """第二级：skeleton 近似匹配（倒排候选 + 双阈值 + 预算上限）。

    ⚠️ 性能（2026-10-01 压测修复）：朴素两两在 80 章 × 600 句（48000 句）下
    会退化到 11 亿次 bigram 集合运算，**单次预检跑几十秒**。改为**倒排索引
    候选生成**：只有「至少共享 N 个 bigram」的句子对才进入 Dice 精判。
    不共享任何 bigram 的两条句子 Dice 必为 0，跳过它**不改变任何结论**，
    故这是**精确剪枝**而非近似采样（护栏 test_pairwise_pruning_is_lossless）。

    ✅ R223（P1 · 预检 CPU 热点 · 生产实证 6.4s）追加两道**保结论不变**的提速：

      ① **复用预算 bigram + 交集下界**：候选拿到后先算**真实**交集
         ``shared = |A ∩ B|``（用 ``_prepare_sections`` 已算好的集合），按分档
         双阈值反导下界 ``max(⌈tier_cont·min(|A|,|B|)⌉, ⌈tier_dice·(|A|+|B|)/2⌉)``
         直接跳过不可能命中的候选；命中者再用 :func:`_near_match_from_grams`
         算 Dice/containment（不再重跑归一化）。下界用的是**真实交集**，与
         ``_MAX_POSTING_LEN`` 高 DF 剪枝正交，故不引入任何漏判。

         ⚠️ 该下界**不可**下沉到 ``_candidate_indices``（那里的计数跳过高频 gram，
         只是真实交集的下界，用它会漏判 —— 2026-11-12 实测漏 7 对）。
      ② 只比前向对（见 ``_candidate_indices``）。

    预算仍作为硬上限：触顶即停并置 truncated=True，调用方据此判断完整性。
    """
    groups: list[dict] = []
    compared = 0
    truncated = False
    inverted = _build_bigram_inverted_index(flat)
    for i in range(len(flat)):
        if compared >= MAX_PAIRWISE_COMPARISONS:
            truncated = True
            break
        a_id, a_title, a_skeleton, a_grams = flat[i]
        if not a_grams:
            continue
        a_len = len(a_skeleton)
        a_n = len(a_grams)
        _, tier_cont_a, tier_dice_a = _tier_for(a_len)
        for j in _candidate_indices(a_grams, inverted, i, flat):
            b_id, b_title, b_skeleton, b_grams = flat[j]
            # flat 已按长度升序：超过 2 倍即 Dice 有上界（|B| ≥ |A|），直接剪枝
            if len(b_skeleton) > a_len * 2:
                continue
            key = frozenset((a_id, b_id))
            if key in claimed or key in excluded:
                continue
            # ✅ R223①：真实交集 + 分档下界（j>i ⇒ |B| ≥ |A| ⇒ min 即本句）
            shared = len(a_grams & b_grams)
            b_n = len(b_grams)
            mn = a_n if a_n <= b_n else b_n
            need = max(math.ceil(tier_cont_a * mn), math.ceil(tier_dice_a * (a_n + b_n) / 2.0))
            if shared < need:
                continue
            compared += 1
            match = _near_match_from_grams(a_skeleton, b_skeleton, a_grams, b_grams, shared)
            if match is None:
                continue
            claimed.add(key)
            groups.append(
                {
                    "reason": match["reason"],
                    "skeleton": (a_skeleton or "")[:120],
                    "sentences": [a_skeleton[:160]],
                    "section_pairs": [(a_id, b_id, a_title, b_title)],
                    "large": max(a_len, len(b_skeleton)) >= COPY_GROUP_LARGE_CHARS,
                    "dice": float(match.get("dice") if match.get("dice") is not None else 1.0),
                }
            )
    return groups, truncated, compared


def find_cross_section_copies(sections: list, *, exclude_pairs=None) -> dict:
    """跨章节段落搬运检测，返回 ``{groups, truncated, pairwise_compared}``。

    Args:
        sections: ``[{id, title, content}, ...]``（与 ``PreflightContext.sections``
            同形；缺键一律容忍）。
        exclude_pairs: 已被其它规则（CON-05 整章级）判定的**章节 id 对集合**
            ``[("a", "b"), ...]``。传入后这些章节对的段落命中不再报告，
            避免同一现象被两条规则双报。

    算法（两级，先便宜后贵）：
      1. **exact 骨架匹配（索引级，O(n)）**：全方案句子按骨架键分组，
         同一骨架键出现在 ≥2 个章节即为一组搬运。零成本、零误报；
      2. **skeleton 近似匹配（两两，O(n²) 但受预算限制）**：只对
         「骨架键长度相差 ≤2 倍」的句子对做双阈值判定（长度差过大时 Dice
         有上界，直接剪枝），触顶 ``MAX_PAIRWISE_COMPARISONS`` 即停止。

    每组：``{reason, skeleton, sentences, section_pairs, large, dice}``
      - ``reason``：``exact``（骨架键完全相同）/ ``skeleton``（双阈值命中）；
      - ``section_pairs``：``[(a_id, b_id, a_title, b_title), ...]``；
      - ``large``：单句归一化字数是否 ≥ ``COPY_GROUP_LARGE_CHARS``
        （调用方据此升级严重度）。

    ⚠️ 第一级已声明的章节对会被 ``claimed`` 记录，第二级不再重复报告，
    保证「同一搬运现象只报一次」。
    """
    excluded = _to_exclude_set(exclude_pairs)
    by_skeleton, flat = _prepare_sections(sections)
    claimed: set = set()

    groups = _collect_exact_groups(by_skeleton, flat, excluded, claimed)
    near_groups, truncated, compared = _collect_near_groups(flat, excluded, claimed)
    groups.extend(near_groups)

    # 排序：大段优先 → Dice 降序 → 覆盖章节数多者优先
    groups.sort(key=lambda group: (not group["large"], -group["dice"], -len(group["section_pairs"])))
    overflow = len(groups) > MAX_COPY_GROUPS
    return {
        "groups": groups[:MAX_COPY_GROUPS],
        "truncated": truncated or overflow,
        "pairwise_compared": compared,
    }


# ---------------------------------------------------------------------------
# 标题近似雷同检测
# ---------------------------------------------------------------------------
def title_similarity(a: str, b: str) -> float:
    """两个标题的字符 2-gram Dice 相似度。

    用于「目录标题近似雷同」检测（区别于完全同名 —— 后者由
    ``outline_quality.check_outline_continuity`` 的 ``duplicate_titles``
    负责）。例：「基坑降水与支护施工」vs「基坑降水及支护施工」Dice 很高，
    属于应合并 / 明确区分的目录组织问题。
    """
    norm_a = normalize_comparable(a)
    norm_b = normalize_comparable(b)
    if len(norm_a) < MIN_TITLE_CHARS or len(norm_b) < MIN_TITLE_CHARS:
        return 0.0
    return round(dice(char_bigrams(norm_a), char_bigrams(norm_b)), 4)


def _unique_title_nodes(nodes: list) -> list[tuple[str, str, str]]:
    """过滤脏数据与过短标题，按归一化标题去重（保留首次出现）。"""
    clean: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        title = str(node.get("title") or "").strip()
        norm = normalize_comparable(title)
        if len(norm) < MIN_TITLE_CHARS or norm in seen:
            continue
        seen.add(norm)
        clean.append((str(node.get("path") or ""), title, norm))
    return clean


def find_similar_titles(nodes: list, *, threshold: float = TITLE_DICE_THRESHOLD, max_groups: int = 20) -> list[dict]:
    """检测**近似雷同**的标题对（2-gram Dice ≥ 阈值，且不完全同名）。

    Args:
        nodes: ``[{path, title}, ...]`` —— ``path`` 是位置路径（如 ``"1.3"``），
            ``title`` 是标题原文。**完全同名的对会被跳过**：那是结构缺陷，
            由 ``duplicate_titles`` 负责，此处再报只会双报。
        threshold: Dice 阈值，默认 :data:`TITLE_DICE_THRESHOLD`。
        max_groups: 最多返回组数（按相似度降序）。

    返回 ``[{a_path, a_title, b_path, b_title, similarity}, ...]``。
    """
    clean = _unique_title_nodes(nodes)
    result: list[dict] = []
    for i in range(len(clean)):
        for j in range(i + 1, len(clean)):
            similarity = title_similarity(clean[i][2], clean[j][2])
            if similarity < threshold:
                continue
            result.append(
                {
                    "a_path": clean[i][0],
                    "a_title": clean[i][1],
                    "b_path": clean[j][0],
                    "b_title": clean[j][1],
                    "similarity": similarity,
                }
            )
    result.sort(key=lambda item: -item["similarity"])
    return result[:max_groups]
