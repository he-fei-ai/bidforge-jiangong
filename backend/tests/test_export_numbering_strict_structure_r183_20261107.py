# -*- coding: utf-8 -*-
"""R183（2026-11-07）：严格模式 409 必须覆盖「结构编号漂移」。

已取证缺陷（目录模块审计结论 2）：
    `_export_cache.py` 的严格模式守卫只调用
    `validate_scheme_numbering_consistency`（正文子标题 vs outline_json.id），
    完全不看 outline_json.id 是否等于该章节在真实树中的位置。
    因此「第 2 章存储编号被改成 1」这类结构漂移：
      · 导出成稿不受影响（export.py 用 heading_gen 结构计数器重算）；
      · 但严格模式 409 30/30 全部放行 —— 该拦的没拦。
    本用例把该形状固化为回归：strict=True 时必须 409，且报告需暴露结构漂移。

修复口径（只读复用，不复制实现）：
    复用 `app.routers.sections._outline_structure_report`（提交 aac9955），
    它与 `renumber_sections_after_reorder` 同一算法（`_build_tree` +
    `renumber_section_outline_ids`），因此「守卫判定」与「导出重算」同源。
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from app.config import settings
from app.routers.export import _guard_numbering_consistency

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _seed_scheme(db_conn, sections):
    await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("s1", "p1", "测试专项方案"))
    for sec in sections:
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, "
            "title, status, outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sec["id"],
                "s1",
                sec.get("parent_id", ""),
                sec["sort_order"],
                sec["level"],
                sec["title"],
                "generated",
                json.dumps(sec["outline"], ensure_ascii=False),
                sec["content"],
            ),
        )
    await db_conn.commit()


def _drifted_rows():
    """等价样本：两章均存 id=1；第 2 章正确结构编号应为 2。

    正文子标题写的是 `## 1.1`，与陈旧 outline id 对齐 —— 所以旧校验器
    （只看正文 vs outline_json.id）必然判绿，而真实树位置是 1 / 2。
    """
    return [
        {
            "id": "c1",
            "sort_order": 0,
            "level": 1,
            "title": "第一章 总体概述",
            "outline": {"id": "1", "level": 1},
            "content": "## 1.1 总体安排\n干净正文。\n",
        },
        {
            "id": "c2",
            "sort_order": 1,
            "level": 1,
            "title": "第二章 施工工艺",
            "outline": {"id": "1", "level": 1},
            "content": "## 1.1 施工安排\n漂移正文。\n",
        },
    ]


def _clean_rows():
    return [
        {
            "id": "c1",
            "sort_order": 0,
            "level": 1,
            "title": "第一章 总体概述",
            "outline": {"id": "1", "level": 1},
            "content": "## 1.1 总体安排\n干净正文。\n",
        },
        {
            "id": "c2",
            "sort_order": 1,
            "level": 1,
            "title": "第二章 施工工艺",
            "outline": {"id": "2", "level": 1},
            "content": "## 2.1 施工安排\n干净正文。\n",
        },
    ]


async def test_precondition_body_only_checker_is_green_on_drift(db_conn):
    """先证伪：旧校验器在等价样本上必须判绿，证明盲区真实存在。"""
    from app.services.numbering import validate_scheme_numbering_consistency

    await _seed_scheme(db_conn, _drifted_rows())
    rep = await validate_scheme_numbering_consistency(db_conn, "s1")
    assert rep["consistent"] is True
    assert rep["mismatched"] == 0


async def test_structure_report_detects_drift(db_conn):
    """结构自检必须能看出第 2 章存储编号 1 ≠ 树位置 2。"""
    from app.routers.sections import _outline_structure_report

    await _seed_scheme(db_conn, _drifted_rows())
    rep = await _outline_structure_report(db_conn, "s1")
    assert rep["consistent"] is False
    assert rep["mismatched"] == 1
    assert rep["samples"][0]["stored"] == "1"
    assert rep["samples"][0]["expected"] == "2"


async def test_strict_blocks_structure_drift(db_conn, monkeypatch):
    """strict=True：结构编号漂移必须 409（修复前此用例为红）。"""
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "numbering_consistency_strict", True)
    await _seed_scheme(db_conn, _drifted_rows())
    with pytest.raises(HTTPException) as exc:
        await _guard_numbering_consistency(db_conn, "s1")
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["error"] == "numbering_inconsistency"
    assert detail["outline"]["mismatched"] == 1
    assert detail["outline"]["consistent"] is False
    assert detail["outline"]["samples"][0]["expected"] == "2"


async def test_strict_blocks_all_30_drift_samples(db_conn, monkeypatch):
    """实测口径：30 组等价结构漂移样本，严格模式必须 30/30 阻断。"""
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "numbering_consistency_strict", True)
    rows = [
        {
            "id": "c%02d" % i,
            "sort_order": i,
            "level": 1,
            "title": "第%d章 章节" % (i + 1),
            # 全部写成陈旧编号 0；正文也按 0.1 重写 → 旧校验器 30/30 判绿。
            "outline": {"id": "0", "level": 1},
            "content": "## 0.1 小节\n正文。\n",
        }
        for i in range(30)
    ]
    await _seed_scheme(db_conn, rows)
    with pytest.raises(HTTPException) as exc:
        await _guard_numbering_consistency(db_conn, "s1")
    assert exc.value.status_code == 409
    assert exc.value.detail["outline"]["mismatched"] == 30


async def test_strict_passes_clean_scheme(db_conn, monkeypatch):
    """strict=True：结构干净（id 与树位置一致）时不得误伤。"""
    monkeypatch.setattr(settings, "numbering_consistency_strict", True)
    await _seed_scheme(db_conn, _clean_rows())
    await _guard_numbering_consistency(db_conn, "s1")  # 不抛即通过


async def test_non_strict_still_passes_structure_drift(db_conn, monkeypatch):
    """默认非严格：结构漂移只告警，不阻断导出（保持向后兼容）。"""
    monkeypatch.setattr(settings, "numbering_consistency_strict", False)
    await _seed_scheme(db_conn, _drifted_rows())
    await _guard_numbering_consistency(db_conn, "s1")


async def test_guard_reuses_sections_report_not_a_copy(monkeypatch, db_conn):
    """守卫必须调用 sections._outline_structure_report，而不是本地复制实现。"""
    import app.routers.sections as sections_mod

    called = {"n": 0}
    original = sections_mod._outline_structure_report

    async def _spy(db, scheme_id):
        called["n"] += 1
        return await original(db, scheme_id)

    monkeypatch.setattr(sections_mod, "_outline_structure_report", _spy)
    monkeypatch.setattr(settings, "numbering_consistency_strict", True)
    await _seed_scheme(db_conn, _clean_rows())
    await _guard_numbering_consistency(db_conn, "s1")
    assert called["n"] == 1


def test_export_cache_has_no_local_structure_report_copy():
    """静态护栏：结构自检的唯一实现留在 sections.py，导出侧只允许只读复用。"""
    here = os.path.dirname(os.path.abspath(__file__))
    backend = os.path.dirname(here)
    src_path = os.path.join(backend, "app", "routers", "_export_cache.py")
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    assert "def _outline_structure_report" not in src
    assert "from app.routers.sections import _outline_structure_report" in src


async def test_self_check_exception_is_visible_not_green(db_conn, monkeypatch, caplog):
    """自检抛异常：**不阻断导出**，但结论必须 checked=false 且带原因。

    ✅ R183-补（2026-11-07）：本函数是第三个调用 `_outline_structure_report`
    的位置（目录路由 / 审核 DLV-13 / 导出）。旧实现在这里只写 debug 日志 →
    响应中没有任何痕迹，"没检查"被当成"没问题"。本用例锁死该形状：
    异常 → `outline.checked=false`、`consistent=null`、reason 可读、WARNING 可见。
    """
    import app.routers.sections as sections_mod

    async def _boom(db, scheme_id):
        raise RuntimeError("模拟结构自检崩溃")

    monkeypatch.setattr(sections_mod, "_outline_structure_report", _boom)
    monkeypatch.setattr(settings, "numbering_consistency_strict", True)
    await _seed_scheme(db_conn, _clean_rows())
    with caplog.at_level("WARNING"):
        rep = await _guard_numbering_consistency(db_conn, "s1")  # 不抛 = 不阻断导出
    assert rep["outline"]["checked"] is False
    assert rep["outline"]["consistent"] is None
    assert "模拟结构自检崩溃" in rep["outline"]["reason"]
    assert any(r.levelname == "WARNING" and "自检失败" in r.getMessage() for r in caplog.records)


async def test_self_check_symbol_missing_is_visible(db_conn, monkeypatch):
    """符号不可用（改名/下沉）同样必须可见，不得静默当成结构一致。"""
    import app.routers.sections as sections_mod

    monkeypatch.delattr(sections_mod, "_outline_structure_report")
    monkeypatch.setattr(settings, "numbering_consistency_strict", True)
    await _seed_scheme(db_conn, _clean_rows())
    rep = await _guard_numbering_consistency(db_conn, "s1")
    assert rep["outline"]["checked"] is False
    assert rep["outline"]["consistent"] is None
    assert "不可用" in rep["outline"]["reason"]


def test_numbering_check_header_exposes_unchecked_reason():
    """响应头必须把「未检查」下发（latin-1 安全），不能只有 debug 日志。"""
    from urllib.parse import unquote

    from app.routers.export import _numbering_check_headers

    raw = _numbering_check_headers(
        {"outline": {"checked": False, "consistent": None, "reason": "结构自检执行异常：boom"}}
    )["X-Numbering-Check"]
    payload = json.loads(unquote(raw))
    assert payload["checked"] is False
    assert payload["consistent"] is None
    assert "boom" in payload["reason"]
    # 非严格模式（返回 {}）不发结论头，避免把「未自检」包装成任何结论
    assert _numbering_check_headers({}) == {}


def test_docx_and_pdf_all_return_paths_wire_numbering_header():
    """DOCX/PDF × 缓存命中/未命中 共 4 个返回分支都必须下发自检结论头。

    只测守卫返回值不够 —— 任一返回分支漏 merge，用户侧依旧"看不到结论"。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(os.path.dirname(here), "app", "routers", "export.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("**_numbering_check_headers(_num_check)") == 4

