"""G4/G5 接线回归：本章专项提取成果注入 + 两条装配路径的图表配额提示。

G4：`scheme_auto_chapter_inject` 此前全仓 0 消费点，`_build_chapter_extraction_map`
只有定义/导入没有调用。本文件从 `generate_content` 的真实 SSE 链路出发，
断言开关为真时提示词里确实出现该章提取内容，开关为假时既不查询也不出现。

G5：`_chart_hint` 曾在 `_stable_prefix` 条件下才计算，非分层路径拿不到配额提示。
本文件对 `content_stable_prefix_enabled` 两种取值分别运行，断言两条路径都能
看到 `build_chart_quota_hint` 产出的同一段提示。
"""

import ast
import json
from pathlib import Path

import pytest
from app.routers import sse_handlers as sh

_MARKER = "G4-EXTRACT-MARKER-工程概况-20261107"
_BID_ITEM = {
    "item_id": "projectBasicInfo",
    "label": "项目基本信息",
    "output_type": "markdown",
    "content": f"- 工程名称：{_MARKER}",
}
_LONG_AI_TEXT = "## 编制说明\n\n" + ("本章正文内容用于统计超字数。" * 60)


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _G4G5DB:
    """最小桩 DB：一个顶层章 + 两个叶子；可开关提取项与图表计数。"""

    def __init__(self, *, bid_item=None):
        self.bid_queries = 0
        self.saved: list[str] = []
        self._bid_item = bid_item
        self._sections = [
            self._sec("r1", "1", 1, "", 0, 0, "工程概况"),
            self._sec("s1", "1.1", 2, "r1", 0, 200, "工程概况-现状"),
            self._sec("s2", "1.2", 2, "r1", 1, 200, "工程概况-特点"),
        ]

    @staticmethod
    def _sec(sid, num, level, parent, sort, budget, title):
        return {
            "id": sid,
            "scheme_id": "sc1",
            "title": title,
            "level": level,
            "parent_id": parent,
            "sort_order": sort,
            "word_budget": budget,
            "content": "",
            "status": "empty",
            "description": "d",
            "outline_json": json.dumps({"id": num}, ensure_ascii=False),
            "generation_standard": "",
            "review_status": "",
            "word_count": 0,
        }

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
                        "word_budget": 3000,
                        "config_json": "{}",
                        "auto_consistency_repair": 0,
                        "generation_standard": "",
                    }
                ]
            )
        if "FROM sections" in s and "ORDER BY sort_order" in s:
            return self._Cur([dict(x) for x in self._sections])
        if "FROM bid_analysis_items" in s:
            self.bid_queries += 1
            return self._Cur([dict(self._bid_item)] if self._bid_item else [])
        if "FROM chart_predictions" in s:
            return self._Cur([{"chart_type": "gantt", "n": 999}])
        if s.strip().upper().startswith("UPDATE SECTIONS SET CONTENT"):
            self.saved.append(params[0])
            return self._Cur([])
        if "SUM(word_count)" in s:
            return self._Cur([(sum(len(x) for x in self.saved),)])
        return self._Cur([])

    async def executemany(self, *a, **k):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


async def _noop(*a, **k):
    return None


@pytest.fixture()
def captured_prompts(monkeypatch):
    """打桩任务/进度/AI，返回每次 AI 调用的 messages 快照。"""
    captured: list[list[dict]] = []

    async def _register(*a, **k):
        return "task-x", None

    async def _brief(*a, **k):
        return "项目概述（测试桩，不含提取内容）"

    async def _chat(messages, **k):
        captured.append([dict(m) for m in messages])
        return _LONG_AI_TEXT

    monkeypatch.setattr(sh, "register_task", _register)
    monkeypatch.setattr(sh, "_register_task_exclusive", lambda *a, **k: _register())
    monkeypatch.setattr(sh, "update_progress", _noop)
    monkeypatch.setattr(sh, "update_task_stats", _noop)
    monkeypatch.setattr(sh, "finish_task", _noop)
    monkeypatch.setattr(sh, "_save_content_checkpoint", _noop)
    monkeypatch.setattr(sh, "settle_global_conn", _noop)
    monkeypatch.setattr(sh, "wait_resume", lambda *a, **k: _noop())
    monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)
    monkeypatch.setattr(sh, "has_active_task", lambda *a, **k: False)
    monkeypatch.setattr(sh, "chat_with_fallback", _chat)
    monkeypatch.setattr(sh, "_write_review_record", _noop, raising=False)
    monkeypatch.setattr(sh, "_build_structured_brief", _brief)
    return captured


async def _run(db, body=None):
    payload = {"mode": "all", "concurrency": 1, "auto_consistency_repair": False}
    payload.update(body or {})
    resp = await sh.generate_content("sc1", _FakeRequest(payload), db=db)
    chunks: list[str] = []
    async for piece in resp.body_iterator:
        chunks.append(piece.decode("utf-8", "replace") if isinstance(piece, bytes) else str(piece))
    frames = [f for f in "".join(chunks).split("\n\n") if f.strip()]
    events = []
    for frame in frames:
        line = frame.strip()
        if line.startswith(":"):
            continue
        if line.startswith("data: "):
            line = line[6:]
        events.append(json.loads(line))
    return events


def _user_texts(captured: list[list[dict]]) -> list[str]:
    return [
        "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        for messages in captured
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [True, False])
async def test_g4_switch_on_injects_chapter_extraction(captured_prompts, monkeypatch, stable):
    mono = monkeypatch
    mono.setattr(sh.settings, "scheme_auto_chapter_inject", True)
    mono.setattr(sh.settings, "content_stable_prefix_enabled", stable)
    mono.setattr(sh.settings, "content_chart_quota_hint_enabled", False)
    db = _G4G5DB(bid_item=_BID_ITEM)
    await _run(db)

    texts = _user_texts(captured_prompts)
    hit = [t for t in texts if _MARKER in t and "【本章专项提取成果】" in t]
    assert len(hit) == 2, f"两个叶子都应看到本章提取内容，实际命中 {len(hit)}/{len(texts)}"
    assert db.bid_queries == 1, "提取映射应整批只查一次，不得逐章查询"


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [True, False])
async def test_g4_switch_off_keeps_old_prompt(captured_prompts, monkeypatch, stable):
    mono = monkeypatch
    mono.setattr(sh.settings, "scheme_auto_chapter_inject", False)
    mono.setattr(sh.settings, "content_stable_prefix_enabled", stable)
    mono.setattr(sh.settings, "content_chart_quota_hint_enabled", False)
    db_with_item = _G4G5DB(bid_item=_BID_ITEM)
    await _run(db_with_item)
    texts_with_item = _user_texts(captured_prompts)

    # 字节级对照：同一个方案，有提取项 vs 无提取项，开关关闭时提示词必须完全一致。
    captured_prompts.clear()
    db_without_item = _G4G5DB()
    await _run(db_without_item)
    texts_without_item = _user_texts(captured_prompts)

    joined = "\n".join(texts_with_item)
    assert _MARKER not in joined
    assert "【本章专项提取成果】" not in joined
    assert db_with_item.bid_queries == 0, "开关关闭时不得新增 bid_analysis_items 查询"
    assert texts_with_item == texts_without_item, "开关关闭时必须与旧提示词逐字一致"


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [True, False])
async def test_g5_chart_quota_hint_reaches_both_assembly_paths(captured_prompts, monkeypatch, stable):
    mono = monkeypatch
    mono.setattr(sh.settings, "scheme_auto_chapter_inject", False)
    mono.setattr(sh.settings, "content_stable_prefix_enabled", stable)
    mono.setattr(sh.settings, "content_chart_quota_hint_enabled", True)
    db = _G4G5DB()
    await _run(db)

    texts = _user_texts(captured_prompts)
    hit = [t for t in texts if "【本章图表配额" in t and "已达全方案上限" in t]
    assert len(hit) == 2, f"两条装配路径都应收到图表配额提示，实际命中 {len(hit)}/{len(texts)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("stable", [True, False])
async def test_g5_switch_off_removes_hint_from_both_paths(captured_prompts, monkeypatch, stable):
    """回滚口径真实存在：content_chart_quota_hint_enabled=False 两条路径都无提示。"""
    mono = monkeypatch
    mono.setattr(sh.settings, "scheme_auto_chapter_inject", False)
    mono.setattr(sh.settings, "content_stable_prefix_enabled", stable)
    mono.setattr(sh.settings, "content_chart_quota_hint_enabled", False)
    db = _G4G5DB()
    await _run(db)

    joined = "\n".join(_user_texts(captured_prompts))
    assert "【本章图表配额" not in joined


def test_g4_switch_has_production_reader():
    """防复发锁：`scheme_auto_chapter_inject` 至少被一个生产模块读取。

    同时接受 `settings.scheme_auto_chapter_inject` 属性读取与
    `getattr(settings, "scheme_auto_chapter_inject", ...)` 两种形态，避免
    开关再次沦为零消费死配置。
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    hits: list[str] = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "scheme_auto_chapter_inject":
                hits.append(str(path.relative_to(app_dir)))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "scheme_auto_chapter_inject"
            ):
                hits.append(str(path.relative_to(app_dir)))
    assert hits, "scheme_auto_chapter_inject 没有任何生产模块读取（再次变成死开关）"
    assert any("sse_handlers.py" in h for h in hits), f"正文生成入口未读取开关：{hits}"
