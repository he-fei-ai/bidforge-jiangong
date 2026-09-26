"""八模块端到端链路硬化回归（2026-09-18 第三轮）。

覆盖本轮修复的确定性 BUG（每条都有判别性，改动前必失败）：
1. ``/charts/generate-ai-image`` 对 aiosqlite.Row 误用 ``.get()`` → 恒 500
2. 审核状态机与前端 NEXT_ACTIONS 对齐（未审核→驳回 / 已通过→重置待审 / 已驳回→通过）
3. 图表僵尸行迁移不再把「结构化数据型」图表误重置为 pending
4. 增量提取的删除范围只用「本次实际重跑段」，跳过段的未确认事实不得被删
5. ``parse_warnings`` 落库序列化必须产出合法 JSON（旧实现对 JSON 串做字节切片）
6. 单条事实编辑改值后必须清除矛盾标记（否则永远被注入门控排除）
7. 废止标准 / 在库标准编号归一化匹配（无空格、全角破折号）
8. 事务内配图超限时正文同步裁剪（防「清单外幽灵图」）
9. 导出缓存指纹包含方案名（改名后封面不得命中旧缓存）
10. 拖拽重排回写 ``sections.level``（与编号保持一致）
11. 复制方案携带 ``chunk_hash``（副本增量提取不得误删复制来的事实）
12. 任务状态内存分支回传 ``message``
13. 扫描件 OCR 页数上限截断必须留痕
"""
import inspect
import json
import sys
import uuid

import pytest

import app.db as _appdb
import app.routers.charts as charts_mod
import app.routers.global_facts as gf
import app.routers.review as rv
import app.services.facts_extractor as fe
import app.services.file_parser as fp
import app.services.preflight_engine as pf
import app.services.standards_registry as sr
from app.db import get_conn, init_db
from app.services.facts_extractor import (
    ExtractionResult, FactGroup, FactItem, persist_extraction,
)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "round3.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.close()


async def _insert_fact(db, pid, sid, *, name, key, chunk_hash,
                       category="schedule", resolved=0, conflict=0,
                       conflict_keys=""):
    fid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key, "
        "chunk_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g1", "工期安排", name, f"- **{name}**: v",
         category, json.dumps([{"file": "doc.docx", "quote": ""}]),
         0, 1.0, resolved, conflict, conflict_keys, key, chunk_hash))
    await db.commit()
    return fid


# ---------------------------------------------------------------------------
# 1. AI 配图端点不得对 sqlite3.Row 调用 .get()
# ---------------------------------------------------------------------------
def test_generate_ai_image_does_not_call_get_on_row():
    """BUG 判别：sqlite3.Row 没有 .get()，旧实现必抛 AttributeError → 500。"""
    src = inspect.getsource(charts_mod.generate_ai_image)
    assert 'row["word_budget"]' in src
    assert 'row.get("word_budget")' not in src


# ---------------------------------------------------------------------------
# 2. 审核状态机
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("frm,to", [
    ("", "rejected"),        # 前端「驳回」按钮
    ("approved", "pending"),  # 前端「重置为待审核」
    ("rejected", "approved"),  # 前端「整改后通过」/ 方案驳回后重新提交
])
def test_review_transitions_align_with_frontend(frm, to):
    rv._validate_transition(frm, to)   # 不抛异常即通过


def test_review_transitions_still_reject_illegal():
    """非法状态值必须被拒；自流转为幂等放行（契约变更，见下）。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        rv._validate_transition("pending", "unknown")
    with pytest.raises(HTTPException):
        rv._validate_transition("pending", "")
    # ✅ 契约变更（2026-09-21）：自流转不再 400，改为幂等放行。
    #    本用例旧断言编码的正是那个 400 —— UI 未刷新时用户重复点击同一状态
    #    （「再次通过」/「重置为待审核」）会被打回，表现为「点了没反应」。
    #    矩阵各集合现已显式包含自身，幂等由各调用方保证（不写 review_records）。
    rv._validate_transition("pending", "pending")


# ---------------------------------------------------------------------------
# 3. 图表僵尸行迁移
# ---------------------------------------------------------------------------
async def test_chart_zombie_migration_keeps_data_charts(ctx):
    db, pid, sid = ctx
    from app.db import _migrate
    sec = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title) VALUES (?,?,?)",
        (sec, sid, "进度计划"))
    # ① 结构化数据型图表（规范信封：mermaid_code 为空 + data 非空）→ 不得被重置
    data_payload = json.dumps({"mermaid_code": "", "data": {"tasks": []},
                               "title": "横道图", "reason": ""}, ensure_ascii=False)
    await db.execute(
        "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type, "
        "needed, purpose, priority, status, data_json) VALUES (?,?,?,?,1,?,?,?,?)",
        (uuid.uuid4().hex, sec, sid, "gantt", "", 5, "generated", data_payload))
    # ② 真正的僵尸行（generated + 无 mermaid_code + 无 data）→ 应被重置为 pending
    await db.execute(
        "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type, "
        "needed, purpose, priority, status, data_json) VALUES (?,?,?,?,1,?,?,?,?)",
        (uuid.uuid4().hex, sec, sid, "flowchart", "", 5, "generated", "{}"))
    await db.commit()

    await _migrate(db)
    await db.commit()

    cur = await db.execute(
        "SELECT chart_type, status FROM chart_predictions WHERE section_id=?", (sec,))
    status = {r["chart_type"]: r["status"] for r in await cur.fetchall()}
    assert status["gantt"] == "generated", "数据型图表不得被迁移误重置"
    assert status["flowchart"] == "pending", "真僵尸行必须被重置"


# ---------------------------------------------------------------------------
# 4. 增量提取删除范围
# ---------------------------------------------------------------------------
async def test_persist_deletion_scope_uses_run_hashes(ctx):
    """跳过段的未确认事实必须保留（用 all 会误删 → 该段不重跑，事实永久丢失）。"""
    db, pid, sid = ctx
    await _insert_fact(db, pid, sid, name="总工期", key="total_duration",
                       chunk_hash="AAA")          # 本轮跳过
    await _insert_fact(db, pid, sid, name="机械数量", key="excavator_count",
                       chunk_hash="BBB", category="machinery")  # 本轮重跑

    item = FactItem(name="挖掘机型号", value="SY215C", key="excavator_model",
                    category="machinery", source="doc.docx", chunk_hash="BBB")
    result = ExtractionResult()
    result.groups = [FactGroup(title="工期安排", category="schedule", items=[
        FactItem(name="总工期", value="360天", key="total_duration",
                 category="schedule", source="doc.docx", chunk_hash="BBB")]),
        FactGroup(title="机械设备", category="machinery", items=[item])]
    result.total_items = 2
    result.chunk_hashes_all = {"AAA", "BBB"}   # 真实管线口径：含跳过段
    result.chunk_hashes_run = {"BBB"}          # 本轮实际重跑段
    await persist_extraction(result, db, pid, sid)

    cur = await db.execute(
        "SELECT title, chunk_hash FROM global_facts WHERE scheme_id=?", (sid,))
    rows = {(r["title"], r["chunk_hash"]) for r in await cur.fetchall()}
    assert ("总工期", "AAA") in rows, "跳过段的未确认事实不得被删除"
    assert ("机械数量", "BBB") not in rows, "重跑段的旧事实应被刷新"
    assert ("挖掘机型号", "BBB") in rows


# ---------------------------------------------------------------------------
# 5. parse_warnings 序列化
# ---------------------------------------------------------------------------
def test_dump_parse_warnings_always_valid_json():
    many = ["告警内容" * 200 for _ in range(50)]
    raw = fp.dump_parse_warnings(many)
    parsed = json.loads(raw)          # 必须能解析（旧实现的字节切片会产出非法 JSON）
    assert isinstance(parsed, list)
    assert 0 < len(parsed) <= fp.MAX_PARSE_WARNINGS
    assert all(len(w) <= fp.MAX_PARSE_WARNING_CHARS for w in parsed)
    assert fp.dump_parse_warnings([]) == "[]"
    # 判别：旧实现确实会截出非法 JSON（防止回归到字符串切片）
    with pytest.raises(json.JSONDecodeError):
        json.loads(json.dumps(many, ensure_ascii=False)[:2000])


# ---------------------------------------------------------------------------
# 6. 单条事实编辑清除矛盾标记
# ---------------------------------------------------------------------------
async def test_item_update_value_clears_conflict(ctx):
    db, pid, sid = ctx
    fid = await _insert_fact(db, pid, sid, name="总工期", key="total_duration",
                             chunk_hash="AAA", resolved=1, conflict=1,
                             conflict_keys='["- **总工期**: 360天"]')
    updated, _ = await gf._apply_item_updates(
        db, [{"fact_id": fid, "value": "300天"}])
    assert updated == 1
    cur = await db.execute(
        "SELECT has_conflict, conflict_keys, content FROM global_facts WHERE id=?",
        (fid,))
    row = await cur.fetchone()
    assert row["has_conflict"] == 0
    assert row["conflict_keys"] == ""
    assert "300天" in row["content"]


# ---------------------------------------------------------------------------
# 7. 标准编号归一化
# ---------------------------------------------------------------------------
def test_abolished_standard_detected_without_space_or_ascii_dash():
    assert sr.find_abolished_codes("依据 GB50202-2002 施工") == ["GB 50202-2002"]
    assert sr.find_abolished_codes("依据 GB 50202—2002 施工") == ["GB 50202-2002"]
    assert sr.find_abolished_codes("现行 GB 50202-2018 施工") == []


def test_known_standard_matches_normalized_form():
    assert sr.is_known_standard("GB55034-2022")
    assert sr.is_known_standard("GB 55034-2022")
    assert not sr.is_known_standard("JGJ46-2005")   # 已废止


def test_preflight_std01_catches_unspaced_abolished_code():
    ctxobj = pf.PreflightContext(
        sections=[{"id": "1", "title": "编制依据",
                   "content": "本方案依据 GB50202-2002 编制。"}])
    findings = pf.check_standards(ctxobj)
    assert any(f["rule_id"] == "STD-01" for f in findings)


# ---------------------------------------------------------------------------
# 7b. CON-01 数值一致性必须按「对象」分组
# ---------------------------------------------------------------------------
def _con01(ctxobj) -> list:
    return [f for f in pf.check_consistency(ctxobj) if f["rule_id"] == "CON-01"]


def test_con01_ignores_different_equipment_objects():
    """不同设备各有各的数量，不是数值矛盾（旧实现必误报）。"""
    ctxobj = pf.PreflightContext(sections=[
        {"id": "1", "title": "施工部署",
         "content": "现场配置塔吊 2 台，施工电梯 2 台，挖掘机 3 台。"}])
    assert _con01(ctxobj) == []


def test_con01_ignores_different_member_concrete_grades():
    ctxobj = pf.PreflightContext(sections=[
        {"id": "1", "title": "材料",
         "content": "柱混凝土强度等级为C40，梁混凝土强度等级为C30。"}])
    assert _con01(ctxobj) == []


def test_con01_still_detects_same_object_conflict():
    ctxobj = pf.PreflightContext(sections=[
        {"id": "1", "title": "施工部署", "content": "现场配置塔吊 2 台。"},
        {"id": "2", "title": "资源配置", "content": "现场配置塔吊 5 台。"}])
    hits = _con01(ctxobj)
    assert hits and "塔吊" in hits[0]["detail"]


def test_scanner_prescan_groups_by_object():
    import app.services.consistency_scanner as cs
    same = [{"id": "a", "title": "部署",
             "content": "配置塔吊 2 台，施工电梯 2 台。"}]
    assert cs.program_prescan(same) == []
    conflict = [{"id": "a", "title": "部署", "content": "配置塔吊 2 台。"},
                {"id": "b", "title": "资源", "content": "配置塔吊 5 台。"}]
    cands = cs.program_prescan(conflict)
    assert any("设备数量" in c["topic"] for c in cands)


# ---------------------------------------------------------------------------
# 8. 事务内配图超限 → 正文同步裁剪
# ---------------------------------------------------------------------------
VALID_FLOWCHART = (
    'flowchart TD\n    A["开始"] --> B["施工"]\n'
    '    B --> C{"验收合格?"}\n    C --> D["结束"]'
)


async def test_apply_plan_clips_content_when_limit_exceeded(ctx):
    db, pid, sid = ctx
    from app.routers._chart_pipeline import (
        _scan_inline_charts, apply_inline_chart_plan, build_inline_chart_plan,
    )
    content = "前文\n```mermaid\n" + VALID_FLOWCHART + "\n```\n后文"
    ct = _scan_inline_charts(content)[0][0]
    # 其它章节已用满该类型全方案限额（默认 3）
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title) VALUES (?,?,?)",
        ("other-sec", sid, "其它"))
    for _ in range(3):
        await db.execute(
            "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type, "
            "needed, purpose, priority, status, data_json) VALUES (?,?,?,?,1,?,?,?,?)",
            (uuid.uuid4().hex, "other-sec", sid, ct, "", 5, "generated", "{}"))
    await db.commit()

    # 计划阶段用「空快照」（模拟并发：锁外各自判定通过）
    _planned, rows = build_inline_chart_plan(
        sid, "sec-x", content, enforce_limits=True, scheme_type_counts={})
    assert rows, "锁外快照下计划应产出待登记行"

    out = await apply_inline_chart_plan(db, "sec-x", rows, content)
    assert "```mermaid" not in out, "超限未登记的图表块必须从正文移除（防幽灵图）"
    cur = await db.execute(
        "SELECT COUNT(*) c FROM chart_predictions WHERE section_id=?", ("sec-x",))
    assert (await cur.fetchone())["c"] == 0


# ---------------------------------------------------------------------------
# 9. 导出缓存指纹
# ---------------------------------------------------------------------------
def _prep_with_scheme_name(name: str) -> dict:
    return {"scheme": {"name": name, "project_id": "p1"},
            "config": {}, "fe_codes": [], "sections": [],
            "chart_fp": [], "global_facts": []}


def test_export_fingerprint_includes_scheme_name():
    import app.routers.export as ex
    h1 = ex._content_fingerprint(_prep_with_scheme_name("方案A"))[1]
    h2 = ex._content_fingerprint(_prep_with_scheme_name("方案B"))[1]
    assert h1 != h2, "改方案名（封面标题）必须改变内容指纹，否则命中旧缓存"


# ---------------------------------------------------------------------------
# 10. 重排回写 level
# ---------------------------------------------------------------------------
async def test_renumber_writes_sections_level(ctx):
    db, pid, sid = ctx
    import app.routers.sections as sec_mod
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title, parent_id, level, "
        "sort_order, outline_json) VALUES (?,?,?,?,?,?,?)",
        ("p1", sid, "第一章", "", 1, 1, '{"id": "1"}'))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, title, parent_id, level, "
        "sort_order, outline_json) VALUES (?,?,?,?,?,?,?)",
        ("c1", sid, "1.1 小节", "p1", 1, 1, '{"id": "1"}'))
    await db.commit()

    await sec_mod.renumber_sections_after_reorder(db, sid)
    cur = await db.execute(
        "SELECT outline_json, level FROM sections WHERE id=?", ("c1",))
    row = await cur.fetchone()
    assert row["level"] == 2, "level 列必须与编号层级同步回写"
    assert json.loads(row["outline_json"])["level"] == 2


# ---------------------------------------------------------------------------
# 11. 复制方案携带 chunk_hash
# ---------------------------------------------------------------------------
async def test_duplicate_scheme_copies_chunk_hash(ctx):
    db, pid, sid = ctx
    import app.routers.schemes as sch_mod
    await _insert_fact(db, pid, sid, name="总工期", key="total_duration",
                       chunk_hash="AAA")
    res = await sch_mod.duplicate_scheme(project_id=pid, scheme_id=sid, db=db)
    new_id = res["id"]
    cur = await db.execute(
        "SELECT chunk_hash FROM global_facts WHERE scheme_id=?", (new_id,))
    hashes = [r["chunk_hash"] for r in await cur.fetchall()]
    assert hashes == ["AAA"], "副本必须继承 chunk_hash，否则会被当作旧数据刷新删除"


# ---------------------------------------------------------------------------
# 12. 任务状态内存分支
# ---------------------------------------------------------------------------
async def test_task_status_memory_branch_exposes_message():
    import app.routers.sse_handlers as sh
    import app.services.ai.task_registry as tr
    tr._tasks["t-mem"] = {"type": "content_generation", "status": "running",
                          "progress": 0.4, "message": "正在生成第 3 章",
                          "scheme_id": "s1"}
    res = await sh.task_status("t-mem")
    assert res["message"] == "正在生成第 3 章"
    assert res["live"] is True


# ---------------------------------------------------------------------------
# 13. OCR 页数上限截断留痕
# ---------------------------------------------------------------------------
def test_pdf_ocr_page_limit_records_truncation(monkeypatch):
    diag: dict = {"warnings": [], "truncated": False}

    class _Pix:
        def tobytes(self, fmt):
            return b"png-bytes"

    class _Page:
        number = 0

        def get_pixmap(self, dpi=200):
            return _Pix()

    class _Doc:
        page_count = 30

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def pages(self, a, b):
            return [_Page()]

    class _Fitz:
        @staticmethod
        def open(*a, **k):
            return _Doc()

    monkeypatch.setitem(sys.modules, "fitz", _Fitz)
    import app.config as cfg
    monkeypatch.setattr(cfg, "settings", type("S", (), {
        "ocr_pdf_max_pages": 20, "ocr_pdf_dpi": 200})())
    import app.services.ocr as ocr_mod
    monkeypatch.setattr(
        ocr_mod, "ocr_bytes_sync",
        lambda payload, **k: type("R", (), {"text": "识别内容"})())

    out = fp._pdf_ocr_fallback(b"%PDF-1.4 fake", diag=diag)
    assert "识别内容" in out
    assert diag["truncated"] is True
    assert any("20 页" in w for w in diag["warnings"])
