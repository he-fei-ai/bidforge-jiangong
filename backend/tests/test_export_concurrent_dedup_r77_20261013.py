"""P2 收口（2026-10-13）· 同「方案 + 格式 + 配置」并发导出的重复成本。"""
import ast
import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import pytest

import app.config as cfg
import app.db as _appdb
import app.routers.export as export_mod
from app.db import close_db, get_conn, init_db



@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    """建真实 scheme + 章节；桩掉 _prepare_export 与 DOCX 构建（零真实渲染）。

    构建桩通过模块属性 ``_build_docx_sync`` 注入：默认写固定字节；测试可替换
    以模拟构建失败（用于验证异常时锁已释放）。
    """
    _appdb.DB_PATH = tmp_path / "export_dedup.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.execute(
        "INSERT INTO sections(id,scheme_id,level,title,sort_order) VALUES(?,?,1,?,1)",
        (uuid.uuid4().hex, sid, "Overview"))

    calls = []

    async def fake_prep(sid_arg, body, db_arg):
        calls.append(body["_tag"])
        return {
            "config": body.get("config", {}) or {},
            "scheme": {"id": sid_arg, "name": "s", "type": "", "project_id": pid},
            "sections": [{"id": "s1", "title": "Overview", "level": 1,
                          "content": "x", "sort_order": 1}],
            "charts": [], "render_stats": {}, "fe_codes": [],
            "chart_fp": [], "fe_images": {}, "rendered_charts": {},
            "ai_image_pending": 0, "body": body,
        }

    def fake_build(*args, **kwargs):
        """DOCX 构建桩：写入固定字节（真实构建走线程池，此处同步写）。"""
        Path(str(args[0])).write_bytes(b"PK\x03\x04" + b"x" * 12)
        return {"sections": 1, "tables": 0, "figures": 0}

    monkeypatch.setattr(export_mod, "_prepare_export", fake_prep)
    monkeypatch.setattr(export_mod, "_build_docx_sync", fake_build)
    # 不跑真实 DOCX 构建：把构建任务收敛为 (out_path,)，交给上面的桩
    monkeypatch.setattr(export_mod, "_build_docx_task", lambda out, prep: (out,))
    monkeypatch.setattr(export_mod, "EXPORTS_DIR", tmp_path)
    try:
        yield db, sid, calls, tmp_path
    finally:
        await close_db()


async def _count_cache(db, sid):
    cur = await db.execute("SELECT COUNT(*) FROM export_cache WHERE scheme_id=?", (sid,))
    row = await cur.fetchone() if cur is not None else None
    return int(row[0]) if row and row[0] is not None else 0


def _minimal_prep(config: dict) -> dict:
    """满足 ``_content_fingerprint`` 直取键的最小 prep 构造。

    指纹函数对 config/fe_codes/sections/chart_fp 直接用下标取值（其余用 .get），
    故真实全流程会携带这些键；纯函数 parity 用例必须同样带齐，
    避免「锁键 == 指纹 config_hash」的对账因 KeyError 而无法执行。
    """
    return {
        "config": config or {},
        "fe_codes": [],
        "fe_images": {},
        "sections": [],
        "chart_fp": [],
        "scheme": {"id": "s", "name": "s", "project_id": "p"},
    }


class TestLockKeyParity:
    """锁键的 config_hash 必须与缓存判据的 config_hash **同值**。

    锁只串行同键请求；若锁键与缓存键口径分叉，会出现
    「串行了彼此无法复用缓存的请求」或「不同键请求共享一把锁」两种错配。
    """

    def test_equal_to_content_fingerprint_config_hash(self):
        body = {"config": {"line_spacing": 1.5, "margins": {"top": 2.5}}}
        assert (export_mod._export_config_hash(body)
                == export_mod._content_fingerprint(
                    _minimal_prep(body["config"]))[0])

    def test_independent_of_content_dimensions(self):
        cfg0 = {"line_spacing": 1.5}
        a = export_mod._export_config_hash({"config": cfg0})
        b = export_mod._export_config_hash({"config": cfg0})
        assert a == b

    def test_different_config_gives_different_key(self):
        a = export_mod._export_config_hash({"config": {"line_spacing": 1.5}})
        b = export_mod._export_config_hash({"config": {"line_spacing": 1.15}})
        assert a != b

    def test_normalizes_the_same_ways_as_the_fingerprint(self):
        noisy = {"config": {"_ts": 1, "line_spacing": 1.5, "empty": "",
                            "unknown": "z"}}
        body = {"config": noisy}
        assert (export_mod._export_config_hash(body)
                == export_mod._content_fingerprint(
                    _minimal_prep(export_mod._normalize_config(noisy)))[0])

    def test_missing_config_is_stable(self):
        a = export_mod._export_config_hash({})
        b = export_mod._export_config_hash(None)
        assert a == b == hashlib.md5(b"{}").hexdigest()

    def test_hash_is_not_a_side_effectful_path(self):
        body = {"config": {"line_spacing": 1.5}}
        first = export_mod._export_config_hash(body)
        assert body == {"config": {"line_spacing": 1.5}}




EXPORT_PY = Path(export_mod.__file__)
CONFIG_PY = Path(cfg.__file__)
DOCX_DEC = '@router.post("/docx")'
PDF_DEC = '@router.post("/pdf")'



class TestConcurrentDedupEndToEnd:
    """同键并发导出只构建一次；配置不同仍并行；关闭开关回退旧行为。"""

    async def test_same_key_concurrent_exports_build_once(self, ctx):
        db, sid, calls, _ = ctx
        body = {"config": {"line_spacing": 1.5}, "_tag": "same"}
        r1, r2 = await asyncio.gather(
            export_mod.export_docx(sid, dict(body, _tag="a"), db=db),
            export_mod.export_docx(sid, dict(body, _tag="b"), db=db),
            return_exceptions=True)
        assert not isinstance(r1, Exception) and not isinstance(r2, Exception)
        # 核心断言：两个请求都进入过准备阶段（串行化，不是丢弃），且只落一行缓存
        assert len(calls) == 2 and sorted(calls) == ["a", "b"]
        assert await _count_cache(db, sid) == 1

    async def test_second_export_hits_cache_without_rebuilding(self, ctx):
        db, sid, calls, _ = ctx
        body = {"config": {}, "_tag": "solo"}
        await export_mod.export_docx(sid, dict(body, _tag="a"), db=db)
        calls.clear()
        resp = await export_mod.export_docx(sid, dict(body, _tag="b"), db=db)
        assert "hit" in str(resp.headers.get("X-Cache-Status", ""))
        assert calls == ["b"]  # 缓存查询在 prepare 之后，仍各付一次准备

    async def test_different_config_not_serialized(self, ctx):
        db, sid, calls, _ = ctx
        started, both_in = asyncio.Event(), asyncio.Event()

        async def slow_prep(sid_arg, body, db_arg):
            calls.append(body["_tag"])
            started.set()
            await both_in.wait()  # 两个请求都进了准备阶段才算「并行」
            return {
                "config": body.get("config", {}) or {},
                "scheme": {"id": sid_arg, "name": "s", "type": "",
                           "project_id": ""},
                "sections": [], "charts": [], "render_stats": {},
                "fe_codes": [], "chart_fp": [], "fe_images": {},
                "rendered_charts": {}, "body": body,
            }

        export_mod._prepare_export = slow_prep
        tasks = [asyncio.create_task(export_mod.export_docx(
            sid, {"config": {"line_spacing": 1.5}, "_tag": "a"}, db=db)),
            asyncio.create_task(export_mod.export_docx(
            sid, {"config": {"line_spacing": 1.15}, "_tag": "b"}, db=db))]
        # 两个任务都曾让出控制权；等双方都进入 slow_prep 再断言（防调度竞态）
        # ✅ 2026-10-14 加固：原实现固定 200 次 `sleep(0)` 后立即断言。编号一致性
        #    守卫在 db 连接失效时会走**同步** SQLite 回退查询（numbering.py），
        #    同步段不向事件循环让出，200 次 `sleep(0)` 的让出额度会被两个 task 的
        #    同步段耗尽，calls 仍为 0 而**误报**「不同配置的导出被同一把锁串行」
        #    （实测约 1/4 概率误失败，与本用例要证明的锁语义无关）。改为带真实
        #    间隔的上限超时等待：等到双方进入 prep 或真超时，断言语义完全不变。
        _loop = asyncio.get_running_loop()
        _deadline = _loop.time() + 5.0
        while len(calls) < 2 and _loop.time() < _deadline:
            await asyncio.sleep(0.005)
        assert len(calls) == 2, "不同配置的导出不应被同一把锁串行"
        both_in.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(not isinstance(r, Exception) for r in results)
        assert await _count_cache(db, sid) == 2  # 两个不同配置各一行缓存

    async def test_disabled_switch_rebuilds_every_time(self, ctx, monkeypatch):
        db, sid, calls, _ = ctx
        monkeypatch.setattr(cfg.settings, "export_dedup_concurrent", False)
        body = {"config": {}, "_tag": "off"}
        r1, r2 = await asyncio.gather(
            export_mod.export_docx(sid, dict(body, _tag="a"), db=db),
            export_mod.export_docx(sid, dict(body, _tag="b"), db=db),
            return_exceptions=True)
        assert all(not isinstance(r, Exception) for r in (r1, r2))
        # 旧行为：两个请求各自完整跑一遍构建
        assert len(calls) == 2 and sorted(calls) == ["a", "b"]
        assert await _count_cache(db, sid) == 1

    async def test_failure_releases_lock_and_retry_succeeds(self, ctx):
        """锁必须在**异常**时释放，否则本方案此后的所有导出永久挂起。"""
        db, sid, calls, _ = ctx
        state = {"fail_next": True, "builds": 0}
        orig = export_mod._build_docx_sync  # 同步契约（to_thread + docx 构建桩）

        def guarded_build(out_path, prep=None):
            state["builds"] += 1
            if state["fail_next"]:
                state["fail_next"] = False
                raise RuntimeError("构建失败")
            orig(out_path, prep)

        export_mod._build_docx_sync = guarded_build
        try:
            with pytest.raises(RuntimeError):
                await export_mod.export_docx(sid, {"config": {}, "_tag": "boom"},
                                             db=db)
            assert state["builds"] == 1
            # 锁未释放 ⇒ 本次调用会在 await_lock() 上永久挂起
            resp = await asyncio.wait_for(
                export_mod.export_docx(sid, {"config": {}, "_tag": "retry"}, db=db),
                timeout=10)
            assert state["builds"] == 2
            assert resp is not None
        finally:
            export_mod._build_docx_sync = orig
# ---------------------------------------------------------------------------
# 接线守卫（源码级 AST）：锁结构必须保持「守卫锁外 → 构建锁内 → finally 释放」
# ---------------------------------------------------------------------------


def _awaited_calls(node) -> list[str]:
    """按源码顺序收集函数体内所有 ``await name(...)`` 的调用名。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Await):
            v = n.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
                out.append(v.func.id)
    return out


def _awaited_calls_node(nodes) -> list[str]:
    out = []
    for n in nodes:
        out.extend(_awaited_calls(n))
    return out


def _release_in(nodes) -> bool:
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                    and sub.func.id == "_export_dedup_release":
                return True
    return False


# ⚠️ Python 3.14 起 ast.AsyncFunctionDef 不再继承 ast.FunctionDef（直联 stmt），
#    凡「任一函数定义」判定必须同时匹配两类，否则异步端点静默漏检。
_ANY_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _lhs_ids(node) -> set:
    """赋值语句的左侧标识符集合（兼容 Assign 多目标 / AnnAssign 注解赋值）。"""
    if isinstance(node, ast.Assign):
        return {t.id for t in node.targets if isinstance(t, ast.Name)}
    if isinstance(node, ast.AnnAssign):
        return {node.target.id} if isinstance(node.target, ast.Name) else set()
    return set()


def _lock_guard_structure(src: str, endpoint: str) -> bool:
    """端点必须满足：守卫 await 在锁获取之前、构建体在 try 内、释放于 finally。

    实现采用 wrapper-only（函数体不拆分）：构建/缓存逻辑仍直接位于路由函数
    体内，由 try/finally 包住 —— 既有的源码锚点守卫（`_prune_export_cache`、
    `_gc_orphan_exports`、`_display_scheme_name` 等）因此原样保留。
    """
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, _ANY_FUNC)
              and n.name == endpoint)
    awaits = _awaited_calls(fn)
    if len(awaits) < 2:
        return False
    if awaits[:2] != ["_guard_numbering_consistency", "_export_dedup_acquire"]:
        return False
    # 释放必须在 try/finally，且构建体（至少含 _prepare_export）在 try 体内
    for tr in ast.walk(fn):
        if not isinstance(tr, ast.Try):
            continue
        if not _release_in(tr.finalbody):
            continue
        if "_prepare_export" in _awaited_calls(tr):
            return True
    return False


def _read_export_src() -> str:
    return EXPORT_PY.read_text(encoding="utf-8").replace("\r\n", "\n")


class TestLockWiringSource:
    """DOCX/PDF 两条端点必须同构地接上并发去重锁（wrapper-only 结构）。"""

    def test_docx_endpoint_guard_outside_lock_build_inside_release_finally(self):
        assert _lock_guard_structure(_read_export_src(), "export_docx")

    def test_pdf_endpoint_guard_outside_lock_build_inside_release_finally(self):
        assert _lock_guard_structure(_read_export_src(), "export_pdf")

    def test_docx_body_carries_existing_anchors(self):
        """构建/缓存逻辑锚点守卫（2026-10-14 收口后调整）。

        _display_scheme_name / _build_docx_sync 仍在路由体内；写存 / 裁剪 / GC /
        留痕已下沉到统一 helper，路由体内改为调用 _persist_export_cache_row 与
        _finalize_export_delivery（锚点随之更新，既守「构建逻辑仍在路由体」、
        又守「收尾已接入统一 helper」）。
        """
        tree = ast.parse(_read_export_src())
        fn = next(n for n in tree.body if isinstance(n, _ANY_FUNC)
                  and n.name == "export_docx")
        seg = ast.get_source_segment(export_mod.__file__ and _read_export_src(), fn)
        for anchor in ("_persist_export_cache_row", "_finalize_export_delivery",
                       "_display_scheme_name", "_build_docx_sync"):
            assert anchor in seg, f"export_docx 缺锚点 {anchor}"

    def test_prepare_export_must_run_inside_lock(self):
        """锁要抑制的正是 prepare 之后的计费区（AI 生图在其中）。"""
        assert _lock_guard_structure(_read_export_src(), "export_docx")
        tree = ast.parse(_read_export_src())
        fn = next(n for n in tree.body if isinstance(n, _ANY_FUNC)
                  and n.name == "export_docx")
        for tr in ast.walk(fn):
            if isinstance(tr, ast.Try) and _release_in(tr.finalbody):
                assert "_prepare_export" in _awaited_calls(tr)
                return
        raise AssertionError("export_docx 未找到含 _prepare_export 的 try 块")

    def test_release_call_has_no_await(self):
        tree = ast.parse(_read_export_src())
        fn = next(n for n in tree.body if isinstance(n, _ANY_FUNC)
                  and n.name == "_export_dedup_release")
        assert not any(isinstance(n, ast.Await) for n in ast.walk(fn)), (
            "释放不应是异步的（与获取成对，无锁时代价必须为 0）")
# ---------------------------------------------------------------------------
# 字节体检 + 单一事实源
# ---------------------------------------------------------------------------


class TestByteHealthAndSingleSource:
    """触碰文件字节体检 + 锁/指纹符号唯一性 + 开关默认值锁。"""

    def _bytes_ok(self, p: Path) -> bool:
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            return False
        text = raw.decode("utf-8")
        if "\ufffd" in text:
            return False
        return text.count("\r") - text.count("\r\n") == 0

    def test_export_py_byte_health(self):
        assert self._bytes_ok(EXPORT_PY)

    def test_config_py_byte_health(self):
        assert self._bytes_ok(CONFIG_PY)

    def test_export_helpers_defined_once(self):
        src = _read_export_src()
        tree = ast.parse(src)
        toplevel = [n for n in tree.body
                    if isinstance(n, (*_ANY_FUNC, ast.Assign, ast.AnnAssign))]
        for name in ("_export_config_hash", "_export_dedup_acquire",
                     "_export_dedup_release", "_export_build_lock_for",
                     "_export_dedup_enabled", "_EXPORT_BUILD_LOCKS"):
            hits = 0
            for n in toplevel:
                if isinstance(n, _ANY_FUNC) and n.name == name:
                    hits += 1
                elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    if any(getattr(t, "id", "") == name for t in targets):
                        hits += 1
            assert hits == 1, f"{name} 应恰好定义一次，实际 {hits}"
            assert src.count(name) >= 1

    def test_dedup_switch_default_true(self):
        """新增开关必须默认开启（P2 收口基准），且 config.py 只声明一次。

        `export_dedup_concurrent: bool = True` 是注解赋值（AnnAssign），
        2026-10-13 实测 pytest 全仓 AST 扫描普遍只认 Assign —— 漏 AnnAssign
        会把任何带注解的配置项判成「未声明」。此处两类都收。
        """
        tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
        hits = [n for n in ast.walk(tree)
                if isinstance(n, (ast.Assign, ast.AnnAssign))
                and _lhs_ids(n) == {"export_dedup_concurrent"}]
        assert hits, "config.py 未声明 export_dedup_concurrent"
        assert len(hits) == 1
        value = ast.literal_eval(hits[0].value)
        assert value is True


# ---------------------------------------------------------------------------
# 进程内 A/B：不改磁盘，直接对变异后的源码判定守卫是否仍成立
# ---------------------------------------------------------------------------


class TestGuardDiscrimination:
    """静态守卫必须能判别关键回归形态（在内存中变异，不落盘）。"""

    def _src_mutator(self, variant: str) -> str:
        src = _read_export_src()
        if variant == "no_acquire":
            # 只替换第一条（DOCX 端点）；此处校验确有 2 处防锚点漂移
            assert src.count("    _lk = await _export_dedup_acquire(") >= 2
            src = src.replace("    _lk = await _export_dedup_acquire(",
                              "    _lk = None  # 变异", 1)
        elif variant == "no_release":
            # DOCX / PDF 各一处，**全部**替换：任一未被摘除则守卫仍可命中
            assert src.count("        _export_dedup_release(_lk)\n") >= 2
            src = src.replace("        _export_dedup_release(_lk)\n",
                              "        pass  # 变异\n")
        elif variant == "acquire_before_guard":
            lines = src.splitlines()
            out = [ln for ln in lines
                   if not ln.lstrip().startswith("await _guard_numbering_consistency(")]
            guard_call = "    await _guard_numbering_consistency(db, scheme_id)"
            idx = next(i for i, ln in enumerate(out)
                       if ln.startswith("    _lk = await _export_dedup_acquire("))
            out.insert(idx + 1, guard_call)  # 守卫被移到锁获取之后
            src = "\n".join(out)
        return src

    @pytest.mark.parametrize("variant", ["no_acquire", "no_release",
                                         "acquire_before_guard"])
    def test_docx_variants_are_caught(self, variant):
        src = self._src_mutator(variant)
        assert src != _read_export_src()
        assert not _lock_guard_structure(src, "export_docx"), (
            f"变异 {variant!r} 应被守卫判定为失效")

    @pytest.mark.parametrize("variant", ["no_release"])
    def test_pdf_variant_is_caught(self, variant):
        src = self._src_mutator(variant)
        assert src != _read_export_src()
        assert not _lock_guard_structure(src, "export_pdf")
