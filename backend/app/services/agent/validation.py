"""P2 — 阶段产物校验（移植自易标 ``outlineGenerationTaskV2.cjs``）。

来源与对应关系（均在 ``C:\\...\\yibiao_outline\\outlineGenerationTaskV2.cjs``）：

- L10–L24 文件名常量与 ``STAGE_OUTPUT_FILES``（6 个阶段的必需产物文件表，
  键名连字符/下划线差异逐字保留）；
- ``validateJsonOutput`` → ``validate_json_output``；
- ``buildFinalOutline`` → ``build_final_outline``（内部走
  ``outline_identity.accept_agent_outline`` 分配节点身份）；
- ``validateOutlineOutput`` → ``validate_outline_output``；
- ``validateScorePlanningRoots`` → ``validate_score_planning_roots``；
- L909 ``validateStageOutputs`` → ``validate_stage_outputs``。

产出 issue 结构 1:1：``{'severity': 'blocking', 'file': 文件名,
'path': JSON 路径（根为 '/'）, 'message': 中文文案}``。

Schema 注入：易标在本文件内用 Ajv 编译 ``OUTLINE_JSON_SCHEMA`` 等 Schema 本体；
本仓 P3 阶段提示词层才补 Schema 本体，因此校验器由调用方以
``dict[file_path, validator]`` 注入（``validate_stage_outputs`` 的
``schema_validators`` 形参；``validate_outline_output`` /
``validate_score_planning_roots`` 可选 ``validator`` 形参）。

``validator`` 形参契约（对齐易标 Ajv ``validate(value)`` 的用法）::

    validator(value) -> (ok: bool, errors: list[dict] | None)

- ``ok=True``：通过，``errors`` 被忽略；
- ``ok=False``：``errors`` 每项含 Ajv 同名键 ``instancePath``（JSON 实例
  路径，空串/缺失表示根节点）与 ``message``（错误描述）；
- ``validator=None``：该文件暂无 Schema，跳过结构校验（对应易标
  ``!validate || validate(generated)``）。

``validate`` / ``read_file`` 等可调用对象抛出的异常一律向上抛（易标红线：
不吞执行异常，只把空文件、JSON 语法和 Schema 问题写入提交报告）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .outline_identity import accept_agent_outline, collect_outline_ids

# ── 文件名常量（outlineGenerationTaskV2.cjs L10–L16，逐字移植）──────────
OUTLINE_OUTPUT_FILE = "outline.json"
TECHNICAL_SCORE_GROUPS_FILE = "technical-score-groups.json"
SCORE_DIRECTORY_PLAN_FILE = "score-directory-plan.json"
LEAF_ALLOCATION_FILE = "leaf-allocation.json"
LEAF_ALLOCATION_CONTEXT_FILE = "leaf-allocation-context.json"
OUTLINE_REVIEW_FILE = "outline-review.json"
OUTLINE_REVIEW_CONTEXT_FILE = "outline-review-context.json"

#: AI 生成内容模式（L25，``validateScorePlanningRoots`` 校验新增一级目录用）。
AI_CONTENT_MODE = "ai-generate"

#: 一级目录属性枚举（易标 L26 逐字；Schema ``attr`` enum 与提示词共用）。
OUTLINE_ATTRIBUTES = ["通用", "商务/资信", "技术", "其他", "目录", "报价", "业绩"]

#: 内容模式枚举（易标 L27 逐字；Schema ``content_mode`` enum 共用）。
CONTENT_MODES = ["ai-generate", "template-fill", "directory-generate",
                 "manual-fill", "other"]

#: 属性 → 强制内容模式（易标 L28–33 逐字；未列出的属性不强制）。
ATTRIBUTE_CONTENT_MODE_RULES = {
    "商务/资信": "template-fill",
    "目录": "directory-generate",
    "报价": "manual-fill",
    "业绩": "template-fill",
}

#: 6 个 workflow 阶段的必需产物文件表（L17–L24，键名逐字照抄：
#: 前两个用连字符、后四个用下划线）。
STAGE_OUTPUT_FILES: dict[str, list[str]] = {
    "initial-outline": [OUTLINE_OUTPUT_FILE],
    "score-planning": [TECHNICAL_SCORE_GROUPS_FILE, SCORE_DIRECTORY_PLAN_FILE],
    "leaf_allocation": [LEAF_ALLOCATION_FILE],
    "children_generation": [OUTLINE_OUTPUT_FILE],
    "leaf_adjustment": [OUTLINE_OUTPUT_FILE],
    "outline_review": [OUTLINE_OUTPUT_FILE, OUTLINE_REVIEW_FILE],
}

#: Schema 校验器注入表：文件路径 → ``validator(value) -> (ok, errors)``。
SchemaValidators = dict[str, Callable[[Any], tuple[bool, list[dict] | None]]]


@dataclass
class StageMeta:
    """阶段校验上下文（易标 ``validateStageOutputs`` 闭包里的 ``meta``
    与 ``knownNodeIds`` / ``lockedRoots`` 合并载体）。

    - ``workflow_stage``：``STAGE_OUTPUT_FILES`` 的键；
    - ``read_file``：同步按文件名读取产物文本（易标为
      ``await meta.readFile(file)``；读取异常向上抛，由执行层处理）；
    - ``known_node_ids``：当前目录已确认的节点身份（outline.json 校验用）；
    - ``locked_roots``：评分规划阶段用户已确认的一级目录
      （``validateScorePlanningRoots`` 的 ``confirmedRoots``）。
    """

    workflow_stage: str
    read_file: Callable[[str], str]
    known_node_ids: set = field(default_factory=set)
    locked_roots: list = field(default_factory=list)


def validate_json_output(
    content: Any,
    file: str,
    validate: Callable[[Any], tuple[bool, list[dict] | None]] | None,
) -> dict:
    """只把空文件、JSON 语法和 Schema 问题写入提交报告，不吞掉执行异常
    （易标 ``validateJsonOutput``）。

    三类产出（message 逐字移植）：

    1. 空文件：``{file} 未写入或内容为空，请写入完整的 JSON``，``value=None``；
    2. 非法 JSON：仅捕获 ``json.JSONDecodeError``（对齐易标只捕获
       ``SyntaxError``），文案 ``{file}不是合法 JSON：{错误信息}``，
       ``value=None``；其余异常向上抛；
    3. Schema 校验失败：每个 ``errors`` 项产出一条 blocking issue，
       文案 ``{file} 不符合结构要求：{instancePath 或 '/'} {message}``，
       此时 ``value`` 仍保留已解析的值（与易标一致）。

    返回 ``{'value': ..., 'issues': [...]}``。
    """
    text = str(content or "").strip()
    if not text:
        return {"value": None, "issues": [{
            "severity": "blocking",
            "file": file,
            "message": f"{file} 未写入或内容为空，请写入完整的 JSON",
        }]}
    try:
        generated = json.loads(text)
    except json.JSONDecodeError as error:
        return {"value": None, "issues": [{
            "severity": "blocking",
            "file": file,
            "message": f"{file}不是合法 JSON：{error}",
        }]}
    if validate is None:
        return {"value": generated, "issues": []}
    ok, errors = validate(generated)
    if ok:
        return {"value": generated, "issues": []}
    return {"value": generated, "issues": [
        {
            "severity": "blocking",
            "file": file,
            "path": item.get("instancePath") or "/",
            "message": (
                f"{file} 不符合结构要求："
                f"{item.get('instancePath') or '/'} {item.get('message')}"
            ),
        }
        for item in (errors or [])
    ]}


def build_final_outline(candidate: dict | None,
                        known_ids: set | None = None) -> dict:
    """接受阶段输出，保留已有身份并给新节点分配 ID（易标 ``buildFinalOutline``）。"""
    outline = (candidate or {}).get("outline") or []
    return {"outline": accept_agent_outline(outline, known_ids)}


def validate_outline_output(
    content: Any,
    known_ids: set | None = None,  # 仅读不写；None 归一为空集
    check_schema: bool = True,
    validator: Callable[[Any], tuple[bool, list[dict] | None]] | None = None,
) -> dict:
    """目录生成与调整共用解析及身份校验；按调用方原有规则检查 Schema，
    通过后才分配新节点 ID（易标 ``validateOutlineOutput``）。

    - ``known_ids``：Agent 可沿用的已知节点 ID 集合；
    - ``check_schema``：为 False 时跳过 Schema 校验（对应易标
      ``{ checkSchema }`` 解构参数）；``validator`` 为 None 时同样跳过
      （Schema 本体由 P3 注入）；
    - 遍历结构错误（非数组、非对象、未知 ID、重复 ID）全部进入提交修复
      issues，不作为执行异常中断任务；issue 文案逐字移植。

    返回 ``{'value': ..., 'issues': [...]}``，零 issue 时 ``value`` 已是
    ``build_final_outline`` 重组并编号后的目录。
    """
    if known_ids is None:
        known_ids = set()
    report = validate_json_output(
        content, OUTLINE_OUTPUT_FILE,
        validator if check_schema else None,
    )
    if report["issues"]:
        return report
    seen: set = set()

    def visit(items: Any, parent_path: str) -> None:
        if not isinstance(items, list):
            report["issues"].append({
                "severity": "blocking",
                "file": OUTLINE_OUTPUT_FILE,
                "path": parent_path,
                "message": f"目录结构错误：{parent_path} 必须是数组",
            })
            return
        for index, item in enumerate(items):
            node_path = f"{parent_path}/{index}"
            if not isinstance(item, dict):
                report["issues"].append({
                    "severity": "blocking",
                    "file": OUTLINE_OUTPUT_FILE,
                    "path": node_path,
                    "message": f"目录结构错误：{node_path} 必须是非空对象",
                })
                continue
            node_id = item.get("id")
            if node_id is not None:
                if node_id not in known_ids:
                    report["issues"].append({
                        "severity": "blocking",
                        "file": OUTLINE_OUTPUT_FILE,
                        "path": f"{node_path}/id",
                        "message": f"目录包含未知 ID：{node_id}；新节点请使用 id:null",
                    })
                if node_id in seen:
                    report["issues"].append({
                        "severity": "blocking",
                        "file": OUTLINE_OUTPUT_FILE,
                        "path": f"{node_path}/id",
                        "message": f"目录 ID 重复：{node_id}",
                    })
                seen.add(node_id)
            children = item.get("children")
            if children:
                visit(children, f"{node_path}/children")

    value = report["value"]
    outline = value.get("outline") if isinstance(value, dict) else None
    visit(outline if outline is not None else [], "/outline")
    if not report["issues"]:
        report["value"] = build_final_outline(value, known_ids)
    return report


def validate_score_planning_roots(
    content: Any,
    score_directory_plan: dict | None,
    confirmed_roots: list,
    outline_validator: Callable[[Any], tuple[bool, list[dict] | None]] | None = None,
) -> dict:
    """评分规划阶段只接受用户批准新增的技术一级目录；分配新 ID 后按标题
    回填规划分支的 ``root_id``（易标 ``validateScorePlanningRoots``，
    issue 文案逐字移植）。

    - ``content``：候选 ``outline.json`` 文本；
    - ``score_directory_plan``：已解析的 ``score-directory-plan.json`` 对象；
    - ``confirmed_roots``：用户已确认的一级目录（``lockedRoots``）；
    - ``outline_validator``：注入给内层 ``validate_outline_output`` 的
      outline Schema 校验器（可为 None）。

    返回 ``{'value': None, 'issues': ...}``（有问题）或
    ``{'value': {outline.json: ..., score-directory-plan.json: ...}, 'issues': []}``
    （通过，分支 ``root_id`` 已回填）。
    """
    report = validate_outline_output(
        content,
        known_ids=collect_outline_ids(confirmed_roots),
        validator=outline_validator,
    )
    if report["issues"]:
        return {"value": None, "issues": report["issues"]}
    issues: list[dict] = []

    def add_issue(file: str, path: str, message: str) -> None:
        issues.append({"severity": "blocking", "file": file,
                       "path": path, "message": message})

    def root_fields(item: dict | None) -> str:
        item = item or {}
        note = (str(item.get("content_mode_note") or "").strip()
                if item.get("content_mode") == "other" else "")
        return json.dumps([
            str(item.get("title") or "").strip(),
            str(item.get("description") or "").strip(),
            item.get("attr"),
            item.get("content_mode"),
            note,
        ], ensure_ascii=False, separators=(",", ":"))

    plan = score_directory_plan if isinstance(score_directory_plan, dict) else {}
    confirmed_root_ids = {item.get("id") for item in confirmed_roots}
    roots = report["value"]["outline"]
    kept_roots = [item for item in roots if item.get("id") in confirmed_root_ids]
    new_roots = [item for item in roots
                 if item.get("id") not in confirmed_root_ids]
    kept_unchanged = (
        len(kept_roots) == len(confirmed_roots)
        and all(item.get("id") == confirmed_roots[index].get("id")
                and root_fields(item) == root_fields(confirmed_roots[index])
                for index, item in enumerate(kept_roots))
    )
    if not kept_unchanged:
        add_issue(
            OUTLINE_OUTPUT_FILE, "/outline",
            f"{OUTLINE_OUTPUT_FILE} 中用户已确认的一级目录必须全部保留，"
            "id、title、description、attr、content_mode 和相对顺序均不得修改；"
            "本阶段只能插入经用户批准新增的一级目录",
        )
    for index, item in enumerate(roots):
        if item.get("children"):
            add_issue(
                OUTLINE_OUTPUT_FILE, f"/outline/{index}/children",
                f"{OUTLINE_OUTPUT_FILE} 本阶段只调整一级目录，"
                f"「{item.get('title')}」不得包含 children",
            )
    title_counts: dict = {}
    for item in roots:
        title_counts[item.get("title")] = title_counts.get(item.get("title"), 0) + 1
    for item in new_roots:
        if item.get("attr") != "技术" or item.get("content_mode") != AI_CONTENT_MODE:
            add_issue(
                OUTLINE_OUTPUT_FILE, "/outline",
                f"新增一级目录「{item.get('title')}」必须设置 "
                f"attr=技术、content_mode={AI_CONTENT_MODE}",
            )
        if title_counts.get(item.get("title"), 0) > 1:
            add_issue(
                OUTLINE_OUTPUT_FILE, "/outline",
                f"新增一级目录「{item.get('title')}」与其他一级目录标题重复，"
                "请使用唯一标题",
            )

    allow_root_changes = plan.get("allow_root_changes") is True
    if new_roots and not allow_root_changes:
        add_issue(
            SCORE_DIRECTORY_PLAN_FILE, "/allow_root_changes",
            f"{OUTLINE_OUTPUT_FILE} 新增了一级目录，但 allow_root_changes "
            "不是 true；只有用户明确批准一级目录调整时才能新增",
        )
    new_roots_by_title = {item.get("title"): item for item in new_roots}
    new_root_references: dict = {}
    branches: list = []
    for index, branch in enumerate(plan.get("branches") or []):
        path = f"/branches/{index}/root_id"
        if branch.get("root_id") is not None:
            if branch.get("root_id") not in confirmed_root_ids:
                add_issue(
                    SCORE_DIRECTORY_PLAN_FILE, path,
                    f"{SCORE_DIRECTORY_PLAN_FILE} 的 root_id"
                    f"「{branch.get('root_id')}」不是当前已确认的一级目录 id，"
                    f"请从 {OUTLINE_OUTPUT_FILE} 复制",
                )
            branches.append(branch)
            continue
        if not allow_root_changes:
            add_issue(
                SCORE_DIRECTORY_PLAN_FILE, path,
                f"{SCORE_DIRECTORY_PLAN_FILE} 中 root_id 为 null 仅用于用户批准"
                f"新增的一级目录，当前 allow_root_changes 不是 true",
            )
            branches.append(branch)
            continue
        root = new_roots_by_title.get(branch.get("root_title"))
        if root is None:
            add_issue(
                SCORE_DIRECTORY_PLAN_FILE, path,
                f"{SCORE_DIRECTORY_PLAN_FILE} 中 root_id 为 null 的分支"
                f"「{branch.get('root_title')}」在 {OUTLINE_OUTPUT_FILE} 中"
                "没有标题完全一致、id 为 null 的新增一级目录",
            )
            branches.append(branch)
            continue
        new_root_references[root["id"]] = new_root_references.get(root["id"], 0) + 1
        branches.append({**branch, "root_id": root["id"]})
    for item in new_roots:
        if new_root_references.get(item["id"]) != 1:
            add_issue(
                SCORE_DIRECTORY_PLAN_FILE, "/branches",
                f"新增一级目录「{item.get('title')}」必须在 "
                f"{SCORE_DIRECTORY_PLAN_FILE} 中恰好对应一个分支："
                "root_id 填 null，root_title 与该目录标题完全一致",
            )
    for index, item in enumerate(plan.get("extra_titles") or []):
        if item.get("root_id") not in confirmed_root_ids:
            add_issue(
                SCORE_DIRECTORY_PLAN_FILE, f"/extra_titles/{index}/root_id",
                f"{SCORE_DIRECTORY_PLAN_FILE} 中 extra_titles 的 root_id"
                f"「{item.get('root_id')}」不是当前已确认的一级目录 id",
            )
    if issues:
        return {"value": None, "issues": issues}
    return {
        "value": {
            OUTLINE_OUTPUT_FILE: {"outline": roots},
            SCORE_DIRECTORY_PLAN_FILE: {**plan, "branches": branches},
        },
        "issues": issues,
    }


def validate_stage_outputs(
    candidate: Any,
    meta: StageMeta,
    schema_validators: SchemaValidators,
) -> dict:
    """汇总全部阶段产物的逐项问题；读取失败仍由执行错误处理，通过后才交接
    （易标 ``validateStageOutputs``，结构与 issue 文案 1:1）。

    - ``candidate``：候选输出载体，``outline.json`` 的内容取自其
      ``output_content`` 属性（dict 或对象均可），其余文件走 ``meta.read_file``；
    - ``meta``：见 :class:`StageMeta`；
    - ``schema_validators``：``dict[file_path, validator]``，由调用方注入
      （P3 补 Schema 本体），缺项表示该文件暂不校验结构。

    ``score-planning`` 阶段在阶段文件全部零 issue 后追加
    ``validate_score_planning_roots`` 核对一级目录与规划分支的身份对应。
    返回 ``{'value': outputs, 'issues': issues}``。
    """
    output_content = (candidate.get("output_content")
                      if isinstance(candidate, dict)
                      else candidate.output_content)
    outputs: dict = {}
    issues: list[dict] = []
    for file in STAGE_OUTPUT_FILES[meta.workflow_stage]:
        if file == OUTLINE_OUTPUT_FILE:
            report = validate_outline_output(
                output_content,
                meta.known_node_ids,
                validator=schema_validators.get(OUTLINE_OUTPUT_FILE),
            )
        else:
            report = validate_json_output(
                meta.read_file(file), file, schema_validators.get(file))
        outputs[file] = report["value"]
        issues.extend(report["issues"])
    # 评分规划文件本身有效后，再核对一级目录与规划分支的身份对应。
    if meta.workflow_stage == "score-planning" and not issues:
        root_report = validate_score_planning_roots(
            output_content,
            outputs[SCORE_DIRECTORY_PLAN_FILE],
            meta.locked_roots,
            outline_validator=schema_validators.get(OUTLINE_OUTPUT_FILE),
        )
        issues.extend(root_report["issues"])
        if root_report["value"]:
            outputs.update(root_report["value"])
    return {"value": outputs, "issues": issues}
