"""Anthropic 兼容 Provider"""
import json
from typing import AsyncIterator

from app.services.ai.http_pool import get_async_client
from app.services.ai.providers.base import BaseProvider, normalize_messages


class AnthropicCompatibleProvider(BaseProvider):
    name = "anthropic_compatible"

    def supports_json_mode(self) -> bool:
        return False

    async def chat(self, messages: list, temperature: float | None = None,
                   json_mode: bool = False, max_tokens: int | None = None,
                   extra_body: dict | None = None, **_kwargs) -> str:
        # ✅ 移植适配：归一化 AIMessage 对象 → dict；支持参数覆盖
        messages = normalize_messages(messages)
        system = "\n".join(str(m["content"]) for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        # ✅ 修复：messages 全为 system 时 rest 为空列表，Anthropic API 会报 400，
        # 此时把 system 内容转为 user 消息兜底
        if not rest and system:
            rest = [{"role": "user", "content": system}]
            system = ""
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "messages": rest,
        }
        if extra_body:
            payload.update(extra_body)
        if system:
            payload["system"] = system
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        # ✅ 性能（P0-4）：复用进程级连接池，逐请求传入超时
        client = get_async_client(self.base_url, self.proxy_url)
        resp = await client.post(f"{self.base_url}/messages", json=payload,
                                 headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        # ✅ 记录 token 用量（Anthropic：input_tokens/output_tokens/cache_read_input_tokens）
        self._set_usage(data.get("usage"))
        # ✅ 记录结束原因：区分「正常结束(end_turn)」与「被 max_tokens 截断(max_tokens)」
        self.last_finish_reason = str(data.get("stop_reason") or "")
        # ✅ 修复：空响应/空 content 列表防护（旧实现直接 [0] 取下标会 IndexError）
        blocks = data.get("content") or []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return block["text"]
        raise RuntimeError("Anthropic 响应中无文本内容")

    async def stream(self, messages: list, temperature: float | None = None,
                     json_mode: bool = False, max_tokens: int | None = None,
                     extra_body: dict | None = None, **_kwargs) -> AsyncIterator[str]:
        """流式请求（SSE 分片）。

        ✅ 增强（AI 配置「请求方式」新增后）：
          1. 支持 temperature / max_tokens / extra_body 覆盖，与 ``chat()`` 口径一致；
             （Anthropic 不支持 response_format，json_mode 仍由调用方回退处理）
          2. 解析 ``message_start`` / ``message_delta`` 两个事件里的 usage 并**合并**
             —— 输入 token 在 message_start、输出 token 在 message_delta，
             旧实现只取 content_block_delta 的正文，流式用量恒为 0；
          3. 记录 stop_reason（end_turn / max_tokens），供上层区分截断与空响应。
        """
        messages = normalize_messages(messages)
        system = "\n".join(str(m["content"]) for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        # ✅ 修复：全 system 消息兜底（Anthropic 要求 messages 非空）
        if not rest and system:
            rest = [{"role": "user", "content": system}]
            system = ""
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "messages": rest, "stream": True,
        }
        if extra_body:
            payload.update(extra_body)
        if system:
            payload["system"] = system
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        client = get_async_client(self.base_url, self.proxy_url)
        async with client.stream("POST", f"{self.base_url}/messages",
                                 json=payload, headers=headers,
                                 timeout=self.timeout) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    evt = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if not isinstance(evt, dict):
                    continue
                etype = evt.get("type")
                if etype == "message_start":
                    # usage 先到：input_tokens / cache_read_input_tokens
                    self._merge_usage((evt.get("message") or {}).get("usage"))
                elif etype == "message_delta":
                    # 收尾事件：output_tokens 在此，stop_reason 也在此
                    self._merge_usage(evt.get("usage"))
                    stop = (evt.get("delta") or {}).get("stop_reason")
                    if stop:
                        self.last_finish_reason = str(stop)
                elif etype == "content_block_delta":
                    if piece := (evt.get("delta") or {}).get("text"):
                        yield piece
                elif etype == "error":
                    raise RuntimeError(
                        f"Anthropic 流式返回错误：{str(evt.get('error'))[:200]}")