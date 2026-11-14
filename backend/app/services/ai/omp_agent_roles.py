"""Oh My Pi (omp) 三环节 Agent 角色定义、Prompt 模板与结构化校验器（阶段 3）。

本模块是「目录生成 / 正文生成 / 审核与修复」三大 Agent 的**单一事实源**：

* 角色：system prompt、工具白名单、唯一产物文件、验收契约；
* ``plan → execute → verify → fix`` 四段 Prompt 模板（含结构化计划的
  标题 / 步骤 / 文件 / 验收标准）；
* 结构化校验器：``check_json`` / ``verify_outline`` / ``verify_content_draft`` /
  ``verify_audit_fixes`` / ``scan_documents``。

⚠️ 分层约束：services 层禁止 import routers（``test_services_never_import_routers``）。
⚠️ 判据一律委托平台既有单一事实源（``content_utils.text_word_count`` /
   ``find_unclosed_fences`` / ``LEAF_MIN_WORD_BUDGET`` / ``WORD_OVER_RATIO``），
   不造第二份字数、围栏或档位判据。

⚠️ 与 omp 上游能力的对应关系见 ``docs/集成oh-my-pi_OMP_AGENT可行性报告_20261112.md``：
   ``--tools`` 白名单、RPC NDJSON、``maxFrameBytes=1MB``（故长文本一律走工作区文件）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.services.content_utils import (
    LEAF_MIN_WORD_BUDGET,
    WORD_BUDGET_TIERS,
    WORD_OVER_RATIO,
    WORD_UNDER_RATIO,
    find_unclosed_fences,
    text_word_count,
)

# ---------- 目录层级与数量约束（用户规格：L1~L4） ----------
MAX_OUTLINE_LEVEL = 4
#: key = 父节点层级，value = 其子节点数允许区间。
#: L2 3~5 个 / L3 3~5 个 / L4 4~6 个。
LEVEL_CHILD_RANGE: dict[int, tuple[int, int]] = {1: (3, 5), 2: (3, 5), 3: (4, 6)}
#: 单棵树节点总数上限，防止 Agent 无限展开撑爆上下文。
MAX_OUTLINE_NODES = 400

# ---------- 通用硬约束（对抗幻觉 / 越界 / 上下文膨胀） ----------
_FACT_RULE = (
    "【事实铁律】只能引用 global-facts.md 与 conflicts.md 中给出的数据和结论，"
    "严禁编造任何数字、单位、工期、规范编号、材料型号或人名；"
    "如需引用，先用 read/grep 回溯原文交叉验证。"
)
_SANDBOX_RULE = (
    "【工作区红线】可使用 bash 执行工作区内的构建/格式化/测试命令（仅限 --cwd 指向的隔离工作区），"
    "禁止访问网络，禁止读取或写入工作区以外的任何路径，禁止访问业务数据库，"
    "禁止读取 .env / 密钥 / 凭据文件；"
    "长文本一律通过 read 分段读取、通过文件传递，禁止塞进单次输出或单帧。"
)
_TOOL_RULE = (
    "【工具使用】read 分段读文件，grep/glob 做跨节检索，write 创建产物，"
    "edit 只做最小必要改写；不要重写整篇文档。"
)

#: 所有角色共用的 system prompt 前缀。
SYSTEM_PREAMBLE = "\n".join([_FACT_RULE, _SANDBOX_RULE, _TOOL_RULE, ""])

#: 受控工具白名单（放开命令执行：加入 bash，与 config.omp_task_tools / omp_safety.SAFE_DEFAULT_TOOLS 同口径）。
DEFAULT_TOOLS = "write,read,edit,grep,glob,bash"

# ---------- 占位符 / 事实 / 编号 / 图表判据 ----------
_PLACEHOLDER_RE = re.compile(
    r"(?:__[A-Z_]{2,}__|\{\{[^\}]{1,40}\}\}|【待填[^】]*】|待补充|待填写|TODO|TBD|XXX)"
)
#: 只在高敏感单位上做事实回溯，避免把 "100%" / 章节编号误判为编造。
_FACT_TOKEN_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:日历天|天|米|毫米|厘米|MPa|kN|kW|吨|℃|°C|m|mm|cm|t)(?![A-Za-z])"
)
_HEADING_NUMBER_RE = re.compile(r"^#{1,6}\s+(\d+(?:\.\d+)*)\s*[.、]?\s*\S")
# ✅ R232（2026-11-13）：捕获前导字（图|表）——旧版把「图 3-1」与「表 3-1」混进
#    同一个章号桶，同章同号的图+表（工程方案常态）必被判「图号虚跳」假发现。
_FIGURE_RE = re.compile(r"([图表])\s*(\d+)\s*[-—]\s*(\d+)")
_HEADING_LEVEL_RE = re.compile(r"^(#{1,6})\s+\S")


@dataclass(frozen=True)
class AgentRole:
    """一个 omp Agent 角色的静态定义。"""

    key: str
    name: str
    system_prompt: str
    tools: str = DEFAULT_TOOLS
    output_file: str = ""
    acceptance: tuple[str, ...] = ()


OUTLINE_ROLE = AgentRole(
    key="outline",
    name="建筑工程专项施工方案目录编制专家",
    system_prompt=SYSTEM_PREAMBLE
    + "你是「建筑工程专项施工方案目录编制专家」，精通住建部专项施工方案编制导则与"
    "招投标文件评审要点。你的产物是可直接落库的结构化目录树，不得写成散文。",
    output_file="OUTLINE.json",
    acceptance=(
        "目录层级严格限制在 L1~L4（Markdown 对应 #~####）",
        "每个 L1 下 3~5 个 L2、每个 L2 下 3~5 个 L3、每个 L3 下 4~6 个 L4",
        "标题不得携带编号前缀（编号由平台落库器统一生成）",
        "输出为可被 check_json 解析的 OUTLINE.json",
    ),
)

CONTENT_ROLE = AgentRole(
    key="content",
    name="资深工程技术方案撰写专家",
    system_prompt=SYSTEM_PREAMBLE
    + "你是「资深工程技术方案撰写专家」，按目录叶子节点逐节撰写正式、朴实、"
    "专业、不空泛的工程技术方案正文；只写本章对应要素，不越界、不与同级章节重复，"
    "不生成章节标题（标题由平台目录提供）。",
    output_file="sections/{section_id}.md",
    acceptance=(
        "每个 L4 节点正文达到分配的字数（L3 总字数均分给其 L4 叶子）",
        "所有数值/单位可回溯到 global-facts.md",
        "无占位符、无未闭合代码围栏、无重复同级内容",
        "产物写入 sections/<section_id>.md，不修改其它文件",
    ),
)

AUDIT_ROLE = AgentRole(
    key="audit",
    name="严苛的标书质量审核专家",
    system_prompt=SYSTEM_PREAMBLE
    + "你是「严苛的标书质量审核专家」，对整卷 Markdown 做编号连贯性、图表引用、"
    "围栏闭合与排版格式的全量审计。你**不重写全文**，只返回精确的 "
    "old_text/new_text 最小修复指令；连续修复失败时如实标记需人工介入的片段。",
    output_file="FIXES.json",
    acceptance=(
        "每条修复指令含 section_id / old_text / new_text / reason",
        "old_text 在对应章节正文中恰好出现一次（保证精确命中和最小改写）",
        "不得把整章正文作为 old_text，不得新增/删除/重排章节",
        "产出 AUDIT_REPORT.md 完整审核与修复报告",
    ),
)

ROLES: dict[str, AgentRole] = {
    OUTLINE_ROLE.key: OUTLINE_ROLE,
    CONTENT_ROLE.key: CONTENT_ROLE,
    AUDIT_ROLE.key: AUDIT_ROLE,
}


# ===================== Prompt 模板：plan / execute / fix =====================
def _plan_block(title: str, steps: list[str], files: list[str], acceptance: list[str]) -> str:
    """生成结构化计划正文（标题 / 步骤 / 文件 / 验收标准）。"""
    lines = [f"# 计划：{title}", "", "## 步骤"]
    lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    lines += ["", "## 涉及文件"]
    lines += [f"- `{f}`" for f in files]
    lines += ["", "## 验收标准"]
    lines += [f"- {a}" for a in acceptance]
    lines += [
        "",
        "⚠️ 执行前先读本计划；验收由后端结构化校验器（check_json / verify_*）判定，",
        "不通过时按错误信息进入 fix 轮，禁止直接宣称完成。",
        "",
    ]
    return "\n".join(lines)


def build_outline_prompts(
    *, brief: str, project_type: str = "", industry: str = ""
) -> dict[str, str]:
    """目录生成 Agent 的 plan / execute / fix 三段 Prompt。"""
    plan = _plan_block(
        "专项施工方案标准化目录树（L1~L4）",
        [
            "用 read 读取 CONTEXT.md（项目概况与规范约束）、global-facts.md（全局事实）",
            "按九大要素规划一级章：工程概况 / 编制依据 / 施工部署 / 进度计划 / 资源配置 / "
            "主要施工工艺 / 质量保证 / 安全文明与应急 / 环保节能",
            "逐层展开 L2/L3/L4，核对每层子节点数量约束",
            "写出 OUTLINE.json 并自检结构与数量",
        ],
        ["CONTEXT.md", "global-facts.md", "OUTLINE.json"],
        list(OUTLINE_ROLE.acceptance),
    )
    execute = "\n".join(
        [
            "【Execute · 目录生成】先读 PLAN.md 与 CONTEXT.md / global-facts.md。",
            "用 write 写出 OUTLINE.json，结构固定为：",
            '{"sections":[{"title":"工程概况","level":1,"description":"","children":[...]}]}',
            f"硬约束：层级 {1}~{MAX_OUTLINE_LEVEL}；"
            f"L2 {LEVEL_CHILD_RANGE[1][0]}~{LEVEL_CHILD_RANGE[1][1]} 个 / "
            f"L3 {LEVEL_CHILD_RANGE[2][0]}~{LEVEL_CHILD_RANGE[2][1]} 个 / "
            f"L4 {LEVEL_CHILD_RANGE[3][0]}~{LEVEL_CHILD_RANGE[3][1]} 个（按父节点计）。",
            "标题不要写 '1' / '1.1' 之类的编号前缀；编号由平台落库器统一生成。",
            "只引用全局事实中的数据，严禁编造规范编号与工程参数。",
            f"项目类型：{project_type or '未指定'}；行业/专业：{industry or '未指定'}。",
            "项目简报（完整版见 CONTEXT.md）：" + brief[:2000],
            "",
            _SANDBOX_RULE,
        ]
    )
    fix = "\n".join(
        [
            "【Fix · 目录定向修复】后端 check_json / verify_outline 判定不通过，错误如下：",
            "{issues}",
            "用 read 读回 OUTLINE.json，只针对报错节点做补全或裁减：",
            "- L2/L3/L4 数量不足 → 补同级节点（标题不得与已有节点重复）；",
            "- 数量超限 / 层级越界 → 删除或下沉多余节点；",
            "- JSON 结构损坏 → 重写为合法 JSON，但保留已正确的分支；",
            "禁止整体推倒重写；改完再次核对全部数量约束。",
            "",
            _SANDBOX_RULE,
        ]
    )
    return {"plan": plan, "execute": execute, "fix": fix}


def build_content_prompts(
    *, outline_digest: str, facts: str, tier: str = "standard"
) -> dict[str, str]:
    """正文生成 Agent 的 plan / execute / fix 三段 Prompt。"""
    budget = WORD_BUDGET_TIERS.get(tier, WORD_BUDGET_TIERS["standard"])
    plan = _plan_block(
        "按 L4 叶子节点批量撰写正文",
        [
            "用 read 读取 CONTEXT.md（目录树摘要）与 global-facts.md（唯一权威事实）",
            "按 L3 分组，把该章总字数均分给其 L4 叶子节点，登记到预算表",
            "为每个 L4 编排写作要点、需引用的图表编号与知识库素材",
            "逐节写出 sections/<section_id>.md，并自检字数 / 事实 / 占位符",
        ],
        ["CONTEXT.md", "global-facts.md", "sections/<section_id>.md"],
        list(CONTENT_ROLE.acceptance),
    )
    execute = "\n".join(
        [
            "【Execute · 逐节正文生成】先读 PLAN.md、CONTEXT.md 与 global-facts.md。",
            f"档位 {tier}（标准章预算 {budget} 字），本章目标字数以任务指令中的 budget 为准。",
            "为任务指定的 L4 节点撰写正文，用 write 写入 sections/<section_id>.md：",
            "- 只展开本章对应要素，不越界、不重复同级章节内容、不写章节标题；",
            "- 正式工程文体，朴实准确，不空泛、不堆砌套话；",
            "- 图表引用必须与 CONTEXT.md 中登记的编号一致，禁止自造图号；",
            "- 不得出现占位符（TODO / 待补充 / {{X}} / __X__）或未闭合代码围栏。",
            "目录树摘要：" + outline_digest[:4000],
            "",
            _FACT_RULE,
            _SANDBOX_RULE,
        ]
    )
    fix = "\n".join(
        [
            "【Fix · 正文定向修复】后端 verify_content_draft 判定不通过，错误如下：",
            "{issues}",
            "用 read 读回 sections/<section_id>.md，按错误类型定向处理：",
            "- 字数不足 → 触发「扩展循环」：补施工工艺参数、质量控制点、验收要求等实质内容，"
            "不得靠重复句子注水；",
            "- 事实冲突 / 疑似编造 → 用 grep 回溯 global-facts.md，改为权威值，全篇统一；",
            "- 占位符 / 未闭合围栏 → 精确替换或补齐闭合行；",
            "禁止整章推倒重写，禁止引入 global-facts.md 之外的新数据。",
            "",
            _SANDBOX_RULE,
        ]
    )
    return {"plan": plan, "execute": execute, "fix": fix}


def build_audit_prompts(*, document_count: int) -> dict[str, str]:
    """审核与修复 Agent 的 plan / execute / fix 三段 Prompt。"""
    plan = _plan_block(
        "整卷全量审计与结构化修复",
        [
            "用 glob 列出 sections/*.md，用 read 分段读取全部章节",
            "用 grep 跨节检索编号、图表引用、术语与单位的一致性",
            "逐项核对：编号连贯性 / 图表引用正确性 / 围栏闭合 / 排版格式 / 占位符",
            "产出 FIXES.json（精确 old_text/new_text 指令）与 AUDIT_REPORT.md",
        ],
        ["sections/*.md", "FIXES.json", "AUDIT_REPORT.md"],
        list(AUDIT_ROLE.acceptance),
    )
    execute = "\n".join(
        [
            f"【Execute · 全量审计】共 {document_count} 个章节文件，先读 PLAN.md。",
            "用 glob/read/grep 做全文一致性扫描，检查：",
            "① 章节编号是否连贯、有无跳号重号；② 「图 X-Y」/「表 X-Y」引用是否指向已登记编号、"
            "有无虚跳；③ 代码围栏是否闭合；④ 标题层级有无跳级；⑤ 是否残留占位符。",
            "用 write 产出 FIXES.json：",
            '{"fixes":[{"section_id":"s1","old_text":"工期 400 天","new_text":"工期 450 天",'
            '"reason":"与全局事实冲突","severity":"high"}]}',
            "**不要直接重写全文**；old_text 必须是该章节中唯一可精确命中的短片段，"
            "new_text 只改需要改的部分。",
            "同时写出 AUDIT_REPORT.md：发现项、修复指令、未决项与需人工介入的片段标记。",
            "没有问题时 FIXES.json 写 {\"fixes\":[]}，并在报告中说明。",
            "",
            _FACT_RULE,
            _SANDBOX_RULE,
        ]
    )
    fix = "\n".join(
        [
            "【Fix · 修复指令纠偏】后端 verify_audit_fixes / apply_fixes 判定不通过，错误如下：",
            "{issues}",
            "用 read/grep 重新定位原文，改写 FIXES.json：",
            "- old_text 命中 0 次或多次 → 扩写上下文使其唯一，或拆成多条指令；",
            "- old_text 过长（整章重写）→ 收窄到最小差异片段；",
            "- section_id 不存在 → 改为 glob 列出的真实文件名；",
            "连续多轮仍失败时，在 AUDIT_REPORT.md 中明确标记「需人工介入」及异常片段，"
            "由编排层暂停该任务（对应 agentPauseGate 的暂停语义）。",
            "",
            _SANDBOX_RULE,
        ]
    )
    return {"plan": plan, "execute": execute, "fix": fix}


# ===================== 结构化校验器 =====================
def _strip_json_fence(text: str) -> str:
    """剥离 ```json ... ``` 围栏（模型常自行加围栏，属可修复形态而非致命错误）。"""
    s = text.strip()
    m = re.search(r"```(?:json|JSON)?\s*\n?(.*?)```", s, flags=re.S)
    if m:
        return m.group(1).strip()
    return s


def check_json(text: str, *, required_keys: tuple[str, ...] = ()) -> tuple[bool, str, object]:
    """``check_json``：解析 JSON 并校验必填键，返回 ``(ok, error, data)``。

    这是 ``verify`` 阶段的结构完整性闸门；失败原因会原样进入 ``fix`` 轮 Prompt。
    """
    if not text or not text.strip():
        return False, "产物为空（模型未写出文件）", None
    try:
        data = json.loads(_strip_json_fence(text))
    except (ValueError, TypeError) as e:
        return False, f"JSON 解析失败：{e}", None
    if required_keys:
        if not isinstance(data, dict):
            return False, f"顶层应为对象且含键 {list(required_keys)}，实际为 {type(data).__name__}", data
        missing = [k for k in required_keys if k not in data]
        if missing:
            return False, f"缺少必填键：{missing}", data
    return True, "", data


def _iter_nodes(nodes: object, level: int = 1):
    """遍历嵌套目录树，产出 ``(node, level, children)``。"""
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        children = node.get("children") or []
        yield node, level, children
        yield from _iter_nodes(children, level + 1)


def verify_outline(tree: object) -> list[str]:
    """校验目录层级深度、每层子节点数量约束、标题与编号前缀。

    与平台 ``_save_outline_to_db`` 的 ``normalize_outline`` / 编号闸门互补：
    这里只做**生成侧可修复**约束，编号唯一性与点分链由落库器单一持有，
    本函数不造第二份编号判据。
    """
    issues: list[str] = []
    if not isinstance(tree, list) or not tree:
        return ["目录树为空或不是数组"]
    count = 0
    for node, level, children in _iter_nodes(tree):
        count += 1
        if count > MAX_OUTLINE_NODES:
            issues.append(f"节点总数超过上限 {MAX_OUTLINE_NODES}")
            break
        if level > MAX_OUTLINE_LEVEL:
            issues.append(f"层级越界：第 {count} 个节点深度 {level} > L{MAX_OUTLINE_LEVEL}")
            continue
        title = str(node.get("title") or "").strip()
        if not title:
            issues.append(f"第 {count} 个节点标题为空")
        elif re.match(r"^\d+(?:\.\d+)*\s*[.、]?\s", title):
            issues.append(f"标题携带编号前缀（应由落库器生成）：{title[:30]}")
        rng = LEVEL_CHILD_RANGE.get(level)
        if rng:
            lo, hi = rng
            # ⚠️ 不能写成 ``if rng and children``：L1~L3 完全没有子节点时，
            #    旧判据会静默放过「数量不足到 0」这一最严重的漏展开形态。
            n_children = len(children) if isinstance(children, list) else 0
            if not (lo <= n_children <= hi):
                issues.append(
                    f"L{level}「{title[:20]}」子节点 {n_children} 个，"
                    f"要求 {lo}~{hi} 个（补全或裁减）"
                )
    if not issues and count < 2:
        issues.append("目录节点过少（少于 2 个），疑似未展开")
    return issues


def allocate_leaf_budgets(total_budget: int, leaf_count: int) -> list[int]:
    """把一个 L3 章的总字数**均分**给其 L4 叶子节点。

    例：L3 总字数 1200 / 4 个 L4 → 每节 300 字。地板取平台
    ``LEAF_MIN_WORD_BUDGET``（300）：若均分结果低于地板，按地板分配，
    此时总量会超过章预算 —— 这是平台既有「叶子下限优先」口径，注释在此显式声明。
    """
    if leaf_count <= 0:
        return []
    per = max(LEAF_MIN_WORD_BUDGET, int(total_budget) // int(leaf_count))
    return [per] * int(leaf_count)


def _fact_tokens(text: str) -> set[str]:
    return {re.sub(r"\s+", "", m.group(0)) for m in _FACT_TOKEN_RE.finditer(text or "")}


def assert_fact_grounding(markdown: str, facts: str) -> list[str]:
    """用「数值 + 高敏感单位」token 回溯全局事实，交叉验证是否编造。

    只报**疑似**，交由 fix 轮用 grep 复核；刻意不把 ``%``、章节编号、
    纯型号等纳入，避免噪声（对齐本仓 R19x「裸子串误判」教训）。
    """
    if not facts.strip():
        return []
    known = _fact_tokens(facts)
    unknown = sorted(_fact_tokens(markdown) - known)
    if not unknown:
        return []
    return ["疑似脱离全局事实的数据（请用 grep 回溯 global-facts.md 核对）：" + "、".join(unknown[:8])]


def verify_content_draft(
    markdown: str, *, budget: int, facts: str = "", strict_facts: bool = True
) -> list[str]:
    """校验单节正文：字数档位、非法占位符、未闭合围栏、全局事实一致性。"""
    issues: list[str] = []
    if not markdown or not markdown.strip():
        return ["正文为空"]
    wc = text_word_count(markdown)
    low = int(budget * WORD_UNDER_RATIO)
    high = int(budget * WORD_OVER_RATIO)
    if wc < low:
        issues.append(f"字数不足：{wc} 字 < 下限 {low} 字（目标 {budget}），需触发扩展循环")
    elif wc > high:
        issues.append(f"字数超限：{wc} 字 > 上限 {high} 字（目标 {budget}），需定向压缩")
    ph = sorted(set(_PLACEHOLDER_RE.findall(markdown)))
    if ph:
        issues.append("含非法占位符：" + "、".join(str(p) for p in ph[:6]))
    fences = find_unclosed_fences(markdown)
    if fences:
        issues.append(f"存在 {len(fences)} 处未闭合代码围栏（首处行 {fences[0]['line']}）")
    if strict_facts:
        issues.extend(assert_fact_grounding(markdown, facts))
    return issues


def verify_audit_fixes(payload: object, documents: dict[str, str]) -> list[str]:
    """校验修复指令：结构、section_id 存在性、old_text 唯一命中、最小改写。"""
    ok, err, data = check_json(payload, required_keys=("fixes",)) if isinstance(payload, str) else (True, "", payload)
    if not ok:
        return [err]
    fixes = (data or {}).get("fixes") if isinstance(data, dict) else None
    if not isinstance(fixes, list):
        return ["FIXES.json 的 fixes 必须是数组"]
    issues: list[str] = []
    for i, fx in enumerate(fixes):
        tag = f"第 {i + 1} 条指令"
        if not isinstance(fx, dict):
            issues.append(f"{tag}不是对象")
            continue
        sid = str(fx.get("section_id") or "").strip()
        old = str(fx.get("old_text") or "")
        new = str(fx.get("new_text") or "")
        if sid not in documents:
            issues.append(f"{tag}的 section_id「{sid}」不在章节索引中")
            continue
        if len(old) < 2:
            issues.append(f"{tag}的 old_text 过短，无法精确命中")
            continue
        hits = documents[sid].count(old)
        if hits != 1:
            issues.append(f"{tag}的 old_text 在 {sid} 命中 {hits} 次（要求恰好 1 次）")
        if new == old:
            issues.append(f"{tag}的 new_text 与 old_text 相同（空修复）")
        if len(new) > len(old) * 2 + 200:
            issues.append(f"{tag}改写幅度过大，疑似整章重写（{len(old)} → {len(new)} 字）")
        if not str(fx.get("reason") or "").strip():
            issues.append(f"{tag}缺少 reason（修复报告需要归因）")
    return issues


def apply_fix_directives(documents: dict[str, str], fixes: list[dict]) -> tuple[dict[str, str], list[str]]:
    """按精确 ``old_text``/``new_text`` 应用修复，返回新文档与失败项。

    不重写全文：每条指令只替换唯一命中的短片段；任一指令失败即记为
    「需人工介入」，由编排层决定是否暂停（对齐 agentPauseGate 兜底语义）。
    """
    out = dict(documents)
    failures: list[str] = []
    for i, fx in enumerate(fixes):
        sid = str(fx.get("section_id") or "")
        old, new = str(fx.get("old_text") or ""), str(fx.get("new_text") or "")
        if sid not in out or len(old) < 2 or out[sid].count(old) != 1:
            failures.append(f"第 {i + 1} 条：section_id={sid} 无法精确命中，需人工介入")
            continue
        out[sid] = out[sid].replace(old, new, 1)
    return out, failures


def scan_documents(documents: dict[str, str]) -> list[dict]:
    """程序侧全量审计：编号连贯 / 图表引用 / 围栏 / 排版 / 占位符。

    ⚠️ 这是**确定性扫描**，不依赖模型判断；Agent 负责生成修复指令，
       判据与结论由本函数与平台既有校验器持有（避免「AI 自报合格」）。
    """
    findings: list[dict] = []
    for sid, text in (documents or {}).items():
        # ① 编号连贯（同级序列递增）。
        # ✅ R232（2026-11-13）：桶键改为「(深度, 父链)」——旧版只按深度归桶，
        #    跨父节点的合法多章编号（1.1 → 1.2 → 2.1）被误判「L2 编号跳变 2→1」。
        #    同一父链内才做末段递增判定；L1 的父链为空元组（同卷内 1→2→3 仍判跳变）。
        levels: dict[tuple[int, tuple[int, ...]], list[int]] = {}
        for line in text.splitlines():
            m = _HEADING_NUMBER_RE.match(line)
            if not m:
                continue
            parts = [int(p) for p in m.group(1).split(".")]
            depth = len(parts)
            levels.setdefault((depth, tuple(parts[:-1])), []).append(parts[-1])
        for (depth, _parent), seq in levels.items():
            for a, b in zip(seq, seq[1:]):
                if b != a + 1:
                    findings.append(
                        {"section_id": sid, "type": "numbering", "detail": f"L{depth} 编号跳变 {a}→{b}"}
                    )
        # ② 图表引用虚跳（同章内序号不连续）。
        # ✅ R232（2026-11-13）：按前导字（图/表）分桶——「图 3-1」与「表 3-1」
        #    是两套独立序号，同章同号并存完全合法，不得互相挤进对方的序列。
        figs: dict[tuple[str, int], list[int]] = {}
        for m in _FIGURE_RE.finditer(text):
            figs.setdefault((m.group(1), int(m.group(2))), []).append(int(m.group(3)))
        for (kind, chap), seq in figs.items():
            for a, b in zip(seq, seq[1:]):
                if b != a + 1:
                    findings.append(
                        {"section_id": sid, "type": "figure_ref", "detail": f"{kind}号虚跳 {kind} {chap}-{a}→{chap}-{b}"}
                    )
        # ③ 未闭合围栏（委托平台单一事实源）
        for f in find_unclosed_fences(text):
            findings.append(
                {"section_id": sid, "type": "fence", "detail": f"第 {f['line']} 行围栏未闭合"}
            )
        # ④ 占位符
        if _PLACEHOLDER_RE.search(text):
            findings.append({"section_id": sid, "type": "placeholder", "detail": "残留占位符"})
        # ⑤ 排版：标题层级跳级
        prev = 0
        for line in text.splitlines():
            m = _HEADING_LEVEL_RE.match(line)
            if not m:
                continue
            lv = len(m.group(1))
            if prev and lv > prev + 1:
                findings.append(
                    {"section_id": sid, "type": "format", "detail": f"标题层级跳级 L{prev}→L{lv}"}
                )
            prev = lv
    return findings


@dataclass
class RoleBundle:
    """三环节 Prompt 模板的成组产物（便于编排层统一消费）。"""

    role: AgentRole
    prompts: dict[str, str] = field(default_factory=dict)
