"""R175 目录生成模块修复回归（2026-11-03）

覆盖三项修复：
1. BUG-1：`_use_stepwise_for_budget` 唯一判定点 + `outline_scheme` 三档矩阵
   （auto 与旧裸比较 `word_budget > 50000` 逐字等价，含边界 50000/50001）；
2. BUG-2：/sections/reorder 完备性校验（全量 / 缺项 / 多项 / 重复 / 跨方案）；
3. G-3：save-outline「空壳一级章」闸门（手动编辑 400、AI 部分成果放行、
   显式 allow_empty_chapters 放行、合法末级节点与全平铺目录不误伤）。
"""

import json
import uuid

import app.db as _appdb
import pytest
from app.db import close_db, get_conn, init_db
from app.routers import sse_handlers as sh
from app.routers.sections import (
    _detect_empty_top_chapters,
    _save_outline_to_db,
    reorder_sections,
)
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def ctx(tmp_path):
    _appdb.DB_PATH = tmp_path / "t.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.commit()
    yield db, sid, pid
    await db.execute("DELETE FROM sections WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM schemes WHERE project_id=?", (pid,))
    await db.execute("DELETE FROM projects WHERE id=?", (pid,))
    await db.commit()
    await close_db()


async def _seed(db, sid, pid, nodes: list):
    """nodes: [(dbid, parent_id, title, sort_order, level)]"""
    for nid, parent, title, so, level in nodes:
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title, level,"
            " sort_order, status, outline_json, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                nid,
                sid,
                pid,
                parent,
                title,
                level,
                so,
                "empty",
                json.dumps({"id": str(level), "confidence": 0.9}, ensure_ascii=False),
                1500,
            ),
        )
    await db.commit()


def _node(node_id: str, title: str, children: list | None = None) -> dict:
    return {"id": node_id, "title": title, "children": children or []}


# ---------------------------------------------------------------------------
# 1. BUG-1：分步生成判定矩阵
# ---------------------------------------------------------------------------
class TestUseStepwiseForBudget:
    def test_auto_matches_legacy_comparison(self, monkeypatch):
        """auto 必须与旧实现 `word_budget > 50000` 逐字等价（含边界取等）。"""
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 50000)
        monkeypatch.setattr(sh, "OUTLINE_SCHEME", "auto")
        assert sh._use_stepwise_for_budget(50000) is False  # 边界取等 = 不分步
        assert sh._use_stepwise_for_budget(50001) is True
        assert sh._use_stepwise_for_budget(30000) is False
        assert sh._use_stepwise_for_budget(49999) is False

    def test_auto_honours_configured_threshold(self, monkeypatch):
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 1000)
        monkeypatch.setattr(sh, "OUTLINE_SCHEME", "auto")
        assert sh._use_stepwise_for_budget(1000) is False
        assert sh._use_stepwise_for_budget(1001) is True

    def test_oneshot_forces_oneshot_regardless_of_budget(self, monkeypatch):
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 50000)
        monkeypatch.setattr(sh, "OUTLINE_SCHEME", "oneshot")
        assert sh._use_stepwise_for_budget(50001) is False
        assert sh._use_stepwise_for_budget(200000) is False

    def test_stepwise_forces_stepwise_regardless_of_budget(self, monkeypatch):
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 50000)
        monkeypatch.setattr(sh, "OUTLINE_SCHEME", "stepwise")
        assert sh._use_stepwise_for_budget(30000) is True
        assert sh._use_stepwise_for_budget(100) is True

    def test_invalid_or_empty_scheme_falls_back_to_auto(self, monkeypatch):
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 50000)
        for bad in ("foo", "", None, "  "):
            monkeypatch.setattr(sh, "OUTLINE_SCHEME", bad)
            assert sh._use_stepwise_for_budget(50001) is True
            assert sh._use_stepwise_for_budget(50000) is False

    def test_scheme_normalized_case_and_whitespace(self):
        assert sh._use_stepwise_for_budget(100, scheme=" STEPWISE ") is True
        assert sh._use_stepwise_for_budget(999999, scheme="OneShot") is False

    def test_dirty_word_budget_never_raises(self, monkeypatch):
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 50000)
        assert sh._use_stepwise_for_budget(None) is False
        assert sh._use_stepwise_for_budget("abc") is False

    def test_oneshot_fallback_semantics_do_not_conflict(self, monkeypatch):
        """强制 oneshot 仍返回 False（首轮单次）；降级兜底只在失败后由主流程接管。"""
        monkeypatch.setattr(sh, "OUTLINE_ONESHOT_FALLBACK", True)
        assert sh._use_stepwise_for_budget(200000, scheme="oneshot") is False
        # 兜底常量独立于判定：关闭兜底也不改变 auto 分档结果
        monkeypatch.setattr(sh, "OUTLINE_ONESHOT_FALLBACK", False)
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 50000)
        assert sh._use_stepwise_for_budget(50001, scheme="auto") is True

    def test_config_default_is_auto(self):
        from app.config import settings

        assert str(getattr(settings, "outline_scheme", "")).strip().lower() == "auto"


# ---------------------------------------------------------------------------
# 2. BUG-2：reorder 完备性校验
# ---------------------------------------------------------------------------
class TestReorderCompleteness:
    async def test_full_order_passes(self, ctx):
        db, sid, pid = ctx
        s1, s2, s2c = (uuid.uuid4().hex for _ in range(3))
        await _seed(
            db,
            sid,
            pid,
            [
                (s1, "", "A", 0, 1),
                (s2, "", "B", 1, 1),
                (s2c, s2, "B1", 0, 2),
            ],
        )
        await reorder_sections(sid, {"order": [s1, s2, s2c]}, db)
        cur = await db.execute("SELECT id, outline_json FROM sections WHERE scheme_id=?", (sid,))
        ids = {r["id"]: json.loads(r["outline_json"])["id"] for r in await cur.fetchall()}
        assert ids[s1] == "1" and ids[s2] == "2" and ids[s2c] == "2.1"

    async def test_missing_one_id_rejected(self, ctx):
        db, sid, pid = ctx
        s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
        await _seed(db, sid, pid, [(s1, "", "A", 0, 1), (s2, "", "B", 1, 1)])
        with pytest.raises(HTTPException) as ei:
            await reorder_sections(sid, {"order": [s1]}, db)
        assert ei.value.status_code == 400
        assert "缺少 1 个" in str(ei.value.detail)

    async def test_extra_id_rejected(self, ctx):
        db, sid, pid = ctx
        s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
        await _seed(db, sid, pid, [(s1, "", "A", 0, 1), (s2, "", "B", 1, 1)])
        with pytest.raises(HTTPException) as ei:
            await reorder_sections(sid, {"order": [s1, s2, uuid.uuid4().hex]}, db)
        assert ei.value.status_code == 400
        assert "不属于本方案" in str(ei.value.detail)

    async def test_duplicate_id_rejected(self, ctx):
        db, sid, pid = ctx
        s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
        await _seed(db, sid, pid, [(s1, "", "A", 0, 1), (s2, "", "B", 1, 1)])
        with pytest.raises(HTTPException) as ei:
            await reorder_sections(sid, {"order": [s1, s2, s2]}, db)
        assert ei.value.status_code == 400
        assert "重复" in str(ei.value.detail)

    async def test_cross_scheme_id_rejected(self, ctx):
        db, sid, pid = ctx
        s1 = uuid.uuid4().hex
        await _seed(db, sid, pid, [(s1, "", "A", 0, 1)])
        # 另一个方案下的章节 id 混入 order
        sid2 = uuid.uuid4().hex
        await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid2, pid, "s2"))
        s_other = uuid.uuid4().hex
        await _seed(db, sid2, pid, [(s_other, "", "X", 0, 1)])
        with pytest.raises(HTTPException) as ei:
            await reorder_sections(sid, {"order": [s1, s_other]}, db)
        assert ei.value.status_code == 400
        assert "不属于本方案" in str(ei.value.detail)


# ---------------------------------------------------------------------------
# 3. G-3：空壳一级章闸门
# ---------------------------------------------------------------------------
class TestEmptyTopChaptersGate:
    def test_detector_gate_matrix(self):
        # 多级结构中夹空壳一级章 → 检出
        mixed = [_node("1", "A", [_node("1.1", "A1")]), _node("2", "B")]
        assert _detect_empty_top_chapters(mixed) == [{"index": 1, "title": "B"}]
        # 全平铺目录 → 合法短目录，不报
        flat = [_node("1", "A"), _node("2", "B")]
        assert _detect_empty_top_chapters(flat) == []
        # 全部带子章节 → 通过；二级末级节点不参与判据
        nested = [_node("1", "A", [_node("1.1", "A1")]), _node("2", "B", [_node("2.1", "B1")])]
        assert _detect_empty_top_chapters(nested) == []
        # 空树
        assert _detect_empty_top_chapters([]) == []

    async def test_manual_save_with_empty_top_rejected(self, ctx):
        db, sid, _ = ctx
        outline = [_node(uuid.uuid4().hex, "甲", [_node(uuid.uuid4().hex, "甲-1")]), _node(uuid.uuid4().hex, "乙")]
        with pytest.raises(HTTPException) as ei:
            await _save_outline_to_db(db, sid, outline, source="手动编辑")
        assert ei.value.status_code == 400
        assert "无子章节的一级章" in str(ei.value.detail)
        assert "allow_empty_chapters=true" in str(ei.value.detail)

    async def test_ai_partial_save_allowed_and_reported(self, ctx):
        """分步失败章（degraded/部分成果）：source=ai 放行，且加法式回传明细。"""
        db, sid, _ = ctx
        outline = [_node(uuid.uuid4().hex, "甲", [_node(uuid.uuid4().hex, "甲-1")]), _node(uuid.uuid4().hex, "乙")]
        result = await _save_outline_to_db(db, sid, outline, source="ai", lock_roots=True)
        assert result["ok"] is True
        assert result["empty_top_chapters"] == [{"index": 1, "title": "乙"}]
        assert result.get("roots_locked") is True

    async def test_explicit_allow_flag_passes(self, ctx):
        db, sid, _ = ctx
        outline = [_node(uuid.uuid4().hex, "甲", [_node(uuid.uuid4().hex, "甲-1")]), _node(uuid.uuid4().hex, "乙")]
        result = await _save_outline_to_db(db, sid, outline, source="手动编辑", allow_empty_chapters=True)
        assert result["ok"] is True and result["empty_top_chapters"]

    async def test_flat_outline_passes_manual_save(self, ctx):
        """全平铺目录 = 合法短目录，不得误伤。"""
        db, sid, _ = ctx
        outline = [_node(uuid.uuid4().hex, "甲"), _node(uuid.uuid4().hex, "乙")]
        result = await _save_outline_to_db(db, sid, outline, source="手动编辑")
        assert result["ok"] is True and result["count"] == 2
        assert "empty_top_chapters" not in result

    async def test_legal_leaf_nodes_not_rejected(self, ctx):
        """二级末级节点（无三级）是合法节点，不参与空壳判据。"""
        db, sid, _ = ctx
        outline = [
            _node(uuid.uuid4().hex, "甲", [_node(uuid.uuid4().hex, "甲-1")]),
            _node(uuid.uuid4().hex, "乙", [_node(uuid.uuid4().hex, "乙-1")]),
        ]
        result = await _save_outline_to_db(db, sid, outline, source="手动编辑")
        assert result["ok"] is True
        assert "empty_top_chapters" not in result


# ---------------------------------------------------------------------------
# 4. G-3 补接线：上传落库路径（upload_outline.save_as_outline）同一空壳闸门
# ---------------------------------------------------------------------------
class TestUploadOutlineEmptyTopGate:
    async def _seed_upload(self, db, upload_id: str):
        await db.execute("INSERT INTO uploaded_outlines (id, status) VALUES (?, 'parsed')", (upload_id,))

    async def test_mixed_shape_upload_rejected(self, ctx):
        """混合形态（有嵌套一级章 + 空壳一级章）上传 → 400，且零写入。"""
        from app.routers.upload_outline import save_as_outline

        db, sid, _ = ctx
        up = uuid.uuid4().hex
        await self._seed_upload(db, up)
        outline = [
            _node(uuid.uuid4().hex, "工程概况", [_node(uuid.uuid4().hex, "工程简介")]),
            _node(uuid.uuid4().hex, "施工部署"),
        ]
        with pytest.raises(HTTPException) as ei:
            await save_as_outline(up, {"scheme_id": sid, "outline": outline}, db=db)
        assert ei.value.status_code == 400
        assert "无子章节的一级章" in str(ei.value.detail)
        assert "allow_empty_chapters=true" in str(ei.value.detail)
        # 校验在任何写操作之前：sections 零行
        cur = await db.execute("SELECT COUNT(*) c FROM sections WHERE scheme_id=?", (sid,))
        assert (await cur.fetchone())["c"] == 0

    async def test_flat_upload_passes(self, ctx):
        """全平铺上传 = 合法短目录，不误伤。"""
        from app.routers.upload_outline import save_as_outline

        db, sid, pid = ctx
        up = uuid.uuid4().hex
        await self._seed_upload(db, up)
        outline = [_node(uuid.uuid4().hex, "编制依据"), _node(uuid.uuid4().hex, "工程概况")]
        result = await save_as_outline(up, {"scheme_id": sid, "outline": outline}, db=db)
        assert result["ok"] is True and result["count"] == 2
        assert "empty_top_chapters" not in result
        # 方案目录来源已回写（证明整条落库链路真实走完）
        cur = await db.execute("SELECT outline_source FROM schemes WHERE id=?", (sid,))
        assert (await cur.fetchone())["outline_source"] == "上传识别"

    async def test_escape_hatch_saves_with_report(self, ctx):
        """allow_empty_chapters=true → 放行落库，并加法式回传空壳明细。"""
        from app.routers.upload_outline import save_as_outline

        db, sid, _ = ctx
        up = uuid.uuid4().hex
        await self._seed_upload(db, up)
        outline = [
            _node(uuid.uuid4().hex, "工程概况", [_node(uuid.uuid4().hex, "工程简介")]),
            _node(uuid.uuid4().hex, "施工部署"),
        ]
        result = await save_as_outline(up, {"scheme_id": sid, "outline": outline, "allow_empty_chapters": True}, db=db)
        assert result["ok"] is True and result["count"] == 3
        assert result["empty_top_chapters"] == [{"index": 1, "title": "施工部署"}]

    async def test_full_nested_upload_passes(self, ctx):
        """全嵌套目录正常落库（编号 1 / 1.1 / 2 / 2.1 由 normalize_outline 重排）。"""
        from app.routers.upload_outline import save_as_outline

        db, sid, _ = ctx
        up = uuid.uuid4().hex
        await self._seed_upload(db, up)
        outline = [
            _node("n1", "工程概况", [_node("n1a", "工程简介")]),
            _node("n2", "施工工艺", [_node("n2a", "工艺流程")]),
        ]
        result = await save_as_outline(up, {"scheme_id": sid, "outline": outline}, db=db)
        assert result["ok"] is True and result["count"] == 4
        cur = await db.execute("SELECT outline_json FROM sections WHERE scheme_id=?", (sid,))
        ids = {json.loads(r["outline_json"])["id"] for r in await cur.fetchall()}
        # sort_order 是同父局部序号，跨父 ORDER BY 无意义，这里只断言编号集合
        assert ids == {"1", "1.1", "2", "2.1"}


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
