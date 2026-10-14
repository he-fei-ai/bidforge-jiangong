"""目录生成 · 占位标题重写的**第二轮**收口护栏（R73b · 2026-10-12）

R73 第一轮（F1 闸门 / F2 外科式标题重写 / F3 残留复检）落地后，本轮沿着
「改名 → 编号 → 覆盖 → 报告 → 前端」这条数据链继续挖，复现出四条**真实**缺口
（每条都先有红态复现，再修）：

  F4（内容损坏 · 双重编号）：`_apply_title_updates` 把模型回传的新标题**原样**写进
    目录树。实测 `title="2.1 灌注桩支护施工"` → 落库标题带编号，而展示层按位置
    再套一次 → 「2.1 2.1 灌注桩支护施工」；`"灌注桩\n支护施工"` 带换行同样照收。
    本仓**其余每一条标题写入路径**都会剥离内嵌编号：
      · sections.update_section（PATCH 改名，显式 strip_outline_numbering）
      · save-outline / reorder / 上传落库 / 目录库入库（经 normalize_outline）
      · 审核整份修复（normalize_outline）
    唯独新加的标题重写出口漏了 —— 与 AGENTS.md 反复记录的「唯独此路径漏写」同族。

  F5（日志不准确 + 前后端不同步）：`check_outline_continuity` 的 issues 有 **7** 类，
    而两份「标签表」副本都停在旧口径：
      · 后端 `render_continuity_notice` 只有 6 类（漏 generic_titles）⇒ 当目录**唯一**
        问题就是占位标题时（正是用户报的场景），WARNING 渲染成
        「目录连续性校验未通过：」—— 后面是**空的**。
      · 前端 `summarizeOutlineQuality` 的 labels 只有 4 类 ⇒ 占位标题/破折号/空标题
        三类计数被 `Object.keys(labels)` 直接过滤掉，用户界面**零提示**，
        而后端已判 `ok=false`。
    即「检出→报告→呈现」链条在最后一环断裂，等于没检。

  F6（回归窗口）：外科式标题重写**只复检占位标题、不复检覆盖**。占位标题曾以
    宽松匹配（≥4 字公共子串）替某条编制要求「顶」住落点，换成具体标题后该要求
    反而没有落点了，旧实现照样以 `passed=True` 早退。

  F7（谎报已修复）：外科补齐出口只看「补齐调用有没有产出新章」，**不回头验证
    补的章是否真覆盖缺失项**。模型返回任意无关章节即宣告
    「✅ 已外科式补齐 N 项缺失」并 passed=True。

运行：pytest tests/test_outline_title_fix_chain_r73b_20261012.py -q
"""
from __future__ import annotations

import copy
import io
import json
import os
import re

import pytest

from app.routers import sse_handlers as sh
from app.services.outline_quality import (
    check_outline_continuity,
    detect_generic_titles,
    render_continuity_notice,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
_PLACEHOLDER_CHILDREN = [
    {"title": "专项6", "description": "占位", "children": []},
    {"title": "总体实施策划（2）", "description": "占位", "children": []},
    {"title": "总体实施策划（3）", "description": "占位", "children": []},
]

BASE_OUTLINE = [
    {"title": "工程概况", "description": "项目总体说明", "children": []},
    {"title": "基坑支护施工", "description": "支护体系实施",
     "children": copy.deepcopy(_PLACEHOLDER_CHILDREN)},
    {"title": "安全保证措施", "description": "措施", "children": []},
]

REQUIREMENTS = "1. 工程概况\n2. 基坑支护施工\n3. 安全保证措施"

# F6 专用：第 4 条要求**只**靠二级占位标题「总体实施策划（2）」的宽松匹配顶住
REQUIREMENTS_RELY_ON_PLACEHOLDER = REQUIREMENTS + "\n4. 总体实施策划及相关安排"

# F7 专用：明确要求「应急预案演练」，而假补齐只会回一章无关的「监测方案」
REQUIREMENTS_UNCOVERED_BY_PATCH = REQUIREMENTS + "\n4. 应急预案演练"

TITLE_UPDATES = {
    "2.1": "灌注桩支护施工",
    "2.2": "钢内支撑安装与拆除",
    "2.3": "冠梁与混凝土腰梁施工",
}


@pytest.fixture(autouse=True)
def _isolate_checkpoint(monkeypatch):
    """隔离九大法定章节检查（该维度由 test_outline_checkpoint_20261002.py 专测）。"""
    monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False, raising=False)


def _outline():
    return copy.deepcopy(BASE_OUTLINE)


def _titles(tree):
    from app.services.outline_quality import collect_titles
    return [str(e.get("title") or "") for e in collect_titles(tree)]


def _fake_ai(monkeypatch, seen, *, patch_chapter=None, review=None, fix=None):
    """按校验函数分派的假 AI；记录调用序列。

    `patch_chapter`：控制外科补齐回传的新章（默认「监测方案」）。
    """
    title_fix_vf = getattr(sh, "_title_fix_validate_fn", None)

    async def fake_collect(messages, validate_fn=None, **kwargs):
        scene = kwargs.get("scene") or "?"
        seen.append(scene)
        if title_fix_vf is not None and validate_fn is title_fix_vf:
            obj = {"updates": [{"path": p, "title": t}
                               for p, t in TITLE_UPDATES.items()]}
            return obj, json.dumps(obj, ensure_ascii=False)
        if validate_fn is sh._outline_patch_validate_fn:
            obj = {"new_chapters": [patch_chapter or {
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
# A. F4：标题重写必须剥离内嵌编号（与全仓其余标题写入路径同口径）
# ---------------------------------------------------------------------------
class TestTitleMandatorySanitize:
    def test_dot_numbered_title_is_stripped(self):
        tree = _outline()
        _t, applied, rejected = sh._apply_title_updates(
            tree, [{"path": "2.1", "title": "2.1 灌注桩支护施工"}])
        assert applied == 1, rejected
        stored = _t[1]["children"][0]["title"]
        assert stored == "灌注桩支护施工", (
            f"新标题必须剥掉内嵌编号，否则展示层再套一次 → 双重编号；实际存 {stored!r}")

    def test_chinese_chapter_prefix_is_stripped(self):
        _t, applied, _r = sh._apply_title_updates(
            _outline(), [{"path": "2.1", "title": "灌注桩支护施工"}])
        assert applied == 1
        _t2, applied2, _r2 = sh._apply_title_updates(
            _outline(), [{"path": "2.1", "title": "第一章 灌注桩施工"}])
        assert applied2 == 1
        assert _t2[1]["children"][0]["title"] == "灌注桩施工"

    def test_stripping_to_generic_or_empty_is_rejected(self):
        """剥完仍是占位名（「2.1 专项6」）→ 检出闸门关不上，必须整条拒收。"""
        _t, applied, rejected = sh._apply_title_updates(
            _outline(), [{"path": "2.1", "title": "2.1 专项6"}])
        assert applied == 0, f"剥离后仍泛化的标题应被拒绝：{rejected}"
        assert rejected and "泛化" in str(rejected[0].get("reason"))

    def test_title_with_newline_is_rejected(self):
        """标题里带换行会破坏目录树渲染与导出标题行，必须拒收而非照录。"""
        _t, applied, rejected = sh._apply_title_updates(
            _outline(), [{"path": "2.1", "title": "灌注桩\n支护施工"}])
        assert applied == 0, f"含换行的标题应被拒绝：{rejected}"

    def test_clean_title_still_applies(self):
        """向后兼容：本身干净的标题照旧落地（不得因加清洗而误杀）。"""
        _t, applied, rejected = sh._apply_title_updates(
            _outline(), [{"path": "2.1", "title": "灌注桩支护施工"}])
        assert applied == 1, rejected
        assert _t[1]["children"][0]["title"] == "灌注桩支护施工"

    def test_stripped_titles_match_the_unique_numbering_impl(self):
        """与唯一事实源 `strip_outline_numbering` 逐字同口径（不另造尺子）。"""
        from app.services.numbering import strip_outline_numbering
        cases = ["2.1 灌注桩支护施工", "第一章 工程概况", "3、施工准备",
                 "2023 年度安全生产计划", "2层作业平台"]
        for raw in cases:
            _t, applied, _r = sh._apply_title_updates(
                _outline(), [{"path": "2.1", "title": raw}])
            expect = strip_outline_numbering(raw)
            if not applied:
                continue          # 被红线拒收（剥后泛化/为空）也是合法结论
            assert _t[1]["children"][0]["title"] == expect, (
                f"{raw!r} 的清洗结果与唯一实现不一致")


# ---------------------------------------------------------------------------
# B. F5：连续性「标签表」必须是唯一事实源，且前后端同步
# ---------------------------------------------------------------------------
class TestContinuityLabelSingleSource:
    GENERIC_ONLY_OUTLINE = [
        {"title": "工程概况", "children": []},
        {"title": "基坑支护施工", "children": [
            {"title": "专项6", "children": []},
            {"title": "总体实施策划（2）", "children": []},
        ]},
    ]

    def test_labels_constant_covers_every_issue_kind(self):
        from app.services.outline_quality import CONTINUITY_ISSUE_LABELS
        report = check_outline_continuity(self.GENERIC_ONLY_OUTLINE)
        # 契约口径：标签表负责令**缺陷类**，而缺陷类的唯一清单是 `issue_counts`
        # （由 issues 派生）。顶层还有 `similar_titles` 这类**组织建议**附录键，
        # 它们不进 issues、不改 ok —— 给它们贴标签会把建议误播为缺陷。
        issue_keys = set(report.get("issue_counts") or {})
        assert issue_keys, "夹具报告必须带 issue_counts（否则本用例证明不了什么）"
        missing = issue_keys - set(CONTINUITY_ISSUE_LABELS)
        assert not missing, f"连续性新增问题类缺中文标签：{sorted(missing)}"
        assert "generic_titles" in CONTINUITY_ISSUE_LABELS
        assert "similar_titles" not in CONTINUITY_ISSUE_LABELS

    def test_advisory_keys_are_not_announced_as_defects(self):
        """近似雷同类建议不得出现在缺陷摘要里（不影响 ok，也不得拉低 ok）。"""
        tree = [{"title": "基坑降水与支护施工", "children": []},
                {"title": "基坑降水及支护施工", "children": []}]
        report = check_outline_continuity(tree)
        assert report.get("ok") is True, "近似标题是建议，不得判结构不合格"
        assert report.get("similar_titles"), "夹具应确实产生近似标题建议"
        assert "similar_titles" not in render_continuity_notice(report)

    def test_notice_names_generic_titles(self):
        """修复前实测：唯一问题就是占位标题时，WARNING 尾部是空的。"""
        report = check_outline_continuity(self.GENERIC_ONLY_OUTLINE)
        assert detect_generic_titles(self.GENERIC_ONLY_OUTLINE), "夹具必须含占位标题"
        assert report.get("ok") is False
        notice = render_continuity_notice(report)
        assert "占位" in notice and "2 处" in notice, (
            f"日志必须点出问题类别与数量，实际渲染：{notice!r}")

    def test_notice_is_not_empty_when_not_ok(self):
        report = check_outline_continuity(self.GENERIC_ONLY_OUTLINE)
        assert render_continuity_notice(report), "ok=False 却渲染空串 = 日志不准确"

    def test_backend_and_frontend_label_tables_agree(self):
        """跨语言 parity：前端 labels 表必须覆盖后端全部问题类（本仓前端不便读盘，
        故 parity 放 pytest 侧，同 TestExportIssueLabelParity 做法）。"""
        from app.services.outline_quality import CONTINUITY_ISSUE_LABELS
        _root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(sh.__file__)))))
        page = os.path.join(_root, "frontend", "src", "pages",
                            "SchemeWorkbenchPage.tsx")
        assert os.path.isfile(page), f"未找到前端页面源码：{page}"
        src = io.open(page, encoding="utf-8").read().replace("\r\n", "\n")
        m = re.search(r"const labels: Record<string, string> = \{(.*?)\n\s*\};",
                      src, re.S)
        assert m, "未在前端找到 summarizeOutlineQuality 的 labels 表"
        fe = dict(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)"', m.group(1)))
        gap = set(CONTINUITY_ISSUE_LABELS) - set(fe)
        assert not gap, (
            "前端连续性标签缺项 —— 该类问题计数会被 Object.keys(labels) 过滤掉，"
            f"界面零提示（用户看不到）：{sorted(gap)}")
        assert all(re.search(r"[\u4e00-\u9fff]", v) for v in fe.values()), \
            "前端标签必须是中文，不得显示英文裸代号"


# ---------------------------------------------------------------------------
# C. F6 / F7：程序化出口必须「诚实」——改名与补齐都要回头复检覆盖
# ---------------------------------------------------------------------------
class TestProgrammaticExitHonesty:
    async def test_rename_that_breaks_coverage_is_not_early_exit(self, monkeypatch):
        """占位标题曾顶住某条编制要求：换成具体标题后覆盖出现新缺口 ⇒ 不得早退。"""
        covered_before, missing_before = sh._check_requirements_coverage(
            REQUIREMENTS_RELY_ON_PLACEHOLDER, _outline(), False)
        assert covered_before and not missing_before, (
            "夹具必须处于「改名前算全覆盖」的状态，否则本用例证明不了回归窗口")

        seen = []
        _fake_ai(monkeypatch, seen)
        outline, review = await sh._review_and_fix_outline(
            _outline(), "基坑", False, "摘要",
            requirements=REQUIREMENTS_RELY_ON_PLACEHOLDER)

        mode = str(review.get("review_mode") or "")
        if mode.startswith("programmatic"):
            covered, missing = sh._check_requirements_coverage(
                REQUIREMENTS_RELY_ON_PLACEHOLDER, outline, False)
            assert covered, (
                f"出口以 {mode} 宣称通过，但目录仍有覆盖缺口：{missing}")
        assert len(seen) >= 2, (
            f"改名破坏覆盖时必须继续走补齐/审核（不能只发一次标题重写就收工）：{seen}")

    async def test_surgical_patch_that_misses_is_not_reported_as_fixed(self, monkeypatch):
        """补齐回传的章节并没有覆盖缺失项 ⇒ 不得写「已外科式补齐 N 项缺失」。"""
        seen = []
        _fake_ai(monkeypatch, seen)
        outline, review = await sh._review_and_fix_outline(
            _outline(), "基坑", False, "摘要",
            requirements=REQUIREMENTS_UNCOVERED_BY_PATCH)

        mode = str(review.get("review_mode") or "")
        assert not mode.startswith("programmatic"), (
            f"缺口未真正补上，不得以 {mode} 宣告程序化通过")
        assert "outline_review" in seen, (
            f"应落回完整 AI 审核链路，实际调用序列：{seen}")
        assert not any("已外科式补齐" in s for s in review.get("suggestions") or []), \
            "补齐未覆盖缺失项时不得谎报「已补齐」"

    async def test_effective_patch_still_exits_programmatically(self, monkeypatch):
        """向后兼容：补齐确实覆盖了缺失项 ⇒ 旧的省调用出口逐字保留。"""
        seen = []
        _fake_ai(monkeypatch, seen, patch_chapter={
            "title": "监测方案", "description": "监测点布置与预警",
            "insert_after": "安全保证措施", "children": []})
        # 目录本身干净（无占位）时，补齐成功即应以 programmatic+surgical 早退
        clean = [{"title": "工程概况", "children": []},
                 {"title": "基坑支护施工", "children": [
                     {"title": "灌注桩支护施工", "children": []}]},
                 {"title": "安全保证措施", "children": []}]
        assert detect_generic_titles(clean) == []

        outline, review = await sh._review_and_fix_outline(
            clean, "基坑", False, "摘要", requirements=REQUIREMENTS + "\n4. 监测方案")

        assert review.get("review_mode") == "programmatic+surgical"
        assert "监测方案" in _titles(outline)
        assert "outline_review" not in seen, "旧行为红线：不该再拖进 AI 审核"


# ---------------------------------------------------------------------------
# D. 不变式：任何以 programmatic 开头的出口，结论都必须与最终目录一致
# ---------------------------------------------------------------------------
class TestExitInvariant:
    async def test_programmatic_exits_never_claim_pass_with_residual(self, monkeypatch):
        """总闸：只要出口是 programmatic*，最终目录既不得有占位标题，
        passed 也必须为 True（三处闸门任一失效都会在这里红）。"""
        seen = []
        _fake_ai(monkeypatch, seen)
        outline, review = await sh._review_and_fix_outline(
            _outline(), "基坑", False, "摘要", requirements=REQUIREMENTS)
        if str(review.get("review_mode") or "").startswith("programmatic"):
            assert detect_generic_titles(outline) == [], _titles(outline)
            assert review.get("passed") is True

    def test_helpers_are_pure_and_config_gated(self):
        """F6/F7 的复检必须是纯函数调用（零 AI 调用），且受同一开关门控。"""
        assert callable(sh._coverage_recheck)
        tree = _outline()
        snapshot = copy.deepcopy(tree)
        covered, missing = sh._coverage_recheck(REQUIREMENTS, tree, False, None)
        assert isinstance(covered, bool) and isinstance(missing, list)
        assert tree == snapshot, "复检不得修改目录树（纯函数红线）"
        assert getattr(sh.settings, "outline_title_fix", None) is True
