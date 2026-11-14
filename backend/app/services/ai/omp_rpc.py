"""omp (oh-my-pi) RPC 客户端：与 ``omp --mode rpc`` 子进程的 NDJSON 协议通信（共享模块）。

阶段 0 实测确认的协议事实（见 docs/集成oh-my-pi_OMP_AGENT可行性报告_20261112.md 第 10 章）：
  - 启动即发 ``ready`` 握手帧（protocolVersion / maxFrameBytes / maxReassembledFrameBytes）；
  - ``prompt`` 受理返回 ``response{command:prompt,success:true}``（仅是受理确认，不是结束）；
  - 结束标记为 ``prompt_result``（或 ``error``）；
  - omp 始终以 ``stream:true`` 请求其底层模型 —— 这是 omp→模型 这一侧的协议，RPC 侧仍是
    简单的逐行 NDJSON。
本模块被 OmpProvider（对话模型代理）与 OmpTaskAgent（文件工作区 Agent）共用，避免重复实现
进程启动 / 握手 / 帧读取 / 失效重启。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from typing import Callable

from app.services.ai.omp_safety import assert_tool_allowlist

logger = logging.getLogger("ai_provider.omp_rpc")


def _try_json(line: bytes) -> dict | None:
    """把一行 stdout 解析为 JSON 对象；非 JSON（日志等）返回 None。"""
    text = line.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_assistant_text(frames: list[dict]) -> str:
    """从 omp RPC 帧序列中提取助手最终文本。

    兼容两种形态：
      - ``message_start`` 携带 ``message.content``（parts 列表）；
      - ``message_update`` 携带顶层 ``content``（增量 parts）；
      - ``prompt_result`` 可能再次携带最终 ``message``。
    按顺序拼接所有 ``type==text`` 的片段；工具调用帧（无 text）被忽略。
    """
    chunks: list[str] = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        candidates = []
        if isinstance(frame.get("message"), dict):
            candidates.append(frame["message"].get("content"))
        if frame.get("content") is not None:
            candidates.append(frame.get("content"))
        for content in candidates:
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        chunks.append(str(part.get("text") or ""))
    return "".join(chunks)


def binary_exists(binary: str) -> bool:
    """判断 omp 二进制是否可被执行（绝对/相对路径看文件，否则看 PATH）。"""
    if os.path.isabs(binary) or os.path.split(binary)[1]:
        if os.path.sep in binary or (os.altsep and os.altsep in binary):
            return os.path.isfile(binary)
    return shutil.which(binary) is not None


class OmpRpcSession:
    """一个 omp ``--mode rpc`` 子进程会话（按调用方需求复用或单任务独占）。

    负责握手、发 prompt、读帧直到 ``prompt_result``，并自动重启失效进程。
    每个会话绑定一个独立 ``--cwd`` 工作区；文件工作区 Agent 可在 ``run_prompt``
    结束后直接从 ``workspace`` 读取 omp 生成的产物。
    """

    def __init__(
        self,
        binary: str,
        model: str,
        api_key: str,
        *,
        workspace: str,
        tools: str = "",
        rpc_timeout: int = 120,
        system_prompt: str | None = None,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self._binary = binary
        self._model = model
        self._api_key = api_key
        self._workspace = workspace
        self._tools = tools
        self._rpc_timeout = rpc_timeout
        self._system_prompt = system_prompt
        self._extra_args = extra_args or []
        # ✅ R237（2026-11-13 · 单轨重构）：Provider 授权以**环境变量**注入子进程。
        #    此前 OmpAgentExecutor 传 ``env=`` 而本类**不接收**该参数 ⇒ 授权注入
        #    实际从未生效（首次真实调用即 TypeError）。此处补齐：``env`` 与父进程
        #    环境**合并**后交给子进程（同名键以注入值为准）。
        self._env = {str(k): str(v) for k, v in (env or {}).items() if str(k).strip()}
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def workspace(self) -> str:
        return self._workspace

    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        # ✅ R235（2026-11-13 · P1 · 纵深防御）：拉起子进程**之前**校验工具白名单。
        #    Provider 角色刻意不挂工具（tools="" ⇒ 不传 --tools），故只在**显式给了**
        #    白名单时校验内容；Agent 角色的空白名单由 OmpTaskAgent 的闸门拦截。
        #    判据单一事实源：``app.services.ai.omp_safety``。
        if self._tools:
            try:
                assert_tool_allowlist(self._tools)
            except ValueError as exc:
                raise RuntimeError(f"omp 工具白名单不安全：{exc}") from exc
        args = [
            self._binary,
            "--mode", "rpc",
            "--no-ui",
            "--no-session",
            "--cwd", self._workspace,
            "--model", self._model,
            "--api-key", self._api_key,
        ]
        if self._tools:
            args += ["--tools", self._tools]
        if self._system_prompt:
            args += ["--system-prompt", self._system_prompt]
        args += self._extra_args
        # 授权注入：父进程环境 + 注入项（同名以注入为准）。
        proc_env = dict(os.environ)
        proc_env.update(self._env)
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )
        # 等待 ready 握手帧
        try:
            async with asyncio.timeout(self._rpc_timeout):
                while True:
                    line = await self._proc.stdout.readline()
                    if not line:
                        raise RuntimeError("omp 进程启动失败（未输出 ready 即退出）")
                    obj = _try_json(line)
                    if obj and obj.get("type") == "ready":
                        logger.info(
                            "omp RPC 握手成功：protocolVersion=%s, maxFrameBytes=%s",
                            obj.get("protocolVersion"),
                            obj.get("maxFrameBytes"),
                        )
                        return
        except asyncio.TimeoutError:
            await self._kill()
            raise RuntimeError(f"omp 未在 {self._rpc_timeout}s 内发送 ready 握手帧")

    async def run_prompt(self, text: str, on_frame: Callable[[dict], None] | None = None) -> str:
        """发送 prompt 并消费 NDJSON 帧直到 ``prompt_result``。

        ⚠️ R237（2026-11-13 · 单轨重构）：新增 ``on_frame`` **流式帧回调** ——
        执行器据此做流式输出解析（帧计数 / 类型统计），业务层无需解析裸协议。
        """
        async with self._lock:
            if not self.alive():
                await self.start()
            prompt_id = "p" + uuid.uuid4().hex
            self._send({"id": prompt_id, "type": "prompt", "message": text})
            frames: list[dict] = []
            try:
                async with asyncio.timeout(self._rpc_timeout):
                    while True:
                        line = await self._proc.stdout.readline()
                        if not line:
                            raise RuntimeError("omp 进程在任务中途退出")
                        obj = _try_json(line)
                        if obj is None:
                            continue
                        frames.append(obj)
                        if on_frame is not None:
                            try:
                                on_frame(obj)
                            except Exception:  # noqa: BLE001 — 回调异常不得影响协议消费
                                logger.debug("run_prompt: on_frame 回调异常（忽略）")
                        t = obj.get("type")
                        if t == "prompt_result":
                            return _extract_assistant_text(frames)
                        if t == "error":
                            reason = obj.get("message") or obj.get("error") or "未知错误"
                            raise RuntimeError(f"omp 返回错误：{reason}")
            except asyncio.TimeoutError:
                await self._kill()
                raise RuntimeError(f"omp 单次 prompt 超过 {self._rpc_timeout}s 未返回 prompt_result")

    def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    async def _kill(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except Exception:  # noqa: BLE001 - 进程正在退出时 stdin 可能已关闭
            logger.debug("_kill: stdin close ignored")
        try:
            self._proc.kill()
            await self._proc.wait()
        except Exception:  # noqa: BLE001 - 进程可能已自然终止
            logger.debug("_kill: kill/wait ignored")
        self._proc = None

    async def stop(self, *, cleanup_workspace: bool = False) -> None:
        await self._kill()
        if cleanup_workspace:
            try:
                if os.path.isdir(self._workspace):
                    shutil.rmtree(self._workspace, ignore_errors=True)
            except Exception:  # noqa: BLE001 - 目录可能已被外部清理
                logger.debug("stop: workspace cleanup ignored")
