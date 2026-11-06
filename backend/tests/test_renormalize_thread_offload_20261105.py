"""结构重排后「全量正文重规范化」移出事件循环的回归护栏（2026-11-05 · R2-P0-1）。

背景（实测数据）
----------------
`sections.py::_renormalize_all_section_contents` 在 async 请求里逐章
`await normalize_section_content_subheadings`。虽然每章之间有一次 await，
但**解析 + 重编号是纯 CPU**（且后续 `text_word_count` 同样是同步扫描），
整轮把事件循环占住：生产日志实测 `/sections` 8,620ms / 10,173ms、
同一时刻 `/sse/tasks` 卡 8,640ms，而其余时段 `/sections` P90 仅 128ms。

修复：新增 `numbering.normalize_section_contents_from_index`（无 IO 的批量
纯计算），由 `asyncio.to_thread` 执行；DB 读写与事务边界不变。

本文件锁定三件事：
1. **逐条等价**：批量结果与「逐章传 index」的旧路径逐字节一致（含脏数据）；
2. **不阻塞事件循环**：批量计算执行期间心跳协程仍在推进（线程身份断言 +
   心跳计数断言，都是确定性判据，不依赖机器速度）；
3. **降级安全**：预取索引缺项时回退逐章查库（与旧实现同一条路径）。
"""

import asyncio
import json
import threading
import time

import pytest


async def _seed(db, rows):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES ('p1','P')")
    await db.execute("INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES ('s1','p1','S')")
    for sid, parent, outline, level, title, content in rows:
        await db.execute(
            "INSERT OR REPLACE INTO sections (id, scheme_id, project_id, title, "
            "parent_id, level, sort_order, outline_json, content) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sid,
                "s1",
                "p1",
                title,
                parent,
                str(level),
                0,
                outline if isinstance(outline, str) else json.dumps(outline, ensure_ascii=False),
                content,
            ),
        )
    await db.commit()


_BODY = "\n".join(["### 1.1 施工准备", "本段为正文内容示例。" * 5, "### 2.3 验收要求", "- 要点一", "- 要点二"])


def _mixed_rows(n=6):
    rows = [(f"sec{i}", None, {"id": str(i + 1)}, 1, f"第{i + 1}章", _BODY) for i in range(n)]
    rows.append(("child_a", "sec0", {"id": "1.1"}, 2, "1.1 子章", _BODY))
    rows.append(("dirty_noid", None, {"id": ""}, 1, "无编号脏数据", "### 9.9 正文"))
    rows.append(("dirty_json", None, "not-a-json", 1, "坏 JSON", "### 8.8 正文"))
    rows.append(("empty", None, {"id": "9"}, 1, "空正文", ""))
    return rows


class TestThreadOffloadParity:
    """批量纯计算与逐章旧路径必须逐字节一致。"""

    async def test_batch_matches_per_section_path(self, db_conn):
        from app.services.numbering import (
            load_scheme_section_index,
            normalize_section_content_subheadings,
            normalize_section_contents_from_index,
        )

        await _seed(db_conn, _mixed_rows())
        cur = await db_conn.execute(
            "SELECT id, content FROM sections WHERE scheme_id='s1' AND COALESCE(content,'')!=''"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        index = await load_scheme_section_index(db_conn, "s1")

        # 旧路径：逐章（传入同一份预取索引，仅把 CPU 放回循环内）
        legacy = {}
        for r in rows:
            new_content, changed = await normalize_section_content_subheadings(
                db_conn, "s1", r["id"], r["content"] or "", index=index
            )
            legacy[r["id"]] = (new_content, changed)

        # 新路径：一次性批量纯计算
        batch, fallback = normalize_section_contents_from_index([(r["id"], r["content"] or "") for r in rows], index)
        # 脏编号（空编号 / 坏 JSON）在预取索引里没有 meta → 必须回退逐章路径
        assert sorted(fallback) == ["dirty_json", "dirty_noid"]
        by_id = {sid: (content, changed) for sid, content, changed in batch}
        assert by_id == {sid: v for sid, v in legacy.items() if sid not in ("dirty_noid", "dirty_json")}

    async def test_db_result_matches_legacy_and_leaves_others_untouched(self, db_conn):
        """整函数级对比：落库结果与旧实现一致，无关章节不得被改写。"""
        from app.routers.sections import _renormalize_all_section_contents
        from app.services.numbering import (
            load_scheme_section_index,
            normalize_section_content_subheadings,
        )

        await _seed(db_conn, _mixed_rows())
        cur = await db_conn.execute(
            "SELECT id, content FROM sections WHERE scheme_id='s1' AND COALESCE(content,'')!=''"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        index = await load_scheme_section_index(db_conn, "s1")
        expected = {}
        for r in rows:
            new_content, changed = await normalize_section_content_subheadings(
                db_conn, "s1", r["id"], r["content"] or "", index=index
            )
            if changed:
                expected[r["id"]] = new_content

        await _renormalize_all_section_contents(db_conn, "s1")

        cur = await db_conn.execute("SELECT id, content FROM sections WHERE scheme_id='s1'")
        actual = {r["id"]: r["content"] for r in await cur.fetchall()}
        for sid, content in expected.items():
            assert actual[sid] == content, f"{sid} 与旧路径结果不一致"
        # 未发生改写的章节必须原样保留（不得被顺手重写/清空）
        for r in rows:
            if r["id"] not in expected:
                assert actual[r["id"]] == r["content"]


class TestThreadOffloadDoesNotBlockLoop:
    """修复的核心目的：CPU 批量计算期间事件循环仍可调度。"""

    async def test_computation_runs_in_worker_thread(self, db_conn, monkeypatch):
        import app.services.numbering as num
        from app.routers.sections import _renormalize_all_section_contents

        await _seed(db_conn, _mixed_rows())
        calls = {"thread": ""}
        real = num.normalize_section_contents_from_index

        def spy(items, index):
            calls["thread"] = threading.current_thread().name
            return real(items, index)

        monkeypatch.setattr(num, "normalize_section_contents_from_index", spy)
        await _renormalize_all_section_contents(db_conn, "s1")
        assert calls["thread"], "批量计算未被执行"
        assert (
            calls["thread"] != threading.main_thread().name
        ), "批量重编号仍在主线程（事件循环）执行 —— to_thread 未生效"

    async def test_event_loop_keeps_ticking_during_slow_batch(self, db_conn, monkeypatch):
        """批量计算阻塞 0.2s 期间，心跳协程仍在推进（旧实现会被完全卡住）。"""
        import app.services.numbering as num
        from app.routers.sections import _renormalize_all_section_contents

        await _seed(db_conn, _mixed_rows())
        real = num.normalize_section_contents_from_index

        def slow(items, index):
            time.sleep(0.2)  # 模拟生产实测的同步 CPU 长耗时
            return real(items, index)

        monkeypatch.setattr(num, "normalize_section_contents_from_index", slow)
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(heartbeat())
        try:
            await _renormalize_all_section_contents(db_conn, "s1")
        finally:
            task.cancel()
        # 若批量计算在事件循环上同步执行，ticks 只会是 0 或 1
        assert ticks >= 5, f"批量计算期间事件循环未推进（ticks={ticks}）"


class TestThreadOffloadFailSoft:
    """降级路径：线程异常 / 索引缺项都不能阻断结构变更。"""

    async def test_thread_failure_falls_back_to_per_section(self, db_conn, monkeypatch):
        import app.services.numbering as num
        from app.routers.sections import _renormalize_all_section_contents

        await _seed(db_conn, _mixed_rows(2))

        def boom(items, index):
            raise RuntimeError("模拟线程内异常")

        monkeypatch.setattr(num, "normalize_section_contents_from_index", boom)
        await _renormalize_all_section_contents(db_conn, "s1")
        # 逐章回退路径仍应把正文规范化（而不是抛错或原样放弃）
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='sec0'")
        row = await cur.fetchone()
        assert row["content"] and row["content"] != _BODY

    async def test_disabled_switch_is_noop(self, db_conn, monkeypatch):
        from app.config import settings
        from app.routers.sections import _renormalize_all_section_contents

        await _seed(db_conn, _mixed_rows(2))
        monkeypatch.setattr(settings, "content_subheading_renumber", False)
        await _renormalize_all_section_contents(db_conn, "s1")
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='sec0'")
        assert (await cur.fetchone())["content"] == _BODY
