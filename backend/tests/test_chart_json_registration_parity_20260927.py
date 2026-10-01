# -*- coding: utf-8 -*-
"""图表登记 / 导出 三侧口径一致性回归（2026-09-27）。

背景（幽灵图修复）:
``routers/_chart_pipeline.py::_scan_chart_fences_full`` 此前只认 chart-json 载荷里
**显式**的 ``type`` 键，而导出侧 ``services/content_blocks._parse_content_blocks``
对缺 ``type`` 的块会调 ``infer_chart_type_from_payload`` **按结构兜底推断**。
两侧口径分叉导致一个不带 ``type`` 的合法 chart-json 块：
  · 导出时被渲染成图并**占用图号**；
  · ``chart_predictions`` 里却**查无此图**（不进图表清单 / 导出预检 / 无法定位修复）；
  · 同时**绕过**「每章 ≤1」「同类型全方案 ≤3」的配图上限（上限判定全在登记侧）。

本文件锁定：登记侧与导出侧对**同一批 chart-json 围栏**必须给出同一组 chart_type
（幽灵图恒为空集），并覆盖上限回归。
"""
import json
import os

import pytest

from app.routers._chart_pipeline import (
    _ALL_CHART_TYPES,
    _scan_chart_fences_full,
    build_inline_chart_plan,
    extract_inline_charts,
    has_inline_charts,
)
from app.services.content_blocks import _parse_content_blocks

# 各类图表的「无 type 字段」载荷（靠结构推断类型）
NO_TYPE_PAYLOADS = {
    "architecture": '{"root": {"name": "项目部", "children": [{"name": "技术部"}]}}',
    "gantt": '{"tasks": [{"id": "a", "name": "施工"}]}',
    "flowchart": '{"steps": [{"id": "s1", "name": "准备"}]}',
    "layout": '{"zones": [{"name": "危险区", "x": 0, "y": 0, "w": 1, "h": 1}]}',
}


def _wrap(payload: str) -> str:
    return "前言段落。\n\n```chart-json\n" + payload + "\n```\n\n后文。"


class TestChartJsonRegistrationParity:
    """登记侧与导出侧口径必须一致（幽灵图回归）。"""

    @pytest.mark.parametrize("expected_ct,payload", sorted(NO_TYPE_PAYLOADS.items()))
    def test_no_type_payload_is_registered_as_expected(self, expected_ct, payload):
        """无 type 字段的合法 chart-json 块必须被登记，且类型与导出侧一致。"""
        content = _wrap(payload)
        reg = _scan_chart_fences_full(content)
        assert [ct for ct, _code, _ord in reg] == [expected_ct], (
            f"{expected_ct} 载荷未被登记为 {expected_ct}，实际 {reg}")

    @pytest.mark.parametrize("expected_ct,payload", sorted(NO_TYPE_PAYLOADS.items()))
    def test_registration_matches_export(self, expected_ct, payload):
        """同一块的登记类型必须与导出块类型逐字相同（不产生幽灵图）。"""
        content = _wrap(payload)
        reg_types = [ct for ct, _c, _o in _scan_chart_fences_full(content)]
        exp_types = [b["chart_type"] for b in _parse_content_blocks(content)
                     if b["type"] == "chart"]
        # 幽灵图判据：导出能出图但登记侧查不到
        assert exp_types == reg_types, (
            f"登记/导出口径分叉：登记={reg_types} 导出={exp_types}")
        assert not (exp_types and not reg_types), "幽灵图：成稿有图但未登记"

    def test_explicit_type_still_wins(self):
        """显式合法 type 仍优先于结构推断（不得被推断覆盖）。"""
        content = _wrap('{"type": "labor", "phases": [{"name": "p"}], '
                        '"categories": [{"name": "c"}], "data": [[1]]}')
        assert [ct for ct, _c, _o in _scan_chart_fences_full(content)] == ["labor"]

    def test_unrenderable_payload_not_registered(self):
        """结构无法判定的载荷不登记（与导出侧「整块跳过」同口径）。"""
        # 非 dict / 空对象 / 无法推断结构
        for payload in ('[{"a": 1}]', '{}', '{"foo": "bar"}'):
            content = _wrap(payload)
            reg = _scan_chart_fences_full(content)
            exp = [b for b in _parse_content_blocks(content) if b["type"] == "chart"]
            assert not reg and not exp, f"{payload} 不应产生图表（登记={reg} 导出={exp}）"

    def test_invalid_json_not_registered(self):
        """非法 JSON 块不登记、不抛异常。"""
        content = _wrap("{not json at all")
        assert _scan_chart_fences_full(content) == []
        assert [b for b in _parse_content_blocks(content)
                if b["type"] == "chart"] == []

    def test_all_chart_types_are_pil_renderable(self):
        """白名单与渲染器可渲染集合必须同口径（否则会造出新的幽灵图）。"""
        from app.services.chart_validators import PIL_RENDERABLE_CHART_TYPES
        assert set(_ALL_CHART_TYPES) == set(PIL_RENDERABLE_CHART_TYPES)


class TestNoTypePayloadRespectsLimits:
    """无 type 载荷不得绕过配图上限（此前正是靠"未登记"绕过的）。"""

    def test_per_section_limit_applies_to_inferred_blocks(self):
        """同章两个不同类型的无 type 块：第 2 个必须被上限裁掉。"""
        content = (
            "```chart-json\n" + NO_TYPE_PAYLOADS["architecture"] + "\n```\n\n"
            "中间段落。\n\n"
            "```chart-json\n" + NO_TYPE_PAYLOADS["gantt"] + "\n```\n"
        )
        new_content, rows = build_inline_chart_plan(
            "scheme-1", "section-1", content,
            enforce_limits=True, scheme_type_counts={})
        assert len(rows) == 1, f"每章上限应只保留 1 张，实际登记 {len(rows)}"
        # 被裁掉的那块必须真的从正文移除（不得留下幽灵块）
        assert new_content.count("```chart-json") == 1, (
            "超限块必须同步从正文移除，否则正文与登记表分叉")

    def test_scheme_type_limit_applies_to_inferred_blocks(self):
        """全方案同类型上限对推断类型同样生效。"""
        content = "```chart-json\n" + NO_TYPE_PAYLOADS["gantt"] + "\n```\n"
        # 已有 3 个 gantt（已达默认上限 3）
        _new, rows = build_inline_chart_plan(
            "scheme-1", "section-1", content,
            enforce_limits=True, scheme_type_counts={"gantt": 3})
        assert rows == [], "同类型已达上限时不应再登记"

    def test_extract_inline_charts_sees_inferred_blocks(self):
        """对外清单口径（extract_inline_charts）也必须能看到无 type 块。"""
        content = _wrap(NO_TYPE_PAYLOADS["gantt"])
        assert extract_inline_charts(content) == [("gantt", NO_TYPE_PAYLOADS["gantt"])]
        assert has_inline_charts(content) is True

    def test_payload_preserved_in_envelope(self):
        """登记载荷必须走 chart_payload 规范信封的 data 分支（形状不倒退）。"""
        from app.services.chart_payload import chart_payload_shape
        content = _wrap(NO_TYPE_PAYLOADS["gantt"])
        _new, rows = build_inline_chart_plan(
            "scheme-1", "section-1", content, enforce_limits=False)
        assert len(rows) == 1
        payload = rows[0][-1]
        assert chart_payload_shape(payload) == "envelope:data", (
            f"结构化载荷必须走 data 分支，实际 {chart_payload_shape(payload)}")
        # 载荷内容必须与正文里的原始 JSON 等价（不得丢字段）
        # 注意 build_chart_envelope 的 data 分支直接内嵌 dict（非 JSON 字符串）
        assert json.loads(payload)["data"] == json.loads(NO_TYPE_PAYLOADS["gantt"])



class TestFenceConstantsSingleSource:
    """围栏常量必须与 content_blocks 唯一实现同源（防"下沉后残留副本"再次分叉）。"""

    def test_max_lines_forwarded(self):
        import app.routers._chart_pipeline as pipe
        from app.services import content_blocks
        assert pipe.MAX_INLINE_CODE_BLOCK_LINES is content_blocks.MAX_INLINE_CODE_BLOCK_LINES

    def test_fence_langs_forwarded(self):
        import app.routers._chart_pipeline as pipe
        from app.services import content_blocks
        assert pipe.INLINE_CHART_FENCE_LANGS is content_blocks.INLINE_CHART_FENCE_LANGS

    def test_read_fenced_block_uses_same_threshold(self):
        """read_fenced_block 的默认 max_lines 必须就是那个唯一常量。"""
        import inspect
        from app.services import content_blocks
        default = inspect.signature(
            content_blocks.read_fenced_block).parameters["max_lines"].default
        assert default == content_blocks.MAX_INLINE_CODE_BLOCK_LINES


class TestRenderEndpointTypeInference:
    """`POST /charts/render` 必须与登记/导出侧同口径推断类型（预览≠导出）。"""

    def test_infers_type_from_payload_structure(self):
        """省略 type 的载荷必须按结构推断出正确类型。"""
        from app.routers.charts import _infer_payload_type
        assert _infer_payload_type(NO_TYPE_PAYLOADS["gantt"]) == "gantt"
        assert _infer_payload_type(NO_TYPE_PAYLOADS["architecture"]) == "architecture"
        assert _infer_payload_type(NO_TYPE_PAYLOADS["flowchart"]) == "flowchart"
        assert _infer_payload_type(NO_TYPE_PAYLOADS["layout"]) == "layout"

    def test_explicit_type_wins(self):
        """显式合法 type 原样返回（调用方显式声明优先）。"""
        from app.routers.charts import _infer_payload_type
        assert _infer_payload_type('{"type": "gantt", "tasks": [{"id": "a"}]}') == "gantt"

    def test_mermaid_syntax_returns_empty(self):
        """Mermaid 语法载荷不推断（类型由参数决定，绝不误伤）。"""
        from app.routers.charts import _infer_payload_type
        assert _infer_payload_type("graph TD\nA --> B\nB --> C") == ""
        assert _infer_payload_type("flowchart TD; A-->B; B-->C") == ""

    def test_invalid_json_returns_empty(self):
        """非法 JSON / 非对象不推断（不抛异常）。"""
        from app.routers.charts import _infer_payload_type
        assert _infer_payload_type("{not json") == ""
        assert _infer_payload_type("[1,2,3]") == ""
        assert _infer_payload_type("") == ""
        assert _infer_payload_type("   ") == ""

    async def test_render_corrects_fabricated_client_type(self, monkeypatch):
        """回归：前端传来捏造的 labor 类型时，后端按结构纠正为 gantt。"""
        from app.routers import charts as charts_router

        seen: dict = {}

        class _FakeCache:
            @staticmethod
            def get_or_render(code, chart_type, fmt, skip_http, allow_pil=None):
                seen["chart_type"] = chart_type
                seen["code"] = code
                return None   # 渲染失败即可，断言只看纠正后的类型

        import app.services.ai.mermaid_renderer as mr
        monkeypatch.setattr(mr, "_chart_cache", _FakeCache)

        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await charts_router.render_chart({
                "chart_type": "labor",              # 前端旧实现的凭空兜底
                "code": NO_TYPE_PAYLOADS["gantt"],  # 实际是甘特图
                "skip_http": True,
            })
        assert seen["chart_type"] == "gantt", (
            f"应按载荷结构纠正为 gantt，实际 {seen['chart_type']}")

    async def test_render_keeps_valid_mermaid_type(self, monkeypatch):
        """Mermaid 语法载荷的显式 chart_type 必须原样保留（向后兼容）。"""
        from app.routers import charts as charts_router

        seen: dict = {}

        class _FakeCache:
            @staticmethod
            def get_or_render(code, chart_type, fmt, skip_http, allow_pil=None):
                seen["chart_type"] = chart_type
                return None

        import app.services.ai.mermaid_renderer as mr
        monkeypatch.setattr(mr, "_chart_cache", _FakeCache)

        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await charts_router.render_chart({
                "chart_type": "flowchart",
                "code": "graph TD\nA --> B\nB --> C",
                "skip_http": True,
            })
        assert seen["chart_type"] == "flowchart"

    async def test_render_rejects_empty_code(self):
        """空代码仍应 400（不因推断逻辑而放宽）。"""
        from app.routers import charts as charts_router
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await charts_router.render_chart({"chart_type": "gantt", "code": ""})
        assert ei.value.status_code == 400


class TestFrontendChartTypesParity:
    """前端白名单必须与后端 PIL_RENDERABLE_CHART_TYPES **逐项一致**。

    前端 vitest 无 node 类型声明（`fs`/`path`/`__dirname` 均无类型，tsc 会报错），
    无法在 vitest 里读盘做跨语言比对，故由本用例读取前端源码锁定一致性。
    """

    _FE_FILE = ("frontend", "src", "utils", "chartTypes.ts")

    def _frontend_types(self) -> set[str]:
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "..", *self._FE_FILE)
        assert os.path.exists(path), f"找不到前端白名单文件：{path}"
        src = open(path, encoding="utf-8").read()
        m = re.search(
            r"RENDERABLE_CHART_TYPES[^=]*=\s*new Set\(\[([\s\S]*?)\]\)", src)
        assert m, "未能从 chartTypes.ts 解析 RENDERABLE_CHART_TYPES"
        return set(re.findall(r'["\']([a-z_]+)["\']', m.group(1)))

    def test_frontend_whitelist_matches_backend(self):
        from app.services.chart_validators import PIL_RENDERABLE_CHART_TYPES
        fe = self._frontend_types()
        assert fe, "前端白名单解析结果为空"
        assert fe == set(PIL_RENDERABLE_CHART_TYPES), (
            "前后端图表白名单漂移："
            f"前端多 {sorted(fe - set(PIL_RENDERABLE_CHART_TYPES))}、"
            f"前端缺 {sorted(set(PIL_RENDERABLE_CHART_TYPES) - fe)}")

    def test_markdownrenderer_has_no_fabricated_labor_fallback(self):
        """前端不得再出现 `obj.type || "labor"` 式的凭空兜底。

        匹配前先剥离行注释与块注释：修复说明本身要引用这段旧代码，
        否则"注释里写着旧写法"会把护栏自身变成假阳性。
        """
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "frontend", "src", "components", "MarkdownRenderer.tsx")
        src = open(path, encoding="utf-8").read()
        code_only = re.sub(r"/\*[\s\S]*?\*/", "", src)      # 块注释
        code_only = re.sub(r"//[^\n]*", "", code_only)      # 行注释
        assert not re.search(r'type\s*\|\|\s*["\']labor["\']', code_only), (
            "MarkdownRenderer 又出现了硬编码 labor 兜底 —— "
            "会让省略 type 的甘特图/架构图载荷被送进 labor 渲染器")
        assert "RENDERABLE_CHART_TYPES.has(declared)" in code_only, (
            "MarkdownRenderer 必须走共享白名单判定，不得自行内联类型表")


