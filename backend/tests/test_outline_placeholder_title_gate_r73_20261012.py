"""目录生成 · 占位标题「检出闸门」与外科式标题重写收口（R73 · 2026-10-12）

用户实测缺陷（生产实证）：TOC 三级标题大量是占位式泛化名称（「专项6」
「总体实施策划（2）」「总体实施策划（3）」），且带「（N）」编号冲突后缀。

2026-10-10 那一轮已经把 `detect_generic_titles` 接到 `_review_and_fix_outline`，
但接在了 **AI 审核之后**（原 :2939）。本轮根因核查确认三处真实缺口：

  根因 A（主）：`_review_and_fix_outline` 的两条**程序化早退**
    - `review_mode="programmatic"`（覆盖预检全过 → 跳过 AI 审核）
    - `review_mode="programmatic+surgical"`（外科补齐成功）
    都在 `detect_generic_titles` **之前** return ⇒ 只要用户填了编制要求 / 有方案解析
    （长方案常态），泛化占位标题**从未被检出、从未被修复**，直接落库。
    既有测试 `test_outline_generic_titles_20261010.py::TestReviewInjection` 靠
    monkeypatch `OUTLINE_REVIEW_MODE="always"` 才命中检测分支 —— 恰好证明程序化
    路径从未被覆盖过。

  根因 B（次）：修复被采用后（合并 fixed_outline / 独立修复轮）一律
    `review_obj["passed"] = True` 并追加「✅ 已根据审核意见自动修复」，**没有复检**
    ⇒ 占位标题仍在却谎报已修复（结论 / 日志不准确）。

  缺口 C：唯一修复手段是「整份目录重写」，长方案下受 `OUTLINE_FIX_MIN_COVERAGE`
    退化护栏与 120s 超时约束极易回退「保留原目录」⇒ 检出也无从修正。本轮补
    **外科式标题重写**（只送占位节点 + 上下文，1 次小调用，按 path 精确回填）。

运行：pytest tests/test_outline_placeholder_title_gate_r73_20261012.py -q
"""
from __future__ import annotations

import copy
import json

import pytest

from app.routers import sse_handlers as sh
from app.services.outline_quality import collect_titles, detect_generic_titles


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
# 一级标题齐备（覆盖编制要求），但二级位全是占位泛化名 —— 正是用户报的形态
PLACEHOLDER_OUTLINE = [
    {"title": "工程概况", "description": "项目总体说明", "children": []},
    {"title": "基坑支护施工", "description": "支护体系实施", "children": [
        {"title": "专项6", "description": "占位", "children": []},
        {"title": "总体实施策划（2）", "description": "占位", "children": []},
        {"title": "总体实施策划（3）", "description": "占位", "children": []},
    ]},
    {"title": "安全保证措施", "description": "措施", "children": []},
]

REQUIREMENTS = "1. 工程概况\n2. 基坑支护施工\n3. 安全保证措施"

# 占位节点的重写结果（path 来自 collect_titles 的点分位置路径）
TITLE_UPDATES = {
    "2.1": "灌注桩支护施工",
    "2.2": "钢内支撑安装与拆除",
    "2.3": "冠梁与混凝土腰梁施工",
}


@pytest.fixture(autouse=True)
def _isolate_checkpoint(monkeypatch):
    """隔离「九大法定章节」检查：本文件测占位标题闸门，不测九章覆盖。

    九章覆盖由 tests/test_outline_checkpoint_20261002.py 单独、显式地测。
    """
    monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False, raising=False)


def _outline():
    return copy.deepcopy(PLACEHOLDER_OUTLINE)


def _titles(tree):
    return [str(e.get("title") or "") for e in collect_titles(tree)]


def _fake_ai(monkeypatch, seen, *, title_fix_ok=True, patch_ok=True,
             review=None, fix=None):
    """按校验函数分派的假 AI；记录调用序列（scene）。

    ⚠️ `_title_fix_validate_fn` 用 getattr 取：修复前该函数不存在时，本夹具
    退化成「不认识标题重写请求」，让用例以**断言失败**红（证明缺陷）而非
    AttributeError 红（证明夹具坏了）。
    """
    title_fix_vf = getattr(sh, "_title_fix_validate_fn", None)

    async def fake_collect(messages, validate_fn=None, **kwargs):
        scene = kwargs.get("scene") or "?"
        seen.append(scene)
        if title_fix_vf is not None and validate_fn is title_fix_vf:
            if not title_fix_ok:
                raise RuntimeError("标题重写不可用（用例刻意制造）")
            obj = {"updates": [{"path": p, "title": t}
                               for p, t in TITLE_UPDATES.items()]}
            return obj, json.dumps(obj, ensure_ascii=False)
        if validate_fn is sh._outline_patch_validate_fn:
            if not patch_ok:
                raise RuntimeError("外科补齐不可用")
            obj = {"new_chapters": [{
                "title": "监测方案", "description": "监测点布置与预警",
                "insert_after": "安全保证措施", "children": []}]}
            return obj, json.dumps(obj, ensure_ascii=False)
        if validate_fn is sh._outline_fix_validate_fn:
            if fix is None:
                raise RuntimeError("整目录重写不可用（用例刻意制造）")
            return fix, json.dumps(fix, ensure_ascii=False)
        return review or ({"passed": True, "suggestions": []}), "{}"

    monkeypatch.setattr(sh, "collect_json_response", fake_collect)
    monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")


# ---------------------------------------------------------------------------
# A. 根因 A：程序化早退必须带「占位标题闸门」，不得带着占位判通过
# ---------------------------------------------------------------------------
class TestEarlyExitGate:
    async def test_placeholder_blocks_programmatic_early_exit(self, monkeypatch):
        """覆盖预检全过 + 含占位标题 ⇒ 不得走 0 调用的 programmatic 早退。"""
        assert detect_generic_titles(_outline()), "夹具本身必须含占位标题（防空转）"
        seen = []
        _fake_ai(monkeypatch, seen)

        outline, review = await sh._review_and_fix_outline(
            _outline(), "基坑", False, "摘要", requirements=REQUIREMENTS)

        assert review.get("review_mode") != "programmatic", (
            "含占位标题时严禁以 programmatic 早退宣称通过（根因 A）")
        assert detect_generic_titles(outline) == [], (
            f"占位标题应被改写，实际标题：{_titles(outline)}")
        assert "专项6" not in _titles(outline)
        assert "总体实施策划（2）" not in _titles(outline)
        assert review.get("passed") is True

    async def test_clean_outline_still_exits_without_ai(self, monkeypatch):
        """向后兼容红线：无占位标题时逐字保持旧的 0 调用早退（省 1~2 次调用）。"""
        seen = []
        _fake_ai(monkeypatch, seen)
        clean = [{"title": "工程概况", "children": []},
                 {"title": "基坑支护施工", "children": [
                     {"title": "灌注桩支护施工", "children": []}]},
                 {"title": "安全保证措施", "children": []}]
        assert detect_generic_titles(clean) == []

        _out, review = await sh._review_and_fix_outline(
            clean, "基坑", False, "摘要", requirements=REQUIREMENTS)

        assert seen == [], "干净目录不得被拖进任何 AI 调用（旧行为红线）"
        assert review["review_mode"] == "programmatic"
        assert review["passed"] is True

    async def test_switch_off_restores_old_early_exit(self, monkeypatch):
        """`outline_check_generic_titles=False` ⇒ 完整回到本轮之前的早退行为。

        ⚠️ 本用例只验证**占位标题开关**的可回退性，故同时隔离 R89 新增的
        「重复标题闸门」：同一开关管不到它，而夹具的「总体实施策划（2）」与
        「（3）」经 `_norm` 归一后正好构成重复，会独立挡住早退（这正是 R89
        想要的新行为，不是本用例要测的东西）。
        """
        seen = []
        _fake_ai(monkeypatch, seen)
        monkeypatch.setattr(sh.settings, "outline_check_generic_titles", False,
                            raising=False)
        monkeypatch.setattr(sh, "_detect_duplicate_titles", lambda _t: [])

        _out, review = await sh._review_and_fix_outline(
            _outline(), "基坑", False, "摘要", requirements=REQUIREMENTS)

        assert review.get("review_mode") == "programmatic"
        assert seen == [], "关闭开关后不应新增任何 AI 调用（可回退红线）"


# ---------------------------------------------------------------------------
# B. 根因 A 的第二条早退：外科补齐成功后同样不得带着占位返回
# ---------------------------------------------------------------------------
class TestSurgicalExitGate:
    async def test_surgical_exit_rewrites_placeholder_titles(self, monkeypatch):
        """缺章 + 含占位 ⇒ 补齐后仍要重写占位，出口结论必须是真的干净。"""
        seen = []
        _fake_ai(monkeypatch, seen)

        outline, review = await sh._review_and_fix_outline(
            _outline(), "基坑", False, "摘要",
            requirements=REQUIREMENTS + "\n4. 监测方案")

        assert "监测方案" in _titles(outline), "外科补齐应先补上缺章"
        assert detect_generic_titles(outline) == [], (
            f"补齐出口仍残留占位标题：{_titles(outline)}")
        assert str(review.get("review_mode", "")).endswith("titled"), (
            f"补齐 + 标题重写的出口应带 titled 口径，实际：{review.get('review_mode')}")
        assert "outline_fix" in seen, "标题重写复用 outline_fix 场景归因"


# ---------------------------------------------------------------------------
# C. 根因 B：修复采用后必须复检，残留时如实报告（绝不谎报「已自动修复」）
# ---------------------------------------------------------------------------
class TestResidualHonesty:
    async def test_residual_generic_is_not_reported_as_passed(self, monkeypatch):
        """AI 回传的 fixed_outline 仍含占位 ⇒ passed=False + 透明人工核对警告。"""
        residual = copy.deepcopy(PLACEHOLDER_OUTLINE)
        residual[1]["children"][0]["title"] = "灌注桩支护施工"   # 只改掉一个

        seen = []
        _fake_ai(monkeypatch, seen, title_fix_ok=False,
                 review={"passed": False, "suggestions": ["占位请改"],
                         "fixed_outline": copy.deepcopy(residual)})
        monkeypatch.setattr(sh, "OUTLINE_REVIEW_MODE", "always")

        outline, review = await sh._review_and_fix_outline(
            _outline(), "基坑", False, "摘要")

        assert detect_generic_titles(outline), "夹具设计前提：修复后确有残留"
        assert review.get("passed") is False, (
            "残留占位标题却报 passed=True 即根因 B（结论不准确）")
        assert int(review.get("generic_residual") or 0) >= 1
        sugg = " ".join(review.get("suggestions") or [])
        assert ("人工" in sugg) or ("核对" in sugg), f"应给出可核对的透明警告：{sugg}"
        assert "已根据审核意见自动修复" not in sugg, f"不得谎报已修复：{sugg}"

    async def test_clean_fix_result_still_reports_passed(self, monkeypatch):
        """修复后干净 ⇒ 保持 passed=True 与旧的「已自动修复」文案（不误伤）。"""
        corrected = copy.deepcopy(PLACEHOLDER_OUTLINE)
        for i, t in enumerate(("灌注桩支护施工", "钢内支撑安装与拆除",
                               "冠梁与混凝土腰梁施工")):
            corrected[1]["children"][i]["title"] = t

        seen = []
        _fake_ai(monkeypatch, seen,
                 review={"passed": False, "suggestions": ["占位请改"],
                         "fixed_outline": copy.deepcopy(corrected)})
        monkeypatch.setattr(sh, "OUTLINE_REVIEW_MODE", "always")

        outline, review = await sh._review_and_fix_outline(
            _outline(), "基坑", False, "摘要")

        assert detect_generic_titles(outline) == []
        assert review.get("passed") is True
        assert int(review.get("generic_residual") or 0) == 0


# ---------------------------------------------------------------------------
# D. 外科式标题重写：纯函数按 path 精确回填，四条保守红线
# ---------------------------------------------------------------------------
class TestApplyTitleUpdates:
    def test_applies_valid_updates_only(self):
        tree, applied, rejected = sh._apply_title_updates(
            _outline(), [
                {"path": "2.1", "title": "灌注桩支护施工"},
                {"path": "2.9", "title": "不存在的节点"},        # 路径不存在
                {"path": "2.2", "title": "专项7"},               # 新标题自身仍泛化
                {"path": "2.3", "title": "灌注桩支护施工"},       # 与兄弟重名
            ])
        assert applied == 1
        titles = _titles(tree)
        assert "灌注桩支护施工" in titles
        assert "专项7" not in titles
        assert tree[1]["children"][1]["title"] == "总体实施策划（2）"
        assert tree[1]["children"][0]["description"] == "占位", "只改标题，不动描述"
        assert len(rejected) == 3

    def test_pure_does_not_mutate_input(self):
        src = _outline()
        snapshot = json.dumps(src, ensure_ascii=False, sort_keys=True)
        sh._apply_title_updates(src, [{"path": "2.1", "title": "灌注桩支护施工"}])
        assert json.dumps(src, ensure_ascii=False, sort_keys=True) == snapshot, (
            "纯函数：绝不就地改坏调用方持有的目录")

    def test_rename_leaves_numbering_untouched(self):
        """标题改写不改变位置路径 ⇒ 无需重算编号（位置是编号唯一事实源）。"""
        tree, _a, _r = sh._apply_title_updates(
            _outline(), [{"path": "2.1", "title": "灌注桩支护施工"}])
        assert [e["path"] for e in collect_titles(tree)] == [
            "1", "2", "2.1", "2.2", "2.3", "3"]

    def test_malformed_updates_never_raise(self):
        tree, applied, rejected = sh._apply_title_updates(
            _outline(), [None, "x", {}, {"path": 2.1},
                         {"path": "2.1", "title": "   "}])
        assert applied == 0 and len(rejected) == 5
        assert _titles(tree) == _titles(PLACEHOLDER_OUTLINE)


# ---------------------------------------------------------------------------
# E. 提示词契约：新模板必须注册 + 登记变量契约（r39 双向护栏）
# ---------------------------------------------------------------------------
class TestPromptContract:
    def test_registered_with_contract(self):
        from app.services.ai.prompts._registry import (
            PROMPT_VARIABLE_CONTRACTS,
            _ALL_PROMPTS,
        )
        assert "outline_title_fix_system" in _ALL_PROMPTS
        assert "outline_title_fix_system" in PROMPT_VARIABLE_CONTRACTS

    def test_template_states_rules(self):
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        text = _ALL_PROMPTS["outline_title_fix_system"]["default_content"]
        assert "{broken_items}" in text
        assert "（N）" in text
        assert ("泛化" in text) or ("占位" in text)

    def test_contract_drift_guard_clean(self):
        from app.services.ai.prompts._registry import check_prompt_variables
        assert check_prompt_variables() == []


# ---------------------------------------------------------------------------
# F. 配置项：默认开、可回退
# ---------------------------------------------------------------------------
class TestConfigDefaults:
    def test_default_on(self):
        from app.config import Settings
        assert Settings().outline_title_fix is True
