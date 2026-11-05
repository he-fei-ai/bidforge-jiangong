"""事实提取成功后回写文档提取状态的护栏（2026-11-05 · R2-P1 提取状态）。

背景：生产库 ``doc_chunks=266``、``global_facts=190``，但
``project_documents.extract_status`` 长期 ``pending``。唯一写该列的是
``doc_pipeline.sync_extract_layer``（需显式调用），事实 SSE 完成分支落库后
从不回写 —— 状态与数据长期矛盾。
"""


async def _seed_docs(db):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES ('p1','P')")
    rows = [
        ("d1", "a.docx", "已解析正文", "pending"),
        ("d2", "b.pdf", "", "pending"),          # 未解析：不应被标 success
        ("d3", "c.md", "另一份正文", "pending"),
        ("d4", "other.docx", "别的项目", "pending"),
    ]
    for doc_id, name, body, status in rows:
        await db.execute(
            "INSERT OR REPLACE INTO project_documents "
            "(id, project_id, file_name, parsed_markdown, extract_status) VALUES (?,?,?,?,?)",
            (doc_id, "p1" if doc_id != "d4" else "p2", name, body, status),
        )
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES ('p2','P2')")
    await db.commit()


class TestMarkDocsExtractStatus:
    async def test_marks_only_documents_with_parsed_text(self, db_conn):
        from app.routers.sse_handlers import _mark_docs_extract_status

        await _seed_docs(db_conn)
        n = await _mark_docs_extract_status(db_conn, "p1", "success")
        assert n == 2  # d1 / d3；d2 无正文、d4 属于别的项目

        cur = await db_conn.execute(
            "SELECT id, extract_status, extract_time FROM project_documents ORDER BY id"
        )
        rows = {r["id"]: dict(r) for r in await cur.fetchall()}
        assert rows["d1"]["extract_status"] == "success"
        assert rows["d3"]["extract_status"] == "success"
        assert rows["d1"]["extract_time"], "extract_time 必须同时回写"
        assert rows["d2"]["extract_status"] == "pending"
        assert rows["d4"]["extract_status"] == "pending"

    async def test_is_idempotent(self, db_conn):
        from app.routers.sse_handlers import _mark_docs_extract_status

        await _seed_docs(db_conn)
        await _mark_docs_extract_status(db_conn, "p1")
        await _mark_docs_extract_status(db_conn, "p1")
        cur = await db_conn.execute(
            "SELECT COUNT(*) AS n FROM project_documents WHERE project_id='p1' AND extract_status='success'"
        )
        assert (await cur.fetchone())["n"] == 2

    async def test_project_without_docs_returns_zero(self, db_conn):
        from app.routers.sse_handlers import _mark_docs_extract_status

        await db_conn.execute("INSERT OR IGNORE INTO projects (id, name) VALUES ('p3','P3')")
        await db_conn.commit()
        assert await _mark_docs_extract_status(db_conn, "p3") == 0

    async def test_falls_back_when_columns_missing(self, db_conn):
        """旧库缺列时必须静默跳过（0 行），不能抛错阻断提取完成分支。"""
        from app.routers.sse_handlers import _mark_docs_extract_status

        await db_conn.execute("DROP TABLE project_documents")
        await db_conn.execute(
            "CREATE TABLE project_documents (id TEXT PRIMARY KEY, project_id TEXT, "
            "file_name TEXT, parsed_markdown TEXT)"
        )
        await db_conn.commit()
        assert await _mark_docs_extract_status(db_conn, "p1") == 0
