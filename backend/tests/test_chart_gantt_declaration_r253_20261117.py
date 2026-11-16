# -*- coding: utf-8 -*-
"""R253（2026-11-17）· R252 遗留收口：缺首行 gantt 的校验/修复/渲染闭环。

R252 让三侧**识别**出 title-first 甘特载荷为 gantt，但后端校验/AI 修复
（image_engine.validate_mermaid / repair_mermaid）与 PIL 主渲染仍按首行 `gantt`
硬分流 —— 缺首行的载荷：
  · validate_mermaid → 落底部「不支持的 Mermaid 类型」→ 正文删块（P0）
  · repair_mermaid 的 dateFormat/axisFormat 确定性修复不生效（P0）
  · PIL 主分支 `"gantt" in code.lower()` 落空 → 走宽松兜底、丢 section（P1）

R253 统一前置归一 chart_validators.ensure_gantt_declaration，四处复用；
并放宽 _looks_like_gantt_body 判据以覆盖「section + 任务」而省略 dateFormat 的变体。

手法：行为实证（validate/repair/幂等/section 透传）+ 反证（不误伤合法首行 /
真实代码 / 普通文本）+ 源码锁（三消费点接线）。
"""
from __future__ import annotations

from pathlib import Path

from app.services.ai.image_engine import repair_mermaid, validate_mermaid
from app.services.ai.mermaid_gantt import _parse_mermaid_gantt
from app.services.chart_validators import (
    _looks_like_gantt_body,
    detect_mermaid_chart_type,
    ensure_gantt_declaration,
)

# 截图取证形态（缺首行 gantt，含 dateFormat/axisFormat/section）
TITLE_FIRST = (
    "title 机电安装施工进度计划\n"
    "dateFormat YYYY-MM-DD\n"
    "axisFormat %m-%d\n"
    "section 施工准备\n"
    "现场勘测与技术交底 :2026-05-18, 5d\n"
    "材料设备招标 :2026-05-20, 7d\n"
    "section 系统调试\n"
    "单机调试 :2026-10-01, 15d\n"
    "竣工验收与移交 :2026-11-11, 8d\n"
)

# 更残缺变体：连 dateFormat/axisFormat 都省，只剩 section + 任务
SECTION_ONLY = (
    "title 计划\n"
    "section 施工准备\n"
    "任务A :2026-05-18, 5d\n"
    "任务B :2026-05-20, 7d\n"
)


class TestEnsureGanttDeclaration:
    def test_prepends_for_title_first(self):
        out = ensure_gantt_declaration(TITLE_FIRST)
        assert out.startswith("gantt\n")
        assert "title 机电安装施工进度计划" in out

    def test_covers_section_only_variant(self):
        assert _looks_like_gantt_body(SECTION_ONLY) is True
        assert ensure_gantt_declaration(SECTION_ONLY).startswith("gantt\n")

    def test_idempotent(self):
        once = ensure_gantt_declaration(TITLE_FIRST)
        assert ensure_gantt_declaration(once) == once

    def test_keeps_existing_gantt(self):
        ok = "gantt\ntitle x\nsection A\n任务:t1,0,5d"
        assert ensure_gantt_declaration(ok) == ok

    def test_never_touches_other_types(self):
        for code in (
            "flowchart TD\n A-->B\n B-->C",
            "graph LR\n A-->B\n B-->C",
            "pie title 占比\n A: 60\n B: 40",
            "普通段落文字",
        ):
            assert ensure_gantt_declaration(code) == code, code


class TestValidateRepairClosedLoop:
    def test_validate_title_first_passes(self):
        ok, reason = validate_mermaid(TITLE_FIRST)
        assert ok is True, reason

    def test_validate_section_only_reaches_gantt_branch(self):
        # 缺 dateFormat 的载荷：validate 补首行 gantt 后进入 gantt 分支，正确报
        # 「缺少 dateFormat」而非落底部「不支持的 Mermaid 类型」（后者会跳过
        # 确定性修复直接删块）。repair 补 dateFormat 后再 validate 通过由下方测试覆盖。
        ok, reason = validate_mermaid(SECTION_ONLY)
        assert ok is False
        assert "dateFormat" in reason
        assert "不支持的 Mermaid 类型" not in reason

    def test_repair_title_first_prepends_and_keeps_dateformat(self):
        out = repair_mermaid(TITLE_FIRST)
        assert out.startswith("gantt")
        assert "dateFormat" in out

    def test_repair_section_only_adds_dateformat_then_validates(self):
        repaired = repair_mermaid(SECTION_ONLY)
        assert repaired.startswith("gantt")
        assert "dateFormat" in repaired
        ok, reason = validate_mermaid(repaired)
        assert ok is True, reason

    def test_registration_still_detects_gantt(self):
        assert detect_mermaid_chart_type(TITLE_FIRST, default="") == "gantt"
        assert detect_mermaid_chart_type(SECTION_ONLY, default="") == "gantt"


class TestRenderSectionPreserved:
    def test_normalized_code_hits_main_branch_and_keeps_section(self):
        # 主分支闸门是 `"gantt" in code.lower()`（mermaid_renderer:590）。归一后
        # 必须含字面 gantt（否则落宽松兜底、丢 section 阶段带），且 PIL 解析器
        # 能从归一后的 code 提取 section + 任务。
        norm = ensure_gantt_declaration(TITLE_FIRST)
        assert "gantt" in norm.lower()
        tasks = _parse_mermaid_gantt(norm)
        assert tasks
        assert {t["section"] for t in tasks} == {"施工准备", "系统调试"}


class TestSourceLock:
    def test_three_consumers_wire_ensure_gantt_declaration(self):
        root = Path(__file__).parents[1]
        ie = (root / "app/services/ai/image_engine.py").read_text(encoding="utf-8")
        mr = (root / "app/services/ai/mermaid_renderer.py").read_text(encoding="utf-8")
        cv = (root / "app/services/chart_validators.py").read_text(encoding="utf-8")
        assert ie.count("ensure_gantt_declaration(") >= 2  # validate + repair
        assert "ensure_gantt_declaration(" in mr
        assert "def ensure_gantt_declaration" in cv
