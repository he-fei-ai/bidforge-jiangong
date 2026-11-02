"""全局事实 - 文档管理（从 global_facts.py 拆出，2026-10-09）。

负责文档上传、解析、预览、分类管理。与 facts CRUD 解耦，单独成模块。
"""
import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.db import get_db, safe_rowcount
from app.services.doc_categories import (
    AUTO_CLASSIFY_RULES,
    auto_classify_document,
    category_options,
)
from app.services.doc_pipeline import pipeline as doc_pipeline
from app.services.facts_extractor import (
    SCHEME_PROJECT_ID_SQL,
    invalidate_export_cache,
    row_field,
)
from app.services.file_parser import (
    SUPPORTED_EXTENSIONS,
    ParseError,
    count_page_markers,
    dump_parse_warnings,
    signature_valid,
)
from app.utils.log_context import new_trace_id, set_context

logger = logging.getLogger("global_facts.docs")

router = APIRouter(prefix="/api/v1/global-facts", tags=["global_facts"])


#: 解析结果落库上限（字数）—— **全仓唯一事实源**（R70 收敛）。
#: `global_facts.py` 从本文件 import 同名常量（避免两份副本），所有
#: 「是否已截断 / 截断到多少」的判定都读这一处。
#:
#: ⚠️ G2（2026-10-12 收口）：**上传配额不在本文件定义**。此前这里另有三个
#: `MAX_UPLOAD_BYTES / MAX_UPLOAD_FILES_PER_REQUEST / MAX_UPLOAD_TOTAL_BYTES`
#: 静态占位（30MB / 20 / 200MB），是权威值在 `global_facts.py` 的**第二份
#: 字面量副本**（本仓反复踩的「同一判据两份取值」同族），且从未被任何代码/
#: 测试读取。现整体删除，`upload_documents` 内直接动态读 `gf.MAX_UPLOAD_*`
#: （权威值由 `settings.upload_*` 解析，支持 monkeypatch）—— 单一事实源。
MAX_PARSED_CHARS = 400_000

# ✅ R137（2026-10-28）：无文字层、解析必须依赖 OCR 的扩展名。
#    刻意**不含 PDF** —— PDF 有无文字层需打开才知道（可能是原生文本 PDF），
#    不打开就提前告警会误报，反向损害可读性。
_OCR_ONLY_TYPES = frozenset({"png", "jpg", "jpeg", "bmp", "tiff", "tif"})


def _local_ocr_available() -> bool:
    """本地 OCR 引擎（tesseract / rapidocr）是否至少有一个可用。

    fail-soft：未启用 OCR 或探测本身异常时返回 True（视为可用/不判定），
    宁可少报也不误报 —— 上传前置告警是 advisory 档，不得因探测失败而阻断。
    """
    try:
        from app.config import settings as _st
        if not bool(getattr(_st, "ocr_enabled", True)):
            return False
        from app.services import ocr as _ocr
        return bool(_ocr.tesseract_available() or _ocr.rapidocr_available())
    except Exception:
        logger.warning("OCR 引擎可用性探测失败，跳过上传前置告警", exc_info=True)
        return True


@router.get("/documents")
async def list_documents(
    project_id: str = "",
    scheme_id: str = "",
    db=Depends(get_db),
):
    """列出项目资料文档。

    ✅ 加固：支持直接传 scheme_id（内部反查 project_id）。
    此前前端完全依赖 sections 接口返回的 scheme.project_id：一旦该字段缺失
    （如后端未重启、旧版本返回），查询参数为空 → 前端静默跳过 → 列表恒为空，
    表现为「上传了文件却不显示、像是没保存」，而文件其实已正常落库落盘。

    ✅ BUG 修复（跨项目数据泄漏）：旧实现无作用域时返回【全部项目】的文档列表。
    与 list_facts 口径对齐：缺 scheme_id/project_id 直接 400。

    ⚠️ 排序差异（有意设计，勿"统一"）：
    - 本函数 `ORDER BY created_at DESC`：最新上传在前，符合【列表展示】直觉；
    - `load_parsed_docs` `ORDER BY created_at, id`：按上传先后，供【下游消费】
      （事实提取按资料顺序拼 `=== 文件名 ===` 来源标注、批量解析按上传顺序
      给进度）。两者口径不同是刻意保留的，改任何一侧前先核对全部调用方。
    """
    real_pid = await _resolve_project_id(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    sql = ("SELECT id, project_id, file_name, file_type, doc_type, "
           "length(parsed_markdown) as text_len, "
           "doc_category, file_size, parse_time, parse_warnings, "
           "parse_truncated, "
           "parse_status, created_at FROM project_documents")
    params: list = [real_pid]
    sql += " WHERE project_id=?"
    sql += " ORDER BY created_at DESC"
    cur = await db.execute(sql, params)
    # ✅ P1（R13 漏改 · 2026-10-05）：读路径判空。此前直接 `await cur.fetchall()`
    #    → AttributeError → 500。**不得**降级成空列表 —— 前端会把「数据库瞬时
    #    故障」显示成「项目下没有资料」，用户据此重新上传或去查为什么文件丢了，
    #    而文件其实完好在库。503 语义 = 可重试的暂不可用（同 _load_doc 口径）。
    if cur is None:
        logger.warning("查询文档列表失败（db.execute 返回 None），project=%s", real_pid)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    rows = [dict(r) for r in await cur.fetchall()]
    # ✅ 增强：解析器诊断告警（JSON 列 → 数组回传，畸形数据容错为空数组）
    for r in rows:
        r["parse_warnings"] = _decode_parse_warnings(r.get("parse_warnings"))
        # ✅ F2（2026-09-26）：优先读取持久化的解析器级截断标记；旧库（该列为空）
        #    回退到按字数反推，覆盖两种口径，避免漏报「PDF 截页/表格截行」类截断。
        r["truncated"] = doc_is_truncated(r)
    return {"documents": rows}


async def load_parsed_docs(db, project_id: str,
                           limit: int | None = None,
                           truncated_out: list | None = None) -> list[tuple[str, str]]:
    """✅ 唯一读取入口：按上传先后取该项目【已解析】文档的 (file_name, 正文)。

    - 仅返回正文非空的文档；`limit=None` 表示不限份数（全量，供事实提取使用）。
    - 「非空」条件下推到 SQL、并按 (created_at, id) 固定排序 —— 旧写法
      （先 LIMIT 再在 Python 里过滤空值）会让新上传的未解析文档占满名额，
      已解析资料一份都取不到，生成端静默退化成"只有工程类型 + 项目名称"。
    - 需要**文件名**的调用方（如事实提取要拼 `=== 文件名 ===` 做来源标注）用本
      函数；只需要正文的用 load_parsed_texts（它委托到本函数）。

    ✅ 2026-09-30（第十四轮 · P0 静默截断）：本函数此前只取
    ``(file_name, parsed_markdown)``、**不读** ``parse_truncated`` ——
    而解析阶段已把「PDF 截页 / 表格截行 / 落库字数超限」写进该列。于是目录
    生成（limit=5）与正文生成（limit=3）都可能在**残缺文本**上工作，而用户
    只看到「生成成功」。（事实提取链路已在 §4.18.2 单独收口。）
    现按实际列集合动态拼装（缺列只降级该列），并：
      · 恒记一条 WARNING（即使调用方不传 ``truncated_out``，也能在日志里
        看到「本次生成用了截断文档」）——**可观测性不应依赖调用方记得传参**；
      · 调用方传入 ``truncated_out`` 列表时，把被截断的文件名写进去自行处置。

    截断**不阻止**使用：残缺依据仍比没有依据好，静默丢文档会让生成直接失败。
    """
    if not project_id:
        return []
    base = ("SELECT file_name, parsed_markdown FROM project_documents "
            "WHERE project_id=? AND parsed_markdown IS NOT NULL AND parsed_markdown!='' "
            "ORDER BY created_at, id")
    tail = ""
    params: list = [project_id]
    if limit is not None:
        try:
            n = max(int(limit), 1)
        except (TypeError, ValueError):
            n = 5
        tail = " LIMIT ?"
        params.append(n)

    pairs, truncated = await _query_docs_with_diag(db, base, tail, tuple(params))
    if truncated:
        logger.warning(
            "生成链路使用了 %d 份被截断的文档（依据不完整）：%s；"
            "建议在「全局事实」页重新解析或拆分文件后重新上传",
            len(truncated), "、".join(truncated[:5]))
        if truncated_out is not None:
            truncated_out.extend(truncated)
    return pairs


async def _query_docs_with_diag(db, base: str, tail: str, params: tuple):
    """按可用列动态拼装 SQL 读取已解析文档，返回 ``(pairs, truncated_names)``。

    三层降级（任一失败都不阻断读路径）：
    ① 有 ``parse_truncated`` 列 → 直接读；
    ② 仅有 ``parse_warnings`` 列（旧库）→ 按告警文本含「截断」兜底；
    ③ 两列都没有 / 读失败 → 只取基础两列（= 引入前行为）。
    """
    pairs: list[tuple[str, str]] = []
    truncated: list[str] = []
    try:
        cols: set = set()
        try:
            cur = await db.execute("PRAGMA table_info(project_documents)")
            rows = await cur.fetchall()
            cols = {str(r[1]) for r in rows} if rows else set()
        except Exception:  # pragma: no cover
            cols = set()
        has_trunc = "parse_truncated" in cols
        has_warn = "parse_warnings" in cols
        if not (has_trunc or has_warn):
            cur = await db.execute(base + tail, params)
            for r in await cur.fetchall():
                if r[1]:
                    pairs.append((r[0] or "", r[1]))
            return pairs, truncated
        extra = ([("parse_truncated")] if has_trunc else []) + \
                ([("parse_warnings")] if has_warn else [])
        sql = ("SELECT file_name, parsed_markdown, " + ", ".join(extra)
               + " FROM project_documents "
               "WHERE project_id=? AND parsed_markdown IS NOT NULL AND parsed_markdown!='' "
               "ORDER BY created_at, id" + tail)
        cur = await db.execute(sql, params)
        for r in await cur.fetchall():
            d = dict(r)
            if not d.get("parsed_markdown"):
                continue
            name = d.get("file_name") or ""
            pairs.append((name, d["parsed_markdown"]))
            # ✅ 2026-10-05 修复（跨模块口径断裂）：改用统一的
            #    doc_is_truncated（四信号取「或」），与 bid_analysis / 列表 /
            #    预览同口径。旧实现是互斥 if/elif，只在「无 parse_truncated
            #    列」时才看告警文本，导致两类存量行漏报：
            #      ① parse_truncated=0（该列上线前的存量行）但 parse_warnings
            #         含「截断」；
            #      ② 旧版按 80000 字截断、告警已丢失，仅凭落库长度等于
            #         历史/当前上限可识别。
            #    漏报会让目录/正文/事实生成链路看不到「依据不完整」，
            #    truncated_out 恒空、WARNING 不落日志。
            if doc_is_truncated(d):
                truncated.append(name)
    except Exception as e:  # noqa: BLE001 - 诊断列读取失败不应阻断生成
        logger.warning("读取文档截断诊断失败（按未截断处理）: %s", e)
        pairs = []
        # ⚠️ 兜底本身也必须包 try：DB 整体不可用时，「再查一次基础列」会抛第二
        #    个异常并**穿透**出去，把「降级为不判截断」变成「提取/生成直接崩」。
        try:
            cur = await db.execute(base + tail, params)
            for r in await cur.fetchall():
                if r[1]:
                    pairs.append((r[0] or "", r[1]))
        except Exception as e2:  # noqa: BLE001
            logger.warning("读取已解析文档失败（降级为空）: %s", e2)
    return pairs, truncated


async def load_parsed_texts(db, project_id: str, limit: int = 5,
                            truncated_out: list | None = None) -> list[str]:
    """（兼容入口）只返回已解析文档的正文，最多 limit 份。

    ✅ 统一入口：完整语义见 load_parsed_docs —— 本函数委托它，避免"已解析文档"
    的 SQL 在多处各写一份而漂移（历史上正是这种漂移导致过资料取不到）。

    ``truncated_out``（可选，2026-09-30 第十四轮新增）：被截断文档的文件名列表
    出参，供调用方把「依据不完整」显式告知用户；不传时 ``load_parsed_docs``
    仍会恒记 WARNING，可观测性不依赖调用方记得传参。
    """
    return [text for _name, text in
            await load_parsed_docs(db, project_id, limit, truncated_out)]


def _decode_parse_warnings(raw) -> list[str]:
    """把 parse_warnings 列（JSON 数组字符串）安全解码为字符串数组。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(w) for w in parsed if w]


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, db=Depends(get_db)):
    cur = await db.execute(
        "SELECT file_path, project_id FROM project_documents WHERE id=?", (doc_id,))
    # ✅ R13 读路径补漏（2026-10-05）：cursor 为 None 时下方 fetchone 抛
    #    AttributeError → 500。也不得降级成「无此文档」——那会把 DB 瞬时
    #    故障误报成 404「文档不存在」，用户以为文件丢了。503 = 可重试。
    if cur is None:
        logger.warning("删除文档前查询失败（db.execute 返回 None），doc=%s", doc_id)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    # 只删除事实上传目录内的文件。历史/手工写入的异常路径只清理数据库，
    # 避免删除接口被利用为任意文件删除器。
    fpath = row["file_path"] if "file_path" in row.keys() else ""
    candidate = _managed_upload_path(fpath)
    if candidate and candidate.exists():
        try:
            candidate.unlink()
        except OSError as e:
            logger.warning("删除原始文件失败（忽略）: %s", e)
    elif fpath:
        logger.warning("拒绝删除上传目录外的文档路径: %s", fpath)
    # ✅ R13 写路径补漏（2026-10-05）：DELETE 未生效仍回 ok=True 是「假成功」
    #    —— 前端提示已删除、列表却仍在，且四层产物与事实 stale 都被错误清理。
    del_doc_cur = await db.execute(
        "DELETE FROM project_documents WHERE id=?", (doc_id,))
    if del_doc_cur is None:
        logger.warning("删除文档未生效（db.execute 返回 None，R13），doc=%s", doc_id)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    # ✅ R72（2026-10-12）补 rowcount：上方 SELECT 已确认行存在，DELETE 却影响
    #    0 行 = 预检与写之间目标行被并发删除（多标签页同时删 / 删项目级联插队）。
    #    旧实现只拦 None（503）、未拦 0 行，仍回 ok=True 并继续 purge →
    #    与 upload_outline._r13_write、本文件已确立的「行数不足」口径不一致。
    if safe_rowcount(del_doc_cur, what=f"删除文档 {doc_id}") < 1:
        logger.warning("删除文档 0 行生效（并发已删），doc=%s", doc_id)
        raise HTTPException(404, "文档不存在或已被删除")
    await db.commit()
    # ✅ 四层存储清理：解析层/提取层/语义层目录 + doc_chunks/doc_extractions/
    #    doc_validation_reports 关联行 + 文档索引条目（项目级事实不删，多文档共享）
    proj_id = row["project_id"] if "project_id" in row.keys() else ""
    if proj_id:
        try:
            await doc_pipeline.purge_document(db, doc_id=doc_id,
                                              project_id=proj_id)
        except Exception:
            logger.exception("文档 %s 四层产物清理失败（记录已删除）", doc_id)
        # ✅ 跨模块链路收敛：删除源文档后旧事实立即退出生成链路，并失效所有方案缓存。
        stale_count = await _mark_project_facts_stale(db, proj_id)
        await db.commit()
        await _invalidate_project_export_caches(db, proj_id)
        logger.info("删除文档 %s，标记项目 %s 的 %d 条事实为 stale",
                    doc_id, proj_id, stale_count)
    return {"ok": True, "stale_facts": stale_count if proj_id else 0}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ✅ 分步工作流（2026-09）：
#    ① 上传保存（仅存原始文件，不解析不提取）
#    ② 解析（用户点击 → parse_file_content → parsed_markdown 落库）
#    ③ 提取（用户点击 → SSE /sse/generate-facts 基于已解析文档提取）
#    ④ 修改（列表编辑/确认/删除，既有功能）
# ---------------------------------------------------------------------------

# ✅ 历史落库上限。早期版本按 80000 字截断，这些文档的长度恰好等于旧上限，
#    若只按当前 MAX_PARSED_CHARS 判定会被误判为"未截断" → 前端永远不显示
#    「重新解析」入口，旧截断文档无法自愈。
_LEGACY_PARSE_LIMITS: tuple[int, ...] = (80_000,)


def _is_truncated(text_len) -> bool:
    """判断文档文本是否疑似被截断（当前上限 或 历史上限 命中）。"""
    try:
        n = int(text_len or 0)
    except (TypeError, ValueError):
        return False
    if n <= 0:
        return False
    # 注意：本函数入参是**已落库文本的长度**，而落库时 `stored = text[:MAX]` ——
    # 只要发生过截断，落库长度就恰好等于 MAX。故此处必须用 `>=`（用 `>` 会漏判
    # 所有真实截断），与写入侧 `len(text) > MAX` 判定互为补充、并不矛盾。
    return n >= MAX_PARSED_CHARS or n in _LEGACY_PARSE_LIMITS


#: 解析告警里表示「内容不完整」的关键词（中英都认，兼容历史/外部写入的告警文本）。
_TRUNCATION_HINTS: tuple[str, ...] = ("截断", "truncated")


def doc_is_truncated(row) -> bool:
    """判断一行 ``project_documents`` 记录的正文字数 / 告警是否表明「被截断」。

    ✅ 2026-10-05 单一口径收口：本模块的列表（``list_documents``）/ 预览
    （``preview_document``）/ 正文读取（``_query_docs_with_diag``）此前各拼一套
    口径，且与 :mod:`app.routers.bid_analysis` 的 ``_doc_is_truncated`` 不一致 ——
    同一份文档在「列表页」判截断、在「生成链路」判不截断。现全部走本函数，
    与 ``bid_analysis._doc_is_truncated`` 行为等价（由
    tests/test_parse_truncation_chain.py 的 parity 用例钉住）。

    四类信号取「或」，任一命中即为截断：

    ① ``parse_truncated`` 持久化标记（解析阶段写入，覆盖「字数超上限」与
       「PDF 截页 / 表格截行」两类解析器级截断）；
    ② ``text_len == -1`` 旧版「入库时被截断」哨兵值（既有契约，保留对旧数据 /
       外部写入方无害）；
    ③ 字数达落库上限（当前 ``MAX_PARSED_CHARS`` 或历史 80000 字）—— 与
       ``_is_truncated`` 同口径；无 ``text_len`` 列时按 ``len(parsed_markdown)``
       回推（写侧 ``stored = text[:MAX]``，故发生截断则落库长度恰为上限）；
    ④ ``parse_warnings`` 文本兜底（中英都认，兼容历史/外部写入；也接受已被
       ``_decode_parse_warnings`` 解成数组的形态）。

    ⚠️ 必须用「或」而非互斥分支：``parse_truncated`` 列是后补的，上线前的存量
    行该列恒为 0，却可能早已在告警里留下截断提示 —— 把它当成排除条件会让这批
    文档继续漏报（正是本函数要修复的缺陷）。
    """
    pt = row.get("parse_truncated")
    if isinstance(pt, (int, float)) and not isinstance(pt, bool) and pt:
        return True
    text_len = row.get("text_len")
    if text_len is None:
        md = row.get("parsed_markdown")
        text_len = len(md) if isinstance(md, str) else 0
    if text_len == -1:
        return True
    if _is_truncated(text_len):
        return True
    warnings = row.get("parse_warnings")
    if not isinstance(warnings, str):
        warnings = " ".join(str(w) for w in warnings) if warnings else ""
    return any(k in warnings for k in _TRUNCATION_HINTS)



# Windows 保留设备名：不区分大小写，且「主名命中即保留」，带任意扩展名
# （CON.txt / NUL.doc）在 Windows 上仍指向设备，落盘会失败或写入错误位置。
_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


#: ✅ R145（2026-10-29，L2）：清洗后文件名长度上限。Windows MAX_PATH=260，
#:    落盘路径 = FACT_UPLOADS_DIR / project_uuid(36) / doc_id[:8]_(9) + 文件名，
#:    叠加安装目录深度后，文件名超过 120 字符即可能令 open("wb") 抛 OSError
#:    → 整批上传 500（用户看不出是名字太长）。截断只影响极端输入，正常文件名
#:    （≤120 字符）逐字节不变；极端同名前缀碰撞属可接受残余（与既有同名替换
#:    语义一致，后者同名即替换）。
_SAFE_FILENAME_MAX = 120


def _safe_filename(raw: str) -> str:
    """清洗上传文件名：剥离路径（防目录穿越）、去除非法字符与控制字符、
    规避 Windows 保留设备名、截断超长名（保留扩展名）。

    ✅ BUG 修复：旧实现直接用客户端提供的 filename 参与路径拼接与落盘。
    部分客户端（旧版 IE/某些 HTTP 库）会携带完整路径（如 "C:\\a\\b.docx"），
    在 Windows 上会导致落盘路径被篡改；控制字符与通配符亦会造成写盘失败。
    ✅ 加固：CON/NUL/PRN/AUX/COM1-9/LPT1-9（含带扩展名形式）此前漏网，
    在 Windows 落盘时会命中设备名（如 NUL）导致文件静默丢失；统一加下划线前缀。
    ✅ R145（L2）：超长文件名（>120 字符）截断并保留扩展名 —— 旧实现不截断，
    极端文件名在写盘时抛 OSError → 整批 500，用户无从得知原因。
    """
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    if not name:
        return "uploaded_file"
    stem = name.split(".", 1)[0].strip().upper()
    if stem in _WINDOWS_RESERVED_STEMS:
        name = f"_{name}"
    if len(name) > _SAFE_FILENAME_MAX:
        _stem, _dot, _ext = name.rpartition(".")
        if _dot and 0 < len(_ext) <= 16:
            name = f"{name[:_SAFE_FILENAME_MAX - len(_ext) - 1]}.{_ext}"
        else:
            name = name[:_SAFE_FILENAME_MAX]
        name = name.strip().strip(".") or "uploaded_file"
    return name


def _signature_valid(ftype: str, prefix: bytes) -> bool:
    """对常见二进制格式做轻量文件头校验，拒绝扩展名伪造。

    ✅ 增强：签名表已抽到 `file_parser.signature_valid` 统一维护 ——
    目录识别上传链路（upload-outline）此前完全没有这道校验，两条上传
    路径的防护口径不一致。此处保留原函数名以兼容既有调用/测试。
    """
    return signature_valid(ftype, prefix)


async def _stream_to_disk(f: UploadFile, dest: Path, limit: int) -> int:
    """流式落盘：分块读取，避免整份文件（可达 30MB×N）一次性驻留内存。

    返回写入字节数；超过 limit 时删除半成品并返回 -1。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with dest.open("wb") as out:
        while True:
            chunk = await f.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                break
            out.write(chunk)
    if total > limit:
        try:
            dest.unlink()
        except OSError:
            pass
        return -1
    return total


async def _resolve_project_id(db, scheme_id: str, project_id: str) -> str:
    """解析并校验资料作用域，禁止客户端伪造跨项目 scheme_id。"""
    # 只接受真正的字符串：直接以函数方式调用路由（测试 / 内部复用）时，
    # FastAPI 的 Query(...) 默认值会以【对象】形式传入，str() 化后得到
    # "Query('')" 这种假值 → 被误判为"scheme_id 与 project_id 不匹配"，
    # 报错信息完全指向错误方向。非字符串一律按"未提供"处理。
    sid = scheme_id.strip() if isinstance(scheme_id, str) else ""
    pid = project_id.strip() if isinstance(project_id, str) else ""
    if sid:
        cur = await db.execute(
            SCHEME_PROJECT_ID_SQL, (sid,))
        # ✅ R13 读路径补漏（2026-10-05）：本函数是所有 documents 接口的
        #    作用域入口，cursor 为 None 时 fetchone 抛 AttributeError → 500；
        #    也不得降级成「方案不存在」（会把 DB 瞬时故障误报为 404）。
        if cur is None:
            logger.warning("解析 scheme 作用域失败（db.execute 返回 None），sid=%s", sid)
            raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
        row = await cur.fetchone()
        # ✅ F6 续（2026-10-08）：取值走 row_field（键名优先、序列兜底），
        #   Mapping 行不再抛 KeyError；判定口径与旧 `not row or not row[0]` 逐字一致。
        real_pid = row_field(row, "project_id")
        if not real_pid:
            raise HTTPException(404, "方案不存在")
        if pid and pid != real_pid:
            raise HTTPException(400, "scheme_id 与 project_id 不匹配")
        return real_pid
    if pid:
        cur = await db.execute("SELECT id FROM projects WHERE id=?", (pid,))
        if cur is None:
            logger.warning("解析 project 作用域失败（db.execute 返回 None），pid=%s", pid)
            raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
        if not await cur.fetchone():
            raise HTTPException(404, "项目不存在")
        return pid
    return ""


def _doc_file_path(project_id: str, doc_id: str, fname: str) -> Path:
    safe_name = _safe_filename(fname)
    # 延迟从 global_facts 命名空间读 FACT_UPLOADS_DIR，兼容 monkeypatch
    from app.routers import global_facts as _gf
    return _gf.FACT_UPLOADS_DIR / project_id / f"{doc_id[:8]}_{safe_name}"


def _managed_upload_path(raw_path: str | Path) -> Path | None:
    """返回事实上传目录内的安全路径，目录外路径返回 None。"""
    if not raw_path:
        return None
    try:
        from app.routers import global_facts as _gf
        root = _gf.FACT_UPLOADS_DIR.resolve()
        candidate = Path(raw_path).resolve()
        candidate.relative_to(root)
        return candidate
    except (OSError, ValueError, RuntimeError):
        return None


@router.post("/upload-documents")
async def upload_documents(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    files: list[UploadFile] = File(...),
    db=Depends(get_db),
):
    """① 上传保存：仅保存原始文件与档案记录，不做解析与提取。

    同名文件重复上传时替换旧记录（含旧文件删除）。
    """
    from app.routers import global_facts as gf
    # 动态读配额常量，兼容 monkeypatch.setattr(gf, "MAX_UPLOAD_*", N)
    max_files = gf.MAX_UPLOAD_FILES_PER_REQUEST
    max_bytes = gf.MAX_UPLOAD_BYTES
    max_total = gf.MAX_UPLOAD_TOTAL_BYTES
    real_pid = await _resolve_project_id(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")

    # ✅ 2026-09-23 日志埋点：上传入口生成全链路 trace_id，并绑定 project 上下文，
    #    使后续解析 / 提取 / 保存 / 分类的日志都能凭 trace_id 串联（见 log_context）。
    set_context(trace_id=new_trace_id(), project_id=real_pid, scheme_id=scheme_id)

    saved: list[dict] = []
    oversize: list[str] = []
    unsupported: list[str] = []
    signature_invalid: list[str] = []
    empty_files: list[str] = []
    replaced = 0
    written: list[Path] = []  # 已落盘文件，整体异常时回滚，避免产生孤儿文件
    old_paths_to_delete: list[Path] = []  # DB 提交成功后再清理，保证可回滚
    # ✅ BUG 修复（同名替换孤儿泄漏）：被替换旧文档只删了 project_documents 行，
    #    其 doc_chunks 行、提取/校验产物与四层磁盘目录无人清理 ——
    #    /documents/{id}/chunks 等按 doc_id 查询的入口虽在，索引却查不到该文档，
    #    旧块与旧层目录永久残留（占空间且与「删除文档即 purge_document」口径不一致）。
    #    与旧原件同策略：commit 成功后再清理，失败可回滚。
    purged_old_doc_ids: list[tuple[str, str]] = []  # (doc_id, project_id)

    # ✅ 配额：单请求文件数上限。超出部分直接拒绝，不做任何落盘。
    too_many: list[str] = []
    if len(files) > max_files:
        too_many = [(x.filename or "uploaded_file")
                    for x in files[max_files:]]
        files = files[:max_files]
    total_bytes = 0
    quota_exceeded = False
    # ✅ 回归（2026-09-21）：累计体积超限被拒的文件必须归入 quota_files 独立上报，
    #    不得混进 oversize —— oversize 语义是「单文件超 30MB」，前端据此提示
    #    「超过 30MB」；一个 8 字节文件因累计配额被拒时混入 oversize 会让用户
    #    误以为文件太大去拆分重试（错误归因）。
    quota_files: list[str] = []

    try:
        for _idx, f in enumerate(files):
            raw_name = f.filename or "uploaded_file"
            fname = _safe_filename(raw_name)
            ftype = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            # 格式预检：不支持的扩展名直接拒绝，避免存了却解析不了
            if ftype and ftype not in SUPPORTED_EXTENSIONS:
                unsupported.append(raw_name)
                continue
            # 大小预检：优先用 Content-Length（f.size），避免大文件白读一遍
            declared_size = getattr(f, "size", None)
            if declared_size and declared_size > max_bytes:
                oversize.append(raw_name)
                continue
            # 文件头校验只读取极小前缀并复位指针，不改变后续流式落盘。
            prefix = await f.read(16)
            await f.seek(0)
            if not _signature_valid(ftype, prefix):
                signature_invalid.append(raw_name)
                continue
            doc_id = str(uuid.uuid4())
            fpath = _doc_file_path(real_pid, doc_id, fname)
            # ✅ 优化：流式落盘（分块），旧实现 `await f.read()` 会把整份文件
            #    读入内存——多文件同时上传时峰值 = N × 30MB，易触发 OOM。
            size = await _stream_to_disk(f, fpath, max_bytes)
            if size < 0:
                oversize.append(raw_name)
                continue
            if size == 0:
                # ✅ 修复：0 字节文件既占档案位又注定解析失败，直接拒绝并说明
                try:
                    fpath.unlink()
                except OSError as e:
                    logger.warning(
                        "清理空文件 %s 失败（可能残留孤儿文件）: %s",
                        fpath, e, exc_info=True)
                empty_files.append(raw_name)
                continue
            # ✅ 配额：累计体积上限。已落盘文件需立即清理，避免孤儿文件。
            if total_bytes + size > max_total:
                try:
                    fpath.unlink()
                except OSError:
                    pass
                quota_files.append(raw_name)
                quota_exceeded = True
                # ✅ P2（2026-10-05 · 静默丢文件名）：`break` 不只停掉本文件，
                #    还会跳过**排在触发文件之后的全部剩余文件** —— 它们既不在
                #    too_many / oversize / unsupported 里，也不在 quota_files 里。
                #    实测：一次上传 10 个文件、第 3 个触顶时，响应里只点名 1 个，
                #    其余 6 个在任何字段里都不出现（用户以为传上去了，刷新后也
                #    找不到，且没有任何提示说明它们没被保存）。
                #    这些文件与触发文件同因（累计配额触顶后停止处理）未保存，
                #    一并登记进 quota_files —— 既有的提示文案「超出部分未保存
                #    （…）」无需改动即可覆盖，前端 `quota_files` 消费逻辑不变。
                quota_files.extend((x.filename or "uploaded_file")
                                   for x in files[_idx + 1:])
                break
            total_bytes += size
            written.append(fpath)

            # 同名替换：先删旧记录，旧文件延迟到 DB commit 成功后清理。
            # 若后续文件/数据库失败，rollback 后旧记录和旧文件仍可用。
            cur = await db.execute(
                "SELECT id, file_path FROM project_documents "
                "WHERE project_id=? AND file_name=?",
                (real_pid, fname))
            # ✅ P1（R13 漏改 · 2026-10-05）：返回 None 时下方 `cur.fetchall()`
            #    抛 AttributeError，被外层 except 接住 → rollback + 原样上抛 →
            #    用户拿到 500 且只有一句「上传保存失败」，看不出是数据库瞬时
            #    故障（可重试）还是自己的文件有问题。改抛 503（可重试）。
            #    外层 except 的回滚 + 已落盘文件清理仍会执行，不会留孤儿。
            if cur is None:
                logger.warning("同名文档查询失败（db.execute 返回 None），project=%s", real_pid)
                raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
            for old in await cur.fetchall():
                old_path = old["file_path"] if "file_path" in old.keys() else ""
                safe_old = _managed_upload_path(old_path)
                if safe_old and safe_old != fpath:
                    old_paths_to_delete.append(safe_old)
                elif old_path:
                    logger.warning("跳过上传目录外旧文件清理: %s", old_path)
                # ✅ P1（R13 写路径 · 2026-10-05）：DELETE 未生效就继续往下走，
                #    会让同名旧档案行与新 INSERT 并存 → 列表里出现两条同名文档、
                #    下游按 file_name 去重/取数的结果不确定。整批拒绝并回滚。
                del_cur = await db.execute(
                    "DELETE FROM project_documents WHERE id=?", (old["id"],))
                if del_cur is None:
                    logger.warning("同名替换删除旧档案未生效（db.execute 返回 None，R13），"
                                   "old=%s", old["id"])
                    raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
                # ✅ R144（2026-10-29）：同名替换的旧档案 DELETE 只判 None（R13），
                #    未校验影响行数 —— 连接池每请求独立连接下，两个并发同名上传
                #    会各自 SELECT 到同一旧行、各自 DELETE（其一影响 0 行不被察觉）、
                #    各自 INSERT → 同一 project+file_name 出现两行，下游按 file_name
                #    去重/取数结果不确定。补 safe_rowcount 守卫（R72 口径）：0 行 =
                #    该旧行已被并发请求删除，中止本请求（回滚），避免重复建档。
                if safe_rowcount(del_cur,
                                 what=f"同名替换删除旧档案 {old['id']}") < 1:
                    logger.warning(
                        "同名替换删除旧档案影响 0 行（疑似并发同名上传已删除该行），"
                        "中止本次上传避免重复建档，old=%s", old["id"])
                    raise HTTPException(409, "同名文档正在被其它请求处理，请稍后重试")
                purged_old_doc_ids.append((str(old["id"]), real_pid))
                replaced += 1
            # 落库（parsed_markdown 留空 = 待解析）
            # ✅ 增强：自动文件分类 + 文件大小记录（供前端「文件导入」Tab 展示）
            doc_category = _auto_classify_document(fname, ftype)
            ins_cur = await db.execute(
                "INSERT INTO project_documents "
                "(id, project_id, file_name, file_type, doc_type, parsed_markdown, file_path, "
                "doc_category, file_size) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (doc_id, real_pid, fname, ftype, "全局事实上传", "", str(fpath),
                 doc_category, size))
            # ✅ P1（R13 写路径 · 2026-10-05）：INSERT 返回 None = 档案行没写进去，
            #    而本函数的返回值是靠 `saved.append(...)` 在内存里攒的 —— 不拦就会
            #    回 `{"saved_count": N}`（前端显示「已保存 N 个」）而库里一行没有，
            #    文件躺在磁盘上成了永远查不到、删不掉的孤儿。这是本模块 R13 唯一
            #    一处「假成功」出口，必须在 append 之前拦。503 让用户整批重试
            #    （外层 except 回滚 + 清理已落盘文件，不留半截状态）。
            if ins_cur is None:
                logger.warning("上传档案 INSERT 未生效（db.execute 返回 None，R13），"
                               "project=%s file=%s", real_pid, fname)
                raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
            # ✅ 四层存储（阶段1 原文层）：指纹入库 + meta 落盘 + 文档索引。
            #    失败只降级告警，不阻断既有上传主链路（DB 档案已建）。
            try:
                meta = await doc_pipeline.ingest_upload(
                    doc_id=doc_id, project_id=real_pid, file_name=fname,
                    file_type=ftype, saved_path=fpath, size=size,
                    doc_category=doc_category)
                if meta:
                    hash_cur = await db.execute(
                        "UPDATE project_documents SET file_hash_md5=?,"
                        " file_hash_sha256=?, parse_status='pending' WHERE id=?",
                        (meta.get("file_hash_md5", ""),
                         meta.get("file_hash_sha256", ""), doc_id))
                    # ✅ P1（R13 写路径 · 2026-10-05）：返回 None 时指纹回写静默
                    #    丢失 → 时效性判定（file_changed / 增量跳过）从此失真且
                    #    无法自愈。落库已成功，故只告警不阻断上传主链路。
                    if hash_cur is None:
                        logger.warning(
                            "上传后回写文件指纹未生效（db.execute 返回 None，R13）"
                            "，时效性判定将停留在旧值：%s", doc_id)
            except Exception:
                logger.exception("文档 %s 原文层入库失败（不影响上传）", fname)
            # ✅ R136（2026-10-27）：`saved[]` 补齐 file_type / doc_category，与
            #    `GET /documents` 的字段集对齐。旧实现只回 id/file_name/size ——
            #    前端拿到「已保存」后想用文件类型渲染图标、或想知道自动分类结果，
            #    必须再拉一次列表；对接目录识别链路的调用方更是完全拿不到类型。
            #    纯加法式补键（既有消费方只读 saved_count / rejected 列表，零影响）。
            saved.append({"id": doc_id, "file_name": fname, "size": size,
                          "file_type": ftype, "doc_category": doc_category})
    except Exception:
        # ✅ BUG 修复（悬空事务）：循环中途抛异常时，此前执行的 DELETE（同名替换）
        #    与 INSERT（档案记录）已在连接的隐式事务中排布但【尚未 commit】。
        #    旧实现只清理磁盘文件、不 rollback —— 连接归还写池后被下一个请求复用，
        #    这些残留语句会随该请求的 commit 一并提交，产生"幽灵删除/幽灵新增"。
        try:
            await db.rollback()
        except Exception:
            # 连接已损坏（如 disk I/O）时忽略，交由连接池剔除逻辑处理
            pass
        # ✅ 修复：旧实现在循环中抛异常时，已写盘的文件没有对应 DB 记录，
        #    成为永久孤儿文件（占空间且无法在界面删除）。异常时统一清理。
        for p in written:
            try:
                p.unlink()
            except OSError:
                pass
        logger.exception("上传保存失败，已回滚 %d 个已落盘文件", len(written))
        raise

    await db.commit()
    # 新记录已提交后再删除旧文件；删除失败不影响已提交的数据库状态。
    for old_path in old_paths_to_delete:
        try:
            old_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("清理被替换的旧上传文件失败: %s (%s)", old_path, e)
    # 被替换旧文档的四层产物同步清理（与手动删除文档同一 purge 口径）。
    for old_doc_id, old_pid in purged_old_doc_ids:
        try:
            await doc_pipeline.purge_document(db, doc_id=old_doc_id,
                                              project_id=old_pid)
        except Exception:
            logger.warning("清理被替换旧文档 %s 的四层产物失败", old_doc_id)
    # ✅ R144（2026-10-29，P2）：同名替换删除了旧档案 → 旧文档内容退出项目资料
    #    全集，与 delete_document / parse_document（R79 A-4）同属「输入全集变化」。
    #    旧实现只 purge 四层产物、不标记事实 stale、不失效导出缓存 → 生成链路
    #    继续用「基于已被替换文档内容」的旧事实（is_stale=0），直到新文件被
    #    重新解析提取才自愈。补同口径失效（幂等 UPDATE + 按 scheme 失效缓存）。
    replaced_stale = 0
    if replaced > 0:
        replaced_stale = await _mark_project_facts_stale(db, real_pid)
        await db.commit()
        await _invalidate_project_export_caches(db, real_pid)
    result: dict = {"ok": True, "saved": saved,
                    "saved_count": len(saved),
                    "replaced": replaced}
    if replaced > 0:
        result["stale_facts"] = replaced_stale
    warnings: list[str] = []
    if oversize:
        result["oversize"] = oversize
        warnings.append(
            f"以下文件超过 {max_bytes // (1024 * 1024)}MB 未保存：{', '.join(oversize)}")
    if unsupported:
        result["unsupported"] = unsupported
        warnings.append(
            f"以下文件格式不支持（支持 {', '.join(sorted(SUPPORTED_EXTENSIONS))}）：{', '.join(unsupported)}")
    if signature_invalid:
        result["signature_invalid"] = signature_invalid
        warnings.append("以下文件扩展名与文件头不匹配，未保存：" + ", ".join(signature_invalid))
    if empty_files:
        result["empty"] = empty_files
        warnings.append(f"以下文件为空（0 字节）已忽略：{', '.join(empty_files)}")
    if too_many:
        result["too_many"] = too_many
        warnings.append(
            f"单次最多上传 {max_files} 个文件，"
            f"以下 {len(too_many)} 个文件未处理：" + "、".join(too_many[:10]))
    if quota_exceeded:
        result["quota_exceeded"] = True
        # ✅ 独立字段：被累计体积上限拒绝的文件清单（勿混入 oversize）
        if quota_files:
            result["quota_files"] = quota_files
        warn_files = f"（{', '.join(quota_files[:10])}）" if quota_files else ""
        warnings.append(
            f"本次上传累计体积超过 {max_total // (1024 * 1024)}MB 上限，"
            f"超出部分未保存{warn_files}，请分批上传")
    if replaced:
        warnings.append(f"替换了 {replaced} 个同名旧文件")
    # ✅ R137（2026-10-28，遗留 L2）：扫描件/图片型资料 + 本地 OCR 缺失 ——
    #    上传当场告知，不等解析完再报。这类文件没有文字层，解析完全依赖
    #    OCR；旧实现只在「解析」阶段才以 fail-soft 400 / 告警暴露，用户传完
    #    等一轮解析才发现，还得自己猜是格式错还是引擎缺。走既有 warnings
    #    通道（前端 workflowDerived.summarizeUploadResult 已消费 → msg.info），
    #    零前端改动；只有「确定需要 OCR」的纯图像格式才判，故不会误报。
    _scan_saved = [str(s.get("file_name") or "") for s in saved
                   if str(s.get("file_type") or "").lower() in _OCR_ONLY_TYPES]
    if _scan_saved and not _local_ocr_available():
        _show = ", ".join(_scan_saved[:5])
        _more = f" 等 {len(_scan_saved)} 个" if len(_scan_saved) > 5 else ""
        warnings.append(
            f"{_show}{_more} 是无文字层的图片/扫描件，识别需依赖 OCR，"
            "但当前未启用也未检测到本地 OCR 引擎；若同时未配置视觉模型，"
            "解析将无法提取正文（启用方式见 /api/v1/diagnostics/capabilities）")
    if warnings:
        result["warnings"] = warnings
    return result


async def _ingest_parsed_doc(db, doc_id: str, project_id: str, file_name: str,
                             elapsed: float, diag: dict,
                             warnings: list[str], reparse: bool) -> dict:
    """✅ 四层存储（阶段2+3）：解析成功后落盘解析层产物 + 分块入 doc_chunks。

    失败只告警不阻断主链路（DB parsed_markdown 才是下游消费的唯一硬依赖，
    磁盘四层产物可通过 reparse 重建）。返回落盘摘要（可能为空 dict）。
    """
    try:
        cur = await db.execute(
            "SELECT parsed_markdown, parse_version FROM project_documents"
            " WHERE id=?", (doc_id,))
        r = dict(await cur.fetchone() or {})
        # 首次解析 prev='' → v1；重解析（源文档此前已成功解析过）→ 代次递增。
        # ✅ BUG 修复：旧版文档的 parse_version 列可能为空（四层存储上线前解析的），
        #    重解析时若拿空串当 prev_version，bump 不出来会被当成【首次解析】
        #    重置回 v1 —— 代次回退，时效性/增量判定永久失真。
        #    重解析路径上空版本一律视作 v1，本次落 v2。
        prev_version = (r.get("parse_version") or "v1") if reparse else ""
        md = r.get("parsed_markdown") or ""
        return await doc_pipeline.ingest_parse_result(
            db, doc_id=doc_id, project_id=project_id, file_name=file_name,
            markdown=md, page_count=int(diag.get("page_count") or 1),
            parse_duration_s=elapsed, parse_engine=str(diag.get("file_type") or ""),
            warnings=warnings, prev_version=prev_version,
            # ✅ R136（2026-10-27）：透传源文件真实总页数（仅 PDF 通道会上报）。
            #    缺省 0 = 未上报，下游保持「总页数退化为已解析页数」的旧行为。
            source_page_count=int(diag.get("source_page_count") or 0))
    except Exception:
        logger.exception("文档 %s 解析层/分块落盘失败（不影响解析结果）", file_name)
        return {}


# ✅ 上传解析模块并发守卫（2026-09-23）：同一文档可能被多个请求并发解析
#    （前端 parsingDocId/parsingDocs 仅防主路径；外部直连 / 多标签页 /
#    批量 parse-all 与单份 parse 同时触发会绕过前端守卫）。并发解析同一份文件
#    会导致：重复 OCR 资源争用（扫描件尤甚）、parsed_markdown 被后写者覆盖、
#    四层分块重复写入。逐文档 asyncio.Lock 串行化同文档解析；锁条目在最后一个
#    使用者退出后回收，避免随文档数无限增长。
_doc_parse_locks: dict[str, "_DocParseLockEntry"] = {}


class _DocParseLockEntry:
    """引用计数的逐文档解析锁条目（2026-10-10 竞态修复）。

    ✅ 旧实现：解析结束（含异常）后无条件从字典删除条目。当锁上有等待者排队时
    这样删对等待者本身安全 —— 他们已持有锁对象引用、会彼此串行；但【新到达】
    的请求 D 查不到条目会**新建一把锁**，与刚接棒持锁的 B 并发解析同一文档，
    恰好重新打开本守卫要堵的「重复 OCR / parsed_markdown 后写覆盖 / 四层分块
    重复写入」窗口（多标签页 + 批量 parse-all 与单份 parse 混发时可达）。

    现改为引用计数：进入守卫 +1、退出 -1，仅在计数归零（无人持有也无人排队）
    时才回收条目。事件循环单线程，「查→+1」与「-1→删」之间无 await，天然原子，
    不需要额外同步。
    """

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


def _get_doc_parse_lock(doc_id: str) -> asyncio.Lock:
    """兼容入口：返回该文档当前的解析锁（无条目则懒创建）。

    生产链路请使用 :func:`_doc_parse_guard`（带引用计数与回收）；本函数保留
    给既有调用点/测试按旧签名取锁。
    """
    entry = _doc_parse_locks.get(doc_id)
    if entry is None:
        entry = _DocParseLockEntry()
        _doc_parse_locks[doc_id] = entry
    return entry.lock


@asynccontextmanager
async def _doc_parse_guard(doc_id: str):
    """✅ 性能稳定性（2026-09-23）：解析并发守卫 + 锁条目自动回收。

    逐文档串行化解析（防并发重复 OCR / 后写覆盖），并在【最后一个使用者】
    退出后从 `_doc_parse_locks` 字典删除该条目（防随文档数无限增长的内存
    泄漏）。回收条件用引用计数而非「退出即删」：等待者排队期间新请求必须
    复用同一把锁（见 `_DocParseLockEntry` docstring 的竞态分析）。
    """
    entry = _doc_parse_locks.get(doc_id)
    if entry is None:
        entry = _DocParseLockEntry()
        _doc_parse_locks[doc_id] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users <= 0:
            # 「-1 → 判零 → 删」为同步段，同循环内无请求可插入；身份校验仅
            # 防御未来有人在该区间引入 await 时误删后来者新建的条目。
            if _doc_parse_locks.get(doc_id) is entry:
                _doc_parse_locks.pop(doc_id, None)


async def _reconcile_parse_status(db, doc_id: str) -> bool:
    """✅ BUG 修复（2026-09-25，解析结果「看起来丢了」/ 状态错乱）：

    把「正文非空但 parse_status 未置为 success」的陈旧行修正为 success。

    成因：``project_documents.parse_status`` 由 ``db._migrate`` 以
    ``TEXT DEFAULT 'pending'`` 增量补列 —— 四层存储上线前就已解析好的存量
    文档，正文躺在 ``parsed_markdown`` 里，状态列却被回填成 'pending'。
    而 ``parse_document`` / ``parse_all_documents`` 的「已解析」短路分支
    只回 ``already_parsed`` / 直接 ``continue``，**从不修正状态列**，于是：

      · 前端 ``computeDocStats`` 把这类文档恒计为「待解析」；
      · 点「解析」→ 后端回「已解析过」但列表标签仍是「待解析」；
      · 点「解析全部」→ 后端回「所有文档均已解析」、什么都没改；
      · 唯一出口是「全部重解析」(force)，白白重跑一遍 OCR。

    本函数在短路命中时顺带修正状态列：单条条件 UPDATE、幂等（已 success
    不重复写）、失败只告警不阻断解析主链路。

    :return: 是否实际执行了修正（True = 本次把状态从非 success 改为 success）。
    """
    try:
        cur = await db.execute(
            "UPDATE project_documents SET parse_status='success' "
            "WHERE id=? AND parsed_markdown IS NOT NULL AND parsed_markdown<>'' "
            "AND (parse_status IS NULL OR parse_status<>'success')",
            (doc_id,))
        await db.commit()
        # ✅ 2026-09-30 收敛到 safe_rowcount 单一出口（R13）：旧写法
        #   int(getattr(cur, "rowcount", 0) or 0) 不会抛 AttributeError，但
        #   execute() 返回 None 时**静默返回 0、零日志** —— 正是 AGENTS.md
        #   §4.12 要求消除的「写操作没生效却无任何日志」。safe_rowcount 会
        #   打带操作名的 WARNING，且把负 rowcount 归一为 0。
        # ⚠️ 本函数契约是 `-> bool`（调用方用 truthiness、测试用 `is False`），
        #    故必须显式转 bool：safe_rowcount 返回 int，直接返回会破坏契约
        #    （0 is False 恒为 False → 调用方与断言双双失真）。
        return bool(safe_rowcount(cur, what="单文档 parse_status 修正为 success"))
    except Exception:
        logger.exception("修正文档 %s 的解析状态失败（可忽略）", doc_id)
        return False


async def _reconcile_parse_status_all(db, project_id: str) -> int:
    """项目级批量修正陈旧 parse_status（见 :func:`_reconcile_parse_status`）。

    用于 ``parse_all_documents`` 入口：**放在 SELECT 待解析列表之前**，
    这样"所有文档均已解析"的早退分支也不会漏掉这批行。单条条件 UPDATE，
    只动"正文非空 + 状态非 success"的行；返回值供响应回传修正条数。
    """
    try:
        cur = await db.execute(
            "UPDATE project_documents SET parse_status='success' "
            "WHERE project_id=? AND parsed_markdown IS NOT NULL "
            "AND parsed_markdown<>'' AND (parse_status IS NULL "
            "OR parse_status<>'success')",
            (project_id,))
        await db.commit()
        # ✅ 2026-09-30 同上：收敛到 safe_rowcount（R13 静默失败 → WARNING）
        return safe_rowcount(cur, what="项目级 parse_status 批量修正")
    except Exception:
        logger.exception("批量修正项目 %s 的解析状态失败（可忽略）", project_id)
        return 0


async def _log_parse_event(*, event: str, level: str = "INFO",
                           doc_id: str = "", project_id: str = "",
                           duration_ms: int | None = None, status: str = "",
                           message: str = "",
                           detail: dict | None = None) -> None:
    """解析链路结构化事件（R157 · 2026-11-01 · 遗留 TD-7 便宜收口）。

    R136-L1 判「无 metrics 骨架不做」的前提已被 R142 推翻：observability.log_event
    （system_logs 结构化通道，fail-soft、自动补 trace/project 上下文、30 天保留）
    即是现成轨道 —— 解析失败率/中止率从此可在日志界面按 module='parse' 直接盘点，
    无需新建指标子系统。事件族：parse.failed / parse.done / parse.aborted /
    parse.batch。⚠️ 必须用 get_conn() 独立连接：log_event 内部 commit 所传入
    连接，若传请求级 db 会把失败路径上尚未回滚的半截事务提前固化。
    """
    try:
        from app.db import get_conn
        from app.services.observability import log_event
        conn = await get_conn()
        _d = dict(detail or {})
        if doc_id:
            _d["doc_id"] = doc_id
        await log_event(conn, level=level, module="parse", event=event,
                        project_id=project_id, duration_ms=duration_ms,
                        status=status, message=message or event, detail=_d)
    except Exception as e:  # noqa: BLE001 - fail-soft：可观测性绝不阻断解析主链路
        logger.warning("parse 事件写入 system_logs 失败（event=%s）: %s", event, e)


async def _mark_parse_failed(db, doc_id: str, reason: str) -> None:
    """✅ 解析失败可观测性（2026-09-23）：把 parse_status 置为 'failed' 并把失败
    原因写入 parse_warnings 持久化，使文档列表能区分"待解析"与"解析失败"。

    旧实现解析失败时既不写状态、也不留痕 —— 文档永远停留在 'pending'，前端
    恒显"待解析"，用户无从得知已失败、会反复重试同一份文件。失败原因随列表
    回传，前端可展示"⚠ 解析失败"标签与具体原因。

    失败写入独立于主解析链路；即便写入本身异常也不影响已经抛出的解析错误。

    ✅ FIX-4（2026-10-20 · P2 · 告警覆盖）：旧实现是**无条件覆盖**写
    ``parse_warnings`` ⇒ 重解析失败时把上一轮已持久化的告警
    （「原文 N 字，已截断至上限」/「PDF 共 N 页，仅解析前 M 页」/ OCR 兜底提示）
    **整条抹掉**。后果：用户重解析一份 300 页 PDF 失败后，原本「这份文档会被
    截断」的关键信息消失，文档只剩一条「解析失败」，无从判断该拆分还是该
    换引擎。失败原因是**追加事件**而非**当前状态**，不应抹掉历史告警。

    现改为「读旧值 → 追加失败原因 → 回写」，并保留去重（同一 reason 不重复堆叠）。
    为避免无界增长，回写仍经 ``dump_parse_warnings``（20 条 × 300 字容量护栏）。
    """
    try:
        # 先读旧告警（读失败降级为空，不阻断失败标记）
        old: list[str] = []
        _proj = ""
        try:
            cur0 = await db.execute(
                "SELECT parse_warnings, project_id FROM project_documents WHERE id=?",
                (doc_id,))
            if cur0 is not None:
                row0 = await cur0.fetchone()
                if row0:
                    old = _decode_parse_warnings(
                        row0["parse_warnings"] if "parse_warnings" in row0.keys()
                        else None)
                    if "project_id" in row0.keys():
                        _proj = row0["project_id"] or ""
        except Exception:
            old = []
        msg = f"解析失败：{reason}"
        # 去重追加：同一失败原因重复重试不堆叠
        merged = list(old)
        if msg not in merged:
            merged.append(msg)
        await db.execute(
            "UPDATE project_documents SET parse_status='failed', "
            "parse_warnings=? WHERE id=?",
            (dump_parse_warnings(merged), doc_id))
        await db.commit()
        # ✅ R157：解析失败结构化事件（TD-7 收口）—— module='parse' 单一咽喉
        #    覆盖单份/批量全部失败路径；project_id 缺省时由 log_event 的
        #    context_snapshot 兜底（上传入口已 set_context）。
        await _log_parse_event(
            event="parse.failed", level="WARNING", doc_id=doc_id,
            project_id=_proj, status="failed", message=reason)
    except Exception:
        logger.exception("标记文档 %s 解析失败状态失败（可忽略）", doc_id)


class ParseTargetGoneError(Exception):
    """解析目标文档在解析过程中被并发删除（落库 UPDATE 影响 0 行）。

    ✅ R145（2026-10-29，L4）：与「DB 瞬时不可用」（execute 返回 None → False
    → 重试）显式分离 —— 文档已不在，重试无意义、503 会引导用户重试一个不
    存在的文档。调用方（parse_document / parse_all_documents）捕获后走 404 /
    计入 failed，且**不写** failed 检查点（对已删文档写检查点即孤儿行）。
    """


async def _persist_parse_result(
    db,
    doc_id: str,
    stored: str,
    elapsed: float,
    warnings: list,
    diag: dict,
    truncated: bool,
) -> bool:
    """✅ 解析成功结果的唯一落库口径（重构 2026-10-04）。

    单文档解析与「解析全部」此前各写了一份字段完全相同的 UPDATE，历史上二者
    已多次发生口径漂移（parse_time / parse_truncated / 截断判定都曾不一致，
    对应「解析用时为空」「截断静默漏报」等已修复缺陷）。抽到此处后，写库字段
    只此一份，两条链路天然保持一致：
      - parsed_markdown：已按 MAX_PARSED_CHARS 截断的文本；
      - parse_time：解析耗时；parse_warnings：诊断告警（含截断说明）；
      - file_type：解析器按文件头嗅探的真实类型，空值不覆盖原值；
      - parse_status='success'：独立于四层入库成败，杜绝状态矛盾；
      - parse_truncated：字数截断或解析器级截断（PDF 截页/表格行数截断）。

    ✅ P1（R13 写路径漏改 · 2026-10-05）：本函数是**解析正文唯一的落库点**，
       返回值此前被丢弃 —— `db.execute()` 返回 None（R13）时 UPDATE 没执行，
       调用方照样往下走 `_ingest_parsed_doc`（它从库里读到空 parsed_markdown
       → 四层落空）、照样回 `{"ok": True, "text_len": N}`，**用户看着「解析
       成功、N 字」，刷新后列表里该文档仍是「待解析」、正文为 0 字**，且日志
       无任何痕迹。现改为显式返回是否写入成功，由调用方决定失败语义：
       单份解析 → 503（明确告知重试）；批量解析 → 计入 failed 并落
       parse_status='failed'（与其它失败原因同口径）。
    """
    cur = await db.execute(
        "UPDATE project_documents SET parsed_markdown=?, parse_time=?, parse_warnings=?,"
        " file_type=COALESCE(NULLIF(?, ''), file_type), parse_status='success',"
        " parse_truncated=? WHERE id=?",
        (stored, elapsed, dump_parse_warnings(warnings),
         str((diag or {}).get("file_type") or "").strip().lower(),
         int(bool(truncated)), doc_id))
    if cur is None:
        logger.warning(
            "解析结果落库未生效（db.execute 返回 None，R13），正文未写入：%s", doc_id)
        # ⚠️ 不 commit：没有成功的语句要提交，提交只会把上一次语句的残留
        #    事务上下文固化（见 doc_pipeline.py:322 同款告警的理由）。
        return False
    # ✅ R144（2026-10-29，P2）：UPDATE 只判 None（R13），不校验影响行数 ——
    #    并发 delete_document 在本函数读行与写库之间删掉该文档时，此 UPDATE
    #    命中 0 行仍返回 True → 调用方继续 _ingest_parsed_doc（读到空正文、
    #    重建已删 parsed 层）并 _save_parse_checkpoint 为已删文档写检查点，
    #    响应回 ok=True（幽灵成功）。补 safe_rowcount（R72 口径）：0 行 = 文档
    #    已不在（被并发删除）。
    # ✅ R145（2026-10-29，L4 契约精化）：R144 首版此处 return False —— 调用方
    #    无法区分「文档已删」与「DB 瞬时」，会写一行 failed 检查点（对已删文档
    #    即孤儿）并回 503（误导重试）。改为抛 ParseTargetGoneError：重试包装器
    #    不重试（结构性失败），调用方分流 404 / failed 且不写检查点。
    if safe_rowcount(cur, what=f"解析结果落库 {doc_id}") < 1:
        logger.warning(
            "解析结果落库影响 0 行（疑似文档已被并发删除），正文未写入：%s", doc_id)
        raise ParseTargetGoneError(doc_id)
    await db.commit()
    return True


# ✅ R123（2026-10-21）：解析断点续跑检查点 + 落库瞬时重试。
#    解析主流程（parse_document）分阶段（extract→persist→layers→stale）；失败时在
#    检查点缓存已提取文本，下次解析可从失败阶段续跑，跳过代价高昂的重新提取。
#    检查点表由 db.py::_migrate 幂等创建，纯辅助、删除即退回旧行为，无数据风险。
async def _load_parse_checkpoint(db, doc_id: str) -> dict | None:
    try:
        # ✅ R136（2026-10-27）：同时读回 `diag_json`（诊断字典快照）。
        cur = await db.execute(
            "SELECT stage, status, error, text_cache, retries, diag_json FROM "
            "doc_parse_checkpoints WHERE doc_id=?", (doc_id,))
        if cur is None:
            return None
        row = await cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None


# ✅ R137（2026-10-28）：检查点诊断 JSON 的限长与键集常量（裁剪 helper 专用）。
_CHECKPOINT_DIAG_LIMIT = 4000
_CHECKPOINT_DIAG_SCALARS = ("file_type", "truncated", "page_count",
                            "source_page_count", "text_len")
_CHECKPOINT_DIAG_ITEM_MAX = 200


def _compact_diag_for_checkpoint(diag: dict,
                                 limit: int = _CHECKPOINT_DIAG_LIMIT) -> str:
    """R137：把诊断字典压成**始终合法**的 JSON 串（检查点限长专用）。

    旧实现 `json.dumps(diag, ensure_ascii=False, default=str)[:4000]` 是对
    JSON **字符串**直接切片 —— 一旦超长就把尾部拦腰截断（`..."warn`），
    读侧 `json.loads` 必然 `ValueError` ⇒ `_restore_diag_from_checkpoint`
    的 `_d = None` 分支把**整个**诊断丢掉（页数 / 截断标记 / 全部告警一起
    消失），而不是只丢超限的那几条。真实触发场景：扫描件 PDF 逐页 OCR
    不可用时解析器逐页 `_note_info`，500 页即 500 条告警，轻松越过 4000 字。

    现改为**结构化裁剪**：
      ① 标量键逐个限长保留（永不丢弃 —— 它们是覆盖率/截断判定的输入）；
      ② `warnings` 按**整条**保留（绝不切半句），从尾部成批砍；
      ③ 被砍掉的条数补一条说明留在 `warnings` 末尾，走既有告警通道
         透出，用户仍能看出「还有告警没显示」而不是静默无感。
    返回值恒定可被 `json.loads` 解析。
    """
    if not isinstance(diag, dict):
        return ""
    payload: dict = {}
    for _k in _CHECKPOINT_DIAG_SCALARS:
        if _k not in diag:
            continue
        _v = diag[_k]
        if isinstance(_v, bool) or isinstance(_v, (int, float)):
            payload[_k] = _v
        else:
            payload[_k] = str(_v)[:_CHECKPOINT_DIAG_ITEM_MAX]
    _warns = diag.get("warnings")
    clean: list[str] = []
    if isinstance(_warns, list):
        clean = [str(_w)[:_CHECKPOINT_DIAG_ITEM_MAX] for _w in _warns
                 if str(_w).strip()]
    # 整条保留 + 末尾补省略说明；超限则成批（每次减半）砍尾部重试。
    keep = len(clean)
    while True:
        kept = list(clean[:keep])
        omitted = len(clean) - len(kept)
        if omitted:
            kept.append(f"另有 {omitted} 条解析告警未随断点保存（超长已裁剪）")
        body = dict(payload)
        body["warnings"] = kept
        try:
            text = json.dumps(body, ensure_ascii=False, default=str)
        except Exception:
            return ""
        if len(text) <= limit or keep == 0:
            return text
        keep = max(1, keep // 2)


async def _save_parse_checkpoint(db, doc_id: str, stage: str, status: str,
                                 error: str = "", text_cache: str = "",
                                 *, diag: dict | None = None) -> None:
    """写解析断点检查点。

    ✅ R136（2026-10-27）：新增 keyword-only 参数 `diag`（诊断字典）。
    R123 只缓存正文与阶段，resume 时 diag 重建为 `{resumed_from_stage,
    warnings: []}` —— page_count 回落 1、truncated 与解析器告警全丢、
    file_type 丢失。现一并持久化（JSON 串，限长 4000 字防异常膨胀）；
    缺省 None = 不写该列（空串），旧调用点行为逐字不变。
    ✅ R137（2026-10-28）：限长改由 `_compact_diag_for_checkpoint` 结构化
    裁剪（`[:4000]` 切片会切出非法 JSON 导致诊断整体丢失，见该 helper）。
    """
    _diag_json = ""
    if diag is not None:
        _diag_json = _compact_diag_for_checkpoint(diag)
    try:
        await db.execute(
            "INSERT INTO doc_parse_checkpoints "
            "(doc_id, stage, status, error, text_cache, diag_json, updated_at) "
            "VALUES (?,?,?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(doc_id) DO UPDATE SET stage=excluded.stage, "
            "status=excluded.status, error=excluded.error, "
            "text_cache=excluded.text_cache, diag_json=excluded.diag_json, "
            "updated_at=datetime('now','localtime')",
            (doc_id, stage, status, error[:200], text_cache, _diag_json))
        await db.commit()
    except Exception:
        logger.warning("保存解析检查点失败（可忽略）: %s", doc_id)


def _restore_diag_from_checkpoint(cp: dict | None, resume_stage: str, text: str,
                                  file_type_fallback: str = "") -> dict:
    """R136：从断点检查点重建诊断字典（续跑路径专用）。

    旧实现在 resume 分支硬写 `{"resumed_from_stage": ..., "warnings": []}`，
    把第一阶段已求出的页数/截断/告警/类型全部丢掉。此处改为：
      ① 优先从检查点的 `diag_json` 反序列化（只取白名单键，防止历史脏
         数据把任意字段透传给下游）；
      ② `file_type` 缺失时回落到 project_documents.file_type（旧检查点
         无 diag_json 时的兼容路径）；
      ③ `page_count` 仍为 0 时用 `count_page_markers(text)` 现算 ——
         与解析器侧同一口径（去重页号数），保证旧检查点也不回落成 1。
    """
    diag: dict = {"resumed_from_stage": resume_stage, "warnings": []}
    raw = str((cp or {}).get("diag_json") or "")
    if raw:
        try:
            _d = json.loads(raw)
        except ValueError:
            # ✅ R137（2026-10-28）：旧版写侧 `[:4000]` 切片会留下半截 JSON。
            #    此处仍降级（不阻断续跑），但不再静默 —— 否则「诊断全丢」
            #    在日志里毫无痕迹，无法判定是没存还是存坏了。
            logger.warning("检查点 diag_json 非法（旧版截断遗留），诊断降级重建")
            _d = None
        if isinstance(_d, dict):
            for _k in ("file_type", "truncated", "page_count",
                       "source_page_count", "text_len"):
                if _k in _d:
                    diag[_k] = _d[_k]
            _w = _d.get("warnings")
            if isinstance(_w, list):
                diag["warnings"] = [str(x) for x in _w]
    if not str(diag.get("file_type") or "").strip() and file_type_fallback:
        diag["file_type"] = file_type_fallback.strip().lower()
    diag["truncated"] = bool(diag.get("truncated"))
    if int(diag.get("page_count") or 0) <= 0:
        diag["page_count"] = count_page_markers(text) or (
            1 if (text or "").strip() else 0)
    return diag


async def _clear_parse_checkpoint(db, doc_id: str) -> None:
    try:
        await db.execute("DELETE FROM doc_parse_checkpoints WHERE doc_id=?", (doc_id,))
        await db.commit()
    except Exception:
        pass


async def _persist_parse_result_with_retry(
    db, doc_id: str, stored: str, elapsed: float,
    warnings: list, diag: dict, truncated: bool,
) -> bool:
    """R123：落库瞬时失败指数退避重试（仅 DB 瞬时不可用，不重试结构性失败）。

    与既有 R13 守卫同源：`_persist_parse_result` 在 db.execute 返回 None（连接池
    瞬时故障）时返回 False。此处对 False 做有限次重试，耗尽仍失败才让调用方 503。
    结构性失败（文件/格式）在落库前已以 400 结束，不会走到这里。
    """
    from app.config import settings as _settings
    max_retries = int(getattr(_settings, "parse_retry_max", 2))
    base = float(getattr(_settings, "parse_retry_base_ms", 300))
    cap = float(getattr(_settings, "parse_retry_cap_ms", 3000))
    attempt = 0
    while True:
        ok = await _persist_parse_result(
            db, doc_id, stored, elapsed, warnings, diag, truncated)
        if ok:
            return True
        attempt += 1
        if attempt > max_retries:
            return False
        wait_ms = min(base * (2 ** (attempt - 1)), cap)
        logger.warning(
            "解析落库瞬时失败，%dms 后第 %d/%d 次重试 doc=%s",
            int(wait_ms), attempt, max_retries, doc_id)
        await asyncio.sleep(wait_ms / 1000.0)


async def _mark_project_facts_stale(db, project_id: str) -> int:
    """资料来源发生变化后，保守标记项目下全部事实为 stale。

    当前事实表尚未逐事实保存 source_doc_id，精确血缘无法可靠反查；项目级失效
    会在多方案共享事实场景下偏保守，但能确保正文/导出不继续使用旧解析结果。
    重新提取或人工改值/裁决后对应行可解除标记。
    """
    if not project_id:
        return 0
    cur = await db.execute(
        "UPDATE global_facts SET is_stale=1, "
        "updated_at=datetime('now','localtime') WHERE project_id=? AND is_stale=0",
        (project_id,))
    return safe_rowcount(cur, what="项目级事实批量置 stale")


async def _invalidate_project_export_caches(db, project_id: str) -> None:
    """✅ 跨模块链路收敛（2026-09-23）：删除 / 强制重解析文档后，失效该项目下所有方案的
    导出缓存，避免下游导出陈旧 docx。

    导出缓存默认关闭（O11），无缓存行时 `invalidate_export_cache` 为 no-op，安全；
    即便开启，也只删 export_cache 行 + 磁盘产物（不回滚已生成正文，既定设计）。
    按 scheme 失效：文档属项目级，项目下可能有多个方案，需逐一失效。
    """
    try:
        cur = await db.execute(
            "SELECT id FROM schemes WHERE project_id=?", (project_id,))
        sids = [r["id"] for r in await cur.fetchall()]
        for sid in sids:
            # 本路径的上游 _mark_project_facts_stale 刚把项目下全部事实置 stale，
            # 属于「事实已变更」→ 同步推进章节失效标记的时间戳。
            await invalidate_export_cache(db, sid, facts_touched=True)
    except Exception:
        logger.exception("失效项目 %s 导出缓存失败（可忽略）", project_id)


@router.post("/documents/{doc_id}/parse")
async def parse_document(doc_id: str, force: bool = False, db=Depends(get_db)):
    """② 解析单份文档：读取原始文件 → parse_file_content → 落库

    force=true 时忽略"已解析"直接重解析：用于早期版本受 80000 字上限
    截断的文档，重解析后可按当前上限（400000 字）获得完整内容。
    """
    import time as _time

    from app.routers import global_facts as _gf
    _parse_start = _time.time()
    cur = await db.execute(
        "SELECT file_name, file_path, parsed_markdown, project_id, parse_version,"
        " file_type FROM project_documents WHERE id=?", (doc_id,))
    # ✅ R13 读路径补漏（2026-10-08）：2026-10-05 收口时本函数只给【锁内二次
    #    复核】查询补了判空（见下方），首查一直是漏改点 —— None 游标直接
    #    fetchone → AttributeError → 500。同一端点两次「按 id 读文档」语义
    #    分叉（首查 500 / 复查 503）；且 _load_doc、delete/preview/category
    #    各兄弟端点对同类查询全部回 503。补齐同口径：503 = 可重试。
    if cur is None:
        logger.warning("读取文档失败（db.execute 返回 None，R13），doc=%s", doc_id)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    # ✅ 2026-09-23 日志埋点：解析入口绑定 project/doc 上下文，使本次解析及后续
    #    四层落盘 / 提取的日志都能凭 trace_id + doc 串联。
    set_context(trace_id=new_trace_id(),
                project_id=(row["project_id"] or "") if "project_id" in row.keys() else "",
                doc_id=doc_id)
    fname = row["file_name"]
    fpath = row["file_path"] if "file_path" in row.keys() else ""
    safe_path = _managed_upload_path(fpath)
    if fpath and safe_path is None:
        raise HTTPException(400, "文档路径不在事实上传目录内，请重新上传")
    # ✅ 并发守卫（2026-09-23）：锁内串行化同文档解析，并在锁内复核"已解析"
    #    状态 —— 否则两个并发请求都会在各自读到的 parsed_markdown='' 上各解析
    #    一次（重复 OCR + 后写覆盖）。锁内再查一次，已解析且非强制则直接返回。
    #    解析结束（含异常）后由 _doc_parse_guard 自动回收锁字典条目（防内存泄漏）。
    async with _doc_parse_guard(doc_id):
        cur = await db.execute(
            "SELECT parsed_markdown FROM project_documents WHERE id=?", (doc_id,))
        # ✅ P1（R13 漏改 · 2026-10-05）：读路径判空。直接 fetchone →
        #    AttributeError → 500（且用户只看到「解析失败」的裸错误，看不出是
        #    数据库瞬时故障还是文件问题）。503 = 可重试，与 _load_doc 同口径。
        if cur is None:
            logger.warning("读取解析状态失败（db.execute 返回 None），doc=%s", doc_id)
            raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
        _cur = await cur.fetchone()
        if _cur and _cur["parsed_markdown"] and not force:
            # ✅ BUG 修复（2026-09-25）：短路返回前先自修复陈旧的 parse_status
            #    （存量文档正文非空但状态仍为 pending → 前端恒显「待解析」、
            #    点解析永远「已解析过」却无任何变化，体感「解析结果丢失」）。
            reconciled = await _reconcile_parse_status(db, doc_id)
            result = {"ok": True, "already_parsed": True,
                      "parse_status": "success",
                      "text_len": len(_cur["parsed_markdown"])}
            if reconciled:
                result["reconciled"] = True
            return result
        if not safe_path or not safe_path.exists():
            raise HTTPException(400, f"原始文件缺失（{fname}），请重新上传")
        # 本次是否为重解析（源文档此前已有解析内容）—— 解析代次递增判定
        _was_parsed = bool(_cur and _cur["parsed_markdown"])
        # ✅ R123（2026-10-21）：解析断点续跑检查点。
        #    force=重解析走完整流程，先清旧检查点（不续跑旧失败态）。
        if force:
            await _clear_parse_checkpoint(db, doc_id)
        _cp = await _load_parse_checkpoint(db, doc_id)
        _resume_text = None
        _resume_stage = "extract"
        if (not force) and _cp and _cp.get("status") == "failed" and _cp.get("text_cache"):
            _rs = _cp.get("stage") or "persist"
            if _rs in ("persist", "layers", "stale"):
                _resume_text = _cp["text_cache"]
                _resume_stage = _rs
        if _resume_text is not None:
            # 断点续跑：复用上次已提取文本，跳过代价高昂的重新解析。
            text = _resume_text
            # ✅ R136（2026-10-27，BUG 修复 · 续跑丢诊断）：从检查点恢复完整
            #    diag。旧实现硬写 `{"resumed_from_stage": ..., "warnings": []}`，
            #    导致续跑成功的文档：① `page_count` 回落成 1（下游 ingest 用
            #    `or 1`）→ 解析页数/覆盖率/质量评分全部失真；② `truncated`
            #    与解析器告警（PDF 截页 / 表格截行 / OCR 兜底 / 加密）彻底消失
            #    → 用户以为拿到全文；③ `file_type` 丢失 → parse_engine 写空。
            #    三项均为「第一阶段已花代价求出、第二阶段原地丢失」的信息。
            diag = _restore_diag_from_checkpoint(
                _cp, _resume_stage, text,
                file_type_fallback=str(row["file_type"] or "")
                if "file_type" in row.keys() else "")
            logger.info("解析断点续跑 doc=%s 从阶段 %s 恢复（跳过提取）", doc_id, _resume_stage)
        else:
            try:
                content = await asyncio.to_thread(safe_path.read_bytes)
                text, diag = await asyncio.to_thread(
                    _gf.parse_file_content_ex, content, fname)
            except ParseError as e:
                # ParseError 的消息是给用户看的（缺依赖 / 无 OCR 引擎 / 压缩炸弹），
                # 属于可操作提示，原样返回。结构化失败不重试（重试无意义）。
                logger.warning("文档 %s 解析失败: %s", fname, e)
                # ✅ 解析失败可观测性：标记 failed 状态 + 持久化原因（前端区分"待解析"）
                await _mark_parse_failed(db, doc_id, str(e))
                raise HTTPException(400, f"解析失败：{e}")
            except Exception:
                # ✅ 其它异常（库版本、路径、编码等）只进日志，不回传细节，避免泄露环境信息
                logger.exception("文档 %s 解析出现未预期异常", fname)
                await _mark_parse_failed(db, doc_id, "文件无法识别或已损坏")
                raise HTTPException(400, "解析失败：文件无法识别或已损坏，请查看服务端日志")
            if len(text.strip()) < 10:
                await _mark_parse_failed(
                    db, doc_id,
                    "未解析到有效文本（空白文件 / 扫描件无 OCR / 已损坏）")
                raise HTTPException(400,
                    f"「{fname}」未解析到有效文本：可能是空白文件、扫描件或已损坏。"
                    f"若为扫描件/图片，请确认 OCR 引擎可用"
                    f"（打开 /api/v1/diagnostics/capabilities 查看启用方法）")
            # ✅ R123：提取阶段成功，记录检查点（便于后续阶段失败时可续跑）。
            await _save_parse_checkpoint(db, doc_id, "extract", "done")
        # ✅ BUG 修复：落库长度与实际存储长度一致，避免前端显示"文本长度 X"
        # 但库里只存了 Y < X 的假象。超限时返回 truncated=true 让前端提示重解析。
        # ✅ 两类截断都要告知：① 落库字数超上限；② 解析器自身截断（PDF 页数 /
        #    表格行数）。旧实现只认第 ① 类，"第 50 页之后的设计参数没进事实库"
        #    这类问题在界面上完全无提示。
        char_truncated = len(text) > MAX_PARSED_CHARS
        stored = text[:MAX_PARSED_CHARS]
        # ✅ 增强：记录解析耗时（供前端显示「解析用时」诊断）
        _elapsed = round(_time.time() - _parse_start, 2)
        # ✅ 增强：解析器诊断告警持久化（截断 / OCR 兜底 / 加密 PDF）。
        #    旧实现告警只在本次响应里出现，刷新后丢失 —— 用户事后查看文档列表
        #    无从得知"该文档内容不完整"。落库后由 /documents 随列表回传。
        parser_warnings = [str(w) for w in (diag.get("warnings") or [])]
        if char_truncated:
            parser_warnings.append(
                f"原文 {len(text)} 字，已截断至 {MAX_PARSED_CHARS} 字上限")
        # ✅ 解析成功即置 parse_status='success'（2026-09-23）：旧实现依赖四层入库
        #    （_ingest_parsed_doc）成功才写该列；一旦四层落盘失败，列仍为 'pending'
        #    而 parsed_markdown 已填充 → 状态矛盾，下游 doc_pipeline 去重/结构化
        #    读取会误判"未解析"，造成「解析结果丢失」隐患。此处让状态列与真实
        #    解析结果一致，独立于四层入库成败。
        # ✅ F2（2026-09-26）：字数截断与解析器级截断（PDF 截页/表格截行）一并
        #    持久化到 parse_truncated，使列表接口能直接读取、刷新后不漏报。
        truncated = char_truncated or bool(diag.get("truncated"))
        # ✅ 重构：成功结果统一走 _persist_parse_result，与「解析全部」同口径
        # ✅ P1（R13 写路径 · 2026-10-05）：落库未生效时**不能**继续 —— 后面的
        #    _ingest_parsed_doc 会从库里读到空正文、result 也会回 ok=True+text_len，
        #    用户看到「解析成功 N 字」而刷新后是 0 字。503 让用户明确重试。
        # ✅ R123：落库瞬时失败指数退避重试；耗尽仍失败 → 记录断点（缓存文本）后 503。
        # ✅ R145（2026-10-29，L4）：ParseTargetGoneError 与瞬时失败分流 —— 文档在
        #    解析过程中被并发删除时：不写 failed 检查点（对已删文档是孤儿行）、
        #    顺手清掉本请求此前可能已写的 extract 检查点，回 404（真实状态）而非
        #    503（503 会引导用户重试一个已不存在的文档）。
        try:
            _persisted = await _persist_parse_result_with_retry(
                db, doc_id, stored, _elapsed, parser_warnings, diag, truncated)
        except ParseTargetGoneError:
            logger.warning("文档 %s 在解析过程中被并发删除，解析中止（不落检查点）",
                           doc_id)
            await _log_parse_event(
                event="parse.aborted", level="WARNING", doc_id=doc_id,
                project_id=str(row["project_id"] or ""),
                message="文档已被删除，本次解析已中止")
            await _clear_parse_checkpoint(db, doc_id)
            raise HTTPException(404, "文档已被删除，本次解析已中止")
        if not _persisted:
            # ✅ R136（2026-10-27）：检查点同时带上 diag —— 否则下次续跑虽然
            #    跳过了昂贵的提取，却拿不回页数/截断/告警（见 resume 分支注释）。
            await _save_parse_checkpoint(
                db, doc_id, "persist", "failed",
                "落库瞬时失败（已重试耗尽）", text, diag=diag)
            raise HTTPException(
                503, "解析结果保存失败（数据库暂时不可用），请稍后重试")
        await _save_parse_checkpoint(db, doc_id, "persist", "done")
        # ✅ 四层存储（阶段2+3）：解析层产物落盘 + 分块入库（失败不阻断解析主结果）
        layer_info = await _ingest_parsed_doc(
            db, doc_id, row["project_id"], fname, _elapsed, diag,
            parser_warnings, _was_parsed)
        logger.info("文档 %s 解析完成：%d 字%s%s", fname, len(stored),
                    "（已截断至上限）" if char_truncated else "",
                    "（解析器截断）" if diag.get("truncated") else "")
        await _save_parse_checkpoint(db, doc_id, "layers", "done")
        result: dict = {"ok": True, "text_len": len(stored), "truncated": truncated}
        if layer_info:
            result["layers"] = layer_info
        if parser_warnings:
            result["warnings"] = parser_warnings
        if char_truncated:
            result["warning"] = (f"原文 {len(text)} 字，已截断至 {MAX_PARSED_CHARS} 字上限；"
                                 "如需完整提取请拆分文件后重新上传")
        if force:
            # 强制重解析改变了源文本：旧事实立即退出目录/正文/导出生成链路。
            stale_count = await _mark_project_facts_stale(db, row["project_id"])
            await db.commit()
            # ✅ 跨模块链路收敛：强制重解析改变了源文本 → 失效该项目所有方案的
            #    导出缓存，避免下游导出陈旧 docx（导出缓存默认关闭时为 no-op）。
            await _invalidate_project_export_caches(db, row["project_id"])
            result["stale_facts"] = stale_count
            result["note"] = (f"文档已重新解析，{stale_count} 条旧事实已停止注入；"
                              "请重新执行「AI 提取事实」更新全局事实")
        elif not _was_parsed:
            # ✅ R79 A-4：**首次**解析成功同样改变了事实/导出的输入全集 ——
            #    旧实现把 stale 标记与导出缓存失效全关在 `if force:` 里，
            #    于是「先解析 A → 已提取事实 → B 首次解析成功（如装好 OCR
            #    后重试，force=False）」这条真实链路上，全局事实与导出缓存
            #    停留在半残缺输入对应的旧值：用户以为事实已覆盖全部资料，
            #    实际漏掉整份文档，且响应里没有任何提示。
            #    首次解析时事实通常尚未提取（本步为空操作）；若已提取过，
            #    此处按 force 同口径补齐，代价是一次幂等 UPDATE + 一次 DELETE。
            stale_count = await _mark_project_facts_stale(db, row["project_id"])
            await db.commit()
            await _invalidate_project_export_caches(db, row["project_id"])
            result["stale_facts"] = stale_count
            result["note"] = ("文档解析成功，已纳入项目资料全集；"
                              "如已提取过全局事实，请重新执行「AI 提取事实」")
        # ✅ R123：全阶段成功，记录 stale 完成并清除断点检查点。
        await _save_parse_checkpoint(db, doc_id, "stale", "done")
        await _clear_parse_checkpoint(db, doc_id)
        # ✅ R157：解析成功事件（时长/字数/截断态进 system_logs，可盘点成功率）。
        await _log_parse_event(
            event="parse.done", doc_id=doc_id,
            project_id=str(row["project_id"] or ""),
            duration_ms=int(_elapsed * 1000), status="success",
            message=f"解析成功 {len(stored)} 字" + ("（截断）" if truncated else ""),
            detail={"truncated": bool(truncated),
                    "warnings": len(parser_warnings)})
        return result


@router.post("/documents/parse-all")
async def parse_all_documents(
    scheme_id: str = Query(""),
    project_id: str = Query(""),
    force: bool = False,
    db=Depends(get_db),
):
    """② 批量解析：解析该项目下所有未解析的文档

    ✅ 增强：force=true 时【连同已解析文档一起重解析】。用于两类场景：
      1. 早期版本按 80000 字上限截断的旧文档，重解析后可按当前上限补齐；
      2. 中途启用了 OCR 引擎后，重解析可让此前识别失败的扫描件重新获得文本。
    """
    from app.routers import global_facts as _gf
    real_pid = await _resolve_project_id(db, scheme_id, project_id)
    if not real_pid:
        raise HTTPException(400, "需要 scheme_id 或 project_id")
    # ✅ 2026-09-23 日志埋点：批量解析入口绑定 project 上下文，便于按 trace_id 串联
    #    本次批量解析涉及的所有文档。
    set_context(trace_id=new_trace_id(), project_id=real_pid, scheme_id=scheme_id)
    # ✅ BUG 修复（2026-09-25，「解析全部」体感失效）：先做一轮状态自修复。
    #    存量文档正文非空但 parse_status 仍是 pending（四层存储上线前的默认值
    #    回填）时，它们**不进**下方 pending 列表 → 直接命中"所有文档均已解析"
    #    早退，用户看到的还是「待解析」。放在 SELECT 之前使两个分支都正确。
    reconciled_count = await _reconcile_parse_status_all(db, real_pid)
    sql = ("SELECT id, project_id, file_name, file_path FROM project_documents "
           "WHERE project_id=?")
    if not force:
        sql += " AND (parsed_markdown='' OR parsed_markdown IS NULL)"
    # ✅ 顺序固定：批量解析进度与失败列表按上传先后给出，避免 SQLite 返回顺序漂移
    sql += " ORDER BY created_at, id"
    cur = await db.execute(sql, (real_pid,))
    # ✅ P1（R13 漏改 · 2026-10-05）：读路径判空。降级成空 pending 列表会回
    #    「所有文档均已解析」—— 对着一个根本没读到的库宣布全解析完毕，是比
    #    500 更糟的假结论。503 让前端可重试。
    if cur is None:
        logger.warning("查询待解析文档失败（db.execute 返回 None），project=%s", real_pid)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    pending = [dict(r) for r in await cur.fetchall()]
    for _p in pending:
        _p["force_reparse"] = bool(force)
    if not pending:
        _msg = "所有文档均已解析"
        if reconciled_count:
            _msg += (f"（已修正 {reconciled_count} 份存量文档的解析状态）")
        return {"ok": True, "parsed": 0, "failed_count": 0, "failed": [],
                "truncated_count": 0, "truncated": [],
                # ✅ 响应形状一致（2026-09-25）：早退分支与主分支都带
                # reconciled/reconciled_count，前端才能用统一判据做提示。
                "reconciled_count": reconciled_count, "reconciled": [],
                "message": _msg}

    import time as _time

    parsed, failed = 0, []
    truncated_files: list[dict] = []
    reconciled: list[str] = []
    for doc in pending:
        # ✅ 并发守卫（2026-09-23）：逐文档锁串行化，防与并发单份 parse / 另一
        #    次 parse-all 重复解析同一文档（重复 OCR + 后写覆盖）。锁内先复核
        #    "已解析"状态，已解析且非强制则跳过（与单份解析同口径）。解析结束
        #    后由 _doc_parse_guard 自动回收锁字典条目（防内存泄漏）。
        async with _doc_parse_guard(doc["id"]):
            try:
                cur = await db.execute(
                    "SELECT parsed_markdown FROM project_documents WHERE id=?", (doc["id"],))
                _c = await cur.fetchone()
                if _c and _c["parsed_markdown"] and not force:
                    # ✅ BUG 修复（2026-09-25）：与单份解析同口径，短路前自修复
                    #    陈旧的 parse_status —— 否则「解析全部」对存量文档回
                    #    「所有文档均已解析」却不改状态，前端列表恒显「待解析」。
                    if await _reconcile_parse_status(db, doc["id"]):
                        reconciled.append(doc["file_name"])
                    continue
            except Exception as e:
                # ✅ A-4（2026-10-01）：原为 `except Exception: pass`，完全静默。
                #    该分支是「批量解析前复核该文档是否已解析」——DB 瞬时故障时会
                #    把**已解析**文档误判为未解析并重跑一遍（对大 PDF 意味着重复
                #    且昂贵的 OCR/MinerU 调用），事后却没有任何痕迹可查。
                logger.warning(
                    "复核文档 %s 解析状态失败（按未解析继续，可能重复解析）: %s",
                    doc.get("file_name", doc.get("id", "")), e, exc_info=True)
            try:
                _parse_start = _time.time()
                fpath = doc.get("file_path", "")
                safe_path = _managed_upload_path(fpath)
                if not safe_path:
                    # ✅ BUG 修复（2026-09-24）：路径不安全/原文件缺失/有效文本不足三个
                    #    分支此前只 append failed 后 continue，未落 parse_status='failed'，
                    #    文档永久停留 pending、前端恒显「待解析」（与单文档解析口径不一致）。
                    _reason = "原始文件路径不安全或缺失"
                    await _mark_parse_failed(db, doc["id"], _reason)
                    failed.append({"file_name": doc["file_name"], "reason": _reason})
                    continue
                if not safe_path.exists():
                    _reason = "原始文件缺失"
                    await _mark_parse_failed(db, doc["id"], _reason)
                    failed.append({"file_name": doc["file_name"], "reason": _reason})
                    continue
                content = await asyncio.to_thread(safe_path.read_bytes)
                text, diag = await asyncio.to_thread(
                    _gf.parse_file_content_ex, content, doc["file_name"])
                if len(text.strip()) < 10:
                    _reason = ("未解析到有效文本（空白/扫描件/损坏）；"
                               "扫描件需 OCR 引擎，见 /api/v1/diagnostics/capabilities")
                    await _mark_parse_failed(db, doc["id"], _reason)
                    failed.append({"file_name": doc["file_name"], "reason": _reason})
                    continue
                # ✅ BUG 修复：截断是「解析成功但内容不完整」的告警，不应计入 failed，
                #    否则前端提示"失败 N 个"会让用户误以为整份解析失败。
                char_truncated = len(text) > MAX_PARSED_CHARS
                stored = text[:MAX_PARSED_CHARS]
                # ✅ 增强：解析器诊断告警随解析结果持久化（与单文档解析一致）
                all_warnings = [str(w) for w in (diag.get("warnings") or [])]
                if char_truncated:
                    all_warnings.append(
                        f"原文 {len(text)} 字，已截断至 {MAX_PARSED_CHARS} 字上限")
                # ✅ BUG 修复（口径不一致）：单文档解析会写 parse_time，批量解析不写，
                #    于是「解析全部」之后前端列表的「解析用时」恒为空 —— 用户无法
                #    判断到底是哪份文件拖慢了整批解析。现与单文档解析保持一致。
                _elapsed = round(_time.time() - _parse_start, 2)
                # ✅ 解析成功即置 parse_status='success'（与单份解析同口径，2026-09-23）：
                #    消除四层入库失败导致 parse_status 陈旧、下游误判"未解析"。
                # ✅ F2（2026-09-26）：与单文档解析一致，持久化截断标记
                doc_truncated = char_truncated or bool(diag.get("truncated"))
                # ✅ 重构：成功结果统一走 _persist_parse_result，与单文档解析同口径
                # ✅ P1（R13 写路径 · 2026-10-05）：落库未生效时不得计入 parsed ——
                #    否则响应报「成功 N 份」而库里正文是空的（假成功）。归入 failed
                #    并落 parse_status='failed'，与其它失败原因同口径、可重试。
                # ✅ R136（2026-10-27，BUG 修复 · 重试口径分叉）：改走
                #    `_persist_parse_result_with_retry`。旧实现直接调无重试的
                #    `_persist_parse_result`：单份解析遇 DB 瞬时故障会退避重试 2 次
                #    （见 _persist_parse_result_with_retry），而「解析全部」一次失败
                #    就把该文档判为 failed —— 同一份文件单个点能成功、批量却失败，
                #    长批次后半段更容易因连接池争用批量翻红（用户重跑仍可能复现）。
                # ✅ R145（2026-10-29，L4）：与单份解析同口径分流 —— 文档被并发
                #    删除时不写 failed 状态/检查点（行已不在，写了既是孤儿又是
                #    0 行无效写），计入 failed 并注明原因，批次继续处理其余文档。
                try:
                    _persisted = await _persist_parse_result_with_retry(
                        db, doc["id"], stored, _elapsed, all_warnings, diag,
                        doc_truncated)
                except ParseTargetGoneError:
                    logger.warning("文档 %s 在批量解析中被并发删除，跳过",
                                   doc["id"])
                    failed.append({"file_name": doc["file_name"],
                                   "reason": "文档已被删除，本次解析已中止"})
                    continue
                if not _persisted:
                    _reason = "解析结果保存失败（数据库暂时不可用），请重试"
                    await _mark_parse_failed(db, doc["id"], _reason)
                    failed.append({"file_name": doc["file_name"], "reason": _reason})
                    continue
                # ✅ 四层存储（阶段2+3）：逐文档落盘解析层 + 分块（失败不阻断批量）
                await _ingest_parsed_doc(
                    db, doc["id"], doc["project_id"], doc["file_name"],
                    _elapsed, diag, all_warnings,
                    bool(doc.get("force_reparse")))
                parsed += 1
                # ✅ BUG 修复（2026-09-25，口径不一致）：单文档解析的截断判定是
                #    `char_truncated or bool(diag["truncated"])`，本函数旧实现多加了
                #    一个 `and diag["warnings"]` 条件 —— 解析器标记了 truncated
                #    但 warnings 为空时（例如仅页数截断、未附告警文本）批量解析
                #    **静默漏报**，用户以为拿到了全文。现与单份解析完全对齐。
                doc_truncated = char_truncated or bool(diag.get("truncated"))
                if doc_truncated:
                    if char_truncated:
                        truncated_files.append({
                            "file_name": doc["file_name"],
                            "reason": f"原文 {len(text)} 字，已截断至 {MAX_PARSED_CHARS} 字上限（建议拆分文件后重新上传）",
                        })
                    else:
                        truncated_files.append({
                            "file_name": doc["file_name"],
                            "reason": ("；".join(str(w) for w in diag["warnings"])
                                       if diag.get("warnings")
                                       else "解析器判定内容不完整（PDF 页数/表格行数截断）"),
                        })
            except ParseError as e:
                logger.warning("文档 %s 批量解析失败: %s", doc["file_name"], e)
                # ✅ 解析失败可观测性：标记 failed 状态（前端区分"待解析"）
                await _mark_parse_failed(db, doc["id"], str(e)[:120])
                failed.append({"file_name": doc["file_name"], "reason": str(e)[:120]})
            except Exception:
                logger.exception("文档 %s 批量解析出现未预期异常", doc["file_name"])
                await _mark_parse_failed(db, doc["id"], "文件无法识别或已损坏")
                failed.append({"file_name": doc["file_name"],
                               "reason": "文件无法识别或已损坏，请查看服务端日志"})
    # ✅ 循环内已对每个成功文档逐条 commit（避免长事务锁库），此处无需再 commit；
    #    旧实现的尾部重复 commit 属无谓 IO 且造成「事务已完成」的误导语义。

    result: dict = {"ok": True, "parsed": parsed, "failed_count": len(failed),
                    "failed": failed,
                    "truncated_count": len(truncated_files),
                    "truncated": truncated_files,
                    "force": force,
                    # ✅ 2026-09-25：本轮自修复的陈旧 parse_status 条数（0 表示无）
                    "reconciled_count": reconciled_count + len(reconciled),
                    "reconciled": reconciled}
    # ✅ R157：批量解析汇总事件（一批一条，不逐文档刷屏；逐文档失败已由
    #    _mark_parse_failed 咽喉发 parse.failed）。
    await _log_parse_event(
        event="parse.batch", level="WARNING" if failed else "INFO",
        project_id=real_pid,
        status="partial" if failed else "done",
        message=f"批量解析 parsed={parsed} failed={len(failed)}",
        detail={"parsed": parsed, "failed": len(failed),
                "truncated": len(truncated_files),
                "reconciled": reconciled_count + len(reconciled)})
    if parsed:
        # ✅ R79 A-4：force 与否**只要本轮有新文本落库**，事实与导出缓存的
        #    输入全集就变了 —— 旧实现只在 `force and parsed` 时处理，
        #    非强制批量解析（最常规的「上传后点解析全部」）从不刷新下游。
        stale_count = await _mark_project_facts_stale(db, real_pid)
        await db.commit()
        # ✅ 跨模块链路收敛：重解析整批文档 → 失效该项目所有方案导出缓存
        #    （避免下游导出陈旧 docx；导出缓存默认关闭时为 no-op）。
        await _invalidate_project_export_caches(db, real_pid)
        result["stale_facts"] = stale_count
        if force:
            result["note"] = (f"已强制重新解析，{stale_count} 条旧事实已停止注入；"
                              "请重新执行「③ AI 提取事实」")
        else:
            result["note"] = ("本轮新解析的文档已纳入项目资料全集；"
                              "如已提取过全局事实，请重新执行「③ AI 提取事实」")
    warnings: list[str] = []
    if failed:
        warnings.append(
            f"{len(failed)} 份文档解析失败：" + "；".join(
                f"{f['file_name']}（{f['reason']}）" for f in failed[:5]))
    if truncated_files:
        warnings.append(
            f"{len(truncated_files)} 份文档内容超长被截断：" + "；".join(
                f"{f['file_name']}（{f['reason']}）" for f in truncated_files[:5]))
    if warnings:
        result["warnings"] = warnings
    return result


# =========================================================================
# ✅ 文件导入/解析模块增强：自动分类 + 文件预览 API
# =========================================================================

# ✅ 口径收敛（2026-09-25）：分类清单与关键词规则的**唯一事实源**已迁到
# ``services/doc_categories.py``（该模块同时被 bid_analysis 的提取优先级复用）。
# 此处保留 `_DOC_CATEGORY_KEYWORDS` 名称作为**只读别名**，兼容既有调用方与
# 单测的引用习惯；新代码请直接用 doc_categories.AUTO_CLASSIFY_RULES。
_DOC_CATEGORY_KEYWORDS = tuple(AUTO_CLASSIFY_RULES)


def _auto_classify_document(file_name: str, file_type: str = "") -> str:
    """根据文件名自动判断文档分类（用于「文件导入」Tab 展示）。

    ✅ 口径收敛（2026-09-25）：分类清单与关键词规则的**唯一事实源**已迁到
    ``services/doc_categories.py`` —— 旧实现把 9 类关键词只写在本文件里，
    而 ``bid_analysis._combine_doc_texts`` 另写了一份只覆盖 6 类的优先级表，
    三处口径彼此漂移（漏改「资质材料/人员资料/财务资料/业绩证明」→ 命中后
    被 AI 提取静默排到最后）。现只做委托，保留原函数名兼容既有调用与单测。
    """
    return auto_classify_document(file_name, file_type)


@router.get("/documents/{doc_id}/preview")
async def preview_document(doc_id: str, max_chars: int = Query(5000, ge=100, le=50000),
                              db=Depends(get_db)):
    """文件预览：返回已解析 Markdown 的前 N 个字符供前端快速浏览。

    用于「文件解析」Tab 的「查看解析内容」弹窗——**不下载整份文件**，
    避免大文件（20 万+ 字）把前端内存打爆。
    """
    cur = await db.execute(
        "SELECT id, file_name, file_type, parsed_markdown, doc_category, "
        "file_size, parse_time, parse_warnings, parse_truncated, created_at "
        "FROM project_documents WHERE id=?",
        (doc_id,))
    # ✅ R13 读路径补漏（2026-10-05）：list_documents 已在 R41 收口，本接口
    #    同属「文档读取面」却漏改 —— cursor 为 None 时 fetchone 抛
    #    AttributeError → 500，前端预览弹窗只看到裸错误。503 = 可重试。
    if cur is None:
        logger.warning("预览查询失败（db.execute 返回 None），doc=%s", doc_id)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    # ✅ BUG 修复（2026-09-18）：get_conn 的 row_factory 是 sqlite3.Row ——
    #    它支持 row["col"] 下标但**没有 .get() 方法**。旧实现用 row.get(...)
    #    使 /documents/{id}/preview 在文档已存在时必然抛 AttributeError → 500
    #    （HTTP 冒烟实测崩溃点）。
    row = dict(row)
    md = row["parsed_markdown"] or ""
    if not md:
        # ✅ BUG 修复（契约对称，2026-09-21）：未解析分支此前只回 6 个字段，
        #    缺 file_type / doc_category / file_size / parse_time / created_at /
        #    preview_truncated —— 前端用同一 interface 消费预览响应时，未解析
        #    文档拿到 undefined 会渲染出空白占位（如「格式：」空值）。
        #    现两条返回路径字段集合完全一致（由
        #    test_preview_response_contract_symmetric 钉住）。
        return {
            "doc_id": doc_id,
            "file_name": row["file_name"],
            "file_type": row["file_type"],
            "doc_category": row["doc_category"],
            "file_size": row["file_size"],
            "parse_time": row["parse_time"],
            "parse_warnings": _decode_parse_warnings(row.get("parse_warnings")),
            "created_at": row["created_at"],
            "preview": "",
            "text_len": 0,
            "is_parsed": False,
            "preview_truncated": False,
            "truncated": False,
            "message": "该文档尚未解析，请先执行解析",
        }

    total_len = len(md)
    preview = md[:max_chars]
    truncated = total_len > max_chars
    # ✅ 2026-09-26（口径区分，补齐 G2 可感知性缺口）：预览弹窗此前只有
    #    ``preview_truncated``（"预览只截取了前 N 字"），**没有解析级截断信号**。
    #    一份 PDF 因超 50 页被截断的文档，用户在预览里看到的正文与完整文档
    #    一模一样长（都远小于预览上限），于是以为资料齐全，实际上 18 项提取
    #    与正文生成都只看到了前 50 页。现新增 ``truncated`` 承载解析级截断
    #    （与 /documents 列表口径完全一致：持久化标记 + 字数上限双口径），
    #    两个字段各司其职、互不覆盖。
    #    向后兼容：新增字段，既有消费方（`preview_truncated`）语义不变。
    doc_truncated = doc_is_truncated(row)
    return {
        "doc_id": doc_id,
        "file_name": row["file_name"],
        "file_type": row["file_type"],
        "doc_category": row["doc_category"],
        "file_size": row["file_size"],
        "parse_time": row["parse_time"],
        "parse_warnings": _decode_parse_warnings(row.get("parse_warnings")),
        "created_at": row["created_at"],
        "preview": preview,
        "text_len": total_len,
        # ✅ 补齐契约对称：未解析分支回 is_parsed=False，已解析分支旧实现
        #    漏回该字段 —— 前端用 `data.is_parsed === true` 硬判断时会退到
        #    「未解析」分支。现两条返回路径字段集合一致。
        "is_parsed": True,
        "preview_truncated": truncated,
        "truncated": doc_truncated,
        "message": "（预览仅显示前 {0} 字，完整内容请到「结构化解析」中使用或重新解析）".format(max_chars) if truncated else "",
    }


@router.patch("/documents/{doc_id}/category")
async def update_document_category(doc_id: str, body: dict, db=Depends(get_db)):
    """手动修改文件分类（自动分类不准确时用户可手动调整）。

    ✅ 三侧口径对齐：分类值参与多处下游逻辑（提取侧按分类排优先级、
    展示侧按分类映射 Tag 颜色），写入未知值会让这些口径静默退化。
    故只接受 category-options 登记的合法分类（含「其他」）。
    """
    category = body.get("doc_category", "").strip()
    if not category:
        raise HTTPException(400, "doc_category 不能为空")
    valid_categories = {cat for cat, _ in _DOC_CATEGORY_KEYWORDS} | {"其他"}
    if category not in valid_categories:
        raise HTTPException(
            400, f"未知分类（{category}），可选："
            f"{', '.join(sorted(valid_categories))}")
    cur = await db.execute("SELECT id FROM project_documents WHERE id=?", (doc_id,))
    # ✅ R13 读路径补漏（2026-10-05）：cursor 为 None 时 fetchone 抛
    #    AttributeError → 500；503 更准确（DB 瞬时故障可重试）。
    if cur is None:
        logger.warning("改分类前查询失败（db.execute 返回 None），doc=%s", doc_id)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    # ✅ R13 写路径补漏（2026-10-05）：UPDATE 未生效仍回 ok=True 是「假成功」
    #    —— 前端显示分类已改，刷新后又变回旧值。
    upd_cur = await db.execute(
        "UPDATE project_documents SET doc_category=? WHERE id=?", (category, doc_id))
    if upd_cur is None:
        logger.warning("改分类未生效（db.execute 返回 None，R13），doc=%s", doc_id)
        raise HTTPException(503, "文档服务暂时不可用，请稍后重试")
    # ✅ R72（2026-10-12）补 rowcount：上方 SELECT 已确认行存在，UPDATE 却影响
    #    0 行 = 预检与写之间目标行被并发删除。旧实现只拦 None（503）未拦 0 行，
    #    仍回 ok=True → 前端显示分类已改、刷新后变回旧值（与 _r13_write 口径不一致）。
    if safe_rowcount(upd_cur, what=f"改文档分类 {doc_id}") < 1:
        logger.warning("改分类 0 行生效（并发已删），doc=%s", doc_id)
        raise HTTPException(404, "文档不存在或已被删除")
    await db.commit()
    return {"ok": True, "doc_id": doc_id, "doc_category": category}


@router.get("/documents/category-options")
async def list_category_options():
    """返回所有可用的文档分类选项（供前端下拉选择）。

    ✅ 口径收敛（2026-09-25）：选项与关键词规则统一取自
    ``services/doc_categories.py``（分类唯一事实源），不再在本文件散落维护。
    """
    return {
        "options": category_options(),
        "auto_keywords": [{"category": cat, "keywords": list(kws)}
                          for cat, kws in AUTO_CLASSIFY_RULES],
    }


# ✅ 提取入口：SSE /sse/generate-facts/{scheme_id}（见 sse_handlers.py）
