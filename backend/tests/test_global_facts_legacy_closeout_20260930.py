"""全局事实 · 遗留项收口护栏（2026-09-30 第十三轮）

上一轮（第十二轮 §4.16.5）记录的 5 条遗留项，本轮**全部收口**：

============================  ==============================================
遗留项                          本轮落点
============================  ==============================================
缺值模式值域两侧各一份          ``facts_patches.normalize_missing_value_mode``
                              成为唯一出口（sse_handlers / facts_extractor
                              均改为调用它）
知识库/原方案补丁源未接          ``facts_enrich.apply_knowledge_patches``
                              （对齐易标 :876-891）
最终整理（finalize）未引入      ``facts_enrich.finalize_facts``
                              （对齐易标 :909-920）
上下文预算分段未接线            ``facts_extractor.resolve_chunk_size``
                              （对齐易标 :363-377）
英文归一化键不走九大章节规则    ``facts_classification.FACT_KEY_TO_CHAPTER``
============================  ==============================================

⚠️ 三个「新增行为」开关（``facts_knowledge_patch_enabled`` /
``facts_finalize_enabled`` / ``facts_context_budget_split``）**默认全部关闭**，
关闭时既有链路行为逐字节不变——本文件对「默认关闭」有专门断言。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.config import settings
from app.services import facts_classification as fc
from app.services import facts_enrich as fe
from app.services import facts_extractor as fx
from app.services.facts_patches import normalize_missing_value_mode

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _items(**kw):
    return fx.FactItem(**kw)


# =========================================================================
# 一、遗留项 ①：缺值模式值域单一出口
# =========================================================================
class TestMissingValueSingleSource:
    def test_all_defaults_are_off(self):
        """三个新增开关默认关闭 → 既有链路行为不变（向后兼容底线）。"""
        assert settings.facts_knowledge_patch_enabled is False
        assert settings.facts_finalize_enabled is False
        assert settings.facts_context_budget_split is False

    def test_no_hardcoded_mode_tuple_left_in_pipeline(self):
        """facts_extractor / sse_handlers 不得再各自写一份合法值域字面量。

        缺值模式是「用户可选的三种语义」，漏改一处 → 入口放行新模式而下游按
        fabricate 处理，用户选了却看到「合理补全」结果且无任何报错。
        """
        for path in (APP_DIR / "services" / "facts_extractor.py",
                     APP_DIR / "routers" / "sse_handlers.py"):
            src = path.read_text(encoding="utf-8")
            assert '("fabricate", "omit", "placeholder")' not in src, path.name
            assert 'not in ("fabricate"' not in src, path.name

    def test_both_call_sites_delegate_to_single_exit(self):
        """两个调用点都必须调用 normalize_missing_value_mode。"""
        for path, needle in (
                (APP_DIR / "services" / "facts_extractor.py",
                 "normalize_missing_value_mode(missing_value_mode)"),
                (APP_DIR / "routers" / "sse_handlers.py",
                 "_norm_facts_mode(")):
            src = path.read_text(encoding="utf-8")
            assert needle in src, f"{path.name} 未走单一出口"

    def test_single_exit_semantics(self):
        assert normalize_missing_value_mode("omit") == "omit"
        assert normalize_missing_value_mode(" PLACEHOLDER ") == "placeholder"
        for bad in ("", None, "nope", 123, ["omit"]):
            assert normalize_missing_value_mode(bad) == "fabricate"

    def test_pipeline_signature_accepts_knowledge_text(self):
        """管线新增 knowledge_text 形参（默认空串 → 不触发任何新增调用）。"""
        sig = inspect.signature(fx.run_extraction_pipeline)
        assert sig.parameters["knowledge_text"].default == ""


# =========================================================================
# 二、遗留项 ②：知识库补充（对齐易标 :876-891）
# =========================================================================
class TestKnowledgePatchStage:
    def test_prompt_registered_and_renders(self):
        from app.services.ai.prompts._registry import render
        s = render("facts_knowledge_patch_system",
                   current_facts="[]", knowledge_text="KB")
        assert "patches" in s and "KB" in s

    def test_scene_registered(self):
        """AGENTS.md §4.5：新增 AI 调用点必须显式传 scene 并登记白名单。"""
        from app.services.ai.provider_factory import KNOWN_SCENES
        assert "facts_knowledge_patch" in KNOWN_SCENES
        assert KNOWN_SCENES["facts_knowledge_patch"].strip()

    def test_validate_patches_accepts_empty(self):
        """无内容可补是**正常结果**，不得判为格式错误（否则白白重试）。"""
        assert fe._validate_patches({"patches": []}) == []
        assert fe._validate_patches({}) != []      # 缺字段要报错
        assert fe._validate_patches("x") != []
        assert fe._validate_patches({"patches": "no"}) != []

    def test_build_patches_known_target_is_not_new(self):
        items = [_items(name="工期", value="450", key="total_duration")]
        ps = fe._build_patches(
            {"patches": [{"target_fact_id": "total_duration", "value": "KB补充"}]}, items)
        assert len(ps) == 1
        assert ps[0].create is False
        assert ps[0].target_fact_id == "total_duration"

    def test_build_patches_hallucinated_anchor_becomes_new(self):
        """AI 幻觉锚点不得挂到不存在的事实上（否则补丁被静默丢弃、用户以为已补）。"""
        items = [_items(name="工期", value="450", key="total_duration")]
        ps = fe._build_patches(
            {"patches": [{"target_fact_id": "ghost_key", "name": "臆造", "value": "x"}]},
            items)
        assert ps[0].create is True
        assert ps[0].target_fact_id == ""

    def test_build_patches_drops_empty_and_bad_mode(self):
        items = [_items(name="工期", value="450", key="total_duration")]
        ps = fe._build_patches({"patches": [
            {"target_fact_id": "total_duration", "value": "   "},
            "junk",
            {"target_fact_id": "total_duration", "value": "ok", "mode": "delete-all"},
        ]}, items)
        assert len(ps) == 1
        assert ps[0].mode == "append"

    def test_build_patches_accepts_alias_content_keys(self):
        items = [_items(name="工期", value="450", key="total_duration")]
        for key in ("value", "content", "text"):
            ps = fe._build_patches(
                {"patches": [{"target_fact_id": "total_duration", key: "X"}]}, items)
            assert ps and ps[0].content.strip() == "X", key

    def test_build_patches_garbage_returns_empty(self):
        items = [_items(name="工期", value="450", key="total_duration")]
        for bad in (None, {}, {"patches": None}, {"patches": "x"}, "x"):
            assert fe._build_patches(bad, items) == []

    def test_items_to_prompt_json_shape_and_clip(self):
        items = [_items(name="工期", value="长" * 900, key="total_duration",
                        source="orig", confidence=0.9, is_simulated=True)]
        out = fe.items_to_prompt_json(items)
        assert set(out[0]) == {"fact_key", "name", "value"}
        # 只带三项，运营字段不得外泄（否则模型会去改写溯源/模拟值标记）
        assert "source" not in out[0] and "confidence" not in out[0]
        assert out[0]["fact_key"] == "total_duration"
        assert len(out[0]["value"]) <= fe._VALUE_CLIP + 1



# =========================================================================
# 三、遗留项 ③：最终整理（对齐易标 :909-920）
# =========================================================================
class TestFinalizeStage:
    def test_prompt_registered_and_renders(self):
        from app.services.ai.prompts._registry import render
        s = render("facts_finalize_system", current_facts="[]")
        assert "facts" in s

    def test_scene_registered(self):
        from app.services.ai.provider_factory import KNOWN_SCENES
        assert "facts_finalize" in KNOWN_SCENES

    def test_validate_finalize(self):
        assert fe._validate_finalize({"facts": []}) == []
        for bad in (None, "x", {}, {"facts": "no"}):
            assert fe._validate_finalize(bad) != []

    def test_finalize_rewrites_value_preserving_metadata(self):
        items = [_items(name="工期", value="不超过450天", key="total_duration",
                        source="orig", confidence=0.9, is_simulated=True,
                        chapter="plan")]
        out, changed = fe.apply_finalize_result(items, {"facts": [
            {"fact_key": "total_duration", "name": "工期", "value": "450 日历天"}]})
        assert changed == 1
        assert out[0].value == "450 日历天"
        # 核心不变式：整理不得冲掉溯源 / 置信度 / 闸门 / 章节标注
        assert out[0].source == "orig"
        assert out[0].confidence == 0.9
        assert out[0].is_simulated is True
        assert out[0].chapter == "plan"

    def test_finalize_never_adds_or_removes_items(self):
        """整理阶段只改写、不增删（防 AI 把本任务当成重新提取撑大事实库）。"""
        items = [_items(name="工期", value="a", key="k1"),
                 _items(name="地点", value="b", key="k2")]
        out, _ = fe.apply_finalize_result(items, {"facts": [
            {"fact_key": "k1", "value": "A"},
            {"fact_key": "brand_new", "name": "臆造", "value": "X"},
        ]})
        assert len(out) == 2
        assert [i.key for i in out] == ["k1", "k2"]

    def test_finalize_drops_hallucinated_fact(self):
        items = [_items(name="工期", value="a", key="k1")]
        out, changed = fe.apply_finalize_result(items, {"facts": [
            {"fact_key": "ghost", "value": "X"}]})
        assert changed == 0
        assert out[0].value == "a"

    def test_finalize_matches_by_name_when_key_missing(self):
        items = [_items(name="建设工期", value="a", key="k1")]
        out, changed = fe.apply_finalize_result(items, {"facts": [
            {"fact_key": "", "name": "建设工期", "value": "B"}]})
        assert changed == 1 and out[0].value == "B"

    def test_finalize_empty_value_is_skipped(self):
        items = [_items(name="工期", value="a", key="k1")]
        out, changed = fe.apply_finalize_result(items, {"facts": [
            {"fact_key": "k1", "value": "   "}]})
        assert changed == 0 and out[0].value == "a"

    def test_finalize_no_change_when_value_identical(self):
        items = [_items(name="工期", value="450", key="k1")]
        _out, changed = fe.apply_finalize_result(items, {"facts": [
            {"fact_key": "k1", "value": "450"}]})
        assert changed == 0

    def test_finalize_size_cap_protects_against_re_extraction(self):
        """AI 若误当成「重新提取」返回海量事实，不得因此新增事实。"""
        items = [_items(name="工期", value="a", key="k1")]
        flood = {"facts": [{"fact_key": f"k{i}", "value": "x"} for i in range(2000)]}
        out, _changed = fe.apply_finalize_result(items, flood)
        assert len(out) == 1

    def test_finalize_garbage_returns_untouched(self):
        items = [_items(name="工期", value="a", key="k1")]
        for bad in (None, {}, "x", {"facts": None}):
            out, changed = fe.apply_finalize_result(items, bad)
            assert changed == 0 and out[0].value == "a"



# =========================================================================
# 四、遗留项 ④：上下文预算分段（对齐易标 :363-377）
# =========================================================================
class TestContextBudgetSplit:
    def test_disabled_returns_input_unchanged(self, monkeypatch):
        monkeypatch.setattr(settings, "facts_context_budget_split", False)
        assert fx.resolve_chunk_size(fx.CHUNK_SIZE) == fx.CHUNK_SIZE
        assert fx.resolve_chunk_size(1234) == 1234

    def test_enabled_never_shrinks_below_baseline(self, monkeypatch):
        """窗口够大时放宽；窗口很小时**不得**比历史基线更激进（否则段数爆炸）。"""
        monkeypatch.setattr(settings, "facts_context_budget_split", True)
        assert fx.resolve_chunk_size(fx.CHUNK_SIZE) >= fx.CHUNK_SIZE

    def test_split_is_byte_identical_when_disabled(self, monkeypatch):
        """默认关闭 → 切分结果与显式传 CHUNK_SIZE 逐字节一致。"""
        monkeypatch.setattr(settings, "facts_context_budget_split", False)
        text = "\n".join(f"## H{i}\n" + ("内容内容。" * 400) for i in range(10))
        a = [c.text for c in fx.split_into_chunks(text)]
        b = [c.text for c in fx.split_into_chunks(text, chunk_size=fx.CHUNK_SIZE)]
        assert a == b and a

    def test_resolve_failure_falls_back_to_old_behavior(self, monkeypatch):
        """任何异常都必须退回旧行为（fail-soft），不得让整轮提取崩掉。"""
        monkeypatch.setattr(settings, "facts_context_budget_split", True)
        import app.services.facts_patches as fpatch

        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(fpatch, "get_segment_limit", boom)
        assert fx.resolve_chunk_size(fx.CHUNK_SIZE) == fx.CHUNK_SIZE

    def test_fixed_skeleton_constant_is_positive(self):
        assert fx._FIXED_PROMPT_SKELETON and len(fx._FIXED_PROMPT_SKELETON) > 0


# =========================================================================
# 五、遗留项 ⑤：英文归一化键走九大章节
# =========================================================================
class TestEnglishFactKeyChapter:
    def test_fact_key_map_nonempty_and_targets_known_chapter(self):
        assert fc.FACT_KEY_TO_CHAPTER
        for key, chapter in fc.FACT_KEY_TO_CHAPTER.items():
            assert key and key.isascii()
            assert chapter in fc.CHAPTER_ORDER, key

    def test_english_danger_keys_classified(self):
        """上一轮遗留：foundation_depth / span / total_load 等英文键全落空串。"""
        for key in ("foundation_depth", "excavation_depth", "span", "total_load",
                    "line_load", "crane_capacity", "slope_height",
                    "install_height"):
            assert fc.classify_chapter_from_text("x", "", "", "", key) == "overview", key

    def test_four_arg_call_still_works_unchanged(self):
        """新增可选参数不得改变既有 4 参调用的行为（向后兼容）。"""
        assert fc.classify_chapter_from_text("基坑深度", "6.5m") == "overview"
        assert fc.classify_chapter_from_text("未知名词") == ""

    def test_chinese_text_rules_still_win_over_fact_key(self):
        """优先级不倒置：中文文本规则仍是最专指信号，fact_key 只在其未命中时兜底。

        「监测频率」命中 safety 章的文本规则；若 fact_key 被误提到文本规则之前，
        传 ``span`` 会把它判成 overview（第一章工程概况）——性质完全错误。
        """
        got = fc.classify_chapter_from_text("监测频率", "1次/天", "", "", "span")
        assert got == "safety"

    def test_fact_key_only_used_when_text_rules_miss(self):
        """中文文本规则未命中时才轮到 fact_key。"""
        # 「未知参数名」不命中任何中文规则 → 走 fact_key → overview
        assert fc.classify_chapter_from_text("未知参数名", "", "", "", "span") == "overview"
        # fact_key 缺失 → 落空串（未分类），而不是回退到某个默认章节
        assert fc.classify_chapter_from_text("未知参数名", "") == ""

    def test_chapter_of_row_passes_fact_key(self):
        row = {"name": "unknown-name", "value": "v", "fact_key": "foundation_depth"}
        assert fc.chapter_of_row(row) == "overview"

    def test_dimensions_for_row_inherits_fact_key(self):
        row = {"name": "unknown-name", "value": "v", "fact_key": "foundation_depth"}
        assert fc.dimensions_for_row(row)["chapter"] == "overview"

    def test_fact_key_is_optional_everywhere(self):
        """签名默认值必须是空串，既有调用点零改动。"""
        sig = inspect.signature(fc.classify_chapter_from_text)
        assert sig.parameters["fact_key"].default == ""

    def test_counted_in_chapter_coverage(self):
        got = fc.chapter_field_completeness(
            [{"name": "unknown", "value": "v", "fact_key": "foundation_depth"}])
        assert got["chapters"]["overview"]["fact_count"] == 1



# =========================================================================
# 六、管线接线与静态护栏
# =========================================================================
class TestPipelineWiring:
    def test_pipeline_has_both_gated_stages(self):
        """两个阶段必须挂在管线上、且受配置开关控制（默认关闭）。"""
        src = inspect.getsource(fx.run_extraction_pipeline)
        assert "settings.facts_knowledge_patch_enabled" in src
        assert "settings.facts_finalize_enabled" in src
        assert "knowledge_text" in src

    def test_knowledge_stage_runs_before_finalize(self):
        """顺序契约：先补充、再整理（整理要在补齐之后再统一口径）。"""
        src = inspect.getsource(fx.run_extraction_pipeline)
        assert src.index("facts_knowledge_patch_enabled") < src.index(
            "facts_finalize_enabled")

    def test_enrichment_stages_are_fail_soft(self):
        """补充/整理失败必须被捕获 —— 不得让已成功的提取整批丢失。"""
        src = inspect.getsource(fx.run_extraction_pipeline)
        assert "fail-soft" in src

    def test_enrich_module_has_no_factitem_rebuild(self):
        """适配器保不变式：不得出现 ``FactItem(`` 重建调用。"""
        tree = ast.parse(inspect.getsource(fe))
        fns = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "FactItem" for c in ast.walk(n))}
        assert fns == set(), f"facts_enrich 重建了 FactItem：{fns}"

    def test_both_enrich_async_functions_declare_scene(self):
        """AGENTS.md §4.5：漏传 scene → 统计归入空场景且无法单独路由模型。"""
        src = inspect.getsource(fe)
        assert 'scene="facts_knowledge_patch"' in src
        assert 'scene="facts_finalize"' in src

    def test_sse_passes_knowledge_text_to_pipeline(self):
        src = (APP_DIR / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert "knowledge_text=_facts_knowledge_text" in src
        assert "facts_knowledge_patch_enabled" in src

    def test_no_dot_get_on_sqlite_row(self):
        """`_apply_item_updates` 里的 ``row`` 是 ``sqlite3.Row``（**无 .get**）。

        本轮接线时在此写了 ``row.get("fact_key")``，直接
        ``AttributeError: 'sqlite3.Row' object has no attribute 'get'`` 打挂 4 个
        用例（改分类重派生章节的两条 + 章节 parity + 值未变保留冲突）。
        与 AGENTS.md §5.5 R13 同类：`Row` / `Cursor` 与 dict 的接口差异。
        本护栏按源码文本锁定该函数内不得出现 ``row.get(``。
        """
        src = inspect.getsource(
            __import__("app.routers.global_facts", fromlist=["x"])._apply_item_updates)
        assert "row.get(" not in src, \
            "_apply_item_updates 内 row 是 sqlite3.Row，不能用 .get() 取值"
        # 对照组：真正的 dict 参数（旧值快照）用 .get 是正确的
        assert 'old.get("fact_key")' in inspect.getsource(
            __import__("app.routers.global_facts", fromlist=["x"]))

    def test_no_stray_temp_artifacts_in_touched_dirs(self):
        """AGENTS.md §3.1.9：不得往仓库里新增调试产物。

        只扫本轮实际改动的两个目录（``app/services`` 与 ``tests``）的**浅层**：
        整树 ``rglob`` 会遍历 ``data/`` / ``logs/`` 等大目录，实测单次 >30s，
        会把单测拖到超时（本护栏最初就是这么写的）。
        """
        import glob
        stray: list[str] = []
        for base in (APP_DIR / "services", APP_DIR.parent / "tests"):
            for pattern in ("_gen*.py", "_tmp*.py", "_dbg*.py"):
                stray.extend(glob.glob(str(base / pattern)))
        assert stray == [], f"新增了调试产物：{stray}"

