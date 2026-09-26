"""export_presets 导出格式预设库 CRUD 测试

直接调用 export.py 的端点函数（db 注入测试连接），覆盖：
- 创建 / 列出（默认置顶）/ 设为默认 / 更新 / 删除
- 配置按白名单规范化（未知键丢弃、空值丢弃）
- 项目维度隔离（A 项目的预设对 B 项目不可见）
"""
import pytest

from app.routers import export as export_router


async def _seed(db_conn, projects: list[str], schemes: dict[str, str]):
    for pid in projects:
        await db_conn.execute("INSERT INTO projects (id, name) VALUES (?, ?)", (pid, pid))
    for sid, pid in schemes.items():
        await db_conn.execute(
            "INSERT INTO schemes (id, project_id, name) VALUES (?, ?, ?)", (sid, pid, sid))
    await db_conn.commit()


async def test_export_presets_crud(db_conn):
    await _seed(db_conn, ["p1"], {"s1": "p1"})

    cfg = {"font_name": "黑体", "page_header": "XX项目", "show_page_number": True,
           "unknown_key": "drop_me", "empty_val": ""}
    # 创建
    r = await export_router.create_export_preset("s1", {"name": "标准A", "config": cfg}, db=db_conn)
    pid = r["id"]
    assert r["name"] == "标准A"
    # 配置规范化：白名单外/空值被丢弃
    assert "unknown_key" not in r["config"]
    assert "empty_val" not in r["config"]
    assert r["config"].get("font_name") == "黑体"

    # 列出（仅 1 条）
    lst = await export_router.list_export_presets("s1", db=db_conn)
    assert len(lst["presets"]) == 1

    # 设为默认
    await export_router.set_default_export_preset("s1", pid, db=db_conn)
    lst = await export_router.list_export_presets("s1", db=db_conn)
    assert lst["presets"][0]["is_default"] is True

    # 更新名称与配置
    await export_router.update_export_preset("s1", pid, {"name": "标准B", "config": {"font_name": "宋体"}}, db=db_conn)
    lst = await export_router.list_export_presets("s1", db=db_conn)
    assert lst["presets"][0]["name"] == "标准B"
    assert lst["presets"][0]["config"].get("font_name") == "宋体"

    # 删除
    await export_router.delete_export_preset("s1", pid, db=db_conn)
    lst = await export_router.list_export_presets("s1", db=db_conn)
    assert len(lst["presets"]) == 0


async def test_export_presets_project_isolation(db_conn):
    await _seed(db_conn, ["p1", "p2"], {"s1": "p1", "s2": "p2"})
    await export_router.create_export_preset("s1", {"name": "仅p1", "config": {}}, db=db_conn)

    # 同级项目不可见
    lst2 = await export_router.list_export_presets("s2", db=db_conn)
    assert len(lst2["presets"]) == 0
    # 同项目可见
    lst1 = await export_router.list_export_presets("s1", db=db_conn)
    assert len(lst1["presets"]) == 1


async def test_export_presets_requires_name(db_conn):
    await _seed(db_conn, ["p1"], {"s1": "p1"})
    with pytest.raises(Exception):  # HTTPException 400
        await export_router.create_export_preset("s1", {"name": "  ", "config": {}}, db=db_conn)
