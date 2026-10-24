"""P2 提交策略 / 阶段产物校验 / 目录身份用例（可直接放入 backend/tests/ 运行）。

覆盖：
- 提交策略三档推进（normal → change-strategy → minimum → stop/accept）、
  进展判定（blocking 下降 / 无 blocking 时 quality 下降 / stalled 归零）；
- 三档修复提示文案逐字断言；
- ``validate_json_output`` 三类产出与 message 逐字；
- ``STAGE_OUTPUT_FILES`` 6 阶段与文件常量逐字（连字符/下划线差异）；
- ``accept_agent_outline`` 身份沿用 / UUID 分配 / 未知与重复 ID 报错 /
  ``number_outline`` 编号不改身份；
- ``validate_outline_output`` 结构与身份 issue 文案、
  ``validate_stage_outputs`` 阶段汇总与评分规划 ``root_id`` 回填。
"""
import json
import re
import uuid

import pytest
from app.services.agent.outline_identity import (
    NODE_ID_PATTERN,
    accept_agent_outline,
    collect_outline_ids,
    number_outline,
)
from app.services.agent.submission_policy import (
    build_submission_repair_prompt,
    create_submission_policy,
)
from app.services.agent.validation import (
    LEAF_ALLOCATION_CONTEXT_FILE,
    LEAF_ALLOCATION_FILE,
    OUTLINE_OUTPUT_FILE,
    OUTLINE_REVIEW_CONTEXT_FILE,
    OUTLINE_REVIEW_FILE,
    SCORE_DIRECTORY_PLAN_FILE,
    STAGE_OUTPUT_FILES,
    TECHNICAL_SCORE_GROUPS_FILE,
    StageMeta,
    validate_json_output,
    validate_outline_output,
    validate_stage_outputs,
)

# ── 公共样例 ─────────────────────────────────────────────────────────────
BLOCKING_A = {"severity": "blocking", "file": "outline.json",
              "path": "/outline/0", "message": "阻塞问题A"}
BLOCKING_B = {"severity": "blocking", "file": "outline.json",
              "path": "/outline/1", "message": "阻塞问题B"}
QUALITY = {"severity": "quality", "file": "outline.json",
           "path": "/outline/2", "message": "质量瑕疵"}

ROOT_ID = "11111111-1111-4111-8111-111111111111"
KEPT_ROOT = "44444444-4444-4444-8444-444444444444"


def make_report(*issues: dict) -> dict:
    return {"issues": list(issues)}


def quality_report(n: int) -> dict:
    return {"issues": [{"severity": "quality", "file": "outline.json",
                        "message": f"瑕疵{i}"} for i in range(n)]}


# ── 提交策略：三档推进与止损 ─────────────────────────────────────────────

def test_三档推进_修复到最低目标后止损():
    policy = create_submission_policy()
    assert policy.inspect(make_report(BLOCKING_A, QUALITY)) == {
        "action": "repair", "mode": "normal", "attempt": 1}
    # 连续无进展：blocking 与 remaining 均未下降
    assert policy.inspect(make_report(BLOCKING_A, QUALITY)) == {
        "action": "repair", "mode": "change-strategy", "attempt": 2}
    assert policy.inspect(make_report(BLOCKING_A, QUALITY)) == {
        "action": "repair", "mode": "minimum", "attempt": 3}
    assert policy.attempts == 3
    # minimum 档之后只看 blocking：仍有 → stop 兜底，修复不再累加
    assert policy.inspect(make_report(BLOCKING_A)) == {"action": "stop"}
    assert policy.inspect(make_report(BLOCKING_A, QUALITY)) == {"action": "stop"}
    assert policy.attempts == 3


def test_最低目标档之后无阻塞直接验收():
    policy = create_submission_policy()
    policy.inspect(make_report(BLOCKING_A))
    policy.inspect(make_report(BLOCKING_A))
    assert policy.inspect(make_report(BLOCKING_A))["mode"] == "minimum"
    # minimumAttempted 后 blocking 清零 → accept
    assert policy.inspect(make_report(QUALITY)) == {"action": "accept"}


def test_无问题直接验收_不计入修复次数():
    policy = create_submission_policy()
    assert policy.inspect({"issues": []}) == {"action": "accept"}
    assert policy.attempts == 0


def test_阻塞数下降视为进展_停滞轮数归零():
    policy = create_submission_policy()
    assert policy.inspect(make_report(BLOCKING_A, BLOCKING_B))["attempt"] == 1
    assert policy.inspect(make_report(BLOCKING_A, BLOCKING_B)) == {
        "action": "repair", "mode": "change-strategy", "attempt": 2}
    # blocking 2 → 1 视为 improved，stalled 归零（此处若未归零会给出 minimum）
    assert policy.inspect(make_report(BLOCKING_A)) == {
        "action": "repair", "mode": "normal", "attempt": 3}
    assert policy.inspect(make_report(BLOCKING_A)) == {
        "action": "repair", "mode": "change-strategy", "attempt": 4}


def test_无阻塞时质量剩余下降也算进展():
    policy = create_submission_policy()
    assert policy.inspect(quality_report(6)) == {
        "action": "repair", "mode": "normal", "attempt": 1}
    assert policy.inspect(quality_report(6)) == {
        "action": "repair", "mode": "change-strategy", "attempt": 2}
    # blocking 均为 0 且 remaining 6 → 4 视为 improved
    assert policy.inspect(quality_report(4)) == {
        "action": "repair", "mode": "normal", "attempt": 3}
    assert policy.inspect(quality_report(4)) == {
        "action": "repair", "mode": "change-strategy", "attempt": 4}


def test_连续两次无进展且无阻塞则验收():
    policy = create_submission_policy()
    policy.inspect(quality_report(5))
    policy.inspect(quality_report(5))
    # stalled >= 2 且无 blocking → accept（不进入 minimum）
    assert policy.inspect(quality_report(5)) == {"action": "accept"}
    assert policy.attempts == 2


def test_progress字段优先于质量计数且缺省回退():
    policy = create_submission_policy()
    assert policy.inspect({"issues": [QUALITY], "progress": 9}) == {
        "action": "repair", "mode": "normal", "attempt": 1}
    assert policy.inspect({"issues": [QUALITY], "progress": 9}) == {
        "action": "repair", "mode": "change-strategy", "attempt": 2}
    # progress 9 → 3 即使 quality 计数不变也视为 improved
    assert policy.inspect({"issues": [QUALITY], "progress": 3}) == {
        "action": "repair", "mode": "normal", "attempt": 3}

    # progress 为 None 视同缺省，回退到 quality 计数（独立实例）
    fresh = create_submission_policy()
    assert fresh.inspect({"issues": [QUALITY, QUALITY, QUALITY],
                          "progress": None}) == {
        "action": "repair", "mode": "normal", "attempt": 1}
    assert fresh.inspect({"issues": [QUALITY, QUALITY, QUALITY]}) == {
        "action": "repair", "mode": "change-strategy", "attempt": 2}


# ── 提交修复提示：三档文案逐字 ───────────────────────────────────────────

def test_修复提示_正常档逐字():
    prompt = build_submission_repair_prompt(
        make_report(BLOCKING_A, QUALITY),
        {"action": "repair", "mode": "normal", "attempt": 2},
        "业务提示：文件与工具细节",
    )
    assert prompt.split("\n\n") == [
        "业务提示：文件与工具细节",
        "本轮待修复问题：\n1. 阻塞问题A\n2. 质量瑕疵",
        "这是本阶段第 2 次提交修复。",
        "保留已完成成果，根据当前问题修复后重新提交。",
    ]


def test_修复提示_换方法档逐字():
    prompt = build_submission_repair_prompt(
        make_report(BLOCKING_A, QUALITY),
        {"action": "repair", "mode": "change-strategy", "attempt": 3},
        "业务提示",
    )
    parts = prompt.split("\n\n")
    # 非 minimum 档列出全部问题
    assert parts[1] == "本轮待修复问题：\n1. 阻塞问题A\n2. 质量瑕疵"
    assert parts[2] == "这是本阶段第 3 次提交修复。"
    assert parts[3] == (
        "上一轮修复未取得进展。先核对最新产物并定位原方法未解决问题的原因，"
        "再更换具体处理方法；不要重复相同操作。"
        "可使用当前可用工具和命令验证新的处理方法，修复后重新提交。"
    )


def test_修复提示_最低目标档只列阻塞并带自定义目标():
    prompt = build_submission_repair_prompt(
        {"issues": [BLOCKING_A, QUALITY],
         "minimumGoal": "修正层级并补齐缺失章节"},
        {"action": "repair", "mode": "minimum", "attempt": 5},
        "",
    )
    parts = prompt.split("\n\n")
    # 空业务提示被 filter(Boolean) 丢弃；minimum 档只列 blocking
    assert len(parts) == 3
    assert parts[0] == "本轮待修复问题：\n1. 阻塞问题A"
    assert parts[1] == "这是本阶段第 5 次提交修复。"
    assert parts[2] == (
        "连续两次修复未取得进展。现在进行最后一轮最低目标修复。\n"
        "最低完成目标：修正层级并补齐缺失章节\n"
        "本轮只要求消除上述阻塞问题，暂不要求消除一致性、表格、字数等质量瑕疵；"
        "此前业务提示中的质量完成条件本轮不再作为验收条件。"
        "完成最低目标后重新提交；如果仍有阻塞问题，任务将停止。"
    )


def test_修复提示_最低目标缺省文案回退():
    default_goal = ("最低完成目标：解决下列阻塞问题，"
                    "使当前产物能被后续流程正常读取和使用。")
    # 未提供 minimumGoal → 回退易标默认目标
    prompt = build_submission_repair_prompt(
        make_report(BLOCKING_B),
        {"action": "repair", "mode": "minimum", "attempt": 1},
        "业务提示",
    )
    assert default_goal in prompt
    # 空串同样按 falsy 回退
    prompt = build_submission_repair_prompt(
        {"issues": [BLOCKING_B], "minimumGoal": ""},
        {"action": "repair", "mode": "minimum", "attempt": 2},
        "业务提示",
    )
    assert default_goal in prompt


# ── validate_json_output：三类产出与 message 逐字 ──────────────────────

def test_json输出_空文件三类文案():
    report = validate_json_output("", OUTLINE_OUTPUT_FILE, None)
    assert report["value"] is None
    assert report["issues"] == [{
        "severity": "blocking",
        "file": "outline.json",
        "message": "outline.json 未写入或内容为空，请写入完整的 JSON",
    }]
    # None 与纯空白视同空文件
    assert validate_json_output(None, "a.json", None)["value"] is None
    assert validate_json_output("  \n\t", "a.json", None)["value"] is None


def test_json输出_非法json文案逐字():
    content = "{"
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        expected = str(exc)
    report = validate_json_output(content, LEAF_ALLOCATION_FILE, None)
    assert report["value"] is None
    assert report["issues"] == [{
        "severity": "blocking",
        "file": "leaf-allocation.json",
        "message": f"leaf-allocation.json不是合法 JSON：{expected}",
    }]


def test_json输出_校验之外的异常向上抛():
    def boom(_value):
        raise RuntimeError("校验器炸了")

    with pytest.raises(RuntimeError, match="校验器炸了"):
        validate_json_output('{"a": 1}', "x.json", boom)
    # json.loads 的非 JSONDecodeError 异常（整数超长 ValueError）不得被吞
    with pytest.raises(ValueError):
        validate_json_output("9" * 6000, "x.json", None)


def test_json输出_schema失败逐条产出blocking_issue():
    def validate_stub(_value):
        return False, [
            {"instancePath": "", "message": "must be object"},
            {"instancePath": "/items/0/name", "message": "must be string"},
        ]

    report = validate_json_output('{"a": 1}', OUTLINE_REVIEW_FILE, validate_stub)
    # 解析值保留（与易标一致），问题只进 issues
    assert report["value"] == {"a": 1}
    assert report["issues"] == [
        {"severity": "blocking", "file": "outline-review.json", "path": "/",
         "message": "outline-review.json 不符合结构要求：/ must be object"},
        {"severity": "blocking", "file": "outline-review.json",
         "path": "/items/0/name",
         "message": "outline-review.json 不符合结构要求：/items/0/name must be string"},
    ]


def test_json输出_校验通过与未注入校验器():
    assert validate_json_output("[1, 2]", "a.json",
                                lambda _v: (True, [])) == {"value": [1, 2],
                                                           "issues": []}
    assert validate_json_output("[1]", "a.json", None) == {"value": [1],
                                                           "issues": []}


# ── 阶段文件表：6 阶段逐字（连字符 / 下划线差异） ────────────────────────

def test_阶段文件表与易标逐字一致():
    assert STAGE_OUTPUT_FILES == {
        "initial-outline": ["outline.json"],
        "score-planning": ["technical-score-groups.json",
                           "score-directory-plan.json"],
        "leaf_allocation": ["leaf-allocation.json"],
        "children_generation": ["outline.json"],
        "leaf_adjustment": ["outline.json"],
        "outline_review": ["outline.json", "outline-review.json"],
    }
    assert OUTLINE_OUTPUT_FILE == "outline.json"
    assert TECHNICAL_SCORE_GROUPS_FILE == "technical-score-groups.json"
    assert SCORE_DIRECTORY_PLAN_FILE == "score-directory-plan.json"
    assert LEAF_ALLOCATION_FILE == "leaf-allocation.json"
    assert LEAF_ALLOCATION_CONTEXT_FILE == "leaf-allocation-context.json"
    assert OUTLINE_REVIEW_FILE == "outline-review.json"
    assert OUTLINE_REVIEW_CONTEXT_FILE == "outline-review-context.json"


# ── 目录身份：沿用 / 分配 / 报错 / 编号 ──────────────────────────────────

def test_接受目录_沿用已知id并为新节点分配uuid():
    items = [{
        "id": ROOT_ID,
        "title": " 一 ",
        "description": "描述",
        "attr": "技术",
        "children": [
            {"id": None, "title": "子", "description": "",
             "content_mode": "ai-generate"},
        ],
    }]
    out = accept_agent_outline(items, {ROOT_ID})
    assert out[0]["id"] == ROOT_ID
    assert out[0]["title"] == "一"
    child = out[0]["children"][0]
    assert child["id"] != ROOT_ID
    assert re.fullmatch(NODE_ID_PATTERN, child["id"])
    assert uuid.UUID(child["id"]).version == 4  # 与 Node randomUUID 同为 v4
    assert out[0]["number"] == "1"
    assert child["number"] == "1.1"
    # 有 children 的节点只带 children，不带叶子字段
    assert "content_mode" not in out[0]


def test_接受目录_未知id与重复id文案逐字():
    with pytest.raises(ValueError) as exc:
        accept_agent_outline([{"id": "not-a-uuid", "title": "x"}], set())
    assert str(exc.value) == "目录包含未知 ID：not-a-uuid；新节点请使用 id:null"

    dup = "22222222-2222-4222-8222-222222222222"
    with pytest.raises(ValueError) as exc:
        accept_agent_outline(
            [{"id": dup, "title": "a"}, {"id": dup, "title": "b"}], {dup})
    assert str(exc.value) == f"目录 ID 重复：{dup}"


def test_编号只改number不改身份且不改动原对象():
    items = [
        {"id": ROOT_ID, "title": "一"},
        {"id": None, "title": "二", "children": [{"id": None, "title": "子"}]},
    ]
    before = collect_outline_ids(items)
    numbered = number_outline(items)
    assert [node["number"] for node in numbered] == ["1", "2"]
    assert numbered[1]["children"][0]["number"] == "2.1"
    assert collect_outline_ids(numbered) == before
    assert numbered[0]["id"] == ROOT_ID
    assert numbered[1]["id"] is None
    assert "number" not in items[0]  # 原对象未被写入
    assert number_outline(None) == []
    assert [node["number"] for node in
            number_outline(items, prefix="1")] == ["1.1", "1.2"]


def test_叶子节点_content_mode_note规则():
    other = accept_agent_outline(
        [{"id": None, "title": "t", "content_mode": "other",
          "content_mode_note": "  说明  "}], set())[0]
    assert other["content_mode_note"] == "说明"
    ai = accept_agent_outline(
        [{"id": None, "title": "t", "content_mode": "ai-generate",
          "content_mode_note": "多余"}], set())[0]
    assert "content_mode_note" not in ai


# ── validate_outline_output：结构与身份 issue 文案 ──────────────────────

def test_目录输出_父层与节点结构错误文案():
    report = validate_outline_output(
        json.dumps({"outline": {"a": 1}}), set(), check_schema=False)
    assert report["issues"] == [{
        "severity": "blocking", "file": "outline.json", "path": "/outline",
        "message": "目录结构错误：/outline 必须是数组",
    }]
    assert report["value"] == {"outline": {"a": 1}}  # 保留解析值

    report = validate_outline_output(
        json.dumps({"outline": [None, 123]}, ensure_ascii=False),
        set(), check_schema=False)
    assert [issue["message"] for issue in report["issues"]] == [
        "目录结构错误：/outline/0 必须是非空对象",
        "目录结构错误：/outline/1 必须是非空对象",
    ]


def test_目录输出_未知与重复id文案逐字():
    dup = "33333333-3333-4333-8333-333333333333"
    content = json.dumps({"outline": [
        {"id": "ghost", "title": "a"},
        {"id": dup, "title": "b"},
        {"id": dup, "title": "c"},
    ]}, ensure_ascii=False)
    report = validate_outline_output(content, {dup}, check_schema=False)
    assert [(issue["path"], issue["message"]) for issue in report["issues"]] == [
        ("/outline/0/id", "目录包含未知 ID：ghost；新节点请使用 id:null"),
        ("/outline/2/id", f"目录 ID 重复：{dup}"),
    ]


def test_目录输出_通过后分配身份并编号():
    content = json.dumps({"outline": [
        {"id": KEPT_ROOT, "title": "既有", "attr": "技术", "children": [
            {"id": None, "title": "新", "content_mode": "ai-generate"},
        ]},
    ]}, ensure_ascii=False)
    report = validate_outline_output(content, {KEPT_ROOT}, check_schema=False)
    assert report["issues"] == []
    outline = report["value"]["outline"]
    assert outline[0]["id"] == KEPT_ROOT
    assert outline[0]["number"] == "1"
    assert re.fullmatch(NODE_ID_PATTERN, outline[0]["children"][0]["id"])
    assert outline[0]["children"][0]["number"] == "1.1"


def test_目录输出_schema开关与注入校验器():
    def failing(_value):
        return False, [{"instancePath": "", "message": "bad"}]

    content = json.dumps({"outline": []})
    # check_schema=False → 跳过注入的失败校验器
    report = validate_outline_output(content, set(), check_schema=False,
                                     validator=failing)
    assert report["issues"] == []
    # check_schema=True 且注入失败校验器 → blocking issue
    report = validate_outline_output(content, set(), validator=failing)
    assert report["issues"] == [{
        "severity": "blocking", "file": "outline.json", "path": "/",
        "message": "outline.json 不符合结构要求：/ bad",
    }]


# ── validate_stage_outputs：阶段汇总与评分规划 ──────────────────────────

def test_阶段汇总_initial_outline读取候选输出():
    meta = StageMeta(workflow_stage="initial-outline",
                     read_file=lambda _name: pytest.fail("不应读文件"))
    result = validate_stage_outputs(
        {"output_content": json.dumps({"outline": []})}, meta, {})
    assert result["issues"] == []
    assert result["value"] == {"outline.json": {"outline": []}}


def test_阶段汇总_读取异常向上抛():
    def read_file(_name):
        raise OSError("读盘失败")

    meta = StageMeta(workflow_stage="leaf_allocation", read_file=read_file)
    with pytest.raises(OSError, match="读盘失败"):
        validate_stage_outputs({"output_content": ""}, meta, {})


def test_评分规划_通过后回填root_id并分配新身份():
    kept = {"id": KEPT_ROOT, "title": "技术方案", "description": "既有根",
            "attr": "技术", "content_mode": "ai-generate"}
    new = {"id": None, "title": "新增章节", "description": "批准新增",
           "attr": "技术", "content_mode": "ai-generate"}
    outline_content = json.dumps({"outline": [kept, new]}, ensure_ascii=False)
    plan = {
        "allow_root_changes": True,
        "branches": [
            {"root_id": KEPT_ROOT, "root_title": "技术方案"},
            {"root_id": None, "root_title": "新增章节"},
        ],
        "extra_titles": [],
    }
    files = {
        TECHNICAL_SCORE_GROUPS_FILE: json.dumps({"groups": []}),
        SCORE_DIRECTORY_PLAN_FILE: json.dumps(plan, ensure_ascii=False),
    }
    meta = StageMeta(workflow_stage="score-planning",
                     read_file=files.__getitem__,
                     known_node_ids={KEPT_ROOT},
                     locked_roots=[kept])
    result = validate_stage_outputs({"output_content": outline_content},
                                    meta, {})
    assert result["issues"] == []
    outline = result["value"][OUTLINE_OUTPUT_FILE]["outline"]
    assert outline[0]["id"] == KEPT_ROOT
    new_id = outline[1]["id"]
    assert re.fullmatch(NODE_ID_PATTERN, new_id)
    out_plan = result["value"][SCORE_DIRECTORY_PLAN_FILE]
    assert out_plan["branches"][1]["root_id"] == new_id
    assert out_plan["branches"][0] == {"root_id": KEPT_ROOT,
                                       "root_title": "技术方案"}
    assert out_plan["allow_root_changes"] is True
    assert result["value"][TECHNICAL_SCORE_GROUPS_FILE] == {"groups": []}


def test_评分规划_已确认根被改动则报错():
    kept = {"id": KEPT_ROOT, "title": "技术方案", "description": "既有根",
            "attr": "技术", "content_mode": "ai-generate"}
    altered = {**kept, "title": "被改名"}
    outline_content = json.dumps({"outline": [altered]}, ensure_ascii=False)
    plan = {"allow_root_changes": False,
            "branches": [{"root_id": KEPT_ROOT, "root_title": "被改名"}]}
    files = {
        TECHNICAL_SCORE_GROUPS_FILE: json.dumps({"groups": []}),
        SCORE_DIRECTORY_PLAN_FILE: json.dumps(plan, ensure_ascii=False),
    }
    meta = StageMeta(workflow_stage="score-planning",
                     read_file=files.__getitem__,
                     known_node_ids={KEPT_ROOT},
                     locked_roots=[kept])
    result = validate_stage_outputs({"output_content": outline_content},
                                    meta, {})
    assert [issue["message"] for issue in result["issues"]] == [
        "outline.json 中用户已确认的一级目录必须全部保留，"
        "id、title、description、attr、content_mode 和相对顺序均不得修改；"
        "本阶段只能插入经用户批准新增的一级目录",
    ]
    # 根报告失败时 outputs 保留原始解析值，未回填
    assert result["value"][SCORE_DIRECTORY_PLAN_FILE]["branches"] == \
        plan["branches"]
    assert OUTLINE_OUTPUT_FILE not in result["value"]


def test_评分规划_已确认根不得包含children():
    kept = {"id": KEPT_ROOT, "title": "技术方案", "description": "既有根",
            "attr": "技术", "content_mode": "ai-generate",
            "children": [{"id": None, "title": "子"}]}
    outline_content = json.dumps({"outline": [kept]}, ensure_ascii=False)
    plan = {"allow_root_changes": False,
            "branches": [{"root_id": KEPT_ROOT, "root_title": "技术方案"}]}
    files = {
        TECHNICAL_SCORE_GROUPS_FILE: json.dumps({"groups": []}),
        SCORE_DIRECTORY_PLAN_FILE: json.dumps(plan, ensure_ascii=False),
    }
    meta = StageMeta(workflow_stage="score-planning",
                     read_file=files.__getitem__,
                     known_node_ids={KEPT_ROOT},
                     locked_roots=[{k: v for k, v in kept.items()
                                    if k != "children"}])
    result = validate_stage_outputs({"output_content": outline_content},
                                    meta, {})
    # 与易标一致：带 children 的节点在重建时丢掉 content_mode，rootFields 比对
    # 先报「必须全部保留」，再报「不得包含 children」
    assert [(issue["path"], issue["message"])
            for issue in result["issues"]] == [
        ("/outline",
         "outline.json 中用户已确认的一级目录必须全部保留，"
         "id、title、description、attr、content_mode 和相对顺序均不得修改；"
         "本阶段只能插入经用户批准新增的一级目录"),
        ("/outline/0/children",
         "outline.json 本阶段只调整一级目录，「技术方案」不得包含 children"),
    ]
