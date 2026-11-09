"""AI 提供商工厂（自动降级链：当前配置 → 其余配置按 priority → 内置兜底）"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import threading
import time
import uuid
from collections import deque
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

#: ✅ R19x：429 词边界判定（_status_from_error 专用，裸子串会误吃 42900/42980）。
_STATUS_429_RE = re.compile(r"\b429\b")

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
            {
                "value": "hunyuan-turbos-latest",
                "label": "Hunyuan-TurboS (极速)",
                "context": "256K",
                "recommended": True,
            },
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
            {
                "value": "claude-sonnet-4-20250514",
                "label": "Claude Sonnet 4",
                "context": "200K",
                "recommended": True,
                "vision": True,
            },
            {"value": "claude-opus-4-20250514", "label": "Claude Opus 4 (旗舰)", "context": "200K", "vision": True},
            {
                "value": "claude-3-5-haiku-20241022",
                "label": "Claude 3.5 Haiku (极速)",
                "context": "200K",
                "vision": True,
            },
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
            {
                "value": "meta/llama-3.1-405b-instruct",
                "label": "Llama-3.1-405B (旗舰)",
                "context": "128K",
                "recommended": True,
            },
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
    _preset.setdefault(
        "plans",
        {
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
        },
    )


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
    return cls(api_key=api_key, base_url=base_url, model=model or default_model, **kw)


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


def _setting_num(name: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    """读取数值型 ``settings`` 的**单一出口**。

    ⚠️ 2026-10-06（B-6）：禁用 ``getattr(settings, name, default) or default``
       这一写法 —— 它把用户**显式配置的 0** 当成「未配置」而静默回落到默认值。
       本仓已在 ``bid_analysis`` 修过同一类问题（AGENTS §4.15.10），
       本模块当时只改了一处、其余仍留旧写法。语义改为：
         * 未配置 / 配置为 ``None`` / 配置为空串 → 用 ``default``；
         * 显式配置 ``0`` → **尊重用户意图**（再按 ``lo``/``hi`` 做安全钳制）；
         * 配成非数值 → 记 WARNING 后回落 ``default``（不静默）。

    调用方仍可用 ``max(1.0, ...)`` 等下游守卫把 0 抬到安全值；
    本函数只保证「不把显式 0 当成未配置」这一件事。
    """
    raw = getattr(settings, name, None)
    if raw is None or raw == "":
        val = float(default)
    else:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            logger.warning("配置项 %s 取值非法（%r），已按默认 %s 处理", name, raw, default)
            val = float(default)
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


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


def coerce_enabled_flag(raw) -> int | None:
    """把「启用/停用」入参归一为 ``1``/``0``；``None`` 表示「本次不改动」。

    ✅ R179（2026-11-05）：R175 引入 ``ai_config.enabled`` 后，只有独立开关接口
    （``PUT /config/{id}/enabled``）会写该列；保存与导入两条入口都不消费它 ——
    API 调用方传 ``enabled=false`` 被静默忽略（新建行默认启用），属「写入的值
    不生效」。本函数作为唯一解析出口，避免两处各自处理 bool / "0" / "false"。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1 if raw else 0
    v = str(raw).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return 1
    if v in ("0", "false", "no", "off"):
        return 0
    return None


#: 探测/校验侧可直接使用的请求方式值域（单一出口）。
#: ⚠️ 2026-10-06（B-9）：``routers/ai_config/connectivity.py::test_config`` 此前
#:    自行写了一份 ``raw_mode in ("normal", "stream")`` 判定（还额外引入了一个
#:    不在白名单里的第三值 ``auto``）。同一个值域两份实现 → 白名单一旦扩展
#:    （如新增 ``sse``），探测侧会静默把新值当 ``auto`` 处理，与运行时口径分叉。
#:    现统一从本模块取白名单，``auto`` 语义（未指定=按既有策略）仍留在调用方。
VALID_REQUEST_MODES: tuple[str, ...] = tuple(_VALID_REQUEST_MODES)


def clamp_config_numbers(raw: dict) -> dict:
    """把 AI 配置的数值字段收敛到合法区间。

    统一入口：``save_ai_config``（表单保存）与 ``/ai/config/import``（外部导入）
    共用，避免「界面校验得住、导入文件却能把 temperature 写成 99」这类
    绕过界面的脏数据进入运行链路（超界值会在 provider 请求里被厂商拒绝）。
    """
    return {
        "max_tokens": int(_clamp(raw.get("max_tokens"), *_RANGE["max_tokens"], DEFAULT_CONFIG_NUMBERS["max_tokens"])),
        "temperature": round(
            _clamp(raw.get("temperature"), *_RANGE["temperature"], DEFAULT_CONFIG_NUMBERS["temperature"]), 3
        ),
        "timeout": int(_clamp(raw.get("timeout"), *_RANGE["timeout"], DEFAULT_CONFIG_NUMBERS["timeout"])),
        "concurrency": int(
            _clamp(raw.get("concurrency"), *_RANGE["concurrency"], DEFAULT_CONFIG_NUMBERS["concurrency"])
        ),
    }


def _is_internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判定 IP 是否属于内网/回环/链路本地/未指定/保留网段（SSRF 防护判据）。"""
    return bool(
        getattr(ip, "is_private", False)
        or getattr(ip, "is_loopback", False)
        or getattr(ip, "is_link_local", False)
        or getattr(ip, "is_unspecified", False)
        or getattr(ip, "is_reserved", False)
    )


def _is_internal_host(host: str) -> bool:
    """判定主机名是否指向内网/回环地址（含域名解析结果，防 DNS rebinding 静态版）。

    解析失败时按「无法确认」处理（放行）：不能因为本机 DNS 暂时不可用，
    就把用户配置的内网/公网地址一律判死 —— 真正的网络错误会在调用时暴露。
    """
    h = (host or "").strip().lower()
    if not h:
        return False
    if h == "localhost":
        return True
    try:
        return _is_internal_ip(ipaddress.ip_address(h))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, 0)
    except Exception:
        return False
    for info in infos:
        addr = info[4][0] if info and len(info) > 4 else ""
        try:
            if _is_internal_ip(ipaddress.ip_address(addr)):
                return True
        except ValueError:
            continue
    return False


def normalize_base_url(raw: str) -> str:
    """归一化并校验 API Base URL。

    ✅ 原实现直接把用户输入写库，只做 ``.strip()``：
      - 协议未校验（`file://`、`ftp://` 等会被 httpx 接受并尝试发起请求）；
      - 未校验域名 —— `http://` 这类空主机地址会一路写库，直到运行时才失败；
      - 尾部斜杠未归一 —— 与 provider 内 `base_url.rstrip("/")` 不一致，
        造成「配置里显示带斜杠、实际请求不带」的表里不一。

    ✅ R175（2026-11-03）：新增 SSRF 防护 —— 回环/私网/链路本地/云元数据/
    0.0.0.0/localhost 以及「域名解析到内网」一律按安全策略拒绝；
    本机自建模型服务（Ollama/vLLM 等）需显式设置
    ``AI_ALLOW_PRIVATE_BASE_URL=1`` 才放行。
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
    # ✅ R175（2026-11-03）：SSRF 防护 —— 默认拒绝指向内网/回环的地址。
    # ✅ 2026-11-05：拒绝文案统一化 —— 不回显 host（字面 IP 与域名两条路径
    #    的结论必须逐字一致；回显还可能把用户误填的凭据带进日志），
    #    原因写明「内网/回环/链路本地」，逃生开关写全名 AI_ALLOW_PRIVATE_BASE_URL=true。
    if not bool(getattr(settings, "ai_allow_private_base_url", False)):
        if _is_internal_host(parsed.hostname):
            raise ValueError(
                "Base URL 指向内网/回环/链路本地地址，已按安全策略拒绝；"
                "如确需连接本机/内网自建模型服务，请设置环境变量 "
                "AI_ALLOW_PRIVATE_BASE_URL=true 后重试"
            )
    return url


#: 数值字段的中文名，用于把「界面填了越界值、后端静默收敛」显式告知用户。
_NUMBER_LABELS = {
    "max_tokens": "最大 Token",
    "temperature": "温度",
    "timeout": "超时（秒）",
    "concurrency": "并发数",
}


#: 自定义供应商的哨兵名（不在 ``PROVIDER_PRESETS`` 中，必须自带 Base URL）。
CUSTOM_PROVIDER_NAME = "custom"


def resolve_config_base_url(provider_name: str, plan: str, raw_base_url: str) -> str:
    """「计费方式 ↔ Base URL」联动校验与回填的**唯一出口**。

    规则（与前端表单联动校验同口径）：
      - 自定义供应商：必须手填 Base URL；
      - 包月套餐（``coding_plan``）：必须手填 Base URL（各平台包月接入点
        不同于按量计费，落到按量地址会按错误的价格体系扣费/直接 402）；
      - 按量计费（``pay_as_you_go``）：未填地址时自动回填平台预设地址。

    ⚠️ 2026-10-06（B-1）：此前这段逻辑只内嵌在 ``save_ai_config`` 里，
      ``/ai/config/import``（外部导入文件）完全绕过 —— 导入一条
      ``plan=coding_plan`` 且 ``base_url`` 为空的配置会落库成功，而运行时
      ``_build_provider`` 对「已知预设 + 空地址」一律回退到**按量计费**地址，
      于是「包月配置实际按量调用」（静默的计费方式错配，且界面看不出异常）。
      现收敛为单一出口，保存与导入共用。

    :raises ValueError: 违反上述任一联动规则时抛出（调用方决定是 400 还是跳过）。
    """
    base_url = normalize_base_url(raw_base_url or "")
    if provider_name == CUSTOM_PROVIDER_NAME:
        if not base_url:
            raise ValueError("自定义供应商必须填写 Base URL")
    elif plan == "coding_plan":
        if not base_url:
            raise ValueError("包月套餐（Coding Plan）必须手动填写 API 地址，" "包月接入点不同于按量计费")
    elif plan == "pay_as_you_go":
        if not base_url:
            preset = PROVIDER_PRESETS.get(provider_name)
            if preset:
                base_url = preset.get("base_url", "")
    if not base_url:
        raise ValueError("Base URL 不能为空（按量计费请选择已知供应商或手动填写地址）")
    return base_url


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
            warns.append(f"{_NUMBER_LABELS[field]}取值非法（{raw[field]!r}），已按默认 {clamped[field]} 保存")
            continue
        if v < lo or v > hi:
            warns.append(f"{_NUMBER_LABELS[field]} {raw[field]} 超出允许范围 {lo}~{hi}，已按 {clamped[field]} 保存")
    return warns


async def save_ai_config(data: dict) -> str:
    cid = data.get("id") or str(uuid.uuid4())
    # ✅ R175（2026-11-03）：Key 先 strip 再加密 —— 前端粘贴常带换行/首尾空格，
    #    旧实现把「空白串」当成有效 Key 加密落库（下游解密后为空仍进候选链），
    #    且与「留空=不修改已有 Key」的语义冲突。
    api_key_raw = (data.get("api_key") or "").strip()
    enc = encrypt_api_key(api_key_raw)
    plan = normalize_plan(data.get("plan"))
    provider_name = (data.get("provider_name") or "").strip()
    model = (data.get("model") or "").strip()
    # ✅ R181（2026-11-07）：remark 落 ai_configs.remark（TEXT，容量足够）→ 存全文，
    #    与导入路径（routers/ai_config/config.py）同列同口径。
    remark = (data.get("remark") or "").strip()

    if not provider_name:
        raise ValueError("供应商不能为空")
    if not model:
        raise ValueError("模型名称不能为空")

    # 计费方式与 Base URL 的联动校验/回填（统一出口，与 /ai/config/import 同口径）
    base_url = resolve_config_base_url(provider_name, plan, data.get("base_url") or "")

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
    # ✅ R179（2026-11-05）：启用/停用入参显式生效（None = 本次不改动）。
    #    旧实现只在 INSERT 时依赖列默认值、UPDATE 完全不写 enabled ——
    #    通过保存接口传 enabled=false 会被静默丢弃，与界面/接口契约不一致。
    enabled_flag = coerce_enabled_flag(data.get("enabled"))
    if enabled_flag == 0 and bool(data.get("is_active")):
        raise ValueError("当前使用中的配置不能停用；请先把其它配置设为「当前使用」再停用该配置")
    # priority 为 None 表示「本次保存不改动降级顺序」（降级链有独立编辑入口），
    # 若保存时无脑写 0，会把用户拖好的顺序每次保存配置时全部清零。
    raw_priority = data.get("priority", None)

    try:
        # ✅ 并发修复：多语句事务改用独立写池连接。原实现在全局共享连接
        # get_conn() 上 rollback，会把并发协程（任务进度写、任务注册 INSERT 等）
        # 已执行未提交的合法语句一并回滚，造成静默数据丢失。
        # ✅ R108（2026-10-18 · 日志审计 P0-4）：整事务体提取为可重放闭包接
        # retry_db_op——生产实证保存 AI 配置偶发 500（批量生成写锁窗口内
        # 首条 UPDATE/INSERT 抛 locked 直通）。write_tx_conn 每次进入获取独立
        # 连接、异常路径统一回滚，locked 抛出时事务必未提交 → 整事务重放安全
        # （INSERT 用函数外生成的固定 cid，重放不会双写；语义错误不重试原样上抛）。
        async def _save_tx_once() -> None:
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
                            " request_mode=?, env=?, is_active=?, enabled=COALESCE(?, enabled),"
                            " remark=?, updated_at=? WHERE id=?",
                            (
                                provider_name,
                                plan,
                                api_key_raw,
                                enc,
                                base_url,
                                model,
                                max_tokens,
                                temperature,
                                timeout,
                                concurrency,
                                request_mode,
                                env,
                                int(data.get("is_active", True)),
                                enabled_flag,
                                remark,
                                datetime.now().isoformat(),
                                cid,
                            ),
                        )
                    else:
                        await conn.execute(
                            "UPDATE ai_config SET provider_name=?, plan=?,"
                            " api_key_encrypted=CASE WHEN ?!='' THEN ? ELSE api_key_encrypted END,"
                            " base_url=?, model=?, max_tokens=?, temperature=?, timeout=?, concurrency=?,"
                            " request_mode=?, env=?, is_active=?, enabled=COALESCE(?, enabled),"
                            " remark=?, priority=?, updated_at=? WHERE id=?",
                            (
                                provider_name,
                                plan,
                                api_key_raw,
                                enc,
                                base_url,
                                model,
                                max_tokens,
                                temperature,
                                timeout,
                                concurrency,
                                request_mode,
                                env,
                                int(data.get("is_active", True)),
                                enabled_flag,
                                remark,
                                int(raw_priority),
                                datetime.now().isoformat(),
                                cid,
                            ),
                        )
                else:
                    # 闭包局部 prio：每次重放都重新读外层 raw_priority，
                    # 避免闭包内赋值把外层名字判为局部（UnboundLocalError）
                    prio = raw_priority
                    if prio is None:
                        # 新配置默认排到降级链末尾，而不是与既有配置同为 0
                        # （同值时只能靠 updated_at 兜底排序，顺序不可预期）
                        cur = await conn.execute("SELECT COALESCE(MAX(priority), -1) + 1 FROM ai_config")
                        row = await cur.fetchone()
                        prio = int(row[0]) if row and row[0] is not None else 0
                    await conn.execute(
                        "INSERT INTO ai_config (id, provider_name, plan, api_key_encrypted, base_url, model,"
                        " max_tokens, temperature, timeout, concurrency, request_mode, env,"
                        " is_active, enabled, priority, remark)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?,1),?,?)",
                        (
                            cid,
                            provider_name,
                            plan,
                            enc,
                            base_url,
                            model,
                            max_tokens,
                            temperature,
                            timeout,
                            concurrency,
                            request_mode,
                            env,
                            int(data.get("is_active", True)),
                            enabled_flag,
                            int(prio),
                            remark,
                        ),
                    )
                # 注：此处原有一句 INSERT INTO provider_history，已删除 —— 该表全库无任何读取
                # （数据流审计 R2 强信号），属只增不消费的死写，每次保存配置都会白写一行。
                await conn.commit()

        await retry_db_op(_save_tx_once, max_retries=4, base_delay=0.1, jitter=0.05)
    except Exception:
        # 半提交状态已由 write_tx_conn 统一回滚（独立连接，不影响其他协程）
        raise
    # ✅ R108（P1-4）：保存即视为用户改过这条配置（模型/端点/密钥任一），
    #    确定性死连击清零——改对则回候选链、改错则重新攒 3 次。
    with _provider_reliability_lock:
        _det_dead_streak.pop(_reliability_key("", "", cid, ""), None)
        # ✅ R178（D-3 · 2026-11-05）：保存配置 = 用户改过这条配置（模型/端点/密钥），
        #    此前只清「确定性死连击」，**不清实时成功率统计** → 用户把模型名改对了，
        #    成功率却仍停在旧值（判死阈值 0.20 / 后置阈值 0.60）→ 改对了仍被剔除或
        #    静默后置；且统计一旦冻结就**永远判死**，唯一出口是进程重启后 24h 预热。
        #    现一并清掉该配置身份的实时成功率统计 → 保存即刻生效（用户改配置即恢复）。
        _provider_reliability.pop(_reliability_key("", "", cid, ""), None)
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
            "SELECT concurrency FROM ai_config WHERE is_active = 1 " "ORDER BY updated_at DESC LIMIT 1"
        )
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
                logger.warning("AI 配置并发数 %d 超过全局上限 %d，已钳到 %d", c, _MAX_CONCURRENCY, _MAX_CONCURRENCY)
                return _MAX_CONCURRENCY
    except Exception as e:
        # ✅ 2026-10-06（B-8 · 级别对称性）：成功路径记 INFO、失败路径记
        #    DEBUG —— DEBUG 在默认日志级别下不落盘，于是「并发数没生效」
        #    这类静默失效在日志里完全不可见（与 AGENTS §3.1.6「可恢复异常用
        #    WARNING」相悖）。降级仍不阻断服务，但必须可观测。
        logger.warning("应用配置并发失败（不影响服务，本次沿用上一次生效值）: %s", e)
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

#: ✅ 2026-10-05（A4 静默告警）：未登记场景首次命中时打一次 warning。
#: 用 set 记录已告警场景，避免高频调用刷屏（每进程生命周期内只告一次）。
#: 重启即清空，配合 KNOWN_SCENES 双向漂移护栏（TestKnownScenesDrift）双保险。
_scene_unregistered_warned_scenes: set[str] = set()


def _scene_unregistered_warned(scene: str) -> bool:
    """标记/查询某场景是否已发过"未在 KNOWN_SCENES 登记"告警。

    返回 True 表示此前已告警过，调用方应跳过本次重复告警；
    返回 False 表示本次调用完成标记，是首次告警。
    """
    if scene in _scene_unregistered_warned_scenes:
        return True
    _scene_unregistered_warned_scenes.add(scene)
    return False


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
        ttl = float(getattr(settings, "ai_config_cache_ttl", _DEFAULT_CONFIG_CACHE_TTL) or _DEFAULT_CONFIG_CACHE_TTL)
    except (TypeError, ValueError):
        return _DEFAULT_CONFIG_CACHE_TTL
    return ttl if ttl > 0 else _DEFAULT_CONFIG_CACHE_TTL


# P0-4 性能优化：AI 审计日志攒批（满 50 条或 10s 定时批量落库，替代每次调用一次 fsync commit）
_audit_buffer: list[tuple] = []
_audit_lock = threading.Lock()
_AUDIT_BATCH_SIZE = 50
_AUDIT_FLUSH_INTERVAL = 10.0
_audit_last_flush: dict = {"ts": time.time()}  # 模块加载起计时，避免首条即刷

# A1（2026-10-05）审计溢出队列：审计批量落库重试耗尽后不再丢失，
# 而是移入本队列，等待后续 flush 优先落库。容量与滞留时长双限，防止
# 长时间 DB 故障导致内存无界增长。
# - 单条记录 tuple 长度固定（13 字段，见 _audit_buffer.append），5 万条 ≈ 50MB 上限。
# - 单批 flush 尝试时优先消费 overflow（旧数据优先），再处理新缓冲。
# - 队列条目超过 max_age 秒仍未落库则丢弃并告警（比无告警静默丢失更明确）。
_audit_overflow: deque = deque()  # deque[(ts, rows)]，FIFO
AUDIT_OVERFLOW_MAX_BATCHES = 500  # 最多 500 批 × 每批 50 条 = 25k 条上限
AUDIT_OVERFLOW_MAX_AGE = 86400.0  # 单日未落库条目过期
_audit_overflow_evicted = 0  # 累计被 max_age / 容量驱逐的批次数（观测）

# ✅ 2026-09-16 新增：进程内实时可靠性统计（滑动窗口 50 次调用）
# 用于 _fallback_chain 自动过滤历史成功率过低的 provider（<50% 的直接排除）
# 主配置总是保留（即使成功率低），只有 fallback 候选会被过滤。
_provider_reliability_lock = threading.Lock()
_provider_reliability: dict[str, dict] = {}  # provider_name → {"ok": int, "fail": int}
_PROVIDER_RELIABILITY_WINDOW = 50  # 每个 provider 统计最近 N 次

# ✅ 2026-11-05（对冲自适应阈值）：进程内按「配置身份」记录最近成功调用的耗时，
# 用于计算对冲延迟的 P50/P90。只记成功调用（失败/取消的 0.18s 快速失败会
# 把分位数拉向 0，反而让对冲更早触发）。重启后为空 → 回落到冷启动阈值。
_provider_latency_lock = threading.Lock()
_provider_latency: dict[str, deque] = {}
_PROVIDER_LATENCY_WINDOW = 50


def _percentile(values: list[float], p: float) -> float:
    """线性插值分位数（p ∈ [0,100]），空列表调用方保证非空。"""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * max(0.0, min(100.0, p)) / 100.0
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _note_provider_latency(key: str, duration: float) -> None:
    """记录一次成功调用耗时（供自适应对冲阈值使用）。"""
    if not key or duration <= 0:
        return
    with _provider_latency_lock:
        dq = _provider_latency.get(key)
        if dq is None:
            dq = deque(maxlen=_PROVIDER_LATENCY_WINDOW)
            _provider_latency[key] = dq
        dq.append(float(duration))


def _provider_latency_percentiles(key: str, pct: float = 90.0) -> tuple[int, float | None, float | None]:
    """返回 (样本数, P50, 指定分位)；无样本时后两项为 None。"""
    with _provider_latency_lock:
        dq = _provider_latency.get(key)
        samples = list(dq) if dq else []
    if not samples:
        return 0, None, None
    return len(samples), _percentile(samples, 50.0), _percentile(samples, pct)


def reset_provider_latency() -> None:
    """清空自适应对冲延迟样本（测试隔离 / 配置重载用）。"""
    with _provider_latency_lock:
        _provider_latency.clear()


def _adaptive_hedge_delay(
    key: str,
    *,
    fixed_delay: float,
    adaptive: bool = True,
    pct: float = 90.0,
    p50_ratio: float = 1.5,
    min_delay: float = 30.0,
    max_delay: float = 90.0,
    min_samples: int = 12,
) -> float:
    """计算对冲阈值（秒）。

    规则（✅ 2026-11-05）：
      · ``fixed_delay <= 0`` → 0（显式立即竞速，保留旧语义）；
      · 非自适应 → 固定阈值；
      · 自适应但样本不足 → 固定阈值（冷启动）；
      · 自适应且有样本 → ``clamp(max(P{pct}, P50×ratio), [min, max])``。
    依据：生产 agnes 成功调用 P50=25.83s，旧固定 20s 低于中位数，正常偏慢
    调用会被复制；取 P90 可只复制最慢约 10% 的调用，min=30s 保证阈值高于 P50。
    """
    if fixed_delay <= 0:
        return 0.0
    if not adaptive:
        return fixed_delay
    n, p50, hi = _provider_latency_percentiles(key, pct)
    if n < max(1, min_samples) or p50 is None or hi is None:
        return fixed_delay
    target = max(hi, p50 * max(0.0, p50_ratio))
    return max(min_delay, min(target, max_delay))


# ✅ R108（2026-10-18 · 日志审计 P1-4 · 确定性死配置连击剔除）：
#    生产实证 volcengine/nvidia 多条配置长期以 HTTP 404 UnsupportedModel/
#    InvalidEndpointOrModel 失败——「模型/端点不存在」是**确定性**错误
#    （重放必败，非瞬态），但旧死配置判据走「样本数 × 成功率」统计口径，
#    滑动窗口压缩（50→折半）+ 成功混入会让这类配置反复回流候选链，
#    每次降级都白烧一次远程调用与数十秒超时。现按身份键记连击：
#    连续 ≥3 次命中确定性 404 签名 → 直接判死；任一成功即清零自愈
#    （用户改对模型名/保存配置同样清零）。内存态、零 DDL、零新增配置。
_det_dead_streak: dict[str, int] = {}
DET_DEAD_STREAK_THRESHOLD = 3
#: 确定性失败签名（小写匹配）：模型或端点在该接入地址下不存在。
_DET_DEAD_ERROR_SIGNATURES = ("unsupportedmodel", "invalidendpointormodel")


def _is_deterministic_dead_error(error_text: str) -> bool:
    """确定性死配置错误签名判据（封闭枚举，宁缺毋滥）。"""
    low = (error_text or "").lower()
    return any(sig in low for sig in _DET_DEAD_ERROR_SIGNATURES)


# ✅ 修复（2026-09-17）：统一为与 _is_dead_provider 相同的配置项门槛。
#    旧实现硬编码 0.50 与 _order_candidates 使用的 ai_provider_dead_success_rate(0.20)
#    冲突，导致 20%~50% 成功率的候选被 _fallback_chain 静默剔除（即便 _order_candidates
#    认为"未死"保留），降级覆盖被悄悄收窄，违反"配置化优先、不隐式钳制"约束。
_PROVIDER_DEAD_SUCCESS_RATE = _setting_num("ai_provider_dead_success_rate", 0.20)
# ✅ 2026-10-06（B-6 · 阈值单一源）：此前同一个 setting
#    ``ai_provider_dead_success_rate`` 被**读成两个模块常量**
#    （``_PROVIDER_MIN_SUCCESS_RATE`` 与 ``_PROVIDER_DEAD_SUCCESS_RATE``，
#    同一个 ``getattr(...) or 0.20`` 写了两遍），两处各自演进 → 一旦只改一处，
#    「降级链成功率过滤」与「死配置剔除」会静默用不同门限（同一个 provider
#    在两条链路上一个被剔除、一个被保留）。现统一为同一常量派生。
_PROVIDER_MIN_SUCCESS_RATE = _PROVIDER_DEAD_SUCCESS_RATE
_PROVIDER_MIN_SAMPLES = 5  # 成功率统计生效的最小样本数
# ✅ P0-1（2026-09-17）：死配置剔除 / 主配置后置门限
#    实测 volcengine 21 次调用 100% 失败、spark 78.6% 失败 —— 留在候选链上
#    只会白等一轮网络往返；主配置（sensetime）44% 失败却因"无条件首位"让
#    成功率仅 3.5% 的兜底 agnes 永远排在最后。
#    注意：样本门槛两侧不同（过滤 5 / 死配置 10）是**有意**的 ——
#    过滤只影响降级链排序，死配置会被直接移除，故要求更多样本。
_PROVIDER_DEAD_MIN_SAMPLES = 10
_PROVIDER_DEMOTE_SUCCESS_RATE = _setting_num("ai_provider_demote_success_rate", 0.60)

# ---------- AI 调用实时状态（「后台任务状态栏」轮询读取，进程内存态） ----------
# 记录正在进行的调用数、本次会话累计成败数与最近一次调用结果 ——
# 审计日志有最长 10s 的攒批延迟且只有已完成的调用，无法表达「AI 调用中」。
_ai_live_lock = threading.Lock()
_ai_live: dict = {
    "in_flight": 0,  # 正在进行的 AI 调用数
    "total": 0,  # 本次会话累计调用尝试数（含降级重试）
    "failed": 0,  # 本次会话累计**真实失败**尝试数（不含取消）
    # ✅ 2026-11-04（统计口径修复）：对冲落选 / 用户停止 / 断连以
    #    asyncio.CancelledError 收尾，旧实现与真实失败共用 failed ——
    #    对冲每胜出一次就凭空多记一次失败（生产实证会话 126 vs 审计 9）。
    "cancelled": 0,  # 本次会话累计被取消的调用尝试数（非故障）
    # ✅ 2026-11-05（统计口径修复 · 逻辑调用 / 尝试拆分）：
    #    total/failed 是**尝试**口径（一次降级链里每个候选各计一次），
    #    calls/call_failed 是**逻辑调用**口径（一次 chat_with_fallback 只计一次成败）。
    #    面板同时展示两者，避免「一次业务调用失败 3 次降级 = 失败 3 次」的误读。
    "calls": 0,  # 本次会话逻辑调用数（含最终成功/失败）
    "call_failed": 0,  # 本次会话逻辑调用最终失败数
    "call_cancelled": 0,  # 本次会话逻辑调用被取消数（调用方停止/断连）
    # ✅ 2026-11-04（可观测性）：全熔断快速失败在门控处直接抛错，
    #    不发请求也不写审计（生产实证 3 分钟 316 次抛错在库里零记录）。
    "fast_fail": 0,  # 本次会话累计全熔断快速失败拒绝次数
    "last_provider": "",  # 最近一次调用的 Provider
    "last_model": "",  # 最近一次调用的模型
    "last_ok": True,  # 最近一次调用是否成功
    "last_duration": 0.0,  # 最近一次调用耗时（秒）
    "last_at": 0.0,  # 最近一次调用完成的时间戳（time.time()）
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
        cur = await conn.execute("SELECT value FROM ai_runtime_settings WHERE key = ?", (key,))
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
        (key, value, datetime.now().isoformat()),
    )


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
    await save_runtime_setting(RUNTIME_DISABLED_PROVIDERS_KEY, json.dumps(cleaned, ensure_ascii=False))
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
    # ✅ R181（2026-11-06 · 遗留项「_load_active_config 不过滤脏行」）：
    #    外部改库 / 旧版本可能留下「is_active=1 且 enabled=0」的矛盾行。
    #    运行时必须按 enabled=0 的真实语义忽略它，否则「已停用」配置仍被当成
    #    主配置调用（与 enabled 开关承诺相反）。归一入口见
    #    POST /api/v1/ai/config/repair-consistency（显式、可重入、写审计）。
    _sql = (
        "SELECT * FROM ai_config WHERE is_active = 1"
        " AND COALESCE(enabled, 1) = 1{extra} ORDER BY updated_at DESC LIMIT 1"
    )
    row = None
    if env:
        row = await (await conn.execute(_sql.format(extra=" AND env = ?"), (env,))).fetchone()
        if not row:
            row = await (await conn.execute(_sql.format(extra=" AND env = ''"), ())).fetchone()
            if row:
                logger.debug("环境 %s 无专用主配置，回落通用主配置", env)
    else:
        row = await (await conn.execute(_sql.format(extra=""), ())).fetchone()
    if row is None:
        # 不再静默：若被忽略的唯一原因是「当前使用但已停用」，给出可执行的修复入口。
        try:
            _dirty_sql = "SELECT COUNT(*) FROM ai_config WHERE is_active = 1" " AND COALESCE(enabled, 1) = 0{extra}"
            if env:
                _dirty = await (await conn.execute(_dirty_sql.format(extra=" AND env = ?"), (env,))).fetchone()
            else:
                _dirty = await (await conn.execute(_dirty_sql.format(extra=""), ())).fetchone()
            if _dirty and int(_dirty[0] or 0) > 0:
                logger.warning(
                    "检测到 %d 条「is_active=1 且 enabled=0」的矛盾配置，已按停用忽略；"
                    "请调用 POST /api/v1/ai/config/repair-consistency 归一（会记录审计）",
                    int(_dirty[0]),
                )
        except Exception as e:  # noqa: BLE001 - 诊断查询失败不得影响主配置读取
            logger.debug("矛盾配置诊断查询失败（忽略）: %s", e)
    cfg = dict(row) if row else None
    if gen != _cache_generation:
        # 读库期间配置被改动并失效了缓存：本次结果已过期，直接返回但不写缓存
        logger.debug("主配置读取期间缓存被失效，丢弃本次缓存写入（代际 %d→%d）", gen, _cache_generation)
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
        # ✅ R181：同步加上 enabled 过滤，避免「脏行」被识别为主配置后，
        #    真正干净的主配置又被当成降级候选重复调用。
        primary_id = ""
        prim_cur = await conn.execute(
            "SELECT id FROM ai_config WHERE is_active = 1"
            " AND COALESCE(enabled, 1) = 1" + _env_filter + " ORDER BY updated_at DESC LIMIT 1",
            _env_params,
        )
        prim_row = await prim_cur.fetchone()
        if prim_row:
            primary_id = prim_row[0]

        cur = await conn.execute(
            # ✅ R175（2026-11-03）：disabled 配置即使有 Key 也不得进入降级链。
            "SELECT * FROM ai_config WHERE 1=1"
            + _env_filter
            + " AND COALESCE(enabled, 1) = 1"
            + " ORDER BY priority ASC, updated_at DESC",
            _env_params,
        )
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
                        row.get("provider_name", "?"),
                        row.get("model", "?"),
                    )
                continue
            chain.append(
                {
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
                }
            )
    except Exception:
        # 降级链加载失败不应阻断主流程
        logger.exception("加载 DB 降级链失败，退回内置兜底")

    # ✅ 本轮修复：内置兜底从 chain 中独立出来（见下方「兜底不被截断」）。
    builtin_agnes = None
    if settings.agnes_api_key:
        builtin_agnes = {
            "provider_name": "agnes",
            "api_key": settings.agnes_api_key,
            "base_url": settings.agnes_base_url,
            "model": settings.agnes_model,
            # ✅ R178（D-1 · 2026-11-05）：内置兜底标记 —— `_order_candidates`
            #    依此豁免「判死剔除」（该函数在其后运行），兑现「兜底必须留在
            #    链内」的设计意图（见下方「兜底不被截断」注释）。
            "_is_builtin_fallback": True,
        }

    # ✅ 2026-09-16 新增：根据实时成功率过滤低质量 fallback 候选
    # （主配置由 chat_with_fallback 另行排在首位，这里只处理 fallback 链）
    if chain:
        filtered = []
        for c in chain:
            pname = c.get("provider_name", "")
            rate = _provider_success_rate(pname, c.get("model", ""), c.get("config_id", ""), c.get("base_url", ""))
            if rate is not None and rate < _PROVIDER_MIN_SUCCESS_RATE:
                # ✅ R178（D-2）：设计内节流降 INFO（生产实证 253 条）。
                logger.info(
                    "fallback 候选 %s 实时成功率 %.1f%% < %.0f%%，已自动排除（样本≥5次）",
                    pname,
                    rate * 100,
                    _PROVIDER_MIN_SUCCESS_RATE * 100,
                )
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
            c.get("provider_name", ""), c.get("model", ""), c.get("config_id", ""), c.get("base_url", "")
        )
        if rate is None:
            return (1, 0.0)  # 无样本：排在「已知健康」之后（冷启动不误杀）
        return (0, -rate)  # 有样本：成功率高的优先

    chain.sort(key=_health_rank)  # 稳定排序：同档仍按 priority

    # ✅ P0-3（2026-09-17）：降级链长度上限。
    #    实测 ai_config 曾配 14 条（priority 全为 0）→ 降级候选 13 个，
    #    主候选失败后被逐个**串行**尝试（每个候选还带着调用方的 300s 超时），
    #    单章一次 AI 调用最坏被放大到外层总预算 660s。
    #    收敛到 N 个（默认 3）后，最坏路径随之收敛。
    _max_chain = int(getattr(settings, "ai_fallback_chain_max", 3) or 3)
    if len(chain) > _max_chain:
        logger.info("降级链收敛：%d → %d 个候选（ai_fallback_chain_max）", len(chain), _max_chain)
        chain = chain[:_max_chain]
    # ✅ 本轮修复：内置兜底必须留在链内。旧实现把它 append 在末尾参与截断，
    #    配置 ≥3 条时兜底恒被 ``chain[:3]`` 切掉 —— 名义上有兜底，实际没有。
    if builtin_agnes and builtin_agnes not in chain:
        chain.append(builtin_agnes)

    if _gen != _cache_generation:
        # 读库期间配置被改动并失效了缓存：不写回，避免把旧顺序钉住一整个 TTL
        logger.debug("降级链读取期间缓存被失效，丢弃本次缓存写入（代际 %d→%d）", _gen, _cache_generation)
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
    # —— 文种标签（R179 Phase A）：仅规则零命中时补类别建议，不产出危大结论 ——
    "genre_tag_assist": "文种标签辅助判断",
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
    "content_plan": "正文离散编排",
    # ✅ R77（2026-10-14）：standard_report 检出 error 级事实冲突后的
    #    「AI 定点编辑 + 唯一命中才替换」修复调用点（content_fact_autofix）。
    #    仅在检出冲突的章节触发（无冲突章节零调用），需可单独观测与指定模型。
    "content_fact_autofix": "正文事实冲突定点修复",
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
    # ✅ R179 M1（2026-11-05）：评分项提取（app/services/scoring_items.py 调用点）。
    #    必须登记，否则「场景模型路由」里配了它也不生效（静默失效），
    #    且 TestKnownScenesDrift / known_scenes 双向漂移护栏会失败。
    "scoring_items_extract": "评分项提取",
    # —— 一致性 ——
    "consistency_scan": "全文一致性扫描",
    # ✅ R57（2026-10-09）：原 "consistency_repair" 同时承载「定点编辑」与
    #    「整章重写」两条成本/成功率差异极大的路径，无法分别观测与分别
    #    指定模型。现拆分：consistency_repair_edits = 定点编辑。
    #    存量 ai_scene_routes 行不受影响（scene_routes 有白名单外兜底分支）。
    "consistency_repair_edits": "一致性问题修复·定点编辑",
    "consistency_repair": "一致性问题修复·整章重写",
    # ✅ R122-G2（2026-10-21）：整卷合并一致性修复（少轮整卷往返 + 四道小节
    #    闸门），与逐章 consistency_repair 并存、由开关
    #    consistency_fullbook_repair_enabled 分流（默认关闭走逐章旧链路）。
    "consistency_fullbook": "一致性问题修复·整卷合并",
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
        cur = await conn.execute("SELECT scene, config_id FROM ai_scene_routes WHERE config_id != ''")
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

    ✅ 2026-10-05（A4 静默告警）：未登记的 scene 仍可正常调用（保持向后兼容），
       但会在首次命中时打 warning，避免"配了路由却静默失效"这类难以归因的问题。
       仅日志，无行为变化。
    """
    scene = (scene or "").strip()
    if not scene:
        return None
    # 未登记场景：仍然按既有语义返回 None 走主配置，但打印一次告警便于排查
    if scene not in KNOWN_SCENES and not _scene_unregistered_warned(scene):
        logger.warning(
            "场景 %r 未在 KNOWN_SCENES 登记，场景路由不会生效（回落主配置）。"
            "若是新增 AI 调用点，请同步登记到 provider_factory.KNOWN_SCENES；"
            "详见 tests/test_ai_config_security_routing.py::TestKnownScenesDrift",
            scene,
        )
    try:
        routes = await load_scene_routes()
    except Exception as e:
        # ✅ 2026-10-06（B-3 · 零日志静默失效）：本分支此前是裸
        #    ``except Exception: return None``（同文件 :1352 的等价分支有日志），
        #    于是「场景路由表读不出来 / 缓存层抛错」时场景路由静默停摆，
        #    用户在界面上仍看到路由配置完好、AI 却一直用主配置 —— 无任何线索。
        #    回落主配置的行为不变（fail-soft），但必须留下可观测记录。
        logger.warning("读取场景路由失败，场景 %r 本次回落主配置: %s", scene, e)
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
    # ✅ R179（2026-11-05）：场景路由必须尊重用户停用（enabled=0）。
    #    旧实现只校验「存在 + 环境」，被停用的配置照样被场景路由拉进真实调用，
    #    与 R175 定义的「停用 = 不再参与真实调用」自相矛盾，且界面显示停用却仍有
    #    用量（静默失效）。停用 → 回落主配置/降级链，与降级链口径一致。
    enabled_raw = cfg.get("enabled", 1)
    if enabled_raw is None:
        enabled_raw = 1
    if int(enabled_raw) == 0:
        logger.warning(
            "场景 %s 路由到的配置 %s 已被用户停用（enabled=0），跳过场景路由并回落主配置",
            scene,
            cid,
        )
        return None
    # ✅ 2026-09-23（多环境）：路由指向的配置若属于**其它环境**，视为未配置 ——
    #    否则切了环境后场景仍会打到别环境的地址，与「环境隔离」的预期相反。
    env = await resolve_active_env()
    if env and str(cfg.get("env") or "").strip() not in ("", env):
        logger.warning(
            "场景 %s 路由到的配置 %s 属于环境 %s（当前环境 %s），跳过场景路由并回落主配置",
            scene,
            cid,
            cfg.get("env"),
            env,
        )
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
        candidates.append(
            {
                "provider_name": cfg.get("provider_name", "openai"),
                "api_key": primary_key,
                "base_url": cfg.get("base_url", ""),
                "model": cfg.get("model", ""),
                "max_tokens": cfg.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"]),
                "temperature": 0.0,
                "timeout": cfg.get("timeout", DEFAULT_CONFIG_NUMBERS["timeout"]),
            }
        )
    for c in await _fallback_chain():
        if c.get("api_key") and c not in candidates:
            candidates.append(c)

    # ✅ R175（2026-11-03）：运行时 disabled 的供应商不再参与视觉通道 ——
    #    否则用户在界面禁用了某个 provider，OCR 仍会继续把图片发给它。
    try:
        _disabled = await resolve_disabled_providers()
    except Exception:
        _disabled = set()
    if _disabled:
        candidates = [c for c in candidates if (c.get("provider_name") or "openai") not in _disabled]

    out: list[BaseProvider] = []
    for c in candidates:
        try:
            provider = _build_provider(
                c["provider_name"],
                c["api_key"],
                c["base_url"],
                c["model"],
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


def _is_missing_column_error(e: BaseException) -> bool:
    """「表缺列」异常签名判据（✅ R108 · 日志审计 P1-2 误诊收口）。

    生产实证：批量生成写锁窗口内，`_flush_audit_rows_once` 首条 executemany
    抛 `database is locked` 也被宽 `except Exception` 归因成
    「ai_audit_logs 缺 config_id/base_url 列（旧库未迁移）」——**误诊 WARNING
    长期误导排障方向**；且锁败后降级写若碰巧成功，config_id/base_url 被静默
    裁掉（可靠性统计身份缺失）。锁瞬态必须上抛给外层 ``retry_db_op``
    （`_flush_audit_buffer` L1632 已接）整批重放，仅真缺列才走降级链。

    SQLite 两种缺列文案：INSERT 列名不匹配 → "table X has no column named Y"；
    SELECT/其他引用不存在的列 → "no such column: Y"。其余一律非缺列。
    """
    msg = str(e).lower()
    return "no column named" in msg or "no such column" in msg


# ✅ 2026-11-05（审计合并 · 错误分类）：新版 16 列写入（13 列 + error_class /
#    attempt_count / attempts_json）。新列**只能追加在末尾**，降级切片链才安全。
#    不能直接改 _AUDIT_INSERT_FULL：compliance.py 仍按 13 列元组复用它，改动会
#    让合规审计批量写入因绑定数不匹配而整批丢失。FULL 保持 13 列不变。
_AUDIT_INSERT_RICH = (
    "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
    " prompt_tokens, completion_tokens, cached_tokens, duration, success, error, scene,"
    " config_id, base_url, error_class, attempt_count, attempts_json)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
# 新库迁移前的兼容写入（13 列，含 scene/config_id/base_url）
_AUDIT_INSERT_FULL = (
    "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
    " prompt_tokens, completion_tokens, cached_tokens, duration, success, error, scene,"
    " config_id, base_url)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
# 旧库缺 config_id/base_url 列时的降级写入（保留 scene）
_AUDIT_INSERT_NO_IDENTITY = (
    "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
    " prompt_tokens, completion_tokens, cached_tokens, duration, success, error, scene)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
)
# 更旧库缺 scene 列时的降级写入（审计记录不丢，仅场景/身份标记为空）
_AUDIT_INSERT_LEGACY = (
    "INSERT INTO ai_audit_logs (id, provider_name, model, action,"
    " prompt_tokens, completion_tokens, cached_tokens, duration, success, error)"
    " VALUES (?,?,?,?,?,?,?,?,?,?)"
)
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
            await conn.executemany(_AUDIT_INSERT_RICH, [r[:16] for r in rows])
        except Exception as e:
            # ✅ R108（P1-2 · 误诊收口）：锁/i/o 瞬态与非缺列错误**原样上抛**，
            #    交给外层 retry_db_op 整批重放（write_tx_conn 已回滚，无半写）；
            #    只有真·缺列才进降级链。旧实现宽 except 把批量生成写锁窗口的
            #    locked 归因成「旧库未迁移」，误诊 + config_id 被裁双重损伤。
            #    ⚠️ 例外：ProgrammingError（绑定数≠占位符数）= 行宽与新列数
            #    不匹配（溢出队列可能残留跨版本的 13/11/10 元组旧行），属降级
            #    切片链的预期场景而非确定性故障——上抛会让该批永远回灌溢出、
            #    并连带拖死同队列新批次。维持既有「按行宽逐级降级」语义。
            if not (_is_missing_column_error(e) or isinstance(e, sqlite3.ProgrammingError)):
                raise
            # 第一级降级：13 列（旧库缺 error_class/attempt_count/attempts_json）。
            # ✅ 2026-10-06（B-5 · 降级写入的长度守卫）：降级 INSERT 依赖
            #    **元组按位切片**（13 列 → r[:11] → r[:10]）与各自的列数严格
            #    对齐。一旦将来在元组**中间**插入新字段（例如在 error 之前
            #    插一列），切片不会报错，而是把「错位的值」静默写进降级表 ——
            #    比直接抛异常糟糕得多（数据看似正常、实则全错）。
            #    故每级降级前先断言行宽足够，不足则跳过该级并记 ERROR。
            try:
                if all(len(r) >= 13 for r in rows):
                    await conn.executemany(_AUDIT_INSERT_FULL, [r[:13] for r in rows])
                else:
                    raise ValueError(
                        f"审计行宽 {min(len(r) for r in rows)} < 13，无法按"
                        "「含配置身份、不含错误分类」降级写入（可能已变更审计元组结构）"
                    )
                # 仅「真缺列」才告警；行宽不足导致的 ProgrammingError 属旧版
                # 元组混入，已正常降级，不再误报为旧库未迁移。
                if _is_missing_column_error(e) and not _audit_legacy_warned:
                    _audit_legacy_warned = True
                    logger.error(
                        "ai_audit_logs 缺 error_class/attempt_count/attempts_json 列"
                        "（旧库未迁移），审计已降级写入；请重启服务触发迁移补列，"
                        "否则面板无法按失败类别聚合"
                    )
            except Exception as e2:
                # ✅ R108（P1-2）：本级同样只对「缺列/行宽守卫」继续降级；
                #    撞锁等瞬态上抛重放（行宽 ValueError 属守卫自抛，维持
                #    既有「再降一级」语义）。
                if not (_is_missing_column_error(e2) or isinstance(e2, ValueError)):
                    raise
                # 第二级降级：11 列（旧库缺 config_id/base_url，保留 scene）
                try:
                    if all(len(r) >= 11 for r in rows):
                        await conn.executemany(_AUDIT_INSERT_NO_IDENTITY, [r[:11] for r in rows])
                    else:
                        raise ValueError(
                            f"审计行宽 {min(len(r) for r in rows)} < 11，无法按"
                            "「保留场景、不含配置身份」降级写入（可能已变更审计元组结构）"
                        )
                except Exception as e3:
                    if not (_is_missing_column_error(e3) or isinstance(e3, ValueError)):
                        raise
                    # 更旧库缺 scene：再降一级，审计行不丢
                    if all(len(r) >= 10 for r in rows):
                        await conn.executemany(_AUDIT_INSERT_LEGACY, [r[:10] for r in rows])
                    else:
                        logger.error(
                            "审计行宽 %d < 10，无法降级写入（可能已变更审计元组结构），" "本批 %d 条审计记录丢弃",
                            min(len(r) for r in rows),
                            len(rows),
                        )
                        await conn.commit()
                        return
                    if not _audit_legacy_warned:
                        _audit_legacy_warned = True
                        logger.error(
                            "ai_audit_logs 缺 scene/config_id/base_url 列（旧库未迁移），"
                            "审计已降级写入；请重启服务以触发迁移补列"
                        )
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

    ✅ A1（2026-10-05 · 审计溢出队列）：即使 ``retry_db_op`` 三次重试全部
       失败，也不再直接丢失审计记录 —— 该行进入内存溢出队列
       ``_audit_overflow``，等待下一次 flush 优先落库（旧数据优先）。
       队列带双重护栏：
       - 容量上限 ``AUDIT_OVERFLOW_MAX_BATCHES``，超限时驱逐最旧批次并告警
       - 滞留时长上限 ``AUDIT_OVERFLOW_MAX_AGE``，超期批次直接丢弃并告警
       这两条护栏确保长时间 DB 故障不会拖垮内存，且每一次丢失都留下可
       检索的告警（对齐 AGENTS.md「静默丢失零容忍」原则）。
    """
    global _audit_legacy_warned
    with _audit_lock:
        rows = list(_audit_buffer) if _audit_buffer else None
        if rows:
            _audit_buffer.clear()
            _audit_last_flush["ts"] = time.time()

    if rows:
        try:
            await retry_db_op(lambda: _flush_audit_rows_once(rows))
        except Exception as e:
            # 主缓冲重试耗尽 → 转溢出队列，等待后续 flush 落库
            _enqueue_audit_overflow(rows, str(e))

    # 顺带尝试消费溢出队列（旧数据优先）：即使本次主缓冲为空也尝试旧批次，
    # 避免溢出队列被"新数据"堵死，且在无新审计条目时仍能恢复历史滞留数据。
    await _drain_audit_overflow()


async def _drain_audit_overflow() -> None:
    """从溢出队列头部逐批落库；任一批次失败即返回（下一轮继续）。"""
    if not _audit_overflow:
        return
    _expire_audit_overflow()
    while _audit_overflow:
        _entry_ts, _rows = _audit_overflow.popleft()
        try:
            await retry_db_op(lambda r=_rows: _flush_audit_rows_once(r))
        except Exception as e2:
            _enqueue_audit_overflow(_rows, str(e2), _entry_ts)
            break


def _enqueue_audit_overflow(rows: list, reason: str, original_ts: float | None = None) -> None:
    """把未落库的审计行移入溢出队列（受容量/时长护栏约束）。

    调用者必须在 ``_audit_buffer`` 已经清空之后调用本函数，否则会与新到达的
    审计条目竞争。溢出队列条目为 ``(ts, rows)`` 元组，``ts`` 记录该批首次入队
    的时间戳，用于超期驱逐判定。
    """
    global _audit_overflow_evicted
    now = time.time()
    if original_ts is None:
        original_ts = now
    with _audit_lock:
        # 1) 容量护栏：超过上限则从队首驱逐最旧批次
        while len(_audit_overflow) >= AUDIT_OVERFLOW_MAX_BATCHES:
            dropped_ts, dropped_rows = _audit_overflow.popleft()
            _audit_overflow_evicted += 1
            logger.warning(
                "AI 审计溢出队列已满（%d 批），驱逐最旧批次（%d 条，滞留 %.1fs）；" "累计驱逐 %d 批",
                AUDIT_OVERFLOW_MAX_BATCHES,
                len(dropped_rows),
                now - dropped_ts,
                _audit_overflow_evicted,
            )
        _audit_overflow.append((original_ts, list(rows)))
        logger.warning(
            "AI 审计批量落库重试耗尽，%d 条转入溢出队列等待下轮落库（队列 %d 批）：%s",
            len(rows),
            len(_audit_overflow),
            reason,
        )


def _expire_audit_overflow() -> None:
    """从溢出队列头部丢弃滞留时间超过 ``AUDIT_OVERFLOW_MAX_AGE`` 的批次。

    与容量护栏配合，防止长时间 DB 故障下溢出队列无限堆积。每次驱逐都会
    记录累计条数，便于运维观测审计丢失规模。
    """
    global _audit_overflow_evicted
    now = time.time()
    dropped_rows_total = 0
    while _audit_overflow and (now - _audit_overflow[0][0]) > AUDIT_OVERFLOW_MAX_AGE:
        _ts, _rows = _audit_overflow.popleft()
        _audit_overflow_evicted += 1
        dropped_rows_total += len(_rows)
    if dropped_rows_total:
        logger.warning(
            "AI 审计溢出队列驱逐滞留超过 %.0fs 的 %d 条记录（累计驱逐 %d 批）；" "如需追补请检查 DB 状态",
            AUDIT_OVERFLOW_MAX_AGE,
            dropped_rows_total,
            _audit_overflow_evicted,
        )


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
    # ✅ R109（P1）：HTTP 401（Unauthorized / 令牌失效）与 402/403/404 同属
    #    永久性认证错误——token 过期/吊销后重试必然再败，生产实证 agnes 401
    #    「该令牌状态不可用」被当作瞬态重试 49 次。现统一纳入非重试集合，
    #    触发配额冷却 + 不浪费 HTTP 往返。429 限流仍走退避路径（可恢复）。
    if "HTTP 401" in s or "HTTP 402" in s or "HTTP 403" in s or "HTTP 404" in s:
        return True
    low = s.lower()
    return any(
        tag in low
        for tag in (
            "insufficient balance",
            "quota_exceeded",
            "quota exceeded",
            "exceeded your current quota",
            "no valid authorization",
            "invalid api key",
            "unsupportedmodel",
            "not found for account",
            "令牌状态不可用",
            "token status",
            "invalid token",
            "unauthorized",
        )
    )


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
QUOTA_COOLDOWN_MAX = max(0.0, float(getattr(settings, "ai_fail_cooldown_max_seconds", 600.0) or 0.0))
_quota_cool_until: dict[str, float] = {}
_quota_last_probe: dict[str, float] = {}
_quota_lock = threading.Lock()

# ✅ 2026-11-05（429 处理）：``Retry-After`` 可能出现在异常文本（openai_compatible
# 拼接）或 httpx 响应的 headers 中；只接受数字秒，HTTP-date 形态忽略。
_RETRY_AFTER_RE = re.compile(r"retry[-\s]?after\s*[:=]?\s*(\d+(?:\.\d+)?)", re.I)


def _extract_retry_after(err) -> float | None:
    """从异常/响应头解析 ``Retry-After``（秒）；取不到返回 None。"""
    raw = None
    try:
        resp = getattr(err, "response", None)
        headers = getattr(resp, "headers", None)
        if headers is not None:
            raw = headers.get("retry-after")
    except Exception:
        raw = None
    if raw is not None:
        try:
            return max(0.0, float(str(raw).strip()))
        except (TypeError, ValueError):
            pass
    m = _RETRY_AFTER_RE.search(str(err or ""))
    if m:
        try:
            return max(0.0, float(m.group(1)))
        except (TypeError, ValueError):
            pass
    return None


def _is_rate_limit_error(err) -> bool:
    """是否属于「限流」类错误（429 / rate limit / too many requests / 限流）。

    与 ``_is_quota_error`` 的区别：后者还包含 402/403/404 等确定性配额/鉴权
    错误；限流有独立的 ``Retry-After`` 语义，不能与普通指数退避混为一谈。
    """
    s = (str(err or "")).lower()
    return "429" in s or "rate limit" in s or "too many requests" in s or "限流" in s


def _is_quota_error(err) -> bool:
    """配额/限流/认证类错误（429 + 确定性错误）：冷却节流的对象。"""
    s = str(err or "")
    return "HTTP 429" in s or is_non_retryable_error(err)


def _quota_cooldown_seconds(err) -> float:
    """本次配额失败应设置的冷却秒数。

    ✅ 2026-11-05：429 优先尊重上游 ``Retry-After``（不低于默认冷却，不让
    限流参与普通指数退避的放大）；非限流类确定性错误沿用默认冷却。
    """
    base = QUOTA_FAIL_COOLDOWN
    if _is_rate_limit_error(err):
        retry_after = _extract_retry_after(err)
        if retry_after is not None:
            cap = QUOTA_COOLDOWN_MAX if QUOTA_COOLDOWN_MAX > 0 else retry_after
            return max(base, min(retry_after, cap))
    return base


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
    """记录配额类失败：进入冷却窗口。

    ✅ 2026-11-05：429 的冷却时长优先尊重上游 ``Retry-After``（见
    ``_quota_cooldown_seconds``），不再用固定值反复撞限流。
    """
    cooldown = _quota_cooldown_seconds(err)
    with _quota_lock:
        _quota_cool_until[pname] = time.time() + cooldown
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


# ---------- ✅ R110（P1-2 · 思考吞噬翻倍失败后 provider 短时冷却）----------
# R109 实证：sensetime 1536→3072 翻倍后仍失败，降级链若全是推理模型会重复
# "吞噬→翻倍→失败"。翻倍失败后标记该 provider 短时冷却，后续候选跳过，
# 与既有 `_quota_cool_until` 同构（time + dict + threading.Lock）。
THINKING_DEAD_COOLDOWN = max(0.0, float(getattr(settings, "ai_thinking_dead_cooldown", 60.0) or 0.0))
_thinking_dead_until: dict[str, float] = {}
_thinking_dead_lock = threading.Lock()


def _note_thinking_dead(pname: str) -> None:
    """记录「思考吞噬翻倍失败」：该 provider 进入短时冷却。

    触发条件：``_call_one_candidate`` 内翻倍 max_tokens 后仍 ``_is_thinking_exhausted``
    （已重试一次、达到上限、或翻倍前已达上限）。冷却期内 ``_gate_candidates`` 跳过，
    与 ``_quota_cooldown_active`` 同走 ``circuit_skipped`` 审计口径。
    """
    if THINKING_DEAD_COOLDOWN <= 0 or not pname:
        return
    with _thinking_dead_lock:
        _thinking_dead_until[pname] = time.time() + THINKING_DEAD_COOLDOWN
    logger.warning(
        "Provider %s 翻倍后仍思考吞噬，标记 %ds 冷却（期间跳过该 provider）", pname, int(THINKING_DEAD_COOLDOWN)
    )


def _is_thinking_dead(pname: str) -> bool:
    """该 provider 是否在思考吞噬冷却中。"""
    now = time.time()
    with _thinking_dead_lock:
        until = _thinking_dead_until.get(pname, 0.0)
        if now >= until:
            if until:
                _thinking_dead_until.pop(pname, None)
            return False
        return True


def clear_thinking_dead(pname: str = "") -> None:
    """清除思考吞噬冷却（成功调用后重置）。"""
    with _thinking_dead_lock:
        if pname:
            _thinking_dead_until.pop(pname, None)
        else:
            _thinking_dead_until.clear()


def reset_thinking_dead() -> None:
    """清空思考吞噬状态与「疑似推理模型」标记（测试隔离 / 配置变更后立即生效用）。"""
    clear_thinking_dead()
    with _thinking_suspect_lock:
        _thinking_suspect.clear()


# ---------- ✅ R110（P1-1 · 跨章全熔断快速失败）----------
# R109 实证：所有候选均熔断/配额冷却时，`_gate_candidates` 放行 1 个 probe；
# 多章生成各自独立 probe → 5 章 × probe 超时 ≈ 150-300s 空转。
# 连续 N 次所有候选不可用后激活短时快速失败，不再 probe。
ALL_CIRCUIT_FAST_FAIL_THRESHOLD = max(0, int(getattr(settings, "ai_all_circuit_fast_fail_threshold", 2) or 0))
ALL_CIRCUIT_FAST_FAIL_COOLDOWN = max(0.0, float(getattr(settings, "ai_all_circuit_fast_fail_cooldown", 60.0) or 0.0))
_all_circuit_consecutive: int = 0
_all_circuit_dead_until: float = 0.0
_all_circuit_lock = threading.Lock()


def _is_all_circuit_fast_fail() -> bool:
    """是否处于「全熔断快速失败」窗口。"""
    with _all_circuit_lock:
        return _all_circuit_dead_until > 0 and time.time() < _all_circuit_dead_until


def _record_all_circuit_outcome(success: bool) -> None:
    """记录本次 gate 结果；连续失败达阈值后激活快速失败。

    - success=True（gate 放行非空候选 / 某次调用成功）→ 立即清零
    - success=False（所有候选熔断/冷却）→ 计数 +1，达阈值后置快速失败窗口
    """
    global _all_circuit_consecutive, _all_circuit_dead_until
    if ALL_CIRCUIT_FAST_FAIL_THRESHOLD <= 0:
        return
    with _all_circuit_lock:
        if success:
            if _all_circuit_consecutive > 0 or _all_circuit_dead_until > 0:
                logger.info("全熔断快速失败解除（有候选可用）")
            _all_circuit_consecutive = 0
            _all_circuit_dead_until = 0.0
        else:
            _all_circuit_consecutive += 1
            if _all_circuit_consecutive >= ALL_CIRCUIT_FAST_FAIL_THRESHOLD:
                _all_circuit_dead_until = time.time() + ALL_CIRCUIT_FAST_FAIL_COOLDOWN
                logger.warning(
                    "全熔断快速失败激活：连续 %d 次所有候选不可用，%ds 内快速失败（不再 probe）",
                    _all_circuit_consecutive,
                    int(ALL_CIRCUIT_FAST_FAIL_COOLDOWN),
                )


def reset_all_circuit_fast_fail() -> None:
    """清空全熔断快速失败状态（测试隔离 / 配置变更后立即生效用）。"""
    global _all_circuit_consecutive, _all_circuit_dead_until
    with _all_circuit_lock:
        _all_circuit_consecutive = 0
        _all_circuit_dead_until = 0.0


def reset_reliability() -> None:
    """清空进程内可靠性统计与确定性死配置连击（测试隔离用）。

    ✅ R169：与 reset_thinking_dead / reset_all_circuit_fast_fail 同族——
    ``_provider_reliability``（滑动窗口成功率，影响降级链过滤）与
    ``_det_dead_streak``（确定性 404 连击判死，R108）同为模块级状态，
    conftest 未重置时跨用例泄漏：任一用例触发连击后，同进程后续依赖相同
    身份键（无 config_id 的候选，如内置 agnes）的用例会被静默剔除出链，
    表现为顺序相关偶发红。
    """
    with _provider_reliability_lock:
        _provider_reliability.clear()
        _det_dead_streak.clear()


# ✅ 2026-11-05（推理模型输出预算）：识别「会思考」的候选，预先给足输出预算，
# 避免 1536 这类小预算被思考过程吃光。名称判据之外，本会话一旦发生过
# 「思考吞噬」，该 provider 也被记为疑似推理模型（覆盖 custom 任意模型名）。
_REASONING_MODEL_RE = re.compile(
    r"(reasoner|reasoning|thinking|qwq|deepseek[-_]?r1|(?:^|[-_/])(?:o1|o3)(?:[-_/]|$))", re.I
)
_thinking_suspect_lock = threading.Lock()
_thinking_suspect: set[str] = set()


def _looks_like_reasoning_model(model: str) -> bool:
    """按模型名判断是否推理模型（保守封闭枚举，避免误伤普通模型）。"""
    return bool(_REASONING_MODEL_RE.search(str(model or "")))


def _note_thinking_suspect(pname: str) -> None:
    """记录「该 provider 出现过思考吞噬」，后续调用预先给足输出预算。"""
    if not pname:
        return
    with _thinking_suspect_lock:
        _thinking_suspect.add(pname)


def _is_thinking_suspect(pname: str) -> bool:
    with _thinking_suspect_lock:
        return pname in _thinking_suspect


def _is_reasoning_candidate(c: dict) -> bool:
    """候选是否应按推理模型给输出预算：名称命中 / 全局 reasoning_effort / 已实测吞噬。"""
    if _looks_like_reasoning_model(c.get("model", "")):
        return True
    if str(getattr(settings, "ai_reasoning_effort", "") or "").strip():
        return True
    return _is_thinking_suspect(c.get("provider_name", ""))


def _reasoning_retry_tokens(current: int, cap: int, floor: int) -> int:
    """思考吞噬/截断重试时的目标 max_tokens（一次性放大，严格一次）。

    旧实现 ``min(mt*2, cap)``：1536→3072，仍装不下思考过程；生产实测 14 次
    失败累计浪费 675.5s。现至少抬到 ``ai_reasoning_retry_floor_tokens``
    （默认 8192），让「思考 + 正文」一次到位。
    """
    target = max(current * 2, floor)
    return min(target, cap) if cap > 0 else target


def _is_thinking_exhausted(err) -> bool:
    """推理模型把 max_tokens 全部耗在思考过程（finish_reason=length、正文为空）。

    2026-09-22（调用次数优化 O3）：运行库实测 101 次该类失败 —— 旧实现
    直接判失败进入降级链，每个推理模型候选各白烧一次。现由调用方识别后
    翻倍 max_tokens 重试一次（仅「正文为空」触发，不影响正常响应）。
    """
    s = str(err or "")
    return "finish_reason=length" in s and "正文为空" in s


# ---------- 批处理按 provider 成功率分级（2026-09-22 · 调用次数优化 O6） ----------
def get_effective_batch_size(
    base: int, provider_name: str = "", model: str = "", config_id: str = "", base_url: str = ""
) -> int:
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
    total, rate = _provider_reliability_snapshot(provider_name, model, config_id, base_url)
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
            base,
            pname,
            str((cfg or {}).get("model") or ""),
            str((cfg or {}).get("id") or ""),
            str((cfg or {}).get("base_url") or ""),
        )
    except Exception:
        return get_effective_batch_size(base, "")


# ---------- ✅ 2026-11-05（审计合并 · 失败分类） ----------
# 一次逻辑业务调用（含重试 / 降级链 / 对冲副本）落**一条**主审计记录，
# 逐次尝试压缩进 attempts_json。失败按机器可读类别聚合，避免面板只能按
# 自由文本 error 前缀猜（"HTTP 429..." / "TimeoutError..." 混杂）。
ERROR_CLASSES = (
    "rate_limit",
    "auth",
    "timeout",
    "truncated",
    "network",
    "upstream_5xx",
    "invalid_response",
    "circuit_skipped",
    "cancelled",
    "unknown",
)

_HTTP_STATUS_RE = re.compile(r"\b(?:http[^\d]{0,3})?(\d{3})\b", re.I)


def _error_class(err) -> str:
    """把异常/错误文本映射为稳定类别码（ERROR_CLASSES 之一）。

    判定顺序刻意从「确定性、可动作」到「兜底」：限流 > 认证 > 超时 >
    截断（思考吞噬）> 网络 > 上游 5xx > 响应格式 > 熔断跳过 > unknown。
    截断必须排在 invalid_response 之前 —— 否则「finish_reason=length +
    正文为空」会被归成 invalid_response，丢失「加预算」这一唯一正确处置。
    """
    if not err:
        return ""
    if isinstance(err, str):
        msg = err
    else:
        msg = f"{type(err).__name__}: {err}"
    low = msg.lower()
    code = None
    match = _HTTP_STATUS_RE.search(low)
    if match:
        try:
            code = int(match.group(1))
        except ValueError:
            code = None

    if code == 429 or "rate limit" in low or "too many requests" in low or "限流" in low:
        return "rate_limit"
    if code in (401, 403) or "unauthorized" in low or "invalid api key" in low or "认证" in low or "鉴权" in low:
        return "auth"
    if isinstance(err, (TimeoutError, asyncio.TimeoutError)) or "timed out" in low or "timeout" in low or "超时" in low:
        return "timeout"
    if _is_thinking_exhausted(err) or "finish_reason=length" in low or "truncated" in low or "截断" in low:
        return "truncated"
    if "connecterror" in low or "network" in low or "connection" in low or "dns" in low or "网络" in low:
        return "network"
    if code is not None and 500 <= code <= 599:
        return "upstream_5xx"
    if (
        "invalid json" in low
        or "json" in low
        or "parse" in low
        or "invalid response" in low
        or "empty" in low
        or "正文为空" in low
        or "空内容" in low
        or "格式" in low
    ):
        return "invalid_response"
    if "circuit" in low or "熔断" in low or "冷却" in low:
        return "circuit_skipped"
    return "unknown"


class _LogicalAuditScope:
    """一次逻辑调用的逐次尝试收集器（供合并审计 + 诊断）。

    对冲副本在独立 asyncio task 中执行，但共享同一个 scope 实例（由调用方
    显式传入，不依赖 ContextVar 的 task 拷贝语义），因此胜者/败者/超时尝试
    都能归到同一条主记录。
    """

    __slots__ = ("scene", "started_at", "attempts", "cancelled")

    def __init__(self, scene: str = ""):
        self.scene = scene or ""
        self.started_at = time.monotonic()
        self.attempts: list[dict] = []
        self.cancelled = False

    def record(
        self,
        *,
        provider_name: str,
        model: str,
        config_id: str = "",
        base_url: str = "",
        duration: float = 0.0,
        success: bool,
        error: str = "",
        error_class: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> None:
        self.attempts.append(
            {
                "provider": (provider_name or "")[:64],
                "model": (model or "")[:64],
                "config_id": (config_id or "")[:64],
                "base_url": (base_url or "")[:200],
                "ok": 1 if success else 0,
                "duration": round(float(duration or 0.0), 2),
                "error_class": error_class or "",
                "error": (error or "")[:120],
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "cached_tokens": int(cached_tokens or 0),
            }
        )

    def last(self) -> dict:
        return self.attempts[-1] if self.attempts else {}

    def last_success(self) -> dict:
        for item in reversed(self.attempts):
            if item.get("ok"):
                return item
        return {}

    def attempts_json(self, limit: int = 2000) -> str:
        """逐次尝试序列化（硬截断保护，防止异常长的错误文本撑大审计行）。"""
        try:
            raw = json.dumps(self.attempts, ensure_ascii=False)
        except Exception:
            return ""
        return raw[:limit]

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


def _update_reliability_stats(
    provider_name: str,
    model: str,
    action: str,
    success: bool,
    error: str = "",
    config_id: str = "",
    base_url: str = "",
) -> None:
    """更新进程内实时可靠性统计（从 _log_audit 拆出，供逐次尝试复用）。"""
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
                    # ✅ R108（P1-4）：任一成功 → 确定性连击清零（改对模型名自愈）
                    _det_dead_streak.pop(key, None)
                else:
                    stats["fail"] += 1
                    # ✅ R108（P1-4）：确定性 404 签名连击计数；**非连续**失败
                    #    （瞬态/其它错误）清零——429 限流夹在两次 404 之间说明
                    #    请求到达过供应商，模型可能已改，不冤枉。
                    if _is_deterministic_dead_error(error):
                        streak = _det_dead_streak.get(key, 0) + 1
                        _det_dead_streak[key] = streak
                        if streak == DET_DEAD_STREAK_THRESHOLD:
                            logger.warning(
                                "配置 %s 连续 %d 次确定性模型/端点不存在错误，"
                                "运行期判为死配置并从候选链剔除（保存该配置即重置）",
                                key,
                                streak,
                            )
                    else:
                        _det_dead_streak.pop(key, None)
    except Exception as e:
        # ✅ 2026-10-06（B-4 · 裸 pass 吞掉可观测性）：可靠性统计异常此前
        #    与审计写入共用一个 ``except Exception: pass``，既不留日志，
        #    又让「统计没更新」与「审计没落库」两种后果无法区分。
        #    统计失真会**直接影响候选排序**（死配置剔除 / 主配置后置 /
        #    降级链分档），静默失真 = 排序依据悄悄错。故单独告警。
        logger.warning("更新 Provider 实时可靠性统计失败（%s/%s action=%s）: %s", provider_name, model, action, e)


async def _append_audit_row(row: tuple) -> None:
    """审计行入缓冲并按其触发条件攒批落库（失败只告警，绝不中断 AI 主流程）。"""
    try:
        with _audit_lock:
            _audit_buffer.append(row)
            buf_len = len(_audit_buffer)
            due = time.time() - _audit_last_flush["ts"] >= _AUDIT_FLUSH_INTERVAL
        if buf_len >= _AUDIT_BATCH_SIZE or (buf_len and due):
            await _flush_audit_buffer()
    except Exception as e:
        # 审计是「尽力而为」，写入失败绝不能中断 AI 调用主流程（保持原语义）；
        # 但必须留痕 —— 否则「用量统计/审计日志」整块功能静默为空而无人知晓。
        logger.warning(
            "写入 AI 调用审计缓冲失败（%s/%s，本次审计记录丢失）: %s",
            row[1] if len(row) > 1 else "?",
            row[3] if len(row) > 3 else "?",
            e,
        )


def _build_audit_row(
    *,
    provider_name: str,
    model: str,
    action: str,
    duration: float,
    success: bool,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    error: str = "",
    scene: str = "",
    config_id: str = "",
    base_url: str = "",
    error_class: str = "",
    attempt_count: int = 1,
    attempts_json: str = "",
) -> tuple:
    """构造 16 列审计行（新列追加在末尾，降级切片链依赖此顺序）。"""
    return (
        str(uuid.uuid4()),
        provider_name,
        model,
        action,
        int(prompt_tokens or 0),
        int(completion_tokens or 0),
        int(cached_tokens or 0),
        duration,
        1 if success else 0,
        (error or "")[:300],
        (scene or "")[:64],
        (config_id or "")[:64],
        (base_url or "")[:512],
        (error_class or "")[:32],
        max(0, int(attempt_count or 0)),
        (attempts_json or "")[:2000],
    )


async def _log_audit(
    provider_name: str,
    model: str,
    action: str,
    duration: float,
    success: bool,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    error: str = "",
    scene: str = "",
    config_id: str = "",
    base_url: str = "",
    error_class: str = "",
    attempt_count: int = 1,
    attempts_json: str = "",
):
    """记录 AI 调用审计日志（P0-4：攒批落库，避免每次调用一次 fsync commit）

    token 统计为可选参数（默认 0），保持既有 5 位置参数调用方式兼容。

    ✅ 2026-09-16 新增：同时更新进程内实时可靠性统计，供 _fallback_chain
    自动排除低成功率 provider。

    ✅ 2026-09-17 新增：``error`` 记录失败原因摘要（前 300 字符）——
    实测近 7 天 1061 条失败记录零信息，无法区分 429 限流/超时/认证失败，
    排障只能翻 stderr。落库后 /ai/stats 可输出「失败原因 TOP」分布。

    ✅ 2026-09-21 新增：``scene`` 业务场景标记（如 outline_draft /
    outline_review），/ai/stats 按场景聚合，定位「哪一步在烧调用」。

    ✅ 2026-11-05：新增 ``error_class`` / ``attempt_count`` / ``attempts_json``
    （追加在 13 列之后，降级切片链安全）。普通调用仍走本函数（一条尝试
    一条记录）；业务调用由 ``_chat_with_fallback_result`` 合并成一条主记录。
    """
    _update_reliability_stats(provider_name, model, action, success, error, config_id, base_url)
    if not error_class and error:
        error_class = _error_class(error)
    await _append_audit_row(
        _build_audit_row(
            provider_name=provider_name,
            model=model,
            action=action,
            duration=duration,
            success=success,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            error=error,
            scene=scene,
            config_id=config_id,
            base_url=base_url,
            error_class=error_class,
            attempt_count=attempt_count,
            attempts_json=attempts_json,
        )
    )


async def _record_attempt(
    scope: _LogicalAuditScope | None,
    *,
    provider_name: str,
    model: str,
    config_id: str = "",
    base_url: str = "",
    duration: float = 0.0,
    success: bool,
    error: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    counts_reliability: bool = True,
) -> None:
    """记录**一次底层尝试**：更新实时可靠性 + 收集进逻辑调用 scope。

    ✅ 2026-11-05（方案 B）：被合并的尝试不再各写一条审计行；只有 scope
    为空（非业务调用路径 / 单元测试直调 ``_attempt_candidate``）才回退到
    单条落库，保证旧调用方行为不变。取消的尝试只进 scope（用于 attempt
    计数与诊断）而不更新可靠性——取消不是供应商的失败。
    """
    error_class = "" if success else _error_class(error)
    if scope is None:
        # 非业务调用路径（单元测试直调 / 兼容旧调用方）：保持「一次尝试 =
        # 一条审计行」，并复用 ``_log_audit`` 单一出口（可靠性统计与审计
        # 写入同源，且 action 字面量可被白名单静态扫描覆盖）。
        await _log_audit(
            provider_name,
            model,
            "chat",
            duration,
            success,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            error=error,
            config_id=config_id,
            base_url=base_url,
            error_class=error_class,
            attempt_count=1,
        )
        return
    if counts_reliability:
        _update_reliability_stats(provider_name, model, "chat", success, error, config_id, base_url)
    scope.record(
        provider_name=provider_name,
        model=model,
        config_id=config_id,
        base_url=base_url,
        duration=duration,
        success=success,
        error=error,
        error_class=error_class,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
    )


async def _flush_logical_audit(
    scope: _LogicalAuditScope,
    *,
    success: bool,
    error: str = "",
    error_class: str = "",
) -> None:
    """把一次逻辑调用的逐次尝试合并成**一条**主审计记录。

    主记录语义：
    - success=True：winning attempt 的 provider/model/token，duration=整段逻辑
      调用耗时；attempt_count=实际尝试数（含已取消的对冲副本）。
    - success=False：最后一次失败尝试的 provider/model，error=最终错误摘要，
      error_class=最终错误类别；逐次失败类别仍在 attempts_json 中可查。
    - 无任何尝试（gate 快速失败，如全熔断/全部无 Key）：attempt_count=0，
      error_class=circuit_skipped，使面板「失败调用」与审计行能对上。
    """
    try:
        attempt = scope.last_success() if success else (scope.attempts[-1] if scope.attempts else {})
        if not attempt:
            attempt = {}
        scene = scope.scene
        final_error = "" if success else (error or attempt.get("error", ""))
        final_class = "" if success else (error_class or attempt.get("error_class", "") or _error_class(final_error))
        if not success and not scope.attempts and not final_class:
            final_class = "circuit_skipped"
        await _append_audit_row(
            _build_audit_row(
                provider_name=attempt.get("provider_name", attempt.get("provider", "")),
                model=attempt.get("model", ""),
                action="chat",
                duration=scope.elapsed,
                success=success,
                prompt_tokens=attempt.get("prompt_tokens", 0) or 0,
                completion_tokens=attempt.get("completion_tokens", 0) or 0,
                cached_tokens=attempt.get("cached_tokens", 0) or 0,
                error=final_error,
                scene=scene,
                config_id=attempt.get("config_id", ""),
                base_url=attempt.get("base_url", ""),
                error_class=final_class,
                attempt_count=len(scope.attempts),
                attempts_json=scope.attempts_json(),
            )
        )
    except Exception as e:
        # 审计失败不得改变业务调用结果（保持「尽力而为」语义）
        logger.warning("合并写入逻辑调用审计失败（scene=%s）: %s", getattr(scope, "scene", ""), e)


def _reliability_key(provider_name: str, model: str = "", config_id: str = "", base_url: str = "") -> str:
    """可靠性统计身份键：优先 config_id；否则 provider+model+base_url。

    同一 provider 可以有多条配置、多个模型或不同接入地址，只在 provider 维度
    统计会把它们的成败混在一起，导致「一条坏配置拖死整个厂商」。
    """
    cid = (config_id or "").strip()
    if cid:
        return f"cfg:{cid}"
    return f"prov:{(provider_name or '').strip()}" f"|model:{(model or '').strip()}" f"|url:{(base_url or '').strip()}"


def _provider_reliability_snapshot(
    provider_name: str, model: str = "", config_id: str = "", base_url: str = ""
) -> tuple[int, float | None]:
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


def _provider_success_rate(
    provider_name: str, model: str = "", config_id: str = "", base_url: str = ""
) -> float | None:
    """返回某个配置身份的实时成功率（0~1），样本不足时返回 None。"""
    return _provider_reliability_snapshot(provider_name, model, config_id, base_url)[1]


# ---------- R103（2026-10-18 · 日志审计遗留 L1 收口）：配置健康可观测 ----------
# 背景（R102 日志基线）：provider_factory 的熔断/冷却/实时成功率剔除都是
# **进程内存态的自愈机制**——「已剔除实时成功率过低的候选」在生产日志里刷了
# 209 条 WARNING，但「哪条配置被判死、样本多少、成功率多少」只在日志里，
# 「文本模型配置」页完全看不见 ⇒ 用户看到的是「明明配了 9 条，降级链却只有
# 3 条」，只能靠翻日志。本组函数把既有判定收敛为**单一只读访问器**，
# 供启动报告与 /ai/configs-health 共用，不新增任何存储、不改任何选路行为。
def provider_health_snapshot(provider_name: str, model: str = "", config_id: str = "", base_url: str = "") -> dict:
    """某个配置身份的运行时健康快照（只读，fail-soft 永不为空）。

    返回：``{"samples": int, "success_rate": float|None, "dead": bool,
             "circuit_open": bool, "cooldown_remaining_s": float,
             "quota_cooldown_s": float}``。

    ⚠️ 判据单一事实源：dead 走 ``_is_dead_provider``、熔断走
    ``circuit_breaker.allow_request``、冷却走 ``_quota_cooldown_active``——
    与运行时选路逐字同源，绝不另写一套阈值（否则界面说的和实际做的不一致）。
    """
    pname = (provider_name or "").strip()
    out: dict = {
        "samples": 0,
        "success_rate": None,
        "dead": False,
        "circuit_open": False,
        "cooldown_remaining_s": 0.0,
        "quota_cooldown_s": 0.0,
    }
    try:
        total, rate = _provider_reliability_snapshot(pname, model, config_id, base_url)
        out["samples"] = total
        out["success_rate"] = rate
        out["dead"] = _is_dead_provider(pname, model, config_id, base_url)
    except Exception:  # noqa: BLE001 - 快照失败不影响调用方主流程
        pass
    try:
        from app.services.ai.workflows_base import circuit_breaker

        out["circuit_open"] = not circuit_breaker.allow_request(pname)
        out["cooldown_remaining_s"] = round(circuit_breaker.cooldown_remaining(pname), 1)
    except Exception:  # noqa: BLE001 - 熔断器不可读时按「未熔断」快照，不影响调用方
        pass
    try:
        if QUOTA_FAIL_COOLDOWN > 0 and _quota_cooldown_active(pname):
            out["quota_cooldown_s"] = round(max(0.0, _quota_cool_until.get(pname, 0.0) - time.time()), 1)
    except Exception:  # noqa: BLE001 - 冷却态读取失败按 0 呈现，不影响调用方
        pass
    return out


async def report_dead_configs_at_startup() -> list[dict]:
    """启动期死配置报告（R103）：把预热后的内存统计翻译成可行动清单。

    动机：``warmup_reliability_from_db`` 恢复的是**统计**，而死配置判定
    （``_is_dead_provider``）发生在每次选路时——「重启后哪些配置一上来就
    会被剔除」此前无可观测输出。生产实证（R102 基线）：4 条 nvidia 死配置
    （HTTP 404 UnsupportedModel）与 volcengine 不支持 coding plan 的模型
    长期占据候选池，每次冷启动都先白打一轮。

    实现：枚举 ``ai_config`` 全部行，逐行走 ``provider_health_snapshot``
    （身份键解析与运行时选路**同一函数**：库里有 config_id ⇒ ``cfg:{id}``
    键，绝不自己拼键——自己拼必然与运行时错位）。**只读日志、零行为
    变更**：不剔除、不禁用、不写库，仅把死配置身份逐条 WARNING，引导用户
    去「文本模型配置」清理（或接运行时厂商开关）。fail-soft：任何异常只记
    debug，绝不阻断启动。
    """
    dead: list[dict] = []
    try:
        conn = await get_conn()
        cur = await conn.execute("SELECT id, provider_name, model, base_url FROM ai_config")
        rows = await cur.fetchall()
        for r in rows:
            cid = str(r["id"] or "")
            pname = str(r["provider_name"] or "")
            model = str(r["model"] or "")
            url = str(r["base_url"] or "")
            snap = provider_health_snapshot(pname, model, cid, url)
            if not snap.get("dead"):
                continue
            dead.append(
                {
                    "config_id": cid,
                    "provider_name": pname,
                    "model": model,
                    "base_url": url,
                    "samples": snap.get("samples", 0),
                    "success_rate": snap.get("success_rate"),
                }
            )
            # ✅ R111（P2-2 · 门槛倒挂透明化）：死配置「复活循环」可观测。
            det_dead = _det_dead_streak.get(_reliability_key(pname, model, cid, url), 0) >= DET_DEAD_STREAK_THRESHOLD
            total, _rate = _provider_reliability_snapshot(pname, model, cid, url)
            if not det_dead and total < _PROVIDER_DEAD_MIN_SAMPLES:
                logger.info(
                    "启动检查：死配置 %s/%s 样本仅 %d 次（< 死配置门槛 %d），"
                    "本轮判死主要靠确定性 404 连击而非统计口径 —— 重启后 24h 内"
                    "无新调用将随滑窗自然复活，建议直接清理该配置或接运行时"
                    "厂商开关禁用（scheme=%s）",
                    pname,
                    model or "?",
                    total,
                    _PROVIDER_DEAD_MIN_SAMPLES,
                    snap.get("samples", total),
                )

        if dead:
            logger.warning(
                "启动检查：%d 条 AI 配置近 24h 实时成功率过低，"
                "已被自动剔除出降级链（建议到「文本模型配置」清理或禁用）：%s",
                len(dead),
                "; ".join(
                    f"{d['provider_name']}/{d['model'] or '?'}"
                    f"（{d['samples']} 次样本，成功率 "
                    f"{(d['success_rate'] or 0):.0%}）"
                    for d in dead[:8]
                ),
            )
        else:
            logger.info("启动检查：无实时成功率过低的死配置（近 24h）")
    except Exception as e:  # noqa: BLE001 - 报告失败绝不影响启动
        logger.debug("启动死配置报告失败（不影响服务）: %s", e)
        return []
    return dead


def _is_dead_provider(provider_name: str, model: str = "", config_id: str = "", base_url: str = "") -> bool:
    """死配置判定：样本充足且实时成功率低于 ``_PROVIDER_DEAD_SUCCESS_RATE``。

    ✅ R108（P1-4）：新增**确定性死配置**前置判据——连续 ≥
    ``DET_DEAD_STREAK_THRESHOLD`` 次命中 UnsupportedModel/
    InvalidEndpointOrModel 签名即判死（重放必败，不必等统计口径攒样本，
    也不会被滑窗折半稀释）。``provider_health_snapshot`` / 启动报告 /
    前端健康列均复用本函数，判据自动同口径。
    """
    key = _reliability_key(provider_name, model, config_id, base_url)
    with _provider_reliability_lock:
        if _det_dead_streak.get(key, 0) >= DET_DEAD_STREAK_THRESHOLD:
            return True
    total, rate = _provider_reliability_snapshot(provider_name, model, config_id, base_url)
    return rate is not None and total >= _PROVIDER_DEAD_MIN_SAMPLES and rate < _PROVIDER_DEAD_SUCCESS_RATE


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

    # ✅ R178（D-1 · 2026-11-05）：内置兜底**豁免判死**。
    #    设计注释明写「内置兜底必须留在链内」（_fallback_chain:1582-1585），
    #    但该候选是在**成功率过滤之后**追加的、随后又流经本函数 —— 于是
    #    「兜底不得被截断」的设计意图被本函数打破：兜底一旦因成功率过低被判死，
    #    就会被**静默剔除**，而这恰恰发生在主配置也已不可用的最坏时刻。
    #    现对带 ``_is_builtin_fallback`` 标记的候选豁免判死 —— 只降级不剔除；
    #    它在链内本就位于尾部（_fallback_chain 末尾追加），仍保持「最后兜底」序。
    alive = [
        c
        for c in candidates
        if c.get("_is_builtin_fallback")
        or not _is_dead_provider(
            c.get("provider_name", ""), c.get("model", ""), c.get("config_id", ""), c.get("base_url", "")
        )
    ]
    if not alive:
        alive = list(candidates)  # 全部被判定为死配置 → 保底不空
    if len(alive) < len(candidates):
        dropped = [c.get("provider_name", "?") for c in candidates if c not in alive]
        # ✅ R178（D-2 · 2026-11-05）：设计内节流降为 INFO。
        #    生产实证（logs/backend.log 2026-10-28~11-04）：本条 WARNING 刷了
        #    372 次，配合「主配置后置」的 642 次共 1014 条，占全量 WARNING 的 24%，
        #    把真正的故障（429/401/锁冲突/库损坏）彻底淹没。按 R103 先例
        #    （test_config_health_closeout_r103_20261018.py:184-191「熔断/配额冷却
        #    跳过是设计内节流，降为 INFO」+「旧 WARNING 形态不得回流」），
        #    本条同属「自愈机制的正常动作」，降 INFO。
        logger.info("已剔除实时成功率过低的候选：%s", ", ".join(dropped))

    primary = [c for c in alive if c.get("_is_primary")]
    rest = [c for c in alive if not c.get("_is_primary")]
    if primary and rest:
        _total, rate = _provider_reliability_snapshot(
            primary[0].get("provider_name", ""),
            primary[0].get("model", ""),
            primary[0].get("config_id", ""),
            primary[0].get("base_url", ""),
        )
        if rate is not None and rate < _PROVIDER_DEMOTE_SUCCESS_RATE:
            # ✅ R178（D-2）：同上，设计内节流降 INFO（生产 642 条）。
            logger.info(
                "主配置 %s 实时成功率 %.1f%% < %.0f%%，本次调用将其后置到候选链尾部",
                primary[0].get("provider_name"),
                rate * 100,
                _PROVIDER_DEMOTE_SUCCESS_RATE * 100,
            )
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
                (_PROVIDER_MIN_SAMPLES,),
            )
            has_identity = True
        except Exception:
            # 旧库尚无 config_id/base_url 列：回退 provider+model 聚合
            cur = await conn.execute(
                "SELECT '', provider_name, model, '', SUM(success), COUNT(*) - SUM(success) "
                "FROM ai_audit_logs "
                "WHERE action='chat' AND created_at >= datetime('now','localtime','-1 day') "
                "GROUP BY provider_name, model HAVING COUNT(*) >= ?",
                (_PROVIDER_MIN_SAMPLES,),
            )
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
            loaded,
            "" if has_identity else "，旧库按 provider+model 聚合",
        )
    except Exception as e:
        logger.debug("预热 Provider 可靠性统计失败（不影响服务）: %s", e)


# ---------- A2：Provider 运行时状态持久化（2026-10-05）----------
# 目的：`_quota_cool_until` / `_quota_last_probe` 与 `circuit_breaker._per_provider`
#      是进程内 dict，重启即清零。若重启发生在冷却窗口或熔断窗口内，冷却/熔断
#      「蒸发」→ 下一次调用又白烧一次 429 或一次死配置网络往返。
# 写入策略：不修改 `record_failure` / `record_success` / `_note_quota_failure`
#      等热路径同步方法，改为由 lifespan 的周期任务（10s）flush 快照。
#      代价：最坏延迟 10s；收益：零热路径耦合、零调用签名变更、失败绝不影响主流程。
# 读取策略：启动时 warmup（表缺失/无数据 → 静默降级为旧行为）。
# 清理策略：flush 时删掉超过 24h 未更新的陈旧行（长期不活跃 provider 的残留）。

# 持久化的两类 kind（kind 列取值）
_STATE_KIND_QUOTA = "quota_cooldown"
_STATE_KIND_CB = "circuit_breaker"


def _now_iso() -> str:
    """与 ai_audit_logs.created_at 同一时间口径（localtime ISO8601，秒级）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _snapshot_quota_state() -> dict:
    """当前配额冷却状态的快照（只拷贝 dict，不修改原结构）。"""
    with _quota_lock:
        return {
            "cool_until": dict(_quota_cool_until),
            "last_probe": dict(_quota_last_probe),
        }


async def flush_provider_state_to_db() -> None:
    """把当前 Provider 运行时状态快照写入 ai_provider_state（幂等 UPSERT）。

    写入两类：
      * quota_cooldown：``{pname: {cool_until, last_probe}}``
      * circuit_breaker：``{pname: {state, failures, opened_at,
                          effective_cooldown, last_failure_at}}``

    仅在**非默认**状态下写入（配额：cool_until 剩余 >0；熔断：state!=CLOSED），
    避免把全 0 默认态刷入产生大量噪音行。失败仅告警，不影响主流程；
    顺手清理 updated_at 超过 24h 的陈旧行。
    """
    now_iso = _now_iso()
    state_snapshot = _snapshot_quota_state()
    cb_snapshot = circuit_breaker.snapshot_state()
    writes: list[tuple[str, str, str]] = []
    for pname, payload in state_snapshot.get("cool_until", {}).items():
        until = float(payload or 0.0)
        if until <= 0:
            continue
        last_probe = float(state_snapshot.get("last_probe", {}).get(pname, 0.0))
        writes.append((pname, _STATE_KIND_QUOTA, json.dumps({"cool_until": until, "last_probe": last_probe})))
    for pname, entry in cb_snapshot.items():
        if str(entry.get("state") or "CLOSED") == "CLOSED":
            continue
        writes.append((pname, _STATE_KIND_CB, json.dumps(entry)))
    if not writes:
        return

    async def _snapshot_tx_once() -> None:
        async with write_tx_conn() as conn:
            for pname, kind, payload in writes:
                await conn.execute(
                    "INSERT INTO ai_provider_state(provider_name, kind, payload_json, updated_at) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(provider_name, kind) DO UPDATE SET "
                    "  payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                    (pname, kind, payload, now_iso),
                )
            await conn.execute(
                "DELETE FROM ai_provider_state " "WHERE datetime(updated_at, '+24 hours') < datetime('now','localtime')"
            )

    try:
        # ✅ R108（P1-2 锁族同源）：UPSERT/DELETE 均幂等，整事务接 retry_db_op
        #    ——批量生成写锁窗口内快照曾零重试直通 WARNING（冷却/熔断状态
        #    跨重启恢复链就此断）。耗尽仍失败则维持既有 fail-soft WARNING。
        await retry_db_op(_snapshot_tx_once, max_retries=4, base_delay=0.1, jitter=0.05)
        logger.debug("Provider 状态快照写入 ai_provider_state（%d 行）", len(writes))
    except Exception as e:
        logger.warning("Provider 状态快照写入失败（不影响主流程）: %s", e)


async def warmup_provider_state_from_db() -> None:
    """进程启动时从 ai_provider_state 恢复配额冷却与熔断状态。

    与 warmup_reliability_from_db 分工：
      * warmup_reliability_from_db：从 ai_audit_logs 24h 聚合成功率（**结果型**）；
      * warmup_provider_state_from_db：从 ai_provider_state 恢复当前**状态型**字段
        （冷却时刻、熔断窗口、探测时刻）。

    过滤：
      * quota_cooldown：``cool_until`` 已过 → 跳过（等价于「冷却已自然过期」）；
      * circuit_breaker：``state=CLOSED`` 或 OPEN 且已过期 → 跳过
        （restore_state 也会自动降级为 HALF_OPEN）。

    表缺失 / 数据异常 → 静默降级为旧行为（DEBUG 日志，不影响启动）。
    """
    try:
        conn = await get_conn()
        try:
            cur = await conn.execute("SELECT provider_name, kind, payload_json FROM ai_provider_state")
            rows = await cur.fetchall()
        except Exception as e:
            logger.debug("预热 Provider 状态失败（表缺失或读取失败，忽略）: %s", e)
            return
    except Exception as e:
        logger.debug("预热 Provider 状态失败（连接不可用）: %s", e)
        return

    now = time.time()
    restored_quota = 0
    cb_entries: dict[str, dict] = {}
    for r in rows:
        pname = str(r[0] or "")
        kind = str(r[1] or "")
        payload_text = str(r[2] or "")
        if not pname or not kind or not payload_text:
            continue
        try:
            payload = json.loads(payload_text)
            if not isinstance(payload, dict):
                continue
        except Exception:
            continue

        if kind == _STATE_KIND_QUOTA:
            until = float(payload.get("cool_until") or 0.0)
            last_probe = float(payload.get("last_probe") or 0.0)
            if until <= now:
                continue
            with _quota_lock:
                _quota_cool_until[pname] = until
                if last_probe:
                    _quota_last_probe[pname] = last_probe
            restored_quota += 1
        elif kind == _STATE_KIND_CB:
            state = str(payload.get("state") or "CLOSED")
            if state == "CLOSED":
                continue
            opened_at = float(payload.get("opened_at") or 0.0)
            cooldown = float(payload.get("effective_cooldown") or 0.0)
            if state == "OPEN" and opened_at and (now - opened_at) >= cooldown:
                payload["state"] = "HALF_OPEN"
            cb_entries[pname] = payload

    restored_cb = circuit_breaker.restore_state(cb_entries) if cb_entries else 0

    if restored_quota or restored_cb:
        logger.info(
            "已从 ai_provider_state 预热 Provider 状态（配额冷却 %d 个、熔断 %d 个）", restored_quota, restored_cb
        )


async def _persist_provider_state_loop(stop_event: asyncio.Event, interval: float = 10.0) -> None:
    """周期落库 Provider 状态（默认 10s）。失败仅告警，下一周期重试。

    调用点在 main.py lifespan：
        task = asyncio.create_task(_persist_provider_state_loop(stop))
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            try:
                await flush_provider_state_to_db()
            except Exception as e:
                logger.warning("Provider 状态周期落库失败（下一周期重试）: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Provider 状态周期落库任务异常（下一周期重试）: %s", e)


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


def _ai_live_record_cancelled(provider_name: str, model: str, duration: float):
    """记录一次**被取消**的调用尝试（对冲落选 / 用户停止 / 断连）。

    ✅ 2026-11-04（口径修复）：取消不是 Provider 故障，不能计入 ``failed``；
    单列 ``cancelled``，同时保证 in_flight 递减与最近一次状态更新。
    """
    with _ai_live_lock:
        _ai_live["in_flight"] = max(0, int(_ai_live["in_flight"]) - 1)
        _ai_live["last_provider"] = provider_name
        _ai_live["last_model"] = model
        _ai_live["last_duration"] = duration
        _ai_live["last_at"] = time.time()
        _ai_live["cancelled"] = int(_ai_live.get("cancelled", 0)) + 1
    _ab.notify()


def _ai_live_record_logical(ok: bool = False, cancelled: bool = False) -> None:
    """记录一次**逻辑调用**的最终结果（一次 chat_with_fallback 计一次）。

    与 ``_ai_live_record`` 的尝试口径分开：降级链里 3 个候选各失败一次只让
    ``failed`` +3，而 ``call_failed`` 只 +1（整次业务调用最终失败）。
    """
    with _ai_live_lock:
        _ai_live["calls"] = int(_ai_live.get("calls", 0)) + 1
        if cancelled:
            _ai_live["call_cancelled"] = int(_ai_live.get("call_cancelled", 0)) + 1
        elif not ok:
            _ai_live["call_failed"] = int(_ai_live.get("call_failed", 0)) + 1
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
        # ✅ R175（2026-11-03）：落库前脱敏 —— 上游 4xx 报错常把请求体/Key 片段
        #    原样回显（如 "Incorrect API key provided: sk-..."），审计表长期留存
        #    等价于把密钥写进数据库与导出包。
        from app.services.redaction import redact_sensitive_text

        return redact_sensitive_text(msg)[:300]
    name = type(e).__name__
    if "Timeout" in name or name == "TimeoutError":
        return f"{name}（无错误信息，常见于请求超时）"
    return f"{name}（无错误信息）"


def _status_from_error(e: Exception) -> int:
    """从异常文本推断 HTTP 状态码（429 优先，其次 ``HTTP <code>``，兜底 500）。

    ✅ R19x（2026-11-09）：429 改词边界判定 —— 裸子串 ``"429" in`` 会把
    「context 42900 tokens」「HTTP 42980」误判为限流，错记自适应并发窗口
    并触发配额冷却路径。
    """
    err_str = str(e)
    if _STATUS_429_RE.search(err_str):
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
    base = int(req_timeout if req_timeout is not None else (c.get("timeout") or DEFAULT_CONFIG_NUMBERS["timeout"]))
    if not c.get("_is_primary"):
        cap = int(getattr(settings, "ai_fallback_attempt_timeout", 120) or 120)
        base = min(base, cap)
    return max(1, base)


async def _collect_stream(
    provider,
    messages: list,
    *,
    temperature: float | None,
    json_mode: bool,
    max_tokens: int | None,
    extra_body: dict | None = None,
) -> str:
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
    async for piece in provider.stream(
        messages, temperature=temperature, json_mode=json_mode, max_tokens=max_tokens, extra_body=extra_body
    ):
        if piece:
            parts.append(piece)
    text = "".join(parts)
    if not text.strip():
        # 空流与 chat 路径的空内容同义：必须报错，否则会被当成「成功但内容为空」
        finish = str(getattr(provider, "last_finish_reason", "") or "").lower()
        if finish in ("length", "max_tokens"):
            raise RuntimeError(
                f"流式响应被 max_tokens 截断且正文为空（finish_reason={finish}）："
                f"请调大 max_tokens 或改用非推理模型"
            )
        raise RuntimeError("流式请求未返回任何内容（该平台可能不支持 stream 流式接口，" "或流式参数受限）")
    return text


# ---------- 流式能力记忆（避免每章白跑一次必然失败的流式请求） ----------
# 依据运行日志：部分平台不支持 stream（HTTP 400: stream not supported / 空流），
# 旧实现每次调用都先流式失败一次再回退普通请求 —— 正文生成 N 章就白跑 N 次 HTTP，
# 且每次都要多等一次超时窗口。现按 provider+model 记忆「连续流式失败」：
# 连续 STREAM_FAIL_STREAK 次后，在 STREAM_CAPABILITY_TTL 秒内直接走普通请求
# （流式只改变与厂商之间的传输方式，应用侧语义完全不变）；流式一旦成功立即
# 清除记忆；TTL 到期后自动重新探测一次（平台修复/换模型可自愈）。
STREAM_FAIL_STREAK = 2  # 连续失败几次才认定不支持（单次要能容忍偶发抖动）
# ✅ 2026-09-22（调用次数优化 O5）：TTL 配置化并延长。3600s → 86400s（1 天）：
#    模型对流式的支持不会一天内变化，缩短只会增加「TTL 到期后每章重探测一次
#    必败流式」的浪费；到期自动重探测与成功即清除的机制保持不变（可自愈）。
STREAM_CAPABILITY_TTL = max(1.0, float(getattr(settings, "ai_stream_capability_ttl_seconds", 86400) or 86400.0))

# ✅ R4 修复（2026-09-22）：流式请求命中确定性错误时是否回退普通请求。
# 默认 True = 不回退（确定性错误对同一 provider 非流式同样 100% 失败，重试白烧 HTTP）。
# False = 旧行为（一律回退普通请求重试），可通过环境变量关闭以恢复旧行为。
ai_retry_on_non_retryable = bool(getattr(settings, "ai_retry_on_non_retryable", True))

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


async def _call_provider(
    provider,
    messages: list,
    *,
    request_mode: str,
    temperature: float | None,
    json_mode: bool,
    max_tokens: int | None,
    extra_body: dict | None = None,
) -> str:
    """按配置的「请求方式」向厂商发起一次调用，返回完整正文。

    - ``normal``：普通请求（等待完整响应），与历史行为一致；
    - ``stream``：流式请求，边收边拼，收完才返回。

    ✅ 增强：流式失败（平台不支持 stream / 流式参数受限 / 空流 / 流式专属
      限流）时**自动用普通请求重试一次**，与「测试连接」的探测口径保持一致 ——
      否则用户为了更快首字节勾了流式，反而整条链路直接失败，
      而同样的配置用普通请求其实完全可用（请求方式不该成为可用性开关）。
    """
    # ✅ R179（2026-11-05 · 出站脱敏兜底从未接线）：prompt_material_guard 早已
    #    提供 ``redact_outbound_messages()``（注释声称它是「覆盖全部 AI 链路的
    #    出站单点收口」），但全仓**零调用方** —— 用户开启 PROMPT_REDACT_SENSITIVE
    #    后，只有经过 govern_text/govern_blob 的资料入口会脱敏，其它链路（直接
    #    拼 f-string、把资料塞进 system 消息等）仍会把疑似凭据原样发往外部 AI。
    #    此处是全部 provider 调用的唯一出口（normal/stream 两分支共用），作为
    #    兜底接线：只替换 user 消息中的高置信度凭据形态，不触碰库内原文。
    #    开关关闭时 ``redact_outbound_messages`` 直接返回原列表，零行为变化。
    try:
        from app.services.prompt_material_guard import redact_outbound_messages

        messages = redact_outbound_messages(messages)
    except Exception:  # noqa: BLE001 - 脱敏失败绝不阻断调用
        logger.debug("出站消息脱敏失败（使用原文继续）", exc_info=True)
    # ✅ R202（2026-11-09 · G1 · 检测与围栏解耦）：注入手法**检测**默认常开。
    #    旧实现把注入防护整体挂在 ``prompt_injection_defense``（默认 False）
    #    之下 → 默认部署下既无围栏、也无任何检测，运维完全看不到「用户上传
    #    的招标文件里带了越狱指令」。本函数是全部 provider 调用的唯一出口，
    #    检测在这里收口即可覆盖所有链路（含直接拼 f-string 的链路）。
    #    检测**只读**：不改任何提示词字节、不阻断生成（误报成本高），
    #    只产出 WARNING + 指标；围栏仍受开关控制（改字节，须显式开启）。
    #    ⚠️ 覆盖边界（全仓仅此两处直连 provider.chat，均**无用户文本**入模，
    #    故不在文本检测面内）：``routers/ai_config/connectivity._probe_chat``
    #    （固定串「回复ok」）与 ``services/ocr.ocr_bytes_vision``
    #    （图片 base64 + 固定模板提示词）。
    try:
        from app.services.prompt_material_guard import scan_outbound_messages

        messages = scan_outbound_messages(messages)
    except Exception:  # noqa: BLE001 - 检测失败绝不阻断调用
        logger.debug("出站消息注入检测失败（使用原文继续）", exc_info=True)
    if request_mode != "stream":
        return await provider.chat(
            messages, temperature=temperature, json_mode=json_mode, max_tokens=max_tokens, extra_body=extra_body
        )
    _cap_key = _stream_capability_key(provider)
    if _stream_disabled(_cap_key):
        # ✅ 能力记忆命中：省掉一次必然失败的流式 HTTP（正文生成按章累积，收益显著）
        logger.info(
            "Provider %s 近期流式不可用（已记忆 %ds 内不再尝试），本次直接走普通请求",
            getattr(provider, "name", "?"),
            int(STREAM_CAPABILITY_TTL),
        )
        return await provider.chat(
            messages, temperature=temperature, json_mode=json_mode, max_tokens=max_tokens, extra_body=extra_body
        )
    try:
        text = await _collect_stream(
            provider,
            messages,
            temperature=temperature,
            json_mode=json_mode,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
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
                getattr(provider, "name", "?"),
                _error_summary(stream_err)[:160],
            )
            raise
        if _note_stream_failure(_cap_key):
            logger.warning(
                "Provider %s 连续 %d 次流式请求失败，后续 %ds 内直接走普通请求",
                getattr(provider, "name", "?"),
                STREAM_FAIL_STREAK,
                int(STREAM_CAPABILITY_TTL),
            )
        logger.warning(
            "流式请求失败，自动回退普通请求重试（provider=%s）：%s",
            getattr(provider, "name", "?"),
            _error_summary(stream_err)[:160],
        )
        return await provider.chat(
            messages, temperature=temperature, json_mode=json_mode, max_tokens=max_tokens, extra_body=extra_body
        )


async def _attempt_candidate(
    c: dict,
    messages: list,
    *,
    temperature: float | None,
    json_mode: bool,
    max_tokens: int | None,
    req_timeout: int | None,
    scene: str = "",
    extra_body: dict | None = None,
    audit_scope: _LogicalAuditScope | None = None,
) -> tuple[str, Exception | None]:
    """单候选调用（JSON 模式不被支持时同候选回退普通模式重试一次）。

    成功返回 ``(正文, None)``；失败返回 ``("", 异常)``。
    本函数负责该次尝试的全部记账：实时状态、并发控制器采样、
    熔断器成败、审计日志。被取消时登记实时状态但不计入熔断失败。

    ✅ 新增：按候选自身的 ``request_mode`` 选择普通请求 / 流式请求
      （``_call_provider``），流式只影响厂商间传输方式，不影响上层语义。
    """
    pname = c.get("provider_name", "?")
    base_mt = max_tokens or c.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"])
    # ✅ 2026-11-05（推理预算）：已知/疑似推理模型先给足输出预算，避免调用方
    #    传入的小预算（正文生成 MAX_TOKENS_FLOOR=4000，见 content_utils）被
    #    思考过程整段吃掉。两者取较大者（4000 vs 4096），互不覆盖。
    _reasoning_floor = max(0, int(getattr(settings, "ai_reasoning_output_floor", 4096) or 0))
    if _reasoning_floor > 0 and _is_reasoning_candidate(c):
        base_mt = max(int(base_mt), _reasoning_floor)

    def _build(mt: int):
        """按本次尝试的 max_tokens 构建 Provider（O3 思考吞噬重试需要翻倍）。"""
        return _build_provider(
            pname,
            c["api_key"],
            c["base_url"],
            c["model"],
            max_tokens=mt,
            temperature=(
                temperature if temperature is not None else c.get("temperature", DEFAULT_CONFIG_NUMBERS["temperature"])
            ),
            timeout=_candidate_timeout(c, req_timeout),
        )

    last_err: Exception | None = None
    request_mode = normalize_request_mode(c.get("request_mode"))
    # ✅ 2026-09-22（调用次数优化 O3）：思考吞噬重试的开关与上限
    _retry_thinking = bool(getattr(settings, "ai_retry_on_thinking_exhausted", True))
    _reasoning_cap = max(0, int(getattr(settings, "ai_reasoning_max_tokens", 16384) or 0))
    _reasoning_retry_floor = max(0, int(getattr(settings, "ai_reasoning_retry_floor_tokens", 8192) or 0))
    # ✅ 2026-10-07：非空正文被 max_tokens 截断时的重试（与 O3 同构，严格一次）
    _retry_partial = bool(getattr(settings, "ai_retry_on_partial_truncation", True))
    _best_partial: str | None = None
    # JSON 模式优先：支持则用，被厂商拒绝则同 Provider 立即回退普通模式
    for use_json in [True, False] if json_mode else [False]:
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
                        provider,
                        messages,
                        request_mode=request_mode,
                        temperature=temperature,
                        json_mode=use_json,
                        max_tokens=mt,
                        extra_body=extra_body,
                    )
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
                    _note_quota_success(pname)  # ✅ O2：成功即清除配额冷却
                    clear_thinking_dead(pname)  # ✅ R110（P1-2）：成功即清除思考吞噬冷却
                    # ✅ 2026-11-05：记录成功耗时样本（自适应对冲阈值的唯一数据源）
                    _note_provider_latency(
                        _reliability_key(pname, c.get("model", ""), c.get("config_id", ""), c.get("base_url", "")),
                        duration,
                    )
                    # ✅ 修复：成功路径补齐 token 用量（原实现不传，用量统计恒为 0）
                    u = extract_usage(provider)
                    # ✅ 2026-11-05（方案 B）：尝试级记录只进 scope，
                    #    由 _chat_with_fallback_result 合并成一条主记录。
                    await _record_attempt(
                        audit_scope,
                        provider_name=pname,
                        model=c.get("model", ""),
                        config_id=c.get("config_id", ""),
                        base_url=c.get("base_url", ""),
                        duration=duration,
                        success=True,
                        prompt_tokens=u["prompt_tokens"],
                        completion_tokens=u["completion_tokens"],
                        cached_tokens=u["cached_tokens"],
                    )
                    # ✅ 2026-10-07（生产库实证）：非空正文 + finish_reason=length/max_tokens
                    #    = 被 max_tokens 截断的半截正文。旧实现直接返回且不留痕 ——
                    #    生产方案 5 个章节句尾停在半句，用户无从得知，审核/导出照常用。
                    #    现：先记 WARNING 让截断可观测；开关开启时按 ai_reasoning_max_tokens
                    #    上限翻倍 max_tokens 重试一次（严格一次），仍截断则返回当前结果。
                    _finish = str(getattr(provider, "last_finish_reason", "") or "").lower()
                    if _finish in ("length", "max_tokens"):
                        logger.warning(
                            "Provider %s 正文疑似被 max_tokens 截断" "（finish_reason=%s，%d 字）",
                            pname,
                            _finish,
                            len(result or ""),
                        )
                        if _retry_partial:
                            _retry_partial = False  # 严格重试一次，避免连锁翻倍
                            _best_partial = result  # 重试后失败时用它兜底
                            _nxt = _reasoning_retry_tokens(mt, _reasoning_cap, _reasoning_retry_floor)
                            if _nxt > mt:
                                logger.warning(
                                    "Provider %s 正文被截断，max_tokens 由 %d 放大至 %d 重试一次", pname, mt, _nxt
                                )
                                mt = _nxt
                                continue
                    # ✅ R176 B2（2026-11-04）：把本次 provider 的「流式解析跳过」
                    #    计数带出到候选 dict，供 chat_with_fallback_meta 上报
                    #    （非流式/本地分片路径恒为 0，与引入前行为一致）。
                    c["_stream_parse_skipped"] = int(getattr(provider, "last_stream_parse_skipped", 0) or 0)
                    c["_stream_parse_skipped_reason"] = str(
                        getattr(provider, "last_stream_parse_skipped_reason", "") or ""
                    )
                    return result, None
                except asyncio.CancelledError:
                    # ✅ 对冲/停止被取消：只登记实时状态（保证 in_flight 不泄漏），
                    #    不计入熔断失败（取消不是 provider 故障），异常向上传播。
                    # ✅ 2026-11-04（口径修复）：取消单列 cancelled，不再计入 failed。
                    _ai_live_record_cancelled(pname, c.get("model", ""), time.time() - t0)
                    # ✅ 2026-11-05（方案 B）：对冲落败/任务停止的取消也算一次
                    #    真实尝试（已发出上游 POST，会消耗额度），进 scope 但不
                    #    更新可靠性（取消不是供应商失败）。scope 路径无 await，
                    #    在被取消的协程里记录是安全的。
                    if audit_scope is not None:
                        audit_scope.record(
                            provider_name=pname,
                            model=c.get("model", ""),
                            config_id=c.get("config_id", ""),
                            base_url=c.get("base_url", ""),
                            duration=time.time() - t0,
                            success=False,
                            error="调用被取消（对冲落败/任务停止）",
                            error_class="cancelled",
                        )
                    raise
                except Exception as e:
                    duration = time.time() - t0
                    status = _status_from_error(e)
                    _ai_live_record(pname, c.get("model", ""), False, duration)
                    concurrency_controller.record(duration, status=status)
                    concurrency_controller.adjust_concurrency()
                    last_err = e
                    # ✅ 2026-09-17：失败原因摘要落审计（/ai/stats 失败原因分布的数据源）
                    # ✅ 2026-11-05（方案 B）：不再逐次落「失败行」，合并进逻辑调用主记录。
                    await _record_attempt(
                        audit_scope,
                        provider_name=pname,
                        model=c.get("model", ""),
                        config_id=c.get("config_id", ""),
                        base_url=c.get("base_url", ""),
                        duration=duration,
                        success=False,
                        error=_error_summary(e),
                    )
                    if use_json and _json_mode_unsupported(str(e)):
                        # 参数不支持 ≠ Provider 故障：不计入熔断，回退普通模式重试
                        # ✅ R183：异常文本可能含上游回显的 Key；日志统一走脱敏摘要
                        logger.warning("Provider %s 不支持 JSON 模式，回退普通模式重试: %s", pname, _error_summary(e))
                        circuit_breaker.record_success(pname)
                        break
                    # ✅ 2026-09-22（调用次数优化 O3）：推理模型把 max_tokens 全部
                    #    耗在思考过程（finish_reason=length、正文为空）时，翻倍
                    #    max_tokens 重试一次 —— 旧实现直接判失败进入降级链，
                    #    每个推理模型候选各白烧一次（运行库实测 101 次）。
                    if _retry_thinking and _is_thinking_exhausted(e):
                        _note_thinking_suspect(pname)  # 标记疑似推理模型，后续预扩预算
                        nxt = _reasoning_retry_tokens(mt, _reasoning_cap, _reasoning_retry_floor)
                        if nxt > mt:
                            logger.warning(
                                "Provider %s 思考吞噬 max_tokens=%d（正文为空），" "放大至 %d 重试一次", pname, mt, nxt
                            )
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
                    # ✅ R110（P1-2 · 思考吞噬翻倍失败标记冷却）：
                    #    1) 已重试一次（_retry_thinking 已 False）后仍
                    #       _is_thinking_exhausted → 翻倍失败；
                    #    2) 翻倍前已达上限（nxt <= mt，无法 continue）→
                    #       无法重试的思考吞噬。
                    #    两种情况都标记该 provider 短时冷却，降级链后续候选
                    #    在 _gate_candidates 跳过（与配额冷却同口径）。
                    if _is_thinking_exhausted(e):
                        _note_thinking_suspect(pname)
                        _note_thinking_dead(pname)
                    # ✅ 2026-09-22（调用次数优化 O2）：配额类错误记入冷却窗口，
                    #    窗口内该 provider 的后续调用直接跳过（不再打配额）。
                    if QUOTA_FAIL_COOLDOWN > 0 and _is_quota_error(e):
                        _note_quota_failure(pname, e)
                    # ✅ 2026-11-05（429 处理）：限流不参与熔断冷却的指数放大；
                    #    冷却时长由 ``Retry-After`` / 配额冷却负责。旧实现
                    #    ``is_429=True`` 每次把熔断冷却翻倍，放大限流影响。
                    circuit_breaker.record_failure(pname)
                    # ✅ R204（2026-11-09 · 日志脱敏口径统一）：本行此前打印**原始
                    #   异常**（``%s", e``），而同文件其余分支一律走 ``_error_summary``
                    #   （内部再做 redact_sensitive_text）—— 非 openai_compatible 厂商的
                    #   异常文本（如 httpx 异常里含带 userinfo 的 base_url）会**明文落盘**。
                    logger.error("Provider %s 调用失败: %s", pname, _error_summary(e))
                    if _best_partial:
                        # 截断重试后失败：返回已拿到的半截正文，而不是丢弃它再降级到
                        # 下一候选（本候选已产出可用内容：半截 > 拿不到）。
                        logger.warning("Provider %s 截断重试后失败，返回已获取的部分正文: %s", pname, _error_summary(e))
                        c["_stream_parse_skipped"] = int(getattr(provider, "last_stream_parse_skipped", 0) or 0)
                        c["_stream_parse_skipped_reason"] = str(
                            getattr(provider, "last_stream_parse_skipped_reason", "") or ""
                        )
                        return _best_partial, None
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
        candidates.append(
            {
                "config_id": cfg.get("id", ""),
                "provider_name": cfg.get("provider_name", "openai"),
                "api_key": _pk,
                "base_url": cfg.get("base_url", ""),
                "model": cfg.get("model", ""),
                "max_tokens": cfg.get("max_tokens", DEFAULT_CONFIG_NUMBERS["max_tokens"]),
                "temperature": cfg.get("temperature", DEFAULT_CONFIG_NUMBERS["temperature"]),
                "timeout": cfg.get("timeout", DEFAULT_CONFIG_NUMBERS["timeout"]),
                "request_mode": normalize_request_mode(cfg.get("request_mode")),
                "key_broken": bool(_enc) and not _pk,
                "_is_primary": True,
            }
        )
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


async def _gate_candidates(candidates: list[dict], scene: str) -> tuple[list[dict], Exception | None]:
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
    skipped_thinking = 0
    _disabled = await resolve_disabled_providers()
    last_err: Exception | None = None

    # ✅ R110（P1-1 · 跨章全熔断快速失败）：连续 N 次所有候选不可用后，
    #    短时快速失败不再 probe（R109 实证 5 章各自 probe 空转 150-300s）。
    if _is_all_circuit_fast_fail():
        # ✅ 2026-11-04（可观测性）：此路径不发请求、不写审计行，旧实现
        #    在数据库里完全不可见（生产实证 3 分钟内 316 次拒绝零记录）。
        #    仅进程内计数，随 /system/activity 暴露；不写审计以免污染成功率分母。
        with _ai_live_lock:
            _ai_live["fast_fail"] = int(_ai_live.get("fast_fail", 0)) + 1
        _ab.notify()
        raise RuntimeError(
            "全熔断快速失败：连续多次所有 Provider 不可用，" f"等待 {int(ALL_CIRCUIT_FAST_FAIL_COOLDOWN)}s 恢复后重试"
        )

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
            await _log_audit(
                pname,
                c.get("model", ""),
                "provider_disabled",
                0.0,
                False,
                error="运行时开关已禁用该厂商",
                scene=scene,
                config_id=c.get("config_id", ""),
                base_url=c.get("base_url", ""),
                error_class="circuit_skipped",
                attempt_count=0,
            )
            if last_err is None:
                last_err = RuntimeError(f"Provider {pname} 已被运行时开关禁用")
            continue
        if _is_thinking_dead(pname):
            # ✅ R110（P1-2）：思考吞噬翻倍失败的 provider 短时冷却，
            #    与配额冷却同走 circuit_skipped 审计口径。
            skipped_thinking += 1
            logger.info("思考吞噬冷却中，跳过 %s（冷却 %ds）", pname, int(THINKING_DEAD_COOLDOWN))
            await _log_audit(
                pname,
                c.get("model", ""),
                "circuit_skipped",
                0.0,
                False,
                error="思考吞噬冷却，跳过本次调用",
                scene=scene,
                config_id=c.get("config_id", ""),
                base_url=c.get("base_url", ""),
                error_class="circuit_skipped",
                attempt_count=0,
            )
            if last_err is None:
                last_err = RuntimeError(f"Provider {pname} 处于思考吞噬冷却中")
            continue
        if QUOTA_FAIL_COOLDOWN > 0 and _quota_cooldown_active(pname):
            skipped_cb += 1
            cb_skipped.append(c)
            # ✅ R103（2026-10-18 · 可观测性降噪）：配额冷却是**设计内的节流**
            #    （冷却窗口内少打请求、每 QUOTA_PROBE_EVERY 秒放行一次探测），
            #    不是异常。生产实证（R102 日志基线）：此类跳过单窗口 422 条
            #    WARNING，占全部 WARNING 的 50%，把真实问题淹没了。按 R11
            #    「正常控制流不得以 WARNING 刷屏」先例降为 INFO（审计行
            #    circuit_skipped 不变，数据链完整可查）。
            logger.info("配额冷却中，跳过 %s（冷却窗口 %ds）", pname, int(QUOTA_FAIL_COOLDOWN))
            await _log_audit(
                pname,
                c.get("model", ""),
                "circuit_skipped",
                0.0,
                False,
                error="配额冷却，跳过本次调用",
                scene=scene,
                config_id=c.get("config_id", ""),
                base_url=c.get("base_url", ""),
                error_class="circuit_skipped",
                attempt_count=0,
            )
            if last_err is None:
                last_err = RuntimeError(f"Provider {pname} 处于配额冷却中")
            continue
        if not circuit_breaker.allow_request(pname):
            skipped_cb += 1
            cb_skipped.append(c)
            # ✅ R103：同上——熔断 OPEN 是自愈机制的设计内状态，非异常。
            logger.info("熔断器 OPEN，跳过 %s", pname)
            await _log_audit(
                pname,
                c.get("model", ""),
                "circuit_skipped",
                0.0,
                False,
                error="熔断器 OPEN，跳过本次调用",
                scene=scene,
                config_id=c.get("config_id", ""),
                base_url=c.get("base_url", ""),
                error_class="circuit_skipped",
                attempt_count=0,
            )
            if last_err is None:
                last_err = RuntimeError(f"Provider {pname} 被熔断器跳过")
            continue
        usable.append(c)

    if usable:
        # ✅ R110（P1-1）：gate 放行非空候选 → 重置全熔断计数
        _record_all_circuit_outcome(success=True)
        return usable, last_err

    # ✅ R110（P1-1）：usable 为空 = 所有候选被门禁跳过 → 计入全熔断计数
    # ✅ R177（D-2 · P1 · 2026-11-05 · 生产日志实证）：「所有 Provider 不可用」
    #    只认**厂商侧**不可用（熔断 OPEN / 配额冷却 / 思考吞噬冷却）。
    #    此前本行在**所有 raise 分支之前**无条件执行 → 「密钥解不开 / 没填 Key /
    #    被运行时开关禁用」这类**本地配置问题**同样计入全局闸门：
    #    生产实证（logs/backend.log 2026-11-01 前后）连续 2 次「候选 storm 已保存的
    #    密钥无法解密」即激活 60s 全局快速失败，该窗口内**全站所有模块**
    #    （正文生成 / bid_analysis / content_plan / 事实提取）全部中止，
    #    与厂商可用性毫无关系 —— 一个本地密钥问题被放大成全站停摆。
    #    现按跳过原因分类：只有厂商侧原因才计入全局闸门，本地配置问题照常
    #    走各自的 raise 分支（文案与语义完全不变）。
    if skipped_cb > 0 or skipped_thinking > 0:
        _record_all_circuit_outcome(success=False)

    if skipped_disabled > 0 and skipped_cb == 0 and skipped_no_key == 0 and skipped_key_broken == 0:
        raise RuntimeError(
            "所有候选 Provider 均被运行时开关禁用（disabled_providers="
            f"{sorted(_disabled)}），请在「文本模型配置 → 运行时厂商开关」恢复后再试"
        )
    if skipped_cb > 0 and cb_skipped:
        probeable = [
            c
            for c in cb_skipped
            if not (QUOTA_FAIL_COOLDOWN > 0 and _quota_cooldown_active(c.get("provider_name", "")))
        ]
        if not probeable:
            raise RuntimeError(
                "所有候选 Provider 均处于熔断/配额冷却中"
                f"（探测节流 {int(QUOTA_PROBE_EVERY)}s），请稍后重试 "
                f"(skipped_no_key={skipped_no_key}, skipped_cb={skipped_cb})"
            )
        probe = min(probeable, key=lambda c: circuit_breaker.cooldown_remaining(c.get("provider_name", "")))
        _pname = probe.get("provider_name", "")
        _remain = circuit_breaker.cooldown_remaining(_pname)
        circuit_breaker.force_probe(_pname)
        usable.append(probe)
        logger.warning("全部候选均处于熔断冷却中，放行 %s 做一次探测（原剩余冷却 %.0fs）", _pname, _remain)
        return usable, last_err
    if skipped_cb > 0:
        raise RuntimeError(
            "所有候选 Provider 均被熔断器跳过，请等待冷却重试 "
            f"(skipped_no_key={skipped_no_key}, skipped_cb={skipped_cb})"
        )
    if skipped_key_broken and not skipped_no_key:
        # ✅ 2026-10-06（B-2 · 归因准确）：此前「全部候选均被禁用」的判定
        #    只看 skipped_no_key，**漏掉 skipped_key_broken** —— 当候选里同时
        #    有一条「密文解不开」和一条「被禁用」时，错误文案归因成
        #    「都被禁用了，请去恢复开关」，用户去恢复开关后问题依旧（真因是密钥）。
        #    现把 key_broken 纳入判据，并在两类原因并存时同时如实报出。
        _extra = f"（另有 {skipped_disabled} 条候选被运行时开关禁用）" if skipped_disabled else ""
        raise RuntimeError(
            "候选 Provider 已保存的 API Key 无法解密（常见于更换过加密密钥 "
            "FERNET_KEY 或删除过 data/secret_key.key），"
            f"请在「文本模型配置」重新填写并保存 Key{_extra}"
        )
    if skipped_no_key > 0 or skipped_key_broken > 0:
        raise RuntimeError(
            "没有配置任何有 API Key 的 Provider"
            + (
                f"（其中 {skipped_key_broken} 条已保存的密钥无法解密，" "请重新填写或删除该配置）"
                if skipped_key_broken
                else ""
            )
        )
    raise RuntimeError(f"所有 AI 提供商调用失败：{last_err}")


def _classify_fallback_reason(err: Exception | None) -> str:
    """把降级触发原因归一为稳定的机器码（供报表/前端聚合）。

    ✅ R176 B2（2026-11-04）：失败原文含随机 request-id/URL，按文本聚合无法
    得到稳定分布；这里在落库前归一到 4 类，口径与审计 error 文本解耦。
      - timeout       请求超时（含 ``HTTP 408``）
      - rate_limited  限流/配额（429 / quota / rate limit）
      - parse_failed  响应为空/被截断/JSON 解析失败
      - provider_error 其它上游错误（5xx、鉴权、网络等）
    无错误（主候选一次成功）时返回空串。
    """
    if err is None:
        return ""
    msg = (str(err) or "").lower()
    if not msg:
        return "provider_error"
    if "408" in msg or "timeout" in msg or "timed out" in msg or "超时" in msg:
        return "timeout"
    if (
        "429" in msg
        or "rate limit" in msg
        or "too many requests" in msg
        or "quota" in msg
        or "配额" in msg
        or "限流" in msg
    ):
        return "rate_limited"
    if "json" in msg or "parse" in msg or "解析" in msg or "空正文" in msg or "截断" in msg or "empty" in msg:
        return "parse_failed"
    return "provider_error"


async def _chat_with_fallback_inner(
    messages: list,
    ai_config: dict | None = None,
    *,
    temperature: float | None = None,
    json_mode: bool = False,
    timeout: int | None = None,
    max_tokens: int | None = None,
    scene: str = "",
    reasoning_effort: str = "",
    audit_scope: _LogicalAuditScope | None = None,
) -> dict:
    """带熔断降级 + **对冲请求**的对话调用（返回正文 + 降级元信息）。

    ✅ R176 B2（2026-11-04）：原 ``chat_with_fallback`` 只回正文，调用方无法
    知道「本次是否走了降级、为什么降级、流式解析是否跳过」。现把编排结果
    统一收敛为 dict，正文入口保持 ``chat_with_fallback`` 薄包装（行为不变）。
    返回键：
      · text                    正文（与旧返回值逐字节一致）
      · provider_used           实际胜出的 provider_name
      · fallback_reason         降级原因机器码（见 _classify_fallback_reason）
      · stream_parse_skipped    流式分片解析失败被跳过的行数
      · stream_parse_skipped_reason 跳过原因摘要

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
    # ✅ 2026-11-05（对冲放大修复）：
    #   ① 同一逻辑调用最多 _hedge_max_copies 个对冲副本（默认 1），杜绝
    #      "候选链 3 个 → 最多 2 个副本" 的重复请求放大；
    #   ② 阈值改为按候选最近成功调用 P90 自适应（至少 P50×1.5），并夹在
    #      [min, max] 内；样本不足时用 ai_hedge_delay_seconds（默认 45s）。
    _hedge_raw = float(getattr(settings, "ai_hedge_delay_seconds", 45.0) or 0.0)
    hedge_fixed_delay = _hedge_raw if _hedge_raw > 0 else 0.0
    _hedge_adaptive = bool(getattr(settings, "ai_hedge_adaptive_enabled", True))
    _hedge_pct = float(getattr(settings, "ai_hedge_percentile", 90.0) or 90.0)
    _hedge_p50_ratio = max(0.0, float(getattr(settings, "ai_hedge_p50_ratio", 1.5) or 0.0))
    _hedge_min_delay = max(0.0, float(getattr(settings, "ai_hedge_min_delay_seconds", 30.0) or 0.0))
    _hedge_max_delay = max(
        _hedge_min_delay,
        float(getattr(settings, "ai_hedge_max_delay_seconds", 90.0) or 0.0),
    )
    _hedge_min_samples = max(1, int(getattr(settings, "ai_hedge_min_samples", 12) or 1))
    _hedge_max_copies = max(0, int(getattr(settings, "ai_hedge_max_copies", 1) or 0))
    hedge_enabled = bool(getattr(settings, "ai_hedge_enabled", True)) and len(usable) > 1 and _hedge_max_copies > 0
    hedges_launched = 0

    def _hedge_delay_for(c: dict) -> float:
        """某个在跑候选的自适应对冲阈值（秒）。"""
        return _adaptive_hedge_delay(
            _reliability_key(
                c.get("provider_name", ""),
                c.get("model", ""),
                c.get("config_id", ""),
                c.get("base_url", ""),
            ),
            fixed_delay=hedge_fixed_delay,
            adaptive=_hedge_adaptive,
            pct=_hedge_pct,
            p50_ratio=_hedge_p50_ratio,
            min_delay=_hedge_min_delay,
            max_delay=_hedge_max_delay,
            min_samples=_hedge_min_samples,
        )

    def _hedge_candidate_blocked(c: dict) -> bool:
        """对冲前复检：命中配额冷却/熔断/思考吞噬冷却的候选不得被对冲拉起。"""
        pname = c.get("provider_name", "")
        if _is_thinking_dead(pname):
            return True
        if QUOTA_FAIL_COOLDOWN > 0 and _quota_cooldown_active(pname):
            return True
        return not circuit_breaker.allow_request(pname)

    def _promote_next_candidate() -> bool:
        """前进到下一个未被冷却/熔断的候选；全部不可用返回 False。"""
        nonlocal next_idx
        while next_idx < len(usable):
            cand = usable[next_idx]
            if _hedge_candidate_blocked(cand):
                logger.info(
                    "候选 %s 处于配额冷却/熔断，跳过（不发起对冲/补位）",
                    cand.get("provider_name", "?"),
                )
                next_idx += 1
                continue
            return True
        return False

    # ✅ 2026-09-22（对齐 OpenBidKit reasoning_effort 能力）：推理类模型「思考力度」透传。
    #    空串（默认）= 不发送该参数，完全向后兼容；非空前作为 extra_body["reasoning_effort"]
    #    随每次 AI 调用下发（OpenAI 兼容协议），由 provider 合并进请求体。
    _eff = (reasoning_effort or getattr(settings, "ai_reasoning_effort", "") or "").strip()
    extra_body = {"reasoning_effort": _eff} if _eff else None

    def _launch() -> None:
        nonlocal next_idx
        c = usable[next_idx]
        next_idx += 1
        # 记录启动时刻：对冲窗口按「该候选已运行时长」计算，避免被其他
        # 任务的完成事件重置计时（旧实现每轮 asyncio.wait 都重新计 20s）。
        c["_launched_at"] = time.monotonic()
        task = asyncio.create_task(
            _attempt_candidate(
                c,
                messages,
                temperature=temperature,
                json_mode=json_mode,
                max_tokens=max_tokens,
                req_timeout=timeout,
                scene=scene,
                extra_body=extra_body,
                audit_scope=audit_scope,
            )
        )
        pending[task] = c

    try:
        _launch()
        while pending:
            # ✅ 2026-11-05：仅当「仍有对冲名额 + 还有候选」时才设置等待窗口；
            #    已用满对冲名额 / 剩余候选全部处于冷却或熔断 → 等到有结果为止。
            wait_timeout = None
            if hedge_enabled and hedges_launched < _hedge_max_copies and next_idx < len(usable):
                if _promote_next_candidate():
                    oldest = min(pending.values(), key=lambda x: float(x.get("_launched_at", 0.0)))
                    delay = _hedge_delay_for(oldest)
                    elapsed = time.monotonic() - float(oldest.get("_launched_at", 0.0))
                    wait_timeout = max(0.0, delay - elapsed)
            done, _ = await asyncio.wait(set(pending), timeout=wait_timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                # 窗口到期：复检冷却/熔断后再拉起「唯一一个」对冲副本。
                if not _promote_next_candidate():
                    continue
                target = usable[next_idx]
                oldest = min(pending.values(), key=lambda x: float(x.get("_launched_at", 0.0)))
                logger.info(
                    "对冲请求：%s 已运行超 %.0fs 无响应，并行启动候选 %s（本逻辑调用剩余对冲名额 %d）",
                    ",".join(c.get("provider_name", "?") for c in pending.values()),
                    _hedge_delay_for(oldest),
                    target.get("provider_name", "?"),
                    _hedge_max_copies - hedges_launched,
                )
                _launch()
                hedges_launched += 1
                continue

            winner: str | None = None
            winner_c: dict | None = None
            for task in done:
                c = pending.pop(task, None)
                if c is None:
                    continue
                try:
                    result, err = task.result()
                except asyncio.CancelledError:
                    result, err = "", RuntimeError(f"候选 {c.get('provider_name', '?')} 被取消")
                except Exception as e:  # pragma: no cover - 防御：任务内异常已自处理
                    result, err = "", e
                if err is None and result:
                    winner = result
                    winner_c = c
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
                # ✅ R110（P1-1）：调用成功 → 重置全熔断快速失败计数
                _record_all_circuit_outcome(success=True)
                _wc = winner_c or {}
                return {
                    "text": winner,
                    "provider_used": _wc.get("provider_name", ""),
                    "fallback_reason": _classify_fallback_reason(errors[-1] if errors else None),
                    "stream_parse_skipped": int(_wc.get("_stream_parse_skipped", 0) or 0),
                    "stream_parse_skipped_reason": str(_wc.get("_stream_parse_skipped_reason", "") or ""),
                }

            # 有空位且仍有候选 → 立即补位（失败快速返回时无需等对冲延迟）；
            # ✅ 2026-11-05：补位同样跳过处于配额冷却/熔断的候选，避免每个
            #    分段都去撞一次已限流的 provider。
            if not pending and next_idx < len(usable) and _promote_next_candidate():
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


async def _chat_with_fallback_result(
    messages: list,
    ai_config: dict | None = None,
    *,
    temperature: float | None = None,
    json_mode: bool = False,
    timeout: int | None = None,
    max_tokens: int | None = None,
    scene: str = "",
    reasoning_effort: str = "",
) -> dict:
    """逻辑调用外壳：一次调用（含重试/降级/对冲）只记一次业务成败。

    ✅ 2026-11-05（统计口径修复）：``_ai_live`` 里的 ``total/failed`` 是**尝试**
    口径（降级链每个候选各计一次）；本外壳额外维护 ``calls/call_failed``
    **逻辑调用**口径，面板据此同时展示「业务调用 N 次 / 失败 M 次」与
    「尝试 A 次 / 尝试失败 B 次」，避免把一次业务失败误读成多次失败。

    ✅ 2026-11-05（方案 B · 审计合并）：一次逻辑调用只落**一条**审计主记录，
    逐次尝试压缩进 attempts_json；``attempt_count`` 与上面的尝试口径一致。
    取消（任务停止）不写失败行——取消不是业务失败，仍在实时面板 cancelled
    单列；对冲落败副本的取消会进入胜者主记录的 attempts_json。
    """
    audit_scope = _LogicalAuditScope(scene)
    try:
        result = await _chat_with_fallback_inner(
            messages,
            ai_config,
            temperature=temperature,
            json_mode=json_mode,
            timeout=timeout,
            max_tokens=max_tokens,
            scene=scene,
            reasoning_effort=reasoning_effort,
            audit_scope=audit_scope,
        )
    except asyncio.CancelledError:
        _ai_live_record_logical(cancelled=True)
        raise
    except Exception as e:
        await _flush_logical_audit(
            audit_scope,
            success=False,
            error=_error_summary(e),
            error_class=_error_class(e),
        )
        _ai_live_record_logical(ok=False)
        raise
    else:
        await _flush_logical_audit(audit_scope, success=True)
        _ai_live_record_logical(ok=True)
        return result


async def chat_with_fallback(
    messages: list,
    ai_config: dict | None = None,
    *,
    temperature: float | None = None,
    json_mode: bool = False,
    timeout: int | None = None,
    max_tokens: int | None = None,
    scene: str = "",
    reasoning_effort: str = "",
) -> str:
    """正文入口（旧签名/旧返回值）：只回正文，行为与引入元信息前逐字节一致。"""
    _result = await _chat_with_fallback_result(
        messages,
        ai_config,
        temperature=temperature,
        json_mode=json_mode,
        timeout=timeout,
        max_tokens=max_tokens,
        scene=scene,
        reasoning_effort=reasoning_effort,
    )
    return _result["text"]


async def chat_with_fallback_meta(
    messages: list,
    ai_config: dict | None = None,
    *,
    temperature: float | None = None,
    json_mode: bool = False,
    timeout: int | None = None,
    max_tokens: int | None = None,
    scene: str = "",
    reasoning_effort: str = "",
) -> dict:
    """元信息入口：返回 text/provider_used/fallback_reason/stream_parse_skipped*。

    ✅ R176 B2（2026-11-04）：需要观测降级链与流式解析跳过时使用；
    不需要时继续用 chat_with_fallback（同一编排实现，零分叉）。
    """
    return await _chat_with_fallback_result(
        messages,
        ai_config,
        temperature=temperature,
        json_mode=json_mode,
        timeout=timeout,
        max_tokens=max_tokens,
        scene=scene,
        reasoning_effort=reasoning_effort,
    )


def _messages_contain_images(messages: list) -> bool:
    """检测 messages 中是否包含图片内容（OpenAI Vision 格式）"""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False
