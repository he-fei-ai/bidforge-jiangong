"""预算器逐段账面（R186 · 2026-11-07 · 可观测面）。

背景：``allocate_context_budget`` 返回了逐段 ``detail``（原文/保留/省略），
但 ``apply_context_budget`` 只取 ``[0]`` 把它丢掉，且削减**不在提示词里留
任何标记** —— 默认 ``prompt_context_budget=0`` 所以不发作，一旦开启，
模型与用户都看不到被削了什么。

本用例锁住最小可观测方案：

* **逐段账面落日志**：每段一行「段「标签」优先级 N：原文 → 保留 字，省略 M 字」
  （含未削减段），日志是权威可查账目；
* **逐段账面落指标（按标签）**：既有 ``token_budget_truncated`` 记次数，
  新增 ``token_budget_omitted_chars`` 记省略字数；
* **不消耗预算本身**：提示词正文不含账目标记；
* **调整要求（优先级 1）被压缩时回传用户**：``adjust_outline`` 响应新增
  加法式 ``instruction_truncated`` 键（未截断时不出现，既有响应不变）。
"""

import logging

import app.services.ai.prompts._metrics as _metrics
import pytest
from app.services import prompt_governance as _pg

_CUT = "【全局事实】：" + "\n".join("第%03d行 施工参数与要求" % i for i in range(40))
_KB = "【知识库】：" + "\n".join("资料第%02d行" % i for i in range(10))
_OVERSIZED = _CUT + "\n" + _KB


@pytest.fixture(autouse=True)
def _isolate_counters():
    _metrics.reset()
    yield
    _metrics.reset()


def test_each_segment_ledger_is_logged_with_from_to_omitted(caplog):
    with caplog.at_level(logging.INFO, logger="prompt_governance"):
        _out, info = _pg.allocate_context_budget(_OVERSIZED, 120)
    assert info["applied"] is True, "测试前提：预算必须真正生效"
    ledger = [r.getMessage() for r in caplog.records if "上下文预算账面" in r.getMessage()]
    assert len(ledger) == len(info["detail"]), "逐段都必须有账面（含未削减段）"
    for d, msg in zip(info["detail"], ledger):
        assert d["label"] in msg
        assert f"{d['from']} → {d['to']}" in msg
        assert f"省略 {d['from'] - d['to']} 字" in msg


def test_ledger_metrics_by_label_include_omitted_chars():
    _out, info = _pg.allocate_context_budget(_OVERSIZED, 120)
    cut = [d for d in info["detail"] if d["cut"]]
    assert cut, "测试前提：至少一段被削减"
    snap = _metrics.get_snapshot()
    assert "token_budget_omitted_chars" in snap
    for d in cut:
        assert snap["token_budget_truncated"].get(d["label"], 0) == 1
        assert snap["token_budget_omitted_chars"].get(d["label"], 0) == d["from"] - d["to"]


def test_ledger_does_not_consume_prompt_budget():
    """留痕不得写进 prompt：标记会吃掉被削减的空间本身。"""
    out, info = _pg.allocate_context_budget(_OVERSIZED, 120)
    assert info["applied"] is True
    assert len(out) <= 120
    assert "上下文预算" not in out
    assert "省略" not in out
    # 未削减的段头保留，账目只存在于日志/指标
    assert "【全局事实】：" in out


def test_report_entry_returns_detail_and_legacy_wrapper_identical(monkeypatch):
    from app.config import settings
    from app.routers._sse_outline_review import (
        _apply_prompt_context_budget,
        _apply_prompt_context_budget_report,
    )

    text = "【调整要求】：" + "必须完整执行。" * 200
    monkeypatch.setattr(settings, "prompt_context_budget", 120, raising=False)
    out, info = _apply_prompt_context_budget_report(text)
    assert info["applied"] is True and info["detail"]
    # 只取结果的便捷入口与旧行为逐字节一致
    assert out == _apply_prompt_context_budget(text)


def test_adjust_instruction_truncation_surfaces_priority1_ledger():
    from app.routers.sections import _adjust_instruction_truncation

    info = {
        "detail": [
            {"label": "现有目录", "priority": 1, "from": 100, "to": 100, "cut": False},
            {"label": "调整要求", "priority": 1, "from": 200, "to": 40, "cut": True},
        ]
    }
    got = _adjust_instruction_truncation(info)
    assert got == {
        "from": 200,
        "to": 40,
        "omitted": 160,
        "notice": "调整要求超出上下文预算，仅保留前 40 字（原文 200 字，省略 160 字）",
    }
    assert _adjust_instruction_truncation({"detail": []}) is None
    assert (
        _adjust_instruction_truncation(
            {"detail": [{"label": "调整要求", "priority": 1, "from": 50, "to": 50, "cut": False}]}
        )
        is None
    )


@pytest.mark.asyncio
async def test_adjust_outline_returns_instruction_truncation(db_conn, monkeypatch):
    """端到端：预算开启且调整要求被压缩时，响应带加法式账面键。"""
    import uuid

    import app.routers.sections as sec
    from app.config import settings

    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)", (sid, pid, "深基坑专项方案", "目录已确认")
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, title, level, status,"
        " word_count, word_budget, review_status, sort_order)"
        " VALUES (?,?,?,?,?, 'generated', 100, 1500, 'pending', ?)",
        ("R1", sid, "", "工程概况", 1, 0),
    )
    await db_conn.commit()

    async def fake(messages, validate_fn, **kwargs):
        obj = {"outline": [{"id": "R1", "title": "工程概况", "children": []}], "summary": "未改动"}
        issues = validate_fn(obj)
        if issues:
            raise ValueError("校验不通过: " + ";".join(issues))
        return obj, ""

    monkeypatch.setattr(sec, "collect_json_response", fake)
    monkeypatch.setattr(settings, "prompt_context_budget", 300, raising=False)

    res = await sec.adjust_outline(sid, {"instruction": "把第一章细化。" + "必须逐条落实。" * 200}, db=db_conn)
    assert res["ok"] is True
    book = res.get("instruction_truncated")
    assert book, res
    assert book["from"] > book["to"] > 0
    assert book["omitted"] == book["from"] - book["to"]
    assert "调整要求超出上下文预算" in book["notice"]


@pytest.mark.asyncio
async def test_adjust_outline_without_budget_has_no_truncation_key(db_conn, monkeypatch):
    """默认 budget=0：响应逐字节保持既有三键，不出现新增键。"""
    import uuid

    import app.routers.sections as sec

    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)", (sid, pid, "深基坑专项方案", "目录已确认")
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, title, level, status,"
        " word_count, word_budget, review_status, sort_order)"
        " VALUES (?,?,?,?,?, 'generated', 100, 1500, 'pending', ?)",
        ("R1", sid, "", "工程概况", 1, 0),
    )
    await db_conn.commit()

    async def fake(messages, validate_fn, **kwargs):
        obj = {"outline": [{"id": "R1", "title": "工程概况", "children": []}], "summary": "未改动"}
        return obj, ""

    monkeypatch.setattr(sec, "collect_json_response", fake)
    res = await sec.adjust_outline(sid, {"instruction": "改标题"}, db=db_conn)
    assert set(res) == {"ok", "outline", "summary"}


# ---------------------------------------------------------------------------
# 2026-11-07（第 2 条）：物理上限与预算两道刀合并成一条账面
# ---------------------------------------------------------------------------
def test_instruction_truncation_reason_physical_cap_only():
    from app.services.prompt_governance import instruction_truncation

    cap = {"from": 7000, "to": 5999, "omitted": 1001, "limit": 6000}
    got = instruction_truncation(cap, {"applied": False, "budget": 0, "segments": 0})
    assert got == {
        "from": 7000,
        "to": 5999,
        "omitted": 1001,
        "notice": "调整要求超出单条上限（6000 字），仅保留前 5999 字（原文 7000 字，省略 1001 字）",
    }


def test_instruction_truncation_reason_both_uses_original_from_and_smaller_to():
    from app.services.prompt_governance import instruction_truncation

    cap = {"from": 7000, "to": 5999, "omitted": 1001, "limit": 6000}
    info = {"detail": [{"label": "调整要求", "priority": 1, "from": 5999, "to": 120, "cut": True}]}
    got = instruction_truncation(cap, info)
    assert got["from"] == 7000, "from 必须取用户原文长度，而不是上限后的长度"
    assert got["to"] == 120, "to 必须取两道刀中更小者"
    assert got["omitted"] == 7000 - 120
    assert "同时超出单条上限（6000 字）与上下文预算" in got["notice"]


def test_instruction_truncation_none_when_nothing_cut():
    from app.services.prompt_governance import instruction_truncation

    assert instruction_truncation(None, None) is None
    assert instruction_truncation(None, {"detail": []}) is None
    assert (
        instruction_truncation(None, {"detail": [{"label": "调整要求", "priority": 1, "from": 50, "to": 50, "cut": False}]})
        is None
    )


@pytest.mark.asyncio
async def test_adjust_outline_reports_physical_cap_when_budget_off(db_conn, monkeypatch):
    """预算关闭 + 指令超 6000 字：只报物理上限（不能因为预算没开就静默）。"""
    import uuid

    import app.routers.sections as sec
    from app.config import settings
    from app.services.prompt_material_guard import INSTRUCTION_PROMPT_MAX_CHARS

    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.execute(
        "INSERT INTO schemes(id,project_id,name,status) VALUES(?,?,?,?)", (sid, pid, "深基坑专项方案", "目录已确认")
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, title, level, status,"
        " word_count, word_budget, review_status, sort_order)"
        " VALUES (?,?,?,?,?, 'generated', 100, 1500, 'pending', ?)",
        ("R1", sid, "", "工程概况", 1, 0),
    )
    await db_conn.commit()

    async def fake(messages, validate_fn, **kwargs):
        obj = {"outline": [{"id": "R1", "title": "工程概况", "children": []}], "summary": "未改动"}
        return obj, ""

    monkeypatch.setattr(sec, "collect_json_response", fake)
    monkeypatch.setattr(settings, "prompt_context_budget", 0, raising=False)
    instruction = "必须完整执行。" * 900
    assert len(instruction) > INSTRUCTION_PROMPT_MAX_CHARS

    res = await sec.adjust_outline(sid, {"instruction": instruction}, db=db_conn)
    book = res.get("instruction_truncated")
    assert book, res
    assert book["from"] == len(instruction)
    assert book["to"] <= INSTRUCTION_PROMPT_MAX_CHARS
    assert "超出单条上限" in book["notice"]


@pytest.mark.asyncio
async def test_adjust_facts_physical_cap_only_on_preview_not_on_apply(db_conn, monkeypatch):
    """global_facts：预览路径回传物理上限账面；apply=True 不渲染提示词、不得报。"""
    import uuid

    import app.routers.global_facts as gf
    from app.config import settings
    from app.services.prompt_material_guard import INSTRUCTION_PROMPT_MAX_CHARS

    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db_conn.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db_conn.execute(
        "INSERT INTO global_facts (id, project_id, scheme_id, group_id, group_title,"
        " title, content, category, source_ref, is_simulated, confidence, is_resolved,"
        " has_conflict, conflict_keys, fact_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "f1",
            pid,
            sid,
            "g_f1",
            "基坑深度",
            "基坑深度",
            "- **基坑深度**: 5.6m",
            "tech_param",
            "[]",
            0,
            0.9,
            1,
            0,
            "",
            "基坑深度",
        ),
    )
    await db_conn.commit()

    long_instruction = "必须完整执行。" * 900
    assert len(long_instruction) > INSTRUCTION_PROMPT_MAX_CHARS

    async def fake(messages, validate_fn, **kwargs):
        obj = {"operations": [{"op": "update", "fact_id": "f1", "value": "5.8m"}], "summary": "改值"}
        issues = validate_fn(obj)
        if issues:
            raise ValueError("校验不通过: " + ";".join(issues))
        return obj, ""

    monkeypatch.setattr(gf, "collect_json_response", fake)
    monkeypatch.setattr(settings, "prompt_context_budget", 0, raising=False)

    preview = await gf.adjust_facts(
        gf.AdjustRequest(instruction=long_instruction, scheme_id=sid), db=db_conn
    )
    book = preview.get("instruction_truncated")
    assert book, preview
    assert book["from"] == len(long_instruction)
    assert "超出单条上限" in book["notice"]

    applied = await gf.adjust_facts(
        gf.AdjustRequest(
            instruction=long_instruction,
            scheme_id=sid,
            apply=True,
            operations=[{"op": "update", "fact_id": "f1", "value": "5.8m"}],
        ),
        db=db_conn,
    )
    assert "instruction_truncated" not in applied, "apply=True 不渲染提示词，不得虚报截断"

