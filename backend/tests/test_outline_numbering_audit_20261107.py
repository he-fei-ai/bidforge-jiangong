"""目录编号审计回归（2026-11-07）。

覆盖本次三个已证缺陷：
  B1 结构编号盲区：outline_json.id/level 与真实树位置漂移时，编号一致性端点/修复必须看见并修好
  B2 标题跨保存漂移：以子项标记开头的标题重复落库必须是不动点
  B3 跨模块元数据丢失：普通目录保存已有章节时必须继承模板/占位元数据

每个用例都对应「改回去即变红」的变异点，见各用例断言语义。
"""

import json

import pytest


async def _seed_scheme(db, pid="p1", sid="s1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "P"))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "S"))
    await db.commit()


def _outline(id_: str, level: int, **extra) -> str:
    payload = {"id": id_, "level": level, "confidence": 1}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


class TestOutlineStructureNumbering:
    async def test_report_detects_and_repair_fixes_stale_ids(self, db_conn):
        """B1：真实树第 2 章存成 "1" → 报告必须红；修复后必须回写 2 并同步正文。"""
        from app.routers.sections import get_numbering_consistency, repair_numbering_consistency

        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, level, sort_order,"
            " outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
            ("secA", "s1", "p1", "工程概况", "", 1, 0, _outline("1", 1), "## 1.1 概况\n正文\n"),
        )
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, level, sort_order,"
            " outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
            ("secB", "s1", "p1", "施工部署", "", 1, 1, _outline("1", 1), "## 1.1 部署\n正文\n"),
        )
        await db_conn.commit()

        before = await get_numbering_consistency("s1", db=db_conn)
        # 变异点：若去掉 endpoint 的 _outline_structure_report 合并，consistent 会变 True
        assert before["consistent"] is False
        assert before["outline"]["mismatched"] == 1
        assert before["outline"]["samples"][0]["section_id"] == "secB"
        assert before["outline"]["samples"][0]["expected"] == "2"

        res = await repair_numbering_consistency("s1", db=db_conn)
        # 变异点：若修复只跑正文（旧实现），outline_fixed 为 0、secB 仍是 "1"
        assert res["outline_fixed"] == 1
        assert res["fixed"] == 1

        cur = await db_conn.execute("SELECT id, outline_json, level, content FROM sections ORDER BY sort_order")
        rows = {r["id"]: r for r in await cur.fetchall()}
        assert json.loads(rows["secA"]["outline_json"])["id"] == "1"
        assert json.loads(rows["secB"]["outline_json"])["id"] == "2"
        assert str(rows["secB"]["level"]) == "1"
        assert "## 2.1 部署" in (rows["secB"]["content"] or "")

        after = await get_numbering_consistency("s1", db=db_conn)
        assert after["consistent"] is True
        assert after["outline"]["mismatched"] == 0
        assert after["mismatched"] == 0

    async def test_consistent_tree_stays_green(self, db_conn):
        """同源树（save-outline 正常写入）不得被结构自检误报。"""
        from app.routers.sections import _save_outline_to_db, get_numbering_consistency

        await _seed_scheme(db_conn)
        saved = await _save_outline_to_db(
            db_conn,
            "s1",
            [
                {
                    "title": "第一章 工程概况",
                    "children": [{"title": "1.1 现场条件", "children": [{"title": "1.1.1 周边环境", "children": []}]}],
                },
                {"title": "第二章 施工部署", "children": [{"title": "2.1 总体流程", "children": []}]},
            ],
        )
        assert saved.get("ok") is not False
        rep = await get_numbering_consistency("s1", db=db_conn)
        assert rep["outline"]["checked"] == 5
        assert rep["outline"]["mismatched"] == 0
        assert rep["outline"]["unverifiable"] == 0
        assert rep["consistent"] is True


class TestTitleStability:
    async def test_marker_title_is_fixpoint_across_saves(self):
        """B2：重复保存同一棵树，标题必须逐字稳定（旧实现第二次会把标记当编号剥掉）。"""
        import copy

        from app.services.numbering import renumber_outline_nodes

        first = renumber_outline_nodes([{"title": "第1章 （1）质量验收"}])
        assert first[0]["title"] == "（1）质量验收"
        second = renumber_outline_nodes(copy.deepcopy(first))
        third = renumber_outline_nodes(copy.deepcopy(second))
        assert second[0]["title"] == "（1）质量验收"
        assert third[0]["title"] == "（1）质量验收"

    async def test_marker_followed_by_chapter_number_converges(self):
        """标记后残留章节编号时，只剥标记后的编号，且结果是不动点。"""
        import copy

        from app.services.numbering import renumber_outline_nodes

        once = renumber_outline_nodes([{"title": "（1）1.1 质量验收"}])
        assert once[0]["title"] == "（1）质量验收"
        twice = renumber_outline_nodes(copy.deepcopy(once))
        assert twice[0]["title"] == "（1）质量验收"

    async def test_chapter_prefix_still_stripped(self):
        """既有锁定契约不回退：多级章节前缀仍要剥净。"""
        from app.services.numbering import renumber_outline_nodes

        out = renumber_outline_nodes([{"title": "第1章 1.1 1.1.1 质量验收"}])
        assert out[0]["title"] == "质量验收"


class TestOutlineMetaRetention:
    async def test_existing_meta_survives_normal_outline_save(self, db_conn):
        """B3：已有章节的模板/占位元数据不得被普通保存抹掉。"""
        from app.routers.sections import _save_outline_to_db

        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, level, sort_order,"
            " outline_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                "secA",
                "s1",
                "p1",
                "工程概况分析",
                "",
                1,
                0,
                _outline(
                    "1",
                    1,
                    content_type="experience",
                    placeholder=True,
                    fact_deps=["周边环境"],
                    fact_values={"层数": 12},
                ),
            ),
        )
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, level, sort_order,"
            " outline_json) VALUES (?,?,?,?,?,?,?,?)",
            ("secB", "s1", "p1", "施工部署", "secA", 2, 0, _outline("1.1", 2)),
        )
        await db_conn.commit()

        saved = await _save_outline_to_db(
            db_conn,
            "s1",
            [
                {
                    "title": "工程概况分析",
                    "__original_id": "secA",
                    "children": [{"title": "施工部署", "__original_id": "secB", "children": []}],
                },
                {"title": "新增章节", "children": [{"title": "新增子项", "children": []}]},
            ],
        )
        assert saved.get("ok") is not False

        cur = await db_conn.execute("SELECT id, outline_json FROM sections WHERE scheme_id='s1'")
        rows = {r["id"]: json.loads(r["outline_json"] or "{}") for r in await cur.fetchall()}
        old_meta = rows["secA"]
        # 变异点：若去掉 existing meta 继承，这四个键会全部消失
        assert old_meta["content_type"] == "experience"
        assert old_meta["placeholder"] is True
        assert old_meta["fact_deps"] == ["周边环境"]
        assert old_meta["fact_values"] == {"层数": 12}
        assert old_meta["id"] == "1"
        assert rows["secB"]["id"] == "1.1"
        # 新节点仍保持既有三键契约（不得被元数据继承逻辑污染）
        new_keys = [k for k in rows if k not in ("secA", "secB")]
        assert len(new_keys) == 2
        for k in new_keys:
            assert sorted(rows[k].keys()) == ["confidence", "id", "level"]

    async def test_existing_meta_survives_reupload_save(self, db_conn):
        """B3 同族：上传识别 → save-as-outline 命中已有章节时同样不得抹掉元数据。"""
        from app.routers.upload_outline import save_as_outline

        await _seed_scheme(db_conn)
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, project_id, scheme_id, status) VALUES (?,?,?,?)",
            ("u1", "p1", "", "parsed"),
        )
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, level, sort_order,"
            " outline_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                "secA",
                "s1",
                "p1",
                "工程概况分析",
                "",
                1,
                0,
                _outline(
                    "1",
                    1,
                    content_type="experience",
                    placeholder=True,
                    fact_deps=["周边环境"],
                    fact_values={"层数": 12},
                ),
            ),
        )
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, level, sort_order,"
            " outline_json) VALUES (?,?,?,?,?,?,?,?)",
            ("secB", "s1", "p1", "施工部署", "secA", 2, 0, _outline("1.1", 2)),
        )
        await db_conn.commit()

        res = await save_as_outline(
            "u1",
            {
                "scheme_id": "s1",
                "outline": [
                    {
                        "title": "工程概况分析",
                        "children": [{"title": "施工部署", "children": []}],
                    }
                ],
            },
            db=db_conn,
        )
        assert res.get("ok") is True

        cur = await db_conn.execute("SELECT id, outline_json FROM sections WHERE scheme_id='s1'")
        rows = {r["id"]: json.loads(r["outline_json"] or "{}") for r in await cur.fetchall()}
        assert set(rows) == {"secA", "secB"}
        # 变异点：若 SELECT 漏取 outline_json 或去掉 matched meta 继承，这四个键会全部消失
        assert rows["secA"]["content_type"] == "experience"
        assert rows["secA"]["placeholder"] is True
        assert rows["secA"]["fact_deps"] == ["周边环境"]
        assert rows["secA"]["fact_values"] == {"层数": 12}
        assert sorted(rows["secB"].keys()) == ["confidence", "id", "level"]
