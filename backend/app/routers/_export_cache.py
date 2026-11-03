"""导出缓存·指纹与治理群（配置指纹/内容指纹/缓存行判据/缓存修剪/孤儿产物 GC，自 export.py 按导出阶段拆出，facade 延迟绑定，纯搬移零行为变更）"""

import app.routers.export as _sh  # noqa: F401 — facade 命名空间延迟绑定（monkeypatch 兼容）

_EXPORT_CONFIG_KEYS = frozenset({
    "font_name", "font_size", "page_header", "page_footer",
    "show_page_number", "show_title_page", "show_toc",
    "bidder_name", "chapter_page_break", "page_number_style",
    "line_spacing", "toc_depth", "cover_info", "margins",
    "allow_pil_fallback", "chart_fail_placeholder", "heading_styles",
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
})


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

    return {k: _clean(v) if isinstance(v, dict) else v
            for k, v in config.items()
            if k in _sh._EXPORT_CONFIG_KEYS and v is not None and v != ""}


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
            "model": str(getattr(_s, "image_model", "") or ""),
            "size": str(getattr(_s, "image_default_size", "") or ""),
            "base_url": str(getattr(_s, "image_base_url", "") or ""),
        }
    except Exception:  # pragma: no cover - 配置不可用时退化为稳定值
        return {"enabled": False, "model": "", "size": "", "base_url": ""}


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
        fe_sig = _sh.hashlib.md5(
            "\n---\n".join(sorted(prep["fe_codes"])).encode()).hexdigest()[:12]
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
                    "前端渲染像素指纹采集失败（该图不入指纹，换图后可能命中旧缓存）:"
                    " key=%s", _code_key, exc_info=True)
                continue
        if _parts:
            fe_pixels_sig = _sh.hashlib.md5(
                "\n---\n".join(sorted(_parts)).encode()).hexdigest()[:16]
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
        _demote_on = bool(
            getattr(_demote_settings, "body_subheading_demote_with_children", True))
    except Exception:  # pragma: no cover - 配置不可用时沿用渲染端同款兜底
        _demote_on = True
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
        _sh.json.dumps({
            # ✅ BUG 修复（2026-09-18）：封面标题/页眉取自 schemes.name，旧指纹未含
            #    方案名 → 改名后正文与配置未变会命中旧缓存，交付文档封面仍是旧名。
            "scheme": ((prep.get("scheme") or {}).get("name", ""),
                       (prep.get("scheme") or {}).get("project_id", "")),
            "sections": [(s["id"], s.get("parent_id", ""), s.get("sort_order"),
                          s.get("level"), s.get("title", ""), s.get("content") or "")
                         for s in prep["sections"]],
            "charts": sorted(prep["chart_fp"]),
            # ✅ 修复（2026-09-17）：事实变更应触发缓存更新（此前指纹未含事实，
            # invalidate_export_cache 形同空转）。用 .get 防御部分调用方拼装的
            # 最小 prep（缺该键视为无事实），避免 _content_fingerprint 抛 KeyError。
            "global_facts": [(f.get("gt", ""), f.get("title", ""), f.get("content") or "")
                             for f in (prep.get("global_facts") or [])],
            # ✅ D-3：补充附录内容纳入指纹 —— 否则开关打开/数据源变更后
            #    指纹不变，会永久命中不含附录的旧产物（与 2026-09-17 事实变更
            #    不失效是同一类缺陷）。用 .get 防御最小 prep。
            "appendix_sources": [
                (g.get("title", ""),
                 [(i.get("name", ""), i.get("value", "")) for i in (g.get("items") or [])])
                for g in (prep.get("appendix_sources") or [])
            ],
            # 事实查询失败与正常空事实必须使用不同的缓存键。
            "global_facts_status": prep.get("global_facts_status") or {},
            "config": config,
            "fe_render": fe_sig,
            # R-1：前端 PNG 像素字节指纹（补 fe_render 只看 code 的盲区）
            "fe_render_pixels": fe_pixels_sig,
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
            # ✅ 缓存失效修复（2026-09-23）：纯渲染逻辑改进（mermaid/PIL 渲染器升级，
            #    _RENDERER_VERSION 未变但逻辑已变）也应失效 DOCX 缓存，否则旧图被烤进
            #    二进制后永远命中。图表数据(data_json)变化固然会让 chart_fp 改变，
            #    但渲染器自身变化不反映在数据里，须显式纳入指纹。
            "renderer": _RENDERER_VERSION,
        }, ensure_ascii=False, sort_keys=True).encode()
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
        if not sec.get("consistent"):
            sample = ""
            for d in (sec.get("diffs") or [])[:1]:
                sample = " | ".join(d.get("new", [])) if d.get("new") else ""
            parts.append(f"[{sec.get('section_id', '?')[:8]}]{sample}")
    return "; ".join(parts) or f"不一致章节数={report.get('mismatched', 0)}"


async def _guard_numbering_consistency(db, scheme_id: str) -> None:
    """✅ 编号统一（2026-09-26 · 显式跨校验器）：导出前校验落库正文子标题编号与
    当前 outline 编号一致（捕获 D4 类漂移残留，原始需求 五.6 后半）。
    DOCX 与 PDF 两条链路共用（均经此守卫，避免"改了一处漏一处"）。

    严格模式（numbering_consistency_strict）直接 409 阻断产出不一致文档
    （CI 校验场景）；默认仅告警——导出成稿本身仍由 _compute_subheading 重算
    保证正确，落库正文可用 /sections/numbering-consistency/repair 修复。
    校验器自身异常绝不阻断导出（降级为 debug 日志）。
    """
    try:
        from app.config import settings as _settings
        _strict = bool(_settings.numbering_consistency_strict)
    except Exception:
        _sh.logger.debug("读取 numbering_consistency_strict 失败（沿用默认非严格模式）",
                     exc_info=True)
        _strict = False
    if _strict:
        from app.services.numbering import validate_scheme_numbering_consistency
        _rep = await validate_scheme_numbering_consistency(db, scheme_id)
        if not _rep["consistent"]:
            raise _sh.HTTPException(
                409,
                detail={"error": "numbering_inconsistency",
                        "mismatched": _rep["mismatched"],
                        "summary": _sh._summarize_numbering_consistency(_rep)})
    else:
        try:
            from app.services.numbering import validate_scheme_numbering_consistency
            _rep = await validate_scheme_numbering_consistency(db, scheme_id)
            if not _rep["consistent"]:
                _sh.logger.warning(
                    "导出前编号一致性校验发现 %d 章落库正文与目录编号不一致（scheme=%s），"
                    "建议调用 /numbering-consistency/repair 或触发结构重排以同步。",
                    _rep["mismatched"], scheme_id[:8])
        except Exception as _e:  # noqa: BLE001
            _sh.logger.debug("编号一致性预检失败（不影响导出）: %s", _e)


def _cache_row_viable(result_path: str) -> bool:
    """缓存行是否仍可用：result_path 非空、文件存在且**非空文件**。

    ✅ BUG 修复（2026-10-14 · P2 判据不一致）：命中判据（export_docx / export_pdf）
    要求 `Path(...).stat().st_size > 0`，而本函数的两处判据只查 `.exists()`：
      · 一份 0 字节（写盘中断 / 磁盘满）的产物既永远不会被返回给用户（命中判据拦住），
        又被本函数当成「有效缓存」占用 5 份保留配额；
      · 于是它会在裁剪时把**更新且可用**的缓存行挤出保留窗口，
        用户表现为「明明没改内容，缓存却突然失效、重新付一次全量渲染成本」。
    抽成共享判据，使「可用缓存」在读写两侧同一口径。
    """
    if not result_path:
        return False
    try:
        p = _sh.Path(result_path)
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


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
        "SELECT id, result_path FROM export_cache WHERE scheme_id=?"
        " ORDER BY created_at DESC, rowid DESC",
        (scheme_id,))
    cache_rows = [dict(r) for r in await _sh._db_fetch_all(
        cur, what="_prune_export_cache")]
    if not cache_rows:
        # 无行 = 查询失败或本方案确无缓存；两种情况都无需裁剪（fail-soft）
        return
    keep_paths = {str(protect_path)}
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
                try:
                    _sh.Path(rp).unlink(missing_ok=True)
                except OSError:
                    pass
        placeholders = ",".join("?" * len(stale_ids))
        await _sh._db_exec(db, f"DELETE FROM export_cache WHERE id IN ({placeholders})", stale_ids,
                    what="_prune_export_cache 清理")


#: 孤儿临时产物的宽限期（秒）。DOCX 原子替换失败会降级为「本次不写缓存」，
#: 返回给用户的产物仍是那个 ``.tmp.docx`` —— 而 FileResponse 在 handler
#: 返回**之后**才开始流式读取该文件，因此临时产物必须留一个宽限期，否则
#: 下一次导出就会把正在被读取的临时产物顺手清掉。正常交付产物
#: （``.docx`` / ``.pdf``）不需要宽限：它们要么被现存缓存行引用（不会命中
#: 孤儿判定），要么本身就是孤儿（可以立即清理）。
_ORPHAN_TMP_TTL_SECONDS = 600


async def _gc_orphan_exports(db, scheme_id: str,
                             protect: set[_sh.Path] | None = None) -> int:
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
                await db.execute(
                    "SELECT result_path FROM export_cache WHERE scheme_id=?",
                    (scheme_id,)),
                what="_gc_orphan_exports 缓存行", strict=True)
        except _sh.DBReadError as e:
            _sh.logger.warning(
                "导出：孤儿回收跳过（无法确认哪些产物仍被缓存行引用，"
                "保守不动任何文件；scheme=%s）: %s", scheme_id[:8], e)
            return deleted
        live = {os.path.normcase(str(dict(r).get("result_path") or ""))
                for r in rows if dict(r).get("result_path")}
        protected = {os.path.normcase(str(p)) for p in (protect or set())}
        # ✅ BUG 修复（2026-10-14 · P0）：跳过「在飞产物」。不同配置指纹的并发导出
        #    持不同的去重锁，可在本函数执行期间正把产物 os.replace 到最终路径、
        #    但 INSERT+commit 尚未发生 —— 此时它既不在 live（DB 无行）、也不在任何
        #    调用方的 protect 里，且文件名不含 .tmp. → 会被误判孤儿 unlink，
        #    导致对端请求 500/404 或缓存行永久指向已删文件。
        #    见模块内 _EXPORT_INFLIGHT_PATHS 的注释（在同模块下方定义，运行时解析）。
        inflight = _sh._export_inflight_snapshot()
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
                if key in live or key in protected or key in inflight:
                    continue
                if ".tmp." in f.name and (now - f.stat().st_mtime) < _sh._ORPHAN_TMP_TTL_SECONDS:
                    continue  # 宽限期内：可能是刚降级返回、仍在流式读取的产物
                f.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                continue
    except Exception as e:  # noqa: BLE001
        _sh.logger.warning("孤儿导出产物回收失败（不影响本次导出）: %s", e)
    if deleted:
        _sh.logger.info("导出：已回收 %d 个孤儿导出产物（scheme=%s, dir=%s）",
                    deleted, scheme_id[:8], _sh.EXPORTS_DIR.name)
    return deleted
