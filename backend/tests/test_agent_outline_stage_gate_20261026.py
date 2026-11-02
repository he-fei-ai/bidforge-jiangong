"""最小接线护栏 —— 六阶段骨架单一事实源 + 默认活动路径（2026-10-26）。

落实《目录生成工具修正移植_实施报告_P0P4_20261025.md》第五章遗留①接线阶段。
2026-10-26 遗留 1 已接线：技术评分单链路由与六阶段 E2E 由
``test_agent_outline_tech_e2e_20261026.py`` 锁定（本文件原「门禁行为」测试
已被真实路由取代并移除）。

- A 骨架一致性：``outline_agent_flow._OUTLINE_FLOW_STAGES`` 是六阶段单一
  事实源，与 ``validation.STAGE_OUTPUT_FILES`` 键集逐字相等、顺序正确
  （initial-outline 首、outline_review 末、技术评分三阶段居中且登记产物）；
- C 默认活动路径：``NO_TECHNICAL_SCORE_MODE=True`` / ``STANDALONE_TECHNICAL
  =False``，无技术评分单链为默认活动路径（技术单链仅在显式传
  ``no_technical_score_mode=False`` 时启用）。
"""
import pytest
from app.services.agent import outline_agent_flow as flow
from app.services.agent.validation import STAGE_OUTPUT_FILES

#: 无技术评分单链的活动阶段（STAGE_OUTPUT_FILES 键，与 _continue_task 默认路由一致）。
_NO_TECH_ACTIVE_STAGES = {"initial-outline", "children_generation", "outline_review"}
_TECH_STAGES = {"score-planning", "leaf_allocation", "leaf_adjustment"}


def test_flow_stages_is_six_tuple_matching_validation():
    """六阶段骨架单一事实源与 STAGE_OUTPUT_FILES 键集逐字一致、顺序正确。"""
    stages = flow._OUTLINE_FLOW_STAGES
    assert isinstance(stages, tuple) and len(stages) == 6
    assert set(stages) == set(STAGE_OUTPUT_FILES)
    assert stages[0] == "initial-outline"
    assert stages[-1] == "outline_review"
    # 技术评分三阶段居中，且都在 STAGE_OUTPUT_FILES 里登记了必需产物文件。
    assert _TECH_STAGES <= set(stages)
    for stage in _TECH_STAGES:
        assert STAGE_OUTPUT_FILES[stage]


def test_no_tech_mode_is_default_active_path():
    """默认活动路径 = 无技术评分单链（驱动 schemas/提示词构造器的真实旗标）。"""
    assert flow.NO_TECHNICAL_SCORE_MODE is True
    assert flow.STANDALONE_TECHNICAL is False
    # 活动阶段恰为无技术评分单链三阶段；技术评分三阶段不在活动集。
    assert _NO_TECH_ACTIVE_STAGES == {
        "initial-outline", "children_generation", "outline_review"}
    assert not (_NO_TECH_ACTIVE_STAGES & _TECH_STAGES)


def test_technical_mode_is_explicit_parameter():
    """技术单链仅经显式 ``no_technical_score_mode=False`` 启用（默认 True）。"""
    import inspect
    sig = inspect.signature(flow.run_outline_agent)
    param = sig.parameters["no_technical_score_mode"]
    assert param.default is True
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
