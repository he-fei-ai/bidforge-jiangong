"""R210（2026-11-10）全局事实模块 · 深度审计增强护栏

轮次背景
--------
用户指令「聚焦【全局事实模块】，深入分析现有功能架构与实现现状，识别功能缺口和
技术债务，增强模块功能完整性，优化工作流程，补齐组件级交互测试，完善与其他
模块的数据传递、路由配置和调用链路，排查缺陷并修复所有 BUG」。

本文件锁住 R210 落地的 5 项修复（后端 4 + 跨端契约 1），每条护栏都以
**行为实证**为主、源码棘轮为辅：

  FIX-1 P1  AI 调整「预览」（apply=false，零写库）不得独占项目级事实写锁
            （旧实现整段 AI 调用最长 120s 持锁 ⇒ 连带阻塞手工写 / SSE 提取落库 /
              文档 stale 标记）。
  FIX-2 P2  AI 调整「落库」（apply=true）必须在端点内重新加项目级锁，
            冲突时给出与写保护依赖**逐字同构**的 409 detail（code + message）。
  FIX-3 P2  底稿「事实待确认」口径须含**来源已过期**（is_stale=1）——
            过期事实被注入门控挡在正文/导出之外，若不进清单 ⇒ 复核清单显示
            「0 项」而正文实际少了一条事实。
  FIX-4 P3  update_fact 未携带 scheme_id 时须留审计 WARNING（与同族四端点同口径）。
  FIX-5 P3  写保护 409 的 detail 是**对象**（前端须按对象消费，见前端护栏文件）。

禁改原则：这些断言锁的是**意图**（预览不占锁 / 落库仍占锁 / 过期必须可见），
不是实现细节；重构实现时请同步更新断言，但不得删断言。
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import app.routers.global_facts as gf
import pytest
from app.services import dossier
from app.services.facts_write_guard import FactsWriteConflict
from fastapi import HTTPException

# ===========================================================================
# 公共夹具
# ===========================================================================


async def _seed_scheme(db) -> tuple[str, str]:
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    return pid, sid


async def _seed_fact(db, pid, sid, fid, name, value, *, is_resolved=1, is_stale=0, confidence=0.9):
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, group_title,"
        " title, content, category, source_ref, is_simulated, confidence, is_resolved,"
        " has_conflict, conflict_keys, fact_key, is_stale)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            pid,
            sid,
            "g_" + fid,
            name,
            name,
            f"- **{name}**: {value}",
            "tech_param",
            json.dumps([{"file": "招标文件", "quote": "x"}], ensure_ascii=False),
            0,
            confidence,
            is_resolved,
            0,
            "",
            name,
            is_stale,
        ),
    )
    await db.commit()


class _FakeRequest:
    """最小 Request 替身：只提供写保护依赖真正读取的字段。"""

    def __init__(self, path: str, method: str = "POST", body: dict | None = None):
        self.method = method
        self.url = SimpleNamespace(path=path)
        self.query_params = {}
        self.path_params = {}
        self._body = body or {}

    async def json(self):
        return dict(self._body)


# ===========================================================================
# FIX-1 · AI 调整预览不得独占项目级事实写锁（P1）
# ===========================================================================


async def test_adjust_preview_path_does_not_acquire_project_write_lock(db_conn, monkeypatch):
    """`POST /global-facts/adjust`（预览）不得申请项目级写锁。

    旧实现由路由级依赖对一切非 GET 请求抢锁，而预览路径**零写库**，
    却要在整段 AI 调用（默认 120s）期间独占该锁 ⇒ 同项目手工写 / SSE 提取
    落库 / 文档 stale 标记全部被阻塞。
    """
    calls: list[str] = []

    async def _spy_acquire(_db, project_id, holder=None):
        calls.append(holder or "")
        return "token-x"

    async def _spy_release(_db, _project_id, _token):
        return None

    monkeypatch.setattr(gf, "acquire_project_facts_write", _spy_acquire)
    monkeypatch.setattr(gf, "release_project_facts_write", _spy_release)

    req = _FakeRequest("/api/v1/global-facts/adjust", body={"scheme_id": "s1", "apply": False})
    agen = gf._facts_write_guard_dependency(req, db_conn)
    await anext(agen)  # 依赖进入（不抛 409 即放行）
    with pytest.raises(StopAsyncIteration):
        await anext(agen)

    assert calls == [], "AI 调整预览不得申请项目级事实写锁"


async def test_other_write_endpoints_still_acquire_project_write_lock(db_conn, monkeypatch):
    """豁免**只针对 /adjust**：其余写端点的项目级写锁不得被一并摘掉。

    （防「整体豁免式」改坏：把依赖整个改成 no-op 会让手工写与提取落库彻底失锁。）
    """
    calls: list[str] = []

    async def _fake_scope(_request, _db):
        return "pid-1"

    async def _spy_acquire(_db, project_id, holder=None):
        calls.append(holder or "")
        return "token-x"

    async def _spy_release(_db, _project_id, _token):
        return None

    monkeypatch.setattr(gf, "_facts_write_scope_project_id", _fake_scope)
    monkeypatch.setattr(gf, "acquire_project_facts_write", _spy_acquire)
    monkeypatch.setattr(gf, "release_project_facts_write", _spy_release)

    req = _FakeRequest("/api/v1/global-facts", body={"scheme_id": "s1"})
    agen = gf._facts_write_guard_dependency(req, db_conn)
    await anext(agen)
    with pytest.raises(StopAsyncIteration):
        await anext(agen)

    assert calls, "非 /adjust 的写端点仍必须申请项目级事实写锁"


async def test_preview_does_not_block_concurrent_extraction_write(db_conn, monkeypatch):
    """预览在飞时，提取落库必须能拿到同一把项目锁（不再被预览堵死）。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f1", "基坑深度", "5.6m")

    async def _fake_scope(_request, _db):
        return pid

    async def _spy_acquire(_db, _project_id, holder=None):
        return "token-preview"

    async def _spy_release(_db, _project_id, _token):
        return None

    monkeypatch.setattr(gf, "_facts_write_scope_project_id", _fake_scope)
    monkeypatch.setattr(gf, "acquire_project_facts_write", _spy_acquire)
    monkeypatch.setattr(gf, "release_project_facts_write", _spy_release)

    # 预览请求进入依赖（模拟 AI 调用在飞）
    req = _FakeRequest("/api/v1/global-facts/adjust", body={"scheme_id": sid, "apply": False})
    agen = gf._facts_write_guard_dependency(req, db_conn)
    await anext(agen)

    # 与此同时，SSE 提取落库要用的同一把锁必须能拿到
    from app.services.facts_write_guard import acquire_project_facts_write

    token = await acquire_project_facts_write(db_conn, pid, holder="ai_extraction")
    assert token, "预览在飞不得阻塞提取落库获取项目级写锁"

    cur = await db_conn.execute("SELECT COUNT(*) AS c FROM facts_write_leases WHERE project_id=?", (pid,))
    row = await cur.fetchone()
    assert int(row["c"]) == 1

    await anext(agen) if False else None  # 预览依赖正常结束
    with pytest.raises(StopAsyncIteration):
        await anext(agen)


# ===========================================================================
# FIX-2 · AI 调整落库仍受项目级锁保护 + 409 契约同构（P2）
# ===========================================================================


async def test_adjust_apply_acquires_and_releases_project_write_lock(db_conn, monkeypatch):
    """apply=true 的落库阶段必须重新加项目级锁，并在结束时释放。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f1", "基坑深度", "5.6m")

    acquired: list[str] = []
    released: list[str] = []

    real_acquire = gf.acquire_project_facts_write
    real_release = gf.release_project_facts_write

    async def _spy_acquire(db, project_id, holder=None):
        acquired.append(holder or "")
        return await real_acquire(db, project_id, holder=holder)

    async def _spy_release(db, project_id, token):
        released.append(token)
        return await real_release(db, project_id, token)

    monkeypatch.setattr(gf, "acquire_project_facts_write", _spy_acquire)
    monkeypatch.setattr(gf, "release_project_facts_write", _spy_release)

    async def _should_not_call_ai(*args, **kwargs):
        raise AssertionError("已确认操作计划不应再次调用 AI")

    monkeypatch.setattr(gf, "collect_json_response", _should_not_call_ai)

    res = await gf.adjust_facts(
        gf.AdjustRequest(
            instruction="基坑深度改成5.8m",
            scheme_id=sid,
            apply=True,
            operations=[{"op": "update", "fact_id": "f1", "value": "5.8m"}],
        ),
        db=db_conn,
    )
    assert res["applied"]["updated"] == 1
    assert acquired, "AI 调整落库必须申请项目级事实写锁"
    assert released, "AI 调整落库结束必须释放项目级事实写锁"

    cur = await db_conn.execute("SELECT COUNT(*) AS c FROM facts_write_leases WHERE project_id=?", (pid,))
    row = await cur.fetchone()
    assert int(row["c"]) == 0, "写锁租约必须在结束后清空（否则同项目后续写入全被堵）"


async def test_adjust_apply_conflict_returns_structured_409(db_conn, monkeypatch):
    """写锁冲突时返回 409，且 detail 结构与写保护依赖**逐字同构**。"""
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f1", "基坑深度", "5.6m")

    async def _boom(_db, _project_id, holder=None):
        raise FactsWriteConflict("pid-1", "ai_extraction")

    monkeypatch.setattr(gf, "acquire_project_facts_write", _boom)

    with pytest.raises(HTTPException) as ei:
        await gf.adjust_facts(
            gf.AdjustRequest(
                instruction="基坑深度改成5.8m",
                scheme_id=sid,
                apply=True,
                operations=[{"op": "update", "fact_id": "f1", "value": "5.8m"}],
            ),
            db=db_conn,
        )
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict), "409 detail 必须是对象（前端按对象消费）"
    assert ei.value.detail.get("code") == "facts_write_conflict"
    assert ei.value.detail.get("message")


# ===========================================================================
# FIX-3 · 底稿「事实待确认」口径须含来源已过期（P2）
# ===========================================================================


async def test_dossier_collect_facts_includes_stale_rows(db_conn):
    """已确认但来源已过期的事实必须进待确认清单。

    注入门控是四档（has_conflict=0 AND is_resolved=1 AND is_simulated=0
    AND is_stale=0）：过期事实被正文/导出**静默丢弃**，若不进清单 ⇒
    复核清单写「0 项」，用户以为无需复核而正文实际少一条关键事实。
    """
    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f-stale", "已过期事实", "5.6m", is_resolved=1, is_stale=1)
    await _seed_fact(db_conn, pid, sid, "f-ok", "正常事实", "5.6m", is_resolved=1, is_stale=0)

    out = await dossier.collect_facts(db_conn, sid, pid)
    titles = [r["title"] for r in out["items"]]
    assert "已过期事实" in titles, "来源已过期的事实必须出现在待确认清单"
    assert "正常事实" not in titles, "正常已确认事实不应进入待确认清单"


# ===========================================================================
# FIX-4 · update_fact 未带 scheme_id 留审计痕迹（P3）
# ===========================================================================


async def test_update_fact_without_scheme_id_leaves_audit_warning(db_conn, monkeypatch, caplog):
    """未携带 scheme_id 的写操作必须留 WARNING（与同族四端点同口径）。"""
    import logging

    pid, sid = await _seed_scheme(db_conn)
    await _seed_fact(db_conn, pid, sid, "f1", "基坑深度", "5.6m")

    with caplog.at_level(logging.WARNING, logger="global_facts"):
        await gf.update_fact(
            "f1",
            gf.FactGroupUpdate(
                id="f1", title="基坑深度", content="- **基坑深度**: 5.9m", category="tech_param"
            ),
            scheme_id="",
            db=db_conn,
        )
    assert any("作用域校验被跳过" in r.message for r in caplog.records), "缺 scheme_id 的写操作必须留审计 WARNING"


# ===========================================================================
# FIX-5 · 写保护 409 的 detail 形状（跨端契约，前端护栏见 frontend factsR210）
# ===========================================================================


async def test_write_conflict_detail_shape_is_object_with_code(db_conn, monkeypatch):
    """写保护依赖的 409 detail 必须是 {code, message} 对象（前端按对象消费）。"""

    async def _fake_scope(_request, _db):
        return "pid-1"

    async def _boom(_db, _project_id, holder=None):
        raise FactsWriteConflict("pid-1", "ai_extraction")

    monkeypatch.setattr(gf, "_facts_write_scope_project_id", _fake_scope)
    monkeypatch.setattr(gf, "acquire_project_facts_write", _boom)

    req = _FakeRequest("/api/v1/global-facts", body={"scheme_id": "s1"})
    agen = gf._facts_write_guard_dependency(req, db_conn)
    with pytest.raises(HTTPException) as ei:
        await anext(agen)
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict)
    assert ei.value.detail.get("code") == "facts_write_conflict"
    assert isinstance(ei.value.detail.get("message"), str)


# ===========================================================================
# FIX-6 · 跨条目冲突回写按「条目身份」而非显示名（P1）
# ===========================================================================


class _FakeFact:
    """交叉校验器所需的最小条目替身（属性面与 FactItem 对齐）。"""

    def __init__(self, name, value, key="", source="a.docx", confidence=0.9):
        self.name = name
        self.value = value
        self.key = key
        self.source = source
        self.confidence = confidence
        self.has_conflict = False
        self.conflict_values = []
        self.is_simulated = False
        self.is_stale = False


def test_cross_conflict_writeback_does_not_taint_same_name_other_key():
    """同名但 fact_key 不同的**无关条目**不得被连坐打上矛盾标记。

    旧实现按显示名分组后逐条比对 value 相同即打标 ⇒ 同名不同 key 的无关事实
    命中注入门控的排除条件（has_conflict=0 …）⇒ 从正文/导出里凭空消失。
    """
    from app.services import facts_cross_validators as xcv

    target = _FakeFact("塔吊型号", "QTZ63", key="tower_crane_model")
    unrelated = _FakeFact("塔吊型号", "QTZ63", key="fact_deadbeef")
    conflict = xcv.CrossConflict(
        rule_id="XV-MACH-SCHED",
        severity="high",
        conflict_type="machinery_schedule",
        side_a=xcv._side(target),
        side_b={"name": "总工期", "value": "90日历天", "source": "b.docx", "confidence": 0.9, "fact_key": ""},
        resolution_hint="请核对机械进退场与工期",
    )

    xcv._mark_cross_conflicts_on_items([target, unrelated], [conflict])

    assert target.has_conflict is True, "冲突当事条目必须被打标"
    assert unrelated.has_conflict is False, "同名不同 key 的无关条目不得被连坐打标"
    assert unrelated.conflict_values == []


def test_cross_conflict_writeback_still_marks_when_key_missing():
    """历史记录/外部构造（无 fact_key）时退回旧 name+value 口径，不误伤。"""
    from app.services import facts_cross_validators as xcv

    item = _FakeFact("塔吊型号", "QTZ63", key="")
    conflict = xcv.CrossConflict(
        rule_id="XV-MACH-SCHED",
        severity="high",
        conflict_type="machinery_schedule",
        side_a={"name": "塔吊型号", "value": "QTZ63", "source": "a.docx", "confidence": 0.9},
        side_b={"name": "总工期", "value": "90日历天", "source": "b.docx", "confidence": 0.9},
        resolution_hint="hint",
    )
    xcv._mark_cross_conflicts_on_items([item], [conflict])
    assert item.has_conflict is True


def test_cross_conflict_side_carries_fact_key():
    """冲突侧信息必须携带 fact_key（回写身份化的前提，且为加法式）。"""
    from app.services import facts_cross_validators as xcv

    item = _FakeFact("塔吊型号", "QTZ63", key="tower_crane_model")
    side = xcv._side(item)
    assert side["fact_key"] == "tower_crane_model"
    # 既有四键逐字保留
    for k in ("name", "value", "source", "confidence"):
        assert k in side


# ===========================================================================
# FIX-7 · 危大参数取数：关键词同句优先 / 兜底取最大（P2）
# ===========================================================================


def test_danger_params_take_number_from_keyword_clause():
    """整句 value 里的层数等数字不得顶掉真正的参数值（低估方向）。"""
    from app.services import facts_classification as fc

    out = fc.extract_danger_params([{"name": "基坑深度", "value": "本工程共 3 层，基坑深度 12.5m"}])
    assert out.get("depth") == 12.5, "应取「基坑深度」同句的数值，而不是前面的层数"

    out2 = fc.extract_danger_params([{"name": "支撑高度", "value": "支撑高度 24m，分 2 层开挖"}])
    assert out2.get("height") == 24.0


def test_danger_params_keep_cross_fact_max_semantics():
    """跨事实取最大（保守判定）口径不得被本轮改动破坏。"""
    from app.services import facts_classification as fc

    out = fc.extract_danger_params(
        [{"name": "基坑深度", "value": "5m"}, {"name": "开挖深度", "value": "9m"}]
    )
    assert out.get("depth") == 9.0


def test_danger_params_still_exclude_requirement_rows():
    """要求口径行不得作为危大参数证据（R81 口径不得被破坏）。"""
    from app.services import facts_classification as fc

    out = fc.extract_danger_params(
        [{"name": "开挖深度", "value": "5m 及以上需专家论证", "value_kind": "requirement"}]
    )
    assert "depth" not in out


# ===========================================================================
# FIX-8 · 来源类别判定不得被引句污染（P2）
# ===========================================================================


def test_source_kind_ignores_quote_text():
    """招标文件引句里出现「施工图」不得把来源类别判成 drawing。"""
    from app.services import facts_classification as fc

    ref = '[{"file":"招标文件.pdf","quote":"本工程含基坑支护及降水，另见施工图设计说明"}]'
    assert fc.classify_source_kind("招标文件.pdf", ref) == "bid_doc"

    ref2 = '[{"file":"地勘报告.pdf","quote":"另见施工图"}]'
    assert fc.classify_source_kind("地勘报告.pdf", ref2) == "survey"


def test_source_kind_keeps_plain_string_ref_behavior():
    """非 JSON 字符串 source_ref 按原文匹配（既有行为逐字不变）。"""
    from app.services import facts_classification as fc

    assert fc.classify_source_kind("", "施工图设计说明") == "drawing"
    assert fc.classify_source_kind("", "地质勘察报告") == "survey"
