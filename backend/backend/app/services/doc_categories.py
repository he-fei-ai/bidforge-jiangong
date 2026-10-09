"""项目资料文档分类（单一事实源）。

背景（2026-09-25 技术债收敛）：分类口径此前散落三处、彼此漂移——

  1. ``routers/global_facts.py::_DOC_CATEGORY_KEYWORDS``
     —— 自动分类关键词 + ``/documents/category-options`` 下拉选项；
  2. ``routers/bid_analysis.py::_combine_doc_texts`` 内的
     ``CATEGORY_PRIORITY`` —— AI 提取合并多份文档时的「资料优先级」；
  3. 前端 ``components/DocumentParseList.tsx::CATEGORY_COLOR_MAP`` —— 标签配色。

其中第 1 处有 9 类，第 2 处只维护了 6 类（漏「资质材料 / 人员资料 /
财务资料 / 业绩证明」）——这 4 类命中后落到 ``99``，被静默排到所有文档之后，
即「用户明明把资料归了类，AI 提取却几乎读不到」，且三处新增分类时极易漏改
（与 AGENTS.md §4.4「下游全部直接查表、口径必须一致」冲突）。

现统一收口到本模块：分类清单、自动分类规则、提取优先级全部在此维护，
后端两处调用方只读不写。前端配色表保留本地副本但带兜底色，新增分类不会破。

向后兼容：分类集合与既有顺序**完全不变**（新增分类需同步评估前端下拉与
配色），``extract_priority`` 对既有 6 类的取值与旧 ``CATEGORY_PRIORITY`` 逐字节一致。
"""
from __future__ import annotations

#: 有序分类清单：顺序即①自动分类规则优先级 ②``/documents/category-options``
#: 下拉顺序 ③前端下拉顺序。首个命中即返回（见 ``auto_classify_document``）。
DOC_CATEGORIES: tuple[str, ...] = (
    "招标文件",
    "合同文件",
    "设计文件",
    "地勘报告",
    "报价清单",
    "资质材料",
    "人员资料",
    "财务资料",
    "业绩证明",
)

#: 兜底分类：规则全不命中时返回。
OTHER_CATEGORY = "其他"

#: 全部分类（含兜底）：``/documents/category-options`` 的完整选项集合。
ALL_DOC_CATEGORIES: tuple[str, ...] = DOC_CATEGORIES + (OTHER_CATEGORY,)

#: 自动分类规则：``(分类, 关键词组)``，**顺序即优先级**，第一个分类中命中任一
#: 关键词即返回该分类。
#:
#: ✅ BUG 修复（2026-09-25，分类错误）：旧规则中「设计文件」含过宽的「设计」
#: 单字关键词，且排在「招标文件」之后——于是**施工组织设计**（本平台的头号
#: 核心资料：专项方案通常就脱胎于施工组织设计）被归到「设计文件」，
#: 在 AI 提取合并时按优先级 2 排在「设计图纸 / 设计说明」之后，
#: 而它本应是最优先读取的资料。现把「施工组织设计 / 施工组织」明确归入
#: 「招标文件」（施工组织设计在招投标实务中属投标文件的核心组成部分），
#: 使其以优先级 0 参与提取。
AUTO_CLASSIFY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("招标文件", ("招标", "投标", "标书", "标函", "招标公告", "招标邀请",
                  "投标须知", "施工组织设计", "施工组织")),
    ("合同文件", ("合同", "协议", "承包", "发包", "施工合", "采购合")),
    ("设计文件", ("设计", "图纸", "cad", "施工图", "方案设计", "初步设计",
                  "设计说明")),
    ("地勘报告", ("地勘", "勘察", "地质", "岩土", "土探", "钻孔")),
    ("报价清单", ("报价", "清单", "工程量", "预算", "造价", "工程量清",
                  "综合单价")),
    ("资质材料", ("资质", "执照", "许可", "证书", "认证", "体系",
                  "营业执照")),
    ("人员资料", ("人员", "项目经理", "技术负责人", "注册", "建造师",
                  "工程师")),
    ("财务资料", ("财务", "审计", "年报", "报表", "资产", "现金流")),
    ("业绩证明", ("业绩", "案例", "合同额", "竣工", "交付", "完工")),
)

#: AI 提取合并多份资料时的读取优先级（**越小越先读**）。
#:
#: 与旧 ``bid_analysis._combine_doc_texts.CATEGORY_PRIORITY`` 的取值**逐条对齐**
#: （招标文件 0 …… 报价清单 4、其他 5），补齐旧表缺失的 4 类，并把它们排在
#: 「其他」之后——避免把「资质/人员/财务/业绩」这类对方案正文关联度较低的资料
#: 提到未分类的重要资料之前。未知分类（历史脏数据 / 前端手填新值）返回
#: ``UNKNOWN_CATEGORY_PRIORITY``，落在最后。
EXTRACT_PRIORITY: dict[str, int] = {
    "招标文件": 0,
    "合同文件": 1,
    "设计文件": 2,
    "地勘报告": 3,
    "报价清单": 4,
    "其他": 5,
    "资质材料": 6,
    "人员资料": 7,
    "财务资料": 8,
    "业绩证明": 9,
}

#: 未登记分类的提取优先级（排在所有已知分类之后）。
UNKNOWN_CATEGORY_PRIORITY = 99


def auto_classify_document(file_name: str, file_type: str = "") -> str:
    """按文件名自动判断文档分类。

    匹配口径（与旧实现一致 + 修复过宽关键词）：
      - 仅按**文件名**（不含扩展名）做大小写不敏感的子串匹配；
      - 规则按 ``AUTO_CLASSIFY_RULES`` 顺序取首个命中；
      - 全部不命中 → 返回 ``OTHER_CATEGORY``。

    ``file_type`` 目前不参与判定，保留入参以兼容既有调用方签名。
    """
    fname_lower = (file_name or "").lower()
    for category, keywords in AUTO_CLASSIFY_RULES:
        for kw in keywords:
            if kw.lower() in fname_lower:
                return category
    return OTHER_CATEGORY


def extract_priority(category: str | None) -> int:
    """文档分类 → AI 提取合并优先级（越小越先读）。

    空值（历史行 ``doc_category DEFAULT ''`` / 未分类）**等价于「其他」**，
    与旧 ``_combine_doc_texts.CATEGORY_PRIORITY.get(cat, 99)`` 的口径一致
    （旧实现先 ``or "其他"`` 再查表）—— 保持该映射可避免存量未分类文档
    被静默挪到所有已分类文档之后。
    """
    cat = (category or "").strip() or OTHER_CATEGORY
    return EXTRACT_PRIORITY.get(cat, UNKNOWN_CATEGORY_PRIORITY)


def category_options() -> list[str]:
    """``/documents/category-options`` 返回的完整选项列表。"""
    return list(ALL_DOC_CATEGORIES)
