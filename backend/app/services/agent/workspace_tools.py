"""P1 — 工作区路径防护与六个原生文件工具（read/write/edit/grep/find/ls）

移植来源（易标 Yibiao 2.30.20 / @earendil-works/pi-coding-agent）：

- ``normalize_workspace_file_path`` / ``resolve_workspace_file``：1:1 移植自
  piJsonValidationTool.cjs 顶部的 ``normalizeWorkspaceFilePath`` /
  ``resolveWorkspaceFile``（相对 POSIX 路径、拒绝绝对路径，越界抛
  ValueError，中文文案与易标一致）。
- 截断常量与 ``truncate_head`` / ``truncate_line`` / ``format_size``：移植自
  pi 的 truncate.js（DEFAULT_MAX_LINES=2000、DEFAULT_MAX_BYTES=50KB、
  GREP_MAX_LINE_LENGTH=500）。
- 六个工具的参数与语义移植自 pi 的 read.js / write.js / edit.js /
  grep.js / find.js / ls.js；与 pi 源码的差异点（均在对应函数注明）：
  1) 不做图片读取与 bash/sed 回退，read 首行超字节上限时改给中文
     「继续用 offset 读取」提示；
  2) grep/find 用 Python re + pathlib/os.walk 实现，绝不调用 shell
     （rg/fd 不在本仓可用工具集内）；
  3) 不解析 .gitignore（pi 依赖 rg/fd 的 gitignore 行为），遍历时仅跳过
     ``.git`` 与 ``node_modules``（对齐 pi find.js 自定义 glob 分支的
     ignore 列表）。

工具统一返回 ``ToolCallOutcome``（app.services.agent.agent_loop 契约）：
handler 内部 fail-soft，路径越界、文件不存在、edit 不唯一等一律回传
``is_error`` 文本，不抛异常打断 Agent 循环。
"""
from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import Callable, NamedTuple

from app.services.agent.agent_loop import AgentTool, ToolCallOutcome

# ── 截断常量（移植自 truncate.js，取值一字不差）──────────────────────────
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB
GREP_MAX_LINE_LENGTH = 500  # grep 单行输出字符上限
GREP_DEFAULT_LIMIT = 100
FIND_DEFAULT_LIMIT = 1000
LS_DEFAULT_LIMIT = 500

#: grep/find/ls 的最终输出只受字节数上限约束（对齐 pi 源码里
#: Number.MAX_SAFE_INTEGER 行数上限的用法）。
_UNLIMITED_LINES = 1 << 62

#: Windows 盘符前缀（C:、d: 等）。反斜杠归一为 "/" 后形如 "C:/x"，
#: POSIX 语义并不认为它绝对，必须单独拒绝才能复现易标 Windows 行为。
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")

#: 遍历时跳过的目录（对齐 pi find.js 自定义 glob 分支的 ignore 列表）。
_SKIP_DIR_NAMES = frozenset({".git", "node_modules"})


class WorkspaceFile(NamedTuple):
    """``resolve_workspace_file`` 的返回值，对应 JS 的 {relativePath, resolvedPath}。"""

    relative_path: str  # 工作区内相对路径（POSIX 分隔符）
    resolved_path: str  # 绝对路径（已通过工作区包含性检查）


# ── 路径防护（1:1 移植自 piJsonValidationTool.cjs）──────────────────────

def normalize_workspace_file_path(file_path) -> str:
    """统一为工作区相对路径。

    移植自 piJsonValidationTool.cjs ``normalizeWorkspaceFilePath``：先去首尾
    空白、把反斜杠归一为 ``/``，随后拒绝空串与一切绝对路径（POSIX ``/``
    开头、Windows 盘符），最后按 POSIX 规则 normalize。``..`` 越界的最终
    拦截在 ``resolve_workspace_file``——与易标一致，本函数对 ``../x`` 只
    归一不报错。

    Raises:
        ValueError: 路径为空或属于绝对路径（文案与易标逐字一致）。
    """
    relative_path = str(file_path or "").strip().replace("\\", "/")
    if (not relative_path or relative_path.startswith("/")
            or _DRIVE_PREFIX_RE.match(relative_path)):
        raise ValueError("file_path 必须是当前工作区内的非空相对路径")
    # Python 标准库没有 Node 的 path.posix.normalize，语义等价物是 normpath；
    # 唯一差异是 normpath 会吞掉结尾斜杠，这里补回以保持与易标 1:1。
    normalized = posixpath.normpath(relative_path)
    if relative_path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def resolve_workspace_file(workspace_dir, file_path) -> WorkspaceFile:
    """把 Agent 给出的相对路径解析到工作区内，越界抛 ValueError。

    移植自 piJsonValidationTool.cjs ``resolveWorkspaceFile``：解析结果必须
    等于工作区根或位于根之下，否则抛 ``file_path 超出当前工作区：<原路径>``。
    与 Node ``path.resolve`` 的唯一差异：用 ``os.path.abspath``（不解析符号
    链接），避免目录联接点干扰包含性判断。

    Returns:
        :class:`WorkspaceFile`：(相对 POSIX 路径, 绝对路径)。

    Raises:
        ValueError: 空路径、绝对路径或 ``..`` 越界。
    """
    relative_path = normalize_workspace_file_path(file_path)
    workspace_root = os.path.abspath(workspace_dir)
    resolved_path = os.path.abspath(os.path.join(workspace_root, relative_path))
    if (resolved_path != workspace_root
            and not resolved_path.startswith(workspace_root + os.sep)):
        raise ValueError(f"file_path 超出当前工作区：{file_path}")
    return WorkspaceFile(relative_path, resolved_path)


def _resolve_path_arg(workspace_root: str, raw, *, optional: bool) -> WorkspaceFile:
    """解析工具的 ``path`` 参数；非法时抛 ValueError（由调用方转成错误结果）。

    可选参数缺省/空串时退回工作区根目录（对齐 pi 各工具的 ``path || "."``）。
    """
    if optional and (raw is None or raw == ""):
        raw = "."
    if not isinstance(raw, str) or not raw:
        raise ValueError("path 必须是工作区内的非空相对路径")
    return resolve_workspace_file(workspace_root, raw)


# ── 截断工具（移植自 truncate.js）────────────────────────────────────────

def format_size(num_bytes: int) -> str:
    """人类可读大小（移植自 truncate.js ``formatSize``，保留 toFixed 的 .0 风格）。"""
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def truncate_head(content: str, *, max_lines: int = DEFAULT_MAX_LINES,
                  max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """从头部截断内容（移植自 truncate.js ``truncateHead``）。

    行数与字节数两个上限先到者生效，永不返回半行；首行本身超过字节上限时
    返回空内容并置 ``first_line_exceeds_limit=True``。
    """

    def split_lines(text: str) -> list[str]:
        if not text:
            return []
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        return lines

    total_bytes = len(content.encode("utf-8"))
    lines = split_lines(content)
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return {
            "content": content, "truncated": False, "truncated_by": None,
            "total_lines": total_lines, "total_bytes": total_bytes,
            "output_lines": total_lines, "output_bytes": total_bytes,
            "first_line_exceeds_limit": False,
            "max_lines": max_lines, "max_bytes": max_bytes,
        }
    first_line_bytes = len(lines[0].encode("utf-8"))
    if first_line_bytes > max_bytes:
        return {
            "content": "", "truncated": True, "truncated_by": "bytes",
            "total_lines": total_lines, "total_bytes": total_bytes,
            "output_lines": 0, "output_bytes": 0,
            "first_line_exceeds_limit": True,
            "max_lines": max_lines, "max_bytes": max_bytes,
        }
    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    for i, line in enumerate(lines):
        if i >= max_lines:
            break
        line_bytes = len(line.encode("utf-8")) + (1 if i > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output_lines.append(line)
        output_bytes += line_bytes
    if len(output_lines) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"
    output_content = "\n".join(output_lines)
    return {
        "content": output_content, "truncated": True, "truncated_by": truncated_by,
        "total_lines": total_lines, "total_bytes": total_bytes,
        "output_lines": len(output_lines),
        "output_bytes": len(output_content.encode("utf-8")),
        "first_line_exceeds_limit": False,
        "max_lines": max_lines, "max_bytes": max_bytes,
    }


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> tuple[str, bool]:
    """单行截断（移植自 truncate.js ``truncateLine``），返回 (文本, 是否截断)。"""
    if len(line) <= max_chars:
        return line, False
    return line[:max_chars] + "... [truncated]", True


# ── glob / 遍历辅助（供 grep、find 共用）─────────────────────────────────

def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """glob 转正则：``**`` 跨目录、``*``/``?`` 不跨 ``/``（对齐 pi fd --glob）。"""
    parts: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts), re.DOTALL)


def _make_glob_matcher(pattern) -> Callable[[str], bool] | None:
    """按 pi find.js 的 fd 分支规则构造 glob 匹配器。

    - 模式含 ``/``：对相对全路径匹配，且不含 ``**/`` 前缀时自动补上
      （对应 pi 给 fd 追加的 ``**/`` 与 ``--full-path`` 语义）；
    - 模式不含 ``/``：按文件名匹配（fd --glob 默认行为）。

    ``pattern`` 为空/None 时返回 None，表示不过滤。
    """
    if not pattern:
        return None
    effective = pattern
    if ("/" in pattern and not pattern.startswith("/")
            and not pattern.startswith("**/") and pattern != "**"):
        effective = "**/" + pattern
    rx = _glob_to_regex(effective)
    if "/" in pattern:
        return lambda rel: rx.fullmatch(rel) is not None
    return lambda rel: rx.fullmatch(rel.rsplit("/", 1)[-1]) is not None


def _iter_workspace_files(search_dir: str):
    """按字典序递归遍历目录下所有文件（含隐藏文件），跳过 .git / node_modules。"""
    for dirpath, dirnames, filenames in os.walk(search_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIR_NAMES)
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def _read_text_lines(path: str) -> list[str] | None:
    """按 pi grep.js ``getFileLines`` 的规则读行。

    CRLF/CR 归一为 LF；含 NUL 的二进制文件返回 None（对齐 rg 默认跳过
    二进制的行为）；读取失败返回 None。
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return []
    lines = text.split("\n")
    # 末尾换行不产生幽灵空行（对齐 rg 的行语义；pi 的 getFileLines 仅用于
    # 上下文展示，本仓匹配与展示共用同一读行函数，故按 rg 收尾）。
    if text.endswith("\n"):
        lines.pop()
    return lines


def _posix_relpath(path: str, base: str) -> str:
    return os.path.relpath(path, base).replace(os.sep, "/")


# ── 六个原生工具构造器 ───────────────────────────────────────────────────

def build_read_tool(workspace_dir: str) -> AgentTool:
    """read 工具（移植自 pi read.js；差异见模块 docstring）。

    参数：path（必填）、offset（1-indexed 行号）、limit（最大行数）。
    超出文件末尾的 offset 报错；截断上限沿用 truncate.js 常量，截断时给出
    ``Use offset=N to continue.`` 式续读提示。
    """
    root = os.path.abspath(workspace_dir)

    async def _handler(args: dict) -> ToolCallOutcome:
        try:
            target = _resolve_path_arg(root, args.get("path"), optional=False)
        except ValueError as e:
            return ToolCallOutcome.error(str(e))
        try:
            offset = int(args["offset"]) if args.get("offset") is not None else None
            limit = int(args["limit"]) if args.get("limit") is not None else None
        except (TypeError, ValueError):
            return ToolCallOutcome.error("offset/limit 必须是整数")
        try:
            content = Path(target.resolved_path).read_text(
                encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolCallOutcome.error(f"无法读取文件 {target.relative_path}：{e}")
        all_lines = content.split("\n")
        total_lines = len(all_lines)
        # offset 为 1-indexed；0/缺省都从首行开始（对齐 pi 的三元表达式）。
        start = max(0, offset - 1) if offset else 0
        if start >= total_lines:
            return ToolCallOutcome.error(
                f"offset {offset} 超出文件末尾（共 {total_lines} 行）")
        start_display = start + 1
        if limit is not None:
            end = min(start + limit, total_lines)
            selected = "\n".join(all_lines[start:end])
        else:
            selected = "\n".join(all_lines[start:])
        truncation = truncate_head(selected)
        if truncation["first_line_exceeds_limit"]:
            # pi 原文给 bash sed 回退提示；本仓不做 shell 回退，改中文 offset 提示。
            line_size = format_size(len(all_lines[start].encode("utf-8")))
            output = (
                f"第 {start_display} 行大小 {line_size}，超过 "
                f"{format_size(DEFAULT_MAX_BYTES)} 上限，无法整行输出。"
                f"请继续用 offset={start_display + 1} 读取后续内容。"
            )
        elif truncation["truncated"]:
            end_display = start_display + truncation["output_lines"] - 1
            next_offset = end_display + 1
            if truncation["truncated_by"] == "lines":
                note = (f"[Showing lines {start_display}-{end_display} of "
                        f"{total_lines}. Use offset={next_offset} to continue.]")
            else:
                note = (f"[Showing lines {start_display}-{end_display} of "
                        f"{total_lines} ({format_size(DEFAULT_MAX_BYTES)} limit). "
                        f"Use offset={next_offset} to continue.]")
            output = f"{truncation['content']}\n\n{note}"
        elif limit is not None and start + limit < total_lines:
            # 用户指定的 limit 提前收尾，文件仍有剩余。
            remaining = total_lines - (start + limit)
            next_offset = start + limit + 1
            output = (f"{truncation['content']}\n\n[{remaining} more lines in file. "
                      f"Use offset={next_offset} to continue.]")
        else:
            output = truncation["content"]
        return ToolCallOutcome.text(output)

    return AgentTool(
        name="read",
        description=(
            "读取当前工作区内的文本文件。参数：path（必填，工作区内的相对路径，"
            "不允许绝对路径或 .. 越界）、offset（可选，起始行号，从 1 开始）、"
            "limit（可选，最多读取的行数）。文本按 2000 行或 50KB 上限截断"
            "（先到者为准），截断时结果末尾给出 Use offset=N to continue. 提示，"
            "大文件请用 offset 分段读完。不支持图片与二进制文件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "要读取的文件路径（工作区内的相对路径）"},
                "offset": {"type": "integer",
                           "description": "起始行号（从 1 开始）"},
                "limit": {"type": "integer",
                          "description": "最多读取的行数"},
            },
            "required": ["path"],
        },
        handler=_handler,
        # 原文照抄（pi read.js promptGuidelines）
        prompt_guidelines=["Use read to examine files instead of cat or sed."],
    )


def build_write_tool(workspace_dir: str) -> AgentTool:
    """write 工具（移植自 pi write.js）：path + content，覆盖写、自动建父目录。"""
    root = os.path.abspath(workspace_dir)

    async def _handler(args: dict) -> ToolCallOutcome:
        raw_path = args.get("path")
        content = args.get("content")
        if not isinstance(raw_path, str) or not raw_path:
            return ToolCallOutcome.error("path 必须是工作区内的非空相对路径")
        if not isinstance(content, str):
            return ToolCallOutcome.error("content 必须是字符串")
        try:
            target = resolve_workspace_file(root, raw_path)
        except ValueError as e:
            return ToolCallOutcome.error(str(e))
        try:
            Path(target.resolved_path).parent.mkdir(parents=True, exist_ok=True)
            # 写字节而非文本模式，避免 Windows 下 \n 被自动翻成 \r\n（对齐 Node 写入语义）。
            Path(target.resolved_path).write_bytes(content.encode("utf-8"))
        except OSError as e:
            return ToolCallOutcome.error(f"写入文件 {target.relative_path} 失败：{e}")
        byte_count = len(content.encode("utf-8"))
        return ToolCallOutcome.text(
            f"Successfully wrote {byte_count} bytes to {raw_path}")

    return AgentTool(
        name="write",
        description=(
            "写入当前工作区内的文件。参数：path（必填，工作区内相对路径）、"
            "content（必填，完整文件内容）。文件不存在则创建（自动创建父目录），"
            "已存在则整体覆盖。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "要写入的文件路径（工作区内的相对路径）"},
                "content": {"type": "string", "description": "写入文件的完整内容"},
            },
            "required": ["path", "content"],
        },
        handler=_handler,
        # 原文照抄（pi write.js promptGuidelines）
        prompt_guidelines=["Use write only for new files or complete rewrites."],
    )


def build_edit_tool(workspace_dir: str) -> AgentTool:
    """edit 工具（移植自 pi edit.js / edit-diff.js 的精确替换核心）。

    参数：path + edits（{oldText, newText} 列表）。每条 oldText 都针对原文件
    匹配：必须唯一、互不重叠，否则报错；多处修改合并为一次调用。匹配为精确
    匹配（pi 的 normalizeForFuzzyMatch 模糊回退未移植，模型侧描述明确要求
    "必须完全一致"）。行尾风格与 BOM 按 pi 逻辑保留。
    """
    root = os.path.abspath(workspace_dir)

    async def _handler(args: dict) -> ToolCallOutcome:
        raw_path = args.get("path")
        edits = args.get("edits")
        if not isinstance(raw_path, str) or not raw_path:
            return ToolCallOutcome.error("path 必须是工作区内的非空相对路径")
        if not isinstance(edits, list) or not edits:
            return ToolCallOutcome.error("edit 参数无效：edits 至少要包含一条替换。")
        pairs: list[tuple[str, str]] = []
        for i, item in enumerate(edits):
            if (not isinstance(item, dict)
                    or not isinstance(item.get("oldText"), str)
                    or not isinstance(item.get("newText"), str)):
                return ToolCallOutcome.error(
                    f"edits[{i}] 必须包含字符串类型的 oldText 与 newText")
            pairs.append((item["oldText"], item["newText"]))
        try:
            target = _resolve_path_arg(root, raw_path, optional=False)
        except ValueError as e:
            return ToolCallOutcome.error(str(e))
        rel = target.relative_path
        try:
            raw = Path(target.resolved_path).read_bytes()
        except OSError as e:
            return ToolCallOutcome.error(f"无法编辑文件 {rel}：{e}")
        text = raw.decode("utf-8", errors="replace")
        # BOM 先摘掉再匹配（模型的 oldText 不会带不可见 BOM），写回时补上。
        bom = ""
        if text.startswith("\ufeff"):
            bom, text = "\ufeff", text[1:]
        # 行尾检测（移植自 edit-diff.js detectLineEnding）。
        lf_idx = text.find("\n")
        crlf_idx = text.find("\r\n")
        if lf_idx == -1 or crlf_idx == -1:
            ending = "\n"
        elif crlf_idx < lf_idx:
            ending = "\r\n"
        else:
            ending = "\n"
        base = text.replace("\r\n", "\n").replace("\r", "\n")
        # 所有 edit 都针对同一份原文件匹配（不增量生效），逐条校验唯一性。
        matches: list[tuple[int, int, int, str]] = []
        for i, (old_raw, new_raw) in enumerate(pairs):
            old = old_raw.replace("\r\n", "\n").replace("\r", "\n")
            new = new_raw.replace("\r\n", "\n").replace("\r", "\n")
            if not old:
                return ToolCallOutcome.error(f"edits[{i}].oldText 不能为空（{rel}）")
            count = base.count(old)
            if count == 0:
                if len(pairs) == 1:
                    return ToolCallOutcome.error(
                        f"在 {rel} 中未找到要替换的文本。oldText 必须与原文完全一致"
                        f"（含空格与换行）。")
                return ToolCallOutcome.error(
                    f"在 {rel} 中未找到 edits[{i}]。oldText 必须与原文完全一致"
                    f"（含空格与换行）。")
            if count > 1:
                if len(pairs) == 1:
                    return ToolCallOutcome.error(
                        f"在 {rel} 中找到 {count} 处相同文本，oldText 必须唯一，"
                        f"请补充上下文使其唯一。")
                return ToolCallOutcome.error(
                    f"在 {rel} 中找到 edits[{i}] 的 {count} 处相同文本。"
                    f"每个 oldText 必须唯一，请补充上下文使其唯一。")
            matches.append((base.find(old), len(old), i, new))
        # 重叠检测（移植自 edit-diff.js applyEditsToNormalizedContent）。
        matches.sort(key=lambda m: m[0])
        for prev, curr in zip(matches, matches[1:]):
            if prev[0] + prev[1] > curr[0]:
                return ToolCallOutcome.error(
                    f"edits[{prev[2]}] 与 edits[{curr[2]}] 在 {rel} 中重叠，"
                    f"请合并为一条或改为互不重叠的编辑")
        # 从后往前替换，保持前面匹配的下标稳定（对齐 applyReplacements）。
        new_content = base
        for idx, length, _i, new in sorted(matches, key=lambda m: m[0], reverse=True):
            new_content = new_content[:idx] + new + new_content[idx + length:]
        if new_content == base:
            return ToolCallOutcome.error(
                f"未对 {rel} 做出任何修改：替换结果与原文相同")
        final_text = new_content.replace("\n", "\r\n") if ending == "\r\n" else new_content
        try:
            Path(target.resolved_path).write_bytes((bom + final_text).encode("utf-8"))
        except OSError as e:
            return ToolCallOutcome.error(f"写入文件 {rel} 失败：{e}")
        return ToolCallOutcome.text(
            f"Successfully replaced {len(pairs)} block(s) in {raw_path}.")

    return AgentTool(
        name="edit",
        description=(
            "对当前工作区内的单个文件做精确文本替换。参数：path（必填）、"
            "edits（必填，{oldText, newText} 对象数组）。每条 oldText 必须在原文件中"
            "唯一且互不重叠，须与原文完全一致（含空格与换行）；同一文件要改多处时，"
            "在一次调用里给出多条 edits，不要拆成多次调用，也不要用大段未变文本"
            "去连接相距很远的改动。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "要编辑的文件路径（工作区内的相对路径）"},
                "edits": {
                    "type": "array",
                    "description": "一条或多条定向替换，全部针对原文件匹配，不得重叠或嵌套；"
                                   "两处改动落在同一块或相邻行时合并成一条。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {
                                "type": "string",
                                "description": "要被替换的原文，必须在文件中唯一，且与其他"
                                               "edits[].oldText 不重叠。",
                            },
                            "newText": {"type": "string",
                                        "description": "替换后的新文本。"},
                        },
                        "required": ["oldText", "newText"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
        handler=_handler,
        # 原文照抄（pi edit.js promptGuidelines）
        prompt_guidelines=[
            "Use edit for precise changes (edits[].oldText must match exactly)",
            "When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls",
            "Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
            "Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.",
        ],
    )


def build_grep_tool(workspace_dir: str) -> AgentTool:
    """grep 工具（移植自 pi grep.js；本仓用 Python re 实现，不调 shell）。

    参数：pattern + 可选 path/glob/ignore_case/literal/context/limit。
    单行输出按 GREP_MAX_LINE_LENGTH=500 截断；总输出受 100 条匹配与 50KB
    字节上限约束，截断时追加 pi 原文提示。
    """
    root = os.path.abspath(workspace_dir)

    async def _handler(args: dict) -> ToolCallOutcome:
        pattern = args.get("pattern")
        if not isinstance(pattern, str):
            return ToolCallOutcome.error("pattern 必须是字符串")
        try:
            target = _resolve_path_arg(root, args.get("path"), optional=True)
        except ValueError as e:
            return ToolCallOutcome.error(str(e))
        glob_pattern = args.get("glob")
        if glob_pattern is not None and not isinstance(glob_pattern, str):
            return ToolCallOutcome.error("glob 必须是字符串")
        try:
            raw_limit = args.get("limit")
            limit = max(1, int(raw_limit)) if raw_limit is not None else GREP_DEFAULT_LIMIT
            raw_context = args.get("context")
            context = max(0, int(raw_context)) if raw_context is not None else 0
        except (TypeError, ValueError):
            return ToolCallOutcome.error("limit/context 必须是整数")
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        if args.get("literal"):
            rx = re.compile(re.escape(pattern), flags)
        else:
            try:
                rx = re.compile(pattern, flags)
            except re.error as e:
                return ToolCallOutcome.error(f"正则表达式无效：{e}")
        search_path = target.resolved_path
        if not os.path.exists(search_path):
            return ToolCallOutcome.error(f"路径不存在：{target.relative_path}")
        if os.path.isfile(search_path):
            candidates = [search_path]
        elif os.path.isdir(search_path):
            candidates = list(_iter_workspace_files(search_path))
        else:
            return ToolCallOutcome.error(
                f"路径不是普通文件或目录：{target.relative_path}")
        matcher = _make_glob_matcher(glob_pattern)
        match_limit = False
        lines_truncated = False
        matches: list[tuple[str, int, list[str]]] = []
        file_lines: dict[str, list[str]] = {}
        for abs_path in candidates:
            rel = (_posix_relpath(abs_path, search_path) if os.path.isdir(search_path)
                   else os.path.basename(abs_path))
            if matcher is not None and not matcher(rel):
                continue
            lines = file_lines.get(abs_path)
            if lines is None:
                lines = _read_text_lines(abs_path) or []
                file_lines[abs_path] = lines
            if not lines:
                continue
            for lineno, line in enumerate(lines, start=1):
                if rx.search(line):
                    matches.append((rel, lineno, lines))
                    if len(matches) >= limit:
                        match_limit = True
                        break
            if match_limit:
                break
        if not matches:
            return ToolCallOutcome.text("No matches found")
        output_parts: list[str] = []
        for rel, lineno, lines in matches:
            if context == 0:
                text, was_truncated = truncate_line(lines[lineno - 1])
                lines_truncated = lines_truncated or was_truncated
                output_parts.append(f"{rel}:{lineno}: {text}")
            else:
                start = max(1, lineno - context)
                end = min(len(lines), lineno + context)
                for cur in range(start, end + 1):
                    text, was_truncated = truncate_line(lines[cur - 1])
                    lines_truncated = lines_truncated or was_truncated
                    if cur == lineno:
                        output_parts.append(f"{rel}:{cur}: {text}")
                    else:
                        output_parts.append(f"{rel}-{cur}- {text}")
        raw_output = "\n".join(output_parts)
        truncation = truncate_head(raw_output, max_lines=_UNLIMITED_LINES)
        output = truncation["content"]
        notices: list[str] = []
        if match_limit:
            notices.append(
                f"{limit} matches limit reached. Use limit={limit * 2} for more, "
                f"or refine pattern")
        if truncation["truncated"]:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
        if lines_truncated:
            notices.append(
                f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars. "
                f"Use read tool to see full lines")
        if notices:
            output += "\n\n[" + ". ".join(notices) + "]"
        return ToolCallOutcome.text(output)

    return AgentTool(
        name="grep",
        description=(
            "在当前工作区的文件内容中搜索模式，返回相对路径、行号与匹配行。"
            "参数：pattern（必填，正则表达式）、path（可选，搜索目录或文件，"
            "默认工作区根目录）、glob（可选，文件名过滤，如 *.ts 或 "
            "**/*.spec.ts）、ignore_case（可选，忽略大小写）、literal（可选，"
            "true 时按字面量匹配而非正则）、context（可选，匹配行前后各显示的"
            "行数，默认 0）、limit（可选，最大匹配条数，默认 100）。单行超过 "
            "500 字符会截断；结果最多返回 100 条匹配或 50KB。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "搜索模式（正则或字面量字符串）"},
                "path": {"type": "string",
                         "description": "搜索的目录或文件（默认工作区根目录）"},
                "glob": {"type": "string",
                         "description": "按 glob 过滤文件，如 '*.ts' 或 '**/*.spec.ts'"},
                "ignore_case": {"type": "boolean",
                                "description": "忽略大小写搜索（默认 false）"},
                "literal": {"type": "boolean",
                            "description": "把 pattern 当字面量而非正则（默认 false）"},
                "context": {"type": "integer",
                            "description": "每条匹配前后各显示的行数（默认 0）"},
                "limit": {"type": "integer",
                          "description": "返回的最大匹配条数（默认 100）"},
            },
            "required": ["pattern"],
        },
        handler=_handler,
        # pi grep.js 无 promptGuidelines（仅 promptSnippet），故留空。
        prompt_guidelines=[],
    )


def build_find_tool(workspace_dir: str) -> AgentTool:
    """find 工具（移植自 pi find.js；本仓用 pathlib 遍历，不调 shell）。

    参数：pattern（glob）+ 可选 path/limit（默认 1000）。返回相对搜索目录的
    POSIX 路径，命中 limit 时追加 pi 原文提示。
    """
    root = os.path.abspath(workspace_dir)

    async def _handler(args: dict) -> ToolCallOutcome:
        pattern = args.get("pattern")
        if not isinstance(pattern, str):
            return ToolCallOutcome.error("pattern 必须是字符串")
        try:
            target = _resolve_path_arg(root, args.get("path"), optional=True)
        except ValueError as e:
            return ToolCallOutcome.error(str(e))
        try:
            raw_limit = args.get("limit")
            limit = int(raw_limit) if raw_limit is not None else FIND_DEFAULT_LIMIT
        except (TypeError, ValueError):
            return ToolCallOutcome.error("limit 必须是整数")
        search_path = target.resolved_path
        if not os.path.isdir(search_path):
            return ToolCallOutcome.error(f"路径不存在：{target.relative_path}")
        matcher = _make_glob_matcher(pattern)
        results: list[str] = []
        result_limit_reached = False
        for abs_path in _iter_workspace_files(search_path):
            rel = _posix_relpath(abs_path, search_path)
            if matcher is not None and not matcher(rel):
                continue
            results.append(rel)
            if len(results) >= limit:
                result_limit_reached = True
                break
        if not results:
            return ToolCallOutcome.text("No files found matching pattern")
        raw_output = "\n".join(results)
        truncation = truncate_head(raw_output, max_lines=_UNLIMITED_LINES)
        output = truncation["content"]
        notices: list[str] = []
        if result_limit_reached:
            notices.append(
                f"{limit} results limit reached. Use limit={limit * 2} for more, "
                f"or refine pattern")
        if truncation["truncated"]:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
        if notices:
            output += "\n\n[" + ". ".join(notices) + "]"
        return ToolCallOutcome.text(output)

    return AgentTool(
        name="find",
        description=(
            "按 glob 模式在当前工作区内查找文件，返回相对搜索目录的路径。"
            "参数：pattern（必填，如 *.ts、**/*.json、src/**/*.spec.ts）、"
            "path（可选，搜索目录，默认工作区根目录）、limit（可选，最大结果"
            "数，默认 1000）。输出最多 1000 条或 50KB。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "匹配文件的 glob 模式，如 '*.ts'、"
                                           "'**/*.json'、'src/**/*.spec.ts'"},
                "path": {"type": "string",
                         "description": "搜索目录（默认工作区根目录）"},
                "limit": {"type": "integer",
                          "description": "返回的最大结果数（默认 1000）"},
            },
            "required": ["pattern"],
        },
        handler=_handler,
        # pi find.js 无 promptGuidelines（仅 promptSnippet），故留空。
        prompt_guidelines=[],
    )


def build_ls_tool(workspace_dir: str) -> AgentTool:
    """ls 工具（移植自 pi ls.js）。

    参数：可选 path/limit（默认 500）。按字母序（忽略大小写）排序，目录名带
    ``/`` 后缀，包含隐藏文件；不存在/非目录分别报「路径不存在」「不是目录」。
    """
    root = os.path.abspath(workspace_dir)

    async def _handler(args: dict) -> ToolCallOutcome:
        try:
            target = _resolve_path_arg(root, args.get("path"), optional=True)
        except ValueError as e:
            return ToolCallOutcome.error(str(e))
        try:
            raw_limit = args.get("limit")
            limit = int(raw_limit) if raw_limit is not None else LS_DEFAULT_LIMIT
        except (TypeError, ValueError):
            return ToolCallOutcome.error("limit 必须是整数")
        dir_path = target.resolved_path
        if not os.path.exists(dir_path):
            return ToolCallOutcome.error(f"路径不存在：{target.relative_path}")
        if not os.path.isdir(dir_path):
            return ToolCallOutcome.error(f"不是目录：{target.relative_path}")
        try:
            entries = os.listdir(dir_path)
        except OSError as e:
            return ToolCallOutcome.error(f"无法读取目录 {target.relative_path}：{e}")
        # 对齐 pi：按字母序、大小写不敏感。
        entries.sort(key=lambda name: name.lower())
        results: list[str] = []
        entry_limit_reached = False
        for entry in entries:
            if len(results) >= limit:
                entry_limit_reached = True
                break
            full = os.path.join(dir_path, entry)
            try:
                is_dir = os.path.isdir(full)
            except OSError:
                continue  # stat 失败的条目直接跳过（对齐 pi）
            results.append(entry + ("/" if is_dir else ""))
        if not results:
            return ToolCallOutcome.text("(empty directory)")
        raw_output = "\n".join(results)
        truncation = truncate_head(raw_output, max_lines=_UNLIMITED_LINES)
        output = truncation["content"]
        notices: list[str] = []
        if entry_limit_reached:
            notices.append(
                f"{limit} entries limit reached. Use limit={limit * 2} for more")
        if truncation["truncated"]:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
        if notices:
            output += "\n\n[" + ". ".join(notices) + "]"
        return ToolCallOutcome.text(output)

    return AgentTool(
        name="ls",
        description=(
            "列出当前工作区内的目录内容。参数：path（可选，要列出的目录，"
            "默认工作区根目录）、limit（可选，最大条目数，默认 500）。条目按"
            "字母序（忽略大小写）排序，目录名带 / 后缀，包含隐藏文件；输出最多"
            " 500 条或 50KB。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "要列出的目录（默认工作区根目录）"},
                "limit": {"type": "integer",
                          "description": "返回的最大条目数（默认 500）"},
            },
        },
        handler=_handler,
        # pi ls.js 无 promptGuidelines（仅 promptSnippet），故留空。
        prompt_guidelines=[],
    )


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    "WorkspaceFile",
    "build_edit_tool",
    "build_find_tool",
    "build_grep_tool",
    "build_ls_tool",
    "build_read_tool",
    "build_write_tool",
    "normalize_workspace_file_path",
    "resolve_workspace_file",
]
