"""R181（2026-11-06）正文 / 图表 / 目录遗留项回归。

覆盖：
1. 单章保存与 SSE 生成共用同一围栏告警构造（同判定、同载荷键）；
2. section_done 事件体积闸门（截断必须带可见降级字段）；
3. chart-json `types + values` 形态的识别、归一与真实渲染；
4. 甘特 JSON 无 HTTP 兜底的失败信号；
5. 分步生成空章/失败章单元内定向重试与状态追踪；
6. 目录提示词子标题示例按本节前缀泛化；
7. 列表项字母序前缀剥离的落库/导出同语料一致。
"""

import json

import pytest
from app.services.content_standard import (
    FENCE_SWALLOWED_CODE,
    attach_fence_swallowed_issue,
    fence_swallowed_issue,
)


# ============================================================
# 1. 围栏告警：两路径同口径
# ============================================================
def test_fence_swallowed_issue_payload_is_single_source():
    issue = fence_swallowed_issue(3)
    assert issue is not None
    assert issue["code"] == FENCE_SWALLOWED_CODE
    assert issue["severity"] == "warning"
    assert "3 行" in issue["message"]
    # 0 / 负数 / 非法值一律不产生告警（默认路径零新增）
    assert fence_swallowed_issue(0) is None
    assert fence_swallowed_issue(-1) is None
    assert fence_swallowed_issue("x") is None


def test_attach_fence_issue_matches_sse_and_manual_save_payload():
    """SSE 的 report 与单章保存的 standard_report 使用同一构造 → 逐字段等价。"""
    sse_report = attach_fence_swallowed_issue({}, 2)
    manual_report = attach_fence_swallowed_issue({}, 2)
    assert json.dumps(sse_report, ensure_ascii=False, sort_keys=True) == json.dumps(
        manual_report, ensure_ascii=False, sort_keys=True
    )
    assert sse_report["issues"][0]["code"] == "fence_swallowed_prose"
    assert sse_report["warning_count"] == 1
    assert sse_report.get("passed", True) is True


@pytest.mark.asyncio
async def test_manual_save_exposes_fence_swallowed_warning(db_conn):
    """单章保存路径必须把未闭合围栏吞正文写进 standard_report（此前完全没有）。"""
    from app.routers.sections import SectionUpdate, update_section

    await db_conn.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", ("p-r181", "项目"))
    await db_conn.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s-r181", "p-r181", "方案"),
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " level, sort_order, status, word_budget) VALUES (?,?,?,?,?,?,?,?,?)",
        ("c-r181", "s-r181", "p-r181", "", "第一章", 1, 0, "empty", 1500),
    )
    await db_conn.commit()

    # 未闭合 mermaid 围栏 + 其后中文正文 → 补闭合时被吞
    content = "施工流程如下：\n```mermaid\nflowchart TD\n  A --> B\n后续应在外面的正文一句。\n"
    resp = await update_section("s-r181", "c-r181", SectionUpdate(content=content), db=db_conn)
    assert resp["ok"] is True
    assert resp["fence_swallowed_lines"] > 0, resp
    report = resp.get("standard_report")
    assert report is not None, "单章保存路径缺少围栏告警载荷"
    assert report["issues"][0]["code"] == FENCE_SWALLOWED_CODE
    assert report["warning_count"] == 1
    # 与 SSE 共用构造：同一 swallowed 计数产出逐字段相同的 issue
    assert report["issues"][0] == fence_swallowed_issue(resp["fence_swallowed_lines"])


# ============================================================
# 2. section_done 体积闸门
# ============================================================
def test_section_done_payload_gate_is_explicit_and_default_off_for_small():
    from app.routers.sse_handlers import _degrade_section_done_payload

    small = {"event": "section_done", "section_id": "c1", "content": "正文" * 10}
    # 默认上限（256KB）下正常载荷原样返回，不新增任何键
    assert _degrade_section_done_payload(small, 262144) == small
    assert set(_degrade_section_done_payload(small, 262144).keys()) == set(small.keys())
    # max<=0 完全不干预
    assert _degrade_section_done_payload(small, 0) is small


def test_section_done_payload_gate_default_matches_status_quo():
    """约束「新增配置默认值等于现状」：类默认必须是 0（完全不干预事件载荷）。"""
    from app.config import Settings
    from app.routers.sse_handlers import _degrade_section_done_payload

    assert Settings.model_fields["section_done_payload_max_chars"].default == 0
    payload = {"event": "section_done", "section_id": "c1", "content": "正文" * 5000}
    keys = set(payload.keys())
    # 默认 0 时逐对象原样返回，不新增任何键（与引入前完全一致）
    assert _degrade_section_done_payload(payload, 0) is payload
    assert set(_degrade_section_done_payload(dict(payload), 0).keys()) == keys


def test_section_done_payload_gate_truncates_visibly():
    from app.routers.sse_handlers import _degrade_section_done_payload

    content = "长正文内容。" * 500
    payload = {"event": "section_done", "section_id": "c1", "content": content}
    out = _degrade_section_done_payload(payload, 2000)
    assert out["truncated"] is True
    assert out["kept_chars"] == len(out["content"])
    assert out["truncated_chars"] == len(content) - out["kept_chars"]
    assert out["truncated_chars"] > 0
    assert out["content_ref"] == {"section_id": "c1", "source": "db"}
    assert "已截断" in out["truncation_note"]
    # 显式字段必须足以让调用方判断「内容不完整」
    assert len(json.dumps(out, ensure_ascii=False)) <= 2000 + 64


# ============================================================
# 3. chart-json types + values
# ============================================================
def test_types_values_chart_json_is_recognized_and_normalized():
    from app.services.chart_validators import infer_chart_type_from_payload, normalize_labor_data

    payload = {"types": ["木工", "钢筋工", "架子工"], "values": [12, 20, 8]}
    assert infer_chart_type_from_payload(payload) == "labor"
    data = normalize_labor_data(payload)
    assert data is not None
    assert data["categories"] == ["木工", "钢筋工", "架子工"]
    assert data["phases"] == ["阶段1"]
    assert data["data"] == [[12.0, 20.0, 8.0]]


def test_types_values_chart_json_renders_end_to_end():
    from app.services.ai.mermaid_labor import _render_labor_image_v2

    payload = {"types": ["木工", "钢筋工", "架子工"], "values": [12, 20, 8]}
    image = _render_labor_image_v2(payload)
    assert image is not None, "types+values 必须能真正出图，而不是被静默丢弃"
    raw = image.getvalue() if hasattr(image, "getvalue") else bytes(image)
    assert raw[:4] in (b"\x89PNG", b"\xff\xd8"), "渲染结果必须是非空位图"


# ============================================================
# 4. 甘特 JSON 无 HTTP 兜底的失败信号
# ============================================================
@pytest.mark.asyncio
async def test_gantt_json_failure_exposes_no_http_fallback_signal(db_conn, monkeypatch):
    from app.routers.export import export_docx
    from app.services.ai import mermaid_renderer as mr
    from fastapi.responses import FileResponse, JSONResponse

    monkeypatch.setattr(mr._chart_cache, "get_or_render", lambda *_a, **_k: None)

    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s-r181-gantt", "p-r181", "甘特 JSON"),
    )
    content = (
        "进度安排如下。\n\n"
        "```chart-json\n"
        '{"type":"gantt","title":"进度计划","tasks":[{"name":"准备","start":"2026-01-01","end":"2026-01-05"}]}\n'
        "```\n"
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("c-r181-gantt", "s-r181-gantt", "", 0, 1, "第一章 进度", "generated", content),
    )
    await db_conn.commit()

    # 闸门回归护栏：不带 override 时必须真被阻断，而非闸门失效
    blocked = await export_docx(
        "s-r181-gantt",
        {"project_name": "示例", "bid_section": "第一标段", "config": {}, "chart_images": [], "fe_codes": []},
        db=db_conn,
    )
    assert isinstance(blocked, JSONResponse)
    assert blocked.status_code == 409
    blocked_payload = json.loads(blocked.body)
    assert blocked_payload.get("error") == "export_blocked_high_issues"
    assert blocked_payload.get("blocked") is True
    assert "X-Export-Override" not in blocked.headers

    resp = await export_docx(
        "s-r181-gantt",
        {
            "project_name": "示例",
            "bid_section": "第一标段",
            "config": {},
            "chart_images": [],
            "fe_codes": [],
            "override_high_issues": True,
        },
        db=db_conn,
    )
    # 覆写路径必须留响应头，证明本轮是「已知悉并继续」而非闸门失效
    assert resp.headers.get("X-Export-Override") == "high-issues"
    assert isinstance(resp, FileResponse)
    stats = json.loads(resp.headers.get("X-Chart-Render-Stats") or "{}")
    assert stats.get("failed") == 1, stats
    assert stats.get("no_http_fallback") == 1, stats
    assert stats.get("no_http_fallback_types") == ["gantt"], stats
    # 甘特有本地 PIL 兜底，不应误记为 no_pil_fallback
    assert not stats.get("no_pil_fallback"), stats


# ============================================================
# 5. 分步生成空章重试状态机
# ============================================================
@pytest.mark.asyncio
async def test_empty_chapter_retry_recovers_and_tracks_state(monkeypatch):
    from app.routers import sse_handlers

    calls = {"n": 0}

    async def _fake_fetch(index, chapter, **_kwargs):
        calls["n"] += 1
        return "ok", [{"id": f"child-{index}", "title": "补出的子节点"}]

    monkeypatch.setattr(sse_handlers, "_fetch_chapter_children", _fake_fetch)
    monkeypatch.setattr(sse_handlers, "is_stopped", lambda _tid: False)
    retry_state: dict = {}
    chapter = {"id": "c1", "title": "第一章"}
    per = await sse_handlers._retry_empty_chapters(
        [(0, chapter)], [("failed", [])], [["prompt"]], "t1", None, 30, None, retry_state
    )
    assert per[0][0] == "ok"
    assert len(per[0][1]) == 1
    assert retry_state["1"]["status"] == "recovered"
    assert retry_state["1"]["attempts"] == 1


@pytest.mark.asyncio
async def test_empty_chapter_retry_exhausted_is_tracked(monkeypatch):
    from app.routers import sse_handlers

    async def _fake_fetch(index, chapter, **_kwargs):
        return "failed", []

    monkeypatch.setattr(sse_handlers, "_fetch_chapter_children", _fake_fetch)
    monkeypatch.setattr(sse_handlers, "is_stopped", lambda _tid: False)
    retry_state: dict = {}
    per = await sse_handlers._retry_empty_chapters(
        [(0, {"id": "c1"})], [("ok", [])], [["prompt"]], "t1", None, 30, None, retry_state
    )
    assert per[0][0] == "ok" and per[0][1] == []
    assert retry_state["1"]["status"] == "exhausted"
    assert retry_state["1"]["attempts"] == 1


def test_outline_snapshot_exposes_chapter_retry_only_when_present():
    from app.routers._sse_outline_build import _snapshot_outline_stats

    base = {"started_at": 1.0, "total": 3}
    assert "chapter_retry" not in _snapshot_outline_stats(dict(base))
    base["chapter_retry"] = {
        "1": {"attempts": 1, "status": "recovered"},
        "2": {"attempts": 1, "status": "exhausted"},
    }
    snap = _snapshot_outline_stats(dict(base))
    assert snap["chapter_retry"]["attempted"] == 2
    assert snap["chapter_retry"]["recovered"] == 1
    assert snap["chapter_retry"]["exhausted"] == 1


# ============================================================
# 6. 提示词子标题示例按本节前缀泛化
# ============================================================
def test_subheading_examples_follow_section_prefix():
    from app.services.ai.heading_templates import build_subheading_rule

    prefixed = build_subheading_rule(has_db_children=False, section_prefix="3.2")
    assert "二级小标题：3.2.1 标题" in prefixed
    assert "三级小标题：3.2.1.1 标题" in prefixed
    assert "- 二级小标题：1.1 标题" not in prefixed
    assert "- 二级小标题：3.2.1 标题" in prefixed

    # 不传/无法解析前缀 → 保持旧文案（默认行为零回归）
    legacy = build_subheading_rule(has_db_children=False)
    assert "二级小标题：1.1 标题" in legacy
    assert build_subheading_rule(has_db_children=False, section_prefix="第3章") == legacy


def test_subheading_demoted_rule_unaffected_by_prefix():
    from app.services.ai.heading_templates import build_subheading_rule

    rule = build_subheading_rule(has_db_children=True, section_prefix="3.2")
    assert rule == build_subheading_rule(has_db_children=True)


# ============================================================
# 7. 列表项前缀剥离：落库 / 导出同语料一致
# ============================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a、吊顶与内装接口", "吊顶与内装接口"),
        ("a、a、吊顶与内装接口", "吊顶与内装接口"),
        ("ab、施工准备", "施工准备"),
        ("A) 钢筋绑扎", "钢筋绑扎"),
        ("a. 模板安装", "模板安装"),
        ("a、", "a、"),
        ("（1）施工准备", "（1）施工准备"),
        ("普通正文", "普通正文"),
    ],
)
def test_list_item_prefix_strip_parity(raw, expected):
    from app.routers.export import _normalize_render_blocks
    from app.services.content_blocks import strip_subitem_ordinal_prefix

    shared = strip_subitem_ordinal_prefix(raw)
    # 先锁剥离语义本身（否则两侧共用同一实现时，回归成「都不剥离」也能绿）
    assert shared == expected, f"前缀剥离语义漂移: {raw!r} → {shared!r}（期望 {expected!r}）"
    blocks = _normalize_render_blocks([{"type": "list_item", "text": raw}])
    exported = str(blocks[0].get("text") or "")
    assert exported == shared, f"落库侧与导出侧输出不一致: {raw!r} → {shared!r} vs {exported!r}"
