"""导出后端高风险回归基线。

覆盖审查提出的反例：前端 PNG/图像配置进入指纹、图表失败不缓存、
跨章节图表不得借用其他章节代码、生图并发必须服从配置，以及 DOCX
原子临时文件。涉及当前生产实现尚不满足的契约时使用 strict xfail：
既保留可运行的反例，又避免把未修复问题伪装成绿色通过。
"""

import asyncio
import json
import re
from io import BytesIO
from pathlib import Path

import app.routers.export as export_mod
import pytest
from app.config import settings as _unused_settings  # noqa: F401  # 保持测试导入路径与导出模块一致
from app.routers.export import _DEFAULT_HEADING_STYLES  # noqa: F401  # _prep 内构造 heading_styles 使用
from docx import Document
from fastapi.responses import FileResponse
from PIL import Image

_CODE = "graph TD\n  A[开始] --> B[完成]"


def _png_bytes(color):
    """生成真实 PNG，避免测试只覆盖任意字节串。"""
    buf = BytesIO()
    Image.new("RGB", (2, 2), color).save(buf, format="PNG")
    return buf.getvalue()


def _section(section_id="s1", order=0, content="正文"):
    return {
        "id": section_id,
        "parent_id": "",
        "sort_order": order,
        "level": 1,
        "title": "第一章 概述",
        "content": content,
    }


def _prep(*, sections=None, config=None, render_stats=None):
    """构造不触网的最小导出准备结果。"""
    sections = list(sections or [_section()])
    return {
        "scheme": {"id": "risk-scheme", "project_id": "p1", "name": "高风险回归"},
        "sections": sections,
        "content_audit": {},
        "global_facts": [],
        "chart_fp": [],
        "chart_lookup": {},
        "rendered_bytes": {},
        "image_bytes": {},
        "blocks_cache": {s["id"]: export_mod._parse_content_blocks(s.get("content") or "") for s in sections},
        "roots": sections,
        "children_map": {"": sections},
        "fe_codes": [],
        "render_stats": render_stats or {"fe": 0, "backend_ok": 0, "failed": 0},
        "config": config or {},
        "heading_styles": dict(_DEFAULT_HEADING_STYLES),
        "docx_options": {
            "font_name": "宋体",
            "font_size": 12,
            "page_header": "",
            "page_footer": "",
            "show_page_number": False,
            "show_title_page": False,
            "show_toc": False,
            "bidder_name": "",
            "page_break_before_chapter": True,
            "line_spacing": 1.15,
            "page_number_style": "simple",
            "toc_depth": 3,
            "cover_info": None,
            "margins": None,
            "chart_fail_placeholder": False,
            "heading_border": False,
        },
    }


async def _seed_scheme(db, scheme_id="risk-scheme", project_id="p1", name="高风险回归"):
    await db.execute(
        "INSERT INTO schemes(id, project_id, name) VALUES (?,?,?)",
        (scheme_id, project_id, name),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# 1. 指纹：前端 PNG 内容与图像配置
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="审查反例：当前指纹仅哈希 mermaid_code，忽略前端 PNG 实际字节",
)
def test_frontend_png_content_changes_content_fingerprint():
    """同一 Mermaid 代码的前端 PNG 内容变化时，缓存必须失效。"""
    red = _prep()
    blue = _prep()
    red["fe_codes"] = blue["fe_codes"] = [_CODE]
    red["fe_images"] = {export_mod._norm_code(_CODE): BytesIO(_png_bytes("red"))}
    blue["fe_images"] = {export_mod._norm_code(_CODE): BytesIO(_png_bytes("blue"))}

    red_hash = export_mod._content_fingerprint(red)[1]
    blue_hash = export_mod._content_fingerprint(blue)[1]
    assert red_hash != blue_hash, "前端 PNG 像素变化仍命中了旧 DOCX 缓存"


@pytest.mark.xfail(
    strict=True,
    reason="审查反例：当前 prepare 未把图像模型/尺寸配置纳入导出指纹",
)
def test_image_config_changes_content_fingerprint():
    """图像模型或规格变化会改变产物，必须使导出缓存失效。"""
    first = _prep()
    second = _prep()
    first["image_config"] = {"image_model": "image-a", "image_default_size": "1K"}
    second["image_config"] = {"image_model": "image-b", "image_default_size": "1K"}

    assert export_mod._content_fingerprint(first)[1] != export_mod._content_fingerprint(second)[1]


@pytest.mark.asyncio
async def test_auto_image_passes_runtime_image_config(monkeypatch):
    """导出自动生图必须把当前图像配置完整传给 provider 引擎。"""
    from app.config import settings

    captured = {}

    async def fake_generate(provider, prompt, style, size, provider_name, image_config):
        captured.update(image_config)
        return "https://img.example/runtime-config.png"

    monkeypatch.setattr(settings, "image_enabled", False)
    monkeypatch.setattr(settings, "image_api_key", "image-secret")
    monkeypatch.setattr(settings, "image_base_url", "https://image.example/v1")
    monkeypatch.setattr(settings, "image_model", "image-model-x")
    monkeypatch.setattr(settings, "image_default_size", "4K")
    monkeypatch.setattr(settings, "image_price_per_image", 0.25)
    monkeypatch.setattr("app.services.ai.image_engine.generate_illustration_image", fake_generate)

    code = json.dumps({"prompt": "基坑剖面", "title": "基坑剖面图"}, ensure_ascii=False)
    blocks = {"s1": [{"type": "ai_image", "code": code, "title": "基坑剖面图"}]}
    assert await export_mod._auto_generate_ai_image_blocks([{"id": "s1"}], blocks, {}) == 1
    assert captured == {
        "image_enabled": False,
        "image_api_key_enc": "image-secret",
        "image_base_url": "https://image.example/v1",
        "image_model": "image-model-x",
        "image_default_size": "4K",
        "image_price_per_image": 0.25,
    }



# ---------------------------------------------------------------------------
# 2. 图表失败不进入持久缓存
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chart_render_failure_is_not_cached(db_conn, tmp_path, monkeypatch):
    """渲染失败产物可返回给本次请求，但绝不能写入 export_cache。"""
    scheme_id = "failed-chart"
    section_id = "failed-chart-s1"
    await _seed_scheme(db_conn, scheme_id)
    await db_conn.execute(
        "INSERT INTO sections(id, scheme_id, parent_id, sort_order, level, title, content) "
        "VALUES (?,?,?,?,?,?,?)",
        (section_id, scheme_id, "", 0, 1, "施工流程", "[CHART_TYPE: flowchart]"),
    )
    await db_conn.execute(
        "INSERT INTO chart_predictions"
        "(id, section_id, scheme_id, chart_type, needed, purpose, priority, status, data_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("cp-failed", section_id, scheme_id, "flowchart", 1, "流程图", 1, "generated",
         json.dumps({"mermaid_code": _CODE}, ensure_ascii=False)),
    )
    await db_conn.commit()

    from app.services.ai.mermaid_renderer import _chart_cache

    monkeypatch.setattr(_chart_cache, "get_or_render", lambda *args, **kwargs: None)
    monkeypatch.setattr(export_mod, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        export_mod, "_record_export_review_trace",
        lambda *args, **kwargs: asyncio.sleep(0),
    )

    response = await export_mod.export_docx(
        scheme_id, {"config": {"allow_pil_fallback": False}}, db=db_conn
    )

    assert isinstance(response, FileResponse)
    assert response.headers["x-cache-status"] == "degraded"
    assert json.loads(response.headers["x-chart-render-stats"])["failed"] == 1
    cur = await db_conn.execute(
        "SELECT COUNT(*) FROM export_cache WHERE scheme_id=?", (scheme_id,)
    )
    assert (await cur.fetchone())[0] == 0, "图表失败产物被写入缓存，后续将永久命中残缺文档"


# ---------------------------------------------------------------------------
# 3. 跨章节图表隔离
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="审查反例：当前倒排索引按 chart_type 全局取首个 code，会跨章节借用",
)
async def test_missing_chart_does_not_borrow_other_chapter_code(
    db_conn, tmp_path, monkeypatch
):
    """第一章缺失登记时，不得静默复用第二章同类型图表代码。"""
    scheme_id = "cross-chart"
    await _seed_scheme(db_conn, scheme_id)
    for sid, order in (("chapter-a", 0), ("chapter-b", 1)):
        await db_conn.execute(
            "INSERT INTO sections(id, scheme_id, parent_id, sort_order, level, title, content) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, scheme_id, "", order, 1, f"第{order + 1}章 流程", "[CHART_TYPE: flowchart]"),
        )
    await db_conn.execute(
        "INSERT INTO chart_predictions"
        "(id, section_id, scheme_id, chart_type, needed, purpose, priority, status, data_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("cp-b", "chapter-b", scheme_id, "flowchart", 1, "流程图", 1, "generated",
         json.dumps({"mermaid_code": _CODE}, ensure_ascii=False)),
    )
    await db_conn.commit()

    from app.services.ai.mermaid_renderer import _chart_cache

    png = _png_bytes("green")
    monkeypatch.setattr(
        _chart_cache, "get_or_render", lambda *args, **kwargs: BytesIO(png)
    )
    prep = await export_mod._prepare_export(scheme_id, {"config": {}}, db_conn)
    output = tmp_path / "cross-chapter.docx"
    export_mod._build_docx_sync(*export_mod._build_docx_task(str(output), prep))

    assert len(Document(output).inline_shapes) == 1, "第一章静默借用了第二章的流程图"


# ---------------------------------------------------------------------------
# 4. AI 生图并发配置
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_image_respects_configured_concurrency(monkeypatch):
    """image_max_concurrency=1 时，多个唯一占位码必须串行生成。"""
    from app.config import settings

    monkeypatch.setattr(settings, "image_max_concurrency", 1)
    active = 0
    peak = 0

    async def fake_generate(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return "https://img.example/generated.png"
        finally:
            active -= 1

    monkeypatch.setattr(
        "app.services.ai.image_engine.generate_illustration_image", fake_generate
    )
    sections = []
    blocks = {}
    for i in range(4):
        sid = f"image-section-{i}"
        sections.append({"id": sid})
        code = json.dumps({"prompt": f"配图 {i}"}, ensure_ascii=False)
        blocks[sid] = [{"type": "ai_image", "code": code, "title": f"配图 {i}"}]

    assert await export_mod._auto_generate_ai_image_blocks(sections, blocks, {}) == 4
    # 并发上限必须真正生效：峰值同时生成数不得超过 image_max_concurrency=1
    assert peak == 1, f"配置并发为 1，实际峰值 {peak}"


# ---------------------------------------------------------------------------
# 5. 原子临时文件
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_docx_builds_unique_temp_then_atomically_replaces_target(
    db_conn, tmp_path, monkeypatch
):
    """构建期间目标文件不可见，替换成功后不残留 tmp 文件。"""
    scheme_id = "atomic-ok"
    await _seed_scheme(db_conn, scheme_id)
    monkeypatch.setattr(export_mod, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        export_mod, "_record_export_review_trace",
        lambda *args, **kwargs: asyncio.sleep(0),
    )

    name_pattern = re.compile(
        r"^(?P<target>.+_[0-9a-f]{8}_[0-9a-f]{8})\.[0-9a-f]{32}\.tmp\.docx$"
    )
    observed = {}

    def fake_build(out_path, *args, **kwargs):
        temp = Path(out_path)
        match = name_pattern.match(temp.name)
        assert match, f"临时文件名不符合唯一命名约束: {temp.name}"
        target = temp.with_name(match.group("target") + ".docx")
        observed["target"] = target
        observed["temp"] = temp
        assert not target.exists(), "构建尚未完成，目标文件提前可见"
        temp.write_bytes(b"complete-docx")
        return {}

    monkeypatch.setattr(export_mod, "_build_docx_sync", fake_build)
    response = await export_mod.export_docx(scheme_id, {"config": {}}, db=db_conn)

    assert Path(response.path) == observed["target"]
    assert observed["target"].read_bytes() == b"complete-docx"
    assert not observed["temp"].exists()
    assert list(tmp_path.glob("*.tmp.docx")) == []


@pytest.mark.asyncio
async def test_docx_build_failure_removes_partial_temp_file(
    db_conn, tmp_path, monkeypatch
):
    """DOCX 构建抛错时必须清理部分写入的临时文件，且不得生成目标文件。"""
    scheme_id = "atomic-error"
    await _seed_scheme(db_conn, scheme_id)
    monkeypatch.setattr(export_mod, "EXPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        export_mod, "_record_export_review_trace",
        lambda *args, **kwargs: asyncio.sleep(0),
    )

    def fail_build(out_path, *args, **kwargs):
        Path(out_path).write_bytes(b"partial-docx")
        raise RuntimeError("模拟 DOCX 构建失败")

    monkeypatch.setattr(export_mod, "_build_docx_sync", fail_build)
    with pytest.raises(RuntimeError, match="模拟 DOCX 构建失败"):
        await export_mod.export_docx(scheme_id, {"config": {}}, db=db_conn)

    assert list(tmp_path.glob("*.tmp.docx")) == []
    assert list(tmp_path.glob("*.docx")) == []
