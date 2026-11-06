"""文种判断第三层：语气与编制主体约束（确定性规则，零 AI）。

依据
----
- 《危险性较大的分部分项工程安全管理规定》（住建部令第 37 号）
  · 第十条：施工单位应当在危大工程施工前组织工程技术人员编制专项施工方案；
  · 第十一条：专项施工方案应当由施工单位技术负责人审核签字、加盖单位公章，
    并由总监理工程师审查签字后方可实施；
  · 第十二条：对于超过一定规模的危大工程，施工单位应当组织召开专家论证会
    对专项施工方案进行论证，论证前应通过施工单位审核和总监理工程师审查。

设计约定
--------
- 本模块只做「文本里有没有禁止/必备表述」的确定性检查，不推断工程事实；
- 三态输出（yes / no / undetermined）：未提供文本 → undetermined，
  绝不把「没检查」显示成「没问题」；
- 规则只作提示与护栏，不自动改写文本，也不替代人工复核。
"""

from __future__ import annotations

import re

from app.services.scheme_classification import TriState

#: 第三层规则集。``mode``：
#:   forbidden —— 命中即「不通过」（禁止性表述）
#:   required  —— 未命中即「不通过」（必备表述）
#: ``applicable_oversize`` 为 True 时仅当超规模结论为 yes 才执行（论证前置）。
GENRE_TEXT_RULES: list[dict] = [
    {
        "rule_id": "GR-L3-001",
        "title": "禁止代签代编",
        "clause_ref": "住建部令第37号 第十条、第十一条",
        "mode": "forbidden",
        "patterns": ("代签", "代编", "代为编制", "代替编制", "代写"),
        "message": "方案编制/审核签字不得代签代编，须由施工单位技术负责人审核签字并加盖单位公章。",
    },
    {
        "rule_id": "GR-L3-002",
        "title": "禁止未经论证即实施",
        "clause_ref": "住建部令第37号 第十二条",
        "mode": "forbidden",
        "patterns": (
            r"未经[^。；\n]{0,6}论证[^。；\n]{0,8}(即|便|直接|擅自)[^。；\n]{0,4}(实施|施工|作业)",
            r"未经论证[^。；\n]{0,10}(实施|施工|作业)",
            r"未论证[^。；\n]{0,10}(即|直接|擅自)?(实施|施工)",
        ),
        "message": "超过一定规模的危大工程未经专家论证不得实施。",
    },
    {
        "rule_id": "GR-L3-003",
        "title": "编制主体须为施工单位技术负责人（项目总工）",
        "clause_ref": "住建部令第37号 第十条、第十一条",
        "mode": "required",
        "patterns": ("项目技术负责人", "项目总工", "技术负责人", "总工程师"),
        "message": "方案应由施工单位组织工程技术人员编制，并由施工单位技术负责人审核签字。",
    },
]


def _matched(rule: dict, text: str) -> list[str]:
    """返回规则命中的原文片段（正则/字面量统一处理）。"""
    found: list[str] = []
    for pat in rule.get("patterns") or ():
        try:
            m = re.search(pat, text)
        except re.error:
            # 字面量兜底：避免规则写坏导致整层失效
            if pat in text:
                found.append(pat)
            continue
        if m:
            found.append(m.group(0))
    return found


def evaluate_genre_text(text: str, oversize: str = TriState.UNDETERMINED.value) -> list[dict]:
    """对编制说明/方案文本执行第三层规则，逐条返回三态结论。

    Args:
        text: 待检查文本（编制说明、方案正文片段等）。为空时全部规则输出
            ``undetermined``。
        oversize: 第一层得到的超规模三态结论；仅 ``yes`` 时执行论证前置规则。

    Returns:
        规则记录列表，每项含
        ``rule_id / layer / title / conclusion / evidence / clause_ref``。
    """
    has_text = bool((text or "").strip())
    records: list[dict] = []

    for rule in GENRE_TEXT_RULES:
        hits = _matched(rule, text or "") if has_text else []
        if not has_text:
            conclusion = TriState.UNDETERMINED.value
            evidence = ["未提供可检查文本，无法判定"]
        elif rule["mode"] == "forbidden":
            conclusion = TriState.NO.value if hits else TriState.YES.value
            evidence = hits if hits else ["未发现禁止性表述"]
        else:
            conclusion = TriState.YES.value if hits else TriState.NO.value
            evidence = hits if hits else [rule["message"]]
        records.append(
            {
                "rule_id": rule["rule_id"],
                "layer": 3,
                "title": rule["title"],
                "conclusion": conclusion,
                "evidence": evidence,
                "clause_ref": rule["clause_ref"],
            }
        )

    # 论证前置：只对超规模方案适用；缺文本时仍输出 undetermined。
    if oversize == TriState.YES.value:
        if not has_text:
            conclusion = TriState.UNDETERMINED.value
            evidence = ["未提供可检查文本，无法判定是否写入专家论证前置条件"]
        elif "专家论证" in (text or ""):
            conclusion = TriState.YES.value
            evidence = ["文本已包含专家论证前置条件"]
        else:
            conclusion = TriState.NO.value
            evidence = ["超规模方案未在文本中写明专家论证前置条件"]
        records.append(
            {
                "rule_id": "GR-L3-004",
                "layer": 3,
                "title": "超规模方案须写明专家论证前置条件",
                "conclusion": conclusion,
                "evidence": evidence,
                "clause_ref": "住建部令第37号 第十二条",
            }
        )
    return records
