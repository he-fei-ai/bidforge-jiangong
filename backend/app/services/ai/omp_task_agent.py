"""Oh My Pi (omp) 文件工作区 Agent —— 阶段 2 骨架：OmpTaskAgent 服务（目录生成试点）。

⚠️ 角色定位
-----------
把 omp 真正当作「文件工作区多步 Agent」使用（报告 §6.4 安全沙箱 + 阶段 2 路线）：
给定项目简报，让 omp 在**隔离工作区**内借助受控工具白名单（write/read/edit/grep/glob）
自主生成目录大纲文件，再由本服务读取、解析、规整回平台的数据模型。

它**不触碰**已历经 20+ 轮加固的目录生成主链路（sse_outline / workflows_outline_v51）；
作为旁路服务，由调用方在受控灰度下调用（settings.omp_task_enabled）。

⚠️ 安全闸门（默认关闭，与 OmpProvider 同套）
-------------------------------------------
- ``omp_task_enabled=False`` → ``generate_outline`` 抛清晰错误，调用方据此回退到主链路。
- 二进制缺失 / 未配置模型（``omp_task_model``）→ 抛清晰错误（不会拉起子进程）。
- 每个任务独立隔离工作区（``settings.omp_workspace_dir`` 或系统临时目录），与业务数据隔离。
- 工具白名单默认 ``write,read,edit,grep,glob``（**不含 bash/exec**，避免任意命令执行）。

⚠️ 已知局限（骨架范围）
-----------------------
- 解析仅覆盖 Markdown 标题树与简单 JSON；omp 若产出其它形态需扩展解析器。
- 单次任务单 omp 进程（无会话池）；目录生成多步循环可能较长，超时由 ``omp_task_timeout`` 控。
- 不对 omp 产物做语义校验（编号唯一性 / 合规）—— 这些是调用方（目录主链路）的职责。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

from app.services.ai.omp_rpc import OmpRpcSession, binary_exists
from app.services.ai.omp_safety import assert_tool_allowlist

logger = logging.getLogger("ai_provider.omp_task")


# omp 被要求写出的目录文件名（Markdown 标题树）
_OUTLINE_FILENAME = "OUTLINE.md"
# 可选的逐节说明文件
_NOTES_FILENAME = "NOTES.md"


@dataclass
class GeneratedSection:
    """解析后的目录条目（与平台目录树同构的最小字段集）。"""

    level: int
    title: str
    path: str = ""          # 形如 "1.2.3" 的编号（可选，omp 可能不写）
    description: str = ""


@dataclass
class OutlineResult:
    """OmpTaskAgent.generate_outline 的产出。"""

    sections: list[GeneratedSection] = field(default_factory=list)
    workspace: str = ""      # omp 工作区路径（keep_workspace=True 时可用于排查）
    raw_text: str = ""       # omp 最终回复文本
    notes: str = ""          # NOTES.md 内容（若有）
    source_file: str = ""    # 实际读取的产物文件名


def parse_outline_markdown(text: str) -> list[GeneratedSection]:
    """把 Markdown 标题树解析为 ``GeneratedSection`` 列表。

    支持 ``#/##/###`` 标题；标题若以 ``1.2.3`` 数字编号开头，则提取为 ``path``，
    去除编号后的文本作为 ``title``；无编号则 ``path`` 留空。
    """
    sections: list[GeneratedSection] = []
    for raw in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", raw)
        if not m:
            continue
        level = len(m.group(1))
        heading = m.group(2).strip()
        path = ""
        title = heading
        pm = re.match(r"^(\d+(?:\.\d+)*)\s*[.、]?\s*(.*)$", heading)
        if pm:
            path = pm.group(1)
            title = pm.group(2).strip() or heading
        sections.append(GeneratedSection(level=level, title=title, path=path))
    return sections


def parse_outline_json(text: str) -> list[GeneratedSection]:
    """解析嵌套 / 扁平 JSON 为 ``GeneratedSection``（辅助形态）。

    支持：``[{"title","level","path","children":[...]}]`` 或
    ``{"title","children":[...]}`` 或 ``{"sections":[...]}``。
    """
    data = json.loads(text)
    out: list[GeneratedSection] = []

    def walk(node, level: int, prefix: str) -> None:
        if isinstance(node, dict):
            title = str(node.get("title") or node.get("name") or "").strip()
            p = str(node.get("path") or prefix).strip()
            # ✅ R231（2026-11-13 · P2 · 容器根不是章节）：dict-root 容器形态
            #    （带 sections/children 键）以 level=0 起步，其 title 是文档标题
            #    而非章节——与 ``{"sections":[...]}`` 容器语义一致，不得产出
            #    level=0 条目。旧行为在 title 存在时产出 level=0，下游
            #    ``fold_generated_sections_to_tree`` 的 (0, root) 哨兵被
            #    ``0 >= 0`` 弹空 ⇒ IndexError（实测探针：两种 dict-root 形态
            #    均崩，端点 500）。容器根 title 在此丢弃；纯 dict（无
            #    children/sections 键）走 else 分支 level=1，行为不变。
            if title and level >= 1:
                out.append(GeneratedSection(level=level, title=title, path=p))
            children = node.get("children") or node.get("sections") or []
            for i, c in enumerate(children):
                child_prefix = f"{p}.{i + 1}" if p else str(i + 1)
                walk(c, level + 1, child_prefix)
        elif isinstance(node, str) and node.strip():
            out.append(GeneratedSection(level=level, title=node.strip(), path=prefix))

    if isinstance(data, list):
        for i, n in enumerate(data):
            walk(n, 1, str(i + 1))
    elif isinstance(data, dict):
        if "sections" in data or "children" in data:
            walk(data, 0, "")
        else:
            walk(data, 1, "1")
    return out


def _build_outline_prompt(
    *, brief: str, project_type: str = "", industry: str = "", extra: str = ""
) -> str:
    """构造发给 omp 的目录生成指令（含工作区 / 工具约束，强化安全沙箱）。"""
    parts = [
        "你是一名资深的招投标「专项施工方案」目录大纲设计专家。",
        "任务：根据下方项目简报，设计一份完整、专业、符合行业规范的方案目录大纲。",
        "",
        "要求：",
        "1. 使用 Markdown 标题层级表达目录结构：# 表示章，## 表示节，### 表示子节（最多到 ###）。",
        "2. 章/节编号采用『1』『1.1』『1.1.1』形式，写在标题最前面。",
        "3. 覆盖专项施工方案的典型要素：工程概况、编制依据、施工部署、施工进度计划、"
        "资源配置、主要施工工艺、质量保证、安全文明施工、应急预案、环保与节能等。",
        "4. 只在本工作区内创建并写入文件 OUTLINE.md（上述标题树）。不要修改工作区外的任何文件。",
        "5. 不要执行任何 shell 命令、不要访问网络、不要读取工作区外的路径。",
    ]
    if project_type:
        parts.append(f"项目类型：{project_type}")
    if industry:
        parts.append(f"行业/专业：{industry}")
    if extra:
        parts.append(f"额外要求：{extra}")
    parts.append("")
    parts.append("项目简报：")
    parts.append(brief)
    return "\n".join(parts)


class OmpTaskAgent:
    """文件工作区多步 Agent（阶段 2 骨架）：以 omp 生成目录大纲为试点。

    用法：``await OmpTaskAgent().generate_outline(brief=...)``。默认从 settings 读配置；
    构造参数可覆盖（便于测试 / 显式调用）。
    """

    name = "omp_task"

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str | None = None,
        tools: str | None = None,
        workspace_root: str | None = None,
        timeout: int | None = None,
        keep_workspace: bool | None = None,
        system_prompt: str | None = None,
        enabled: bool | None = None,
        binary: str | None = None,
    ):
        from app.config import settings  # 延迟导入，避免模块级循环依赖

        self._enabled = bool(enabled if enabled is not None else getattr(settings, "omp_task_enabled", False))
        self._binary = (binary or getattr(settings, "omp_binary_path", "") or "omp").strip() or "omp"
        self._model = (model or getattr(settings, "omp_task_model", "") or "").strip()
        self._tools = (
            tools or getattr(settings, "omp_task_tools", "") or "write,read,edit,grep,glob"
        ).strip()
        self._workspace_root = (workspace_root or getattr(settings, "omp_workspace_dir", "") or "").strip()
        self._timeout = int(timeout if timeout is not None else getattr(settings, "omp_task_timeout", 300) or 300)
        self._keep_workspace = bool(
            keep_workspace if keep_workspace is not None else getattr(settings, "omp_task_keep_workspace", False)
        )
        self._api_key = api_key
        self._system_prompt = system_prompt

    def _assert_ready(self) -> None:
        """三道安全闸门（所有 omp 任务类型的**单一事实源**）。

        未启用 / 二进制缺失 / 未配置模型均抛清晰错误，且不拉起子进程。
        ``OmpConsistencyAgent`` 等子任务复用本方法，避免闸门判据分化。
        """
        if not self._enabled:
            raise RuntimeError(
                "OmpTaskAgent 未启用（settings.omp_task_enabled=False）；请回退到平台既有主链路"
            )
        if not binary_exists(self._binary):
            raise RuntimeError(
                f"omp 可执行文件不存在：{self._binary}；请安装 omp 或设置 settings.omp_binary_path"
            )
        if not self._model:
            raise RuntimeError(
                "OmpTaskAgent 未配置模型（settings.omp_task_model 为空）；omp 需指向真实 LLM provider"
            )
        # ✅ R235（2026-11-13 · P1 · 沙箱铁律 fail-closed）：工具白名单闸门。
        #    此前本方法只有三道闸门（启用/二进制/模型），**不校验工具白名单** ⇒
        #    ① 白名单含 bash/exec/eval 时直接透传给 omp（违反 doc §8「禁任意命令」）；
        #    ② 白名单为空时 omp 收不到 --tools ⇒ 放开**全部**内置工具（含 bash）。
        #    判据单一事实源：``app.services.ai.omp_safety``（禁命令 + 禁网 + 空白名单）。
        #    拦在**拉起子进程之前**（fail-closed），不靠事后诊断才发现。
        try:
            assert_tool_allowlist(self._tools)
        except ValueError as exc:
            raise RuntimeError(f"omp 工具白名单不安全：{exc}") from exc

    def check_ready(self) -> None:
        """公开预检：router 在任何 AI 调用前把配置缺失转成明确 4xx。

        与 ``OmpConsistencyAgent.check_ready`` 同口径，统一委托
        ``_assert_ready()`` 单一事实源；避免「配置不全被编排层重试循环吞成
        completed 但任务全 paused」的假成功（阶段 3 收口）。
        """
        self._assert_ready()

    async def generate_outline(
        self,
        *,
        brief: str,
        project_type: str = "",
        industry: str = "",
        extra_instructions: str = "",
        system_prompt: str | None = None,
    ) -> OutlineResult:
        """让 omp 在隔离工作区生成目录大纲，读回并解析为 ``OutlineResult``。

        安全闸门：未启用 / 二进制缺失 / 未配置模型 均抛清晰错误（不拉起子进程）。
        """
        self._assert_ready()
        ws = self._workspace_root or tempfile.mkdtemp(prefix="omp_task_")
        os.makedirs(ws, exist_ok=True)
        session = OmpRpcSession(
            self._binary,
            self._model,
            self._api_key,
            workspace=ws,
            tools=self._tools,
            rpc_timeout=self._timeout,
            system_prompt=system_prompt or self._system_prompt,
        )
        prompt = _build_outline_prompt(
            brief=brief, project_type=project_type, industry=industry, extra=extra_instructions
        )
        try:
            await session.start()
            raw_text = await session.run_prompt(prompt)
        except Exception:
            await session.stop(cleanup_workspace=not self._keep_workspace)
            raise

        sections, source_file, notes = self._read_workspace(ws)
        if not sections:
            await session.stop(cleanup_workspace=not self._keep_workspace)
            raise RuntimeError(
                f"omp 未在工作区生成可解析的目录文件（期望 {_OUTLINE_FILENAME}）；"
                f"omp 最终回复（截断）：{raw_text[:200]}"
            )
        await session.stop(cleanup_workspace=not self._keep_workspace)
        return OutlineResult(
            sections=sections,
            workspace=ws,
            raw_text=raw_text,
            notes=notes,
            source_file=source_file,
        )

    # 别名：语义更贴近「目录」概念
    generate_directory = generate_outline

    def _read_workspace(self, ws: str) -> tuple[list[GeneratedSection], str, str]:
        """读取 omp 产物：优先 OUTLINE.md，退路为工作区内首个 .md/.json。"""
        outline_path = os.path.join(ws, _OUTLINE_FILENAME)
        notes_path = os.path.join(ws, _NOTES_FILENAME)
        sections: list[GeneratedSection] = []
        source_file = ""
        if os.path.isfile(outline_path):
            with open(outline_path, "r", encoding="utf-8") as f:
                sections = parse_outline_markdown(f.read())
            source_file = _OUTLINE_FILENAME
        else:
            for fn in sorted(os.listdir(ws)):
                if fn == _NOTES_FILENAME:
                    # ✅ R231（2026-11-13 · P2 · 元文件不得当目录解析）：NOTES.md 是
                    #    omp 的备注元文件（下方 notes 字段单独读回它）。OUTLINE.md
                    #    缺失时的「首个 .md/.json」回退若不排除它，sorted() 把大写
                    #    开头的 NOTES.md 排在任何小写产物文件之前 ⇒ 备注散文
                    #    （「工作备忘」「遗留问题」…）被当章节标题解析入库
                    #    （实测探针：NOTES.md + outline.json 并存时 NOTES.md 胜出）。
                    #    仅 NOTES.md 存在时回到「无可解析目录」RuntimeError 主语义。
                    continue
                if not fn.lower().endswith((".md", ".json")):
                    continue
                fp = os.path.join(ws, fn)
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        t = f.read()
                except Exception:
                    continue
                if fn.lower().endswith(".json"):
                    try:
                        sections = parse_outline_json(t)
                        source_file = fn
                        break
                    except Exception:
                        continue
                sections = parse_outline_markdown(t)
                source_file = fn
                break
        notes = ""
        if os.path.isfile(notes_path):
            with open(notes_path, "r", encoding="utf-8") as f:
                notes = f.read()
        return sections, source_file, notes
