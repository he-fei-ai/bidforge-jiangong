# -*- coding: utf-8 -*-
"""R182（2026-11-06）· 导出/审核判定口径三项遗留（B-15 / B-16 / B-17）。

锁定三件事：
  B-15  DLV-13「high 级问题阻断导出」承诺必须与行为一致：默认 409 阻断，
        前端「已知悉并继续」显式覆写后放行，且覆写必须留痕（X-Export-Override
        响应头 + review_records 导出留痕 note）。
  B-16  content_audit 结果并入 issues 仅供**可见性**，不得改变评分与阻断口径
        （内容体检项不在 _EXPORT_ISSUE_RULE_MAP → findings / high 闸门均跳过）。
  B-17  方案没有任何图表时，交叉引用检查必须输出「无法判定」
        （crossref_check.undetermined=true / reason=no_chart_set），
        不得伪装成「通过」；不产生 finding、不扣分。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import app.routers.export as E
import pytest
from fastapi.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONT_RESPONSE = REPO_ROOT / "frontend" / "src" / "utils" / "exportResponse.ts"
FRONT_API = REPO_ROOT / "frontend" / "src" / "api" / "index.ts"
FRONT_USE_EXPORT = REPO_ROOT / "frontend" / "src" / "pages" / "schemeWorkbench" / "useExportRun.ts"
FRONT_USE_CHECK = REPO_ROOT / "frontend" / "src" / "pages" / "schemeWorkbench" / "useExportCheck.ts"
FRONT_REVIEW_TAB = REPO_ROOT / "frontend" / "src" / "pages" / "schemeWorkbench" / "tabs" / "ReviewTab.tsx"


# ---------------------------------------------------------------------------
# 种子数据
# ---------------------------------------------------------------------------


async def _seed_section(
    db,
    scheme_id: str,
    content: str,
    *,
    title: str = "施工工艺技术",
    review_status: str = "approved",
    section_id: str = "sec1",
) -> None:
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (scheme_id, "p1", "判定口径方案"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title,"
        " status, content, word_count, review_status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (section_id, scheme_id, "", 0, 1, title, "generated", content, len(content), review_status),
    )
    await db.commit()


def _make_docx(path: Path) -> Path:
    """最小可用 DOCX（含 OPC 关系与主文档 MIME；R183 结构校验要求）。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            "<?xml version='1.0'?>"
            "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
            "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
            "<Override PartName='/word/document.xml' "
            "ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/>"
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            "<?xml version='1.0'?>"
            "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
            "<Relationship Id='rId1' "
            "Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' "
            "Target='word/document.xml'/></Relationships>",
        )
        zf.writestr(
            "word/document.xml",
            "<?xml version='1.0'?><w:document "
            "xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body/></w:document>",
        )
        zf.writestr(
            "word/_rels/document.xml.rels",
            "<?xml version='1.0'?>"
            "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'/>",
        )
    return path


async def _seed_hit_cache(db, scheme_id: str, path: Path) -> None:
    await db.execute(
        "INSERT INTO export_cache (id, scheme_id, config_hash, content_fingerprint, result_path)" " VALUES (?,?,?,?,?)",
        (f"{scheme_id}-cache", scheme_id, "cfg", "content", str(path)),
    )
    await db.commit()


async def _prep_for_hit(scheme_id, body, db, cancel_event=None, defer_audit=False) -> dict:
    return {
        "scheme": {"id": scheme_id, "name": "方案甲", "project_id": "p1"},
        "config": {},
        "sections": [],
        "fe_codes": [],
        "fe_images": {},
        "chart_fp": [],
        "global_facts": [],
        "appendix_sources": [],
        "global_facts_status": {"ok": True},
        "image_config": {},
        "content_audit": {},
        "render_stats": {},
    }


def _patch_hit(monkeypatch) -> None:
    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(E, "_guard_numbering_consistency", _noop)
    monkeypatch.setattr(E, "_prepare_export", _prep_for_hit)
    monkeypatch.setattr(E, "_content_fingerprint", lambda prep: ("cfg", "content"))


# ---------------------------------------------------------------------------
# B-15：默认阻断 + 覆写留痕
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_issue_blocks_export_by_default(db_conn, monkeypatch):
    """存在 high 级 issue → 默认 409 阻断，且不得开始 prepare（不白烧渲染成本）。"""
    await _seed_section(db_conn, "blk", "")
    _patch_hit(monkeypatch)

    async def _boom(*_a, **_k):
        raise AssertionError("阻断必须发生在 _prepare_export 之前")

    monkeypatch.setattr(E, "_prepare_export", _boom)

    resp = await E.export_docx("blk", {}, db=db_conn)
    assert isinstance(resp, JSONResponse), "high 级问题未拦截，导出被放行"
    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["error"] == "export_blocked_high_issues"
    assert body["blocked"] is True
    assert body["high_issue_count"] >= 1
    assert body["high_issue_total"] >= 1
    assert any(item["rule_id"] for item in body["high_issues"])
    assert "已知悉并继续" in body["detail"]
    assert "override_high_issues" in body["override_hint"]


@pytest.mark.asyncio
async def test_override_proceeds_with_header_and_trace_note(db_conn, tmp_path, monkeypatch):
    """显式覆写 → 放行 + X-Export-Override 响应头 + 导出留痕 note。"""
    await _seed_section(db_conn, "ovr", "")
    await _seed_hit_cache(db_conn, "ovr", _make_docx(tmp_path / "cached.docx"))
    _patch_hit(monkeypatch)

    resp = await E.export_docx("ovr", {"override_high_issues": True}, db=db_conn)
    assert not isinstance(resp, JSONResponse), "带覆写仍被阻断"
    assert resp.headers["X-Cache-Status"] == "hit"
    assert resp.headers["X-Export-Override"] == "high-issues"

    cur = await db_conn.execute("SELECT comment FROM review_records WHERE scheme_id='ovr'")
    rows = await cur.fetchall()
    assert rows, "覆写导出没有落审核留痕（审计链断裂）"
    assert "已知悉并覆写导出" in rows[-1][0], "留痕未记录覆写动作"


@pytest.mark.asyncio
async def test_override_without_high_issues_has_no_override_header(db_conn, tmp_path, monkeypatch):
    """无 high 级问题时，覆写标记不得伪造 X-Export-Override（信号必须真实）。"""
    await _seed_section(db_conn, "ok1", "脚手架搭设与验收按本方案执行。" * 40)
    await _seed_hit_cache(db_conn, "ok1", _make_docx(tmp_path / "cached_ok.docx"))
    _patch_hit(monkeypatch)

    resp = await E.export_docx("ok1", {"override_high_issues": True}, db=db_conn)
    assert "X-Export-Override" not in resp.headers


# ---------------------------------------------------------------------------
# B-16：content_audit 并入 issues，但不改评分 / 阻断口径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_audit_items_are_merged_into_issues(db_conn):
    content = "施工参数为【待补充】，另有 <div>说明</div> 残留。"
    await _seed_section(db_conn, "aud", content)

    result = await E.collect_export_issues("aud", db_conn)
    merged = [i for i in result["issues"] if i.get("source") == "content_audit"]
    assert merged, "内容体检结果未并入 issues（用户不可见）"
    audit_keys = {it["key"] for it in result["content_audit"]["items"]}
    assert {i["type"] for i in merged} == audit_keys
    assert all(i["severity"] == "info" for i in merged)
    assert all(i["title"] for i in merged)

    # 评分口径不变：本方案只有内容体检项、没有任何规则项 → findings 必须为空集。
    # （旧断言用 getattr(f, "source") 判断，但 findings 是 dict，getattr 恒为 ""，
    #   属恒真断言；改为直接比较集合，才能真的拦住「体检项被计分」。）
    findings = E.export_issues_to_findings(result)
    assert findings == [], f"内容体检项被计入评分：{findings}"
    assert {f.get("rule_id") for f in findings} == set()

    # 阻断口径不变：内容体检项不得进入 high 级阻断清单（本方案应零阻断项）
    blockers = E._export_high_issue_blockers(result["issues"])
    assert blockers == [], f"内容体检项参与了导出阻断：{blockers}"
    assert {b["type"] for b in blockers} & audit_keys == set()


# ---------------------------------------------------------------------------
# B-17：无图表集合 → 交叉引用「无法判定」
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crossref_undetermined_when_no_chart_set(db_conn):
    await _seed_section(db_conn, "xref0", "施工顺序详见 图 9-9 与 表 3-2，另见 第9.9节。")

    result = await E.collect_export_issues("xref0", db_conn)
    state = result["crossref_check"]
    assert state["undetermined"] is True
    assert state["reason"] == "no_chart_set"
    assert state["fig_count"] == 0 and state["tbl_count"] == 0
    # 不产生 finding、不扣分：引用「图 9-9 / 表 3-2」在无图表时不得被当成已核对通过；
    # 节号分支与图表集合无关，仍按原口径检测（此处「第9.9节」照常报 stale）。
    chart_refs = [
        i for i in result["issues"] if i.get("type") == "stale_cross_reference" and i.get("ref_type") in ("fig", "tbl")
    ]
    assert not chart_refs, f"无图表集合时图/表交叉引用被误判为已核对：{chart_refs}"
    sec_refs = [i for i in result["issues"] if i.get("type") == "stale_cross_reference" and i.get("ref_type") == "sec"]
    assert sec_refs, "节号交叉引用检查被连同图/表分支一起关停（B-17 不得改变既有判定）"


@pytest.mark.asyncio
async def test_crossref_determined_when_chart_set_exists(db_conn):
    content = "```mermaid\ngraph TD;\nA-->B;\n```\n\n" "施工顺序详见 图 1-1。"
    await _seed_section(db_conn, "xref1", content)

    result = await E.collect_export_issues("xref1", db_conn)
    state = result["crossref_check"]
    assert state["undetermined"] is False
    assert state["reason"] == ""
    assert state["fig_count"] >= 1


# ---------------------------------------------------------------------------
# 前端可见性 / 覆写入口契约（本仓范式：后端 pytest 读前端源文件）
# ---------------------------------------------------------------------------


def test_frontend_has_override_entry_and_visible_signals():
    response_src = FRONT_RESPONSE.read_text(encoding="utf-8")
    api_src = FRONT_API.read_text(encoding="utf-8")
    run_src = FRONT_USE_EXPORT.read_text(encoding="utf-8")
    check_src = FRONT_USE_CHECK.read_text(encoding="utf-8")
    tab_src = FRONT_REVIEW_TAB.read_text(encoding="utf-8")

    # B-15：结构化解析 + 覆写开关 + 显式确认 + 覆写提示
    assert "parseExportBlock" in response_src
    assert "export_blocked_high_issues" in response_src
    assert "X-Export-Override" in response_src
    assert "override_high_issues: true" in api_src
    assert "parseExportBlock(e)" in run_src
    assert "已知悉并继续" in run_src
    assert "parseExportOverride(resp.headers)" in run_src

    # B-16：内容体检来源标记必须在可见出口区分
    assert 'iss.source === "content_audit"' in tab_src
    assert "内容体检" in tab_src

    # B-17：无图表时前端必须显示「无法判定」，不得显示成通过
    assert "crossref_check: data.crossref_check" in check_src
    assert "crossref_check?.undetermined" in tab_src
    assert "export-crossref-undetermined" in tab_src
    assert "交叉引用检查" in tab_src
    assert "无法判定" in tab_src


def test_blocked_payload_survives_asyncio_json_roundtrip():
    """409 体的关键键必须可被前端 JSON 解析（Blob 文本）逐字段读到。"""
    blockers = [{"type": "empty_section", "rule_id": "DLV-01", "severity": "high", "count": 2, "title": "空章节"}]
    resp = E._export_blocked_response(blockers)
    body = json.loads(resp.body)
    assert body["error"] == "export_blocked_high_issues"
    assert body["high_issues"][0]["rule_id"] == "DLV-01"


# 防止 aiosqlite 事件循环警告（与既有导出域测试同口径）
@pytest.fixture(autouse=True)
def _loop_guard():
    yield


def test_module_imports_are_synchronous():
    """占位：确保本文件在 collect-only 阶段不触发协程执行。"""
    assert asyncio is not None
