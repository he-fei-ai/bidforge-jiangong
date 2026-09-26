"""方案名称主线解析 —— 目录生成「三项依据」第 1 项（专项施工方案名称）的唯一构建器

背景（2026-09-26 目录生成深度审计）
------------------------------------
目录生成要求「按专项施工方案名称生成目录」。改造前方案名称在目录链路里只有
两个用途：① 整串原样塞进提示词交给模型自行领会；② ``scheme_scope`` 抽出
「主要施工内容」。结果：

- **方案类型**走 ``schemes.type`` 下拉值（``sse_handlers.is_dangerous``），
  名称里写「深基坑支护」而 type 选「其它」时，危大必备章节约束不会触发；
- **工序 / 工艺 / 施工对象**三个维度全库无任何确定性解析，全靠模型自觉；
- 24 个标准章节模板（``outline_templates``）与 6 大类危大分类
  （``scheme_classification``）能力齐备，但**目录生成一次都没调用**。

本模块把方案名称解析补齐并**收敛成单一对象**，供目录生成单点消费。

设计约束（对齐需求「不得编造」与 AGENTS.md §3.1）
------------------------------------------------
1. 纯函数、零 IO、零 AI：全部结论来自**方案名称字面**的关键词匹配；
2. **绝不推断名称之外的施工内容**：命中不到即返回空列表，绝不猜工序/工艺/对象；
3. **不重复维护既有知识**（AGENTS.md §4.8「禁止在另一处重复维护阈值表」的同源要求）：

   ==========================  ============================================
   解析维度                     唯一事实来源（既有，本模块只做组合）
   ==========================  ============================================
   主要施工内容                 ``services.scheme_scope.extract_construction_scope``
   方案类型 / 六大类危大及子类  ``services.scheme_classification.classify_scheme_name``
   标准章节模板                 ``services.outline_templates.match_template``
   编制规范与标准               ``services.standards_registry.get_standards_text``
   ==========================  ============================================

   本模块**仅新增**既有服务都没有的「工序 / 工艺 / 对象」三个字面维度。

4. 关键词库按**长度倒序**匹配，保证最长最具体者优先（"SMW工法桩" 先于 "桩"，
   "地下连续墙" 先于 "墙"），避免短词抢占导致误判。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("scheme_basis")


# ============================================================================
# 一、字面关键词库（工序 / 工艺 / 对象）
#
# 覆盖六大类危大工程（基坑、模板支撑、脚手架、起重吊装、临时用电、拆除）
# 及其常见子类，并外扩到装饰装修 / 防水 / 机电 / 砌体 / 冬雨季等非危大专项。
#
# ⚠️ 维护约定：条目必须来自真实工程语料，**不得**为了「覆盖某个方案名」而
# 臆造；新增条目须在 tests/test_outline_three_basis_20260926.py 补反例用例
# （非专项方案名不得误命中）。
# ============================================================================

#: 施工工序（施工动作/步骤，按工程语序排列）
PROCESS_KEYWORDS: tuple[str, ...] = (
    # 土方与基坑
    "开挖", "清底", "验槽", "修边", "放坡", "支护", "锚固", "张拉", "注浆", "旋喷",
    "搅拌", "压注", "静压", "换填", "回填", "夯实", "压实", "降水", "回灌", "截水",
    "抽降", "封底", "换撑",
    # 桩基与基础
    "成孔", "清孔", "沉渣", "灌注", "浇筑", "振捣", "养护", "拆模", "回弹",
    "承台", "垫层", "绑扎", "支模", "接桩", "截桩", "试桩", "检测",
    # 模板支撑与脚手架
    "搭设", "支设", "拆除", "卸载", "顶升", "附着", "下降", "上升", "拉结", "验收",
    # 起重吊装与机械
    "安装", "拆卸", "安拆", "吊装", "就位", "试运转",
    # 用电与临建
    "布线", "敷设", "架设", "接地", "穿线", "接引",
    # 防水 / 装饰 / 砌体 / 机电
    "清扫", "湿润", "涂刷", "滚涂", "喷涂", "铺贴", "热熔", "冷粘", "试水", "蓄水",
    "甩浆", "挂网", "抹灰", "吊顶", "打胶", "钻孔", "开槽", "剔凿", "封堵",
    "套管", "预埋", "试压", "冲洗", "通球", "调试", "联动",
    # 拆除与监测
    "静力破碎", "爆破", "支撑", "监测", "测点布置", "巡视", "复测",
)

#: 施工工艺 / 工法（具体工艺名，最长优先）
TECHNIQUE_KEYWORDS: tuple[str, ...] = (
    # 基坑支护
    "地下连续墙", "SMW工法桩", "钻孔灌注桩", "人工挖孔桩", "水泥搅拌桩", "旋喷桩",
    "静压桩", "预制桩", "碎石桩", "树根桩", "微型桩", "排桩", "桩间土",
    "止水帷幕", "高压旋喷", "压密注浆", "袖阀管注浆", "降水井", "回灌井",
    "土钉墙", "土钉", "锚索", "钢支撑", "混凝土支撑", "格构柱", "立柱桩",
    "逆作法", "盖挖法", "放坡开挖", "分级放坡", "分层分段开挖", "跳槽开挖",
    # 模板支撑与脚手架
    "承插型盘扣式", "盘扣式", "扣件式", "落地式脚手架", "悬挑式脚手架", "附着式升降",
    "卸料平台", "爬架", "混凝土泵架", "早拆体系", "大体积混凝土", "后浇带",
    "施工缝", "装配整体式",
    # 起重吊装
    "塔式起重机", "施工升降机", "人货电梯", "汽车吊", "人字桅杆", "群塔作业",
    "起重吊装",
    # 临时用电与临建
    "三级配电两级保护", "三级配电一级保护", "TN-S", "TN-C-S", "二级漏电保护",
    "一级配电", "二级配电", "三级配电", "临时用水", "临时用电", "消防管网",
    # 防水 / 装饰 / 砌体 / 机电
    "卷材防水", "涂膜防水", "刚性防水", "种植屋面", "屋面防水", "地下防水",
    "外墙外保温", "外墙内保温", "幕墙安装", "吊顶工程", "二次结构", "构造柱",
    "植筋", "砌体", "填充墙", "加气块砌筑", "机电安装", "管线综合", "电气安装",
    "给排水安装", "通风空调", "智能化", "节能改造", "绿色施工",
    # 拆除 / 监测 / 季节
    "静力破碎拆除", "机械拆除", "人工拆除", "爆破拆除", "控制爆破",
    "基坑监测", "变形监测", "沉降观测", "第三方监测", "冬期施工", "雨期施工",
    "高温施工", "冬雨季施工", "专项应急预案",
)

#: 施工对象（部位 / 构件 / 设施），用于三级目录标题必须具体到部位
OBJECT_KEYWORDS: tuple[str, ...] = (
    # 基坑与地基
    "基坑", "边坡", "沟槽", "土壁", "坑底", "坑外", "洞口", "井道",
    "桩基", "承台", "筏板", "基础", "地下室", "地下室外墙", "底板", "后浇带",
    "桩头", "冠梁", "腰梁", "围护墙", "桩身",
    # 主体与围护
    "主体结构", "楼面", "屋面", "外墙", "内墙", "女儿墙", "幕墙", "吊顶", "地面",
    "卫生间", "楼梯间", "砌体", "填充墙", "构造柱", "圈梁", "过梁",
    "变形缝", "施工缝", "伸缩缝",
    # 支撑与临建
    "模板", "支撑体系", "脚手架", "作业层", "卸料平台", "爬架", "安全网",
    "临边", "塔吊", "施工电梯", "人货梯", "吊篮", "配电箱", "配电柜",
    "电箱", "线路", "开关箱", "电缆", "电缆沟", "水管", "消火栓", "消防箱",
    "临建", "围挡", "大门", "道路", "排水沟", "沉淀池", "洗车槽",
    # 工艺部位
    "支撑", "防水层", "涂膜", "卷材", "保护层", "面层", "基层", "结合层",
    "找平层", "保温层",
)



# ============================================================================
# 二、字面匹配工具
# ============================================================================

#: 中文连续段（用于 2-gram 切分，与 sse_handlers._facts_keywords 同源口径）
_CJK_SEG_RE = re.compile(r"[一-鿿]+")
#: ASCII 单词（≥3 字母，如 CFG / MJS / HDPE / SMW）
_ASCII_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def _normalize_library(terms) -> tuple[str, ...]:
    """关键词库规范化：去空白、去重、按长度倒序（最长最具体者优先）。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in terms or ():
        t = str(t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    out.sort(key=len, reverse=True)
    return tuple(out)


#: 规范化后的三库（import 期一次性构建，运行期零排序开销）
PROCESS_TERMS = _normalize_library(PROCESS_KEYWORDS)
TECHNIQUE_TERMS = _normalize_library(TECHNIQUE_KEYWORDS)
OBJECT_TERMS = _normalize_library(OBJECT_KEYWORDS)


def match_terms(text: str, terms) -> list[str]:
    """按关键词库在文本中做**字面**匹配，返回去重保序的命中词。

    - 关键词库已按长度倒序，保证 "SMW工法桩" 先于 "桩" 命中；
    - 长词命中时抑制被其包含的短词（避免 "地下连续墙" 之后又记一条 "墙"），
      否则会向提示词注入冗余噪声并放大相关性打分的权重偏差。
    """
    if not text or not isinstance(text, str):
        return []
    hits: list[str] = []
    claimed: list[str] = []
    for t in terms or ():
        if t not in text:
            continue
        # 被更长的已命中词包含 → 跳过（"墙" ⊂ "地下连续墙"）
        if any(t != c and t in c for c in claimed):
            continue
        hits.append(t)
        claimed.append(t)
    return hits


def text_keywords(text: str) -> set[str]:
    """抽取相关性关键词集合（中文 2-gram + ASCII 单词≥3）。

    与 ``sse_handlers._facts_keywords`` 同源同口径（2-gram 是零依赖的中文
    切词近似）。放在本模块是为了让「方案名称相关性」与「事实/提取相关性」
    用**同一把尺子**，避免两处口径漂移。
    """
    kws: set[str] = set()
    if not text or not isinstance(text, str):
        return kws
    for seg in _CJK_SEG_RE.findall(text):
        for i in range(len(seg) - 1):
            kws.add(seg[i:i + 2])
    for w in _ASCII_WORD_RE.findall(text):
        kws.add(w.lower())
    return kws


def relevance_score(text: str, keywords: set[str]) -> int:
    """统计文本命中的相关性关键词个数（0 = 与方案名称无关）。"""
    if not text or not keywords or not isinstance(text, str):
        return 0
    return sum(1 for kw in keywords if kw in text)


@dataclass
class SchemeBasis:
    """专项施工方案名称的确定性解析结果（目录生成第 1 项依据）。

    每个字段都只来自方案名称字面；某字段为空表示「名称里没有该维度的字面
    信息」，调用方应据此回退到依据二 / 三，**不得编造**。
    """

    raw_name: str = ""
    #: 剥离通用后缀（安全/专项/施工/组织 + 方案/设计/预案/措施）后的名称主体
    core: str = ""
    #: 主要施工内容清单（scheme_scope，既有）
    scope_items: list[str] = field(default_factory=list)
    #: 方案类型（由危大分类 / 模板名派生；无法判定时为空串）
    scheme_type: str = ""
    #: 是否危大工程（名称字面命中六大类危大即 True）
    is_dangerous: bool = False
    #: 危大六大类及子类命中列表（scheme_classification.classify_scheme_name）
    hazard_hits: list[dict] = field(default_factory=list)
    #: 命中的标准章节模板 key（outline_templates.match_template）
    template_key: str = "general"
    #: 施工工序（字面）
    process_steps: list[str] = field(default_factory=list)
    #: 施工工艺 / 工法（字面）
    techniques: list[str] = field(default_factory=list)
    #: 施工对象 / 部位（字面）
    objects: list[str] = field(default_factory=list)

    # -- 相关性 ---------------------------------------------------------
    def keywords(self) -> set[str]:
        """本方案名称派生的相关性关键词集合（供依据二/三打分）。

        = 施工内容 + 工序 + 工艺 + 对象 的 2-gram / ASCII 并集。
        无任何命中时返回空集 —— 调用方**不得**据此删除数据（宁可不加权也不丢）。
        """
        parts = list(self.scope_items) + list(self.process_steps) + \
            list(self.techniques) + list(self.objects)
        return text_keywords("".join(parts))

    def relevance(self, text: str) -> int:
        """文本与本方案名称的相关性得分（0 = 无关）。"""
        return relevance_score(text, self.keywords())

    # -- 渲染 -----------------------------------------------------------
    def prompt_text(self) -> str:
        """渲染供提示词注入的「方案名称解析」文本（多项，空维度不输出）。

        无任何可解析维度时返回空串 —— 调用方据此**不注入该区块**，
        保持「信息不足时结合依据二、三补全，不得编造」的红线。
        """
        lines: list[str] = []
        if self.scheme_type:
            lines.append(f"方案类型：{self.scheme_type}")
        if self.scope_items:
            lines.append(f"主要施工内容：{'、'.join(self.scope_items)}")
        if self.process_steps:
            lines.append(f"施工工序：{'、'.join(self.process_steps)}")
        if self.techniques:
            lines.append(f"施工工艺：{'、'.join(self.techniques)}")
        if self.objects:
            lines.append(f"施工对象：{'、'.join(self.objects)}")
        if self.hazard_hits:
            names = "、".join(
                f"{h.get('category_name', '')}·{h.get('sub_name', '')}".strip("·")
                for h in self.hazard_hits[:4]
            )
            if names:
                lines.append(f"危大分类：{names}（危大工程，章节须覆盖九大必要章节）")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        """台账视图（供差集审计 / 调试端点）。"""
        return {
            "raw_name": self.raw_name,
            "core": self.core,
            "scheme_type": self.scheme_type,
            "is_dangerous": self.is_dangerous,
            "scope_items": list(self.scope_items),
            "process_steps": list(self.process_steps),
            "techniques": list(self.techniques),
            "objects": list(self.objects),
            "hazard_hits": list(self.hazard_hits),
            "template_key": self.template_key,
        }


def parse_scheme_basis(scheme_name: str) -> SchemeBasis:
    """解析专项施工方案名称 → :class:`SchemeBasis`（纯函数，永不抛异常）。

    组合四个既有服务 + 本模块新增的三个字面维度：

    1. 施工内容 ← ``scheme_scope.extract_construction_scope``（既有）
    2. 危大六大类及子类 ← ``scheme_classification.classify_scheme_name``（既有）
    3. 标准章节模板 ← ``outline_templates.match_template``（既有）
    4. 编制规范 ← 由调用方按方案名/类型取（既有，见 standards_registry）
    5. 工序 / 工艺 / 对象 ← 本模块字面关键词库（新增）

    任一子依赖不可用时**降级为空**（不抛异常、不阻断目录生成主流程）。
    """
    name = scheme_name if isinstance(scheme_name, str) else ""
    basis = SchemeBasis(raw_name=name.strip())
    if not basis.raw_name:
        return basis

    # ---- 1. 主要施工内容（既有 service） ----
    try:
        from app.services.scheme_scope import extract_construction_scope
        basis.scope_items = extract_construction_scope(name)
    except Exception:  # pragma: no cover - 既有 service 为纯函数，异常即代码缺陷
        logger.warning("解析方案名称主要施工内容失败（降级为空）", exc_info=True)

    # ---- 2. 危大六大类及子类（既有 service） ----
    try:
        from app.services.scheme_classification import classify_scheme_name
        basis.hazard_hits = classify_scheme_name(name)
        basis.is_dangerous = bool(basis.hazard_hits)
    except Exception:
        logger.warning("解析方案名称危大分类失败（降级为非危大）", exc_info=True)

    # ---- 3. 标准章节模板（既有 service） ----
    try:
        from app.services.outline_templates import match_template
        basis.template_key = match_template(name) or "general"
    except Exception:
        logger.warning("匹配标准章节模板失败（降级为 general）", exc_info=True)

    # ---- 4. 方案类型：危大分类名优先，其次模板名 ----
    if basis.hazard_hits:
        cat = basis.hazard_hits[0].get("category_name") or ""
        sub = basis.hazard_hits[0].get("sub_name") or ""
        basis.scheme_type = f"{cat}·{sub}" if sub else cat
    if not basis.scheme_type:
        try:
            from app.services.outline_templates import _TEMPLATE_NAMES
            basis.scheme_type = _TEMPLATE_NAMES.get(basis.template_key, "")
        except Exception:
            basis.scheme_type = ""

    # ---- 5. 工序 / 工艺 / 对象（本模块字面维度） ----
    # 匹配范围用「名称主体 + 施工内容」：复合名拆出的施工内容本身就是名称的
    # 一部分，两者合起来覆盖全部字面信息；不引入名称之外的任何文本（不编造）。
    hay = name + "".join(basis.scope_items)
    basis.process_steps = match_terms(hay, PROCESS_TERMS)
    basis.techniques = match_terms(hay, TECHNIQUE_TERMS)
    basis.objects = match_terms(hay, OBJECT_TERMS)

    # ---- 6. 名称主体（剥后缀，失败不影响其余字段） ----
    try:
        from app.services.scheme_scope import _strip_suffix
        basis.core = _strip_suffix(name)
    except Exception:
        basis.core = name.strip()
    return basis


# ============================================================================
# 四、依据二 / 依据三的相关性工具
# ============================================================================


def rank_by_relevance(rows: list, basis: SchemeBasis | None,
                      text_fn, *, stable: bool = True) -> tuple[list, int]:
    """把 ``rows`` 按与方案名称的相关性**前置**排序（不删除任何一行）。

    供「头部优先截断」的下游使用（``_render_facts_text`` 遇到预算即 ``break``）：
    相关项前置即可让相关事实在固定预算下**全部可见** —— 这正是需求
    「与专项方案相关的事实进入目录」的关键：**顺序即见性**。

    Args:
        rows: 待排序列表。
        basis: 方案名称解析结果；None / 无关键词 / 无相关行 → 原样返回。
        text_fn: ``row -> str``，抽取用于打分的文本。
        stable: 同分项是否保持原相对顺序（True = 稳定排序，不扰动既有顺序）。

    Returns:
        ``(排序后的列表, 命中相关的条数)``。命中数为 0 时原样返回，调用方据此
        判断「本次无相关性信号，勿做加权」。
    """
    rows = list(rows or [])
    if basis is None or not rows:
        return rows, 0
    kws = basis.keywords()
    if not kws:
        return rows, 0
    scored = []
    hit = 0
    for idx, r in enumerate(rows):
        try:
            s = relevance_score(str(text_fn(r) or ""), kws)
        except Exception:
            s = 0
        if s > 0:
            hit += 1
        scored.append((-s, idx, r))
    if hit == 0:
        return rows, 0
    if stable:
        scored.sort(key=lambda x: (x[0], x[1]))
    else:
        scored.sort(key=lambda x: x[0])
    return [r for _, _, r in scored], hit


def relevance_weights(rows: list, basis: SchemeBasis | None,
                      text_fn, *, relevant_boost: float = 2.0) -> list[float]:
    """为「按 `## ` 小节配额截断」生成**相关性权重**（依据二专用）。

    ``sse_handlers._allocate_char_budgets`` 按「各节原始长度占比」分配字符预算。
    改造前所有小节权重相同 → 与方案名称强相关的「施工工艺/安全措施」提取项
    与无关项（商务条款/企业资质）争抢同一份额，被截断概率相同。权重把相关项的
    **份额**放大 ``relevant_boost`` 倍（默认 2.0），让相关项优先拿到预算。

    **零信息损失**：权重只影响份额分配，不删除任何小节；每节仍有
    ``_allocate_char_budgets`` 的保底份额（min_abs / min_share），无关项
    仍然可见 —— 满足需求「完整调用不丢失不截断」。

    Args:
        rows: 待打分行（顺序必须与调用方后续消费的顺序一致）。
        basis: 方案名称解析结果；None 或无关键词 → 全 1.0（等价于未加权）。
        text_fn: ``row -> str``，抽取用于打分的文本。
        relevant_boost: 相关项的份额放大倍数；<=1.0 等价于关闭加权。

    Returns:
        与 ``rows`` **等长对齐**的权重列表（未命中为 1.0）。
        返回 list 而非 dict 是因为调用方的行常是 dict（不可哈希）。
    """
    n = len(rows or [])
    if basis is None or n == 0 or relevant_boost <= 1.0:
        return [1.0] * n
    kws = basis.keywords()
    if not kws:
        return [1.0] * n
    out: list[float] = []
    for r in rows:
        try:
            s = relevance_score(str(text_fn(r) or ""), kws)
        except Exception:
            s = 0
        out.append(float(relevant_boost) if s > 0 else 1.0)
    return out

