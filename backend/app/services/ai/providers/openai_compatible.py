"""OpenAI 兼容 Provider（覆盖 OpenAI/DeepSeek/SiliconFlow/通义/智谱/Agnes）"""
import json
import logging
from typing import AsyncIterator

import httpx

from app.services.ai.http_pool import get_async_client
from app.services.ai.providers.base import BaseProvider, normalize_messages

logger = logging.getLogger("ai_provider")


class OpenAICompatibleProvider(BaseProvider):
    name = "openai_compatible"

    @staticmethod
    def _ensure_user_message(messages: list) -> list:
        """Agnes 等厂商要求 messages 中必须存在 role=user 的消息，否则返回 400"""
        if not any(m.get("role") == "user" for m in messages):
            messages = list(messages) + [{"role": "user", "content": "请按系统提示执行任务。"}]
        return messages

def _is_response_format_unsupported(status: int, body: str) -> bool:
    """检测厂商是否不支持 response_format 参数（参考 OpenBidKit isResponseFormatUnsupported）。

    部分平台（旧版 qwen、glm、本地 Ollama 模型等）返回 400 + 包含 response_format
    字样的错误，去掉该字段后用普通文本请求即可（系统提示词已约束 JSON 格式）。
    命中后自动降级重试，避免整轮 AI 调用白烧。
    """
    if status != 400:
        return False
    low = (body or "").lower()
    if "response_format" not in low:
        return False
    return any(marker in low for marker in (
        "not supported", "does not support", "not support", "unsupported",
        "unknown parameter", "invalid parameter", "must be",
    ))


class OpenAICompatibleProvider(BaseProvider):
    name = "openai_compatible"

    @staticmethod
    def _ensure_user_message(messages: list) -> list:
        """Agnes 等厂商要求 messages 中必须存在 role=user 的消息，否则返回 400"""
        if not any(m.get("role") == "user" for m in messages):
            messages = list(messages) + [{"role": "user", "content": "请按系统提示执行任务。"}]
        return messages

    async def chat(self, messages: list, temperature: float | None = None,
                   json_mode: bool = False, max_tokens: int | None = None,
                   extra_body: dict | None = None, **_kwargs) -> str:
        # ✅ 移植适配：归一化 AIMessage 对象 → dict；支持参数覆盖
        messages = normalize_messages(messages)
        payload = {
            "model": self.model,
            "messages": self._ensure_user_message(messages),
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if extra_body:
            payload.update(extra_body)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        # ✅ 性能（P0-4）：复用进程级连接池，避免每次调用重建 TCP/TLS；
        #    单次请求超时按 self.timeout 逐请求传入（httpx 支持 per-request timeout）。
        client = get_async_client(self.base_url, self.proxy_url)
        # ✅ 2026-09-22 增强（参考 OpenBidKit）：response_format 不支持时自动去掉
        #    重试一次（部分厂商 400 + response_format 字样错误，去掉后用普通文本即可）。
        for attempt in (0, 1):
            resp = await client.post(f"{self.base_url}/chat/completions", json=payload,
                                     headers=headers, timeout=self.timeout)
            if resp.status_code >= 400:
                body_text = resp.text[:800]
                if (attempt == 0 and json_mode
                        and _is_response_format_unsupported(resp.status_code, body_text)):
                    logger.warning(
                        "厂商 %s 不支持 response_format（HTTP 400），去掉后重试（将改用普通文本输出 JSON）",
                        self.name)
                    payload.pop("response_format", None)
                    json_mode = False
                    continue
                raise RuntimeError(f"HTTP {resp.status_code}: {body_text}")
            resp.raise_for_status()
            break
        data = resp.json()
        # ✅ 记录 token 用量（供审计统计消费）
        self._set_usage(data.get("usage"))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"厂商返回空 choices: {str(data)[:200]}")
        self.last_finish_reason = str(choices[0].get("finish_reason") or "")
        # ✅ 修复：content 可能为 None（tool_calls/内容过滤时），避免 None 传给上层
        content = (choices[0].get("message") or {}).get("content")
        if not (content or "").strip():
            # ✅ 空内容必须抛错而不是返回空串：返回空串会被上层当「成功」，
            #    导致既不降级到下一 Provider 也不重试，章节直接标失败
            #    （用户看到「AI 返回空内容」且无任何自愈）。
            #    常见根因：
            #    a) 推理模型（deepseek-reasoner 等）把 max_tokens 全部耗在
            #       reasoning_content 上 → content 为 null、finish_reason=length；
            #    b) 内容过滤 / 厂商抖动返回 200 + 空 content。
            _msg = choices[0].get("message") or {}
            _finish = choices[0].get("finish_reason") or "unknown"
            _reasoning = str(_msg.get("reasoning_content")
                             or _msg.get("reasoning") or "").strip()
            if _reasoning and _finish == "length":
                _eff_max = max_tokens or self.max_tokens
                raise RuntimeError(
                    f"推理模型把 max_tokens({_eff_max}) 全部消耗在思考过程"
                    f"（finish_reason=length），正文为空：请调大 max_tokens"
                    f" 或改用非推理模型")
            raise RuntimeError(
                f"厂商返回空 content（finish_reason={_finish}）: {str(data)[:200]}")
        return content

    async def stream(self, messages: list, temperature: float | None = None,
                     json_mode: bool = False, max_tokens: int | None = None,
                     extra_body: dict | None = None, **_kwargs) -> AsyncIterator[str]:
        """流式请求（SSE 分片）。

        ✅ 增强（AI 配置「请求方式」新增后）：
          1. 支持 temperature / max_tokens / json_mode / extra_body 覆盖，
             与 ``chat()`` 参数口径一致 —— 否则流式调用只能吃 Provider 构造时的
             固定参数，「表单里改的 max_tokens / 温度对流式不生效」，
             同一份配置在两种请求方式下行为不一致；
          2. 透传 ``stream_options.include_usage``：让厂商在最后一个分片回传
             usage，流式调用的 token 用量统计不再恒为 0。
             该字段是可选参数，部分平台不认识并返回 400 —— 此时自动去掉后
             重试一次，不因为一个可选字段把「流式请求」整个判死；
          3. 记录 ``finish_reason``，供上层区分「被 max_tokens 截断」与「真·空响应」。
        """
        messages = normalize_messages(messages)
        messages = self._ensure_user_message(messages)
        payload = {
            "model": self.model, "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if extra_body:
            payload.update(extra_body)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        client = get_async_client(self.base_url, self.proxy_url)
        # ✅ 2026-09-22 增强：降级链 —— stream_options 不支持 → 去掉；
        #    response_format 不支持 → 去掉（参考 OpenBidKit isResponseFormatUnsupported）。
        # 两个降级都尝试，最多各重试一次。
        _stream_downgraded = set()
        for attempt in (0, 1, 2):
            try:
                async for piece in self._iter_stream(client, payload, headers):
                    yield piece
                return
            except httpx.HTTPStatusError as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                body_text = ""
                try:
                    body_text = str(getattr(getattr(e, "response", None), "text", "") or "")[:800]
                except Exception:
                    pass
                if attempt < 2 and status == 400:
                    # stream_options 不支持
                    if "stream_options" in payload and "stream_options" not in _stream_downgraded:
                        logger.warning(
                            "厂商不支持 stream_options（HTTP 400），去掉该字段重试流式请求")
                        payload.pop("stream_options", None)
                        _stream_downgraded.add("stream_options")
                        continue
                    # response_format 不支持
                    if ("response_format" in payload and json_mode
                            and "response_format" not in _stream_downgraded
                            and _is_response_format_unsupported(status, body_text)):
                        logger.warning(
                            "厂商 %s 不支持流式 response_format（HTTP 400），去掉后重试",
                            self.name)
                        payload.pop("response_format", None)
                        json_mode = False
                        _stream_downgraded.add("response_format")
                        continue
                raise

    async def _iter_stream(self, client, payload: dict,
                           headers: dict) -> AsyncIterator[str]:
        """单次 SSE 请求并逐片产出正文（不做任何重试/降级决策）。"""
        async with client.stream("POST", f"{self.base_url}/chat/completions",
                                 json=payload, headers=headers,
                                 timeout=self.timeout) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                # usage 通常只在最后一个分片出现（此时 choices 为空数组），
                # 旧实现 `["choices"][0]` 直接 IndexError 把它吞掉 → 用量恒为 0。
                if isinstance(data, dict) and data.get("usage"):
                    self._set_usage(data["usage"])
                choices = data.get("choices") or [] if isinstance(data, dict) else []
                if not choices:
                    continue
                choice = choices[0] or {}
                if choice.get("finish_reason"):
                    self.last_finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                if piece := delta.get("content"):
                    yield piece