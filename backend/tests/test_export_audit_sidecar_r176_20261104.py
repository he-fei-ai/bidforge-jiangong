"""R176 · D2-2：内容体检结果旁车（``<产物>.audit.json``）行为锁定。

覆盖派单要求的六类场景 + 路由级「命中不再跑 audit_content」：
命中复用 / 缺失回退 / 损坏回退 / 内容变更不复用 / 三处校验失配不复用 /
prune 与孤儿回收连带删除 / 旁车结论与实时重算逐字段相同。

旁车是**纯加速层**：任何异常都必须回退实时体检，绝不能抛错，也绝不能把
坏数据当结论（否则命中路径会静默少发/错发 X-Content-Audit）。
"""

import asyncio
import json
import zipfile
from pathlib import Path

import app.routers.export as E
import pytest


def _make_docx(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    return path


def _prep(content: str = "```mermaid\nflowchart TD\nA-->B\n", *, fences_fixed: int = 0, n: int = 1) -> dict:
    return {
        "sections": [
            {"id": f"s{i}", "parent_id": "", "sort_order": i, "level": 1, "title": f"第{i}章", "content": content}
            for i in range(n)
        ],
        "fences_fixed": fences_fixed,
    }


def _audit_of(prep: dict) -> dict:
    return E.audit_content(prep["sections"], fences_fixed=prep["fences_fixed"])


@pytest.mark.asyncio
async def test_sidecar_reused_on_hit_without_rerun(tmp_path, monkeypatch):
    """旁车命中：结论与实时重算逐字段相同，且不再调用 audit_content。"""
    artifact = _make_docx(tmp_path / "a.docx")
    prep = _prep()
    expected = _audit_of(prep)

    await E._write_audit_sidecar(artifact, {**prep, "content_audit": expected}, "h1")
    got = await E._read_audit_sidecar(artifact, prep, "h1")
    assert got == expected, "旁车结论与实时重算不一致"

    def _boom(*_a, **_k):
        raise AssertionError("命中路径不应再跑 audit_content")

    monkeypatch.setattr(E, "audit_content", _boom)
    via_resolve = await E._resolve_content_audit(prep, content_hash="h1", cached_path=artifact)
    assert via_resolve == expected


@pytest.mark.asyncio
async def test_sidecar_missing_falls_back_to_recompute(tmp_path, monkeypatch):
    artifact = _make_docx(tmp_path / "b.docx")
    prep = _prep()
    calls = {"n": 0}
    real = E.audit_content

    def _count(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(E, "audit_content", _count)
    got = await E._resolve_content_audit(prep, content_hash="h2", cached_path=artifact)
    assert calls["n"] == 1
    assert got == _audit_of(prep)


@pytest.mark.asyncio
async def test_corrupt_sidecar_falls_back_to_recompute(tmp_path, monkeypatch):
    """半截 JSON 必须静默回退重算（不抛错、不采用坏数据）。"""
    artifact = _make_docx(tmp_path / "c.docx")
    sidecar = E._audit_sidecar_path(artifact)
    full = json.dumps(
        {
            "v": E._AUDIT_SIDECAR_VERSION,
            "content_hash": "h3",
            "fences_fixed": 0,
            "sections": 1,
            "audit": _audit_of(_prep()),
        },
        ensure_ascii=False,
    )
    sidecar.write_text(full[: len(full) // 2], encoding="utf-8")

    prep = _prep()
    got = await E._resolve_content_audit(prep, content_hash="h3", cached_path=artifact)
    assert got == _audit_of(prep)
    assert await E._read_audit_sidecar(artifact, prep, "h3") is None


@pytest.mark.asyncio
async def test_sidecar_bad_field_type_falls_back(tmp_path):
    """合法 JSON 但字段类型畸形（"abc" 转 int 失败）同样回退，不抛错。"""
    artifact = _make_docx(tmp_path / "c2.docx")
    sidecar = E._audit_sidecar_path(artifact)
    sidecar.write_text(
        json.dumps(
            {
                "v": E._AUDIT_SIDECAR_VERSION,
                "content_hash": "h4",
                "fences_fixed": "abc",
                "sections": 1,
                "audit": {"items": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prep = _prep()
    assert await E._read_audit_sidecar(artifact, prep, "h4") is None
    assert await E._resolve_content_audit(prep, content_hash="h4", cached_path=artifact) == _audit_of(prep)


@pytest.mark.asyncio
async def test_content_change_not_reused(tmp_path):
    """内容指纹变化后旁车不复用（旧结论不得套到新内容上）。"""
    artifact = _make_docx(tmp_path / "d.docx")
    prep = _prep("旧内容")
    await E._write_audit_sidecar(artifact, {**prep, "content_audit": _audit_of(prep)}, "oldhash")
    assert await E._read_audit_sidecar(artifact, prep, "newhash") is None


@pytest.mark.asyncio
async def test_fences_and_section_count_mismatch_not_reused(tmp_path):
    """围栏修复数 / 章节数变化同样会让旁车作废（它们改变体检结论）。"""
    artifact = _make_docx(tmp_path / "e.docx")
    prep = _prep()
    await E._write_audit_sidecar(artifact, {**prep, "content_audit": _audit_of(prep)}, "h5")
    assert await E._read_audit_sidecar(artifact, _prep(fences_fixed=1), "h5") is None
    assert await E._read_audit_sidecar(artifact, _prep(n=2), "h5") is None


@pytest.mark.asyncio
async def test_version_bump_invalidates_sidecar(tmp_path):
    artifact = _make_docx(tmp_path / "f.docx")
    prep = _prep()
    await E._write_audit_sidecar(artifact, {**prep, "content_audit": _audit_of(prep)}, "h6")
    data = json.loads(E._audit_sidecar_path(artifact).read_text(encoding="utf-8"))
    data["v"] = E._AUDIT_SIDECAR_VERSION + 1
    E._audit_sidecar_path(artifact).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert await E._read_audit_sidecar(artifact, prep, "h6") is None


@pytest.mark.asyncio
async def test_sidecar_deleted_with_artifact(tmp_path):
    """删产物必连带删旁车（prune / 孤儿回收共用同一删除出口）。"""
    artifact = _make_docx(tmp_path / "g.docx")
    prep = _prep()
    await E._write_audit_sidecar(artifact, {**prep, "content_audit": _audit_of(prep)}, "h7")
    assert E._audit_sidecar_path(artifact).exists()
    E._remove_export_artifact(artifact)
    assert not artifact.exists()
    assert not E._audit_sidecar_path(artifact).exists()


@pytest.mark.asyncio
async def test_gc_keeps_live_sidecar_and_removes_orphan_sidecar(db_conn, tmp_path, monkeypatch):
    await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("sc3", "p1", "方案甲"))
    monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
    live = _make_docx(tmp_path / "sc3_live.docx")
    orphan = _make_docx(tmp_path / "sc3_gone.docx")
    await db_conn.execute(
        "INSERT INTO export_cache (id, scheme_id, config_hash," " content_fingerprint, result_path) VALUES (?,?,?,?,?)",
        ("sc3-c", "sc3", "cfg", "h9", str(live)),
    )
    await db_conn.commit()

    prep = _prep()
    for p in (live, orphan):
        await E._write_audit_sidecar(p, {**prep, "content_audit": _audit_of(prep)}, "h8")
    await E._gc_orphan_exports(db_conn, "sc3")
    assert E._audit_sidecar_path(live).exists(), "存活产物的旁车被误删"
    assert not orphan.exists()
    assert not E._audit_sidecar_path(orphan).exists()


@pytest.mark.asyncio
async def test_sidecar_without_cache_row_never_used(db_conn, tmp_path, monkeypatch):
    """旁车单独存在（无缓存行）时不得被沿用：未命中必然实时重算。"""
    await db_conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("sc1", "p1", "方案甲"))
    await db_conn.commit()
    stale = _make_docx(tmp_path / "stale.docx")
    prep = _prep()
    await E._write_audit_sidecar(stale, {**prep, "content_audit": _audit_of(prep)}, "abc12345")

    calls = {"n": 0}
    real = E.audit_content

    def _count(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    def _fake_build(path):
        _make_docx(Path(path))
        return {"failed": 0}

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(E, "_prepare_export", _prep_for_route("abc12345"))
    monkeypatch.setattr(E, "_content_fingerprint", lambda _p: ("cfg", "abc12345"))
    monkeypatch.setattr(E, "audit_content", _count)
    monkeypatch.setattr(E, "_guard_numbering_consistency", _noop)
    monkeypatch.setattr(E, "_prepare_export_render", _noop)
    monkeypatch.setattr(E, "_build_docx_task", lambda out_path, _p: (str(out_path),))
    monkeypatch.setattr(E, "_build_docx_sync", _fake_build)
    monkeypatch.setattr(E, "_persist_export_cache_row", _noop)
    monkeypatch.setattr(E, "_finalize_export_delivery", _noop)

    resp = await E.export_docx("sc1", {"override_high_issues": True}, db_conn)
    assert resp.headers["X-Cache-Status"] == "miss"
    assert calls["n"] == 1, "无缓存行却沿用了孤立旁车"


@pytest.mark.asyncio
async def test_hit_route_reads_sidecar_and_skips_audit(db_conn, tmp_path, monkeypatch):
    """命中路由级：旁车可用时 audit_content 一次都不跑，X-Content-Audit 照发。"""
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name, export_round)" " VALUES (?,?,?,?)", ("sc2", "p1", "方案甲", 0)
    )
    cached = _make_docx(tmp_path / "cached.docx")
    await db_conn.execute(
        "INSERT INTO export_cache (id, scheme_id, config_hash," " content_fingerprint, result_path) VALUES (?,?,?,?,?)",
        ("sc2-c", "sc2", "cfg", "abc12345", str(cached)),
    )
    await db_conn.commit()

    prep = _prep()
    await E._write_audit_sidecar(cached, {**prep, "content_audit": _audit_of(prep)}, "abc12345")

    def _boom(*_a, **_k):
        raise AssertionError("命中路径不应再跑 audit_content")

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(E, "_prepare_export", _prep_for_route("abc12345"))
    monkeypatch.setattr(E, "_content_fingerprint", lambda _p: ("cfg", "abc12345"))
    monkeypatch.setattr(E, "audit_content", _boom)
    monkeypatch.setattr(E, "_finalize_export_delivery", _noop)

    resp = await E.export_docx("sc2", {"override_high_issues": True}, db_conn)
    assert resp.headers["X-Cache-Status"] == "hit"
    assert "X-Content-Audit" in resp.headers, "命中路径静默丢了审计头"
    assert resp.headers["X-Export-Round"] == "1"


@pytest.mark.asyncio
async def test_sidecar_payload_carries_rule_fingerprint(tmp_path):
    """载荷必须带当前审计规则指纹（R177：失效判据不再靠人工 bump 版本号）。"""
    artifact = _make_docx(tmp_path / "fp.docx")
    prep = _prep()
    await E._write_audit_sidecar(artifact, {**prep, "content_audit": _audit_of(prep)}, "hf")
    data = json.loads(E._audit_sidecar_path(artifact).read_text(encoding="utf-8"))
    assert data["audit_fp"] == E.audit_rules_fingerprint()
    assert data["v"] == E._AUDIT_SIDECAR_VERSION


@pytest.mark.asyncio
async def test_rule_change_invalidates_sidecar_and_recomputes(tmp_path, monkeypatch):
    """**真的改一处审计规则**（运行时替换规则表，验证后由 monkeypatch 自动恢复）：
    指纹必须变化、旧旁车必须失效、且回退重算确实按新规则产出。"""
    artifact = _make_docx(tmp_path / "fp2.docx")
    prep = _prep()
    await E._write_audit_sidecar(artifact, {**prep, "content_audit": _audit_of(prep)}, "hr")
    assert await E._read_audit_sidecar(artifact, prep, "hr") is not None

    before = E.audit_rules_fingerprint()
    real_rules = E._AUDIT_RULES
    new_rule = ("r177_probe", "R177 临时规则", "error", E.re.compile(r"R177PROBE"))
    monkeypatch.setattr(E, "_AUDIT_RULES", real_rules + (new_rule,))

    # 1) 规则确实改了：audit_content 能命中新规则
    probe_prep = {
        "sections": [
            {"id": "s1", "parent_id": "", "sort_order": 0, "level": 1, "title": "第1章", "content": "R177PROBE"}
        ],
        "fences_fixed": 0,
    }
    assert [it["key"] for it in E.audit_content(probe_prep["sections"])["items"]] == ["r177_probe"]

    # 2) 指纹随之变化，旧旁车读侧失效
    assert E.audit_rules_fingerprint() != before
    assert await E._read_audit_sidecar(artifact, prep, "hr") is None

    # 3) 回退路径（不是抛错、也不是沿用旧结论）
    got = await E._resolve_content_audit(probe_prep, content_hash="hr", cached_path=artifact)
    assert got == E.audit_content(probe_prep["sections"], fences_fixed=0)


@pytest.mark.asyncio
async def test_comment_change_does_not_invalidate_sidecar(tmp_path):
    """**真的改一处注释**（临时改源码，finally 原样恢复）：指纹必须不变、
    旁车继续可用 —— 实现是 AST 归一化，对注释/空行/换行风格不敏感。"""
    import linecache

    import app.routers._export_issue_scan as S

    src_path = Path(S.__file__)
    original = src_path.read_bytes()
    artifact = _make_docx(tmp_path / "fp3.docx")
    prep = _prep()
    await E._write_audit_sidecar(artifact, {**prep, "content_audit": _audit_of(prep)}, "hc")
    before = S.audit_rules_fingerprint()

    try:
        text = original.decode("utf-8")
        anchor = "    hits: dict[str, dict] = {}"
        assert text.count(anchor) == 1, "锚点漂移：请更新本用例的注入位置"
        idx = text.index(anchor)
        text = text[:idx] + "    # R177-FP-PROBE 这是一行临时注释\r\n" + text[idx:]
        src_path.write_bytes(text.encode("utf-8"))
        linecache.clearcache()

        after = S.audit_rules_fingerprint()
        assert after == before, "改注释不应让旁车失效（AST 应忽略注释）"
        assert await E._read_audit_sidecar(artifact, prep, "hc") is not None
    finally:
        src_path.write_bytes(original)
        linecache.clearcache()

    assert S.audit_rules_fingerprint() == before


def _prep_for_route(content_hash: str):
    async def _fake_prep(scheme_id, body, db, cancel_event=None, defer_audit=False):
        return {
            "scheme": {"id": scheme_id, "name": "方案甲", "project_id": "p1"},
            "config": {},
            "sections": _prep()["sections"],
            "fe_codes": [],
            "fe_images": {},
            "chart_fp": [],
            "global_facts": [],
            "appendix_sources": [],
            "render_stats": {},
            "content_audit": None,
            "fences_fixed": 0,
        }

    return _fake_prep
