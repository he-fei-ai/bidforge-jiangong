"""目录占位章节（2026-11-05 需求）回归用例。

覆盖：
  1. 标题命中 / 不命中 / 全角半角与括号书名号变体；
  2. 命中章节从生成目标中剔除（不进入 AI 任务与进度分母）；
  3. 占位落库覆盖旧正文，且标题/层级/sort_order 不变（目录编号不跳号）；
  4. 字数与预检不误报（DLV-01/02/08/16、CMP-09、TRC-01）；
  5. 导出预检不报空章节/低字数/审核缺失；
  6. DOCX 正常输出章节标题 + 「另行补充内容」；
  7. 一致性扫描不把占位章节当作待扫描正文。
"""

import json
import os

import pytest
from app.routers import sse_handlers as sh
from app.routers.export import collect_export_issues, export_docx
from app.services.consistency_scanner import load_leaf_sections
from app.services.placeholder_sections import (
    PLACEHOLDER_CONTENT,
    PLACEHOLDER_STATUS,
    is_placeholder_title,
    persist_placeholder_sections,
    split_placeholder_leaves,
)
from app.services.preflight_engine import PreflightContext, run_preflight
from docx import Document


@pytest.mark.parametrize(
    "title",
    [
        "第六章 计算书及相关图纸（含附图）",
        "第6章 附　图",
        "6.1 施工平面布置图",
        "节点详图与系统图",
        "《附件》一",
        "（１）计算书",  # 全角数字
        "ＣＡＤ图纸",  # 全角字母
        "5.2 主体结构施工图纸",
    ],
)
def test_title_hits_variants(title):
    assert is_placeholder_title(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "第七章 施工准备",
        "安全技术交底",
        "施工进度计划",
        "",
        None,
    ],
)
def test_title_misses_normal_chapters(title):
    assert is_placeholder_title(title) is False


def test_switch_and_keywords_are_configurable():
    # 显式关闭后一律不命中
    assert is_placeholder_title("第六章 计算书", enabled=False) is False
    # 显式关键词（同样走归一化）可替换默认清单
    assert is_placeholder_title("第六章 计算书", keywords=["附图"]) is False
    assert is_placeholder_title("附图一 现场平面", keywords=["附图"]) is True


def test_split_placeholder_leaves_excludes_from_generation():
    leaves = [
        {"id": "a", "title": "第七章 施工准备"},
        {"id": "b", "title": "6.1 施工平面布置图（附图）"},
        {"id": "c", "title": "节点详图与系统图"},
    ]
    generating, placeholders = split_placeholder_leaves(leaves)
    assert [s["id"] for s in generating] == ["a"]
    assert [s["id"] for s in placeholders] == ["b", "c"]


async def _seed_scheme_sections(db):
    await db.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", ("s1", "p1", "深基坑支护专项施工方案")
    )
    await db.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content, word_count, word_budget) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("calc", "s1", "", 5, 1, "第六章 计算书及相关图纸（含附图）", "generated", "旧的计算书正文", 1200, 1200),
    )
    await db.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content, word_count, word_budget) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("next", "s1", "", 6, 1, "第七章 施工准备", "generated", "施工准备内容", 1200, 0),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_persist_overwrites_old_content_and_keeps_numbering(db_conn):
    await _seed_scheme_sections(db_conn)
    leaf = {"id": "calc", "title": "第六章 计算书及相关图纸（含附图）", "content": "旧的计算书正文", "word_count": 1200}
    count = await persist_placeholder_sections(db_conn, [leaf], updated_at="2026-11-05T00:00:00")
    assert count == 1

    cur = await db_conn.execute(
        "SELECT title, sort_order, level, parent_id, content, word_count, status FROM sections WHERE id='calc'"
    )
    row = await cur.fetchone()
    assert row["content"] == PLACEHOLDER_CONTENT
    assert row["status"] == PLACEHOLDER_STATUS
    assert row["word_count"] == 0
    # 标题 / 层级 / sort_order / parent 全部保持不变 → 编号不跳号
    assert row["title"] == "第六章 计算书及相关图纸（含附图）"
    assert row["sort_order"] == 5
    assert row["level"] == 1
    assert row["parent_id"] == ""
    # sort_order=6 的下一章仍在原位
    cur = await db_conn.execute("SELECT sort_order FROM sections WHERE id='next'")
    assert (await cur.fetchone())["sort_order"] == 6
    # 内存快照同步更新，避免同级上下文读到旧正文
    assert leaf["content"] == PLACEHOLDER_CONTENT
    assert leaf["status"] == PLACEHOLDER_STATUS


def test_preflight_does_not_report_placeholder_as_empty_or_low_word():
    long_text = "施工准备内容包括现场围挡、临时道路与材料堆放安排。" * 80
    ctx = PreflightContext(
        scheme_id="s1",
        scheme_name="深基坑支护专项施工方案",
        scheme_type="",
        word_budget=1200,
        sections=[
            {
                "id": "calc",
                "title": "第六章 计算书及相关图纸（含附图）",
                "content": PLACEHOLDER_CONTENT,
                "word_count": 0,
                "word_budget": 1200,
                "parent_id": "",
                "level": 1,
                "status": PLACEHOLDER_STATUS,
            },
            {
                "id": "next",
                "title": "第七章 施工准备",
                "content": long_text,
                "word_count": 1200,
                "word_budget": 0,
                "parent_id": "",
                "level": 1,
                "status": "generated",
            },
        ],
    )
    findings = run_preflight(ctx)
    bad = [f for f in findings if f.get("rule_id") in ("DLV-01", "DLV-02", "DLV-08", "DLV-16", "CMP-09", "TRC-01")]
    assert not bad, f"占位章节被误报：{[f.get('rule_id') + ':' + f.get('detail', '') for f in bad]}"
    assert not any("计算过程" in (f.get("detail") or "") for f in findings)


@pytest.mark.asyncio
async def test_export_issues_ignore_placeholder(db_conn):
    await _seed_scheme_sections(db_conn)
    await persist_placeholder_sections(db_conn, [{"id": "calc", "title": "第六章 计算书及相关图纸（含附图）"}])
    # 普通章节显式标记已审核，确保 review_* 只可能来自占位章节
    await db_conn.execute("UPDATE sections SET review_status='approved' WHERE id='next'")
    await db_conn.commit()
    result = await collect_export_issues("s1", db_conn)
    issues = result.get("issues") or []
    types = {i.get("type") for i in issues}
    assert "empty_section" not in types
    assert "low_word_count" not in types
    assert "review_missing" not in types
    assert "review_pending" not in types


@pytest.mark.asyncio
async def test_docx_contains_placeholder_text(db_conn):
    await _seed_scheme_sections(db_conn)
    await persist_placeholder_sections(db_conn, [{"id": "calc", "title": "第六章 计算书及相关图纸（含附图）"}])
    body = {
        "project_name": "示例项目",
        "bid_section": "第一标段",
        "config": {},
        "chart_images": [],
        "fe_codes": [],
        "override_high_issues": True,
    }
    resp = await export_docx("s1", body, db=db_conn)
    # 覆写路径必须留响应头，证明本轮是「已知悉并继续」而非闸门失效
    assert resp.headers.get("X-Export-Override") == "high-issues"
    path = resp.path
    assert path and os.path.exists(path)

    doc = Document(path)
    joined = "\n".join(p.text for p in doc.paragraphs)
    calc_pos = joined.find("计算书及相关图纸")
    assert calc_pos >= 0, f"未输出占位章节标题，实际内容:\n{joined}"
    assert PLACEHOLDER_CONTENT in joined, f"未输出占位正文，实际内容:\n{joined}"
    next_pos = joined.find("施工准备")
    assert next_pos > calc_pos, "占位章节后的下一章缺失或顺序错乱（编号可能跳号）"


@pytest.mark.asyncio
async def test_consistency_scan_ignores_placeholder(db_conn):
    await _seed_scheme_sections(db_conn)
    await persist_placeholder_sections(db_conn, [{"id": "calc", "title": "第六章 计算书及相关图纸（含附图）"}])
    sections = await load_leaf_sections(db_conn, "s1")
    assert [s["id"] for s in sections] == ["next"]


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _PlaceholderOnlyDB:
    """最小桩 DB：唯一目标章节命中占位规则（已有旧正文，符合 section 模式选章）。"""

    def __init__(self):
        self.updates: list[tuple] = []
        self._sections = [
            {
                "id": "calc",
                "scheme_id": "sc1",
                "title": "第六章 计算书及相关图纸（含附图）",
                "level": 1,
                "parent_id": "",
                "sort_order": 5,
                "word_budget": 1200,
                "content": "旧的计算书正文",
                "status": "generated",
                "description": "",
                "outline_json": json.dumps({"id": "6"}, ensure_ascii=False),
                "generation_standard": "",
                "review_status": "",
                "word_count": 1200,
            }
        ]

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        async def fetchone(self):
            return self._rows[0] if self._rows else None

        async def fetchall(self):
            return list(self._rows)

    async def execute(self, sql, params=()):
        s = str(sql)
        if "FROM schemes" in s:
            return self._Cur(
                [
                    {
                        "id": "sc1",
                        "project_id": "p1",
                        "name": "测试方案",
                        "type": "深基坑",
                        "word_budget": 1200,
                        "config_json": "{}",
                        "auto_consistency_repair": 0,
                        "generation_standard": "",
                    }
                ]
            )
        if "FROM sections" in s:
            return self._Cur([dict(x) for x in self._sections])
        return self._Cur([])

    async def executemany(self, sql, params_seq):
        if "UPDATE sections SET content" in str(sql):
            self.updates.extend(tuple(p) for p in params_seq)

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.fixture()
def stub_generation_env(monkeypatch):
    """打桩任务注册/进度/AI；AI 调用计入 ai_calls，用于断言占位章节零调用。"""
    ai_calls: list = []
    ckpts: list[dict] = []

    async def _register(*a, **k):
        return "task-x", None

    async def _noop(*a, **k):
        return None

    async def _finish(*a, **k):
        return None

    async def _save_ckpt(task_id, payload):
        ckpts.append(payload)

    async def _chat(messages, **k):
        ai_calls.append(messages)
        return "不应被调用"

    async def _wait_resume(*a, **k):
        return None

    monkeypatch.setattr(sh, "register_task", _register)
    monkeypatch.setattr(sh, "_register_task_exclusive", lambda *a, **k: _register())
    monkeypatch.setattr(sh, "update_progress", _noop)
    monkeypatch.setattr(sh, "update_task_stats", _noop)
    monkeypatch.setattr(sh, "finish_task", _finish)
    monkeypatch.setattr(sh, "_save_content_checkpoint", _save_ckpt)
    monkeypatch.setattr(sh, "settle_global_conn", _noop)
    monkeypatch.setattr(sh, "wait_resume", _wait_resume)
    monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)
    monkeypatch.setattr(sh, "has_active_task", lambda *a, **k: False)
    monkeypatch.setattr(sh, "chat_with_fallback", _chat)
    monkeypatch.setattr(sh, "_write_review_record", _noop, raising=False)
    return {"ai_calls": ai_calls, "ckpts": ckpts}


@pytest.mark.asyncio
async def test_generation_skips_ai_and_registers_placeholder(stub_generation_env):
    """命中占位规则的单章生成：不调用 AI、终态正常、落库为占位文本。"""
    db = _PlaceholderOnlyDB()
    body = {
        "mode": "section",
        "section_id": "calc",
        "force_rewrite": True,
        "concurrency": 1,
        "auto_consistency_repair": False,
    }
    resp = await sh.generate_content("sc1", _FakeRequest(body), db=db)
    chunks = []
    async for piece in resp.body_iterator:
        chunks.append(piece.decode("utf-8", "replace") if isinstance(piece, bytes) else str(piece))
    text = "".join(chunks)
    events = [json.loads(ln[6:]) for ln in text.splitlines() if ln.startswith("data: ")]
    terminal = [e for e in reversed(events) if e.get("event") in ("completed", "stopped", "error", "degraded")][0]

    assert terminal["event"] == "completed", f"占位章节不应失败: {events[-3:]}"
    assert stub_generation_env["ai_calls"] == [], "占位章节不得触发任何 AI 调用（含字数分配）"
    assert db.updates, "占位章节必须落库固定文本"
    content, status, _ts, section_id = db.updates[0]
    assert (content, status, section_id) == (PLACEHOLDER_CONTENT, PLACEHOLDER_STATUS, "calc")
    # 未进入生成任务 → 不产生 section_start/section_done，进度分母为 0
    assert not [e for e in events if e.get("event") in ("section_start", "section_done")]
    assert stub_generation_env["ckpts"][-1]["total"] == 0
    assert terminal["message"] == "没有待生成的章节"
