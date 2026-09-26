"""解析提取模块（import）后端加固回归测试（2026-09-23）。

本轮审计在「项目提取（bid-analysis）」后端发现 4 个真实缺陷，本文件逐条钉住：

1. **[H] POST /bid-analysis/check-sections 静默清空已选投标范围**
   旧实现用只写 7 列的 `INSERT OR REPLACE`；SQLite 的 REPLACE 语义是「先 DELETE
   再 INSERT」，`selected_section_id / _title / _json / status / error` 全部回落
   DEFAULT。用户已选定的标段被下一次点击「多标段检测」清空 → 标段上下文提示丢失
   → AI 把其它标段参数混进本次方案，且无任何告警。

2. **[H] POST /bid-analysis/select-section 前后端契约断裂**
   后端只从 JSON body 读 scheme_id / project_id，而前端
   `bidAnalysisApi.selectSection` 把它们放在 URL query（与本模块其它端点同口径）
   → 真实前端调用 100% 命中「需要 scheme_id 或 project_id」的 400。

3. **[H] _run_bid_analysis_sync 预热项落库失败导致任务「假成功」**
   先写 `results[rid] = res`（success），再落库；若落库失败，except 只改 DB 不
   改 results → `_get_missing_required` 命中 `rid in results` 分支判为已完成 →
   返回 ok=True，而库里该项是 error。

4. **[M] _repair_json 的 scene 硬编码**
   单项提取的 JSON 修复也被记到 `bid_analysis_merge`，/ai/stats 按场景聚合时
   单项提取量被错记到分段合并（违反 AGENTS.md §4.5 的 scene 语义）。
"""
import uuid

import pytest
from fastapi import HTTPException

import app.routers.bid_analysis as ba
from app.main import app
from app.services.bid_analysis_service import (
    REQUIRED_ITEM_IDS, AnalysisConfig, get_item_def,
)

# 多标段文本：显式声明总数 + 两个标段定义，规则检测必命中
MULTI_SECTION_TEXT = (
    "本项目划分为三个标段。\n"
    "一标段：土建工程施工\n"
    "二标段：安装工程施工\n"
    "三标段：装饰装修工程\n"
)


def _label(item_id: str) -> str:
    return (get_item_def(item_id) or {}).get("label", item_id)


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


async def _seed_parsed_doc(db, pid: str, text: str = MULTI_SECTION_TEXT) -> str:
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO project_documents"
        " (id, project_id, file_name, file_type, parsed_markdown, doc_category,"
        "  parse_status) VALUES (?,?,?,?,?,?,?)",
        (doc_id, pid, "招标文件.txt", "txt", text, "招标文件", "success"))
    await db.commit()
    return doc_id


async def _seed_selected_section(db, pid: str, sid: str) -> None:
    """写入「已检测为多标段 + 已选定 section-2」的 bid_sections 行。"""
    await db.execute(
        "INSERT OR REPLACE INTO bid_sections"
        " (id, project_id, scheme_id, is_multi, total_declared, detected_sections,"
        "  selected_section_id, selected_section_title, selected_section_json,"
        "  status, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"{pid}_default", pid, sid, 1, 3,
         '["标段:1","标段:2","标段:3"]',
         "section-2", "二标段",
         '{"id": "section-2", "title": "二标段", "headLine": "二标段：安装工程"}',
         "success", ""))
    await db.commit()


# ---------------------------------------------------------------------------
# 1. /check-sections 不得清空已选投标范围
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_sections_preserves_selected_section(db_conn):
    """重跑多标段检测后，用户已选定的标段必须原样保留。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_parsed_doc(db_conn, pid)
    await _seed_selected_section(db_conn, pid, sid)

    result = await ba.check_bid_sections(scheme_id=sid, project_id="", db=db_conn)

    assert result["ok"] is True
    assert result["has_multiple"] is True
    # 选择必须保留
    assert result["selected_section_id"] == "section-2"
    assert result["selected_section_title"] == "二标段"
    assert result["needs_selection"] is False

    # 库里也必须保留（前端随后会读 GET /bid-sections）
    cur = await db_conn.execute(
        "SELECT selected_section_id, selected_section_title, selected_section_json,"
        " status FROM bid_sections WHERE id=?", (f"{pid}_default",))
    row = await cur.fetchone()
    assert row["selected_section_id"] == "section-2"
    assert row["selected_section_title"] == "二标段"
    assert "section-2" in row["selected_section_json"]
    assert row["status"] == "success", "status 列不得回落 DEFAULT"

    # 且标段上下文提示仍可生成（否则下游 AI 调用会退化成无标段约束）
    hint, section = await ba.resolve_section_hint(db_conn, pid, sid)
    assert section.get("id") == "section-2"
    assert hint and "当前选择标段：二标段" in hint


@pytest.mark.asyncio
async def test_check_sections_reports_needs_selection_when_unselected(db_conn):
    """多标段但未选择 → 必须如实返回 needs_selection=True（前端据此提示选择）。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_parsed_doc(db_conn, pid)
    await db_conn.execute(
        "INSERT INTO bid_sections (id, project_id, scheme_id, is_multi)"
        " VALUES (?,?,?,?)", (f"{pid}_default", pid, sid, 1))
    await db_conn.commit()

    result = await ba.check_bid_sections(scheme_id=sid, project_id="", db=db_conn)
    assert result["has_multiple"] is True
    assert result["needs_selection"] is True
    assert result["selected_section_id"] == ""


@pytest.mark.asyncio
async def test_check_sections_single_section_never_requires_selection(db_conn):
    """单标段（未命中多标段特征）→ needs_selection 恒为 False。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_parsed_doc(db_conn, pid,
                           text="# 施工组织设计\n本工程为单一标段施工。")

    result = await ba.check_bid_sections(scheme_id=sid, project_id="", db=db_conn)
    assert result["has_multiple"] is False
    assert result["needs_selection"] is False


# ---------------------------------------------------------------------------
# 2. /select-section 契约：query 参数（前端写法）与 body 参数（旧写法）都可用
# ---------------------------------------------------------------------------

def test_select_section_declares_scope_as_query_params():
    """OpenAPI 契约层校验：scheme_id / project_id 必须是 query 参数。

    这是本次修复的核心断言 —— 前端 bidAnalysisApi.selectSection 用
    `params: { scheme_id, project_id }` 发请求；若后端只把它们当作 body 字段，
    真实 HTTP 调用永远拿不到值，接口 100% 返回 400。用 OpenAPI 描述做校验，
    既覆盖「声明为 Query」这一接线事实，又无需访问数据库。
    """
    schema = app.openapi()
    post = schema["paths"]["/api/v1/bid-analysis/select-section"]["post"]
    query_names = {p["name"] for p in post.get("parameters", [])
                   if p.get("in") == "query"}
    assert {"scheme_id", "project_id"} <= query_names, (
        "select-section 必须把 scheme_id/project_id 声明为 query 参数，"
        f"实际 query 参数为 {sorted(query_names)}")

    # 对照：同模块其它端点保持同口径（防再次漂移）
    cs = schema["paths"]["/api/v1/bid-analysis/check-sections"]["post"]
    cs_query = {p["name"] for p in cs.get("parameters", [])
                if p.get("in") == "query"}
    assert {"scheme_id", "project_id"} <= cs_query, "check-sections 口径应保持 query"


@pytest.mark.asyncio
async def test_select_section_accepts_query_params(db_conn):
    """前端写法：scheme_id/project_id 走 query，body 只有标段明细。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_selected_section(db_conn, pid, sid)

    got = await ba.select_bid_section(
        {"section_id": "section-3", "section_title": "三标段",
         "head_line": "三标段：装饰装修工程"},
        scheme_id=sid, project_id="", db=db_conn)

    assert got["ok"] is True
    assert got["selected_section_id"] == "section-3"
    assert got["selected_section_title"] == "三标段"

    cur = await db_conn.execute(
        "SELECT selected_section_id, detected_sections FROM bid_sections"
        " WHERE id=?", (f"{pid}_default",))
    row = await cur.fetchone()
    assert row["selected_section_id"] == "section-3"
    # 检测结果（detected_sections）不得因「选择」操作被清空
    assert "标段:2" in row["detected_sections"]


@pytest.mark.asyncio
async def test_select_section_still_accepts_body_params(db_conn):
    """向后兼容：既有脚本/单测把 scheme_id 放进 body 的写法必须继续可用。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_selected_section(db_conn, pid, sid)

    got = await ba.select_bid_section(
        {"scheme_id": sid, "section_id": "section-1", "section_title": "一标段"},
        scheme_id="", project_id="", db=db_conn)

    assert got["ok"] is True
    assert got["selected_section_id"] == "section-1"


@pytest.mark.asyncio
async def test_select_section_requires_scope_anywhere(db_conn):
    """query 与 body 都没有 scope → 必须 400（不能静默写入空项目）。"""
    with pytest.raises(HTTPException) as ei:
        await ba.select_bid_section(
            {"section_id": "section-1"}, scheme_id="", project_id="", db=db_conn)
    assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# 3. _run_bid_analysis_sync 不得「假成功」
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_first_item_db_failure_is_not_fake_success(db_conn, monkeypatch):
    """预热项内容已取到但落库失败 → 任务必须如实报 failed，而非 ok=True。

    回归点：旧实现 except 分支只写 DB（status=error）不回写 results，
    results 里留着 status=success 的条目，_get_missing_required 命中
    `rid in results` 分支就判为已完成 → 任务返回 ok=True（假成功）。
    """
    pid, sid = await _seed_scheme(db_conn)
    item_id = "projectBasicInfo"
    config = AnalysisConfig(mode="item", selected_item_ids=[item_id]).normalize()
    assert len(config.get_task_items()) == 1

    async def fake_run_single_item(*args, **kwargs):
        return {"status": "success", "content": "## 有效提取内容", "item_id": item_id}

    real_update = ba._update_item_status
    calls = {"n": 0}

    async def flaky_update_item_status(db, project_id, iid, status,
                                       content="", error="", evidence=None):
        calls["n"] += 1
        if calls["n"] == 1:  # 第一次（success 落库）失败
            raise OSError("database is locked")
        return await real_update(db, project_id, iid, status,
                                 content, error, evidence)

    monkeypatch.setattr(ba, "_run_single_item", fake_run_single_item)
    monkeypatch.setattr(ba, "_update_item_status", flaky_update_item_status)

    result = await ba._run_bid_analysis_sync(
        db_conn, pid, sid, config, "本项目建筑面积 12000 平方米。")

    assert result["ok"] is False, "落库失败不得被报告为成功"
    assert _label(item_id) in result["missing_required"], "缺失清单必须包含预热项"

    # DB 里的真实状态是 error
    cur = await db_conn.execute(
        "SELECT status FROM bid_analysis_items WHERE id=?", (f"{pid}_{item_id}",))
    row = await cur.fetchone()
    assert row is not None and row["status"] == "error"


@pytest.mark.asyncio
async def test_sync_first_item_ai_failure_is_not_fake_success(db_conn, monkeypatch):
    """预热项 AI 调用抛异常 → 任务必须如实报 failed（原本就正确，防回归）。"""
    pid, sid = await _seed_scheme(db_conn)
    item_id = "projectBasicInfo"
    config = AnalysisConfig(mode="item", selected_item_ids=[item_id]).normalize()

    async def boom(*args, **kwargs):
        raise RuntimeError("provider quota exhausted")

    monkeypatch.setattr(ba, "_run_single_item", boom)

    result = await ba._run_bid_analysis_sync(
        db_conn, pid, sid, config, "本项目建筑面积 12000 平方米。")

    assert result["ok"] is False
    assert _label(item_id) in result["missing_required"]


@pytest.mark.asyncio
async def test_sync_all_items_fail_reports_all_missing(db_conn, monkeypatch):
    """多项全失败 → missing_required 必须列全必选项（不得漏报）。"""
    pid, sid = await _seed_scheme(db_conn)
    config = AnalysisConfig(mode="key").normalize()
    items = config.get_task_items()
    assert len(items) == len(REQUIRED_ITEM_IDS)

    async def boom(*args, **kwargs):
        raise RuntimeError("all providers down")

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(ba, "_run_single_item", boom)
    # 隔离 DB 写入：本用例只关心返回契约，不把断言绑死在写库副作用上
    monkeypatch.setattr(ba, "_update_item_status", _noop)

    result = await ba._run_bid_analysis_sync(
        db_conn, pid, sid, config, "本项目建筑面积 12000 平方米。")

    assert result["ok"] is False
    for rid in REQUIRED_ITEM_IDS:
        assert _label(rid) in result["missing_required"], f"{rid} 漏报缺失"
    assert result["total"] == len(REQUIRED_ITEM_IDS)


# ---------------------------------------------------------------------------
# 4. _repair_json 的 scene 必须由调用方显式指定
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repair_json_default_scene_is_merge(db_conn, monkeypatch):
    seen = {}

    async def fake_chat(messages, **kwargs):
        seen["scene"] = kwargs.get("scene")
        return "not-json"  # 触发兜底返回 "{}"

    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)

    out = await ba._repair_json("{bad", [{"role": "user", "content": "x"}])
    assert out == "{}"
    assert seen["scene"] == "bid_analysis_merge", "未显式指定时沿用合并场景默认值"


@pytest.mark.asyncio
async def test_repair_json_accepts_explicit_scene(db_conn, monkeypatch):
    seen = {}

    async def fake_chat(messages, **kwargs):
        seen["scene"] = kwargs.get("scene")
        return '{"ok":true}'

    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)

    out = await ba._repair_json("{bad", [{"role": "user", "content": "x"}],
                                scene="bid_analysis")
    assert out == '{"ok":true}'
    assert seen["scene"] == "bid_analysis"


@pytest.mark.asyncio
async def test_single_call_repair_uses_bid_analysis_scene(db_conn, monkeypatch):
    """单项提取的 JSON 修复必须记入 bid_analysis，不得混入合并场景。"""
    seen: list[str | None] = []

    async def fake_chat(messages, **kwargs):
        seen.append(kwargs.get("scene"))
        return "{not valid json"

    async def fake_repair(bad_json, original_messages, scene="bid_analysis_merge"):
        seen.append(scene)
        return "{}"

    monkeypatch.setattr(ba, "chat_with_fallback", fake_chat)
    monkeypatch.setattr(ba, "_repair_json", fake_repair)

    await ba._run_single_call(
        get_item_def("projectBasicInfo"), "本项目建筑面积 12000 平方米。",
        output_type="json")

    assert seen and seen[0] == "bid_analysis"
    assert "bid_analysis" in seen, "修复调用必须显式指定场景"
    assert "bid_analysis_merge" not in seen, (
        f"单项提取的修复被错记到合并场景：{seen}")