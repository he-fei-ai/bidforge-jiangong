"""AI 配图人工入口门控回归测试（P4，2026-09-23）。

v17 产品约束：图表全自动生成、无人工生图入口。/charts/generate-ai-image
为兼容保留端点，默认（ai_image_manual_enabled=False）拒绝人工/脚本触发，
返回 409 说明已全自动；仅显式开启 AI_IMAGE_MANUAL_ENABLED 才恢复手动链路。
"""
import uuid

import pytest
from fastapi import HTTPException

import app.config as _cfg
import app.db as _appdb
from app.db import close_db, get_conn, init_db
import app.routers.charts as charts


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "ai_image_gate.sqlite"
    await init_db()
    db = await get_conn()
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.commit()
    try:
        yield db, pid
    finally:
        await close_db()


async def test_manual_disabled_returns_409(ctx, monkeypatch):
    """默认关闭手动入口：非自动令牌触发应被 409 拒绝。"""
    db, _pid = ctx
    monkeypatch.setattr(_cfg.settings, "ai_image_manual_enabled", False)
    with pytest.raises(HTTPException) as exc:
        await charts.generate_ai_image(body={"section_id": "s1"}, db=db)
    assert exc.value.status_code == 409


async def test_manual_enabled_passes_gate(ctx, monkeypatch):
    """显式开启后门控放行（继续走后续查章节逻辑，章节不存在则 404）。"""
    db, _pid = ctx
    monkeypatch.setattr(_cfg.settings, "ai_image_manual_enabled", True)
    with pytest.raises(HTTPException) as exc:
        await charts.generate_ai_image(body={"section_id": "s1"}, db=db)
    # 门控已放行，进入章节查询 → 章节不存在返回 404（而非 409）
    assert exc.value.status_code == 404


def test_frontend_manual_wrapper_removed_and_endpoint_retained():
    """AGENTS.md 显式迁移决策（2026-09-23）：前端包装删除、后端端点保留。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    api_ts = root / "frontend" / "src" / "api" / "index.ts"
    assert api_ts.exists(), "前端 api/index.ts 不存在，请人工确认仓库布局"
    code_lines = [ln for ln in api_ts.read_text(encoding="utf-8").splitlines()
                  if not ln.strip().startswith("//")]
    assert "generateAiImage" not in "\n".join(code_lines)

    charts_src = (root / "backend" / "app" / "routers" / "charts.py").read_text(
        encoding="utf-8")
    assert '@router.post("/generate-ai-image")' in charts_src
    assert "ai_image_manual_enabled" in charts_src
