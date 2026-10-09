"""目录生成模块专项修复回归测试（2026-10-07）

覆盖本轮两处修复：

1. check_outline_continuity 编号错位误报（outline_quality.py）
   ----------------------------------------------------------
   位置路径旧实现用 ``enumerate`` 原始下标 ``j+`` 计数，与
   ``numbering.renumber_outline_nodes``「非 dict 项跳过且不占号」的口径不一致。
   normalize（renumber 不删除非 dict 项、只跳过不给 id）后再做连续性校验时，
   脏数据数组（AI 畸形输出 / 手工编辑残留，如 ``[dict, "字符串残片", dict]``）中
   后续合法节点的真实 id（"2"）与路径（"3"）永不相等，被误报 numbering_mismatch，
   把编号完全正确的目录判成 ``ok=False``（生成日志误报「目录连续性问题：编号错位」）。

2. 长方案分步路径漏挂生成后质量自检（sse_handlers.py）
   ---------------------------------------------------
   短方案直出路径在 _review_and_fix_outline 后调 _attach_quality_report
   （连续性 / 名称覆盖 / 冗余 → review.quality，前端 summarizeOutlineQuality 消费），
   长方案分步路径（word_budget ≥ OUTLINE_STEPWISE_MIN_WORDS，章节最多最易缺章）
   漏挂 —— 前端对长方案永远拿不到质量自检结果。接线契约测试锁定两条路径都必须挂。
"""
import inspect

import app.routers.sse_handlers as sh
from app.services.ai.json_response import renumber_outline
from app.services.outline_quality import check_outline_continuity


class TestContinuityNumberingParity:
    """连续性校验的位置路径必须与 renumber 占号口径一致。"""

    def test_non_dict_top_level_items_do_not_occupy_slots(self):
        # 顶层混入字符串/数字残片（renumber 会跳过但不从数组删除）
        outline = [
            {"title": "工程概况", "children": []},
            "模型输出的字符串残片",
            42,
            {"title": "编制依据", "children": []},
        ]
        renumber_outline(outline)
        rep = check_outline_continuity(outline)
        assert rep["numbering_mismatch"] == []
        assert rep["ok"] is True

    def test_non_dict_nested_children_do_not_occupy_slots(self):
        # 嵌套层：第一章的 children 中混有非 dict 项，后续合法子节点不得误报
        outline = [
            {"title": "工程概况", "children": [
                {"title": "项目简介", "children": []},
                None,
                "残片",
                {"title": "建设条件", "children": []},
            ]},
            {"title": "编制依据", "children": []},
        ]
        renumber_outline(outline)
        rep = check_outline_continuity(outline)
        assert rep["numbering_mismatch"] == [], rep["numbering_mismatch"]
        assert rep["ok"] is True
        #  renumber 口径：非 dict 不占号 → 两个合法子节点是 1.1 / 1.2
        assert outline[0]["children"][0]["id"] == "1.1"
        assert outline[0]["children"][3]["id"] == "1.2"

    def test_real_numbering_mismatch_still_detected(self):
        # 防改瞎：真错位（id 与位置不符）必须仍能检出
        outline = [
            {"id": "1", "title": "工程概况", "children": []},
            {"id": "5", "title": "编制依据", "children": []},
        ]
        rep = check_outline_continuity(outline)
        assert rep["numbering_mismatch"], "真实编号错位必须被检出"
        mismatch = rep["numbering_mismatch"][0]
        assert mismatch["id"] == "5"
        assert mismatch["expected"] == "2"
        assert rep["ok"] is False

    def test_clean_renumbered_tree_has_no_mismatch(self):
        # 常规干净树（三级）renumber 后连续性应完全通过
        outline = [
            {"title": "第一章 工程概况", "children": [
                {"title": "1.1 项目简介", "children": [
                    {"title": "1.1.1 参建单位", "children": []},
                ]},
            ]},
            {"title": "第二章 编制依据", "children": [
                {"title": "法规", "children": []},
            ]},
        ]
        renumber_outline(outline)
        rep = check_outline_continuity(outline)
        assert rep["numbering_mismatch"] == []
        assert rep["level_gaps"] == []


class TestStepwiseQualityReportWiring:
    """长/短两条生成路径都必须挂生成后质量自检（接线契约）。"""

    def test_generate_outline_endpoint_attaches_quality_on_both_paths(self):
        src = inspect.getsource(sh.generate_outline)
        # 短方案直出 + 长方案分步各一次；本断言在「长路径漏挂」时为 1（失败）。
        assert src.count("_attach_quality_report(") == 2, (
            "短方案与长方案分步路径都必须在审核后挂 _attach_quality_report，"
            "否则长方案 completed 事件的 review 载荷缺少 quality 段"
        )

    def test_attach_quality_report_is_fail_soft(self):
        # 质量自检异常绝不影响生成结果（编排层无 try 包裹也安全的前提）
        review: dict = {}
        rep = sh._attach_quality_report(["x", None, 1], None, review)
        assert isinstance(rep, dict)
