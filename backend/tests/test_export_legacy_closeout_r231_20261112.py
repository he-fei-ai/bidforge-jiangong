"""R231 导出模块遗留收口护栏（2026-11-12）。

承接 R230 §九登记项的重新取证与落地（详见 docs/导出模块遗留收口_R231_20261112.md）：
- B5  混合矩阵形态：矩阵环境与环境外内容在 m:oMath 内并列拼接（整式幂等、双矩阵、
      片段不误挂悬空占位框）
- B7  图片回滚连带清理 blip 引用的 relationship（media part 不再随失败膨胀）
- B8  列表缩进口径单源（视觉缩进 tab=4；tab 缩进嵌套列表不再被拍平到 0 级）
- R100 取数段 render_stats 全 0 空壳移除（零真消费实锤）
- F5  PDF/DOCX 行为级测试落地：锁等待超时 → _PDFConvertBusy（503 映射源码锚）、
      构建后取消 → 499 + tmp 清理 + 不写缓存行（R230 A9 行为实证）
- F13 X-Export-Round 信息性头（前端刻意不消费，见前端 exportR230.test.ts 证伪锚）
"""

import asyncio
import inspect
import io
import re
import threading
import zipfile

import app.routers.export as E
import pytest
from app.services import content_blocks as CB
from app.services import docx_math as DM


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# B5：混合矩阵形态
# ---------------------------------------------------------------------------


class TestB5MixedMatrix:
    BS = chr(92)

    def _matrix(self, env: str = "pmatrix", body: str = " a & b ") -> str:
        return f"{self.BS}begin{{{env}}}{body}{self.BS}end{{{env}}}"

    def test_mixed_env_plus_text(self):
        r = DM.latex_to_omml("K" + self._matrix() + chr(92) + "delta")
        assert r.count("<m:oMath") == 1, "混合形态仍是单公式"
        assert "<m:m>" in r and ">K<" in r, "矩阵与环境外文本并列"
        assert "pmatrix" not in r and "&amp;" not in r, "环境名与列分隔符不得残留"

    def test_two_matrices_same_oMath(self):
        r = DM.latex_to_omml(self._matrix("bmatrix", " 1 ") + self._matrix("bmatrix", " 2 "))
        assert r.count("<m:m>") == 2 and r.count("<m:oMath") == 1

    def test_no_false_dangling_inside_mixed(self):
        # 片段以关系运算符结尾（右值=矩阵）时不得误挂「待填」框
        r = DM.latex_to_omml("x = " + self._matrix("matrix", " 1 "))
        assert "m:box" not in r, "混合路径不挂悬空占位（宁漏勿误）"
        assert ">x<" in r and ">=" in r

    def test_whole_form_still_idempotent(self):
        whole = self._matrix()
        assert DM.latex_to_omml(whole).count("<m:m>") == 1

    def test_xml_wellformed(self):
        import xml.etree.ElementTree as ET

        r = DM.latex_to_omml("K" + self._matrix() + chr(92) + "delta")
        ET.fromstring(r)  # WPS 可打开性必要条件

    def test_regex_not_greedy_across_two_envs(self):
        """贪婪版 `(.+)` 会把两个环境并成一个 body —— 非贪婪修正的行为锁。"""
        src = inspect.getsource(DM)
        assert (
            r'\}(.+?)\\end\s*\{\1\}' in src
        ), "矩阵环境体必须非贪婪（.+?），否则双环境被吞成一个 body（行为见 test_two_matrices_same_oMath）"


# ---------------------------------------------------------------------------
# B7：图片回滚连带清理 relationship
# ---------------------------------------------------------------------------


class TestB7RollbackCleansRels:
    def _doc_with_picture(self):
        from docx import Document

        d = Document()
        d.add_paragraph("正文")
        pic = d.add_picture(io.BytesIO(_png_bytes()))
        return d, pic._inline.graphic.graphicData.pic.blipFill.blip.embed

    def test_rollback_removes_rel_and_media(self):
        from app.routers import _export_docx_parts as EP
        from docx import Document

        d, _ = self._doc_with_picture()  # 既有正文 + 既有图（对照组）
        rb = EP._DocxBodyRollback(d)  # ✅ 快照必须先于本次插入（真实时序）
        pic = d.add_picture(io.BytesIO(_png_bytes()))
        rid = pic._inline.graphic.graphicData.pic.blipFill.blip.embed
        # 模拟「图片已写入、图题阶段失败」→ rollback
        removed = rb.rollback()
        assert removed >= 1
        assert rid not in d.part.rels, "rollback 必须连带删除本次图片的 relationship"

        out = io.BytesIO()
        d.save(out)
        media = [n for n in zipfile.ZipFile(io.BytesIO(out.getvalue())).namelist() if n.startswith("word/media/")]
        assert media == [], "保存按 rels 图遍历序列化：删 rel 后 media part 不得再写入产物（孤儿膨胀根治）"

    def test_existing_rels_unaffected(self):
        from app.routers import _export_docx_parts as EP
        from docx import Document

        d, rid = self._doc_with_picture()
        old = d.add_picture(io.BytesIO(_png_bytes()))
        old_rid = old._inline.graphic.graphicData.pic.blipFill.blip.embed
        rb = EP._DocxBodyRollback(d)
        rb.rollback()
        assert old_rid in d.part.rels, "既有图片的 rel 绝不受影响（只删本次新增）"

    def test_source_anchor(self):
        from app.routers import _export_docx_parts as EP

        src = inspect.getsource(EP._DocxBodyRollback)
        assert 'qn("a:blip")' in src and "r:embed" in src, "rollback 必须遍历被删元素的 blip 引用"


# ---------------------------------------------------------------------------
# B8：列表缩进口径单源（视觉缩进 tab=4）
# ---------------------------------------------------------------------------


class TestB8VisualIndent:
    def _parse(self, md: str):
        return [b for b in CB._parse_content_blocks(md) if b.get("type") == "list_item"]

    def test_tab_no_longer_flattened(self):
        t = chr(9)
        items = self._parse(f"{t}- 甲\n{t}{t}- 乙")
        assert items[0]["indent"] == 4, "1 个 tab 视觉缩进 = 4（旧原始字符口径=1 → 导出 //2 拍平到 0 级）"
        assert items[0]["indent"] // 2 >= 1, "tab 缩进列表不得再被拍平到 0 级"
        assert items[1]["indent"] == 8 and items[1]["indent"] // 2 > items[0]["indent"] // 2, "嵌套保序"

    def test_spaces_semantics_unchanged(self):
        items = self._parse("- 甲\n  - 乙")
        assert items[0]["indent"] == 0 and items[0]["indent"] // 2 == 0
        assert items[1]["indent"] == 2 and items[1]["indent"] // 2 == 1, "2 空格嵌套语义不变"

    def test_continuation_judgement_same_source(self):
        """续行合并判据与 list_item.indent 必须同口径（旧两口径分叉：tab=1 vs tab=4）。

        R231 口径归一后的行为改进：与列表项**同视觉缩进**的行（旧口径因 tab=1
        被误判「更深」而误并入）不再是续行；更深缩进仍并入（R182 语义保留）。
        """
        t = chr(9)
        # 更深缩进 → 仍并入（R182 主场景）
        md = f"{t}- 甲\n{t}{t}续行属于甲"
        blocks = CB._parse_content_blocks(md)
        items = [b for b in blocks if b.get("type") == "list_item"]
        assert len(items) == 1 and "续行属于甲" in items[0]["text"], "更深缩进续行仍并入列表项"
        # 同视觉缩进 → 不并入（旧口径 tab=1 使 4>1 误合并同层段落）
        md2 = f"{t}- 甲\n{t}同层段落"
        blocks2 = CB._parse_content_blocks(md2)
        paras = [b for b in blocks2 if b.get("type") == "paragraph"]
        assert any("同层段落" in str(b.get("text")) for b in paras), "同层行不得误并入列表项"


# ---------------------------------------------------------------------------
# R100：取数段 render_stats 全 0 空壳移除
# ---------------------------------------------------------------------------


class TestR100EmptyShellRemoved:
    async def test_prepare_no_render_stats_key_before_render(self, db_conn):
        """fast_path 开启时命中前 prep 无 render_stats 键（旧全 0 壳有被误当真值消费的风险）。"""
        sid = "r100-shell"
        await db_conn.execute("INSERT INTO schemes(id, project_id, name) VALUES (?,?,?)", (sid, "p", "空壳移除"))
        await db_conn.execute(
            "INSERT INTO sections(id, scheme_id, parent_id, sort_order, level, title, content) VALUES (?,?,?,?,?,?,?)",
            ("r100-s1", sid, "", 0, 1, "概况", "本工程为机电安装工程。"),
        )
        await db_conn.commit()
        prep = await E._prepare_export(sid, {"config": {}}, db_conn, cancel_event=None, defer_audit=True)
        assert "render_stats" not in prep, "取数段不得再预置全 0 空壳（渲染段自建并在末尾整体赋值）"

    def test_render_stage_builds_its_own(self):
        src = inspect.getsource(E._prepare_export_render)
        assert "render_stats: dict = {" in src, "渲染段自建统计 dict"
        assert 'plan["render_stats"] = render_stats' in src, "渲染段末尾整体赋值"


# ---------------------------------------------------------------------------
# F5：行为级 —— PDF 锁等待超时 / DOCX 构建后取消
# ---------------------------------------------------------------------------


class TestBehaviourLevelR231:
    def test_pdf_lock_timeout_raises_busy(self, monkeypatch):
        """锁被占用且等待超过上限 → _PDFConvertBusy（路由层映射 503，源码锚另锁）。"""
        monkeypatch.setattr(E, "_PDF_CONVERT_LOCK_TIMEOUT", 0.2)
        gate = threading.Event()

        def _holder():
            with E._PDF_CONVERT_LOCK:
                gate.set()
                threading.Event().wait(2.0)

        th = threading.Thread(target=_holder, daemon=True)
        th.start()
        gate.wait(2.0)
        with pytest.raises(E._PDFConvertBusy):
            E._convert_docx_to_pdf("whatever.docx", "whatever.pdf")

    def test_busy_maps_to_503(self):
        src = inspect.getsource(E.export_pdf)
        assert "except _PDFConvertBusy" in src and "503" in src, "_PDFConvertBusy → 503 映射在位"

    async def test_docx_post_build_cancel_499(self, db_conn, tmp_path, monkeypatch):
        """R230 A9 行为实证：构建完成后置位取消 → 499 + 临时产物清理 + 不写缓存行。"""
        from pathlib import Path

        pytest.importorskip("docx")
        sid = "r231-cancel"
        await db_conn.execute("INSERT INTO schemes(id, project_id, name) VALUES (?,?,?)", (sid, "p", "取消行为"))
        await db_conn.execute(
            "INSERT INTO sections(id, scheme_id, parent_id, sort_order, level, title, content) VALUES (?,?,?,?,?,?,?)",
            ("r231-s1", sid, "", 0, 1, "概况", "本工程为机电安装工程，含设备安装与调试。"),
        )
        await db_conn.commit()
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        monkeypatch.setattr(E, "_record_export_review_trace", lambda *a, **k: asyncio.sleep(0))
        monkeypatch.setattr(E, "_export_cache_fast_path_enabled", lambda: False)

        # /cancel 的核心置位动作（_export_cancel_request 同步函数，工作线程可直调）
        # 在构建「之后」被调用：fake_build 返回前置位 → A9 构建后检查点命中
        def fake_build(out_path, *args, **kwargs):
            Path(out_path).write_bytes(b"built")
            E._export_cancel_request(sid)
            return {}

        monkeypatch.setattr(E, "_build_docx_sync", fake_build)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            await E.export_docx(sid, {"config": {}, "override_high_issues": True}, db=db_conn)
        assert ei.value.status_code == 499, "构建后取消检查点必须命中（A9 行为实证）"
        assert list(tmp_path.glob("*.tmp.docx")) == [], "取消后临时产物必须被清理"
        cur = await db_conn.execute("SELECT COUNT(*) FROM export_cache WHERE scheme_id=?", (sid,))
        assert (await cur.fetchone())[0] == 0, "取消产物不得写缓存行"

    def test_cancellation_maps_to_499(self):
        for fn in (E.export_docx, E.export_pdf):
            src = inspect.getsource(fn)
            assert "HTTPException(499" in src, f"{fn.__name__} 取消必须映射 499"
