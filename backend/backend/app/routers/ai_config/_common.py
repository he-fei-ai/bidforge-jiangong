"""AI 配置路由 · 公共错误分类与网络预检。

被 ``connectivity`` / ``models`` 子模块复用的纯函数，单独抽离以避免巨型单文件。
"""
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger("ai_config")


def _same_base_url(left: str, right: str) -> bool:
    """比较两个 API Base URL 是否指向同一地址（仅忽略首尾空白和尾斜杠）。"""
    return (left or "").strip().rstrip("/") == (right or "").strip().rstrip("/")


def _http_status_of(exc: Exception) -> int | None:
    """从 httpx 异常中取真实 HTTP 状态码。

    ✅ 修复：原实现用 `"404" in msg` 之类**子串匹配**判状态码，
      典型误判：端口号 4040、token 数 1404、模型名含 400 等都会命中，
      把「网络不通」误报成「Base URL 错误」。优先用异常自带的结构化状态码。
    """
    status = getattr(exc, "response", None)
    code = getattr(status, "status_code", None)
    if isinstance(code, int):
        return code
    # 兜底：provider 抛的是 RuntimeError(f"HTTP {code}: {body}")，需精确匹配前缀
    msg = str(exc)
    marker = "HTTP "
    idx = msg.find(marker)
    if idx >= 0:
        tail = msg[idx + len(marker):]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if len(digits) == 3:
            return int(digits)
    return None


def _classify_error(exc: Exception) -> dict:
    """把底层异常分类为可展示的结构 {category, message, suggestion}"""
    msg = str(exc)
    status_code = _http_status_of(exc)

    # 参数校验类（api_key 空、Illegal header value 等）
    if isinstance(exc, ValueError) or "Illegal header value" in msg or "Bearer " in msg:
        return {
            "category": "config",
            "label": "API Key 未配置",
            "message": "API Key 为空或格式不正确",
            "suggestion": (
                "请检查：① 是否已填写 API Key 并保存 ② Key 是否有多余的空格 "
                "③ Key 字符串中是否包含非法字符"
            ),
            "raw": msg,
        }

    # DNS 解析失败
    if "getaddrinfo failed" in msg or "Name or service not known" in msg or "nodename nor servname" in msg:
        host = ""
        for part in msg.split():
            if "://" in part:
                host = part.split("://")[-1].split("/")[0].split(":")[0]
                break
        return {
            "category": "dns",
            "label": "DNS 解析失败",
            "message": f"无法解析域名{f'：{host}' if host else ''}",
            "suggestion": (
                "请检查：① 网络是否已连接 ② 域名拼写是否正确 "
                "③ 是否需要配置代理 ④ 该厂商平台域名在本网络是否可达"
            ),
            "raw": msg,
        }

    # 超时
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in msg.lower() or "TimeoutError" in type(exc).__name__:
        return {
            "category": "timeout",
            "label": "连接超时",
            "message": "请求在规定时间内未收到响应",
            "suggestion": "请检查网络连接，或尝试在「文本模型配置」中增大超时时间（默认 900s）",
            "raw": msg,
        }

    # SSL / 证书
    if "SSLError" in type(exc).__name__ or "certificate" in msg.lower() or "SSL" in msg:
        return {
            "category": "ssl",
            "label": "SSL 证书错误",
            "message": "HTTPS 证书验证失败",
            "suggestion": "如果是内网或自测环境，可检查证书是否已安装；如使用代理，请确认代理未拦截 HTTPS",
            "raw": msg,
        }

    # 连接被拒绝 / 重置
    if "Connection refused" in msg or "ECONNREFUSED" in msg or "Connection reset" in msg or "Connection aborted" in msg:
        return {
            "category": "conn",
            "label": "连接被拒绝",
            "message": "目标服务器拒绝连接",
            "suggestion": "请确认 Base URL 端口是否正确、服务是否已启动、防火墙是否放行",
            "raw": msg,
        }

    # httpx 所有连接尝试都失败（DNS 解析到了 IP 但 TCP/SSL 全连不上）
    if "all connection attempts failed" in msg.lower():
        return {
            "category": "conn",
            "label": "网络无法到达该厂商",
            "message": "后端服务器无法建立到该厂商平台的连接",
            "suggestion": (
                "请检查：① 本机防火墙或安全软件是否拦截了出站 443 端口 "
                "② 公司网络是否限制了该厂商域名 ③ 是否需要配置 HTTP 代理 "
                "④ 尝试切换 DeepSeek / 通义千问 / 智谱等可连通的厂商"
            ),
            "raw": msg,
        }

    # HTTP 401 / 403 → 认证问题
    if status_code in (401, 403) or "Unauthorized" in msg or "Invalid API key" in msg.lower() or "authentication" in msg.lower():
        return {
            "category": "auth",
            "label": "API Key 无效",
            "message": "供应商返回认证失败（HTTP 401/403）",
            "suggestion": "请检查 API Key 是否正确、是否已过期、是否有调用权限",
            "raw": msg,
        }

    # HTTP 404 → URL 错
    if status_code == 404 or "Not Found" in msg:
        return {
            "category": "url",
            "label": "Base URL 错误",
            "message": "供应商返回 404 Not Found",
            "suggestion": "请检查 Base URL 路径是否完整，例如应包含 /v1 等路径段",
            "raw": msg,
        }

    # HTTP 429 → 限流
    if status_code == 429 or "rate limit" in msg.lower() or "Too Many Requests" in msg:
        return {
            "category": "ratelimit",
            "label": "触发供应商限流",
            "message": "请求过于频繁（HTTP 429）",
            "suggestion": "请稍后再试，或在「文本模型配置」中降低并发数",
            "raw": msg,
        }

    # HTTP 5xx → 供应商故障
    if (status_code is not None and 500 <= status_code < 600) or \
            "Bad Gateway" in msg or "Service Unavailable" in msg:
        return {
            "category": "provider",
            "label": "供应商服务异常",
            "message": f"供应商返回服务器错误（HTTP {status_code or '5xx'}）",
            "suggestion": "这是供应商侧问题，请稍后再试或切换其他厂商平台",
            "raw": msg,
        }

    # 其余 4xx（400 参数/模型名错误、402 余额不足等）按状态码给出可读提示
    if status_code == 400:
        return {
            "category": "request",
            "label": "请求被拒绝（HTTP 400）",
            "message": "供应商认为请求参数不合法，最常见原因是模型名称不存在",
            "suggestion": "请核对模型名称是否为该平台真实可用的模型（可用「获取最新模型」自动拉取）",
            "raw": msg,
        }
    if status_code == 402:
        return {
            "category": "balance",
            "label": "账户余额不足（HTTP 402）",
            "message": "供应商账户余额/额度不足",
            "suggestion": "请到供应商平台充值或更换其他渠道",
            "raw": msg,
        }

    # 有 HTTP 状态码但未匹配到具体
    if status_code is not None or "HTTP" in msg or "status code" in msg.lower():
        return {
            "category": "http",
            "label": f"HTTP 请求失败{'（' + str(status_code) + '）' if status_code else ''}",
            "message": msg[:80],
            "suggestion": "请检查 Base URL、API Key 和模型名称是否正确",
            "raw": msg,
        }

    return {
        "category": "unknown",
        "label": "未知错误",
        "message": msg[:120],
        "suggestion": "如反复出现，请把完整错误信息提交给管理员排查",
        "raw": msg,
    }


def _dns_precheck(base_url: str) -> dict:
    """先只做 DNS 解析 + TCP 握手，不发送任何请求体"""
    try:
        parsed = urlparse(base_url or "")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            return {"ok": False, "step": "dns", "message": "Base URL 格式无效"}
        # DNS
        try:
            ips = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            ip = ips[0][4][0]
        except Exception as e:
            return {
                "ok": False, "step": "dns",
                "message": f"DNS 解析失败：无法找到 {host}",
                "raw": str(e),
            }
        # TCP connect
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.close()
        except Exception as e:
            return {
                "ok": False, "step": "tcp",
                "message": f"TCP 握手失败：{host}:{port} 不可达",
                "raw": str(e),
            }
        return {"ok": True, "step": "ok", "host": host, "ip": ip, "port": port}
    except Exception as e:
        return {"ok": False, "step": "unknown", "message": str(e)}


async def _dns_precheck_async(base_url: str) -> dict:
    """``_dns_precheck`` 的异步包装。

    ✅ 修复（性能）：`_dns_precheck` 内部是 **同步阻塞** 的
      `socket.getaddrinfo` + `socket.create_connection(timeout=5)`。
      直接在 async 端点里调用会阻塞事件循环——DNS 慢或被墙时，
      本机 5 秒内**所有**请求（含正文生成的 SSE 流）全部卡住。
      改为丢到线程池执行，不占用事件循环。
    """
    import asyncio
    return await asyncio.to_thread(_dns_precheck, base_url)
