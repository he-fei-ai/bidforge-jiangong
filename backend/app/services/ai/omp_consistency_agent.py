"""Oh My Pi (omp) 主 Agent —— 正文生成后的全文跨节一致性比对 + 统一修复（阶段 2 旁路）。

⚠️ 定位与边界
---------------
参考工作流（``docs/reference_research/03_global_facts.md`` §8.5）把「全文一致性 Agent
修复」定义为：``global-facts.md`` + ``technical-plan.md`` 放进工作区，Agent 自行
「搜索、分段读取、建立索引、多轮编辑」，程序校验后只回写变化小节。本仓既有
``consistency_fullbook_repair.py`` 是它的**程序级等价物**（少轮整卷往返 + 四道小节闸门），
用 ``chat_with_fallback`` 单次往返；本模块补上「真正的 omp 文件工作区主 Agent」这一层：

  - 后端预写工作区文件（整卷正文走**文件**而非单帧 —— omp RPC ``maxFrameBytes=1MB``，
    整卷正文塞进 prompt 必被截断）；
  - 第 1 轮：强制用 ``read``（配合 ``grep``/``glob``）**分段跨节比对**，产出 ``FINDINGS.md``；
  - 第 2 轮：读回 FINDINGS.md 与相关章节，用 ``edit`` 对 ``technical-plan.md`` 做
    **同一主题在所有出现章节统一改成同一权威值**的最小必要修改；
  - 后端读回 ``technical-plan.md`` 作为回包文本，交给既有
    ``parse_fullbook_workspace`` + 四道闸门 + ``create_snapshot`` + ``save_repair``。

**本模块不落库、不判闸门、不碰 SSE 主链路**（``sse_handlers.py:6710-6943`` 的
``auto_consistency_repair`` 默认 pass 完全不动）。落库与 confirm/rollback 由
``routers/consistency_repair.py`` 的独立旁路端点注入 ``workspace_agent_fn`` 完成。

⚠️ 分层约束：services 层禁止 import routers（``test_services_never_import_routers``）。
⚠️ ``--no-session`` 不保证保留对话上下文，故**每轮 prompt 必须自包含**（显式列出文件与
   任务、要求从文件读状态），轮次间状态一律经工作区文件传递，不依赖模型记忆。

⚠️ 工具白名单默认 ``write,read,edit,grep,glob,bash``（放开命令执行，仅限隔离工作区内）；prompt 内再次
   声明禁止网络 / 工作区外读取 / 业务数据库访问。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass

from app.services.ai.omp_rpc import OmpRpcSession
from app.services.ai.omp_task_agent import OmpTaskAgent

logger = logging.getLogger("ai_provider.omp_consistency")

# 工作区文件名（``technical-plan.md`` 是唯一允许被 edit 的回写目标）
_FACTS_FILE = "global-facts.md"
_CONFLICTS_FILE = "conflicts.md"
_PLAN_FILE = "technical-plan.md"
_FINDINGS_FILE = "FINDINGS.md"
_TASK_FILE = "TASK.md"
_SECTION_DIR = "sections"

# 仅用于**生成只读分节索引**：把整卷正文按小节标记切开，方便 Agent 用 read/grep
# 逐节比对。闸门判定与回包解析一律由 ``consistency_fullbook_repair.parse_fullbook_workspace``
# 单一持有，本正则不参与任何"是否放行"的判据（避免同一判据第二份副本）。
_MARKER_RE = re.compile(r"^<!--\s*§:([^\s>]+)\s*-->\s*$", flags=re.M)
# 防路径穿越：分节文件名只接受安全字符，非法 id 跳过不写（绝不拼进路径）。
_SAFE_SID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass
class OmpConsistencyResult:
    """``run_consistency_check`` 的产出。"""

    workspace_text: str   # 修订后的 technical-plan.md 全文（交给 parse_fullbook_workspace）
    workspace: str        # omp 隔离工作区路径（keep_workspace=True 时供排查）
    raw_text: str = ""    # 最后一轮 omp 回复文本
    findings: str = ""    # FINDINGS.md 内容（跨节比对结论，供审计）
    rounds: int = 2       # 实际执行的 prompt 轮数
    source_file: str = _PLAN_FILE


def _build_task_file(section_count: int) -> str:
    return "\n".join(
        [
            "# 全文一致性主 Agent 任务",
            "",
            f"本方案共 {section_count} 个末级章节，全部正文已写入工作区文件。",
            "",
            "## 文件说明",
            f"- {_FACTS_FILE}：全局事实变量（**唯一权威口径**，优先遵守、不得矛盾）。",
            f"- {_CONFLICTS_FILE}：已仲裁的跨章节冲突清单（权威值 / 错误取值 / 修复指令）。",
            f"- {_PLAN_FILE}：整卷正文，**每章以独占一行 `<!-- §:章节ID -->` 起始**；这是唯一允许修改的文件。",
            f"- {_SECTION_DIR}/*.md：按章拆分的**只读索引**，便于逐节 read / grep 跨节比对，禁止修改。",
            f"- {_FINDINGS_FILE}：第 1 轮跨节比对结论（由你写出，第 2 轮读回）。",
            "",
            "## 不可逾越的红线",
            "1. bash 命令仅限 --cwd 指向的隔离工作区内执行；不得访问网络、不得读取工作区外路径、不得访问业务数据库。",
            f"2. 唯一回写目标是 {_PLAN_FILE}；不得新增/删除/重排章节，不得改动章节标题文本。",
            "3. 必须保留全部 `<!-- §:章节ID -->` 标记、章节编号、HTML 注释标记与 section id。",
            "4. 不得把非空章节改成空；不得重写与冲突无关的正文；修改范围仅限标记之间的正文。",
            "5. 同一主题在**所有**出现章节必须统一改成同一个权威值（统一修复，不能只改一处）。",
            "",
            "## 输出",
            f"最终使 {_PLAN_FILE} 成为程序可解析并回写的最终正文文件；结构校验由后端四道闸门负责。",
            "",
        ]
    )


def build_consistency_round_prompts(
    *, section_count: int, repair_nudge: str = ""
) -> list[str]:
    """构造两轮自包含 prompt（纯函数，便于护栏断言）。

    轮次间状态经 ``FINDINGS.md`` / ``technical-plan.md`` 文件传递，不依赖
    ``--no-session`` 下的对话上下文。
    """
    round1 = "\n".join(
        [
            "【第 1 轮 · 跨节比对（READ 工具执行）】",
            f"先读 {_TASK_FILE} 了解工作区与红线。",
            f"用 read 工具分段读取 {_FACTS_FILE}、{_CONFLICTS_FILE}、{_PLAN_FILE} 及 {_SECTION_DIR}/*.md；"
            "用 grep / glob 工具跨节检索同一主题在不同章节的取值。",
            "**禁止把整卷正文塞进单次输出或单帧**；必须分段读取、逐主题交叉比对。",
            "",
            "比对范围：与全局事实冲突、前后矛盾、同一信息多处表述不一致、术语/单位/编号不统一、",
            "同级章节内容重复。",
            f"把结论写入 {_FINDINGS_FILE}（用 write 工具），每条包含：主题、涉及章节 id、错误取值、"
            "权威值（以全局事实/冲突清单为准）、建议统一后的表述。",
            f"本轮**不要修改** {_PLAN_FILE}。完成后简述发现数量。",
            "",
            "bash 仅限隔离工作区内执行；禁止访问网络 / 读取工作区外路径 / 访问业务数据库。",
            "",
        ]
    )
    round2_parts = [
        "【第 2 轮 · 统一修复（READ 工具读回 + edit 最小改写）】",
        f"先读 {_TASK_FILE} 与 {_FINDINGS_FILE}（read 工具），再分段读取 {_PLAN_FILE} 中涉及的章节。",
        "",
        f"用 edit 工具**只修改** {_PLAN_FILE}：把 FINDINGS.md 中每条不一致，在所有出现章节统一改为权威值。",
        "硬性约束：",
        "- 保留全部 `<!-- §:章节ID -->` 标记、章节编号、标题、HTML 注释与 section id；不得增删/重排章节。",
        "- 只做最小必要修改，不动无关正文，不重写整章，不把非空章节改成空。",
        "- 同一主题必须全篇统一（这是「统一修复」的核心），不得只改其中一处。",
        f"- {_SECTION_DIR}/*.md 为只读索引，禁止修改。",
        "",
        "bash 仅限隔离工作区内执行；禁止访问网络 / 读取工作区外路径 / 访问业务数据库。",
    ]
    if repair_nudge:
        round2_parts += [
            "",
            "⚠️ 上一轮换包被后端结构闸门拒绝，原因如下；本轮请在修复的同时纠正该结构问题：",
            repair_nudge,
        ]
    round2_parts += ["", "完成后在最终回复中写明改动章节数与统一后的口径摘要。", ""]
    return [round1, "\n".join(round2_parts)]


def _write_workspace(
    ws: str, *, workspace_text: str, facts: str, conflicts_digest: str, section_count: int
) -> int:
    """预写工作区输入文件，返回写出的分节索引数量。

    纯文件 IO（同步、微秒级）；正文一律走文件，不进 prompt 单帧。
    """
    os.makedirs(os.path.join(ws, _SECTION_DIR), exist_ok=True)

    def _put(name: str, text: str) -> None:
        with open(os.path.join(ws, name), "w", encoding="utf-8") as f:
            f.write(text)

    _put(_FACTS_FILE, facts or "（无全局事实）")
    _put(_CONFLICTS_FILE, conflicts_digest or "（无已仲裁冲突）")
    _put(_PLAN_FILE, workspace_text)
    _put(_TASK_FILE, _build_task_file(section_count))

    # 分节只读索引：按 marker 切分，文件名经白名单过滤防路径穿越。
    markers = list(_MARKER_RE.finditer(workspace_text))
    written = 0
    for i, m in enumerate(markers):
        sid = m.group(1)
        if not _SAFE_SID_RE.fullmatch(sid):
            continue
        end = markers[i + 1].start() if i + 1 < len(markers) else len(workspace_text)
        block = workspace_text[m.end():end].strip("\n")
        with open(os.path.join(ws, _SECTION_DIR, f"{sid}.md"), "w", encoding="utf-8") as f:
            f.write(f"<!-- §:{sid} -->\n{block}\n")
        written += 1
    return written


class OmpConsistencyAgent(OmpTaskAgent):
    """全文一致性主 Agent（复用 ``OmpTaskAgent`` 配置与三道闸门）。

    用法：``await OmpConsistencyAgent().revise_workspace(...)`` 即可直接作为
    ``consistency_fullbook_repair.run_fullbook_repair`` 的 ``workspace_agent_fn``。
    """

    name = "omp_consistency"

    def check_ready(self) -> None:
        """公开预检：router 在任何 AI 调用前调用，把配置缺失转成明确 4xx。

        ``run_fullbook_repair`` 的重试循环会捕获 ``Exception`` 并重问，若不留此预检，
        「二进制缺失 / 未配模型」会被吞成「items 全 failed 但 status=completed」的假成功。
        """
        self._assert_ready()

    async def run_consistency_check(
        self,
        *,
        workspace_text: str,
        facts: str = "",
        conflicts_digest: str = "",
        section_count: int = 0,
        repair_nudge: str = "",
        system_prompt: str | None = None,
    ) -> OmpConsistencyResult:
        """在隔离工作区执行「跨节比对 → 统一修复」，读回 ``technical-plan.md``。

        **只返回回包文本**，不落库、不做闸门判定 —— 那些由调用方
        （``run_fullbook_repair``）的既有实现单一持有。
        """
        self._assert_ready()
        ws = self._workspace_root or tempfile.mkdtemp(prefix="omp_consistency_")
        os.makedirs(ws, exist_ok=True)
        n_index = _write_workspace(
            ws,
            workspace_text=workspace_text,
            facts=facts,
            conflicts_digest=conflicts_digest,
            section_count=section_count,
        )

        session = OmpRpcSession(
            self._binary,
            self._model,
            self._api_key,
            workspace=ws,
            tools=self._tools,
            rpc_timeout=self._timeout,
            system_prompt=system_prompt or self._system_prompt,
        )
        prompts = build_consistency_round_prompts(
            section_count=section_count, repair_nudge=repair_nudge
        )
        raw_text = ""
        try:
            await session.start()
            # 同一进程连续多轮，共享工作区；--no-session 下每轮 prompt 自包含。
            for prompt in prompts:
                raw_text = await session.run_prompt(prompt)
            # ⚠️ 必须在 stop()（可能清理工作区）**之前**读回回包与比对结论。
            plan_path = os.path.join(ws, _PLAN_FILE)
            with open(plan_path, "r", encoding="utf-8") as f:
                revised = f.read()
            findings = ""
            findings_path = os.path.join(ws, _FINDINGS_FILE)
            if os.path.isfile(findings_path):
                with open(findings_path, "r", encoding="utf-8") as f:
                    findings = f.read()
        finally:
            # 任何异常（RPC 超时 / 回包文件被删 / 读回失败）都必须回收子进程，
            # 否则常驻 omp 进程会随失败任务堆积。
            await session.stop(cleanup_workspace=not self._keep_workspace)
        logger.info(
            "omp 一致性主 Agent 完成：章节索引 %d 个、轮次 %d、回包 %d 字",
            n_index, len(prompts), len(revised),
        )
        return OmpConsistencyResult(
            workspace_text=revised,
            workspace=ws,
            raw_text=raw_text,
            findings=findings,
            rounds=len(prompts),
        )

    async def revise_workspace(
        self,
        *,
        workspace: str,
        facts: str = "",
        conflicts_digest: str = "",
        section_count: int = 0,
        repair_nudge: str = "",
    ) -> str:
        """``workspace_agent_fn`` 适配器：返回修订后的整卷工作区文本。"""
        result = await self.run_consistency_check(
            workspace_text=workspace,
            facts=facts,
            conflicts_digest=conflicts_digest,
            section_count=section_count,
            repair_nudge=repair_nudge,
        )
        return result.workspace_text
