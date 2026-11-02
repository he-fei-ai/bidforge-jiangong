"""技术评分单链六阶段 E2E 桩用例（2026-10-26 遗留 1 接线）。

落实《目录生成工具修正移植_实施报告_P0P4_20261025.md》遗留①技术评分模式接线：
``no_technical_score_mode=False`` 走六阶段
``initial-outline → score-planning → leaf_allocation → children_generation →
leaf_adjustment → outline_review``。

- 评分项来源：requirements（编制要求/评审要点）→ 宿主预置
  ``技术评分信息.md`` + ``technical-score-groups.json``（R{n} 分组）；
- score-planning 模型写分组 + ``score-directory-plan.json``（分支引用已确认根 id）；
- leaf_allocation 模型写 ``leaf-allocation.json``（agent-decides）；
- children_generation / leaf_adjustment 模型写完整目录；
- outline_review 模型写 ``outline-review.json``（passed 收束）；
- locked_roots：score-planning 校验（``validate_stage_outputs``）必须拦截
  分支 root_id 非已确认根的规划。

桩策略与单链测试同构：``chat_fn`` 驱动真实 ``run_agent_loop`` + P1 工具表，
仅模型侧打桩；任务注册走 conftest ``db_conn`` 内存库。
"""
import json
import uuid
from pathlib import Path

import app.services.ai.task_registry as tr
import pytest
from app.services.agent.outline_agent_flow import (
    _build_technical_score_groups,
    run_outline_agent,
)
from app.services.agent.outline_schemas import (
    build_json_validation_schemas,
    build_output_validators,
)
from app.services.agent.validation import (
    LEAF_ALLOCATION_CONTEXT_FILE,
    LEAF_ALLOCATION_FILE,
    OUTLINE_OUTPUT_FILE,
    OUTLINE_REVIEW_CONTEXT_FILE,
    OUTLINE_REVIEW_FILE,
    SCORE_DIRECTORY_PLAN_FILE,
    TECHNICAL_SCORE_GROUPS_FILE,
    StageMeta,
    validate_stage_outputs,
)

SCHEME = {"id": "scheme-tech-e2e", "name": "技术评分端到端方案", "type": "technical"}
_REVIEW_PASSED = {"status": "passed", "issues": [], "user_feedback": "",
                  "summary": "评分项覆盖完整，未发现需要修复的问题"}
_CONTEXT = {
    "requirements_text": "施工组织设计总体部署\n关键工序质量保证措施\n安全生产与文明施工",
    "project_brief": "项目概述：某市政道路提升工程。",
}


def _tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _assistant_write(content: str, path: str) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        _tool_call("write", {"path": path, "content": content})]}


def _assistant_read(path: str) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        _tool_call("read", {"path": path})]}


def _assistant_text(text: str) -> dict:
    return {"role": "assistant", "content": text, "tool_calls": []}


def _json_tree(nodes: list) -> str:
    return json.dumps({"outline": nodes}, ensure_ascii=False, indent=2)


def _root_leaf(title: str) -> dict:
    return {"id": None, "title": title, "description": f"{title}的说明",
            "attr": "技术", "content_mode": "ai-generate"}


def _leaf(title: str) -> dict:
    return {"id": None, "title": title, "description": f"{title}的说明",
            "content_mode": "ai-generate"}


def _to_branch(root: dict, leaf_titles: list) -> dict:
    base = {key: value for key, value in root.items()
            if key not in ("content_mode", "content_mode_note")}
    base["children"] = [_leaf(title) for title in leaf_titles]
    return base


def _deep_branch(root: dict) -> dict:
    """把已确认根叶节点扩展为**完整三级子树**（R162 三级叶子标准形态：
    一级下 3 个二级、二级下 3 个三级叶子）。

    ✅ R162：Schema minItems 3/3 + 完成前合规终检要求完整三级树，
    三级叶子（content_mode、无 children）即合法形态。
    """
    base = {key: value for key, value in root.items()
            if key not in ("content_mode", "content_mode_note")}
    children = []
    for j in range(1, 4):
        l3s = [{"id": None,
                "title": f"{root['title']}小节{j}{k}",
                "description": f"{root['title']}的小节说明",
                "content_mode": "ai-generate"}
               for k in range(1, 4)]
        children.append({"id": None,
                         "title": f"{root['title']}分节{j}",
                         "description": f"{root['title']}的分节说明",
                         "children": l3s})
    base["children"] = children
    return base


def _read_tree(ws: Path) -> list:
    return json.loads(
        (ws / OUTLINE_OUTPUT_FILE).read_text(encoding="utf-8"))["outline"]


def _read_json(ws: Path, name: str) -> dict:
    try:
        return json.loads((ws / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


async def _register(scheme_id: str) -> str:
    return await tr.register_task("outline", project_id="proj-tech-e2e",
                                  scheme_id=scheme_id)


# ── 六阶段桩 chat_fn ──────────────────────────────────────────────────────

def _tech_chain_chat(ws: Path, counter: dict):
    """技术评分六阶段桩：initial → 评分规划 → 叶子分配 → 子目录 → 叶子调整 → 审核。"""

    async def chat(messages, *, tools=None, tool_choice=None, **kw):
        counter["n"] += 1
        n = counter["n"]
        if n == 1:  # initial-outline：写三个评分大项一级目录（id null）
            return _assistant_write(_json_tree([
                _root_leaf("施工组织设计总体部署"),
                _root_leaf("关键工序质量保证措施"),
                _root_leaf("安全生产与文明施工")]), OUTLINE_OUTPUT_FILE)
        if n == 2:
            return _assistant_text("一级目录已写入")
        if n == 3:  # score-planning：读已确认根（真实 id）
            return _assistant_read(OUTLINE_OUTPUT_FILE)
        if n == 4:
            roots = _read_tree(ws)
            groups = {"groups": [
                {"requirement_id": f"R{i + 1}", "title": r["title"],
                 "description": r["description"],
                 "detail_points": [r["title"]]}
                for i, r in enumerate(roots)]}
            plan = {"allow_root_changes": False, "extra_titles": [],
                    "branches": [
                        {"root_id": r["id"], "root_title": r["title"],
                         "score_item_level": 1,
                         "mappings": [{"requirement_id": f"R{i + 1}",
                                       "target_title": r["title"]}]}
                        for i, r in enumerate(roots)]}
            return {"role": "assistant", "content": "", "tool_calls": [
                _tool_call("write", {"path": TECHNICAL_SCORE_GROUPS_FILE,
                                     "content": json.dumps(
                                         groups, ensure_ascii=False, indent=2)}),
                _tool_call("write", {"path": SCORE_DIRECTORY_PLAN_FILE,
                                     "content": json.dumps(
                                         plan, ensure_ascii=False, indent=2)}),
            ]}
        if n == 5:
            return _assistant_text("评分规划已写入")
        if n == 6:  # leaf_allocation：读宿主上下文
            return _assistant_read(LEAF_ALLOCATION_CONTEXT_FILE)
        if n == 7:
            ctx = _read_json(ws, LEAF_ALLOCATION_CONTEXT_FILE)
            branches = ctx.get("technical_branches") or []
            alloc = {"mode": "agent-decides", "target_ai_leaf_count": None,
                     "fixed_ai_leaf_count": 0, "allocatable_ai_leaf_count": None,
                     "allocations": [{"root_id": b.get("root_id")}
                                     for b in branches]}
            return _assistant_write(
                json.dumps(alloc, ensure_ascii=False, indent=2),
                LEAF_ALLOCATION_FILE)
        if n == 8:
            return _assistant_text("叶子分配已写入")
        if n == 9:  # children_generation：读已确认根
            return _assistant_read(OUTLINE_OUTPUT_FILE)
        if n == 10:
            roots = _read_tree(ws)
            tree = [_deep_branch(r) for r in roots]
            return _assistant_write(_json_tree(tree), OUTLINE_OUTPUT_FILE)
        if n == 11:
            return _assistant_text("完整目录已写入")
        if n == 12:  # leaf_adjustment：读当前完整目录
            return _assistant_read(OUTLINE_OUTPUT_FILE)
        if n == 13:
            tree = _read_tree(ws)
            return _assistant_write(_json_tree(tree), OUTLINE_OUTPUT_FILE)
        if n == 14:
            return _assistant_text("叶子调整完成")
        if n == 15:  # outline_review：审核通过
            return _assistant_write(
                json.dumps(_REVIEW_PASSED, ensure_ascii=False, indent=2),
                OUTLINE_REVIEW_FILE)
        if n == 16:
            return _assistant_text("最终审核完成")
        raise AssertionError(f"技术单链不应发起第 {n} 次模型调用")

    return chat


# ── 用例 ───────────────────────────────────────────────────────────────────

async def test_技术评分单链_六阶段端到端(db_conn, tmp_path):
    """no_technical_score_mode=False 走完六阶段，产出完整目录与审核结论。"""
    task_id = await _register("scheme-tech-chain")
    ws = tmp_path / "ws_tech_chain"
    counter = {"n": 0}
    chat = _tech_chain_chat(ws, counter)
    holds: list = []
    events: list = []

    async def hold(tree):
        holds.append(list(tree))

    async def emit_event(event):
        events.append(event)

    outcome = await run_outline_agent(
        task_id=task_id, scheme=SCHEME, context=_CONTEXT,
        workspace_dir=str(ws), chat_fn=chat,
        hold_partial=hold, emit_event=emit_event,
        no_technical_score_mode=False,
    )
    # 六阶段走完 → 完整目录 + 审核结论 + 终态 stage。
    assert outcome.stage == "outline_review"
    assert outcome.outline, "应产出非空完整目录"
    assert outcome.review.get("status") == "passed"
    # 每个一级目录都扩展了子目录（父节点形态）。
    assert all(node.get("children") for node in outcome.outline)
    # 宿主预置评分项来源：technical-score-groups.json + 技术评分信息.md。
    groups = _read_json(ws, TECHNICAL_SCORE_GROUPS_FILE)
    assert len(groups.get("groups") or []) == 3
    assert (ws / "技术评分信息.md").read_text(encoding="utf-8").strip()
    # 审核上下文含评分规划（技术模式注入 scoreDirectoryPlan）。
    review_ctx = _read_json(ws, OUTLINE_REVIEW_CONTEXT_FILE)
    assert "scoreDirectoryPlan" in review_ctx


def test_评分规划校验_锁死未知根id():
    """score-planning 校验：分支 root_id 非已确认根 id → blocking（locked_roots 生效）。"""
    schemas = build_json_validation_schemas(
        no_technical_score_mode=False, standalone_technical=False)
    validators = build_output_validators(schemas)
    # 已确认根（真实 UUID v4 身份）。
    confirmed_root_id = str(uuid.uuid4())
    confirmed_root = {"id": confirmed_root_id, "title": "施工组织设计总体部署",
                      "description": "施工组织设计总体部署的说明",
                      "attr": "技术", "content_mode": "ai-generate"}
    groups = {"groups": [{"requirement_id": "R1", "title": "施工组织设计总体部署",
                          "description": "施工组织设计总体部署的说明",
                          "detail_points": ["施工组织设计总体部署"]}]}
    bad_plan = {"allow_root_changes": False, "extra_titles": [],
                "branches": [{"root_id": str(uuid.uuid4()),
                              "root_title": "施工组织设计总体部署",
                              "score_item_level": 1,
                              "mappings": [{"requirement_id": "R1",
                                            "target_title": "施工组织设计总体部署"}]}]}
    files = {
        TECHNICAL_SCORE_GROUPS_FILE: json.dumps(groups, ensure_ascii=False),
        SCORE_DIRECTORY_PLAN_FILE: json.dumps(bad_plan, ensure_ascii=False),
    }
    meta = StageMeta(workflow_stage="score-planning",
                     read_file=lambda name: files.get(name, ""),
                     known_node_ids={confirmed_root_id},
                     locked_roots=[confirmed_root])
    report = validate_stage_outputs(
        {"output_file": OUTLINE_OUTPUT_FILE,
         "output_content": _json_tree([confirmed_root]),
         "workspace": "mem"},
        meta, validators)
    issues = report["issues"]
    assert issues, "未知 root_id 应产生 blocking issue"
    assert any("root_id" in (issue.get("path") or "")
               for issue in issues)


def test_评分项来源_取自requirements():
    """宿主从 requirements 预置评分分组（R1/R2/R3 逐条对应，非空字段）。"""
    groups = _build_technical_score_groups(_CONTEXT["requirements_text"])
    assert [g["requirement_id"] for g in groups] == ["R1", "R2", "R3"]
    assert all(g["title"] and g["detail_points"] and g["description"]
               for g in groups)
