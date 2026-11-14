"""Oh My Pi (omp) Provider —— 阶段 1 骨架：把 omp 作为降级链里的「对话模型代理」。

⚠️ 角色定位（与阶段 2 区分）
---------------------------
本 Provider 让 omp 以 **纯对话 / JSON** 形态进入现有 provider 降级链，扮演一个
「由 omp 多步 Agent 循环背书的模型后端」。它**默认不挂工具**（``settings.omp_tools``
为空），因此 omp 在此角色下只做正常的文本 / JSON 补全，不会触碰文件系统。

真正的「文件工作区多步 Agent」角色（辅助目录 / 正文 / 审核修复）属于**阶段 2** 的
``OmpTaskAgent`` 服务（文件工作区模式 + 受控工具白名单 + 工作区隔离），与本文件解耦。

⚠️ 安全闸门（默认关闭）
-----------------------
1. ``settings.omp_provider_enabled`` 必须为 ``True`` 才会真正可用；否则 ``chat()`` 抛
   清晰错误，降级链自动跳到下一候选，对现有链路**零影响**。
2. omp 二进制缺失时同样抛错跳过。
3. ``--cwd`` 指向独立的隔离工作区（``settings.omp_workspace_dir`` 或系统临时目录），
   与业务数据库 / 项目资料严格隔离。

⚠️ 已知局限（骨架范围，留待阶段 2 / 生产加固）
-------------------------------------------
- ``stream()`` 当前整块返回（omp RPC 本身是完整结果）；真正的增量流式需解析 delta，
  在 OmpTaskAgent 阶段统一实现。
- ``json_mode`` 通过提示词指令约束 omp 输出纯 JSON，omp 偶尔仍会包裹 markdown 围栏，
  生产需在 collect_json_response 侧做容错清洗（或阶段 2 改为结构化工具回收）。
- 单实例单 omp 进程 + 串行锁；高并发需做会话池（TODO）。
- token 用量（``last_usage``）omp RPC 不回传，保持全 0，不影响主流程。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any, AsyncIterator

# 向后兼容：测试从本模块导入这两个解析/工具函数（实现已上移到共享模块 omp_rpc）
from app.services.ai.omp_rpc import (
    OmpRpcSession,
    _extract_assistant_text,  # noqa: F401
    _try_json,  # noqa: F401
    binary_exists,
)
from app.services.ai.providers.base import BaseProvider, normalize_messages

logger = logging.getLogger("ai_provider.omp")


def _content_to_text(content: Any) -> str:
    """把 OpenAI 风格 message content（str 或 parts 列表）压成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text":
                    chunks.append(str(part.get("text") or ""))
                elif "text" in part:
                    chunks.append(str(part.get("text") or ""))
        return "\n".join(c for c in chunks if c)
    return str(content)


def _render_prompt(messages: list, json_mode: bool = False) -> str:
    """把 OpenAI 风格 messages 渲染成单一纯文本 prompt 喂给 omp。

    系统指令与多轮对话被平铺为可读文本；``json_mode`` 时追加「仅返回 JSON」约束。
    """
    system_parts: list[str] = []
    conv: list[str] = []
    for m in normalize_messages(messages):
        role = str(m.get("role") or "user")
        text = _content_to_text(m.get("content"))
        if role == "system":
            system_parts.append(text)
        else:
            conv.append(f"{role.capitalize()}: {text}")
    blocks: list[str] = []
    if system_parts:
        blocks.append("System instructions:\n" + "\n".join(system_parts))
    if conv:
        blocks.append("Conversation:\n" + "\n".join(conv))
    if json_mode:
        blocks.append(
            "IMPORTANT: Respond with a single valid JSON object only. "
            "Do not wrap it in markdown code fences. Do not include any explanatory text."
        )
    return "\n\n".join(blocks)


class OmpProvider(BaseProvider):
    """把 omp 接入 provider 降级链的 Stage-1 骨架实现。

    仅实现 provider 契约的 ``chat()`` / ``stream()``；底层通过共享 ``OmpRpcSession``（omp_rpc 模块）
    驱动 omp 的 RPC 协议（stdio NDJSON）。默认关闭，需 ``settings.omp_provider_enabled``。
    """

    name = "omp"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        max_tokens: int = 8192,
        temperature: float = 0.7,
        timeout: int = 60,
        **_kw,
    ):
        # omp 不使用 base_url（走本地 RPC），空串亦可；其余沿用基类契约。
        super().__init__(
            api_key=api_key,
            base_url=base_url or "",
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        from app.config import settings  # 延迟导入，避免模块级循环依赖

        self._enabled = bool(getattr(settings, "omp_provider_enabled", False))
        self._binary = (getattr(settings, "omp_binary_path", "") or "omp").strip() or "omp"
        self._tools = (getattr(settings, "omp_tools", "") or "").strip()
        ws_root = (getattr(settings, "omp_workspace_dir", "") or "").strip()
        self._rpc_timeout = int(getattr(settings, "omp_rpc_timeout", 120) or 120)
        self._workspace_root = ws_root
        self._session: OmpRpcSession | None = None
        self._session_is_temp: bool = False
        self._session_lock = asyncio.Lock()

    # ---- 能力声明 ----------------------------------------------------------
    def supports_vision(self) -> bool:
        # omp 在此 Provider 角色下不是直接的视觉端点（视觉应走真实多模态厂商）。
        return False

    # ---- 会话管理 ----------------------------------------------------------
    async def _get_session(self) -> OmpRpcSession:
        if not self._enabled:
            raise RuntimeError(
                "OmpProvider 未启用（settings.omp_provider_enabled=False）；不参与降级链"
            )
        if not binary_exists(self._binary):
            raise RuntimeError(
                f"omp 可执行文件不存在：{self._binary}；请在 PATH 中安装 omp 或设置 settings.omp_binary_path"
            )
        async with self._session_lock:
            if self._session is None or not self._session.alive():
                ws = self._workspace_root or tempfile.mkdtemp(prefix="omp_ws_")
                os.makedirs(ws, exist_ok=True)
                self._session_is_temp = not self._workspace_root
                self._session = OmpRpcSession(
                    self._binary,
                    self.model,
                    self.api_key,
                    workspace=ws,
                    tools=self._tools,
                    rpc_timeout=self._rpc_timeout,
                )
                await self._session.start()
            return self._session

    async def _invalidate_session(self) -> None:
        async with self._session_lock:
            if self._session is not None:
                # 仅当用户未配置固定工作区（即本实例用临时目录）时才清理，
                # 避免误删 omp_workspace_dir 这个可能被复用的目录。
                await self._session.stop(cleanup_workspace=self._session_is_temp)
                self._session = None

    async def aclose(self) -> None:
        await self._invalidate_session()

    # ---- provider 契约 -----------------------------------------------------
    async def chat(
        self,
        messages: list,
        temperature: float | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
        **_kwargs,
    ) -> str:
        session = await self._get_session()
        prompt = _render_prompt(messages, json_mode=json_mode)
        try:
            text = await session.run_prompt(prompt)
        except Exception:
            # 任一次失败都让会话失效，下次调用重建，避免复用半死进程。
            await self._invalidate_session()
            raise
        if not (text or "").strip():
            # 与 OpenAICompatibleProvider 同口径：空内容必须抛错，否则会被当成功、
            # 既不降级也不重试。
            raise RuntimeError("omp 返回空内容（finish_reason 未知）；视为失败触发降级")
        self.last_finish_reason = "stop"
        return text

    async def stream(
        self,
        messages: list,
        temperature: float | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
        extra_body: dict | None = None,
        **_kwargs,
    ) -> AsyncIterator[str]:
        # 骨架：整块返回（omp RPC 为完整结果）。真正增量流式留待阶段 2。
        text = await self.chat(
            messages,
            temperature=temperature,
            json_mode=json_mode,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        yield text
