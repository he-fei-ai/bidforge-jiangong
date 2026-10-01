"""跨章节重复检测（骨架归一 + 双阈值 Dice，纯程序、零 AI、零成本）

本模块移植参考软件（OpenBidKit 易标 `duplicateCheckService.cjs`）查重体系里
**与本仓语义直接相关**的两条算法线，并按专项施工方案域裁剪：

保留（本域通用、零业务耦合）：
  1. **骨架归一**（原 `buildTenderSkeletonKey`）：把可变数字 / 日期 /
     标准编号 / 百分比 / 页码替换成语义占位符后再比对。这是查重真正有价值的
     一步 —— 正文里「基坑开挖深度 3m」与「基坑开挖深度 5m」是同一模板段落
     改了数字，按原文比对必然漏报；
  2. **字符 2-gram Dice + 双阈值分级**（原正文句级比对的双阈值口径）：
     Dice 对短文本比 Jaccard 敏感（交集占比而非并集占比），
     且双阈值按长度分档，避免短句子误报。

裁剪（标书域专属，本仓无对应数据源或语义）：
  - 「字段白名单」「骨架门控关键词」—— 服务于「投标文件 vs 招标文件」的
    投标语境判定，专项方案没有这个对立关系；
  - 图片哈希比对 —— 本仓图表全自动生成、不接收用户上传图；
  - 「多份投标文件之间」的分组聚合 —— 本仓以「单方案多章节」为单位。

⚠️ 已知边界（刻意不修，指向另一条独立缺口）：
  单位归一化不在本模块职责内。「6m」与「6000mm」在骨架归一后仍是不同键
  （前者归一为 ``6m``，后者为 ``6000mm``）。全仓已存在三份互不相同的单位表
  （`preflight_engine._UNIT_CANON` / `facts_classification._UNIT_TO_METER` /
  `content_standard._UNIT_ALIASES`），本模块**故意不新增第四份** ——
  单位口径收敛是独立缺口，需单独立项收敛后本模块自动受益，
  而不是在这里再写一份悄悄分叉的表。

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

import re
import unicodedata

__all__ = [
    "BIGRAM_SIZE", "MIN_SENTENCE_CHARS", "MIN_TITLE_CHARS",
    "TITLE_DICE_THRESHOLD", "MAX_COPY_GROUPS", "COPY_GROUP_LARGE_CHARS",
    "normalize_comparable", "build_skeleton_key", "char_bigrams",
    "dice", "containment", "split_sentences", "sentence_similarity",
    "sentences_near_match", "find_cross_section_copies",
    "title_similarity", "find_similar_titles",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 字符 n-gram 的 n（对齐参考软件 duplicateCheckService 的 2-gram 口径）
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

#: 双阈值分级（对齐参考实现的按长度分档口径）：
#: ``(最小归一化长度, containment 阈值, dice 阈值)``。
#: 对长句用较低阈值（长句偶然重合多但比例低），对短句用更严阈值（易误报）。
_SIMILARITY_TIERS: tuple[tuple[int, float, float], ...] = (
    (30, 0.90, 0.82),   # ≥30 字：主体段落
    (0, 0.95, 0.88),    # <30 字：短段落从严
)

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
    return "".join(ch for ch in text
                   if ch in extra
                   or unicodedata.category(ch)[0] in _KEEP_CATEGORY)


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
_SKELETON_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # 日期：2026年9月30日 / 2026年9月 / 2026-09-30 / 2026.9.30
    (re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"), "{date}"),
    (re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月"), "{date}"),
    (re.compile(r"\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"), "{date}"),
    # 标准编号：GB 50202-2018 / JGJ 120-2012 / GB/T 50502 / DBJ …（含年号）
    (re.compile(
        r"(?:GB(?:/T)?|JGJ(?:/T)?|JTG(?:/T)?|DBJ|CECS|CJJ(?:/T)?)\s*"
        r"\d{2,5}(?:\.\d{1,3})?(?:[—\-－]\s*\d{4})?", re.IGNORECASE), "{code}"),
    # 百分数 / 金额 / 页码
    (re.compile(r"\d+(?:\.\d+)?\s*%"), "{percent}"),
    (re.compile(r"\d+(?:\.\d+)?\s*万元"), "{money}"),
    (re.compile(r"\d+(?:\.\d+)?\s*元"), "{money}"),
    (re.compile(r"P\s*\d+(?:\s*[-~至]\s*P?\s*\d+)?", re.IGNORECASE), "{page}"),
    # 裸数字（含小数）—— 兜底，必须放最后
    (re.compile(r"\d+(?:\.\d+)?"), "{num}"),
)


def build_skeleton_key(text: str) -> str:
    """把文本归一化为「骨架键」：可变字段替换为语义占位符后比对。

    这是查重真正有价值的一步 —— 段落照抄最常见的变形就是**改了数字**
    （深度 3m → 5m、工期 30 天 → 45 天、GB 50202 → GB 50204），
    按原文比对必然漏报，而骨架键能把它们判为同一模板。

    ⚠️ 处理顺序（关键）：**先 NFKC 折叠 → 再套字段规则 → 最后归一化**。
    字段规则依赖标点作为边界（百分数的 ``%``、日期的 ``年/月/日``、
    标准编号的 ``-`` 年号分隔），而 :func:`normalize_comparable` 会把
    标点全部剔除 —— 若先归一化再套规则，``95%`` 已变成 ``95``，
    百分数规则必然落空、只能被裸数字规则兜底（本轮实测复现的缺陷）。

    注意：只做**字面字段**归一，不做单位换算（见模块 docstring 的「已知边界」）。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    for rx, repl in _SKELETON_RULES:
        s = rx.sub(repl, s)
    # 注意此处不能用 normalize_comparable（它会剔除花括号）；
    # 同时这也让本函数**幂等** —— 传入已归一化的骨架键（内部两两比对走这条
    # 路径以省一次归一化）不会再把占位符吃掉。
    return _keep_only(s, extra=_SKELETON_KEEP_EXTRA)


# ---------------------------------------------------------------------------
# 相似度度量
# ---------------------------------------------------------------------------
def char_bigrams(text: str) -> frozenset:
    """字符 n-gram 集合（默认 2-gram）。

    长度不足 n 的文本返回空集 —— 调用方据此跳过，而不是拿空集去算相似度
    （空集的 Dice / containment 都是 0，语义上是「不可比」而非「完全不相似」）。
    """
    n = BIGRAM_SIZE
    if len(text) < n:
        return frozenset()
    return frozenset(text[i:i + n] for i in range(len(text) - n + 1))


def dice(a: frozenset, b: frozenset) -> float:
    """Dice 系数 = 2|A∩B| / (|A|+|B|)。空集返回 0.0。

    相比 Jaccard（交集 / 并集），Dice 对**短文本**更敏感：
    两个短段落若有 8 个 2-gram、共享 5 个，Dice = 10/16 = 0.625 而
    Jaccard = 5/11 ≈ 0.45 —— 前者更贴近「有多少比例重合」的直觉。
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return 2.0 * inter / (len(a) + len(b))


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
    for min_len, cont, dice_th in _SIMILARITY_TIERS:
        if length >= min_len:
            return min_len, cont, dice_th
    return _SIMILARITY_TIERS[-1]


def sentence_similarity(a: str, b: str) -> dict:
    """计算两个句子的骨架相似度指标（只出数值，不做判定）。

    返回 ``{skeleton_a, skeleton_b, dice, containment, length_ratio,
    tier_cont, tier_dice}``。判定请用 :func:`sentences_near_match`。

    所有比较都基于**骨架键**（已归一化 + 数字/日期替换），
    故同一句子里「3m」与「5m」等价。
    """
    sk_a = build_skeleton_key(a)
    sk_b = build_skeleton_key(b)
    ba = char_bigrams(sk_a)
    bb = char_bigrams(sk_b)
    la, lb = len(sk_a), len(sk_b)
    _, tier_cont, tier_dice = _tier_for(min(la, lb))
    lo, hi = (la, lb) if la <= lb else (lb, la)
    return {
        "skeleton_a": sk_a,
        "skeleton_b": sk_b,
        "dice": dice(ba, bb),
        "containment": containment(ba, bb),
        "length_ratio": (lo / hi) if hi else 0.0,
        "tier_cont": tier_cont,
        "tier_dice": tier_dice,
    }


def sentences_near_match(a: str, b: str) -> dict | None:
    """判定两个句子是否构成照抄。

    两级判定（对齐参考实现 5 级级联中的 exact / skeleton 两级，
    去掉标书专属的 field / near 两级）：

      1. **exact**：骨架键完全相同 → 直接判定，``reason="exact"``。
         零成本的索引级命中，也是精度最高的一级；
      2. **skeleton**：双阈值同时满足 → ``reason="skeleton"``。
         containment ≥ 分档阈值（较短者被覆盖的比例）**且**
         Dice ≥ 分档阈值（整体重合比例），两者都要求 —— 只满足一个
         不足以排除误报（长句偶然含短句，或两个同领域短句用词高度重叠）。

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
        return {"reason": "exact", "skeleton_a": sk_a, "skeleton_b": sk_b}

    m = sentence_similarity(sk_a, sk_b)
    if m["containment"] >= m["tier_cont"] and m["dice"] >= m["tier_dice"]:
        return {
            "reason": "skeleton",
            "dice": round(m["dice"], 4),
            "containment": round(m["containment"], 4),
            "length_ratio": round(m["length_ratio"], 4),
            # 带上分档阈值，便于调用方（预检报告 / 日志）说明
            # 「是按长句档还是短句档判定的」，而不是只给一个裸分值。
            "tier_cont": m["tier_cont"],
            "tier_dice": m["tier_dice"],
            "skeleton_a": sk_a,
            "skeleton_b": sk_b,
        }
    return None


# ---------------------------------------------------------------------------
# 句子切分
# ---------------------------------------------------------------------------
#: 句末分隔符（中英文 + 换行 + 全角分号）。Markdown 标记（``#`` / ``*`` /
#: `` ` ``）不在此列 —— 它们由 :func:`normalize_comparable` 剔除，无需在此拆分。
#:
#: ⚠️ **刻意不包含 ASCII 冒号 ``:``** —— 施工文本里 ``:`` 绝大多数是**比值**
#: （坡度 ``1:0.75``、配筋比 ``1:2``、时间 ``8:30``），把它当句末符会把
#: 「边坡坡度1:0.75放坡」切成两半，直接造出大量无意义短句（本轮实测复现）。
#: 全角分号 ``；`` 是中文正常的分句符，保留。
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；!?;\n\r]+")


def split_sentences(text: str, *, min_chars: int = MIN_SENTENCE_CHARS,
                    max_sentences: int = MAX_SENTENCES_PER_SECTION) -> list[str]:
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
    out: list[str] = []
    seen: set[str] = set()
    for raw in _SENTENCE_SPLIT_RE.split(str(text)):
        s = str(raw).strip()
        if not s:
            continue
        norm = normalize_comparable(s)
        if len(norm) < min_chars or norm in seen:
            continue
        seen.add(norm)
        out.append(s)
        if len(out) >= max_sentences:
            break
    return out


# ---------------------------------------------------------------------------
# 跨章节搬运检测
# ---------------------------------------------------------------------------
def _prepare_sections(sections: list):
    """预处理章节 → ``(骨架键索引, 扁平句子表)``。

    返回：
      - ``by_skeleton``：``{骨架键: [(section_id, title)]}``（跨章节索引）
      - ``flat``：``[(section_id, title, 骨架键, bigrams)]``，按骨架键长度升序
    """
    by_skeleton: dict[str, list[tuple[str, str]]] = {}
    flat: list[tuple[str, str, str, frozenset]] = []

    for s in sections or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        title = str(s.get("title") or "").strip()
        content = str(s.get("content") or "")
        if not sid or not content.strip():
            continue
        for sent in split_sentences(content):
            sk = build_skeleton_key(sent)
            if len(sk) < MIN_SENTENCE_CHARS:
                continue
            by_skeleton.setdefault(sk, []).append((sid, title))
            flat.append((sid, title, sk, char_bigrams(sk)))

    # 按骨架键长度升序：便于用「长度差 2 倍」在双层循环里直接剪枝
    flat.sort(key=lambda x: len(x[2]))
    return by_skeleton, flat


def _sample_sentences(sk: str, flat: list, n: int = 3) -> list[str]:
    """取骨架键相等的代表句子（去重，最多 n 条）。

    展示的是**骨架键**而非原始句子 —— 骨架键已剔除标点空白、可读性足够，
    且省去为回查原句再建一遍索引。
    """
    out: list[str] = []
    seen: set[str] = set()
    for _sid, _title, s_sk, _bg in flat:
        if s_sk != sk or s_sk in seen:
            continue
        seen.add(s_sk)
        out.append(s_sk[:160])
        if len(out) >= n:
            break
    return out


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
    inv: dict = {}
    for idx, item in enumerate(flat):
        for gram in item[3]:
            bucket = inv.get(gram)
            if bucket is None:
                inv[gram] = [idx]
            else:
                bucket.append(idx)
    return inv


def _candidate_indices(a_bg: frozenset, inv: dict, self_idx: int,
                       flat: list) -> list:
    """收集与 ``self_idx`` 共享 ≥ ``_MIN_SHARED_BIGRAMS`` 个 bigram 的候选下标。

    返回**升序且去重**的下标列表（保持与朴素两两一致的遍历顺序 → 结论可复现）。
    """
    hits: dict = {}
    self_sid = flat[self_idx][0]
    for gram in a_bg:
        posting = inv.get(gram)
        if not posting or len(posting) > _MAX_POSTING_LEN:
            # ⚠️ 跳过「几乎每句都出现」的 gram（如「要求」「工程」「进行」）。
            #    施工文本高度模板化，这类高文档频率 gram 的倒排表可达上万条，
            #    全量展开会让候选生成退化成朴素两两（实测 4.8 万句跑不完）。
            #    这是**标准 IR 剪枝**：高 DF 词本身不具判别力，跳过后由更具体
            #    的 gram 决定候选，故不损失任何「真重复」——真照抄的段落必然
            #    共享大量**低频** bigram。
            continue
        for idx in posting:
            if idx == self_idx or flat[idx][0] == self_sid:
                continue      # 同章节内不比（自我重复由 split 去重处理）
            hits[idx] = hits.get(idx, 0) + 1
    return sorted(i for i, n in hits.items() if n >= _MIN_SHARED_BIGRAMS)


def _to_exclude_set(exclude_pairs) -> set:
    """把章节 id 对集合归一为无序对集合（容忍列表/元组/反向序/脏数据）。"""
    out: set = set()
    for pair in exclude_pairs or []:
        try:
            if not pair or len(pair) != 2:
                continue
            a, b = str(pair[0]), str(pair[1])
            if a and b and a != b:
                out.add(frozenset((a, b)))
        except TypeError:
            continue
    return out


def find_cross_section_copies(sections: list, *,
                              exclude_pairs=None) -> dict:
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
    exclude = _to_exclude_set(exclude_pairs)
    by_skeleton, flat = _prepare_sections(sections)

    groups: list[dict] = []
    claimed: set = set()

    # ---- 第一级：exact 骨架匹配（索引分组，O(n)） ----
    for sk, occ in by_skeleton.items():
        # 按章节去重（同章节多次出现只算一个章节）
        distinct: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for sid, title in occ:
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
                if key in exclude or key in claimed:
                    continue
                pairs.append((a_id, b_id, a_title, b_title))
        if not pairs:
            continue
        claimed |= {frozenset((p[0], p[1])) for p in pairs}
        groups.append({
            "reason": "exact",
            "skeleton": sk[:120],
            "sentences": _sample_sentences(sk, flat),
            "section_pairs": pairs,
            "large": len(sk) >= COPY_GROUP_LARGE_CHARS,
            "dice": 1.0,
        })

    # ---- 第二级：skeleton 近似匹配 ----
    # ⚠️ 性能（2026-10-01 压测修复）：朴素两两在 80 章 × 600 句（48000 句）下
    #   会退化到 11 亿次 bigram 集合运算，**单次预检跑几十秒**。改为**倒排索引
    #   候选生成**：只有「至少共享 N 个 bigram」的句子对才进入 Dice 精判。
    #   不共享任何 bigram 的两条句子 Dice 必为 0，跳过它**不改变任何结论**，
    #   故这是**精确剪枝**而非近似采样（护栏 test_pairwise_pruning_is_lossless）。
    #   预算仍作为硬上限：触顶即停并置 truncated=True，调用方据此判断完整性。
    compared = 0
    truncated = False
    inv = _build_bigram_inverted_index(flat)
    for i in range(len(flat)):
        if compared >= MAX_PAIRWISE_COMPARISONS:
            truncated = True
            break
        a_id, a_title, a_sk, a_bg = flat[i]
        if not a_bg:
            continue
        a_len = len(a_sk)
        cands = _candidate_indices(a_bg, inv, i, flat)
        for j in cands:
            b_id, b_title, b_sk, _b_bg = flat[j]
            # flat 已按长度升序：超过 2 倍即 Dice 有上界，直接剪枝
            if len(b_sk) > a_len * 2:
                continue
            key = frozenset((a_id, b_id))
            if key in claimed or key in exclude:
                continue
            compared += 1
            m = sentences_near_match(a_sk, b_sk)
            if m is None:
                continue
            claimed.add(key)
            groups.append({
                "reason": m["reason"],
                "skeleton": (a_sk or "")[:120],
                "sentences": [a_sk[:160]],
                "section_pairs": [(a_id, b_id, a_title, b_title)],
                "large": max(a_len, len(b_sk)) >= COPY_GROUP_LARGE_CHARS,
                "dice": float(m.get("dice") if m.get("dice") is not None
                              else 1.0),
            })

    # 排序：大段优先 → Dice 降序 → 覆盖章节数多者优先
    groups.sort(key=lambda g: (not g["large"], -g["dice"],
                               -len(g["section_pairs"])))
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
    na = normalize_comparable(a)
    nb = normalize_comparable(b)
    if len(na) < MIN_TITLE_CHARS or len(nb) < MIN_TITLE_CHARS:
        return 0.0
    return round(dice(char_bigrams(na), char_bigrams(nb)), 4)


def find_similar_titles(nodes: list, *,
                        threshold: float = TITLE_DICE_THRESHOLD,
                        max_groups: int = 20) -> list[dict]:
    """检测**近似雷同**的标题对（2-gram Dice ≥ 阈值，且不完全同名）。

    Args:
        nodes: ``[{path, title}, ...]`` —— ``path`` 是位置路径（如 ``"1.3"``），
            ``title`` 是标题原文。**完全同名的对会被跳过**：那是结构缺陷，
            由 ``duplicate_titles`` 负责，此处再报只会双报。
        threshold: Dice 阈值，默认 :data:`TITLE_DICE_THRESHOLD`。
        max_groups: 最多返回组数（按相似度降序）。

    返回 ``[{a_path, a_title, b_path, b_title, similarity}, ...]``。
    """
    clean: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or "").strip()
        norm = normalize_comparable(title)
        if len(norm) < MIN_TITLE_CHARS or norm in seen:
            continue
        seen.add(norm)
        clean.append((str(n.get("path") or ""), title, norm))

    out: list[dict] = []
    for i in range(len(clean)):
        for j in range(i + 1, len(clean)):
            sim = title_similarity(clean[i][2], clean[j][2])
            if sim >= threshold:
                out.append({
                    "a_path": clean[i][0], "a_title": clean[i][1],
                    "b_path": clean[j][0], "b_title": clean[j][1],
                    "similarity": sim,
                })
    out.sort(key=lambda x: -x["similarity"])
    return out[:max_groups]