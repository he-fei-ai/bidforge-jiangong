"""可复核底稿导出路由（R179 Phase D，2026-11-06）。

与既有 ``/export/docx`` 链路**完全独立**：
  · 不读写 ``export_cache``、不递增 ``export_round``、不写审计旁车；
  · 正文复用 ``_prepare_export`` + ``_build_docx_sync``（同一套渲染，不另写格式引擎）；
  · 附属产物由 ``app.services.dossier`` 追加到同一份 docx 末尾。

开关 ``dossier_enabled`` 默认关闭 → handler 首行 404，既有导出行为零差异。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.config import EXPORTS_DIR, settings
from app.db import get_db
from app.routers import export as export_router
from app.services import dossier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["dossier"])

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _require_enabled() -> None:
    if not settings.dossier_enabled:
        raise HTTPException(404, "Not Found")


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", name or "").strip()
    return cleaned or fallback


@router.post("/schemes/{scheme_id}/dossier")
async def export_dossier(scheme_id: str, db=Depends(get_db)):
    """生成可复核底稿：既有渲染产出主文档 + 三类附属产物。"""
    _require_enabled()
    # ✅ R195：生成中互斥守卫 —— 与 /export/docx 同口径（复用 export.py 的
    # 判据与文案），防止生成中导出缺章底稿。
    export_router._ensure_no_generation_on_export(scheme_id, "dossier")
    # ✅ R195 收口（P1）：底稿链此前完全旁路 DLV 阻断闸门与编号一致性守卫 ——
    #    同一方案 DOCX 导出被 DLV-13 high 阻断、底稿却照常产出，两条交付链
    #    承诺不一致。底稿无 override 入口，检出 high 即阻断。
    _blockers, _gate = await export_router._export_high_issue_gate(scheme_id, db, {})
    if _blockers and not _gate["override"]:
        return export_router._export_blocked_response(_blockers)
    await export_router._guard_numbering_consistency(db, scheme_id)

    prep = await export_router._prepare_export(scheme_id, {"config": {}}, db, defer_audit=True)
    await export_router._prepare_export_render(prep)

    # ✅ R195 收口（P1）：产物名必须以 `{scheme_id}_` 开头 —— _gc_orphan_exports
    #   只扫该前缀；旧名 `dossier_{scheme_id}_*` 不在扫描范围，BackgroundTask
    #   兜底在断连/进程崩溃时不执行 → 永久孤儿且 GC 永远收不到。
    out = EXPORTS_DIR / f"{scheme_id}_dossier_{uuid.uuid4().hex}.docx"
    try:
        await asyncio.to_thread(export_router._build_docx_sync, *export_router._build_docx_task(out, prep))
        data = await dossier.collect_all(db, scheme_id, prep.get("scheme"))
        await asyncio.to_thread(dossier.append_to_docx, out, data)
    except Exception:
        out.unlink(missing_ok=True)  # R177-DELETE-EXEMPT: 异常时仅清理本次临时产物
        raise

    filename = f"{_safe_filename(str((prep.get('scheme') or {}).get('name') or ''), scheme_id)}_可复核底稿.docx"
    logger.info("可复核底稿导出完成（scheme=%s, defects=%d）", scheme_id[:8], len(data["defects"]["items"]))
    return FileResponse(
        str(out),
        filename=filename,
        media_type=_DOCX_MIME,
        background=BackgroundTask(out.unlink, missing_ok=True),
    )
