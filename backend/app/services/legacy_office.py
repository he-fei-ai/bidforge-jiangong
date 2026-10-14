"""旧版 Word（.doc / .wps）转换模块 —— 移植自 OpenBidKit。

参考实现：`client/electron/services/doc2markdown/convert.mjs`
  - `withLegacyWordDocxFile()`          ：多后端依次尝试，全部失败才报错
  - `buildLegacyWordConversionBackends()`：LibreOffice 优先；.wps 时 WPS COM 优先
  - `runLibreOfficeDocxConversion()`     ：`soffice --headless --convert-to docx`
  - `runWindowsComOfficeConvert()`       ：PowerShell 调 Word/WPS COM 另存为 docx
  - `documentParseErrors.cjs`            ：缺组件时给「可操作」提示（含下载地址）

本模块只做一件事：把旧版 Word 二进制转成 **DOCX 字节**，随后交给
`file_parser._parse_docx` 走既有 OOXML 解析通道 —— 这样旧版 Word 与 .docx
共用同一套标题/表格结构化逻辑，不产生第二套解析实现。

设计约束：
- 纯同步实现（调用方 `file_parser.parse_file_content_ex` 本身跑在线程池里，
  避免在事件循环里做阻塞子进程 IO）。
- 失败一律抛 `ParseError`（用户可读消息），绝不返回错误文本当作正文。
- 多后端「依次尝试、逐个记录失败原因」，与参考实现一致：LibreOffice 装了但
  转换失败时，仍会继续尝试 Windows 上的 Word / WPS COM。
"""
from __future__ import annotations

import logging
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from app.config import settings
from app.services.file_parser import OLE_COMPOUND_HEADERS, ParseError

logger = logging.getLogger(__name__)

#: 缺转换组件时的提示（对齐 OpenBidKit documentParseErrors.cjs 的文案口径：
#: 明确「装什么 / 或者怎么绕过」，而不是只报一句「解析失败」）
LIBREOFFICE_DOWNLOAD_URL = "https://zh-cn.libreoffice.org/download/libreoffice/"
LEGACY_OFFICE_REQUIRED_MESSAGE = (
    ".doc和.wps识别，需要安装 LibreOffice、WPS Office 或 Microsoft Word "
    "任意一种本地转换组件（LibreOffice 下载："
    f"{LIBREOFFICE_DOWNLOAD_URL}），或者手动将文件转换为.docx格式再上传"
)

#: 旧版 Word 扩展名（OLE 复合文档，非 ZIP 容器）
LEGACY_WORD_EXTENSIONS = {"doc", "wps"}

#: 常见安装位置（避免用户装了但不在 PATH 里）
_SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/opt/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)

#: COM ProgID（对齐参考实现：WPS 的 ProgID 大小写在不同版本间不一致，全部探一遍）
_WPS_PROG_IDS = ("Kwps.Application", "KWPS.Application", "wps.Application")
_WORD_PROG_IDS = ("Word.Application",)

#: PowerShell COM 转换脚本（逐行拼接，避免缩进差异；与参考实现等价：
#: 优先 SaveAs2(16=wdFormatDocumentDefault)，失败回退 SaveAs(12=wdFormatXMLDocument)）
_POWERSHELL_OFFICE_CONVERT_SCRIPT = "\n".join([
    "param(",
    "  [Parameter(Mandatory=$true)][string]$ProgId,",
    "  [Parameter(Mandatory=$true)][string]$InputPath,",
    "  [Parameter(Mandatory=$true)][string]$OutputPath",
    ")",
    '$ErrorActionPreference = "Stop"',
    "function Release-ComObject([object]$ComObject) {",
    "  if ($null -ne $ComObject) {",
    "    try { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($ComObject) } catch {}",
    "  }",
    "}",
    "function Save-AsDocx([object]$Document, [string]$TargetPath) {",
    "  $lastError = $null",
    "  foreach ($format in @(16, 12)) {",
    "    try {",
    "      if (Test-Path -LiteralPath $TargetPath) { Remove-Item -LiteralPath $TargetPath -Force }",
    "      try { $Document.SaveAs2($TargetPath, $format) } catch { $Document.SaveAs($TargetPath, $format) }",
    "      if (Test-Path -LiteralPath $TargetPath) { return }",
    "    } catch {",
    "      $lastError = $_",
    "    }",
    "  }",
    "  if ($null -ne $lastError) { throw $lastError }",
    '  throw "save failed"',
    "}",
    "$app = $null",
    "$doc = $null",
    "try {",
    "  $app = New-Object -ComObject $ProgId",
    "  try { $app.Visible = $false } catch {}",
    "  try { $app.DisplayAlerts = 0 } catch {}",
    "  try { $app.AutomationSecurity = 3 } catch {}",
    "  $doc = $app.Documents.Open($InputPath, $false, $true, $false)",
    "  Save-AsDocx $doc $OutputPath",
    "  if (!(Test-Path -LiteralPath $OutputPath)) { throw \"output missing\" }",
    "} finally {",
    "  if ($null -ne $doc) { try { $doc.Close($false) } catch {} }",
    "  if ($null -ne $app) { try { $app.Quit() } catch {} }",
    "  Release-ComObject $doc",
    "  Release-ComObject $app",
    "  [GC]::Collect()",
    "  [GC]::WaitForPendingFinalizers()",
    "}",
    "",
])


def legacy_word_enabled() -> bool:
    """旧版 Word 解析是否启用（配置关闭时按「不支持的格式」处理）。"""
    return bool(getattr(settings, "legacy_office_enabled", True))


# ---------- G4（2026-10-12）：OLE 复合文档头结构预检 ----------
# 损坏 / 被截断的 .doc 若直接送 LibreOffice，会白等一次「单后端转换超时」
# （默认 120s/后端、最多两后端 ≈ 4 分钟）并占住一个线程池 worker，最终仍只能
# 得到「转换失败」。这里用纯字节判定在毫秒级早退。
#
# ⚠️ 判定只覆盖「合法 CFB 文件的**必要条件**」（头字段值域 + 扇区算术 +
# 保留索引），绝不做内容语义解析 —— 因此不可能把正常的老版 .doc 误判成
# 损坏；非 CFB 容器（WPS 新格式 / ZIP）也明确不在本函数职责内，保持旧行为。
#: CFB 固定头部尺寸（MS-CFB 2.2）
_CFB_HEADER_SIZE = 512
#: 保留的扇区索引值下界：0xFFFFFFFB=XSECT / 0xFFFFFFFC=FREESECT /
#: 0xFFFFFFFE=ENDOFCHAIN / 0xFFFFFFFF=FATSECT（MS-CFB 2.1），均不是真实扇区
_CFB_RESERVED_SECTORS = 0xFFFFFFFB


def validate_ole_compound_header(content: bytes) -> str | None:
    """校验字节流是否为结构合法的 CFB（OLE 复合文档）。

    Returns:
        ``None`` = 结构合法，或**不在本函数职责内**（文件太短、不是 CFB
        容器）—— 交由后续转换后端自行报错，保持旧行为；
        否则返回可直接展示给用户的失败原因。

    判据（全部是合法 CFB 的必要条件，逐条可证伪）：
    ① 至少一个完整头部；② 小版本 = 0x3E、大版本 ∈ {3,4}、字节序 = 0xFFFE、
    扇区位移 ∈ [7,12]、迷你扇区位移 = 6；③ **仅 v4** 必须有 FAT / 目录扇区
    （v3 允许 FAT 内联在 DIFAT 数组中且规范要求目录扇区数为 0）；④ 头部声明的
    扇区总数 ≤ 文件实际扇区数（**截断**文件的判据）；⑤ 目录扇区索引必须落在
    文件内且不是保留值。
    """
    if len(content) < _CFB_HEADER_SIZE:
        return None  # 连一个头都不够，转换后端会快速失败，无需本函数判定
    if content[:8] not in OLE_COMPOUND_HEADERS:
        return None  # 非 CFB 容器：保持旧行为，交后端判定
    # 字段偏移严格对照 MS-CFB 2.2（v3 / v4 同布局）：
    #   0x18 小版本 · 0x1A 大版本 · 0x1C 字节序 · 0x1E 扇区位移 · 0x20 迷你位移
    #   0x28 目录扇区数 · 0x2C FAT 扇区数 · 0x30 首个目录扇区
    #   0x40 迷你 FAT 扇区数 · 0x48 DIFAT 扇区数
    # ⚠️ 0x34~0x3C 是「事务签名 / 迷你扇区上限 / 首个迷你 FAT 扇区」，不是扇区
    #    计数：早期版本从这里连读 4 个计数，把真实的 Excel 97 .xls 误判成「截断」。
    try:
        minor, major, byte_order, sec_shift, mini_shift = struct.unpack_from(
            "<HHHHH", content, 0x18)
        n_dir, n_fat, first_dir = struct.unpack_from("<III", content, 0x28)
        n_mini = struct.unpack_from("<I", content, 0x40)[0]
        n_difat = struct.unpack_from("<I", content, 0x48)[0]
    except Exception:  # pragma: no cover - 偏移量已受头部尺寸约束，纯防御
        return "OLE 头字段无法解析"
    if minor != 0x003E:
        return "OLE 头小版本号非法"
    if major not in (3, 4):
        return f"OLE 头大版本号非法（{major}，规范要求 3 或 4）"
    if byte_order != 0xFFFE:
        return "OLE 头字节序非法"
    if not 7 <= sec_shift <= 12:
        return "OLE 头扇区大小非法"
    if mini_shift != 6:
        return "OLE 头迷你扇区大小非法"
    # ⚠️ 只对 v4 要求「必须有 FAT / 目录扇区」：CFB v3 允许 FAT 内联在 DIFAT
    #    数组里（num_fat=0），且规范要求 v3 的目录扇区数写 0 —— 真实的
    #    Excel 97-2003 .xls 就是这种形态，据此判定会误拒正常文件。
    if major == 4 and n_fat < 1:
        return "OLE 头声明 FAT 扇区数为 0"
    if major == 4 and n_dir < 1:
        return "OLE 头声明目录扇区数为 0"
    sector = 1 << sec_shift
    total = (len(content) - _CFB_HEADER_SIZE) // sector
    declared = n_fat + n_dir + n_mini + n_difat
    if total < declared:
        return (f"OLE 头声明的扇区数（{declared}）超出文件实际容量"
                f"（{total} 个扇区），文件已被截断")
    # 只校验目录扇区索引 —— 它是整份文件的入口。首个 FAT 扇区索引在 v3 里
    # 可能合法地指向内联位置，多校验一条只增加误拒风险、收益极小。
    if first_dir >= _CFB_RESERVED_SECTORS or first_dir >= total:
        return "OLE 头目录扇区索引越界（文件已损坏）"
    return None


def legacy_ole_preflight_enabled() -> bool:
    """OLE 头结构预检是否启用（关闭时完全回到旧行为：直接进转换后端）。"""
    return bool(getattr(settings, "legacy_ole_preflight", True))


def ole_preflight_reason(content: bytes) -> str | None:
    """G4 预检唯一出口：合法 / 非 CFB → ``None``；损坏 → 可读原因。

    ⚠️ 预检自身的任何异常都 **fail-open** 为 ``None``：预检只是加速用的辅助
    判定，绝不能因为预检自己出错而阻断原本可以成功的老版 Word 解析。
    """
    if not legacy_ole_preflight_enabled():
        return None
    try:
        return validate_ole_compound_header(content)
    except Exception as _e:
        logger.warning("OLE 头预检自身异常，已跳过预检（fail-open）：%s", _e)
        return None


def find_libreoffice_command() -> str:
    """探测 soffice 可执行文件：显式配置 > PATH > 常见安装路径。

    ✅ 对齐参考实现 `findLibreOfficeCommand()`：只做「找得到 / 找不到」的判定，
    找不到返回空串（由调用方决定报错文案），不抛异常。
    """
    configured = (getattr(settings, "legacy_office_path", "") or "").strip()
    if configured:
        if os.path.isfile(configured):
            return configured
        logger.warning("LEGACY_OFFICE_PATH 指向的文件不存在：%s", configured)
    for name in ("soffice", "soffice.exe", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _SOFFICE_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _find_powershell() -> str:
    """Windows 下探测 PowerShell（COM 后端依赖它）。"""
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _is_com_progid_registered(prog_id: str) -> bool:
    """查注册表判断 COM ProgID 是否可用（Word/WPS 是否装了）。"""
    reg = shutil.which("reg.exe") or shutil.which("reg")
    if not reg:
        return False
    try:
        proc = subprocess.run(
            [reg, "query", f"HKCR\\{prog_id}\\CLSID"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return False
    return proc.returncode == 0


def detect_office_backends(ext: str = "doc") -> list[dict]:
    """按优先级返回可用的转换后端列表（与参考实现同顺序）。

    - `.wps`：WPS COM → LibreOffice → Word COM（WPS 生成的文件优先让 WPS 转）
    - 其它  ：LibreOffice → Word COM → WPS COM
    """
    soffice = find_libreoffice_command()
    libreoffice = ([{"type": "libreoffice", "label": "LibreOffice",
                     "command": soffice}] if soffice else [])

    word, wps = [], []
    if os.name == "nt" and getattr(settings, "legacy_office_com_enabled", True):
        powershell = _find_powershell()
        if powershell:
            for prog_id in _WORD_PROG_IDS:
                if _is_com_progid_registered(prog_id):
                    word.append({"type": "word", "label": f"Microsoft Word ({prog_id})",
                                 "command": powershell, "prog_id": prog_id})
                    break
            for prog_id in _WPS_PROG_IDS:
                if _is_com_progid_registered(prog_id):
                    wps.append({"type": "wps", "label": f"WPS Office ({prog_id})",
                                "command": powershell, "prog_id": prog_id})
                    break

    if (ext or "").lower() == "wps":
        return [*wps, *libreoffice, *word]
    return [*libreoffice, *word, *wps]


def _run_process(cmd: list[str], timeout_s: int) -> subprocess.CompletedProcess:
    """执行子进程（不弹窗、捕获输出、超时抛错）。"""
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout_s,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _run_libreoffice_convert(soffice: str, src: Path, out_dir: Path,
                             timeout_s: int) -> Path:
    """`soffice --headless --convert-to docx --outdir <dir> <src>` 并返回产物路径。"""
    _run_process([
        soffice, "--headless", "--norestore", "--nolockcheck",
        "--convert-to", "docx", "--outdir", str(out_dir), str(src),
    ], timeout_s)
    produced = sorted(out_dir.glob("*.docx"))
    if not produced:
        raise ParseError("LibreOffice 未生成 DOCX 文件")
    # 多产物时优先取与源文件同名的
    same_stem = [p for p in produced if p.stem == src.stem]
    return (same_stem or produced)[0]


def _run_com_convert(powershell: str, prog_id: str, src: Path, dst: Path,
                     timeout_s: int) -> None:
    """Windows COM 转换：PowerShell 调 Word/WPS 另存为 DOCX。"""
    script = src.parent / f"office-convert-{prog_id.split('.')[0]}.ps1"
    script.write_text(_POWERSHELL_OFFICE_CONVERT_SCRIPT, encoding="utf-8")
    try:
        proc = _run_process([
            powershell, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(script),
            prog_id, str(src), str(dst),
        ], timeout_s)
    except subprocess.TimeoutExpired as e:
        raise ParseError(f"{prog_id} 转换超时（{timeout_s}s）") from e
    if not dst.exists() or dst.stat().st_size == 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace")[:200].strip()
        raise ParseError(f"{prog_id} 未生成 DOCX 文件{('：' + detail) if detail else ''}")


def convert_legacy_word_to_docx(content: bytes, file_name: str) -> tuple[bytes, str]:
    """把 .doc / .wps 字节流转换为 DOCX 字节流。

    Returns:
        (docx_bytes, backend_label) —— backend_label 用于解析诊断（"已通过
        LibreOffice 转换" 这类提示需要落库给用户看）。

    Raises:
        ParseError: 未安装任何转换组件，或全部后端转换失败。
    """
    if not legacy_word_enabled():
        raise ParseError(
            "旧版 Word（.doc/.wps）解析已关闭（LEGACY_OFFICE_ENABLED=false）："
            "请改用 .docx 格式上传")
    if not content:
        raise ParseError("文件内容为空，无法转换")

    # ✅ G4（2026-10-12）：转换前先做廉价的 OLE 头结构校验。损坏 / 被截断的
    #    上传件在此毫秒级早退，不再白等「单后端转换超时」（默认 120s/后端）。
    #    放在 `detect_office_backends` 之前：连组件探测的开销都省掉。
    _ole_reason = ole_preflight_reason(content)
    if _ole_reason:
        logger.warning("旧版 Word 文件 OLE 结构异常，跳过转换后端：%s（%s）",
                       _ole_reason, file_name)
        raise ParseError(
            f"旧版 Word 文件已损坏（{_ole_reason}）："
            "请用 Word/WPS 重新打开并另存为标准 .docx 后重新上传")

    ext = (file_name.rsplit(".", 1)[-1].lower()
           if "." in (file_name or "") else "doc")
    if ext not in LEGACY_WORD_EXTENSIONS:
        ext = "doc"

    backends = detect_office_backends(ext)
    if not backends:
        # ✅ 对齐参考实现的「可操作提示」：明确告知装什么 / 或者怎么绕过
        raise ParseError(LEGACY_OFFICE_REQUIRED_MESSAGE)

    timeout_s = int(getattr(settings, "legacy_office_timeout", 120) or 120)
    stem = Path(file_name or "legacy").stem or "legacy"

    with tempfile.TemporaryDirectory(prefix="legacy-office-") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / f"{stem}.{ext}"
        src.write_bytes(content)

        attempts: list[str] = []
        for backend in backends:
            try:
                if backend["type"] == "libreoffice":
                    produced = _run_libreoffice_convert(
                        backend["command"], src, tmp_dir, timeout_s)
                    docx_bytes = produced.read_bytes()
                else:
                    dst = tmp_dir / f"{stem}-{backend['type']}.docx"
                    _run_com_convert(backend["command"], backend["prog_id"],
                                     src, dst, timeout_s)
                    docx_bytes = dst.read_bytes()
            except subprocess.TimeoutExpired:
                attempts.append(f"{backend['label']}：转换超时（{timeout_s}s）")
                continue
            except ParseError as e:
                attempts.append(f"{backend['label']}：{e}")
                continue
            except Exception as e:  # pragma: no cover - 环境相关（权限/杀软）
                attempts.append(f"{backend['label']}：{e}")
                continue
            if docx_bytes:
                logger.info("旧版 Word 已通过 %s 转换为 DOCX（%d 字节）",
                            backend["label"], len(docx_bytes))
                return docx_bytes, backend["label"]
            attempts.append(f"{backend['label']}：转换结果为空")

    detail = "；".join(attempts[:3])
    raise ParseError(
        f"旧版 Word 文件转换失败：{detail or '未知原因'}。"
        "请用 Word/WPS 另存为标准 .docx 后重新上传")
