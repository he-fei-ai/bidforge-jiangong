"""MinerU 云端解析客户端（扫描件 / 复杂 PDF 兜底）。

两条链路（与 OpenBidKit 解析模块对齐，2026-09-17 移植）：
- MinerU-Agent 轻量解析（v1 API，免 Token，按 IP 限频）：
    ① POST /api/v1/agent/parse/file 申请上传链接（task_id + file_url）
    ② PUT 二进制直传原始文件
    ③ GET /api/v1/agent/parse/{task_id} 轮询至 done
    ④ 下载 data.markdown_url 得到 Markdown
- MinerU 精准解析（v4 API，需 Bearer Token）：
    ① POST /api/v4/file-urls/batch 批量申请上传链接（batch_id + file_urls）
    ② PUT 二进制直传
    ③ GET /api/v4/extract-results/batch/{batch_id} 轮询至 done
    ④ 下载 full_zip_url 的 zip，解包取 full.md

设计约束：
- 全部为同步实现 —— 调用方（file_parser）本身运行在线程池中，
  避免在事件循环里做阻塞网络 IO。
- 任何失败抛 ParseError（可读消息），绝不返回错误文本当作正文。
- 行为参数一律来自 app.config.settings（可环境变量覆盖），不做隐式钳制。
"""
from __future__ import annotations

import io
import logging
import re
import time
import zipfile

from app.config import settings
from app.services.file_parser import ParseError

logger = logging.getLogger(__name__)

_AGENT_BASE = "https://mineru.net/api/v1"
_ACCURATE_BASE = "https://mineru.net/api/v4"
_HTTP_TIMEOUT = 60          # 单次 HTTP 请求超时（秒），轮询超时由 settings 控制
_ZIP_MARKDOWN_MAX_BYTES = 64 * 1024 * 1024   # 结果 zip 内 Markdown 读取上限


def make_data_id(file_name: str) -> str:
    """生成 MinerU 精准解析的 data_id。

    仅保留字母数字、下划线/点/连字符与中文（其余字符折叠为单个下划线），
    截断 96 字符；纯非法字符时回退 "document"。
    """
    cleaned = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff.-]+", "_", file_name or "")
    cleaned = cleaned.strip("_")
    return (cleaned[:96] or "document")


def extract_markdown_from_zip(zip_bytes: bytes) -> str:
    """从 MinerU 精准解析结果 zip 中提取 Markdown（优先 full.md，其次任意 .md）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            target = next(
                (n for n in names if re.search(r"(^|[/\\])full\.md$", n, re.I)),
                None)
            if target is None:
                target = next(
                    (n for n in names if n.lower().endswith(".md")), None)
            if target is None:
                raise ParseError(
                    "MinerU 精准解析结果包中未找到 Markdown 文件")
            # 有界读取：防中央目录谎报大小
            chunks: list[bytes] = []
            total = 0
            with zf.open(target) as fp:
                while total < _ZIP_MARKDOWN_MAX_BYTES:
                    chunk = fp.read(1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            return b"".join(chunks).decode("utf-8", errors="ignore")
    except ParseError:
        raise
    except zipfile.BadZipFile as e:
        raise ParseError(f"MinerU 精准解析结果包损坏：{e}") from e
    except Exception as e:
        raise ParseError(f"MinerU 精准解析结果解包失败：{e}") from e


def _http():
    import httpx
    return httpx.Client(timeout=_HTTP_TIMEOUT)


def _raise_api_error(resp, step: str):
    """统一 API 错误转译：HTTP 非 2xx 或业务 code != 0 → ParseError。"""
    if resp.status_code >= 400:
        raise ParseError(
            f"MinerU 云端解析失败（{step}）：HTTP {resp.status_code}，"
            f"{resp.text[:200]}")
    try:
        payload = resp.json()
    except Exception as e:
        raise ParseError(f"MinerU 云端解析失败（{step}）：响应非 JSON") from e
    code = payload.get("code", payload.get("data", {}).get("code") if
                       isinstance(payload.get("data"), dict) else None)
    if code not in (0, "0", None):
        msg = payload.get("msg") or payload.get("message") or "未知错误"
        raise ParseError(f"MinerU 云端解析失败（{step}）：{msg}")
    return payload.get("data") or {}


def _poll_until(done_states: tuple[str, ...], fetch_state, timeout_s: int,
                interval: float, task_hint: str) -> dict:
    """通用轮询骨架：fetch_state() -> (state_str, payload)；failed/超时抛 ParseError。"""
    deadline = time.monotonic() + timeout_s
    while True:
        state, payload = fetch_state()
        if state in done_states:
            return payload
        if state in ("failed", "error"):
            msg = (payload or {}).get("err_msg") or \
                  (payload or {}).get("message") or "未知原因"
            raise ParseError(f"MinerU 云端解析失败：{msg}")
        if time.monotonic() > deadline:
            raise ParseError(
                f"MinerU 云端解析轮询超时（{timeout_s}s），{task_hint}，请稍后重试")
        time.sleep(max(interval, 0.5))


def _download_bytes(url: str, client, what: str) -> bytes:
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content
    except ParseError:
        raise
    except Exception as e:
        raise ParseError(f"下载 MinerU {what} 失败：{e}") from e


def parse_with_mineru_agent(content: bytes, file_name: str) -> str:
    """MinerU-Agent 轻量解析（v1，免 Token）。"""
    with _http() as client:
        # ① 申请上传链接
        try:
            resp = client.post(f"{_AGENT_BASE}/agent/parse/file", json={
                "file_name": file_name,
                "language": "ch",
                "enable_table": True,
                "is_ocr": True,
                "enable_formula": True,
            })
            data = _raise_api_error(resp, "申请上传链接")
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"MinerU-Agent 申请上传链接失败：{e}") from e
        task_id = str(data.get("task_id") or "")
        upload_url = str(data.get("file_url") or "")
        if not task_id or not upload_url:
            raise ParseError("MinerU-Agent 响应缺少 task_id / file_url")

        # ② 二进制直传
        try:
            put = client.put(upload_url, content=content,
                             headers={"Content-Type": "application/octet-stream"})
            put.raise_for_status()
        except Exception as e:
            raise ParseError(f"MinerU-Agent 文件上传失败：{e}") from e

        # ③ 轮询
        def _fetch():
            r = client.get(f"{_AGENT_BASE}/agent/parse/{task_id}")
            d = _raise_api_error(r, "轮询解析结果")
            inner = d if isinstance(d, dict) else {}
            return str(inner.get("state") or ""), inner

        final = _poll_until(
            ("done",), _fetch,
            timeout_s=int(settings.mineru_agent_timeout),
            interval=float(settings.mineru_poll_interval),
            task_hint=f"task_id: {task_id}")

        # ④ 下载 Markdown
        md_url = str(final.get("markdown_url") or "")
        if not md_url:
            raise ParseError("MinerU-Agent 结果缺少 markdown_url")
        raw = _download_bytes(md_url, client, "Markdown")
    return raw.decode("utf-8", errors="ignore")


def parse_with_mineru_accurate(content: bytes, file_name: str) -> str:
    """MinerU 精准解析（v4，需 Token）。"""
    token = (settings.mineru_token or "").strip()
    if not token:
        raise ParseError(
            "MinerU 精准解析需要 Token：请在 .env 配置 MINERU_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    with _http() as client:
        # ① 批量申请上传链接
        try:
            resp = client.post(
                f"{_ACCURATE_BASE}/file-urls/batch",
                headers=headers,
                json={
                    "files": [{"name": file_name,
                               "data_id": make_data_id(file_name),
                               "is_ocr": True}],
                    "model_version": "vlm",
                    "language": "ch",
                    "enable_table": True,
                    "enable_formula": True,
                })
            data = _raise_api_error(resp, "申请批量上传链接")
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"MinerU 精准解析申请上传链接失败：{e}") from e
        batch_id = str(data.get("batch_id") or "")
        urls = data.get("file_urls") or []
        upload_url = str(urls[0]) if urls else ""
        if not batch_id or not upload_url:
            raise ParseError("MinerU 精准解析响应缺少 batch_id / file_urls")

        # ② 二进制直传
        try:
            put = client.put(upload_url, content=content,
                             headers={"Content-Type": "application/octet-stream"})
            put.raise_for_status()
        except Exception as e:
            raise ParseError(f"MinerU 精准解析文件上传失败：{e}") from e

        # ③ 轮询（按 file_name 精确匹配，缺失时取 items[0] 兜底）
        def _fetch():
            r = client.get(
                f"{_ACCURATE_BASE}/extract-results/batch/{batch_id}",
                headers=headers)
            d = _raise_api_error(r, "轮询解析结果")
            items = d.get("extract_result") or []
            item = next((x for x in items
                         if isinstance(x, dict)
                         and x.get("file_name") == file_name),
                        items[0] if items else {})
            return str((item or {}).get("state") or ""), (item or {})

        final = _poll_until(
            ("done",), _fetch,
            timeout_s=int(settings.mineru_accurate_timeout),
            interval=float(settings.mineru_poll_interval),
            task_hint=f"batch_id: {batch_id}")

        # ④ 下载结果 zip 并解包 full.md
        zip_url = str(final.get("full_zip_url") or "")
        if not zip_url:
            raise ParseError("MinerU 精准解析结果缺少 full_zip_url")
        zip_bytes = _download_bytes(zip_url, client, "结果包")
    return extract_markdown_from_zip(zip_bytes)


def parse_with_mineru(content: bytes, file_name: str) -> str:
    """按配置分派 MinerU 云端解析；未启用 / 失败统一抛 ParseError。"""
    if not settings.mineru_enabled:
        raise ParseError("MinerU 云端解析未启用（MINERU_ENABLED=false）")
    provider = (settings.mineru_provider or "").strip().lower()
    if provider == "agent":
        return parse_with_mineru_agent(content, file_name)
    if provider == "accurate":
        return parse_with_mineru_accurate(content, file_name)
    raise ParseError(
        "MinerU 云端解析未配置（MINERU_PROVIDER 需为 agent 或 accurate）")
