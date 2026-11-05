"""敏感信息脱敏（✅ R175 · 2026-11-03）。

审计发现的上游错误体回显面：
  * ``provider_factory._error_summary`` 把异常文本截 300 字符写入
    ``ai_audit_logs.error``，``/ai/audit-logs`` 原样返回；
  * ``/ai/config/test``、``/ai/config/fetch-models`` 把厂商响应体
    （``resp.text[:300]``）作为 ``raw`` 回传前端；
  * ``_attempt_candidate`` 的 ``logger.error`` 记录异常字符串。

部分厂商在 401/403 响应体里会回显请求头（``Bearer <key>``）或 ``api_key`` 字段。
本模块提供**单一出口**的文本脱敏，供上述三处复用；只做形态识别，不依赖具体厂商。

注意：脱敏是「降低泄露面」而非「消除泄露」——无法保证覆盖所有自定义密钥格式，
真正的边界仍应是「不把上游原文落库/回显」。
"""

import re

# 44 字符 urlsafe base64（43 字符主体 + 结尾 ``=``）。注意结尾 ``=`` 是非单词
# 字符，若右侧用 ``\b`` 会要求「非单词-单词」边界 —— 密钥位于文本末尾时匹配
# 失败（脱敏漏网）。改用负向断言：前后都不得紧邻同类字符。
_FERNET_KEY = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{43}=(?![A-Za-z0-9_\-])")
# sk-xxx / pk-xxx / rk-xxx / ak-xxx 等常见 Key 前缀（保留前缀，隐藏主体）
_PREFIXED_KEY = re.compile(r"(?i)\b(sk|pk|rk|ak)-[A-Za-z0-9_\-]{6,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")
_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s\"',;]+")
_KEY_FIELD = re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|secret(?:[_-]?key)?)\s*[\"'=:]\s*)" r"[^\s\"',;&]{6,}")


def redact_sensitive_text(text: str | None) -> str:
    """把文本中疑似 API Key / Token / 主密钥的片段替换为占位符。"""
    if not text:
        return ""
    s = str(text)
    s = _PREFIXED_KEY.sub(lambda m: f"{m.group(1)}-****", s)
    s = _BEARER.sub(lambda m: f"{m.group(1)}****", s)
    s = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}****", s)
    s = _KEY_FIELD.sub(lambda m: f"{m.group(1)}****", s)
    s = _FERNET_KEY.sub("<fernet-key>", s)
    return s
