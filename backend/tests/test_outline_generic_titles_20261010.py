"""目录生成 · 泛化/占位标题与「（N）」编号冲突后缀收口测试（2026-10-10）

背景（生产实证）：三级标题大量出现占位式泛化名称（「专项6」「总体实施策划（2）」
「总体实施策划（3）」），且带「（N）」编号冲突后缀。根因：
  ① 提示词对泛化/占位标题的禁止不够强，模型仍产出此类名称；
  ② 程序化连续性校验此前只查 duplicate_titles（精确同名），不识别泛化占位名，
     且质量报告只读、不触发修复，AI 审核又常漏报 → 占位标题直接落库。

本轮修复：
  - outline_quality.detect_generic_titles：精确锚定泛化/占位标题与「（N）」后缀；
  - check_outline_continuity 新增 generic_titles issue（受 outline_check_generic_titles 门控）；
  - sse_handlers._review_and_fix_outline 在审核后程序化兜底：检出即追加针对性
    修复建议并强制进入修复链路，重写为具体到施工对象/部位/工序的真实标题；
  - 6 个 outline 提示词新增硬性禁止规则。

所有结论均有代码/提示词依据；测试可独立运行（pytest tests/test_outline_generic_titles_20261010.py）。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.outline_quality import (
    check_outline_continuity,
    detect_generic_titles,
)


# 复现生产实证的三级标题占位现象
GENERIC_OUTLINE = [
    {"title": "工程概况", "description": "项目总体说明", "children": [
        {"title": "项目地理位置", "description": "位置", "children": []},
    ]},
    {"title": "专项6", "description": "占位一级", "children": [
        {"title": "总体实施策划（2）", "description": "占位二级", "children": [
            {"title": "深基坑支护施工", "description": "具体三级", "children": []},
        ]},
        {"title": "总体实施策划（3）", "description": "占位二级", "children": []},
    ]},
]

# 合法标题（不得误报）
LEGIT_OUTLINE = [
    {"title": "专项施工方案", "description": "d", "children": []},
    {"title": "深基坑支护施工", "description": "d", "children": []},
    {"title": "第3章 工程概况", "description": "d", "children": []},
    {"title": "其他安全措施", "description": "d", "children": []},
    {"title": "通用施工工艺要点", "description": "d", "children": []},
    {"title": "主要施工内容", "description": "d", "children": []},
    {"title": "深基坑支护（2）", "description": "合法：带部位限定", "children": []},
]


def _titles(tree):
    out = []
    for n in tree or []:
        if isinstance(n, dict):
            out.append(n.get("title"))
            out.extend(_titles(n.get("children")))
    return out


# ---------------------------------------------------------------------------
# 1. detect_generic_titles 命中生产实证模式
# ---------------------------------------------------------------------------
class TestDetectGenericTitles:
    def test_catches_complaint_patterns(self):
        hits = detect_generic_titles(GENERIC_OUTLINE)
        mapping = {h["title"]: h["kind"] for h in hits}
        assert mapping.get("专项6") == "generic"
        assert mapping.get("总体实施策划（2）") == "generic"
        assert mapping.get("总体实施策划（3）") == "generic"

    def test_conflict_suffix_detected_on_collision(self):
        # 同基名 +（N）后缀（AI 消歧）→ 归类为 conflict_suffix（用户明确投诉的项）
        out = [{"title": "降水井施工（2）", "children": []},
               {"title": "降水井施工（3）", "children": []}]
        hits = detect_generic_titles(out)
        assert len(hits) == 2
        assert all(h["kind"] == "conflict_suffix" for h in hits)

    def test_single_qualified_suffix_not_flagged(self):
        # 合法带部位限定、且基名不重复 → 不误报（精度）
        out = [{"title": "深基坑支护（2）", "children": []}]
        assert detect_generic_titles(out) == []

    def test_no_false_positive_on_legitimate(self):
        # 精确锚定，绝不按子串模糊匹配 → 合法标题零误报
        assert detect_generic_titles(LEGIT_OUTLINE) == []

    def test_collect_all_levels(self):
        # 占位可出现在任意层级，detect 不限于 L3
        out = [{"title": "专项6", "children": [{"title": "其他", "children": []}]}]
        hits = detect_generic_titles(out)
        paths = {h["path"] for h in hits}
        assert "1" in paths and "1.1" in paths


# ---------------------------------------------------------------------------
# 2. check_outline_continuity 集成 generic_titles
# ---------------------------------------------------------------------------
class TestContinuityGeneric:
    def test_reports_generic_titles_and_fails_ok(self):
        r = check_outline_continuity(GENERIC_OUTLINE)
        assert r["ok"] is False
        assert r["generic_titles"]
        assert r["issue_counts"]["generic_titles"] == 3

    def test_config_off_disables_detection(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "outline_check_generic_titles", False)
        r = check_outline_continuity(GENERIC_OUTLINE)
        assert r["generic_titles"] == []

    def test_clean_outline_has_no_generic(self):
        r = check_outline_continuity(LEGIT_OUTLINE)
        assert r["generic_titles"] == []


# ---------------------------------------------------------------------------
# 3. 审核后程序化兜底：强制进入修复链路
# ---------------------------------------------------------------------------
class TestReviewInjection:
    def test_injects_generic_suggestion_and_fixes(self, monkeypatch):
        from app.routers import sse_handlers

        # 跳过程序化预检，直达 AI 审核
        monkeypatch.setattr(sse_handlers, "OUTLINE_REVIEW_MODE", "always")

        corrected = json.loads(json.dumps(GENERIC_OUTLINE))
        corrected[1]["title"] = "基坑降水与支护"
        corrected[1]["children"][0]["title"] = "降水井施工"
        corrected[1]["children"][1]["title"] = "排水沟施工"

        fake = AsyncMock(return_value=(
            {"passed": True, "suggestions": [], "fixed_outline": corrected}, ""))

        with patch.object(sse_handlers, "collect_json_response", fake):
            out, rev = asyncio.run(sse_handlers._review_and_fix_outline(
                json.loads(json.dumps(GENERIC_OUTLINE)), "深基坑", False,
                scheme_name="测试方案", construction_scope="降水支护"))

        sugg_text = " ".join(rev.get("suggestions") or [])
        # 兜底建议命中占位标题与修复动作
        assert "专项6" in sugg_text
        assert ("泛化" in sugg_text) or ("占位" in sugg_text)
        # 修复后目录不再含占位标题
        titles = _titles(out)
        assert "专项6" not in titles
        assert "总体实施策划（2）" not in titles


# ---------------------------------------------------------------------------
# 4. 提示词强化：6 个 outline 提示词均含禁止规则
# ---------------------------------------------------------------------------
class TestPromptHardening:
    def _render(self, name, **kw):
        from app.services.ai.prompts._registry import render
        return render(name, scheme_name=kw.get("scheme_name", "测试"), **kw)

    def test_generation_prompts_forbid_generic(self):
        for name in ("outline_short_system", "outline_level1_system",
                     "outline_sublevel_system", "outline_sublevel_batch_system"):
            text = self._render(name, scheme_type="深基坑", construction_scope="降水",
                                project_facts="", scheme_basis="", standards_text="",
                                project_brief="", other_outline="", prior_chapters="",
                                requirements="", reference_outline="",
                                chapter_id="1", chapter_title="降水", chapter_desc="",
                                chapters_text="")
            assert "（N）" in text, f"{name} 缺少（N）后缀禁止规则"
            assert ("泛化" in text) or ("占位" in text), f"{name} 缺少泛化/占位禁止规则"

    def test_review_prompt_flags_generic(self):
        text = self._render("outline_review_system", scheme_type="深基坑",
                             is_dangerous="否", project_facts="", scheme_basis="",
                             outline_json="[]")
        assert "（N）" in text
        assert ("泛化" in text) or ("占位" in text)

    def test_feedback_prompt_eliminates_generic(self):
        text = self._render("outline_feedback_system", scheme_type="深基坑",
                             project_brief="", project_facts="",
                             original_outline="[]", review_suggestions="")
        assert "（N）" in text
        assert ("泛化" in text) or ("占位" in text)
