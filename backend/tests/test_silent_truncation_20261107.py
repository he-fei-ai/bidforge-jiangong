"""R180（2026-11-07）：进模型请求/审计的静默硬截断改为「留痕裁剪」。

覆盖改动点：
* ``content_data_contract.render_data_dictionary_block``
* ``review_autofix.fix_by_ai``（AI 定点修复提示词）
* ``review_autofix._chain_section_fixes`` / ``_fix_each_section``（before/after 审计）
* ``consistency_fullbook_repair.run_fullbook_repair``（before/after 审计）
* ``consistency_scanner`` / ``conflict_arbiter`` / ``repair_agent`` 的 ``_clip``

约定：超长时**模型可见处**出现「原文 N 字 / 已省略 M 字」说明；未超长时
输出逐字节不变；切点优先落在段落/句子边界。另有 AST 防复发锁：把任一
处改回裸切片（``content[:4000]`` / ``content[:SECTION_CONTENT_CAP]`` /
``block[:max_chars]`` / 本地 ``def _clip``）会立刻变红。
"""

import ast
from pathlib import Path

import pytest
from app.services import conflict_arbiter, consistency_scanner, repair_agent
from app.services import consistency_fullbook_repair as cfr
from app.services.content_data_contract import render_data_dictionary_block
from app.services.prompt_material_guard import clip_text_notice
from app.services.review_autofix import (
    FIX_MODE_AI,
    FIX_MODE_MANUAL,
    Capability,
    _chain_section_fixes,
    _fix_each_section,
    fix_by_ai,
)

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"

# 足够长且带清晰句末边界的正文（用于「不切断」断言）
LONG_TEXT = "".join(f"第{i}项说明：现场应按规范组织施工并做好记录。" for i in range(1, 200))


def _split_notice(out: str) -> tuple[str, str]:
    idx = out.find("\n\n[……")
    assert idx >= 0, f"缺少模型可见截断说明：{out[:80]!r}"
    return out[:idx], out[idx:]


def _assert_notice(out: str, total: int) -> str:
    body, notice = _split_notice(out)
    assert "已截断" in notice and f"原文 {total} 字" in notice and "已省略" in notice
    return body


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return list(self._rows)

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _DB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql, params=()):
        return _Cur(self._rows)


# ---------------------------------------------------------------------------
# 1. 共用留痕裁剪
# ---------------------------------------------------------------------------


def test_clip_text_notice_short_is_byte_identical():
    text = "第一段。\n\n第二段。"
    assert clip_text_notice(text, 1000, "测试") == text


def test_clip_text_notice_long_has_counts_and_sentence_boundary(caplog):
    out = clip_text_notice(LONG_TEXT, 200, "测试正文")
    body = _assert_notice(out, len(LONG_TEXT))
    assert body.endswith("。"), "切点应落在句末，不得切在句子中间"
    assert len(body) <= 200
    assert "上下文截断（测试正文）" in caplog.text


# ---------------------------------------------------------------------------
# 2. 全局数据字典（进模型请求）
# ---------------------------------------------------------------------------


def _dictionary(n: int) -> dict:
    return {
        f"k{i}": {
            "chapter": "",
            "name": f"关键指标{i}",
            "value": f"取值{i}",
            "source_title": "全局事实",
            "confidence": 0.9,
        }
        for i in range(n)
    }


def test_data_dictionary_short_is_byte_identical():
    data = {
        "k1": {
            "chapter": "",
            "name": "项目名称",
            "value": "某工程",
            "source_title": "全局事实",
            "confidence": 0.9,
        }
    }
    out = render_data_dictionary_block(data, max_chars=10000)
    assert "已截断" not in out
    assert out == render_data_dictionary_block(data, max_chars=len(out))


def test_data_dictionary_long_has_item_boundary_notice():
    data = _dictionary(40)
    full = render_data_dictionary_block(data, chapter_key="1", max_chars=10**9)
    out = render_data_dictionary_block(data, chapter_key="1", max_chars=300)
    body = _assert_notice(out, len(full))
    kept = body.rstrip("\n").splitlines()[-1]
    assert kept.startswith("- 关键指标"), "应按条目边界收刀，不得切在条目中间"
    assert kept.endswith("）")


# ---------------------------------------------------------------------------
# 3. AI 定点修复提示词
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_by_ai_prompt_short_is_byte_identical(monkeypatch):
    captured = {}

    def fake_render(name, **kwargs):
        if name == "review_autofix_user":
            captured.update(kwargs)
        return "x"

    async def fake_chat(*a, **k):
        return ""

    monkeypatch.setattr("app.services.review_autofix.render", fake_render)
    monkeypatch.setattr("app.services.review_autofix.chat_with_fallback", fake_chat)

    content = "短正文。"
    await fix_by_ai(
        cap=Capability(mode=FIX_MODE_AI, instruction="改"),
        finding={},
        scheme={},
        section={"id": "s1", "title": "T", "content": content},
        targets=[],
        facts="",
        standards_text="",
    )
    assert captured["section_content"] == content


@pytest.mark.asyncio
async def test_fix_by_ai_prompt_long_has_notice(monkeypatch):
    captured = {}

    def fake_render(name, **kwargs):
        if name == "review_autofix_user":
            captured.update(kwargs)
        return "x"

    async def fake_chat(*a, **k):
        return ""

    monkeypatch.setattr("app.services.review_autofix.render", fake_render)
    monkeypatch.setattr("app.services.review_autofix.chat_with_fallback", fake_chat)

    await fix_by_ai(
        cap=Capability(mode=FIX_MODE_AI, instruction="改"),
        finding={},
        scheme={},
        section={"id": "s1", "title": "T", "content": LONG_TEXT * 2},
        targets=[],
        facts="",
        standards_text="",
    )
    body = _assert_notice(captured["section_content"], len(LONG_TEXT * 2))
    assert body.endswith("。")


# ---------------------------------------------------------------------------
# 4. review_autofix before/after 审计（容量足够 → 全文）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_manual_keeps_full_before_after(monkeypatch):
    monkeypatch.setattr(
        "app.services.review_autofix.capability_of",
        lambda _rid: Capability(mode=FIX_MODE_MANUAL, reason="人工"),
    )
    content, items = await _chain_section_fixes(
        {"id": "s1", "title": "T", "content": LONG_TEXT},
        [{"rule_id": "R1"}],
        {},
        "",
        "",
    )
    assert items[0]["status"] == "unsupported"
    assert items[0]["before"] == LONG_TEXT and items[0]["after"] == LONG_TEXT
    assert content == LONG_TEXT


@pytest.mark.asyncio
async def test_chain_not_located_keeps_full_before_after(monkeypatch):
    monkeypatch.setattr(
        "app.services.review_autofix.capability_of",
        lambda _rid: Capability(mode=FIX_MODE_AI, instruction="改"),
    )
    monkeypatch.setattr("app.services.review_autofix.locate_targets", lambda *a, **k: [])
    _content, items = await _chain_section_fixes(
        {"id": "s1", "title": "T", "content": LONG_TEXT},
        [{"rule_id": "R1"}],
        {},
        "",
        "",
    )
    assert items[0]["status"] == "not_located"
    assert items[0]["before"] == LONG_TEXT and items[0]["after"] == LONG_TEXT


@pytest.mark.asyncio
async def test_chain_repaired_after_kept_full_for_confirm(monkeypatch):
    long_after = LONG_TEXT + "补充整改说明。"

    async def fake_fix_each(*a, **k):
        return [{"status": "repaired", "after": long_after, "problems": []}], [], []

    monkeypatch.setattr(
        "app.services.review_autofix.capability_of",
        lambda _rid: Capability(mode=FIX_MODE_AI, instruction="改"),
    )
    monkeypatch.setattr(
        "app.services.review_autofix.locate_targets",
        lambda *a, **k: [{"section_title": "T", "line": 1, "context": "x", "why": "y"}],
    )
    monkeypatch.setattr("app.services.review_autofix._fix_each_section", fake_fix_each)
    content, items = await _chain_section_fixes(
        {"id": "s1", "title": "T", "content": LONG_TEXT},
        [{"rule_id": "R1"}],
        {},
        "",
        "",
    )
    assert items[0]["after"] == long_after, "after 会被 confirm 写回正文，必须全文"
    assert items[0]["before"] == LONG_TEXT
    assert content == long_after


@pytest.mark.asyncio
async def test_fix_each_items_keep_full_before_after(monkeypatch):
    async def fake_fix_one(*a, **k):
        return LONG_TEXT + "已整改。", []

    monkeypatch.setattr("app.services.review_autofix.fix_by_ai", fake_fix_one)
    monkeypatch.setattr("app.services.review_autofix.validate_fixed", lambda *a, **k: (True, []))
    items, pending, _skipped = await _fix_each_section(
        {"rule_id": "R1"},
        [{"id": "s1", "title": "T", "content": LONG_TEXT, "word_budget": 3000}],
        Capability(mode=FIX_MODE_AI, instruction="改"),
        {},
        "",
        "",
        [{"section_id": "s1", "section_title": "T", "line": 1, "context": "x", "why": "y"}],
    )
    assert items[0]["before"] == LONG_TEXT
    assert items[0]["after"] == LONG_TEXT + "已整改。"
    assert pending and pending[0][2] == LONG_TEXT + "已整改。"


# ---------------------------------------------------------------------------
# 5. 整卷修复 before/after 审计
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fullbook_items_keep_full_before_after(monkeypatch):
    captured = {}

    async def fake_snapshot(db, scheme_id, sections):
        return "snap-1"

    async def fake_save(db, **kwargs):
        captured.update(kwargs)
        repaired = sum(1 for i in kwargs["items"] if i.get("status") == "repaired")
        return {
            "items": kwargs["items"],
            "repair_id": "r1",
            "snapshot_id": "snap-1",
            "repaired": repaired,
            "failed": len(kwargs["items"]) - repaired,
            "skipped": 0,
            "total_conflicts": kwargs["total_conflicts"],
        }

    async def fake_leaves(db, scheme_id):
        return [{"id": "s1", "title": "T", "content": LONG_TEXT, "word_budget": 3000}]

    async def fake_chat(*a, **k):
        return "raw"

    long_after = LONG_TEXT + "已整改。"

    target = {
        "id": "c1",
        "topic": "工期",
        "conflict_type": "value",
        "severity": "high",
        "authoritative_value": "85 天",
        "occurrences": [
            {"section_id": "s1", "section_title": "T", "value": "90 天", "text": "工期 90 天。"}
        ],
    }
    monkeypatch.setattr(repair_agent, "filter_conflicts", lambda *a, **k: [target])
    monkeypatch.setattr(consistency_scanner, "load_leaf_sections", fake_leaves)
    monkeypatch.setattr(cfr, "build_fullbook_workspace", lambda **k: "workspace")
    monkeypatch.setattr(cfr, "render", lambda *a, **k: "p")
    monkeypatch.setattr(cfr, "chat_with_fallback", fake_chat)
    monkeypatch.setattr(cfr, "parse_fullbook_workspace", lambda raw, sids: ({"s1": long_after}, []))
    monkeypatch.setattr(cfr, "validate_repair", lambda **k: (True, []))
    monkeypatch.setattr(cfr.repair_record, "create_snapshot", fake_snapshot)
    monkeypatch.setattr(cfr.repair_record, "save_repair", fake_save)

    await cfr.run_fullbook_repair(_DB([]), scheme_id="sc1", scan_id="sn1", conflicts=[target], facts="")
    assert captured["items"][0]["status"] == "repaired"
    assert captured["items"][0]["before"] == LONG_TEXT
    assert captured["items"][0]["after"] == long_after


# ---------------------------------------------------------------------------
# 6. 三处同名 _clip 的调用点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scanner_project_docs_short_byte_identical():
    rows = [("a.md", "bid", "短资料。")]
    out = await consistency_scanner.build_project_docs_text(_DB(rows), "p1")
    assert out == "### a.md\n短资料。\n"


@pytest.mark.asyncio
async def test_scanner_project_docs_long_has_notice():
    rows = [("a.md", "bid", LONG_TEXT)]
    out = await consistency_scanner.build_project_docs_text(_DB(rows), "p1")
    _assert_notice(out, len(LONG_TEXT))


def test_scanner_standards_long_has_notice(monkeypatch):
    monkeypatch.setattr(consistency_scanner, "get_standards_text", lambda *a, **k: LONG_TEXT)
    out = consistency_scanner.build_standards_text("方案", "深基坑")
    body = _assert_notice(out, len(LONG_TEXT))
    assert body.endswith("。")


def test_scanner_standards_short_byte_identical(monkeypatch):
    monkeypatch.setattr(consistency_scanner, "get_standards_text", lambda *a, **k: "规范摘录。")
    assert consistency_scanner.build_standards_text("方案", "深基坑") == "规范摘录。"


@pytest.mark.asyncio
async def test_arbiter_prompt_long_text_has_notice(monkeypatch):
    captured = {}

    def fake_render(name, **kwargs):
        if name == "consistency_arbitrate_user":
            captured.update(kwargs)
        return "p"

    async def fake_collect(*a, **k):
        return {"arbitrations": []}, {}

    monkeypatch.setattr(conflict_arbiter, "render", fake_render)
    monkeypatch.setattr(conflict_arbiter, "collect_json_response", fake_collect)

    await conflict_arbiter._arbitrate_batch(
        [{"id": "c1", "conflict_type": "value", "topic": "工期", "occurrences": [{"text": LONG_TEXT}]}],
        facts="",
        design_docs="",
        standards="",
        project_requirements="",
    )
    assert "已截断" in captured["conflicts"] and f"原文 {len(LONG_TEXT)} 字" in captured["conflicts"]


@pytest.mark.asyncio
async def test_arbiter_prompt_short_byte_identical(monkeypatch):
    captured = {}

    def fake_render(name, **kwargs):
        if name == "consistency_arbitrate_user":
            captured.update(kwargs)
        return "p"

    async def fake_collect(*a, **k):
        return {"arbitrations": []}, {}

    monkeypatch.setattr(conflict_arbiter, "render", fake_render)
    monkeypatch.setattr(conflict_arbiter, "collect_json_response", fake_collect)

    await conflict_arbiter._arbitrate_batch(
        [{"id": "c1", "conflict_type": "value", "topic": "工期", "occurrences": [{"text": "工期 90 天。"}]}],
        facts="",
        design_docs="",
        standards="",
        project_requirements="",
    )
    assert '"text": "工期 90 天。"' in captured["conflicts"]


def test_repair_agent_quote_long_has_notice():
    groups = repair_agent.group_by_section(
        [
            {
                "id": "c1",
                "topic": "工期",
                "conflict_type": "value",
                "authoritative_value": "85 天",
                "occurrences": [{"section_id": "s1", "value": "90 天", "text": LONG_TEXT}],
            }
        ]
    )
    quote = groups["s1"]["conflicts"][0]["quote"]
    body = _assert_notice(quote, len(LONG_TEXT))
    assert body.endswith("。")


def test_repair_agent_quote_short_byte_identical():
    groups = repair_agent.group_by_section(
        [
            {
                "id": "c1",
                "topic": "工期",
                "conflict_type": "value",
                "authoritative_value": "85 天",
                "occurrences": [{"section_id": "s1", "value": "90 天", "text": "工期 90 天。"}],
            }
        ]
    )
    assert groups["s1"]["conflicts"][0]["quote"] == "工期 90 天。"


# ---------------------------------------------------------------------------
# 7. 防复发锁：裸切片 / 本地 _clip 不得回流
# ---------------------------------------------------------------------------


def test_no_silent_raw_slices_in_scope():
    files = [
        "content_data_contract.py",
        "review_autofix.py",
        "consistency_fullbook_repair.py",
        "consistency_scanner.py",
        "conflict_arbiter.py",
        "repair_agent.py",
    ]
    bad: list[str] = []
    for name in files:
        tree = ast.parse((SERVICES / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_clip":
                bad.append(f"{name}: 本地 def _clip 回流")
            if not isinstance(node, ast.Subscript):
                continue
            if not isinstance(node.slice, ast.Slice):
                continue
            upper = node.slice.upper
            if isinstance(upper, ast.Constant) and upper.value == 4000:
                bad.append(f"{name}:[:4000] 裸切片")
            if isinstance(upper, ast.Attribute) and upper.attr == "SECTION_CONTENT_CAP":
                bad.append(f"{name}:**[:SECTION_CONTENT_CAP] 裸切片**")
            if (
                isinstance(upper, ast.Name)
                and upper.id == "max_chars"
                and isinstance(node.value, ast.Name)
                and node.value.id == "block"
            ):
                bad.append(f"{name}:block[:max_chars] 裸切片")
    assert not bad, "发现静默硬截断回流：" + "；".join(bad)
