"""正文生成的「有效数据调用」契约层（R52）。

把「提取项目内容 / 全局事实 → 正文生成」的**调用关系**变成可枚举、可注入、
可追溯、可审计的确定性结构，针对四类生产实证缺陷：

1. **前后数据不一致（P0）**：同一关键数据在不同章节出现不同取值。生产库实证
   （scheme d3c1a897…，33 章有正文）：「总工期」在 4 章出现 4 种口径 ——
   「工程基本情况」184 天 /「劳动力配置计划」195 天 /「劳动力配置计划」120 天 /
   「施工重难点与对策」6 个月；而 ``global_facts`` 里**没有**工期权威值，各章
   AI 只能各自编造。对策：:func:`build_global_data_dictionary` 把每个前后
   一致字段的**权威值**收敛为「一个字段只有一个值」，作为唯一取值表注入。

2. **数据未调用 / 调用不完整（P0）**：逐章相关性预筛把大量全局事实裁掉。
   生产库实证：9 个章节共丢弃 3399 条次，单章丢弃率 60%~95%
   （「监测内容与点布设」丢 121/127）。对策：:func:`consistency_facts_rows`
   给出「承载一致性主题值的事实行」集合，调用方必须无条件保留 —— 这些值被
   裁掉后 AI 看不到权威值只能自己编，缺陷 1 随之产生。

3. **读截断**：单条事实超 ``per_fact`` 上限被截断（生产实证「执行规范」452 字
   截到 300 字，尾部规范编号消失）。对策：数据字典抽取发生在**原始 content**
   上（不经任何截断），权威值不随事实文本截断而丢失。

4. **调用不可追溯 / 无对照**：无法回答「本章调用了哪些字段、哪些字段在库里
   但没被读、读了但正文没体现」。对策：:func:`section_data_trace`（每章调用
   痕迹）+ :func:`audit_call_matrix`（字段完整性对照表 / 调用完整性对照表 /
   缺失清单），落 ``last_generation_report`` 供前端消费。

设计约束：

* **判据不复制**：跨章节数值一致性判定（正则 + 单位归一）由
  ``preflight_engine.numeric_consistency_findings`` 唯一实现，本模块只引用其
  常量，与预检 CON-01 同源（同 TRC-01 / CON-06 的收敛模式）。
* **权威值只来自 global_facts**：绝不从正文反推权威值（否则「后面章节跟着
  前面写错的内容错」无法避免）。
* **零 AI、零 IO、fail-soft**：全部纯函数，脏输入返回空结构，绝不抛出。
"""
from __future__ import annotations

import logging
import re

from .preflight_engine import normalize_unit_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 一致性主题规则表（唯一事实源）
# ---------------------------------------------------------------------------
# 四元组：(key, 主题名, value_regex, object_hint)
#
#   key           数据字典键名（= 提示词展示名，稳定不变）
#   主题名         中文展示名，与 value_regex 的命名组 ``name`` 取同一词
#   value_regex   从「事实标题 + 事实内容」抽权威值。第一捕获组**必须**命名
#                 ``name`` 且匹配**主题词本身**，第二组起是取值与单位。
#                 ``_value_text`` 把匹配压成「<name><value><unit>」—— 同一
#                 主题无论写成「总工期 180 天」还是「工期约 6 个月」，压形
#                 一致，可直接作一致性判定 key。
#   object_hint   取值限定词，给「不同部位 / 不同对象」的差异化取值加前缀
#                 （见 :func:`_qualify_key`）。None = 无对象维度。
#
# ⚠️ 对象维度为何必要：生产事实里「保修期」有 5 条 —— 屋面及防水工程保修期
# 5 年 / 装修工程保修期 2 年 / 电气给排水设备安装工程保修期 2 年 /
# 缺陷责任期 24 个月 / 缺陷责任期最长期限 24 月。它们**不是矛盾**（各部位法定
# 保修期本就不同），不分对象会把 5 个合法取值误报成 CON-01 冲突；预检
# _NUM_TOPICS 的 object_group 也是这个口径 —— 两侧必须同向。
#
# ⚠️ 「总工期」与「工期」：生产事实里**没有**工期行（实证工期 facts=0），所以
# 「总工期约 184 天 / 195 天 / 120 天 / 6 个月」全是 AI 编造。``name`` 组写成
# 「总工期|工期」使两种写法各成一条压形 —— 这是期望行为（用户能在数据字典里
# 同时看到两种口径并手工裁决），而非被静默合并。
# 主题词与取值之间的「桥接」字符类（唯一事实源，各规则共用）。
#
# ⚠️ 为何不能用 ``[^0-9]``：生产事实标题常是复合词 —— 「缺陷责任期最长期限」
# 「缺陷责任期届满通知期限」都以「缺陷责任期」起首，`[^0-9]{0,16}` 会把
# 「最长期限」整个当桥接吃掉，于是三个语义完全不同的字段（24 月保修 / 7 天
# 通知 / 28 天撤离）被压成同一个 key，且真正该保留的「24 个月」被静默覆盖。
# 现桥接只允许**分隔符/空白**与**系词**（为|是|约|达|计|共），汉字后缀一律
# 不允许跨过 —— 语义不同的字段因此各自成键。
def _bridge(max_chars: int = 6) -> str:
    return r"(?:(?:[^\d\w\u4e00-\u9fff])|(?:为|是|约|达|计|共|超过|不低于|不高于|不少于)){0,%d}" % max_chars


#: 「项目全称」规则键。它必然与章节标题强相关、不会被预筛裁掉，
#: 所以不纳入 MUST_KEEP_TOPIC_NAMES（纳入只会让保留集合变大而无收益）。
_PROJECT_NAME_KEY = "项目全称"


CONSISTENCY_KEY_RULES: tuple[tuple[str, str, re.Pattern, tuple[str, ...] | None], ...] = (
    ("项目全称", "工程名称|项目名称|项目全称|工程全称",
     re.compile(r"(?P<name>工程名称|项目名称|项目全称|工程全称)"
                + _bridge()
                + r"([\u4e00-\u9fffA-Za-z0-9（）()、\-—]{2,60})"),
     None),
    ("计划开工日期", "计划开工日期|开工日期",
     re.compile(r"(?P<name>计划开工日期|开工日期)" + _bridge()
                + r"(\d{4}年\d{1,2}月\d{1,2}日)"),
     None),
    ("计划竣工日期", "计划竣工日期|竣工日期",
     re.compile(r"(?P<name>计划竣工日期|竣工日期)" + _bridge()
                + r"(\d{4}年\d{1,2}月\d{1,2}日)"),
     None),
    ("总工期", "总工期|工期",
     re.compile(r"(?P<name>总工期|建设工期|施工工期|工期)" + _bridge()
                + r"(\d+(?:\.\d+)?)\s*(日历天|自然天|天|个?月|周)"),
     None),
    ("保修期与缺陷责任期", "缺陷责任期|保修期|质量保证期",
     re.compile(r"(?P<name>缺陷责任期|保修期|质量保证期)" + _bridge()
                + r"(\d+(?:\.\d+)?)\s*(个?月|年|日历天|天)"),
     ("屋面及防水", "防水", "装修", "电气", "给排水", "安装", "外墙", "渗漏")),
    ("响应时限", "响应期限|响应时间|到场时间|到达时限",
     re.compile(r"(?P<name>响应期限|响应时间|到场时间|到场时限|到达时限|到场|响应)"
                + _bridge()
                + r"(\d+(?:\.\d+)?)\s*(小时|时|分钟|天|日历天|工作日)"),
     ("变更", "质量", "安全", "缺陷", "投诉", "索赔", "设计")),
    ("人员数量", "管理人员数|劳动力人数|作业人员数|作业人员人数|人员数量|人数",
     re.compile(r"(?P<name>管理人员数|管理人员|劳动力人数|劳动力|作业人员人数"
                r"|作业人员|人员数量|人数)" + _bridge()
                + r"(\d+(?:\.\d+)?)\s*(人|名)"),
     ("管理", "劳动力", "作业", "特种", "专职", "技术")),
    ("设备型号", "设备型号|机械型号|起重机型号|升降机型号",
     re.compile(r"(?P<name>设备型号|机械型号|起重机型号|升降机型号|塔机型号)"
                + _bridge()
                + r"([\w\-／/]{2,20})"),
     ("塔机", "升降机", "起重机", "吊车")),
)

#: 事实文本里会破坏取值抽取的 markdown 标记。
#: ⚠️ 生产事实 content 形态是 ``- **工程名称**: 上海市…``；若不先剥掉 ``*``，
#: 「项目全称」规则会把 ``**`` 当成取值抓进去（实测抓到 ``工程名称**``），
#: 数据字典里会出现一个完全无意义的权威值。
_MD_MARKS_RE = re.compile(r"[*#`~]")


def _prep(text: str) -> str:
    """抽取前统一清洗：剥掉 markdown 强调/标题标记（不改变数字与单位）。"""
    return _MD_MARKS_RE.sub("", str(text or ""))


def _topic_words() -> set:
    """规则表里全部主题词（把替代式按 ``|`` 展开）。

    规则表第二元素存的是**正则替代式**（如 ``"缺陷责任期|保修期"``），直接拿它
    与抽取结果里的**单个词**（``m.group("name")``）比较永远不相等 —— 审计表
    ``consistency_topics_covered`` 因此恒为空、``consistency_topics_missing``
    恒报全部主题（实测恒假红，等于审计表失去判据力）。这里统一展开，
    并用作**覆盖度自检**：抓到的词必须都在表内，否则就是某处替代式漂移。
    """
    words: set = set()
    for _k, pattern, _r, _o in CONSISTENCY_KEY_RULES:
        for w in str(pattern).split("|"):
            if w:
                words.add(w)
    return words


#: 规则键 → 主题词替代式（审计表展示用，让用户看清一个主题含哪些同义词）。
_RULE_ALIASES: dict[str, str] = {k: p for k, p, _r, _o in CONSISTENCY_KEY_RULES}


#: 参与「逐章必保留」判定的主题词集合（按 ``|`` 展开成单个词，可直接用于
#: 章节标题的逐词匹配）。不含「项目全称」—— 它必然与章节标题强相关、不会被
#: 预筛裁掉，纳入只会让保留集合变大而无收益。
MUST_KEEP_TOPIC_NAMES: frozenset[str] = frozenset(
    w for k, pattern, _r, _o in CONSISTENCY_KEY_RULES
    if k != _PROJECT_NAME_KEY
    for w in str(pattern).split("|") if w)

#: 数据字典注入块 / 跨章数据摘要的字符上限（提示词预算硬约束）。
DATA_DICT_MAX_CHARS = 1600
DIGEST_MAX_CHARS = 1500
DIGEST_MAX_ITEMS = 24

#: 低置信度阈值以下的事实不作为权威值（与 facts_builder 同口径）。
LOW_CONFIDENCE_FOR_DICT = 0.5



def _value_text(m: re.Match) -> str:
    """把一次匹配压成「<主题名><取值><单位>」形态（一致性判定 key）。

    主题名取自命名捕获组 ``name``；取值与单位为后续位置捕获组（可能缺省）。
    空格一律去掉，使「总工期 180 天」与「总工期180天」同形；单位再做归一
    （``个?月``→``月``、``日历天``→``天``…），使「24 个月」与「24 月」同形 ——
    归一逻辑复用 ``preflight_engine.normalize_unit_text``（与预检 CON-01 同一
    份单位表，避免本模块再抄一份）。
    """
    try:
        name = m.group("name") or ""
    except (IndexError, AssertionError):
        name = ""
    last = m.lastindex or 0
    tail = "".join(m.group(i) or "" for i in range(2, last + 1))
    return normalize_unit_text(re.sub(r"\s+", "", f"{name}{tail}"))


def _is_self_ref(m: re.Match, value_text: str) -> bool:
    """判断一次匹配是否是「主题词自己吞掉了主题词」。

    事实文本是 ``<标题> - **<标题>**: 值`` 形态，标题与内容会重复同一个词。
    桥接字符类允许跨过 ``-`` 和空格，于是**第一个**标题词能跨过分隔符把
    **第二个**标题词当成取值，产出「工程名称工程名称」「项目名称项目名称」
    这种完全无意义的权威值（实测复现）。判据：压形结果恰好等于
    「主题词 + 主题词」→ 判定为自指，跳过。
    """
    try:
        name = m.group("name") or ""
    except (IndexError, AssertionError):
        name = ""
    return bool(name) and value_text == f"{name}{name}"


#: 「工程名称 / 项目名称」这类**自由文本取值**规则里，出现这些词就判定为
#: 「抓到了后面的散文说明而不是工程名」。生产实证：章节正文写
#: 「……工程名称划分依据及数量说明」「……工程名称建设单位、施工单位、监理
#: 单位、开竣工日期等信息」，桥接字符类跨过空格后把整段说明当成工程名。
#: 工程名里出现这些词的概率极低，但工程名外的散文中几乎必然出现。
_NAME_VALUE_STOPWORDS = frozenset((
    "说明", "依据", "划分", "数量", "要求", "规定", "程序", "方法",
    "措施", "标准", "规范", "图纸", "清单", "单位", "工期", "编制",
))

#: 只对「自由文本取值」规则启用上面的停用词判据。
#: ⚠️ **绝不能对所有规则启用**：数值类规则的取值本身就会带主题词
#: （「总工期184天」含「工期」），无差别启用会把合法数值整条丢掉。
#: 新增自由文本规则时必须同步登记，否则会出现散文误抓；新增数值规则
#: 时**不要**登记，否则会出现合法值漏抽。
_FREE_TEXT_VALUE_KEYS = frozenset({_PROJECT_NAME_KEY})


def _stopwords_for(rule_key: str) -> frozenset:
    """按规则类型返回取值停用词（停用词判据的唯一分派点）。"""
    return _NAME_VALUE_STOPWORDS if rule_key in _FREE_TEXT_VALUE_KEYS else frozenset()


def _iter_value_matches(text: str, rx: re.Pattern, *,
                        name_value_stopwords: frozenset = frozenset()):
    """按「自指匹配需重扫」的语义逐次产出 ``(match, value_text)``。

    ⚠️ 为什么不能用 ``finditer`` 加 continue：自指匹配已经**消费掉了第二个
    同名主题词**，``finditer`` 会继续从该匹配的末尾找，于是「项目名称 -
    项目名称: 上海市…」里真正带取值的那一处再也没机会被扫到（实测复现：
    工程名称 / 项目名称两个权威值被整条吞掉）。自指命中时从
    ``m.start() + 1`` 重扫即可找回。

    ``name_value_stopwords`` 用于「自由文本取值」规则（如工程名称）：取值里
    出现说明性名词时判为误抓，直接丢弃（见 _NAME_VALUE_STOPWORDS）。
    """
    pos = 0
    while True:
        m = rx.search(text, pos)
        if not m:
            return
        vt = _value_text(m)
        nm = m.group("name") or ""
        bare = len(vt) <= len(nm)                        # 只取到主题名，没取到取值
        self_ref = (not bare) and _is_self_ref(m, vt)      # 主题词自吞主题词
        noisy = (bool(name_value_stopwords)
                 and any(w in vt for w in name_value_stopwords))
        if bare or self_ref or noisy:
            # 自指匹配吞掉了下一个同名词 → 只前进 1 字符重扫；
            # 其余情况直接从匹配末尾继续（取值组至少 2 字，匹配恒非空）
            pos = (m.start() + 1) if self_ref else m.end()
            continue
        yield m, vt
        pos = m.end()


def _qualify_key(value_text: str, obj_hints, context: str) -> str:
    """给「不同对象 / 不同部位」的取值加限定前缀，避免合法差异被判成冲突。

    限定词取自规则的 ``object_hint``，在**匹配所在的文本窗口**里找（不是只在
    压形后的取值里找 —— 对象词通常出现在主题词**之前**的标题部分，如
    「屋面及防水工程保修期」，压形后只剩「保修期5年」根本不含对象词）。
    命中则前缀化。找不到时原样返回 —— 宁可不区分对象（可能误报一次 CON-01），
    也不凭空编对象名。

    ⚠️ 限定词**已经在取值里**时不再前缀化：否则「管理人员34人」会变成
    「管理管理人员34人」（实测复现）—— 取值本身已携带对象词时，再加前缀
    只是重复信息，还会把两个同对象同数值的取值拆成不同 key。
    """
    if not obj_hints:
        return value_text
    ctx = str(context or "")
    for hint in obj_hints:
        if hint and hint not in value_text and hint in ctx:
            return f"{hint}{value_text}"
    return value_text


def _row_text(row) -> tuple[str, str]:
    """取事实行的 (title, content)。兼容 dict 与 3/4/5 元组行。"""
    if isinstance(row, dict):
        return str(row.get("title") or ""), str(row.get("content") or "")
    try:
        return str(row[1] or ""), str(row[2] or "")
    except (TypeError, IndexError):
        return "", ""


def _row_confidence(row):
    """取事实行置信度（4/5 元组第 3 项 / dict 的 confidence / 脏值 → None）。"""
    try:
        if isinstance(row, dict):
            raw = row.get("confidence")
        else:
            raw = row[3] if len(row) >= 4 else None
        return float(raw) if raw is not None else None
    except (TypeError, ValueError, IndexError):
        return None


def _row_chapter(row) -> str:
    """取事实行九大章节 key（5 元组第 4 项 / dict 的 chapter）。"""
    try:
        if isinstance(row, dict):
            return str(row.get("chapter") or "")
        return str(row[4] or "") if len(row) >= 5 else ""
    except (TypeError, IndexError):
        return ""


def _row_source_ref(row) -> str:
    """取事实行来源引用（dict 的 source_ref；元组行无此列 → 空串）。

    ⚠️ 历史事故：本函数体曾被错插到 ``audit_call_matrix`` 末尾，成为一段
    **不可达**代码，而这里只剩 docstring —— 于是全仓每个事实的 ``source_ref``
    都静默返回 ``None``（溯源引用字段整体失效，且零日志零报错）。新增行时务必
    确认落在本函数体内。
    """
    try:
        return str(row.get("source_ref") or "") if isinstance(row, dict) else ""
    except (TypeError, ValueError):
        return ""
def iter_consistency_values(facts_rows) -> list:
    """从全局事实行抽取全部一致性主题取值（不截断、不去重）。

    返回元素：``{key, name, value, value_text, chapter, title, confidence,
    source_ref, raw_len}``。

    - ``key`` 是数据字典键名（含对象前缀，见 :func:`_qualify_key`）；
    - ``value_text`` 是压形后的判定 key（``<name><value><unit>``，去空格）；
    - 抽取发生在**原始 content** 上，不受 ``per_fact`` 截断影响 —— 这是
      「单条事实超 300 字被截断」不再丢权威值的结构性保证；
    - 一个事实行可命中多个主题（如「工期」+「计划开工日期」），逐条产出。

    脏输入 / 单行异常一律返回已收集结果，绝不抛出。
    """
    out: list = []
    for row in facts_rows or []:
        title, content = _row_text(row)
        text = _prep(f"{title} {content}").strip()
        if not text:
            continue
        conf = _row_confidence(row)
        if conf is not None and conf < LOW_CONFIDENCE_FOR_DICT:
            continue  # 低置信度事实不充当权威值（避免把待裁决值当确定值）
        chapter = _row_chapter(row)
        source_ref = _row_source_ref(row)
        try:
            for _key, name_pattern, rx, obj_hints in CONSISTENCY_KEY_RULES:
                for m, value_text in _iter_value_matches(
                        text, rx, name_value_stopwords=_stopwords_for(_key)):
                    # 对象限定词只在**匹配附近**找（±40 字窗口）。⚠️ 这里绝不能传整行
                    # 文本：一条事实里对象词可能出现在与取值毫无关系的段落中，全篇扫描
                    # 会把无关词误当前缀（与 :func:`build_cross_section_digest` 同口径）。
                    win = text[max(0, m.start() - 40):m.start() + max(len(value_text), 12)]
                    out.append({
                        "key": _qualify_key(value_text, obj_hints, win),
                        "rule_key": _key,
                        "name": m.group("name") or "",
                        "value": value_text,
                        "value_text": value_text,
                        "chapter": chapter,
                        "title": title,
                        "confidence": conf,
                        "source_ref": source_ref,
                        "raw_len": len(content),
                    })
        except Exception:
            continue  # 单行失败不影响其它行
    return out


def build_global_data_dictionary(facts_rows) -> dict:
    """构建**全局数据字典**：每个前后一致字段只有一个权威值。

    这是「前后数据一致」的唯一权威源。规则：

    - 权威值**只**来自 ``global_facts``（绝不从正文反推 —— 否则「后面章节
      跟着前面写错的内容错」的缺陷无法避免）；
    - 同一 ``value_text`` 命中多条事实时取**置信度最高**的一条（同置信度取
      ``raw_len`` 最长者 —— 表述越完整越可能是设计文件原文口径）；
    - 同一 ``key`` 出现多个不同 ``value_text`` 时保留最权威的一条，其余写进
      ``conflicting`` —— 供用户 / 审核显式裁决，**不静默丢弃**（生产实证：
      「保修期」合法存在 5 个不同对象取值，静默取一会把另外 4 个合法值
      当错误）。
    - 字典里**没有**的字段表示「事实库里没有权威值」→ 不注入该行，让模型
      按模糊生成规则写条件式表述，**绝不留【待补充】**、**绝不编造**。

    Returns:
        ``{key: {name, value, value_text, chapter, source_title, source_ref,
        confidence, conflicting}}``；无事实 → 空 dict。
    """
    dict_: dict = {}
    for hit in iter_consistency_values(facts_rows):
        conf = hit["confidence"]
        rank = (conf if conf is not None else -1.0, hit["raw_len"])
        cur = dict_.get(hit["key"])
        if cur is None:
            dict_[hit["key"]] = {
                "name": hit["name"],
                "rule_key": hit["rule_key"],
                "value": hit["value"],
                "value_text": hit["value_text"],
                "chapter": hit["chapter"],
                "source_title": hit["title"],
                "source_ref": hit["source_ref"],
                "confidence": conf,
                "conflicting": [],
                "_rank": rank,
            }
        else:
            if cur["value_text"] != hit["value_text"] and rank > cur["_rank"]:
                # 更权威的一条整体替换，并把旧值收进候选列表
                older = cur["value_text"]
                new_conflicting = [v for v in cur["conflicting"]
                                   if v not in (hit["value_text"], older)]
                if older not in new_conflicting:
                    new_conflicting.append(older)
                dict_[hit["key"]] = {
                    "name": hit["name"],
                    "rule_key": hit["rule_key"],
                    "value": hit["value"],
                    "value_text": hit["value_text"],
                    "chapter": hit["chapter"],
                    "source_title": hit["title"],
                    "source_ref": hit["source_ref"],
                    "confidence": conf,
                    "conflicting": new_conflicting,
                    "_rank": rank,
                }
            elif cur["value_text"] != hit["value_text"]:
                if hit["value_text"] not in cur["conflicting"]:
                    cur["conflicting"].append(hit["value_text"])
    for entry in dict_.values():
        entry.pop("_rank", None)
    return dict_


#: 提示词里明确「事实库无权威值 → 不得编造」的收尾说明。
DICTIONARY_ABSENT_HINT = (
    "说明：上方未列出的字段在「全局事实」中没有权威值。涉及这些字段时，"
    "按「模糊生成规则」写成完整的技术表述（条件式 / 范围式 / 以设计文件为准），"
    "并严禁编造具体数字或日期，严禁出现【待补充】、××、待定 等占位标记。"
)


def render_data_dictionary_block(data_dict, *, chapter_key: str = "",
                                 exclude_keys=(),
                                 max_chars: int = DATA_DICT_MAX_CHARS) -> str:
    """渲染「全局数据字典」注入块（提示词段）。

    - 每个字段**只有一个权威值**，并注明来源事实（可追溯）；
    - 明确禁止各章自行编造 / 出现不同取值；
    - 有候选冲突时列出冲突候选并明确「以本表权威值为准」；
    - 字典为空时返回空串（调用方不注入该段 → 提示词与引入前逐字一致）。

    Args:
        data_dict: :func:`build_global_data_dictionary` 的产物。
        chapter_key: 当前章节九大章节 key；命中时优先列出该章相关条目
            （不隐藏其余条目，只影响顺序）。
        exclude_keys: 排除的键名（提示词已有同义段时去重，例如【方案名称】
            已下发则排除「项目全称」）。
        max_chars: 输出字符上限。
    """
    if not data_dict:
        return ""
    excluded = set(exclude_keys or ())
    items = [(k, v) for k, v in data_dict.items() if k not in excluded]
    if not items:
        return ""
    items.sort(key=lambda kv: 0 if kv[1].get("chapter") == chapter_key else 1)
    lines = [
        "【全局数据字典（跨章节唯一权威取值，各章必须一致引用）】：",
        "下列字段是全方案**前后必须一致**的关键数据，取值来自「全局事实」中"
        "已确认的事实。写本章涉及其中任一字段时，**必须逐字引用本表取值**，"
        "不得自行换算、改写或编造其它数字；与已生成章节出现不一致时以本表为准。",
    ]
    for _k, v in items:
        conf = v.get("confidence")
        suffix = f"（来源：{v.get('source_title') or '全局事实'}"
        if conf is not None:
            suffix += f"，置信度 {conf:.2f}"
        suffix += "）"
        lines.append(f"- {v.get('name')} = {v.get('value')}　{suffix}")
        if v.get("conflicting"):
            lines.append(
                "  · 该主题在事实库中另有候选取值："
                + "、".join(v["conflicting"][:4])
                + "。以本表权威值为准；如需改口径，请在「全局事实」中"
                  "完成裁决后重新生成，不要在本章自行取值。")
    lines.append(DICTIONARY_ABSENT_HINT)
    block = "\n".join(lines)
    return block[:max_chars]


# ---------------------------------------------------------------------------
# 二、必保留事实集合（防止预筛裁掉权威值）
# ---------------------------------------------------------------------------
def consistency_fact_titles(facts_rows) -> set:
    """返回「承载一致性主题权威值」的事实标题集合。

    调用方在逐章相关性预筛后必须把命中本集合的事实**无条件补回** ——
    这些事实是跨章节一致性的唯一依据，被裁掉后模型只能自己编数。

    判据：整行文本命中任一主题正则即保留其标题。
    """
    titles: set = set()
    for row in facts_rows or []:
        title, content = _row_text(row)
        text = _prep(f"{title} {content}")
        if not text.strip():
            continue
        for _key, _name, rx, _obj in CONSISTENCY_KEY_RULES:
            if rx.search(text):
                if title:
                    titles.add(title)
                break
    return titles


def must_keep_consistency_rows(facts_rows, keep_titles: set) -> list:
    """从事实行里取出「必须保留」的那部分（按标题命中）。"""
    if not keep_titles:
        return []
    out = []
    for row in facts_rows or []:
        title, _content = _row_text(row)
        if title in keep_titles:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# 三、章节调用痕迹（可追溯）
# ---------------------------------------------------------------------------
def section_data_trace(leaf, *, facts_rows, keep_titles,
                       prefiltered_rows=None) -> dict:
    """产出单章节的**数据调用痕迹**，落 ``last_generation_report``。

    回答「本章调用了哪些字段、哪些字段在库里但没被读」：

    - ``facts_total``：库里事实条数；
    - ``prefilter_kept`` / ``prefilter_dropped``：语义预筛保留 / 裁掉的条数
      （裁掉率是「调用不完整」的直接度量，生产实证 60%~95%）；
    - ``facts_injected``：合并必保留后的实际注入条数
      （= :func:`merge_injected_rows` 的结果条数，与提示词里实际下发的
      事实严格一致）；
    - ``consistency_topics``：本章可用的一致性主题（name → 权威值），即
      本章**应当**引用数据字典的字段清单；
    - ``consistency_forced``：本应保留但被预筛裁掉、由本模块强制补回的事实
      标题（= 缺口暴露的直接证据）；
    - ``required_fields`` 由调用方合并（其单一事实源在
      ``scheme_classification.required_fields_for_chapter``，本模块不复制）。

    返回结构对上游异常完全不敏感：任何缺字段一律回退空值。
    """
    leaf = leaf or {}
    rows = list(facts_rows or [])
    keep = set(keep_titles or ())
    prefiltered = list(prefiltered_rows or [])
    pref_titles = {_row_text(r)[0] for r in prefiltered}
    injected_rows = merge_injected_rows(prefiltered, rows, keep)
    forced = [t for t in keep if t not in pref_titles]

    topics: dict = {}
    for hit in iter_consistency_values(rows):
        topics.setdefault(hit["name"], []).append(hit["value"])

    return {
        "section_id": str(leaf.get("id") or ""),
        "section_title": str(leaf.get("title") or ""),
        "chapter_key": str(leaf.get("chapter_key") or ""),
        "facts_total": len(rows),
        "prefilter_kept": len(prefiltered),
        "prefilter_dropped": max(len(rows) - len(prefiltered), 0),
        "facts_injected": len(injected_rows),
        "consistency_topics": {k: v[:4] for k, v in topics.items()},
        "consistency_forced": forced,
    }


# ---------------------------------------------------------------------------
# 四、跨章节关键数据摘要（传入已生成章节的关键取值）
# ---------------------------------------------------------------------------
def build_cross_section_digest(existing_contents, *,
                               max_chars: int = DIGEST_MAX_CHARS,
                               max_items: int = DIGEST_MAX_ITEMS) -> str:
    """把「已生成章节」的关键取值汇成摘要，注入当前章节提示词。

    - 输入 ``{section_id: content}`` 或 ``{section_id: 章节dict}``；
    - 用与预检 CON-01 同一批主题正则抽取取值，按章节名去重；
    - 明确告知模型：已生成章节的取值**只是参考**，与本表不一致时以
      【全局数据字典】为准 —— 避免「前面章节写错了后面跟着错」。
    """
    items: list = []
    seen: set = set()
    for sec_id, val in (existing_contents or {}).items():
        if isinstance(val, dict):
            title = str(val.get("title") or sec_id or "")[:24]
            content = str(val.get("content") or "")
        else:
            title, content = "", str(val or "")
        if not content.strip():
            continue
        clean = _prep(content)
        for _key, _name_pattern, rx, obj_hints in CONSISTENCY_KEY_RULES:
            for m, vt in _iter_value_matches(
                    clean, rx, name_value_stopwords=_stopwords_for(_key)):
                # 对象限定词只在**匹配附近**找（±40 字窗口）：prose 里对象词
                # 通常与取值同句，全篇扫描会把无关段落里的词误当前缀。
                win = clean[max(0, m.start() - 40):m.start() + max(len(vt), 12)]
                vt = _qualify_key(vt, obj_hints, win)
                sig = (title, vt)
                if sig in seen:
                    continue
                seen.add(sig)
                items.append(f"- 【{title}】{vt}")
                if len(items) >= max_items:
                    break
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break
    if not items:
        return ""
    lines = [
        "【已生成章节的关键取值参考（仅供行文衔接参考）】：",
        "以下取值来自本方案**已生成**章节，它们本身可能写错："
        "**一切取值以【全局数据字典】为唯一权威**，本段只用于保证行文衔接"
        "与指代一致，不要沿用与之冲突的数值。",
    ]
    lines.extend(items)
    return "\n".join(lines)[:max_chars]


def chapter_facts_for_key(chapter_key: str, facts_rows) -> list:
    """按九大章节 key 取本章归属事实。

    与 ``facts_builder._filter_facts_rows`` 的差异：这里**只做章节归属匹配**，
    不做语义相关性打分。调用方应把它与语义预筛结果**取并集**，才能保证
    「既相关又不漏」（仅用预筛会裁掉 60%~95% 的事实）。
    """
    return [r for r in (facts_rows or []) if _row_chapter(r) == chapter_key]


def merge_injected_rows(prefiltered, facts_rows, keep_titles) -> list:
    """合并「语义预筛结果 + 章节归属 + 一致性必保留」，返回注入行。

    这是「完整注入」的唯一出口，保证三条来源都被覆盖：

    1. ``prefiltered``：语义相关性预筛（facts_builder 现有链路）；
    2. 章节归属：:func:`chapter_facts_for_key` 的结果（调用方算好后并入
       ``prefiltered``）；
    3. ``keep_titles``：承载一致性权威值的事实 —— **无条件保留**，
       即使语义预筛判它们「不相关」也不裁。

    去重按 (group_title, title) 键，预筛顺序优先（相关性的顺序有意义）。
    """
    def _key_of(row):
        title, _content = _row_text(row)
        if isinstance(row, dict):
            group = str(row.get("group_title") or "")
        elif isinstance(row, (list, tuple)) and len(row) >= 1:
            group = str(row[0] or "")
        else:
            group = ""
        return (group, title)

    keep_titles = set(keep_titles or ())
    out, seen = [], set()
    for row in list(prefiltered or []):
        k = _key_of(row)
        if k in seen:
            continue
        seen.add(k)
        out.append(row)
    for row in list(facts_rows or []):
        title, _c = _row_text(row)
        if title not in keep_titles:
            continue  # 只补回「被预筛裁掉的必保留事实」（已在预筛里的由去重跳过）
        k = _key_of(row)
        if k in seen:
            continue
        seen.add(k)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 六、生成后自检：跨章节取值一致性（与预检 CON-01 同源）
# ---------------------------------------------------------------------------
def cross_section_value_findings(sections, *, new_section_id: str = "",
                                 limit: int = 3) -> list:
    """跨章节数值一致性自检 —— **与预检 CON-01 同一判据实现**。

    直接调用 ``preflight_engine.numeric_consistency_findings``（预检唯一
    实现），**不复制主题正则与单位归一**；再按本章标题过滤，只保留
    「冲突证据里包含本章」的结论。

    ⚠️ 判据同源是硬约束（TRC-01、CON-06 已收敛过同一模式）：若在此重抄
    正则，「生成侧自检全绿、预检照报 CON-01 high」必然复现。

    全部 fail-soft：脏输入 / 异常返回空结论，绝不阻断落库。
    """
    try:
        from app.services.preflight_engine import numeric_consistency_findings
    except Exception:
        return []
    try:
        all_findings = numeric_consistency_findings(list(sections or []), limit=0)
    except Exception:
        return []
    new_title = ""
    for s in sections or []:
        if str(s.get("id") or "") == str(new_section_id or ""):
            new_title = str(s.get("title") or "")
            break
    if not new_title:
        return []
    out = []
    for f in all_findings:
        if new_title not in " ".join(f.get("evidence") or []):
            continue
        nf = dict(f)
        nf["source"] = "selfcheck"
        nf["detail"] = (str(f.get("detail", ""))
                          + f"（本章「{new_title}」的取值需与全局事实一致）")
        out.append(nf)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 七、方案级字段完整性对照表（缺失清单 / 影响分析）
# ---------------------------------------------------------------------------
def audit_call_matrix(*, extraction_items, facts_rows, sections,
                      chapter_fields=None, generated_report=None):
    """产出**字段完整性对照表 / 调用完整性对照表 / 缺失清单 / 影响分析**。

    四个视图（全部为加法式新报告，不改既有计数口径）：

    - ``extraction_inventory``：提取项目字段清单（item_id / label / required /
      状态 / 内容长度），标注「已提取 / 未提取 / 内容为空」；
    - ``facts_inventory``：全局事实清单（按九大章节分组计数 + 置信度分布 +
      一致性主题覆盖 / 缺失）；
    - ``call_matrix``：提取项 → 正文的调用对照，逐项标注 ``state``：
      ``covered``（正文提到该提取项标签或 id）/ ``missing``（库里没有内容）/
      ``read_not_reflected``（已提取但正文未体现 —— 调用不完整或注入被截断）；
    - ``gaps``：缺失清单（按严重度排序）+ 每项的影响分析文字。

    零 AI、零 IO、fail-soft；任何一段异常都不影响其它段。
    """
    extraction_items = [x for x in (extraction_items or []) if isinstance(x, dict)]
    facts_rows = list(facts_rows or [])
    sections = [x for x in (sections or []) if isinstance(x, dict)]
    chapter_fields = dict(chapter_fields or {})

    extraction_inventory = []
    for it in extraction_items:
        content = str(it.get("content") or it.get("content_md") or "")
        extraction_inventory.append({
            "item_id": str(it.get("item_id") or ""),
            "label": str(it.get("label") or ""),
            "required": bool(it.get("required")),
            "status": str(it.get("status") or ""),
            "content_len": len(content),
            "is_empty": len(content.strip()) == 0,
            "output_type": str(it.get("output_type") or ""),
        })

    by_chapter: dict = {}
    confs = []
    for r in facts_rows:
        ch = _row_chapter(r) or "(未分类)"
        by_chapter[ch] = by_chapter.get(ch, 0) + 1
        c = _row_confidence(r)
        if c is not None:
            confs.append(c)
    values = iter_consistency_values(facts_rows)
    # ⚠️ 覆盖度必须按**规则键**聚合，不能按单个主题词：一个规则键的替代式里
    # 有多个同义词（"总工期|工期"、"缺陷责任期|保修期|质量保证期"），只要
    # 抓到其中一个词就算该主题已覆盖。按词统计会把 8 个规则算成 27 个"主题"，
    # 实测在 127 条事实上 missing 恒为 20 项、gaps 冲到 51 条 —— 全是同义词
    # 噪声，审计表失去判据力。
    covered_rule_keys = {h.get("rule_key") for h in values if h.get("rule_key")}
    consistency_covered = [k for k, _p, _r, _o in CONSISTENCY_KEY_RULES
                           if k in covered_rule_keys]
    consistency_missing = [k for k, _p, _r, _o in CONSISTENCY_KEY_RULES
                           if k not in covered_rule_keys]
    # 自检：抓到的主题词必须都在规则表替代式里，否则就是替代式漂移
    # （例如某处把 name 组写成了表里没有的词，覆盖度会静默漏报）。
    try:
        _unknown = {h.get("name") for h in values if h.get("name")} - _topic_words()
        if _unknown:
            logger.warning("consistency topic drift: %s", sorted(_unknown))
    except Exception:
        pass

    facts_inventory = {
        "total": len(facts_rows),
        "by_chapter": by_chapter,
        "confidence_min": min(confs) if confs else None,
        "confidence_avg": round(sum(confs) / len(confs), 3) if confs else None,
        "confidence_below_0_6": sum(1 for c in confs if c < 0.6),
        "consistency_topics_covered": [f"{k}（{_RULE_ALIASES[k]}）" for k in consistency_covered],
        "consistency_topics_missing": [f"{k}（{_RULE_ALIASES[k]}）" for k in consistency_missing],
    }

    sec_texts = [str(s.get("content") or "") for s in sections]
    joined = "\n".join(sec_texts)

    call_matrix = []
    for it in extraction_items:
        label = str(it.get("label") or "")
        item_id = str(it.get("item_id") or "")
        content = str(it.get("content") or it.get("content_md") or "")
        if not content.strip():
            state = "missing"          # 库里没有 → 章节无法体现
        elif (label and label in joined) or (item_id and item_id in joined):
            state = "covered"
        else:
            state = "read_not_reflected"  # 已提取但正文未体现
        call_matrix.append({
            "item_id": item_id, "label": label,
            "state": state, "required": bool(it.get("required")),
        })

    gaps = []
    for entry in call_matrix:
        if entry["state"] == "missing" and entry["required"]:
            gaps.append({
                "kind": "extraction_missing", "severity": "high",
                "field": entry["label"], "item_id": entry["item_id"],
                "impact": "必填提取项无内容：相关章节只能按模糊规则成文，"
                          "具体参数无法落到正文",
            })
        elif entry["state"] == "read_not_reflected":
            gaps.append({
                "kind": "read_not_reflected", "severity": "medium",
                "field": entry["label"], "item_id": entry["item_id"],
                "impact": "已提取但正文未体现：调用不完整或注入被截断，"
                          "需核查该章节提示词是否被上下文预算裁掉",
            })
    for topic in facts_inventory["consistency_topics_missing"]:
        gaps.append({
            "kind": "consistency_value_missing", "severity": "high",
            "field": topic, "item_id": "",
            "impact": f"事实库缺少「{topic}」权威值：各章 AI 只能自行编造，"
                      "这是跨章节数据不一致的主要成因（生产实证：工期在 4 章"
                      "出现 4 种口径）",
        })

    for key, fields in chapter_fields.items():
        if not fields:
            continue
        hits = sum(1 for t in sec_texts for f in fields if f and f in t)
        tot = len(sec_texts) * len(fields)
        if tot and hits / tot < 0.5:
            gaps.append({
                "kind": "chapter_field_undercovered", "severity": "medium",
                "field": f"章节{key}必含要素", "item_id": "",
                "impact": f"九大章节「{key}」必含要素仅 {hits}/{tot} 命中，"
                          "章节要素覆盖不足",
            })

    gaps.sort(key=lambda g: (0 if g["severity"] == "high" else 1, g["field"]))

    return {
        "extraction_inventory": extraction_inventory,
        "facts_inventory": facts_inventory,
        "call_matrix": call_matrix,
        "gaps": gaps,
        "generated_report": dict(generated_report or {}),
    }
