"""多标段「投标范围」上下文注入回归测试（2026-09-22）。

引入背景（对齐 OpenBidKit `utils/bidSectionContext.cjs` +
`services/bidAnalysisTask.cjs` 的 sectionHint 注入）：
同一份多标段招标文件里各标段的工程规模/技术参数/工期完全不同。若不显式告诉
模型「本次只处理 X 标段」，模型会把其它标段的参数抽进本次方案。

本文件覆盖：
1. 提示构建纯函数（空 / 兜底 / 完整 / evidence 上限 / 空白归一化）；
2. **向后兼容锚点**：未选择标段时 system prompt 必须与旧版逐字一致；
3. 单项 AI 调用注入标段提示（system 消息）；
4. 分段合并调用同时注入「标段提示」与「原始任务要求（含 JSON 字段清单）」；
5. 库读取链路（resolve_section_hint：无行 / 明细 JSON / 仅有标题的历史数据）；
6. /select-section 与 /bid-sections 端点契约（写入、读回、清除、needs_selection）；
7. 提取结果变化 → 下游导出缓存失效（force_rerun / 人工校正 / 清空单项）。
"""
import uuid

import app.routers.bid_analysis as ba
import app.services.bid_section_context as bsc
import pytest
from app.services.bid_analysis_service import (
    STABLE_SYSTEM_PROMPT,
    build_system_prompt,
    get_item_def,
)

# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------

async def _seed_scheme(db) -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (sid, pid, "s"))
    await db.commit()
    return pid, sid


async def _seed_multi_sections(db, pid: str, sid: str) -> None:
    """写入一条「已检测为多标段」的 bid_sections 行（模拟 check-sections 的结果）。"""
    await db.execute(
        "INSERT OR REPLACE INTO bid_sections "
        "(id, project_id, scheme_id, is_multi, total_declared, detected_sections) "
        "VALUES (?,?,?,?,?,?)",
        (f"{pid}_default", pid, sid, 1, 2, '["标段:1","标段:2"]'))
    await db.commit()


# ---------------------------------------------------------------------------
# 1. 提示构建纯函数
# ---------------------------------------------------------------------------

def test_hint_empty_when_nothing_selected():
    assert bsc.build_bid_section_context_hint({}) == ""
    assert bsc.build_bid_section_context_hint(None) == ""


def test_hint_fallback_when_selected_but_no_detail():
    hint = bsc.build_bid_section_context_hint({}, has_selected_section=True)
    assert hint == bsc._NO_DETAIL_HINT
    assert "不要主动扩展到其他标段" in hint


def test_hint_contains_all_detail_fields():
    hint = bsc.build_bid_section_context_hint({
        "id": "section-2",
        "title": "  二标段 ",
        "headLine": "二标段： 设备采购及安装",
        "description": "设备采购、安装、调试",
        "evidence": ["二标段采购清单", "  "],
    })
    assert hint.startswith(bsc._BASE_HINT)
    assert "当前选择标段：二标段" in hint          # 空白被归一化
    assert "AI 识别标题行：二标段： 设备采购及安装" in hint
    assert "AI 识别描述：设备采购、安装、调试" in hint
    assert "AI 识别依据：二标段采购清单" in hint   # 空字符串条目被剔除


def test_hint_evidence_capped_at_six():
    hint = bsc.build_bid_section_context_hint(
        {"title": "一标段", "evidence": [f"依据{i}" for i in range(10)]})
    assert "依据5" in hint
    assert "依据6" not in hint, "evidence 必须截断为 6 条（避免 system 消息膨胀）"


def test_hint_accepts_snake_case_head_line():
    hint = bsc.build_bid_section_context_hint(
        {"title": "一标段", "head_line": "一标段：土建"})
    assert "AI 识别标题行：一标段：土建" in hint


def test_parse_selected_section_json_tolerant():
    assert bsc.parse_selected_section_json("") == {}
    assert bsc.parse_selected_section_json("not-json") == {}
    assert bsc.parse_selected_section_json('["a"]') == {}
    assert bsc.parse_selected_section_json('{"title":"一标段"}')["title"] == "一标段"


# ---------------------------------------------------------------------------
# 2. 向后兼容锚点
# ---------------------------------------------------------------------------

def test_system_prompt_unchanged_without_hint():
    """未选择投标范围时，system 消息必须与旧版逐字一致。"""
    assert build_system_prompt("") == STABLE_SYSTEM_PROMPT
    assert build_system_prompt() == STABLE_SYSTEM_PROMPT


def test_system_prompt_appends_hint_when_present():
    out = build_system_prompt("当前选择标段：二标段")
    assert out.startswith(STABLE_SYSTEM_PROMPT)
    assert "【当前处理标段上下文】当前选择标段：二标段" in out


# ---------------------------------------------------------------------------
# 3. 单项调用注入
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_single_call_injects_section_hint(monkeypatch):
    captured: list[list[dict]] = []

    async def fake_chat(messages, **kwargs):
        captured.append(messages)
        return "## 提取结果"

    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)
    item = get_item_def("overviewParams")
    hint = "当前选择标段：二标段"

    await ba._run_single_call(item, "项目资料", "markdown", section_hint=hint)

    assert captured, "必须发生一次 AI 调用"
    # ✅ 2026-09-30 第十四轮：标段上下文改为**独立的第二条 system 消息**
    #    （对齐易标 buildTenderContextMessages），不再拼进通用 system 提示词。
    #    断言因此从「第一条 system 含 hint」升级为「messages 里存在一条
    #    只承载标段上下文的 system 消息」——后者才真正锁住参考实现的口径。
    systems = [m for m in captured[0] if m["role"] == "system"]
    assert systems, "必须存在 system 消息"
    assert systems[0]["content"] == STABLE_SYSTEM_PROMPT, \
        "第一条 system 必须是原样的通用提示词（标段上下文不得再拼进去）"
    assert len(systems) == 2, "标段上下文必须是独立的第二条 system 消息"
    assert hint in systems[1]["content"]
    users = [m for m in captured[0] if m["role"] == "user"]
    assert users and users[0] is captured[0][-1]


@pytest.mark.asyncio
async def test_run_single_call_without_hint_keeps_legacy_prompt(monkeypatch):
    captured: list[list[dict]] = []

    async def fake_chat(messages, **kwargs):
        captured.append(messages)
        return "## 提取结果"

    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)
    await ba._run_single_call(get_item_def("overviewParams"), "资料", "markdown")

    assert captured[0][0]["content"] == STABLE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 4. 分段合并：标段提示 + 原始任务要求
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_segment_merge_carries_section_hint_and_task_schema(monkeypatch):
    """合并调用必须同时携带标段提示与原始任务要求（JSON 字段清单）。

    旧实现只给「输出形态一致」的口头约束：projectBasicInfo 有 37 个字段，
    模型在合并阶段不知道要保留哪些 key → 合并后字段丢失。
    """
    # ✅ 2026-09-24：fake 必须与 _run_single_call 新签名保持一致（classification_hint），
    #    并记录入参以锁定「逐段调用 + 合并调用」三处透传契约。
    seg_calls: list[dict] = []

    async def fake_single_call(item, text, output_type, section_hint="",
                               classification_hint=""):
        seg_calls.append({"text": text, "section_hint": section_hint,
                          "classification_hint": classification_hint})
        return '{"project_name":"某项目","project_number":"ZB-001"}'

    merge_calls: list[list[dict]] = []

    async def fake_chat(messages, **kwargs):
        merge_calls.append(messages)
        return '{"project_name":"某项目","project_number":"ZB-001"}'

    monkeypatch.setattr(ba, "_run_single_call", fake_single_call)
    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)

    item = get_item_def("projectBasicInfo")
    hint = "当前选择标段：二标段"
    class_hint = "危大级别：超过一定规模；重点核查基坑支护参数"
    await ba._run_single_item(None, "pid", "sid", item, "全文", ["段1", "段2"],
                              section_hint=hint, classification_hint=class_hint)

    assert merge_calls, "多段必须触发一次合并调用"
    # 逐段调用必须同时透传两个 hint（防 _run_single_call 形参漂移回归）
    assert len(seg_calls) == 2
    assert {c["text"] for c in seg_calls} == {"段1", "段2"}
    assert all(c["section_hint"] == hint for c in seg_calls)
    assert all(c["classification_hint"] == class_hint for c in seg_calls)
    # ✅ 标段上下文已是独立第二条 system 消息（易标口径）；
    #    危大分类结论仍拼在第一条（它与通用纪律同属「提取要求」）。
    systems = [m for m in merge_calls[0] if m["role"] == "system"]
    assert len(systems) == 2, "合并调用也要带独立的标段 system 消息"
    assert hint in systems[1]["content"], "合并调用同样要注入标段上下文"
    assert class_hint in systems[0]["content"], "合并调用同样要注入危大分类结论"
    user = [m for m in merge_calls[0] if m["role"] == "user"][0]
    assert "project_number" in user["content"], (
        "合并消息必须包含原始任务的 JSON 字段清单，否则合并丢字段")
    assert "原始任务要求" in user["content"]
    assert "分段 1" in user["content"] and "分段 2" in user["content"]


@pytest.mark.asyncio
async def test_single_segment_skips_merge(monkeypatch):
    """单段不触发合并调用（避免多一次 AI 调用）；classification_hint 仍须透传。"""
    calls: list[str] = []
    single_calls: list[dict] = []

    async def fake_single_call(item, text, output_type, section_hint="",
                               classification_hint=""):
        single_calls.append({"section_hint": section_hint,
                             "classification_hint": classification_hint})
        return "## 单段结果"

    async def fake_chat(messages, **kwargs):  # pragma: no cover
        calls.append("merge")
        return "x"

    monkeypatch.setattr(ba, "_run_single_call", fake_single_call)
    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)

    class_hint = "危大级别：危大工程"
    res = await ba._run_single_item(None, "pid", "sid",
                                    get_item_def("overviewParams"),
                                    "全文", ["唯一段"],
                                    classification_hint=class_hint)
    assert res["content"] == "## 单段结果"
    assert not calls
    # 单段路径同样必须把 classification_hint 透传给唯一一次 AI 调用
    assert len(single_calls) == 1
    assert single_calls[0]["classification_hint"] == class_hint
    assert single_calls[0]["section_hint"] == ""


# ---------------------------------------------------------------------------
# 5. 库读取链路
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_section_hint_without_row_returns_empty(db_conn):
    pid, sid = await _seed_scheme(db_conn)
    hint, section = await bsc.resolve_section_hint(db_conn, pid, sid)
    assert hint == "" and section == {}, "无选择时必须返回空（保持旧行为）"


@pytest.mark.asyncio
async def test_resolve_section_hint_reads_detail_json(db_conn):
    pid, sid = await _seed_scheme(db_conn)
    await _seed_multi_sections(db_conn, pid, sid)
    await ba.select_bid_section({
        "scheme_id": sid, "section_id": "section-2", "section_title": "二标段",
        "description": "设备采购及安装", "evidence": ["二标段清单"],
    }, db=db_conn)

    hint, section = await bsc.resolve_section_hint(db_conn, pid, sid)
    assert section["id"] == "section-2" and section["title"] == "二标段"
    assert "当前选择标段：二标段" in hint
    assert "AI 识别依据：二标段清单" in hint


@pytest.mark.asyncio
async def test_resolve_section_hint_supports_legacy_title_only(db_conn):
    """历史数据只有 selected_section_title（无明细 JSON）也要能生成提示。"""
    pid, sid = await _seed_scheme(db_conn)
    await db_conn.execute(
        "INSERT INTO bid_sections "
        "(id, project_id, scheme_id, is_multi, selected_section_id, "
        " selected_section_title) VALUES (?,?,?,?,?,?)",
        ("legacy", pid, sid, 1, "section-1", "一标段"))
    await db_conn.commit()

    hint, section = await bsc.resolve_section_hint(db_conn, pid, sid)
    assert section["title"] == "一标段"
    assert "当前选择标段：一标段" in hint


# ---------------------------------------------------------------------------
# 6. 端点契约
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_section_roundtrip_and_clear(db_conn):
    pid, sid = await _seed_scheme(db_conn)
    await _seed_multi_sections(db_conn, pid, sid)

    res = await ba.select_bid_section({
        "scheme_id": sid, "section_id": "section-2",
        "section_title": "二标段", "head_line": "二标段：设备采购",
        "description": "设备采购、安装、调试",
        "evidence": ["二标段：设备采购"],
    }, db=db_conn)
    assert res["ok"] is True and res["cleared"] is False
    assert res["context_hint"]

    got = await ba.get_bid_sections(scheme_id=sid, project_id="", db=db_conn)
    assert got["is_multi"] is True and got["total_declared"] == 2
    assert got["selected_section_id"] == "section-2"
    assert got["selected_section_title"] == "二标段"
    assert got["needs_selection"] is False
    assert "当前选择标段：二标段" in got["context_hint"]

    # 清除选择 → 恢复旧行为（不注入任何上下文）
    cleared = await ba.select_bid_section(
        {"scheme_id": sid, "section_id": ""}, db=db_conn)
    assert cleared["cleared"] is True and cleared["context_hint"] == ""

    got2 = await ba.get_bid_sections(scheme_id=sid, project_id="", db=db_conn)
    assert got2["needs_selection"] is True, "多标段未选择时前端需要被提示"
    assert got2["context_hint"] == ""


@pytest.mark.asyncio
async def test_get_bid_sections_without_detection_row(db_conn):
    pid, sid = await _seed_scheme(db_conn)
    got = await ba.get_bid_sections(scheme_id=sid, project_id="", db=db_conn)
    assert got["is_multi"] is False and got["needs_selection"] is False
    assert got["detected_sections"] == []
    assert got["context_hint"] == ""


@pytest.mark.asyncio
async def test_select_section_requires_scope(db_conn):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await ba.select_bid_section({"section_id": "section-1"}, db=db_conn)
    assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# 7. 下游缓存失效
# ---------------------------------------------------------------------------

async def _seed_export_cache(db, sid: str) -> None:
    await db.execute(
        "INSERT INTO export_cache (id, scheme_id, result_path) VALUES (?,?,?)",
        (uuid.uuid4().hex, sid, ""))
    await db.commit()


@pytest.mark.asyncio
async def test_invalidate_downstream_cache_clears_export_cache(db_conn):
    pid, sid = await _seed_scheme(db_conn)
    await _seed_export_cache(db_conn, sid)

    n = await ba._invalidate_downstream_cache(db_conn, pid)
    assert n == 1
    cur = await db_conn.execute(
        "SELECT COUNT(*) c FROM export_cache WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())["c"] == 0, "提取结果变化后导出缓存必须失效"


@pytest.mark.asyncio
async def test_reset_items_for_rerun_invalidates_downstream(db_conn):
    """force_rerun 清空解析项 → 必须联动失效导出缓存（否则导出内容与提取不一致）。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_export_cache(db_conn, sid)

    await ba._reset_items_for_rerun(db_conn, pid, ["schemeBasicInfo"])

    cur = await db_conn.execute(
        "SELECT COUNT(*) c FROM export_cache WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())["c"] == 0


@pytest.mark.asyncio
async def test_update_single_result_invalidates_downstream(db_conn):
    """人工校正（PUT /results/{item_id}）同样属于提取结果变化。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_export_cache(db_conn, sid)

    await ba.update_single_result(
        "schemeBasicInfo", {"content": "## 人工校正内容"},
        scheme_id=sid, project_id="", db=db_conn)

    cur = await db_conn.execute(
        "SELECT COUNT(*) c FROM export_cache WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())["c"] == 0


@pytest.mark.asyncio
async def test_clear_single_result_invalidates_downstream(db_conn):
    pid, sid = await _seed_scheme(db_conn)
    await _seed_export_cache(db_conn, sid)

    await ba.clear_single_result(
        "schemeBasicInfo", scheme_id=sid, project_id="", db=db_conn)

    cur = await db_conn.execute(
        "SELECT COUNT(*) c FROM export_cache WHERE scheme_id=?", (sid,))
    assert (await cur.fetchone())["c"] == 0
