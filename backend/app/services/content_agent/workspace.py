"""Agent 修复隔离工作区（R135）。

移植来源：易标 ``contentGenerationAgent.cjs`` 的工作区布局
（``正文/{section_id}.html`` 按小节 id 存文件 + 输入材料独立文件）与
``contentGenerationConsistencyTools.cjs`` 的「小节目录」约定。

与易标的差异（本仓约束，均已在函数 docstring 注明）：

1. 小节正文用 **Markdown**（本仓正文即 Markdown，不是易标的受限 HTML），
   文件名仍按 ``section_id``（UUID）确定，避免中文标题重名/路径字符问题；
2. 每个小节文件带 ``<!-- SECTION:{id}:BEGIN -->`` / ``END`` 程序标记，
   Agent 的修改必须发生在标记对内 —— 程序解析时**只认标记对**，
   标记缺失/重复/次序颠倒一律判无效（对应易标「提交校验」的章节结构检查）；
3. 标题行写在标记**之外**（只读上下文，不参与回写提取）。
"""
from __future__ import annotations

import os
from pathlib import Path

from app.config import DATA_DIR

#: 工作区内的小节文件目录（与易标 ``正文/`` 同名）。
SECTION_DIR = "正文"
#: 输入材料文件名（全局事实设定 → 易标 ``全局事实设定.md`` 同义）。
FACTS_FILE = "全局事实.md"
#: 任务说明（冲突清单 + 修复要求；程序写入、基线保护）。
TASK_FILE = "任务说明.md"

_BEGIN_FMT = "<!-- SECTION:{sid}:BEGIN -->"
_END_FMT = "<!-- SECTION:{sid}:END -->"


def section_relpath(section_id: str) -> str:
    """小节文件的工作区相对路径（POSIX 分隔符）。

    与易标 ``sectionFile(id)`` 同构：文件名由目录 ID 确定，避免中文标题
    重名或 Windows 路径字符。id 内出现路径分隔符属脏数据，直接拒绝。
    """
    sid = str(section_id or "").strip()
    if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
        raise ValueError(f"非法的小节 ID：{section_id!r}")
    return f"{SECTION_DIR}/{sid}.md"


def section_markers(section_id: str) -> tuple[str, str]:
    """小节开始/结束标记（``(BEGIN, END)``）。"""
    return _BEGIN_FMT.format(sid=section_id), _END_FMT.format(sid=section_id)


def render_section_file(title: str, content: str, section_id: str) -> str:
    """渲染一个小节文件全文：标题行（标记外）+ 标记对包裹的正文。"""
    begin, end = section_markers(section_id)
    header = f"# {str(title or '').strip()}\n\n" if str(title or "").strip() else ""
    return f"{header}{begin}\n{content or ''}\n{end}\n"


def parse_section_content(text: str, section_id: str) -> str | None:
    """从一个小节文件全文中提取标记对内的正文。

    判据（任一不满足即返回 ``None``，绝不猜）：
    - BEGIN / END 各出现**恰好一次**；
    - BEGIN 在 END **之前**；
    - 两者都**独占一行**（防止 Agent 把标记写进段落中间或拼进行尾）。

    返回的正文剥掉首尾空白行（物化时补的换行），**不改内部任何字符**。
    """
    begin, end = section_markers(section_id)
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    begin_idx = [i for i, ln in enumerate(lines) if ln.strip() == begin]
    end_idx = [i for i, ln in enumerate(lines) if ln.strip() == end]
    if len(begin_idx) != 1 or len(end_idx) != 1 or begin_idx[0] >= end_idx[0]:
        return None
    inner = lines[begin_idx[0] + 1:end_idx[0]]
    while inner and not inner[0].strip():
        inner.pop(0)
    while inner and not inner[-1].strip():
        inner.pop()
    return "\n".join(inner)


def read_section_file(workspace_dir: str | Path, section_id: str) -> str | None:
    """读取一个小节文件的**全文**；文件不存在返回 ``None``。"""
    path = Path(workspace_dir) / section_relpath(section_id)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def workspace_root_for(task_id: str) -> Path:
    """Agent 任务工作区根（``DATA_DIR/agent_workspaces/<task_id>``）。

    与 ``services/agent/outline_agent_flow.workspace_root_for`` 同一约定；
    task_id 带 ``content_repair_`` 前缀区分业务。
    """
    return DATA_DIR / "agent_workspaces" / str(task_id)


def materialize_workspace(
    workspace_dir: str | Path,
    *,
    sections: list[dict],
    facts_text: str = "",
    task_instructions: str = "",
) -> None:
    """把主存储的小节正文与输入材料写入隔离工作区（覆盖式物化）。

    Args:
        sections: ``[{"section_id", "title", "content"}]``，每个小节一个文件；
        facts_text: 全局事实文本（``全局事实.md``；空则跳过该文件）；
        task_instructions: 任务说明（``任务说明.md``；冲突清单 + 修复要求）。
    """
    root = Path(workspace_dir)
    (root / SECTION_DIR).mkdir(parents=True, exist_ok=True)
    for sec in sections:
        sid = str(sec.get("section_id") or "")
        rel = section_relpath(sid)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_section_file(sec.get("title", ""), sec.get("content") or "", sid),
            encoding="utf-8", newline="\n")
    if str(facts_text or "").strip():
        (root / FACTS_FILE).write_text(str(facts_text), encoding="utf-8",
                                       newline="\n")
    (root / TASK_FILE).write_text(
        str(task_instructions or "").strip() or "（无）",
        encoding="utf-8", newline="\n")
    # 确保目录真实存在（write_text 已建，这里兜底静态检查场景）。
    os.makedirs(root, exist_ok=True)
