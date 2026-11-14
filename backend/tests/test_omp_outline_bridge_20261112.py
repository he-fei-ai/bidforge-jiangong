"""OmpTaskAgent → 平台目录树 转换桥（阶段 2 集成 seam）单元测试。

覆盖：折树转换（多层 / 跳级 / 层级回退 / 空输入）、generate_outline_tree 折叠（mock
OmpTaskAgent 不拉起真实子进程）、以及 sections 旁路端点 generate_outline_via_omp 的
禁用闸门（settings.omp_task_enabled 默认 False → 409 提示回退主链路）。
"""

import asyncio

import pytest
from app.services.ai.omp_outline_bridge import (
    fold_generated_sections_to_tree,
    generate_outline_tree,
)
from app.services.ai.omp_task_agent import GeneratedSection, OmpTaskAgent, OutlineResult


# ===================== 折树转换（纯逻辑） =====================
def test_fold_two_levels():
    secs = [
        GeneratedSection(level=1, title="A"),
        GeneratedSection(level=2, title="A1"),
        GeneratedSection(level=2, title="A2"),
        GeneratedSection(level=1, title="B"),
    ]
    tree = fold_generated_sections_to_tree(secs)
    assert [n["title"] for n in tree] == ["A", "B"]
    a = tree[0]
    assert [c["title"] for c in a["children"]] == ["A1", "A2"]
    assert a["children"][0]["children"] == []


def test_fold_three_levels():
    secs = [
        GeneratedSection(level=1, title="A"),
        GeneratedSection(level=2, title="A1"),
        GeneratedSection(level=3, title="A1a"),
        GeneratedSection(level=2, title="A2"),
    ]
    tree = fold_generated_sections_to_tree(secs)
    a = tree[0]
    assert [c["title"] for c in a["children"]] == ["A1", "A2"]
    assert [gc["title"] for gc in a["children"][0]["children"]] == ["A1a"]


def test_fold_level_gap():
    # level 1 之后直接 level 3：3 挂在最近的更小前驱（level 1）下
    secs = [
        GeneratedSection(level=1, title="A"),
        GeneratedSection(level=3, title="A-x"),
    ]
    tree = fold_generated_sections_to_tree(secs)
    assert tree[0]["title"] == "A"
    assert tree[0]["children"][0]["title"] == "A-x"
    assert tree[0]["children"][0]["level"] == 3


def test_fold_level_reset():
    # 层级回退：3 之后来 1，应成为新的根节点
    secs = [
        GeneratedSection(level=1, title="A"),
        GeneratedSection(level=2, title="A1"),
        GeneratedSection(level=3, title="A1a"),
        GeneratedSection(level=1, title="B"),
    ]
    tree = fold_generated_sections_to_tree(secs)
    assert [n["title"] for n in tree] == ["A", "B"]
    assert tree[0]["children"][0]["children"][0]["title"] == "A1a"


def test_fold_empty():
    assert fold_generated_sections_to_tree([]) == []


def test_fold_preserves_description():
    secs = [GeneratedSection(level=1, title="A", description="说明")]
    tree = fold_generated_sections_to_tree(secs)
    assert tree[0]["description"] == "说明"


# ===================== generate_outline_tree（mock Agent） =====================
async def test_generate_outline_tree_folds(monkeypatch):
    fake = OutlineResult(
        sections=[
            GeneratedSection(level=1, title="章一"),
            GeneratedSection(level=2, title="节1.1"),
            GeneratedSection(level=2, title="节1.2"),
        ]
    )

    async def _fake_generate(self, **kwargs):  # noqa: ANN001
        return fake

    monkeypatch.setattr(OmpTaskAgent, "generate_outline", _fake_generate)
    tree = await generate_outline_tree(brief="某桥梁施工方案")
    assert [n["title"] for n in tree] == ["章一"]
    assert [c["title"] for c in tree[0]["children"]] == ["节1.1", "节1.2"]


# ===================== 旁路端点禁用闸门 =====================
async def test_endpoint_disabled_returns_409():
    # settings.omp_task_enabled 默认 False；禁用时端点应 409 提示回退主链路，
    # 且不触碰 db / 不拉起 omp 子进程。
    from app.routers import sections as sec_mod

    with pytest.raises(Exception) as exc_info:
        await sec_mod.generate_outline_via_omp("scheme-x", {"brief": "x"}, None)
    # FastAPI HTTPException；断言状态码与提示文案
    assert exc_info.value.status_code == 409
    assert "未启用" in str(exc_info.value.detail)


# ===================== feature-flags 端点 =====================
async def test_feature_flags_endpoint_default_false():
    # 轻量功能开关下发：默认 omp_task_enabled=False，前端据此预隐藏入口。
    from app.routers.system import feature_flags_get

    res = await feature_flags_get()
    assert res == {"omp_task_enabled": False}


# ============ 旁路端点：brief 派生 / Row 契约 / 同型防重入（行为实证） ============
class _RowLike:
    """模拟 aiosqlite.Row：只支持 row[col] / row[i]，**没有 .get()**。"""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row

    async def execute(self, sql, params=()):
        return _FakeCursor(self._row)


async def test_omp_endpoint_derive_brief_and_reject_concurrent(monkeypatch):
    """一次覆盖四项加固：
      - brief 缺省从方案派生（不 400）；
      - db 行是 Row 而非 dict（row.get 会抛 AttributeError 的回归）；
      - config_json 是合法但非 dict 的 JSON（cfg.get 抛 AttributeError 的回归）；
      - 同方案第二个 omp 请求被 409 拦下，且首个任务结束后标记被 finally 释放。
    """
    import app.config as cfg
    from app.routers import sections as sec_mod
    from app.services.ai import omp_outline_bridge as bridge

    sec_mod._OMP_OUTLINE_INFLIGHT.clear()
    monkeypatch.setattr(cfg.settings, "omp_task_enabled", True)
    monkeypatch.setattr(sec_mod, "content_generation_in_progress", lambda _sid: None)
    monkeypatch.setattr(sec_mod, "outline_generation_in_progress", lambda _sid: None)

    started = asyncio.Event()
    release = asyncio.Event()
    captured: dict = {}

    async def _slow_tree(**kwargs):
        captured.update(kwargs)
        started.set()
        await release.wait()
        return [{"title": "章一", "level": 1, "description": "", "children": []}]

    async def _fake_save(db, scheme_id, outline, *args, **kwargs):
        return {"ok": True, "count": len(outline)}

    monkeypatch.setattr(bridge, "generate_outline_tree", _slow_tree)
    monkeypatch.setattr(sec_mod, "_save_outline_to_db", _fake_save)

    row = _RowLike(
        {
            "name": "某桥梁专项施工方案",
            "type": "桥梁工程",
            "profession": "市政",
            "word_budget": 30000,
            "config_json": "[]",  # 合法 JSON 但非 dict
        }
    )
    db = _FakeDB(row)

    t1 = asyncio.create_task(sec_mod.generate_outline_via_omp("s1", {}, db))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert "某桥梁专项施工方案" in captured["brief"]
    assert captured["project_type"] == "桥梁工程"
    assert captured["industry"] == "市政"

    # 同方案并发第二个请求 → 409（防重入）
    with pytest.raises(Exception) as ei:
        await sec_mod.generate_outline_via_omp("s1", {}, db)
    assert ei.value.status_code == 409
    assert "已有 omp" in str(ei.value.detail)

    release.set()
    res = await asyncio.wait_for(t1, timeout=2)
    # ✅ R235（2026-11-13 · 并行轮次收口）：引擎路由层落地后，端点响应**加法式**
    #    携带引擎标注键（``engine_used`` 等）⇒ 旧的「全等」断言属锁实现而非锁意图，
    #    按纪律重锚为「意图 + 加法键在场」（意图保留，强度不减）。
    assert res["ok"] is True and res["count"] == 1
    assert res.get("engine_used") == "omp"
    # finally 必须释放，否则僵尸标记会让后续所有请求永久 409
    assert "s1" not in sec_mod._OMP_OUTLINE_INFLIGHT


async def test_omp_endpoint_404_when_scheme_missing(monkeypatch):
    import app.config as cfg
    from app.routers import sections as sec_mod

    monkeypatch.setattr(cfg.settings, "omp_task_enabled", True)
    monkeypatch.setattr(sec_mod, "content_generation_in_progress", lambda _sid: None)
    monkeypatch.setattr(sec_mod, "outline_generation_in_progress", lambda _sid: None)
    with pytest.raises(Exception) as ei:
        await sec_mod.generate_outline_via_omp("missing", {}, _FakeDB(None))
    assert ei.value.status_code == 404
    # 404 发生在 acquire 之前，不应留下僵尸标记
    assert "missing" not in sec_mod._OMP_OUTLINE_INFLIGHT


def test_omp_endpoint_no_row_get_source_lock():
    """源码棘轮：Row 对象无 .get()；config_json 非 dict 必须有 isinstance 守卫。"""
    import inspect

    from app.routers import sections as sec_mod

    src = inspect.getsource(sec_mod.generate_outline_via_omp)
    assert "row.get(" not in src
    assert 'row["type"]' in src
    assert "isinstance(_parsed, dict)" in src
    assert "finally:" in src
    assert "_OMP_OUTLINE_INFLIGHT.discard" in src

