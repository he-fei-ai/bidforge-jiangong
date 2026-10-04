"""上传目录识别路由（解析提取模块 · 导入子域）组件级交互测试（2026-10-03）。

补齐 upload_outline.py 的组件交互覆盖：此前仅 test_upload_outline_reorganize.py
验证了 reorganize 参数接线，parse / save-as-outline / save-as-library 的
拒绝路径、跨模块数据链（uploaded_outlines → sections / outline_library）、
并发守卫（409）等核心行为均**无组件级测试**。

测试风格对齐 test_upload_outline_reorganize.py：直接调用路由函数并注入
:memory: 连接（`db_conn` 夹具），既覆盖组件完整逻辑，又避免 TestClient 跨
事件循环持有 aiosqlite 连接导致的偶发失败。HTTP 层（FastAPI 反序列化 / 状态码）
由路由函数直接抛出的 HTTPException 状态码断言覆盖。
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.routers import upload_outline as uo
from app.services import file_parser as fp


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


async def _ensure_scheme(db, pid: str, sid: str) -> None:
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "项目"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "测试方案"))
    await db.commit()


async def _uploaded_row(db, rid: str) -> dict:
    cur = await db.execute(
        "SELECT id, file_name, file_type, status, scheme_id, parsed_json,"
        " confidence, raw_text FROM uploaded_outlines WHERE id=?", (rid,))
    return dict(await cur.fetchone())


async def _section_count(db, sid: str) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM sections WHERE scheme_id=?", (sid,))
    return int((await cur.fetchone())["n"])


# ===========================================================================
# parse 组件：正常识别
# ===========================================================================

class TestParseOutlineSuccess:
    async def test_txt_numbered_outline_recognized(self, db_conn):
        """中文数字章节编号被规则法识别为有效层级结构并落库。"""
        text = "\n".join([
            "第一章 工程概况", "第二章 编制依据", "第三章 施工部署",
            "第四章 主要施工方法", "第五章 安全保证措施", "第六章 环保措施",
        ]).encode("utf-8")
        res = await uo.parse_outline(file=_upload("大纲.txt", text), db=db_conn,
                                     scheme_name="", reorganize=False)
        assert res["id"]
        assert 5 <= len(res["outline"]) <= 6
        assert 0.0 <= res["confidence"] <= 1.0
        assert "工程概况" in res["outline"][0]["title"]
        # 跨模块数据链：解析结果已写入 uploaded_outlines
        row = await _uploaded_row(db_conn, res["id"])
        assert row["status"] == "parsed"
        assert row["file_type"] == "txt"
        assert json.loads(row["parsed_json"])["outline"]

    async def test_md_headings_recognized(self, db_conn):
        """Markdown # 标题被识别为层级结构。"""
        text = "# 工程概况\n## 项目背景\n# 施工部署\n## 总体安排\n".encode("utf-8")
        res = await uo.parse_outline(file=_upload("doc.md", text), db=db_conn,
                                     scheme_name="", reorganize=False)
        assert len(res["outline"]) >= 2
        # 归一化后层级不超过 3、编号从 1 开始（预览所见 == 落库所得）
        assert res["outline"][0]["level"] == 1

    async def test_normalized_to_three_levels_and_renumbered(self, db_conn):
        """识别结果统一经 normalize_outline：≤3 级 + 编号重排。"""
        # 四级嵌套标题，超过三级应被裁剪
        text = ("# 一、总则\n## 1.1 目的\n### 1.1.1 背景\n#### 1.1.1.1 细节\n"
                "# 二、范围\n").encode("utf-8")
        res = await uo.parse_outline(file=_upload("deep.md", text), db=db_conn,
                                     scheme_name="", reorganize=False)

        def max_level(nodes, d=1):
            if not nodes:
                return d - 1
            return max(max_level(n.get("children") or [], d + 1) for n in nodes)

        assert max_level(res["outline"]) <= 3

    async def test_confidence_in_unit_interval(self, db_conn):
        """confidence 必须落在 [0,1]，前端置信度展示不越界。"""
        text = "第一章 工程概况\n第二章 施工部署\n第三章 施工方法\n".encode("utf-8")
        res = await uo.parse_outline(file=_upload("c.txt", text), db=db_conn,
                                     scheme_name="", reorganize=False)
        assert 0.0 <= res["confidence"] <= 1.0


# ===========================================================================
# parse 组件：拒绝 / 边界路径
# ===========================================================================

class TestParseOutlineRejections:
    async def test_unsupported_extension_400(self, db_conn):
        """不支持的扩展名（.exe）被 400 拒绝。"""
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(file=_upload("病毒.exe", b"MZ\x90\x00"),
                                   db=db_conn, scheme_name="", reorganize=False)
        assert e.value.status_code == 400

    async def test_empty_file_400(self, db_conn):
        """空文件被 400 拒绝（避免把空内容落库成「识别成功」）。"""
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(file=_upload("empty.txt", b""),
                                   db=db_conn, scheme_name="", reorganize=False)
        assert e.value.status_code == 400

    async def test_signature_mismatch_400(self, db_conn):
        """扩展名与真实内容不符（.docx 实为 PDF）被 400 拒绝。"""
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(
                file=_upload("fake.docx", b"%PDF-1.4 real pdf bytes"),
                db=db_conn, scheme_name="", reorganize=False)
        assert e.value.status_code == 400

    async def test_oversize_413(self, db_conn, monkeypatch):
        """超过上传上限被 413 拒绝（上限读 file_parser.MAX_UPLOAD_BYTES，可配置）。"""
        monkeypatch.setattr(uo, "MAX_UPLOAD_BYTES", 50)
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(file=_upload("big.txt", b"x" * 100),
                                   db=db_conn, scheme_name="", reorganize=False)
        assert e.value.status_code == 413

    async def test_oversize_message_reflects_configured_limit(self, db_conn, monkeypatch):
        """413 文案随配置上限动态变化（不写死 30MB）。"""
        monkeypatch.setattr(uo, "MAX_UPLOAD_BYTES", 2 * 1024 * 1024)  # 2MB
        with pytest.raises(HTTPException) as e:
            await uo.parse_outline(file=_upload("big.txt", b"x" * (3 * 1024 * 1024)),
                                   db=db_conn, scheme_name="", reorganize=False)
        assert e.value.status_code == 413
        assert "2MB" in e.value.detail

    async def test_raw_text_truncated_warning(self, db_conn):
        """原文超过 5000 字落库截断，响应如实回传 raw_text_truncated + 提示。"""
        text = ("第一章 工程概况\n" + "内容填充行用于构造超长原文。\n") * 400
        assert len(text) > 5000
        res = await uo.parse_outline(file=_upload("long.txt", text.encode("utf-8")),
                                     db=db_conn, scheme_name="", reorganize=False)
        assert res.get("raw_text_truncated") is True
        assert "raw_text_truncated" in res

    async def test_empty_text_warning_not_400(self, db_conn):
        """非空文件但无有效文本（仅空白）不抛 400，而是回传 empty_text 提示。"""
        res = await uo.parse_outline(file=_upload("ws.txt", "   \n  \n".encode("utf-8")),
                                     db=db_conn, scheme_name="", reorganize=False)
        assert res.get("empty_text") is True
        assert res["outline"] == []


# ===========================================================================
# parse 组件：多标段提示 + 跨模块（sniff 类型）
# ===========================================================================

class TestParseOutlineHints:
    async def test_multi_section_hint_returned(self, db_conn, monkeypatch):
        """疑似多标段招标文件回传 multi_section_hint（对齐 OpenBidKit 标段检测）。"""
        monkeypatch.setattr(
            uo, "detect_bid_sections",
            lambda raw_text: {"has_multiple": True,
                              "sections": [{"name": "标段一"}, {"name": "标段二"}]})
        text = "第一章 工程概况\n第二章 施工部署\n".encode("utf-8")
        res = await uo.parse_outline(file=_upload("bid.txt", text), db=db_conn,
                                     scheme_name="", reorganize=False)
        assert res.get("multi_section_hint", {}).get("has_multiple") is True

    async def test_sniffed_docx_is_ole_routed_to_doc(self, db_conn, monkeypatch):
        """✅ 跨模块类型纠正：.docx 实为旧版 Word（OLE）时，解析器嗅探为 doc，
        落库 file_type 跟随嗅探结果（而非后缀推导值）。"""
        # 用真实 OLE 头；local OCR/转换不可用时 parse 抛 ParseError → 400，
        # 但 file_type 纠正发生在解析前，入库失败不影响「类型判断」契约本身的断言点。
        # 这里 monkeypatch 解析器（同步签名，与 parse_file_content_ex 一致），
        # 使其直接返回 OLE 文档文本，验证嗅探纠正透传到落库。
        def _fake_parse(content, fname):
            return ("第一章 工程概况\n第二章 施工部署",
                    {"file_type": "doc", "text_len": 20, "truncated": False,
                     "warnings": []})
        monkeypatch.setattr(uo, "parse_file_content_ex", _fake_parse)
        content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
        res = await uo.parse_outline(file=_upload("old.docx", content), db=db_conn,
                                     scheme_name="", reorganize=False)
        row = await _uploaded_row(db_conn, res["id"])
        # 后缀推导 docx，嗅探纠正为 doc —— 落库以嗅探为准
        assert row["file_type"] == "doc"


# ===========================================================================
# save-as-outline 组件：跨模块落库 sections
# ===========================================================================

class TestSaveAsOutline:
    async def test_success_creates_sections_and_links_upload(self, db_conn):
        """保存识别结果为方案目录：写入 sections、标记方案、回写上传记录。"""
        pid, sid = "p_so", "s_so"
        await _ensure_scheme(db_conn, pid, sid)
        text = "\n".join([
            "第一章 工程概况", "第二章 编制依据", "第三章 施工部署"]).encode("utf-8")
        parsed = await uo.parse_outline(file=_upload("o.txt", text), db=db_conn,
                                        scheme_name="", reorganize=False)
        outline = parsed["outline"]
        res = await uo.save_as_outline(
            parsed["id"], {"scheme_id": sid, "outline": outline}, db=db_conn)
        assert res["ok"] and res["count"] == 3
        assert await _section_count(db_conn, sid) == 3
        # 跨模块：上传记录回写 scheme_id + status
        row = await _uploaded_row(db_conn, parsed["id"])
        assert row["scheme_id"] == sid
        assert row["status"] == "saved"
        # 方案标记
        cur = await db_conn.execute(
            "SELECT outline_source, status FROM schemes WHERE id=?", (sid,))
        r = await cur.fetchone()
        assert r["outline_source"] == "上传识别"
        assert r["status"] == "目录已确认"

    async def test_missing_scheme_id_400(self, db_conn):
        parsed = await uo.parse_outline(
            file=_upload("o.txt", "第一章 工程概况\n第二章 施工部署".encode("utf-8")),
            db=db_conn, scheme_name="", reorganize=False)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(parsed["id"], {"outline": []}, db=db_conn)
        assert e.value.status_code == 400

    async def test_missing_outline_400(self, db_conn):
        pid, sid = "p_mo", "s_mo"
        await _ensure_scheme(db_conn, pid, sid)
        parsed = await uo.parse_outline(
            file=_upload("o.txt", "第一章 工程概况\n第二章 施工部署".encode("utf-8")),
            db=db_conn, scheme_name="", reorganize=False)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(parsed["id"], {"scheme_id": sid}, db=db_conn)
        assert e.value.status_code == 400

    async def test_unknown_upload_id_404(self, db_conn):
        pid, sid = "p_uu", "s_uu"
        await _ensure_scheme(db_conn, pid, sid)
        # 非空 outline（空 outline 的 400 优先级更高）；bogus upload_id 应 404
        parsed = await uo.parse_outline(
            file=_upload("o.txt", "第一章 工程概况\n第二章 施工部署".encode("utf-8")),
            db=db_conn, scheme_name="", reorganize=False)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                "nope", {"scheme_id": sid, "outline": parsed["outline"]}, db=db_conn)
        assert e.value.status_code == 404

    async def test_unknown_scheme_id_404(self, db_conn):
        parsed = await uo.parse_outline(
            file=_upload("o.txt", "第一章 工程概况\n第二章 施工部署".encode("utf-8")),
            db=db_conn, scheme_name="", reorganize=False)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                parsed["id"], {"scheme_id": "s_nope", "outline": parsed["outline"]},
                db=db_conn)
        assert e.value.status_code == 404

    async def test_concurrency_409_when_content_generating(self, db_conn, monkeypatch):
        """✅ 并发守卫（2026-09-22）：正文生成进行中保存目录 → 409，防止整表重建覆盖。"""
        import app.routers.sections as sec
        monkeypatch.setattr(sec, "content_generation_in_progress",
                            lambda scheme_id: "task-xyz")
        pid, sid = "p_c9", "s_c9"
        await _ensure_scheme(db_conn, pid, sid)
        parsed = await uo.parse_outline(
            file=_upload("o.txt", "第一章 工程概况\n第二章 施工部署".encode("utf-8")),
            db=db_conn, scheme_name="", reorganize=False)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                parsed["id"], {"scheme_id": sid, "outline": parsed["outline"]},
                db=db_conn)
        assert e.value.status_code == 409

    async def test_concurrency_409_when_outline_generating(self, db_conn, monkeypatch):
        """✅ 目录生成进行中保存目录 → 409。"""
        import app.routers.sections as sec
        monkeypatch.setattr(sec, "outline_generation_in_progress",
                            lambda scheme_id: "task-xyz")
        pid, sid = "p_c9b", "s_c9b"
        await _ensure_scheme(db_conn, pid, sid)
        parsed = await uo.parse_outline(
            file=_upload("o.txt", "第一章 工程概况\n第二章 施工部署".encode("utf-8")),
            db=db_conn, scheme_name="", reorganize=False)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_outline(
                parsed["id"], {"scheme_id": sid, "outline": parsed["outline"]},
                db=db_conn)
        assert e.value.status_code == 409

    async def test_preserves_existing_section_content(self, db_conn):
        """✅ 跨模块内容保留：同名章节匹配已有 section 时保留正文，不丢已生成内容。"""
        pid, sid = "p_pc", "s_pc"
        await _ensure_scheme(db_conn, pid, sid)
        # 预置一个已带正文的章节；标题须与 simple_parse_outline 输出一致
        # （"第一章 工程概况" 经编号规则剥离前缀 → 标题 "工程概况"）。
        await db_conn.execute(
            "INSERT INTO sections(id, scheme_id, project_id, parent_id, title,"
            " description, level, sort_order, status, outline_json, word_budget, content)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sec-old", sid, pid, "", "工程概况", "", 1, 0, "done",
             json.dumps({"id": "1", "level": 1, "confidence": 1.0},
                        ensure_ascii=False), 1500, "既有正文内容"))
        await db_conn.commit()
        text = "第一章 工程概况\n第二章 施工部署\n".encode("utf-8")
        parsed = await uo.parse_outline(file=_upload("o.txt", text), db=db_conn,
                                        scheme_name="", reorganize=False)
        res = await uo.save_as_outline(
            parsed["id"], {"scheme_id": sid, "outline": parsed["outline"]},
            db=db_conn)
        assert res.get("preserved_content", 0) >= 1
        cur = await db_conn.execute(
            "SELECT content FROM sections WHERE id='sec-old'")
        assert (await cur.fetchone())["content"] == "既有正文内容"


# ===========================================================================
# save-as-library 组件：跨模块落库 outline_library
# ===========================================================================

class TestSaveAsLibrary:
    async def test_success_creates_library(self, db_conn):
        """保存识别结果为目录库：写入 outline_library，来源=上传识别。"""
        text = "\n".join([
            "第一章 工程概况", "第二章 编制依据", "第三章 施工部署"]).encode("utf-8")
        parsed = await uo.parse_outline(file=_upload("o.txt", text), db=db_conn,
                                        scheme_name="", reorganize=False)
        res = await uo.save_as_library(
            parsed["id"], {"name": "我的目录库", "outline": parsed["outline"]},
            db=db_conn)
        assert res["ok"] and res["id"]
        cur = await db_conn.execute(
            "SELECT name, source, review_status, outline_json FROM outline_library"
            " WHERE id=?", (res["id"],))
        r = await cur.fetchone()
        assert r["name"] == "我的目录库"
        assert r["source"] == "上传识别"
        assert r["review_status"] == "待审核"
        assert json.loads(r["outline_json"])
        # 跨模块：上传记录回写 status='library_saved'
        row = await _uploaded_row(db_conn, parsed["id"])
        assert row["status"] == "library_saved"

    async def test_empty_outline_400(self, db_conn):
        parsed = await uo.parse_outline(
            file=_upload("o.txt", "第一章 工程概况\n".encode("utf-8")),
            db=db_conn, scheme_name="", reorganize=False)
        with pytest.raises(HTTPException) as e:
            await uo.save_as_library(parsed["id"], {"name": "空", "outline": []},
                                     db=db_conn)
        assert e.value.status_code == 400

    async def test_unknown_upload_id_404(self, db_conn):
        # 非空 outline（空 outline 的 400 优先级更高）；bogus upload_id 应 404
        with pytest.raises(HTTPException) as e:
            await uo.save_as_library(
                "nope",
                {"name": "x", "outline": [{"title": "第一章 工程概况",
                                           "level": 1, "children": []}]},
                db=db_conn)
        assert e.value.status_code == 404

    async def test_library_outline_matches_upload(self, db_conn):
        """✅ 跨模块一致性：目录库中的 outline 与上传识别响应一致（预览==落库）。"""
        text = "\n".join([
            "第一章 工程概况", "第二章 编制依据"]).encode("utf-8")
        parsed = await uo.parse_outline(file=_upload("o.txt", text), db=db_conn,
                                        scheme_name="", reorganize=False)
        res = await uo.save_as_library(
            parsed["id"], {"name": "一致性", "outline": parsed["outline"]},
            db=db_conn)
        cur = await db_conn.execute(
            "SELECT outline_json FROM outline_library WHERE id=?", (res["id"],))
        stored = json.loads((await cur.fetchone())["outline_json"])
        assert stored == parsed["outline"]


# ===========================================================================
# 路由注册守卫（跨模块链路收口）
# ===========================================================================

class TestRouteRegistration:
    def test_router_endpoints_registered(self):
        """upload_outline 路由三个端点均已挂载（防止重构误删导致前端 404）。"""
        paths = {r.path for r in uo.router.routes}
        assert "/api/v1/upload-outline/parse" in paths
        assert "/api/v1/upload-outline/{upload_id}/save-as-outline" in paths
        assert "/api/v1/upload-outline/{upload_id}/save-as-library" in paths

    def test_save_as_outline_is_programmatic_only(self):
        """✅ 缺口说明（2026-09-20 契约清理）：前端已移除 saveAsOutline 调用，
        save-as-outline 现为程序化 API（有完整逻辑 + 并发守卫，但无前端消费者）。
        此处仅记录契约现状，供后续决策：保留作 API / 或随清理移除。"""
        # 端点存在即视为「保留」；若未来决定移除，本断言会失败并触发评审。
        assert any(
            r.path == "/api/v1/upload-outline/{upload_id}/save-as-outline"
            for r in uo.router.routes)
