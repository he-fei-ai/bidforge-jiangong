"""公共文件解析模块。

支持格式：DOCX、PDF、MD、TXT、XLSX、XLS、CSV、图片（OCR）。
所有临时文件使用 try/finally 确保异常时清理。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import tempfile

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    "docx", "pdf", "md", "txt", "xlsx", "xls", "csv",
    "png", "jpg", "jpeg", "bmp", "tiff",
    # ✅ 2026-09-22 新增（对齐 OpenBidKit 招标文件解析模块）：旧版 Word 二进制格式。
    #    招投标场景仍有大量招标文件是 .doc/.wps，旧实现直接以「不支持的文件格式」
    #    400 拒绝，用户必须自己另存为 .docx；现由 services/legacy_office.py
    #    调用本地 LibreOffice/Word/WPS 转换为 .docx 后复用同一套 OOXML 解析。
    #    未安装任何转换组件时给出「可操作」提示（含下载地址），而非泛化失败。
    "doc", "wps",
}

#: OLE 复合文档文件头（旧版 Word / Excel 的容器格式）
OLE_COMPOUND_HEADERS = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)

# ✅ 修复（2026-09-25）：旧版 Excel/Word 的 OLE 目录项以 UTF-16LE 存储流名。
#    据此把 OLE 容器细分为 .xls（Workbook 流）与 .doc（WordDocument 流），
#    旧实现把所有 OLE 一律判为 doc 走 LibreOffice→docx 次优转换通道。
#    注意：只认 "Workbook"（Excel 97-2003 主流），不认 "Book" —— 后者是普通
#    英文单词，可能出现在 Word 正文（UTF-16 存储）里造成误判。
_OLE_XLS_MARKER = "Workbook".encode("utf-16-le")
_OLE_DOC_MARKER = "WordDocument".encode("utf-16-le")


def _ole_content_kind(content: bytes) -> str:
    """按 OLE 目录流名细分容器类型：返回 "xls" / "doc" / ""（无法区分）。"""
    if content[:8] not in OLE_COMPOUND_HEADERS:
        return ""
    has_xls = _OLE_XLS_MARKER in content
    has_doc = _OLE_DOC_MARKER in content
    if has_xls and not has_doc:
        return "xls"
    if has_doc and not has_xls:
        return "doc"
    return ""  # 双标记/无标记：交回调用方按原逻辑兜底

# ✅ 公共文件头签名：仅对**二进制格式**做轻量校验（文本类不做限制）。
#    提取到解析模块统一维护，供两条上传链路（global-facts 资料上传、
#    upload-outline 目录识别）复用 —— 旧实现只在资料上传处校验，目录识别
#    上传路径完全没有防护，把 .exe 改名为 .docx 也能落盘并进入解析流程。
_BINARY_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    # OOXML（docx/xlsx）为 ZIP 容器；旧版 .xls / 部分 docx 为 OLE 复合文档
    "docx": (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"),
    "xlsx": (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"),
    "xls": (b"PK\x03\x04", b"\xd0\xcf\x11\xe0"),
    # ✅ 旧版 Word：正常为 OLE 复合文档；同时放行 ZIP（部分工具会把 .docx
    #    改名成 .doc），真实类型由 parse_file_content_ex 的文件头路由决定。
    "doc": (b"\xd0\xcf\x11\xe0", b"PK\x03\x04"),
    "wps": (b"\xd0\xcf\x11\xe0", b"PK\x03\x04"),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "bmp": (b"BM",),
    "tiff": (b"II*\x00", b"MM\x00*"),
}


def signature_valid(ftype: str, prefix: bytes) -> bool:
    """轻量文件头校验：拒绝「扩展名与真实内容不符」的上传。

    只读取极小前缀（调用方读 16 字节即可），不解析内容，开销可忽略。
    未登记的扩展名（txt/md/csv 等文本类）一律放行，保持兼容。
    """
    sigs = _BINARY_SIGNATURES.get((ftype or "").lower())
    if not sigs:
        return True
    return any(prefix.startswith(s) for s in sigs)


#: 支持的扩展名全集（解析路由据此分流；图片按扩展名路由到 OCR）。
_SUPPORTED_EXTENSIONS = {
    "docx", "pdf", "md", "txt", "csv", "xlsx", "xls",
    "png", "jpg", "jpeg", "bmp", "tiff",
    "doc", "wps",   # 旧版 Word（需本地 Office 组件转换，见 legacy_office.py）
}


def _sniff_type(content: bytes) -> str:
    """按文件头嗅探明确可路由的二进制类型（无扩展名时也能正确解析）。

    仅覆盖「扩展名无法可靠反推解析器」之外的明确类型：PDF、常见图片，
    以及 OLE 复合文档（旧版 Word/Excel 的容器，对齐 OpenBidKit
    convert.mjs 的 `isOleCompoundHeader → legacy_word` 路由）。
    ZIP 容器（.docx/.xlsx）魔数相同、无法区分，返回空串交由调用方按
    「疑似二进制即拒绝（提示保留扩展名）」处理。
    """
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if content[:3] == b"\xff\xd8\xff":
        return "jpg"
    if content[:2] == b"BM":
        return "bmp"
    if content[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if content[:8] in OLE_COMPOUND_HEADERS:
        # ✅ 修复（2026-09-25）：旧实现把所有 OLE 一律判为 doc，改名的旧版
        #    .xls 会被送进 LibreOffice→docx 转换通道（次优路径）。现按 OLE
        #    目录流名细分：含 Workbook 流 → xls（走 xlrd 专用解析），
        #    否则维持 doc 兜底（与参考实现一致）。
        return _ole_content_kind(content) or "doc"
    return ""


def _looks_binary(content: bytes) -> bool:
    """采样前若干字节判断内容是否疑似二进制（用于拒绝无扩展名的二进制文件）。"""
    head = content[:8192]
    if not head:
        return False
    if b"\x00" in head:
        return True
    nontext = sum(1 for b in head if b < 0x09 or (0x0E <= b < 0x20))
    return nontext / len(head) > 0.1


def _reject_binary_text_ext(content: bytes) -> None:
    """文本扩展名（txt/md/csv）的二进制防护。

    ✅ 修复（2026-09-26，F1）：旧实现二进制检测只在「扩展名未知」时执行，
    ``.exe`` 改名 ``.txt`` 会绕过，经 ``utf-8(errors="ignore")`` 兜底解码成
    乱码入库、污染事实提取。这里对文本扩展名同样做防护：以**原始字节**判定
    为主（含 NUL / 高比例控制字符即疑似二进制）—— 避免 gbk 把含 0x00 的二进制
    静默解成带 ``U+0000`` 的"文本"而漏过。仅当严格解码成功且解码结果**不含
    NUL** 才确为文本放行。
    """
    if not _looks_binary(content):
        # 原始字节不像二进制：正常文本（UTF-8/GBK 均无 NUL），直接放行
        return
    # 疑似二进制：二次确认 —— 严格解码（utf-8-sig/gbk）能成功且解码结果不含
    # NUL，才确为文本；否则（解码失败或仍含 NUL）拒绝。
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = content.decode(enc)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return
    raise ParseError(
        "文件内容疑似二进制（扩展名与真实内容不符）：请确认源文件类型，"
        "保留原始扩展名（.txt/.md/.csv 等文本）后重新上传")


def _resolve_pdf_max_pages(default: int = 50) -> int:
    """读取 PDF 文本层页数上限（``settings.pdf_text_max_pages``）。

    独立的单一出口，便于：① 单测直接 monkeypatch 本函数验证回退；
    ② 将来若改成「按文件体积动态决定」只需改这一处。

    两类非法输入都必须回落到 ``default``，**绝不静默取小值**：

    - 非正数（``0`` / 负数）→ 返回 0 会让 ``doc.pages(0, 0)`` 一页都不解析，
      且因「页数 > 上限」不成立而**不产生任何截断告警** —— 用户拿到空文档
      却看不到原因。
    - **非整数**（``3.7`` / ``"50.5"``）→ 若直接 ``int()`` 截断会得到 3，
      于是「配了个小数」静默变成「只解析 3 页」，比报错糟得多。
    """
    try:
        from app.config import settings
        raw = getattr(settings, "pdf_text_max_pages", default)
    except Exception:  # pragma: no cover - 配置不可用时保持旧行为
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    # 非整数输入：只有「数值上恰好等于其整数部分」时才接受
    try:
        if float(raw) != n:
            return default
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


# ⚠️ 修复（2026-09-30 第十四轮 · P0 数据丢失）：旧值是**硬编码 50**且不可配置。
#    招标文件 / 施工组织设计常见 100~400 页 —— 一份 300 页的招标文件只提取前
#    50 页，**后面 250 页的工程参数、清单、图纸说明全部丢失**，且不会进入
#    目录 / 正文 / 事实 / 导出任何一级。
#    改为读 ``settings.pdf_text_max_pages``（默认 500，与 MAX_PARSED_CHARS
#    =400000 字 ≈ 500 页对齐，避免「解析了却在落库环节二次截断」）。
#    ⚠️ 仍保留为**模块级常量**：全部调用点按模块全局读取，故既有
#    ``monkeypatch.setattr(fp, "MAX_PDF_PAGES", N)`` 的单测全部照常生效。
MAX_PDF_PAGES = _resolve_pdf_max_pages()
# ✅ 增强：机械统计表（全局事实最高价值结构化区）常超 100 行，旧上限会静默丢行。
#    解析产物只是文本，后续 split_into_chunks 会按 8000 字/段切分，
#    把超长表拆成多段（每段复用表头），不会造成输入爆炸。上调到 1000 行，
#    覆盖绝大多数招投标机械/材料统计表；超出仍记录告警（不静默丢数据）。
MAX_CSV_ROWS = 1000
MAX_EXCEL_ROWS = 1000

# ---------- ✅ 资源防护（压缩炸弹 / 解压膨胀 / 条目数） ----------
# DOCX/XLSX 本质是 ZIP 容器：30MB 的上传上限不等于解压后的体积上限，
# 精心构造的归档可以做到 1:1000 以上的膨胀比，在解压阶段直接打满内存/CPU。
# 这里在解析前先读 ZIP 中央目录（只读元数据、不解压）做三道闸门。
MAX_ARCHIVE_ENTRIES = 2000                      # 单归档最大条目数
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 400 * 1024 * 1024   # 单归档最大解压总量（400MB）
MAX_COMPRESSION_RATIO = 200                     # 最大允许压缩比（解压总量 / 压缩总量）
MAX_ARCHIVE_ENTRY_BYTES = 64 * 1024 * 1024      # 单条目「实际读出」上限（防中央目录谎报大小）


class ParseError(RuntimeError):
    """不可恢复的解析失败（缺少依赖 / 无可用 OCR 引擎 / 文件损坏）。

    ✅ 设计约束：失败时必须【抛异常】而不是返回「解析失败：...」这类字符串——
    旧实现把错误说明当作正文返回，上层会把它当作有效文本落库，
    最终 AI 会从这段「错误信息」里提取事实，污染全局事实库。
    """


def parse_file_content(content: bytes, fname: str) -> str:
    """解析上传文件内容为纯文本（兼容入口，等价于带诊断解析的 text 部分）。

    Raises:
        ParseError: 不可恢复的解析失败（缺依赖 / 无可用 OCR 引擎 / 文件损坏 /
            归档膨胀超限）。调用方应把消息展示给用户，切勿把消息当作正文入库。
    """
    return parse_file_content_ex(content, fname)[0]


def parse_file_content_ex(content: bytes, fname: str) -> tuple[str, dict]:
    """解析上传文件内容为 (纯文本, 诊断信息)。

    ✅ 增强（可观测性）：旧实现只返回字符串，"PDF 超 50 页 / CSV 超 1000 行 /
    归档膨胀被限制"这类「解析成功但内容不完整」的情况只能进日志，路由层无法
    告知用户，表现为"事实明明在文件里却没被提取"却查不到原因。现返回诊断字典：

        {"file_type": "pdf", "text_len": 12345, "truncated": True,
         "warnings": ["..."]}

    诊断只用于告警与展示，绝不参与正文构造。

    Raises:
        ParseError: 不可恢复的解析失败（缺依赖 / 无 OCR 引擎 / 文件损坏 /
            压缩炸弹嫌疑）。
    """
    ftype = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    diag: dict = {"file_type": ftype, "text_len": 0, "truncated": False,
                  "warnings": []}
    if not content:
        return "", diag

    # ✅ B-1 修复：无扩展名 / 未知扩展名的文件不能静默走文本解码（二进制 PDF/ZIP
    #    会被当文本解码成乱码入库，污染事实提取）。先用文件头嗅探真实类型：
    #    嗅探命中则按真实类型解析（PDF / 图片即使无扩展名也能正确解析）；
    #    嗅探未命中但内容疑似二进制 → 明确拒绝并提示保留扩展名；
    #    仅当确为文本内容时才按文本解码。
    if not ftype or ftype not in _SUPPORTED_EXTENSIONS:
        _sniffed = _sniff_type(content)
        if _sniffed:
            ftype = _sniffed
        elif _looks_binary(content):
            raise ParseError(
                "无法识别文件类型：请保留原始扩展名（.pdf/.docx/.xlsx/.txt/.md/"
                ".csv 或常见图片）后重新上传")
        else:
            ftype = "txt"
    # ✅ 增强（2026-09-22，对齐易标 convert.mjs 的文件头路由）：
    #    OLE 复合文档头（D0 CF 11 E0）不可能是真正的 .docx（OOXML 是 ZIP 容器）。
    #    扩展名声称 .docx 而内容为 OLE 的文件（旧版 Word「另存为 docx」失败、
    #    或手工改名）此前会走 _parse_docx → docx2python 全部失败，用户只看到
    #    「文件无法识别或已损坏」；现按真实类型改走旧版 Word 转换通道。
    if ftype == "docx" and content[:8] in OLE_COMPOUND_HEADERS:
        ftype = "doc"
        _note_info(diag, "文件实际为旧版 Word（OLE 复合文档）格式，"
                         "已按 .doc 通道转换解析")
    # ✅ 修复（2026-09-25）：扩展名与 OLE 真实类型双向纠错 ——
    #    改名的旧版 .xls（声称 .doc/.wps）不再走 LibreOffice→docx 次优转换，
    #    改走 xlrd 专用解析；反之声称 .xls 的旧版 Word 也不再误入 xlrd。
    #    仅在流名标记无歧义时纠错（双标记/无标记维持原路由，行为不变）。
    if ftype in ("doc", "wps") and _ole_content_kind(content) == "xls":
        ftype = "xls"
        _note_info(diag, "文件实际为旧版 Excel（OLE 复合文档，含 Workbook 流），"
                         "已按 .xls 通道解析")
    elif ftype == "xls" and _ole_content_kind(content) == "doc":
        ftype = "doc"
        _note_info(diag, "文件实际为旧版 Word（OLE 复合文档，含 WordDocument 流），"
                         "已按 .doc 通道转换解析")

    # 嗅探/兜底可能改写了 ftype，同步到诊断字典
    diag["file_type"] = ftype

    # ✅ F1 修复（2026-09-26）：文本扩展名同样做二进制防护（见 _reject_binary_text_ext）。
    #    旧实现二进制检测只在「扩展名未知」时执行，.exe 改名 .txt 会绕过。
    if ftype in ("txt", "md", "csv"):
        _reject_binary_text_ext(content)

    # ✅ 压缩炸弹闸门：DOCX/XLSX 为 ZIP 容器，解析前先校验归档规模。
    #    .xls / .doc / .wps 是 OLE 复合文档，不适用本检查。
    if ftype in ("docx", "xlsx"):
        _guard_zip_archive(content, ftype)

    raw_text = ""
    if ftype == "docx":
        raw_text = _parse_docx(content)
    elif ftype == "pdf":
        raw_text = _parse_pdf(content, diag, fname=fname)
    elif ftype in ("md", "txt"):
        raw_text, _dec_w = _decode_text_ex(content)
        diag["warnings"].extend(_dec_w)
    elif ftype == "csv":
        raw_text = _parse_csv(content, diag)
    elif ftype in ("xlsx", "xls"):
        raw_text = _parse_excel(content, ftype, diag)
    elif ftype in ("png", "jpg", "jpeg", "bmp", "tiff"):
        raw_text = _parse_image_ocr(content, ftype, diag)
    elif ftype in ("doc", "wps"):
        raw_text = _parse_legacy_word(content, fname, diag)
    else:
        raw_text, _dec_w = _decode_text_ex(content)
        diag["warnings"].extend(_dec_w)

    text = raw_text or ""
    diag["text_len"] = len(text)
    # ✅ 四层存储（解析层）：统计正文中的页标记数作为解析页数。非 PDF 格式
    #    无分页概念，整体视为 1 页（页码溯源仍以 #page:1 定位）。
    #    ✅ 修复（2026-09-25）：按「去重页号数」而非标记总数统计 —— 主通道
    #    与 OCR 兜底通道拼接时（扫描件检测命中：原生文字层过少 → OCR 补充），
    #    两条通道各自逐页注入 `<!-- page:N -->`，同一页号出现两次，标记总数
    #    会虚增一倍（3 页 PDF 报 6 页）。页数语义 = 出现过的不同页号个数。
    diag["page_count"] = len(set(_PDF_PAGE_MARK_RE.findall(text))) or (1 if text.strip() else 0)
    return text, diag


# ---------------------------------------------------------------------------
# ✅ 四层存储（解析层）：页标记 —— 可追溯性的锚点。
#    PDF 解析逐页注入 `<!-- page:N -->`，解析层结构化抽取（md_structured）
#    据此把内容/表格/图片归位到页。已有标记的通道（MinerU 云端 Markdown 等）
#    不重复注入。
# ---------------------------------------------------------------------------
_PDF_PAGE_MARK_RE = re.compile(r"<!--\s*page\s*:?\s*(\d+)\s*-->", re.I)


def _add_page_markers(items: list[tuple[int, str]]) -> str:
    """把 (页号, 页文本) 序列拼为带 `<!-- page:N -->` 标记的正文。"""
    out: list[str] = []
    for num, txt in items:
        t = (txt or "").strip()
        if not t:
            continue
        if _PDF_PAGE_MARK_RE.search(t):
            out.append(t)
        else:
            out.append(f"<!-- page:{num} -->\n{t}")
    return "\n".join(out)


def _note_truncation(diag: dict | None, message: str) -> None:
    """记录"解析成功但内容被截断"的诊断（同时写日志，便于无 diag 调用方排查）。"""
    logger.warning("%s", message)
    if diag is None:
        return
    diag["truncated"] = True
    warnings = diag.setdefault("warnings", [])
    if message not in warnings:
        warnings.append(message)


def _note_info(diag: dict | None, message: str) -> None:
    """记录"解析过程提示"（OCR 兜底 / 加密 PDF 等），不影响 truncated 标记。

    ✅ 增强：与截断告警共用 warnings 列表（都会随 diag 返回并持久化到
    project_documents.parse_warnings），但不置 truncated=True —— 信息类提示
    不应让前端把文档标成"可能被截断"。
    """
    logger.info("%s", message)
    if diag is None:
        return
    warnings = diag.setdefault("warnings", [])
    if message not in warnings:
        warnings.append(message)


#: 解析告警持久化的条数 / 单条长度上限（落库列 parse_warnings 的容量护栏）
MAX_PARSE_WARNINGS = 20
MAX_PARSE_WARNING_CHARS = 300


def dump_parse_warnings(warnings) -> str:
    """把解析告警列表序列化为**合法** JSON 字符串（供 parse_warnings 列落库）。

    ✅ BUG 修复（2026-09-18）：旧实现在两处路由里写
    ``json.dumps(warnings, ensure_ascii=False)[:2000]`` —— 直接对序列化后的 JSON
    字符串做字节切片，告警较多/较长时会把 JSON 截成非法串；读取端的
    ``_decode_parse_warnings`` 一旦 json.loads 失败就返回 []，于是**全部告警静默丢失**。
    现改为：先在 Python 侧限制「条数 + 单条长度」，保证序列化结果本身合法
    （最坏情况只是告警被截断成 N 条），绝不会产出非法 JSON。

    Args:
        warnings: 告警列表（元素会被 str() 归一）。

    Returns:
        合法 JSON 数组字符串（无告警时为 ``"[]"``）。
    """
    items = [str(w).strip() for w in (warnings or [])]
    items = [w for w in items if w][:MAX_PARSE_WARNINGS]
    items = [w if len(w) <= MAX_PARSE_WARNING_CHARS
             else w[:MAX_PARSE_WARNING_CHARS - 1] + "…" for w in items]
    return json.dumps(items, ensure_ascii=False)


def _guard_zip_archive(content: bytes, ftype: str) -> None:
    """ZIP 归档安全闸门：条目数 / 解压总量 / 压缩比，防压缩炸弹。

    只读取中央目录元数据（不解压任何条目），开销与文件大小同阶、可忽略。
    任一闸门超限即抛 ParseError，由路由层转成可读的 400 提示。

    设计取舍：宁可拒绝一个可疑的超大归档，也不要在解析阶段把内存打满——
    解析发生在服务进程内，OOM 会连带影响正在生成的方案任务。
    """
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            infos = z.infolist()
    except zipfile.BadZipFile as e:
        raise ParseError(f"文件已损坏或不是有效的 {ftype.upper()} 文件：{e}") from e
    except Exception as e:
        # 归档结构异常（非标准 ZIP 变体）交由后续解析通道处理，不在此拦截
        logger.warning("归档结构检查失败（继续解析）: %s", e)
        return

    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ParseError(
            f"文件内部条目过多（{len(infos)} > {MAX_ARCHIVE_ENTRIES}），"
            "疑似异常归档，已拒绝解析")

    total_uncompressed = sum(max(i.file_size, 0) for i in infos)
    total_compressed = sum(max(i.compress_size, 1) for i in infos)
    if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ParseError(
            f"文件解压后体积过大（{total_uncompressed // (1024 * 1024)}MB > "
            f"{MAX_ARCHIVE_UNCOMPRESSED_BYTES // (1024 * 1024)}MB），已拒绝解析")

    # ✅ 必须**逐条**判定，不能只算汇总：汇总压缩比可以被稀释 —— 攻击者在归档里
    #    塞一个体积大、几乎不可压缩的填充条目（比如 74KB 随机数据），就能把
    #    「8MB 的炸弹条目」造成的整体比值从 1000:1 拉到 111:1，从而低于阈值。
    #    实测（安全审计）：74KB 的归档携带 8MB XML，整体比值 111 < 200 → 放行，
    #    随后 docx 解析耗时 13.1s、峰值内存 107MB。
    for info in infos:
        declared = max(info.file_size, 0)
        packed = max(info.compress_size, 1)
        if declared > MAX_ARCHIVE_ENTRY_BYTES:
            raise ParseError(
                f"文件内部存在超大条目「{info.filename}」"
                f"（解压后 {declared // (1024 * 1024)}MB > "
                f"{MAX_ARCHIVE_ENTRY_BYTES // (1024 * 1024)}MB），已拒绝解析")
        entry_ratio = declared / packed
        if entry_ratio > MAX_COMPRESSION_RATIO:
            raise ParseError(
                f"文件内部条目「{info.filename}」压缩比异常"
                f"（{entry_ratio:.0f}:1 > {MAX_COMPRESSION_RATIO}:1），"
                "疑似压缩炸弹，已拒绝解析")

    ratio = total_uncompressed / total_compressed if total_compressed else 0
    if ratio > MAX_COMPRESSION_RATIO:
        raise ParseError(
            f"文件压缩比异常（{ratio:.0f}:1 > {MAX_COMPRESSION_RATIO}:1），"
            "疑似压缩炸弹，已拒绝解析")


def _read_zip_entry_limited(z, name: str,
                            limit: int | None = None) -> bytes:
    """有界读取归档内单个条目，防「中央目录谎报大小」绕过 `_guard_zip_archive`。

    为什么需要：`_guard_zip_archive` 只能读**中央目录声明**的 size / ratio，
    而中央目录本身是可伪造的 —— 把 `file_size` 谎报成 100、真实压缩数据却
    能解出 GB 级内容，就能从闸门底下溜过去（已实测复现）。这里对**实际读出的
    字节数**再加一道上限，不依赖任何声明值，避免把「防护是否生效」寄托在
    zipfile 的实现细节（例如它恰好在解压前先校验 CRC）上。
    """
    # 默认值必须在调用时解析：写成 `limit: int = MAX_ARCHIVE_ENTRY_BYTES`
    # 会在函数**定义时**就把常量固化进 __defaults__，之后 monkeypatch
    # 模块常量（测试 / 运行期调参）将完全失效。
    if limit is None:
        limit = MAX_ARCHIVE_ENTRY_BYTES
    chunks: list[bytes] = []
    total = 0
    with z.open(name) as fp:
        while total < limit:
            chunk = fp.read(min(1024 * 1024, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        # 已达上限却还有剩余 → 该条目实际内容超限，按压缩炸弹处理
        if total >= limit and fp.read(1):
            raise ParseError(
                f"文件内部条目过大（超过 {limit // (1024 * 1024)}MB），"
                "疑似压缩炸弹，已拒绝解析")
    return b"".join(chunks)


def _decode_text_ex(content: bytes) -> tuple[str, list[str]]:
    """智能解码文本：返回 ``(文本, 告警列表)``。

    编码回退顺序：``utf-8-sig``（剥离 BOM）→ ``gbk`` → ``gb18030`` →
    ``utf-8(errors="ignore")``。

    ✅ 用 utf-8-sig 替代 utf-8：Excel 导出的 CSV 常带 BOM，旧实现首列表头
    残留 \\ufeff，导致名称归一/映射表失配、去重与分类失败。utf-8-sig 对
    无 BOM 文本解码结果与 utf-8 完全一致。

    ✅ 修复（2026-09-17）：旧实现直接 ``for enc in (utf-8-sig, gbk, gb18030)``
    顺序解码，GBK 容错性极强，一个**轻微损坏/被截断的 UTF-8 文件**在 utf-8-sig
    失败后会被 GBK 静默解成乱码，且永不到达末尾的 errors="ignore" 安全分支、
    也不告警 —— 下游事实提取会从中提取出错误"事实"（静默错误数据流）。
    现改为：utf-8-sig 严格优先；仅当 UTF-8 真正失败时再回退 GBK/GB18030，
    且回退成功时记录告警，提示存在乱码风险（供上层诊断透传）。

    ✅ 修复（2026-09-25）：末级 ``utf-8(errors="ignore")`` 兜底分支对 Big5 /
    Shift-JIS 等**无法识别的编码**同样会产出乱码（errors="ignore" 直接丢弃
    非法字节），但旧实现既不告警、也不回传任何信号 —— 乱码污染下游事实库却
    无任何痕迹。现显式返回一条告警，由调用方写入解析诊断（``parse_warnings``）
    持久化透传，前端可在「信息显示窗口」诊断区看见编码风险。
    """
    warnings: list[str] = []
    text: str | None = None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = None
    if text is None:
        for enc in ("gbk", "gb18030"):
            try:
                text = content.decode(enc)
            except UnicodeDecodeError:
                continue
            warnings.append(
                f"文本非 UTF-8，已按 {enc} 解码（可能存在乱码，建议确认源文件编码）")
            break
        if text is None:
            # UTF-8 / GBK 均失败：以 UTF-8 容错解码，保证不崩、乱码可控（但需告警）
            warnings.append(
                "源文件编码无法识别（非 UTF-8/GBK/GB18030），已按 UTF-8 容错解码，"
                "可能存在乱码，建议确认源文件编码（如 Big5/Shift-JIS）")
            text = content.decode("utf-8", errors="ignore")
    # ✅ F5 修复（2026-09-26）：归一不可见字符，避免污染事实去重与比对。
    text, _inv_changed = _normalize_invisible(text)
    if _inv_changed:
        warnings.append(
            "源文件含不可见控制字符 / 零宽字符 / 不间断空格，已自动归一"
            "（删除或替换为空格），可能影响事实去重与比对")
    return text, warnings


def _normalize_invisible(text: str) -> tuple[str, bool]:
    """归一不可见字符：删除 NUL 与 C0 控制字符（保留 \\n/\\t、\\r 归一为 \\n）、
    删除零宽字符（U+200B/C/D、非首部 U+FEFF）、NBSP(U+00A0) 替换为空格。

    ✅ 修复（2026-09-26，F5）：这些字符会钻进 AI 返回的 JSON 围栏破坏解析、
    让同一事实因隐藏字符被判为不同键产生重复、污染 normalized evidence 比对。
    返回 ``(归一后文本, 是否发生过修改)``。
    """
    out: list[str] = []
    changed = False
    for ch in text:
        o = ord(ch)
        if ch == "\r":
            # 归一为换行：\\r\\n → \\n，孤立 \\r 直接丢弃
            changed = True
            continue
        if o == 0xFEFF:
            # BOM 已由 utf-8-sig 剥离；残留的 FEFF 视为零宽删除
            changed = True
            continue
        if ch in ("\u200b", "\u200c", "\u200d"):
            changed = True
            continue
        if o == 0x00A0:
            out.append(" ")
            changed = True
            continue
        if o < 0x20 and ch != "\n" and ch != "\t":
            # 其它 C0 控制字符（含 NUL）删除
            changed = True
            continue
        out.append(ch)
    return "".join(out), changed


def _decode_text(content: bytes) -> str:
    """兼容入口：仅返回文本（告警被丢弃，仅供无需诊断的内部调用）。"""
    return _decode_text_ex(content)[0]


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_heading_level(p_el, ) -> int:
    """从段落的 pStyle 推断标题层级（0 = 非标题）。
    兼容 Heading1/heading 1/标题 1 等中英文样式名。"""
    import re as _re
    pstyle = p_el.find(f"{{{_W_NS}}}pPr/{{{_W_NS}}}pStyle")
    if pstyle is None:
        return 0
    val = (pstyle.get(f"{{{_W_NS}}}val") or "").strip()
    m = _re.match(r'^(?:heading|标题)\s*(\d)$', val, _re.IGNORECASE)
    if m:
        return min(int(m.group(1)), 6)
    return 0


def _docx_bold_heading_level(p_el) -> int:
    """启发式：未用 Heading 样式、但明显是标题的段落提升为 markdown 标题。

    ✅ 增强：大量真实 DOCX 只用「加粗 + 缩进」充当章节标题，OOXML 路径识别不到
    pStyle → 整篇被当一段 → 事实提取分段器（按 # 切分）无从切分 → 提取质量
    严重下降。这里把短且加粗 / 含编号 / 含「章节篇部」的段落提升为标题，
    让分段器正确按章节切分。判定保守，避免把正文句子误判为标题。
    """
    txt = "".join(
        t.text or "" for t in p_el.iter(f"{{{_W_NS}}}t")).strip()
    if not txt or len(txt) > 40:
        return 0
    # 编号标题（第X章 / 一、 / 1.1 等）
    if re.match(
        r'^(第[一二三四五六七八九十百\d]+[章節篇部]|'
        r'[一二三四五六七八九十]+[、.．]|[\d]+[.、)）])',
        txt,
    ):
        return 3
    # 加粗判定：任一 run 含 w:b
    bold = False
    for r in p_el.iter(f"{{{_W_NS}}}r"):
        rpr = r.find(f"{{{_W_NS}}}rPr")
        if rpr is not None and rpr.find(f"{{{_W_NS}}}b") is not None:
            bold = True
            break
    if not bold:
        return 0
    if any(k in txt for k in ("章", "节", "部分", "篇")):
        return 3
    # 短加粗且不以句末标点结尾（不像正文句子）→ 视为小标题
    if len(txt) <= 16 and not txt.endswith(("。", "，", "；", "：", "、")):
        return 4
    return 0


def _parse_docx(content: bytes) -> str:
    """DOCX 解析（增强版）：直接解析 OOXML，输出结构化 Markdown。

    - 标题样式（Heading/标题）→ "#/##/###" 层级，
      与事实提取的分段器（按 # 切分）联动，章节语义完整
    - 表格 → "【表格】" + 竖线分隔行，避免单元格内容糊成一团
    - 解析失败自动回退 docx2python
    """
    import io as _io
    import zipfile
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(_io.BytesIO(content)) as z:
            xml_bytes = _read_zip_entry_limited(z, "word/document.xml")
        body = ET.fromstring(xml_bytes).find(f"{{{_W_NS}}}body")
        if body is None:
            raise ValueError("document.xml 无 body")
    except ParseError:
        # 安全闸门（条目过大 / 压缩炸弹）必须直达用户，不能被回退链路吞掉
        raise
    except Exception as e:
        logger.warning("DOCX OOXML 解析失败，回退 docx2python: %s", e)
        return _parse_docx_fallback(content)

    def para_text(p) -> str:
        return "".join(
            t.text or "" for t in p.iter(f"{{{_W_NS}}}t")).strip()

    parts: list[str] = []
    for child in body:
        tag = child.tag.split("}")[-1]
        if tag == "p":
            txt = para_text(child)
            if not txt:
                continue
            level = _docx_heading_level(child) or _docx_bold_heading_level(child)
            parts.append(f"{'#' * level} {txt}" if level else txt)
        elif tag == "tbl":
            row_cells: list[list[str]] = []
            for tr in child.findall(f"{{{_W_NS}}}tr"):
                cells = []
                for tc in tr.findall(f"{{{_W_NS}}}tc"):
                    cell_txt = " ".join(
                        t for t in (para_text(p) for p in tc.iter(
                            f"{{{_W_NS}}}p")) if t)
                    cells.append(cell_txt)
                row_cells.append(cells)
            if row_cells:
                # ✅ BUG 修复（表格结构完整性）：旧实现用自己的拼接逻辑，
                #    单元格内的竖线未做转义 → 内容里的「|」被下游当作列分隔符，
                #    整张表的列对齐被撕裂（机械统计表因此丢掉部分列）。
                #    现统一走 _md_cells（与 CSV/Excel 同一口径），并补上表头后的
                #    Markdown 对齐分隔行（GFM 渲染表格的必要条件，见 _md_separator）。
                md_rows = [_md_cells(c) for c in row_cells]
                md_rows.insert(1, _md_separator(row_cells[0]))
                parts.append("【表格】\n" + "\n".join(md_rows))

    text = "\n".join(parts)
    if not text.strip():
        logger.warning("DOCX 结构化解析结果为空，回退 docx2python")
        return _parse_docx_fallback(content)
    return text


def _parse_docx_fallback(content: bytes) -> str:
    """DOCX 回退解析：docx2python（纯文本）"""
    from docx2python import docx2python
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
    try:
        tmp.write(content)
        tmp.close()
        with docx2python(tmp.name) as doc:
            return doc.text
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def _parse_legacy_word(content: bytes, fname: str,
                       diag: dict | None = None) -> str:
    """旧版 Word（.doc/.wps）解析：本地 Office 组件转 .docx → 复用 OOXML 解析。

    ✅ 新增（2026-09-22，对齐 OpenBidKit `doc2markdown::withLegacyWordDocxFile`
    与 `fileService.resolveFileParser`）：招投标场景大量招标文件仍是 .doc/.wps，
    旧实现直接以「不支持的文件格式」400 拒绝，用户必须自己另存为 .docx 才能用。

    转换后端与优先级由 `services/legacy_office.detect_office_backends` 决定
    （LibreOffice 优先，Windows 上回退 Word/WPS COM；.wps 时 WPS COM 优先），
    转换结果走既有 `_parse_docx`，因此标题层级 / 表格 Markdown 结构化能力
    与 .docx 完全一致。转换用的具体组件名会写入解析诊断（persist 到
    parse_warnings），用户事后可核对「这份内容是怎么来的」。

    Raises:
        ParseError: 未安装任何转换组件，或全部后端转换失败。
    """
    from app.services.legacy_office import convert_legacy_word_to_docx

    docx_bytes, backend = convert_legacy_word_to_docx(content, fname)
    # 转换产物同样是 ZIP 容器：过一遍归档闸门，避免异常容器绕过统一防护
    _guard_zip_archive(docx_bytes, "docx")
    text = _parse_docx(docx_bytes)
    _note_info(diag, f"已通过 {backend} 将旧版 Word 文件转换为 .docx 后解析")
    return text


# ---------------------------------------------------------------------------
# ✅ PDF 文本归一化 / 文字层缺失判定 / 双引擎表格合并去重（2026-09-17，
#    与 OpenBidKit 解析模块同源算法：报告 §5.3 / §13 常量口径）
# ---------------------------------------------------------------------------

# 文字层判定：去空白后必须存在连续 10 个以上字母/数字/汉字。
# 低于此要求（如只有页码、页眉、零散符号）视同「无可选文字层」→ 扫描件处理。
_PDF_INFORMATIVE_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{10,}")


def has_informative_text(text: str) -> bool:
    """文字层缺失判定：去掉全部空白后是否存在连续 ≥10 个可读字符。"""
    probe = re.sub(r"\s+", "", str(text or ""))
    return bool(_PDF_INFORMATIVE_RE.search(probe))


def _collapse_pdf_whitespace(line: str) -> str:
    """单行空白压缩：多空格合一、删中文字符间空格、删中文标点前空格。"""
    line = re.sub(r"[ \t\u00a0]+", " ", line).strip()
    line = re.sub(r"([\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", r"\1", line)
    line = re.sub(r" +([，。；：！？、）】》”])", r"\1", line)
    line = re.sub(r"([（【“]) +", r"\1", line)
    return line


def _collapse_repeated_pdf_text(line: str) -> str:
    """重复文本压缩（PDF 常把同一段文字重复绘制两遍）：

    1. 滑动窗口：相邻等长片段（2~12 字符）相同且【含中文】时删除后半段。
       仅认中文片段是刻意的保守取舍——"1.1.1"、"www.xxx.com"、纯数字编号
       这类合法重复序列不能被误伤。
    2. 中文短语级：如 "工程概况 工程概况"（中间空白分隔）压成单份。
    """
    s = line
    for size in range(2, 13):
        i = 0
        while i + 2 * size <= len(s):
            a, b = s[i:i + size], s[i + size:i + 2 * size]
            if a == b and re.search(r"[\u4e00-\u9fff]", a):
                s = s[:i + size] + s[i + 2 * size:]
            else:
                i += 1
    s = re.sub(
        r"([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9（）()：:、，,。.．\-]{1,29})\s+\1",
        r"\1", s)
    return s


def normalize_pdf_text(text: str) -> str:
    """PDF 原生文本归一化：换页符 → 空行、逐行空白/重复压缩、压缩连续空行。"""
    if not text:
        return ""
    text = text.replace("\f", "\n\n")
    lines = [_collapse_repeated_pdf_text(_collapse_pdf_whitespace(ln))
             for ln in text.split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return out.strip()


def _compact_dedup_probe(value: str) -> str:
    """去重指纹：删表格语法/标点/空白后转小写（只保留内容本身）。"""
    return re.sub(
        r"[|\s`*_#，。；：！？、,.!:;?%（）()\[\]{}《》<>\"'\-—]",
        "", str(value or "")).lower()


def _line_covered_by_tables(line_probe: str, table_probe: str) -> bool:
    """判断一行文本是否已被表格覆盖（避免同一内容在 Markdown 里出现两次）。

    与 OpenBidKit 同口径：完全包含即覆盖；≥8 字符的行按 6~12 字符分块，
    75% 以上分块命中表格指纹即判覆盖（容忍 OCR/文本层与表格单元格的
    轻微文本差异）。
    """
    if not line_probe:
        return True
    if len(line_probe) <= 2:
        return line_probe in table_probe
    if line_probe in table_probe:
        return True
    if len(line_probe) < 8:
        return False
    chunk_size = min(12, max(6, len(line_probe) // 2))
    covered = total = 0
    for i in range(0, len(line_probe), chunk_size):
        chunk = line_probe[i:i + chunk_size]
        if len(chunk) < 4:
            continue
        total += 1
        if chunk in table_probe:
            covered += 1
    return total > 0 and covered / total >= 0.75


def remove_lines_covered_by_tables(text: str, table_blocks: list[str]) -> str:
    """双引擎结果合并去重：从正文文本中剔除已被表格 Markdown 覆盖的行。"""
    if not text or not table_blocks:
        return text
    table_probe = _compact_dedup_probe("\n".join(table_blocks))
    if not table_probe:
        return text
    kept: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if s and _line_covered_by_tables(_compact_dedup_probe(s), table_probe):
            continue
        kept.append(line)
    return "\n".join(kept)


def _extract_pdf_page_tables(page) -> list[str]:
    """PDF 双引擎·引擎 B：PyMuPDF 内建表格识别 → Markdown 表格块。

    引擎 A 是 get_text 的原生文本流（表格被拍平成普通行）；引擎 B 用
    find_tables 按几何线条重建表格结构。两者结果合并前，先由
    remove_lines_covered_by_tables 把被表格覆盖的正文行剔除，避免同内容双份。
    单页/单表失败一律安全跳过（不影响整体解析，同 safePdfCall 语义）。
    """
    out: list[str] = []
    try:
        finder = getattr(page, "find_tables", None)
        if finder is None:      # 旧版 PyMuPDF 无此能力 → 引擎 B 静默缺席
            return out
        tables = finder().tables or []
        for t in tables:
            try:
                rows = t.extract() or []
            except Exception:
                continue
            norm_rows: list[list[str]] = []
            for r in rows:
                cells = ["" if c is None else
                         str(c).replace("\r", " ").replace("\n", " ").strip()
                         for c in r]
                if any(cells):
                    norm_rows.append(cells)
            if len(norm_rows) < 2:
                continue
            md_rows = [_md_cells(r) for r in norm_rows]
            md_rows.insert(1, _md_separator(norm_rows[0]))
            out.append("【表格】\n" + "\n".join(md_rows))
    except Exception as e:
        logger.debug("PDF 表格引擎 B 提取失败（跳过）: %s", e)
    return out


# ---------------------------------------------------------------------------
# 表头智能识别（CSV / Excel 共用，2026-09-17）
# ---------------------------------------------------------------------------

SPREADSHEET_HEADER_SCAN_ROWS = 20    # 表头在前 20 行内寻找
SPREADSHEET_HEADER_MIN_SCORE = 2     # 不同值数 < 2 视为无表头
SPREADSHEET_HEADER_MERGE_UP = 2      # 多行表头最多向上再吃 2 行


def _spreadsheet_header_score(row) -> int:
    """表头行打分：非空单元格的不同值个数（全部相同/单列 → 0 分）。"""
    vals = [str(v).strip() for v in row
            if v is not None and str(v).strip()]
    if len(vals) < 2:
        return 0
    return len(set(vals))


_NUMERIC_CELL_RE = re.compile(r"^[+-]?\d+([.,]\d+)*%?$")


def _is_header_like(row) -> bool:
    """「像表头」判定：不同值 ≥2 且非空率达标、且单元格以文本为主。

    纯数字行（如数量/金额数据行）不能当表头——数据行的不同值数
    完全可能高于真正的表头行，不排除会把数据行误选为表头。
    """
    vals = [str(v).strip() for v in row
            if v is not None and str(v).strip()]
    if len(vals) < 2 or len(set(vals)) < 2:
        return False
    non_numeric = [v for v in vals if not _NUMERIC_CELL_RE.fullmatch(v)]
    # 阈值 0.75：含数值列的数据行（如 "塔吊/2/全站仪" 文本占 2/3）不会被并入表头
    return len(non_numeric) / len(vals) >= 0.75


def find_table_header(rows: list) -> tuple[int, int, list[str]]:
    """在行集的前 20 行中识别表头（可能为多行表头）。

    返回 (表头起始行索引, 表头结束行索引, 纵向合并后的表头单元格列表)；
    未识别到表头时返回 (-1, -1, [])——调用方应生成「列1..列N」通用表头。

    算法：从上向下找第一个「像表头」的行作为起点，随后最多向下并入
    MERGE_UP 个同样「像表头」的行（两级分类表头），纵向拼接为 "上级 / 下级"
    （列内相邻相同值去重）。遇到数字主导的数据行即停止扩展。
    """
    scan = rows[:SPREADSHEET_HEADER_SCAN_ROWS]
    # 单列表格无表头识别意义（不同值数恒 ≤1）：保持旧口径，首行即表头
    if scan and max(len(r) for r in scan) == 1:
        first = rows[0][0] if rows[0] else None
        return 0, 0, [str(first).strip() if first is not None else ""]
    start = -1
    for i, row in enumerate(scan):
        if _is_header_like(row):
            start = i
            break
    if start < 0:
        return -1, -1, []
    end = start
    for j in range(start + 1, min(start + 1 + SPREADSHEET_HEADER_MERGE_UP,
                                  len(scan))):
        if not _is_header_like(scan[j]):
            break
        end = j
    if end == start:
        return start, end, [
            str(v).strip() if v is not None else "" for v in rows[start]]
    cols = max(len(rows[r]) for r in range(start, end + 1))
    merged: list[str] = []
    for c in range(cols):
        parts: list[str] = []
        for r in range(start, end + 1):
            row = rows[r]
            v = str(row[c]).strip() if c < len(row) and row[c] is not None else ""
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        merged.append(" / ".join(parts))
    return start, end, merged


def render_table_with_header(rows: list) -> list[str]:
    """表头智能识别 + Markdown 渲染（CSV / Excel 统一出口）。

    - 识别到表头：表头行 + 对齐分隔行 + 数据行；表头之前的说明行
      （项目名称、编制日期等低结构化信息）保留为正文行，不静默丢弃。
    - 未识别到表头：生成「列1..列N」通用表头，全部行按数据输出。
    """
    if not rows:
        return []
    header_start, header_end, header = find_table_header(rows)
    lines = ["【表格】"]
    if header_start > 0:
        for r in rows[:header_start]:
            vals = [str(v).strip() for v in r
                    if v is not None and str(v).strip()]
            if vals:
                lines.append("；".join(dict.fromkeys(vals)))
    if header_start < 0:
        cols = max(len(r) for r in rows)
        header = [f"列{i + 1}" for i in range(cols)]
        data_rows = rows
    else:
        data_rows = rows[header_end + 1:]
    lines.append(_md_cells(header))
    lines.append(_md_separator(header))
    lines.extend(_md_cells(r) for r in data_rows)
    return lines


def _parse_pdf(content: bytes, diag: dict | None = None,
               fname: str = "document.pdf") -> str:
    """PDF 解析（增强版）：
    1. 主通道 PyMuPDF（比 pdfplumber 快数倍），逐页容错（单页损坏不中断整体）
    2. ✅ 双引擎表格（2026-09-17）：引擎 A = get_text 原生文本流；
       引擎 B = find_tables 几何表格重建。合并前按「行覆盖指纹」剔除
       被表格覆盖的正文行，避免同一张表在 Markdown 里出现两次
    3. ✅ 文本归一化（2026-09-17）：换页符、中文间空格、重复绘制片段逐行压缩
    4. ✅ 文字层缺失判定（2026-09-17）：去空白后无连续 10 个可读字符即视同
       扫描件（旧实现只看平均每页字符数，页眉页码堆叠的空壳 PDF 会漏判）
    5. 回退通道 pdfplumber → pypdf；扫描件 OCR 兜底；✅ MinerU 云端兜底
    6. 页数超限时通过 diag 回报截断（旧实现只截断不告知）
    """
    text = ""
    n_pages = 0
    table_blocks: list[str] = []   # 引擎 B 产出的表格 Markdown（逐页收集）
    # MuPDF 连「打开」都失败 → 该 PDF 结构异常，不能再交给无闸门的回退通道
    fitz_open_failed = False
    # ✅ B-3 修复：记录「已加密且空密码认证失败」——这类 PDF 主通道提取必为空，
    #    若最终仍无有效文本，应在返回处明确告知根因（文件已加密），而非泛化
    #    的「未解析到有效文本」，让用户误以为是解析引擎问题。
    encrypted_unauth = False

    fitz = None
    try:
        import fitz  # PyMuPDF
    except ImportError:
        try:
            import pymupdf as fitz
        except ImportError:
            fitz = None

    if fitz is not None:
        try:
            with fitz.open(stream=content, filetype="pdf") as doc:
                if doc.is_encrypted:
                    # 尝试空密码（常见于仅限制权限的 PDF）
                    if not doc.authenticate(""):
                        encrypted_unauth = True
                        logger.warning("PDF 已加密且无密码，文本提取可能为空")
                        _note_info(diag, "PDF 已加密且无密码，文本提取可能不完整（建议解除限制后重新上传）")
                n_pages = doc.page_count
                page_items: list[tuple[int, str]] = []
                for page in doc.pages(0, MAX_PDF_PAGES):
                    try:
                        page_text = normalize_pdf_text(
                            page.get_text("text") or "")
                    except Exception as pe:
                        logger.warning("PDF 第 %d 页提取失败，跳过: %s",
                                       page.number, pe)
                        page_text = ""
                    # ✅ 引擎 B：几何表格重建 + 正文去重（单页失败安全跳过）
                    page_tables = _extract_pdf_page_tables(page)
                    if page_tables:
                        page_text = remove_lines_covered_by_tables(
                            page_text, page_tables)
                        table_blocks.extend(page_tables)
                    page_items.append((page.number + 1, page_text))
                text = _add_page_markers(page_items)
        except Exception as e:
            fitz_open_failed = True
            # ✅ A-9（2026-10-01）：这是 PDF **主通道整体失败**的唯一日志点，
            #    旧实现无堆栈，无法区分「文件损坏 / 加密 / 缺字体 / 引擎版本问题」，
            #    而这些成因的处理方式完全不同。
            logger.warning("PyMuPDF 解析失败: %s", e, exc_info=True)
            text = ""

    if not text.strip():
        if fitz_open_failed:
            # ✅ 安全：回退通道（pdfplumber / pypdf）**没有任何体积或压缩比闸门**，
            #    而 MuPDF 恰恰是最先、也是唯一能拦住异常压缩 PDF 的通道。
            #    实测（安全审计）：20KB 的畸形 PDF，MuPDF 0.0s 拒绝，
            #    转交 pdfplumber 后耗时 39.7s、零产出（2000x 放大）。
            #    历史实现在这里直接转回退通道，等于把已被闸门拦下的文件送进
            #    无防护解析器。注意：仅在「MuPDF 打不开」时短路；MuPDF 能打开
            #    但提取不到文字（扫描件等）仍照常回退，不影响既有兼容性。
            raise ParseError(
                "PDF 无法解析：文件结构异常或包含异常压缩数据。"
                "请用 PDF 阅读器「另存为」标准 PDF 后重新上传")
        text, n_pages = _parse_pdf_fallback(content)

    # ✅ 页数截断回报：只提取了前 MAX_PDF_PAGES 页，超出部分内容不会进入事实库
    if n_pages > MAX_PDF_PAGES:
        _note_truncation(
            diag,
            f"PDF 共 {n_pages} 页，仅解析前 {MAX_PDF_PAGES} 页，"
            f"其余 {n_pages - MAX_PDF_PAGES} 页内容未纳入（建议拆分文件后重新上传）")

    # 扫描件检测（双信号）：平均每页文本 < 阈值，或文字层缺失判定命中
    # → 疑似扫描件，尝试 OCR 兜底
    from app.config import settings
    threshold = getattr(settings, "ocr_min_chars_per_page", 30)
    avg_chars = len(text.strip()) / max(n_pages, 1)
    text_missing = not has_informative_text(text)
    if avg_chars < threshold or text_missing:
        logger.info(
            "PDF 疑似扫描件（平均每页 %.0f 字符 < %d%s），尝试 OCR 兜底",
            avg_chars, threshold,
            "，且无可选文字层" if text_missing else "")
        try:
            ocr_text = _pdf_ocr_fallback(content, diag=diag)
        except ParseError as e:
            # 无可用 OCR 引擎：正文全空 → 明确报错（含启用方法）；
            # 正文非空 → 保留已提取文字并告警，不因 OCR 缺失而整体失败。
            if not text.strip():
                raise
            logger.warning("PDF OCR 兜底不可用（保留已提取文字）: %s", e)
            ocr_text = ""
        if ocr_text.strip():
            text = f"{text}\n{ocr_text}" if text.strip() else ocr_text
            # ✅ 增强：OCR 兜底是"内容获得方式"的重要信息（OCR 识别表格/编号
            #    精度低于原生文本提取），随 diag 返回并持久化，供用户判断
            #    提取结果可信度。
            _note_info(diag, f"扫描件已通过 OCR 兜底识别（OCR 文本 {len(ocr_text)} 字），"
                             "表格与编号精度可能低于原生文本")

    # ✅ MinerU 云端兜底（2026-09-17）：文字层缺失且 OCR 兜底未产出有效文本时，
    #    若配置了 MINERU_PROVIDER（agent / accurate），自动走云端解析。
    #    未配置时保持纯本地行为（与旧版一致），失败只告警不中断。
    if not has_informative_text(text) and settings.mineru_enabled \
            and (settings.mineru_provider or "").strip():
        try:
            from app.services.mineru_client import parse_with_mineru
            md = parse_with_mineru(content, fname)
        except Exception as e:
            logger.warning("MinerU 云端解析兜底失败: %s", e)
            _note_info(diag, f"MinerU 云端解析兜底失败：{str(e)[:120]}")
        else:
            if has_informative_text(md):
                text = md if not text.strip() else f"{text}\n{md}"
                _note_info(
                    diag,
                    f"已通过 MinerU 云端解析兜底（{len(md)} 字，"
                    f"provider={settings.mineru_provider}）")

    if table_blocks:
        n_tables = len(table_blocks)
        logger.info("PDF 双引擎表格：共识别 %d 个表格", n_tables)

    # ✅ B-3 修复：已加密且空密码认证失败的 PDF，主/回退通道与 OCR/MinerU 均无法
    #    取出有效文字，应在末尾明确报出根因（文件已加密），避免上层返回泛化的
    #    「未解析到有效文本」误导用户以为是解析引擎问题。
    if not has_informative_text(text) and encrypted_unauth:
        raise ParseError(
            "PDF 已加密且无法通过空密码解密，无法提取文本。"
            "请先解除密码/权限限制后再重新上传")
    return text


def _parse_pdf_fallback(content: bytes) -> tuple[str, int]:
    """PDF 回退通道：pdfplumber → pypdf，逐页容错。返回 (text, 页数)"""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        tmp.write(content)
        tmp.close()
        try:
            import pdfplumber
            with pdfplumber.open(tmp.name) as pdf:
                pages_text: list[tuple[int, str]] = []
                for p in pdf.pages[:MAX_PDF_PAGES]:
                    try:
                        pages_text.append((p.page_number, p.extract_text() or ""))
                    except Exception as pe:
                        logger.warning("pdfplumber 第 %d 页失败: %s",
                                       p.page_number, pe)
                        pages_text.append((p.page_number, ""))
                return _add_page_markers(pages_text), len(pdf.pages)
        except ImportError:
            logger.warning("pdfplumber 未安装，回退到 pypdf")
        except Exception as e:
            logger.warning("pdfplumber 解析失败，回退到 pypdf: %s", e)
        try:
            from pypdf import PdfReader
            reader = PdfReader(tmp.name)
            pages_text2: list[tuple[int, str]] = []
            for idx, p in enumerate(reader.pages[:MAX_PDF_PAGES]):
                try:
                    pages_text2.append((idx + 1, p.extract_text() or ""))
                except Exception as pe:
                    logger.warning("pypdf 页提取失败: %s", pe)
                    pages_text2.append((idx + 1, ""))
            return _add_page_markers(pages_text2), len(reader.pages)
        except Exception as e:
            logger.warning("pypdf 也解析失败: %s", e)
            return "", 0
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def _pdf_ocr_fallback(content: bytes, max_pages: int | None = None,
                      diag: dict | None = None) -> str:
    """扫描件 PDF 的 OCR 兜底：PyMuPDF 渲染页面 → ocr 模块（多引擎识别）。

    页数与 DPI 由配置控制（OCR_PDF_MAX_PAGES / OCR_PDF_DPI）。
    无可用 OCR 引擎时抛 ParseError（消息包含启用方法），由调用方决定是否致命。

    ✅ BUG 修复（2026-09-18）：OCR 页数上限（默认 20）小于文本通道的 MAX_PDF_PAGES
    （50），旧实现从第 21 页起内容既不入正文、也不产生任何告警（而截断告警只覆盖
    「页数 > 50」）。现按模块既有的「截断必须留痕」契约记录告警。
    """
    from app.config import settings
    from app.services.ocr import ocr_bytes_sync, OcrUnavailableError

    max_pages = max_pages or getattr(settings, "ocr_pdf_max_pages", 20)
    dpi = getattr(settings, "ocr_pdf_dpi", 200)
    try:
        import fitz
    except ImportError:
        try:
            import pymupdf as fitz
        except ImportError:
            raise ParseError("扫描件 PDF OCR 需要 PyMuPDF，请执行 pip install pymupdf")

    ocr_parts: list[tuple[int, str]] = []
    skipped_pages = 0
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            if doc.page_count > max_pages:
                skipped_pages = doc.page_count - max_pages
                _note_truncation(
                    diag,
                    f"扫描件 OCR 仅覆盖前 {max_pages} 页（共 {doc.page_count} 页），"
                    f"其余 {skipped_pages} 页内容未识别"
                    "（可调大 OCR_PDF_MAX_PAGES 或拆分文件后重新上传）")
            for page in doc.pages(0, max_pages):
                try:
                    png = page.get_pixmap(dpi=dpi).tobytes("png")
                    res = ocr_bytes_sync(png)
                except OcrUnavailableError as oe:
                    raise ParseError(str(oe)) from oe
                except Exception as pe:
                    logger.warning("PDF OCR 第 %d 页失败: %s", page.number, pe)
                    continue
                if res.text:
                    ocr_parts.append((page.number + 1, res.text))
    except ParseError:
        raise
    except Exception as e:
        logger.warning("PDF OCR 整体失败: %s", e)
    if skipped_pages and not ocr_parts:
        # 全部超限页都未产出内容：上面已记录截断告警，这里只补一条日志便于排查
        logger.warning("PDF OCR 未产出任何文本（页数上限 %d，共跳过 %d 页）",
                       max_pages, skipped_pages)
    return _add_page_markers(ocr_parts)


def _md_cells(values) -> str:
    """把一行单元格转成 Markdown 表格行（``| a | b | c |``）。

    ✅ 增强（信息保存/提取质量）：表格是全局事实最高价值的结构化来源，
    下游 split_into_chunks 的「表格识别 → 表格保护 → 机械统计等高权重区分类」
    全部依赖 Markdown 竖线分隔语法。这里对单元格做最小清洗：
    - None → 空串；
    - 换行/回车 → 空格（避免破坏「一行一记录」结构）；
    - 竖线 ``|`` → 全角 ``／``（避免单元格内容被误判为列分隔符，撕裂表格）。
    """
    parts: list[str] = []
    for v in values:
        if v is None:
            parts.append("")
            continue
        s = str(v).replace("\r", " ").replace("\n", " ").replace("|", "／")
        parts.append(s.strip())
    return "| " + " | ".join(parts) + " |"


def _md_separator(values_or_cols) -> str:
    """生成 Markdown 表格对齐分隔行（``| --- | --- | --- |``）。

    ✅ BUG 修复（表格渲染/语义完整性）：旧实现所有表格都只输出「表头 + 数据行」，
    缺少 GFM 要求的对齐分隔行，带来两个真实后果：
      1. 前端 [[MarkdownRenderer]] 用 marked(gfm) 渲染解析预览时，**无分隔行的
         `|` 行块不会被识别为表格**，用户看到的是满屏竖线原文而非表格；
      2. 下游 `_split_table_rows` 的表头判定里专门写了「若下一行为对齐分隔行，
         一并保留」的分支（说明接口本就按"可能带分隔行"设计），生成端漏产出
         使该分支成为死代码，表格列语义在切分时更易丢失。

    列数取该行单元格数；无法判定时至少输出一列，保证语法合法。
    """
    try:
        cols = len(values_or_cols)
    except TypeError:
        cols = 0
    return "| " + " | ".join(["---"] * max(cols, 1)) + " |"


def _parse_csv(content: bytes, diag: dict | None = None) -> str:
    """CSV 解析：输出 Markdown 表格（首行视为表头）。

    ✅ BUG 修复（结构性）：旧实现输出制表符（``\\t``）分隔的纯文本，
    而下游 [[facts_extractor.split_into_chunks]] 的表格识别、表格完整性保护、
    机械统计表（machinery_stat）等高权重结构化区分类【只认竖线分隔的
    Markdown 表格】。结果：CSV/XLSX 这类最典型的机械统计表被当作普通文本——
    表格保护失效、machinery_stat 高权重区不激活（P11 机械统计规则块不注入）、
    超大表被字符窗口硬切导致行/单元格被截半。统一输出 Markdown 表格后，
    整条结构化提取管线才能正确识别并保护这些表格。
    """
    text, _dec_w = _decode_text_ex(content)
    for _w in _dec_w:
        _note_info(diag, _w)
    # ✅ 资源配额：边读边截断，不再 `[r for r in reader]` 全量物化。
    #    旧实现先整表读进内存再截断 ——「截断」只影响输出、不影响内存峰值，
    #    一个 200MB 的 CSV 仍会完整展开成上百万元素的行列表。
    rows: list[list[str]] = []
    overflow = False
    try:
        for row in csv.reader(io.StringIO(text)):
            if len(rows) >= MAX_CSV_ROWS:
                overflow = True
                break
            rows.append(row)
    except csv.Error as e:
        # 旧实现让 _csv.Error 冒到路由的泛化 except，用户只看到「文件无法识别
        # 或已损坏」，真正原因（超长且未加引号的单元格）被吞掉。
        raise ParseError(
            f"CSV 内容异常（{e}）：常见原因是存在超长且未加引号的单元格，"
            "请检查该列是否包含分隔符或二进制内容") from e
    if not rows:
        return ""
    # ✅ 修复：超限截断必须留痕——机械统计表（最高价值区）常超上限，
    #    静默截断会让用户无从得知"部分机械配置丢失"。
    if overflow:
        _note_truncation(
            diag,
            f"CSV 行数超过上限 {MAX_CSV_ROWS} 行已截断"
            "（建议拆分文件或转换格式后重新上传）")
    # ✅ 增强：表头智能识别（2026-09-17）——首行不再无条件视为表头；
    #    前 20 行内按「不同值数」打分识别表头，支持多行表头纵向拼接，
    #    表头前的说明行保留为正文行（不静默丢弃），无表头时生成通用表头。
    lines = render_table_with_header(rows)
    return "\n".join(lines)


def _parse_excel(content: bytes, ftype: str, diag: dict | None = None) -> str:
    """Excel 解析：逐工作表输出 Markdown 表格（保留工作表名与表头）。

    ✅ BUG 修复（结构性）：同 [[_parse_csv]]，旧实现输出制表符纯文本，
    无法被下游表格保护/高权重区分类识别。现统一输出 Markdown 表格，
    并保留工作表名（多 sheet 时不会把不同表的数据糊在一起）。
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ftype}")
    try:
        tmp.write(content)
        tmp.close()
        if ftype == "xls":
            try:
                import xlrd
                book = xlrd.open_workbook(tmp.name)
                parts = []
                for sheet in book.sheets():
                    n_rows = min(sheet.nrows, MAX_EXCEL_ROWS)
                    rows = [sheet.row_values(rx) for rx in range(n_rows)]
                    if not rows:
                        continue
                    if sheet.nrows > MAX_EXCEL_ROWS:
                        _note_truncation(
                            diag,
                            f"Excel 工作表「{sheet.name}」共 {sheet.nrows} 行，"
                            f"超过上限 {MAX_EXCEL_ROWS} 行已截断"
                            "（建议拆分文件或转换格式后重新上传）")
                    parts.append(f"【工作表：{sheet.name}】")
                    # ✅ 增强：表头智能识别（与 CSV 同口径，2026-09-17）
                    parts.extend(render_table_with_header(rows))
                return "\n".join(parts)
            except ImportError:
                logger.warning("xlrd 未安装（pip install xlrd==1.2.0），无法解析 .xls")
                # ✅ 修复：抛异常而不是返回错误字符串——旧实现会把
                #    「【Excel .xls 解析失败：缺少 xlrd...】」当作正文返回并落库。
                raise ParseError(
                    "无法解析 .xls 文件：缺少 xlrd 库。请执行 "
                    "`pip install xlrd==1.2.0`，或将文件另存为 .xlsx 后重新上传")
        from openpyxl import load_workbook
        # ✅ 增强：data_only=True 取公式单元格的缓存计算值，避免正文里出现
        #    "=SUM(...)" 这类公式原文（机械统计表的合计列常为公式）。
        wb = load_workbook(tmp.name, read_only=True, data_only=True)
        # ✅ BUG 修复（Windows 致命）：read_only 模式下 openpyxl 会持续持有
        #    文件句柄，必须显式 close()。旧实现从不关闭 → 下方 finally 删除
        #    临时文件时抛 PermissionError（WinError 32），异常从 finally 冒出，
        #    导致【xlsx 解析整体失败】且临时文件泄漏。
        try:
            parts: list[str] = []
            for ws in wb.worksheets:
                rows = list(ws.iter_rows(max_row=MAX_EXCEL_ROWS,
                                         values_only=True))
                if not rows:
                    continue
                parts.append(f"【工作表：{ws.title}】")
                # ✅ 增强：表头智能识别（与 CSV 同口径，2026-09-17）
                parts.extend(render_table_with_header(rows))
                # ✅ 性能修复（2026-09-26，F3）：仅在读满上限（len(rows)==上限）
                #    时才访问 ws.max_row —— 该属性在缺 <dimension> 元数据的文件里
                #    会触发整表扫描。小表未读满上限则不触发扫描，避免无谓开销。
                if len(rows) >= MAX_EXCEL_ROWS and (ws.max_row or 0) > MAX_EXCEL_ROWS:
                    _note_truncation(
                        diag,
                        f"Excel 工作表「{ws.title}」共 {ws.max_row} 行，"
                        f"超过上限 {MAX_EXCEL_ROWS} 行已截断"
                        "（建议拆分文件或转换格式后重新上传）")
            return "\n".join(parts)
        finally:
            wb.close()
    finally:
        # ✅ 加固：临时文件清理失败（Windows 句柄占用/杀软扫描）不应掩盖
        #    解析结果或抛出 PermissionError。
        if os.path.exists(tmp.name):
            try:
                os.unlink(tmp.name)
            except OSError as e:
                logger.debug("临时文件清理失败（忽略）: %s", e)


def _split_tiff_frames(content: bytes) -> list[bytes]:
    """把多页 TIFF 逐帧转为 PNG 字节；单帧 / 缺 Pillow / 解码失败时返回空列表。

    ✅ BUG 修复（内容静默丢失）：旧实现把整个 TIFF 交给 OCR 通道，而
    `ocr._preprocess` 用 `PIL.Image.open(...)` 只读**第 1 帧** —— 多页 TIFF
    （扫描仪常见的图纸/多页清单形态）从第 2 页起的内容全部被静默丢弃，
    且不会产生任何告警，用户以为"文件已解析成功"。
    """
    try:
        from PIL import Image
    except ImportError:
        return []
    frames: list[bytes] = []
    try:
        with Image.open(io.BytesIO(content)) as img:
            n = getattr(img, "n_frames", 1)
            if n <= 1:
                return []
            for i in range(n):
                img.seek(i)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                frames.append(buf.getvalue())
    except Exception as e:  # pragma: no cover - 依赖 Pillow 行为
        logger.debug("多页 TIFF 拆帧失败（按单帧识别）: %s", e)
        return []
    return frames


def _has_usable_image_text(text: str) -> bool:
    """图片 OCR/云端结果是否可用（比 PDF 的「信息性文本」判定更宽松）。

    图片本身常只有标题式的少量文字（"施工平面布置图"），PDF 那条
    「连续 ≥10 个可读字符」的判定会把有效结果判成空。这里只要求去空白后
    至少有 5 个字符，避免把云端成功的结果丢掉。
    """
    return len(re.sub(r"\s+", "", str(text or ""))) >= 5


def _mineru_image_fallback(content: bytes, ftype: str,
                           diag: dict | None = None) -> str:
    """图片的 MinerU 云端解析兜底（对齐 OpenBidKit：MinerU 支持 png/jpg 等）。

    ✅ 新增（2026-09-22）：旧实现只给 **PDF** 做 MinerU 兜底 —— 扫描件以图片
    形式上传（.png/.jpg/.tiff，图纸、照片型招标文件附件）时，本地没有 OCR
    引擎就直接失败，即便用户已经配好了 MinerU 也不用上。现与 PDF 口径一致：
    本地 OCR 不可用 / 无有效结果时，配置了 MINERU_PROVIDER 才走云端，
    未配置时行为与旧版逐字一致（保持向后兼容）。
    """
    from app.config import settings
    if not (settings.mineru_enabled and (settings.mineru_provider or "").strip()):
        return ""
    # ✅ 增强（2026-09-23，审计报告 §4-4）：单图过大时跳过云端兜底，避免把超大图
    #    塞给云端触发 413/超时。默认阈值 100MB（参考实现 agent 模式限制）；设为 0
    #    表示不限制。本地又无 OCR → 下方返回 "" 后由调用方抛 ParseError 提示用户。
    cap = int(getattr(settings, "mineru_image_max_bytes", 0) or 0)
    if cap > 0 and len(content) > cap:
        _note_info(
            diag,
            f"扫描件图片 {len(content) // (1024 * 1024)}MB 超过云端单图上限"
            f"（{cap // (1024 * 1024)}MB），已跳过 MinerU 云端兜底")
        return ""
    try:
        from app.services.mineru_client import parse_with_mineru
        md = parse_with_mineru(content, f"scanned-image.{ftype or 'png'}")
    except Exception as e:
        logger.warning("图片 MinerU 云端解析兜底失败: %s", e)
        _note_info(diag, f"MinerU 云端解析兜底失败：{str(e)[:120]}")
        return ""
    if _has_usable_image_text(md):
        _note_info(
            diag,
            f"扫描件图片已通过 MinerU 云端解析兜底（{len(md)} 字，"
            f"provider={settings.mineru_provider}）")
        return md
    _note_info(diag, "MinerU 云端解析未返回有效内容")
    return ""


def _parse_image_ocr(content: bytes, ftype: str,
                     diag: dict | None = None) -> str:
    """图片 OCR（多层引擎：tesseract / RapidOCR / 视觉大模型）。

    ✅ 修复：旧实现失败时返回「OCR 解析失败：...」字符串，会被上层当作正文
    落库并进入事实提取。现统一抛 ParseError，由路由层转成可读的 400 提示。
    ✅ 增强：多页 TIFF 逐帧识别（见 `_split_tiff_frames`），不再只取首帧。
    ✅ 修复（2026-09-18）：帧级失败/引擎不可用原先只写日志（PDF 路径有告警、
    图片路径没有），现经 `_note_info` 随 diag 回传并持久化，口径与 PDF 一致。
    ✅ 增强（2026-09-22）：本地 OCR 不可用 / 无有效结果时，配置了
    MINERU_PROVIDER 则走 MinerU 云端兜底（与 PDF 口径一致）。
    """
    from app.services.ocr import ocr_bytes_sync, OcrUnavailableError

    if ftype == "tiff":
        frames = _split_tiff_frames(content)
        if len(frames) > 1:
            parts: list[str] = []
            for idx, frame in enumerate(frames):
                try:
                    res = ocr_bytes_sync(frame)
                except OcrUnavailableError as e:
                    # 首帧就无引擎 → 致命；已有内容 → 保留并告警
                    if not parts:
                        cloud = _mineru_image_fallback(content, ftype, diag)
                        if cloud:
                            return cloud
                        raise ParseError(str(e)) from e
                    logger.warning("多页 TIFF 第 %d 帧OCR 不可用（保留已识别内容）: %s",
                                   idx + 1, e)
                    _note_info(diag, f"多页 TIFF 第 {idx + 1} 帧起 OCR 引擎不可用，"
                                     "已保留前面帧的识别结果")
                    break
                except Exception as e:
                    logger.warning("多页 TIFF 第 %d 帧识别失败（跳过）: %s", idx + 1, e)
                    _note_info(diag, f"多页 TIFF 第 {idx + 1} 帧识别失败已跳过：{str(e)[:80]}")
                    continue
                if res.text:
                    parts.append(res.text)
            if len(parts) < len(frames):
                _note_info(diag, f"多页 TIFF 共 {len(frames)} 帧，"
                                 f"仅 {len(parts)} 帧识别出文本")
            if not parts:
                cloud = _mineru_image_fallback(content, ftype, diag)
                if cloud:
                    return cloud
            return "\n".join(parts)

    try:
        res = ocr_bytes_sync(content)
    except OcrUnavailableError as e:
        # ✅ 无本地引擎：先试云端兜底，仍不可用才按原错误抛出（旧行为不变）
        cloud = _mineru_image_fallback(content, ftype, diag)
        if cloud:
            return cloud
        raise ParseError(str(e)) from e
    except ParseError:
        raise
    except Exception as e:
        # ✅ 本地引擎异常（依赖缺失 / 图像损坏）同样尝试云端兜底
        cloud = _mineru_image_fallback(content, ftype, diag)
        if cloud:
            return cloud
        raise ParseError(f"图片 OCR 失败：{e}") from e
    if not _has_usable_image_text(res.text):
        # 本地引擎跑通但几乎没识别出文字（低质量扫描件）→ 云端再试一次
        cloud = _mineru_image_fallback(content, ftype, diag)
        if cloud:
            return cloud
    return res.text


# ---------------------------------------------------------------------------
# 目录识别（降级解析）正则：模块级预编译，避免每次调用重复编译
# （simple_parse_outline 上传识别时调用，编号 pattern 较多）
# ---------------------------------------------------------------------------
_OUTLINE_CN_NUMS = "一二三四五六七八九十"
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")
# 页码 / 分隔符行（"第 12 页"、"第12页/24"、"- 12 -"、"12 / 24"），需跳过
_PAGE_MARKER_RE = re.compile(
    r"^(第\s*\d+\s*页(\s*/\s*\d+)?|[-—]\s*\d+\s*[-—]|\d+\s*/\s*\d+)$")
# 目录编号 pattern 列表： (固定层级 or None=按上下文推断, pattern)
# ✅ OCR 鲁棒性增强：真实 OCR / 扫描件常在编号关键位置掺入空格
#   （"第 1 章"、"1 . 1"、"（ 一 ）"、"一 、 工程概况"）。
#   下列 pattern 在编号与分隔符之间允许 \s*，显著提升上传目录识别准确率
#   （PRD 9.2：OCR 噪声原需大量人工校正，现自动容错）。
#   注意：点分编号的层级按"去空格后"的点号数计算，避免 "1 . 1" 被误判为三级。
_OUTLINE_PATTERNS = [
    # 中文数字章节编号（含 章/节/篇/部分，容忍 第 与 编号 之间的空格）
    (1, re.compile(rf"^第\s*([{_OUTLINE_CN_NUMS}]+)\s*(?:部分|章|节|篇)\s*(.*)")),
    # 阿拉伯数字章节编号（"第 1 章"/"第 2 节"/"第 3 部分"）
    (1, re.compile(r"^第\s*(\d+)\s*(?:部分|章|节|篇)\s*(.*)")),
    # 点分编号（容忍编号内空格：1 . 1 / 1.1.1）
    (None, re.compile(r"^(\d+(?:\s*\.\s*\d+)+)\s*[、.\s]\s*(.*)")),
    # 单数字编号（"1 总则" / "1. 总则" / "1、总则"）
    (1, re.compile(r"^(\d+)\s*[、.\s]\s*(.*)")),
    # 中文数字顿号（"一、工程概况"，容忍前后空格）
    (2, re.compile(rf"^([{_OUTLINE_CN_NUMS}]+)\s*[、]\s*(.*)")),
    # 中文数字括号（"（ 一 ）" / "（一）"，容忍空格）
    (None, re.compile(rf"^[（(]\s*([{_OUTLINE_CN_NUMS}]+)\s*[)）]\s*(.*)")),
    # 阿拉伯数字括号（"（ 3 ）" / "（3）"）
    (3, re.compile(r"^[（(]\s*(\d+)\s*[)）]\s*(.*)")),
    # 右括号编号（"3)"，兜底）
    (3, re.compile(r"^(\d+)\s*[)）]\s*(.*)")),
]


def simple_parse_outline(text: str) -> list:
    """降级解析：按编号规则从文本中提取目录结构。

    支持的编号格式：
    - Markdown 标题：# / ## / ### / #### / ##### / ######  → level 1-6
    - 第X章 / 第一章 / 第X节 / 第X部分  → level 1
    - X. / X.X / X.X.X / X.X.X.X        → level = 点分深度
    - 一、 / 二、                        → level 2
    - （一）                              → level 2 或 3（根据上下文推断）
    - (1) / 1）                           → level 3

    栈逻辑：stack[i] 保存 level i+1 的当前节点，新节点入栈时截断到父级。
    """
    lines = text.split("\n")
    outline: list[dict] = []
    stack: list[dict] = []
    _id_counter = [0]
    _used_ids: set[str] = set()

    def _next_id() -> str:
        while True:
            _id_counter[0] += 1
            nid = f"n{_id_counter[0]}"
            if nid not in _used_ids:
                _used_ids.add(nid)
                return nid

    def _unique_id(preferred: str) -> str:
        """优选编号作为 id；已被占用时回退到序号 id，保证目录内 id 全局唯一。

        ✅ BUG 修复（id 冲突）：旧实现把点分编号（"1.1"）直接当节点 id，
        而真实文档里同一编号完全可能重复出现（OCR 误识别、跨章重复编号、
        同一编号被用于并列子项）。id 一旦重复：
          - 前端 antd Tree 用 id 作 React key → 重复 key 导致节点错位/渲染异常；
          - 落库时 outline_json.id 也重复，正文生成按该 id 取「章节编号」会串号。
        这里只对冲突者回退为序号 id，未冲突的编号仍保留（避免改变既有语义）。
        """
        if preferred and preferred not in _used_ids:
            _used_ids.add(preferred)
            return preferred
        return _next_id()

    def _attach(node: dict, level: int) -> None:
        while len(stack) >= level:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            outline.append(node)
        stack.append(node)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # ✅ 增强：跳过页码 / 分隔符（如 "第 12 页"、"第12页/24"、"- 12 -"、"12 / 24"），
        #    否则会被误识别为标题章节
        if _PAGE_MARKER_RE.match(line):
            continue

        m_md = _MD_HEADING_RE.match(line)
        if m_md:
            level = len(m_md.group(1))
            title = m_md.group(2).strip()
            node = {
                "id": _next_id(),
                "title": title,
                "level": level,
                "confidence": 0.75,
                "children": [],
            }
            _attach(node, level)
            continue

        for fixed_level, pat in _OUTLINE_PATTERNS:
            m = pat.match(line)
            if not m:
                continue

            raw_id = m.group(1)
            if fixed_level is not None:
                level = fixed_level
            elif "." in raw_id:
                # ✅ OCR 鲁棒性：点分编号可能含空格（"1 . 1"），
                #    去空格后再按点号数定层级，避免误判为更深层。
                level = raw_id.replace(" ", "").count(".") + 1
            else:
                if stack and stack[-1]["level"] == 1:
                    level = 2
                elif stack and stack[-1]["level"] >= 2:
                    level = 3
                else:
                    level = 2

            title = m.group(2).strip() if m.group(2) else ""
            if not title:
                title = line

            # ✅ 增强：防止把正文行误判为编号标题。
            #    如 "2023 年最新规范" 会被 `^(\d+)\s*[、.\s]\s*(.*)` 匹配成
            #    「编号 2023 + 标题 年最新规范」。判定规则：编号为 4 位以上
            #    （年份样式）且标题以 年/月/日/度 开头时，视为正文行，不生成节点。
            #    规则足够窄，不会误伤 "1 年度计划"（编号仅 1 位）等正常标题。
            if len(raw_id) >= 4 and title[:1] in ("年", "月", "日", "度"):
                break

            # ✅ 修复（2026-09-19）：正文行防误判（第二道防线）——列举条目/正文句
            #    以"；。"等句读标点收尾（如 "3.1.12 身份证复印件、照片；"），
            #    是内容条目不是标题；生成节点会落库为 section，导出即
            #    "有标题无正文"的空章节。
            if re.search(r"[。；;，,]\s*$", title):
                break

            node = {
                # 点分编号含空格（"1 . 1"）时存去空格后的编号；其余用临时 id
                "id": _unique_id(raw_id.replace(" ", "")) if "." in raw_id else _next_id(),
                "title": title,
                "level": level,
                "confidence": 0.65 if "." in raw_id else 0.7,
                "children": [],
            }
            _attach(node, level)
            break

    return outline