"""图表路由：图表预测 + Mermaid 代码生成 + 图表渲染"""

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db

# ✅ BUG 修复（2026-09-16 · ruff F821）：`fix-mermaid` 里在 `if row:` 分支**内部**
#    才 import `_rewrite_code_block`，而该名字在第 380 行（更早的
#    「同步重写章节正文内联代码块」分支）就被引用 —— Python 视其为函数局部名，
#    先引用后赋值 → `UnboundLocalError`，被外层 except 吞掉：
#    表现为「预览已修复、导出仍是坏图」的旧问题**始终存在**（该修复从未生效）。
#    这里提到模块顶部，两处引用都能拿到。
from app.routers._chart_pipeline import _rewrite_code_block  # noqa: E402
from app.routers.review import (
    reset_review_on_content_change,  # ✅ R215：图表修复/AI 配图改正文 → 审核退回待审核（G9 收口）
)
from app.services.ai.prompts._registry import render
from app.services.ai.provider_factory import chat_with_fallback
from app.services.chart_payload import build_chart_envelope, extract_chart_payload

# 图表类型标签统一共享自 chart_validators（别名保持原引用点不变）
from app.services.chart_validators import CHART_TYPE_LABELS as _CHART_LABELS
from app.services.chart_validators import infer_chart_type_from_payload
from app.services.content_blocks import chart_type_tags
from app.services.prompt_material_guard import clip_text_notice

logger = logging.getLogger("charts")
router = APIRouter(prefix="/api/v1/charts", tags=["charts"])

# ✅ R179（2026-11-05）：生成链路写入 chart_predictions.purpose/title 的是
#    流程说明而非图名（见 _chart_pipeline 内联登记），清单展示时按占位值处理。
_PLACEHOLDER_CHART_PURPOSES = {"正文同步生成", "（正文同步生成）", "生成图表", "图表生成"}


def _display_chart_title(purpose: str, chart_type: str, section_title: str) -> str:
    """图表清单展示标题：真实图名优先，占位流程说明回退到「章节 · 类型」。"""
    value = str(purpose or "").strip()
    if value and value not in _PLACEHOLDER_CHART_PURPOSES:
        return value
    label = _CHART_LABELS.get(chart_type, chart_type)
    sec = str(section_title or "").strip()
    return f"{sec} · {label}" if sec else label


async def _guard_chart_target(db, section_id: str, action: str, prediction_id: str = "") -> None:
    """图表正文写点的生成竞态守卫（✅ R98 · R90 遗留⑦收口）。

    本路由前缀不带 scheme_id，先按 section_id（缺省按 prediction_id）反查
    方案，再委派 routers.sections.ensure_no_generation_write（判据单一事实源，
    与审核修复链 / shrink 共用同一守卫）。反查失败/查无 → 不拦：让后续
    既有的 404/兜底链按原语义处理，守卫绝不比被保护路径更"知道"章节存在。
    """
    scheme_id = ""
    try:
        if section_id:
            cur = await db.execute("SELECT scheme_id FROM sections WHERE id=?", (section_id,))
            row = await cur.fetchone() if cur is not None else None
        elif prediction_id:
            cur = await db.execute("SELECT scheme_id FROM chart_predictions WHERE id=?", (prediction_id,))
            row = await cur.fetchone() if cur is not None else None
        else:
            row = None
        if row is not None:
            scheme_id = str(row["scheme_id"] or "")
    except Exception as e:  # noqa: BLE001 - 反查异常不阻断（fail-open 到原链路）
        logger.debug("图表守卫：%s 反查 scheme_id 失败（跳过拦截）: %s", (section_id or prediction_id)[:8], e)
        return
    if scheme_id:
        from app.routers.sections import ensure_no_generation_write

        ensure_no_generation_write(scheme_id, action)


def _infer_payload_type(code: str) -> str:
    """结构化图表载荷的**类型推断**（唯一判据，与登记侧/导出侧共用同一函数）。

    仅当 ``code`` 是 JSON 对象时才有返回值；Mermaid 语法载荷返回空串
    （语法载荷的类型由 ``chart_type`` 参数决定，不在此推断）。
    显式写了 ``type`` 且合法时原样返回 —— 调用方的显式声明优先。
    """
    text = (code or "").strip()
    if not text.startswith(("{", "[")):
        return ""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    return infer_chart_type_from_payload(obj)


@router.post("/render")
async def render_chart(body: dict):
    """将 Mermaid 代码/JSON 数据渲染为 PNG 图片"""
    chart_type = body.get("chart_type", "flowchart")
    mermaid_code = body.get("code", "")
    # ✅ P0-1 修复：默认 False — 让 HTTP Service（Mermaid 原生引擎）先尝试。
    # 原默认 True 永远跳过最优渲染路径，直接进 PIL。前端若显式传 skip_http=true
    # 时才走 PIL（在 HTTP Service 不可用的本地开发场景省重试超时）。
    skip_http = body.get("skip_http", False)

    if not mermaid_code:
        raise HTTPException(400, "缺少图表代码")

    # ✅ BUG 修复（2026-09-27 · 预览与导出类型错配）：结构化载荷的 chart_type
    #    此前**完全信任调用方**。而前端 MarkdownRenderer 对 chart-json 块取
    #    `obj.type || "labor"` —— 载荷省略 type 时就发来一个**凭空捏造的
    #    chart_type="labor"**，后端拿它去选渲染器：甘特图/架构图载荷被送进
    #    labor 渲染器 → 必然渲染失败 → 预览显示"渲染引擎暂不可用"，
    #    而**导出 DOCX 却是对的**（导出侧走 infer_chart_type_from_payload 推断）。
    #    即"预览坏、导出好"的错配，且错误提示把类型问题误报成引擎不可用。
    #    现与登记侧/导出侧**同用 infer_chart_type_from_payload**：
    #    仅当载荷确实是结构化 JSON、且调用方给的类型不在可渲染白名单时才纠正；
    #    显式合法类型与 Mermaid 语法载荷的行为完全不变（向后兼容）。
    _inferred = _infer_payload_type(mermaid_code)
    if _inferred and _inferred != chart_type:
        logger.info("/charts/render：chart_type=%r 与载荷结构不符，按结构纠正为 %r", chart_type, _inferred)
        chart_type = _inferred

    # ✅ R102（2026-10-18 · 日志审计收口）：结构化载荷**非法 JSON**（AI 生成被
    #    max_tokens 截断的典型形态）提前拒绝。生产实证（backend_err.log
    #    2026-10-01 14:22:21 trace=646c14650b7d）：342 字符 gantt 载荷截断 →
    #    `_infer_payload_type` 的 json.loads 失败返回空串 → 类型纠正确失效 →
    #    前端默认 chart_type=flowchart 拿到 gantt 数据 → PIL v2 必然失败 →
    #    500 且错误文案误导（"请检查 Mermaid 代码语法"——载荷根本不是 Mermaid）。
    #    截断载荷对任何渲染器都是确定必败，早退 400 比 500 更可行动：
    #    前端可明确提示"图表数据损坏，请重新生成"，而不是引导用户去改语法。
    if mermaid_code.lstrip().startswith(("{", "[")):
        try:
            json.loads(mermaid_code)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "/charts/render：结构化图表载荷不是合法 JSON（疑似生成截断，" "长度=%d），拒绝渲染", len(mermaid_code)
            )
            raise HTTPException(
                400, "图表数据载荷不是合法 JSON（疑似内容生成时被截断而损坏），" "无法渲染；请重新生成该图表"
            )

    try:
        from app.services.ai.mermaid_renderer import _chart_cache

        img_bytes = await asyncio.to_thread(_chart_cache.get_or_render, mermaid_code, chart_type, "png", skip_http)
        if img_bytes and len(img_bytes.getvalue()) > 100:
            from fastapi.responses import Response

            return Response(content=img_bytes.getvalue(), media_type="image/png")
        else:
            # ✅ P0-3 修复：返回更具体的错误信息，不再只给泛化的"图表渲染失败"
            # ✅ 再修复：旧实现探测 `hasattr(client, "is_available")`，而客户端当时
            #    并没有该方法 → http_ok 恒为 False，提示永远声称"HTTP Service 不可用"，
            #    把"图表代码语法错误"误导成"渲染服务没部署"。
            from app.services.ai.mermaid_service import get_mermaid_service_client

            client = get_mermaid_service_client(timeout=5)
            avail = client.is_available() if hasattr(client, "is_available") else None
            avail_text = {True: "可用", False: "不可用", None: "尚未探测"}.get(avail, "未知")
            # ✅ R102：JSON 数据载荷的失败原因与 Mermaid 语法载荷完全不同
            #    （前者是数据/结构问题，后者才是语法问题），错误文案必须分流，
            #    否则把"数据坏了"误导成"请检查 Mermaid 语法"（trace=646c14650b7d 实证）。
            if mermaid_code.lstrip().startswith("{"):
                _advice = "请检查图表数据结构是否完整（字段缺失/嵌套错误），或重新生成该图表。"
            else:
                _advice = "请检查 Mermaid 代码语法，或把分号分隔改为换行格式后重试。"
            raise HTTPException(
                500,
                f"图表渲染失败（chart_type={chart_type}）。"
                f"渲染后端最近一次探测结果: {avail_text}（skip_http={skip_http}）。"
                f"{_advice}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("图表渲染异常 chart_type=%s", chart_type)
        raise HTTPException(500, f"图表渲染异常: {e}")


@router.get("/types")
async def list_chart_types():
    """列出所有支持的图表类型"""
    return {
        "types": [
            {"key": "flowchart", "label": "流程图", "description": "施工工序、管理流程"},
            {"key": "gantt", "label": "甘特图", "description": "施工进度计划、工期安排"},
            {"key": "architecture", "label": "组织架构图", "description": "项目组织架构、管理体系"},
            {"key": "labor", "label": "劳动力配置图", "description": "劳动力配置计划"},
            {"key": "comparison", "label": "对比图", "description": "方案对比、饼图、柱状图"},
            {"key": "layout", "label": "总平面布置图", "description": "施工总平面布置"},
            {"key": "timeline", "label": "时间轴", "description": "关键里程碑时间线"},
        ]
    }


# ---------- 图表清单 / 定位 / 删除 / 结构化数据 / AI 修复 ----------


async def _load_section(db, section_id: str) -> dict | None:
    cur = await db.execute("SELECT * FROM sections WHERE id=?", (section_id,))
    # ✅ R13 守卫（2026-10-04）：全局单连接 + aiosqlite 下 execute() 可能返回 None，
    #    直接 .fetchone() 会 AttributeError → 500。读路径按仓库口径 **503**（见
    #    routers/prompts.py::_fetch_content_row 与 doc_pipeline 的同款处理），
    #    绝不返回「章节不存在」——那会把数据库瞬时故障伪装成业务空数据。
    if cur is None:
        logger.warning("读取章节失败：db.execute 返回 None（R13，section=%s）", section_id[:8])
        raise HTTPException(503, "数据库连接暂时不可用，请稍后重试")
    row = await cur.fetchone()
    return dict(row) if row else None


@router.get("/list/{scheme_id}")
async def list_charts(scheme_id: str, db=Depends(get_db)):
    """图表清单：chart_predictions 表为主，sections 表 *_json 列为辅合并

    ``placed`` 语义：该章节正文中**是否存在本类型图表的落点** ——
    现由正文内联代码块（```mermaid / ```chart-json）承载，兼容历史
    ``[CHART_TYPE: x]`` 标记。旧实现只认标记，导致"正文同步内联"图表
    恒被判为未落位（清单口径与事实不符）。
    """
    # ✅ P1-2 性能优化：合并两次全表读为一次（含 *_json 历史列，供兼容扫描复用）
    cur = await db.execute(
        "SELECT id, title, content, flowchart_json, gantt_json, architecture_json,"
        " labor_json, comparison_json, layout_json, timeline_json"
        " FROM sections WHERE scheme_id=?",
        (scheme_id,),
    )
    # ✅ R13 守卫（2026-10-04）：charts.py 此前**零个**判空，清单端点在 DB 异常态直接 500。
    #    走 503 而非返回空清单 —— 空清单会让用户以为「本方案没有图表」（同 R39 ① 教训：
    #    把数据库故障伪装成业务空数据，用户看到「尚未扫描/没有图表」却无从排查）。
    if cur is None:
        logger.warning("图表清单查询失败：db.execute 返回 None（R13，scheme=%s）", scheme_id[:8])
        raise HTTPException(503, "数据库连接暂时不可用，请稍后重试")
    _section_rows = [dict(r) for r in await cur.fetchall()]
    sec_map = {r["id"]: r for r in _section_rows}

    # 逐章节缓存"正文内联图表类型集合"（同章节多行共用一次解析，避免重复扫描）
    from app.routers._chart_pipeline import extract_inline_charts

    _inline_types_cache: dict[str, set[str]] = {}

    def _is_placed(sid: str, content: str, ct: str) -> bool:
        """正文是否已含该类型图表的落点（内联代码块 或 历史标记）"""
        # ✅ R99（2026-10-17）：历史标记判据收敛到 content_blocks.chart_type_tags
        #    **单一事实源**。旧写法用半角方括号的字面串内联匹配，只认半角形态，
        #    与块解析侧的正则各写一份 → 全角/异常写法下「清单显示已落位、导出却按
        #    普通段落处理（机器标记原文还会印进成稿）」，两侧口径分叉。
        if ct in chart_type_tags(content):
            return True
        if sid not in _inline_types_cache:
            try:
                _inline_types_cache[sid] = {c for c, _ in extract_inline_charts(content or "")}
            except Exception:
                _inline_types_cache[sid] = set()
        return ct in _inline_types_cache[sid]

    items: list[dict] = []

    # 1) chart_predictions 表
    cur = await db.execute(
        "SELECT * FROM chart_predictions WHERE scheme_id=? AND needed=1 ORDER BY priority DESC", (scheme_id,)
    )
    if cur is None:
        logger.warning("图表清单查询失败：db.execute 返回 None（R13，scheme=%s）", scheme_id[:8])
        raise HTTPException(503, "数据库连接暂时不可用，请稍后重试")
    seen_pairs: set[tuple[str, str]] = set()
    for r in await cur.fetchall():
        p = dict(r)
        sid = p.get("section_id", "")
        ct = p.get("chart_type", "")
        seen_pairs.add((sid, ct))
        sec = sec_map.get(sid, {})
        code = ""
        if p.get("data_json"):
            # ✅ BUG 修复：统一走 chart_payload 的**唯一解析器**，不再自搓一套。
            #    旧实现 `dj.get("mermaid_code") or json.dumps(dj.get("data") or dj)`
            #    需要 data_json 是 JSON 对象，遇到**历史裸 Mermaid 载荷**
            #    （data_json 直接是 "graph TD..." 文本）时 json.loads 抛错被吞 →
            #    code 恒为空串 → 图表清单"点开无内容、无法预览/定位/修复"。
            #    与 chart_payload 文档确立的"读取侧唯一解析器"契约相悖。
            code = extract_chart_payload(p["data_json"])
        content = sec.get("content") or ""
        items.append(
            {
                "id": p["id"],
                "section_id": sid,
                "section_title": sec.get("title", ""),
                "chart_type": ct,
                "chart_label": _CHART_LABELS.get(ct, ct),
                # ✅ R179（2026-11-05）：内联图表的 purpose/title 落库值是流程
                #    说明「正文同步生成」（占位标题，不是图名）。清单此前直接
                #    展示该串，用户看到的"图表标题"是系统流程术语，无法据此
                #    定位图表。改为：真实 purpose 优先；占位值回退到
                #    「章节标题 · 图表类型」，只改展示、不改库、不改导出图题。
                "title": _display_chart_title(p.get("purpose"), ct, sec.get("title", "")),
                "priority": p.get("priority", 3),
                "status": p.get("status", "generated"),
                "placed": _is_placed(sid, content, ct),
                "code": code,
            }
        )

    # 2) sections 表 *_json 列（兼容历史数据）
    field_map = {
        "flowchart": "flowchart_json",
        "gantt": "gantt_json",
        "architecture": "architecture_json",
        "labor": "labor_json",
        "comparison": "comparison_json",
        "layout": "layout_json",
        "timeline": "timeline_json",
    }
    for s in _section_rows:
        for ct, field in field_map.items():
            data = s.get(field)
            if not data or not str(data).strip():
                continue
            if (s["id"], ct) in seen_pairs:
                continue
            content = s.get("content") or ""
            # ✅ 与 chart_predictions 分支同一口径：经唯一解析器归一，
            #    历史信封形态（{"mermaid_code": ...}）也能拿到可渲染载荷。
            _norm_code = extract_chart_payload(data) or str(data)
            items.append(
                {
                    "id": f"{s['id']}:{ct}",
                    "section_id": s["id"],
                    "section_title": s.get("title", ""),
                    "chart_type": ct,
                    "chart_label": _CHART_LABELS.get(ct, ct),
                    "title": _CHART_LABELS.get(ct, ct),
                    "priority": 3,
                    "status": "generated",
                    "placed": _is_placed(s["id"], content, ct),
                    "code": _norm_code,
                }
            )
    return {"items": items}


def _json_fix_candidate_rejected(is_json_fix: bool, cand: str) -> bool:
    """JSON 型图表修复的候选形态闸（✅ R169）。

    旧实现按**候选形态**分流校验：AI 不遵守「只返回 JSON」把数据型图表
    （labor/layout/timeline 等）改写成 Mermaid 文本时，``_validate_inline_chart``
    回落 Mermaid 校验照样放行 → ``validated=True`` → 写库 + 重写正文（保留原
    ```` ```chart-json ```` 围栏）→ 三侧全部不可见：导出解析 ``json.loads`` 失败
    整块跳过、JSON 型渲染器对 Mermaid 文本必然 None、清单 ``placed`` 翻 false。

    与提示词选择处 ``_looks_mermaid`` 纪律对齐：``is_json_fix`` 的候选必须是
    JSON 数据形态（对象或数组开头），否则该轮候选作废、带错误反馈进入下一轮。
    """
    return bool(is_json_fix) and not (cand or "").lstrip().startswith(("{", "["))


@router.post("/fix-mermaid")
async def fix_mermaid(body: dict, db=Depends(get_db)):
    """AI 自动修复渲染失败的 Mermaid 代码

    传入 section_id / prediction_id 时，修复结果写回 chart_predictions.data_json，
    并同步重写该章节正文中的内联代码块（导出以内联代码为最高优先级，
    不改正文则导出仍是旧坏代码）。chart_type 缺省时按代码本体推断。

    Returns:
        {code, written_back, content_updated, new_content, word_count, validated, attempts}
        validated=True 表示修复结果已通过确定性语法校验（修复闭环）。
        word_count 仅在 content_updated=True 时非 None（= text_word_count(new_content)，
        供前端图表修复回写时优先取后端权威字数，不本地伪造字符口径）。
    """
    code = body.get("code", "")
    error = body.get("error", "渲染失败")
    if not code or not code.strip():
        raise HTTPException(400, "缺少待修复的 Mermaid 代码")

    prediction_id = (body.get("prediction_id") or "").strip()
    section_id = (body.get("section_id") or "").strip()
    chart_type = (body.get("chart_type") or "").strip()
    # ✅ R200（2026-11-09）：前端透传的图表围栏序号（iter_inline_chart_fences
    #    0 基序号，含全部图表家族围栏）。同章存在**内容相同的多个坏块**时，
    #    按内容匹配只能命中首个 —— 前端点的是第 2 块的修复按钮，写回却改了
    #    第 1 块（预览修好成稿仍坏）。序号仅在与代码双重吻合时采纳，防伪造/漂移。
    _fence_ordinal = body.get("fence_ordinal")
    fence_ordinal: int | None = _fence_ordinal if isinstance(_fence_ordinal, int) and _fence_ordinal >= 0 else None

    # ✅ BUG 修复：预览修复入口可能传错/不传 chart_type（旧前端硬编码 "flowchart"），
    #    而 chart_predictions 回写按 (section_id, chart_type) 定位 —— 类型不符时
    #    查不到行，回写与正文同步全部静默失效。图表类型以代码本体为准：
    #    JSON 载荷取 "type" 字段，Mermaid 载荷按首关键字推断，推断失败才退回传参。
    if section_id and not prediction_id:
        derived = ""
        if code.lstrip().startswith("{"):
            try:
                derived = str(json.loads(code).get("type", "")).strip().lower()
            except (json.JSONDecodeError, AttributeError, ValueError):
                derived = ""
        if not derived:
            from app.services.chart_validators import detect_mermaid_chart_type

            derived = detect_mermaid_chart_type(code, default="")
        if derived:
            chart_type = derived

    # 兼容列表 id 格式 "{section_id}:{chart_type}"（sections.*_json 历史数据项）
    if prediction_id and ":" in prediction_id:
        section_id, chart_type = prediction_id.rsplit(":", 1)
        prediction_id = ""

    # ✅ R98（R90 遗留⑦收口）：本端点回写 data_json 并同步重写章节正文内联块，
    #    正文/目录生成在跑时会与生成链路互相覆盖。守卫在 AI 修复调用之前。
    await _guard_chart_target(db, section_id, "图表代码修复", prediction_id=prediction_id)

    # ✅ BUG 修复：chart_mermaid_fix 提示词含 {scheme_type} 占位符，而旧实现
    #    只传 error/code —— 渲染后系统提示里残留字面量 "{scheme_type}"，模型看到的是
    #    占位符而非真实方案类型，修复针对性下降。此处按章节反查方案类型补全。
    scheme_type_text = "通用工程"
    if section_id:
        try:
            _cur = await db.execute(
                "SELECT s.type FROM schemes s JOIN sections sec ON sec.scheme_id=s.id" " WHERE sec.id=?", (section_id,)
            )
            _srow = await _cur.fetchone()
            if _srow and _srow["type"]:
                scheme_type_text = str(_srow["type"])
        except Exception as e:
            logger.debug("修复提示词方案类型反查失败（用默认值）: %s", e)

    # ✅ 修复闭环（对齐 OpenBidKit prepareRenderableMermaid 的修复循环）：
    #    旧实现单轮 AI 修复、不验证修复结果 —— 坏代码修好后仍是坏代码，
    #    用户要手动反复点击。现改为「AI 修复 → 确定性校验（mermaid 语法 /
    #    chart-json 结构）→ 未通过则带错误反馈再修」，最多 3 轮。
    MAX_FIX_ATTEMPTS = 3
    fixed = ""
    validated = False
    fix_attempts = 0
    # ✅ BUG 修复：保留最近一次 AI 输出作为"尽力而为"的兜底结果。
    #    旧实现在 3 轮都没通过确定性校验时直接 raise 500「修复返回空代码」，
    #    用户看到的是"AI 修复失败"，而 AI 其实每次都返回了可用代码
    #    （典型场景：数据型图表被 AI 改写成 Mermaid 文本、或弱模型只给 2 个节点）。
    #    接口返回契约本就带 validated 标志（validated=False 表示未过校验），
    #    现改为降级返回该候选并标记 validated=False，避免整条修复链路硬失败。
    best_effort = ""
    last_error = str(error) or "渲染失败"

    # ✅ BUG 修复：按**原始载荷形态**选择修复提示词。旧实现对 labor/layout/
    #    timeline/architecture 等 chart-json 数据也发 chart_mermaid_fix（通篇
    #    Mermaid 语法规则），AI 倾向于把 JSON 数据改写成 Mermaid 文本 ——
    #    改写物既非合法数据信封也无法回写，JSON 类错误修复实质必然失败。
    from app.routers._chart_pipeline import _JSON_CHART_TYPES

    original_is_json = code.lstrip().startswith(("{", "["))
    # ✅ BUG 修复（与 _validate_inline_chart 对齐）：提示词选择按**载荷形态**分流，
    #    而不是仅凭 chart_type 集合。旧实现 `chart_type in _JSON_CHART_TYPES` 会把
    #    ```mermaid 围栏的 gantt / timeline 代码（类型同名但载荷是 Mermaid 语法）
    #    错送 chart_json_fix —— AI 被要求"严禁 Mermaid 语法"，只能把原代码改写成
    #    JSON 数据，原文进度编排被无谓重构。现先用 first_mermaid_keyword 判定代码
    #    本体形态：带合法 Mermaid 关键字的一律走 Mermaid 修复路径，与校验闭环
    #    （_validate_inline_chart 同样按 code.startswith("{") 分流）保持一致。
    from app.services.chart_validators import first_mermaid_keyword

    _looks_mermaid = bool(first_mermaid_keyword(code))
    is_json_fix = original_is_json or (chart_type in _JSON_CHART_TYPES and not _looks_mermaid)
    if is_json_fix and not chart_type:
        try:
            chart_type = str(json.loads(code).get("type", "")).strip().lower()
        except (json.JSONDecodeError, AttributeError, ValueError):
            chart_type = ""

    # ✅ 分类型专项修复提示词（2026-09-17）：按 chart_type 优先选择
    #    chart_{mermaid|json}_fix_{type} 专项版（甘特依赖/劳动力矩阵/平面布置等
    #    类型规则在通用大而全提示词中被稀释），未登记的类型回退通用版。
    from app.services.ai.prompts._registry import get_default_prompt as _prompt_registered

    def _select_fix_prompt(is_json: bool) -> tuple[str, str]:
        """返回 (render key, user_hint)；专项版未注册时回退通用版。"""
        if is_json:
            _k = f"chart_json_fix_{chart_type}" if chart_type else ""
            if _k and _prompt_registered(_k):
                return _k, "请修复以上图表 JSON 数据，只返回修复后的完整 JSON。"
            return "chart_json_fix", "请修复以上图表 JSON 数据，只返回修复后的完整 JSON。"
        _k = f"chart_mermaid_fix_{chart_type}" if chart_type else ""
        if _k and _prompt_registered(_k):
            return _k, "请修复以上 Mermaid 代码，只返回修复后的完整代码。"
        return "chart_mermaid_fix", "请修复以上 Mermaid 代码，只返回修复后的完整代码。"

    _fix_key, _fix_hint = _select_fix_prompt(is_json_fix)

    try:
        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            fix_attempts = attempt
            if is_json_fix:
                sys_prompt = render(
                    _fix_key,
                    error=clip_text_notice(last_error or "", 500, "图表修复错误信息"),
                    code=code,
                    scheme_type=scheme_type_text,
                    chart_type=chart_type or "comparison",
                )
                user_hint = _fix_hint
            else:
                sys_prompt = render(
                    _fix_key,
                    error=clip_text_notice(last_error or "", 500, "图表修复错误信息"),
                    code=code,
                    scheme_type=scheme_type_text,
                )
                user_hint = _fix_hint
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": (f"{user_hint}（第 {attempt}/{MAX_FIX_ATTEMPTS} 次修复尝试）")},
            ]
            # ✅ 2026-09-23 修复：该调用点原先未传 scene（违反「任何新增 AI 调用点
            #    必须显式传 scene」的约定），导致图表修复的调用在 /ai/stats 场景聚合里
            #    归入空场景、也无法被「场景模型路由」单独指定模型。现补上。
            cand = await chat_with_fallback(messages, scene="chart_fix")
            cand = cand.strip().strip("`").strip()
            # ✅ BUG 修复：剥离 ```json 围栏的语言标签。
            #    旧实现只处理 "mermaid" 前缀，AI 返回 JSON 时得到 "json\n{...}"，
            #    json.loads 必然失败 → JSON 修复结果被当成普通文本写入 code 键。
            _first, _sep, _rest = cand.partition("\n")
            if _sep and _first.strip().lower() in ("json", "mermaid"):
                cand = _rest.strip()
            elif cand.startswith("mermaid"):
                cand = cand[len("mermaid") :].strip()
            if not cand:
                last_error = "修复返回空代码，请重新输出完整代码"
                continue
            best_effort = cand
            # ✅ R169：JSON 型修复的候选形态闸。旧实现按**候选形态**分流校验——
            #    AI 不遵守「只返回 JSON」把数据型图表（labor/layout/timeline 等）
            #    改写成 Mermaid 文本时，_validate_inline_chart 回落 Mermaid 校验
            #    照样放行 → validated=True → 写库 + _rewrite_code_block（保留原
            #    ```chart-json 围栏）→ 三侧全部不可见：导出解析 json.loads 失败
            #    整块跳过、labor 渲染器对 Mermaid 文本必然 None、清单 placed 翻
            #    false。现与提示词选择处 _looks_mermaid 纪律对齐：is_json_fix 的
            #    候选必须是 JSON 数据形态，否则带错误反馈进入下一轮（3 轮耗尽走
            #    best_effort 降级返回、validated=False，绝不写库）。
            if _json_fix_candidate_rejected(is_json_fix, cand):
                last_error = "该图表为 JSON 数据型，修复结果必须返回完整 JSON 数据" "（严禁 Mermaid 语法），请重新输出"
                logger.info(
                    "JSON 图表修复第 %d/%d 轮返回 Mermaid 文本，拒绝该候选" "（section=%s）",
                    attempt,
                    MAX_FIX_ATTEMPTS,
                    (section_id or prediction_id)[:8],
                )
                continue
            # 校验闭环：按修复后代码的**实际形态**分流（JSON 数据 / Mermaid 语法）
            _vt = chart_type
            if cand.lstrip().startswith("{"):
                try:
                    _vt = str(json.loads(cand).get("type", "")).strip().lower() or _vt
                except (json.JSONDecodeError, AttributeError, ValueError):
                    pass
            elif not _vt:
                from app.services.chart_validators import detect_mermaid_chart_type

                _vt = detect_mermaid_chart_type(cand, default="")
            from app.routers._chart_pipeline import _validate_inline_chart

            ok, validated_code = _validate_inline_chart(_vt, cand)
            if ok:
                fixed = validated_code or cand
                validated = True
                break
            last_error = (
                f"第 {attempt} 轮修复结果仍未通过语法校验"
                f"（{validated_code or '结构非法'}），请修正后重新输出完整代码"
            )
            logger.info(
                "Mermaid 修复第 %d/%d 轮未通过校验（section=%s）",
                attempt,
                MAX_FIX_ATTEMPTS,
                (section_id or prediction_id)[:8],
            )
    except Exception as e:
        raise HTTPException(500, f"Mermaid 修复失败: {e}")

    if not fixed and best_effort:
        # ✅ 降级：多轮均未通过确定性校验 → 返回最后一次 AI 输出（validated=False），
        #    由调用方按"未过校验"提示用户，而不是整条修复链路 500。
        #    ⚠️ 该候选**不写库、不改正文**（见下方 validated 守卫）：旧实现把未过
        #    校验的候选直接写回 chart_predictions 并同步正文，产生"标记成功、实际
        #    仍是坏图/未知载荷"的假闭环，导出时依旧渲染失败。
        fixed = best_effort
        validated = False
        logger.warning(
            "Mermaid 修复 %d 轮均未通过语法校验，降级返回最后一次 AI 输出" "（section=%s，不写回）",
            fix_attempts,
            (section_id or prediction_id)[:8],
        )
    if not fixed:
        raise HTTPException(500, "修复返回空代码")

    # 写回修复结果 + 同步重写正文内联代码块（同一事务，仅一次 commit）。
    # ✅ BUG 修复：
    #   1) 只有 validated=True 的结果允许落库，未过校验的 best_effort 仅供前端预览；
    #   2) 旧实现 chart_predictions UPDATE 与 sections UPDATE 各 commit 一次 ——
    #      第二次失败时图表记录已是新代码而正文仍是旧坏代码，两侧长期不一致；
    #      现合并为单事务（任一失败整体 rollback）。
    wrote_back = False
    content_updated = False
    new_content = ""
    section_word_count = None  # F2（R71）：正文重写后的权威字数，仅 content_updated 时非 None
    if validated and (prediction_id or (section_id and chart_type)):
        try:
            if prediction_id:
                cur = await db.execute(
                    # ✅ BUG 修复：旧实现只 SELECT data_json，下方却用 row["id"]
                    #    → KeyError("No item with that key") 被外层 except 吞掉，
                    #    修复结果**从未写回**（与函数文档承诺相反，前端预览已修复
                    #    而导出仍用旧代码）。补上 id 列。
                    "SELECT id, data_json FROM chart_predictions WHERE id=?",
                    (prediction_id,),
                )
                if cur is None:
                    # ✅ R13 守卫（2026-10-04）：写回是**单事务**语义（任一失败整体
                    #    rollback + written_back=false）。此处抛错会被下方 except 接住，
                    #    按既定 fail-soft 契约返回 200 + written_back=false + WARNING，
                    #    而不是让 AttributeError 冒出去变成 500（也会丢掉 fixed 预览）。
                    raise RuntimeError("R13：db.execute 返回 None（chart_predictions 按 id 回写查询）")
                row = await cur.fetchone()
            else:
                cur = await db.execute(
                    "SELECT id, data_json FROM chart_predictions " "WHERE section_id=? AND chart_type=? LIMIT 1",
                    (section_id, chart_type),
                )
                if cur is None:
                    raise RuntimeError("R13：db.execute 返回 None（chart_predictions 按章节回写查询）")
                row = await cur.fetchone()

            patched = ""
            srow = None
            # ✅ BUG 修复（关键）：同步重写章节正文中的内联代码块。
            #    导出链路（export.write_section）取码优先级是"正文内联块 > chart_predictions"，
            #    只回写 chart_predictions 时，正文里的旧坏代码原样进入文档 ——
            #    表现为"预览已修复、导出仍是坏图"。
            if row and section_id:
                # ✅ R215：补 scheme_id 列 —— 审核状态回退 / 一致性缓存失效需要它
                cur = await db.execute(
                    "SELECT content, word_budget, scheme_id FROM sections WHERE id=?", (section_id,)
                )
                if cur is None:
                    raise RuntimeError("R13：db.execute 返回 None（sections 正文回写查询）")
                srow = await cur.fetchone()
                old_content = (srow["content"] or "") if srow else ""
                if old_content:
                    # ✅ R196（2026-11-09）：按围栏序号精确改写（只动目标块）——
                    #    旧实现 _rewrite_code_block 按代码内容匹配，`code == old_code`
                    #    命中**所有**同内容块：同章两块相同代码（手动保存路径
                    #    enforce_limits=False 不裁剪）会同时被替换，违背 R48 确立的
                    #    「按围栏序号精确改写、只动指定块」纪律。登记目标定位不到时
                    #    （正文与清单漂移）回退旧行为兜底。
                    from app.routers._chart_pipeline import _apply_chart_fence_edits, _scan_chart_fences_full

                    _target_ords = [_o for _ct, _cd, _o in _scan_chart_fences_full(old_content) if _cd == code]
                    if _target_ords:
                        # ✅ R199（2026-11-09）：同章多块同码时只改第一块是既有口径
                        #    （R196 按围栏序号精确改写、登记行只存一条/类型），但
                        #    多匹配意味着正文与清单已漂移（手动保存路径允许同码
                        #    多块）——修第 1 块后其余块仍是坏代码、导出照渲。
                        #    至少 WARNING 留痕，让「预览修好、成稿仍坏」可定位。
                        # ✅ R200：前端点的是**哪一块**由 fence_ordinal 精确指定
                        #    （0 基图表家族围栏序号）；仅当该序号处的代码与本次待修
                        #    code 双重吻合时采纳，否则告警回退首块（防序号漂移/伪造
                        #    把修复写进另一块）。未传（旧前端）行为不变。
                        _chosen_ord = _target_ords[0]
                        if fence_ordinal is not None and len(_target_ords) > 1:
                            if fence_ordinal in _target_ords:
                                _chosen_ord = fence_ordinal
                            else:
                                logger.warning(
                                    "fix-mermaid：前端围栏序号 %s 与正文同码块 %s 不吻合，回退首块",
                                    fence_ordinal,
                                    _target_ords,
                                )
                        if len(_target_ords) > 1 and _chosen_ord != fence_ordinal:
                            logger.warning(
                                "fix-mermaid：正文命中 %d 个同码图表块（围栏序号 %s），"
                                "改写序号 %s，其余块仍为坏代码（正文与清单漂移）",
                                len(_target_ords),
                                _target_ords,
                                _chosen_ord,
                            )
                        patched = _apply_chart_fence_edits(old_content, {_chosen_ord: fixed}) or ""
                    else:
                        patched = _rewrite_code_block(old_content, code, fixed) or ""

            if row:
                try:
                    payload = json.loads(row["data_json"] or "{}")
                    if not isinstance(payload, dict):
                        payload = {}
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                title = str(payload.get("title") or "")
                reason = str(payload.get("reason") or "")
                # ✅ BUG 修复：写回必须遵守 chart_payload 的"唯一构造器"契约。
                #    旧实现一律 payload["mermaid_code"] = fixed：
                #    · 原为代码信封（envelope:mermaid）→ 恰好正确；
                #    · 原为 JSON 数据信封（envelope:data，labor/layout/timeline/
                #      architecture）→ 覆盖后 mermaid_code 与 data 同时非空，
                #      chart_payload_shape 判为 "unknown"（双载荷非法），
                #      extract_chart_payload 按 code 优先取值丢掉 data ——
                #      数据型图表修复一次即从合法形态退化为未知形态，
                #      若 AI 返回的是 Mermaid 语法还会让数据渲染路径失效。
                #    现按 AI 实际返回物重建规范信封：JSON → data 信封，
                #    文本 → code 信封，两种情况都满足 is_canonical_chart_payload。
                from app.services.chart_payload import build_chart_envelope

                fixed_data = None
                if fixed.lstrip().startswith(("{", "[")):
                    try:
                        _obj = json.loads(fixed)
                        if isinstance(_obj, (dict, list)) and _obj:
                            fixed_data = _obj
                    except (json.JSONDecodeError, TypeError):
                        fixed_data = None
                if fixed_data is not None:
                    new_payload = build_chart_envelope(data=fixed_data, title=title, reason=reason)
                else:
                    new_payload = build_chart_envelope(code=fixed, title=title, reason=reason)
                cur = await db.execute(
                    "UPDATE chart_predictions SET data_json=?, status='generated' WHERE id=?", (new_payload, row["id"])
                )
                # ✅ R13 守卫（2026-10-04）：写操作返回 None = 语句未执行，但旧行为
                #    照样置 wrote_back=True → 响应谎报「已写回」而库中仍是坏代码
                #    （前端显示已修复、下次打开又变回坏图）。走同一 rollback 语义。
                if cur is None:
                    raise RuntimeError("R13：db.execute 返回 None（chart_predictions 写回 UPDATE）")
                wrote_back = True

                if patched and patched != (srow["content"] or ""):
                    from app.services.content_utils import DEFAULT_WORD_BUDGET, text_word_count, word_status_for

                    wb = (srow["word_budget"] if srow else None) or DEFAULT_WORD_BUDGET
                    # ✅ 与正文生成/手工保存同一字数口径（剔除图表代码块）
                    wc = text_word_count(patched)
                    cur = await db.execute(
                        "UPDATE sections SET content=?, word_count=?, word_status=?," " updated_at=? WHERE id=?",
                        (patched, wc, word_status_for(wc, wb), datetime.now().isoformat(), section_id),
                    )
                    if cur is None:
                        raise RuntimeError("R13：db.execute 返回 None（sections 正文写回 UPDATE）")
                    content_updated = True
                    new_content = patched
                    section_word_count = wc
                    # ✅ R215（2026-11-11 · P3）：正文真实变化 → ① 审核结论退回待审核
                    #    （G9 同族第三处漏改：numbering 修复 R175-B09 / 手工保存 G9 已接，
                    #    图表修复与 AI 配图两端点此前漏接 —— 已 approved 的正文被改写后
                    #    仍挂审核结论，导出预检 review 判定与成稿不一致）；
                    #    ② 一致性扫描缓存失效（R46：每个正文写点必须失效）。同事务，
                    #    commit 在本函数尾部统一提交。
                    try:
                        await reset_review_on_content_change(
                            db, str(srow["scheme_id"] or ""), section_id, actor="图表修复"
                        )
                    except Exception as _rr:  # noqa: BLE001 — 回退失败不阻断修复写回
                        logger.debug("章节 %s 图表修复后审核状态回退失败: %s", section_id[:8], _rr)
                    # ✅ R215：函数内延迟 import（sections 为大模块且存在反向依赖面）
                    from app.routers.sections import invalidate_consistency_scan_cache

                    await invalidate_consistency_scan_cache(db, str(srow["scheme_id"] or ""))
                    logger.info("章节 %s 内联图表代码已随修复同步更新", section_id[:8])

            await db.commit()
        except Exception as e:
            logger.warning("Mermaid 修复结果写回失败（已回滚）: %s", e)
            try:
                await db.rollback()
            except Exception as rb_err:
                logger.debug("charts 写回失败路径的 rollback 也失败（清理路径，忽略）: %s", rb_err)
            wrote_back = False
            content_updated = False
            new_content = ""
            section_word_count = None

    return {
        "code": fixed,
        "written_back": wrote_back,
        "content_updated": content_updated,
        "new_content": new_content,
        "word_count": section_word_count,
        "validated": validated,
        "attempts": fix_attempts,
    }


@router.post("/generate-ai-image")
async def generate_ai_image(body: dict, db=Depends(get_db)):
    """对章节内联的 ```ai_image 块生成真实配图（文生图），并改写为 ![title](url)

    ⚠️ v17（2026-09-22）起为**兼容保留**端点：AI 配图已在导出准备阶段由
    `_auto_generate_ai_image_blocks` 自动生成（前端「生成配图」按钮已移除，
    对齐"图表全自动生成、无人工生图入口"的产品约束）。本端点仍可用于
    手动重试/脚本调用，行为不变。

    对齐 OpenBidKit 的 ai 插图类型：正文生成阶段只占位（chart_predictions
    status=pending），此处由脚本/手动重试触发（前端无「生成配图」按钮，见
    AGENTS.md §4.3；默认 ai_image_manual_enabled=False 时本端点直接返回 409，
    需设 AI_IMAGE_MANUAL_ENABLED=true 才放行），复用
    image_engine.generate_illustration_image（提供商降级链 + MD5/失败缓存）。
    图像模型配置取自 app.config.settings（image_* 字段）。生成成功后将正文中的
    ai_image 代码块改写为 Markdown 图片链接，导出走既有 image 块路径；
    同时把对应 chart_predictions 记录置为完成态 generated。图像生成失败时优雅返回，
    不阻塞调用方（与 image_engine 语义一致）。
    """
    section_id = (body.get("section_id") or "").strip()
    if not section_id:
        raise HTTPException(400, "缺少 section_id")
    # ✅ P4（2026-09-23）：v17 产品约束「图表全自动生成、无人工生图入口」。
    #    默认 ai_image_manual_enabled=False 时，本兼容保留端点拒绝人工/脚本触发，
    #    说明配图已在导出 DOCX 时自动生成；仅显式开启 AI_IMAGE_MANUAL_ENABLED
    #    才恢复手动重试链路（向后兼容）。
    from app.config import settings as _cfg

    if not _cfg.ai_image_manual_enabled:
        raise HTTPException(
            409,
            "图表已全自动生成（v17 约束）：AI 配图在导出 DOCX 时自动生成真实图片，"
            "无需人工触发。如需脚本/手动重试，请设置 AI_IMAGE_MANUAL_ENABLED=true。",
        )
    # ✅ R98（R90 遗留⑦收口）：本端点把 ai_image 块改写为图片链接（重写正文），
    #    生成在跑时与生成链路互相覆盖。守卫在生图成本发生之前。
    await _guard_chart_target(db, section_id, "AI 配图生成")
    cur = await db.execute(
        # ✅ R215：补 scheme_id 列 —— 审核状态回退 / 一致性缓存失效需要它
        "SELECT id, content, word_budget, scheme_id FROM sections WHERE id=?",
        (section_id,),
    )
    # ✅ R13 守卫（2026-10-04）：读路径 fail-closed 503，绝不把 DB 异常伪装成 404
    #    「章节不存在」（否则用户会去重建/找章节，排障方向被带偏）。
    if cur is None:
        logger.warning("AI 配图章节读取失败：db.execute 返回 None（R13，section=%s）", section_id[:8])
        raise HTTPException(503, "数据库连接暂时不可用，请稍后重试")
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "章节不存在")
    content = row["content"] or ""
    # ✅ BUG 修复：连接 row_factory 是 aiosqlite.Row（= sqlite3.Row），**没有 .get()**
    #    方法；旧实现用字典式取值读取 word_budget 必抛 AttributeError → 该端点恒 500，
    #    「生成配图」全链路不可用。与上一行的下标取值保持一致。
    #    回归守卫见 tests/test_e2e_chain_hardening.py（源码级断言）。
    from app.services.content_utils import DEFAULT_WORD_BUDGET

    word_budget = row["word_budget"] or DEFAULT_WORD_BUDGET

    from app.routers._chart_pipeline import _rewrite_code_block, _scan_chart_fences_full

    # ✅ R215（2026-11-11 · P3）：改用全量块扫描 —— extract_inline_charts 是
    #    「对外清单口径」（每类型只保留第一个，_chart_pipeline 同类型去重），
    #    同章多个不同 prompt 的 ai_image 块此前只有第一个被枚举生图（其余永不
    #    处理，且 _rewrite_code_block 会把它们改写成同一 URL）。
    _blocks = [(ct, c) for ct, c, _ord in _scan_chart_fences_full(content) if ct == "ai_image"]
    if not _blocks:
        raise HTTPException(404, "本章节正文无 AI 配图（ai_image）块")

    from app.config import settings
    from app.services.ai.image_engine import generate_illustration_image, resolve_image_config

    # ✅ R238：改用 resolve_image_config（DB 优先、settings 兜底）—— 手动重试端点与
    #    导出链路同源配置（换库后此处也生效，避免「导出用新配置、重试用旧配置」分叉）。
    image_config = await resolve_image_config(db, settings)

    new_content = content
    generated = 0
    for _ct, _code in _blocks:
        try:
            _obj = json.loads(_code)
        except Exception:
            _obj = {}
        _prompt = str((_obj or {}).get("prompt") or "").strip()
        if not _prompt:
            continue
        _style = str((_obj or {}).get("style") or "engineering_diagram")
        _title = str((_obj or {}).get("title") or "AI 配图")
        # ✅ 配图提示词优化（提示词 ILLUSTRATION_PROMPT_OPTIMIZE，可在管理页编辑）：
        #    内联 ai_image 块的 prompt 通常较短，直接送图像模型细节不足。
        #    先用文本模型按风格映射扩写为「主体+构图+视角+光照+质感」的完整提示词；
        #    优化失败 / 返回为空时回退原始 prompt，不阻塞生成（与图像生成同语义）。
        try:
            _opt_sys = render("ILLUSTRATION_PROMPT_OPTIMIZE", style=_style, section_title=_title)
            _opt_resp = await chat_with_fallback(
                [
                    {"role": "system", "content": _opt_sys},
                    {"role": "user", "content": f"原始提示词：{_prompt}\n\n请直接输出优化后的提示词文本。"},
                ],
                scene="image_prompt_optimize",
            )
            _optimized = str(_opt_resp or "").strip().strip("`").strip()
            # 剥离误带的语言标签 / 围栏残留
            _f, _sep, _r = _optimized.partition("\n")
            if _sep and _f.strip().lower() in ("text", "prompt"):
                _optimized = _r.strip()
            if _optimized:
                logger.info("配图提示词已优化（章节 %s）: %d -> %d 字", section_id[:8], len(_prompt), len(_optimized))
                _prompt = _optimized[:600]
        except Exception as e:
            logger.warning("配图提示词优化失败（使用原始提示词，章节 %s）: %s", section_id[:8], e)
        try:
            _url = await generate_illustration_image(
                None, _prompt, _style, "16:9", settings.image_provider, image_config
            )
        except Exception as e:
            logger.warning("ai_image 生成异常（章节 %s）: %s", section_id[:8], e)
            _url = None
        if not _url:
            continue
        new_content = _rewrite_code_block(new_content, _code, f"![{_title}]({_url})")
        generated += 1
        try:
            _c = await db.execute(
                # ✅ 状态口径统一：全表唯一的完成态是 'generated'（登记/修复同口径，
                # 统计侧 CHART_DONE_STATUSES 双值兼容仅为历史脏数据）。此处旧写
                # 'done' 使新生成的配图在严格按 'generated' 过滤的链路上漏统。
                "UPDATE chart_predictions SET status='generated', data_json=? "
                "WHERE section_id=? AND chart_type='ai_image' AND status='pending'",
                (build_chart_envelope(code=_code, title=_title, reason=_url), section_id),
            )
            if _c is None:
                # R13：写入未执行（状态仍 pending），走既有 fail-soft → WARNING
                raise RuntimeError("R13：db.execute 返回 None（ai_image 状态 UPDATE）")
        except Exception as e:
            logger.warning("ai_image chart_predictions 更新失败: %s", e)

    if generated == 0:
        raise HTTPException(502, "所有 AI 配图生成失败（请检查图像模型配置或网络）")

    # ✅ 与 fix-mermaid 一致：同步刷新 word_count/word_status，避免正文与统计不一致
    from app.services.content_utils import text_word_count, word_status_for

    _wc = text_word_count(new_content)
    _cur = await db.execute(
        "UPDATE sections SET content=?, word_count=?, word_status=?, updated_at=? WHERE id=?",
        (new_content, _wc, word_status_for(_wc, word_budget), datetime.now().isoformat(), section_id),
    )
    # ✅ R13 守卫（2026-10-04）：写入未执行时旧行为照样返回 content_updated=True
    #    （响应谎报，正文下次读仍是旧的）。fail-closed 503。
    if _cur is None:
        logger.warning("AI 配图正文回写失败：db.execute 返回 None（R13，section=%s）", section_id[:8])
        raise HTTPException(503, "配图已生成但正文回写失败，请稍后重试")
    # ✅ R215（2026-11-11 · P3）：与 fix-mermaid 同口径 —— 正文真实变化 →
    #    审核结论退回待审核（G9 收口）+ 一致性扫描缓存失效（R46）。
    from app.routers.sections import invalidate_consistency_scan_cache

    try:
        await reset_review_on_content_change(db, str(row["scheme_id"] or ""), section_id, actor="AI 配图")
    except Exception as _rr:  # noqa: BLE001 — 回退失败不阻断配图写回
        logger.debug("章节 %s AI 配图后审核状态回退失败: %s", section_id[:8], _rr)
    await invalidate_consistency_scan_cache(db, str(row["scheme_id"] or ""))
    await db.commit()
    return {"generated": generated, "content_updated": True, "new_content": new_content}
