"""上传目录识别 → 整理为标准结构（reorganize 参数接线）回归测试

背景（BUG）：前端 `uploadOutlineApi.parse(file, {scheme_name, reorganize})` 一直
在传这两个查询参数，但后端 `/upload-outline/parse` 从未声明它们 —— FastAPI 对未知
query 参数静默忽略，于是「整理为标准结构」以及 services/outline_reorganize.py
这套实现（含 4 个单测）从未被任何路由调用。本轮接线后按参数生效。
"""
import inspect
import io
import json

import pytest
from starlette.datastructures import UploadFile

from app.routers import upload_outline as uo

#: 6 个顶层章节（用「第X章」编号 ⇒ 规则法解析为同级顶层节点；
#: 节点数 >= 5 ⇒ 视为有效识别，不会触发 AI 兜底调用）
_SAMPLE = "\n".join([
    "第一章 工程简介",
    "第二章 编制说明",
    "第三章 施工总体部署",
    "第四章 主要施工方法",
    "第五章 安全保障",
    "第六章 BIM 应用管理",
])


def _upload(name: str = "outline.txt", text: str = _SAMPLE) -> UploadFile:
    return UploadFile(file=io.BytesIO(text.encode("utf-8")), filename=name)


async def _parse(db, **kwargs):
    """直接调用路由函数：必须显式传全部参数。

    ⚠️ 直接调用时 FastAPI 的 `Query(...)` 默认值不会被解析（拿到的仍是 Query
    对象，恒为真），因此默认值单独用 `test_route_defaults_are_backward_compatible`
    校验签名，不在此处依赖。
    """
    kwargs.setdefault("scheme_name", "")
    kwargs.setdefault("reorganize", False)
    return await uo.parse_outline(file=_upload(), db=db, **kwargs)


class TestReorganizeParam:
    def test_route_defaults_are_backward_compatible(self):
        """默认不得改变既有行为：reorganize 默认 False（未显式要求即原样镜像）。"""
        sig = inspect.signature(uo.parse_outline)
        assert sig.parameters["reorganize"].default.default is False
        assert sig.parameters["scheme_name"].default.default == ""

    async def test_false_keeps_mirror_behaviour(self, db_conn):
        """reorganize=false：识别结果 = 上传文档结构的镜像（既有行为不变）。"""
        res = await _parse(db_conn, scheme_name="深基坑工程专项施工方案")
        assert "reorganized" not in res
        assert "reorganize_report" not in res
        titles = [n.get("title", "") for n in res["outline"]]
        assert any("BIM" in t for t in titles), "镜像结果应保留原文标题"

    async def test_true_rebuilds_to_standard_framework(self, db_conn):
        """reorganize=true：归位到标准章节骨架 + 返回归位报告。"""
        res = await _parse(db_conn, scheme_name="深基坑工程专项施工方案",
                           reorganize=True)
        assert res.get("reorganized") is True
        report = res.get("reorganize_report") or {}
        assert report.get("matched_chapters", 0) >= 3, report
        assert report.get("template"), "应给出命中的标准模板 key"
        titles = [n.get("title", "") for n in res["outline"]]
        for must in ("工程概况", "编制依据", "施工安全保证措施"):
            assert any(must in t for t in titles), f"标准章节缺失: {must}"

    async def test_unmatched_content_is_not_lost(self, db_conn):
        """✅ 内容不丢失：无法归位的真实章节（BIM）以标题形式并入描述。"""
        res = await _parse(db_conn, scheme_name="深基坑工程专项施工方案",
                           reorganize=True)
        flat = json.dumps(res["outline"], ensure_ascii=False)
        assert "BIM" in flat, "未归位的用户章节标题必须保留（并入描述或补充章节）"

    async def test_reorganize_result_is_normalized_to_three_levels(self, db_conn):
        """整理结果同样经过三级裁剪 + 编号重排（预览所见 == 落库所得）。"""
        res = await _parse(db_conn, scheme_name="深基坑工程专项施工方案",
                           reorganize=True)

        def depth(nodes, d=1):
            if not nodes:
                return d - 1
            return max(depth(n.get("children") or [], d + 1) for n in nodes)

        assert depth(res["outline"]) <= 3
        assert res["outline"][0]["id"] == "1"       # 编号已重排

    async def test_upload_record_stores_normalized_payload(self, db_conn):
        """上传记录里的 parsed_json 与响应一致（避免"预览一套、落库一套"）。"""
        res = await _parse(db_conn, scheme_name="深基坑工程专项施工方案",
                           reorganize=True)
        cur = await db_conn.execute(
            "SELECT parsed_json, confidence FROM uploaded_outlines WHERE id=?",
            (res["id"],))
        row = await cur.fetchone()
        assert row is not None
        stored = json.loads(row[0])["outline"]
        assert stored == res["outline"]
        assert 0.0 <= row[1] <= 1.0
