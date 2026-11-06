"""R179 降级加固回归（2026-11-05）。

覆盖本轮 4 项高风险降级修复的正例 + 反例（改坏被防行为必须变红）：
  1. T-01 全局事实「同名可注入行」唯一性闸门（resolve_fact / resolve_conflict）；
  2. 章节手动保存的围栏补齐计数回传（fence_fixed / fence_swallowed_lines）；
  3. AI 场景路由列表回传配置启停态（enabled，防「已路由」承诺落空）；
  4. 图表清单占位标题回退 + 生成/导出引导语判据同源。
"""

import json
import uuid

import app.routers.global_facts as gf
import pytest
from app.routers import charts as charts_mod
from app.routers import sections as sec
from app.routers._chart_pipeline import _drop_dangling_lead_in
from app.routers.ai_config import scene_routes as sr
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------


async def _seed_scope(db, pid: str = "p1", sid: str = "s1") -> None:
    cur = await db.execute("SELECT 1 FROM projects WHERE id=?", (pid,))
    if not await cur.fetchone():
        await db.execute("INSERT INTO projects (id, name) VALUES (?,?)", (pid, "项目A"))
    cur = await db.execute("SELECT 1 FROM schemes WHERE id=?", (sid,))
    if not await cur.fetchone():
        await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "专项方案"))
    await db.commit()


async def _insert_fact(
    db,
    fid: str,
    *,
    title: str = "基坑深度",
    value: str = "12.0m",
    resolved: int = 0,
    conflict: int = 0,
    sim: int = 0,
    stale: int = 0,
    sid: str = "s1",
    pid: str = "p1",
    conflict_keys: str = "",
) -> str:
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, group_title,"
        " title, content, category, source_ref, is_simulated, confidence,"
        " is_resolved, has_conflict, conflict_keys, fact_key, is_stale) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            pid,
            sid,
            "g1",
            "技术参数",
            title,
            gf._build_fact_content(title, value, bool(sim)),
            "tech_param",
            "",
            sim,
            0.9,
            resolved,
            conflict,
            conflict_keys,
            "depth",
            stale,
        ),
    )
    await db.commit()
    return fid


# ---------------------------------------------------------------------------
# 1) T-01 同名可注入事实唯一性闸门
# ---------------------------------------------------------------------------


async def test_confirm_rejects_duplicate_injectable_name_with_different_value(db_conn):
    """同名且取值不同的另一条已可注入 → 409，且目标行不得被改写。"""
    await _seed_scope(db_conn)
    await _insert_fact(db_conn, "peer", value="12.0m", resolved=1)
    await _insert_fact(db_conn, "target", value="18.0m", resolved=0)

    with pytest.raises(HTTPException) as ei:
        await gf.resolve_fact("target", db=db_conn)

    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "duplicate_injectable_fact"
    assert "peer" in ei.value.detail["duplicate_fact_ids"]
    cur = await db_conn.execute("SELECT is_resolved FROM global_facts WHERE id='target'")
    assert (await cur.fetchone())[0] == 0


async def test_confirm_allows_duplicate_name_when_value_is_identical(db_conn):
    """取值逐字相同的重复登记只是冗余，不构成矛盾 → 放行。"""
    await _seed_scope(db_conn)
    await _insert_fact(db_conn, "peer", value="12.0m", resolved=1)
    await _insert_fact(db_conn, "target", value="12.0m", resolved=0)

    out = await gf.resolve_fact("target", db=db_conn)
    assert out == {"ok": True}
    cur = await db_conn.execute("SELECT is_resolved FROM global_facts WHERE id='target'")
    assert (await cur.fetchone())[0] == 1


async def test_confirm_allows_duplicate_name_when_only_whitespace_differs(db_conn):
    """事实名比较去全部空白（含全角空格），「基坑 深度」与「基坑深度」视为同名。"""
    await _seed_scope(db_conn)
    await _insert_fact(db_conn, "peer", title="基坑深度", value="12.0m", resolved=1)
    await _insert_fact(db_conn, "target", title="基坑\u3000深度", value="18.0m", resolved=0)

    with pytest.raises(HTTPException) as ei:
        await gf.resolve_fact("target", db=db_conn)
    assert ei.value.status_code == 409


async def test_confirm_allows_different_names(db_conn):
    """不同事实名不拦（防止闸门扩大到「同分类即冲突」）。"""
    await _seed_scope(db_conn)
    await _insert_fact(db_conn, "peer", title="基坑宽度", value="30m", resolved=1)
    await _insert_fact(db_conn, "target", title="基坑深度", value="18.0m", resolved=0)

    out = await gf.resolve_fact("target", db=db_conn)
    assert out == {"ok": True}


async def test_confirm_is_not_blocked_by_unconfirmed_peer(db_conn):
    """未确认的同名行本来就不进正文，不构成双值注入 → 不拦。"""
    await _seed_scope(db_conn)
    await _insert_fact(db_conn, "peer", value="12.0m", resolved=0)
    await _insert_fact(db_conn, "target", value="18.0m", resolved=0)

    out = await gf.resolve_fact("target", db=db_conn)
    assert out == {"ok": True}


async def test_conflict_resolution_rejects_duplicate_injectable_name(db_conn):
    """冲突裁决为真实值时同样受闸门约束，且裁决结果不得落库。"""
    await _seed_scope(db_conn)
    await _insert_fact(db_conn, "peer", value="12.0m", resolved=1)
    fid = uuid.uuid4().hex
    await _insert_fact(
        db_conn,
        fid,
        value="12.5m",
        sim=1,
        conflict=1,
        conflict_keys=json.dumps(
            [{"value": "18.0m", "source": "地质报告.pdf", "confidence": 0.95, "is_simulated": False}],
            ensure_ascii=False,
        ),
    )

    with pytest.raises(HTTPException) as ei:
        await gf.resolve_conflict(fid, gf.ResolveConflictRequest(value="18.0m"), db=db_conn)
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "duplicate_injectable_fact"

    cur = await db_conn.execute("SELECT content, is_resolved, has_conflict FROM global_facts WHERE id=?", (fid,))
    row = await cur.fetchone()
    assert row["is_resolved"] == 0 and row["has_conflict"] == 1
    assert "18.0m" not in (row["content"] or "")


async def test_norm_fact_text_removes_all_whitespace():
    """比较归一：空白（半角/全角）全部移除并转小写。"""
    assert gf._norm_fact_text(" 基坑\u3000 深度 ") == "基坑深度"
    assert gf._norm_fact_text("ABC-12") == "abc-12"


# ---------------------------------------------------------------------------
# 2) 章节手动保存围栏补齐计数回传
# ---------------------------------------------------------------------------


async def _seed_section(db) -> None:
    await _seed_scope(db, pid="p1", sid="sc1")
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
        " status, content, word_count, word_budget, sort_order) VALUES"
        " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("s1", "sc1", "p1", "", "第一章 工程概况", 1, "generated", "旧正文", 100, 1500, 0),
    )
    await db.commit()


async def test_manual_save_reports_fence_repair_counts(db_conn):
    """未闭合围栏被补齐时，响应体必须回传计数（此前只有服务端日志）。"""
    await _seed_section(db_conn)
    body = sec.SectionUpdate(content="正文第一段。\n\n```mermaid\ngraph TD\nA-->B\n")
    res = await sec.update_section("sc1", "s1", body, db_conn)

    assert res["ok"] is True
    assert res["fence_fixed"] >= 1
    assert res["fence_swallowed_lines"] == 0


async def test_title_only_update_has_no_fence_fields(db_conn):
    """只改标题（未提交正文）时不得伪造围栏计数。"""
    await _seed_section(db_conn)
    res = await sec.update_section("sc1", "s1", sec.SectionUpdate(title="第一章 工程概述"), db_conn)
    assert "fence_fixed" not in res
    assert "fence_swallowed_lines" not in res


# ---------------------------------------------------------------------------
# 3) 场景路由列表配置启停态
# ---------------------------------------------------------------------------


async def test_scene_route_list_exposes_enabled_flag(db_conn):
    """停用的配置在列表里必须 enabled=False（否则 UI 承诺与运行时相反）。"""
    scene = next(iter(sr.KNOWN_SCENES))
    await db_conn.execute(
        "INSERT INTO ai_config (id, provider_name, model, enabled) VALUES (?,?,?,?)",
        ("c1", "openai", "gpt-4o-mini", 0),
    )
    await db_conn.execute("INSERT INTO ai_scene_routes (scene, config_id) VALUES (?,?)", (scene, "c1"))
    await db_conn.commit()

    data = await sr.list_scene_routes(db=db_conn)
    item = next(i for i in data["items"] if i["scene"] == scene)
    assert item["config_id"] == "c1"
    assert item["enabled"] is False


async def test_scene_route_list_enabled_true_for_active_config(db_conn):
    scene = next(iter(sr.KNOWN_SCENES))
    await db_conn.execute(
        "INSERT INTO ai_config (id, provider_name, model, enabled) VALUES (?,?,?,?)",
        ("c2", "openai", "gpt-4o-mini", 1),
    )
    await db_conn.execute("INSERT INTO ai_scene_routes (scene, config_id) VALUES (?,?)", (scene, "c2"))
    await db_conn.commit()

    data = await sr.list_scene_routes(db=db_conn)
    item = next(i for i in data["items"] if i["scene"] == scene)
    assert item["enabled"] is True


# ---------------------------------------------------------------------------
# 4) 图表清单占位标题 + 引导语判据同源
# ---------------------------------------------------------------------------


def test_chart_list_title_skips_process_placeholder():
    """落库占位值「正文同步生成」不得出现在清单标题里。"""
    title = charts_mod._display_chart_title("正文同步生成", "flowchart", "3.2 施工工艺流程")
    assert title == "3.2 施工工艺流程 · 流程图"


def test_chart_list_title_keeps_real_title():
    assert charts_mod._display_chart_title("基坑支护施工流程", "flowchart", "3.2 施工工艺流程") == ("基坑支护施工流程")


def test_chart_list_title_falls_back_to_type_label_without_section():
    assert charts_mod._display_chart_title("", "gantt", "") == "甘特图"


def test_drop_dangling_lead_in_accepts_word_order_variant():
    """词序变体「施工流程图如下：」曾被生成侧窄判据漏掉（只剩导出侧回收）。"""
    out = ["正文第一段。", "", "施工流程图如下："]
    assert _drop_dangling_lead_in(out) is True
    # 引导语连同其上方的空行一起回收（与导出侧一致，不残留空段）
    assert out == ["正文第一段。", ""]


def test_drop_dangling_lead_in_keeps_long_paragraph():
    long_text = "本工程基坑支护采用排桩加内支撑体系，" * 3 + "施工流程图如下："
    assert _drop_dangling_lead_in(["正文。", "", long_text]) is False


def test_drop_dangling_lead_in_keeps_list_intro():
    """「要求如下：」是引出列表的正当行文（不含图/表字样），不得删除。"""
    assert _drop_dangling_lead_in(["正文。", "", "要求如下："]) is False


# ---------------------------------------------------------------------------
# 5) 配置导出：场景路由读失败必须显式化（不得以空列表冒充「本来没有路由」）
# ---------------------------------------------------------------------------


async def test_config_export_marks_scene_routes_ok_when_readable(db_conn):
    from app.routers.ai_config import config as cfg

    data = await cfg.export_config(db=db_conn)
    assert data["scene_routes_ok"] is True


async def test_config_export_flags_scene_routes_query_failure(db_conn):
    from app.routers.ai_config import config as cfg

    await db_conn.execute("DROP TABLE ai_scene_routes")
    await db_conn.commit()

    data = await cfg.export_config(db=db_conn)
    assert data["scene_routes"] == []
    assert data["scene_routes_ok"] is False
