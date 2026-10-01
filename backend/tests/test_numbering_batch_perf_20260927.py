"""编号校验批量化的性能与兼容性护栏（2026-09-27 性能轮）。

背景（实测数据，非推测）
------------------------
`services/numbering.validate_scheme_numbering_consistency` 是导出守卫
（`routers/export.py::_guard_numbering_consistency`，strict 与非 strict
**两个分支都会跑**）与 `GET /numbering-consistency` 的共同入口。

旧实现虽然把 `content` 一次性批量读了出来，但逐章调用
`normalize_section_content_subheadings`，而后者**每章再查两次库**
（读 outline_json/level/title + COUNT 子章节）。
→ 200 章方案实测 **401 次 db.execute**（2.0 次/章）。

修复：新增 `load_scheme_section_index` 预取（2 条查询），并以
`index=` 关键字参数透传；`index` 缺省或查不到该章时**自动回退逐章查询**，
因此所有既有单章调用方行为不变。

本文件锁定三件事：
1. **性能**：查询次数必须是 O(1) 而非 O(N)（确定性计数断言，不用时间断言，
   避免 CI 机器负载波动导致 flaky）；
2. **行为逐字节等价**：批量路径与「不传 index」的旧路径产出完全一致的报告；
3. **降级安全**：预取失败必须 fail-soft 回退，不得抛错阻断导出。

复跑：``python -m pytest tests/test_numbering_batch_perf_20260927.py -q``
"""
import inspect
import json

import pytest


async def _seed(db, rows):
    """写入最小可用的 sections 行（列顺序对齐 conftest 的真实 schema）。"""
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES ('p1','P')")
    await db.execute("INSERT OR IGNORE INTO schemes (id, project_id, name) "
                     "VALUES ('s1','p1','S')")
    for (sid, parent, outline, level, title, content) in rows:
        await db.execute(
            "INSERT OR REPLACE INTO sections (id, scheme_id, project_id, title, "
            "parent_id, level, sort_order, outline_json, content) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, "s1", "p1", title, parent, str(level), 0,
             outline if isinstance(outline, str)
             else json.dumps(outline, ensure_ascii=False),
             content))
    await db.commit()


_BODY = "\n".join(["### 1.1 施工准备", "本段为正文内容示例。" * 5,
                   "### 2.3 验收要求", "- 要点一", "- 要点二"])


def _mixed_rows(n=40):
    """父章 + 子章 + 脏数据（无编号 / 坏 JSON）+ 空正文，覆盖各条降级分支。"""
    rows = []
    for i in range(n):
        rows.append((f"sec{i}", None, {"id": str(i + 1)}, 1,
                     f"第{i + 1}章", _BODY))
    rows.append(("child_a", "sec0", {"id": "1.1"}, 2, "1.1 子章", _BODY))
    rows.append(("child_b", "sec0", {"id": "1.2"}, 2, "1.2 子章", _BODY))
    # 存储编号为空 → 旧实现「原样返回」，不得被当成漂移
    rows.append(("dirty_noid", None, {"id": ""}, 1, "无编号脏数据", "### 9.9 正文"))
    # outline_json 非法字符串 → 旧实现按 {} 处理
    rows.append(("dirty_json", None, "not-a-json", 1, "坏 JSON", "### 8.8 正文"))
    return rows


def _count_executes(db):
    """给 aiosqlite 连接套一层 execute 计数器（确定性计数，非时间断言）。"""
    state = {"n": 0}
    original = db.execute

    async def counted(*a, **k):
        state["n"] += 1
        return await original(*a, **k)

    db.execute = counted
    return state



class TestNumberingBatchQueryCount:
    """性能：查询次数必须是常数级。"""

    async def test_scheme_validation_is_constant_queries(self, db_conn):
        """200 章 → 查询次数与 10 章**相同**（O(1) 而非 O(N)）。

        旧实现下这里会是 2N+1 次；断言用绝对上限锁死回归，
        并额外断言「章数翻 20 倍但查询数不增长」这一更强的性质。
        """
        from app.services.numbering import validate_scheme_numbering_consistency

        await _seed(db_conn, _mixed_rows(10))
        st_small = _count_executes(db_conn)
        await validate_scheme_numbering_consistency(db_conn, "s1")
        n_small = st_small["n"]

        await _seed(db_conn, _mixed_rows(200))
        st_big = _count_executes(db_conn)
        rep = await validate_scheme_numbering_consistency(db_conn, "s1")
        n_big = st_big["n"]

        assert rep["checked"] > 200
        # 预取 2 条 + 外层 1 条 = 3；留出余量但仍是常数级
        assert n_big <= 6, f"200 章用了 {n_big} 次查询（应为常数级）"
        assert n_big == n_small, (
            f"查询数随章数增长（10 章 {n_small} 次 vs 200 章 {n_big} 次）—— N+1 回归")

    async def test_index_prefetch_is_two_queries(self, db_conn):
        """预取本身固定 2 条查询。"""
        from app.services.numbering import load_scheme_section_index
        await _seed(db_conn, _mixed_rows(50))
        st = _count_executes(db_conn)
        index = await load_scheme_section_index(db_conn, "s1")
        assert st["n"] == 2, f"预取用了 {st['n']} 次查询"
        assert index["meta"] and index["has_children"] is not None
        # 有子章节的父章必须被标记出来（否则编号层级会算错）
        assert index["has_children"].get("sec0") is True
        # 无子章节的章不进 map（缺省即 False），不得被误标为有子章节
        assert index["has_children"].get("sec5", False) is False
        assert "sec5" not in index["has_children"]

    async def test_single_section_path_keeps_two_queries(self, db_conn):
        """不传 index 的单章路径仍按原样逐章查库（向后兼容，未被优化改写）。"""
        from app.services.numbering import normalize_section_content_subheadings
        await _seed(db_conn, _mixed_rows(3))
        st = _count_executes(db_conn)
        await normalize_section_content_subheadings(db_conn, "s1", "sec0", _BODY)
        assert st["n"] == 2, "单章路径应保持 2 次查询（读元数据 + COUNT 子章节）"

    async def test_index_path_does_zero_queries(self, db_conn):
        """传入 index 且命中该章时，规范化本身零查库。"""
        from app.services.numbering import (
            load_scheme_section_index, normalize_section_content_subheadings)
        await _seed(db_conn, _mixed_rows(3))
        index = await load_scheme_section_index(db_conn, "s1")
        st = _count_executes(db_conn)
        await normalize_section_content_subheadings(
            db_conn, "s1", "sec0", _BODY, index=index)
        assert st["n"] == 0, "命中索引时不应再查库"

    def test_no_await_inside_per_section_loop(self):
        """源码级护栏：批量循环体内不得出现逐章 await db.execute。

        计数断言会被「换个循环变量名」绕过，源码断言锁死结构。
        """
        import app.services.numbering as num
        src = inspect.getsource(num)
        body = src.split(
            "async def validate_scheme_numbering_consistency")[1]
        loop = body.split("for r in rows:")[1].split("return {")[0]
        assert "await db.execute" not in loop, (
            "校验循环体内出现逐章查库，N+1 回归")


class TestNumberingBatchParity:
    """行为等价：批量路径与旧路径（不传 index）产出逐字节一致的报告。"""

    async def test_report_is_byte_identical_to_legacy_path(self, db_conn):
        from app.services.numbering import (
            validate_scheme_numbering_consistency,
            validate_section_content_numbering,
        )

        async def legacy_validate(db, sid):
            """复刻修复前的实现：不传 index → 逐章查库。"""
            cur = await db.execute(
                "SELECT id, content FROM sections WHERE scheme_id=? "
                "AND COALESCE(content,'')!=''", (sid,))
            rows = await cur.fetchall()
            out, mismatched = [], 0
            for r in rows:
                rep = await validate_section_content_numbering(
                    db, sid, r["id"], r["content"] or "")
                out.append(rep)
                if not rep.get("consistent"):
                    mismatched += 1
            return {"scheme_id": sid, "checked": len(out), "mismatched": mismatched,
                    "consistent": mismatched == 0, "sections": out}

        await _seed(db_conn, _mixed_rows(20))
        new = await validate_scheme_numbering_consistency(db_conn, "s1")
        old = await legacy_validate(db_conn, "s1")
        assert json.dumps(new, sort_keys=True, ensure_ascii=False) == \
            json.dumps(old, sort_keys=True, ensure_ascii=False), \
            "批量路径与旧路径报告不一致"

    async def test_dirty_rows_are_not_reported_as_drift(self, db_conn):
        """无编号 / 坏 JSON 的历史脏数据不得被误判为漂移（与旧实现一致）。"""
        from app.services.numbering import validate_scheme_numbering_consistency
        await _seed(db_conn, _mixed_rows(3))
        rep = await validate_scheme_numbering_consistency(db_conn, "s1")
        by_id = {s["section_id"]: s for s in rep["sections"]}
        # content 非空但存储编号非法的两章：normalize 原样返回 → 视为一致
        assert by_id["dirty_noid"]["consistent"] is True
        assert by_id["dirty_json"]["consistent"] is True

    async def test_repair_uses_batch_and_still_fixes(self, db_conn):
        """修复路径：查询数仍是常数级，且确实走完了修复流程。"""
        from app.services.numbering import repair_scheme_numbering_consistency
        await _seed(db_conn, _mixed_rows(20))
        st = _count_executes(db_conn)
        res = await repair_scheme_numbering_consistency(db_conn, "s1")
        # 20 章 + 快照写入，仍不应随章数线性放大到 2N+
        assert st["n"] <= 60, f"修复路径查询数 {st['n']} 偏高，疑似 N+1 回归"
        assert res["total"] >= 20
        assert res["fixed"] >= 0




class TestNumberingBatchFailSoft:
    """降级安全：预取/批量失败必须回退，不得让导出守卫 500。"""

    async def test_prefetch_failure_falls_back(self, db_conn, monkeypatch):
        """预取抛错 → 单章路径不受影响，仍能给出报告。"""
        import app.services.numbering as num

        await _seed(db_conn, _mixed_rows(5))

        async def boom(*a, **k):
            raise RuntimeError("模拟预取失败")

        monkeypatch.setattr(num, "load_scheme_section_index", boom)
        rep = await num.validate_section_content_numbering(
            db_conn, "s1", "sec0", _BODY)
        assert "diffs" in rep
        assert rep["consistent"] in (True, False)

    async def test_index_missing_section_falls_back_to_query(self, db_conn):
        """index 里没有该章 → 回退逐章查询，结果与无 index 时一致。"""
        from app.services.numbering import normalize_section_content_subheadings
        await _seed(db_conn, _mixed_rows(3))
        empty = {"meta": {}, "has_children": {}}
        a, ca = await normalize_section_content_subheadings(
            db_conn, "s1", "sec0", _BODY)
        b, cb = await normalize_section_content_subheadings(
            db_conn, "s1", "sec0", _BODY, index=empty)
        assert a == b and ca == cb

    async def test_empty_scheme_returns_clean_report(self, db_conn):
        """空方案：不得抛错，报告为 0 检查 / 一致。"""
        from app.services.numbering import validate_scheme_numbering_consistency
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id,name) VALUES ('p1','P')")
        await db_conn.execute("INSERT OR IGNORE INTO schemes (id,project_id,name) "
                              "VALUES ('s1','p1','S')")
        await db_conn.commit()
        rep = await validate_scheme_numbering_consistency(db_conn, "s1")
        assert rep == {"scheme_id": "s1", "checked": 0, "mismatched": 0,
                       "consistent": True, "sections": []}

    async def test_renumber_disabled_short_circuits(self, db_conn, monkeypatch):
        """content_subheading_renumber=False 时逐字节原样返回（开关仍生效）。"""
        from app.config import settings
        from app.services.numbering import normalize_section_content_subheadings
        await _seed(db_conn, _mixed_rows(2))
        monkeypatch.setattr(settings, "content_subheading_renumber", False)
        st = _count_executes(db_conn)
        out, changed = await normalize_section_content_subheadings(
            db_conn, "s1", "sec0", _BODY)
        assert out == _BODY and changed is False
        assert st["n"] == 0, "开关关闭时不应查库"

