"""2026-10-14 · 正文生成 / 渲染链路定点回归（六个审查维度）。

本文件对应本轮修复的三个真实缺陷：

1. 维度五（并发与竞态）· P0：跨配置指纹的并发导出互相删除对方刚落盘的最终产物。
   去重锁的键含 config_hash，两个不同配置的请求持不同锁、完全并发；
   ``_gc_orphan_exports`` 判孤儿的依据是「DB 里没有缓存行指向它」，
   因此「os.replace 成功 → INSERT + commit」窗口内的产物会被对方删掉。
   修复：进程内「在飞产物」集合，GC 一律跳过在飞路径。

2. 维度四（缓存一致性）· P2：命中判据与保留判据不一致。
   命中要求 ``st_size > 0``，而 ``_prune_export_cache`` 只查 ``exists()``
   → 0 字节僵尸产物永远无法被返回，却占用 5 份保留配额，把新产物挤出窗口。
   修复：可用判据收敛到 ``_cache_row_viable``（单一事实源）。

3. 维度六（异常处理与兜底）· P1：未闭合代码围栏吞噬正文。
   旧实现只在**找到**中文句读行时才把截断点之后的正文还给段落解析；
   找不到句读行（截断点之后是列表 / 表格 / 英文 / 无标点内容）时
   整段被当代码块原样印进交付 DOCX。
   修复：无句读行且非图表围栏时整块跳过，不把正文当代码输出。
"""
from __future__ import annotations

import inspect
import os
import re
import time
from pathlib import Path

import pytest
from app.routers import export as E
from app.services.content_blocks import _parse_content_blocks


def _code_only(src: str) -> str:
    """去掉行尾注释，便于静态断言不被注释里的文字误伤。"""
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


# ===========================================================================
# 维度五 · 在飞产物集合（并发误删守卫）
# ===========================================================================
class TestInflightRegistry:
    """在飞集合本身的语义：注册 / 注销 / 快照 / 幂等。"""

    def test_register_unregister_roundtrip(self):
        p = Path("C:/exports/s1_abcd1234_ffff.docx")
        E._EXPORT_INFLIGHT_PATHS.clear()
        E._export_inflight_register(p)
        assert E._export_inflight_snapshot() == {E._export_inflight_key(p)}
        E._export_inflight_unregister(p)
        assert E._export_inflight_snapshot() == set()

    def test_unregister_is_idempotent(self):
        p = Path("/tmp/never-registered.docx")
        # 从未注册也绝不抛异常（降级路径 / PDF 降级分支会传 None）
        E._export_inflight_unregister(p)
        E._export_inflight_unregister(None)

    def test_key_is_casefolded_for_windows(self):
        """Windows 上大小写不敏感，键必须 normcase 后才能正确命中保护。"""
        a = Path("C:/EXPORTS/S1_ABCD1234_X.docx")
        b = Path("C:/exports/s1_abcd1234_x.docx")
        assert E._export_inflight_key(a) == E._export_inflight_key(b)


class TestGcSkipsInflight:
    """GC 必须跳过在飞产物 —— 这是「跨配置并发误删」的直接护栏。"""

    @staticmethod
    def _mk(path: Path, *, age: float = 0.0) -> Path:
        path.write_bytes(b"docx-bytes" * 256)
        if age:
            ts = time.time() - age
            os.utime(path, (ts, ts))
        return path

    @pytest.mark.asyncio
    async def test_final_product_protected_while_inflight(self, db_conn,
                                                          tmp_path,
                                                          monkeypatch):
        """非 .tmp. 的最终产物只要处于在飞状态，就不得被当作孤儿删除。

        旧实现（无在飞集合）下本用例**必然失败**：该文件既不在 live（DB 无行）、
        也不在 protect 里、也不是 .tmp.，会被立即 unlink。
        """
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.docx")
        E._EXPORT_INFLIGHT_PATHS.clear()
        E._export_inflight_register(p)
        try:
            assert await E._gc_orphan_exports(db_conn, "s1") == 0
            assert p.exists(), "在飞产物被误判为孤儿删除（跨配置并发互删回归）"
        finally:
            E._export_inflight_unregister(p)

    @pytest.mark.asyncio
    async def test_final_product_deleted_after_unregister(self, db_conn,
                                                          tmp_path,
                                                          monkeypatch):
        """注销后同一文件必须按普通孤儿处理（否则守卫会永久泄漏产物）。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.docx")
        E._EXPORT_INFLIGHT_PATHS.clear()
        E._export_inflight_register(p)
        E._export_inflight_unregister(p)
        assert await E._gc_orphan_exports(db_conn, "s1") == 1
        assert not p.exists()

    @pytest.mark.asyncio
    async def test_other_scheme_not_protected(self, db_conn, tmp_path,
                                              monkeypatch):
        """在飞保护必须限定在本方案前缀内（不得跨方案误保护）。

        注册的是**别的方案**的在飞产物，本方案前缀下的孤儿仍须删除。
        """
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.docx")
        E._EXPORT_INFLIGHT_PATHS.clear()
        # 在飞的是别方案的产物（s2_*），本方案的 s1_* 仍属孤儿，必须删
        E._export_inflight_register(tmp_path / "s2_bbbbcccc_1234abcd.docx")
        assert await E._gc_orphan_exports(db_conn, "s1") == 1
        assert not p.exists()


class TestInflightWiredIntoExportPaths:
    """静态护栏：注册 / 注销必须覆盖 replace → INSERT → commit → 回收 整段窗口。

    ✅ 2026-10-14 收口：注销调用已统一收敛进 _finalize_export_delivery，
    本类断言改为「路由函数调用 helper 时正确传参」+「helper 内部顺序契约」。
    """

    def test_docx_registers_and_unregisters(self):
        code = _code_only(inspect.getsource(E.export_docx))
        assert code.count("_export_inflight_register(out_path)") == 1, (
            "DOCX 必须在 out_path 确定后注册在飞保护（且仅一次）")
        assert code.count("_inflight_final = out_path") == 1, (
            "注册前须把 out_path 另存为 _inflight_final —— degraded 分支会把 "
            "out_path 重绑定为 tmp_out_path，若注销时读 out_path 会拿错路径")
        assert code.count("_finalize_export_delivery(") == 2, (
            "DOCX 的两条交付路径（degraded 提前返回 + 正常成功）都必须走统一收尾")
        assert code.count("inflight_path=_inflight_final") == 2, (
            "两条路径都必须注销注册时另存的 _inflight_final，否则本条注册永久残留")

    def test_pdf_registers_and_guards_degraded_branch(self):
        """PDF 降级分支不落盘 out_path，注销必须用条件表达式（否则 UnboundLocalError）。"""
        code = _code_only(inspect.getsource(E.export_pdf))
        assert code.count("_export_inflight_register(out_path)") == 1
        assert code.count("_finalize_export_delivery(") == 1, (
            "PDF 成功/降级共用尾部，必须恰好一次统一收尾")
        assert "inflight_path=(out_path if not degraded else None)" in code, (
            "PDF 注销在飞保护缺 `if not degraded` 守卫 —— 降级路径下 out_path 未绑定")

    def test_finalize_helper_holds_order_contract(self):
        """helper 内部：留痕 → 回收 → 注销，顺序不得颠倒；None 表示无注册可注销。"""
        src = _code_only(inspect.getsource(E._finalize_export_delivery))
        i_trace = src.index("_record_export_review_trace(")
        i_gc = src.index("_gc_orphan_exports(")
        i_unreg = src.index("_export_inflight_unregister(inflight_path)")
        assert i_trace < i_gc < i_unreg, (
            "顺序契约被破坏：注销必须在 GC 之后（GC 期间产物仍需受保护），"
            "GC 必须在留痕之后（留痕可能 commit）")
        assert "if inflight_path is not None:" in src, (
            "缺 None 守卫 —— PDF degraded 分支传 None 时会误注销 Path()")
        assert src.index("protect=gc_protect") > i_trace, (
            "GC 必须透传调用方的 protect 集合")

    def test_persist_helper_holds_double_commit_contract(self):
        """helper 内部：INSERT → commit → prune → commit（PDF 2026-10-13 口径）。"""
        src = _code_only(inspect.getsource(E._persist_export_cache_row))
        i_ins = src.index("INSERT OR IGNORE INTO export_cache")
        i_c1 = src.index("await db.commit()")
        i_prune = src.index("_prune_export_cache(")
        i_c2 = src.index("await db.commit()", i_prune)
        assert i_ins < i_c1 < i_prune < i_c2, (
            "提交顺序契约被破坏：新行必须先落盘再裁剪，裁剪后再提交 —— "
            "prune 回滚不得带走新缓存行")

    def test_gc_consults_inflight_snapshot(self):
        src = _code_only(inspect.getsource(E._gc_orphan_exports))
        assert "_export_inflight_snapshot()" in src, (
            "GC 未读取在飞快照（跨配置并发互删回归）")
        assert "in inflight" in src, "在飞路径未参与孤儿判定"

    def test_cache_hit_branch_still_never_calls_gc(self):
        """缓存命中分支交付的是历史产物（DB 有行），仍不得调用回收。"""
        src = inspect.getsource(E.export_docx)
        i_hit = src.index('X-Cache-Status": "hit"')
        assert "_gc_orphan_exports" not in src[:i_hit + 400]


# ===========================================================================
# 维度四 · 缓存行可用判据（命中 / 保留 单一事实源）
# ===========================================================================
class TestCacheRowViable:
    """0 字节产物既不能被返回，也不得占用保留配额。"""

    def test_rejects_empty_string_and_none(self):
        assert E._cache_row_viable("") is False
        assert E._cache_row_viable(None) is False

    def test_rejects_missing_file(self, tmp_path):
        assert E._cache_row_viable(str(tmp_path / "nope.docx")) is False

    def test_rejects_zero_byte_file(self, tmp_path):
        p = tmp_path / "empty.docx"
        p.write_bytes(b"")
        assert E._cache_row_viable(str(p)) is False

    def test_rejects_directory(self, tmp_path):
        d = tmp_path / "dir.docx"
        d.mkdir()
        assert E._cache_row_viable(str(d)) is False

    def test_accepts_non_empty_file(self, tmp_path):
        p = tmp_path / "ok.docx"
        p.write_bytes(b"z")
        assert E._cache_row_viable(str(p)) is True

    def test_survives_oserror(self):
        # 非法路径在 stat 时可能抛 OSError，判据必须静默降级
        assert E._cache_row_viable("Z" * 4000) is False

    def test_viable_check_includes_size_guard(self):
        assert "st_size > 0" in inspect.getsource(E._cache_row_viable)


@pytest.mark.asyncio
class TestPruneTreatsZeroByteAsStale:
    """保留判据必须与命中判据同口径：0 字节产物要被判为陈旧并清掉。"""

    async def test_zero_byte_row_is_pruned_not_kept(self, db_conn, tmp_path):
        empty = tmp_path / "s1_empty.docx"
        empty.write_bytes(b"")
        good = tmp_path / "s1_good.docx"
        good.write_bytes(b"good-bytes")
        for row_id, fp, path, ts in (
            ("e1", "fp-empty", empty, "2020-01-01 00:00:00"),
            ("g1", "fp-good", good, "2026-01-01 00:00:00"),
        ):
            await db_conn.execute(
                "INSERT INTO export_cache (id, project_id, scheme_id, config_hash,"
                " content_fingerprint, cache_key, result_path, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (row_id, "", "s1", "cfg", fp, "s1_cfg", str(path), ts))
        await db_conn.commit()

        await E._prune_export_cache(db_conn, "s1", protect_path=good)

        rows = {r[0] for r in await db_conn.execute_fetchall(
            "SELECT id FROM export_cache WHERE scheme_id='s1'")}
        assert "e1" not in rows, (
            "0 字节僵尸缓存行未被判为陈旧（旧实现只查 exists()，"
            "会占住保留配额把新产物挤出窗口）")
        assert "g1" in rows, "正常缓存行被误删"

    async def test_prune_and_hit_share_one_predicate(self):
        """静态护栏：命中判据与保留判据必须收敛到同一个 helper。"""
        src = _code_only(inspect.getsource(E.export_docx))
        src += _code_only(inspect.getsource(E.export_pdf))
        prune = _code_only(inspect.getsource(E._prune_export_cache))
        assert "_cache_row_viable" in src, (
            "export_docx / export_pdf 的命中判据未走 _cache_row_viable")
        assert "_cache_row_viable" in prune, (
            "_prune_export_cache 的保留判据未走 _cache_row_viable")
        assert "st_size > 0" not in prune, (
            "保留判据仍在函数内硬编码 size 检查（判据分叉回归）")


# ===========================================================================
# 维度六 · 未闭合围栏不得吞噬正文
# ===========================================================================
class TestUnclosedFenceDoesNotSwallowBody:
    """无句读行时不得把截断点之后的正文当成代码块输出。"""

    def test_bullet_list_after_truncation_not_dropped_as_code(self):
        """截断点之后是项目符号列表（无中文句读）→ 不得整体成为代码块。

        旧实现：``_cut is None`` → code_lines 保持「消费至文件末尾」的整段，
        随后落入通用 code 分支，整段列表被印成等宽代码块。
        """
        content = (
            "## 2.3 临时用电措施\n\n"
            "配电箱应按三级配电两级保护设置，具体要求如下：\n"
            "```python\n"
            "B --> C{\n"                      # 未闭合代码围栏（模拟截断）
            "- 总配电箱由项目部统一设置\n"
            "- 分配电箱按施工区域就近布置\n"
            "- 开关箱执行一机一闸一漏电保护\n"
        )
        blocks = _parse_content_blocks(content)
        code_blocks = [b for b in blocks if b.get("type") == "code"]
        assert code_blocks == [], (
            "未闭合围栏且无句读行时，其后正文被当成代码块输出："
            f"code blocks = {code_blocks!r}")

    def test_sentence_boundary_still_restored(self):
        """有句读行时仍按原逻辑把其后正文还给段落解析（行为不回归）。"""
        content = (
            "## 2.3 施工部署\n\n"
            "```python\n"
            "B --> C{\n"
            "1. 组织部署\n"
            "2. 资源配置\n"
            "本方案由项目经理统一组织。\n"      # 句读行 → 正文起点
            "后续段落继续说明。"
        )
        blocks = _parse_content_blocks(content)
        text = "".join(str(b.get("text") or "")
                       + " ".join(b.get("lines") or []) for b in blocks)
        assert "本方案由项目经理统一组织" in text, (
            "有句读行的正文还原逻辑被破坏")

    def test_chart_fence_unclosed_still_skipped(self):
        """图表家族围栏未闭合仍走「跳过」，不产出图也不产出代码块。"""
        content = (
            "## 2.4 施工流程\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            "A[开始] --> B[\n"
            "- 后续正文条目\n"
        )
        blocks = _parse_content_blocks(content)
        assert not [b for b in blocks if b.get("type") == "chart"]
        assert not [b for b in blocks if b.get("type") == "code"]

    def test_closed_fences_unchanged(self):
        """闭合围栏的行为必须完全不变（回归护栏）。"""
        content = (
            "```python\n"
            "x = 1\n"
            "```\n"
            "正文段落。"
        )
        blocks = _parse_content_blocks(content)
        code = [b for b in blocks if b.get("type") == "code"]
        assert len(code) == 1
        assert code[0]["lines"] == ["x = 1"]


# ===========================================================================
# 收口（2026-10-14 · 遗留建议执行）：两个统一 helper 的行为契约
# ===========================================================================
class TestFinalizeExportDeliveryBehavior:
    """mock 三个下游，锁定「留痕 → 回收 → 注销」的调用顺序与参数透传。"""

    @pytest.mark.asyncio
    async def test_order_and_passthrough(self, monkeypatch):
        calls = []

        async def _trace(db, sid, kind, round_no, fname):
            calls.append(("trace", sid, kind, round_no, fname))

        async def _gc(db, sid, protect=None):
            calls.append(("gc", sid, protect))
            return 0

        monkeypatch.setattr(E, "_record_export_review_trace", _trace)
        monkeypatch.setattr(E, "_gc_orphan_exports", _gc)
        E._EXPORT_INFLIGHT_PATHS.clear()
        p = Path("C:/exports/s1_aabbccdd_eeff0011.docx")
        E._export_inflight_register(p)
        await E._finalize_export_delivery(
            object(), scheme_id="s1", kind="docx", round_no=3,
            export_filename="方案_2026-10-14_第3轮.docx",
            gc_protect={p}, inflight_path=p)
        assert [c[0] for c in calls] == ["trace", "gc"], (
            "顺序契约：留痕必须先于 GC（GC 判定依赖已提交行），"
            "注销必须在 GC 之后（GC 期间产物仍需受保护）")
        assert calls[0][1:] == ("s1", "docx", 3, "方案_2026-10-14_第3轮.docx")
        assert calls[1][2] == {p}, "protect 集合必须原样透传给 GC"
        # 注销在 GC 返回之后生效：集合里该键已被移除
        assert E._export_inflight_key(p) not in E._export_inflight_snapshot()

    @pytest.mark.asyncio
    async def test_none_inflight_skips_unregister(self, monkeypatch):
        """inflight_path=None（PDF degraded 分支）不得触碰在飞集合 ——
        旧实现靠 `if not degraded:` 守卫，现由 None 判定承担同一防护。"""
        async def _trace(*a, **k):
            pass

        async def _gc(*a, **k):
            return 0

        monkeypatch.setattr(E, "_record_export_review_trace", _trace)
        monkeypatch.setattr(E, "_gc_orphan_exports", _gc)
        E._EXPORT_INFLIGHT_PATHS.clear()
        sentinel = Path("C:/exports/s1_00000000_11111111.pdf")
        E._export_inflight_register(sentinel)
        await E._finalize_export_delivery(
            object(), scheme_id="s1", kind="pdf", round_no=1,
            export_filename="f.pdf", inflight_path=None)
        assert E._export_inflight_key(sentinel) in E._export_inflight_snapshot(), (
            "None 注销误伤了无关的在飞注册")
        E._export_inflight_unregister(sentinel)


class TestPersistExportCacheRowBehavior:
    """INSERT OR IGNORE → commit → prune → commit 的真实 DB 行为。"""

    @staticmethod
    def _args(db, tmp_path, **over):
        out = tmp_path / "s1_aabbccdd_eeff0011.docx"
        out.write_bytes(b"docx-bytes")
        kw = dict(db=db, scheme={"project_id": "p1"}, scheme_id="s1",
                  config_hash="aabbccdd" * 8, content_fingerprint="eeff0011" * 8,
                  cache_key="s1_aabbccdd", out_path=out, what="t")
        kw.update(over)
        return kw

    @pytest.mark.asyncio
    async def test_row_lands_with_cache_key(self, db_conn, tmp_path, monkeypatch):
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        kw = self._args(db_conn, tmp_path)
        await E._persist_export_cache_row(**kw)
        cur = await db_conn.execute(
            "SELECT result_path, cache_key, project_id FROM export_cache"
            " WHERE scheme_id='s1'")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["result_path"] == str(kw["out_path"])
        assert rows[0]["cache_key"] == "s1_aabbccdd"
        assert rows[0]["project_id"] == "p1"

    @pytest.mark.asyncio
    async def test_new_row_survives_prune_failure(self, db_conn, tmp_path,
                                                  monkeypatch):
        """双提交语义：prune 抛异常时，新缓存行已在第一次 commit 落盘。

        旧 DOCX「INSERT → prune → 单次 commit」口径下本用例必然失败 ——
        prune 异常把同事务的新行一并回滚，缓存永不落盘（2026-10-13 P1 同族）。
        """
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        kw = self._args(db_conn, tmp_path)

        async def _boom(db, scheme_id, protect_path):
            raise RuntimeError("prune boom")

        monkeypatch.setattr(E, "_prune_export_cache", _boom)
        with pytest.raises(RuntimeError, match="prune boom"):
            await E._persist_export_cache_row(**kw)
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM export_cache WHERE scheme_id='s1'")
        assert (await cur.fetchone())[0] == 1, (
            "prune 失败把已写入的新缓存行一并回滚（双提交契约被破坏）")

    @pytest.mark.asyncio
    async def test_prune_keeps_at_most_five(self, db_conn, tmp_path, monkeypatch):
        """helper 内联裁剪：预置 5 份旧缓存 + 本次新行 = 6 行 → 裁到 5 行。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        for i in range(5):
            fp = tmp_path / f"s1_old{i:04d}_f{i:07d}.docx"
            fp.write_bytes(b"old")
            await db_conn.execute(
                "INSERT INTO export_cache (id, project_id, scheme_id, config_hash,"
                " content_fingerprint, cache_key, result_path, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"old{i}", "p1", "s1", f"old{i:04d}", f"f{i:07d}" * 8,
                 f"s1_old{i:04d}", str(fp), f"2026-10-01 0{i}:00:00"))
        await db_conn.commit()
        await E._persist_export_cache_row(**self._args(db_conn, tmp_path))
        cur = await db_conn.execute(
            "SELECT result_path FROM export_cache WHERE scheme_id='s1'")
        rows = await cur.fetchall()
        assert len(rows) == 5, f"保留策略未生效：{len(rows)} 行"
        # 最旧的 old0 必须被裁掉，本次新行必须保留
        paths = {r[0] for r in rows}
        assert str(tmp_path / "s1_old0000_f0000000.docx") not in paths
        assert str(tmp_path / "s1_aabbccdd_eeff0011.docx") in paths

    @pytest.mark.asyncio
    async def test_insert_or_ignore_keeps_existing_row(self, db_conn, tmp_path,
                                                       monkeypatch):
        """同指纹重复调用（并发导出）只保留一行（B5 唯一约束口径）。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        kw = self._args(db_conn, tmp_path)
        await E._persist_export_cache_row(**kw)
        await E._persist_export_cache_row(**kw)
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM export_cache WHERE scheme_id='s1'")
        assert (await cur.fetchone())[0] == 1