# -*- coding: utf-8 -*-
"""「重置正文」不变量回归测试（2026-09-20 新增）。

POST /schemes/{id}/sections/reset-content 的核心不变量：
  1. **只清正文及其随生产物**：content / word_count / status / word_status /
     review_status / 内联图表 8 列 / chart_predictions；
  2. **目录结构必须保留**：title / parent_id / level / sort_order /
     word_budget / locked / outline_json 原样不动；
  3. **竞态守卫**：本方案 content_generation 任务运行中返回 409
     （与 update_section 手动保存同一口径）；
  4. 方案不存在 404；跨方案隔离（只清本方案）；重复重置幂等。
"""
import uuid

import pytest
from fastapi import HTTPException

from app.routers import sections as sec


async def _seed_scheme(db, scheme_id: str, *, with_content: bool = True):
    """插入 scheme + 3 章（1 一级 + 2 二级），二级章带正文与随生产物。"""
    pid = f"p-{scheme_id[:8]}"
    await db.execute(
        "INSERT INTO projects (id, name) VALUES (?, ?)", (pid, "测试项目"))
    await db.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?, ?, ?)",
        (scheme_id, pid, "测试方案"))
    root_id = f"{scheme_id}-root"
    child1 = f"{scheme_id}-c1"
    child2 = f"{scheme_id}-c2"
    rows = [
        # (id, parent_id, title, level, sort_order, word_budget, content, status,
        #  word_count, word_status, review_status, flowchart_json, gantt_json)
        (root_id, "", "第一章 总述", 1, 0, 2000,
         "一级正文" if with_content else "", "empty" if not with_content else "generated",
         0 if not with_content else 4, "normal" if not with_content else "under", "", "", ""),
        (child1, root_id, "1.1 工程概况", 2, 0, 1500,
         "概况正文" * 10 if with_content else "", "generated" if with_content else "empty",
         40 if with_content else 0, "normal", "已审核" if with_content else "",
         '{"type":"flowchart"}' if with_content else "",
         '{"type":"gantt"}' if with_content else ""),
        (child2, root_id, "1.2 施工工艺", 2, 1, 1800,
         "工艺正文" * 10 if with_content else "", "generated" if with_content else "empty",
         40 if with_content else 0, "normal", "已审核" if with_content else "",
         "", ""),
    ]
    for r in rows:
        (sid, parent_id, title, level, sort_order, budget,
         content, status, word_count, word_status, review_status,
         flowchart, gantt) = r
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " description, level, sort_order, status, word_count, word_budget,"
            " word_status, content, review_status, flowchart_json, gantt_json,"
            " outline_json, locked)"
            " VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (sid, scheme_id, pid, parent_id, title, level, sort_order,
             status, word_count, budget, word_status, content,
             review_status, flowchart, gantt, '{"id":"1.1"}'))
    if with_content:
        await db.execute(
            "INSERT INTO chart_predictions (id, scheme_id, section_id, chart_type, needed)"
            " VALUES (?, ?, ?, 'gantt', 1)",
            (f"cp-{scheme_id[:8]}", scheme_id, child1))
    await db.commit()
    return root_id, child1, child2


async def _get_section(db, section_id: str) -> dict:
    cur = await db.execute("SELECT * FROM sections WHERE id=?", (section_id,))
    row = await cur.fetchone()
    return dict(row) if row else {}


class TestResetContent:
    @pytest.mark.asyncio
    async def test_clears_content_and_byproducts_but_keeps_structure(self, db_conn):
        """正文与随生产物清空；目录结构 / 字数预算 / outline_json 保留。"""
        scheme_id = str(uuid.uuid4())
        root_id, c1, c2 = await _seed_scheme(db_conn, scheme_id)

        result = await sec.reset_content(scheme_id, db=db_conn)

        assert result == {"ok": True, "cleared": 3}
        for sid in (root_id, c1, c2):
            row = await _get_section(db_conn, sid)
            # —— 清空口径 ——
            assert row["content"] == ""
            assert row["word_count"] == 0
            assert row["status"] == "empty"
            assert row["word_status"] == "normal"
            assert row["review_status"] == ""
            assert row["flowchart_json"] == "" and row["gantt_json"] == ""
            # —— 保留口径 ——
            assert row["title"] and row["parent_id"] is not None
            assert row["level"] >= 1
            assert row["word_budget"] in (1500, 1800, 2000)
            assert row["outline_json"] == '{"id":"1.1"}' or sid == root_id
        # 结构关系不变：两个子章仍挂在 root 下
        r1 = await _get_section(db_conn, c1)
        r2 = await _get_section(db_conn, c2)
        assert r1["parent_id"] == root_id and r2["parent_id"] == root_id

    @pytest.mark.asyncio
    async def test_chart_predictions_cleared(self, db_conn):
        """重置后 chart_predictions 残留必须一并清掉（导出 fallback 口径）。"""
        scheme_id = str(uuid.uuid4())
        await _seed_scheme(db_conn, scheme_id)
        await sec.reset_content(scheme_id, db=db_conn)
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM chart_predictions WHERE scheme_id=?", (scheme_id,))
        assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_404_when_scheme_missing(self, db_conn):
        with pytest.raises(HTTPException) as ei:
            await sec.reset_content("no-such-scheme", db=db_conn)
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_409_while_content_task_running(self, db_conn, monkeypatch):
        """本方案正文生成任务运行中必须 409，且**不得**改动任何章节。

        ✅ G12-4（2026-09-20）：守卫改为**状态感知**（只拦 running/paused），
        因此 fixture 必须写全 `status='running'`（与 register_task 的真实形状一致）。
        旧 fixture 无 status 字段，在状态感知守卫下会被当成终态残留而放行。
        """
        from app.services.ai import task_registry as tr
        scheme_id = str(uuid.uuid4())
        root_id, _, _ = await _seed_scheme(db_conn, scheme_id)
        monkeypatch.setattr(tr, "_tasks", {
            "t1": {"type": "content_generation", "status": "running",
                   "scheme_id": scheme_id},
        })
        with pytest.raises(HTTPException) as ei:
            await sec.reset_content(scheme_id, db=db_conn)
        assert ei.value.status_code == 409
        row = await _get_section(db_conn, root_id)
        assert row["content"] != ""  # 守卫前置：未清任何数据

    @pytest.mark.asyncio
    async def test_terminal_task_does_not_block(self, db_conn, monkeypatch):
        """G12-4 回归：终态残留条目（stopped/failed/completed）不得阻塞重置。

        旧守卫只看 type + scheme_id，残留一个 stopped 条目就会让本方案所有章节
        的手工保存 / 重置正文永久 409，只能重启后端。
        """
        from app.services.ai import task_registry as tr
        scheme_id = str(uuid.uuid4())
        await _seed_scheme(db_conn, scheme_id)
        monkeypatch.setattr(tr, "_tasks", {
            "t1": {"type": "content_generation", "status": "stopped",
                   "scheme_id": scheme_id},
        })
        result = await sec.reset_content(scheme_id, db=db_conn)
        assert result["ok"] is True and result["cleared"] == 3

    @pytest.mark.asyncio
    async def test_other_scheme_task_does_not_block(self, db_conn, monkeypatch):
        """其它方案的生成任务不应阻断本方案重置。"""
        from app.services.ai import task_registry as tr
        scheme_id = str(uuid.uuid4())
        await _seed_scheme(db_conn, scheme_id)
        monkeypatch.setattr(tr, "_tasks", {
            "t1": {"type": "content_generation", "scheme_id": "another-scheme"},
        })
        result = await sec.reset_content(scheme_id, db=db_conn)
        assert result["ok"] is True and result["cleared"] == 3

    @pytest.mark.asyncio
    async def test_cross_scheme_isolation_and_idempotent(self, db_conn):
        """只清本方案；重复重置幂等（cleared 递减，不报错）。"""
        s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
        await _seed_scheme(db_conn, s1)
        await _seed_scheme(db_conn, s2)

        first = await sec.reset_content(s1, db=db_conn)
        assert first == {"ok": True, "cleared": 3}
        # 另一方案不受影响
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM sections WHERE scheme_id=? AND content!=''", (s2,))
        assert (await cur.fetchone())[0] == 3
        # 幂等：第二次重置 cleared=0
        again = await sec.reset_content(s1, db=db_conn)
        assert again == {"ok": True, "cleared": 0}
