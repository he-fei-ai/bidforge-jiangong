"""Provider 基类"""
from dataclasses import dataclass
from typing import Any, AsyncIterator

# 已知支持图片/视觉能力的模型名（小写匹配）
_VISION_KEYWORDS = (
    "vision", "gpt-4o", "gpt-4v", "gpt-4-turbo", "gpt-4.1",
    "claude-3", "claude-sonnet-4", "claude-opus-4",
    "gemini", "qwen-vl", "qwen2-vl", "glm-4v", "glm-4v-plus",
    "doubao-vision", "deepseek-vl", "yi-vision",
)


@dataclass
class AIMessage:
    """统一消息对象（自招投标方案平台移植，image_engine 依赖）

    与 dict 形式消息等价：providers 在发送前会通过 normalize_messages 统一转换。
    """
    role: str = "user"
    content: Any = ""

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


def normalize_messages(messages: list) -> list[dict]:
    """把 AIMessage 对象 / dict 混合列表统一转为 dict 列表

    image_engine 等移植模块会传入 AIMessage 对象列表，
    而 httpx payload 需要 dict；这里做一次兼容转换。
    """
    out: list[dict] = []
    for m in messages:
        if isinstance(m, AIMessage):
            out.append(m.to_dict())
        elif isinstance(m, dict):
            out.append(m)
        elif hasattr(m, "role") and hasattr(m, "content"):
            out.append({"role": m.role, "content": m.content})
        else:
            out.append({"role": "user", "content": str(m)})
    return out


class BaseProvider:
    name = "base"

    def __init__(self, api_key: str, base_url: str, model: str,
                 max_tokens: int = 8192, temperature: float = 0.7,
                 timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        # ✅ 最近一次调用的 token 用量（标准化三字段），供审计日志统计消费。
        #    厂商未返回 usage 时保持全 0，不影响主流程。
        self.last_usage: dict = {
            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
        }
        # 最近一次调用的结束原因（OpenAI: finish_reason / Anthropic: stop_reason）。
        # 用于区分「正常结束」与「被 max_tokens 截断」—— 推理模型在流式下
        # 只回 reasoning 不回正文时，finish_reason=length 是唯一线索。
        self.last_finish_reason: str = ""

    def _set_usage(self, usage: dict | None) -> None:
        """把各厂商不同命名的 usage 字段归一化为统一三字段。

        兼容：
          - OpenAI / OpenAI 兼容：prompt_tokens / completion_tokens /
            prompt_tokens_details.cached_tokens
          - Anthropic：input_tokens / output_tokens / cache_read_input_tokens
          - DeepSeek：prompt_cache_hit_tokens
        """
        u = usage or {}
        if not isinstance(u, dict):
            return
        details = u.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}

        def _num(*candidates) -> int:
            for c in candidates:
                if c is None:
                    continue
                try:
                    return int(c)
                except (TypeError, ValueError):
                    continue
            return 0

        self.last_usage = {
            "prompt_tokens": _num(u.get("prompt_tokens"), u.get("input_tokens")),
            "completion_tokens": _num(u.get("completion_tokens"), u.get("output_tokens")),
            "cached_tokens": _num(details.get("cached_tokens"),
                                  u.get("cache_read_input_tokens"),
                                  u.get("prompt_cache_hit_tokens")),
        }

    def _merge_usage(self, usage: dict | None) -> None:
        """增量合并 usage（Anthropic 流式把输入/输出 token 分散在不同事件里）。

        ``_set_usage`` 是整体覆盖语义，直接连用会把前一个事件里的
        prompt_tokens 冲成 0，故先快照、解析后再回填本次为空的历史值。
        """
        if not isinstance(usage, dict):
            return
        before = dict(self.last_usage)
        self._set_usage(usage)
        for key, val in before.items():
            if not self.last_usage.get(key) and val:
                self.last_usage[key] = val

    def _proxies(self) -> dict | None:
        from app.config import settings
        if settings.proxy_host and settings.proxy_port:
            return {"http://": f"http://{settings.proxy_host}:{settings.proxy_port}",
                    "https://": f"http://{settings.proxy_host}:{settings.proxy_port}"}
        return None

    @property
    def proxy_url(self) -> str | None:
        """代理 URL（供共享连接池做 cache key；无代理返回 None）。"""
        return proxy_url_from(self._proxies())

    def supports_json_mode(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        """判断当前模型是否支持图片/视觉输入"""
        model_lower = self.model.lower()
        return any(kw in model_lower for kw in _VISION_KEYWORDS)

    @staticmethod
    def build_vision_message(text: str, image_urls: list[str]) -> dict:
        """构建 OpenAI Vision 格式的多模态 user message

        Args:
            text: 文本指令
            image_urls: 图片 URL 列表（支持 http(s):// 和 data:image/... 格式）
        """
        content = [{"type": "text", "text": text}]
        for url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": "user", "content": content}

    async def chat(self, messages: list, temperature: float | None = None,
                   json_mode: bool = False, max_tokens: int | None = None,
                   extra_body: dict | None = None, **_kwargs) -> str:
        """对话调用

        ✅ 移植适配：支持 temperature/json_mode/max_tokens/extra_body 覆盖参数
        （image_engine 及源项目 collect_json_response 会传这些 kwarg），
        实现方按需取用，未知参数忽略。
        """
        raise NotImplementedError

    async def stream(self, messages: list, temperature: float | None = None,
                     json_mode: bool = False, max_tokens: int | None = None,
                     extra_body: dict | None = None, **_kwargs) -> AsyncIterator[str]:
        """流式调用：产出**增量文本片段**（不含任何协议封装）。

        实现方必须保证：分片按顺序产出，且不把「非正文」内容（reasoning、
        usage 帧等）混入；用量/结束原因通过 ``last_usage`` /
        ``last_finish_reason`` 回传，而不是混进正文。
        """
        raise NotImplementedError
        yield ""  # pragma: no cover


def proxy_url_from(proxies: dict | None) -> str | None:
    """取代理 URL 字符串（httpx ``proxy=`` 与连接池 cache key 共用）。"""
    if not proxies:
        return None
    return next(iter(proxies.values()), None)


def is_vision_model(model_name: str) -> bool:
    """工具函数：判断模型名是否支持视觉"""
    ml = model_name.lower()
    return any(kw in ml for kw in _VISION_KEYWORDS)