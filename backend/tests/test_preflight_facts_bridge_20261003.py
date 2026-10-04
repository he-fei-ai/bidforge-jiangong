"""全局事实桥接 · 预检事实反哺危大判定（SAF-08）回归测试（2026-10-03）。

锁定 G3 数据断链收口：
- PreflightContext.facts（路由层经 load_resolved_facts_for_scope 装配，
  与正文/导出同一 fail-closed 门控）→ check_hazard_params 事实反哺；
- 判据同源：与 /global-facts/danger-check 共用 facts_classification.danger_check；
- 只报真实阈值命中（缺参保守分支不计入）；关键词通道已危大不重复报；
- 事实签名并入预检/总检的**进程内**缓存键（落库 content_fingerprint 语义不变）。
"""
import inspect
import uuid

import app.db as _appdb
import app.routers.compliance as compliance
import pytest
from app.db import close_db, get_conn, init_db
from app.routers.compliance import _build_preflight_context, _facts_signature
from app.services import audit_rules as ar
from app.services.facts_classification import danger_check, extract_danger_params
from app.services.preflight_engine import (
    PreflightContext,
    check_hazard_params,
    run_preflight,
)
from app.services.scheme_classification import is_hazardous_by_keywords


@pytest.fixture
async def db_ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "facts-bridge.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,type,word_budget) VALUES(?,?,?,?,?)",
        (sid, pid, "综合楼项目专项施工方案", "", 0))
    await db.commit()
    yield db, pid, sid
    await close_db()


async def _insert_section(db, sid, title="工程概况", content="正文内容",
                          sort_order=0):
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", "", title, "", 1, "done",
         len(content), 0, content, "", sort_order))


async def _insert_fact(db, pid, sid, *, name, value, unit="",
                       simulated=0, resolved=1, conflict=0, stale=0):
    fid = uuid.uuid4().hex
    content = f"- **{name}**: {value}"
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key, "
        "value_unit, is_stale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, pid, sid, "g1", "工期安排", name, content, "schedule",
         '[{"file":"招标文件","quote":"x"}]', simulated, 0.95, resolved,
         conflict, "", name, unit, stale))
    await db.commit()
    return fid


# ---------------------------------------------------------------------------
# SAF-08 规则注册
# ---------------------------------------------------------------------------
def test_saf08_rule_registered():
    rule = ar.get_rule("SAF-08")
    assert rule is not None, "SAF-08 未注册到 audit_rules"
    assert rule.dimension == "safety"
    assert rule.severity == "high"
    assert rule.basis, "安全规则必须有行业依据"
    assert "37号" in rule.basis


# ---------------------------------------------------------------------------
# check_hazard_params 判定边界
# ---------------------------------------------------------------------------
#: 名称不含任何危大类目关键词（测试内先断言该前提，防空转）
_NO_KEYWORD_NAME = "综合楼项目专项施工方案"
_HAZARD_FACTS = [{"name": "基坑开挖深度", "value": "16m", "fact_key": "excavation_depth"}]


def _precondition_guards():
    """防空转：样本名称确实不被关键词判危大、事实参数确实达到阈值。"""
    assert not is_hazardous_by_keywords(_NO_KEYWORD_NAME), \
        "样本名称被关键词判为危大，用例前提失效"
    params = extract_danger_params(_HAZARD_FACTS)
    assert params.get("depth") == 16.0, "样本事实参数未抽出 depth=16"
    probe = danger_check(_NO_KEYWORD_NAME, _HAZARD_FACTS,
                         extra_text="基坑开挖深度 16m")
    assert probe["classification"]["is_hazardous"], "样本在 danger_check 下不判危大"


def test_saf08_fires_on_real_threshold_hit():
    _precondition_guards()
    ctx = PreflightContext(scheme_name=_NO_KEYWORD_NAME, facts=_HAZARD_FACTS)
    out = check_hazard_params(ctx)
    assert len(out) == 1
    f = out[0]
    assert f["rule_id"] == "SAF-08"
    assert f["dimension"] == "safety"
    assert f["severity"] == "high"
    assert any(e.startswith("depth=") for e in f["evidence"]), \
        "证据应包含达到阈值的参数（depth=…）"


def test_saf08_silent_when_keyword_channel_already_hazardous():
    _precondition_guards()
    name = "深基坑土方开挖专项施工方案"
    assert is_hazardous_by_keywords(name), "对照名称应命中关键词（前提失效）"
    ctx = PreflightContext(scheme_name=name, facts=_HAZARD_FACTS)
    assert check_hazard_params(ctx) == []


def test_saf08_silent_on_facts_empty():
    ctx = PreflightContext(scheme_name=_NO_KEYWORD_NAME, facts=[])
    assert check_hazard_params(ctx) == []


def test_saf08_silent_below_threshold():
    """参数低于阈值（2m < 3m）→ 不报。"""
    ctx = PreflightContext(
        scheme_name=_NO_KEYWORD_NAME,
        facts=[{"name": "基坑开挖深度", "value": "2m", "fact_key": "excavation_depth"}])
    assert check_hazard_params(ctx) == []


def test_saf08_silent_on_missing_param_conservative_branch():
    """只有类目词、没有定量参数（缺参保守 is_hazardous=True）→ 不报。

    SAF-08 只认真实阈值命中（hazard_reasons 非空），缺参保守分支
    交由 danger-check 的 missing_params 引导补全，不由预检红标。
    样本「基坑支护形式」命中基坑支护类目关键词，但不在 DANGER_PARAM_RULES
    参数关键词内 → 缺参保守分支（前提在下方断言锁定）。
    """
    facts = [{"name": "基坑支护形式", "value": "灌注桩+内支撑",
              "fact_key": "support_type"}]
    probe = danger_check(_NO_KEYWORD_NAME, facts,
                         extra_text="基坑支护形式 灌注桩+内支撑")
    assert probe["classification"]["is_hazardous"], "缺参保守分支应判危大（前提失效）"
    assert not probe["classification"]["hazards"][0].get("hazard_reasons"), \
        "样本应落在缺参保守分支而非真实阈值命中（前提失效）"
    ctx = PreflightContext(scheme_name=_NO_KEYWORD_NAME, facts=facts)
    assert check_hazard_params(ctx) == []


# ---------------------------------------------------------------------------
# run_preflight 集成与向后兼容
# ---------------------------------------------------------------------------
async def test_run_preflight_includes_saf08_with_facts(db_ctx):
    db, pid, sid = db_ctx
    await _insert_section(db, sid)
    await db.commit()
    await _insert_fact(db, pid, sid, name="基坑开挖深度", value="16m", unit="m")
    ctx = await _build_preflight_context(sid, db)
    assert ctx.facts, "装配路径未注入已确认事实"
    rids = [f["rule_id"] for f in run_preflight(ctx)]
    assert "SAF-08" in rids


def test_run_preflight_without_facts_no_saf08():
    """无 facts（既有直接构造 ctx 的调用方）→ 行为逐字不变，无 SAF-08。"""
    ctx = PreflightContext(
        scheme_name=_NO_KEYWORD_NAME,
        sections=[{"id": "s1", "title": "工程概况", "content": "正文",
                   "word_count": 10, "parent_id": ""}])
    assert "SAF-08" not in [f["rule_id"] for f in run_preflight(ctx)]


# ---------------------------------------------------------------------------
# 事实签名（缓存键）与门控一致性
# ---------------------------------------------------------------------------
async def test_facts_signature_scope_and_gate(db_ctx):
    db, pid, sid = db_ctx
    base = await _facts_signature(db, sid)
    assert base.startswith("0:"), "无事实时签名计数应为 0"
    await _insert_fact(db, pid, sid, name="合同工期", value="365天")
    with_fact = await _facts_signature(db, sid)
    assert with_fact != base, "新增已确认事实后签名必须变化"
    # 模拟值（未确认）被 fail-closed 门控排除：不改变签名
    await _insert_fact(db, pid, sid, name="质保期", value="两年", simulated=1,
                       resolved=0)
    assert await _facts_signature(db, sid) == with_fact, \
        "未确认模拟值不得进入签名（与注入门控同口径）"
    # 矛盾值同样被排除
    await _insert_fact(db, pid, sid, name="总工期", value="300天", conflict=1)
    assert await _facts_signature(db, sid) == with_fact


# ---------------------------------------------------------------------------
# 接线静态锁
# ---------------------------------------------------------------------------
def test_wiring_static_locks():
    """三处接线不得被静默摘除：ctx 装配事实 + 双缓存键并入签名 + 引擎注册检查项。"""
    src_build = inspect.getsource(compliance._build_preflight_context)
    assert "load_resolved_facts_for_scope" in src_build
    assert "facts=facts" in src_build
    src_preflight = inspect.getsource(compliance.run_preflight_check)
    assert "_facts_signature" in src_preflight
    src_overview = inspect.getsource(compliance.readiness_overview)
    assert "_facts_signature" in src_overview
    import app.services.preflight_engine as pe
    src_engine = inspect.getsource(pe.run_preflight)
    assert "check_hazard_params" in src_engine
