"""R188（2026-11-08）目录生成模块遗留项收口 · 护栏集。

承接 R187 报告 §七 的 14 项登记。三路只读亲验后逐条定罪，处置分两类：

【落地修复 10 项】
  L2  快照回退加法式结构编号自检 / 目录库 restore-version 补规范化
  L3  方案复制 renumber 失败时同源校验，脏副本回滚 409
  L4  _build_tree 环内不可达节点可观测 + 严格模式导出判红
  L7  EMA 窗口跨度真实化 + stats 快照读 _prog 真实值
  L8  停止态 checkpoint 写失败保留成果 + 二次兜底 + checkpoint_failed
  L10 编号回滚注入图表重挂回调
  L11 L1 瞬态错误单次重试 + 裸 issues 数组容忍
  L12 7 处 getattr fallback 反向修正 + boost 死配置注释诚实化
  L13 start_all.bat 数据目录断链自检
  L14 upload_outline 恢复 clip_text_logged 截断留痕
  L9  前端 UI 可达化（snapshot_id / roots_locked / checkpoint_failed）

【证伪闭合 + 双向棘轮 4 项】
  L1  严格模式守卫已前置缓存、交付物无实害 → 锁顺序 + 锁「指纹不含 outline_json」
  L5  同型抢占是显式设计且已有 409/前端 disabled → 三锁固化当前态
  L6  逐章断点续跑属大工程 → 锁 checkpoint 白名单当前态 + 方案登记棘轮
  L9  双向 parity：前端消费键 ⊆ 后端产出键，且至少 3 键可达

护栏三层：行为实证（含 fake db / monkeypatch）/ 源码 AST·文本棘轮 / 非空转自证。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

# ✅ 导入顺序纪律：_sse_outline_build / _sse_outline_review 模块顶部会反向
#    import sse_handlers facade。若以它们为导入起点，sse_handlers:666 反过来
#    from _sse_outline_build import _OUTLINE_CALIBRATE_MIN_SAMPLE 会撞上
#    「部分初始化模块」→ ImportError（首跑实测）。先显式完整加载 facade，
#    再按序取各模块，行为与生产启动链一致。
importlib.import_module("app.routers.sse_handlers")
ob = importlib.import_module("app.routers._sse_outline_build")
rev = importlib.import_module("app.routers._sse_outline_review")
sec = importlib.import_module("app.routers.sections")
ou = importlib.import_module("app.services.outline_utils")

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
FRONTEND = ROOT / "frontend" / "src"


def _src(rel: str) -> str:
    return (BACKEND / "app" / rel).read_text(encoding="utf-8")


def _front(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


class _FakeCursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _FakeDB:
    """只回应 R188 L4 新增的「全表 id」查询，其余交给 monkeypatch 的 _build_tree。"""

    def __init__(self, all_ids: list[str]):
        self.all_ids = all_ids

    async def execute(self, sql, params=()):
        if "SELECT id FROM sections" in sql:
            return _FakeCursor([{"id": i} for i in self.all_ids])
        return _FakeCursor([])


def _node(nid: str, stored_id: str, level: int = 1, children=None) -> dict:
    return {
        "id": nid,
        "level": level,
        "sort_order": 0,
        "title": f"章节{nid}",
        "outline_json": json.dumps({"id": stored_id, "level": level}),
        "children": children or [],
    }


# ---------------------------------------------------------------- A · L4
class TestUnmountedCycleNodes:
    @pytest.mark.asyncio
    async def test_cycle_nodes_are_reported(self, monkeypatch):
        """环内节点被 _build_tree 丢弃 → 报告必须给出 unmounted，不再假绿隐形。"""
        tree = [_node("r1", "1")]
        monkeypatch.setattr(sec, "_build_tree", lambda db, sid: _async_return(tree))
        report = await sec._outline_structure_report(_FakeDB(["r1", "a", "b"]), "S")
        assert report["unmounted_count"] == 2
        assert set(report["unmounted"]) == {"a", "b"}
        # consistent 语义刻意不变（保既有数值断言），由严格模式单独判红
        assert isinstance(report["consistent"], bool)

    @pytest.mark.asyncio
    async def test_self_proof_clean_tree_has_zero_unmounted(self, monkeypatch):
        """非空转自证：干净树必须 0，否则上面那条护栏恒红、形同虚设。"""
        tree = [_node("r1", "1")]
        monkeypatch.setattr(sec, "_build_tree", lambda db, sid: _async_return(tree))
        report = await sec._outline_structure_report(_FakeDB(["r1"]), "S")
        assert report["unmounted_count"] == 0
        assert report["unmounted"] == []

    def test_strict_export_guard_source_ratchet(self):
        cache = _src("routers/_export_cache.py")
        assert "_unmounted_bad = bool(_outline_detail.get(\"unmounted_count\"))" in cache
        assert '"unmounted_count": int(_outline.get("unmounted_count") or 0)' in cache
        assert "导出会缺章" in cache
        assert "_body_bad or _outline_bad or _unmounted_bad" in cache


async def _async_return(value):
    return value


# ---------------------------------------------------------------- B · L3
class TestDuplicateSchemeFailClosed:
    @staticmethod
    def _fn() -> str:
        src = _src("routers/schemes.py")
        return src.split("async def duplicate_scheme(", 1)[1].split("\nasync def ", 1)[0]

    def test_source_ratchet(self):
        fn = self._fn()
        assert "_renumber_failed = None" in fn
        assert "_outline_structure_report(db, new_id)" in fn
        assert "await db.rollback()" in fn
        assert 'HTTPException(409, "副本章节编号校验未通过，复制已回滚，请重试")' in fn
        # R85/R86 既有 AST 锁：重排调用必须仍在 commit 之前（判据限本函数体）
        assert fn.index("renumber_sections_after_reorder(db, new_id)") < fn.index("await db.commit()")

    def test_success_path_has_no_extra_query(self):
        """正常路径（renumber 成功）零额外自检查询：校验只在失败分支内。"""
        fn = self._fn()
        assert fn.index("if _renumber_failed:") < fn.index("_outline_structure_report(db, new_id)")


# ---------------------------------------------------------------- C · L7
class TestEmaAndStats:
    def test_stats_snapshot_reads_real_prog_values(self):
        st = {
            "started_at": 0.0, "sub_total": 10, "sub_done": 3,
            "last_p": 0.4, "phase": "sublevels", "nodes": 6,
            "concurrency": 4, "running": [{"index": 2, "title": "施工部署"}],
        }
        snap = ob._snapshot_outline_stats(st)
        assert snap["concurrency"] == 4
        assert snap["running"] == [{"index": 2, "title": "施工部署"}]
        assert snap["words"] == 0  # 目录链无对应量，缺省 0 是正确语义

    def test_ema_uses_real_window_span(self):
        sse = _src("routers/sse_handlers.py")
        assert "time.monotonic() - _cur_chapter_at" in sse
        assert '_cur_chapter_at - _prog.get("chapter_started_at", 0.0)' not in sse

    def test_concurrency_and_running_wired(self):
        sse = _src("routers/sse_handlers.py")
        assert '_prog["concurrency"] = _u5_outline_conc' in sse
        assert '_prog["running"] = [' in sse
        assert '_prog["running"] = []' in sse
        build = _src("routers/_sse_outline_build.py")
        assert '"concurrency": st.get("concurrency", 0)' in build
        assert '"running": list(st.get("running") or [])' in build


# ---------------------------------------------------------------- D · L8
class TestStoppedCheckpointGuard:
    @pytest.mark.asyncio
    async def test_first_write_fails_then_retry_succeeds(self, monkeypatch):
        calls: list[str] = []

        async def _fake_save(task_id, payload, *_fs_args, **_fs_kwargs):
            calls.append(task_id)
            if len(calls) == 1:
                raise RuntimeError("database is locked")

        async def _fake_finish(task_id, status, message):
            calls.append(f"finish:{status}")

        monkeypatch.setattr(rev._sh, "_save_outline_checkpoint", _fake_save)
        monkeypatch.setattr(rev._sh, "finish_task", _fake_finish)
        holder = {"outline": [{"title": "A"}], "failed_chapters": []}
        line = await rev._finish_stopped_with_partial("t1", holder)
        assert calls.count("t1") == 2, "第二次幂等兜底必须发生"
        assert "finish:stopped" in calls
        assert holder["outline"] == []
        assert "checkpoint_failed" not in line

    @pytest.mark.asyncio
    async def test_both_writes_fail_keep_outline_and_mark_payload(self, monkeypatch):
        async def _fake_save(task_id, payload, *_fs_args, **_fs_kwargs):
            raise RuntimeError("disk full")

        async def _fake_finish(task_id, status, message):
            return None

        monkeypatch.setattr(rev._sh, "_save_outline_checkpoint", _fake_save)
        monkeypatch.setattr(rev._sh, "finish_task", _fake_finish)
        holder = {"outline": [{"title": "A"}], "failed_chapters": ["B"]}
        line = await rev._finish_stopped_with_partial("t2", holder)
        assert '"checkpoint_failed": true' in line
        assert holder["outline"], "写失败必须保留 holder，不能无条件下掉成果"
        assert '"outline": [{"title": "A"}]' in line, "payload 自带成果，前端可直接保存"

    @pytest.mark.asyncio
    async def test_self_proof_success_writes_once(self, monkeypatch):
        calls: list[int] = []

        async def _fake_save(task_id, payload, *_fs_args, **_fs_kwargs):
            calls.append(1)

        async def _fake_finish(task_id, status, message):
            return None

        monkeypatch.setattr(rev._sh, "_save_outline_checkpoint", _fake_save)
        monkeypatch.setattr(rev._sh, "finish_task", _fake_finish)
        holder = {"outline": [{"title": "A"}], "failed_chapters": []}
        await rev._finish_stopped_with_partial("t3", holder)
        assert len(calls) == 1, "成功路径不得多写一次"


# ---------------------------------------------------------------- E · L11
class TestAiChainHardening:
    def test_l1_transient_retry_source_ratchet(self):
        sse = _src("routers/sse_handlers.py")
        assert "async def _l1_call():" in sse
        # ✅ R190 重锚：瞬态类别扩到 ValueError（JSON 修复链耗尽），且重试前
        #    复检暂停/停止闸门（wait_resume + is_stopped），其余意图不变。
        assert "except (asyncio.TimeoutError, RuntimeError, ValueError) as _l1_err:" in sse
        assert "if _is_quota_error(_l1_err):" in sse
        assert "完整提示词重试一次" in sse
        assert "if is_stopped(task_id):" in sse

    def test_bare_issues_array_tolerated(self):
        review = _src("routers/_sse_outline_review.py")
        assert "if isinstance(review_obj, list):" in review
        assert "_coerce_suggestions(review_obj)" in review
        assert '"skip_reason": "bare_issues_array"' in review

    def test_coerce_suggestions_accepts_string_list(self):
        assert rev._sh._coerce_suggestions(["补充监测", "补验收"]) == ["补充监测", "补验收"]


# ---------------------------------------------------------------- F · L12
class TestConfigDrift:
    def test_no_reversed_getattr_defaults(self):
        for rel in (
            "routers/_sse_outline_build.py",
            "routers/_sse_outline_review.py",
            "services/facts_builder.py",
        ):
            src = _src(rel)
            for key in ("outline_basis_relevance", "outline_name_basis", "outline_standards_inject"):
                assert f'"{key}", False' not in src, f"{rel} 仍有反向 fallback: {key}"

    def test_all_three_keys_have_true_fallback(self):
        build = _src("routers/_sse_outline_build.py")
        review = _src("routers/_sse_outline_review.py")
        facts = _src("services/facts_builder.py")
        assert '"outline_basis_relevance", True' in build
        assert '"outline_basis_relevance", True' in facts
        assert review.count('"outline_name_basis", True') == 4
        assert '"outline_standards_inject", True' in review

    def test_boost_double_parity_ratchet(self):
        """双向棘轮：定义必须在场 + 读取点必须为零（当前是预留字段）。

        用 AST 只认真实读取（Attribute / getattr），docstring 里的历史说明不算。
        """
        import ast

        config = _src("config.py")
        assert "outline_relevance_boost: float = 2.0" in config
        assert config.count("预留") >= 2  # 注释与字段说明两处诚实化标记
        readers = []
        for p in (BACKEND / "app").rglob("*.py"):
            if p.name == "config.py":
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                hit = isinstance(n, ast.Attribute) and n.attr == "outline_relevance_boost"
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr":
                    hit = hit or (
                        len(n.args) >= 2
                        and isinstance(n.args[1], ast.Constant)
                        and n.args[1].value == "outline_relevance_boost"
                    )
                if hit:
                    readers.append(str(p.relative_to(BACKEND)))
                    break
        assert not readers, f"预留字段出现读取点，须同步更新注释/护栏: {readers}"


# ---------------------------------------------------------------- G · L14
class TestTruncationTrail:
    """L14：R181 配置化裸切 + R188 显式日志留痕。

    ⚠️ 刻意不走 clip_text_logged：R186 护栏
    ``test_prompt_truncation_ratchet_20261107.py::_LOGGED_ONLY_IN`` 限定它只用于
    「只喂正则/关键词判定、插标记会改变匹配结果」的内部串；本处文本进模型请求，
    语义不符。也不插 clip_text_notice 标记（会改 AI 输入与 govern_blob 预算）。
    """

    def test_upload_outline_logs_truncation_and_keeps_config_limit(self):
        src = _src("routers/upload_outline.py")
        assert "settings.outline_ai_fallback_max_chars" in src  # R181 配置化护栏
        assert "raw_text[: settings.outline_ai_fallback_max_chars]" in src
        assert "上传目录识别 AI 兜底正文截断（仅日志）" in src
        assert "if len(raw_text) > settings.outline_ai_fallback_max_chars:" in src
        # 仅断言没有「实际调用」（注释里的引用说明是合规留痕的一部分）
        assert "clip_text_logged(" not in src
        assert "clip_text_notice(" not in src


# ---------------------------------------------------------------- H · L2/L10/L13
class TestRestoreRollbackStartup:
    def test_snapshot_restore_reports_numbering(self):
        src = _src("routers/sections.py")
        assert 'result["numbering_report"]' in src
        assert "目录快照回退后结构编号不一致" in src
        assert "自检失败不得阻断回退" in src

    def test_library_restore_version_normalizes(self):
        # ✅ R214 重锚（意图保留、强度加强）：R188 L2 锁的是「写回主表前必须
        #    规范化」，旧断言把「用哪个函数」也锁死了（锁实现）。R214 把三条
        #    版本路径收口到统一出口 `_normalize_for_write`（内含标题清洗 +
        #    三级裁剪 + 编号重排 + 校验），并补上此前缺失的空目录 400 ⇒
        #    照旧断言反而会阻止加固。现锁「统一出口 + 空目录拒绝 + 结果落库」。
        src = _src("routers/outline_library.py")
        assert "_restored_json = _normalize_for_write(" in src
        assert 'context="restore_version"' in src
        assert 'raise HTTPException(400, "历史版本目录内容为空或格式非法，无法回滚")' in src
        assert "_restored_json," in src
        assert "normalize_outline_for_library" in src  # 统一出口的唯一实现

    def test_numbering_rollback_resync_wired(self):
        assert "resync_fn=None" in _src("services/numbering.py")
        assert "resync_fn=resync_fn" in _src("services/numbering.py")
        sse = _src("routers/sections.py")
        assert "async def _resync_rollback_charts(db_, sid, content):" in sse
        assert "resync_fn=_resync_rollback_charts" in sse

    def test_startup_junction_health_check(self):
        raw = (ROOT / "start_all.bat").read_bytes()
        assert b"R188: data junction health check" in raw
        assert b"sys.exit(0 if pathlib.Path(sys.argv[1]).is_dir() else 1)" in raw
        # 只提示、不自动改链接（R187 教训 1）：mklink 只出现在 echo 行里一次
        assert raw.count(b"mklink /J") == 1
        assert b"echo     Rebuild it manually: mklink /J" in raw


# ------------------------------------------------- I · L1/L5/L6/L9 双向棘轮
class TestDesignRatchets:
    def test_l1_guard_precedes_cache_lookup(self):
        """L1 证伪闭合：严格模式守卫在缓存查询之前，命中不会跳过自检。"""
        export = _src("routers/export.py")
        lines = export.splitlines()
        guard_idx = [i for i, l in enumerate(lines) if "_guard_numbering_consistency(db, scheme_id)" in l]
        cache_idx = [i for i, l in enumerate(lines) if "cached and cached[0]" in l]
        assert len(guard_idx) == 2 and len(cache_idx) == 2
        assert guard_idx[0] < cache_idx[0] and guard_idx[1] < cache_idx[1]

    def test_l1_fingerprint_excludes_outline_json(self):
        """双向锁：指纹不含 outline_json，且导出查询也不 SELECT 它。"""
        cache = _src("routers/_export_cache.py")
        seg = cache.split('"sections": [', 1)[1].split("],", 1)[0]
        assert "outline_json" not in seg
        export = _src("routers/export.py")
        assert "SELECT id, parent_id, sort_order, level, title, content" in export

    def test_l5_preemption_design_is_fixed(self):
        """L5 产品决策态三锁：后端抢占 + 路由 409 + 前端禁用。"""
        registry = _src("services/ai/task_registry.py")
        assert "if stale:" in registry
        assert 'request_control(old_id, "stop")' in registry
        assert "请勿重复触发" in _src("routers/sse_handlers.py")
        outline_tab = _front("pages/schemeWorkbench/tabs/OutlineTab.tsx")
        # ✅ R231 重锚（2026-11-13 · 意图保留、强度不减）：omp 目录生成旁路引入后，
        #    前端写入口禁用判据从单一生成态扩展为 outlineBusy（主链路 generating
        #    与 omp 旁路 generatingOmp 两态都禁用）——原字面量已被
        #    ompOutlineEntry20261112.test.ts 反向禁止（not.toContain），本断言
        #    同步锁新契约：outlineBusy 定义在位、≥5 处按钮接线、旧单态字面量
        #    不得回流。
        assert "const outlineBusy = generating || !!generatingOmp" in outline_tab
        assert outline_tab.count("disabled={outlineBusy") >= 5
        assert "disabled={generating}" not in outline_tab

    def test_l6_checkpoint_schema_has_no_chapter_set(self):
        """L6 当前态锁：白名单不含章节集合；方案登记在 R188 报告 §五。"""
        schema = _src("services/checkpoint_schema.py")
        assert '"outline_generation"' in schema
        assert "done_titles" not in schema and "done_indexes" not in schema
        report = (ROOT / "docs" / "目录生成模块遗留收口_R188_20261108.md").read_text(encoding="utf-8")
        assert "L6" in report and "断点续跑" in report

    def test_l9_frontend_consumes_backend_additive_keys(self):
        """双向 parity：前端消费键 ⊆ 后端产出键，且至少 3 键可达。"""
        backend_keys = {
            "roots_locked": '"roots_locked"' in _src("routers/sections.py"),
            "snapshot_id": '"snapshot_id"' in _src("routers/sections.py"),
            "checkpoint_failed": '"checkpoint_failed"' in _src("routers/sse_handlers.py"),
            "instruction_truncated": '"instruction_truncated"' in _src("routers/sections.py"),
        }
        gen = _front("pages/schemeWorkbench/useOutlineGeneration.tsx")
        consumed = {
            "roots_locked": "data?.roots_locked" in _front("pages/SchemeWorkbenchPage.tsx"),
            "snapshot_id": "data?.snapshot_id" in _front("pages/SchemeWorkbenchPage.tsx"),
            "checkpoint_failed": gen.count("evt.checkpoint_failed") >= 2,  # completed + stopped 双分支
        }
        assert all(backend_keys.values()), backend_keys
        assert sum(consumed.values()) >= 3, consumed
        assert all(consumed[k] <= backend_keys[k] for k in consumed)
        assert "checkpoint_failed?: boolean" in _front("pages/schemeWorkbench/workbenchTypes.ts")
