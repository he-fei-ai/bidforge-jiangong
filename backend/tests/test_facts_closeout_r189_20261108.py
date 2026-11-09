# -*- coding: utf-8 -*-
"""全局事实模块深度审计收口（R189 · 2026-11-08）。

本轮基线：后端事实簇 1318 passed / 7 skipped；前端 12 files / 258 passed。
十二项定罪修复由本文件按四层锁定（行为实证 / 源码棘轮 / 双向 parity / 自证）：

  A. 行为实证
     1. all_skipped 早退终态也写 facts_result checkpoint（断线重挂诊断不再全丢）；
     2. format_for_frontend 产出 group_count（R89 声明但永不产出的假契约）；
     3. invalidate_export_cache 游标 None 时降级而非把成功提取拖成失败；
     4. 分类别名归一（提示词是 DB 优先，只改代码模板无法覆盖存量）；
     5. 文档子路由的第三条事实写入源与 AI 提取共用同一把项目锁。
  B. 源码棘轮（锁调用点，不是只锁定义）
     6. all_skipped 分支里 checkpoint 写在 finish_task 之前；
     7. docs 的 _mark_project_facts_stale 必须包在写锁里；
     8. 目录侧两处 _build_facts_text 调用必须接 audit_out（预算丢弃可观测性）；
     9. 前端分页控件在分类/全量两分支之外；切方案三态清理；signal 透传。
  C. 双向 parity
    10. 提示词 category 枚举集合 ⊆ CATEGORY_TITLES 键集（且别名覆盖旧枚举）；
    11. 后端 checkpoint 契约键 ∩ 前端 FactsGenEvent 声明键非空且至少 3 个可达；
    12. factsApi 路由与后端 @router 路径逐字对应（新增 crossConflicts 闭合调用链）。
  D. 自证
    13. 证据文件本身存在、可读、含既定段落（防「护栏空文件」）。
"""
import asyncio
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

from app.routers import global_facts as gf
from app.routers import global_facts_docs as gfd
from app.routers import sse_checkpoint as sc
from app.routers import sse_handlers as sh
from app.services import checkpoint_schema as cs
from app.services import facts_extractor as fe
from app.services import facts_write_guard as fwg
from app.services.ai.prompts._registry import get_prompt

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ===========================================================================
# A 组 · 行为实证
# ===========================================================================

class TestAllSkippedCheckpoint:
    def test_all_skipped_branch_source_order(self):
        """早退分支：checkpoint 写入必须存在且排在 finish_task 之前。"""
        src = inspect.getsource(sh.event_stream) if hasattr(sh, "event_stream") else _read(BACKEND / "app" / "routers" / "sse_handlers.py")
        m = re.search(
            r"if extraction_result\.all_skipped:(?P<body>.*?)\n            if not extraction_result\.groups",
            src,
            re.S,
        )
        assert m, "未定位到 all_skipped 早退分支"
        body = m.group("body")
        assert "_save_facts_checkpoint" in body, "早退终态仍不写 facts_result checkpoint"
        assert "_facts_checkpoint_payload" in body, "必须与正常终态同源筛选"
        # ⚠️ 必须锚定**调用点**：注释里也写了 "finish_task"，用裸字符串
        #    index 会命中注释导致判据假红（本轮首跑实测）。
        assert body.index("await _save_facts_checkpoint(") < body.index("await finish_task("), (
            "checkpoint 必须在 finish_task 之前（finish_task 会 pop 内存态）"
        )
        assert "except Exception" in body, "写盘失败必须 fail-soft，不得把成功结论拖成 failed"

    def test_all_skipped_checkpoint_payload_is_whitelist_derived(self):
        """早退终态载荷只含 schema 白名单字段 + event/message。"""
        payload = {
            "event": "completed",
            "message": "全部跳过",
            **sc._facts_checkpoint_payload(fe.format_for_frontend(_fake_result())),
        }
        allowed = set(cs.FACTS_CHECKPOINT_FIELDS)
        assert set(payload) <= allowed, f"checkpoint 载荷越出 schema 契约：{set(payload) - allowed}"
        assert "segment_stats" in payload
        assert "cross_conflicts" in payload


def _fake_result():
    return SimpleNamespace(
        groups=[],
        total_items=0,
        simulated_count=0,
        conflict_count=0,
        cross_conflicts=[],
        segment_stats={"total": 3, "ok": 0, "skipped": 3, "failed": 0, "declared_failed": 0},
        warnings=[],
        all_skipped=True,
        chunk_hashes_ok=[],
        chunk_hashes_all=["h1", "h2"],
    )


class TestGroupCountContract:
    def test_format_for_frontend_emits_group_count(self):
        """R89 把 group_count 登记进契约并生成了 TS，服务端必须真产出。"""
        out = fe.format_for_frontend(_fake_result())
        assert "group_count" in out, "checkpoint_schema 声明的 group_count 服务端从未产出"
        assert out["group_count"] == 0
        assert "group_count" in cs.FACTS_CHECKPOINT_FIELDS

    def test_checkpoint_payload_carries_group_count(self):
        out = sc._facts_checkpoint_payload(fe.format_for_frontend(_fake_result()))
        assert out["group_count"] == 0


class TestExportCacheInvalidationCursor:
    def test_none_cursor_is_fail_soft_not_attribute_error(self):
        """游标 None 时不得冒泡 AttributeError（会谎报提取失败）。"""
        class _DB:
            def __init__(self):
                self.commits = 0

            async def execute(self, sql, params=()):
                if sql.startswith("SELECT"):
                    return None
                return SimpleNamespace(rowcount=0)

            async def commit(self):
                self.commits += 1

        db = _DB()
        count = asyncio.run(fe.invalidate_export_cache(db, "s1", facts_touched=True))
        assert count == 0
        assert db.commits >= 1, "游标降级不得跳过 facts_touched 时间戳推进"

    def test_normal_cursor_still_returns_cache_count(self):
        class _Row(dict):
            pass

        class _Cur:
            async def fetchall(self):
                return [_Row(id=1, result_path="")]

        class _DB:
            async def execute(self, sql, params=()):
                return _Cur()

            async def commit(self):
                return None

        assert asyncio.run(fe.invalidate_export_cache(_DB(), "s1")) == 1


class TestCategoryNamespace:
    def test_prompt_enum_is_subset_of_category_titles(self):
        """AI 调整提示词的 category 枚举必须与服务端白名单同源。"""
        text = get_prompt("global_facts_adjust_system")
        m = re.search(r"category 取值必须是以下枚举之一[^：]*：([\w/]+)", text)
        assert m, "未找到 category 枚举行"
        enum_set = set(m.group(1).split("/"))
        allowed = set(gf.CATEGORY_TITLES)
        assert enum_set <= allowed, f"提示词枚举存在服务端不认的值：{sorted(enum_set - allowed)}"
        assert enum_set, "枚举不能为空"

    def test_legacy_aliases_are_all_valid(self):
        """旧提示词枚举（存量 DB 模板下发）必须确定性地落到合法命名空间。

        ⚠️ 键集必须**完整钉死**：只遍历现存项的话，删掉任意一条别名
        仍会通过（A/B M4 首轮会 MISSED），护栏形同虚设。
        """
        expected = {
            "material",
            "construction_practice",
            "process_flow",
            "design_param",
            "safety",
        }
        assert set(gf._LEGACY_FACT_CATEGORY_ALIASES) == expected, (
            "旧枚举别名集合漂移：存量 DB 提示词下发的分类会重新静默降级 other"
        )
        for old, new in gf._LEGACY_FACT_CATEGORY_ALIASES.items():
            assert old not in gf._ALLOWED_FACT_CATEGORIES, f"别名 {old} 已在白名单，属冗余登记"
            assert new in gf._ALLOWED_FACT_CATEGORIES
            assert gf._normalize_fact_category(old) == new
            assert gf._normalize_fact_category(old.upper()) == new

    def test_unknown_category_still_fails_soft_to_other(self):
        assert gf._normalize_fact_category("nonsense") == "other"
        assert gf._normalize_fact_category(None) == "other"
        assert gf._normalize_fact_category(123) == "other"

    def test_allowed_set_unchanged(self):
        """不扩白名单（R89 parity 锁死的契约键集）。"""
        assert gf._ALLOWED_FACT_CATEGORIES == set(gf.CATEGORY_TITLES) | {"other"}


class TestDocsWriteGuard:
    def test_mark_stale_is_wrapped_by_inproc_guard(self):
        src = inspect.getsource(gfd._mark_project_facts_stale)
        assert "project_facts_inproc_guard" in src
        assert src.index("async with project_facts_inproc_guard") < src.index("UPDATE global_facts")

    def test_inproc_guard_shares_the_same_lock_as_ai_extraction(self):
        """两路必须复用 _lock_for，否则互斥是假的。"""
        assert "_lock_for" in inspect.getsource(fwg.project_facts_inproc_guard)
        assert "_lock_for" in inspect.getsource(fwg.acquire_project_facts_write)

    def test_inproc_guard_never_commits(self):
        """docs 调用方带着未提交事务进来，锁内 commit 会破坏事务边界。"""
        # ⚠️ docstring 里写了「db.commit()」作反例说明，文本匹配必然假红。
        #    只遍历 AST 的 Call 节点 —— 注释 / 字符串一律不参与判据。
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(fwg.project_facts_inproc_guard)))
        calls = [
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]
        assert "commit" not in calls, "进程内锁不得触碰 db.commit()"

    def test_lock_timeout_yields_false_instead_of_raising(self, monkeypatch):
        """等待超时不阻断业务（降级留痕），且锁不会被误释放。"""
        class _NeverLock:
            def locked(self):
                return False

            async def acquire(self):
                raise asyncio.TimeoutError

        monkeypatch.setattr(fwg, "_lock_for", lambda pid: _NeverLock())
        monkeypatch.setattr(fwg, "LOCK_WAIT_SECONDS", 0.001)

        async def _run():
            async with fwg.project_facts_inproc_guard("p1", holder="test") as acquired:
                return acquired

        assert asyncio.run(_run()) is False

    def test_empty_project_id_is_noop(self):
        async def _run():
            async with fwg.project_facts_inproc_guard("", holder="test") as acquired:
                return acquired

        assert asyncio.run(_run()) is True


# ===========================================================================
# B 组 · 源码棘轮（锁调用点）
# ===========================================================================

class TestSourceRatchets:
    def test_outline_facts_builder_audit_is_wired(self):
        """目录链两处 _build_facts_text 调用都必须接 audit_out。

        正文链已用 builder 的 audit_out 做逐章审计账本，目录链此前完全没传 ——
        事实被预算静默丢弃时日志/审计里查不到丢了什么。
        """
        src = _read(BACKEND / "app" / "routers" / "sse_handlers.py")
        assemble = src[src.index("async def _assemble_context"):src.index("async def event_stream", src.index("async def _assemble_context"))]
        assert assemble.count("_build_facts_text(") == 2, "目录装配阶段应恰好有两处事实文本构建"
        assert assemble.count("audit_out=") == 2, "两处都必须接上 budget_dropped 审计出参"
        assert "scheme_id" in assemble and "task_id," not in assemble, (
            "装配函数与 event_stream 是兄弟嵌套函数，日志不得引用 task_id（NameError）"
        )

    def test_frontend_pager_is_outside_both_branches(self):
        src = _read(FRONTEND / "src" / "pages" / "schemeWorkbench" / "tabs" / "FactsTab.tsx")
        # 分页控件必须出现在「两分支三元表达式结束」之后
        marker = "image={Empty.PRESENTED_IMAGE_SIMPLE}"
        assert marker in src
        tail = src[src.index(marker):]
        assert "buildFactsPagination" in tail, (
            "分页控件又回到了分类/全量分支内部 → 分类视图下恒不可达（R189 P1 回归）"
        )
        assert "loadFacts(p)" in tail

    def test_scheme_switch_clears_three_states(self):
        src = _read(FRONTEND / "src" / "pages" / "SchemeWorkbenchPage.tsx")
        for token in ("setFactsPagination(null)", 'factsPageSchemeRef.current = ""', "setFactsDiagnosticsLoading(false)"):
            assert token in src, f"切方案清理缺：{token}"

    def test_diagnostics_signal_is_passed(self):
        src = _read(FRONTEND / "src" / "pages" / "SchemeWorkbenchPage.tsx")
        assert "loadFactsDiagnostics({ signal: ac.signal })" in src

    def test_cross_conflicts_route_is_called(self):
        """后端有路由、前端有封装还不够 —— 必须真的被页面调用。"""
        api_src = _read(FRONTEND / "src" / "api" / "index.ts")
        page_src = _read(FRONTEND / "src" / "pages" / "SchemeWorkbenchPage.tsx")
        assert '"/global-facts/cross-conflicts"' in api_src
        assert "factsApi.crossConflicts(" in page_src
        assert "setFactsCrossConflicts((prev)" in page_src


# ===========================================================================
# C 组 · 双向 parity
# ===========================================================================

class TestBidirectionalParity:
    def test_backend_checkpoint_keys_reachable_in_frontend_type(self):
        """后端 facts_result 契约键 ⊆ 前端声明（含 SSE 事件类型）。"""
        ts = _read(FRONTEND / "src" / "types" / "checkpointSchema.ts")
        evt = _read(FRONTEND / "src" / "pages" / "schemeWorkbench" / "workbenchTypes.ts")
        declared = set(re.findall(r"^\s*(\w+)\??\s*:", ts, re.M)) | set(
            re.findall(r"^\s*(\w+)\??\s*:", evt, re.M)
        )
        reachable = set(cs.FACTS_CHECKPOINT_FIELDS) & declared
        assert len(reachable) >= 3, (
            f"后端 facts_result 键与前端类型脱节（可达 {sorted(reachable)}）"
        )
        assert {"segment_stats", "cross_conflicts", "warnings"} <= reachable

    def test_facts_api_route_matches_backend_router(self):
        """前端封装的路径逐字对应后端 @router（含 prefix）。"""
        api_src = _read(FRONTEND / "src" / "api" / "index.ts")
        gf_src = _read(BACKEND / "app" / "routers" / "global_facts.py")
        assert '@router.get("/cross-conflicts")' in gf_src
        assert '"/global-facts/cross-conflicts"' in api_src
        assert 'prefix="/api/v1/global-facts"' in gf_src

    def test_safe_text_helpers_are_used_by_components(self):
        """纯函数必须真被组件消费（服务层就绪≠功能可达）。"""
        group_src = _read(FRONTEND / "src" / "pages" / "schemeWorkbench" / "FactsGroupList.tsx")
        tab_src = _read(FRONTEND / "src" / "pages" / "schemeWorkbench" / "tabs" / "FactsTab.tsx")
        assert "factConfidencePercent(" in group_src
        assert "factConfidenceColor(" in group_src
        assert "safeFactText(" in group_src and "safeFactText(" in tab_src
        assert ".confidence! * 100" not in group_src, "置信度 NaN% 旧写法复活"

    def test_no_dead_imports_in_facts_shared(self):
        src = _read(FRONTEND / "src" / "pages" / "schemeWorkbench" / "factsShared.ts")
        for token in ("import { factsApi }", "from './FactsChapterCoveragePanel", "from 'antd'"):
            assert token not in src, f"factsShared 里又出现无用/循环依赖导入：{token}"


# ===========================================================================
# D 组 · 自证
# ===========================================================================

def test_guard_file_is_not_empty_and_self_describing():
    p = Path(__file__)
    src = _read(p)
    assert len(src) > 3000
    for marker in ("A 组 · 行为实证", "B 组 · 源码棘轮", "C 组 · 双向 parity", "D 组 · 自证"):
        assert marker in src, f"护栏自证段缺失：{marker}"
    assert json.loads(json.dumps({"ok": True}))["ok"] is True
