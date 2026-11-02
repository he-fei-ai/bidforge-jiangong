# -*- coding: utf-8 -*-
"""R156（2026-11-01）· 遗留收口 · 正文落库写端 0 行守卫（L-7 便宜收口面）护栏。

【背景】R149 L-7（生成↔sections 写端点 TOCTOU 毫秒级窗口）判「结构性、单独立项」。
本轮复核发现其**后果面**可用 R13/R72 既有纪律便宜收口：
  - ``sse_handlers._persist_content_once``（正文终态落库）UPDATE 无任何行数守卫：
    delete_section 的 409 守卫是**前置判定**，与 DELETE 提交之间存在毫秒级窗口；
    窗口命中 → UPDATE 命中 0 行仍走 done_ids/checkpoint 记「完成」→ **幽灵成功**，
    断线重挂跳过该章 = 永久缺章。R149 FIX-D 只补了目录保存侧（_save_outline_to_db），
    正文侧是同族漏网（「同族修一处漏一处」第四次命中）。
  - ``charts.py`` 两处正文回写只有 R13 判空守卫（``cur is None``），缺 R72 行数面；
    同族的 review_autofix / consistency_repair 均已走 ``review_db.exec_write``
    （safe_rowcount + require_rows 0 行 503）。

【修法（R156）】
  - FIX-1a：``_persist_content_once`` 走 ``safe_rowcount`` 唯一出口，0 行抛领域
    异常（SSE 上下文不可用 HTTPException）→ gen_one 判该章失败、checkpoint 如实记。
  - FIX-1b：charts.py 两处补 safe_rowcount 行数守卫（:612 RuntimeError→rollback；
    :779 HTTPException 503，与既有 R13 判空守卫同文案族）。

【判据教训】源码锚断**调用形态**（R144 教训②）；行为级测试打桩必须落在**函数级
导入的源模块**（R149 教训⑦：``generate_illustration_image`` 在端点体内 import）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A 组：sse_handlers._persist_content_once 写端守卫（源码形态锚）
# ---------------------------------------------------------------------------
class TestSseContentWriteGuard:
    def _closure_src(self) -> str:
        src = _src("app/routers/sse_handlers.py")
        start = src.index("async def _persist_content_once():")
        end = src.index("await retry_db_op(_persist_content_once", start)
        return src[start:end]

    def test_safe_rowcount_call_shape(self):
        """闭包体含 safe_rowcount 调用形态（R13 唯一出口，非裸 .rowcount）。"""
        body = self._closure_src()
        assert "safe_rowcount(" in body
        assert "_cur, what=f\"正文落库（section=" in body

    def test_zero_row_raises_domain_error(self):
        """0 行 → 领域异常（SSE 上下文不可用 HTTPException），章节已不存在。"""
        body = self._closure_src()
        assert "if _n < 1:" in body
        assert "章节已不存在" in body
        assert "raise RuntimeError" in body

    def test_safe_rowcount_imported(self):
        """sse_handlers 模块已导入 safe_rowcount（既有导入，F821 免验）。"""
        src = _src("app/routers/sse_handlers.py")
        assert "safe_rowcount" in src.split("async def ")[0]

    def test_guard_before_commit(self):
        """守卫在 commit **之前**（0 行时不提交半截事务）。"""
        body = self._closure_src()
        assert body.index("safe_rowcount(") < body.index("await db.commit()")


# ---------------------------------------------------------------------------
# B 组：charts.py generate_ai_image 写端守卫（行为级，真实端点函数 + 桩 DB）
# ---------------------------------------------------------------------------
class _Row(dict):
    """sqlite3.Row 最小替身（下标取值）。"""


class _Cursor:
    def __init__(self, rowcount: int = 1, row=None, rows=None):
        self.rowcount = rowcount
        self._row = row
        self._rows = rows if rows is not None else ([row] if row else [])

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _StubDB:
    """按 SQL 前缀分派的异步桩 DB。"""

    def __init__(self, sections_rowcount: int, section_row: dict):
        self.sections_rowcount = sections_rowcount
        self.section_row = section_row
        self.commits = 0

    async def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            return _Cursor(rowcount=1, row=self.section_row)
        if "UPDATE chart_predictions" in sql:
            return _Cursor(rowcount=1)
        if "UPDATE sections" in sql:
            return _Cursor(rowcount=self.sections_rowcount)
        return _Cursor(rowcount=1)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


_AI_IMAGE_CONTENT = (
    "施工流程如下。\n\n```ai_image\n"
    '{"prompt": "施工现场流程图", "title": "施工流程图", "style": "engineering_diagram"}\n'
    "```\n\n以上。"
)


@pytest.fixture()
def _patch_ai_chain(monkeypatch):
    """打桩 AI 链：提示词优化 / 图像生成 / 目标守卫。

    ⚠️ generate_illustration_image 是端点体内函数级导入 —— 桩必须落在源模块
    属性（R149 教训⑦），打 charts 模块属性无效。
    """
    import app.config as cfg
    import app.routers.charts as charts_mod
    import app.services.ai.image_engine as image_engine

    async def _noop_guard(db, section_id, action, prediction_id=""):
        return None

    async def _fake_optimize(messages, **kw):
        return "优化后的提示词"

    async def _fake_image(*a, **kw):
        return "http://img.test/generated.png"

    monkeypatch.setattr(cfg.settings, "ai_image_manual_enabled", True, raising=False)
    monkeypatch.setattr(charts_mod, "_guard_chart_target", _noop_guard)
    monkeypatch.setattr(charts_mod, "chat_with_fallback", _fake_optimize)
    monkeypatch.setattr(image_engine, "generate_illustration_image", _fake_image)


def _section_row():
    return _Row(id="sec-1", content=_AI_IMAGE_CONTENT, word_budget=1200)


class TestGenerateAiImageRowcountGuard:
    async def test_zero_row_returns_503(self, _patch_ai_chain):
        """0 行（章节在请求与写之间被删）→ 503，绝不汇报 content_updated=True。"""
        from app.routers.charts import generate_ai_image
        from fastapi import HTTPException
        db = _StubDB(sections_rowcount=0, section_row=_section_row())
        with pytest.raises(HTTPException) as ei:
            await generate_ai_image({"section_id": "sec-1"}, db=db)
        assert ei.value.status_code == 503
        assert "章节已不存在" in str(ei.value.detail)

    async def test_normal_row_still_succeeds(self, _patch_ai_chain):
        """负控：行数 1 → 正常返回 content_updated=True（正常路径零扰动）。"""
        from app.routers.charts import generate_ai_image
        db = _StubDB(sections_rowcount=1, section_row=_section_row())
        resp = await generate_ai_image({"section_id": "sec-1"}, db=db)
        assert resp["content_updated"] is True
        assert resp["generated"] >= 1
        assert "![施工流程图](http://img.test/generated.png)" in resp["new_content"]
        assert db.commits == 1

    def test_fix_mermaid_write_guard_shape(self):
        """fix-mermaid 正文写回点含 safe_rowcount 调用形态（源码锚）。"""
        src = _src("app/routers/charts.py")
        assert 'what=f"Mermaid 修复正文写回（section=' in src
        assert "章节已不存在，正文写回未生效" in src

    def test_charts_imports_safe_rowcount(self):
        """charts.py 从 app.db 导入 safe_rowcount（唯一出口）。"""
        src = _src("app/routers/charts.py")
        head = src[:4000]
        assert "from app.db import" in head and "safe_rowcount" in head


# ---------------------------------------------------------------------------
# C 组：同族完成度（全仓正文写点守卫族谱）
# ---------------------------------------------------------------------------
class TestFamilyCompleteness:
    def test_review_autofix_uses_exec_write(self):
        """review_autofix 正文写走 exec_write（严格模式既有，族完成标记）。"""
        src = _src("app/routers/review_autofix.py")
        assert "review_db.exec_write(" in src
        assert "UPDATE sections SET content=?" in src

    def test_consistency_repair_uses_exec_write(self):
        """consistency_repair 正文恢复写走 exec_write（R78 F-3 既有）。"""
        src = _src("app/routers/consistency_repair.py")
        assert "review_db.exec_write(" in src

    def test_no_bare_rowcount_in_changed_files(self):
        """改动文件不引入裸 .rowcount（R13 集中化扫描器口径自证）。"""
        import re
        for rel in ("app/routers/sse_handlers.py", "app/routers/charts.py"):
            src = _src(rel)
            offenders = [
                m.group(0) for m in re.finditer(r"\.\browcount\b", src)
            ]
            assert not offenders, f"{rel} 出现裸 rowcount: {offenders[:3]}"

    def test_persist_via_retry_db_op_unchanged(self):
        """retry_db_op 包装保留（瞬态重试语义不变，仅新增 0 行确定性异常）。"""
        body = TestSseContentWriteGuard()._closure_src()
        # 闭包体外紧随 retry_db_op 包装
        src = _src("app/routers/sse_handlers.py")
        idx = src.index("await retry_db_op(_persist_content_once")
        assert "max_retries=4" in src[idx:idx + 120]
