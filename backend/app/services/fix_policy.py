"""修复策略判定：建议权 / 执行权分离的禁改白名单（R179 Phase C，2026-11-06）。

职责边界
--------
本模块只回答一个问题：**这条缺陷的修复建议能不能被「一键执行」直接写库**。
- ``free``：可由执行路径（``POST /defects/{id}/apply``）写入；
- ``protected``：禁止自动执行，必须由人走 ``POST /defects/{id}/decision``。

为什么需要白名单
----------------
安全数值、计算书结论、审批主体、强制性条文四类内容一旦被模型改写，
错误不会在生成阶段暴露，而会在评审/施工阶段放大。因此判定必须**失败即保护**
（fail-safe）：只要命中任一白名单类别，就按 ``protected`` 处理，宁可多拦。

判定依据
--------
1. 规则编号（preflight/一致性扫描的 rule_id）——确定性、可审计；
2. 文本关键词——覆盖手工录入缺陷（无 rule_id）的场景。

新增/删除任一白名单条目都会改变执行权，故测试对四个类别逐一钉住。
"""

from __future__ import annotations

#: 四类禁改白名单。顺序即命中优先级（同一缺陷可命中多类，按此顺序记录）。
#: ``rule_ids`` 为确定性判据；``keywords`` 为手工缺陷的兜底判据。
PROTECTED_CATEGORIES: dict[str, dict] = {
    "safety_value": {
        "label": "安全数值",
        "rule_ids": ("SAF-08", "CON-01", "CON-07"),
        "keywords": ("安全系数", "安全数值", "临界值", "荷载取值", "埋深", "支护参数"),
    },
    "calculation_conclusion": {
        "label": "计算书结论",
        "rule_ids": ("CMP-09", "TRC-01", "TRC-02"),
        "keywords": ("计算书", "验算", "计算结果", "计算结论", "配筋", "受力结论"),
    },
    "approval_subject": {
        "label": "审批主体",
        "rule_ids": ("DLV-09",),
        "keywords": ("审批主体", "审批人", "签字", "盖章", "代签", "技术负责人", "总监理工程师"),
    },
    "mandatory_clause": {
        "label": "强制性条文",
        "rule_ids": ("STD-02",),
        "keywords": ("强制性条文", "强制性标准", "全文强制", "工程建设规范"),
    },
}


def classify_fix_policy(
    *,
    rule_id: str = "",
    dimension: str = "",
    title: str = "",
    detail: str = "",
    suggestion: str = "",
) -> dict:
    """返回 ``{"policy": "free"|"protected", "categories": [...], "reason": str}``。

    ``dimension`` 仅用于证据展示，不单独触发保护 —— 维度过宽（如 safety）
    会把普通措施类缺陷全部拦死，反而让用户绕过账本。
    """
    haystack = f"{title} {detail} {suggestion}"
    hits: list[str] = []
    for key, spec in PROTECTED_CATEGORIES.items():
        if rule_id and rule_id in spec["rule_ids"]:
            hits.append(key)
            continue
        if any(kw in haystack for kw in spec["keywords"]):
            hits.append(key)
    if hits:
        labels = "、".join(PROTECTED_CATEGORIES[k]["label"] for k in hits)
        return {
            "policy": "protected",
            "categories": hits,
            "reason": f"命中禁改白名单：{labels}",
        }
    return {"policy": "free", "categories": [], "reason": "未命中禁改白名单"}
