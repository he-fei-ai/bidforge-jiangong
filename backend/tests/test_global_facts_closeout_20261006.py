"""全局事实模块收口护栏（2026-10-06）。

覆盖本轮 9 项 P0 修复，每项都配「修复前必失败」的反向用例：

  A  extract_value_from_markdown_line 全角冒号优先导致 name/value 错位
  B  segment_failed 段被当作「成功空段」→ 增量提取永久跳过（不可见）
  C  事实写路径 R13 判空 + 影响行数校验（消除假成功）
  D  交叉校验器门控 is_simulated / is_stale
  E  DANGER_PARAM_RULES 补 pile_depth + 移除「立杆步距→height」误映射
  F  _chunk_hash 孤立代理字符被静默丢弃 → 指纹碰撞
  G  merge_and_deduplicate 空 key 塌缩成单一「矛盾簇」
  H  generate_facts 跨类型任务互斥
  I  facts_enrich / facts_patches 二次实现收敛
"""
from __future__ import annotations

import ast
import inspect
import io
import os
import re
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(APP))


def _call_name(node) -> str:
    """取调用点的函数名（AST 判定专用；注释/docstring 天然不参与）。"""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def _has_comprehension(node) -> bool:
    """子树里是否含推导式（DictComp/SetComp/ListComp/GeneratorExp）。

    ⚠️ 不能用 ``"Comprehension" in ast.dump(node)`` —— ``ast.dump`` 输出的是
    **字段名**（``generators``）而非类名，字符串判定恒为 False，
    护栏会以「未推导」误报。首版就是这么写的。
    """
    comps = (ast.DictComp, ast.SetComp, ast.ListComp, ast.GeneratorExp)
    return any(isinstance(n, comps) for n in ast.walk(node))


# =========================================================================
# A. extract_value_from_markdown_line：分隔符必须「最早出现者胜」
# =========================================================================
class TestFactLineSplit:
    """A：值内含全角冒号时，name/value 曾在错误位置切开。

    该函数是 load_resolved_facts_for_scope 的唯一 name/value 回解出口，
    下游是审核 / 预检 / 符合性 / 自动修复四条链路的提示词装配。
    """

    @pytest.mark.parametrize("line,expect", [
        # 修复前：先试全角 → 在「地点：北京」处切开 → name 被污染
        ("- **时间**: 9:00，地点：北京", ("时间", "9:00，地点：北京")),
        ("- **备注**: 见附件：第3页", ("备注", "见附件：第3页")),
        # 全角分隔符在前（书写者本意）—— 修复前后都应正确
        ("- **备注**：见附件：第3页", ("备注", "见附件：第3页")),
        # 常规形态零回归
        ("- **基坑深度**: 12.5m", ("基坑深度", "12.5m")),
        ("- 名称：值", ("名称", "值")),
        ("- **无冒号**", ("无冒号", "")),
        ("", ("", "")),
    ])
    def test_split_uses_earliest_separator(self, line, expect):
        from app.services.facts_extractor import extract_value_from_markdown_line
        assert extract_value_from_markdown_line(line) == expect

    def test_value_containing_fullwidth_colon_is_not_truncated(self):
        """反向用例：修复前 value 会被截成「北京」。"""
        from app.services.facts_extractor import extract_value_from_markdown_line
        name, value = extract_value_from_markdown_line("- **施工部位**: 主体：三层")
        assert name == "施工部位"
        assert value == "主体：三层"
        assert "三层" in value

    def test_ascii_colon_inside_value_survives(self):
        """值内多个半角冒号（时间 9:00:00）不得被切。"""
        from app.services.facts_extractor import extract_value_from_markdown_line
        name, value = extract_value_from_markdown_line("- **浇筑时间**: 9:00:00 开始")
        assert name == "浇筑时间"
        assert value == "9:00:00 开始"

    def test_simulated_marker_still_stripped_first(self):
        """P0-1 修复不得回归 2026-09-21 的模拟值标记剥离顺序。"""
        from app.services.facts_extractor import (
            extract_value_from_markdown_line, SIMULATED_MARKER)
        name, value = extract_value_from_markdown_line(
            f"- **基坑深度**: 12.5m{SIMULATED_MARKER}")
        assert name == "基坑深度"
        assert value == "12.5m"

    def test_separator_constant_is_single_source(self):
        """半角/全角分隔符收敛为单一常量，禁止再出现字面量双写。"""
        src = io.open(
            os.path.join(APP, "app", "services", "facts_extractor.py"),
            encoding="utf-8").read()
        assert "_FACT_LINE_SEPARATORS" in src
        # 旧实现 for sep in ["：", ":"] 已被 _split_fact_line 取代
        assert 'for sep in ["：", ":"]' not in src

    def test_doc_pipeline_shim_uses_single_exit(self):
        """doc_pipeline 的 _Item.value 必须复用单一出口，不得自带第二套切分。

        ⚠️ 必须**同时**校验「调用存在」与「名字被 import 绑定」：
        只查调用点的话，摘掉 import 而留下调用也能通过 —— 那是 NameError 而
        非静默错误，会在**运行期**才炸。A/B 反向验证（A2：同时摘掉两处
        import）实测本例一度全绿，正是这个缺口。
        """
        tree = ast.parse(io.open(
            os.path.join(APP, "app", "services", "doc_pipeline", "pipeline.py"),
            encoding="utf-8").read())
        calls = {_call_name(n) for n in ast.walk(tree)
                 if isinstance(n, ast.Call)}
        assert "extract_value_from_markdown_line" in calls, \
            "shim 未复用 name/value 单一出口"
        # AST 判定：注释/docstring 里提到 c.split 不算（§5.14 锚点纪律）
        assert "c.split" not in calls, "仍存在第二套 name/value 切分"
        assert "split" not in calls, "仍存在裸 split 切分"
        # 名字必须真的被绑定（否则运行期 NameError）
        bound = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module and \
                    n.module.endswith("facts_extractor"):
                bound.update(a.name for a in n.names)
        assert "extract_value_from_markdown_line" in bound, \
            "调用了 extract_value_from_markdown_line 却未 import（运行期 NameError）"


# =========================================================================
# B. segment_failed：模型声明未提取的段必须可见
# =========================================================================
class TestSegmentFailedVisible:
    """B：`segment_failed=true` 曾只被 _validate 读一次就丢弃。

    后果链：① 不进 segment_stats → 前端「N 段失败」永不提示；
    ② 不进 warnings → 用户以为「提取完成」；
    ③ 该段指纹仍写入 chunk_hashes_ok → 下次增量提取永久跳过。
    """

    def test_result_has_declared_failed_fields(self):
        from app.services.facts_extractor import ExtractionResult
        r = ExtractionResult()
        assert r.declared_failed_chunks == 0
        assert r.declared_failed_details == []

    def test_segment_stats_carries_declared_failed(self):
        from app.services.facts_extractor import ExtractionResult
        r = ExtractionResult()
        r.segment_stats = {"total": 3, "ok": 2, "failed": 0,
                           "declared_failed": 1,
                           "declared_failed_details": [{"index": 2}]}
        payload = __import__(
            "app.services.facts_extractor", fromlist=["x"]).format_for_frontend(r)
        # 加法式契约：segment_stats 整体透传，前端无需改契约即可读到
        assert payload["segment_stats"]["declared_failed"] == 1

    def test_extract_flags_segment_failed_through_error_out(self, monkeypatch):
        """模型回 segment_failed=true 且无事实时，必须经 error_out 回传。"""
        import asyncio

        from app.services import facts_extractor as fx

        async def fake_collect(messages, validate_fn, **kw):
            return {"facts": [], "segment_failed": True}, {}

        monkeypatch.setattr(fx, "collect_json_response", fake_collect)
        err: dict = {}
        items = asyncio.run(fx.extract_from_single_chunk(
            text="x" * 300, context_summary="", chunk_index=0, error_out=err))
        assert items == []
        assert err.get("segment_failed") is True
        assert "error" not in err, "模型声明失败不得伪装成「真正报错」"

    def test_segment_failed_with_facts_is_not_flagged(self, monkeypatch):
        """模型误置 segment_failed 但仍给出事实时，不应被登记为未提取段。"""
        import asyncio

        from app.services import facts_extractor as fx

        payload = {"facts": [{"name": "基坑深度", "value": "12.5m"}],
                   "segment_failed": True}

        async def fake_collect(messages, validate_fn, **kw):
            return payload, {}

        monkeypatch.setattr(fx, "collect_json_response", fake_collect)
        err: dict = {}
        items = asyncio.run(fx.extract_from_single_chunk(
            text="x" * 300, context_summary="", chunk_index=0, error_out=err))
        assert len(items) == 1
        assert "segment_failed" not in err

    def test_legit_empty_chunk_is_not_a_failure(self, monkeypatch):
        """封面/目录等「合法无事实段」不得被计为失败（否则 60 段里 40 段虚报）。"""
        import asyncio

        from app.services import facts_extractor as fx

        async def fake_collect(messages, validate_fn, **kw):
            return {"facts": []}, {}

        monkeypatch.setattr(fx, "collect_json_response", fake_collect)
        err: dict = {}
        items = asyncio.run(fx.extract_from_single_chunk(
            text="x" * 300, context_summary="", chunk_index=0, error_out=err))
        assert items == []
        assert err == {}, "合法空段既非失败也非模型声明失败"

    def test_pipeline_surfaces_declared_failed(self):
        """静态锁：编排层必须登记 declared_failed 并给「重新提取」恢复入口。"""
        src = io.open(
            os.path.join(APP, "app", "services", "facts_extractor.py"),
            encoding="utf-8").read()
        assert "declared_failed" in src
        assert '"declared_failed": len(declared_failed)' in src
        # 告警必须给出恢复入口（普通重跑会跳过这些段）
        assert "重新提取" in src

    def test_declared_failed_not_counted_as_failed(self):
        """反向用例：declared_failed 与 failed 是两个独立计数，不得混用。"""
        src = io.open(
            os.path.join(APP, "app", "services", "facts_extractor.py"),
            encoding="utf-8").read()
        # AST 判定：扫 segment_stats 字典字面量的键值对（§5.14 锚点纪律）
        seg_pairs = {}
        for n in ast.walk(ast.parse(src)):
            if not isinstance(n, ast.Dict):
                continue
            keys = [k.value for k in n.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if "declared_failed" not in keys:
                continue
            for k, v in zip(n.keys, n.values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    continue
                if k.value == "failed":
                    seg_pairs["failed"] = ast.dump(v)
                elif k.value == "declared_failed":
                    seg_pairs["declared_failed"] = ast.dump(v)
        assert seg_pairs, "未找到 segment_stats 字典字面量"
        assert "fail_count" in seg_pairs["failed"], \
            f"failed 键应取 fail_count，实际：{seg_pairs['failed']}"
        assert "declared_failed" in seg_pairs["declared_failed"]
        assert seg_pairs["failed"] != seg_pairs["declared_failed"], \
            "failed 与 declared_failed 不得取同一个计数"


# =========================================================================
# C. 事实写路径 R13 判空 + 影响行数校验
# =========================================================================
class TestFactWriteR13:
    """C：AGENTS.md §5.5 R13 —— db.execute() 可能返回 None。

    修复前事实半区零判空、仅 1 处 safe_rowcount，且 8 处写路径
    「写完就 updated += 1 / return ok:True」→ 假成功且零日志。
    """

    def test_helpers_exist(self):
        from app.routers import global_facts as gf
        assert callable(gf._require_fact_write_cursor)
        assert callable(gf._assert_fact_write_applied)

    def test_none_cursor_raises_503(self):
        from fastapi import HTTPException

        from app.routers.global_facts import (
            _require_fact_write_cursor, _assert_fact_write_applied)
        with pytest.raises(HTTPException) as ei:
            _require_fact_write_cursor(None, "测试写")
        assert ei.value.status_code == 503

    def test_zero_rowcount_raises_409(self):
        from fastapi import HTTPException

        from app.routers.global_facts import _assert_fact_write_applied

        class _Cur:
            rowcount = 0

        with pytest.raises(HTTPException) as ei:
            _assert_fact_write_applied(_Cur(), "测试写")
        assert ei.value.status_code == 409

    def test_nominal_rowcount_passes(self):
        from app.routers.global_facts import _assert_fact_write_applied

        class _Cur:
            rowcount = 1

        assert _assert_fact_write_applied(_Cur(), "测试写") == 1

    def test_expect_zero_skips_rowcount_gate(self):
        """批量写场景可传 expect=0 只判空不校验行数。"""
        from app.routers.global_facts import _assert_fact_write_applied

        class _Cur:
            rowcount = 0

        assert _assert_fact_write_applied(_Cur(), "批量", expect=0) == 0

    @pytest.mark.parametrize("fn_name", [
        "resolve_fact", "resolve_conflict", "delete_fact",
        "batch_resolve", "ack_fact_stale", "ack_fact_stale_batch",
    ])
    def test_every_mutating_route_uses_guard(self, fn_name):
        """每个会写 global_facts 的端点函数体必须出现守卫调用。"""
        from app.routers import global_facts as gf
        fn = getattr(gf, fn_name, None)
        if fn is None:
            pytest.skip(f"{fn_name} 不存在")
        src = inspect.getsource(fn)
        assert ("_assert_fact_write_applied" in src
                or "_require_fact_write_cursor" in src), \
            f"{fn_name} 未接入 R13 守卫"

    def test_item_update_path_uses_guard(self):
        from app.routers import global_facts as gf
        assert "_assert_fact_write_applied" in inspect.getsource(
            gf._apply_item_updates)

    def test_group_rebuild_delete_uses_guard(self):
        from app.routers import global_facts as gf
        assert "_require_fact_write_cursor" in inspect.getsource(gf.update_fact)

    def test_batch_resolve_reports_actual_rowcount(self):
        """batch-resolve 必须回实际影响行数，而非 len(changed_ids) 意图数。"""
        from app.routers import global_facts as gf
        src = inspect.getsource(gf.batch_resolve)
        assert '"changed": changed_applied' in src
        assert '"changed": len(changed_ids)' not in src

    def test_no_bare_rowcount_in_facts_half(self):
        """静态扫：事实半区不得出现裸 .rowcount（必须走 safe_rowcount）。"""
        p = os.path.join(APP, "app", "routers", "global_facts.py")
        src = io.open(p, encoding="utf-8").read()
        # AST 判定：只禁真实属性/取值访问，注释与 docstring 天然豁免（§5.14）
        tree = ast.parse(src)
        bare = [n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == "rowcount"]
        assert bare == [], "出现裸 cur.rowcount，应走 safe_rowcount"
        getters = []
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and _call_name(n) == "getattr"
                    and len(n.args) >= 2
                    and isinstance(n.args[1], ast.Constant)
                    and n.args[1].value == "rowcount"):
                getters.append(n.lineno)
        assert getters == [], f"出现 getattr(cur,'rowcount') 绕过：行 {getters}"


# =========================================================================
# D. 交叉校验器门控 is_simulated / is_stale
# =========================================================================
class TestCrossValidatorAdjudicableGate:
    """D：模拟值 / 过期事实曾在交叉校验里与真实值「对撞」并被回写 has_conflict。

    判据与注入门控（get_facts_inject_where，四条件 fail-closed）同源：
    这类事实无论裁决结果如何都进不了生成链路 → 冲突是纯负收益（假冲突 +
    假闸门，用户裁决多少次都无法让它进入交付物）。
    """

    class _It:
        def __init__(self, name, value, is_simulated=False, is_stale=False,
                     confidence=1.0, source="s", has_conflict=False,
                     conflict_values=None, key=""):
            self.name = name
            self.value = value
            self.is_simulated = is_simulated
            self.is_stale = is_stale
            self.confidence = confidence
            self.source = source
            self.has_conflict = has_conflict
            self.conflict_values = list(conflict_values or [])
            self.key = key or name

    def test_simulated_does_not_generate_conflict(self):
        from app.services.facts_cross_validators import run_cross_validations
        items = [
            self._It("总工期", "90日历天"),
            self._It("总工期", "120日历天", is_simulated=True),
        ]
        conflicts = run_cross_validations(items)
        assert conflicts == [], "模拟值不得与真实值对撞生成冲突"
        for it in items:
            assert it.has_conflict is False

    def test_stale_does_not_generate_conflict(self):
        from app.services.facts_cross_validators import run_cross_validations
        items = [
            self._It("总工期", "90日历天"),
            self._It("总工期", "120日历天", is_stale=True),
        ]
        assert run_cross_validations(items) == []
        for it in items:
            assert it.has_conflict is False

    def test_real_facts_still_conflict(self, ):
        """反向用例：真实值之间的矛盾必须仍被报出（不得过度门控）。"""
        from app.services.facts_cross_validators import run_cross_validations
        items = [
            self._It("总工期", "90日历天"),
            self._It("总工期", "120日历天"),
        ]
        conflicts = run_cross_validations(items)
        assert conflicts, "真实值矛盾被门控掉了"
        assert any(c["rule_id"] == "XV-SAME-NAME" for c in conflicts)

    def test_all_ineligible_returns_empty(self):
        from app.services.facts_cross_validators import run_cross_validations
        items = [self._It("总工期", "90日历天", is_simulated=True),
                 self._It("总工期", "120日历天", is_stale=True)]
        assert run_cross_validations(items) == []

    def test_missing_attributes_default_to_adjudicable(self):
        """历史 shim / 测试替身缺属性时按可裁决处理（fail-open 到旧行为）。"""
        from app.services.facts_cross_validators import _is_adjudicable

        class _Bare:
            name = "x"
            value = "y"
        assert _is_adjudicable(_Bare()) is True

    def test_pipeline_shim_exposes_both_dimensions(self):
        """doc_pipeline 的 _Item 必须暴露 is_simulated / is_stale。"""
        p = os.path.join(APP, "app", "services", "doc_pipeline", "pipeline.py")
        src = io.open(p, encoding="utf-8").read()
        assert "self.is_simulated = bool(row.get(\"is_simulated\"))" in src
        assert "self.is_stale = bool(row.get(\"is_stale\"))" in src
        assert "is_simulated, is_stale, has_conflict" in src, "SELECT 未取 is_stale"


# =========================================================================
# E. 危大参数：pile_depth 可达 + 立杆步距不再误映射
# =========================================================================
class TestDangerParamReachability:
    """E：HAZARD_THRESHOLDS 声明的每个 params 键都必须能被事实名产出。"""

    def test_every_threshold_param_is_producible(self):
        from app.services import scheme_classification as sc
        from app.services.facts_classification import DANGER_PARAM_RULES
        producible = {p for _kw, p in DANGER_PARAM_RULES}
        declared = set()
        for _name, spec in sc.HAZARD_THRESHOLDS.items():
            declared.update(spec.get("params") or ())
        missing = sorted(declared - producible)
        assert missing == [], f"阈值参数无法由事实产出（恒走缺参保守分支）：{missing}"

    def test_pile_depth_extracted(self):
        from app.services.facts_classification import extract_danger_params

        class _F:
            def __init__(s, n, v):
                s.name, s.value, s.key = n, v, ""

        got = extract_danger_params([_F("人工挖孔桩深度", "16.5m")])
        assert got.get("pile_depth") == pytest.approx(16.5)

    def test_pile_depth_bored_pile_reaches_threshold(self):
        """端到端：16.5m 人工挖孔桩应判「超过一定规模」而非恒走缺参。"""
        from app.services.facts_classification import danger_check

        class _F:
            def __init__(s, n, v):
                s.name, s.value, s.key = n, v, ""

        rep = danger_check("人工挖孔桩", facts=[_F("人工挖孔桩深度", "16.5m")])
        assert rep["threshold_params"].get("pile_depth") == pytest.approx(16.5)
        assert rep["classification"]["is_oversize"] is True

    def test_pile_depth_10m_not_oversize(self):
        from app.services.facts_classification import danger_check

        class _F:
            def __init__(s, n, v):
                s.name, s.value, s.key = n, v, ""

        rep = danger_check("人工挖孔桩", facts=[_F("人工挖孔桩深度", "10m")])
        assert rep["classification"]["is_oversize"] is False
        assert rep["threshold_params"].get("pile_depth") == pytest.approx(10.0)

    def test_lidar_pitch_not_mapped_to_height(self):
        """反向用例：立杆步距是「间距」，映射到 height 会漏判脚手架超规模。"""
        from app.services.facts_classification import (
            DANGER_PARAM_RULES, extract_danger_params)

        targets = {p for kws, p in DANGER_PARAM_RULES for k in kws
                   if k == "立杆步距"}
        assert targets == set(), "立杆步距不得映射到任何高度类阈值参数"

        class _F:
            def __init__(s, n, v):
                s.name, s.value, s.key = n, v, ""

        got = extract_danger_params([_F("立杆步距", "1.8m")])
        assert "height" not in got, "1.8m 步距被当成搭设高度 → 漏判"

    def test_danger_keywords_are_chapter_classified(self):
        """DANGER_PARAM_RULES 的每个中文关键词都必须被九大章节规则覆盖。

        （两表语义耦合但结构独立；只改其一会让事实落「未分类」空串。）
        """
        from app.services.facts_classification import (
            DANGER_PARAM_RULES, classify_chapter_from_text)
        unclassified = []
        for kws, _p in DANGER_PARAM_RULES:
            for kw in kws:
                if not kw.isascii() and not classify_chapter_from_text(kw, ""):
                    unclassified.append(kw)
        assert unclassified == [], f"危大参数名未被九大章节规则覆盖：{unclassified}"

    def test_length_unit_table_superset_of_validator(self):
        """危大阈值的长度单位集必须 ⊇ 交叉校验器的**长度量纲**单位集。

        否则同一条「0.5km」事实，冲突检测认为与 500m 一致、危大判定却取不到数
        → 相反的失败方向。

        ✅ 2026-10-06：交叉校验器单位表扩到 10 个量纲后，本例**必须**改用
        ``LENGTH_UNITS``（校验器自己声明的长度集）作比对口径 ——
        拿整个 ``_UNIT_TO_BASE`` 比会把 kN/MPa/㎥ 一并要求危大表收录，
        那是不可能的（危大阈值只用长度类参数）。
        """
        from app.services import facts_classification as fc
        from app.services import facts_cross_validators as fv
        assert fv.LENGTH_UNITS, "校验器未声明长度单位集（parity 判据失效）"
        missing = set(fv.LENGTH_UNITS) - set(fc._UNIT_TO_METER)
        assert missing == set(), f"危大阈值长度单位缺口：{sorted(missing)}"

    def test_validator_unit_table_is_single_source(self):
        """单位表必须由 ``_UNIT_SPEC`` 单一结构派生，不得再手写三份视图。"""
        import ast as _ast
        from app.services import facts_cross_validators as fv
        assert set(fv._UNIT_TO_BASE) == set(fv._UNIT_SPEC)
        assert set(fv._UNIT_DIM) == set(fv._UNIT_SPEC)
        assert set(fv.LENGTH_UNITS) == {
            u for u, (d, _b) in fv._UNIT_SPEC.items() if d == "length"}
        # 仿射量纲不得混入长度集（温度比值换算无意义）
        assert "temperature" not in set(fv.LENGTH_UNITS)
        # 每个单位都必须有非零基准系数
        for u, (_d, base) in fv._UNIT_SPEC.items():
            assert base > 0, f"单位 {u} 基准系数非正：{base}"
        # 三个派生视图必须是推导式而非字面量（防手写副本回流）
        tree = _ast.parse(io.open(fv.__file__, encoding="utf-8").read())
        derived = set()
        for n in _ast.walk(tree):
            targets = None
            if isinstance(n, _ast.Assign) and len(n.targets) == 1:
                targets, value = n.targets, n.value
            elif isinstance(n, _ast.AnnAssign):      # LENGTH_UNITS 带类型标注
                targets, value = [n.target], n.value
            if targets is None or not isinstance(targets[0], _ast.Name):
                continue
            if targets[0].id in ("_UNIT_TO_BASE", "_UNIT_DIM", "LENGTH_UNITS") \
                    and value is not None and _has_comprehension(value):
                derived.add(targets[0].id)
        assert derived == {"_UNIT_TO_BASE", "_UNIT_DIM", "LENGTH_UNITS"}, (
            f"单位视图未由 _UNIT_SPEC 推导（手写副本会漂移）：缺 {sorted(
                {'_UNIT_TO_BASE', '_UNIT_DIM', 'LENGTH_UNITS'} - derived)}")

    @pytest.mark.parametrize("text,dim,unit", [
        ("12.5kN", "force", "kn"),
        ("12.5kN/m²", "pressure", "kn/m2"),
        ("3.5 kN/m", "line_load", "kn/m"),
        ("500㎡", "area", "m2"),
        ("120m²", "area", "m2"),
        ("2.0m3", "volume", "m3"),
        ("500L", "volume", "l"),
        ("90日历天", "duration", "日历天"),
        ("3个月", "duration", "个月"),
        ("30℃", "temperature", "℃"),
        ("80%", "ratio", "%"),
        ("20kPa", "pressure", "kpa"),
        ("2.5MPa", "pressure", "mpa"),
    ])
    def test_quantity_dimension_detection(self, text, dim, unit):
        """量纲必须判对 —— 「解析成功但量纲全错」比不解析更危险。"""
        from app.services.facts_cross_validators import _extract_quantity
        q = _extract_quantity(text)
        assert q is not None, f"{text!r} 未解析出量纲"
        assert q[1] == unit, f"{text!r} 单位归一错：{q[1]} != {unit}"
        assert q[2] == dim, f"{text!r} 量纲错：{q[2]} != {dim}"

    @pytest.mark.parametrize("text", ["C30混凝土", "张三", "按设计要求", "", None])
    def test_no_unit_text_yields_none(self, text):
        """无单位文本不得被误判出量纲（否则纯数字/纯文本全被卷进冲突判定）。"""
        from app.services.facts_cross_validators import _extract_quantity
        assert _extract_quantity(text) is None

    def test_suspect_factor_uses_relative_tolerance(self):
        """反向用例：量级判据必须是**相对**容差。

        旧实现 ``abs(1/ratio - f) <= 1e-3`` 是加法容差 → 任意 ratio ≥ 1e6
        都会被误判成「恰好 1000 倍的单位换算错误」。本例 ratio = 1e9。
        """
        from app.services.facts_cross_validators import _SUSPECT_FACTORS
        ratio = 1e9
        additive = any(abs(1.0 / ratio - f) <= 1e-3 for f in _SUSPECT_FACTORS)
        relative = any(abs((1.0 / ratio) / f - 1.0) <= 1e-3
                       for f in _SUSPECT_FACTORS)
        assert additive is True, "前置条件失效：加法容差本就不误报"
        assert relative is False, "相对容差仍在误报"

    def test_large_ratio_not_flagged_as_unit_typo(self):
        """端到端：量级差 1e9 的两条同名荷载不得被报成「单位换算错误」。"""
        from app.services.facts_cross_validators import run_cross_validations

        class _It:
            def __init__(s, n, v, key=""):
                s.name, s.value, s.key = n, v, key or n
                s.is_simulated = s.is_stale = False
                s.has_conflict = False
                s.conflict_values = []
                s.confidence = 1.0
                s.source = "s"

        cs = run_cross_validations([
            _It("施工总荷载", "12500N", "total_load"),
            _It("施工总荷载", "0.0125mN", "total_load"),
        ])
        assert not [c for c in cs
                    if c["rule_id"] == "XV-NUM-UNIT"
                    and c["conflict_type"] == "numeric_scale_suspect"], \
            "ratio=1e9 被误判成单位换算错误"

    def test_force_scale_typo_detected(self):
        """正向：kN↔N 的 1000 倍换算笔误必须被点出。

        ⚠️ 必须用**不同单位**的取值：同单位对在 ``_check_numeric_unit_consistency``
        里直接 continue（交给 XV-SAME-NAME 判「值不同」）—— 那是刻意口径，
        同单位 1000 倍差更像参数写错而非单位笔误。
        """
        from app.services.facts_cross_validators import run_cross_validations

        class _It:
            def __init__(s, n, v, key=""):
                s.name, s.value, s.key = n, v, key or n
                s.is_simulated = s.is_stale = False
                s.has_conflict = False
                s.conflict_values = []
                s.confidence = 1.0
                s.source = "s"

        for a, b in (("12.5kN", "12.5N"), ("12.5m", "12.5cm")):
            cs = run_cross_validations([
                _It("施工总荷载", a, "total_load"),
                _It("施工总荷载", b, "total_load"),
            ])
            assert [c for c in cs
                    if c["rule_id"] == "XV-NUM-UNIT"
                    and c["conflict_type"] == "numeric_scale_suspect"], \
                f"{a} vs {b} 的换算比笔误未被检出"

    def test_same_unit_scale_difference_not_called_unit_typo(self):
        """反向：同单位的量级差**不得**被标成「单位换算错误」（会误导用户）。"""
        from app.services.facts_cross_validators import run_cross_validations

        class _It:
            def __init__(s, n, v, key=""):
                s.name, s.value, s.key = n, v, key or n
                s.is_simulated = s.is_stale = False
                s.has_conflict = False
                s.conflict_values = []
                s.confidence = 1.0
                s.source = "s"

        cs = run_cross_validations([
            _It("开挖深度", "12.5m", "depth"),
            _It("开挖深度", "0.125m", "depth"),
        ])
        assert not [c for c in cs if c["rule_id"] == "XV-NUM-UNIT"], \
            "同单位量级差被误标成单位换算错误（口径应只比不同单位）"

    def test_affine_dimension_only_flags_same_number(self):
        """温度是仿射量纲：只判「同数值不同单位」，不按比值判等价。"""
        from app.services.facts_cross_validators import run_cross_validations

        class _It:
            def __init__(s, n, v, key=""):
                s.name, s.value, s.key = n, v, key or n
                s.is_simulated = s.is_stale = False
                s.has_conflict = False
                s.conflict_values = []
                s.confidence = 1.0
                s.source = "s"

        same = run_cross_validations([
            _It("养护温度", "30℃", "curing_temp"),
            _It("养护温度", "30℉", "curing_temp"),
        ])
        assert [c for c in same if c["rule_id"] == "XV-NUM-UNIT"], \
            "30℃/30℉ 必有一处写错，应报出"
        diff = run_cross_validations([
            _It("养护温度", "30℃", "curing_temp"),
            _It("养护温度", "40℉", "curing_temp"),
        ])
        assert not [c for c in diff if c["rule_id"] == "XV-NUM-UNIT"], \
            "仿射量纲不得按比值换算（30℃ 与 40℉ 无换算关系）"

    def test_same_name_groups_by_fact_key(self):
        """✅ 不同显示名 + 同一 fact_key 的事实必须被比对（旧实现漏报）。"""
        from app.services.facts_cross_validators import run_cross_validations

        class _It:
            def __init__(s, n, v, key=""):
                s.name, s.value, s.key = n, v, key
                s.is_simulated = s.is_stale = False
                s.has_conflict = False
                s.conflict_values = []
                s.confidence = 1.0
                s.source = "s"

        cs = run_cross_validations([
            _It("总工期", "90日历天", "total_duration"),
            _It("施工总工期", "120日历天", "total_duration"),
        ])
        assert [c for c in cs if c["rule_id"] == "XV-SAME-NAME"], \
            "同一 fact_key、不同显示名的取值分歧未被检出"

    def test_same_name_marks_conflict_on_key_group(self):
        """按 key 分组时，「初始无候选」的条目必须被打上 has_conflict。"""
        from app.services.facts_cross_validators import run_cross_validations

        class _It:
            def __init__(s, n, v, key=""):
                s.name, s.value, s.key = n, v, key
                s.is_simulated = s.is_stale = False
                s.has_conflict = False
                s.conflict_values = []
                s.confidence = 1.0
                s.source = "s"

        a = _It("总工期", "90日历天", "total_duration")
        b = _It("施工总工期", "120日历天", "total_duration")
        run_cross_validations([a, b])
        assert a.has_conflict is True and b.has_conflict is True, \
            "key 分组未回写 has_conflict"

    def test_missing_key_falls_back_to_name(self):
        """缺 fact_key 的条目（历史 shim / 手工替身）仍按 name 分组，行为不变。"""
        from app.services.facts_cross_validators import run_cross_validations

        class _It:
            def __init__(s, n, v):
                s.name, s.value, s.key = n, v, ""   # 无 key 属性语义的替身
                s.is_simulated = s.is_stale = False
                s.has_conflict = False
                s.conflict_values = []
                s.confidence = 1.0
                s.source = "s"

        cs = run_cross_validations([
            _It("总工期", "90日历天"), _It("总工期", "120日历天"),
        ])
        assert [c for c in cs if c["rule_id"] == "XV-SAME-NAME"], \
            "无 key 时应回落按 name 分组（旧行为不得丢）"

    def test_different_keys_not_merged(self):
        """反向：不同 fact_key 即使显示名相同也不得跨 key 合并判定。"""
        from app.services.facts_cross_validators import run_cross_validations

        class _It:
            def __init__(s, n, v, key=""):
                s.name, s.value, s.key = n, v, key
                s.is_simulated = s.is_stale = False
                s.has_conflict = False
                s.conflict_values = []
                s.confidence = 1.0
                s.source = "s"

        cs = run_cross_validations([
            _It("总工期", "90日历天", "total_duration"),
            _It("总工期", "120日历天", "other_thing"),
        ])
        assert not [c for c in cs if c["rule_id"] == "XV-SAME-NAME"], \
            "不同 fact_key 被错误合并"

    def test_dm_km_convertible(self):
        from app.services.facts_classification import extract_danger_params

        class _F:
            def __init__(s, n, v):
                s.name, s.value, s.key = n, v, ""

        assert extract_danger_params(
            [_F("人工挖孔桩深度", "0.5km")]).get("pile_depth") == pytest.approx(500.0)


# =========================================================================
# F. _chunk_hash 孤立代理字符指纹碰撞
# =========================================================================
class TestChunkHashNoCollision:
    """F：`encode(errors="ignore")` 遇孤立代理字符会**整字丢弃**。

    该指纹是增量提取的跳过键 + persist_extraction 的删除范围键 →
    碰撞意味着「该段已改过」被判「已提取过」，事实永不刷新（不可见陈旧）。
    """

    def test_lone_surrogate_does_not_collide(self):
        import hashlib
        from app.services.facts_extractor import _chunk_hash
        a, b = "a\udce9b", "ab"
        old = hashlib.sha1(a.encode("utf-8", errors="ignore")).hexdigest()[:16]
        assert old == _chunk_hash(b), "前置条件失效：旧口径本就不碰撞"
        assert _chunk_hash(a) != _chunk_hash(b), "孤立代理仍造成碰撞"

    def test_normal_text_hash_unchanged(self):
        """零 churn 保证：不含孤立代理的文本，指纹与旧口径逐字一致。"""
        import hashlib
        from app.services.facts_extractor import _chunk_hash
        for s in ["", "普通中文段落", "基坑深度 12.5m\nC30 混凝土",
                  "工期 90 日历天\n| 项目 | 值 |", "⚠️(模拟值) 标记"]:
            old = hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:16]
            assert _chunk_hash(s) == old, f"正常文本指纹漂移：{s!r}"

    def test_uses_surrogatepass(self):
        src = io.open(
            os.path.join(APP, "app", "services", "facts_extractor.py"),
            encoding="utf-8").read()
        # AST 判定：只看 encode() 的 errors 关键字实参（docstring 说明不算，§5.14）
        err_modes = []
        for n in ast.walk(ast.parse(src)):
            if not (isinstance(n, ast.Call) and _call_name(n) == "encode"):
                continue
            for kw in n.keywords:
                if kw.arg == "errors" and isinstance(kw.value, ast.Constant):
                    err_modes.append((n.lineno, kw.value.value))
        assert ("surrogatepass" in {m for _l, m in err_modes}), \
            "_chunk_hash 未使用 surrogatepass"
        assert "ignore" not in {m for _l, m in err_modes}, \
            f"仍存在 errors='ignore'（会丢孤立代理字符）：{err_modes}"


# =========================================================================
# G. merge_and_deduplicate 空 key 塌缩
# =========================================================================
class TestMergeEmptyKeyNoCollapse:
    """G：key="" 的条目曾全部落进同一簇 → N 条无关事实变 1 条 + N-1 个假矛盾。"""

    def _mk(self, name, value, key=""):
        from app.services.facts_extractor import FactItem
        it = FactItem(name=name, value=value)
        it.key = key
        it.is_simulated = False
        it.is_stale = False
        it.confidence = 1.0
        it.source = "s"
        it.has_conflict = False
        it.conflict_values = []
        return it

    def test_distinct_empty_key_items_stay_distinct(self):
        from app.services.facts_extractor import merge_and_deduplicate
        items = [self._mk("甲", "1"), self._mk("乙", "2"), self._mk("丙", "3")]
        out = merge_and_deduplicate(items)
        assert len(out) == 3, "空 key 条目被塌缩"
        assert all(it.has_conflict is False for it in out)
        assert sorted(it.name for it in out) == ["丙", "乙", "甲"]

    def test_same_name_empty_key_still_dedups(self):
        """同名空 key 仍应聚类去重（守卫不能把去重能力一并关掉）。"""
        from app.services.facts_extractor import merge_and_deduplicate
        items = [self._mk("基坑深度", "12.5m"), self._mk("基坑深度", "12.5 m")]
        out = merge_and_deduplicate(items)
        assert len(out) == 1
        assert out[0].has_conflict is False

    def test_same_name_empty_key_conflict_still_detected(self):
        from app.services.facts_extractor import merge_and_deduplicate
        items = [self._mk("总工期", "90日历天"), self._mk("总工期", "120日历天")]
        out = merge_and_deduplicate(items)
        assert len(out) == 1
        assert out[0].has_conflict is True

    def test_real_key_dedup_unaffected(self):
        from app.services.facts_extractor import merge_and_deduplicate
        a, b = self._mk("基坑深度", "12.5m", "k1"), self._mk("基坑深度", "12.5m", "k1")
        out = merge_and_deduplicate([a, b])
        assert len(out) == 1

    def test_orphan_without_key_and_name_preserved(self):
        from app.services.facts_extractor import merge_and_deduplicate
        items = [self._mk("", ""), self._mk("", ""), self._mk("甲", "1")]
        out = merge_and_deduplicate(items)
        assert len(out) == 3
        assert sum(1 for it in out if not it.name) == 2


# =========================================================================
# H. generate_facts 跨类型互斥
# =========================================================================
class TestFactsTaskExclusivity:
    """H：三条 SSE 链路里事实提取曾是唯一没有跨类型互斥的一条。

    事实提取与目录/正文生成都读写同一批 global_facts 行：
    正文启动时快照 facts → 提取中途 persist_extraction DELETE+INSERT 整批
    → 正文注入到已被删掉的行；提取收尾推进 facts_updated_at
    → 正在写的章被标「事实已过期」。
    """

    def _src(self):
        return io.open(os.path.join(APP, "app", "routers", "sse_handlers.py"),
                       encoding="utf-8").read()

    def test_all_three_sse_chains_use_exclusive_registration(self):
        src = self._src()
        assert src.count("_register_task_exclusive(") >= 3, \
            "三条生成链路未全部接入跨类型互斥"
        assert "outline_generation" in src
        assert "content_generation" in src
        assert "facts_generation" in src

    def test_facts_conflicts_with_both_other_kinds(self):
        from app.routers import sse_handlers as sh
        src = inspect.getsource(sh.generate_facts)
        assert "_register_task_exclusive" in src
        assert "outline_generation" in src and "content_generation" in src

    def test_facts_no_longer_uses_plain_register(self):
        """事实链路不得再走无 conflict_types 的裸 register_task。"""
        from app.routers import sse_handlers as sh
        src = inspect.getsource(sh.generate_facts)
        assert not re.search(
            r"=\s*await register_task\(\s*\"facts_generation\"", src), \
            "仍存在无跨类型互斥的裸 register_task 调用"

    def test_conflict_path_yields_error_event(self):
        from app.routers import sse_handlers as sh
        src = inspect.getsource(sh.generate_facts)
        assert "_conflict is not None" in src
        assert "'event': 'error'" in src

    def test_task_registry_supports_facts_kind(self):
        from app.services.ai import task_registry as tr
        src = io.open(tr.__file__.replace(".pyc", ".py"), encoding="utf-8").read() \
            if tr.__file__.endswith(".pyc") else io.open(tr.__file__, encoding="utf-8").read()
        assert "conflict_types" in src
        assert "TaskTypeConflict" in src


# =========================================================================
# J. 跨语言契约 parity（前端兜底表 vs 后端权威表）
# =========================================================================
def _frontend_src() -> str:
    root = os.path.dirname(APP)
    p = os.path.join(root, "frontend", "src", "pages", "SchemeWorkbenchPage.tsx")
    assert os.path.exists(p), f"找不到前端页面源码：{p}"
    return io.open(p, encoding="utf-8").read()


def _ts_record_literal(src: str, name: str) -> dict:
    """提取前端 ``NAME: Record<string, string> = { k: "v", ... }``。

    实现说明：本仓前端未装 @types/node / typescript 解析器，无法对 .tsx 做
    AST 解析（AGENTS.md §6 前端注意事项），故跨语言 parity 只能放在 pytest
    侧用**受限扫描**：定位声明 → 逐字符配平花括号（跳过字符串与注释）→
    取顶层 ``key: "value"`` 对。配平/解析失败一律抛错，**不返回半份结果**
    （半份结果会让 parity 断言以「后端多了一项」的形式误报，掩盖真问题）。
    """
    marker = f"{name}: Record<string, string> = {{"
    i = src.find(marker)
    assert i > 0, f"前端未找到 {name} 字典字面量（marker={marker!r}）"
    i += len(marker)

    out: dict = {}
    depth = 1
    token = ""          # 当前顶层键累积缓冲
    key: str | None = None
    val = ""            # 当前顶层值累积缓冲
    line_comment = False
    block_comment = False
    in_str = False
    quote = ""
    while i < len(src):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        # ---- 注释 / 字符串状态机 ----
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if ch == "\\":
                val += src[i + 1] if i + 1 < len(src) else ""
                i += 2
                continue
            if ch == quote:
                in_str = False
                if key is not None and depth == 1:
                    out[key] = val
                    key, val = None, ""
            else:
                val += ch
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in "\"'":
            # 只有在「刚读完一个顶层键」位置上的引号才是值
            if key is not None and depth == 1:
                in_str, quote = True, ch
            i += 1
            continue
        # ---- 结构字符 ----
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                break
            i += 1
            continue
        if depth == 1 and ch == ":":
            key = token.strip().strip('"').strip("'")
            token = ""
            i += 1
            continue
        if depth == 1 and ch == ",":
            token = ""
            key = None
            i += 1
            continue
        if ch in " \t\n\r":
            i += 1
            continue
        if depth == 1 and key is None:
            token += ch
        i += 1
    assert depth == 0, f"{name} 花括号未配平（扫描实现有 bug）"
    assert out, f"{name} 解析结果为空（marker 命中但未取到键值对）"
    return out


class TestFrontendFallbackParity:
    """J：前端兜底枚举表必须与后端权威表**逐项一致**。

    前端三处曾各自硬编码（章节标题 2 份副本 + 事实属性 4 个内联三元 +
    来源类型干脆零展示）。2026-10-06 起权威来源是
    ``GET /global-facts/category-map``，前端常量降级为**离线兜底** ——
    兜底表若与后端漂移，端点不可用时就会显示旧枚举，故必须锁 parity。
    """

    def test_chapter_titles_match_backend(self):
        from app.services.facts_classification import CHAPTER_ORDER, CHAPTER_TITLES
        fe = _ts_record_literal(_frontend_src(), "FACT_CHAPTER_TITLES")
        assert set(fe) == set(CHAPTER_ORDER), (
            f"前端兜底章节键与后端不一致："
            f"缺 {sorted(set(CHAPTER_ORDER) - set(fe))} / "
            f"多 {sorted(set(fe) - set(CHAPTER_ORDER))}")
        for k in CHAPTER_ORDER:
            assert fe[k] == CHAPTER_TITLES[k], f"章节 {k} 标题不一致"

    def test_fact_attr_titles_match_backend(self):
        from app.services.facts_classification import FACT_ATTR_TITLES
        fe = _ts_record_literal(_frontend_src(), "FALLBACK_FACT_ATTR_TITLES")
        assert set(fe) == set(FACT_ATTR_TITLES), (
            f"前端兜底事实属性键不一致：{sorted(set(FACT_ATTR_TITLES) ^ set(fe))}")
        for k, v in FACT_ATTR_TITLES.items():
            assert fe[k] == v, f"事实属性 {k} 标题不一致"

    def test_source_kind_titles_match_backend(self):
        from app.services.facts_classification import SOURCE_KIND_TITLES
        fe = _ts_record_literal(_frontend_src(), "FALLBACK_SOURCE_KIND_TITLES")
        assert set(fe) == set(SOURCE_KIND_TITLES), (
            f"前端兜底来源类型键不一致：{sorted(set(SOURCE_KIND_TITLES) ^ set(fe))}")
        for k, v in SOURCE_KIND_TITLES.items():
            assert fe[k] == v, f"来源类型 {k} 标题不一致"

    def test_category_map_payload_covers_all_three(self):
        """category-map 必须真的下发三套枚举（前端据此覆盖兜底表）。"""
        from app.services.facts_classification import category_map_payload
        p = category_map_payload()
        assert len(p["chapters"]) == 9
        assert p["fact_attr_titles"]
        assert p["source_kind_titles"]

    def test_frontend_actually_calls_category_map(self):
        """反向用例：兜底表不得又变成唯一来源（categoryMap 必须被页面调用）。"""
        src = _frontend_src()
        assert "factsApi.categoryMap()" in src, "页面未消费 /category-map"
        assert "setFactChapterTitles" in src

    def test_bid_analysis_tab_chapter_labels_parity(self):
        """BidAnalysisTab 的 CHAPTER_LABELS 兜底表同样必须与后端一致。"""
        from app.services.facts_classification import CHAPTER_ORDER, CHAPTER_TITLES
        p = os.path.join(
            os.path.dirname(APP),
            "frontend", "src", "components", "BidAnalysisTab.tsx")
        assert os.path.exists(p)
        fe = _ts_record_literal(io.open(p, encoding="utf-8").read(), "CHAPTER_LABELS")
        assert set(fe) == set(CHAPTER_ORDER), (
            f"BidAnalysisTab 章节键不一致：{sorted(set(CHAPTER_ORDER) ^ set(fe))}")
        for k in CHAPTER_ORDER:
            assert fe[k] == CHAPTER_TITLES[k], f"BidAnalysisTab 章节 {k} 标题不一致"


# =========================================================================
# K. 遗留项收口（2026-10-06 第二轮）
# =========================================================================
class TestContextBudgetSplit:
    """L1：``resolve_chunk_size`` 曾是**事实上的 no-op**。

    旧实现调 ``get_segment_limit(None, fixed)`` —— 首参恒 None，
    ``normalize_positive_int`` 永远回落 400_000，实测开关一开
    段长 8000 → **307930**（38.5 倍）。对 32k 窗口模型是致命的，
    而「按模型上下文窗口动态决定」正是该函数唯一的职责。
    """

    def test_default_config_is_off(self):
        """默认关闭 → 行为与引入前逐字节一致。"""
        from app.config import settings
        assert settings.facts_context_budget_split is False
        assert settings.facts_context_length_limit == 0

    def test_switch_off_returns_baseline(self, monkeypatch):
        from app.config import settings
        from app.services import facts_extractor as fx
        monkeypatch.setattr(settings, "facts_context_budget_split", False)
        assert fx.resolve_chunk_size(fx.CHUNK_SIZE) == fx.CHUNK_SIZE

    def test_switch_on_no_longer_blows_up(self, monkeypatch):
        """开启后不得再出现 30 万字级单段。"""
        from app.config import settings
        from app.services import facts_extractor as fx
        monkeypatch.setattr(settings, "facts_context_budget_split", True)
        monkeypatch.setattr(settings, "facts_context_length_limit", 0)
        monkeypatch.setattr(settings, "context_length_limit", 0)
        got = fx.resolve_chunk_size(fx.CHUNK_SIZE)
        assert got > fx.CHUNK_SIZE, "窗口够大时仍应放宽（该函数的意义所在）"
        assert got <= fx.FACTS_SEGMENT_HARD_CEILING, \
            f"单段 {got} 超过硬天花板，32k 窗口模型会直接超限"

    def test_never_returns_the_old_optimistic_value(self, monkeypatch):
        """反向用例：不得再回落 400k 乐观默认（307930 量级）。"""
        from app.config import settings
        from app.services import facts_extractor as fx
        monkeypatch.setattr(settings, "facts_context_budget_split", True)
        monkeypatch.setattr(settings, "facts_context_length_limit", 0)
        monkeypatch.setattr(settings, "context_length_limit", 0)
        got = fx.resolve_chunk_size(fx.CHUNK_SIZE)
        assert got < 100_000, f"段长 {got} 表明仍在用 400k 乐观默认"

    def test_resolution_order(self, monkeypatch):
        """facts 专属 > 全局 context_length_limit > 保守自动默认。"""
        from app.config import settings
        from app.services import facts_extractor as fx
        # ⚠️ 期望值必须取**硬天花板以下**：64000 会被夹到 60000，
        #    那是设计行为（单段过大会显著提高超时/限流风险），不是缺陷。
        monkeypatch.setattr(settings, "facts_context_length_limit", 0)
        monkeypatch.setattr(settings, "context_length_limit", 48000)
        assert fx.resolve_fact_context_chars() == 48000
        monkeypatch.setattr(settings, "facts_context_length_limit", 32000)
        assert fx.resolve_fact_context_chars() == 32000, "事实专属配置应优先"
        # 超天花板一律夹紧（两路来源都算）
        monkeypatch.setattr(settings, "facts_context_length_limit", 0)
        monkeypatch.setattr(settings, "context_length_limit", 500_000)
        assert fx.resolve_fact_context_chars() == fx.FACTS_SEGMENT_HARD_CEILING

    def test_hard_ceiling_always_applies(self, monkeypatch):
        from app.config import settings
        from app.services import facts_extractor as fx
        monkeypatch.setattr(settings, "facts_context_length_limit", 10_000_000)
        assert fx.resolve_fact_context_chars() == fx.FACTS_SEGMENT_HARD_CEILING

    def test_auto_default_is_conservative(self):
        """自动默认必须小于旧的 400k 乐观默认（该仓不持久化模型窗口）。"""
        from app.services import facts_extractor as fx
        from app.services.facts_patches import DEFAULT_CONTEXT_LENGTH_LIMIT
        assert fx.FACTS_AUTO_CONTEXT_CHARS < DEFAULT_CONTEXT_LENGTH_LIMIT, \
            "自动默认不应继续沿用 400k 乐观假设"

    def test_invalid_config_falls_back(self, monkeypatch):
        """非法配置（非数字/负数/None）必须回落，不得抛。"""
        from app.config import settings
        from app.services import facts_extractor as fx
        for bad in ("abc", -1, None, 0):
            monkeypatch.setattr(settings, "facts_context_length_limit", bad)
            got = fx.resolve_fact_context_chars()
            assert got > 0, f"配置 {bad!r} 导致非法结果 {got}"


class TestUnitModelSingleSource:
    """L2：单位模型从 2 量纲扩到 10 量纲，且视图必须派生而非手写。"""

    def test_covers_engineering_dimensions(self):
        from app.services.facts_cross_validators import _UNIT_DIM
        dims = set(_UNIT_DIM.values())
        for want in ("length", "mass", "force", "line_load", "pressure",
                     "area", "volume", "duration", "temperature", "ratio"):
            assert want in dims, f"缺少量纲 {want}"

    def test_line_and_area_load_parsed(self):
        """复合单位此前只取到前缀（kN），线/面荷载永远解析不全。"""
        from app.services.facts_cross_validators import _extract_quantity
        assert _extract_quantity("12.5kN/m²")[2] == "pressure"
        assert _extract_quantity("3.5 kN/m")[2] == "line_load"

    def test_superscript_and_fullwidth_area(self):
        from app.services.facts_cross_validators import _extract_quantity
        for text in ("500㎡", "120m²", "120m2"):
            q = _extract_quantity(text)
            assert q is not None and q[2] == "area", f"{text} 面积解析错：{q}"

    def test_volume_not_mistaken_for_length(self):
        """反向：``2.0m3`` 此前只取到 ``m`` → 体积被误判成长度（解析成功但量纲全错）。"""
        from app.services.facts_cross_validators import _extract_quantity
        q = _extract_quantity("2.0m3")
        assert q is not None and q[2] == "volume", f"体积量纲错：{q}"


class TestInsertColumnsSingleSource:
    """L4：INSERT 列清单曾是两份（服务 27 列 / 路由 28 列）。"""

    def test_columns_defined_once(self):
        from app.services import facts_extractor as fx
        from app.routers import global_facts as gf
        assert gf.MANUAL_FACT_INSERT_COLS is fx.GLOBAL_FACTS_INSERT_COLS, \
            "路由侧必须是服务层列清单的**别名**，不得本地维护"
        assert gf.MANUAL_FACT_INSERT_SQL is fx.GLOBAL_FACTS_INSERT_SQL

    def test_row_width_matches_columns(self):
        """to_db_row / _manual_fact_row 的返回值个数必须等于列数。"""
        from app.services.facts_extractor import FactItem, GLOBAL_FACTS_INSERT_COLS
        it = FactItem(name="x", value="y")
        row = it.to_db_row("g", "p", "s", "T")
        assert len(row) == len(GLOBAL_FACTS_INSERT_COLS), \
            f"to_db_row {len(row)} 值 vs {len(GLOBAL_FACTS_INSERT_COLS)} 列"

    def test_placeholder_count_derived(self):
        from app.services.facts_extractor import (
            GLOBAL_FACTS_INSERT_COLS, GLOBAL_FACTS_INSERT_SQL)
        sql = GLOBAL_FACTS_INSERT_SQL
        assert sql.count("?") == len(GLOBAL_FACTS_INSERT_COLS)
        # 断言 VALUES 段（SQL 以 ")" 结尾，不能用 endswith 整串）
        values = sql.split("VALUES", 1)[1].strip()
        assert values == "(" + ",".join("?" for _ in GLOBAL_FACTS_INSERT_COLS) + ")", \
            "占位符必须由列数派生，且无多余空格（与既有 SQL 逐字一致）"

    def test_no_inline_column_list_in_persist(self):
        """persist_extraction 不得再内联列字面量（漂移源）。"""
        from app.services import facts_extractor as fx
        src = io.open(fx.__file__, encoding="utf-8").read()
        assert "GLOBAL_FACTS_INSERT_SQL, insert_buf" in src
        # 内联 INSERT 只应剩 GLOBAL_FACTS_INSERT_SQL 的定义处
        assert src.count("INSERT INTO global_facts") == 1, \
            "persist_extraction 仍在内联 INSERT 语句"

    def test_dimension_columns_stay_last_four(self):
        """既有契约：to_db_row()[-4:] 是九大章节四维。"""
        from app.services.facts_extractor import FactItem
        it = FactItem(name="x", value="y", chapter="plan", fact_attr="quantitative",
                      source_kind="bid_doc", is_shared=True)
        row = it.to_db_row("g", "p", "s", "T")
        assert row[-4:] == ("plan", "quantitative", "bid_doc", 1)

    def test_is_stale_present_in_columns(self):
        from app.services.facts_extractor import GLOBAL_FACTS_INSERT_COLS
        assert "is_stale" in GLOBAL_FACTS_INSERT_COLS


class TestResolveSchemeProjectIdObservable:
    """L5：曾是无日志裸吞 → 失败时项目共享事实整体消失且无线索。"""

    @pytest.mark.asyncio
    async def test_logs_when_scheme_missing(self, caplog):
        import logging

        from app.services.facts_extractor import resolve_scheme_project_id

        class _Db:
            async def execute(self, *a, **k):
                class _C:
                    async def fetchone(self_inner):
                        return None
                return _C()

        with caplog.at_level(logging.WARNING, logger="facts_extractor"):
            got = await resolve_scheme_project_id(_Db(), "no-such-scheme")
        assert got == ""
        assert any("no-such-scheme" in r.getMessage() for r in caplog.records), \
            "方案查不到必须留 WARNING（否则静默收窄作用域）"

    @pytest.mark.asyncio
    async def test_logs_on_exception(self, caplog):
        import logging

        from app.services.facts_extractor import resolve_scheme_project_id

        class _Db:
            async def execute(self, *a, **k):
                raise RuntimeError("db down")

        with caplog.at_level(logging.WARNING, logger="facts_extractor"):
            got = await resolve_scheme_project_id(_Db(), "s1")
        assert got == ""
        assert any("db down" in r.getMessage() for r in caplog.records), \
            "DB 异常必须留 WARNING"

    @pytest.mark.asyncio
    async def test_empty_scheme_id_is_quiet(self, caplog):
        """未传 scheme_id 是正常调用，不该刷 WARNING。"""
        import logging

        from app.services.facts_extractor import resolve_scheme_project_id

        class _Db:
            async def execute(self, *a, **k):  # pragma: no cover - 不应被调用
                raise AssertionError("空 scheme_id 不应触发查询")

        with caplog.at_level(logging.WARNING, logger="facts_extractor"):
            assert await resolve_scheme_project_id(_Db(), "") == ""
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestPlaceholderInventoryScope:
    """L6：占位符重跑计划此前只取方案级事实，看不到项目共享级。"""

    def test_uses_canonical_query_builder(self):
        from app.services import placeholder_inventory as pi
        src = io.open(pi.__file__, encoding="utf-8").read()
        assert "build_injectable_facts_query" in src, "未复用统一作用域出口"
        # 必须带 gt 别名，否则 ORDER BY gt 报错 → 语料整体为空（假不可重跑）
        assert "FACTS_GT_COLUMN" in src, "columns 缺 gt 别名"

    def test_no_local_scheme_only_where(self):
        from app.services import placeholder_inventory as pi
        tree = ast.parse(io.open(pi.__file__, encoding="utf-8").read())
        # 只允许降级分支里保留 scheme_id-only SQL，且必须有中文告警注释说明
        # ⚠️ 锚点必须同时含 global_facts：裸 "WHERE scheme_id=?" 还会命中
        #    sections / bid_analysis_items 查询（§5.14 锚点过宽）。
        hits = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and "global_facts" in n.value and "WHERE scheme_id=?" in n.value:
                hits.append(n.lineno)
        assert len(hits) <= 1, (
            f"仍有 {len(hits)} 处本地 global_facts scheme_id-only 查询：行 {hits}")

    def test_resolves_project_id(self):
        from app.services import placeholder_inventory as pi
        assert hasattr(pi, "_resolve_project_id_for_scope")


class TestFactKeyChapterNoDeadKeys:
    """L7：FACT_KEY_TO_CHAPTER 曾谎称「从 DANGER_PARAM_RULES 派生」。

    实测两套词表**不是一回事**：
      · DANGER_PARAM_RULES → HAZARD_THRESHOLDS.params（危大阈值参数字汇）
      · normalize_key 产物   → fact_key 命名空间
    旧表 24 键里 10 个属前者、``normalize_key`` 根本产不出来 → 死条目。
    """

    def test_every_key_producible(self):
        from app.services.facts_extractor import _NAME_KEY_INDEX
        from app.services.facts_classification import FACT_KEY_TO_CHAPTER
        producible = {k for _frag, k in _NAME_KEY_INDEX}
        dead = sorted(k for k in FACT_KEY_TO_CHAPTER if k not in producible)
        assert dead == [], f"含产不出的死键：{dead}"

    def test_no_threshold_param_leakage(self):
        """危大阈值参数不得混入 fact_key 表（范畴错误）。"""
        from app.services.facts_classification import (
            DANGER_PARAM_RULES, FACT_KEY_TO_CHAPTER)
        params = {p for _kws, p in DANGER_PARAM_RULES}
        overlap = params & set(FACT_KEY_TO_CHAPTER)
        assert overlap == set(), f"阈值参数混入 fact_key 表：{sorted(overlap)}"

    def test_docstring_no_false_derivation_claim(self):
        """注释不得再宣称「从 DANGER_PARAM_RULES 派生」（已证明是假声明）。"""
        from app.services import facts_classification as fc
        src = io.open(fc.__file__, encoding="utf-8").read()
        # 定位 FACT_KEY_TO_CHAPTER 的注释块（表格定义之前）
        i = src.find("FACT_KEY_TO_CHAPTER")
        j = src.rfind("英文归一化键", 0, i)
        block = src[j:i] if j > 0 else src[max(0, i - 2000):i]
        # ⚠️ 不能断言「旧字串消失」：新注释**有意引用**那句假声明并加以纠正
        #    （「旧注释声称…然而实现却是…」）。正确定性是「纠正说明存在」。
        assert ("不是同一套词表" in block or "范畴错误" in block
                or "假声明" in block), \
            "注释必须写明两套词表（DANGER_PARAM_RULES vs normalize_key）的关系"
        assert "实现却是 24 键手写字面量" in block, \
            "注释必须记录「曾谎称派生」这一事实，便于后人不再被误导"

    def test_danger_params_still_complete(self):
        """回归：清理死键不得影响危大阈值参数字汇自身的完整性。"""
        from app.services import scheme_classification as sc
        from app.services.facts_classification import DANGER_PARAM_RULES
        producible = {p for _kws, p in DANGER_PARAM_RULES}
        declared = set()
        for _n, spec in sc.HAZARD_THRESHOLDS.items():
            declared.update(spec.get("params") or ())
        assert declared <= producible, f"阈值参数缺口：{sorted(declared - producible)}"


class TestFrontendConsumesByChapterAndPagination:
    """L8：后端每次下发的 stats.by_chapter / pagination 曾被前端整体丢弃。"""

    def _src(self):
        root = os.path.dirname(APP)
        return io.open(os.path.join(
            root, "frontend", "src", "pages", "SchemeWorkbenchPage.tsx"),
            encoding="utf-8").read()

    def test_api_sends_limit_offset(self):
        root = os.path.dirname(APP)
        src = io.open(os.path.join(
            root, "frontend", "src", "api", "index.ts"), encoding="utf-8").read()
        assert "options?.limit" in src and "options?.offset" in src, \
            "factsApi.list 未下发分页参数"
        assert "limit: options?.limit" in src or "options?.limit ?" in src

    def test_page_consumes_pagination(self):
        src = self._src()
        assert "buildFactsPagination" in src, "未消费 pagination"
        assert "<Pagination" in src, "未渲染分页控件"
        assert "FACTS_PAGE_SIZE" in src

    def test_page_consumes_by_chapter(self):
        src = self._src()
        assert "buildFactChapterStats" in src, "未消费 stats.by_chapter"
        # ⚠️ 必须锚定 **JSX 挂载**而非函数名：函数定义本身就叫
        # FactsChapterCoveragePanel，只判名字存在的话「删掉挂载」也能过
        # （A/B L8b 首跑实测全绿）。
        assert "<FactsChapterCoveragePanel" in src, \
            "九章事实分布面板未挂载到事实 Tab"
        assert "by_chapter" in src

    def test_types_declare_new_fields(self):
        root = os.path.dirname(APP)
        p = os.path.join(root, "frontend", "src", "types", "facts.ts")
        assert os.path.exists(p)
        src = io.open(p, encoding="utf-8").read()
        assert "by_chapter?: FactChapterStat[]" in src
        assert "interface FactPagination" in src


# =========================================================================
# I. facts_enrich / facts_patches 二次实现收敛
# =========================================================================
class TestEnrichSingleSource:
    """I：facts_enrich 自带 docstring 禁止二次实现，却自己又造了一份。"""

    def test_enrich_reuses_patch_modes_constant(self):
        from app.services import facts_enrich as fe
        src = inspect.getsource(fe)
        assert "from app.services.facts_patches import" in src
        assert "PATCH_MODES" in src, "未复用 facts_patches.PATCH_MODES"
        assert '("append", "prepend", "replace")' not in src, \
            "facts_enrich 仍在内联 PATCH_MODES 字面量"

    def test_enrich_reuses_normalize_patches_response(self):
        from app.services import facts_enrich as fe
        src = inspect.getsource(fe)
        assert "normalize_patches_response" in src, \
            "_build_patches 未复用 facts_patches.normalize_patches_response"

    def test_new_fact_id_prefix_consistent(self):
        """kb_{n} / patch_{n} 两套前缀应收敛为同一约定。

        ⚠️ AST 判定 + **排除 docstring**：``kb_{`` / ``patch_{`` 出现在本轮
        新写的说明 docstring 里；且 AST 里 docstring 本身就是 Constant 节点，
        只做 ast.walk 仍会命中 —— 第四次踩 AGENTS.md §5.14「锚点过宽」。
        判据只取**非裸字符串表达式**的 Constant（即真实参与逻辑的字面量）。
        """
        from app.services import facts_enrich as fe
        from app.services import facts_patches as fp
        assert "fact_{" in inspect.getsource(fp.normalize_fact_id)
        tree = ast.parse(inspect.getsource(fe))
        # docstring = 裸字符串表达式（模块/类/函数首句），整体排除
        docstrings = {id(n.value) for n in ast.walk(tree)
                      if isinstance(n, ast.Expr)
                      and isinstance(n.value, ast.Constant)
                      and isinstance(n.value.value, str)}
        literals = set()
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
                continue
            if id(n) in docstrings:
                continue
            for pref in ("kb_{", "patch_{"):
                if pref in n.value:
                    literals.add(pref)
        assert literals == set(), f"仍使用独立新事实 id 前缀：{sorted(literals)}"

    def test_patches_gates_live_in_extractor_not_enrich(self):
        """两个 enrich 阶段的开关读点必须在编排层，且仅一处。

        ⚠️ 判据用 AST 而非文本：这两个配置名出现在 facts_enrich 的
        **docstring** 里（说明它们由谁读），纯文本匹配会恒失败 ——
        正是 AGENTS.md §5.7/§5.14 记录的「锚点过宽比不写护栏更糟」。
        """
        import ast as _ast
        from app.services import facts_enrich as fe
        tree = _ast.parse(inspect.getsource(fe))
        read_attrs = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute) and node.attr in (
                    "facts_knowledge_patch_enabled", "facts_finalize_enabled"):
                read_attrs.add(node.attr)
        assert read_attrs == set(), (
            f"开关读点散落在 facts_enrich（应只在编排层 facts_extractor）："
            f"{sorted(read_attrs)}")

    def test_replace_mode_conflict_semantics_documented(self):
        """replace 模式会清 has_conflict —— 必须与 auto_resolvable=False 并存说明。"""
        from app.services import facts_patches as fp
        src = inspect.getsource(fp.apply_patches_to_fact_items)
        assert "has_conflict" in src
        assert "replace" in src

    def test_scenes_registered(self):
        from app.services.ai import provider_factory as pf
        assert "facts_knowledge_patch" in pf.KNOWN_SCENES
        assert "facts_finalize" in pf.KNOWN_SCENES
        assert "facts_extract" in pf.KNOWN_SCENES
        assert "global_facts_adjust" in pf.KNOWN_SCENES
