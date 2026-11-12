"""导出缓存·指纹与治理群（配置指纹/内容指纹/缓存行判据/缓存修剪/孤儿产物 GC，自 export.py 按导出阶段拆出，facade 延迟绑定，纯搬移零行为变更）"""

import posixpath
import xml.etree.ElementTree as ET
import zipfile

import app.routers.export as _sh  # noqa: F401 — facade 命名空间延迟绑定（monkeypatch 兼容）

_EXPORT_CONFIG_KEYS = frozenset(
    {
        "font_name",
        "font_size",
        "page_header",
        "page_footer",
        "show_page_number",
        "show_title_page",
        "show_toc",
        "bidder_name",
        "chapter_page_break",
        "page_number_style",
        "line_spacing",
        "toc_depth",
        "cover_info",
        "margins",
        "allow_pil_fallback",
        "chart_fail_placeholder",
        "heading_styles",
        "heading_border",
        # ✅ v15：内容自动改写开关（影响产物内容，必须纳入指纹，切换即失效旧缓存）
        "auto_rewrite_content",
        # ✅ 2026-09-22：未闭合围栏自动修复同样影响产物内容，纳入指纹
        "auto_fix_unclosed_fences",
        # ✅ v17：AI 配图导出期自动生成开关（默认 True，影响产物内容，纳入指纹）
        "ai_image_auto_generate",
        # ✅ 2026-10-02：专项施工方案法定前置表单（编制说明/审批表/专家论证报告/
        #    图纸附件清单）—— 影响产物内容，必须纳入指纹，否则切换开关后
        #    config_hash 不变 → 命中旧缓存 → 用户看不到刚打开的表单。
        "scheme_forms",
        # ✅ 2026-10-11：版式预设（行距 + 四边距整体切换）影响产物版式，纳入指纹。
        "layout_preset",
    }
)


def _normalize_config(config: dict) -> dict:
    """导出配置白名单规范化（用于指纹计算，防止缓存无谓击穿）。

    ✅ 优化：前端序列化带来的任意多余字段（调试字段 `_ts`、未知键、空值）
    都会改变 config_hash → 缓存永远不命中。此函数只保留白名单内、
    值非 None/空串 的键，并递归清洗嵌套 dict（heading_styles/margins/cover_info）。
    注意：须与 _prepare_export 读取的键保持同步。
    """
    if not isinstance(config, dict):
        return {}

    def _clean(d: dict) -> dict:
        cleaned: dict = {}
        for k, v in d.items():
            if not k or k.startswith("_") or v is None or v == "":
                continue
            if isinstance(v, dict):
                sub = _clean(v)
                if sub:
                    cleaned[k] = sub
                continue
            cleaned[k] = v
        return cleaned

    return {
        k: _clean(v) if isinstance(v, dict) else v
        for k, v in config.items()
        if k in _sh._EXPORT_CONFIG_KEYS and v is not None and v != ""
    }


def _image_generation_signature() -> dict:
    """导出期 AI 配图生成参数的指纹片段（参与 ``_content_fingerprint``）。

    ✅ BUG 修复（2026-09-27 · P1）：配图在导出期由
    ``_auto_generate_ai_image_blocks`` 真实生成，其像素结果由下列配置决定；
    这些键此前**不在** ``_EXPORT_CONFIG_KEYS``（那是前端 ``config`` 体白名单），
    也不在任何指纹字段里 → 换模型/换尺寸后指纹不变，命中旧缓存拿到旧模型的图。

    只取「影响像素」的键，**绝不取 api_key**（密钥变化不应让缓存失效，
    也不应把密文写进任何可被日志/响应头回显的指纹材料）。
    读配置失败一律降级为固定 dict，绝不阻断导出。
    """
    try:
        from app.config import settings as _s

        return {
            "enabled": bool(_s.image_enabled),
            # ✅ BUG-4（R175）：`image_provider` 决定实际调用哪家生成器
            #    （export.py 3089 读取），换厂但型号相同 → 像素完全不同，
            #    旧指纹漏收该键会命中旧图。
            "provider": str(getattr(_s, "image_provider", "") or ""),
            "model": str(getattr(_s, "image_model", "") or ""),
            "size": str(getattr(_s, "image_default_size", "") or ""),
            "base_url": str(getattr(_s, "image_base_url", "") or ""),
            # ✅ BUG-4（R175）：AI 配图**全局预算**（export.py:3036 读取）——
            #    `max_ai_images` 由 0（不限）改为 N 时，产物里保留哪些
            #    `ai_image` 占位块会变化（apply_image_budget 分段择优），
            #    属"真正影响产物内容"的服务端配置，必须参与指纹。
            "max_images": int(getattr(_s, "max_ai_images", 0) or 0),
        }
    except Exception:  # pragma: no cover - 配置不可用时退化为稳定值
        return {"enabled": False, "provider": "", "model": "", "size": "", "base_url": "", "max_images": 0}


def _content_fingerprint(prep: dict) -> tuple[str, str]:
    """计算 (config_hash, content_fingerprint)。

    ✅ BUG 修复：旧指纹只含 (章节 id, 正文)。**改标题 / 调整章节顺序 / 移动层级**
    都不会改变指纹 → 命中旧缓存返回"改标题之前"的文档。现纳入标题、层级、
    父级与排序序号；另将前端渲染签名与导出器版本一并纳入指纹。

    ✅ 修复（2026-10-05 · 高风险回归 R-1）：前端 mermaid.js 渲染的 PNG 实际字节
    进入指纹。旧实现仅哈希 mermaid_code，若前端渲染结果（如缩放/DPI/主题/版本）
    与 code 相同而像素不同（浏览器兼容、样式变更、并发竞态拿错图），指纹不变
    → 命中旧 DOCX 缓存拿到旧像素图；此处对每个 fe_images 项计算 md5 后并入
    "fe_render_pixels" 字段（与既有 "fe_render"=codes 维度并列，不破坏向后兼容）。

    ✅ 修复（2026-10-05 · 高风险回归 R-2）：``prep["image_config"]`` 覆盖
    ``_image_generation_signature()`` 中受影响键。旧实现仅读取服务器 settings，
    测试环境 monkeypatch 到 prep（或未来前端传入 per-request 覆盖配置）后指纹不变
    → 换模型/尺寸仍命中旧缓存。合并策略：先取 settings 签名（保留生产路径），
    再按 prep["image_config"] 逐项覆盖 model / size（api_key 永不进指纹）。
    """
    # ✅ 缓存失效修复（2026-09-23）：渲染器版本号纳入指纹（见下方 "renderer" 键）。
    from app.services.ai.mermaid_renderer import _RENDERER_VERSION

    config = _sh._normalize_config(prep["config"])
    fe_sig = ""
    if prep["fe_codes"]:
        fe_sig = _sh.hashlib.md5("\n---\n".join(sorted(prep["fe_codes"])).encode()).hexdigest()[:12]
    # R-1：前端像素指纹（同一 mermaid_code 也可能对应不同 PNG 字节）
    fe_pixels_sig = ""
    _fe_images = prep.get("fe_images") or {}
    if _fe_images:
        _parts: list[str] = []
        for _code_key, _b in _fe_images.items():
            try:
                _bio = _b if isinstance(_b, _sh.BytesIO) else _sh.BytesIO(bytes(_b))
                _payload = _bio.getvalue()
                _bio.seek(0)
                _parts.append(f"{_code_key}|{_sh.hashlib.md5(_payload).hexdigest()}")
            except Exception:
                _sh.logger.warning(
                    "前端渲染像素指纹采集失败（该图不入指纹，换图后可能命中旧缓存）:" " key=%s",
                    _code_key,
                    exc_info=True,
                )
                continue
        if _parts:
            fe_pixels_sig = _sh.hashlib.md5("\n---\n".join(sorted(_parts)).encode()).hexdigest()[:16]
    # ✅ BUG 修复（2026-10-11 · 跨模块数据链 R70 · 服务端开关不入指纹）：
    #    `body_subheading_demote_with_children` 直接决定渲染端的编号口径 ——
    #    开启时「有 DB 子章节」的正文子标题被降级为节内编号（1）/ a）/ …），
    #    **产物里的标题层级与编号随之改变**（:579/:710/:3736 三处读取）。
    #    但它取自服务端 settings，不在 `_EXPORT_CONFIG_KEYS`（那是**请求体**
    #    白名单，而该键根本不由前端传入）内 ⇒ 运维切换此开关后 config_hash 与
    #    content_fingerprint 均不变 → 命中旧缓存，用户改了配置却拿到降级前的文档。
    #    与 `scheme_forms`「切换开关必须失效缓存」同一类缺陷，现显式纳入指纹。
    #    默认值 True 与渲染端 `except: _demote = True` 的兜底口径保持一致。
    try:
        from app.config import settings as _demote_settings

        _demote_on = bool(getattr(_demote_settings, "body_subheading_demote_with_children", True))
    except Exception:  # pragma: no cover - 配置不可用时沿用渲染端同款兜底
        _demote_on = True
    # ✅ BUG-4（R175）：补充附录开关（服务端 settings）—— 与渲染端
    #    export.py `_load_appendix_sources` 同一读取口径，不可用按关闭。
    try:
        from app.config import settings as _apx_settings

        _appendix_on = bool(getattr(_apx_settings, "export_appendix_sources", False))
    except Exception:  # pragma: no cover
        _appendix_on = False
    # R-2：合并 prep["image_config"] 覆盖服务器 settings（保持 settings 路径不变）
    _img_sig = _sh._image_generation_signature()
    _img_cfg = prep.get("image_config") or {}
    if isinstance(_img_cfg, dict):
        _mc = _img_cfg.get("image_model")
        if _mc is not None:
            _img_sig["model"] = str(_mc)
        _ms = _img_cfg.get("image_default_size") or _img_cfg.get("image_size")
        if _ms is not None:
            _img_sig["size"] = str(_ms)
    content_hash = _sh.hashlib.md5(
        _sh.json.dumps(
            {
                # ✅ BUG 修复（2026-09-18）：封面标题/页眉取自 schemes.name，旧指纹未含
                #    方案名 → 改名后正文与配置未变会命中旧缓存，交付文档封面仍是旧名。
                "scheme": (
                    (prep.get("scheme") or {}).get("name", ""),
                    (prep.get("scheme") or {}).get("project_id", ""),
                ),
                "sections": [
                    (
                        s["id"],
                        s.get("parent_id", ""),
                        s.get("sort_order"),
                        s.get("level"),
                        s.get("title", ""),
                        s.get("content") or "",
                    )
                    for s in prep["sections"]
                ],
                "charts": sorted(prep["chart_fp"]),
                # ✅ 修复（2026-09-17）：事实变更应触发缓存更新（此前指纹未含事实，
                # invalidate_export_cache 形同空转）。用 .get 防御部分调用方拼装的
                # 最小 prep（缺该键视为无事实），避免 _content_fingerprint 抛 KeyError。
                "global_facts": [
                    (f.get("gt", ""), f.get("title", ""), f.get("content") or "")
                    for f in (prep.get("global_facts") or [])
                ],
                # ✅ D-3：补充附录内容纳入指纹 —— 否则开关打开/数据源变更后
                #    指纹不变，会永久命中不含附录的旧产物（与 2026-09-17 事实变更
                #    不失效是同一类缺陷）。用 .get 防御最小 prep。
                "appendix_sources": [
                    (g.get("title", ""), [(i.get("name", ""), i.get("value", "")) for i in (g.get("items") or [])])
                    for g in (prep.get("appendix_sources") or [])
                ],
                # 事实查询失败与正常空事实必须使用不同的缓存键。
                "global_facts_status": prep.get("global_facts_status") or {},
                "config": config,
                "fe_render": fe_sig,
                # R-1：前端 PNG 像素字节指纹（补 fe_render 只看 code 的盲区）
                "fe_render_pixels": fe_pixels_sig,
                # ✅ R226（A7）：封面落款日期（构建日）写进产物但不进指纹 →
                #    跨天命中昨日产物、封面日期停在上一导出日。按「天」粒度纳入：
                #    同一天内缓存照常命中，跨天自然失效（产物确实变了）。
                "export_date": _sh.datetime.now().strftime("%Y-%m-%d"),
                "exporter": _sh._EXPORTER_VERSION,
                # ✅ 缓存失效修复（2026-09-27 · P1）：配图侧的生成参数纳入指纹。
                #    mermaid 侧早已用 _RENDERER_VERSION 解决"渲染器变了旧图被烤进二进制"，
                #    但图片侧遗漏：用户在 AI 配置里换 image_model / 改 image_default_size
                #    后再导出，content_fingerprint 不变 → 命中旧缓存拿到**旧模型生成的配图**，
                #    且响应头不提示任何异常。此处显式纳入这几个真正影响像素结果的键。
                # R-2：使用已合并 prep["image_config"] 覆盖的 _img_sig。
                "image_gen": _img_sig,
                # ✅ R70（2026-10-11）：正文子标题降级开关（服务端 settings，非请求体）
                #    —— 直接改变产物编号层级，必须参与指纹，否则切换后命中旧产物。
                "subheading_demote": _demote_on,
                # ✅ BUG-4（R175）：补充附录开关（服务端 settings，export.py:3721）——
                #    「开启但知识库/提取成果为空」时 appendix_sources 仍为 []，与关闭态
                #    指纹相同 → 会命中"没有补充附录"的旧产物。显式纳入开关本身。
                "appendix_sources_on": _appendix_on,
                # ✅ 缓存失效修复（2026-09-23）：纯渲染逻辑改进（mermaid/PIL 渲染器升级，
                #    _RENDERER_VERSION 未变但逻辑已变）也应失效 DOCX 缓存，否则旧图被烤进
                #    二进制后永远命中。图表数据(data_json)变化固然会让 chart_fp 改变，
                #    但渲染器自身变化不反映在数据里，须显式纳入指纹。
                "renderer": _RENDERER_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    config_hash = _sh.hashlib.md5(_sh.json.dumps(config, sort_keys=True).encode()).hexdigest()
    return config_hash, content_hash


# ✅ BUG 修复（2026-09-27 · P0）：本装饰器此前**错贴在本辅助函数上**，
#    而真正的导出端点 ``export_docx`` 没有任何装饰器 →
#    ``POST /sches/{id}/export/docx`` 实际返回 ``"不一致章节数=0"`` 这一 9 字节 JSON
#    字符串，前端 ``exportApi.docx`` 以 ``responseType:"blob"`` 接收 →
#    用户下载到一个打不开的「.docx」，**DOCX 导出功能整体失效**。
#    回归护栏见 tests/test_audit_fixes_20260927.py::TestExportRouteRegistration。
def _summarize_numbering_consistency(report: dict) -> str:
    """将编号一致性报告压缩为单行可读摘要（用于严格模式 409 详情）。"""
    parts = []
    for sec in report.get("sections", []):
        if sec.get("consistent") or sec.get("skipped"):
            continue
        sample = ""
        for d in (sec.get("diffs") or [])[:1]:
            sample = " | ".join(d.get("new", [])) if d.get("new") else ""
        parts.append(f"[{sec.get('section_id', '?')[:8]}]{sample}")
    return "; ".join(parts) or f"不一致章节数={report.get('mismatched', 0)}"


async def _guard_numbering_consistency(db, scheme_id: str) -> dict:
    """✅ 编号统一（2026-09-26 · 显式跨校验器）：导出前校验落库编号与真实树一致。
    DOCX 与 PDF 两条链路共用（均经此守卫，避免"改了一处漏一处"）。

    严格模式（numbering_consistency_strict）直接 409 阻断产出不一致文档
    （CI 校验场景）；默认仅告警——导出成稿本身仍由 _compute_subheading /
    heading_gen 按结构树重算，落库正文可用 repair 接口修复。
    校验器自身异常绝不阻断导出。

    ✅ R183（2026-11-07 · 严格模式盲区）：此前 strict 分支只调用
    `validate_scheme_content_numbering`（正文子标题 vs outline_json.id），
    当 outline_json.id 本身陈旧（例如「第 2 章存储编号被改成 1」）而正文又按
    这份陈旧编号重写时，正文校验判绿 → 严格模式放行，与导出侧「按树重算」
    的结论分叉。现严格模式同时只读复用 `sections._outline_structure_report`
    （与 `renumber_sections_after_reorder` 同一 `_build_tree` +
    `renumber_section_outline_ids` 算法），确保「阻断判定」与「导出重算」同源。

    ✅ R183-补（2026-11-07 · 自检崩溃不得伪装成「结构一致」）：本函数已是
    第三个调用 `_outline_structure_report` 的点（目录路由 / 审核 DLV-13 / 导出）。
    旧实现自检异常时只写 debug 日志、返回 None → 响应里没有任何痕迹，
    「没检查」被当成「没问题」。现在返回值固定为 ``{"outline": {...}}``：

      · 自检成功：``checked=True``，携带 consistent / mismatched / chapters；
      · 自检失败（异常或符号不可用）：``checked=False``、``consistent=None``
        且带 ``reason``，同时写 WARNING 日志；**不阻断导出**（自检崩了不能
        害用户导不出），但结论不得显示成结构一致。

    非严格模式返回 ``{}``（本就不做结构自检，不发任何结论头）。
    """
    try:
        from app.config import settings as _settings

        _strict = bool(_settings.numbering_consistency_strict)
    except Exception:
        _sh.logger.debug("读取 numbering_consistency_strict 失败（沿用默认非严格模式）", exc_info=True)
        _strict = False
    if _strict:
        from app.services.numbering import validate_scheme_content_numbering

        _rep = await validate_scheme_content_numbering(db, scheme_id)
        _outline = None
        _outline_reason = ""
        try:
            # 只读复用，不复制实现：结构自检的唯一事实源在 sections.py。
            from app.routers.sections import _outline_structure_report
        except Exception as _e:  # noqa: BLE001 — 符号不可用同样必须可见
            _outline_reason = f"结构自检不可用（符号导入失败）：{_e}"
            _sh.logger.warning(
                "导出前结构编号自检不可用（不阻断导出，结论标记 checked=false）: %s", _e, exc_info=True
            )
        else:
            try:
                _outline = await _outline_structure_report(db, scheme_id)
            except Exception as _e:  # noqa: BLE001 — 自检异常不阻断，但必须可见
                _outline_reason = f"结构自检执行异常：{_e}"
                _sh.logger.warning(
                    "导出前结构编号自检失败（不阻断导出，结论标记 checked=false）: %s", _e, exc_info=True
                )
        if _outline is not None:
            _outline_detail = {
                "checked": True,
                "consistent": bool(_outline.get("consistent")),
                "mismatched": int(_outline.get("mismatched") or 0),
                "chapters": int(_outline.get("checked") or 0),
                "unverifiable": int(_outline.get("unverifiable") or 0),
                "samples": _outline.get("samples", []),
                # ✅ R188（L4）：环内不可达节点加法式透传，供严格模式判红。
                "unmounted": _outline.get("unmounted") or [],
                "unmounted_count": int(_outline.get("unmounted_count") or 0),
            }
        else:
            _outline_detail = {
                "checked": False,
                "consistent": None,
                "mismatched": None,
                "reason": _outline_reason or "结构自检未执行（原因未知）",
            }
        _body_bad = not _rep["consistent"]
        _outline_bad = bool(_outline_detail["checked"] and not _outline_detail["consistent"])
        # ✅ R188（L4）：环内不可达节点会被 _build_tree 静默丢弃，既有的
        #    consistent/mismatched 都判绿，但 write_section 根本不写这些章 →
        #    DOCX 静默缺章。严格模式据此拒绝导出并给出可行动说明。
        _unmounted_bad = bool(_outline_detail.get("unmounted_count"))
        if _body_bad or _outline_bad or _unmounted_bad:
            _summary = _summarize_numbering_consistency(_rep)
            if _outline_bad:
                _outline_note = f"结构编号与树位置不一致={_outline_detail['mismatched']} 章（stored≠树位置）"
                _summary = f"{_summary}；{_outline_note}" if _body_bad else _outline_note
            if _unmounted_bad:
                _unmounted_note = (
                    f"目录树不可达节点={_outline_detail['unmounted_count']} 章"
                    "（parent_id 成环/悬挂，导出会缺章）"
                )
                _summary = f"{_summary}；{_unmounted_note}" if _summary else _unmounted_note
            detail = {
                "error": "numbering_inconsistency",
                "mismatched": _rep["mismatched"],
                "summary": _summary,
                "outline": _outline_detail,
            }
            raise _sh.HTTPException(409, detail=detail)
        return {"outline": _outline_detail}
    else:
        try:
            from app.services.numbering import validate_scheme_content_numbering

            _rep = await validate_scheme_content_numbering(db, scheme_id)
            if _rep.get("failed"):
                # ✅ 2026-11-05（R2 日志驱动）：校验器读取失败时返回 failed=True +
                #    mismatched=0，旧实现会打出「发现 0 章不一致」的误导日志；
                #    现与「校验通过」显式区分（fail-closed 结论如实记录）。
                _sh.logger.warning(
                    "导出前编号一致性校验读取失败（按未通过处理，不影响导出）: %s", _rep.get("error") or "unknown"
                )
            elif not _rep["consistent"]:
                _sh.logger.warning(
                    "导出前编号一致性校验发现 %d 章落库正文与目录编号不一致（scheme=%s），"
                    "建议调用 /numbering-consistency/repair 或触发结构重排以同步。"
                    "不一致章节（前 5 个）：%s",
                    _rep["mismatched"],
                    scheme_id[:8],
                    "; ".join(_summarize_numbering_consistency(_rep).split("; ")[:5]),
                )
        except Exception as _e:  # noqa: BLE001
            _sh.logger.debug("编号一致性预检失败（不影响导出）: %s", _e)
        return {}


#: ZIP 完整性校验结果记忆化（键 = (路径, 大小, mtime_ns)），避免每次命中/裁剪
#: 都重新解压整份 DOCX。✅ BUG-3（R175）：此前可用判据只看 `st_size > 0`，
#: 一次写到一半被杀进程 / 磁盘满留下的**截断 DOCX**（size>0 但中央目录缺失或
#: CRC 坏）会被判为「可用缓存」，命中后直接返回给用户 —— 正是用户看到的
#: 「文档打不开」。现对 .docx 做真实完整性校验（`zipfile.testzip()`），
#: 结果按 (路径, 大小, mtime) 记忆化：同一文件只解压一次，文件一旦被重写
#: 大小/mtime 变化 → 自动重新校验，绝不出现「旧结论套用到新文件」。
_ZIP_VERIFIED: dict[tuple[str, int, int], bool] = {}
_ZIP_VERIFIED_LOCK = _sh.threading.Lock()
_ZIP_VERIFIED_MAX = 1024

# ---------------------------------------------------------------------------
# ✅ R183（2026-11-07 · 用户硬要求）：DOCX 包结构级校验
# ---------------------------------------------------------------------------
# 背景：R175 的 `_zip_integrity_ok` 只做 `testzip()` + 「两个文件名存在」，
# 对「CRC 全好但 OOXML 包结构损坏」的文件一律放行。实测四类可复现误判
# （见 tests/test_export_docx_ooxml_structure_r183_20261107.py）：
#   ① [Content_Types].xml 为空壳 `<Types/>`（Word/WPS 打开即报损坏）；
#   ② [Content_Types].xml 本身不是合法 XML；
#   ③ word/document.xml 被截断成非法 XML；
#   ④ document.xml 在 [Content_Types].xml 中没有任何 ContentType 声明。
# 这四类文件作为「缓存命中」交付时，用户看到的就是「导出文档无法打开」。
# 现按 OPC / ECMA-376 最小充分判据逐项校验：ZIP CRC、必需部件、全部
# XML/RELS 可解析、Content_Types 覆盖与主文档 MIME、关系目标存在性、
# 关键根元素命名空间。判据失败 → 该产物不可作为缓存命中（触发重建）。

_DOCX_REQUIRED_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
    "word/_rels/document.xml.rels",
)
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_WML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WML_DOCUMENT_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_REL_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"


def _rels_target_part(base_rels_part: str, target: str) -> str:
    """把关系文件里的相对 Target 解析为包内部件名（空串 = 无法解析）。"""
    tgt = (target or "").split("#", 1)[0].strip()
    if not tgt:
        return ""
    if tgt.startswith("/"):
        return posixpath.normpath(tgt.lstrip("/"))
    base_dir = posixpath.dirname(posixpath.dirname(base_rels_part))
    return posixpath.normpath(posixpath.join(base_dir, tgt))


def _docx_package_problems(path) -> list[str]:
    """返回 DOCX 包结构问题列表（空列表 = 通过 OOXML 结构级校验）。

    只读、不修改文件；任何异常都收敛为一条问题而不是抛出 —— 调用方按
    「不可用缓存」处理（fail-closed：校验不了就不交付）。
    """
    problems: list[str] = []
    try:
        with zipfile.ZipFile(str(path)) as zf:
            bad_member = zf.testzip()
            if bad_member is not None:
                problems.append(f"zip-crc:{bad_member}")
            names = set(zf.namelist())
            for req in _DOCX_REQUIRED_PARTS:
                if req not in names:
                    problems.append(f"missing-part:{req}")

            # 1) 全部 XML / RELS 部件必须良构（截断的 document.xml 在此暴露）
            for name in sorted(names):
                if not (name.endswith(".xml") or name.endswith(".rels")):
                    continue
                try:
                    with zf.open(name) as fh:
                        ET.parse(fh)
                except Exception as exc:  # noqa: BLE001 — 逐部件记录，不中断
                    problems.append(f"xml-invalid:{name}:{type(exc).__name__}")

            # 2) Content_Types 覆盖 + 主文档 MIME
            if "[Content_Types].xml" in names:
                try:
                    ct_root = ET.fromstring(zf.read("[Content_Types].xml"))
                    if ct_root.tag != f"{{{_CT_NS}}}Types":
                        problems.append("ct-root-invalid")
                    defaults = {
                        (el.get("Extension") or "").lower()
                        for el in ct_root.findall(f"{{{_CT_NS}}}Default")
                    }
                    overrides: dict[str, str] = {}
                    for el in ct_root.findall(f"{{{_CT_NS}}}Override"):
                        part = (el.get("PartName") or "").lstrip("/")
                        if part:
                            overrides[part] = el.get("ContentType") or ""
                    for name in sorted(names):
                        if name == "[Content_Types].xml" or name.endswith(".rels"):
                            continue
                        ext = posixpath.basename(name).rsplit(".", 1)[-1].lower() if "." in posixpath.basename(name) else ""
                        if name in overrides or (ext and ext in defaults):
                            continue
                        problems.append(f"ct-uncovered:{name}")
                    if overrides.get("word/document.xml") != _WML_DOCUMENT_CT:
                        problems.append("main-part-mime-invalid")
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"ct-unparsable:{type(exc).__name__}")

            # 3) 关系目标存在性 + 与主文档的关系
            for name in sorted(names):
                if not name.endswith(".rels"):
                    continue
                try:
                    rel_root = ET.fromstring(zf.read(name))
                    if rel_root.tag != f"{{{_REL_NS}}}Relationships":
                        problems.append(f"rels-root-invalid:{name}")
                    rel_ids: set[str] = set()
                    for rel in rel_root.findall(f"{{{_REL_NS}}}Relationship"):
                        rid = rel.get("Id") or ""
                        if rid in rel_ids:
                            problems.append(f"rels-duplicate-id:{name}:{rid}")
                        rel_ids.add(rid)
                        if (rel.get("TargetMode") or "").lower() == "external":
                            continue
                        resolved = _rels_target_part(name, rel.get("Target") or "")
                        if not resolved or resolved not in names:
                            problems.append(f"rels-target-missing:{name}:{rel.get('Target') or ''}")
                        if name == "_rels/.rels" and (rel.get("Type") or "") == _REL_OFFICE_DOCUMENT:
                            if resolved != "word/document.xml":
                                problems.append(f"office-document-target-invalid:{resolved}")
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"rels-unparsable:{name}:{type(exc).__name__}")

            # 4) 关键根元素命名空间（防止扩展名对、内容却是别的东西）
            if "word/document.xml" in names:
                try:
                    doc_root = ET.fromstring(zf.read("word/document.xml"))
                    if doc_root.tag != f"{{{_WML_NS}}}document":
                        problems.append("document-root-invalid")
                except Exception:  # noqa: BLE001 — 已在第 1 步记录
                    pass
    except Exception as exc:  # noqa: BLE001 — 打不开包 = 结构性不可用
        problems.append(f"package-unreadable:{type(exc).__name__}")
    return problems[:20]



def _zip_integrity_ok(path, size: int, mtime_ns: int) -> bool:
    """DOCX（ZIP 容器）是否通过 OOXML 结构级校验；结果记忆化（线程安全）。"""
    key = (str(path), int(size), int(mtime_ns))
    with _ZIP_VERIFIED_LOCK:
        cached = _ZIP_VERIFIED.get(key)
    if cached is not None:
        return cached
    problems: list[str] = []
    try:
        # ✅ R183：不只是 CRC —— 结构损坏但 CRC 正常的包同样「打不开」。
        problems = _docx_package_problems(path)
        ok = not problems
    except Exception:  # BadZipFile / OSError / CRC 异常
        ok = False
    if not ok:
        _sh.logger.warning(
            "DOCX 产物未通过 OOXML 结构校验（按不可用缓存/不可交付处理）: %s | %s",
            path,
            "; ".join(problems[:8]) or "unknown",
        )
    with _ZIP_VERIFIED_LOCK:
        if len(_ZIP_VERIFIED) >= _ZIP_VERIFIED_MAX:
            _ZIP_VERIFIED.clear()
        _ZIP_VERIFIED[key] = ok
    return ok


#: PDF 完整性校验结果记忆化（键 = (路径, 大小, mtime_ns)），避免每次命中/裁剪
#: 都重读整份 PDF。✅ B1-3（R176）：与 DOCX 的 `_ZIP_VERIFIED` 同型，文件被重写后
#: 大小/mtime 变化 → 自动重新判定，绝不把旧结论套用到新文件。
_PDF_VERIFIED: dict[tuple[str, int, int], bool] = {}
_PDF_VERIFIED_LOCK = _sh.threading.Lock()
_PDF_VERIFIED_MAX = 512


def _pdf_integrity_ok(path, size: int, mtime_ns: int) -> bool:
    """PDF 是否结构完整（可解析的最小判据）；结果记忆化（线程安全）。

    ✅ B1-3（R176 · 2026-11-04）：旧实现 .pdf 只做 `size>0` —— 写盘中断/磁盘满
    留下的**截断 PDF**（size>0 但缺文件头或 trailer）会被判「可用缓存」命中并
    直接 FileResponse 交付，用户拿到打不开的文档。不引入 PDF 解析依赖
    （部署环境不保证有 pypdf），改用规范规定的结构判据：
      · 文件头 1024 字节内出现 ``%PDF-``（规范允许前导垃圾，故不限定偏移 0）；
      · 文件尾 2048 字节内出现 ``%%EOF``（正常与增量更新的 trailer 均在末尾）。
    两处同时满足才认为可解析；判据失败 → 不可命中（调用方重建产物）。
    """
    key = (str(path), int(size), int(mtime_ns))
    with _PDF_VERIFIED_LOCK:
        cached = _PDF_VERIFIED.get(key)
    if cached is not None:
        return cached
    ok = False
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
            if b"%PDF-" in head:
                f.seek(max(0, int(size) - 2048))
                ok = b"%%EOF" in f.read()
    except OSError:
        ok = False
    with _PDF_VERIFIED_LOCK:
        if len(_PDF_VERIFIED) >= _PDF_VERIFIED_MAX:
            _PDF_VERIFIED.clear()
        _PDF_VERIFIED[key] = ok
    return ok


def _cache_row_viable(result_path: str) -> bool:
    """缓存行是否仍可用：result_path 非空、文件存在、非空且**结构完整**。

    ✅ BUG 修复（2026-10-14 · P2 判据不一致）：命中判据（export_docx / export_pdf）
    要求 `Path(...).stat().st_size > 0`，而本函数的两处判据只查 `.exists()`：
      · 一份 0 字节（写盘中断 / 磁盘满）的产物既永远不会被返回给用户（命中判据拦住），
        又被本函数当成「有效缓存」占用 5 份保留配额；
      · 于是它会在裁剪时把**更新且可用**的缓存行挤出保留窗口，
        用户表现为「明明没改内容，缓存却突然失效、重新付一次全量渲染成本」。
    抽成共享判据，使「可用缓存」在读写两侧同一口径。

    ✅ BUG-3（R175）：在 size>0 之上补真实完整性校验（.docx 走 ZIP 校验；
    .pdf 只做非空判据——PDF 无 ZIP 结构，越界校验不在本项范围）。

    ✅ B1-3（R176 · 2026-11-04）：补齐 .pdf 等价判据（``_pdf_integrity_ok``：
    头部 ``%PDF-`` + 尾部 ``%%EOF``）—— 旧实现 .pdf 直接 ``return True``，
    「size>0 的截断 PDF」会被当作可用缓存命中并交付。其余后缀保持原行为。

    🧵 线程安全（R176 补漏 · 2026-11-04）：本函数被缓存**命中分支**放在
    ``asyncio.to_thread`` 中执行（ZIP 整包 CRC / PDF 头尾读是同步阻塞）。
    两处记忆化（``_ZIP_VERIFIED`` / ``_PDF_VERIFIED``）的读与写各自持
    ``threading.Lock``：并发首次命中最多重复一次校验（同键同文件，结论一致，
    不会写坏 dict），超限 ``clear()`` 亦持锁；键含 ``(路径, 大小, mtime_ns)``，
    文件被重写即自动重新判定，绝不套用旧结论。
    """
    if not result_path:
        return False
    try:
        p = _sh.Path(result_path)
        if not p.is_file():
            return False
        st = p.stat()
        # 保留 `st_size > 0` 写法：既有护栏测试 test_viable_check_includes_size_guard
        # 以该子串作为判据锚点（R175 只追加完整性校验，不取消既有判据）。
        if not (st.st_size > 0):
            return False
    except OSError:
        return False
    _suffix = p.suffix.lower()
    if _suffix == ".docx":
        return _zip_integrity_ok(p, st.st_size, st.st_mtime_ns)
    if _suffix == ".pdf":
        # ✅ B1-3：PDF 走等价结构判据（不可解析/截断 → 判不可用 → 调用方重建）
        return _pdf_integrity_ok(p, st.st_size, st.st_mtime_ns)
    return True


async def _prune_export_cache(db, scheme_id: str, protect_path: _sh.Path) -> None:
    """✅ 缓存保留策略（最多 5 份/方案，与前端 tooltip 说明对齐）——
    删除陈旧/僵尸行并连带清理磁盘文件。2026-10-03（T1）：从 DOCX 分支内联逻辑
    提取为共享 helper —— PDF 分支此前只 INSERT 从不裁剪，DB 行与 EXPORTS_DIR
    磁盘文件随导出次数无限增长；现两分支共用同一保留策略（跨格式合计 5 份）。

    ✅ BUG 修复（保留策略误删新产物）：created_at 精度只到秒，同一秒内插入多行时
    `ORDER BY created_at DESC` 次序不稳定 —— 刚插入的本行可能被排到第 5 位之后，
    其磁盘文件随即被当作"陈旧缓存"删除，紧接着的 FileResponse 就会 404/500。
    故增加 rowid 次序兜底（rowid 单调递增，等价于插入顺序），并显式保护当前产物。
    R13 守卫：db.execute 返回 None（连接/事务异常）时 fail-soft 跳过裁剪。
    """
    cur = await db.execute(
        "SELECT id, result_path FROM export_cache WHERE scheme_id=?" " ORDER BY created_at DESC, rowid DESC",
        (scheme_id,),
    )
    cache_rows = [dict(r) for r in await _sh._db_fetch_all(cur, what="_prune_export_cache")]
    if not cache_rows:
        # 无行 = 查询失败或本方案确无缓存；两种情况都无需裁剪（fail-soft）
        return
    keep_paths = {str(protect_path)}
    # ✅ BUG-6（R175）：「正在读取」租约内的产物不得删除 —— DB 行照常清理，
    #    但磁盘文件留到租约过期后由孤儿 GC 兜底（保证下载中的响应不会读到半截）。
    leased = _sh._export_read_lease_snapshot()
    for cr in cache_rows[:5]:
        if _sh._cache_row_viable(cr.get("result_path") or ""):
            keep_paths.add(cr["result_path"])
    stale_ids: list[str] = []
    for i, cr in enumerate(cache_rows):
        # ✅ BUG 修复（2026-09-20）：result_path 为 NULL 时 Path(None) 抛 TypeError；
        #    为空串时 Path("") 等价 Path(".") 恒存在 → 僵尸行被当成「有效缓存」。
        # ✅ 修复（2026-10-14 · P2）：改用 _cache_row_viable（含 st_size>0），
        #    与 export_docx / export_pdf 的命中判据同一口径 —— 0 字节僵尸产物
        #    不再占用 5 份保留配额。
        rp = cr.get("result_path") or ""
        if i >= 5 or not _sh._cache_row_viable(rp):
            stale_ids.append(cr["id"])
    if stale_ids:
        # 删除记录前把磁盘文件一并清理（保留下来的 5 份 + 当前产物不删）
        for cr in cache_rows:
            rp = cr.get("result_path") or ""
            if cr["id"] in stale_ids and rp and rp not in keep_paths:
                if _sh.os.path.normcase(str(rp)) in leased:
                    continue
                # ✅ R176 · D2-2：产物与其审计旁车同生共死（单一删除出口）。
                _sh._remove_export_artifact(rp)
        placeholders = ",".join("?" * len(stale_ids))
        await _sh._db_exec(
            db, f"DELETE FROM export_cache WHERE id IN ({placeholders})", stale_ids, what="_prune_export_cache 清理"
        )


#: 孤儿临时产物的宽限期（秒）。DOCX 原子替换失败会降级为「本次不写缓存」，
#: 返回给用户的产物仍是那个 ``.tmp.docx`` —— 而 FileResponse 在 handler
#: 返回**之后**才开始流式读取该文件，因此临时产物必须留一个宽限期，否则
#: 下一次导出就会把正在被读取的临时产物顺手清掉。正常交付产物
#: （``.docx`` / ``.pdf``）不需要宽限：它们要么被现存缓存行引用（不会命中
#: 孤儿判定），要么本身就是孤儿（可以立即清理）。
_ORPHAN_TMP_TTL_SECONDS = 600


async def _gc_orphan_exports(db, scheme_id: str, protect: set[_sh.Path] | None = None) -> int:
    """✅ 孤儿导出产物回收（2026-10-06）。

    背景：``_prune_export_cache`` 只能清理「DB 有行但已陈旧」的文件，而下面
    两类产物**在 DB 里根本没有行**，于是永久留在 ``EXPORTS_DIR``，随导出
    次数累积且零告警：

      · DOCX 原子替换 3 次重试均失败 → 降级为「本次不写缓存」，
        ``out_path = tmp_out_path``（``.tmp.docx``）直接返回给用户，之后再无人清理；
      · PDF 分支 ``os.replace`` 成功、但随后的 INSERT 抛异常 → ``out_path``
        已在盘上，DB 里没有对应行（``/cache-status`` 也不会显示它）。

    一份 DOCX 可达数十 MB，每失败一次就永久泄漏一份。

    判定三重收窄（宁可少删，绝不误删）：

      ① 只扫 ``EXPORTS_DIR`` 的**顶层文件**，子目录整体跳过 ——
         图表 PNG 缓存目录 ``charts/`` 绝不受影响；
      ② 只处理文件名以 ``{scheme_id}_`` 起头的文件 —— 导出器只创建这种
         命名，其它方案的产物天然不在范围内（跨方案误删在结构上不可能）；
      ③ 排除本方案**现存缓存行**指向的路径，以及调用方传入的 ``protect``
         （本次导出仍在被响应读取的产物）。

    ``.tmp.*`` 额外要求年龄超过 :data:`_ORPHAN_TMP_TTL_SECONDS`（见其注释）；
    非临时产物只要没有缓存行引用即为孤儿，直接清理。

    全程 fail-soft：任何异常只记 WARNING 并返回已删除数，**绝不**影响本次
    导出交付。返回删除的文件数（0 = 无孤儿、或全部被判定为受保护）。

    ⚠️ **缓存行读取失败时整体跳过，而不是按「无缓存行」继续**：本函数是
    唯一的**破坏性**清理路径。若 DB 短暂不可用（R13：``db.execute`` 返回
    None）而 ``live`` 恰好为空集，孤儿判定就会把本方案**所有**现存产物判成
    孤儿 —— 用户下次导出秒级缓存全部作废，数十 MB 的成稿被静默删掉。
    因此这里用 :func:`_db_fetch_all` 的 ``strict=True`` 模式（唯一出口不变，
    但读失败抛 :class:`DBReadError` 而非降级为空集），只有成功读到结果集
    才允许删除。
    """
    import os
    import time

    deleted = 0
    try:
        # 先确认「哪些产物仍被缓存行引用」；读不到就整体跳过（保守不删）。
        try:
            rows = await _sh._db_fetch_all(
                await db.execute("SELECT result_path FROM export_cache WHERE scheme_id=?", (scheme_id,)),
                what="_gc_orphan_exports 缓存行",
                strict=True,
            )
        except _sh.DBReadError as e:
            _sh.logger.warning(
                "导出：孤儿回收跳过（无法确认哪些产物仍被缓存行引用，" "保守不动任何文件；scheme=%s）: %s",
                scheme_id[:8],
                e,
            )
            return deleted
        live = {os.path.normcase(str(dict(r).get("result_path") or "")) for r in rows if dict(r).get("result_path")}
        protected = {os.path.normcase(str(p)) for p in (protect or set())}
        # ✅ BUG 修复（2026-10-14 · P0）：跳过「在飞产物」。不同配置指纹的并发导出
        #    持不同的去重锁，可在本函数执行期间正把产物 os.replace 到最终路径、
        #    但 INSERT+commit 尚未发生 —— 此时它既不在 live（DB 无行）、也不在任何
        #    调用方的 protect 里，且文件名不含 .tmp. → 会被误判孤儿 unlink，
        #    导致对端请求 500/404 或缓存行永久指向已删文件。
        #    见模块内 _EXPORT_INFLIGHT_PATHS 的注释（在同模块下方定义，运行时解析）。
        inflight = _sh._export_inflight_snapshot()
        # ✅ BUG-6（R175）：租约内的产物正在被响应读取，判孤儿也不得删。
        leased = _sh._export_read_lease_snapshot()
        now = time.time()
        prefix = f"{scheme_id}_"
        try:
            entries = list(_sh.EXPORTS_DIR.iterdir())
        except OSError:
            return deleted
        for f in entries:
            try:
                if not f.is_file() or not f.name.startswith(prefix):
                    continue
                key = os.path.normcase(str(f))
                # ✅ R176 · D2-2：旁车不是独立产物 —— 它的生死完全跟随宿主产物。
                #    宿主仍在 live/protected/inflight/leased 时必须保留；宿主已不存在
                #    则旁车必为孤儿，就地清理（宿主被删时也会连带删它）。
                if f.name.endswith(_sh._AUDIT_SIDECAR_SUFFIX):
                    host = _sh.Path(str(f)[: -len(_sh._AUDIT_SIDECAR_SUFFIX)])
                    host_key = os.path.normcase(str(host))
                    if host_key in live or host_key in protected or host_key in inflight or host_key in leased:
                        continue
                    if not host.exists():
                        # 宿主已不存在 ⇒ 该旁车必为孤儿；仍走唯一删除出口，
                        # 避免在导出域出现第二条裸删路径（R177 护栏）。
                        _sh._remove_export_artifact(host)
                        deleted += 1
                    continue  # 宿主存在则由宿主分支连同旁车一并处理
                if key in live or key in protected or key in inflight or key in leased:
                    continue
                if ".tmp." in f.name and (now - f.stat().st_mtime) < _sh._ORPHAN_TMP_TTL_SECONDS:
                    continue  # 宽限期内：可能是刚降级返回、仍在流式读取的产物
                # ✅ R176 · D2-2：删产物必连带删旁车（不留失主旁车）。
                _sh._remove_export_artifact(f)
                deleted += 1
            except OSError:
                continue
    except Exception as e:  # noqa: BLE001
        _sh.logger.warning("孤儿导出产物回收失败（不影响本次导出）: %s", e)
    if deleted:
        _sh.logger.info(
            "导出：已回收 %d 个孤儿导出产物（scheme=%s, dir=%s）", deleted, scheme_id[:8], _sh.EXPORTS_DIR.name
        )
    return deleted
