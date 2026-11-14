"""R246（2026-11-14）· 内建目录模板标题连续性护栏。

深度探查发现：48 个内建模板经 ``renumber_outline`` 后，有 12 个模板被
``check_outline_continuity`` 判为 ``duplicate_titles`` 非空；由于
``outline_auto_confirm.evaluate`` 把 ``duplicate_titles`` 作为硬前置，
这些模板被套用后会在 8 秒自动确认路径上被硬阻断（``ready=False``）。

本轮修复：仅调整模板数据里的标题字符串（父标题语义不变，子标题加限定词
消除父子/跨章同名），不改任何判据、不改 auto-confirm 行为。

本文件锁定：

  A · 全部 48 个模板重排后 ``duplicate_titles == []`` 且
      ``generic_titles == []``；
  B · 全部 48 个模板 ``outline_auto_confirm.evaluate(..., review)`` 的
      ``ready is True``，且无 ``placeholder_titles``/``duplicate_titles``
      blocker；
  C · 28 个重命名标题在对应模板中逐字存在。
"""

from __future__ import annotations

import pytest
from app.services import outline_templates as ot
from app.services.ai.json_response import renumber_outline
from app.services.outline_auto_confirm import evaluate as auto_confirm_evaluate
from app.services.outline_quality import check_outline_continuity


def _titles(nodes: list) -> list[str]:
    out: list[str] = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or "").strip()
        if title:
            out.append(title)
        out.extend(_titles(n.get("children") or []))
    return out


@pytest.mark.parametrize("key", sorted(ot.BUILDERS))
def test_builtin_template_no_duplicate_titles(key: str) -> None:
    """全部内建模板重排后不得存在同名章节（跨层也计）。"""
    outline = renumber_outline(ot.BUILDERS[key]())
    report = check_outline_continuity(outline)
    assert report["duplicate_titles"] == [], f"{key}: {report['duplicate_titles']}"


@pytest.mark.parametrize("key", sorted(ot.BUILDERS))
def test_builtin_template_no_generic_titles(key: str) -> None:
    """全部内建模板重排后不得存在占位/泛化标题。"""
    outline = renumber_outline(ot.BUILDERS[key]())
    report = check_outline_continuity(outline)
    assert report["generic_titles"] == [], f"{key}: {report['generic_titles']}"


@pytest.mark.parametrize("key", sorted(ot.BUILDERS))
def test_builtin_template_auto_confirm_ready(key: str) -> None:
    """内建模板必须是 auto-confirm 可放行的目录（无重复/占位阻断）。"""
    outline = renumber_outline(ot.BUILDERS[key]())
    report = check_outline_continuity(outline)
    verdict = auto_confirm_evaluate(outline, report)
    blocked_codes = {b["code"] for b in verdict["blockers"]}
    assert verdict["ready"] is True, f"{key}: {verdict['blockers']}"
    assert "placeholder_titles" not in blocked_codes, f"{key}: {verdict['blockers']}"
    assert "duplicate_titles" not in blocked_codes, f"{key}: {verdict['blockers']}"


#: 模板键 → 本轮新增限定词标题（修复后必须逐字存在）。
RENAMED_TITLES: dict[str, tuple[str, ...]] = {
    "scaffold": ("剪刀撑与斜撑设置",),
    "work_at_height": ("施工风险辨识",),
    "confined_space": ("作业实施流程",),
    "quality": ("质量目标指标",),
    "emergency": (
        "施工风险辨识",
        "项目应急领导小组",
        "预警信息报告",
        "专项处置措施",
    ),
    "green": (
        "节材技术措施",
        "节材控制指标",
        "节水技术措施",
        "节水控制指标",
        "节能技术措施",
        "节能控制指标",
        "节地技术措施",
        "节地控制指标",
    ),
    "new_tech": ("施工技术要求", "施工风险辨识", "专项安全措施"),
    "scaffold_ground": ("脚手架防雷接地",),
    "scaffold_attached": ("架体技术参数", "防倾覆与防坠落装置"),
    "scaffold_work_platform": ("平台防倾倒措施",),
    "demolition_manual": ("渣土清运与处置",),
    "demolition_blast": ("爆破技术参数", "盲炮处置措施"),
}


@pytest.mark.parametrize("key", sorted(RENAMED_TITLES))
def test_renamed_titles_present_in_expected_template(key: str) -> None:
    """28 个限定词标题必须在对应模板中逐字存在（防修复回退）。"""
    titles = set(_titles(ot.BUILDERS[key]()))
    missing = [t for t in RENAMED_TITLES[key] if t not in titles]
    assert missing == [], f"{key} 缺修复标题：{missing}"
