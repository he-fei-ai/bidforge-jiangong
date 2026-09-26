"""目录库 / 上传识别入库规范化回归测试

覆盖 2026-09 BUG 修复：目录库写入路径（save_as_library / create_library /
update_library / new_version）此前直接落库原始 JSON，未做三级裁剪与编号重排，
导致目录库可能出现 4+ 级节点、标题内嵌编号等脏数据。
现统一经 normalize_outline_json 收口。
"""
import json
import uuid

import pytest

from app.services.outline_utils import normalize_outline_json


def _deep5():
    """5 层线性目录，标题带内嵌编号"""
    node = {"title": "第五章 深层节点", "children": []}
    for lv in range(4, 0, -1):
        node = {"title": f"第{lv}章 层级{lv}", "children": [node]}
    return [node]


# ============================================================
# normalize_outline_json
# ============================================================
class TestNormalizeOutlineJson:
    def test_clamps_and_renumbers(self):
        out = json.loads(normalize_outline_json(_deep5()))
        # 只保留 3 级
        lv1 = out[0]
        assert lv1["id"] == "1"
        assert lv1["level"] == 1
        lv2 = lv1["children"][0]
        assert lv2["id"] == "1.1"
        lv3 = lv2["children"][0]
        assert lv3["id"] == "1.1.1"
        assert lv3["children"] == []
        # 内嵌编号被剥离
        assert lv1["title"] == "层级1"

    def test_unwraps_dict_payload(self):
        out = json.loads(normalize_outline_json(
            {"outline": [{"title": "第一章 工程概况", "children": []}]}))
        assert out[0]["title"] == "工程概况"

    def test_accepts_json_string(self):
        raw = json.dumps([{"title": "A", "children": []}], ensure_ascii=False)
        out = json.loads(normalize_outline_json(raw))
        assert out[0]["title"] == "A"

    def test_invalid_string_returns_default(self):
        assert normalize_outline_json("not json") == "[]"

    def test_non_list_returns_default(self):
        assert normalize_outline_json(123) == "[]"
        assert normalize_outline_json(None) == "[]"

    def test_does_not_mutate_caller_object(self):
        src = [{"title": "第一章 标题", "children": []}]
        normalize_outline_json(src)
        # 原对象保持不变
        assert src[0]["title"] == "第一章 标题"
        assert "id" not in src[0]


# ============================================================
# save_as_library（上传识别 → 目录库）
# ============================================================
@pytest.mark.asyncio
class TestSaveAsLibraryNormalizes:
    async def test_stored_outline_is_normalized(self, db_conn):
        from app.routers.upload_outline import save_as_library
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, file_name, status) VALUES ('u9','z.docx','parsed')")
        await db_conn.commit()

        res = await save_as_library(
            "u9", {"name": "测试库", "outline": _deep5()}, db_conn)
        cur = await db_conn.execute(
            "SELECT outline_json FROM outline_library WHERE id=?", (res["id"],))
        stored = json.loads((await cur.fetchone())[0])
        assert stored[0]["id"] == "1"
        assert stored[0]["title"] == "层级1"
        assert stored[0]["children"][0]["children"][0]["children"] == []

    async def test_empty_outline_rejected(self, db_conn):
        """PRD 9.2：空目录不得沉淀为目录库。"""
        from fastapi import HTTPException
        from app.routers.upload_outline import save_as_library
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, file_name, status) VALUES ('u10','z.docx','parsed')")
        await db_conn.commit()
        with pytest.raises(HTTPException) as ei:
            await save_as_library("u10", {"name": "空库", "outline": []}, db_conn)
        assert ei.value.status_code == 400

    async def test_all_invalid_nodes_rejected(self, db_conn):
        from fastapi import HTTPException
        from app.routers.upload_outline import save_as_library
        await db_conn.execute(
            "INSERT INTO uploaded_outlines (id, file_name, status) VALUES ('u11','z.docx','parsed')")
        await db_conn.commit()
        with pytest.raises(HTTPException) as ei:
            await save_as_library("u11", {"name": "坏库", "outline": ["bad", 1]}, db_conn)
        assert ei.value.status_code == 400


# ============================================================
# create_library / new_version（目录库写入）
# ============================================================
@pytest.mark.asyncio
class TestCreateLibraryNormalizes:
    async def test_create_normalizes_outline_json(self, db_conn):
        from app.routers.outline_library import create_library
        from app.models import OutlineLibraryCreate
        data = OutlineLibraryCreate(
            name="库A", outline_json=json.dumps(_deep5(), ensure_ascii=False))
        created = await create_library(data, db_conn)
        stored = json.loads(created["outline_json"])
        assert stored[0]["id"] == "1"
        assert stored[0]["children"][0]["children"][0]["children"] == []

    async def test_create_rejects_empty_or_invalid_outline(self, db_conn):
        """✅ 修复（2026-09-16）：空/非法目录不得静默写入空目录库。

        旧实现 normalize 后拿到 "[]" 也照样 INSERT —— 目录库列表出现"看似正常、
        点进去空白"的库，套用后把方案目录清空且无任何提示。
        """
        from app.routers.outline_library import create_library
        from app.models import OutlineLibraryCreate
        from fastapi import HTTPException
        for bad in ("[]", "not-json", '{"foo": 1}', '["bad", 1]'):
            with pytest.raises(HTTPException) as ei:
                await create_library(
                    OutlineLibraryCreate(name="空库", outline_json=bad), db_conn)
            assert ei.value.status_code == 400, bad
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM outline_library WHERE name='空库'")
        assert (await cur.fetchone())[0] == 0

    async def test_new_version_normalizes(self, db_conn):
        from app.routers.outline_library import new_version
        lid = str(uuid.uuid4())
        await db_conn.execute(
            "INSERT INTO outline_library (id, name, outline_json, version, review_status)"
            " VALUES (?,?,?,'v1.0','已通过')",
            (lid, "库B", json.dumps([{"title": "旧", "children": []}], ensure_ascii=False)))
        await db_conn.commit()

        await new_version(lid, {"version": "v2.0",
                                "outline_json": json.dumps(_deep5(), ensure_ascii=False)},
                          db_conn)
        cur = await db_conn.execute(
            "SELECT outline_json FROM outline_library WHERE id=?", (lid,))
        stored = json.loads((await cur.fetchone())[0])
        assert stored[0]["id"] == "1"
        assert stored[0]["level"] == 1
