"""图表路由：图表预测 + Mermaid 代码生成 + 图表渲染"""
import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_db
from app.services.ai.provider_factory import chat_with_fallback
from app.services.ai.prompts._registry import render
# 图表类型标签统一共享自 chart_validators（别名保持原引用点不变）
from app.services.chart_validators import CHART_TYPE_LABELS as _CHART_LABELS
from app.services.chart_validators import infer_chart_type_from_payload
from app.services.chart_payload import build_chart_envelope, extract_chart_payload
# ✅ BUG 修复（2026-09-16 · ruff F821）：`fix-mermaid` 里在 `if row:` 分支**内部**
#    才 import `_rewrite_code_block`，而该名字在第 380 行（更早的
#    「同步重写章节正文内联代码块」分支）就被引用 —— Python 视其为函数局部名，
#    先引用后赋值 → `UnboundLocalError`，被外层 except 吞掉：
#    表现为「预览已修复、导出仍是坏图」的旧问题**始终存在**（该修复从未生效）。
#    这里提到模块顶部，两处引用都能拿到。
from app.routers._chart_pipeline import _rewrite_code_block  # noqa: E402

logger = logging.getLogger("charts")
router = APIRouter(prefix="/api/v1/charts", tags=["charts"])


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
        logger.info(
            "/charts/render：chart_type=%r 与载荷结构不符，按结构纠正为 %r",
            chart_type, _inferred)
        chart_type = _inferred

    try:
        from app.services.ai.mermaid_renderer import _chart_cache
        img_bytes = await asyncio.to_thread(
            _chart_cache.get_or_render, mermaid_code, chart_type, "png", skip_http
        )
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
            raise HTTPException(
                500,
                f"图表渲染失败（chart_type={chart_type}）。"
                f"渲染后端最近一次探测结果: {avail_text}（skip_http={skip_http}）。"
                f"请检查 Mermaid 代码语法，或把分号分隔改为换行格式后重试。"
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
        (scheme_id,))
    _section_rows = [dict(r) for r in await cur.fetchall()]
    sec_map = {r["id"]: r for r in _section_rows}

    # 逐章节缓存"正文内联图表类型集合"（同章节多行共用一次解析，避免重复扫描）
    from app.routers._chart_pipeline import extract_inline_charts
    _inline_types_cache: dict[str, set[str]] = {}

    def _is_placed(sid: str, content: str, ct: str) -> bool:
        """正文是否已含该类型图表的落点（内联代码块 或 历史标记）"""
        if f"[CHART_TYPE: {ct}]" in content:
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
        "SELECT * FROM chart_predictions WHERE scheme_id=? AND needed=1 ORDER BY priority DESC",
        (scheme_id,))
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
        items.append({
            "id": p["id"], "section_id": sid,
            "section_title": sec.get("title", ""),
            "chart_type": ct, "chart_label": _CHART_LABELS.get(ct, ct),
            "title": p.get("purpose") or _CHART_LABELS.get(ct, ct),
            "priority": p.get("priority", 3), "status": p.get("status", "generated"),
            "placed": _is_placed(sid, content, ct),
            "code": code,
        })

    # 2) sections 表 *_json 列（兼容历史数据）
    field_map = {
        "flowchart": "flowchart_json", "gantt": "gantt_json",
        "architecture": "architecture_json", "labor": "labor_json",
        "comparison": "comparison_json", "layout": "layout_json",
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
            items.append({
                "id": f"{s['id']}:{ct}", "section_id": s["id"],
                "section_title": s.get("title", ""),
                "chart_type": ct, "chart_label": _CHART_LABELS.get(ct, ct),
                "title": _CHART_LABELS.get(ct, ct),
                "priority": 3, "status": "generated",
                "placed": _is_placed(s["id"], content, ct),
                "code": _norm_code,
            })
    return {"items": items}


@router.post("/fix-mermaid")
async def fix_mermaid(body: dict, db=Depends(get_db)):
    """AI 自动修复渲染失败的 Mermaid 代码

    传入 section_id / prediction_id 时，修复结果写回 chart_predictions.data_json，
    并同步重写该章节正文中的内联代码块（导出以内联代码为最高优先级，
    不改正文则导出仍是旧坏代码）。chart_type 缺省时按代码本体推断。

    Returns:
        {code, written_back, content_updated, new_content, validated, attempts}
        validated=True 表示修复结果已通过确定性语法校验（修复闭环）。
    """
    code = body.get("code", "")
    error = body.get("error", "渲染失败")
    if not code or not code.strip():
        raise HTTPException(400, "缺少待修复的 Mermaid 代码")

    prediction_id = (body.get("prediction_id") or "").strip()
    section_id = (body.get("section_id") or "").strip()
    chart_type = (body.get("chart_type") or "").strip()

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

    # ✅ BUG 修复：chart_mermaid_fix 提示词含 {scheme_type} 占位符，而旧实现
    #    只传 error/code —— 渲染后系统提示里残留字面量 "{scheme_type}"，模型看到的是
    #    占位符而非真实方案类型，修复针对性下降。此处按章节反查方案类型补全。
    scheme_type_text = "通用工程"
    if section_id:
        try:
            _cur = await db.execute(
                "SELECT s.type FROM schemes s JOIN sections sec ON sec.scheme_id=s.id"
                " WHERE sec.id=?", (section_id,))
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
                    _fix_key, error=last_error[:500], code=code,
                    scheme_type=scheme_type_text,
                    chart_type=chart_type or "comparison")
                user_hint = _fix_hint
            else:
                sys_prompt = render(
                    _fix_key, error=last_error[:500], code=code,
                    scheme_type=scheme_type_text)
                user_hint = _fix_hint
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": (
                    f"{user_hint}（第 {attempt}/{MAX_FIX_ATTEMPTS} 次修复尝试）")},
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
                cand = cand[len("mermaid"):].strip()
            if not cand:
                last_error = "修复返回空代码，请重新输出完整代码"
                continue
            best_effort = cand
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
            last_error = (f"第 {attempt} 轮修复结果仍未通过语法校验"
                          f"（{validated_code or '结构非法'}），请修正后重新输出完整代码")
            logger.info("Mermaid 修复第 %d/%d 轮未通过校验（section=%s）",
                        attempt, MAX_FIX_ATTEMPTS, (section_id or prediction_id)[:8])
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
        logger.warning("Mermaid 修复 %d 轮均未通过语法校验，降级返回最后一次 AI 输出"
                       "（section=%s，不写回）",
                       fix_attempts, (section_id or prediction_id)[:8])
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
    if validated and (prediction_id or (section_id and chart_type)):
        try:
            if prediction_id:
                cur = await db.execute(
                    # ✅ BUG 修复：旧实现只 SELECT data_json，下方却用 row["id"]
                    #    → KeyError("No item with that key") 被外层 except 吞掉，
                    #    修复结果**从未写回**（与函数文档承诺相反，前端预览已修复
                    #    而导出仍用旧代码）。补上 id 列。
                    "SELECT id, data_json FROM chart_predictions WHERE id=?",
                    (prediction_id,))
                row = await cur.fetchone()
            else:
                cur = await db.execute(
                    "SELECT id, data_json FROM chart_predictions "
                    "WHERE section_id=? AND chart_type=? LIMIT 1",
                    (section_id, chart_type))
                row = await cur.fetchone()

            patched = ""
            srow = None
            # ✅ BUG 修复（关键）：同步重写章节正文中的内联代码块。
            #    导出链路（export.write_section）取码优先级是"正文内联块 > chart_predictions"，
            #    只回写 chart_predictions 时，正文里的旧坏代码原样进入文档 ——
            #    表现为"预览已修复、导出仍是坏图"。
            if row and section_id:
                cur = await db.execute(
                    "SELECT content, word_budget FROM sections WHERE id=?", (section_id,))
                srow = await cur.fetchone()
                old_content = (srow["content"] or "") if srow else ""
                if old_content:
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
                await db.execute(
                    "UPDATE chart_predictions SET data_json=?, status='generated' WHERE id=?",
                    (new_payload, row["id"]))
                wrote_back = True

                if patched and patched != (srow["content"] or ""):
                    from app.services.content_utils import (
                        word_status_for, text_word_count, DEFAULT_WORD_BUDGET)
                    wb = (srow["word_budget"] if srow else None) or DEFAULT_WORD_BUDGET
                    # ✅ 与正文生成/手工保存同一字数口径（剔除图表代码块）
                    wc = text_word_count(patched)
                    await db.execute(
                        "UPDATE sections SET content=?, word_count=?, word_status=?,"
                        " updated_at=? WHERE id=?",
                        (patched, wc, word_status_for(wc, wb),
                         datetime.now().isoformat(), section_id))
                    content_updated = True
                    new_content = patched
                    logger.info("章节 %s 内联图表代码已随修复同步更新", section_id[:8])

            await db.commit()
        except Exception as e:
            logger.warning("Mermaid 修复结果写回失败（已回滚）: %s", e)
            try:
                await db.rollback()
            except Exception:
                pass
            wrote_back = False
            content_updated = False
            new_content = ""

    return {"code": fixed, "written_back": wrote_back,
            "content_updated": content_updated, "new_content": new_content,
            "validated": validated, "attempts": fix_attempts}


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
            "无需人工触发。如需脚本/手动重试，请设置 AI_IMAGE_MANUAL_ENABLED=true。")
    cur = await db.execute(
        "SELECT id, content, word_budget FROM sections WHERE id=?", (section_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "章节不存在")
    content = (row["content"] or "")
    # ✅ BUG 修复：连接 row_factory 是 aiosqlite.Row（= sqlite3.Row），**没有 .get()**
    #    方法；旧实现用字典式取值读取 word_budget 必抛 AttributeError → 该端点恒 500，
    #    「生成配图」全链路不可用。与上一行的下标取值保持一致。
    #    回归守卫见 tests/test_e2e_chain_hardening.py（源码级断言）。
    word_budget = row["word_budget"] or 1500

    from app.routers._chart_pipeline import extract_inline_charts, _rewrite_code_block
    _blocks = [(ct, c) for ct, c in extract_inline_charts(content) if ct == "ai_image"]
    if not _blocks:
        raise HTTPException(404, "本章节正文无 AI 配图（ai_image）块")

    from app.config import settings
    image_config = {
        "image_enabled": settings.image_enabled,
        "image_api_key_enc": settings.image_api_key,
        "image_base_url": settings.image_base_url,
        "image_model": settings.image_model,
        "image_default_size": settings.image_default_size,
        "image_price_per_image": settings.image_price_per_image,
    }
    from app.services.ai.image_engine import generate_illustration_image

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
            _opt_sys = render(
                "ILLUSTRATION_PROMPT_OPTIMIZE", style=_style, section_title=_title)
            _opt_resp = await chat_with_fallback([
                {"role": "system", "content": _opt_sys},
                {"role": "user", "content":
                    f"原始提示词：{_prompt}\n\n请直接输出优化后的提示词文本。"},
            ], scene="image_prompt_optimize")
            _optimized = str(_opt_resp or "").strip().strip("`").strip()
            # 剥离误带的语言标签 / 围栏残留
            _f, _sep, _r = _optimized.partition("\n")
            if _sep and _f.strip().lower() in ("text", "prompt"):
                _optimized = _r.strip()
            if _optimized:
                logger.info("配图提示词已优化（章节 %s）: %d -> %d 字",
                            section_id[:8], len(_prompt), len(_optimized))
                _prompt = _optimized[:600]
        except Exception as e:
            logger.warning("配图提示词优化失败（使用原始提示词，章节 %s）: %s",
                           section_id[:8], e)
        try:
            _url = await generate_illustration_image(
                None, _prompt, _style, "16:9", settings.image_provider, image_config)
        except Exception as e:
            logger.warning("ai_image 生成异常（章节 %s）: %s", section_id[:8], e)
            _url = None
        if not _url:
            continue
        new_content = _rewrite_code_block(new_content, _code, f"![{_title}]({_url})")
        generated += 1
        try:
            await db.execute(
                # ✅ 状态口径统一：全表唯一的完成态是 'generated'（登记/修复同口径，
                # 统计侧 CHART_DONE_STATUSES 双值兼容仅为历史脏数据）。此处旧写
                # 'done' 使新生成的配图在严格按 'generated' 过滤的链路上漏统。
                "UPDATE chart_predictions SET status='generated', data_json=? "
                "WHERE section_id=? AND chart_type='ai_image' AND status='pending'",
                (build_chart_envelope(code=_code, title=_title, reason=_url), section_id))
        except Exception as e:
            logger.warning("ai_image chart_predictions 更新失败: %s", e)

    if generated == 0:
        raise HTTPException(502, "所有 AI 配图生成失败（请检查图像模型配置或网络）")

    # ✅ 与 fix-mermaid 一致：同步刷新 word_count/word_status，避免正文与统计不一致
    from app.services.content_utils import word_status_for, text_word_count
    _wc = text_word_count(new_content)
    await db.execute(
        "UPDATE sections SET content=?, word_count=?, word_status=?, updated_at=? WHERE id=?",
        (new_content, _wc, word_status_for(_wc, word_budget),
         datetime.now().isoformat(), section_id))
    await db.commit()
    return {"generated": generated, "content_updated": True, "new_content": new_content}
