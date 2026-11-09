"""图表生成与渲染模块 · R196 深度审计收口（2026-11-09）

六组护栏：A image_engine 修复链（provider 前置 / image_enabled 短路 / 成功缓存
TTL / 死代码移除 / 四类 Mermaid 最低结构校验 / subgraph 配对）；B content_plan
编排步图表维度（往返 / 白名单钳制 / guidance 渲染 / 模板契约锁）；C min_words
实际字数闸；D 缩进图表围栏自愈（dedent_chart_fence 三步之首）；E flowchart_json
转义 + ai_image 图题兜底链 + fix-mermaid ordinal 精确改写；F 源码锁（crossref
注释诚实化 / 死 URL 构造器移除 / charts.py ordinal 调用点）。

背景（本轮定罪基线）：基线图表簇 928p/1f（1f=并行会话 R195 在飞回归
export.py finally 未初始化引用，非本轮面）；本轮全部改动为零迁移、零 DDL、
零新增配置项、返回契约加法式。
"""

import json
import time

import pytest

# ---------------------------------------------------------------------------
# A 组 · image_engine 修复链
# ---------------------------------------------------------------------------


class TestImageEngineR196:
    def _call(self, provider_name="agnes", image_config=None, provider=None):
        from app.services.ai.image_engine import generate_illustration_image

        if provider is None:

            class _P:
                api_key = "pk"
                base_url = "https://p.example"
                model = "text-model"

            provider = _P()
        return generate_illustration_image(
            provider, "一张施工示意图", provider_name=provider_name, image_config=image_config
        )

    @pytest.mark.asyncio
    async def test_a1_image_enabled_false_short_circuits(self, monkeypatch):
        """image_enabled=False 显式关闭生图：直接 return None，不发任何模型调用。"""
        from app.services.ai.image_providers import registry as reg

        def _boom(*_a, **_k):
            raise AssertionError("image_enabled=False 不得构建任何适配器/发起调用")

        monkeypatch.setattr(reg.ProviderRegistry, "get_adapter", _boom)
        out = await self._call(image_config={"image_enabled": False, "image_model": "m"})
        assert out is None

    @pytest.mark.asyncio
    async def test_a2_user_provider_always_first(self, monkeypatch):
        """R196-1：provider_name 在降级链内也必须排队首（旧实现被 qwen_image 抢先）。"""
        from app.services.ai.image_providers import registry as reg

        calls: list[tuple[str, str]] = []

        class _Adapter:
            async def generate(self, *, prompt, model, size, ratio):
                return {"image_url": f"http://ok/{model}"}

        # classmethod 被替换后按调用点的位置参数形态（provider_id, config）传参，
        # 用 *args 自适应绑定差异。
        def _fake_get_adapter(*args):
            provider_id, config = args[-2], args[-1]
            calls.append((str(provider_id), str(config.get("model") or "")))
            return _Adapter()

        monkeypatch.setattr(reg.ProviderRegistry, "get_adapter", _fake_get_adapter)
        out = await self._call(
            provider_name="agnes",
            image_config={"image_enabled": True, "image_model": "my-image-model", "image_api_key_enc": "k"},
        )
        assert out == "http://ok/my-image-model"
        # 队首 = 用户配置的 provider + 用户所选模型（旧实现首个调用是 qwen_image+硬编码模型）
        assert calls[0] == ("agnes", "my-image-model")

    @pytest.mark.asyncio
    async def test_a3_success_cache_ttl_expiry(self, monkeypatch):
        """R196-3：成功 URL 超过 TTL 后不再命中（签名 URL 死链防护）。"""
        import hashlib

        from app.services.ai import image_engine as ie

        # 缓存 key 与 generate_illustration_image 同式（prompt|style|size|provider 的 md5）
        key = hashlib.md5(
            f"一张施工示意图|engineering_diagram|16:9|agnes".encode()
        ).hexdigest()
        with ie._image_cache_lock:
            ie._IMAGE_CACHE.clear()
            ie._IMAGE_CACHE[key] = ("http://stale/signed-url", time.monotonic() - ie._IMAGE_SUCCESS_TTL - 10)
        # image_enabled=False 兜底短路（在缓存检查之后），保证不发起真实调用
        out = await self._call(image_config={"image_enabled": False})
        assert out is None
        with ie._image_cache_lock:
            assert key not in ie._IMAGE_CACHE, "过期条目必须被清除"
            ie._IMAGE_CACHE.clear()

    @pytest.mark.asyncio
    async def test_a4_success_cache_fresh_hit(self, monkeypatch):
        """TTL 内的正常命中逐字保持：返回 URL 不走生成链。"""
        import hashlib

        from app.services.ai import image_engine as ie

        key = hashlib.md5(f"一张施工示意图|engineering_diagram|16:9|agnes".encode()).hexdigest()
        ie._image_cache_set(key, "http://fresh/url")
        out = await self._call(image_config={"image_enabled": False})
        assert out == "http://fresh/url"
        with ie._image_cache_lock:
            ie._IMAGE_CACHE.clear()

    def test_a5_validate_four_diagram_types_minimal_structure(self):
        """R196-5：四类此前无条件放行 → 残块登记成图；现补最低结构校验。"""
        from app.services.ai.image_engine import validate_mermaid

        # sequenceDiagram：有消息行 = 合法；只有 header = 残块
        ok, _ = validate_mermaid("sequenceDiagram\n    A->>B: hello")
        assert ok
        bad, why = validate_mermaid("sequenceDiagram\n    participant A")
        assert not bad and "消息" in why
        # classDiagram
        ok, _ = validate_mermaid("classDiagram\n    class Foo\n    Foo : +int x")
        assert ok
        bad, _ = validate_mermaid("classDiagram")
        assert not bad
        # stateDiagram
        ok, _ = validate_mermaid("stateDiagram-v2\n    [*] --> s1\n    s1 --> s2")
        assert ok
        bad, why = validate_mermaid("stateDiagram-v2\n    note right of s1\n    x\n    end note")
        assert not bad
        # erDiagram
        ok, _ = validate_mermaid("erDiagram\n    CUSTOMER ||--o{ ORDER : places")
        assert ok
        bad, why = validate_mermaid("erDiagram\n    CUSTOMER {\n        string name\n    }")
        assert not bad and "关系" in why

    def test_a6_validate_flowchart_subgraph_balance(self):
        """R196-6：subgraph/end 不配对判非法（残缺图不再放行到渲染轨）。"""
        from app.services.ai.image_engine import validate_mermaid

        ok, _ = validate_mermaid(
            "flowchart TD\n    subgraph S1\n        A[开始] --> B[处理]\n    end\n    B --> C[归档]"
        )
        assert ok
        bad, why = validate_mermaid(
            "flowchart TD\n    subgraph S1\n        A[开始] --> B[处理]\n    B --> C[归档]"
        )
        assert not bad and "subgraph/end" in why


# ---------------------------------------------------------------------------
# B 组 · content_plan 编排步图表维度（R196-7）
# ---------------------------------------------------------------------------


class TestContentPlanChartDimR196:
    def test_b1_roundtrip(self):
        from app.services.content_plan import ContentPlanData

        p = ContentPlanData(chart_needed=True, chart_types=["gantt"], chart_note="进度量化")
        p2 = ContentPlanData.from_json(p.to_json())
        assert p2.chart_needed is True
        assert p2.chart_types == ["gantt"]
        assert p2.chart_note == "进度量化"
        # 旧数据（无图表键）向后兼容：零值兜底
        legacy = ContentPlanData.from_json('{"writing_focus":"x","table_needed":true}')
        assert legacy.chart_needed is False and legacy.chart_types == []

    def test_b2_validate_whitelist_clamp(self):
        from app.services.content_plan import ContentPlanData

        p = ContentPlanData.validate(
            {"chart_needed": "true", "chart_types": ["gantt", "pie", "bogus", 42], "chart_note": "r"},
        )
        assert p.chart_needed is True
        assert p.chart_types == ["gantt"], "白名单外类型（pie/bogus/非 str）一律丢弃"
        # chart_needed=false 时类型清零
        p2 = ContentPlanData.validate({"chart_needed": False, "chart_types": ["gantt"]})
        assert p2.chart_types == []
        # ai_image 不进编排建议白名单（计费敏感，导出期独立预算管控）
        p3 = ContentPlanData.validate({"chart_needed": True, "chart_types": ["ai_image"]})
        assert p3.chart_types == []

    def test_b3_guidance_renders_chart_advice(self):
        from app.services.content_plan import ContentPlanData, render_content_plan_guidance

        out = render_content_plan_guidance(
            ContentPlanData(chart_needed=True, chart_types=["flowchart", "gantt"], chart_note="工序可流程化")
        )
        assert "建议本章配图：flowchart、gantt" in out and "工序可流程化" in out
        assert "自主判定" in out, "软建议立场：最终由正文生成步自主裁量"
        no_chart = render_content_plan_guidance(ContentPlanData(writing_focus="x"))
        assert "建议本章配图" not in no_chart

    def test_b4_prompt_contract_lock(self):
        """模板契约锁：content_plan_system/user 必须声明图表决策输出（7 类型白名单）。"""
        from app.services.ai.prompts._registry import render

        sys_txt = render("content_plan_system")
        assert "配图" in sys_txt and "chart_needed" in sys_txt
        assert "flowchart" in sys_txt and "timeline" in sys_txt
        user_txt = render(
            "content_plan_user",
            scheme_name="s",
            scheme_type="t",
            section_number="1",
            section_title="t",
            section_description="d",
            word_budget=800,
            parent_chain="p",
            sibling_lines="s",
            project_brief="b",
            knowledge_names_text="- k",
            fact_titles_text="- f",
        )
        assert '"chart_needed"' in user_txt and '"chart_types"' in user_txt and '"chart_note"' in user_txt
        assert "chart_types 仅能取" in user_txt, "7 类型白名单硬约束必须在模板里"


# ---------------------------------------------------------------------------
# C 组 · min_words 实际字数闸（R196-8）
# ---------------------------------------------------------------------------


class TestMinWordsGateR196:
    _GANTT = '{"type":"gantt","title":"进度","tasks":[{"name":"准备","start":"2026-01-01","end":"2026-01-05"}]}'

    def _plan(self, prose: str, *, enforce: bool):
        from app.routers._chart_pipeline import build_inline_chart_plan

        content = prose + "\n\n```chart-json\n" + self._GANTT + "\n```\n"
        dropped: list = []
        new_content, rows = build_inline_chart_plan(
            "sch", "sec", content, enforce_limits=enforce, dropped_out=dropped
        )
        return new_content, rows, dropped

    def test_c1_short_prose_chart_dropped(self):
        """实际正文 <200 字（剔除围栏）→ 删块 + reason=min_words。"""
        new_content, rows, dropped = self._plan("短内容。", enforce=True)
        assert "chart-json" not in new_content
        assert rows == []
        assert any(d.get("reason") == "min_words" for d in dropped)

    def test_c2_long_prose_chart_kept(self):
        prose = "本章阐述施工进度安排。" + "施工准备与主体施工穿插进行，确保工期与质量目标同步达成。" * 12
        new_content, rows, _dropped = self._plan(prose, enforce=True)
        assert "chart-json" in new_content
        assert len(rows) == 1

    def test_c3_manual_save_path_unaffected(self):
        """enforce_limits=False（手动保存路径）不拦 min_words —— 显式意图优先。"""
        new_content, _rows, _dropped = self._plan("短内容。", enforce=False)
        assert "chart-json" in new_content


# ---------------------------------------------------------------------------
# D 组 · 缩进图表围栏自愈（R196-9）
# ---------------------------------------------------------------------------


class TestIndentedFenceSelfHealR196:
    def _fix(self, content):
        from app.services.content_utils import auto_fix_unclosed_fences

        return auto_fix_unclosed_fences(content)

    def test_d1_dedent_chart_fence(self):
        src = "进度如下。\n\n    ```mermaid\n    graph TD\n    A[开始] --> B[结束]\n    ```\n\n正文继续。"
        fixed, log = self._fix(src)
        assert "    ```mermaid" not in fixed and "```mermaid" in fixed
        assert "    ```\n" not in fixed, "闭围栏行同样反缩进"
        assert fixed.count("```") == 2
        assert log and log[0]["action"] == "dedent_chart_fence" and log[0]["lang"] == "mermaid"

    def test_d2_idempotent(self):
        src = "内容。\n\n   ```chart-json\n   {\"type\":\"gantt\"}\n   ```\n"
        fixed, log = self._fix(src)
        fixed2, log2 = self._fix(fixed)
        assert fixed2 == fixed and log2 == [], "修复后再跑必须零命中（幂等）"

    def test_d3_non_chart_code_block_untouched(self):
        """普通代码块（非图表家族 lang）的缩进是刻意排版，不得触碰。"""
        src = "说明：\n\n    ```python\n    print(1)\n    ```\n"
        fixed, log = self._fix(src)
        assert "    ```python" in fixed
        assert not any(x["action"] == "dedent_chart_fence" for x in log)

    def test_d4_deep_indent_not_touched(self):
        """超过 8 空格的深缩进按嵌套排版处理，不自愈。"""
        src = "内容。\n\n         ```mermaid\n         graph TD\n         A --> B\n         ```\n"
        fixed, log = self._fix(src)
        assert "         ```mermaid" in fixed
        assert not any(x["action"] == "dedent_chart_fence" for x in log)

    def test_d5_fixes_log_uses_additive_action_key(self):
        """既有 fixes_log 契约不被破坏：action 键区分布 + 旧 action 仍可达。"""
        src = "未闭合示例：\n\n```mermaid\ngraph TD\nA --> B\n"
        _fixed, log = self._fix(src)
        assert any(x["action"] == "append_close" for x in log), "第 2 步既有行为保持"


# ---------------------------------------------------------------------------
# E 组 · 转义 / 图题兜底 / ordinal 精确改写（R196-11/12/14）
# ---------------------------------------------------------------------------


class TestEscapeTitleOrdinalR196:
    def test_e1_flowchart_json_label_escape(self):
        from app.services.ai.mermaid_flowchart import flowchart_json_to_mermaid

        out = flowchart_json_to_mermaid(
            {
                "direction": "TD",
                "nodes": [
                    {"id": "A", "label": 123},  # 数字 label：旧实现 AttributeError → 整段空串
                    {"id": "B", "label": "含|竖线"},
                ],
                "edges": [{"from": "A", "to": "B", "label": "是|否"}],
            }
        )
        assert out, "数字 label 不得让整段转换失败"
        assert "123" in out
        assert out.count("|") == 2, "label 内竖线必须被替换（仅保留边定界符两个）"

    def test_e2_ai_image_title_fallback_chain(self):
        """ai_image 图题兜底链与 chart-json 分支同口径：题注 > 载荷 title > 引导语。"""
        from app.services.content_blocks import _parse_content_blocks

        content = (
            "施工布置图如下：\n\n```ai_image\n"
            + json.dumps({"prompt": "一张基坑支护示意图", "style": "engineering_diagram"}, ensure_ascii=False)
            + "\n```\n"
        )
        blocks = _parse_content_blocks(content)
        ai = [b for b in blocks if b.get("type") == "ai_image"]
        assert ai and ai[0]["title"] == "施工布置图", "载荷无 title 时兜底引导语（既有后缀剥离口径：『如下：』被剥）"

        content2 = content.replace(
            json.dumps({"prompt": "一张基坑支护示意图", "style": "engineering_diagram"}, ensure_ascii=False),
            json.dumps({"prompt": "p", "title": "基坑支护剖面"}, ensure_ascii=False),
        )
        blocks2 = _parse_content_blocks(content2)
        ai2 = [b for b in blocks2 if b.get("type") == "ai_image"]
        assert ai2 and ai2[0]["title"] == "基坑支护剖面", "载荷 title 优先于引导语"

    def test_e3_ordinal_rewrite_only_target_block(self):
        """R196-14 判据本体：同章两块相同代码，ordinal 改写只动指定块。"""
        from app.routers._chart_pipeline import _apply_chart_fence_edits, _scan_chart_fences_full

        block = "```mermaid\ngraph TD\nA[开始] --> B[结束]\n```"
        content = f"甲段。\n\n{block}\n\n乙段。\n\n{block}\n"
        found = _scan_chart_fences_full(content)
        same = [o for ct, cd, o in found if cd == "graph TD\nA[开始] --> B[结束]"]
        assert len(same) == 2, "前置：确有两块相同代码"
        new_content = _apply_chart_fence_edits(content, {same[0]: "graph TD\nA[开始] --> C[结束]"})
        assert "A[开始] --> C[结束]" in new_content
        assert new_content.count("graph TD\nA[开始] --> B[结束]") == 1, "第二块保持原样"

    def test_e4_fix_mermaid_uses_ordinal_rewrite(self):
        """R196-14 调用点源码锁：fix-mermaid 写回必须走 _apply_chart_fence_edits。"""
        from pathlib import Path

        src = (Path(__file__).parents[1] / "app" / "routers" / "charts.py").read_text(encoding="utf-8")
        assert "_apply_chart_fence_edits" in src and "_scan_chart_fences_full" in src
        assert src.count("_rewrite_code_block(old_content, code, fixed)") == 1, "旧行为仅允许作为回退保留一处"


# ---------------------------------------------------------------------------
# F 组 · 源码锁
# ---------------------------------------------------------------------------


class TestSourceLocksR196:
    def test_f1_dead_url_builder_removed(self):
        """R196-4：零调用的 build_illustration_url（内嵌硬编码第三方端点）已删除。"""
        import app.services.ai.image_engine as ie

        assert not hasattr(ie, "build_illustration_url")
        from pathlib import Path

        src = Path(ie.__file__).read_text(encoding="utf-8")
        assert "trae-api-cn" not in src, "出网端点残留必须清零"

    def test_f2_crossref_comment_honest(self):
        """R196-13：config 注释不再引用从未落地的 crossref_rewrite_enabled。"""
        from pathlib import Path

        src = (Path(__file__).parents[1] / "app" / "config.py").read_text(encoding="utf-8")
        assert "crossref_rewrite_enabled" not in src

    def test_f3_duplicate_warning_removed(self):
        """R196-4：provider 异常分支的重复 WARNING 已收敛为分级单条。"""
        from pathlib import Path

        src = (Path(__file__).parents[1] / "app" / "services" / "ai" / "image_engine.py").read_text(encoding="utf-8")
        assert src.count('logger.warning(\n                    "generate_illustration_image: Provider \'%s\' 失败 (%s)，尝试下一个",') <= 1

    def test_f4_min_words_judgement_single_source(self):
        """min_words 判据与提示词阈值同源（_CHART_MIN_WORDS 唯一常量）。"""
        import app.routers._chart_pipeline as cp

        assert cp._CHART_MIN_WORDS == 200
        from pathlib import Path

        src = Path(cp.__file__).read_text(encoding="utf-8")
        assert "_actual_words = text_word_count(content)" in src
        assert "_actual_words < _CHART_MIN_WORDS" in src
