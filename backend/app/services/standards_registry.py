"""现行有效工程建设标准/规范库（专项方案编制依据）

目的
----
解决正文生成中"引用过期标准、编造标准编号"的问题：
- 为不同方案类型提供**现行有效**的编制依据清单，随提示词注入 AI；
- 提供**已废止/已被替代**标准清单，用于提示词禁止项与生成后扫描告警。

维护约定（重要）
---------------
1. 本库仅收录住房和城乡建设部、国家市场监督管理总局（国家标准委）公开发布
   且处于**现行有效**状态的标准，逐条标明编号与名称；未核实的条目不得入库。
2. 标准编号中的年份为**发布年号**（如 JGJ/T 46-2024），不得与实施日期混淆。
3. 全文强制性工程建设规范（GB 55xxx 系列）自 2022 年起陆续实施，其发布时
   公告明确"现行标准中有关规定与本规范不一致的，以本规范为准"，本库将其
   置于各类方案首位。
4. 每次标准更新须同步修改 ``STANDARD_DB_VERSION`` 与 ``STANDARD_DB_CHECKED_AT``，
   并在 ``ABOLISHED_STANDARDS`` 中登记被替代版本，避免正文仍引用旧编号。

数据来源：住房和城乡建设部公告、全国标准信息公共服务平台（std.samr.gov.cn）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 标准库版本与核对日期：标准更新后必须同步修改，便于运维比对
STANDARD_DB_VERSION = "2026.09"
STANDARD_DB_CHECKED_AT = "2026-09-13"


@dataclass(frozen=True)
class Standard:
    """一条现行有效标准。

    Attributes:
        code: 标准编号（含年号），如 ``GB 55023-2022``。
        name: 标准名称，如 ``施工脚手架通用规范``。
        mandatory: 是否为全文强制性工程建设规范。
    """

    code: str
    name: str
    mandatory: bool = False

    def cite(self) -> str:
        """返回正文引用格式：《名称》（编号）。"""
        flag = "【全文强制】" if self.mandatory else ""
        return f"- {flag}《{self.name}》（{self.code}）"


# ---------------------------------------------------------------------------
# 一、通用基础类：所有专项方案均应引用
# ---------------------------------------------------------------------------
BASE_STANDARDS: list[Standard] = [
    Standard("GB 55034-2022", "建筑与市政施工现场安全卫生与职业健康通用规范", True),
    Standard("GB 55032-2022", "建筑与市政工程施工质量控制通用规范", True),
    Standard("JGJ 59-2011", "建筑施工安全检查标准"),
    Standard("JGJ/T 46-2024", "建筑与市政工程施工现场临时用电安全技术标准"),
    Standard("GB 50720-2011", "建设工程施工现场消防安全技术规范"),
    Standard("GB 50300-2013", "建筑工程施工质量验收统一标准"),
    Standard("GB/T 50903-2013", "建设工程项目管理规范"),
]

# ---------------------------------------------------------------------------
# 二、专业类别：按方案类型/名称关键词命中
# ---------------------------------------------------------------------------
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "基坑": ("基坑", "土方", "围护", "支护", "降水", "排水", "支撑", "SMW", "型钢", "栈桥"),
    "模板": ("模板", "高大支模", "超限梁", "支撑排架", "支模"),
    "脚手架": ("脚手架", "吊篮", "高处作业", "操作平台", "防护棚", "临边"),
    "起重机械": ("塔吊", "塔式起重机", "起重", "吊装", "升降机", "人货梯", "机械安拆", "汽车吊"),
    "混凝土": ("混凝土", "大体积", "泵送", "缺陷修补", "灌浆"),
    "钢筋": ("钢筋", "套筒", "机械连接", "焊接"),
    "装配式": ("装配式", "PC", "预制", "构件"),
    "砌体二次结构": ("砌体", "二次结构", "隔墙", "构造柱"),
    "防水": ("防水", "防渗漏", "渗漏", "淋水", "屋面"),
    "装饰保温": ("装饰", "装修", "保温", "涂料", "门窗", "幕墙", "外立面"),
    "机电消防": ("机电", "消防", "给排水", "电气", "通风空调", "智能化", "防雷", "电梯"),
    "测量监测": ("测量", "监测", "变形", "沉降", "定位"),
    "绿色文明": ("绿色", "扬尘", "文明", "环保", "大气污染", "噪声"),
    "季节性施工": ("冬期", "雨季", "夏季", "防暑", "高温", "防台", "防汛"),
    "应急处置": ("应急", "救援", "预案", "事故"),
}

CATEGORY_STANDARDS: dict[str, list[Standard]] = {
    "基坑": [
        Standard("GB 55003-2021", "建筑与市政地基基础通用规范", True),
        Standard("JGJ 120-2012", "建筑基坑支护技术规程"),
        Standard("GB 50497-2019", "建筑基坑工程监测技术标准"),
        Standard("GB 50202-2018", "建筑地基基础工程施工质量验收标准"),
        Standard("JGJ 311-2013", "建筑深基坑工程施工安全技术规范"),
        Standard("GB 50007-2011", "建筑地基基础设计规范"),
        Standard("JGJ 111-2016", "建筑与市政降水工程技术规范"),
        Standard("JGJ 8-2016", "建筑变形测量规范"),
    ],
    "模板": [
        Standard("GB 55023-2022", "施工脚手架通用规范", True),
        Standard("GB 50666-2011", "混凝土结构工程施工规范"),
        Standard("GB 50204-2015", "混凝土结构工程施工质量验收规范"),
        Standard("JGJ 162-2008", "建筑施工模板安全技术规范"),
        Standard("GB 51210-2016", "建筑施工脚手架安全技术统一标准"),
        Standard("JGJ 300-2013", "建筑施工临时支撑结构技术规范"),
    ],
    "脚手架": [
        Standard("GB 55023-2022", "施工脚手架通用规范", True),
        Standard("JGJ 130-2011", "建筑施工扣件式钢管脚手架安全技术规范"),
        Standard("JGJ/T 231-2021", "建筑施工承插型盘扣式钢管脚手架安全技术标准"),
        Standard("JGJ 202-2010", "建筑施工工具式脚手架安全技术规范"),
        Standard("JGJ 80-2016", "建筑施工高处作业安全技术规范"),
        Standard("GB 51210-2016", "建筑施工脚手架安全技术统一标准"),
        Standard("GB 19155-2017", "高处作业吊篮"),
    ],
    "起重机械": [
        Standard("GB 5144-2024", "塔式起重机安全规程"),
        Standard("GB/T 5031-2019", "塔式起重机"),
        Standard("JGJ 196-2010", "建筑施工塔式起重机安装、使用、拆卸安全技术规程"),
        Standard("JGJ 215-2010", "建筑施工升降机安装、使用、拆卸安全技术规程"),
        Standard("JGJ 276-2012", "建筑施工起重吊装工程安全技术规范"),
        Standard("JGJ 33-2012", "建筑机械使用安全技术规程"),
        Standard("GB 6067.1-2010", "起重机械安全规程 第1部分：总则"),
    ],
    "混凝土": [
        Standard("GB 50666-2011", "混凝土结构工程施工规范"),
        Standard("GB 50204-2015", "混凝土结构工程施工质量验收规范"),
        Standard("GB 50496-2018", "大体积混凝土施工标准"),
        Standard("GB 50164-2011", "混凝土质量控制标准"),
        Standard("JGJ/T 10-2011", "混凝土泵送施工技术规程"),
        Standard("GB/T 50081-2019", "混凝土物理力学性能试验方法标准"),
    ],
    "钢筋": [
        Standard("GB 50204-2015", "混凝土结构工程施工质量验收规范"),
        Standard("JGJ 18-2012", "钢筋焊接及验收规程"),
        Standard("JGJ 107-2016", "钢筋机械连接技术规程"),
        Standard("GB 50010-2010", "混凝土结构设计规范（2015年版）"),
    ],
    "装配式": [
        Standard("JGJ 1-2014", "装配式混凝土结构技术规程"),
        Standard("JGJ 355-2015", "钢筋套筒灌浆连接应用技术规程"),
        Standard("GB 50204-2015", "混凝土结构工程施工质量验收规范"),
        Standard("GB/T 51231-2016", "装配式混凝土建筑技术标准"),
    ],
    "砌体二次结构": [
        Standard("GB 50203-2011", "砌体结构工程施工质量验收规范"),
        Standard("GB 50924-2014", "砌体结构工程施工规范"),
    ],
    "防水": [
        Standard("GB 55030-2022", "建筑与市政工程防水通用规范", True),
        Standard("GB 50208-2011", "地下防水工程质量验收规范"),
        Standard("GB 50207-2012", "屋面工程质量验收规范"),
        Standard("GB 50345-2012", "屋面工程技术规范"),
    ],
    "装饰保温": [
        Standard("GB 50210-2018", "建筑装饰装修工程质量验收标准"),
        Standard("GB 55015-2021", "建筑节能与可再生能源利用通用规范", True),
        Standard("GB 50411-2019", "建筑节能工程施工质量验收标准"),
        Standard("JGJ 144-2019", "外墙外保温工程技术标准"),
    ],
    "机电消防": [
        Standard("GB 55037-2022", "建筑防火通用规范", True),
        Standard("GB 55036-2022", "消防设施通用规范", True),
        Standard("GB 50303-2015", "建筑电气工程施工质量验收规范"),
        Standard("GB 50242-2002", "建筑给水排水及采暖工程施工质量验收规范"),
        Standard("GB 50243-2016", "通风与空调工程施工质量验收规范"),
    ],
    "测量监测": [
        Standard("GB 50026-2020", "工程测量标准"),
        Standard("JGJ 8-2016", "建筑变形测量规范"),
        Standard("GB 50497-2019", "建筑基坑工程监测技术标准"),
    ],
    "绿色文明": [
        Standard("GB/T 50905-2014", "建筑工程绿色施工规范"),
        Standard("GB/T 50640-2010", "建筑工程绿色施工评价标准"),
        Standard("JGJ 146-2013", "建设工程施工现场环境与卫生标准"),
    ],
    "季节性施工": [
        Standard("JGJ/T 104-2011", "建筑工程冬期施工规程"),
        Standard("GB/T 50905-2014", "建筑工程绿色施工规范"),
    ],
    "应急处置": [
        Standard("GB/T 29639-2020", "生产经营单位生产安全事故应急预案编制导则"),
        Standard("JGJ 59-2011", "建筑施工安全检查标准"),
    ],
}

# ---------------------------------------------------------------------------
# 三、法规与规范性文件（非标准，但为专项方案法定编制依据）
# ---------------------------------------------------------------------------
REGULATIONS: list[str] = [
    "《危险性较大的分部分项工程安全管理规定》（住房和城乡建设部令第37号，2019年修正）",
    "《关于实施〈危险性较大的分部分项工程安全管理规定〉有关问题的通知》（建办质〔2018〕31号）",
    "《建设工程安全生产管理条例》（国务院令第393号）",
    "《建设工程质量管理条例》（国务院令第279号，2019年修订）",
    "《建筑施工特种作业人员管理规定》（建质〔2008〕75号）",
]

# ---------------------------------------------------------------------------
# 四、已废止 / 已被替代标准（正文禁止引用；生成后扫描命中即告警）
# ---------------------------------------------------------------------------
ABOLISHED_STANDARDS: dict[str, str] = {
    "JGJ 46-2005": "已由 JGJ/T 46-2024《建筑与市政工程施工现场临时用电安全技术标准》替代（2025-01-01 实施）",
    "JGJ 59-99": "已由 JGJ 59-2011《建筑施工安全检查标准》替代",
    "JGJ 130-2001": "已由 JGJ 130-2011《建筑施工扣件式钢管脚手架安全技术规范》替代",
    "JGJ 231-2010": "已由 JGJ/T 231-2021《建筑施工承插型盘扣式钢管脚手架安全技术标准》替代",
    "GB 50202-2002": "已由 GB 50202-2018《建筑地基基础工程施工质量验收标准》替代",
    "GB 50203-2002": "已由 GB 50203-2011《砌体结构工程施工质量验收规范》替代",
    "GB 50204-2002": "已由 GB 50204-2015《混凝土结构工程施工质量验收规范》替代",
    "GB 50207-2002": "已由 GB 50207-2012《屋面工程质量验收规范》替代",
    "GB 50208-2002": "已由 GB 50208-2011《地下防水工程质量验收规范》替代",
    "GB 50205-2001": "已由 GB 50205-2020《钢结构工程施工质量验收标准》替代",
    "GB 50210-2001": "已由 GB 50210-2018《建筑装饰装修工程质量验收标准》替代",
    "GB 50026-2007": "已由 GB 50026-2020《工程测量标准》替代",
    "GB 50496-2009": "已由 GB 50496-2018《大体积混凝土施工标准》替代",
    "GB 50497-2009": "已由 GB 50497-2019《建筑基坑工程监测技术标准》替代",
    "GB 50411-2007": "已由 GB 50411-2019《建筑节能工程施工质量验收标准》替代",
    "JGJ 144-2004": "已由 JGJ 144-2019《外墙外保温工程技术标准》替代",
    "GB 50164-92": "已由 GB 50164-2011《混凝土质量控制标准》替代",
    "GB 5144-2006": "已由 GB 5144-2024《塔式起重机安全规程》替代",
    "GB 50500-2013": "已由 GB/T 50500-2024《建设工程工程量清单计价标准》替代（2025-09-01 实施）",
}


def match_categories(scheme_name: str = "", scheme_type: str = "",
                     section_title: str = "") -> list[str]:
    """按方案名称/类型/章节标题关键词命中专业类别。

    Args:
        scheme_name: 方案名称。
        scheme_type: 方案类型（如"深基坑""高支模"）。
        section_title: 当前章节标题，用于补充命中（如"脚手架搭设"）。

    Returns:
        命中的类别名列表（按 CATEGORY_KEYWORDS 定义顺序）。
    """
    haystack = f"{scheme_name} {scheme_type} {section_title}".upper()
    hits: list[str] = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw.upper() in haystack for kw in keywords):
            hits.append(category)
    return hits


def get_standards_text(scheme_name: str = "", scheme_type: str = "",
                       section_title: str = "", max_categories: int = 2,
                       include_regulations: bool = True) -> str:
    """生成提示词用的"编制依据（现行有效标准）"文本块。

    仅列出与本节主题高度相关的标准，避免清单过长稀释注意力。

    Args:
        scheme_name: 方案名称。
        scheme_type: 方案类型。
        section_title: 章节标题。
        max_categories: 最多纳入的专业类别数。
        include_regulations: 是否附法规文件。

    Returns:
        多行文本；无命中时仍返回通用基础标准清单。
    """
    lines: list[str] = ["【编制依据 · 现行有效标准参考清单】"]
    lines.append("（仅可从下列现行标准中引用；确需引用清单外标准时，必须同时给出标准编号与现行年号）")

    hits = match_categories(scheme_name, scheme_type, section_title)[:max_categories]
    seen_codes: set[str] = set()

    for category in hits:
        lines.append(f"· {category}类：")
        for std in CATEGORY_STANDARDS.get(category, []):
            if std.code in seen_codes:
                continue
            seen_codes.add(std.code)
            lines.append("  " + std.cite())

    lines.append("· 通用类：")
    for std in BASE_STANDARDS:
        if std.code in seen_codes:
            continue
        seen_codes.add(std.code)
        lines.append("  " + std.cite())

    if include_regulations:
        lines.append("· 法规依据：")
        lines.extend(f"  - {r}" for r in REGULATIONS)

    lines.append(
        "【禁止引用的已废止版本】"
        + "；".join(f"{k}（{v}）" for k, v in ABOLISHED_STANDARDS.items())
    )
    return "\n".join(lines)


#: 标准编号里常见的"近形连字符"（半角/全角/em/en dash/连字/减号）
_DASH_CHARS = "—－–‐‑−\u2010\u2011"
_WS_RE = re.compile(r"[\s\u3000]+")
_CODE_TOKEN_SPLIT_RE = re.compile(rf"[\s\u3000\-{_DASH_CHARS}]+")


def normalize_standard_code(code: str) -> str:
    """标准编号归一化：大写 + 统一连字符为 ``-`` + 去空白（含全角空格）。

    ✅ 新增（2026-09-18）：正文里 ``GB55034-2022`` / ``GB 50202—2002``（全角破折号）
    等写法与标准库里的规范写法（``GB 55034-2022``）语义相同但字符串不等 —— 旧实现
    用裸子串/等值比较，导致废止标准漏判（STD-01 红线失效）与在库标准误报。
    """
    if not code:
        return ""
    s = str(code).upper()
    for ch in _DASH_CHARS:
        s = s.replace(ch, "-")
    return _WS_RE.sub("", s)


def _code_regex(code: str) -> re.Pattern:
    """把标准编号编译为「空白/连字符任意（含全角）」的宽松正则。

    末尾加 ``(?!\\d)`` 防前缀误命中（如 ``GB 50164-92`` 不应命中 ``GB 50164-920``）。
    """
    tokens = [t for t in _CODE_TOKEN_SPLIT_RE.split(code.strip()) if t]
    body = r"[—\-－–\s]*".join(re.escape(t) for t in tokens)
    return re.compile(body + r"(?!\d)", re.IGNORECASE)


#: 预编译的废止标准匹配器（模块级缓存，避免每次扫描重复编译）
_ABOLISHED_PATTERNS: list[tuple[str, re.Pattern]] = [
    (code, _code_regex(code)) for code in ABOLISHED_STANDARDS]


def find_abolished_codes(text: str) -> list[str]:
    """扫描文本中出现的已废止标准编号，返回命中编号列表（供质量审计告警）。

    ✅ BUG 修复（2026-09-18）：改用归一化正则匹配，"GB50202-2002"（无空格）、
    "GB 50202—2002"（全角破折号）等写法不再漏判。与预检 STD-01 共用本函数，
    消除两处口径分叉。
    """
    if not text:
        return []
    return [code for code, pat in _ABOLISHED_PATTERNS if pat.search(text)]


def is_known_standard(code: str) -> bool:
    """判断标准编号是否收录于现行标准库（用于生成后引用校验）。

    ✅ BUG 修复（2026-09-18）：改为归一化后比较，正文写 ``GB55034-2022``
    （无空格）不再被误判为"未收录"（原实现会误报 STD-03）。
    """
    code_u = normalize_standard_code(code)
    if not code_u:
        return False
    if code_u in {normalize_standard_code(c) for c in ABOLISHED_STANDARDS}:
        return False
    pool = list(BASE_STANDARDS)
    for items in CATEGORY_STANDARDS.values():
        pool.extend(items)
    return any(normalize_standard_code(s.code) == code_u for s in pool)
