"""AI 语义检查侧消费全局事实回归测试（2026-10-03）。

锁定 G3 数据断链收口的 AI 侧：
- compliance `/check`（compliance_check_system）与 `/expert-review`
  （expert_review_system）两条 AI 链路经跨模块只读桥接
  load_resolved_facts_for_scope 装配 {global_facts}（与预检/正文/导出
  同一 fail-closed 门控，无事实/失败降级为「（无）」，不阻断 AI 检查）；
- 提示词契约表同步（启动期 check_prompt_variables 防漂移的静态前提）；
- 门控 parity：未确认模拟值/矛盾值不得进入 AI 提示词（与注入门控同口径）；
- review_autofix 既有事实链路 parity 锁（router → build_global_facts_text
  → 提示词 {global_facts}），防止被静默摘除。
AI 调用一律 mock（collect_json_response），真实 AI 联调由独立脚本执行。
"""
import inspect
import json
import uuid

import pytest

import app.db as _appdb
from app.db import close_db, get_conn, init_db
import app.routers.compliance as compliance
from app.models import ComplianceCheckIn, ExpertReviewIn
from app.routers import review_autofix as review_autofix_router
from app.services.ai.prompts._registry import render


@pytest.fixture
async def db_ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "ai-facts.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,type,word_budget) VALUES(?,?,?,?,?)",
        (sid, pid, "综合楼项目基坑开挖专项施工方案", "", 0))
    await db.commit()
    yield db, pid, sid
    await close_db()


async def _insert_section(db, sid, title="工程概况", content="基坑开挖深度 5.6m。",
                          sort_order=0):
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " description, level, status, word_count, word_budget, content,"
        " review_status, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, sid, "", "", title, "", 1, "done",
         len(content), 0, content, "", sort_order))


async def _insert_fact(db, pid, sid, *, name, value, unit="",
                       simulated=0, resolved=1, conflict=0, stale=0):
    content = f"- **{name}**: {value}"
    await db.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, "
        "group_title, title, content, category, source_ref, is_simulated, "
        "confidence, is_resolved, has_conflict, conflict_keys, fact_key, "
        "value_unit, is_stale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, pid, sid, "g1", "设计参数", name, content, "design",
         '[{"file":"招标文件","quote":"x"}]', simulated, 0.95, resolved,
         conflict, "", name, unit, stale))
    await db.commit()


# ---------------------------------------------------------------------------
# 提示词与契约表
# ---------------------------------------------------------------------------
def test_prompt_contract_includes_global_facts():
    """两条 AI 提示词必须含 {global_facts} 且契约表已同步（启动期防漂移前提）。"""
    from app.services.ai.prompts._registry import PROMPT_VARIABLE_CONTRACTS as C
    assert "global_facts" in C["compliance_check_system"]
    assert "global_facts" in C["expert_review_system"]
    src = render("compliance_check_system", scheme_name="s", scheme_type="t",
                 global_facts="X", checklist="[]", content="c")
    assert "【项目关键事实" in src and "X" in src
    src2 = render("expert_review_system", scheme_name="s", scheme_type="t",
                  global_facts="X", outline_tree="[]", attachments="[]",
                  check_items="i")
    assert "X" in src2


def test_facts_prompt_text_format_and_empty():
    out = compliance._facts_prompt_text([
        {"name": "基坑开挖深度", "value": "5.6", "value_unit": "m",
         "is_safety_critical": 1},
        {"name": "", "value": "应被跳过"},
        {"name": "支护形式", "value": "", "value_unit": ""},
    ])
    assert "- 基坑开挖深度：5.6 m（安全关键）" in out
    assert "- 支护形式：（值缺失）" in out
    assert "应被跳过" not in out
    assert compliance._facts_prompt_text([]) == "（无）"
    assert compliance._facts_prompt_text(None) == "（无）"
    assert compliance._facts_prompt_text([{"name": "n", "value": ""}]) != "（无）"


def test_facts_prompt_text_cap():
    facts = [{"name": f"参数{i}", "value": "v" * 50} for i in range(200)]
    out = compliance._facts_prompt_text(facts)
    assert len(out) <= compliance.FACTS_PROMPT_CAP
    assert out  # 有事实时不为空


def test_facts_prompt_text_cap_keeps_every_fact_line():
    """超支纠偏的双重承诺：总量 ≤ CAP 且**不丢条**（每条至少留代表内容）。

    回归锁（2026-10-03 · R38）：分配器「每项保底 2×等比」在 n=200 时使
    总配额达 2×budget，旧实现总长 6199 > 3000；若纠偏退化为头部硬切片，
    尾部事实整条消失 → 行数断言定向失败。
    """
    facts = [{"name": f"参数{i}", "value": "v" * 50} for i in range(200)]
    out = compliance._facts_prompt_text(facts)
    assert len(out) <= compliance.FACTS_PROMPT_CAP
    lines = out.split("\n")
    assert len(lines) == 200, f"丢条：{len(lines)} < 200"
    assert all(ln.strip() for ln in lines)


def test_join_with_budget_small_n_keeps_proportional_quotas():
    """正常少量条目场景不被均匀硬上限误伤：逐行长度仍等于分配器等比份额。

    纠偏只在 sum(quota 截完) + 换行 > budget 时介入；此处不介入，
    若退化为无条件 per-line-cap，长行份额被压 → 本例定向失败。
    """
    from app.routers.sse_handlers import _allocate_char_budgets
    lines = ["x" * 900, "y" * 90, "z" * 9]
    budget = 500
    quotas = _allocate_char_budgets([len(x) for x in lines], budget)
    expected = [min(int(q or 0), len(x)) for q, x in zip(quotas, lines)]
    assert sum(expected) + len(lines) - 1 <= budget  # 前提：未超支，纠偏不应介入
    out = compliance._join_with_budget(lines, budget)
    assert [len(p) for p in out.split("\n")] == expected


# ---------------------------------------------------------------------------
# /check 集成（AI mock）
# ---------------------------------------------------------------------------
async def test_compliance_check_injects_confirmed_facts(db_ctx, monkeypatch):
    db, pid, sid = db_ctx
    await _insert_section(db, sid)
    await _insert_fact(db, pid, sid, name="基坑开挖深度", value="5.6m", unit="m")
    seen: list = []

    async def fake_ai(messages, validator, **kwargs):
        seen.append(messages)
        return ({"results": [{"rule_id": "CMP-01", "item": "工程概况",
                              "hit": True, "severity": "low",
                              "evidence": "x", "suggestion": ""}]}, "gpt")

    monkeypatch.setattr(compliance, "collect_json_response", fake_ai)
    body = ComplianceCheckIn(scheme_id=sid, checklist=["工程概况"])
    res = await compliance.compliance_check(body, db)
    assert res["results"], "AI mock 返回应被消费"
    sys_text = seen[0][0]["content"]
    assert "基坑开挖深度" in sys_text and "5.6m" in sys_text, \
        "/check 的 AI 提示词未注入已确认事实"
    assert "（无）" not in sys_text


async def test_compliance_check_gate_excludes_unconfirmed(db_ctx, monkeypatch):
    """未确认模拟值/矛盾值与未过期口径一致：不得进入 AI 提示词。"""
    db, pid, sid = db_ctx
    await _insert_section(db, sid)
    await _insert_fact(db, pid, sid, name="已确认参数", value="12m")
    await _insert_fact(db, pid, sid, name="模拟参数", value="99m",
                       simulated=1, resolved=0)
    await _insert_fact(db, pid, sid, name="矛盾参数", value="77m", conflict=1)
    seen: list = []

    async def fake_ai(messages, validator, **kwargs):
        seen.append(messages)
        return ({"results": [{"rule_id": "CMP-01", "item": "i", "hit": True,
                              "severity": "low", "evidence": "e",
                              "suggestion": ""}]}, "gpt")

    monkeypatch.setattr(compliance, "collect_json_response", fake_ai)
    await compliance.compliance_check(ComplianceCheckIn(scheme_id=sid), db)
    sys_text = seen[0][0]["content"]
    assert "已确认参数" in sys_text
    assert "模拟参数" not in sys_text and "矛盾参数" not in sys_text


async def test_compliance_check_without_facts_degrades(db_ctx, monkeypatch):
    """无事实 → 降级为「（无）」，AI 链路照常执行（向后兼容）。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid)
    seen: list = []

    async def fake_ai(messages, validator, **kwargs):
        seen.append(messages)
        return ({"results": [{"rule_id": "CMP-01", "item": "i", "hit": True,
                              "severity": "low", "evidence": "e",
                              "suggestion": ""}]}, "gpt")

    monkeypatch.setattr(compliance, "collect_json_response", fake_ai)
    await compliance.compliance_check(ComplianceCheckIn(scheme_id=sid), db)
    assert "（无）" in seen[0][0]["content"]


async def test_compliance_check_failsoft_on_bridge_error(db_ctx, monkeypatch):
    """桥接异常（mock 抛错）→ 降级「（无）」，不阻断 AI 检查。"""
    db, _pid, sid = db_ctx
    await _insert_section(db, sid)
    seen: list = []

    async def fake_ai(messages, validator, **kwargs):
        seen.append(messages)
        return ({"results": [{"rule_id": "CMP-01", "item": "i", "hit": True,
                              "severity": "low", "evidence": "e",
                              "suggestion": ""}]}, "gpt")

    async def boom(db, scheme_id=""):
        raise RuntimeError("bridge down")

    monkeypatch.setattr(compliance, "load_resolved_facts_for_scope", boom)
    monkeypatch.setattr(compliance, "collect_json_response", fake_ai)
    res = await compliance.compliance_check(ComplianceCheckIn(scheme_id=sid), db)
    assert res["results"]
    assert "（无）" in seen[0][0]["content"]


# ---------------------------------------------------------------------------
# /expert-review 集成（AI mock）
# ---------------------------------------------------------------------------
async def test_expert_review_injects_confirmed_facts(db_ctx, monkeypatch):
    db, pid, sid = db_ctx
    await _insert_section(db, sid)
    await _insert_fact(db, pid, sid, name="基坑开挖深度", value="5.6m", unit="m")
    seen: list = []

    async def fake_ai(messages, validator, **kwargs):
        seen.append(messages)
        return ({"score": 80, "ready": ["工程概况"], "missing": [],
                 "suggestions": []}, "gpt")

    monkeypatch.setattr(compliance, "collect_json_response", fake_ai)
    res = await compliance.expert_review(ExpertReviewIn(scheme_id=sid), db)
    assert res["score"] == 80
    sys_text = seen[0][0]["content"]
    assert "基坑开挖深度" in sys_text and "5.6m" in sys_text, \
        "/expert-review 的 AI 提示词未注入已确认事实"


async def test_expert_review_without_facts_degrades(db_ctx, monkeypatch):
    db, _pid, sid = db_ctx
    await _insert_section(db, sid)
    seen: list = []

    async def fake_ai(messages, validator, **kwargs):
        seen.append(messages)
        return ({"score": 80, "ready": [], "missing": [],
                 "suggestions": []}, "gpt")

    monkeypatch.setattr(compliance, "collect_json_response", fake_ai)
    await compliance.expert_review(ExpertReviewIn(scheme_id=sid), db)
    assert "（无）" in seen[0][0]["content"]


# ---------------------------------------------------------------------------
# 接线静态锁
# ---------------------------------------------------------------------------
def test_wiring_static_locks():
    """两条 AI 链路的事实装配不得被静默摘除；helper 必须走唯一桥接出口。"""
    src_check = inspect.getsource(compliance.compliance_check)
    assert "_load_facts_prompt_text" in src_check
    assert "global_facts=" in src_check
    src_expert = inspect.getsource(compliance.expert_review)
    assert "_load_facts_prompt_text" in src_expert
    assert "global_facts=" in src_expert
    src_helper = inspect.getsource(compliance._load_facts_prompt_text)
    assert "load_resolved_facts_for_scope" in src_helper, \
        "事实装配必须经 facts_extractor 唯一桥接出口，禁止直查 global_facts"
    assert "_facts_prompt_text" in src_helper


# ---------------------------------------------------------------------------
# review_autofix 既有事实链路 parity 锁（防静默摘除）
# ---------------------------------------------------------------------------
def test_review_autofix_facts_chain_parity():
    """review_autofix AI 链路的事实消费已是既有能力（router 装配 +
    提示词 {global_facts} 占位符），本轮只加锁不加行为。"""
    src_router = inspect.getsource(review_autofix_router)
    assert "build_global_facts_text" in src_router, \
        "review_autofix 路由层事实装配被摘除"
    user_prompt = render(
        "review_autofix_user", scheme_name="s", scheme_type="t",
        section_id="x", section_title="t", global_facts="FACTS_MARK",
        standards_text="st", rule_id="R", rule_title="T", issue="i",
        targets="[]", instruction="ins", must_not_contain="",
        must_contain="", section_content="c")
    assert "FACTS_MARK" in user_prompt
