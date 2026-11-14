"""OMP 沙箱契约 —— 工具白名单 / 路径越界 / 红线覆盖的**单一事实源**。

对应《专项方案编制软件引入 oh-my-pi 底层 Agent 引擎.md》§8「安全与治理」与
§10 测试矩阵「安全」行（越界写、禁工具、禁网、密钥读取拒绝）：

* **工具越权**：受控白名单必须 *fail-closed* —— 空白名单（= 不传 ``--tools``，
  等价于放开 omp 全部内置工具，含 bash 与网络工具）一律拒绝启动；白名单含
  **网络 / 浏览器类**工具一律拒绝启动（**在拉起子进程之前**拦，不靠事后诊断）。
* **命令运行（用户裁决 2026-11-13）**：**放开**对 Agent 引擎工具的命令运行约束 ——
  Agent 引擎工具的命令**可在软件内运行**（``bash`` / ``exec`` / ``shell`` …）。
  安全边界因此从「禁命令」迁移为：**工作区隔离 + 路径越界拦截 + 禁网 + 禁业务库 +
  禁读密钥**（见 :data:`COMMAND_TOOLS` 与 :func:`safety_summary` 的
  ``command_execution_policy`` 字段，运维面可查）。
* **路径越界**：工作区相对路径统一经 :func:`resolve_workspace_path` 解析，
  规范化后必须仍位于工作区根目录内（``..`` / Windows 盘符 / 符号链接逃逸一律拒绝）。
* **禁网**：Agent 侧不得暴露网络 / 浏览器类工具（``web_search`` / ``browser`` …）。
* **密钥泄露**：角色提示词必须含「不读 .env / 密钥 / 配置文件」红线。
* **业务库保护**：Agent 不写业务库 —— 由护栏对 ``omp_*`` / ``ompi/*`` 服务模块
  做 AST 扫仓断言（本模块只提供判定原语，不做扫仓）。

⚠️ 本模块是**零依赖叶子模块**（仅标准库），供三层消费：

1. **执行期 fail-closed**：``omp_task_agent`` 启动前的白名单闸门 +
   ``omp_rpc`` 拉起子进程前的兜底校验；
2. **诊断期只读体检**：``omp_diagnostics.preflight`` 与运维诊断端点；
3. **测试期扫仓断言**：护栏（工具越权 / 禁网 / 业务库零写入）。

⚠️ 判据**只在本模块定义一次**：``omp_diagnostics.FORBIDDEN_TOOLS`` 改为委托
   本模块，避免「同一清单两份副本」漂移（本仓最高频反模式）。

⚠️ 分层约束：services 层禁止 import routers；本模块零网络、零数据库访问。
"""

from __future__ import annotations

import os
import re

#: 命令执行类工具（**用户裁决 2026-11-13：放开** —— Agent 引擎工具的命令可在软件内
#: 运行）。安全边界改为依赖工作区隔离 + 禁网 + 路径越界拦截 + 禁业务库 + 禁读密钥，
#: 故 :data:`FORBIDDEN_TOOLS`（命令类部分）置空；本清单仅作**可观测面**登记，
#: 让运维在诊断端点一眼看到「当前放开了哪些命令工具」。
COMMAND_TOOLS: tuple[str, ...] = (
    "bash",
    "exec",
    "eval",
    "shell",
    "sh",
    "cmd",
    "powershell",
    "pwsh",
    "run_command",
    "terminal",
)

#: 命令运行策略（用户裁决口径，供诊断面回显，避免「悄悄放开」）。
COMMAND_EXECUTION_POLICY: str = (
    "allowed：Agent 引擎工具的命令可在软件内运行（用户裁决 2026-11-13）；"
    "边界 = 工作区隔离 + 路径越界拦截 + 禁网 + 禁业务库 + 禁读密钥"
)

#: 命令执行类工具（原沙箱铁律禁 bash / eval / 任意 shell，2026-11-13 放开）。
#: Agent 引擎工具的命令可在软件内运行 —— 安全边界改为依赖工作区隔离 + 禁网 +
#: 路径越界拦截（以下 FORBIDDEN_TOOLS 置空）。
FORBIDDEN_TOOLS: tuple[str, ...] = ()

#: 网络 / 浏览器类工具（doc §8「网络访问」：默认禁网）。
NETWORK_TOOLS: tuple[str, ...] = (
    "web_search",
    "websearch",
    "github",
    "browser",
    "computer",
    "fetch",
    "http",
    "curl",
    "wget",
)

#: 受控默认白名单（放开命令执行：加入 bash，与 ``config.omp_task_tools`` / ``omp_agent_roles.DEFAULT_TOOLS`` 同口径）。
SAFE_DEFAULT_TOOLS: str = "write,read,edit,grep,glob,bash"

#: 疑似密钥 / 凭据文件名片段（供工作区产物体检使用）。
SECRET_FILE_MARKERS: tuple[str, ...] = (
    ".env",
    "secret",
    "id_rsa",
    ".pem",
    ".key",
    "credential",
    "password",
)

#: 角色提示词必须覆盖的红线（key, 任一命中即视为覆盖）。
#: ⚠️ 判据只看**是否出现**，不判句式 —— 目的是「红线必须在场且不得被删」，
#:    而非判断该句是禁令还是授权（授权情形由护栏的源码扫描另行把关）。
#: ✅ 2026-11-13 · no_command 红线已移除（用户裁决：放开命令运行约束）。
#:    由此**命令的作用域约束**（仅限 omp 的隔离工作区）成为唯一的软约束 ——
#:    平台侧无法硬拦子进程内的任意路径访问，故该约束必须在角色提示词里保持在场，
#:    否则「命令可运行」就会退化成「命令可在整机任意路径运行」。
REDLINE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("no_network", ("禁止访问网络", "不访问网络", "不得访问网络")),
    ("workspace_only", ("工作区",)),
    ("command_scope", ("隔离工作区",)),
    ("no_db_write", ("业务数据库", "数据库")),
    ("no_secret_read", (".env", "密钥")),
)

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:")


def parse_tools(tools: str) -> list[str]:
    """把逗号分隔的工具白名单解析成小写去重列表（顺序保持）。"""
    out: list[str] = []
    for raw in str(tools or "").split(","):
        item = raw.strip().lower()
        if item and item not in out:
            out.append(item)
    return out


def check_tool_allowlist(tools: str, *, allow_empty: bool = False) -> dict:
    """检查工具白名单是否满足安全契约（纯函数，不抛异常）。

    返回结构::

        {"ok": bool, "allowed": [...], "forbidden": [...], "network": [...], "problems": [...]}

    ⚠️ **空白名单默认不通过**（``allow_empty=False``）：不传 ``--tools`` 等价于
    放开 omp 全部内置工具（含 bash）；请显式配置受控白名单。
    ⚠️ 2026-11-13：FORBIDDEN_TOOLS 置空（放开命令执行），当前仅拦网络/浏览器工具。
    """
    allowed = parse_tools(tools)
    forbidden = [t for t in allowed if t in FORBIDDEN_TOOLS]
    network = [t for t in allowed if t in NETWORK_TOOLS]
    problems: list[str] = []
    if not allowed and not allow_empty:
        problems.append(
            "工具白名单为空：omp 未收到 --tools 时会启用全部内置工具（含 bash）；"
            f"请显式配置受控白名单（如 {SAFE_DEFAULT_TOOLS}）"
        )
    if forbidden:
        problems.append(f"工具白名单含禁用工具（禁止任意命令执行）：{sorted(forbidden)}")
    if network:
        problems.append(f"工具白名单含网络/浏览器工具（默认禁网）：{sorted(network)}")
    return {
        "ok": not problems,
        "allowed": allowed,
        "forbidden": sorted(forbidden),
        "network": sorted(network),
        "problems": problems,
    }


def assert_tool_allowlist(tools: str, *, allow_empty: bool = False) -> list[str]:
    """同 :func:`check_tool_allowlist`，但不通过时抛 ``ValueError``（含全部原因）。"""
    verdict = check_tool_allowlist(tools, allow_empty=allow_empty)
    if not verdict["ok"]:
        raise ValueError("；".join(verdict["problems"]))
    return verdict["allowed"]


def resolve_workspace_path(root: str, rel_path: str) -> str:
    """把工作区相对路径解析为**必定位于工作区内**的绝对路径。

    拒绝形态：空路径、``..`` 组件、Windows 盘符绝对路径、规范化后逃逸出根目录
    （含符号链接逃逸）。前导 ``/`` 沿用既有语义：视为**工作区相对**路径
    （``omp_workflow._write_file`` 历史行为，保留以免改变既有产物路径）。
    """
    root_abs = os.path.abspath(str(root or ""))
    rel = str(rel_path or "").replace("\\", "/").lstrip("/").strip()
    if not rel:
        raise ValueError("非法工作区路径：空路径")
    if _WINDOWS_ABS_RE.match(rel) or os.path.isabs(rel):
        raise ValueError(f"非法工作区路径（绝对路径）：{rel_path}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"非法工作区路径：{rel_path}")
    if any(p == ".." for p in parts):
        raise ValueError(f"非法工作区路径（越界）：{rel_path}")
    target = os.path.abspath(os.path.join(root_abs, *parts))
    root_real = os.path.realpath(root_abs)
    target_real = os.path.realpath(target)
    try:
        common = os.path.commonpath([root_real, target_real])
    except ValueError:  # pragma: no cover - 跨盘符（Windows）理论上不可达
        common = ""
    if common != root_real:
        raise ValueError(f"非法工作区路径（逃逸工作区）：{rel_path}")
    return target


def missing_redlines(text: str) -> list[str]:
    """返回提示词中**缺失**的红线 key（空列表 = 红线齐全）。"""
    body = str(text or "")
    missing: list[str] = []
    for key, markers in REDLINE_MARKERS:
        if not any(m in body for m in markers):
            missing.append(key)
    return missing


def secret_like_paths(paths) -> list[str]:
    """挑出疑似密钥 / 凭据文件（工作区产物体检用）。"""
    hits: list[str] = []
    for raw in list(paths or []):
        name = os.path.basename(str(raw or "")).lower()
        if not name:
            continue
        if any(marker in name for marker in SECRET_FILE_MARKERS):
            hits.append(str(raw))
    return hits


def safety_summary(*, tools: str | None = None) -> dict:
    """沙箱契约自检摘要（只读，供诊断端点与护栏复用）。

    返回：工具白名单判定 + 红线覆盖情况 + 禁用工具清单（供前端展示）。
    """
    from app.config import settings
    from app.services.ai.omp_agent_roles import (
        AUDIT_ROLE,
        CONTENT_ROLE,
        OUTLINE_ROLE,
        SYSTEM_PREAMBLE,
    )

    raw_tools = tools if tools is not None else getattr(settings, "omp_task_tools", "")
    verdict = check_tool_allowlist(raw_tools)
    roles = {
        "preamble": missing_redlines(SYSTEM_PREAMBLE),
        "outline": missing_redlines(OUTLINE_ROLE.system_prompt),
        "content": missing_redlines(CONTENT_ROLE.system_prompt),
        "audit": missing_redlines(AUDIT_ROLE.system_prompt),
    }
    # 密钥泄露前置信号：配置的工作区根目录里若躺着 ``.env`` / 密钥类文件，
    # 立刻点名（只读列举顶层，限 200 项；目录不存在 / 不可读 ⇒ 空）。
    workspace_root = str(getattr(settings, "omp_workspace_dir", "") or "").strip()
    secret_files: list[str] = []
    if workspace_root and os.path.isdir(workspace_root):
        try:
            secret_files = secret_like_paths(sorted(os.listdir(workspace_root))[:200])
        except OSError:
            secret_files = []
    return {
        "tools": verdict,
        "forbidden_tools": list(FORBIDDEN_TOOLS),
        "command_tools_allowed": list(COMMAND_TOOLS),
        "command_execution_policy": COMMAND_EXECUTION_POLICY,
        "network_tools": list(NETWORK_TOOLS),
        "safe_default_tools": SAFE_DEFAULT_TOOLS,
        "redlines_missing": roles,
        "redlines_ok": all(not v for v in roles.values()),
        "workspace_root": workspace_root,
        "secret_like_files": secret_files,
    }
