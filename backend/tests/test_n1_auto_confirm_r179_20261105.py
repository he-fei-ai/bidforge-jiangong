"""R179 N1（2026-11-05）目录生成自动确认 · 回归护栏。

覆盖：
  1. evaluate 硬前置四类 + no_outline；
  2. **已存在正文的项目永不自动确认**（用户点名的红线用例）；
  3. M1 undetermined 不阻塞（evaluate 不读取评分项状态，纯四类判据）；
  4. save-outline `snapshot_before` 同一事务建快照、保留 5 条；
  5. 回退恢复结构/正文/review_status，**强制失效**导出与一致性缓存、重挂图表；
  6. 治理开关 kv 默认关 + PATCH 往返（零迁移）。
"""

import json

import pytest
from app.routers import sections as sec
from app.routers import system as sys_router
from app.services import outline_auto_confirm as oac
from app.services import outline_versions as ov

CLEAN_TITLES = ("基坑支护及降水施工", "土方开挖与边坡监测")


async def _seed(db, sid: str = "s1", pid: str = "p1") -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)", (sid, pid, "s", "草稿"))
    await db.commit()


def _outline(titles=CLEAN_TITLES) -> list[dict]:
    return [
        {"title": titles[0], "children": [{"title": "1.1 支护结构选型"}, {"title": "1.2 降水井布置"}]},
        {"title": titles[1], "children": [{"title": "2.1 开挖分层"}, {"title": "2.2 监测点布设"}]},
    ]


# ---------------------------------------------------------------- evaluate
def test_n1_evaluate_ready_on_clean_outline():
    out = oac.evaluate(
        _outline(), {"generic_titles": [], "numbering_mismatch": [], "level_gaps": [], "empty_parents": []}
    )
    assert out["ready"] is True
    assert out["blockers"] == []


def test_n1_evaluate_placeholder_titles_block():
    out = oac.evaluate(_outline(("总体方案", "总体部署")))
    assert out["ready"] is False
    assert {b["code"] for b in out["blockers"]} == {"placeholder_titles"}


def test_n1_evaluate_numbering_block():
    review = {"numbering_mismatch": [{"a": 1}], "level_gaps": [{"b": 2}]}
    out = oac.evaluate(_outline(), review)
    assert out["ready"] is False
    assert any(b["code"] == "numbering_invalid" and b["count"] == 2 for b in out["blockers"])


def test_n1_evaluate_empty_parent_block():
    outline = [{"title": "基坑支护及降水施工", "children": []}, {"title": "土方开挖与边坡监测", "children": []}]
    out = oac.evaluate(outline)
    # 两个一级章无子项 → 空壳（empty_parents 口径）
    assert out["ready"] is False
    assert {b["code"] for b in out["blockers"]} == {"empty_chapters"}


def test_n1_evaluate_failed_chapters_block():
    out = oac.evaluate(_outline(), failed_chapters=[{"title": "x"}])
    assert out["ready"] is False
    assert {b["code"] for b in out["blockers"]} == {"failed_sections"}


def test_n1_evaluate_no_outline_block():
    out = oac.evaluate([])
    assert out["ready"] is False
    assert out["blockers"][0]["code"] == "no_outline"


def test_n1_existing_content_never_auto_confirms():
    """红线：已存在正文的项目永不自动确认（哪怕目录完全干净）。"""
    out = oac.evaluate(_outline(), has_content=True)
    assert out["ready"] is False
    assert {b["code"] for b in out["blockers"]} == {"existing_content"}


def test_n1_undetermined_does_not_block():
    """M1 undetermined 不阻塞：evaluate 只认四类硬前置，不读评分项状态。"""
    out = oac.evaluate(_outline(), {"scoring_status": "undetermined", "undetermined_reason": "empty_items"})
    assert out["ready"] is True


async def test_n1_scheme_has_content(db_conn):
    await _seed(db_conn)
    await db_conn.execute(
        "INSERT INTO sections(id,scheme_id,title,level,word_count,sort_order) VALUES('a','s1','甲',1,0,0)"
    )
    await db_conn.commit()
    assert await oac.scheme_has_content(db_conn, "s1") is False
    await db_conn.execute("UPDATE sections SET word_count=120, content='正文' WHERE id='a'")
    await db_conn.commit()
    assert await oac.scheme_has_content(db_conn, "s1") is True


# --------------------------------------------------------------- 快照 / 回退
async def test_n1_save_outline_snapshot_before_and_restore(db_conn, monkeypatch):
    await _seed(db_conn)
    await sec._save_outline_to_db(db_conn, "s1", _outline())
    # 写入正文与 review_status（按 id 更新，避免被编号重写后的标题不匹配）
    cur = await db_conn.execute("SELECT id FROM sections WHERE scheme_id='s1' AND level=2 ORDER BY sort_order LIMIT 1")
    row = await cur.fetchone()
    _with_content_id = row["id"]
    await db_conn.execute("UPDATE sections SET content='原正文', word_count=3 WHERE id=?", (_with_content_id,))
    await db_conn.execute("UPDATE schemes SET review_status='approved' WHERE id='s1'")
    await db_conn.commit()

    res = await sec._save_outline_to_db(
        db_conn,
        "s1",
        [{"title": "完全新目录", "children": [{"title": "新子项"}]}],
        snapshot_before="outline_auto",
    )
    assert res.get("snapshot_id")
    cur = await db_conn.execute("SELECT type, sections FROM scheme_snapshots WHERE id=?", (res["snapshot_id"],))
    row = await cur.fetchone()
    assert row["type"] == "outline_auto"
    payload = json.loads(row["sections"])
    assert payload["review_status"] == "approved"
    assert any(s["content"] == "原正文" for s in payload["sections"])

    # 回退：恢复结构 + 正文 + review_status
    calls = {"export": 0, "consistency": 0, "charts": 0}

    async def _fake_export(db, scheme_id):
        calls["export"] += 1

    async def _fake_consistency(db, scheme_id):
        calls["consistency"] += 1

    async def _fake_charts(db, scheme_id, section_id, content):
        calls["charts"] += 1
        return (1, content)

    monkeypatch.setattr("app.services.facts_extractor.invalidate_export_cache", _fake_export)
    monkeypatch.setattr(sec, "invalidate_consistency_scan_cache", _fake_consistency)
    # 路由顶层已绑定 resync_inline_charts，patch 绑定名（与调用点同一对象）
    monkeypatch.setattr(sec, "resync_inline_charts", _fake_charts)

    # ✅ R179（2026-11-05）：失效与重挂编排在路由层（services 不得依赖 routers），
    #    故此处走 `/outline-versions/{id}/restore` 的真实端到端出口。
    out = await sec.restore_outline_snapshot("s1", res["snapshot_id"], db_conn)
    assert out["ok"] is True and out["restored_sections"] == 6
    assert calls == {"export": 1, "consistency": 1, "charts": 1}  # 只有 1 章带正文
    cur = await db_conn.execute("SELECT title, content FROM sections WHERE scheme_id='s1' ORDER BY sort_order, id")
    rows = await cur.fetchall()
    snap_titles = [s["title"] for s in payload["sections"]]
    assert sorted(r["title"] for r in rows) == sorted(snap_titles)
    assert any(r["content"] == "原正文" for r in rows)
    cur = await db_conn.execute("SELECT review_status FROM schemes WHERE id='s1'")
    assert (await cur.fetchone())["review_status"] == "approved"


async def test_n1_snapshot_keeps_last_five(db_conn):
    await _seed(db_conn)
    for i in range(7):
        await ov.create_outline_snapshot(db_conn, "s1", snapshot_type="outline_auto")
    listed = await ov.list_snapshots(db_conn, "s1", limit=10)
    assert len(listed) == ov.KEEP_SNAPSHOTS


async def test_n1_restore_rejects_foreign_snapshot(db_conn):
    await _seed(db_conn)
    await _seed(db_conn, sid="s2", pid="p2")
    sid = await ov.create_outline_snapshot(db_conn, "s1", snapshot_type="outline_auto")
    with pytest.raises(ValueError):
        await ov.restore_outline_snapshot(db_conn, "s2", sid)


# --------------------------------------------------------------- 治理开关
async def test_n1_governance_switch_default_off_and_roundtrip(db_conn, monkeypatch):
    monkeypatch.setattr(sys_router, "_governance_kv_load", lambda: _kv(db_conn))
    monkeypatch.setattr(sys_router, "_governance_kv_save", lambda kv: _kv_save(db_conn, kv))
    assert (await sys_router.outline_auto_confirm_get())["enabled"] is False
    resp = await sys_router.outline_auto_confirm_put(sys_router.OutlineAutoConfirmSettings(enabled=True))
    assert resp["enabled"] is True
    assert (await sys_router.outline_auto_confirm_get())["enabled"] is True


async def _kv(db):
    cur = await db.execute("SELECT key, value FROM governance_settings")
    return {str(k): str(v) for k, v in await cur.fetchall()}


async def _kv_save(db, kv):
    for k, v in kv.items():
        await db.execute(
            "INSERT INTO governance_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, v),
        )
    await db.commit()


async def test_n1_countdown_setting_is_ops_only():
    from app.config import settings

    assert settings.outline_auto_confirm_delay_ms == 8000


@pytest.fixture(autouse=True)
async def _ensure_kv_table(db_conn):
    await db_conn.execute("CREATE TABLE IF NOT EXISTS governance_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    await db_conn.commit()
    yield
