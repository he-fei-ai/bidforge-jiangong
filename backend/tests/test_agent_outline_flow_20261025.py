"""接线阶段 —— ``outline_agent_flow`` 单链端到端桩用例。

落实《目录生成工具修正移植_实施报告_P0P4_20261025.md》第六章建议 3/4 与
遗留②⑤回归：

- 单链：initial-outline accept → 一级目录自动确认 → children_generation
  accept → outline_review 审核阶段 accept → ``outline_agent_event_stream``
  产出 legacy 同形状 completed 事件（checkpoint → finish_task → yield 终态
  顺序；含审核结论文案与 review 结论回传）；
- stop 兜底：停止命中 ``OutlineAgentStopped`` → stopped 事件带 partial 成果
  与 checkpoint，任务落终态；
- 提交止损：持续 blocking → normal / change-strategy / minimum 三档修复 →
  ``StageStoppedError`` 原样上抛（交 SSE 外层 error 分支收尾）；
- resume 断点：注入 ``initial_stage`` / ``initial_stage_index`` / ``messages``
  从 children_generation 直入，工作区已确认目录恢复 ``known_ids``（根 id 保真）；
- checkpoint 写失败 → ``degraded`` + ``checkpoint_failed``（R89 S2 降级口径）；
- 遗留②：CONTENT_MODES / OUTLINE_ATTRIBUTES / ATTRIBUTE_CONTENT_MODE_RULES
  与易标 L26–33 逐字一致；
- 遗留⑤：bash 工具名消歧（构造器逐字保真、出口统一替换）。

桩策略：``chat_fn`` 驱动真实 ``run_agent_loop`` + P1 工具表（write 真实落盘）+
``stage_runner`` 状态机 + P2 校验，仅模型侧打桩；任务注册走 conftest
``db_conn`` 内存库（register_task / update_progress / finish_task 同库落盘）。
"""
import json
import re
import uuid
from pathlib import Path

import app.services.ai.task_registry as tr
import pytest
from app.services.agent.outline_agent_flow import (
    build_outline_review_context,
    count_ai_leaves,
    disambiguate_prompt_tools,
    outline_agent_event_stream,
    review_status_message,
    run_outline_agent,
)
from app.services.agent.outline_identity import NODE_ID_PATTERN
from app.services.agent.stage_prompts import (
    create_initial_prompt,
    create_no_technical_score_review_prompt,
    create_outline_review_prompt,
)
from app.services.agent.stage_runner import STOP_BLOCKING_PREFIX, StageStoppedError
from app.services.agent.validation import (
    ATTRIBUTE_CONTENT_MODE_RULES,
    CONTENT_MODES,
    OUTLINE_ATTRIBUTES,
    OUTLINE_OUTPUT_FILE,
    OUTLINE_REVIEW_CONTEXT_FILE,
    OUTLINE_REVIEW_FILE,
)

SCHEME = {"id": "scheme-agent-e2e", "name": "接线端到端方案", "type": "technical"}

#: 审核通过桩结论（合法 OUTLINE_REVIEW_SCHEMA 最小体；test1/test2 共用）。
_REVIEW_PASSED = {"status": "passed", "issues": [], "user_feedback": "",
                  "summary": "未发现需要修复的问题"}
CONTEXT = {
    "requirements_text": "响应文件要求：按招标文件第三章组织目录。",
    "project_brief": "项目概述：某市智慧城市基础平台建设。",
}


# ── 桩与素材 ──────────────────────────────────────────────────────────────

def _tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _assistant_write(content: str, path: str = OUTLINE_OUTPUT_FILE) -> dict:
    """桩应答：调用 write 工具把 ``content`` 落盘到指定产物文件。"""
    return {"role": "assistant", "content": "", "tool_calls": [
        _tool_call("write", {"path": path, "content": content})]}


def _assistant_text(text: str) -> dict:
    """桩应答：纯文本收束（无 tool_calls → run_agent_loop 正常完成）。"""
    return {"role": "assistant", "content": text, "tool_calls": []}


def _json_tree(nodes: list) -> str:
    return json.dumps({"outline": nodes}, ensure_ascii=False, indent=2)


def _root_leaf(title: str) -> dict:
    """初版一级目录（根叶节点，过 ROOT_NODE_SCHEMA）。"""
    return {"id": None, "title": title, "description": f"{title}的说明",
            "attr": "技术", "content_mode": "ai-generate"}


def _leaf(title: str) -> dict:
    """二级叶子节点（非根，无 attr）。"""
    return {"id": None, "title": title, "description": f"{title}的说明",
            "content_mode": "ai-generate"}


def _to_branch(root: dict, leaf_titles: list) -> dict:
    """把已确认根叶节点扩展为父节点：剥离 content_mode、补 children。"""
    base = {key: value for key, value in root.items()
            if key not in ("content_mode", "content_mode_note")}
    base["children"] = [_leaf(title) for title in leaf_titles]
    return base


def _read_tree(ws: Path) -> list:
    return json.loads(
        (ws / OUTLINE_OUTPUT_FILE).read_text(encoding="utf-8"))["outline"]


async def _register(scheme_id: str) -> str:
    return await tr.register_task("outline", project_id="proj-agent-e2e",
                                  scheme_id=scheme_id)


async def _consume_stream(events: list, task_id: str, scheme_id: str,
                          ws: Path, chat, **run_kwargs) -> None:
    """消费 ``outline_agent_event_stream``，事件追加进 ``events``。"""
    async for event in outline_agent_event_stream(
            task_id=task_id, scheme_id=scheme_id, scheme=SCHEME,
            context=CONTEXT, workspace_dir=str(ws), chat_fn=chat,
            **run_kwargs):
        events.append(event)


async def _db_row(db_conn, task_id: str, column: str = "status, message"):
    cur = await db_conn.execute(
        f"SELECT {column} FROM task_registry WHERE id=?", (task_id,))
    return await cur.fetchone()


def _single_chain_chat(ws: Path, counter: dict, snapshots: list | None = None):
    """单链桩 chat_fn：写一级目录 → 收束 → 补 children → 收束 → 审核通过 → 收束。"""

    async def chat(messages, *, tools=None, tool_choice=None, **kw):
        counter["n"] += 1
        if snapshots is not None:
            snapshots.append([dict(message) for message in messages])
        if counter["n"] == 1:
            return _assistant_write(_json_tree([
                _root_leaf("项目概述"), _root_leaf("实施方案")]))
        if counter["n"] == 2:
            return _assistant_text("一级目录已写入")
        if counter["n"] == 3:
            tree = [_to_branch(root, ["现状分析", "建设内容"])
                    for root in _read_tree(ws)]
            return _assistant_write(_json_tree(tree))
        if counter["n"] == 4:
            return _assistant_text("完整目录已写入")
        if counter["n"] == 5:
            return _assistant_write(json.dumps(
                _REVIEW_PASSED, ensure_ascii=False, indent=2),
                path=OUTLINE_REVIEW_FILE)
        if counter["n"] == 6:
            return _assistant_text("最终审核完成")
        raise AssertionError(f"单链不应发起第 {counter['n']} 次模型调用")

    return chat


# ── 单链：initial-outline → children_generation ────────────────────────────

async def test_单链_业务层_initial到children_generation全流程(db_conn, tmp_path):
    task_id = await _register("scheme-chain-biz")
    ws = tmp_path / "ws_chain_biz"
    snapshots: list = []
    chat = _single_chain_chat(ws, {"n": 0}, snapshots)

    holds: list = []
    activities: list = []
    events: list = []
    progress: list = []

    async def hold(tree):
        holds.append(list(tree))

    async def report(value, message):
        progress.append((round(float(value), 2), message))

    outcome = await run_outline_agent(
        task_id=task_id, scheme=SCHEME, context=CONTEXT,
        workspace_dir=str(ws), chat_fn=chat,
        hold_partial=hold, report_progress=report,
        emit_activity=lambda item: activities.append(dict(item)),
        emit_event=lambda item: events.append(dict(item)),
    )

    # 阶段推进与终态产物（含 outline_review 审核阶段）
    assert outcome.stage == "outline_review"
    assert outcome.result is not None
    assert outcome.result.prompt_calls == 3
    assert outcome.result.stage_index == 2
    assert outcome.review == _REVIEW_PASSED
    assert outcome.result.submission_retries == 0
    outline = outcome.outline
    assert [node["title"] for node in outline] == ["项目概述", "实施方案"]
    for node in outline:
        assert re.fullmatch(NODE_ID_PATTERN, node["id"])
        assert "content_mode" not in node
        assert len(node["children"]) == 2
        assert all(re.fullmatch(NODE_ID_PATTERN, child["id"])
                   for child in node["children"])
        assert all(child["content_mode"] == "ai-generate"
                   for child in node["children"])
    assert [node["number"] for node in outline] == ["1", "2"]
    assert [child["number"] for child in outline[0]["children"]] == ["1.1", "1.2"]

    # hold 三段式：一级目录确认成果 → children 成果 → 审核后成果
    assert len(holds) == 3
    assert all("children" not in item for item in holds[0])
    assert all(len(item["children"]) == 2 for item in holds[1])
    assert all(len(item["children"]) == 2 for item in holds[2])
    assert holds[1] == holds[2]

    # 活动与监控事件（阶段流转形状）
    assert activities[0]["kind"] == "start"
    assert activities[0]["workflow_stage"] == "initial-outline"
    cont = [item for item in activities
            if item.get("kind") == "workflow.continue"]
    assert [item["workflow_stage"] for item in cont] == [
        "children_generation", "outline_review"]
    assert cont[0]["message"] == "一级目录已确认，开始生成完整目录"
    assert cont[1]["message"] == "Agent 正在审核并修复目录"
    kinds = [(item.get("type"), item.get("workflow_stage")) for item in events]
    assert ("task_output", "initial-outline") in kinds
    assert ("task_input", "children_generation") in kinds
    assert ("task_output", "children_generation") in kinds
    assert ("task_input", "outline_review") in kinds

    # 进度单调：0.10 → 0.45 → 0.88（进入审核）→ 0.95（审核通过）→ 1.0
    assert [value for value, _ in progress] == [0.1, 0.45, 0.88, 0.95, 1.0]

    # 提示词轮次：初版任务指示 → children 规则 → 审核规则（历史消息原样累积）
    assert len(snapshots) == 6
    assert len(snapshots[0]) == 1
    assert "严格按照响应文件要求.md 组织一级目录" in snapshots[0][0]["content"]
    last_user = [m for m in snapshots[2] if m["role"] == "user"][-1]
    assert ("只扩展 attr=技术 且 content_mode=ai-generate 的一级目录"
            in last_user["content"])
    review_user = [m for m in snapshots[4] if m["role"] == "user"][-1]
    assert "执行最终审核" in review_user["content"]
    # 宿主确定性审核结果随材料下发（outline-review-context.json 内容内嵌）
    assert OUTLINE_REVIEW_CONTEXT_FILE in review_user["content"]
    assert '"current_ai_generate"' in review_user["content"]
    # 工作区产物：审核上下文与审核结论文件均已落盘且结构正确
    review_ctx = json.loads(
        (ws / OUTLINE_REVIEW_CONTEXT_FILE).read_text(encoding="utf-8"))
    assert review_ctx["structure"]["valid"] is True
    assert review_ctx["leaf_count"]["target"] is None
    assert (ws / OUTLINE_REVIEW_FILE).is_file()


async def test_单链_事件流_completed且检查点先于finish_task(db_conn, tmp_path):
    task_id = await _register("scheme-chain-stream")
    ws = tmp_path / "ws_chain_stream"
    chat = _single_chain_chat(ws, {"n": 0})

    holder: dict = {}
    ckpts: list = []
    seen: dict = {}

    async def save_checkpoint(tid, payload):
        seen["task_alive"] = tid in tr._tasks
        ckpts.append((tid, dict(payload)))

    events: list = []
    await _consume_stream(events, task_id, "scheme-chain-stream", ws, chat,
                          partial_holder=holder, save_checkpoint=save_checkpoint)

    final = events[-1]
    assert final["event"] == "completed"
    assert final["task_id"] == task_id
    assert final["review"] == _REVIEW_PASSED
    assert "checkpoint_failed" not in final
    assert len(final["outline"]) == 2
    assert all(len(node["children"]) == 2 for node in final["outline"])

    # 终态顺序契约：checkpoint → finish_task → yield
    # （checkpoint 执行时任务尚在 _tasks；流结束时 finish_task 已 pop）
    assert seen["task_alive"] is True
    assert len(ckpts) == 1
    assert ckpts[0][0] == task_id and ckpts[0][1]["event"] == "completed"
    assert task_id not in tr._tasks
    row = await _db_row(db_conn, task_id, "status")
    assert row[0] == "completed"

    # SSE 桥事件形状：进度携带运行统计且单调不倒退
    assert holder.get("outline") and len(holder["outline"]) == 2
    pvals = [event["progress"] for event in events
             if isinstance(event.get("progress"), (int, float))]
    assert pvals == [0.1, 0.45, 0.88, 0.95, 1.0]
    assert all(b >= a for a, b in zip(pvals, pvals[1:]))
    assert any(event.get("message") == "目录生成 Agent 开始执行"
               for event in events)
    assert not any(event.get("event") == "stopped" for event in events)


# ── stop 兜底 ─────────────────────────────────────────────────────────────

async def test_停止兜底_事件流收尾stopped带partial与checkpoint(db_conn, tmp_path):
    task_id = await _register("scheme-chain-stop")
    ws = tmp_path / "ws_chain_stop"
    counter = {"n": 0}

    async def chat(messages, *, tools=None, tool_choice=None, **kw):
        counter["n"] += 1
        if counter["n"] == 1:
            return _assistant_write(_json_tree([
                _root_leaf("项目概述"), _root_leaf("实施方案")]))
        if counter["n"] == 2:
            # 写盘落定后、children 阶段开跑前命中用户停止
            tr.request_control(task_id, "stop")
            return _assistant_text("一级目录已写入")
        raise AssertionError("停止后不应再发起模型调用")

    holder: dict = {}
    ckpts: list = []
    events: list = []

    async def _ckpt(tid, payload):
        ckpts.append((tid, dict(payload)))

    await _consume_stream(events, task_id, "scheme-chain-stop", ws, chat,
                          partial_holder=holder,
                          save_checkpoint=_ckpt)

    final = events[-1]
    assert final["event"] == "stopped"
    assert final["task_id"] == task_id
    assert final.get("partial") is True
    # partial 成果 = 已确认的一级目录（children 未完成）
    assert [node["title"] for node in final["outline"]] == ["项目概述", "实施方案"]
    assert all("children" not in node for node in final["outline"])
    assert holder.get("outline") == final["outline"]

    assert len(ckpts) == 1 and ckpts[0][1]["event"] == "stopped"
    assert task_id not in tr._tasks
    row = await _db_row(db_conn, task_id)
    assert row[0] == "stopped" and row[1] == "用户已停止"
    assert not any(event.get("event") == "completed" for event in events)
    pvals = [event["progress"] for event in events
             if isinstance(event.get("progress"), (int, float))]
    assert 0.45 in pvals  # 一级目录确认进度已发出，停止发生在 children 起跑前


# ── 三档提交止损 ──────────────────────────────────────────────────────────

async def test_三档提交止损_持续blocking上抛StageStoppedError(db_conn, tmp_path):
    task_id = await _register("scheme-chain-stoploss")
    ws = tmp_path / "ws_chain_stoploss"
    counter = {"n": 0}

    async def chat(messages, *, tools=None, tool_choice=None, **kw):
        # 始终不落盘 → outline.json 保持空壳 → blocking 持续
        counter["n"] += 1
        return _assistant_text("无法消除阻塞问题")

    events: list = []
    with pytest.raises(StageStoppedError) as excinfo:
        await _consume_stream(events, task_id, "scheme-chain-stoploss",
                              ws, chat)
    error = excinfo.value
    assert str(error).startswith(STOP_BLOCKING_PREFIX)
    assert "未写入或内容为空" in str(error)
    assert error.issues and error.agent_validation_failed is True

    # normal → change-strategy → minimum → stop：恰好 4 轮提示、3 轮修复
    assert counter["n"] == 4
    repairs = [event for event in events
               if "正在自动修复" in str(event.get("message") or "")]
    assert len(repairs) == 3
    # 建议4：止损显式上报 —— 活动经 SSE 桥转为 progress 事件，恰好 1 条
    stops = [event for event in events
             if str(event.get("message") or "").startswith(STOP_BLOCKING_PREFIX)]
    assert len(stops) == 1
    assert events[0]["event"] == "progress" and events[0].get("progress") == 0.1
    assert not any(event.get("event") in ("completed", "stopped")
                   for event in events)


# ── resume 断点 ───────────────────────────────────────────────────────────

async def test_resume断点_直入children_generation且根id保真(db_conn, tmp_path):
    task_id = await _register("scheme-chain-resume")
    ws = tmp_path / "ws_chain_resume"
    ws.mkdir(parents=True)
    root_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    seeded = {"outline": [
        {"id": root_ids[0], "number": "1", "title": "项目概述",
         "description": "背景与范围", "attr": "技术",
         "content_mode": "ai-generate"},
        {"id": root_ids[1], "number": "2", "title": "实施方案",
         "description": "路线与计划", "attr": "技术",
         "content_mode": "ai-generate"},
    ]}
    (ws / OUTLINE_OUTPUT_FILE).write_text(
        json.dumps(seeded, ensure_ascii=False, indent=2), encoding="utf-8")

    prior = [{"role": "user", "content": "断点前提示词"},
             {"role": "assistant", "content": "断点前应答"}]
    snapshots: list = []
    counter = {"n": 0}

    async def chat(messages, *, tools=None, tool_choice=None, **kw):
        counter["n"] += 1
        snapshots.append([dict(message) for message in messages])
        if counter["n"] == 1:
            tree = [_to_branch(root, ["现状分析", "建设内容"])
                    for root in _read_tree(ws)]
            return _assistant_write(_json_tree(tree))
        if counter["n"] == 2:
            return _assistant_text("断点续跑完成")
        if counter["n"] == 3:
            return _assistant_write(json.dumps(
                _REVIEW_PASSED, ensure_ascii=False, indent=2),
                path=OUTLINE_REVIEW_FILE)
        if counter["n"] == 4:
            return _assistant_text("断点续跑审核完成")
        raise AssertionError(f"断点续跑不应发起第 {counter['n']} 次模型调用")

    activities: list = []
    outcome = await run_outline_agent(
        task_id=task_id, scheme=SCHEME, context={},
        workspace_dir=str(ws), chat_fn=chat,
        initial_stage="children_generation", initial_stage_index=1,
        messages=list(prior),
        emit_activity=lambda item: activities.append(dict(item)),
    )

    # 直入断点阶段：children accept 后继续走完审核阶段，根节点 id 与断点前一致
    assert outcome.stage == "outline_review"
    assert outcome.result is not None
    assert outcome.result.prompt_calls == 2
    assert outcome.result.stage_index == 2
    assert [node["id"] for node in outcome.outline] == root_ids
    assert [node["title"] for node in outcome.outline] == ["项目概述", "实施方案"]
    assert all(len(node["children"]) == 2 for node in outcome.outline)

    assert activities[0]["kind"] == "start"
    assert activities[0]["workflow_stage"] == "children_generation"

    # 注入的既有会话保留，首个提示词即 children 规则；次阶段即审核规则
    assert snapshots[0][:2] == prior
    assert ("只扩展 attr=技术 且 content_mode=ai-generate 的一级目录"
            in snapshots[0][-1]["content"])
    review_user = [m for m in snapshots[2] if m["role"] == "user"][-1]
    assert "执行最终审核" in review_user["content"]


# ── checkpoint 写失败降级（R89 S2）────────────────────────────────────────

async def test_checkpoint写失败_降级degraded带checkpoint_failed(db_conn, tmp_path):
    task_id = await _register("scheme-chain-degraded")
    ws = tmp_path / "ws_chain_degraded"
    chat = _single_chain_chat(ws, {"n": 0})

    async def bad_checkpoint(tid, payload):
        raise RuntimeError("checkpoint 磁盘写入失败")

    events: list = []
    await _consume_stream(events, task_id, "scheme-chain-degraded", ws, chat,
                          save_checkpoint=bad_checkpoint)

    final = events[-1]
    assert final["event"] == "completed"
    assert final["checkpoint_failed"] is True
    assert len(final["outline"]) == 2
    assert task_id not in tr._tasks
    row = await _db_row(db_conn, task_id)
    assert row[0] == "degraded"
    assert "未能写入检查点" in row[1]


# ── 遗留② 枚举逐字回归 ───────────────────────────────────────────────────

def test_遗留2_内容模式与属性枚举逐字对齐易标():
    # 易标 outlineGenerationTaskV2.cjs L27 逐字
    assert CONTENT_MODES == ["ai-generate", "template-fill",
                             "directory-generate", "manual-fill", "other"]
    # 易标 L26 逐字
    assert OUTLINE_ATTRIBUTES == ["通用", "商务/资信", "技术", "其他",
                                  "目录", "报价", "业绩"]
    # 易标 L28–33 逐字
    assert ATTRIBUTE_CONTENT_MODE_RULES == {
        "商务/资信": "template-fill",
        "目录": "directory-generate",
        "报价": "manual-fill",
        "业绩": "template-fill",
    }


# ── 遗留⑤ bash 工具名消歧回归 ─────────────────────────────────────────────

def test_遗留5_bash工具名消歧_构造器保真与出口替换():
    raw_prompts = [
        create_no_technical_score_review_prompt(
            target_leaf_count=None, actual_leaf_count=3, input_files=[]),
        create_outline_review_prompt(
            target_leaf_count=None, actual_leaf_count=3,
            allow_root_changes=False, input_files=[]),
    ]
    for raw in raw_prompts:
        assert "grep、read 或 bash" in raw  # 构造器出厂逐字保真
        fixed = disambiguate_prompt_tools(raw)
        assert "bash" not in fixed
        assert "grep、read 或 find" in fixed
        assert fixed == raw.replace("grep、read 或 bash", "grep、read 或 find")
    # 无目标 token 的提示词逐字透传
    plain = create_initial_prompt(
        "任务指示原文", standalone_technical=False,
        no_technical_score_mode=True)
    assert "任务指示原文" in plain
    assert disambiguate_prompt_tools(plain) == plain
    assert "bash" not in plain


# ── 审核上下文纯函数（易标 L459-503 / L562-588 移植行为实证）────────────────

def test_审核上下文_纯函数与易标结构一致():
    tree = {"outline": [
        {"id": "r1", "title": "A", "children": [
            {"id": "c1", "title": "A1", "content_mode": "ai-generate"},
            {"id": "c2", "title": "A2", "content_mode": "manual-fill"},
        ]},
        {"id": "r2", "title": "B", "children": [
            {"id": "c3", "title": "B1", "content_mode": "ai-generate"},
        ]},
    ]}
    assert count_ai_leaves(tree["outline"]) == 2
    ctx = build_outline_review_context(outline=tree, target_leaf_count=None)
    assert ctx["leaf_count"]["target"] is None
    assert ctx["leaf_count"]["current_ai_generate"] == 2
    assert ctx["leaf_count"]["within_acceptable_range"] is True
    assert ctx["leaf_count"]["by_content_mode"]["ai-generate"] == 2
    assert ctx["leaf_count"]["by_content_mode"]["manual-fill"] == 1
    # 结构：max_depth=2；B 只有 1 个子节点 → single_child_nodes 命中、valid=False
    assert ctx["structure"]["max_depth"] == 2
    assert ctx["structure"]["parent_count"] == 2
    assert ctx["structure"]["single_child_nodes"] == [
        {"id": "r2", "title": "B", "child_count": 1}]
    assert ctx["structure"]["valid"] is False
    # 合法结构（无单子节点、叶子模式全部合法）→ valid=True
    ok_tree = {"outline": [
        {"id": "r1", "title": "A", "children": [
            {"id": "c1", "title": "A1", "content_mode": "ai-generate"},
            {"id": "c2", "title": "A2", "content_mode": "ai-generate"},
        ]},
    ]}
    ok_ctx = build_outline_review_context(outline=ok_tree,
                                          target_leaf_count=None)
    assert ok_ctx["structure"]["valid"] is True
    # target_leaf_count 数值档：可接受范围 ±2（易标 L567-568）
    num_ctx = build_outline_review_context(outline=ok_tree,
                                           target_leaf_count=5)
    assert num_ctx["leaf_count"]["acceptable_min"] == 3
    assert num_ctx["leaf_count"]["acceptable_max"] == 7
    assert num_ctx["leaf_count"]["within_acceptable_range"] is False  # 2 < 3


def test_审核状态映射文案与易标逐字():
    # 易标 L1317-1323 逐字
    assert review_status_message("passed") == "目录审核通过"
    assert (review_status_message("simple_fix")
            == "目录审核完成，Agent 已自动微调简单问题")
    assert (review_status_message("user_feedback")
            == "目录审核完成，已按用户反馈修复")
    # user_refuse / 未登记状态 → 兜底分支（易标 else 分支）
    assert (review_status_message("user_refuse")
            == "目录审核完成，用户选择保留当前目录")
    assert review_status_message(None) == "目录审核完成，用户选择保留当前目录"
