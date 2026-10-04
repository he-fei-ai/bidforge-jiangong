"""AI 提供商工厂（自动降级链：当前配置 → 其余配置按 priority → 内置兜底）"""
import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from app.config import settings
from app.db import get_conn, retry_db_op, write_tx_conn
from app.services import activity_broadcaster as _ab
from app.services.ai.json_mode_compat import json_mode_unsupported
from app.services.ai.providers.anthropic_compatible import AnthropicCompatibleProvider
from app.services.ai.providers.base import BaseProvider
from app.services.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.services.ai.workflows_base import circuit_breaker, concurrency_controller
from app.services.crypto import decrypt_api_key, encrypt_api_key

logger = logging.getLogger("provider_factory")

PROVIDER_PRESETS = {
    # ==================== 国内 20 个平台 ====================
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "type": "openai_compatible",
        "label": "DeepSeek",
        "models": [
            {"value": "deepseek-chat", "label": "DeepSeek-V3 (通用对话)", "context": "64K", "recommended": True},
            {"value": "deepseek-reasoner", "label": "DeepSeek-R1 (深度推理)", "context": "64K"},
        ],
        "description": "国产高性价比大模型，擅长代码与推理",
        "website": "https://platform.deepseek.com",
        "pricing": "输入¥0.5/百万Token，输出¥2/百万Token",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-max",
        "type": "openai_compatible",
        "label": "通义千问",
        "models": [
            {"value": "qwen-max", "label": "Qwen-Max (旗舰)", "context": "32K", "recommended": True},
            {"value": "qwen-plus", "label": "Qwen-Plus (均衡)", "context": "131K"},
            {"value": "qwen-turbo", "label": "Qwen-Turbo (极速)", "context": "1M"},
            {"value": "qwen-long", "label": "Qwen-Long (长文本)", "context": "10M"},
        ],
        "description": "阿里云通义千问，支持超长上下文",
        "website": "https://dashscope.console.aliyun.com",
        "pricing": "Max: 输入¥20/百万Token",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-plus",
        "type": "openai_compatible",
        "label": "智谱GLM",
        "models": [
            {"value": "glm-4-plus", "label": "GLM-4-Plus (旗舰)", "context": "128K", "recommended": True},
            {"value": "glm-4-air", "label": "GLM-4-Air (轻量)", "context": "128K"},
            {"value": "glm-4-flash", "label": "GLM-4-Flash (免费)", "context": "128K"},
            {"value": "glm-4-long", "label": "GLM-4-Long (长文本)", "context": "1M"},
        ],
        "description": "智谱AI开源大模型，Flash版本免费",
        "website": "https://open.bigmodel.cn",
        "pricing": "Flash免费，Plus: 输入¥50/百万Token",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "type": "openai_compatible",
        "label": "月之暗面",
        "models": [
            {"value": "moonshot-v1-8k", "label": "Moonshot-v1-8K", "context": "8K", "recommended": True},
            {"value": "moonshot-v1-32k", "label": "Moonshot-v1-32K", "context": "32K"},
            {"value": "moonshot-v1-128k", "label": "Moonshot-v1-128K", "context": "128K"},
            {"value": "kimi-latest", "label": "Kimi-Latest (最新)", "context": "128K"},
        ],
        "description": "Kimi大模型，擅长长文本处理",
        "website": "https://platform.moonshot.cn",
        "pricing": "8K: 输入¥12/百万Token",
    },
    "baidu": {
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-4.0-8k",
        "type": "openai_compatible",
        "label": "百度千帆",
        "models": [
            {"value": "ernie-4.0-8k", "label": "ERNIE 4.0 (旗舰)", "context": "8K", "recommended": True},
            {"value": "ernie-3.5-8k", "label": "ERNIE 3.5 (均衡)", "context": "8K"},
            {"value": "ernie-speed-8k", "label": "ERNIE Speed (极速)", "context": "8K"},
            {"value": "ernie-tiny-8k", "label": "ERNIE Tiny (轻量)", "context": "8K"},
        ],
        "description": "百度文心一言系列，中文理解能力强",
        "website": "https://console.bce.baidu.com/qianfan",
        "pricing": "4.0: 输入¥120/百万Token",
    },
    "minimax": {
        "base_url": "https://api.minimax.chat/v1",
        "model": "abab6.5s-chat",
        "type": "openai_compatible",
        "label": "MiniMax",
        "models": [
            {"value": "abab6.5s-chat", "label": "ABAB6.5s (对话)", "context": "245K", "recommended": True},
            {"value": "abab6.5-chat", "label": "ABAB6.5 (旗舰)", "context": "8K"},
        ],
        "description": "MiniMax大模型，超长上下文支持",
        "website": "https://platform.minimaxi.com",
        "pricing": "6.5s: 输入¥2/百万Token",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "type": "openai_compatible",
        "label": "硅基流动",
        "models": [
            {"value": "deepseek-ai/DeepSeek-V3", "label": "DeepSeek-V3 (托管)", "context": "64K", "recommended": True},
            {"value": "deepseek-ai/DeepSeek-R1", "label": "DeepSeek-R1 (托管)", "context": "64K"},
            {"value": "Qwen/Qwen2.5-72B-Instruct", "label": "Qwen2.5-72B", "context": "32K"},
            {"value": "Qwen/Qwen2.5-7B-Instruct", "label": "Qwen2.5-7B (免费)", "context": "32K"},
        ],
        "description": "硅基流动聚合平台，一键接入多种开源模型",
        "website": "https://cloud.siliconflow.cn",
        "pricing": "部分模型免费",
    },
    "volcengine": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1.5-pro-32k",
        "type": "openai_compatible",
        "label": "火山引擎",
        "models": [
            {"value": "doubao-1.5-pro-32k", "label": "Doubao-1.5-Pro-32K", "context": "32K", "recommended": True},
            {"value": "doubao-1.5-pro-256k", "label": "Doubao-1.5-Pro-256K", "context": "256K"},
            {"value": "doubao-pro-4k", "label": "Doubao-Pro-4K (轻量)", "context": "4K"},
            {"value": "doubao-pro-32k", "label": "Doubao-Pro-32K", "context": "32K"},
        ],
        "description": "字节跳动豆包大模型，火山引擎平台提供",
        "website": "https://www.volcengine.com/product/doubao",
        "pricing": "Pro-32K: 输入¥5/百万Token",
    },
    "hunyuan": {
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "model": "hunyuan-turbos-latest",
        "type": "openai_compatible",
        "label": "腾讯混元",
        "models": [
            {"value": "hunyuan-turbos-latest", "label": "Hunyuan-TurboS (极速)", "context": "256K", "recommended": True},
            {"value": "hunyuan-turbo-latest", "label": "Hunyuan-Turbo (旗舰)", "context": "256K"},
            {"value": "hunyuan-large-latest", "label": "Hunyuan-Large (超大)", "context": "256K"},
        ],
        "description": "腾讯混元大模型，支持超长上下文",
        "website": "https://cloud.tencent.com/product/hunyuan",
        "pricing": "TurboS: 输入¥1.5/百万Token",
    },
    "spark": {
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "model": "4.0Ultra",
        "type": "openai_compatible",
        "label": "讯飞星火",
        "models": [
            {"value": "4.0Ultra", "label": "Spark 4.0 Ultra (旗舰)", "context": "8K", "recommended": True},
            {"value": "generalv3.5", "label": "Spark 3.5 (均衡)", "context": "8K"},
            {"value": "generalv3", "label": "Spark 3.0 (标准)", "context": "8K"},
        ],
        "description": "科大讯飞星火大模型，语音与教育场景领先",
        "website": "https://xinghuo.xfyun.cn",
        "pricing": "4.0 Ultra: 输入¥35/百万Token",
    },
    "sensetime": {
        "base_url": "https://api.sensenova.cn/compatible-mode/v1",
        "model": "SenseChat-5",
        "type": "openai_compatible",
        "label": "商汤日日新",
        "models": [
            {"value": "SenseChat-5", "label": "SenseChat-5 (旗舰)", "context": "128K", "recommended": True},
            {"value": "SenseChat-Turbo", "label": "SenseChat-Turbo (极速)", "context": "32K"},
        ],
        "description": "商汤科技日日新大模型，多模态能力强",
        "website": "https://platform.sensenova.cn",
        "pricing": "5: 输入¥30/百万Token",
    },
    "lingyi": {
        "base_url": "https://api.lingyiwanwu.com/v1",
        "model": "yi-large",
        "type": "openai_compatible",
        "label": "零一万物",
        "models": [
            {"value": "yi-large", "label": "Yi-Large (旗舰)", "context": "32K", "recommended": True},
            {"value": "yi-medium", "label": "Yi-Medium (均衡)", "context": "16K"},
            {"value": "yi-lightning", "label": "Yi-Lightning (极速)", "context": "16K"},
        ],
        "description": "零一万物Yi系列大模型，李开复创立",
        "website": "https://platform.lingyiwanwu.com",
        "pricing": "Large: 输入¥20/百万Token",
    },
    "stepfun": {
        "base_url": "https://api.stepfun.com/v1",
        "model": "step-2-16k",
        "type": "openai_compatible",
        "label": "阶跃星辰",
        "models": [
            {"value": "step-2-16k", "label": "Step-2-16K (旗舰)", "context": "16K", "recommended": True},
            {"value": "step-1-8k", "label": "Step-1-8K (标准)", "context": "8K"},
            {"value": "step-1-flash", "label": "Step-1-Flash (极速)", "context": "8K"},
        ],
        "description": "阶跃星辰Step系列大模型，多模态能力突出",
        "website": "https://platform.stepfun.com",
        "pricing": "2-16K: 输入¥38/百万Token",
    },
    "baichuan": {
        "base_url": "https://api.baichuan-ai.com/v1",
        "model": "Baichuan4-Turbo",
        "type": "openai_compatible",
        "label": "百川智能",
        "models": [
            {"value": "Baichuan4-Turbo", "label": "Baichuan4-Turbo (旗舰)", "context": "32K", "recommended": True},
            {"value": "Baichuan4-Air", "label": "Baichuan4-Air (轻量)", "context": "32K"},
            {"value": "Baichuan3-Turbo", "label": "Baichuan3-Turbo", "context": "32K"},
        ],
        "description": "百川智能Baichuan系列，中文理解与搜索增强",
        "website": "https://platform.baichuan-ai.com",
        "pricing": "4-Turbo: 输入¥10/百万Token",
    },
    "qihoo360": {
        "base_url": "https://api.360.cn/v1",
        "model": "360gpt2-pro",
        "type": "openai_compatible",
        "label": "360智脑",
        "models": [
            {"value": "360gpt2-pro", "label": "360GPT2-Pro (旗舰)", "context": "32K", "recommended": True},
            {"value": "360gpt-turbo", "label": "360GPT-Turbo (极速)", "context": "8K"},
        ],
        "description": "360智脑大模型，安全与搜索能力突出",
        "website": "https://ai.360.com",
        "pricing": "Pro: 输入¥4/百万Token",
    },
    "tiangong": {
        "base_url": "https://api.tiangong.cn/v1",
        "model": "Skywork-4.0",
        "type": "openai_compatible",
        "label": "昆仑万维",
        "models": [
            {"value": "Skywork-4.0", "label": "Skywork-4.0 (旗舰)", "context": "32K", "recommended": True},
            {"value": "Skywork-3.0", "label": "Skywork-3.0 (标准)", "context": "8K"},
        ],
        "description": "昆仑万维天工Skywork系列，开源生态领先",
        "website": "https://model.tiangong.cn",
        "pricing": "4.0: 输入¥10/百万Token",
    },
    "huawei": {
        "base_url": "https://maas.huawei.com/v1",
        "model": "pangu-4.0",
        "type": "openai_compatible",
        "label": "华为云盘古",
        "models": [
            {"value": "pangu-4.0", "label": "Pangu-4.0 (旗舰)", "context": "32K", "recommended": True},
            {"value": "pangu-3.0", "label": "Pangu-3.0 (标准)", "context": "8K"},
        ],
        "description": "华为云盘古大模型，行业领域知识丰富",
        "website": "https://www.huaweicloud.com/product/pangu.html",
        "pricing": "4.0: 输入¥12/百万Token",
    },
    "ctyun": {
        "base_url": "https://xingchen-api.ctyun.cn/v1",
        "model": "xingchen-pro",
        "type": "openai_compatible",
        "label": "天翼星辰",
        "models": [
            {"value": "xingchen-pro", "label": "Xingchen-Pro (旗舰)", "context": "32K", "recommended": True},
            {"value": "xingchen-lite", "label": "Xingchen-Lite (轻量)", "context": "8K"},
        ],
        "description": "中国电信星辰大模型，运营商级安全合规",
        "website": "https://xingchen.ctyun.cn",
        "pricing": "Pro: 输入¥8/百万Token",
    },
    "modelscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen2.5-72b-instruct",
        "type": "openai_compatible",
        "label": "魔搭社区",
        "models": [
            {"value": "qwen2.5-72b-instruct", "label": "Qwen2.5-72B (开源)", "context": "32K", "recommended": True},
            {"value": "qwen2.5-7b-instruct", "label": "Qwen2.5-7B (免费)", "context": "32K"},
            {"value": "Qwen/Qwen2-72B-Instruct", "label": "Qwen2-72B", "context": "32K"},
        ],
        "description": "阿里魔搭社区开源模型推理服务",
        "website": "https://modelscope.cn",
        "pricing": "部分模型免费",
    },
    "opencompass": {
        "base_url": "https://api.opencompass.org.cn/v1",
        "model": "opencompass-v1",
        "type": "openai_compatible",
        "label": "OpenCompass",
        "models": [
            {"value": "opencompass-v1", "label": "OpenCompass-V1 (评测)", "context": "32K", "recommended": True},
        ],
        "description": "上海AI实验室开源评测平台模型服务",
        "website": "https://opencompass.org.cn",
        "pricing": "免费",
    },

    # ==================== 新加坡 ====================
    "agnes": {
        "base_url": "https://api.agnes-ai.cn/v1",
        "model": "agnes-2.5-flash",
        "type": "openai_compatible",
        "label": "Agnes（内置兜底）",
        "models": [
            {"value": "agnes-2.5-flash", "label": "agnes-2.5-flash (默认)", "context": "128K", "recommended": True},
        ],
        "description": "新加坡Agnes平台，内置兜底供应商",
        "website": "https://agnes-ai.cn",
        "pricing": "平台内置",
    },

    # ==================== 美国 ====================
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "type": "openai_compatible",
        "label": "OpenAI",
        "supports_vision": True,
        "models": [
            {"value": "gpt-4o", "label": "GPT-4o (旗舰)", "context": "128K", "recommended": True, "vision": True},
            {"value": "gpt-4o-mini", "label": "GPT-4o-mini (轻量)", "context": "128K", "vision": True},
            {"value": "gpt-4-turbo", "label": "GPT-4 Turbo", "context": "128K", "vision": True},
            {"value": "o1", "label": "O1 (深度推理)", "context": "200K"},
        ],
        "description": "OpenAI GPT系列，全球领先通用大模型",
        "website": "https://platform.openai.com",
        "pricing": "4o: 输入$2.5/百万Token",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
        "type": "anthropic_compatible",
        "label": "Anthropic",
        "supports_vision": True,
        "models": [
            {"value": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4", "context": "200K", "recommended": True, "vision": True},
            {"value": "claude-opus-4-20250514", "label": "Claude Opus 4 (旗舰)", "context": "200K", "vision": True},
            {"value": "claude-3-5-haiku-20241022", "label": "Claude 3.5 Haiku (极速)", "context": "200K", "vision": True},
        ],
        "description": "Anthropic Claude系列，长文本与推理能力卓越",
        "website": "https://console.anthropic.com",
        "pricing": "Sonnet: 输入$3/百万Token",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "meta/llama-3.1-405b-instruct",
        "type": "openai_compatible",
        "label": "NVIDIA NIM",
        "models": [
            {"value": "meta/llama-3.1-405b-instruct", "label": "Llama-3.1-405B (旗舰)", "context": "128K", "recommended": True},
            {"value": "meta/llama-3.1-70b-instruct", "label": "Llama-3.1-70B", "context": "128K"},
            {"value": "meta/llama-3.1-8b-instruct", "label": "Llama-3.1-8B (轻量)", "context": "128K"},
            {"value": "nvidia/nemotron-4-340b-instruct", "label": "Nemotron-4-340B", "context": "4K"},
            {"value": "mistralai/mixtral-8x22b-instruct-v0.1", "label": "Mixtral-8x22B", "context": "64K"},
        ],
        "description": "NVIDIA NIM平台，托管多种开源大模型推理",
        "website": "https://build.nvidia.com",
        "pricing": "免费额度1000次/月，之后按量计费",
    },
}

# 为每个 preset 注入「计费方式」维度：
# - 按量计费使用平台官方默认接入点（preset 自带 base_url / model）
# - 包月套餐需用户手动填写该套餐对应的 API 地址与模型
for _preset in PROVIDER_PRESETS.values():
    _preset.setdefault("plans", {
        "pay_as_you_go": {
            "label": "按量计费",
            "base_url": _preset.get("base_url", ""),
            "model": _preset.get("model", ""),
        },
        "coding_plan": {
            "label": "包月套餐",
            "base_url": "",
            "model": "",
        },
    })


def _build_provider(preset_name: str, api_key: str, base_url: str, model: str, **kw) -> BaseProvider:
    api_key = (api_key or "").strip()
    base_url = (base_url or "").strip()
    if not api_key:
        raise ValueError("API Key 为空，无法构建 Provider")
    preset = PROVIDER_PRESETS.get(preset_name)
    if preset is None:
        # 自定义或未知供应商：必须自带 base_url，不回退到任何预设
        if not base_url:
            raise ValueError(f"供应商 {preset_name} 未提供 Base URL，且无预设地址可用")
        t = "openai_compatible"
        default_model = ""
    else:
        t = preset.get("type", "openai_compatible")
        default_model = preset.get("model", "")
        # 已知预设：base_url 为空时回退到平台预设地址（仅适用于按量计费兜底）
        if not base_url:
            base_url = preset.get("base_url", "")
    cls = AnthropicCompatibleProvider if t == "anthropic_compatible" else OpenAICompatibleProvider
    return cls(api_key=api_key, base_url=base_url,
               model=model or default_model, **kw)


# 允许的 API 地址协议（白名单，避免 file:// / ftp:// 等被写入配置后由 httpx 发起）
_ALLOWED_URL_SCHEMES = ("http", "https")
# 数值字段合法区间（超界静默收敛，避免前端绕过 / 旧数据导致运行时异常）
# ✅ 并发上限改为读取 settings.max_concurrency（默认 5，与既有硬编码一致）：
#    此前 `_RANGE["concurrency"]` 与 apply_config_concurrency 的 `1<=c<=5`
#    两处各自硬编码，放宽上限需要同步改两处（漏改即静默钳制）。默认零变化。
_MAX_CONCURRENCY = max(1, int(getattr(settings, "max_concurrency", 5) or 5))
_RANGE = {
    "max_tokens": (256, 200000),
    "temperature": (0.0, 2.0),
    "timeout": (10, 3600),
    "concurrency": (1, _MAX_CONCURRENCY),
}

# ✅ 数值字段缺省值单一出口：schema_sql 表默认 / AIConfigIn 默认 / 前端表单默认
#    与运行时候选 fallback（_build_candidates / _fallback_chain /
#    _candidate_timeout / _attempt_candidate）此前各写一份，
#    timeout 曾出现「声明 900、运行时兜底 60」的口径漂移（配置行缺该字段或
#    为 0 时，主候选超时静默缩水到 60s，长正文生成必超时）。现统一引用本表。
DEFAULT_CONFIG_NUMBERS: dict = {
    "max_tokens": 8192,
    "temperature": 0.7,
    "timeout": 900,
    "concurrency": 4,
}


def _clamp(value, lo, hi, fallback):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, v))


#: 合法计费方式（白名单）。脏值（拼错/旧版本导入数据）此前会原样入库，
#: 前端计费方式列只能显示原始字符串，且「包月必须手填地址」的联动校验也会被绕过。
_VALID_PLANS = ("pay_as_you_go", "coding_plan")


def normalize_plan(raw) -> str:
    """归一化计费方式：非白名单值一律回落「按量计费」。"""
    return raw if raw in _VALID_PLANS else "pay_as_you_go"


#: 合法请求方式（白名单）。
#: - normal：普通请求（一次性等待完整响应，正文生成的历史路径）
#: - stream：流式请求（后端以 SSE 分片方式接收并拼接成完整结果后再返回，
#:           仅改变后端与厂商之间的调用方式，应用侧行为不变 —— 仍是「等完整结果」）
#: 与 plan 同理：脏值（拼错/旧版本导入数据）会原样落库，运行时按未知值处理
#: 会静默退回普通请求，界面上却显示成用户填的乱码，故统一归一。
_VALID_REQUEST_MODES = ("normal", "stream")


def normalize_request_mode(raw) -> str:
    """归一化请求方式：非白名单值（含 None/空/大小写不一致）一律回落 ``normal``。"""
    if not isinstance(raw, str):
        return "normal"
    v = raw.strip().lower()
    return v if v in _VALID_REQUEST_MODES else "normal"


def request_mode_label(raw) -> str:
    """请求方式的中文标签（供接口回传/日志使用）。"""
    return "流式请求" if normalize_request_mode(raw) == "stream" else "普通请求"


def clamp_config_numbers(raw: dict) -> dict:
    """把 AI 配置的数值字段收敛到合法区间。

    统一入口：``save_ai_config``（表单保存）与 ``/ai/config/import``（外部导入）
    共用，避免「界面校验得住、导入文件却能把 temperature 写成 99」这类
    绕过界面的脏数据进入运行链路（超界值会在 provider 请求里被厂商拒绝）。
    """
    return {
        "max_tokens": int(_clamp(raw.get("max_tokens"), *_RANGE["max_tokens"],
                                 DEFAULT_CONFIG_NUMBERS["max_tokens"])),
        "temperature": round(_clamp(raw.get("temperature"), *_RANGE["temperature"],
                                    DEFAULT_CONFIG_NUMBERS["temperature"]), 3),
        "timeout": int(_clamp(raw.get("timeout"), *_RANGE["timeout"],
                              DEFAULT_CONFIG_NUMBERS["timeout"])),
        "concurrency": int(_clamp(raw.get("concurrency"), *_RANGE["concurrency"],
                                  DEFAULT_CONFIG_NUMBERS["concurrency"])),
    }


def normalize_base_url(raw: str) -> str:
    """归一化并校验 API Base URL。

    ✅ 原实现直接把用户输入写库，只做 ``.strip()``：
      - 协议未校验（`file://`、`ftp://` 等会被 httpx 接受并尝试发起请求）；
      - 未校验域名 —— `http://` 这类空主机地址会一路写库，直到运行时才失败；
      - 尾部斜杠未归一 —— 与 provider 内 `base_url.rstrip("/")` 不一致，
        造成「配置里显示带斜杠、实际请求不带」的表里不一。
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        # 用户只填域名时按 https 补齐（比直接报「格式错误」更友好）
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError("Base URL 仅支持 http/https 协议")
    if not parsed.hostname:
        raise ValueError("Base URL 缺少有效域名，请填写形如 https://api.example.com/v1 的地址")
    return url


#: 数值字段的中文名，用于把「界面填了越界值、后端静默收敛」显式告知用户。
_NUMBER_LABELS = {
    "max_tokens": "最大 Token",
    "temperature": "温度",
    "timeout": "超时（秒）",
    "concurrency": "并发数",
}


def clamp_warnings(raw: dict) -> list[str]:
    """返回「数值字段被静默收敛」的提示文案列表（未越界/未传时为空）。

    ✅ 2026-09-23 新增：`clamp_config_numbers` 会把越界值悄悄改小/改大后落库，
       但响应里只回 ``{"ok": true}`` —— 用户在界面上填了并发 8，实际生效 5，
       界面上依旧显示 8（旧页面/旧版本前端也会提交越界值），
       属「配置写进库 ≠ 生效」的静默失效。现由路由层把差异回传前端提示。
    """
    clamped = clamp_config_numbers(raw)
    warns: list[str] = []
    for field, (lo, hi) in _RANGE.items():
        if raw.get(field) is None:
            continue
        try:
            v = float(raw[field])
        except (TypeError, ValueError):
            warns.append(
                f"{_NUMBER_LABELS[field]}取值非法（{raw[field]!r}），已按默认 {clamped[field]} 保存")
            continue
        if v < lo or v > hi:
            warns.append(
                f"{_NUMBER_LABELS[field]} {raw[field]} 超出允许范围 {lo}~{hi}，已按 {clamped[field]} 保存")
    return warns


async def save_ai_config(data: dict) -> str:
    cid = data.get("id") or str(uuid.uuid4())
    enc = encrypt_api_key(data.get("api_key", ""))
    plan = normalize_plan(data.get("plan"))
    provider_name = (data.get("provider_name") or "").strip()
    base_url = normalize_base_url(data.get("base_url") or "")
    model = (data.get("model") or "").strip()
    remark = (data.get("remark") or "").strip()[:2000]

    if not provider_name:
        raise ValueError("供应商不能为空")
    if not model:
        raise ValueError("模型名称不能为空")

    # 计费方式与 Base URL 联动校验：
    # - 自定义供应商：必须手填 Base URL
    # - 包月套餐（Coding Plan）：必须手填 API 地址（各平台包月接入点不同于按量计费）
    # - 按量计费：未填地址时自动回填平台预设地址
    if provider_name == "custom":
        if not base_url:
            raise ValueError("自定义供应商必须填写 Base URL")
    elif plan == "coding_plan":
        if not base_url:
            raise ValueError("包月套餐（Coding Plan）必须手动填写 API 地址，包月接入点不同于按量计费")
    elif plan == "pay_as_you_go":
        if not base_url:
            preset = PROVIDER_PRESETS.get(provider_name)
            if preset:
                base_url = preset.get("base_url", "")
    if not base_url:
        raise ValueError("Base URL 不能为空（按量计费请选择已知供应商或手动填写地址）")

    nums = clamp_config_numbers(data)
    max_tokens = nums["max_tokens"]
    temperature = nums["temperature"]
    timeout = nums["timeout"]
    concurrency = nums["concurrency"]
    # ✅ 新增：请求方式（normal / stream）。与 plan 同样走白名单归一 ——
    #    脏值不落库，避免「界面显示乱码、运行时静默按普通请求」的表里不一。
    request_mode = normalize_request_mode(data.get("request_mode"))
    # ✅ 2026-09-23（多环境）：环境标签同样走白名单归一，脏值不落库
    # （非法值直接抛 ValueError → 路由层 400，不静默写成空串）。
    env = normalize_env(data.get("env"))
    # priority 为 None 表示「本次保存不改动降级顺序」（降级链有独立编辑入口），
    # 若保存时无脑写 0，会把用户拖好的顺序每次保存配置时全部清零。
    raw_priority = data.get("priority", None)

    try:
        # ✅ 并发修复：多语句事务改用独立写池连接。原实现在全局共享连接
        # get_conn() 上 rollback，会把并发协程（任务进度写、任务注册 INSERT 等）
        # 已执行未提交的合法语句一并回滚，造成静默数据丢失。
        async with write_tx_conn() as conn:
            if data.get("is_active"):
                await conn.execute("UPDATE ai_config SET is_active = 0")
            exists = await conn.execute("SELECT id FROM ai_config WHERE id = ?", (cid,))
            is_update = bool(await exists.fetchone())
            if is_update:
                # ✅ 修复：原 UPDATE 语句完全不含 remark / priority 两列，
                #    前端「备注」表单填写后静默丢失（列表备注列永远为 "-"）。
                if raw_priority is None:
                    await conn.execute(
                        "UPDATE ai_config SET provider_name=?, plan=?,"
                        " api_key_encrypted=CASE WHEN ?!='' THEN ? ELSE api_key_encrypted END,"
                        " base_url=?, model=?, max_tokens=?, temperature=?, timeout=?, concurrency=?,"
                        " request_mode=?, env=?, is_active=?, remark=?, updated_at=? WHERE id=?",
                        (provider_name, plan, data.get("api_key", ""), enc, base_url,
                         model, max_tokens, temperature, timeout, concurrency,
                         request_mode, env, int(data.get("is_active", True)), remark,
                         datetime.now().isoformat(), cid))
                else:
                    await conn.execute(
                        "UPDATE ai_config SET provider_name=?, plan=?,"
                        " api_key_encrypted=CASE WHEN ?!='' THEN ? ELSE api_key_encrypted END,"
                        " base_url=?, model=?, max_tokens=?, temperature=?, timeout=?, concurrency=?,"
                        " request_mode=?, env=?, is_active=?, remark=?, priority=?, updated_at=? WHERE id=?",
                        (provider_name, plan, data.get("api_key", ""), enc, base_url,
                         model, max_tokens, temperature, timeout, concurrency,
                         request_mode, env, int(data.get("is_active", True)), remark,
                         int(raw_priority), datetime.now().isoformat(), cid))
            else:
                if raw_priority is None:
                    # 新配置默认排到降级链末尾，而不是与既有配置同为 0
                    # （同值时只能靠 updated_at 兜底排序，顺序不可预期）
                    cur = await conn.execute(
                        "SELECT COALESCE(MAX(priority), -1) + 1 FROM ai_config")
                    row = await cur.fetchone()
                    raw_priority = int(row[0]) if row and row[0] is not None else 0
                await conn.execute(
                    "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url, model,"
                    " max_tokens, temperature, timeout, concurrency, request_mode, env,"
                    " is_active, priority, remark)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, provider_name, plan, enc, base_url, model,
                     max_tokens, temperature, timeout, concurrency, request_mode, env,
                     int(data.get("is_active", True)), int(raw_priority), remark))
            # 注：此处原有一句 INSERT INTO provider_history，已删除 —— 该表全库无任何读取
            # （数据流审计 R2 强信号），属只增不消费的死写，每次保存配置都会白写一行。
            await conn.commit()
    except Exception:
        # 半提交状态已由 write_tx_conn 统一回滚（独立连接，不影响其他协程）
        raise
    invalidate_config_cache()
    if data.get("is_active"):
        await apply_config_concurrency()
    return cid


async def apply_config_concurrency() -> int | None:
    """✅ 统一并发体系：把活跃 AI 配置的并发数应用到全局并发控制器。

    AI 配置页的 concurrency 字段此前只落库、运行时无任何消费者；
    现作为全局默认并发（启动/保存/切换配置时应用），
    工作台三档（slow/balanced/fast）在正文生成请求中仍可临时覆盖。

    返回实际应用的并发数（无活跃配置时返回 None）。
    """
    try:
        conn = await get_conn()
        cur = await conn.execute(
            "SELECT concurrency FROM ai_config WHERE is_active = 1 "
            "ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
        if row and row[0]:
            c = int(row[0])
            # ✅ 全局并发上限统一走 settings.max_concurrency（默认 5，与既有
            #    硬编码一致；P0 修复 2026-09-17 引入的约束保持不变）
            if 1 <= c <= _MAX_CONCURRENCY:
                concurrency_controller.set_concurrency(c)
                logger.info("已应用活跃 AI 配置并发数: %d", c)
                return c
            elif c > _MAX_CONCURRENCY:
                concurrency_controller.set_concurrency(_MAX_CONCURRENCY)
                logger.warning("AI 配置并发数 %d 超过全局上限 %d，已钳到 %d",
                               c, _MAX_CONCURRENCY, _MAX_CONCURRENCY)
                return _MAX_CONCURRENCY
    except Exception as e:
        logger.debug("应用配置并发失败（不影响服务）: %s", e)
    return None


_config_cache: dict = {"data": None, "ts": 0.0}
# 降级链缓存（与主配置同 TTL，AI 配置保存时一并失效）
_fallback_cache: dict = {"data": None, "ts": 0.0}
# 场景 → 配置 路由缓存（表为空时等价于「无路由」，行为与旧版一致）
_scene_route_cache: dict = {"data": None, "ts": 0.0}
# ✅ 2026-09-23：当前生效环境缓存（多环境切换；表无行时回落 settings.active_env）
_env_cache: dict = {"data": None, "ts": 0.0}
# ✅ 2026-09-23：运行时「厂商开关」缓存（未设置 = 空集 → 行为与引入前一致）
_disabled_cache: dict = {"data": None, "ts": 0.0}

#: 缓存代际号（防 TOCTOU：见 invalidate_config_cache 的说明）。
#: 只增不减，读侧比对「读库前 vs 写缓存前」是否发生变化。
_cache_generation = 0


#: 配置缓存 TTL 的历史硬编码兜底值（秒）。settings.ai_config_cache_ttl 非法时的回退。
_DEFAULT_CONFIG_CACHE_TTL = 300.0


def _config_cache_ttl() -> float:
    """AI 配置相关内存缓存的 TTL（秒）。

    ✅ 2026-09-23 修复：原先这里是硬编码的 ``_CONFIG_CACHE_TTL = 300.0``，
       而 ``settings.ai_config_cache_ttl``（环境变量 ``AI_CONFIG_CACHE_TTL``）
       全库无任何消费方 —— 「配置项存在、文档写着 3600、实际生效 300」。
       现统一读取该配置项（默认 300 与既有运行行为一致）。
       所有写路径都会 ``invalidate_config_cache()`` 立即失效，TTL 只是外部
       直接改库时的兜底，故调大该值不会有「改了不生效」的风险。
    """
    try:
        ttl = float(getattr(settings, "ai_config_cache_ttl", _DEFAULT_CONFIG_CACHE_TTL)
                    or _DEFAULT_CONFIG_CACHE_TTL)
    except (TypeError, ValueError):
        return _DEFAULT_CONFIG_CACHE_TTL
    return ttl if ttl > 0 else _DEFAULT_CONFIG_CACHE_TTL


# P0-4 性能优化：AI 审计日志攒批（满 50 条或 10s 定时批量落库，替代每次调用一次 fsync commit）

# P0-4 性能优化：AI 审计日志攒批（满 50 条或 10s 定时批量落库，替代每次调用一次 fsync commit）
_audit_buffer: list[tuple] = []
_audit_lock = threading.Lock()
_AUDIT_BATCH_SIZE = 50
_AUDIT_FLUSH_INTERVAL = 10.0
_audit_last_flush: dict = {"ts": time.time()}  # 模块加载起计时，避免首条即刷

# ✅ 2026-09-16 新增：进程内实时可靠性统计（滑动窗口 50 次调用）
# 用于 _fallback_chain 自动过滤历史成功率过低的 provider（<50% 的直接排除）
# 主配置总是保留（即使成功率低），只有 fallback 候选会被过滤。
_provider_reliability_lock = threading.Lock()
_provider_reliability: dict[str, dict] = {}  # provider_name → {"ok": int, "fail": int}
_PROVIDER_RELIABILITY_WINDOW = 50            # 每个 provider 统计最近 N 次
# ✅ 修复（2026-09-17）：统一为与 _is_dead_provider 相同的配置项门槛。
#    旧实现硬编码 0.50 与 _order_candidates 使用的 ai_provider_dead_success_rate(0.20)
#    冲突，导致 20%~50% 成功率的候选被 _fallback_chain 静默剔除（即便 _order_candidates
#    认为"未死"保留），降级覆盖被悄悄收窄，违反"配置化优先、不隐式钳制"约束。
_PROVIDER_MIN_SUCCESS_RATE = float(
    getattr(settings, "ai_provider_dead_success_rate", 0.20) or 0.20)
_PROVIDER_MIN_SAMPLES = 5                    # 成功率统计生效的最小样本数
# ✅ P0-1（2026-09-17）：死配置剔除 / 主配置后置门限
#    实测 volcengine 21 次调用 100% 失败、spark 78.6% 失败 —— 留在候选链上
#    只会白等一轮网络往返；主配置（sensetime）44% 失败却因"无条件首位"让
#    成功率仅 3.5% 的兜底 agnes 永远排在最后。
_PROVIDER_DEAD_MIN_SAMPLES = 10
_PROVIDER_DEAD_SUCCESS_RATE = float(
    getattr(settings, "ai_provider_dead_success_rate", 0.20) or 0.20)
_PROVIDER_DEMOTE_SUCCESS_RATE = float(
    getattr(settings, "ai_provider_demote_success_rate", 0.60) or 0.60)

# ---------- AI 调用实时状态（「后台任务状态栏」轮询读取，进程内存态） ----------
# 记录正在进行的调用数、本次会话累计成败数与最近一次调用结果 ——
# 审计日志有最长 10s 的攒批延迟且只有已完成的调用，无法表达「AI 调用中」。
_ai_live_lock = threading.Lock()
_ai_live: dict = {
    "in_flight": 0,        # 正在进行的 AI 调用数
    "total": 0,            # 本次会话累计调用尝试数（含降级重试）
    "failed": 0,           # 本次会话累计失败尝试数
    "last_provider": "",   # 最近一次调用的 Provider
    "last_model": "",      # 最近一次调用的模型
    "last_ok": True,       # 最近一次调用是否成功
    "last_duration": 0.0,  # 最近一次调用耗时（秒）
    "last_at": 0.0,        # 最近一次调用完成的时间戳（time.time()）
    "started_at": time.time(),  # 会话起点（用于「今日/本会话」语义说明）
}


def get_ai_live_stats() -> dict:
    """返回 AI 调用实时状态快照（只读拷贝，供系统活动聚合端点调用）。"""
    with _ai_live_lock:
        return dict(_ai_live)


def invalidate_config_cache():
    """✅ 修复：AI 配置增删改后立即失效缓存。

    旧实现在保存/切换/删除配置后从不失效 _config_cache（仅 TTL 自然过期），
    导致用户切换活跃供应商后 chat_with_fallback 最长 5 分钟仍用旧供应商。

    ✅ 2026-09-23 增强：同时递增 ``_cache_generation`` 代际号。
      读侧（``_load_active_config`` / ``_fallback_chain`` / ``load_scene_routes``）
      在 ``await`` 读库**之前**记下代际号，写回缓存**之后**比对：
      期间若发生过失效（并发保存配置），本次读到的旧行会被丢弃而不是灌回缓存 ——
      否则「失效 → 另一协程把失效前的旧行重新写进缓存并钉住一整个 TTL」
      会让配置改动静默不生效（TOCTOU 竞态）。
    """
    global _cache_generation
    _cache_generation += 1
    _config_cache["data"] = None
    _config_cache["ts"] = 0.0
    _fallback_cache["data"] = None
    _fallback_cache["ts"] = 0.0
    _scene_route_cache["data"] = None
    _scene_route_cache["ts"] = 0.0
    _env_cache["data"] = None
    _env_cache["ts"] = 0.0
    _disabled_cache["data"] = None
    _disabled_cache["ts"] = 0.0


# ---------------------------------------------------------------------------
# ✅ 2026-09-23 新增：多环境（dev / test / prod）与环境过滤
# ---------------------------------------------------------------------------
#: 运行时「当前生效环境」在 ai_runtime_settings 表中的键名
RUNTIME_ACTIVE_ENV_KEY = "active_env"

#: 环境标签最大长度（防止把备注塞进环境列）
_ENV_MAX_LEN = 32

#: 环境名允许的字符集：字母/数字/下划线/中划线（避免路径与日志注入类脏值）
_ENV_PATTERN = re.compile(r"[A-Za-z0-9_\-]+")


def normalize_env(raw) -> str:
    """归一化环境标签：去空白、限制长度与字符集；非法值抛 ``ValueError``。

    空串是**合法**取值，语义为「通用配置」（可被任何环境使用）。
    """
    env = str(raw or "").strip()
    if not env:
        return ""
    if len(env) > _ENV_MAX_LEN:
        raise ValueError(f"环境名过长（最多 {_ENV_MAX_LEN} 个字符）")
    if not _ENV_PATTERN.fullmatch(env):
        raise ValueError("环境名只允许字母、数字、下划线、中划线（如 dev / test / prod）")
    return env


def _settings_active_env() -> str:
    """``settings.active_env`` 的安全读取（脏值按空串处理，不抛异常）。"""
    try:
        return normalize_env(getattr(settings, "active_env", "") or "")
    except ValueError:
        logger.warning("settings.active_env 取值非法，已按空串（通用）处理")
        return ""


async def load_runtime_setting(key: str) -> str | None:
    """读取运行时设置。

    **返回 None 表示「从未设置过」**（调用方应回落 ``settings`` 默认值）；
    返回空串是「显式设置为空」的合法状态，两者语义不同。
    表不存在（旧库未重启触发建表）时同样返回 None，行为与引入前一致。
    """
    try:
        conn = await get_conn()
        cur = await conn.execute(
            "SELECT value FROM ai_runtime_settings WHERE key = ?", (key,))
        row = await cur.fetchone()
    except Exception as e:
        # 仅「旧库表尚未创建」按未设置兼容；锁、I/O、损坏等读取故障必须向上传播。
        # 否则禁用厂商清单会被误判为空集，合规下线/故障隔离的厂商将被重新启用。
        if isinstance(e, sqlite3.OperationalError) and "no such table" in str(e).lower():
            logger.debug("运行时设置表尚不存在（按未设置处理）: %s", key)
            return None
        logger.error("读取运行时设置 %s 失败（拒绝按默认值继续）: %s", key, e)
        raise
    if row is None:
        return None
    return str(dict(row).get("value") or "")


async def upsert_runtime_setting(db, key: str, value: str) -> None:
    """在调用方事务中写入运行时设置；提交与缓存失效由调用方统一负责。"""
    await db.execute(
        "INSERT INTO ai_runtime_settings (key, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " updated_at=excluded.updated_at",
        (key, value, datetime.now().isoformat()))


async def save_runtime_setting(key: str, value: str) -> None:
    """独立事务写入运行时设置并立即失效配置缓存（服务层/测试兼容入口）。"""
    async with write_tx_conn() as conn:
        await upsert_runtime_setting(conn, key, value)
        await conn.commit()
    invalidate_config_cache()


async def resolve_active_env() -> str:
    """当前生效环境：运行时表 > ``settings.active_env`` > 空串（通用）。

    默认（两者皆空）返回空串，调用方**不做任何环境过滤** ——
    即开启多环境前的行为逐字节不变。
    """
    now = time.time()
    if _env_cache["data"] is not None and now - _env_cache["ts"] < _config_cache_ttl():
        return _env_cache["data"]
    gen = _cache_generation
    raw = await load_runtime_setting(RUNTIME_ACTIVE_ENV_KEY)
    if raw is None:
        env = _settings_active_env()
    else:
        try:
            env = normalize_env(raw)
        except ValueError:
            # 环境标签决定密钥/地址隔离；损坏值回落通用会扩大可见配置范围，
            # 因此与禁用厂商清单一样 fail-closed。
            logger.error("运行时环境取值非法（%r），拒绝按通用环境继续", raw)
            raise ValueError("运行时环境配置损坏，请重新选择环境")
    if gen == _cache_generation:
        _env_cache["data"] = env
        _env_cache["ts"] = now
    return env


# ---------------------------------------------------------------------------
# ✅ 2026-09-23 新增：运行时「厂商开关」（临时禁用某厂商，不删配置、无需重启）
# ---------------------------------------------------------------------------
#: 运行时禁用厂商清单在 ai_runtime_settings 表中的键名（值为 JSON 数组）
RUNTIME_DISABLED_PROVIDERS_KEY = "disabled_providers"

#: 厂商名允许的字符集与长度（与 ai_config.provider_name 的实际取值一致）
_PROVIDER_NAME_PATTERN = re.compile(r"[A-Za-z0-9_\-\.]+")
_PROVIDER_NAME_MAX_LEN = 64


def normalize_provider_name(raw) -> str:
    """归一化厂商名；空串合法（表示「不指定」），非法值抛 ``ValueError``。"""
    name = str(raw or "").strip()
    if not name:
        return ""
    if len(name) > _PROVIDER_NAME_MAX_LEN:
        raise ValueError(f"厂商名过长（最多 {_PROVIDER_NAME_MAX_LEN} 个字符）")
    if not _PROVIDER_NAME_PATTERN.fullmatch(name):
        raise ValueError("厂商名只允许字母、数字、下划线、点、中划线")
    return name


def parse_disabled_providers(raw: str | None) -> set[str]:
    """解析运行时禁用厂商清单。

    兼容两种写法：JSON 数组（工具/接口写入）与逗号分隔（人工直接改库）。
    任何无法归一的片段**跳过并记警告** —— 一个脏值不应让整份设置失效。
    """
    text = str(raw or "").strip()
    if not text:
        return set()
    items: list = []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except Exception:
            # 非法 JSON 无法判断哪些厂商仍处于禁用状态，fail-closed 抛错；
            # 绝不能回落空集（那等价于一次性恢复全部厂商）。
            logger.error("运行时厂商开关取值不是合法 JSON，拒绝按空清单继续")
            raise ValueError("运行时厂商开关配置损坏，请重新保存")
        items = loaded if isinstance(loaded, list) else []
    else:
        items = text.split(",")
    out: set[str] = set()
    for it in items:
        try:
            name = normalize_provider_name(it)
        except ValueError:
            logger.warning("忽略非法的厂商名（运行时开关）：%r", it)
            continue
        if name:
            out.add(name)
    return out


async def resolve_disabled_providers() -> set[str]:
    """当前被运行时开关禁用的厂商集合。

    未设置（旧库/未启用）→ 返回空集，此时**不跳过任何候选**，行为与引入该功能前一致。
    """
    now = time.time()
    if _disabled_cache["data"] is not None and now - _disabled_cache["ts"] < _config_cache_ttl():
        return _disabled_cache["data"]
    gen = _cache_generation
    raw = await load_runtime_setting(RUNTIME_DISABLED_PROVIDERS_KEY)
    disabled = parse_disabled_providers(raw)
    if gen == _cache_generation:
        _disabled_cache["data"] = disabled
        _disabled_cache["ts"] = now
    return disabled


async def save_disabled_providers(names: list[str]) -> list[str]:
    """写入运行时禁用厂商（去重、排序、JSON 落库）并即时失效缓存。"""
    cleaned = sorted({normalize_provider_name(n) for n in (names or [])} - {""})
    await save_runtime_setting(
        RUNTIME_DISABLED_PROVIDERS_KEY, json.dumps(cleaned, ensure_ascii=False))
    return cleaned


async def _load_active_config() -> dict | None:
    import time as _time
    now = _time.time()
    if _config_cache["data"] is not None and now - _config_cache["ts"] < _config_cache_ttl():
        return _config_cache["data"]
    gen = _cache_generation
    env = await resolve_active_env()
    conn = await get_conn()
    # ✅ 2026-09-23（多环境）：env 为空时 SQL 与引入该功能前**完全一致**（零过滤）；
    #    非空时只允许「该环境专用」或「通用」主配置，绝不最终回落到其它环境。
    #    否则 prod 模式可能拿到 dev 的地址和密钥，造成跨环境串用与数据外发。
    _sql = "SELECT * FROM ai_config WHERE is_active = 1{extra} ORDER BY updated_at DESC LIMIT 1"
    row = None
    if env:
        row = await (await conn.execute(_sql.format(extra=" AND env = ?"), (env,))).fetchone()
        if not row:
            row = await (await conn.execute(_sql.format(extra=" AND env = ''"))).fetchone()
            if row:
                logger.debug("环境 %s 无专用主配置，回落通用主配置", env)
    else:
        row = await (await conn.execute(_sql.format(extra=""))).fetchone()
    cfg = dict(row) if row else None
    if gen != _cache_generation:
        # 读库期间配置被改动并失效了缓存：本次结果已过期，直接返回但不写缓存
        logger.debug("主配置读取期间缓存被失效，丢弃本次缓存写入（代际 %d→%d）",
                     gen, _cache_generation)
        return cfg
    _config_cache["data"] = cfg
    _config_cache["ts"] = now
    return cfg


def _primary_api_key(cfg: dict) -> str:
    """取主配置可用的 API Key。

    ✅ 修复（本轮审查）：原实现是 `decrypt_api_key(...) or settings.agnes_api_key` ——
      主配置（例如 deepseek）**压根没填 Key** 时，会把「内置 agnes 的 Key」
      拿去请求 deepseek 的地址，必然 401，后果是：
        1. 真实原因（主配置没填 Key）被掩盖成「厂商 401 认证失败」；
        2. 这次注定失败的请求会计入 deepseek 的熔断计数与审计日志 ——
           界面上看到的是「deepseek 被熔断」，与真相完全无关；
        3. 白白多一次网络往返才降级到兜底。
      现只在该配置本身就是内置 agnes 时才回退到 agnes Key；其余情况返回空串，
      由调用方按「无 Key」跳过。真正的 agnes 兜底候选由 `_fallback_chain()`
      单独追加，自带正确的 base_url / model。
    """
    key = decrypt_api_key(cfg.get("api_key_encrypted", "") or "")
    if key:
        return key
    if (cfg.get("provider_name") or "").strip().lower() == "agnes" and settings.agnes_api_key:
        return settings.agnes_api_key
    return ""


async def _fallback_chain() -> list[dict]:
    """降级候选链：DB 中「除主配置外的全部配置」（按 priority ASC）+ 内置兜底 agnes。

    ✅ BUG-F-02 修复：原实现只返回内置 agnes，**完全不读 ai_config 表**，
    导致 POST /api/v1/ai/fallback-chain 写入的 priority 无人消费 ——
    用户在界面上配置的降级顺序对运行时零影响（静默失效）。

    ✅ BUG-F-02 复发修复（本轮审查）：
      上一版修复后改为 `WHERE is_active = 1`，但 save_ai_config() / toggle
      都会把「当前使用」做成**全局唯一**（写入前先 `UPDATE ai_config SET is_active=0`），
      于是这条 SQL 恒只返回主配置本身，再被 primary_id 跳过 ——
      降级链**永远为空**，用户配的降级顺序依旧零生效（同一类静默失效）。
      现改为读取全部配置：主配置（is_active=1 且 updated_at 最新，与
      _load_active_config() 口径一致）由 chat_with_fallback() 放在候选首位，
      其余所有「有可用 API Key」的配置按 priority 升序作为降级候选。
    """
    import time as _time
    _now = _time.time()
    if _fallback_cache["data"] is not None and _now - _fallback_cache["ts"] < _config_cache_ttl():
        return _fallback_cache["data"]
    _gen = _cache_generation

    chain: list[dict] = []
    try:
        conn = await get_conn()

        # ✅ 2026-09-23（多环境）：非空环境时只纳入「通用 + 该环境」的候选，
        #    避免生产环境降级调到测试地址（跨环境串味）。
        #    环境为空串时 **不加任何条件** —— 与引入该功能前逐字节一致。
        _env = await resolve_active_env()
        _env_filter = " AND (env = '' OR env = ?)" if _env else ""
        _env_params: tuple = (_env,) if _env else ()

        # 主配置口径与 _load_active_config() 保持一致
        primary_id = ""
        prim_cur = await conn.execute(
            "SELECT id FROM ai_config WHERE is_active = 1" + _env_filter
            + " ORDER BY updated_at DESC LIMIT 1", _env_params)
        prim_row = await prim_cur.fetchone()
        if prim_row:
            primary_id = prim_row[0]

        cur = await conn.execute(
            "SELECT * FROM ai_config WHERE 1=1" + _env_filter
            + " ORDER BY priority ASC, updated_at DESC", _env_params)
        rows = [dict(r) for r in await cur.fetchall()]

        for row in rows:
            if row.get("id") == primary_id:
                continue
            key = decrypt_api_key(row.get("api_key_encrypted", "") or "")
            if not key:
                # ✅ 增强：密文存在但解不开（换过 FERNET_KEY / 删过密钥文件）的
                #    候选此前与「未填 Key」同义静默跳过，排查无从下手 —— 现显式告警。
                if row.get("api_key_encrypted"):
                    logger.warning(
                        "降级候选 %s/%s 已保存的密钥无法解密（常见于更换过 FERNET_KEY"
                        " 或删除过 data/secret_key.key），已跳过",
                        row.get("provider_name", "?"), row.get("model", "?"))
                continue
            chain.append({
                "config_id": row.get("id", ""),
                "provider_name": row.get("provider_name", "openai"),
                "api_key": key,
                "base_url": row.get("base_url", ""),
                "model": row.get("model", ""),
                "max_tokens": row.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"]),
                "temperature": row.get("temperature", DEFAULT_CONFIG_NUMBERS["temperature"]),
                "timeout": row.get("timeout", DEFAULT_CONFIG_NUMBERS["timeout"]),
                # ✅ 每条降级候选各自携带请求方式：主配置配了流式，
                #    不代表降级候选的平台也支持流式，必须逐条生效。
                "request_mode": normalize_request_mode(row.get("request_mode")),
            })
    except Exception:
        # 降级链加载失败不应阻断主流程
        logger.exception("加载 DB 降级链失败，退回内置兜底")

    # ✅ 本轮修复：内置兜底从 chain 中独立出来（见下方「兜底不被截断」）。
    builtin_agnes = None
    if settings.agnes_api_key:
        builtin_agnes = {"provider_name": "agnes", "api_key": settings.agnes_api_key,
                         "base_url": settings.agnes_base_url, "model": settings.agnes_model}

    # ✅ 2026-09-16 新增：根据实时成功率过滤低质量 fallback 候选
    # （主配置由 chat_with_fallback 另行排在首位，这里只处理 fallback 链）
    if chain:
        filtered = []
        for c in chain:
            pname = c.get("provider_name", "")
            rate = _provider_success_rate(
                pname, c.get("model", ""), c.get("config_id", ""),
                c.get("base_url", ""))
            if rate is not None and rate < _PROVIDER_MIN_SUCCESS_RATE:
                logger.warning(
                    "fallback 候选 %s 实时成功率 %.1f%% < %.0f%%，已自动排除（样本≥5次）",
                    pname, rate * 100, _PROVIDER_MIN_SUCCESS_RATE * 100)
                continue
            filtered.append(c)
        # 极端情况下（所有 fallback 都被排除）保留前 1 个作为保底
        if not filtered and chain:
            logger.warning("所有 fallback 候选均被过滤，保留首个作为保底")
            filtered = [chain[0]]
        chain = filtered

    # ✅ 本轮修复：截断前先按健康度分档（同档内保持 priority 原顺序）。
    #    运行库实测 13 条配置 priority 全为 0 → 「前 N 个」完全由 updated_at 决定，
    #    4 条重复的 nvidia 死配置（HTTP 404，15 次调用 0 成功）能把有限名额占满，
    #    真正可用的候选反而被截掉。
    def _health_rank(c: dict) -> tuple:
        _t, rate = _provider_reliability_snapshot(
            c.get("provider_name", ""), c.get("model", ""),
            c.get("config_id", ""), c.get("base_url", ""))
        if rate is None:
            return (1, 0.0)   # 无样本：排在「已知健康」之后（冷启动不误杀）
        return (0, -rate)     # 有样本：成功率高的优先

    chain.sort(key=_health_rank)   # 稳定排序：同档仍按 priority

    # ✅ P0-3（2026-09-17）：降级链长度上限。
    #    实测 ai_config 曾配 14 条（priority 全为 0）→ 降级候选 13 个，
    #    主候选失败后被逐个**串行**尝试（每个候选还带着调用方的 300s 超时），
    #    单章一次 AI 调用最坏被放大到外层总预算 660s。
    #    收敛到 N 个（默认 3）后，最坏路径随之收敛。
    _max_chain = int(getattr(settings, "ai_fallback_chain_max", 3) or 3)
    if len(chain) > _max_chain:
        logger.info("降级链收敛：%d → %d 个候选（ai_fallback_chain_max）",
                    len(chain), _max_chain)
        chain = chain[:_max_chain]
    # ✅ 本轮修复：内置兜底必须留在链内。旧实现把它 append 在末尾参与截断，
    #    配置 ≥3 条时兜底恒被 ``chain[:3]`` 切掉 —— 名义上有兜底，实际没有。
    if builtin_agnes and builtin_agnes not in chain:
        chain.append(builtin_agnes)

    if _gen != _cache_generation:
        # 读库期间配置被改动并失效了缓存：不写回，避免把旧顺序钉住一整个 TTL
        logger.debug("降级链读取期间缓存被失效，丢弃本次缓存写入（代际 %d→%d）",
                     _gen, _cache_generation)
        return chain
    _fallback_cache["data"] = chain
    _fallback_cache["ts"] = _time.time()
    return chain


#: 已知「业务场景」白名单与中文名（供配置界面展示可选场景）。
#:
#: ⚠️ 必须与代码中实际使用的 ``scene="..."`` 字面量保持一致 ——
#:    漂移会使「场景模型路由」配不上真实调用（界面配了、运行时不生效），
#:    回归护栏见 ``backend/tests/test_ai_config_security_routing.py::TestKnownScenesDrift``。
#:    新增 AI 调用点时若使用新的 scene，请同步登记在此。
#:    未登记的 scene 仍可正常调用（只是无法为它单独指定模型），因为
#:    ``resolve_scene_config`` 只查路由表、不校验白名单。
KNOWN_SCENES: dict[str, str] = {
    # —— 目录 ——
    # ⚠️ 不设 "outline" 总调度场景：目录链路的每个调用点都已细分打标
    #    （draft / level1 / sublevel / review / fix / adjust / recognition），
    #    再留一个从不使用的"总调度"入口只会让人在「场景模型路由」里
    #    配了它却发现运行时不生效（静默失效）。故此处只登记真实调用点。
    "outline_draft": "目录草稿生成",
    "outline_level1": "一级章节生成",
    "outline_sublevel": "子级章节生成",
    "outline_review": "目录审核",
    "outline_fix": "目录修复",
    "outline_adjust": "目录调整",
    "outline_recognition": "上传目录 AI 识别",
    # —— 正文 ——
    # ⚠️ 不设 "content" 总调度场景：正文链路的每个调用点都已细分打标
    #    （draft / continue / shrink），再留一个从不使用的"总调度"入口只会
    #    让人在「场景模型路由」里配了它却发现运行时不生效（静默失效）——
    #    与上方「outline」总调度场景的处理完全同口径。
    #    ✅ 修复（2026-09-26）：该条目曾以「白名单会校验它确实被引用，故保留」
    #    为由长期留在 KNOWN_SCENES，但代码中**无任何 scene="content" 调用点**
    #    （正文只用 content_draft / content_continue / content_shrink），
    #    注释与事实相反，属 AGENTS.md §4.5 明令禁止的「界面死选项」。
    #    由 tests/test_ai_config_security_routing.py::TestKnownScenesDrift
    #    的双向护栏查出。存量 ai_scene_routes 行不受影响：
    #    scene_routes.py::_scene_items 有「白名单外场景也要展示」的兜底分支。
    "content_draft": "正文生成",
    "content_continue": "正文续写扩充",
    "content_shrink": "正文压缩",
    "word_budget_alloc": "章节字数预算分配",
    # —— 事实 / 项目信息 ——
    "facts_extract": "全局事实提取",
    # ✅ 2026-09-30 第十三轮：知识库补充 / 最终整理两个新增 AI 调用点。
    #    必须登记，否则「场景模型路由」里配了它们也不生效（静默失效），
    #    且双向漂移护栏 TestKnownScenesDrift 会失败。
    "facts_knowledge_patch": "全局事实·知识库补充",
    "facts_finalize": "全局事实·最终整理",
    "global_facts_adjust": "全局事实调整",
    "bid_analysis": "项目信息提取",
    "bid_analysis_merge": "项目信息合并",
    "bid_section_extract": "项目信息分片提取",
    "bid_section_merge": "项目信息分片合并",
    # —— 一致性 ——
    "consistency_scan": "全文一致性扫描",
    "consistency_repair": "一致性问题修复",
    "consistency_arbitrate": "冲突裁决",
    # —— 审核 / 预检 ——
    "compliance_check": "规范符合性检查",
    "expert_review": "专家论证预检",
    "consistency_audit": "全文一致性审计（预检）",
    "review_autofix": "审核预检问题自动修复",
    # —— 图表 / 配图 ——
    "chart_fix": "图表（Mermaid/图表数据）修复",
    "image_prompt_optimize": "配图提示词优化",
}


async def load_scene_routes() -> dict[str, str]:
    """读取「场景 → 配置 id」路由表（带缓存 + 代际防抖）。

    表不存在 / 为空时返回空 dict —— 调用方据此回落主配置，
    即**默认行为与未引入该功能时完全一致**（向后兼容）。
    """
    import time as _time
    _now = _time.time()
    if _scene_route_cache["data"] is not None and _now - _scene_route_cache["ts"] < _config_cache_ttl():
        return _scene_route_cache["data"]
    gen = _cache_generation
    routes: dict[str, str] = {}
    try:
        conn = await get_conn()
        cur = await conn.execute(
            "SELECT scene, config_id FROM ai_scene_routes WHERE config_id != ''")
        for row in await cur.fetchall():
            r = dict(row)
            scene = (r.get("scene") or "").strip()
            cid = (r.get("config_id") or "").strip()
            if scene and cid:
                routes[scene] = cid
    except Exception as e:
        # 旧库尚未建表（未重启触发 _migrate）时不应影响 AI 调用主链路
        logger.debug("场景路由表不可读（忽略，场景路由不生效）: %s", e)
        routes = {}
    if gen == _cache_generation:
        _scene_route_cache["data"] = routes
        _scene_route_cache["ts"] = _now
    return routes


async def resolve_scene_config(scene: str) -> dict | None:
    """按场景取「路由指定」的配置行；未配置 / 配置已删时返回 None。

    ✅ 2026-09-23 新增（补齐「模型路由」缺口）：引入前所有场景共用同一条
       「当前使用」配置（``scene`` 只写审计、不参与选模型），无法做到
       「正文生成用长文模型、事实提取用快模型」。现支持按场景指定配置，
       **未配置的场景行为完全不变**。
    """
    scene = (scene or "").strip()
    if not scene:
        return None
    try:
        routes = await load_scene_routes()
    except Exception:
        return None
    cid = routes.get(scene)
    if not cid:
        return None
    try:
        conn = await get_conn()
        cur = await conn.execute("SELECT * FROM ai_config WHERE id = ?", (cid,))
        row = await cur.fetchone()
    except Exception as e:
        logger.debug("场景路由取配置失败（scene=%s）: %s", scene, e)
        return None
    if not row:
        # 配置被删除后路由残留：静默回落主配置（避免 AI 全线失败）
        logger.warning("场景 %s 路由到的配置 %s 已不存在，回落主配置", scene, cid)
        return None
    cfg = dict(row)
    # ✅ 2026-09-23（多环境）：路由指向的配置若属于**其它环境**，视为未配置 ——
    #    否则切了环境后场景仍会打到别环境的地址，与「环境隔离」的预期相反。
    env = await resolve_active_env()
    if env and str(cfg.get("env") or "").strip() not in ("", env):
        logger.warning(
            "场景 %s 路由到的配置 %s 属于环境 %s（当前环境 %s），跳过场景路由并回落主配置",
            scene, cid, cfg.get("env"), env)
        return None
    return cfg


async def get_vision_providers() -> list[BaseProvider]:
    """返回当前配置中「支持视觉输入（图片）」的 Provider 实例列表（主配置优先）。

    用途：为 OCR 的视觉识别通道提供候选模型 —— 扫描件/图片型 PDF 可直接交给
    多模态大模型识别，无需本地安装 tesseract。无视觉模型时返回空列表。
    """
    candidates: list[dict] = []
    cfg = await _load_active_config()
    primary_key = _primary_api_key(cfg) if cfg else ""
    if cfg and primary_key:
        candidates.append({
            "provider_name": cfg.get("provider_name", "openai"),
            "api_key": primary_key,
            "base_url": cfg.get("base_url", ""),
            "model": cfg.get("model", ""),
            "max_tokens": cfg.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"]),
            "temperature": 0.0,
            "timeout": cfg.get("timeout", DEFAULT_CONFIG_NUMBERS["timeout"]),
        })
    for c in await _fallback_chain():
        if c.get("api_key") and c not in candidates:
            candidates.append(c)

    out: list[BaseProvider] = []
    for c in candidates:
        try:
            provider = _build_provider(
                c["provider_name"], c["api_key"], c["base_url"], c["model"],
                max_tokens=c.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"]),
                temperature=0.0,
                timeout=c.get("timeout", DEFAULT_CONFIG_NUMBERS["timeout"]),
            )
        except Exception:
            continue
        try:
            if provider.supports_vision():
                out.append(provider)
        except Exception:
            continue
    return out


_AUDIT_INSERT_FULL = (
    "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
    " prompt_tokens, completion_tokens, cached_tokens, duration, success, error, scene,"
    " config_id, base_url)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
# 旧库缺 config_id/base_url 列时的降级写入（保留 scene）
_AUDIT_INSERT_NO_IDENTITY = (
    "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
    " prompt_tokens, completion_tokens, cached_tokens, duration, success, error, scene)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?)")
# 更旧库缺 scene 列时的降级写入（审计记录不丢，仅场景/身份标记为空）
_AUDIT_INSERT_LEGACY = (
    "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
    " prompt_tokens, completion_tokens, cached_tokens, duration, success, error)"
    " VALUES (?,?,?,?,?,?,?,?,?,?)")
_audit_legacy_warned = False


async def _flush_audit_rows_once(rows: list) -> None:
    """单批审计行的单次落库尝试（幂等，供 retry_db_op 安全重放）。

    ✅ 修复：_audit_legacy_warned 是模块状态，降级分支必须显式声明 global；
       否则 Python 把赋值判定为局部变量，首次降级会抛 UnboundLocalError，事务
       回滚后整批审计记录丢失。

    ✅ P10 修复（2026-09-23 · 数据流审计）：批量落库走独立写事务连接。
       旧实现在全局共享连接 get_conn() 上 executemany + commit：一批审计行的
       executemany 与其 commit 之间会让出事件循环，并发协程若在同一连接上
       执行自己的 DML+commit，会把本批未提交的审计行提前提交（或本批 commit
       连带提交他协程脏写），事务边界交错。write_tx_conn 独占一条池连接，
       异常只回滚本事务、不影响并发协程。
    """
    global _audit_legacy_warned
    async with write_tx_conn() as conn:
        # ✅ 修复（本轮审查）：原 INSERT 只写 6 列，而 ai_audit_logs 的
        #    prompt_tokens / completion_tokens / cached_tokens 三列**从无写入方**，
        #    导致 /ai/stats 的 Token 用量、/ai/audit-logs 的 Token 列恒为 0 ——
        #    「AI 用量统计」整块功能是死的。现补齐三列。
        # ✅ 2026-09-17：补齐 error 列（失败原因摘要），/ai/stats 失败原因分布的数据源。
        # ✅ 2026-09-21：补齐 scene 列（业务场景标记），/ai/stats 按场景聚合的数据源。
        try:
            await conn.executemany(_AUDIT_INSERT_FULL, rows)
        except Exception:
            try:
                # 旧库缺 config_id/base_url：丢弃配置身份，保留 scene
                await conn.executemany(_AUDIT_INSERT_NO_IDENTITY,
                                       [r[:11] for r in rows])
                if not _audit_legacy_warned:
                    _audit_legacy_warned = True
                    logger.error(
                        "ai_audit_logs 缺 config_id/base_url 列（旧库未迁移），"
                        "审计已降级写入；请重启服务触发迁移补列，"
                        "否则可靠性统计无法按配置身份隔离")
            except Exception:
                # 更旧库缺 scene：再降一级，审计行不丢
                await conn.executemany(_AUDIT_INSERT_LEGACY,
                                       [r[:10] for r in rows])
                if not _audit_legacy_warned:
                    _audit_legacy_warned = True
                    logger.error(
                        "ai_audit_logs 缺 scene/config_id/base_url 列（旧库未迁移），"
                        "审计已降级写入；请重启服务以触发迁移补列")
        await conn.commit()


async def _flush_audit_buffer() -> None:
    """批量落库审计缓冲（P0-4：单次 executemany + 单次 commit，失败记告警）

    ✅ 2026-09-22（调用次数优化 O1）：运行库可能未迁移出 ``scene`` 列
       （实测 2026-09-22 的运行库仍是 10 列旧表结构），含 scene 的 11 列
       INSERT 会整批抛 OperationalError —— 旧实现被外层 except 吞掉，
       **审计日志从此全量静默丢失**。现降级兜底：整批失败时自动改用
       10 列旧语句重试一次，丢的是 scene 标记而不是全部审计记录。

    ✅ 2026-09-23（正文生成深度审计 · P1 数据丢失）：整批落库此前对
       `database is locked` 零重试。运行库实测 2026-09-23 20:47:20
       （logs/backend.log，trace=9fe4498339da）
       "AI 审计日志批量落库失败（丢失 1 条）: database is locked" ——
       外层 except 直接吞掉，审计记录**永久丢失**且不可追补
       （/ai/stats 的调用量/成功率/token 用量从此少算）。接入共享重试：
       只重试瞬态锁错误，业务错误（如缺列）仍走上方 legacy 降级路径。
    """
    global _audit_legacy_warned
    with _audit_lock:
        if not _audit_buffer:
            return
        rows = list(_audit_buffer)
        _audit_buffer.clear()
        _audit_last_flush["ts"] = time.time()
    try:
        await retry_db_op(lambda: _flush_audit_rows_once(rows))
    except Exception as e:
        # ✅ 审计日志不应静默丢失：落库失败至少留下告警（含条数与原因）
        logger.warning("AI 审计日志批量落库失败（丢失 %d 条）: %s", len(rows), e)


def is_non_retryable_error(err) -> bool:
    """判断 AI 调用错误是否为「确定性错误」：重试 100% 无用，应直接放弃。

    ✅ 2026-09-22（调用次数优化 O9）：依据运行库实测的失败原因分布 ——
       `HTTP 402 Insufficient Balance`（225 次）+ `quota_exceeded`（15 次）
       + `Function Not found`（15 次）+ `UnsupportedModel`（10 次）
       + `invalid api key`（认证）等合计约 265 次调用，重试 100% 无用。
    注意：HTTP 429 限流**不在**此列 —— 退避后仍可能成功，走既有的
    限流退避路径（CONTENT_RATE_LIMIT_BACKOFF）。
    """
    s = str(err or "")
    if ("HTTP 402" in s or "HTTP 403" in s or "HTTP 404" in s):
        return True
    low = s.lower()
    return any(tag in low for tag in (
        "insufficient balance", "quota_exceeded", "quota exceeded",
        "exceeded your current quota", "no valid authorization",
        "invalid api key", "unsupportedmodel", "not found for account"))


# ---------- 配额类错误冷却（2026-09-22 · 调用次数优化 O2） ----------
# 运行库实测：840 次 HTTP 429（占 chat 失败的 34.0%）+ 4,459 次熔断空转。
# 配额/认证类错误（429/402/403/404 及 Insufficient Balance 等）命中后，
# 该 provider 在冷却窗口内的后续调用**必然再次失败**——旧实现照发请求，
# 每次白烧一次 HTTP 且加速配额耗尽，进而触发熔断放大。
# 现按 provider 记冷却截止时间：冷却窗口内直接跳过（记 circuit_skipped，
# 不发请求）；每 ai_fail_probe_every_seconds 秒放行一次探测请求，
# 配额恢复后（探测成功）自动接回。
QUOTA_FAIL_COOLDOWN = max(0.0, float(getattr(settings, "ai_fail_cooldown_seconds", 120) or 0))
QUOTA_PROBE_EVERY = max(1.0, float(getattr(settings, "ai_fail_probe_every_seconds", 10) or 10))
_quota_cool_until: dict[str, float] = {}
_quota_last_probe: dict[str, float] = {}
_quota_lock = threading.Lock()


def _is_quota_error(err) -> bool:
    """配额/限流/认证类错误（429 + 确定性错误）：冷却节流的对象。"""
    s = str(err or "")
    return "HTTP 429" in s or is_non_retryable_error(err)


def _quota_cooldown_active(pname: str) -> bool:
    """该 provider 是否处于配额冷却期。

    冷却窗口内每 QUOTA_PROBE_EVERY 秒放行一次探测（更新探测时刻并返回
    False）——冷却的语义是「少打」而不是「一个都不打」，与熔断探测同口径。
    """
    now = time.time()
    with _quota_lock:
        until = _quota_cool_until.get(pname, 0.0)
        if now >= until:
            return False
        if now - _quota_last_probe.get(pname, 0.0) >= QUOTA_PROBE_EVERY:
            _quota_last_probe[pname] = now
            return False
        return True


def _note_quota_failure(pname: str, err) -> None:
    """记录配额类失败：进入冷却窗口。"""
    with _quota_lock:
        _quota_cool_until[pname] = time.time() + QUOTA_FAIL_COOLDOWN
        _quota_last_probe[pname] = time.time()


def _note_quota_success(pname: str) -> None:
    """调用成功：立即清除冷却（配额恢复自动接回）。"""
    with _quota_lock:
        _quota_cool_until.pop(pname, None)
        _quota_last_probe.pop(pname, None)


def reset_quota_cooldown() -> None:
    """清空配额冷却状态（测试隔离 / 配置变更后立即生效用）。"""
    with _quota_lock:
        _quota_cool_until.clear()
        _quota_last_probe.clear()


def _is_thinking_exhausted(err) -> bool:
    """推理模型把 max_tokens 全部耗在思考过程（finish_reason=length、正文为空）。

    2026-09-22（调用次数优化 O3）：运行库实测 101 次该类失败 —— 旧实现
    直接判失败进入降级链，每个推理模型候选各白烧一次。现由调用方识别后
    翻倍 max_tokens 重试一次（仅「正文为空」触发，不影响正常响应）。
    """
    s = str(err or "")
    return "finish_reason=length" in s and "正文为空" in s


# ---------- 批处理按 provider 成功率分级（2026-09-22 · 调用次数优化 O6） ----------
def get_effective_batch_size(base: int, provider_name: str = "",
                             model: str = "", config_id: str = "",
                             base_url: str = "") -> int:
    """按 provider 实时成功率**防御性降批**（O6），返回本次应使用的批大小。

    · ``ai_batch_by_success_rate=False``（默认）→ 原样返回 base（零行为变化）；
    · provider 无足够样本（rate=None）→ 原样返回 base（不干预）；
    · 成功率 < ``ai_batch_min_success_rate``（默认 0.60）→ 强制 1（逐章，质量优先）；
    · 其余 → 原样返回 base（「升批」仍由用户配置显式决定，本函数只降不升）。

    纯函数（依赖注入式的全局快照），可单测。
    """
    base = max(1, int(base))
    if not getattr(settings, "ai_batch_by_success_rate", False):
        return base
    if not provider_name:
        return base
    total, rate = _provider_reliability_snapshot(
        provider_name, model, config_id, base_url)
    if rate is None:
        return base
    threshold = float(getattr(settings, "ai_batch_min_success_rate", 0.60) or 0.0)
    if threshold > 0 and rate < threshold:
        return 1
    return base


async def effective_batch_size(base: int) -> int:
    """O6 异步便捷入口：以当前主配置的 provider 名做分级（读不到则不干预）。"""
    if not getattr(settings, "ai_batch_by_success_rate", False):
        return max(1, int(base))
    try:
        cfg = await _load_active_config()
        pname = str((cfg or {}).get("provider_name") or "")
        return get_effective_batch_size(
            base, pname, str((cfg or {}).get("model") or ""),
            str((cfg or {}).get("id") or ""), str((cfg or {}).get("base_url") or ""))
    except Exception:
        return get_effective_batch_size(base, "")


async def _log_audit(provider_name: str, model: str, action: str,
                     duration: float, success: bool,
                     prompt_tokens: int = 0, completion_tokens: int = 0,
                     cached_tokens: int = 0, error: str = "", scene: str = "",
                     config_id: str = "", base_url: str = ""):
    """记录 AI 调用审计日志（P0-4：攒批落库，避免每次调用一次 fsync commit）

    token 统计为可选参数（默认 0），保持既有 5 位置参数调用方式兼容。

    ✅ 2026-09-16 新增：同时更新进程内实时可靠性统计，供 _fallback_chain
    自动排除低成功率 provider。

    ✅ 2026-09-17 新增：``error`` 记录失败原因摘要（前 300 字符）——
    实测近 7 天 1061 条失败记录零信息，无法区分 429 限流/超时/认证失败，
    排障只能翻 stderr。落库后 /ai/stats 可输出「失败原因 TOP」分布。

    ✅ 2026-09-21 新增：``scene`` 业务场景标记（如 outline_draft /
    outline_review），/ai/stats 按场景聚合，定位「哪一步在烧调用」。
    """
    try:
        # ✅ 更新实时可靠性统计（thread-safe）
        # ✅ 2026-09-17：熔断跳过（circuit_skipped）不是真实网络调用，
        #    不计入可靠性统计 —— 否则被熔断的 provider 陷入
        #    「跳过 → fail+1 → 成功率更低 → 更难出冷却期」的恶性循环。
        # ✅ 2026-09-23：运行时开关禁用（provider_disabled）同理 ——
        #    被用户主动关掉的厂商若计入失败，重新启用后会被「死配置剔除」
        #    继续排除，表现为「开了也没用」。
        if action not in ("circuit_skipped", "provider_disabled"):
            key = _reliability_key(provider_name, model, config_id, base_url)
            with _provider_reliability_lock:
                stats = _provider_reliability.setdefault(key, {"ok": 0, "fail": 0})
                # 滑动窗口：满 2*N 时压缩一半，保证不会无限膨胀
                if stats["ok"] + stats["fail"] >= _PROVIDER_RELIABILITY_WINDOW * 2:
                    stats["ok"] //= 2
                    stats["fail"] //= 2
                if success:
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
        # 原有审计缓冲逻辑
        with _audit_lock:
            _audit_buffer.append((
                str(uuid.uuid4()), provider_name, model, action,
                int(prompt_tokens or 0), int(completion_tokens or 0), int(cached_tokens or 0),
                duration, 1 if success else 0,
                (error or "")[:300], (scene or "")[:64],
                (config_id or "")[:64], (base_url or "")[:512]))
            buf_len = len(_audit_buffer)
            due = time.time() - _audit_last_flush["ts"] >= _AUDIT_FLUSH_INTERVAL
        if buf_len >= _AUDIT_BATCH_SIZE or (buf_len and due):
            await _flush_audit_buffer()
    except Exception:
        pass


def _reliability_key(provider_name: str, model: str = "",
                     config_id: str = "", base_url: str = "") -> str:
    """可靠性统计身份键：优先 config_id；否则 provider+model+base_url。

    同一 provider 可以有多条配置、多个模型或不同接入地址，只在 provider 维度
    统计会把它们的成败混在一起，导致「一条坏配置拖死整个厂商」。
    """
    cid = (config_id or "").strip()
    if cid:
        return f"cfg:{cid}"
    return (f"prov:{(provider_name or '').strip()}"
            f"|model:{(model or '').strip()}"
            f"|url:{(base_url or '').strip()}")


def _provider_reliability_snapshot(provider_name: str, model: str = "",
                                   config_id: str = "",
                                   base_url: str = "") -> tuple[int, float | None]:
    """返回 (样本数, 实时成功率)；样本不足（< ``_PROVIDER_MIN_SAMPLES``）时成功率为 None。"""
    key = _reliability_key(provider_name, model, config_id, base_url)
    with _provider_reliability_lock:
        stats = _provider_reliability.get(key)
        if not stats:
            # 兼容：历史只按 provider 写入的统计仍可被读取
            stats = _provider_reliability.get(provider_name)
        if not stats:
            return 0, None
        total = stats["ok"] + stats["fail"]
        if total < _PROVIDER_MIN_SAMPLES:
            return total, None
        return total, stats["ok"] / total


def _provider_success_rate(provider_name: str, model: str = "",
                           config_id: str = "", base_url: str = "") -> float | None:
    """返回某个配置身份的实时成功率（0~1），样本不足时返回 None。"""
    return _provider_reliability_snapshot(
        provider_name, model, config_id, base_url)[1]


def _is_dead_provider(provider_name: str, model: str = "",
                      config_id: str = "", base_url: str = "") -> bool:
    """死配置判定：样本充足且实时成功率低于 ``_PROVIDER_DEAD_SUCCESS_RATE``。"""
    total, rate = _provider_reliability_snapshot(
        provider_name, model, config_id, base_url)
    return (rate is not None and total >= _PROVIDER_DEAD_MIN_SAMPLES
            and rate < _PROVIDER_DEAD_SUCCESS_RATE)


def _order_candidates(candidates: list[dict]) -> list[dict]:
    """候选排序与死配置剔除（P0-1）。

    规则（顺序执行）：
    1. **剔除死配置**：样本达标且成功率极低的候选直接移除（保底保留至少 1 个）；
    2. **主配置后置**：主配置实时成功率低于 ``_PROVIDER_DEMOTE_SUCCESS_RATE`` 时
       移到候选链尾部，让高成功率候选先跑。未触发阈值时主配置仍在首位
       （保持"用户选择的供应商优先"语义）。
    """
    if not candidates:
        return candidates

    alive = [c for c in candidates
             if not _is_dead_provider(
                 c.get("provider_name", ""), c.get("model", ""),
                 c.get("config_id", ""), c.get("base_url", ""))]
    if not alive:
        alive = list(candidates)  # 全部被判定为死配置 → 保底不空
    if len(alive) < len(candidates):
        dropped = [c.get("provider_name", "?")
                   for c in candidates if c not in alive]
        logger.warning("已剔除实时成功率过低的候选：%s", ", ".join(dropped))

    primary = [c for c in alive if c.get("_is_primary")]
    rest = [c for c in alive if not c.get("_is_primary")]
    if primary and rest:
        _total, rate = _provider_reliability_snapshot(
            primary[0].get("provider_name", ""), primary[0].get("model", ""),
            primary[0].get("config_id", ""), primary[0].get("base_url", ""))
        if rate is not None and rate < _PROVIDER_DEMOTE_SUCCESS_RATE:
            logger.warning(
                "主配置 %s 实时成功率 %.1f%% < %.0f%%，本次调用将其后置到候选链尾部",
                primary[0].get("provider_name"), rate * 100,
                _PROVIDER_DEMOTE_SUCCESS_RATE * 100)
            return rest + primary
    return alive


async def flush_audit_buffer() -> None:
    """公开入口：冲刷审计日志缓冲（周期任务/进程退出时调用）。"""
    await _flush_audit_buffer()


async def audit_flush_loop(stop_event: asyncio.Event) -> None:
    """周期冲刷未满 50 条的审计缓冲；由应用 lifespan 启停。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_AUDIT_FLUSH_INTERVAL)
        except asyncio.TimeoutError:
            await _flush_audit_buffer()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("AI 审计周期冲刷失败（下一周期重试）: %s", e)


async def warmup_reliability_from_db() -> None:
    """✅ 2026-09-17 新增：进程启动时从审计表预热实时可靠性统计。

    动机：``_provider_reliability`` 是进程内存态，重启即清零 ——
    实测 volcengine 近 7 天 47 次调用 100% 失败、zhipu 96.6% 失败，
    每次重启后死配置的历史全部遗忘，降级链又会先白打一轮
    （每轮白等一次网络往返/超时）。启动时用最近 24h 的真实调用记录
    恢复统计，死配置在第一次调用前就会被 ``_order_candidates`` 剔除。

    口径：只统计 action='chat' 的真实调用（排除 circuit_skipped 假失败）；
    每个 provider 按比例缩放到滑动窗口 50 内（保持成功率比例）。
    失败不影响启动（预热只是优化，不是依赖）。
    """
    try:
        conn = await get_conn()
        try:
            cur = await conn.execute(
                "SELECT config_id, provider_name, model, base_url,"
                " SUM(success), COUNT(*) - SUM(success) "
                "FROM ai_audit_logs "
                "WHERE action='chat' AND created_at >= datetime('now','localtime','-1 day') "
                "GROUP BY config_id, provider_name, model, base_url HAVING COUNT(*) >= ?",
                (_PROVIDER_MIN_SAMPLES,))
            has_identity = True
        except Exception:
            # 旧库尚无 config_id/base_url 列：回退 provider+model 聚合
            cur = await conn.execute(
                "SELECT '', provider_name, model, '', SUM(success), COUNT(*) - SUM(success) "
                "FROM ai_audit_logs "
                "WHERE action='chat' AND created_at >= datetime('now','localtime','-1 day') "
                "GROUP BY provider_name, model HAVING COUNT(*) >= ?",
                (_PROVIDER_MIN_SAMPLES,))
            has_identity = False
        rows = await cur.fetchall()
        if not rows:
            return
        loaded = 0
        with _provider_reliability_lock:
            for r in rows:
                cid, pname, model, base_url = r[0], r[1], r[2], r[3]
                ok = max(0, int(r[4] or 0))
                fail = max(0, int(r[5] or 0))
                total = ok + fail
                if total <= 0:
                    continue
                # 按比例缩放到滑动窗口内，保持成功率语义一致
                if total > _PROVIDER_RELIABILITY_WINDOW:
                    ok = round(ok * _PROVIDER_RELIABILITY_WINDOW / total)
                    fail = _PROVIDER_RELIABILITY_WINDOW - ok
                key = _reliability_key(pname, model, cid, base_url)
                _provider_reliability[key] = {"ok": ok, "fail": fail}
                loaded += 1
        logger.info(
            "已从审计日志预热 %d 个配置身份的可靠性统计（近 24h%s）",
            loaded, "" if has_identity else "，旧库按 provider+model 聚合")
    except Exception as e:
        logger.debug("预热 Provider 可靠性统计失败（不影响服务）: %s", e)


def _ai_live_record(provider_name: str, model: str, ok: bool, duration: float):
    """记录一次 AI 调用尝试的收尾状态（成败/耗时/Provider），in_flight 同步递减。"""
    with _ai_live_lock:
        _ai_live["in_flight"] = max(0, int(_ai_live["in_flight"]) - 1)
        _ai_live["last_provider"] = provider_name
        _ai_live["last_model"] = model
        _ai_live["last_ok"] = ok
        _ai_live["last_duration"] = duration
        _ai_live["last_at"] = time.time()
        if not ok:
            _ai_live["failed"] = int(_ai_live["failed"]) + 1
    _ab.notify()


def extract_usage(provider) -> dict:
    """从 Provider 最近一次响应中取出标准化的 token 用量。

    Provider 侧已归一化为 {prompt_tokens, completion_tokens, cached_tokens}；
    取不到（流式、厂商未返回 usage）时返回全 0，绝不影响主流程。
    """
    empty = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    try:
        usage = getattr(provider, "last_usage", None) or {}
        if not isinstance(usage, dict):
            return empty
        return {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
        }
    except Exception:
        return empty


# ---------------------------------------------------------------------------
# JSON 模式（response_format=json_object）兼容性判定 —— 薄包装
# ---------------------------------------------------------------------------
def _json_mode_unsupported(err) -> bool:
    """判断错误是否属于「厂商不支持 response_format(json_object)」（薄包装）。

    ✅ 2026-10-01：判据本体已下沉到 ``services/ai/json_mode_compat.py``
    （唯一事实源）。此前本模块与 ``providers/openai_compatible.py`` 各有一份
    判据、且都只认英文关键词 —— 国内厂商返回中文错误（「该模型暂不支持 JSON
    输出」等）时两端都漏判：不摘字段重试、不回退普通模式，且把「参数不兼容」
    计入熔断失败与配额冷却，整条候选链用同一原因逐个失败，JSON 类任务全挂。

    本函数**保留原名**（唯一调用点零改动），只做转发 —— 新增厂商 / 新增报错
    形态时只改 json_mode_compat，禁止在此或 providers 侧内联关键词
    （护栏：tests/test_json_mode_unsupported_20261001.py）。

    注：关键字表随之从本模块移除，``_JSON_MODE_*`` 别名不再导出
    （无其它调用点；重复导出会诱导后人绕过唯一事实源）。
    """
    return json_mode_unsupported(err)


def _error_summary(e: Exception) -> str:
    """失败原因摘要（落审计用）。

    ✅ 修复（本轮审查）：旧实现直接写 ``str(e)[:300]``，而 httpx 的超时类异常
      （``ReadTimeout`` / ``ConnectTimeout`` / ``TimeoutException``）与
      ``asyncio.CancelledError`` 的 ``str()`` **是空字符串** ——
      实测运行库 2175 条真实失败中 1141 条（52%）error 为空，
      「失败原因 TOP」里完全看不到，排障只能靠猜。
      现兜底为异常类型名，并显式标注「疑似超时」。
    """
    msg = (str(e) or "").strip()
    if msg:
        return msg[:300]
    name = type(e).__name__
    if "Timeout" in name or name == "TimeoutError":
        return f"{name}（无错误信息，常见于请求超时）"
    return f"{name}（无错误信息）"


def _status_from_error(e: Exception) -> int:
    """从异常文本推断 HTTP 状态码（429 优先，其次 ``HTTP <code>``，兜底 500）。"""
    err_str = str(e)
    if "429" in err_str:
        return 429
    if "HTTP " in err_str:
        try:
            return int(err_str.split("HTTP ")[1].split(":")[0])
        except (IndexError, ValueError):
            pass
    return 500


def _candidate_timeout(c: dict, req_timeout: int | None) -> int:
    """单个候选本次尝试的超时（秒）。

    ✅ P0-3（2026-09-17）：主候选用调用方的完整预算（正文生成 300s，长正文需要）；
    降级候选套用 ``ai_fallback_attempt_timeout`` 上限（默认 120s），
    不再把 300s 下发到每一个降级候选造成"超时叠乘"。
    """
    base = int(req_timeout if req_timeout is not None
               else (c.get("timeout") or DEFAULT_CONFIG_NUMBERS["timeout"]))
    if not c.get("_is_primary"):
        cap = int(getattr(settings, "ai_fallback_attempt_timeout", 120) or 120)
        base = min(base, cap)
    return max(1, base)


async def _collect_stream(provider, messages: list, *,
                          temperature: float | None,
                          json_mode: bool,
                          max_tokens: int | None,
                          extra_body: dict | None = None) -> str:
    """消费 ``provider.stream()`` 并把分片拼成完整正文。

    ✅ 新增（请求方式 = 流式请求）：

    流式只改变**后端与厂商之间**的数据传输方式 —— 首个分片到达更快，
    网关/代理不容易因长时间无数据而掐断连接，长正文生成更稳；
    但对上层仍是同步语义：**拼完整个响应才返回**，
    应用侧（章节正文、事实提取、图表生成等）继续流程的时机完全不变。

    失败或空流一律抛异常，交由 ``_attempt_candidate`` 走
    「回退普通请求 → 降级下一候选」，绝不把半截内容当成完整结果。
    """
    parts: list[str] = []
    async for piece in provider.stream(messages, temperature=temperature,
                                       json_mode=json_mode, max_tokens=max_tokens,
                                       extra_body=extra_body):
        if piece:
            parts.append(piece)
    text = "".join(parts)
    if not text.strip():
        # 空流与 chat 路径的空内容同义：必须报错，否则会被当成「成功但内容为空」
        finish = str(getattr(provider, "last_finish_reason", "") or "").lower()
        if finish in ("length", "max_tokens"):
            raise RuntimeError(
                f"流式响应被 max_tokens 截断且正文为空（finish_reason={finish}）："
                f"请调大 max_tokens 或改用非推理模型")
        raise RuntimeError(
            "流式请求未返回任何内容（该平台可能不支持 stream 流式接口，"
            "或流式参数受限）")
    return text


# ---------- 流式能力记忆（避免每章白跑一次必然失败的流式请求） ----------
# 依据运行日志：部分平台不支持 stream（HTTP 400: stream not supported / 空流），
# 旧实现每次调用都先流式失败一次再回退普通请求 —— 正文生成 N 章就白跑 N 次 HTTP，
# 且每次都要多等一次超时窗口。现按 provider+model 记忆「连续流式失败」：
# 连续 STREAM_FAIL_STREAK 次后，在 STREAM_CAPABILITY_TTL 秒内直接走普通请求
# （流式只改变与厂商之间的传输方式，应用侧语义完全不变）；流式一旦成功立即
# 清除记忆；TTL 到期后自动重新探测一次（平台修复/换模型可自愈）。
STREAM_FAIL_STREAK = 2          # 连续失败几次才认定不支持（单次要能容忍偶发抖动）
# ✅ 2026-09-22（调用次数优化 O5）：TTL 配置化并延长。3600s → 86400s（1 天）：
#    模型对流式的支持不会一天内变化，缩短只会增加「TTL 到期后每章重探测一次
#    必败流式」的浪费；到期自动重探测与成功即清除的机制保持不变（可自愈）。
STREAM_CAPABILITY_TTL = max(
    1.0, float(getattr(settings, "ai_stream_capability_ttl_seconds", 86400) or 86400.0))

# ✅ R4 修复（2026-09-22）：流式请求命中确定性错误时是否回退普通请求。
# 默认 True = 不回退（确定性错误对同一 provider 非流式同样 100% 失败，重试白烧 HTTP）。
# False = 旧行为（一律回退普通请求重试），可通过环境变量关闭以恢复旧行为。
ai_retry_on_non_retryable = bool(
    getattr(settings, "ai_retry_on_non_retryable", True))

_stream_fail_streak: dict[str, int] = {}
_stream_unsupported_at: dict[str, float] = {}
_stream_cap_lock = threading.Lock()


def _stream_capability_key(provider) -> str:
    """能力记忆的键：provider + model（同一 provider 换模型需重新判定）。"""
    return f"{getattr(provider, 'name', '?')}|{getattr(provider, 'model', '')}"


def _stream_disabled(key: str) -> bool:
    """该 provider 近期是否已被判定「流式不可用」且仍在有效期内。"""
    with _stream_cap_lock:
        at = _stream_unsupported_at.get(key)
    if not at:
        return False
    if time.time() - at < STREAM_CAPABILITY_TTL:
        return True
    # TTL 到期：清除记忆，下次重新探测（不永久判死）
    with _stream_cap_lock:
        _stream_unsupported_at.pop(key, None)
        _stream_fail_streak.pop(key, None)
    return False


def _note_stream_failure(key: str) -> bool:
    """记录一次流式失败；返回是否已达到「认定不支持」的连续次数。"""
    with _stream_cap_lock:
        n = _stream_fail_streak.get(key, 0) + 1
        _stream_fail_streak[key] = n
        if n >= STREAM_FAIL_STREAK:
            _stream_unsupported_at[key] = time.time()
            return True
    return False


def _note_stream_success(key: str) -> None:
    """流式成功：立即清除失败记忆（平台恢复后自动回到流式）。"""
    with _stream_cap_lock:
        _stream_fail_streak.pop(key, None)
        _stream_unsupported_at.pop(key, None)


def reset_stream_capability() -> None:
    """清空流式能力记忆（测试隔离 / 配置变更后立即重探测用）。"""
    with _stream_cap_lock:
        _stream_fail_streak.clear()
        _stream_unsupported_at.clear()


async def _call_provider(provider, messages: list, *, request_mode: str,
                         temperature: float | None, json_mode: bool,
                         max_tokens: int | None,
                         extra_body: dict | None = None) -> str:
    """按配置的「请求方式」向厂商发起一次调用，返回完整正文。

    - ``normal``：普通请求（等待完整响应），与历史行为一致；
    - ``stream``：流式请求，边收边拼，收完才返回。

    ✅ 增强：流式失败（平台不支持 stream / 流式参数受限 / 空流 / 流式专属
      限流）时**自动用普通请求重试一次**，与「测试连接」的探测口径保持一致 ——
      否则用户为了更快首字节勾了流式，反而整条链路直接失败，
      而同样的配置用普通请求其实完全可用（请求方式不该成为可用性开关）。
    """
    if request_mode != "stream":
        return await provider.chat(messages, temperature=temperature,
                                   json_mode=json_mode, max_tokens=max_tokens,
                                   extra_body=extra_body)
    _cap_key = _stream_capability_key(provider)
    if _stream_disabled(_cap_key):
        # ✅ 能力记忆命中：省掉一次必然失败的流式 HTTP（正文生成按章累积，收益显著）
        logger.info(
            "Provider %s 近期流式不可用（已记忆 %ds 内不再尝试），本次直接走普通请求",
            getattr(provider, "name", "?"), int(STREAM_CAPABILITY_TTL))
        return await provider.chat(messages, temperature=temperature,
                                   json_mode=json_mode, max_tokens=max_tokens,
                                   extra_body=extra_body)
    try:
        text = await _collect_stream(provider, messages, temperature=temperature,
                                     json_mode=json_mode, max_tokens=max_tokens,
                                     extra_body=extra_body)
        _note_stream_success(_cap_key)
        return text
    except asyncio.CancelledError:
        # 对冲/停止取消不是失败，直接向上传播（由调用方登记实时状态）
        raise
    except Exception as stream_err:
        # ✅ R4 修复（2026-09-22）：确定性错误（402/403/404/quota/invalid key 等）
        # 走非流式同样会失败，直接抛出让上层候选链切换，不再白烧一次 HTTP。
        # 5xx/timeout/网络抖动 仍走原有 fallback（普通请求大概率成功）。
        if ai_retry_on_non_retryable and is_non_retryable_error(stream_err):
            logger.info(
                "流式请求命中确定性错误，跳过普通请求重试直接切换候选（provider=%s）：%s",
                getattr(provider, "name", "?"), str(stream_err)[:160])
            raise
        if _note_stream_failure(_cap_key):
            logger.warning(
                "Provider %s 连续 %d 次流式请求失败，后续 %ds 内直接走普通请求",
                getattr(provider, "name", "?"), STREAM_FAIL_STREAK,
                int(STREAM_CAPABILITY_TTL))
        logger.warning(
            "流式请求失败，自动回退普通请求重试（provider=%s）：%s",
            getattr(provider, "name", "?"), str(stream_err)[:160])
        return await provider.chat(messages, temperature=temperature,
                                   json_mode=json_mode, max_tokens=max_tokens,
                                   extra_body=extra_body)


async def _attempt_candidate(c: dict, messages: list, *,
                             temperature: float | None,
                             json_mode: bool,
                             max_tokens: int | None,
                             req_timeout: int | None,
                             scene: str = "",
                             extra_body: dict | None = None) -> tuple[str, Exception | None]:
    """单候选调用（JSON 模式不被支持时同候选回退普通模式重试一次）。

    成功返回 ``(正文, None)``；失败返回 ``("", 异常)``。
    本函数负责该次尝试的全部记账：实时状态、并发控制器采样、
    熔断器成败、审计日志。被取消时登记实时状态但不计入熔断失败。

    ✅ 新增：按候选自身的 ``request_mode`` 选择普通请求 / 流式请求
      （``_call_provider``），流式只影响厂商间传输方式，不影响上层语义。
    """
    pname = c.get("provider_name", "?")
    base_mt = max_tokens or c.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"])

    def _build(mt: int):
        """按本次尝试的 max_tokens 构建 Provider（O3 思考吞噬重试需要翻倍）。"""
        return _build_provider(pname, c["api_key"], c["base_url"], c["model"],
                               max_tokens=mt,
                               temperature=(temperature if temperature is not None
                                            else c.get("temperature",
                                                       DEFAULT_CONFIG_NUMBERS["temperature"])),
                               timeout=_candidate_timeout(c, req_timeout))

    last_err: Exception | None = None
    request_mode = normalize_request_mode(c.get("request_mode"))
    # ✅ 2026-09-22（调用次数优化 O3）：思考吞噬重试的开关与上限
    _retry_thinking = bool(getattr(settings, "ai_retry_on_thinking_exhausted", True))
    _reasoning_cap = max(0, int(getattr(settings, "ai_reasoning_max_tokens", 4096) or 0))
    # JSON 模式优先：支持则用，被厂商拒绝则同 Provider 立即回退普通模式
    for use_json in ([True, False] if json_mode else [False]):
        mt = base_mt
        while True:  # ✅ O3：思考吞噬时同一 use_json 下翻倍 max_tokens 重试一次
            provider = _build(mt)
            t0 = time.time()
            # ✅ 全局并发限流（P0 修复 2026-09-17）：所有 AI HTTP 请求统一走
            #    concurrency_controller.semaphore，保证全局最多 MAX_CONCURRENCY(5) 个在飞。
            #    此前 sse_handlers 用了 semaphore，但 bid_analysis / facts / export 各自用
            #    独立信号量，极限并发可达 20+，导致 AI 服务大规模 429 限流。
            async with concurrency_controller.semaphore:
                # ✅ 实时状态：进入真实网络调用前计数（熔断跳过/无 Key 不算调用）
                with _ai_live_lock:
                    _ai_live["in_flight"] = int(_ai_live["in_flight"]) + 1
                    _ai_live["total"] = int(_ai_live["total"]) + 1
                _ab.notify()
                try:
                    result = await _call_provider(
                        provider, messages, request_mode=request_mode,
                        temperature=temperature, json_mode=use_json, max_tokens=mt,
                        extra_body=extra_body)
                    # ✅ 空返回视为失败：HTTP 200 但正文为空时，旧实现把空串当成功
                    #    直接 return，既不降级到下一 Provider 也不触发重试 ——
                    #    章节生成表现为「AI 返回空内容」且无任何自愈。现转为异常，
                    #    走下方统一的失败处理（熔断计数 → 尝试下一候选）。
                    if not (result or "").strip():
                        raise RuntimeError(f"Provider {pname} 返回空正文")
                    duration = time.time() - t0
                    _ai_live_record(pname, c.get("model", ""), True, duration)
                    concurrency_controller.record(duration, status=200)
                    concurrency_controller.adjust_concurrency()
                    circuit_breaker.record_success(pname)
                    _note_quota_success(pname)   # ✅ O2：成功即清除配额冷却
                    # ✅ 修复：成功路径补齐 token 用量（原实现不传，用量统计恒为 0）
                    u = extract_usage(provider)
                    await _log_audit(pname, c.get("model", ""), "chat", duration, True,
                                     prompt_tokens=u["prompt_tokens"],
                                     completion_tokens=u["completion_tokens"],
                                     cached_tokens=u["cached_tokens"], scene=scene,
                                     config_id=c.get("config_id", ""),
                                     base_url=c.get("base_url", ""))
                    return result, None
                except asyncio.CancelledError:
                    # ✅ 对冲/停止被取消：只登记实时状态（保证 in_flight 不泄漏），
                    #    不计入熔断失败（取消不是 provider 故障），异常向上传播。
                    _ai_live_record(pname, c.get("model", ""), False, time.time() - t0)
                    raise
                except Exception as e:
                    duration = time.time() - t0
                    status = _status_from_error(e)
                    _ai_live_record(pname, c.get("model", ""), False, duration)
                    concurrency_controller.record(duration, status=status)
                    concurrency_controller.adjust_concurrency()
                    last_err = e
                    # ✅ 2026-09-17：失败原因摘要落审计（/ai/stats 失败原因分布的数据源）
                    await _log_audit(pname, c.get("model", ""), "chat", duration, False,
                                     error=_error_summary(e), scene=scene,
                                     config_id=c.get("config_id", ""),
                                     base_url=c.get("base_url", ""))
                    if use_json and _json_mode_unsupported(str(e)):
                        # 参数不支持 ≠ Provider 故障：不计入熔断，回退普通模式重试
                        logger.warning("Provider %s 不支持 JSON 模式，回退普通模式重试: %s",
                                       pname, str(e)[:160])
                        circuit_breaker.record_success(pname)
                        break
                    # ✅ 2026-09-22（调用次数优化 O3）：推理模型把 max_tokens 全部
                    #    耗在思考过程（finish_reason=length、正文为空）时，翻倍
                    #    max_tokens 重试一次 —— 旧实现直接判失败进入降级链，
                    #    每个推理模型候选各白烧一次（运行库实测 101 次）。
                    if _retry_thinking and _is_thinking_exhausted(e):
                        nxt = (min(mt * 2, _reasoning_cap)
                               if _reasoning_cap > 0 else mt * 2)
                        if nxt > mt:
                            logger.warning(
                                "Provider %s 思考吞噬 max_tokens=%d（正文为空），"
                                "翻倍至 %d 重试一次", pname, mt, nxt)
                            mt = nxt
                            # ✅ 2026-09-23（正文生成深度审计 · P1 调用效率）：
                            #    严格「重试一次」。旧实现只靠 `nxt > mt` 与
                            #    ai_reasoning_max_tokens 上限隐式收敛，当
                            #    base_mt < cap/2 时会连续翻倍两次——正文生成
                            #    的 max_tokens 恰好是 1536（MAX_TOKENS_FLOOR），
                            #    4096 上限下走 1536→3072→4096 两跳；运行库实测
                            #    2026-09-23 20:54~20:55（logs/backend.log，
                            #    trace=d283bb90c239）单个正文任务在 75 秒内打出
                            #    6 条「翻倍至 3072 重试一次」全部失败后被取消，
                            #    即每候选多烧 1 次完整网络往返（数十秒 × 候选链 ×
                            #    章节重试）。现显式置位，与注释「重试一次」和
                            #    test_cap_blocks_second_retry 的意图对齐。
                            _retry_thinking = False
                            continue
                    # ✅ 2026-09-22（调用次数优化 O2）：配额类错误记入冷却窗口，
                    #    窗口内该 provider 的后续调用直接跳过（不再打配额）。
                    if QUOTA_FAIL_COOLDOWN > 0 and _is_quota_error(e):
                        _note_quota_failure(pname, e)
                    circuit_breaker.record_failure(pname, is_429=(status == 429))
                    logger.error("Provider %s 调用失败: %s", pname, e)
                    return "", e
    return "", last_err


async def _resolve_chat_config(ai_config: dict | None, scene: str) -> dict | None:
    """候选编排阶段 1：解析本次调用使用的配置行。

    优先级：调用方显式 ``ai_config`` > 场景路由 > 当前环境主配置。
    场景路由表为空时行为与引入该功能前一致。
    """
    if ai_config is not None:
        return ai_config
    return await resolve_scene_config(scene) or await _load_active_config()


async def _build_candidates(cfg: dict | None) -> list[dict]:
    """候选编排阶段 2：构建主候选 + 降级链，并按配置 ID 去重后排序。

    场景路由配置同时也会出现在全局降级链中；按 ``config_id`` 去重可避免
    同一配置因 ``_is_primary`` 等派生字段不同而被尝试两次。
    """
    candidates: list[dict] = []
    is_primary_set = False
    if cfg:
        _pk = _primary_api_key(cfg)
        # ✅ 增强：密文存在但解不开（换过 FERNET_KEY / 删过密钥文件）时，
        #    旧实现与「未填 Key」不可区分，_gate_candidates 全跳过后报
        #    「没有配置任何有 API Key 的 Provider」，把用户指向完全错误的
        #    排查方向。现打 key_broken 标记，门控处分开计数并如实归因。
        _enc = str(cfg.get("api_key_encrypted") or "")
        candidates.append({
            "config_id": cfg.get("id", ""),
            "provider_name": cfg.get("provider_name", "openai"),
            "api_key": _pk,
            "base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
            "max_tokens": cfg.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"]),
            "temperature": cfg.get("temperature", DEFAULT_CONFIG_NUMBERS["temperature"]),
            "timeout": cfg.get("timeout", DEFAULT_CONFIG_NUMBERS["timeout"]),
            "request_mode": normalize_request_mode(cfg.get("request_mode")),
            "key_broken": bool(_enc) and not _pk,
            "_is_primary": True,
        })
        is_primary_set = True

    seen_config_ids: set[str] = set()
    if cfg:
        primary_id = str(cfg.get("id") or "").strip()
        if primary_id:
            seen_config_ids.add(primary_id)
    for c in await _fallback_chain():
        cid = str(c.get("config_id") or "").strip()
        if cid and cid in seen_config_ids:
            continue
        if cid:
            seen_config_ids.add(cid)
        c2 = dict(c)
        if is_primary_set:
            c2["_is_primary"] = False
        candidates.append(c2)

    # 死配置剔除 + 主配置低成功率后置
    return _order_candidates(candidates)


async def _gate_candidates(candidates: list[dict],
                           scene: str) -> tuple[list[dict], Exception | None]:
    """候选编排阶段 3：可用性门禁。

    依次过滤：
      1. 无 API Key；
      2. 运行时厂商开关禁用；
      3. 配额冷却；
      4. 熔断器 OPEN。

    全部不可用时给出可执行错误；熔断全跳过时按既有语义放行一次探测。
    """
    usable: list[dict] = []
    cb_skipped: list[dict] = []
    skipped_no_key = 0
    skipped_key_broken = 0
    skipped_cb = 0
    skipped_disabled = 0
    _disabled = await resolve_disabled_providers()
    last_err: Exception | None = None

    for c in candidates:
        pname = c.get("provider_name", "?")
        if not c.get("api_key"):
            # ✅ 增强：「密文存在但解不开」与「未填 Key」分开计数 ——
            #    两者的修法完全不同（重填 Key vs 检查加密密钥），混报会误导排查。
            if c.get("key_broken"):
                skipped_key_broken += 1
                logger.warning("候选 %s 已保存的密钥无法解密，跳过本次调用", pname)
            else:
                skipped_no_key += 1
            continue
        if pname in _disabled:
            skipped_disabled += 1
            logger.warning("厂商 %s 已被运行时开关禁用，跳过本次调用", pname)
            await _log_audit(pname, c.get("model", ""), "provider_disabled", 0.0,
                             False, error="运行时开关已禁用该厂商", scene=scene,
                             config_id=c.get("config_id", ""),
                             base_url=c.get("base_url", ""))
            if last_err is None:
                last_err = RuntimeError(f"Provider {pname} 已被运行时开关禁用")
            continue
        if QUOTA_FAIL_COOLDOWN > 0 and _quota_cooldown_active(pname):
            skipped_cb += 1
            cb_skipped.append(c)
            logger.warning("配额冷却中，跳过 %s（冷却窗口 %ds）",
                           pname, int(QUOTA_FAIL_COOLDOWN))
            await _log_audit(pname, c.get("model", ""), "circuit_skipped", 0.0,
                             False, error="配额冷却，跳过本次调用", scene=scene,
                             config_id=c.get("config_id", ""),
                             base_url=c.get("base_url", ""))
            if last_err is None:
                last_err = RuntimeError(f"Provider {pname} 处于配额冷却中")
            continue
        if not circuit_breaker.allow_request(pname):
            skipped_cb += 1
            cb_skipped.append(c)
            logger.warning("熔断器 OPEN，跳过 %s", pname)
            await _log_audit(pname, c.get("model", ""), "circuit_skipped", 0.0,
                             False, error="熔断器 OPEN，跳过本次调用", scene=scene,
                             config_id=c.get("config_id", ""),
                             base_url=c.get("base_url", ""))
            if last_err is None:
                last_err = RuntimeError(f"Provider {pname} 被熔断器跳过")
            continue
        usable.append(c)

    if usable:
        return usable, last_err

    if skipped_disabled > 0 and skipped_cb == 0 and skipped_no_key == 0:
        raise RuntimeError(
            "所有候选 Provider 均被运行时开关禁用（disabled_providers="
            f"{sorted(_disabled)}），请在「文本模型配置 → 运行时厂商开关」恢复后再试")
    if skipped_cb > 0 and cb_skipped:
        probeable = [
            c for c in cb_skipped
            if not (QUOTA_FAIL_COOLDOWN > 0
                    and _quota_cooldown_active(c.get("provider_name", "")))]
        if not probeable:
            raise RuntimeError(
                "所有候选 Provider 均处于熔断/配额冷却中"
                f"（探测节流 {int(QUOTA_PROBE_EVERY)}s），请稍后重试 "
                f"(skipped_no_key={skipped_no_key}, skipped_cb={skipped_cb})")
        probe = min(probeable,
                    key=lambda c: circuit_breaker.cooldown_remaining(
                        c.get("provider_name", "")))
        _pname = probe.get("provider_name", "")
        _remain = circuit_breaker.cooldown_remaining(_pname)
        circuit_breaker.force_probe(_pname)
        usable.append(probe)
        logger.warning(
            "全部候选均处于熔断冷却中，放行 %s 做一次探测（原剩余冷却 %.0fs）",
            _pname, _remain)
        return usable, last_err
    if skipped_cb > 0:
        raise RuntimeError(
            "所有候选 Provider 均被熔断器跳过，请等待冷却重试 "
            f"(skipped_no_key={skipped_no_key}, skipped_cb={skipped_cb})")
    if skipped_key_broken and not skipped_no_key:
        raise RuntimeError(
            "候选 Provider 已保存的 API Key 无法解密（常见于更换过加密密钥 "
            "FERNET_KEY 或删除过 data/secret_key.key），"
            "请在「文本模型配置」重新填写并保存 Key")
    if skipped_no_key > 0 or skipped_key_broken > 0:
        raise RuntimeError(
            "没有配置任何有 API Key 的 Provider"
            + (f"（其中 {skipped_key_broken} 条已保存的密钥无法解密，"
               "请重新填写或删除该配置）" if skipped_key_broken else ""))
    raise RuntimeError(f"所有 AI 提供商调用失败：{last_err}")


async def chat_with_fallback(messages: list, ai_config: dict | None = None, *,
                             temperature: float | None = None,
                             json_mode: bool = False,
                             timeout: int | None = None,
                             max_tokens: int | None = None,
                             scene: str = "",
                             reasoning_effort: str = "") -> str:
    """带熔断降级 + **对冲请求**的对话调用（2026-09-17 性能改造）。

    关键设计：
    1. 熔断器 **per-provider 隔离**：sensetime 连续失败只熔断 sensetime，
       不误伤 agnes/deepseek 等其他候选；
    2. ✅ **死配置剔除 + 主配置后置**（P0-1，见 `_order_candidates`）：
       实测主配置 44% 失败率、内置兜底 agnes 仅 3.5% 失败，而旧实现
       "主配置无条件排首位"让好模型永远最后才被尝试；
    3. ✅ **对冲请求（hedged request）**（P0-1）：在跑候选超过
       ``ai_hedge_delay_seconds``（默认 20s）仍无响应时，并行启动下一候选，
       `FIRST_COMPLETED` 先成功者胜并取消其余在途请求。直接消除长尾
       （实测 p90 181s → 收敛到较快候选的数十秒）；代价是触发时多花少量 token；
    4. ✅ **每候选独立超时**（P0-3，见 `_candidate_timeout`）：降级候选单次尝试
       受 ``ai_fallback_attempt_timeout`` 约束，避免调用方的长超时被全链继承；
    5. temperature / json_mode / timeout / max_tokens 可覆盖；
       JSON 模式不被厂商支持时（400）自动回退普通模式，不判死该 Provider。

    ✅ 2026-09-16 修复（P1）：熔断器跳过也记审计日志，便于排查
    「为什么几百个请求全落到低成功率 fallback 上」。
    """
    # ✅ 2026-09-23：优先取「场景路由」指定的配置；该场景未配置路由时回落主配置
    #    （表为空 / 未重启建表时行为与引入场景路由前完全一致）。
    # 第 1 阶段：解析本次调用的配置行
    cfg = await _resolve_chat_config(ai_config, scene)
    # 第 2 阶段：构建候选链（含去重与排序）
    candidates = await _build_candidates(cfg)
    # 第 3 阶段：可用性门禁（Key/运行时开关/冷却/熔断）
    usable, gate_err = await _gate_candidates(candidates, scene)

    errors: list[Exception] = [gate_err] if gate_err is not None else []
    pending: dict[asyncio.Task, dict] = {}
    next_idx = 0
    hedge_enabled = (bool(getattr(settings, "ai_hedge_enabled", True))
                     and len(usable) > 1)
    # 非正值一律按 0 处理（= 等价于"立即并行竞速"），不做人为下限钳制，
    # 否则配置里的短延迟会被悄悄放大（曾钳到 1.0s，与配置语义不符）。
    _hedge_raw = float(getattr(settings, "ai_hedge_delay_seconds", 20.0) or 0.0)
    hedge_delay = _hedge_raw if _hedge_raw > 0 else 0.0

    # ✅ 2026-09-22（对齐 OpenBidKit reasoning_effort 能力）：推理类模型「思考力度」透传。
    #    空串（默认）= 不发送该参数，完全向后兼容；非空前作为 extra_body["reasoning_effort"]
    #    随每次 AI 调用下发（OpenAI 兼容协议），由 provider 合并进请求体。
    _eff = (reasoning_effort or getattr(settings, "ai_reasoning_effort", "") or "").strip()
    extra_body = {"reasoning_effort": _eff} if _eff else None

    def _launch() -> None:
        nonlocal next_idx
        c = usable[next_idx]
        next_idx += 1
        task = asyncio.create_task(_attempt_candidate(
            c, messages, temperature=temperature, json_mode=json_mode,
            max_tokens=max_tokens, req_timeout=timeout, scene=scene,
            extra_body=extra_body))
        pending[task] = c

    try:
        _launch()
        while pending:
            # 还有可对冲的候选时才设置等待窗口，否则等到有结果为止
            wait_timeout = hedge_delay if (hedge_enabled and next_idx < len(usable)) else None
            done, _ = await asyncio.wait(
                set(pending), timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED)
            if not done:
                # 对冲触发：在跑候选超时仍无结果 → 并行启动下一候选
                logger.info(
                    "对冲请求：%s 超过 %.0fs 无响应，并行启动候选 %s",
                    ",".join(c.get("provider_name", "?") for c in pending.values()),
                    hedge_delay, usable[next_idx].get("provider_name", "?"))
                _launch()
                continue

            winner: str | None = None
            for task in done:
                c = pending.pop(task, None)
                if c is None:
                    continue
                try:
                    result, err = task.result()
                except asyncio.CancelledError:
                    result, err = "", RuntimeError(
                        f"候选 {c.get('provider_name', '?')} 被取消")
                except Exception as e:  # pragma: no cover - 防御：任务内异常已自处理
                    result, err = "", e
                if err is None and result:
                    winner = result
                    break
                if err is not None:
                    errors.append(err)

            if winner is not None:
                # ✅ 先成功者胜：取消其余在途候选（其取消记账由 _attempt_candidate 完成）
                if pending:
                    for other in list(pending):
                        other.cancel()
                    await asyncio.gather(*list(pending), return_exceptions=True)
                    pending.clear()
                return winner

            # 有空位且仍有候选 → 立即补位（失败快速返回时无需等对冲延迟）
            if not pending and next_idx < len(usable):
                _launch()
    finally:
        if pending:
            for task in list(pending):
                task.cancel()
            await asyncio.gather(*list(pending), return_exceptions=True)
            pending.clear()

    if errors:
        raise RuntimeError(f"所有 AI 提供商调用失败：{errors[-1]}")
    raise RuntimeError("所有 AI 提供商调用失败：无可用候选")


def _messages_contain_images(messages: list) -> bool:
    """检测 messages 中是否包含图片内容（OpenAI Vision 格式）"""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False
