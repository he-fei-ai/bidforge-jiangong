"""模糊生成规则 —— 正文「不留空、不占位、不编造」的唯一事实源（2026-10-01）。

背景
----
正文生成此前对「资料未给出」的数据只有两条出路，都已被判定为缺陷：

① 精准模式：按「数据真实性红线」输出 ``【待补充：参数名】`` 占位符 →
   交付文档留下人工补录的隐雷，导出成稿带着占位标记。
② 模糊模式：允许「按设计确定 / 按合同要求」等空泛指代 →
   正文空洞、句式模板化，读起来像 AI 凑的。

现统一改为**模糊生成**：正文完整生成、不留占位标记、不写空话；
无明确数据时按八类规则做**合理概括 / 归纳 / 泛化**，严禁编造关键数据。

本模块是四条链路的**唯一事实源**，禁止在别处复制该表或这些正则：

1. 提示词注入（system / user / 续写提醒）—— ``build_fuzzy_rules_block`` /
   ``build_no_placeholder_block``，被 ``services/content_standard.py`` 的文案构造复用；
2. 生成后确定性校验 —— ``services/content_standard.py::standard_report`` 调用
   ``scan_placeholder_marks`` / ``scan_vague_statements`` /
   ``scan_missing_reveal`` / ``scan_fabricated_dates``；
3. 可追溯性内部标记 —— ``services/content_trace.py`` 复用 ``FUZZY_CATEGORIES``
   的 category / data_source / reason 口径，以及 ``detect_fuzzy_expressions``；
4. 模糊表述判据 —— ``HEDGE_PREFIX_RE`` / ``HEDGE_SUFFIX_RE`` /
   ``BARE_PLACEHOLDER_PHRASES`` / ``LIMIT_PHRASES`` 从 ``content_standard.py``
   下沉至此，两侧共用同一对象（防止「同一判据两处各自实现」的历史病）。

层级约束：本模块属 services 层，禁止 import routers。
坐标口径：所有 ``char_start`` / ``char_end`` 均为**调用方传入文本**的字符偏移
（若传入的是 ``strip_code_blocks`` 剔除围栏后的文本，则不映射回原文，
人工定位以 ``excerpt`` 片段为准）。
"""
from __future__ import annotations

import re

from app.services.placeholder_inventory import RE_BARE, RE_FORMATTED, RE_FUZZY

# ---------------------------------------------------------------------------
# 一、模糊生成规则对照表（八类，唯一事实源）
# ---------------------------------------------------------------------------

#: 类别标识 → 规则条目。
#:
#:   label       中文名
#:   precise     有明确数据时必须怎么写（精准优先）
#:   fuzzy       无明确数据时应该怎么写（模糊生成）
#:   forbid      严禁编造什么（关键数据红线）
#:   data_source 模糊生成时的数据来源表述（内部标记 trace.data_source）
#:   reason      模糊生成原因（内部标记 trace.fuzzy_reason）
#:
#: 检测正则不放在本表内（编译期对象，见下方 ``_DETECTORS``）。
FUZZY_CATEGORIES: dict[str, dict] = {
    "number": {
        "label": "数值类",
        "precise": "有明确数值直接使用，数值连同单位一并写出（如“基坑深度 12.5m”）",
        "fuzzy": "有范围按范围表述（如“控制在 12~15m 范围内”）；"
                 "无范围按行业惯例表述（如“控制在设计允许范围内”“控制在规范限值以内”）",
        "forbid": "禁止编造具体数值",
        "data_source": "行业惯例与设计允许范围",
        "reason": "事实未给出该数值",
    },
    "name": {
        "label": "名称类",
        "precise": "有明确名称直接使用（人员写姓名与岗位、设备写品牌与型号）",
        "fuzzy": "无明确名称用岗位或角色表述（如“项目技术负责人”“专职安全生产管理人员”"
                 "“满足要求的塔吊”）",
        "forbid": "禁止编造人名、公司名、品牌名、设备型号",
        "data_source": "岗位与角色表述",
        "reason": "事实未给出具体名称",
    },
    "time": {
        "label": "时间类",
        "precise": "有明确时间直接使用（如“总工期 450 日历天”“2026年8月15日前完成”）",
        "fuzzy": "无明确时间用相对时间或阶段表述（如“主体结构完成后”“施工高峰期前”"
                 "“进场施工后 7 天内”）",
        "forbid": "禁止编造具体日期",
        "data_source": "施工阶段与相对时序",
        "reason": "事实未给出具体日期",
    },
    "quantity": {
        "label": "数量类",
        "precise": "有明确数量直接使用（如“塔吊 1 台”“作业人员 12 人”）",
        "fuzzy": "无明确数量用范围或配置原则表述（如“按同类工程经验配置 2~3 台”"
                 "“按规范配置足够数量的作业人员”）",
        "forbid": "禁止编造具体人数、设备台数",
        "data_source": "资源配置原则",
        "reason": "事实未给出具体数量",
    },
    "promise": {
        "label": "承诺类",
        "precise": "有明确承诺直接使用（原样引用合同与设计文件中的承诺口径）",
        "fuzzy": "无明确承诺用行业通用承诺表述（如“确保施工质量满足设计与合同要求”"
                 "“确保各项检验批一次验收合格”）",
        "forbid": "禁止编造超出常规的承诺（工期承诺、质量奖、免责条款等）",
        "data_source": "行业通用承诺口径",
        "reason": "事实未给出明确承诺",
    },
    "tech": {
        "label": "技术参数类",
        "precise": "有明确参数直接使用（原样引用事实中的强度等级、荷载、偏差、间距等）",
        "fuzzy": "无明确参数用技术原则或标准要求表述（如“参数取值经专项计算确定”"
                 "“满足现行规范要求”）",
        "forbid": "禁止编造具体参数值",
        "data_source": "技术原则与标准要求",
        "reason": "事实未给出具体参数",
    },
    "material": {
        "label": "材料规格类",
        "precise": "有明确规格直接使用（混凝土强度等级、钢筋牌号、防水材料品种等"
                   "原样引用事实，如“HRB400 钢筋”“C30 混凝土”）",
        "fuzzy": "无明确规格用符合规范要求的通用表述（如“材料品种、规格符合设计文件"
                 "与现行规范要求，进场后按批次检验复验合格后使用”）",
        "forbid": "禁止编造具体牌号、品牌与型号",
        "data_source": "规范符合性通用表述",
        "reason": "事实未给出材料规格",
    },
    "process": {
        "label": "工序流程类",
        "precise": "有明确工序直接按事实顺序编排（含节点顺序与做法细节）",
        "fuzzy": "无明确工序按标准施工工艺表述（沿本工种常规工序顺序展开，"
                 "写明各环节控制点，如“测量放线→支架搭设→预压→底模安装”）",
        "forbid": "禁止编造不存在的工序",
        "data_source": "标准施工工艺顺序",
        "reason": "事实未给出工序安排",
    },
}

#: 类别遍历与重叠去重的优先级（前面的类别优先占用区间）。
#: number/time/quantity/name 语义更具体，放在前面避免被 tech 的原则性表述吞掉；
#: material/process（2026-10-02 补齐）带材料/工序名词前缀，比 tech 的泛化措辞更具体，
#: 排在 tech 之前，避免「材料符合设计要求」被 tech 抢先归类。
FUZZY_CATEGORY_ORDER: tuple[str, ...] = (
    "number", "time", "quantity", "name", "material", "process", "tech", "promise")

#: 八类中**无法确定性判定**的类别，仅用于文档与报告说明（不产生误报）。
#: promise 的“确保 / 保证”在方案正文中极常见，强行判定会把正常表述全标成模糊。
FUZZY_UNDETECTABLE: tuple[str, ...] = ("promise",)


# ---------------------------------------------------------------------------
# 二、模糊表述判据（由 content_standard 下沉至此，两侧共用同一对象）
# ---------------------------------------------------------------------------

#: 模糊表述：前缀型（约/大约/大概 + 数字）。
#: 负向后顾屏蔽「合约 12」这类「约」非模糊语素。
HEDGE_PREFIX_RE = re.compile(r"(?<![合结])(约|大约|大概)\s*(\d[\d.]{0,12})")
#: 模糊表述：后缀型（数字 + 可选单位 + 左右/上下/前后，如“12.5 米左右”）
HEDGE_SUFFIX_RE = re.compile(r"(\d[\d.]{0,12})[^0-9]{0,3}(左右|上下|前后)")

#: 模糊表述：空泛指代（无事实豁免例外）
BARE_PLACEHOLDER_PHRASES: tuple[str, ...] = (
    "满足要求的", "符合规范的", "符合要求的", "按设计确定", "按设计要求确定",
    "按设计要求", "按合同要求", "按业主要求", "按相关规定", "按有关规定", "按规范要求",
)
#: 模糊表述：限值措辞（仅当事实本身使用同一措辞时豁免）
LIMIT_PHRASES: tuple[str, ...] = (
    "不少于", "不超过", "不小于", "不大于", "不高于", "不低于",
    "不短于", "不长于", "不厚于", "不多于",
)

_LIMIT_PHRASE_RE = re.compile("|".join(re.escape(p) for p in LIMIT_PHRASES))


# ---------------------------------------------------------------------------
# 三、各类别的模糊表达检测器（仅用于内部标记与质量校验）
# ---------------------------------------------------------------------------

#: 数值类：数值区间（如“12~15m”“12-15 米”）
_RANGE_NUM_RE = re.compile(
    r"\d[\d.]*\s*[~～—－\-]\s*\d[\d.]*\s*"
    r"(?:m|米|mm|cm|km|m3|立方米|m2|平方米|kPa|MPa|人|台|套|层|根|块|个|吨"
    r"|天|日历天|周|年|%|‰)"
)
#: 名称类：岗位 / 角色称谓（这是「不编造人名」时的合规模糊写法）
_ROLE_TERM_RE = re.compile(
    r"项目经理|技术负责人|项目技术负责人|施工员|质量员|安全员|专职安全员|"
    r"专职安全生产管理人员|监理人员|监理工程师|作业人员|作业班组|施工班组|"
    r"现场管理人员|测量员|机械管理员|资料员"
)
#: 时间类：相对时间 / 阶段表述
_RELATIVE_TIME_RE = re.compile(
    r"(?:前期|中期|后期|施工高峰期?|浇筑完成(?:后)?|结构(?:完成|形成)(?:后)?|"
    r"(?:前|上)道工序完成(?:后)?|各(?:阶段|工序)(?:施工)?(?:前|后)?|"
    r"进场(?:施工)?后\s*\d{1,3}\s*(?:天|日历天|h)|主体结构(?:完成|封顶)(?:后)?)"
)
#: 数量类：范围 / 配置原则
_QUANTITY_PRINCIPLE_RE = re.compile(
    r"若干(?:个|台|人|套|处|名)?|适量|按需配置|"
    r"按(?:同类工程经验|规范|设计|施工)?(?:要求)?配置(?:足够)?数量"
)
#: 技术参数类：技术原则 / 标准要求
_TECH_PRINCIPLE_RE = re.compile(
    r"按(?:现行|相关)?(?:国家|行业|地方)?规范(?:要求|规定)?|"
    r"经专项计算(?:确定|验算)|由(?:设计文件|计算书|勘察报告)确定|"
    r"按设计(?:文件)?(?:要求)?(?:确定|执行)|符合(?:现行)?(?:国家)?规范(?:要求)?|"
    r"满足设计要求|以设计文件为准|以专项计算为准"
)
#: 材料规格类：规范符合性通用表述（2026-10-02 补齐）。
#: 刻意要求**材料名词前缀**（材料/构配件/钢筋…），否则会与 tech 的
#: 「符合规范要求」全线重叠、把无材料语境的技术表述也抢归材料类。
#: 两段短桥各 ≤12 字：名词→「符合」（如「品种、规格」「的强度应」），
#: 「符合」→「要求/规定」（如「设计文件」「与现行规范」）；
#: 超桥即不认，防止长距离误拼。
_MAT_PRINCIPLE_RE = re.compile(
    r"(?:材料|构配件|配件|钢筋|混凝土|水泥|砂石|防水材料|钢管|扣件|焊条)"
    r"[^。；\n]{0,12}?符合[^。；\n]{0,12}?(?:要求|规定)"
    r"|选用符合(?:设计文件|现行)?(?:规范|标准)要求的"
)
#: 工序流程类：标准施工工艺顺序表述（2026-10-02 补齐）。
#: 只认显式的「按…施工工艺/工序流程」与「先…后…」顺序句式，
#: 保守判定避免把普通叙述刷成模糊工序。
_PROC_PRINCIPLE_RE = re.compile(
    r"(?:按|依照|遵循|参照)(?:标准|常规|成熟|通行)(?:施工工艺|工序|流程|做法)"
    r"|按(?:同类工程|标准)(?:经验|工艺)(?:组织施工|执行|实施)"
    r"|先[^。；\n]{1,24}，后[^。；\n]{1,24}(?:施工|作业|进行)"
)
#: 承诺类：行业通用承诺口径（刻意保守：不含单独的「确保」「保证」，避免满篇误标）
_PROMISE_PRINCIPLE_RE = re.compile(
    r"满足(?:合同|业主|设计)(?:约定|要求)|按(?:合同|业主)(?:约定|要求)执行|"
    r"确保(?:施工质量|工程质量|安全生产|施工安全)(?:符合|满足)(?:合同|规范|设计)(?:要求|规定)?"
)

#: category → 检测正则集合
_DETECTORS: dict[str, tuple[re.Pattern, ...]] = {
    "number": (HEDGE_PREFIX_RE, HEDGE_SUFFIX_RE, _RANGE_NUM_RE),
    "name": (_ROLE_TERM_RE,),
    "time": (_RELATIVE_TIME_RE,),
    "quantity": (_QUANTITY_PRINCIPLE_RE,),
    "material": (_MAT_PRINCIPLE_RE,),
    "process": (_PROC_PRINCIPLE_RE,),
    "tech": (_TECH_PRINCIPLE_RE, _LIMIT_PHRASE_RE),
    "promise": (_PROMISE_PRINCIPLE_RE,),
}


def detect_fuzzy_expressions(text: str) -> list[dict]:
    """按类别定位正文中的模糊生成片段（纯函数，非法输入返回空表）。

    返回 [{"category", "text", "char_start", "char_end"}]，按 ``char_start``
    升序；**重叠区间去重**时保留优先级更高的类别（见 ``FUZZY_CATEGORY_ORDER``）。

    """
    if not isinstance(text, str) or not text:
        return []
    accepted: list[dict] = []
    try:
        for cat in FUZZY_CATEGORY_ORDER:
            for rx in _DETECTORS.get(cat, ()):
                for m in rx.finditer(text):
                    s, e = m.span()
                    if e <= s:
                        continue
                    # ✅ BUG 修复（2026-10-02 第二十六轮）：旧写法 `for as_, ae, _ in
                    # accepted` 对 4 键字典解包必抛 ValueError → 被外层 except 吞掉，
                    # 导致「正文有 ≥2 处模糊表述时整体丢失标记」。改为显式取键。
                    if any(s < h["char_end"] and e > h["char_start"]
                           for h in accepted):
                        continue  # 已被优先级更高的类别占用
                    accepted.append({
                        "category": cat,
                        "text": m.group(0),
                        "char_start": s,
                        "char_end": e,
                    })
        accepted.sort(key=lambda d: d["char_start"])
    except Exception:
        return []
    return accepted


# ---------------------------------------------------------------------------
# 四、占位标记 / 空话 / 暴露缺失 / 编造日期 检测
# ---------------------------------------------------------------------------

#: 额外占位标记 · **可确定性改写**子集（【…】/ [ … ] 包裹形态，语义明确）。
#:
#: ⚠️ 2026-10-03 拆分原因（生产缺陷驱动）：占位标记的**检测**与**自动改写**
#: 原先在 ``content_checkpoint`` 里另写了一份**更窄**的副本，只收
#: ``【待(补充|定|确认)`` + ``[待补充]`` + ``××``。结果是
#: ``【待完善】【待补录】【略】【】[TBD]`` 等形态被 :func:`scan_placeholder_marks`
#: **检出**，但 CON-04 自检漏报、``rewrite_placeholder_marks`` 也**改不动** ——
#: 「检出却修不掉」。现把检测与改写共用同一份正则表，处置差异用
#: ``rewritable`` 标记表达，不再靠两份副本区分。
#: 刻意要求**括号包裹**：括号形态是明确的「此处应填值」语义，可安全改写为
#: 条件式表述；裸词形态（下条）上下文不明，改错比不改更糟，只报不改。
_EXTRA_MARK_BRACKETED_RE = re.compile(
    r"【\s*(?:待完善|待确定|待确认|待补录|待补|待定|略)\s*(?:[:：][^】]{0,60})?\s*】"
    r"|\[\s*(?:待补充|待完善|待确定|待确认|待补录|待补|待定|数值|参数|TBD)"
    r"\s*(?:[:：][^\]]{0,60})?\s*\]"
    r"|【\s*】|\[\s*\]"
)

#: 额外占位标记 · **只报不改**子集（裸词形态，上下文不明）。
#: 边界守卫与旧版一致：前后不得是英文字母，避免命中 CNA / TBDX 一类词内片段。
_EXTRA_MARK_BARE_RE = re.compile(r"(?<![A-Za-z])(?:TBD|t\.b\.d|N/?A)(?![A-Za-z])")

#: 额外占位标记全集（兼容既有口径：检测用；由上两条**派生**，不再另写字面量）
_EXTRA_MARK_RE = re.compile(
    "(?:" + _EXTRA_MARK_BRACKETED_RE.pattern + ")|(?:" + _EXTRA_MARK_BARE_RE.pattern + ")"
)

#: 占位标记检测 · **唯一事实源**（三类基础 + 扩展，附「可否确定性改写」标记）。
#:
#: 元素形态 ``(正则, kind, rewritable)``：
#: - ``kind`` 与既有口径完全一致（formatted / bare / fuzzy / extended），
#:   纯**加法式**新增 ``rewritable``，不改动任何既有取值；
#: - ``rewritable=True`` → 可被 ``rewrite_placeholder_marks`` 改写为条件式表述；
#: - ``rewritable=False`` → 只报出待人工复核（``××`` 与裸 ``TBD``/``N/A``
#:   上下文不明，自动改写极易误伤正常正文）。
#:
#: ⚠️ 新增占位标记形态时**只改这一处**：检测（:func:`scan_placeholder_marks`）
#: 与改写（``content_checkpoint.rewrite_placeholder_marks``）都从此表取用。
PLACEHOLDER_MARK_PATTERNS: tuple[tuple[re.Pattern, str, bool], ...] = (
    (RE_FORMATTED, "formatted", True),
    (RE_BARE, "bare", True),
    (RE_FUZZY, "fuzzy", False),
    (_EXTRA_MARK_BRACKETED_RE, "extended", True),
    (_EXTRA_MARK_BARE_RE, "extended", False),
)


def placeholder_rewritable_patterns() -> tuple[re.Pattern, ...]:
    """返回「可确定性改写」的占位标记正则集合（改写侧的唯一出口）。

    为什么单独给一个函数而不是让调用方自己过滤：改写侧**绝不能**误用检测侧
    的全量表 —— 把 ``××`` / 裸 ``TBD`` 也送进改写会破坏正常正文（乘号、
    单位缩写）。集中在一处过滤，调用点无机会选错。
    """
    return tuple(rx for rx, _k, rw in PLACEHOLDER_MARK_PATTERNS if rw)


def placeholder_nonrewritable_patterns() -> tuple[re.Pattern, ...]:
    """返回「只报不改」的占位标记正则集合（改写侧报 pending 的唯一出口）。

    与上条互补：这两条合起来**恒等于** ``PLACEHOLDER_MARK_PATTERNS`` 的全集，
    因此收敛判据后不会丢失任何可观测性 —— 上下文不明的形态（``××``、
    裸 ``TBD``/``N/A``）自动改写会误伤正文，但仍必须**报出**待人工复核。
    """
    return tuple(rx for rx, _k, rw in PLACEHOLDER_MARK_PATTERNS if not rw)

#: 空话句式（明确禁止，无条件报错）。
#: 刻意**不含**「以现场实际情况调整」这类真实工程免责表述 —— 它是有实质约束的
#: 条件式写法，不是空话；把正常写法判成缺陷只会逼 AI 换个说法继续凑字。
VAGUE_STATEMENT_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"此处省略"), "省略式空话"),
    (re.compile(r"此处略"), "省略式空话"),
    (re.compile(r"具体(?:内容|细节|做法|数据)?(?:另行|另见)"), "悬空指代式空话"),
    (re.compile(r"(?:细节|内容|做法)另见"), "悬空指代式空话"),
    (re.compile(r"详见附件"), "指向附件式空话"),
    (re.compile(r"详见(?:相关|下文|附件|附录|另文)"), "指向附件式空话"),
    (re.compile(r"后续(?:补充|完善|再行)"), "延后补写式空话"),
    (re.compile(r"待后续(?:补充|完善|确定)"), "延后补写式空话"),
    (re.compile(r"另有(?:说明|章节)?(?:另行)?介绍"), "悬空指代式空话"),
)

#: 暴露数据缺失的表述（严禁出现在正文中 —— 模糊内容不得露痕迹）
MISSING_REVEAL_PHRASES: tuple[str, ...] = (
    "由于资料不足", "资料不足", "资料未提供", "未提供资料", "未提供相关",
    "暂未获取", "尚未获取", "未能获取", "未能取得",
    "缺少资料", "缺少相关", "缺少具体", "未提供具体",
    "无法确定", "无法确认", "尚不明确", "尚未明确", "有待明确",
    "尚未确定", "尚无法", "暂无法", "未能提供",
)

#: 具体日历日期（年月日齐全）。方案正文里凭空写具体日期几乎必然是编造。
_CALENDAR_DATE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")

#: 悬空判定用的「实质锚点」：句子里只要有一个，就不算空话
_ANCHOR_NUM_UNIT_RE = re.compile(
    r"\d[\d.,]{0,12}\s*"
    r"(?:m|mm|cm|km|m3|立方米|m2|平方米|MPa|kPa|kN|kN/m|人|台|套|层|根|块|个|米|吨"
    r"|天|日历天|工作日|次|%|‰|t|kg|h|d|°C|℃|万元)"
)
_ANCHOR_NORM_RE = re.compile(
    r"(?:GB|JGJ|JG|DBJ|CJJ|DB|GB/T|TB|SH|SY)[\s　]*\d{4,6}"
)


def scan_placeholder_marks(text: str) -> list[dict]:
    """扫描正文中的占位标记（四类），返回逐条记录。

    四类口径：``formatted`` / ``bare`` / ``fuzzy`` 三类沿用
    ``placeholder_inventory`` 的正则（与《待补充清单》零分叉）；
    ``extended`` 为本轮新增的补充标记（【待完善】【TBD】【N/A】【】 等）。
    """
    if not isinstance(text, str) or not text:
        return []
    hits: list[dict] = []
    # ⚠️ 判据只从 PLACEHOLDER_MARK_PATTERNS 取用 —— 检测与改写共用同一份表，
    # 不再在此处手抄正则（本仓反复踩过的「同一判据两处实现」分叉病根）。
    for rx, kind, rewritable in PLACEHOLDER_MARK_PATTERNS:
        for m in rx.finditer(text):
            hits.append({
                "kind": kind,
                # 加法式字段：改写侧按此筛选，不影响只读 kind 的既有消费方
                "rewritable": rewritable,
                "mark": m.group(0),
                "char_start": m.start(),
                "char_end": m.end(),
            })
    hits.sort(key=lambda d: d["char_start"])
    return _prune_overlaps(hits)


_SENTENCE_END_RE = re.compile("[。；;！？\n]")
_SENTENCE_END_RE_FWD = re.compile("[。；;！？\n]")


def _sentence_at(text: str, pos: int) -> str:
    """取 pos 所在的**完整句子**（按句末标点 / 换行切分）。

    ⚠️ 刻意**不含逗号**：中文正文里「，」是逗号分句，锚点常常落在分句之后
    （如「设备按合同要求进场，采用 800mm 灌注桩」）—— 若按逗号切句，
    分句一变成「无锚点」→ 整句被判成悬空空话，误报。
    """
    lo = 0
    for p in _SENTENCE_END_RE.finditer(text[:pos]):
        lo = p.end()
    hi = len(text)
    m = _SENTENCE_END_RE_FWD.search(text[pos:])
    if m:
        hi = pos + m.start()
    return text[lo:hi]


def _sentence_is_hollow(sentence: str) -> bool:
    """判断句子是否「悬空」：不含数值+单位、不含规范编号、不含限值措辞。"""
    s = sentence or ""
    if not s.strip():
        return True
    if _ANCHOR_NUM_UNIT_RE.search(s):
        return False
    if _ANCHOR_NORM_RE.search(s):
        return False
    if _LIMIT_PHRASE_RE.search(s):
        return False
    return True


def scan_vague_statements(text: str) -> list[dict]:
    """扫描「空话」（禁止出现），返回逐条记录。

    两类来源：
      1. ``VAGUE_STATEMENT_RULES`` 命中的明确空话句式 —— 无条件报；
      2. ``BARE_PLACEHOLDER_PHRASES`` 命中的空泛指代 —— **仅当所在句悬空**才报。

    第 2 类的悬空判定是关键：``按合同要求组织工序，总工期 450 日历天`` 里有具体
    数值锚点，属正常条件式写法，不报；只有 ``设备按合同要求进场`` 这种整句无
    实质内容的才算空话。否则会退化成「凡出现『按规范』就报错」的满篇误报。
    """
    if not isinstance(text, str) or not text:
        return []
    hits: list[dict] = []
    for rx, label in VAGUE_STATEMENT_RULES:
        for m in rx.finditer(text):
            hits.append({"kind": "vague", "label": label, "mark": m.group(0),
                         "char_start": m.start(), "char_end": m.end()})
    for phrase in BARE_PLACEHOLDER_PHRASES:
        start = 0
        while True:
            pos = text.find(phrase, start)
            if pos < 0:
                break
            start = pos + len(phrase)
            if not _sentence_is_hollow(_sentence_at(text, pos)):
                continue  # 句子里有实质锚点：属正常条件式写法，不报空话
            hits.append({"kind": "vague", "label": "悬空空泛指代", "mark": phrase,
                         "char_start": pos, "char_end": pos + len(phrase)})
    hits.sort(key=lambda d: d["char_start"])
    return _prune_overlaps(hits)


def _prune_overlaps(hits: list[dict]) -> list[dict]:
    """剔除被包含的重叠命中（保留最长、起点最前者），再按起点排序。

    必要性：短语表里天然存在包含关系（「由于资料不足」⊃「资料不足」），
    若不去重，同一处正文会被重复计入 2 次 —— 统计虚高、报告刷屏。
    """
    kept: list[dict] = []
    for h in sorted(hits, key=lambda d: (-(int(d.get("char_end", 0)) - int(d.get("char_start", 0))),
                                         int(d.get("char_start", 0)))):
        s, e = int(h.get("char_start", 0)), int(h.get("char_end", 0))
        if all(e <= k["char_start"] or s >= k["char_end"] for k in kept):
            kept.append(h)
    kept.sort(key=lambda d: int(d.get("char_start", 0)))
    return kept


def scan_missing_reveal(text: str) -> list[dict]:
    """扫描「暴露数据缺失」的表述（禁止出现 —— 模糊内容不得露痕迹）。"""
    if not isinstance(text, str) or not text:
        return []
    hits: list[dict] = []
    for phrase in MISSING_REVEAL_PHRASES:
        start = 0
        while True:
            pos = text.find(phrase, start)
            if pos < 0:
                break
            start = pos + len(phrase)
            hits.append({"kind": "missing_reveal", "mark": phrase,
                         "char_start": pos, "char_end": pos + len(phrase)})
    hits.sort(key=lambda d: d["char_start"])
    return _prune_overlaps(hits)


# ---------------------------------------------------------------------------
# 五、提示词块（注入 system / user，文案单点维护，禁止散落）
# ---------------------------------------------------------------------------

def build_fuzzy_rules_block() -> str:
    """生成「模糊生成规则」提示词段落（八类对照表 + 质量要求）。"""
    lines = [
        "## 模糊生成规则（无明确数据时的唯一写法，必须遵守）",
        "",
        "正文必须**完整生成**：不留空、不写占位标记、不写空话。"
        "凡【全局事实变量】中没有明确数据的内容，按下表处理 —— "
        "只做合理概括、归纳、泛化，**严禁编造关键数据**：",
        "",
        "| 类别 | 有明确数据时 | 无明确数据时（模糊生成） | 严禁编造 |",
        "| --- | --- | --- | --- |",
    ]
    # 直接遍历规则表（插入顺序即展示顺序）：不再另写一份硬编码元组，
    # 避免「新增类别后提示词漏渲染」的表/文案分叉。
    for key, c in FUZZY_CATEGORIES.items():
        lines.append(f"| {c['label']} | {c['precise']} | {c['fuzzy']} | {c['forbid']} |")
    lines += [
        "",
        "### 模糊生成质量要求（与数据红线同级）",
        "- **语言自然**：像一线工程师写的技术文件，不用套话模板、不重复句式，"
        "不要机械地反复使用同一种限定词；",
        "- **逻辑连贯**：与上下文、全局事实、同级章节都不冲突，"
        "不重复同级章节已经写过的内容；",
        "- **专业可信**：术语准确，符合专项施工方案文档风格与行业惯例；"
        "模糊表述仍要落在具体工艺、工序、控制点上，不能只有结论没有做法；",
        "- **不露痕迹**：严禁出现「由于资料不足」「暂未获取」「无法确定」"
        "「尚未明确」等暴露数据缺失的说法；模糊内容与精准内容自然衔接，"
        "读者看不出哪一句是因为缺数据才概括写的。",
    ]
    return "\n".join(lines)


def build_no_placeholder_block() -> str:
    """生成「禁止占位标记与空话」提示词段落（硬性，与数据真实性红线同级）。"""
    return "\n".join([
        "## 禁止占位标记与空话（硬性）",
        "正文的**每一个位置**都必须有实质内容。以下三类表述一律严禁出现：",
        "",
        "1. **占位标记**：【待补充】【待完善】【待填写】【待定】【待确定】、"
        "【】、[数值]、[参数]、TBD、N/A、××、xx —— 一个都不允许出现；",
        "2. **空话**：此处省略、详见附件、后续补充、后续完善、"
        "具体做法另行说明、另有章节介绍；",
        "3. **暴露数据缺失的说法**：由于资料不足、暂未获取、无法确定、"
        "尚未明确、有待明确。",
        "",
        "确实没有明确数据时，按「模糊生成规则」写 —— "
        "把话说完、把做法说清，而不是留一个标记让人去补。",
    ])


def build_data_availability_block(available: list[str] | None = None,
                                  unknown: list[str] | None = None) -> str:
    """生成「本章可用数据 / 缺失数据」说明块（注入 user 消息）。

    让 AI 明确知道哪些字段有明确数据（必须精准使用）、哪些没有（走模糊生成），
    避免它在有数据时反而模糊、或无数据时硬编。
    两个参数都为空列表时返回空串（调用方据此决定是否拼接）。
    """
    if not available and not unknown:
        return ""
    lines = ["## 本章数据可用性"]
    if available:
        lines += [
            "",
            "以下内容**有明确数据**，必须精准使用（数值连同单位原样写出，"
            "不得改写、不得概括、不得四舍五入）：",
        ]
        lines += [f"- {item}" for item in available]
    if unknown:
        lines += [
            "",
            "以下内容**没有明确数据**，按「模糊生成规则」处理"
            "（合理概括 / 归纳 / 泛化；严禁编造关键数据，严禁写占位标记）：",
        ]
        lines += [f"- {item}" for item in unknown]
    return "\n".join(lines)


#: 分级生成策略说明（注入 user 消息，与「生成标准」段同级）
GENERATION_STRATEGY_LINES: tuple[str, ...] = (
    "- **有明确数据** → 精准生成：全局事实与解析提取结果中的数值、型号、"
    "人员、工期、参数必须原样使用，不得改写；",
    "- **有部分数据** → 精准 + 模糊组合：已知部分原样使用，缺口部分"
    "按「模糊生成规则」补齐，两者在同一句或同一段里自然衔接；",
    "- **无明确数据** → 全部模糊生成：按八类规则做合理概括，"
    "仍须落在具体工艺与工序上，不得编造关键数据；",
    "- 无论哪种情况，都返回**完整正文**，不留空、不写占位标记、不写空话。",
)


def build_generation_strategy_block() -> str:
    """生成「分级生成策略」提示词段落。"""
    return "\n".join(
        ["## 分级生成策略（按本章数据完整性自动判定）"] + list(GENERATION_STRATEGY_LINES)
    )


def build_fuzzy_rules_for_standard(standard: str) -> str:
    """按生成标准拼装「模糊生成 + 禁止占位」提示词块（单一出口）。

    - ``precise``：精准优先，但缺失处**同样**按模糊规则补齐（不再留占位标记）；
    - ``fuzzy``  ：允许范围值与限定性表述，但底线是不编造关键数据。

    两档都包含同一套「禁止占位标记与空话」段落 —— 这是本轮需求的核心不变式。
    """
    head = {
        "precise": "本次为**精准内容**模式：有明确数据的必须精准使用；"
                   "没有明确数据的按下方规则做模糊生成，**一律不得写占位标记**。",
        "fuzzy": "本次为**模糊内容**模式：可使用范围值、岗位称谓、相对时间与"
                 "限定性表述；底线是**不得编造关键数据**，且不得写占位标记。",
    }.get(standard or "precise", "")
    return "\n\n".join([head, build_fuzzy_rules_block(),
                        build_generation_strategy_block(),
                        build_no_placeholder_block()])


def scan_fabricated_dates(text: str, fact_text: str = "") -> list[dict]:
    """扫描「凭空编造的具体日期」。

    判据：正文出现年月日齐全的日历日期，且该日期字符串**不出现在任何全局事实
    文本中**。方案正文里的具体日期只能来自事实（合同工期节点）或设计文件，
    凭空写出几乎必然是 AI 编造 —— 这是「不得编造关键数据」里最容易失控的一类。

    只匹配年月日齐全的日期，因此引用规范年号（``GB 55034-2022``）、施工工期
    表述（``2022 年 8 月`` 无日）都不会命中。
    """
    if not isinstance(text, str) or not text:
        return []
    hits: list[dict] = []
    fact_norm = re.sub(r"[\s　]+", "", fact_text or "")
    for m in _CALENDAR_DATE_RE.finditer(text):
        norm = re.sub(r"[\s　]+", "", m.group(0))
        if norm in fact_norm:
            continue  # 事实里就有这个日期：精准引用，不判编造
        hits.append({"kind": "fabricated_date", "mark": m.group(0),
                     "char_start": m.start(), "char_end": m.end()})
    return _prune_overlaps(hits)