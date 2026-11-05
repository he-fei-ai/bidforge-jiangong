"""R177 · 事实注入预算三项验收（L1/BUG-03、L7/BUG-12、L3/BUG-07）。

L1：数据字典不得挤占目录事实预算 —— 预算在渲染前统一分配，事实先占保底，
    字典只吃剩余额度；被裁的行仍进入审计账本（injected + dropped == 输入行数）。
L7：正文 core 与 chapter 共享单一总预算，选中事实从 core 集合剔除后只注入一次。
L3：SSE completed.groups.items 与前端 FactItem 契约逐字段对齐。
"""

import uuid

import app.db as _appdb
import pytest
from app.db import get_conn, init_db
from app.services import facts_builder as fb
from app.services.content_plan import resolve_selected_fact_rows
from app.services.facts_classification import CHAPTER_TITLES
from app.services.facts_extractor import (
    ExtractionResult,
    FactGroup,
    FactItem,
    format_for_frontend,
    persist_extraction,
)

# ---------------------------------------------------------------------------
# L1 / BUG-03：事实保底 + 字典剩余额度
# ---------------------------------------------------------------------------


def _rows(n_core: int = 0, n_rest: int = 10) -> list:
    rows: list = []
    for i in range(n_core):
        rows.append(("工程概况", f"核心事实{i}", "取值" * 12, 0.9, "overview", "text"))
    for i in range(n_rest):
        rows.append(("主体结构", f"主体事实{i}", "取值" * 12, 0.9, "technique", "text"))
    return rows


_DD_TEXT = "【全局数据字典】\n" + ("字典条目：取值\n" * 200)[:1600]


def _fake_render_dd(_dd, max_chars: int = 1600) -> str:
    return _DD_TEXT[:max_chars]


async def test_build_facts_text_dd_budget_keeps_facts_floor(monkeypatch):
    """数据字典 1600 字、max_total=1000：事实行不得低于保底，字典被裁到剩余额度。"""
    rows = _rows(n_rest=10)
    monkeypatch.setattr(fb, "_load_facts_rows", _fake_load_rows(rows))
    monkeypatch.setattr(fb, "build_global_data_dictionary", lambda _rows: {})
    monkeypatch.setattr(fb, "render_data_dictionary_block", _fake_render_dd)
    monkeypatch.setattr(fb.settings, "content_data_dictionary", True)

    dropped: list[str] = []
    injected: list[str] = []
    text = await fb.build_facts_text(None, "scheme", max_total=1000, audit_out=dropped, injected_out=injected)

    assert len(text) <= 1000
    assert len(injected) >= len(rows)  # 事实行不低于保底
    assert len(injected) + len(dropped) == len(rows)
    # 字典只能占剩余额度：其长度不超过「预算 - 事实保底 - 分隔换行」
    _dd_part = text.split("\n### ", 1)[0]
    assert len(_dd_part) <= 1000 - fb._facts_floor_chars(rows) - 1
    assert "### 主体结构" in text


async def test_render_outline_chapter_facts_same_budget_model():
    """子级 1000 预算下，1600 字字典同样不能把事实挤空。"""
    rows = _rows(n_rest=10)
    text = fb.render_outline_chapter_facts(rows, {}, max_total=1000, dd_block=_DD_TEXT)

    assert len(text) <= 1000
    assert "### 主体结构" in text
    # 每行事实至少保留 1 字符 → 事实区至少 10 个换行（含最后一行预留）
    assert text.split("\n### ", 1)[1].count("\n") >= len(rows)


# ---------------------------------------------------------------------------
# L7 / BUG-12：正文总预算 + 选中事实不重复
# ---------------------------------------------------------------------------


def test_content_facts_share_total_budget_and_selected_excludes_core():
    rows = _rows(n_core=4, n_rest=8)
    core = fb.core_facts_rows(rows)
    rest = fb.exclude_rows(rows, core)

    core_audit = fb.fact_injection_audit(core, max_total=6000)
    chapter_budget = max(0, 6000 - len(core_audit["rendered_text"]))
    chapter_audit = fb.fact_injection_audit(rest, max_total=chapter_budget)

    # 总预算不超过上限
    assert (len(core_audit["rendered_text"]) + len(chapter_audit["rendered_text"])) <= 6000

    # 选中事实：核心标题先剔除，再并回本章剩余行
    selected = resolve_selected_fact_rows([core[0][1], rest[0][1]], rows)
    selected = fb.exclude_rows(selected, core)
    assert core[0][1] not in [r[1] for r in selected]
    union = rest + fb.exclude_rows(selected, rest)
    selected_audit = fb.fact_injection_audit(union, full_titles=[core[0][1], rest[0][1]], max_total=chapter_budget)

    # 同一次请求里，每条事实最多被注入一次
    titles = list(core_audit["injected_titles"])
    titles += list(selected_audit["injected_titles"])
    assert len(titles) == len(set(titles))
    assert core[0][1] not in selected_audit["injected_titles"]


def test_sse_uses_shared_budget_and_core_exclusion():
    """源码锁：实际调用必须走统一预算与 core 剔除，而不是测试自证。"""
    import inspect

    from app.routers import sse_handlers as sh

    src = inspect.getsource(sh)
    assert "CONTENT_FACTS_TOTAL_BUDGET" in src
    assert "_chapter_budget = max(" in src
    assert "exclude_rows(_sel_rows, _core_facts_rows)" in src


# ---------------------------------------------------------------------------
# L3 / BUG-07：SSE payload 字段补齐
# ---------------------------------------------------------------------------


@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "facts-r177-contract.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, pid, sid
    await db.close()


async def test_sse_completed_items_match_fact_item_contract(ctx):
    db, pid, sid = ctx
    result = ExtractionResult()
    result.groups = [
        FactGroup(
            title="工程概况",
            category="overview",
            items=[
                FactItem(
                    name="基坑开挖深度",
                    value="12.5",
                    key="pit_depth",
                    category="overview",
                    source="施工方案",
                    source_ref="施工方案.docx",
                )
            ],
        )
    ]
    await persist_extraction(result, db, pid, sid)

    row = (await db.execute_fetchall("SELECT id, is_resolved FROM global_facts WHERE scheme_id=?", (sid,)))[0]

    payload = format_for_frontend(result)
    item = payload["groups"][0]["items"][0]

    assert item["fact_id"] == row[0]
    assert item["is_resolved"] == bool(row[1])
    assert item["chapter_title"] == CHAPTER_TITLES.get(item["chapter"], "")
    assert isinstance(item["shared_chapters"], list)
    assert isinstance(item["value_kind"], str)
    # 前端 FactItem 的必填字段必须全部存在
    assert {
        "fact_id",
        "name",
        "value",
        "category",
        "is_simulated",
        "is_resolved",
        "has_conflict",
        "is_stale",
    } <= set(item)


def _fake_load_rows(rows):
    async def _load(_db, _scheme_id):
        return list(rows)

    return _load
