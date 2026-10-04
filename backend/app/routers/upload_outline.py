"""上传目录识别路由"""
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.db import get_db
from app.services.ai.json_response import (
    OUTLINE_REPAIR_KEY,
    collect_json_response,
)
from app.services.ai.prompts._registry import render
from app.services.bid_section_detector import detect_bid_sections
from app.services.file_parser import (
    MAX_UPLOAD_BYTES,
    SUPPORTED_EXTENSIONS,
    ParseError,
    dump_parse_warnings,
    parse_file_content_ex,
    signature_valid,
    simple_parse_outline,
)

logger = logging.getLogger("upload_outline")

router = APIRouter(prefix="/api/v1/upload-outline", tags=["upload_outline"])


def _outline_has_branch(nodes, depth: int = 0) -> bool:
    """递归判断目录树中是否存在真正的父子层级（子节点为非空列表）。"""
    if depth > 10 or not isinstance(nodes, list):
        return False
    for n in nodes:
        if not isinstance(n, dict):
            continue
        children = n.get("children")
        if isinstance(children, list) and len(children) > 0:
            return True
        if isinstance(children, list) and _outline_has_branch(children, depth + 1):
            return True
    return False


def _outline_node_count(nodes, depth: int = 0) -> int:
    """递归统计目录节点总数（带深度保护）。"""
    if depth > 10 or not isinstance(nodes, list):
        return 0
    total = 0
    for n in nodes:
        if not isinstance(n, dict):
            continue
        total += 1
        children = n.get("children")
        if isinstance(children, list):
            total += _outline_node_count(children, depth + 1)
    return total


def _outline_seems_valid(outline: list) -> bool:
    """规则法/AI 识别结果是否构成有效层级结构。

    - 任一节点带有非空 children（识别出真正的层级），或
    - 节点总数较多（>=5，扁平大纲也算有效）
    仅当结果为空 / 纯叶子平铺时才视为「识别失败」，需要 AI 兜底。

    ✅ 增强：旧实现只看顶层节点、且用 truthy 判断 children，
    一是节点计数只算顶层（扁平但很长的目录被低估），
    二是 `children` 为字符串等畸形值时也被当成"有层级"（放过了坏数据）。
    现改为递归探测 + 要求 children 必须是非空 list。
    """
    if not isinstance(outline, list) or not outline:
        return False
    return _outline_has_branch(outline) or _outline_node_count(outline) >= 5


@router.post("/parse")
async def parse_outline(file: UploadFile = File(...),
                        scheme_name: str = Query(""),
                        reorganize: bool = Query(False),
                        project_id: str = Query(""),
                        db=Depends(get_db)):
    """上传文件并识别目录结构。

    ✅ BUG 修复（2026-09-16）：前端 `uploadOutlineApi.parse(file, {scheme_name,
    reorganize})` 一直在传这两个查询参数，但后端从未声明它们 —— FastAPI 对未知
    query 参数静默忽略，于是「整理为标准结构」这档能力（以及
    services/outline_reorganize.py 全套实现 + 单测）**从未被任何路由调用**，
    用户勾选与否结果完全一样。现按参数真正接线：

      - `reorganize=False`（默认）：保持既有行为（识别结果 = 上传文档结构的镜像）；
      - `reorganize=True`：识别结果先经 reorganize_to_standard 归位到标准章节骨架，
        再把报告随响应返回（report/template 供前端提示"已整理为标准结构"）。
        默认值取 False 以保持向后兼容，避免未显式要求时改变既有识别结果。

    ✅ 链路断裂修复（2026-10-03）：``uploaded_outlines.project_id`` 列自建表起
    就存在，且 R32 已把该表登记进 ``projects._PROJECT_SCOPED_TABLES``（按
    project_id 级联清理）—— 但写入侧从未给它赋值：本路由 INSERT 不含该列、
    save-as-outline 只回写 scheme_id，导致所有新记录 project_id 恒为空串，
    「删项目清掉上传识别记录」对真实链路**形同虚设**（DELETE 匹配 0 行）。
    现接线：
      - ``project_id``（默认空，向后兼容）：传入时校验存在性并随识别记录
        落库，建立「上传记录 ↔ 项目」的归属链；未传（如目录库编辑弹窗的
        无项目上下文入口）保持历史空值行为不变。
      - save-as-outline 另补 scheme 反查回写（见该函数），覆盖历史无链记录
        被保存时机的自愈。
    """
    # ✅ 非字符串默认值守卫：与 _resolve_project_id 同因 —— 直接以函数方式
    #    调用路由（既有单测风格）时 Query("") 会以对象形式落入默认位，
    #    str() 化得到假值，非字符串一律按「未提供」处理。
    _pid = project_id.strip() if isinstance(project_id, str) else ""
    if _pid:
        _cur = await db.execute("SELECT id FROM projects WHERE id=?", (_pid,))
        if not await _cur.fetchone():
            raise HTTPException(404, "项目不存在，无法建立上传记录归属链")
    # ✅ BUG 修复（历史）：旧实现一次性 await file.read() 无任何大小上限，
    #    超大文件会把整个内容读进内存（OOM 风险），且 UploadFile 从不显式
    #    close。现分块读取，超过上限直接 413 拒绝，并在 finally 中关闭文件
    #    句柄；上限读 settings.upload_max_bytes（默认 30MB，见
    #    file_parser.MAX_UPLOAD_BYTES）。累加采用「先收集分块、最后一次 join」
    #    ——旧实现 `content += chunk` 对 bytes 是不可变拼接，30MB 文件会反复
    #    整体拷贝（O(n²)）。
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                _mb = MAX_UPLOAD_BYTES // (1024 * 1024)
                raise HTTPException(
                    413,
                    f"文件过大，请上传 {_mb}MB 以内的文件"
                    if _mb else "文件过大，请上传更小的文件")
            chunks.append(chunk)
    finally:
        await file.close()
    content = b"".join(chunks)
    fname = file.filename or "unknown"
    ftype = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

    # ✅ 修复（2026-09-17）：与「项目资料上传」同一口径，先拒绝空文件与不支持的
    #    扩展名，避免把 .exe/.zip/.htm 等改名后进入解析流程被当文本解码成乱码。
    if not content:
        raise HTTPException(400, "空文件，请上传有效内容")
    if ftype and ftype not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            400, f"不支持的文件格式（.{ftype}），支持："
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}")

    # ✅ 与「项目资料上传」同一口径：拒绝扩展名与真实内容不符的文件。
    #    旧实现完全不校验，把任意文件改名为 .docx 即可进入解析流程。
    if content and not signature_valid(ftype, content[:16]):
        raise HTTPException(
            400, f"文件内容与扩展名（{ftype.upper()}）不符，请检查文件后重新上传")

    try:
        # ✅ BUG 修复：改用带诊断的解析入口。旧实现用 parse_file_content（只返回
        #    正文），解析器诊断（PDF 页数截断 / CSV、Excel 行数截断 / OCR 兜底 /
        #    加密 PDF）全部被丢弃 —— 用户看到"识别成功"，却不知道第 50 页之后的
        #    机械配置、编号根本没参与识别。现随响应回传。
        raw_text, diag = parse_file_content_ex(content, fname)
    except ParseError as e:
        # ✅ 修复（2026-09-17）：解析不可恢复失败（缺依赖/OCR引擎/损坏）用用户可读
        #    文案，与 global-facts 解析路径口径一致；不把底层堆栈/路径泄露给客户端。
        raise HTTPException(400, f"文件解析失败：{e}")
    except Exception as e:
        logger.exception("上传目录解析异常: %s", e)
        raise HTTPException(400, "文件解析失败，请确认文件未损坏且格式受支持")

    # ✅ 修复（2026-09-17）：解析诊断需在「写入上传记录」前算出，随记录持久化，
    #    否则刷新/重新拉取后「识别结果可能不完整」告警丢失（与 project_documents 口径一致）。
    parse_warnings = [str(w) for w in (diag.get("warnings") or [])]

    # ✅ 修复（2026-09-18）：解析器会用文件头嗅探真实类型（无扩展名/被改名的 PDF、
    #    扫描图），旧实现落库与 is_image 判定都用「后缀推导值」→ 无后缀扫描图被当
    #    文本、OCR 噪声惩罚失效，列表 file_type 与实际解析类型不一致。现以前者为准。
    _sniffed_ftype = str(diag.get("file_type") or "").strip().lower()
    if _sniffed_ftype and _sniffed_ftype != ftype:
        logger.info("目录识别：文件 %s 真实类型 %s（后缀推导为 %s）",
                    fname, _sniffed_ftype, ftype or "无")
        ftype = _sniffed_ftype

    # ✅ BUG-O10 修复：根据实际识别质量计算 confidence（旧实现硬编码 0.6/0.8）
    # - 规则法识别且有层级结构：0.85（确定性高）
    # - 规则法识别但扁平大纲：0.65（有效但不够好）
    # - AI 兜底识别：0.75（质量取决于 AI 表现）
    # - 图片/OCR 文件：在上述基础上 -0.1（OCR 噪声影响）
    is_image = ftype in ("png", "jpg", "jpeg", "bmp", "tiff")
    outline = simple_parse_outline(raw_text)
    used_ai = False
    if not _outline_seems_valid(outline) and raw_text and len(raw_text) > 50:
        try:
            sys_prompt = render("outline_recognition_system", raw_text=raw_text[:6000])
            # ✅ 修复（2026-09-24）：补 scene —— 旧实现未传，该 AI 兜底调用的
            #    消耗在 /ai/stats 的场景聚合里不可见，也无法单独指定模型。
            obj, _ = await collect_json_response(
                [{"role": "system", "content": sys_prompt}],
                lambda o: [] if o.get("outline") else ["缺少 outline"],
                scene="outline_recognition",
                repair_key=OUTLINE_REPAIR_KEY)
            ai_outline = obj.get("outline", [])
            if _outline_seems_valid(ai_outline):
                outline = ai_outline
                used_ai = True
        except Exception:
            # AI 失败仍回退到规则法结果（即便不完美也有结构）。
            # ✅ 技术债清理：此前是裸 pass —— AI 兜底静默失效时线上不留任何痕迹，
            #    排查"AI 明明配好了却没生效"无从下手。补 warning（含堆栈）。
            logger.warning("AI 兜底目录识别失败，回退规则法结果", exc_info=True)

    # ✅ 整理为标准结构（对齐前端 uploadOutlineApi.parse 的 reorganize 参数）：
    #    在 normalize 之前把识别结果归位到标准章节骨架（保留用户细分内容，
    #    未归位内容进「补充章节」），随后同样走 normalize_outline 落库口径。
    reorganize_report: dict | None = None
    if reorganize and isinstance(outline, list) and outline:
        try:
            from app.services.outline_reorganize import reorganize_to_standard
            _reorg = reorganize_to_standard(outline, scheme_name or fname)
            if isinstance(_reorg, dict) and _reorg.get("outline"):
                outline = _reorg["outline"]
                reorganize_report = dict(_reorg.get("report") or {})
                reorganize_report["template"] = (
                    _reorg.get("template") or reorganize_report.get("template") or "")
        except Exception:
            logger.warning("上传目录整理为标准结构失败（保留识别结果）", exc_info=True)

    # ✅ 与落库口径一致：解析识别结果在「返回前端预览 / 写入上传记录」前统一走
    #    normalize_outline（三级裁剪 + 编号重排 + 补全 children/level）。
    #    保证用户预览所见 == 最终落库所得，避免前端先展示 4+ 级或标题内嵌编号的
    #    脏结构、点击保存时才被静默归一化的观感不一致（save-as-outline /
    #    save-as-library 落库路径同样调用 normalize_outline，这里对齐口径）。
    if isinstance(outline, list) and outline:
        try:
            from app.services.outline_utils import normalize_outline
            _normalized = normalize_outline(outline)
            if _normalized:
                outline = _normalized
        except Exception:
            # 归一化失败不应阻断识别主流程，保留原始结构回退
            pass

    if used_ai:
        confidence = 0.75
    elif _outline_seems_valid(outline):
        # 规则法识别：有层级结构给高分，扁平大纲给低分
        confidence = 0.85 if _outline_has_branch(outline) else 0.65
    else:
        confidence = 0.5  # 空结果
    if is_image:
        confidence = max(0.4, confidence - 0.1)  # OCR 噪声惩罚

    # 保存记录（raw_text 限 5000 字符，超长时记录截断标记）
    rid = str(uuid.uuid4())
    raw_truncated = len(raw_text) > 5000
    await db.execute(
        "INSERT INTO uploaded_outlines (id, project_id, file_name, file_type, raw_text, parsed_json, confidence, status, parse_warnings)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, _pid, fname, ftype, raw_text[:5000], json.dumps({"outline": outline}, ensure_ascii=False),
         confidence, "parsed", dump_parse_warnings(parse_warnings)))
    await db.commit()

    result = {"id": rid, "outline": outline, "confidence": confidence, "file_name": fname}
    # ✅ 解析诊断回传（与 /global-facts/documents/{id}/parse 口径一致）：
    #    解析成功但内容不完整（PDF 截页、表格截行、OCR 兜底、加密 PDF）时，
    #    前端必须能把「识别结果可能不完整」如实告知用户。
    if diag.get("truncated"):
        result["parse_truncated"] = True
    # ✅ 解析诊断回传（修复）：parse_warnings 此前只持久化到 uploaded_outlines 表，
    #    却未随响应返回前端 —— 用户看到「识别成功」却收不到「内容可能不完整」告警，
    #    与 global-facts /generate-facts 的口径不一致。现随响应回传。
    if parse_warnings:
        result["parse_warnings"] = parse_warnings
    if reorganize_report is not None:
        # ✅ 让前端能提示"已按标准章节骨架整理"并展示归位统计
        result["reorganize_report"] = reorganize_report
        result["reorganized"] = True
    # ✅ 标段检测（对齐 OpenBidKit bidSectionDetector）：导入的招标文件若疑似多标段，
    #    提示用户拆分后分别生成方案，避免 AI 把多个标段正文混为一谈。
    if raw_text:
        hint = detect_bid_sections(raw_text)
        if hint.get("has_multiple"):
            result["multi_section_hint"] = hint
    if raw_truncated:
        result["raw_text_truncated"] = True
        result["warning"] = f"原文 {len(raw_text)} 字，保存记录时截断至 5000 字（不影响目录识别结果）"
    elif not raw_text.strip():
        # ✅ 增强（可操作性）：解析结果为空时必须明确告知原因。旧实现只返回
        #    outline=[]，前端提示"识别完成：共 0 个章节"，用户会误以为是
        #    格式不支持而来回换文件重试；实际多为扫描件缺 OCR 引擎或文件损坏。
        result["empty_text"] = True
        result["warning"] = (
            "未从文件中解析到有效文本（可能是空白文件、扫描件或已损坏）；"
            "若为扫描件，请确认 OCR 引擎可用"
            "（打开 /api/v1/diagnostics/capabilities 查看启用方法）")
    return result


@router.post("/{upload_id}/save-as-outline")
async def save_as_outline(upload_id: str, body: dict, db=Depends(get_db)):
    """保存识别结果为方案目录（写入 sections 表）"""
    scheme_id = body.get("scheme_id", "")
    outline = body.get("outline", [])
    if not scheme_id:
        raise HTTPException(400, "缺少 scheme_id")
    if not outline:
        raise HTTPException(400, "缺少 outline 数据")

    # 先查上传记录是否存在，再查方案是否存在（避免 JOIN 列名歧义）
    cur = await db.execute("SELECT id FROM uploaded_outlines WHERE id=?", (upload_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "上传记录不存在")
    cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
    srow = await cur.fetchone()
    if not srow:
        raise HTTPException(404, "方案不存在")
    project_id = srow["project_id"] if isinstance(srow, dict) else srow[0]

    # ✅ 竞态守卫（2026-09-22 补齐，与 /save-outline、/apply-and-save 同一口径）：
    #    本接口同样**整表重建 sections**（UPDATE + 级联 DELETE + INSERT），
    #    与后台正文/目录生成任务并发时丢失更新的后果不可恢复
    #    （正文被删、刚生成的目录被覆盖）。旧实现完全无守卫，
    #    前端 disabled 只挡主路径，接口层必须兜底直连调用 / 多标签页并发。
    from app.routers.sections import content_generation_in_progress, outline_generation_in_progress
    if content_generation_in_progress(scheme_id):
        raise HTTPException(
            409, "本方案正文正在后台生成中，请等待生成完成（或先停止任务）后再保存目录——"
                 "整表重建会与生成结果互相覆盖")
    if outline_generation_in_progress(scheme_id):
        raise HTTPException(
            409, "本方案目录正在后台生成中，请等待生成完成（或先停止任务）后再保存目录")

    # 写入 sections 表
    # ✅ 目录限三级 + 统一重排编号：上传识别结果落库前统一规范化
    from app.services.outline_utils import normalize_outline
    outline = normalize_outline(outline)
    if not outline:
        raise HTTPException(400, "outline 节点格式非法（应为对象数组）")

    # ✅ BUG 修复：标题匹配保留已有章节正文内容。
    # 旧实现无条件 DELETE FROM sections + INSERT，导致重新上传目录时
    # 所有已生成的正文内容丢失（即使章节标题完全相同）。
    # 现：按归一化标题匹配已有 section，匹配成功则保留 content/word_count/status，
    # 仅更新结构字段；不匹配的旧章节被删除，新章节插入空记录。
    import re as _re
    def _norm_title(t: str) -> str:
        return _re.sub(r'\s+', '', str(t or '')).strip()

    cur = await db.execute(
        "SELECT id, title, content, word_count, status, word_budget, parent_id"
        " FROM sections WHERE scheme_id=? ORDER BY sort_order, created_at",
        (scheme_id,))
    existing_rows = [dict(r) for r in await cur.fetchall()]
    # ✅ BUG 修复（2026-10-04 · 跨父级正文错配）：同名子标题（"施工准备""安全保证
    #    措施"）在不同父章节下合法存在。旧实现用「不分父级、不分层级」的全局队列
    #    queue.pop(0)，重新上传后同名子标题的排列/归属一旦变化，正文就跨父级张冠李戴
    #    （第 1 章正文挂到第 2 章同名节上），而 preserved_count 照算、前端无感知。
    #    现构建三层索引（完整标题路径 / 同父同层 / 同层）+ 全局兜底，按优先级取号，
    #    每个旧章节至多被消费一次；路径由「归一化标题」元组表示，与编号无关
    #    （重传时编号可能整体顺移，路径才是稳定身份）。
    existing_by_title: dict[str, list[dict]] = {}
    existing_by_parent_level: dict[tuple, list[dict]] = {}
    existing_by_level: dict[int, list[dict]] = {}
    for er in existing_rows:
        nt = _norm_title(er.get("title", ""))
        if not nt:
            continue
        existing_by_title.setdefault(nt, []).append(er)
        existing_by_parent_level.setdefault(
            (er.get("parent_id") or "", int(er.get("level") or 1)), []).append(er)
        existing_by_level.setdefault(int(er.get("level") or 1), []).append(er)

    # 旧章节的标题路径索引（id -> 归一化标题元组），由旧树 parent 关系一次性构建
    old_by_id: dict[str, dict] = {er["id"]: er for er in existing_rows}

    def _old_title_path(er: dict) -> tuple:
        parts: list[str] = []
        seen: set[str] = set()
        cur_id = er["id"]
        guard = 0
        while cur_id and cur_id not in seen and guard < 50:
            seen.add(cur_id)
            node = old_by_id.get(cur_id)
            if node is None:
                break
            t = _norm_title(node.get("title", ""))
            if t:
                parts.append(t)
            cur_id = node.get("parent_id") or ""
            guard += 1
        return tuple(reversed(parts))

    existing_by_path: dict[tuple, list[dict]] = {}
    for er in existing_rows:
        if _norm_title(er.get("title", "")):
            existing_by_path.setdefault(_old_title_path(er), []).append(er)

    consumed: set[str] = set()

    def _take_from(candidates: list[dict] | None, expected_nt: str) -> dict | None:
        """从候选桶中取一个未消费且「归一化标题等于 expected_nt」的旧章节。

        ✅ BUG 修复（2026-10-04 · 跨标题错配保留）：旧实现在 by_parent_level /
        by_level 桶取候选时**不校验标题**，导致新标题（如「第一章」「全新章节」）
        会「保留」掉一个**不同标题**的旧章节（如「施工准备」「旧章」）。后果：
          - 「第一章」被误匹配到 a1（施工准备），preserved_content 虚高；
          - 未匹配的旧父章节（"old"）因被误保留而留在 to_delete 之外，
            其子章节（"oldc"）成为孤儿未被级联删除；
          - 用户看到的正文是错的章节内容，前端毫无感知。
        现：所有桶（含 by_path）都强制「归一化标题相等」，
        结构桶（by_parent_level / by_level）仅在标题相等时才消费，
        保持「结构优先」的兜底逻辑不劣于旧版。
        """
        if not candidates:
            return None
        while candidates:
            cand = candidates.pop(0)
            if cand["id"] in consumed:
                continue
            if _norm_title(cand.get("title", "")) != expected_nt:
                continue
            consumed.add(cand["id"])
            return cand
        return None

    preserved_count = 0
    final_section_ids: set[str] = set()
    updates: list[tuple] = []
    new_inserts: list[tuple] = []
    counter = {"n": 0}

    def collect_rows(nodes: list, parent_id: str, path_parts: tuple):
        nonlocal preserved_count
        for i, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            title = node.get("title", "")
            nt = _norm_title(title)
            matched = None
            if nt:
                cur_path = path_parts + (nt,)
                # 1) 完整标题路径精确匹配（跨层级重构时最稳）
                # 2) 同父 + 同层级
                # 3) 仅同层级
                # 4) 全局同名兜底（行为不劣于旧版）
                # 所有桶统一按「归一化标题相等」过滤（见 _take_from），
                # 避免新标题「保留」不同标题的旧章节导致误保留 + 孤儿漏删。
                parent_key = (parent_id, int(node.get("level", 1)))
                for bucket in (existing_by_path.get(cur_path),
                               existing_by_parent_level.get(parent_key),
                               existing_by_level.get(int(node.get("level", 1))),
                               existing_by_title.get(nt)):
                    matched = _take_from(bucket, nt)
                    if matched is not None:
                        break
            # ✅ 统一形态（2026-09-22）：与 _save_outline_to_db / 重排函数同口径
            #    写 id/level/confidence 三键；旧实现 confidence 可能为 None
            #    且缺 level，消费方（前端树/重排回写）需逐处容错。
            # normalize_outline 已在上游统一重排并回写 id/level。
            confidence = node.get("confidence")
            if confidence is None:
                confidence = 1.0
            outline_json = json.dumps(
                {"id": node.get("id"), "level": node.get("level", 1),
                 "confidence": confidence},
                ensure_ascii=False)
            # 节点未携带 word_budget 时沿用已有预算（避免重置用户设置）
            raw_budget = node.get("word_budget")
            if isinstance(raw_budget, (int, float)) and raw_budget > 0:
                word_budget = int(raw_budget)
            elif matched and matched.get("word_budget"):
                word_budget = matched["word_budget"]
            else:
                word_budget = 1500
            if matched:
                # 标题匹配已有章节：保留正文，只更新结构字段
                sid = matched["id"]
                old_path = _old_title_path(matched)
                if old_path and old_path != (path_parts + (nt,)):
                    logger.info(
                        "上传目录保存：同名章节跨路径匹配 title=%s old=%s new=%s（按层级兜底）",
                        title, " > ".join(old_path), " > ".join(path_parts + (nt,)))
                preserved_count += 1
                final_section_ids.add(sid)
                updates.append((
                    title, node.get("description", ""), node.get("level", 1),
                    parent_id, word_budget, outline_json,
                    i, datetime.now().isoformat(), sid))
            else:
                # 新章节：插入空记录
                sid = str(uuid.uuid4())
                final_section_ids.add(sid)
                new_inserts.append((
                    sid, scheme_id, project_id, parent_id, title,
                    node.get("description", ""), node.get("level", 1), i,
                    "empty", outline_json, word_budget))
            counter["n"] += 1
            children = node.get("children")
            if isinstance(children, list) and children:
                collect_rows(children, sid, path_parts + ((nt,) if nt else ()))

    collect_rows(outline, "", ())

    # 删除不再存在的旧章节（级联清理图表预测）
    old_ids = {er["id"] for er in existing_rows}
    to_delete = old_ids - final_section_ids
    # ✅ 正文丢失量化（与 sections._save_outline_to_db 同口径，2026-09-26）：
    #    未匹配上的旧章节若带正文，会随本次整表重建被级联删除且不可恢复，
    #    必须如实回传给前端提示（此前只有 preserved_content 的正向提示）。
    cleared_content = 0
    if to_delete:
        # ✅ BUG 修复：旧实现这里是一段 `for er in existing_rows: pass` 的死代码，
        #    注释声称"递归收集所有后代 ID"但从未实现；且 SELECT 未取 parent_id。
        #    结果删除父章节时其子章节不会级联删除，留下 parent_id 指向已删章节的
        #    孤儿数据（前端树里凭空多出一级、导出时被当作根节点）。
        #    现真正实现：按 parent_id 构建索引后递归收集后代，
        #    并跳过本次仍保留（会被重新挂载）的章节。
        children_map: dict[str, list[str]] = {}
        for er in existing_rows:
            children_map.setdefault(er.get("parent_id") or "", []).append(er["id"])

        def _collect_subtree(ids: set[str]) -> set[str]:
            all_ids = set(ids)
            stack = list(ids)
            while stack:
                nid = stack.pop()
                for cid in children_map.get(nid, []):
                    if cid in final_section_ids:
                        continue  # 该章节在本次目录中已复用，会被重挂到新父节点
                    if cid not in all_ids:
                        all_ids.add(cid)
                        stack.append(cid)
            return all_ids

        full_delete_ids = _collect_subtree(to_delete)
        cleared_content = sum(
            1 for er in existing_rows
            if er["id"] in full_delete_ids and str(er.get("content") or "").strip())
        placeholders = ",".join("?" * len(full_delete_ids))
        await db.execute(
            f"DELETE FROM chart_predictions WHERE section_id IN ({placeholders})",
            list(full_delete_ids))
        await db.execute(
            f"DELETE FROM sections WHERE id IN ({placeholders})",
            list(full_delete_ids))

    # 批量更新匹配章节（结构字段，保留正文）
    if updates:
        await db.executemany(
            "UPDATE sections SET title=?, description=?, level=?, parent_id=?,"
            " word_budget=?, outline_json=?, sort_order=?, updated_at=? WHERE id=?",
            updates)

    # 批量插入新章节
    if new_inserts:
        await db.executemany(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " description, level, sort_order, status, outline_json, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            new_inserts)

    await db.execute(
        "UPDATE schemes SET outline_source='上传识别', status='目录已确认', updated_at=? WHERE id=?",
        (datetime.now().isoformat(), scheme_id))
    # ✅ 链路断裂修复（2026-10-03）：同步回写 project_id（取自方案反查）——
    #    否则历史/无项目上下文入口产生的空 project_id 记录在方案保存后
    #    仍不被删项目级联覆盖（R32 的显式 DELETE 按 project_id 匹配 0 行）。
    await db.execute(
        "UPDATE uploaded_outlines SET scheme_id=?, project_id=?, status='saved' WHERE id=?",
        (scheme_id, project_id or "", upload_id))
    # ✅ 整表重建后作废一致性扫描缓存（与 /save-outline 同口径，2026-09-23）
    from app.routers.sections import invalidate_consistency_scan_cache
    await invalidate_consistency_scan_cache(db, scheme_id)
    # ✅ 编号统一（2026-09-26 · D4 口径补齐）：本接口同样会改变保留章节的编号
    #    （标题匹配命中后按新位置重排），必须按新编号重规范化已落库正文的子标题，
    #    否则与 create/delete/reorder/save-outline 四处漂移。
    from app.routers.sections import _renormalize_all_section_contents
    await _renormalize_all_section_contents(db, scheme_id)
    await db.commit()
    # ✅ BUG 修复（2026-10-04 · 缓存漏失效）：整表重建改变章节树与编号，
    #    旧 export_cache 行与磁盘产物成为孤儿，口径与 /save-outline 一致。
    try:
        from app.services.facts_extractor import invalidate_export_cache
        await invalidate_export_cache(db, scheme_id)
    except Exception as _e:  # noqa: BLE001
        logger.warning("上传目录保存：导出缓存失效失败（不影响保存结果）: %s", _e)
    result = {"ok": True, "count": counter["n"], "outline": outline}
    if preserved_count:
        result["preserved_content"] = preserved_count
        result["note"] = f"已通过标题匹配保留 {preserved_count} 个章节的已有正文内容"
    if cleared_content:
        result["cleared_content_sections"] = cleared_content
        result["note"] = ((result.get("note", "") + "；") if result.get("note") else "") + \
            f"另有 {cleared_content} 个未匹配章节的已生成正文被清除"
    return result


@router.post("/{upload_id}/save-as-library")
async def save_as_library(upload_id: str, body: dict, db=Depends(get_db)):
    """保存识别结果为目录库"""
    name = body.get("name", "上传识别目录")
    # ✅ BUG 修复：入库前统一规范化（三级裁剪 + 编号重排），
    #    与「保存为方案目录」路径保持一致，避免目录库存入 4+ 级/内嵌编号的脏数据。
    from app.services.outline_utils import normalize_outline_json
    raw_outline = body.get("outline", [])
    outline_json = normalize_outline_json(raw_outline)
    # ✅ PRD 9.2：保存前必须通过层级校验；空目录 / 全是非法节点时明确拒绝，
    #    避免在目录库里沉淀空库（旧实现会静默写入空目录库）。
    try:
        normalized_nodes = json.loads(outline_json)
    except (json.JSONDecodeError, TypeError):
        normalized_nodes = []
    if not isinstance(normalized_nodes, list) or not normalized_nodes:
        raise HTTPException(400, "识别结果为空或节点格式非法，无法存入目录库")
    # ✅ BUG 修复（2026-10-03 · 跨模块契约一致性）：旧实现从不校验 upload_id 是否
    #    存在，伪造/过期的 id 会静默命中 `UPDATE ... WHERE id=?`（0 行受影响）并
    #    返回 {"ok": True} —— 调用方误以为保存成功，实则未关联任何上传记录。
    #    与 save_as_outline（先查 uploaded_outlines 再查 schemes，均 404）同口径：
    #    空 outline 的 400 优先级更高，故放到该检查之后。
    cur = await db.execute("SELECT id FROM uploaded_outlines WHERE id=?", (upload_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "上传记录不存在")
    lid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO outline_library (id, name, source, outline_json, review_status)"
        " VALUES (?,?,?,?,?)",
        (lid, name, "上传识别", outline_json, "待审核"))
    await db.execute("UPDATE uploaded_outlines SET status='library_saved' WHERE id=?", (upload_id,))
    await db.commit()
    return {"id": lid, "ok": True}