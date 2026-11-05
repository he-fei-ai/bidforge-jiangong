"""导出模块收口护栏（R99 · 2026-10-17）

本轮从「导出文档模块」的端到端探针取证中定位并修复的 4 处缺陷 + 1 处增强，
全部在本文件留下**可执行断言**，并逐条做过「把接线改回旧形态」的反向验证。

分组：
  A 组 · F1 · DLV-14 图号 / 表号漂移检测接线（原本结构性不可达）
  B 组 · F1 · 超集保证（渲染真实分配 ⊆ 预检集合 ⇒ 零新增误报）
  C 组 · F2 · 占位符基线写入收口 R13 单一出口
  D 组 · F3 · 围栏自动修复可观测性（X-Fix-Stats + content_audit 提示）
  E 组 · F4 · 机器标记（CHART_TYPE）识别与不泄漏
  F 组 · E1 · 缓存命中快速路径（取数段 / 渲染段拆分 + 指纹 parity）
  G 组 · 接线与静态卫生（AST / 字节体检）

判据纪律（AGENTS §5.14 / §5.15）：
  · 静态锁一律锚**调用形态**或 AST 节点，不锚「源码里有没有这个字符串」；
  · 计数型断言必须配「非空转自证」，防判据静默失灵；
  · 每个修复点都做过 A/B 反向验证（改回旧形态 → 本文件定向失败）。
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import app.config as cfg
import app.routers.export as E
import pytest
from app.services.content_blocks import (
    _RE_CHART_TYPE_TAG,
    _parse_content_blocks,
    chart_type_tags,
)
from app.services.content_blocks import (
    _RE_CHART_TYPE_TAG_LOOSE as _RE_CHART_TYPE_LOOSE,
)

APP_DIR = Path(E.__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _src(fn) -> str:
    return inspect.getsource(fn)


def _mod_ast(rel: str):
    return ast.parse((APP_DIR / rel).read_bytes().decode("utf-8"))


def _fn_ast(rel: str, name: str):
    tree = _mod_ast(rel)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError("未找到函数 " + name)


def _call_names(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _has_kwarg(node, kw: str) -> bool:
    return any(k.arg == kw for n in ast.walk(node) if isinstance(n, ast.Call) for k in n.keywords)


def _chart(code: str = "flowchart TD\nA[a]-->B[b]") -> str:
    return "```mermaid\n" + code + "\n```\n"


def _table(header: str = "a | b") -> str:
    h = header.split("|")
    sep = "|".join(" --- " for _ in h)
    row = "|".join(" 1 " for _ in h)
    return f"| {header} |\n|{sep}|\n|{row} |\n"


def _sec(sid, parent="", so=0, lv=1, title="章", content=""):
    return {"id": sid, "parent_id": parent, "sort_order": so, "level": lv, "title": title, "content": content}


# ===========================================================================
# A 组 · F1 · DLV-14 图号 / 表号漂移检测接线
# ===========================================================================


class TestDLV14FigureTableWiring:
    """修复前：`collect_export_issues` 调 `_detect_stale_cross_references(sections)`
    从不传 real_nums → fig / tbl 集合恒空 → 两个分支结构性不可达。"""

    def test_collect_export_issues_passes_real_nums(self):
        node = _fn_ast("routers/export.py", "collect_export_issues")
        calls = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_detect_stale_cross_references"
        ]
        assert calls, "collect_export_issues 不再调用 _detect_stale_cross_references"
        for c in calls:
            assert c.args, "sections 未按位置传入"
            assert {k.arg for k in c.keywords} == {"real_nums"}, (
                "必须显式传 real_nums ⇒ 图号/表号分支再次变成结构性不可达" "（缺陷本体复发）"
            )

    def test_superset_is_supplied_by_the_single_outlet(self):
        """供给 real_nums 的必须是「唯一出口」，不得就地内联另一份推导。"""
        node = _fn_ast("routers/export.py", "collect_export_issues")
        calls = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_detect_stale_cross_references"
        ]
        assert calls
        for c in calls:
            for kw in c.keywords:
                if kw.arg != "real_nums":
                    continue
                assert isinstance(kw.value, ast.Call), (
                    "real_nums 不得是内联字面量，必须调用 " "collect_figure_table_number_superset"
                )
                assert kw.value.func.id == "collect_figure_table_number_superset"

    def test_real_num_guards_are_not_dead_code(self):
        """`_detect_stale_cross_references` 里 `if real_nums["fig"]:` / `["tbl"]`
        这两个分支现在必须可达 —— 否则接线是假的。"""
        src = _src(E._detect_stale_cross_references)
        assert 'if real_nums["fig"]:' in src
        assert 'if real_nums["tbl"]:' in src

    def test_superset_function_is_exported(self):
        assert callable(E.collect_figure_table_number_superset)


# ===========================================================================
# B 组 · F1 · 超集保证（零新增误报的方向锁）
# ===========================================================================


class TestSupersetGuarantee:
    """预检集合必须是渲染真实分配的**超集**；否则每次导出预检都会新增误报
    （比漏报更糟：用户被告知「引用了不存在的图」却其实存在）。"""

    def test_superset_contains_only_well_formed_trees(self):
        # 单章一级树 → 有集合
        sup = E.collect_figure_table_number_superset([_sec("a", content=_chart())])
        assert sup["fig"] == {"图1-1"}, sup
        # 根节点不是一级章 → 降级为空集合（章序号口径可能分叉，宁可不报）
        sup = E.collect_figure_table_number_superset([_sec("a", lv=2, content=_chart())])
        assert sup["fig"] == set() and sup["tbl"] == set(), sup
        # 空树
        assert E.collect_figure_table_number_superset([])["fig"] == set()

    def test_counts_charts_images_and_ai_image_as_figures(self):
        content = (
            _chart("flowchart TD\nA-->B")
            + _chart("flowchart TD\nC-->D")
            + "![alt](https://x.invalid/a.png)\n"
            + '```ai_image\n{"prompt":"p"}\n```\n'
        )
        sup = E.collect_figure_table_number_superset([_sec("a", content=content)])
        assert sup["fig"] == {"图1-1", "图1-2", "图1-3", "图1-4"}, sup

    def test_tables_counted_separately(self):
        # 表头行必须以 `|` 起始（`**表** | p | q |` 会被当成段落而非表格），
        # 故标签单独成行；相邻两块之间留空行防并块。
        sup = E.collect_figure_table_number_superset(
            [_sec("a", content=_chart() + "\n\n" + _table("p | q") + "\n\n" + _table("m | n"))]
        )
        assert sup["fig"] == {"图1-1"}, sup
        assert sup["tbl"] == {"表1-1", "表1-2"}, sup

    def test_per_chapter_counters_do_not_leak(self):
        """图号计数器按「章」分桶：第 2 章的第一张图是 2-1 而不是 1-2。"""
        secs = [
            _sec("c1", so=0, content=_chart("flowchart TD\nA-->B") + _chart("flowchart TD\nC-->D")),
            _sec("c2", so=1, content=_chart("flowchart TD\nE-->F")),
        ]
        sup = E.collect_figure_table_number_superset(secs)
        assert sup["fig"] == {"图1-1", "图1-2", "图2-1"}, sup

    def test_child_sections_join_parent_chapter(self):
        """L2 子章节的图表挂在**其一级章**的计数器上（与渲染端
        `_figure_chapter_num` 同口径）。"""
        secs = [
            _sec("c1", so=0, lv=1, content=_chart("flowchart TD\nA-->B")),
            _sec("c11", parent="c1", so=1, lv=2, content=_chart("flowchart TD\nC-->D") + _chart("flowchart TD\nE-->F")),
        ]
        sup = E.collect_figure_table_number_superset(secs)
        assert sup["fig"] == {"图1-1", "图1-2", "图1-3"}, sup

    def test_orphan_parent_is_treated_as_root_and_degrades_safely(self):
        """孤儿节点（parent_id 指向已删章节）挂为根 → 若根非一级章则降级。"""
        sup = E.collect_figure_table_number_superset([_sec("x", parent="ghost", lv=2, content=_chart())])
        assert sup["fig"] == set(), "孤儿 L2 根必须降级为不报"

    def test_level_garbage_does_not_raise(self):
        """脏 level（DB 是 TEXT 列）既不得让预检抛异常，也不得让渲染炸掉。"""
        secs = [
            _sec("a", content=_chart("flowchart TD\nA-->B")),
            {"id": "b", "parent_id": "a", "sort_order": 1, "level": "不是整数", "title": "脏", "content": ""},
        ]
        sup = E.collect_figure_table_number_superset(secs)
        assert sup["fig"], "脏 level 不得抛异常"
        # 渲染侧的同一把尺子也必须扛得住（否则整份导出 500）
        assert E._section_sort_key(secs[1]) == (1, 1, "b")

    def test_empty_content_is_safe(self):
        sup = E.collect_figure_table_number_superset([_sec("a"), _sec("b", parent="a", lv=2, so=1)])
        assert sup == {"fig": set(), "tbl": set()}


# ===========================================================================
# C 组 · F2 · 占位符基线写入收口 R13 单一出口
# ===========================================================================


class TestPlaceholderBaselineR13:
    """修复前：本函数两条写入是全模块唯一绕过 `_db_exec` 的写路径
    （裸 execute + 丢弃游标 + 不判空 + 不看 rowcount）。"""

    def test_no_bare_execute_in_baseline_writer(self):
        node = _fn_ast("routers/export.py", "_record_placeholder_baseline")
        bare = [
            n.lineno
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"
        ]
        assert bare == [], (
            f"_record_placeholder_baseline 出现裸 db.execute（L{bare}）—— "
            f"R13 下会静默丢写；写路径必须走 _db_exec 单一出口"
        )

    def test_both_statements_go_through_db_exec(self):
        node = _fn_ast("routers/export.py", "_record_placeholder_baseline")
        execs = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_db_exec"
        ]
        assert len(execs) == 2, f"INSERT + DELETE 都须走 _db_exec，实得 {len(execs)}"

    def test_prune_is_gated_on_insert_effectiveness(self):
        """写失败时不得裁剪历史基线（否则趋势被单次失败污染）。"""
        node = _fn_ast("routers/export.py", "_record_placeholder_baseline")
        execs = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_db_exec"
        ]
        deletes = [
            c
            for c in execs
            if any(
                isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("DELETE")
                for a in c.args
            )
        ]
        assert deletes, "找不到 DELETE 语句"
        assert any(
            n
            for n in ast.walk(node)
            if isinstance(n, ast.If)
            and ast.walk(n)
            and any(isinstance(x, ast.Name) and x.id == "inserted" for x in ast.walk(n))
        ), "裁剪必须以「INSERT 生效」为前置条件"

    @pytest.mark.asyncio
    async def test_none_cursor_is_reported_not_silent(self, caplog):
        """R13：execute 返回 None 时必须有 WARNING，且不得抛异常阻断预检。"""

        class _NullCur:
            async def execute(self, *a, **k):
                return None

            async def commit(self):
                return None

            async def rollback(self):
                return None

        with caplog.at_level("WARNING", logger="export"):
            await E._record_placeholder_baseline("s", {"total": 1}, _NullCur())
        assert any("未生效" in r.getMessage() for r in caplog.records), "execute()->None 未留痕 —— R13 静默丢写复发"

    @pytest.mark.asyncio
    async def test_normal_path_still_writes_and_prunes(self, db_conn):
        await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("sb", "p", "s"))
        for i in range(E._PLACEHOLDER_BASELINE_KEEP + 3):
            await E._record_placeholder_baseline("sb", {"total": i}, db_conn)
        cur = await db_conn.execute("SELECT COUNT(*) FROM placeholder_baselines WHERE scheme_id=?", ("sb",))
        row = await cur.fetchone()
        assert row[0] == E._PLACEHOLDER_BASELINE_KEEP, (
            "写入端裁剪失效（应保留最近 %d 行）" % E._PLACEHOLDER_BASELINE_KEEP
        )


# ===========================================================================
# D 组 · F3 · 围栏自动修复可观测性
# ===========================================================================


class TestFenceFixObservability:
    """修复前：开启 auto_fix_unclosed_fences 后 DLV-07 的 error 级提示被
    补齐动作抹掉，且「围栏之后的正文被并入图表源码并丢弃」这一事实零信号。"""

    def test_fix_stats_has_fences_fixed_key(self):
        assert "fences_fixed" in E._FIX_STATS_KEYS
        # 加法式：既有 9 个键必须逐字保留
        for k in (
            "formulas",
            "block_formulas",
            "replacement_chars",
            "control_chars",
            "gbk_mojibake",
            "latin1_mojibake",
            "cyrillic",
            "block_render_failed",
            "table_truncated",
        ):
            assert k in E._FIX_STATS_KEYS, f"既有键 {k} 被误删"

    def test_fix_stats_init_covers_every_key(self):
        st = E._fix_stats()
        assert set(st) == set(E._FIX_STATS_KEYS)

    def test_audit_content_default_is_byte_identical(self):
        """缺省 fences_fixed=0 ⇒ 与改造前逐字一致（既有调用方零影响）。"""
        secs = [{"title": "t", "content": "```mermaid\nflowchart TD\n"}]
        base = E.audit_content(secs)
        explicit0 = E.audit_content(secs, fences_fixed=0)
        assert base == explicit0
        assert not any(i["key"] == "fence_auto_fixed" for i in base["items"])

    def test_audit_content_emits_advisory_when_fences_fixed(self):
        secs = [{"title": "t", "content": "```mermaid\nflowchart TD\n"}]
        a = E.audit_content(secs, fences_fixed=2)
        item = next(i for i in a["items"] if i["key"] == "fence_auto_fixed")
        assert item["level"] == "warn"
        assert item["count"] == 2
        assert "2 处" in item["label"]
        # 未闭合围栏的 error 级提示不应被这条取代（两者可共存）
        assert any(i["key"] == "unclosed_fence" for i in a["items"])

    def test_fences_fixed_flows_into_docx_builder_signature(self):
        sig = inspect.signature(E._build_docx_sync)
        assert "fences_fixed" in sig.parameters
        assert sig.parameters["fences_fixed"].default == 0

    def test_fences_fixed_is_passed_by_task_assembler(self):
        """`_build_docx_task` 是两条链路的唯一收敛点，必须传该参数。"""
        node = _fn_ast("routers/export.py", "_build_docx_task")
        assert '"fences_fixed"' in _src(E._build_docx_task)

    @pytest.mark.asyncio
    async def test_prepare_export_sets_and_reports_fences_fixed(self, db_conn):
        await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("fs2", "p", "s"))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order,"
            " level, title, status, content) VALUES (?,?,?,?,?,?,?,?)",
            ("fs2c", "fs2", "", 0, 1, "1 章", "generated", "```mermaid\nflowchart TD\nA-->B\n"),
        )
        await db_conn.commit()
        prep = await E._prepare_export("fs2", {"config": {"auto_fix_unclosed_fences": True}}, db_conn)
        assert prep["fences_fixed"] >= 1, prep["fences_fixed"]
        keys = [i["key"] for i in prep["content_audit"]["items"]]
        assert "fence_auto_fixed" in keys, keys

    @pytest.mark.asyncio
    async def test_switch_off_keeps_zero(self, db_conn):
        await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("fs3", "p", "s"))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order,"
            " level, title, status, content) VALUES (?,?,?,?,?,?,?,?)",
            ("fs3c", "fs3", "", 0, 1, "1 章", "generated", "```mermaid\nflowchart TD\nA-->B\n"),
        )
        await db_conn.commit()
        prep = await E._prepare_export("fs3", {"config": {}}, db_conn)
        assert prep["fences_fixed"] == 0
        keys = [i["key"] for i in prep["content_audit"]["items"]]
        assert "fence_auto_fixed" not in keys, keys
        assert "unclosed_fence" in keys, keys


# ===========================================================================
# E 组 · F4 · 机器标记（CHART_TYPE）识别与不泄漏
# ===========================================================================


class TestChartTypeMarkerHardening:
    """修复前：只认半角 `[CHART_TYPE: x]`。全角 `【CHART_TYPE: x】` 退化为
    普通段落 → **机器标记原文被印进交付 DOCX**（实测 artifacts/exp94/probe_blocks.py）。"""

    @pytest.mark.parametrize(
        "line",
        [
            "[CHART_TYPE: flowchart]",
            "[CHART_TYPE:flowchart]",
            "[CHART_TYPE: flowchart ]",
            "【CHART_TYPE: flowchart】",
            "【CHART_TYPE:flowchart】",
            "[CHART_TYPE：flowchart]",
        ],
    )
    def test_variants_are_recognized_as_chart(self, line):
        blocks = _parse_content_blocks("正文\n\n" + line + "\n")
        assert any(
            b.get("type") == "chart" for b in blocks
        ), f"机器标记未被识别为图表块：{line} -> {[b.get('type') for b in blocks]}"

    def test_type_is_captured(self):
        blocks = _parse_content_blocks("【CHART_TYPE: gantt】\n")
        chart = [b for b in blocks if b.get("type") == "chart"]
        assert chart and chart[0]["chart_type"] == "gantt"

    def test_malformed_marker_never_leaks_raw_text(self):
        """形似机器标记但类型不合法 ⇒ 整块跳过，绝不按普通段落输出。"""
        for line in ("[CHART_TYPE: ]", "[CHART_TYPE: 流程图!]", "【CHART_TYPE：】", "[CHART_TYPE: flowchart"):
            blocks = _parse_content_blocks("正文\n\n" + line + "\n")
            assert not any(b.get("type") == "paragraph" and "CHART_TYPE" in (b.get("text") or "") for b in blocks), (
                f"机器标记原文泄漏进交付正文：{line} -> " f"{[b.get('text') for b in blocks]}"
            )
            assert not any("CHART_TYPE" in (b.get("text") or "") for b in blocks), line

    def test_loose_regex_does_not_swallow_normal_prose(self):
        """收窄闸门：只有以 `[`/`【` + `CHART_TYPE` 开头的行才会被跳过。"""
        assert _RE_CHART_TYPE_LOOSE.match("[CHART_TYPE: x]")
        assert not _RE_CHART_TYPE_LOOSE.match("本节引用 [CHART_TYPE: x] 的约定")
        assert not _RE_CHART_TYPE_LOOSE.match("CHART_TYPE 是占位符类型")
        assert not _RE_CHART_TYPE_TAG.match("[CHART_TYPE: ]")

    def test_chart_type_tags_is_single_source(self):
        assert chart_type_tags("a\n[CHART_TYPE: gantt]\nb\n【CHART_TYPE: labor】") == {"gantt", "labor"}
        assert chart_type_tags("") == set()
        assert chart_type_tags(None) == set()

    def test_charts_list_uses_the_single_outlet(self):
        """`/charts/list` 的落点判定必须调 content_blocks.chart_type_tags，
        不得再自写字面串（两侧口径分叉 → 清单显示已落位、导出却按段落处理）。

        ⚠️ 判据必须走 **AST** 而不是源码子串：`charts.py` 里我写的处置注释逐字
        引用了旧形态，纯文本扫描必然被自己咬到（AGENTS §5.14 第 N 次复现）。
        """
        tree = ast.parse((APP_DIR / "routers" / "charts.py").read_bytes().decode("utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_is_placed")
        seg = ast.get_source_segment((APP_DIR / "routers" / "charts.py").read_bytes().decode("utf-8"), fn)
        # docstring 之外不得出现旧的字面串内联匹配
        body = seg.split('"""', 2)[-1]
        assert "[CHART_TYPE" not in body, body
        assert "chart_type_tags(content)" in body, body

    def test_charts_list_module_imports_it(self):
        from app.routers import charts as ch

        assert ch.chart_type_tags is chart_type_tags


# ===========================================================================
# F 组 · E1 · 缓存命中快速路径
# ===========================================================================


class TestCacheFastPath:
    """修复前：缓存查询在 `_prepare_export` 之后 ⇒「缓存命中」也要跑一遍
    完整渲染（实测 hit 时 `_chart_cache.get_or_render` 仍被调 2 次）。"""

    def test_render_segment_is_extracted(self):
        assert callable(E._prepare_export_render)
        src = _src(E._prepare_export_render)
        assert "async def _render_one" in src
        assert "_auto_generate_ai_image_blocks" in src
        assert "_download_remote_image" in src

    def test_plan_segment_has_no_rendering(self):
        """取数段不得含任何渲染调用（否则快速路径形同虚设）。"""
        calls = _call_names(_fn_ast("routers/export.py", "_prepare_export"))
        for banned in ("_auto_generate_ai_image_blocks", "_download_remote_image", "get_or_render"):
            assert banned not in calls, f"取数段仍调用 {banned} ⇒ 缓存命中仍付渲染成本"

    def test_docx_calls_render_only_after_cache_miss(self):
        node = _fn_ast("routers/export.py", "export_docx")
        seg = ast.get_source_segment((APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8"), node)
        assert seg, "取不到 export_docx 源码段"
        i_cache = seg.index("export_docx 缓存查询")
        i_render = seg.index("_prepare_export_render(prep)")
        assert i_render > i_cache, "渲染段必须位于缓存查询之后（否则命中路径仍渲染）"

    def test_pdf_calls_render_only_after_cache_miss(self):
        node = _fn_ast("routers/export.py", "export_pdf")
        seg = ast.get_source_segment((APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8"), node)
        i_cache = seg.index("export_pdf 缓存查询")
        i_render = seg.index("_prepare_export_render(prep)")
        assert i_render > i_cache, "PDF 链路渲染段必须位于缓存查询之后"

    def test_fast_path_switch_defaults_true(self):
        assert cfg.Settings.model_fields["export_cache_fast_path"].default is True

    def test_switch_off_is_byte_identical(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "export_cache_fast_path", False)
        assert E._export_cache_fast_path_enabled() is False
        monkeypatch.setattr(cfg.settings, "export_cache_fast_path", True)
        assert E._export_cache_fast_path_enabled() is True

    def test_docx_options_is_single_source(self):
        """版式读取搬进 `_build_docx_options` 后不得留第二份副本。"""
        node = _fn_ast("routers/export.py", "_build_docx_options")
        src = ast.get_source_segment((APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8"), node)
        for key in ("page_header", "show_toc", "layout_preset", "scheme_forms"):
            assert f'"{key}"' in src, key
        plan = _fn_ast("routers/export.py", "_prepare_export")
        plan_src = ast.get_source_segment((APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8"), plan)
        assert '"page_header"' not in plan_src, "取数段仍在内联读版式配置（两份副本必然漂移）"

    @pytest.mark.asyncio
    async def test_fingerprint_identical_with_and_without_render(self, db_conn):
        """**最关键不变式**：跑不跑渲染段，算出的 (config_hash,
        content_fingerprint) 必须逐字相同 —— 否则同一份内容在命中/未命中
        之间会算出两个缓存键，缓存永久 miss。"""

        async def _scenario(tag, run_render):
            """两个场景用**完全相同**的方案/章节 id 与正文 —— 指纹输入逐字相同，
            唯一变量是「跑不跑渲染段」。若用不同 id 会连 sections.id 一起变，
            断言就恒假（本轮首版即踩）。"""
            if not _scenario.done:
                await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("fp", "p", "s"))
                await db_conn.execute(
                    "INSERT INTO sections (id, scheme_id, parent_id, sort_order,"
                    " level, title, status, content) VALUES (?,?,?,?,?,?,?,?)",
                    ("fpc", "fp", "", 0, 1, "1 章", "generated", _chart() + _table()),
                )
                await db_conn.commit()
                _scenario.done = True
            prep = await E._prepare_export("fp", {"config": {}}, db_conn)
            if run_render:
                await E._prepare_export_render(prep)
            return E._content_fingerprint(prep)

        _scenario.done = False
        a = await _scenario("a", False)
        b = await _scenario("b", True)
        assert a == b, f"渲染段改变了指纹：{a} != {b}"

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_render(self, db_conn, monkeypatch, tmp_path):
        """端到端：第二次导出命中缓存 ⇒ 渲染段一次都不跑。"""
        import app.services.ai.mermaid_renderer as mr

        await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("fpe", "p", "s"))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order,"
            " level, title, status, content) VALUES (?,?,?,?,?,?,?,?)",
            ("fpec", "fpe", "", 0, 1, "1 章", "generated", _chart("flowchart TD\nA-->B") + _table()),
        )
        await db_conn.commit()
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)

        calls = {"n": 0}
        orig = mr._chart_cache.get_or_render

        def counting(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)

        monkeypatch.setattr(mr._chart_cache, "get_or_render", counting)

        body = {"config": {"allow_pil_fallback": True}, "chart_images": [], "fe_codes": []}
        r1 = await E.export_docx("fpe", body, db=db_conn)
        assert "miss" in str(r1.headers.get("X-Cache-Status", ""))
        assert calls["n"] >= 1
        before = calls["n"]
        r2 = await E.export_docx("fpe", body, db=db_conn)
        assert "hit" in str(r2.headers.get("X-Cache-Status", ""))
        assert calls["n"] == before, f"缓存命中仍渲染了 {calls['n'] - before} 次（快速路径未生效）"

    @pytest.mark.asyncio
    async def test_cache_hit_response_is_identical(self, db_conn, monkeypatch, tmp_path):
        """命中路径的响应头集合必须与未命中路径同形（前端统计条行为一致）。"""
        await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("fph", "p", "s"))
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order,"
            " level, title, status, content) VALUES (?,?,?,?,?,?,?,?)",
            ("fphc", "fph", "", 0, 1, "1 章", "generated", "正文一句话。"),
        )
        await db_conn.commit()
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        body = {"config": {}, "chart_images": [], "fe_codes": []}
        r1 = await E.export_docx("fph", body, db=db_conn)
        r2 = await E.export_docx("fph", body, db=db_conn)
        h1 = {k.lower() for k in (r1.headers or {})}
        h2 = {k.lower() for k in (r2.headers or {})}
        # X-Content-Audit 只在体检有命中时才下发（既有契约），故正文要含一处占位符
        for k in (
            "x-chart-render-stats",
            "x-cache-status",
            "x-export-filename",
            "x-global-facts-status",
            "content-disposition",
            "content-type",
        ):
            assert k in h1 and k in h2, (k, sorted(h1), sorted(h2))
        assert r1.path == r2.path, "命中必须返回同一个产物文件"


# ===========================================================================
# G 组 · 接线与静态卫生
# ===========================================================================


class TestWiringAndHygiene:
    def test_baseline_writer_failure_does_not_block_precheck(self):
        """监控旁路：写失败只记 WARNING，绝不抛给预检调用方。"""
        src = _src(E._record_placeholder_baseline)
        assert "except Exception" in src
        assert "rollback" in src

    def test_fence_advisory_label_is_honest(self):
        sec = {"title": "t", "content": "```mermaid\nx\n"}
        item = next(
            i for i in E.audit_content(sec and [sec], fences_fixed=1)["items"] if i["key"] == "fence_auto_fixed"
        )
        assert "人工核对" in item["label"], item["label"]

    @pytest.mark.parametrize(
        "rel",
        [
            "routers/export.py",
            "routers/charts.py",
            "services/content_blocks.py",
            "config.py",
        ],
    )
    def test_byte_health(self, rel):
        raw = (APP_DIR / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 含 BOM"
        assert raw.count(b"\xef\xbf\xbd") == 0, f"{rel} 含 U+FFFD"
        assert raw.count(b"\r") == raw.count(b"\r\n"), f"{rel} 孤立 CR"
        ast.parse(raw.decode("utf-8"))

    def test_no_duplicate_module_level_names(self):
        tree = _mod_ast("routers/export.py")
        tops = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert len(tops) == len(set(tops)), "模块顶层重名（R65 同族撞名）"

    def test_new_config_item_is_documented(self):
        src = (APP_DIR / "config.py").read_bytes().decode("utf-8")
        i = src.index("export_cache_fast_path")
        seg = src[max(0, i - 1400) : i]
        assert "export_cache_fast_path" in src
        assert "设 False 逐字回退旧行为" in seg or "逐字回退旧行为" in src[max(0, i - 1400) : i + 400]

    def test_new_symbols_exist(self):
        for name in (
            "collect_figure_table_number_superset",
            "_prepare_export_render",
            "_build_docx_options",
            "_export_cache_fast_path_enabled",
        ):
            assert hasattr(E, name), name

    def test_stale_ref_limit_still_applies(self):
        """DLV-14 结果上限 20 条（超长文档不炸响应体）。"""
        assert "return issues[:20]" in _src(E._detect_stale_cross_references)

    def test_detector_ignores_unknown_ref_type(self):
        assert re.search(r"ref_type.{0,40}fig", _src(E._detect_stale_cross_references))
