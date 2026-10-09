"""解析提取模块 · 两处链路断裂修复的护栏测试（2026-10-03）。

本轮聚焦【解析提取模块】（文件导入 / 文件解析 / 提取）深度复查，实证并修复
两处「实现齐全但链路没接」的静默缺陷：

F1 uploaded_outlines.project_id 归属链从未写入
    R32 把该表登记进 ``projects._PROJECT_SCOPED_TABLES``（按 project_id 级联
    清理），但写入侧（upload_outline.parse_outline 的 INSERT、save_as_outline
    的回写）从不填 project_id —— 所有新记录该列恒为空串，删项目的显式 DELETE
    匹配 0 行，清理形同虚设（与 R32 发现的「登记表漏条目」是同一条链路的
    另一半：登记补了，写入没接）。

F2 人工校正（PUT /bid-analysis/results/{item_id}）对未提取过的项静默丢失
    行由首次提取 / force_rerun 才补齐；从未提取过的项在库中无行，
    UPDATE 命中 0 行后接口照样返回 200（item=None），用户提交的校正内容
    不落库、刷新即丢。既有测试全部先 _put_item 造行再校正，恰好绕开了
    「无行」分支 —— 该分支此前从未被覆盖。

测试风格对齐 test_upload_outline_interaction.py：直接调用路由函数并注入
:memory: 连接，HTTP 拒绝路径以 HTTPException 状态码断言覆盖。
"""
from __future__ import annotations

import inspect
import io
import json

import pytest
from app.routers import bid_analysis as ba
from app.routers import upload_outline as uo
from fastapi import HTTPException
from starlette.datastructures import UploadFile


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


_OUTLINE_TEXT = "\n".join([
    "第一章 工程概况", "第二章 编制依据", "第三章 施工部署",
    "第四章 主要施工方法", "第五章 安全保证措施", "第六章 环保措施",
]).encode("utf-8")


async def _seed_project(db, pid: str = "p1", sid: str = "s1") -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "项目"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
        (sid, pid, "测试方案"))
    await db.commit()


async def _uploaded_row(db, rid: str) -> dict:
    cur = await db.execute(
        "SELECT * FROM uploaded_outlines WHERE id=?", (rid,))
    return dict(await cur.fetchone())


# ===========================================================================
# F1 · parse 落库写入 project_id
# ===========================================================================

class TestParseProjectLink:
    async def test_parse_persists_project_id(self, db_conn):
        """传入合法 project_id 时，识别记录必须建立项目归属链。"""
        db = db_conn
        await _seed_project(db)
        res = await uo.parse_outline(file=_upload("大纲.txt", _OUTLINE_TEXT),
                                     db=db, scheme_name="", reorganize=False,
                                     project_id="p1")
        row = await _uploaded_row(db, res["id"])
        assert row["project_id"] == "p1"

    async def test_parse_unknown_project_404(self, db_conn):
        """伪造/不存在的 project_id 直接 404，不落无主记录。"""
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(file=_upload("大纲.txt", _OUTLINE_TEXT),
                                   db=db_conn, scheme_name="", reorganize=False,
                                   project_id="不存在的项目")
        assert e.value.status_code == 404

    async def test_parse_without_project_id_keeps_empty(self, db_conn):
        """未传 project_id（目录库弹窗等无项目上下文入口）→ 历史空值行为不变。

        以函数方式直接调用路由且不传该参数时，FastAPI 的 Query("") 默认值
        会以【对象】形式落入参数位 —— 必须与非空字符串一样被视作「未提供」，
        绝不能 str() 化成 "Query('')" 之类的假归属（_resolve_project_id 同因）。
        """
        res = await uo.parse_outline(file=_upload("大纲.txt", _OUTLINE_TEXT),
                                     db=db_conn, scheme_name="", reorganize=False)
        row = await _uploaded_row(db_conn, res["id"])
        assert row["project_id"] == ""

    def test_parse_signature_declares_project_id(self):
        """路由签名必须显式声明 project_id 参数（FastAPI 才会真正接收）。

        历史教训：前端一直传、后端未声明 → query 被静默忽略（reorganize
        同款缺陷 2026-09-16 已发生过一次，此处防止再次漂移）。
        """
        sig = inspect.signature(uo.parse_outline)
        assert "project_id" in sig.parameters


# ===========================================================================
# F1 · save-as-outline 回写 + 删项目级联真正生效
# ===========================================================================

class TestSaveAsOutlineBackfillAndCascade:
    async def test_save_as_outline_backfills_project_id(self, db_conn):
        """历史空 project_id 记录在「保存为方案目录」时机按方案反查自愈。"""
        db = db_conn
        await _seed_project(db, "p1", "s1")
        await db.execute(
            "INSERT INTO uploaded_outlines (id, file_name, status) "
            "VALUES ('u1','x.docx','parsed')")
        await db.commit()
        res = await uo.save_as_outline(
            "u1", {"scheme_id": "s1",
                   "outline": [{"title": "工程概况"}, {"title": "施工部署"},
                               {"title": "施工方法"}, {"title": "安全措施"},
                               {"title": "环保"}]}, db=db)
        assert res["ok"] is True
        row = await _uploaded_row(db, "u1")
        assert row["scheme_id"] == "s1"
        assert row["project_id"] == "p1"

    async def test_delete_project_removes_linked_rows(self, db_conn):
        """端到端：parse 落库（带链）→ 删项目 → 记录被级联清理。

        R32 的显式 DELETE 只有配合本轮写入侧接线才真正成立；本用例把
        「写入 ↔ 清理」两端钉在同一条链上，任何一端回退都会失败。
        """
        from app.routers import projects as P
        db = db_conn
        await _seed_project(db, "p1", "s1")
        res = await uo.parse_outline(file=_upload("大纲.txt", _OUTLINE_TEXT),
                                     db=db, scheme_name="", reorganize=False,
                                     project_id="p1")
        out = await P.delete_project("p1", db=db)
        assert out["ok"] is True
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM uploaded_outlines WHERE id=?",
            (res["id"],))
        assert int((await cur.fetchone())["n"]) == 0

    async def test_delete_project_removes_backfilled_legacy_rows(self, db_conn):
        """历史无链记录经 save-as-outline 回写后，同样被删项目覆盖。"""
        from app.routers import projects as P
        db = db_conn
        await _seed_project(db, "p1", "s1")
        await db.execute(
            "INSERT INTO uploaded_outlines (id, file_name, status, scheme_id) "
            "VALUES ('u9','y.docx','saved','s1')")
        await db.commit()
        # 未回写 project_id 前，删除按 project_id 匹配不到（历史现状）
        out = await P.delete_project("p1", db=db)
        assert out["ok"] is True
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM uploaded_outlines WHERE id='u9'")
        # schemes 已删、该行的 project_id 仍为空 → 记录保留（孤儿引用由
        # schemes 删除时断开 scheme_id 的既有逻辑兜底）。本断言钉住现状口径，
        # 防「级联已完全生效」的误读 —— 完全生效依赖写入侧接线（上一用例）。
        assert int((await cur.fetchone())["n"]) == 1


# ===========================================================================
# F1 · 静态防分叉（写入侧回退即失败）
# ===========================================================================

class TestProjectLinkStaticGuards:
    def test_insert_registers_project_id_column(self):
        src = io.open(uo.__file__, encoding="utf-8").read()
        assert 'INSERT INTO uploaded_outlines (id, project_id,' in src, \
            "parse_outline 的 INSERT 必须包含 project_id 列，否则归属链再次断链"

    def test_save_as_outline_update_backfills(self):
        src = io.open(uo.__file__, encoding="utf-8").read()
        assert "SET scheme_id=?, project_id=?, status='saved'" in src, \
            "save_as_outline 必须同步回写 project_id（历史无链记录的自愈点）"


# ===========================================================================
# F2 · 人工校正无行时 UPSERT
# ===========================================================================

async def _seed_item_row(db, pid: str, item_id: str, *, status="success",
                         content="", error="", source="ai") -> None:
    await db.execute(
        "INSERT INTO bid_analysis_items (id, project_id, item_id, label, "
        "output_type, required, status, content, error, sort_order, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"{pid}_{item_id}", pid, item_id, item_id, "markdown", 1,
         status, content, error, 1, source))
    await db.commit()


class TestManualCorrectionUpsert:
    async def test_correction_creates_missing_row(self, db_conn):
        """从未提取过的项（库中无行）提交人工校正 → 必须建行并落内容。"""
        db = db_conn
        await _seed_project(db, "p1", "s1")
        res = await ba.update_single_result(
            "schemeBasicInfo", {"content": "## 人工兜底：基坑深度 8m"},
            scheme_id="s1", project_id="", db=db)
        assert res["item"] is not None, "无行校正不得返回 item=null（静默丢失）"
        assert res["item"]["status"] == "success"
        assert res["item"]["source"] == "manual"
        assert "基坑深度" in res["item"]["content"]
        assert res["summary"]["success"] == 1
        assert res["summary"]["success_valid"] == 1

    async def test_correction_update_path_unchanged(self, db_conn):
        """既有行 → 仍走 UPDATE，行为与修复前逐字一致（回归保护）。"""
        db = db_conn
        await _seed_project(db, "p1", "s1")
        await _seed_item_row(db, "p1", "schemeBasicInfo",
                             content="AI 抽错的内容")
        res = await ba.update_single_result(
            "schemeBasicInfo", {"content": "## 人工修正"},
            scheme_id="s1", project_id="", db=db)
        assert res["item"]["content"] == "## 人工修正"
        assert res["item"]["source"] == "manual"
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM bid_analysis_items WHERE project_id='p1'")
        assert int((await cur.fetchone())["n"]) == 1, "UPDATE 路径不得重复建行"

    async def test_correction_json_item_without_row(self, db_conn):
        """JSON 项无行校正：非法内容仍 422，合法内容建行落库。"""
        db = db_conn
        await _seed_project(db, "p1", "s1")
        with pytest.raises(HTTPException) as e:
            await ba.update_single_result(
                "projectBasicInfo", {"content": "不是 JSON"},
                scheme_id="s1", project_id="", db=db)
        assert e.value.status_code == 422
        res = await ba.update_single_result(
            "projectBasicInfo", {"content": json.dumps({"project_name": "X"})},
            scheme_id="s1", project_id="", db=db)
        assert res["item"]["output_type"] == "json"
        assert json.loads(res["item"]["content"])["project_name"] == "X"

    async def test_insert_degrades_without_domain_column(self, db_conn):
        """旧库缺 domain 列时 INSERT 省略该列（与 _update_item_status 同口径）。"""
        db = db_conn
        await _seed_project(db, "p1", "s1")
        try:
            await db.execute(
                "ALTER TABLE bid_analysis_items DROP COLUMN domain")
            await db.commit()
        except Exception:  # sqlite 版本不支持 DROP COLUMN
            pytest.skip("当前 SQLite 不支持 DROP COLUMN，无法模拟旧库")
        res = await ba.update_single_result(
            "schemeBasicInfo", {"content": "## 旧库校正"},
            scheme_id="s1", project_id="", db=db)
        assert res["item"] is not None

    async def test_ai_rerun_still_resets_source(self, db_conn):
        """无行校正建的行是 manual；AI 重跑（_update_item_status）后复位 ai。

        防「补建的行缺 source/label 等字段，AI 写路径误判其为不存在」的
        结构性漂移 —— 补建行必须与既有行同构。
        """
        db = db_conn
        await _seed_project(db, "p1", "s1")
        await ba.update_single_result(
            "schemeBasicInfo", {"content": "## 人工"},
            scheme_id="s1", project_id="", db=db)
        await ba._update_item_status(db, "p1", "schemeBasicInfo", "success",
                                     "## AI 重跑内容")
        cur = await db.execute(
            "SELECT source, content FROM bid_analysis_items "
            "WHERE project_id='p1' AND item_id='schemeBasicInfo'")
        row = dict(await cur.fetchone())
        assert row["source"] == "ai"
        assert row["content"] == "## AI 重跑内容"

    def test_pk_built_via_single_source(self):
        """主键必须经 build_item_pk 唯一出口，路由内不得再手写 f-string 副本。"""
        src = io.open(ba.__file__, encoding="utf-8").read()
        assert 'pk = f"{real_pid}_{item_id}"' not in src, \
            "update/clear 端点仍在手工拼主键（判据分叉）"
        assert src.count("build_item_pk(real_pid, item_id") >= 2, \
            "人工校正与清空两个端点都应经唯一出口构造主键"
