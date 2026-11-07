"""AI 配置路由 · 连通性 / 健康（测试连接、预检、健康快照）。"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends

from app.config import settings
from app.db import read_db
from app.models import AIConfigTest
from app.services.ai.provider_factory import (
    DEFAULT_CONFIG_NUMBERS,
    PROVIDER_PRESETS,
    VALID_REQUEST_MODES,
    clamp_config_numbers,
    normalize_base_url,
    normalize_request_mode,
    request_mode_label,
)
from app.services.crypto import decrypt_api_key
from app.services.redaction import redact_sensitive_text

from ._common import _classify_error, _dns_precheck_async, _same_base_url

logger = logging.getLogger("ai_config")

router = APIRouter(tags=["ai_config"])

#: ✅ R179（N-09）：单条预检探测的总超时（秒）。底层 DNS+TCP 各 5s 内应完成；
#: 15s 覆盖系统解析器排队的最坏情况。模块级常量便于单测注入更小值。
_PRECHECK_PROBE_TIMEOUT_S = 15

#: ✅ R183（2026-11-07）：单条连通性探测的超时上限（秒）。
#: 前端 ``aiApi.testConfig`` 的 axios 硬超时为 260s，后端最坏路径是
#: 预检 15s +「首选 + 兜底」两条探测，因此单条探测必须 ≤120s，
#: 否则后端已完成错误分类，前端却先被 axios 泛化超时截断、拿不到原因。
#: 配置的 timeout < 120s 时按配置值探测（与实际调用同口径）；大于上限时
#: 通过响应里的 ``config_timeout_s`` / ``probe_timeout_s`` 如实暴露差异。
_PROBE_TIMEOUT_CAP_S = 120


@router.post("/config/test")
async def test_config(data: AIConfigTest, db=Depends(read_db)):
    """测试连接：先做 DNS/TCP 预检，再发真实请求，错误分类返回"""
    from app.services.ai.provider_factory import _build_provider
    from app.services.crypto import decrypt_api_key

    # 先决定用哪个 api_key（前端表单的值 OR 数据库已保存的）
    api_key = (data.api_key or "").strip()
    base_url = (data.base_url or "").strip()
    model = (data.model or "").strip()
    config_id = (data.config_id or "").strip()

    db_request_mode = ""
    db_timeout = 0
    saved_provider_name = ""
    if config_id:
        cur = await db.execute(
            "SELECT provider_name, base_url, api_key_encrypted, model, request_mode, timeout"
            " FROM ai_config WHERE id=?",
            (config_id,),
        )
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "error": "配置不存在（可能已被删除）", "suggestion": "请刷新页面后重新选择配置"}
        saved_base_url = (row["base_url"] or "").strip()
        # ✅ R183：只传 config_id 时（如列表行的快捷测试）表单不带 provider_name，
        #    原实现按空串当「自定义 OpenAI 兼容」，Anthropic 类配置会被探测到
        #    错误的接口路径。现回读库里的 provider_name 作为兜底。
        saved_provider_name = (row["provider_name"] or "").strip()
        # 已保存 Key 与其 Base URL 是一组不可拆分的凭据。地址被改写时若仍从
        # DB 取旧 Key，会把密钥发给任意请求方指定的外部地址，属于密钥外泄。
        # ✅ R183：仅当库里**确有** Key 时才需要这条拦截；未存 Key 的配置改地址
        #    再报「必须重新输入 Key」会把真实原因（API Key 为空）掩盖掉。
        if base_url and not _same_base_url(base_url, saved_base_url) and not api_key and row["api_key_encrypted"]:
            return {
                "ok": False,
                "error": "Base URL 已变更，必须重新输入该地址对应的 API Key",
                "suggestion": "为防止已保存密钥被发送到其他地址，请重新输入新地址对应的 API Key 后再测试",
            }
        if not base_url:
            base_url = saved_base_url
        if not model:
            model = (row["model"] or "").strip()
        try:
            db_request_mode = (row["request_mode"] or "").strip().lower()
        except (IndexError, KeyError):
            db_request_mode = ""
        try:
            db_timeout = int(row["timeout"] or 0)
        except (IndexError, KeyError, TypeError, ValueError):
            db_timeout = 0
        if not api_key and row["api_key_encrypted"]:
            api_key = decrypt_api_key(row["api_key_encrypted"]).strip()
            if not api_key:
                # ✅ 修复（本轮审查）：密文存在但解不开（换过 FERNET_KEY / 删过
                #    data/secret_key.key）时，原实现一路走到「API Key 为空」，
                #    把用户指向「你没填 Key」这个完全错误的方向。现在直接说清楚。
                return {
                    "ok": False,
                    "error": "已保存的 API Key 无法解密",
                    "label": "密钥失效",
                    "category": "config",
                    "suggestion": (
                        "常见于更换过加密密钥（FERNET_KEY）或删除过 "
                        "data/secret_key.key。请重新填写该配置的 API Key "
                        "并保存后再测试。"
                    ),
                }

    # Step 0: 参数校验 —— 没填 key 就拒绝，别让 httpx 去拼出 "Bearer "
    if not base_url:
        return {
            "ok": False,
            "error": "base_url 不能为空",
            "suggestion": "请填写 API Base URL，如 https://api.openai.com/v1",
        }
    if not model:
        return {"ok": False, "error": "模型名称为空", "suggestion": "请先选择或填写模型名称，再测试连接"}
    if not api_key:
        return {
            "ok": False,
            "error": "API Key 为空",
            "suggestion": "请先填写 API Key 并保存，或直接在当前表单输入 Key 后再点「测试连接」",
        }

    # Step 0.5: URL 合法性（协议 / 域名白名单），与保存时的校验口径保持一致
    try:
        base_url = normalize_base_url(base_url)
    except ValueError as e:
        return {
            "ok": False,
            "error": redact_sensitive_text(str(e)),
            "suggestion": "请填写形如 https://api.example.com/v1 的地址",
        }

    # Step 1: 网络预检
    pre = await _dns_precheck_async(base_url)
    if not pre["ok"]:
        # 网络层面不通，直接返回，不浪费请求
        return {
            "ok": False,
            "error": pre["message"],
            "suggestion": _classify_error(Exception(pre["raw"] or pre["message"])).get("suggestion", ""),
            "detail": pre,
        }

    # Step 2: 连通性探测
    # 用 stream=True 比同步等完整响应快得多（340B 大模型同步等完所有 token 要 60-120s），
    # 但两条路径**并不等价**：平台可能只支持其中一条。因此这里按配置的
    # 「请求方式」决定首选探测方式，失败/空流时自动用另一种方式兜底再探一次，
    # 并把**实际生效的方式**如实回传 —— 用户 Key/地址/模型都对时，
    # 不该因为探测方式与平台支持不一致而报「模型名称不存在」。
    raw_mode = (data.request_mode or "").strip().lower() or db_request_mode
    # ⚠️ 值域取自 provider_factory 单一出口（不再自带一份字面量白名单）。
    #    ``auto`` 不是落库值域，只是「未指定 → 沿用既有策略（流式优先）」的
    #    探测侧内部标记，故仍保留在此处而不并入白名单。
    probe_mode = raw_mode if raw_mode in VALID_REQUEST_MODES else "auto"
    # auto（未指定）沿用历史策略：流式优先（首字节快），错误归因按 chat 口径。
    prefer_stream = probe_mode != "normal"

    # ✅ 增强：探测 max_tokens 与保存链路同一口径钳制（256..200000）。
    #    原实现只做 `int(...) or 8192`，手工请求可把 10^9 一路发给厂商拿 400，
    #    错误被归因为「Key/模型不对」。前端表单已限 min/max，这里补 API 层兜底。
    probe_max_tokens = clamp_config_numbers({"max_tokens": int(getattr(data, "max_tokens", 0) or 0) or 8192})[
        "max_tokens"
    ]
    # ✅ R183：探测超时不再硬编码 120s —— 表单值 → 已存配置 → 默认 900s，
    #    并走保存/运行时同一套 clamp 出口；再按前端 260s 契约封顶。
    requested_timeout = int(getattr(data, "timeout", 0) or 0)
    effective_timeout = clamp_config_numbers(
        {"timeout": requested_timeout or db_timeout or DEFAULT_CONFIG_NUMBERS["timeout"]}
    )["timeout"]
    probe_timeout = min(effective_timeout, _PROBE_TIMEOUT_CAP_S)
    provider_name = (data.provider_name or "").strip() or saved_provider_name
    try:
        provider = _build_provider(
            provider_name, api_key, base_url, model, max_tokens=probe_max_tokens, timeout=probe_timeout
        )
    except ValueError as e:
        return {
            "ok": False,
            "error": redact_sensitive_text(str(e)),
            "category": "config",
            "suggestion": "请检查供应商、Base URL 与模型名称是否填写完整",
        }

    import time as _t

    async def _probe_stream() -> str:
        total = ""
        stream = provider.stream([{"role": "user", "content": "回复ok"}])
        try:
            async for chunk in stream:
                if chunk:
                    total += chunk
                    # 凑够一小段响应片段即可收工，不必等全部生成完
                    if len(total) > 80:
                        break
        finally:
            # ✅ R183：提前 break 后 async generator 仍处于挂起态，底层
            #    httpx 连接要等 GC 才释放；并发探测/频繁点「测试连接」时
            #    会累积未关闭连接。这里显式 aclose（关闭失败不影响探测结论）。
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    logger.debug("关闭探测流失败（忽略）", exc_info=True)
        return total

    async def _probe_chat() -> str:
        return await provider.chat(
            [{"role": "user", "content": "回复ok"}], temperature=provider.temperature, max_tokens=probe_max_tokens
        )

    probes = {"stream": _probe_stream, "chat": _probe_chat}
    order = ["stream", "chat"] if prefer_stream else ["chat", "stream"]
    preferred = order[0]

    _t0 = _t.time()
    probe_errors: dict[str, Exception] = {}
    response_text = ""
    used = ""
    for name in order:
        try:
            out = await probes[name]()
        except Exception as e:
            probe_errors[name] = e
            continue
        if (out or "").strip():
            response_text, used = out, name
            break
        # 200 但内容为空：与抛错同义（不能算探测成功）
        probe_errors[name] = RuntimeError("厂商返回了空响应")
    elapsed_ms = int((_t.time() - _t0) * 1000)

    if response_text:
        payload = {
            "ok": True,
            "response": response_text[:100],
            "provider": provider_name,
            "model": provider.model,
            "base_url": base_url,
            "network": pre,
            "latency_ms": elapsed_ms,
            "mode": f"{used}_probe",
            "request_mode": "stream" if used == "stream" else "normal",
            "probe_timeout_s": probe_timeout,
            "config_timeout_s": effective_timeout,
        }
        if used != preferred:
            first_err = probe_errors.get(preferred)
            if preferred == "stream":
                payload["warning"] = (
                    "该平台的流式接口（stream）未返回内容，已回退为普通对话探测："
                    "正文生成不受影响，但流式输出类功能可能不可用。"
                    + (f"（流式错误：{redact_sensitive_text(str(first_err))[:120]}）" if first_err else "")
                )
            else:
                payload["warning"] = (
                    "普通请求探测未返回内容，改用流式请求探测成功："
                    "该平台可能对非流式请求有限制，"
                    "建议把该配置的「请求方式」改为「流式请求」。"
                    + (f"（普通请求错误：{redact_sensitive_text(str(first_err))[:120]}）" if first_err else "")
                )
        return payload

    # 两种方式都没拿到内容：以「该配置实际会被使用的那种方式」的错误为准
    # （显式选了流式就用流式错误；normal / auto 时正文生成走 chat，以 chat 为准），
    # 同时附带另一条路径的错误便于对照差异。
    primary = "stream" if probe_mode == "stream" else "chat"
    err = (
        probe_errors.get(primary)
        or probe_errors.get("chat")
        or probe_errors.get("stream")
        or RuntimeError("厂商返回了空响应")
    )
    classified = _classify_error(err)
    # 如果超时但网络预检 OK，给用户特别提示（NVIDIA 等美国厂商在国内就是慢）
    if classified["category"] == "timeout" and pre.get("ok"):
        classified["message"] = f"请求超时（{elapsed_ms}ms），但网络已连通"
        classified["suggestion"] = (
            "该厂商服务器可能在境外，国内访问延迟较高。"
            "可尝试：① 换国内厂商平台（如阿里百炼、智谱等）② 使用更小更快的模型"
            "③ 稍后重试 ④ 配置代理加速"
        )
    _chat_err = probe_errors.get("chat")
    _stream_err = probe_errors.get("stream")
    if _chat_err is not None and _stream_err is not None:
        if primary == "chat":
            classified["raw"] = f"非流式：{classified.get('raw', '')}" f" ｜ 流式：{str(_stream_err)[:150]}"
        else:
            classified["raw"] = f"流式：{classified.get('raw', '')}" f" ｜ 非流式：{str(_chat_err)[:150]}"
    # ✅ R183：单条探测受前端 260s 契约封顶；配置超时被截断时如实说明，
    #    避免用户按「增大超时时间」建议改到 900s 后仍看到 120s 处的超时。
    if classified["category"] == "timeout" and effective_timeout > probe_timeout:
        classified["suggestion"] += (
            f"；本次探测按 {probe_timeout}s 上限执行（该配置超时为 {effective_timeout}s）"
        )
    classified["network"] = pre
    classified["latency_ms"] = elapsed_ms
    return {
        "ok": False,
        # 兜底脱敏：即使上游文本没走到 _classify_error，也不会明文回显密钥
        "error": redact_sensitive_text(classified["message"]),
        "suggestion": redact_sensitive_text(classified["suggestion"]),
        "category": classified["category"],
        "label": classified.get("label", ""),
        "raw": redact_sensitive_text(classified.get("raw", str(err))),
        "model": model,
        "base_url": base_url,
        "network": pre,
        "latency_ms": elapsed_ms,
        "probe_timeout_s": probe_timeout,
        "config_timeout_s": effective_timeout,
    }


@router.post("/config/precheck")
async def precheck_config(data: AIConfigTest, db=Depends(read_db)):
    """只做连通性预检（DNS + TCP），不发送任何需要认证的请求。

    ✅ 增强：支持只传 config_id（从库中取 base_url），与「测试连接」的能力对齐。
    """
    base_url = (data.base_url or "").strip()
    if not base_url and (data.config_id or "").strip():
        cur = await db.execute("SELECT base_url FROM ai_config WHERE id=?", (data.config_id.strip(),))
        row = await cur.fetchone()
        if row:
            base_url = (row["base_url"] or "").strip()
    if not base_url:
        return {"ok": False, "step": "dns", "message": "Base URL 为空，请先填写地址"}
    try:
        base_url = normalize_base_url(base_url)
    except ValueError as e:
        return {"ok": False, "step": "url", "message": redact_sensitive_text(str(e))}
    result = await _dns_precheck_async(base_url)
    result["base_url"] = base_url
    return result


@router.post("/config/precheck-all")
async def precheck_all(db=Depends(read_db)):
    """批量连通性预检（✅ 新增）。

    降级链生效后，「备选配置到底通不通」直接决定故障时能否兜住。
    本接口对全部已填地址的配置并发做 DNS/TCP 预检 ——
    不发任何带 API Key 的请求，不消耗额度，秒级返回，
    用于一眼看出哪些备选是「假备胎」。
    """
    import asyncio

    cur = await db.execute(
        "SELECT id, provider_name, model, base_url, request_mode, enabled FROM ai_config"
        " ORDER BY priority ASC, updated_at DESC"
    )
    rows = [dict(r) for r in await cur.fetchall()]

    targets = []
    for r in rows:
        url = (r.get("base_url") or "").strip()
        if not url:
            continue
        try:
            url = normalize_base_url(url)
        except ValueError:
            continue
        targets.append((r, url))

    # ✅ R179（N-09）：**有界并发**。旧实现无界 gather —— 每条配置一个
    #    ``asyncio.to_thread`` 线程，配置数大时线程瞬时拉满；且底层
    #    ``socket.getaddrinfo`` 无显式超时，被墙/解析器卡死时会长期占用
    #    线程（AWS Builders' Library：并发探测必须有界超时）。现按
    #    ``settings.precheck_max_concurrency``（默认 8，非正数/非法值回落 8，
    #    与 global_facts._positive_int_setting 同口径）限流；单条探测加 15s
    #    总闸 —— DNS+TCP 各 5s 足够，15s 覆盖系统解析器排队的最坏情况，
    #    超时按「不可达」计入统计而非拖住整个接口。
    try:
        _limit = int(getattr(settings, "precheck_max_concurrency", 8))
    except (TypeError, ValueError):
        _limit = 8
    if _limit <= 0:
        _limit = 8
    sem = asyncio.Semaphore(_limit)

    async def _one(item):
        r, url = item
        async with sem:
            try:
                res = await asyncio.wait_for(_dns_precheck_async(url), timeout=_PRECHECK_PROBE_TIMEOUT_S)
            except asyncio.TimeoutError:
                res = {
                    "ok": False,
                    "step": "timeout",
                    "message": f"预检超时（{_PRECHECK_PROBE_TIMEOUT_S}s）：DNS 解析或网络连接停滞",
                }
        # ✅ R179：回传用户停用状态，避免把「已停用」的配置当作可用备胎
        enabled_val = r.get("enabled", 1)
        enabled = True if enabled_val is None else int(enabled_val or 0) != 0
        return {
            "id": r["id"],
            "provider_name": r["provider_name"],
            "model": r["model"],
            "base_url": url,
            "request_mode": normalize_request_mode(r.get("request_mode")),
            "enabled": enabled,
            "ok": bool(res.get("ok")),
            "message": res.get("message") or ("可达" if res.get("ok") else "不可达"),
            "ip": res.get("ip", ""),
            "step": res.get("step", ""),
        }

    results = await asyncio.gather(*[_one(t) for t in targets]) if targets else []
    ok_count = sum(1 for r in results if r["ok"])
    # ✅ R181（遗留项「超时/限流表现为加法式诊断字段」）：既有 items[].step=timeout
    #    保持不变；这里追加聚合字段，调用方可直接看「这次有多少条被超时闸掐断」
    #    以及本次实际生效的并发上限（排查上游压力时不必猜配置值）。
    timeout_count = sum(1 for r in results if r.get("step") == "timeout")
    return {
        "items": results,
        "total": len(results),
        "ok_count": ok_count,
        "fail_count": len(results) - ok_count,
        "skipped_no_url": len(rows) - len(results),
        # ✅ R179：其中被用户停用（enabled=0）的条数——它们即使网络可达也不参与调用
        "disabled_by_user": sum(1 for r in results if not r["enabled"]),
        "timeout_count": timeout_count,
        "concurrency_limit": _limit,
    }


@router.get("/configs/health")
async def configs_health(db=Depends(read_db)):
    """全部配置的可用性体检（✅ 2026-10-06 G3）：密钥 / 环境 / 地址 / 网络四道关。

    背景：此前只有 ``GET /ai/health``（**只看当前使用那一条**）与
    ``POST /ai/config/precheck-all``（**只看网络**）两个端点，且分属两路 ——
    「这条备选到底还能不能用上」要靠用户人工把两边的结果在脑子里合并。
    本端点一次性给出每条配置的四类判定与汇总；**只读、不发任何认证请求、
    不消耗额度、秒级返回**。

    判定口径全部复用运行时同源实现（不在本端点重抄）：
      - ``has_key`` / ``key_broken`` —— 与 ``GET /ai/config`` 同一判据
        （密文存在但解不开 = key_broken，用户须重新填 Key）；
      - ``in_current_env`` —— 与 ``resolve_active_env`` 同口径
        （env 为空 = 通用配置，任何环境可用；否则须等于当前生效环境）；
      - ``network_ok`` —— 复用 ``_dns_precheck_async``（与 precheck-all
        **同一函数**，不重抄 DNS/TCP 逻辑）；
      - ``disabled`` —— ``resolve_disabled_providers``（运行时厂商开关）；
      - ``runtime_health`` —— ``provider_health_snapshot``（R103 新增：实时
        成功率样本数/死配置判定/熔断/配额冷却，与选路同源）。

    ⚠️ 刻意不做密钥定期巡检：没有后台任务、不消耗配额；这是用户主动触发的一次性体检。
    """
    import asyncio

    from app.services.ai.provider_factory import (
        provider_health_snapshot,
        resolve_active_env,
        resolve_disabled_providers,
    )

    try:
        active_env = await resolve_active_env()
    except Exception:
        active_env = ""
    try:
        disabled = await resolve_disabled_providers()
    except Exception:
        disabled = set()

    cur = await db.execute(
        "SELECT id, provider_name, plan, api_key_encrypted, base_url, model,"
        " env, is_active, priority, request_mode, enabled FROM ai_config"
        " ORDER BY is_active DESC, priority ASC, updated_at DESC"
    )
    rows = [dict(r) for r in await cur.fetchall()]

    async def _net(url: str) -> dict:
        if not url:
            return {"ok": False, "step": "no_url", "message": "未填写 API 地址"}
        try:
            return await _dns_precheck_async(normalize_base_url(url))
        except Exception as e:
            # 地址格式非法 / 预检自身异常都不能让整份体检失败
            return {"ok": False, "step": "url", "message": redact_sensitive_text(str(e))}

    nets = await asyncio.gather(*[_net((r.get("base_url") or "").strip()) for r in rows])

    items: list[dict] = []
    for r, net in zip(rows, nets):
        enc = r.get("api_key_encrypted") or ""
        api_key = decrypt_api_key(enc) if enc else ""
        has_key = bool(api_key)
        key_broken = bool(enc and not api_key)
        url = (r.get("base_url") or "").strip()
        env = str(r.get("env") or "")
        in_env = (env == "") or (env == active_env)
        provider = str(r.get("provider_name") or "")
        # ✅ R179（2026-11-05）：用户停用（enabled=0）此前完全没进体检判定 ——
        #    这样一条配置会被报成「可用」，但降级链/视觉候选/场景路由都不会用它，
        #    即「体检说能用、实际不用」。现纳入结论并单独计数。
        enabled_val = r.get("enabled", 1)
        enabled = True if enabled_val is None else int(enabled_val or 0) != 0
        items.append(
            {
                "id": r["id"],
                "provider_name": provider,
                "model": str(r.get("model") or ""),
                "base_url": url,
                "plan": str(r.get("plan") or "pay_as_you_go"),
                "is_active": bool(r.get("is_active")),
                "priority": r.get("priority"),
                "env": env,
                "in_current_env": in_env,
                "request_mode": normalize_request_mode(r.get("request_mode")),
                "request_mode_label": request_mode_label(r.get("request_mode")),
                # 密钥三态（与 GET /ai/config 同口径）
                "has_key": has_key,
                "key_broken": key_broken,
                "key_hint": api_key[-4:] if has_key else "",
                "no_url": not bool(url),
                # 厂商是否被运行时开关禁用（与运行时选模同一来源）
                "disabled": provider in disabled,
                # 用户是否显式停用该配置（enabled=0；与厂商开关是两层独立开关）
                "enabled": enabled,
                "disabled_by_user": not enabled,
                # 网络（只 DNS + TCP，不发认证请求、不消耗额度）
                "network_ok": bool(net.get("ok")),
                "network_step": str(net.get("step") or ""),
                "network_ip": str(net.get("ip") or ""),
                "network_message": str(net.get("message") or ""),
                # ✅ R103（2026-10-18 · 日志审计遗留 L1 收口）：运行时健康快照
                #    （实时成功率样本/死配置判定/熔断/冷却）。判据单一事实源在
                #    provider_factory.provider_health_snapshot，与选路同源；
                #    此前「配置看着正常、却被自动剔除出降级链」在 UI 完全不可见
                #    （生产实证：降级链 9→3 用户不知该清理谁）。加法式新键。
                "runtime_health": provider_health_snapshot(provider, str(r.get("model") or ""), r["id"], url),
                # 综合：这条配置在当前环境下是否真的能用上（停用即不可用）
                "usable": bool(has_key and not key_broken and url and in_env and provider not in disabled and enabled),
            }
        )

    summary = {
        "total": len(items),
        "active": sum(1 for i in items if i["is_active"]),
        "usable": sum(1 for i in items if i["usable"]),
        "no_key": sum(1 for i in items if not i["has_key"] and not i["key_broken"]),
        "key_broken": sum(1 for i in items if i["key_broken"]),
        "no_url": sum(1 for i in items if i["no_url"]),
        "out_of_env": sum(1 for i in items if not i["in_current_env"]),
        "disabled": sum(1 for i in items if i["disabled"]),
        # ✅ R179：被用户停用（enabled=0）的条数（运行时不参与任何调用）
        "disabled_by_user": sum(1 for i in items if i["disabled_by_user"]),
        "network_ok": sum(1 for i in items if i["network_ok"]),
        "network_unreachable": sum(1 for i in items if i["base_url"] and not i["network_ok"]),
        # ✅ R103：实时成功率过低（已被自动剔除出降级链）的配置条数。
        #    与 items[].runtime_health.dead 同源，供体检汇总一眼看出
        #    「该清理谁」；冷启动/无样本时为 0（不误报）。
        "runtime_dead": sum(1 for i in items if (i.get("runtime_health") or {}).get("dead")),
    }
    return {
        "items": items,
        "summary": summary,
        "active_env": active_env,
        "disabled_providers": sorted(disabled),
        "checked_at": datetime.now().isoformat(),
    }


@router.get("/health")
async def ai_health(db=Depends(read_db)):
    """当前 AI 配置健康快照。

    ✅ 增强：原实现只返回 status/provider/model/supports_vision，
      排查「明明配了却调不通」时看不到 Base URL、降级候选数、熔断状态与并发，
      只能靠翻库。现一次性给出运行时可观测信息。
    """
    from app.services.ai.provider_factory import (
        _build_provider,
        _fallback_chain,
        _load_active_config,
        resolve_active_env,
        resolve_disabled_providers,
    )
    from app.services.ai.workflows_base import circuit_breaker, concurrency_controller

    # 必须复用运行时同一主配置解析：多环境下裸查 is_active=1 会把其它环境
    # 的配置误报为当前使用，与实际 chat_with_fallback 选模结果不一致。
    # ✅ 2026-09-25（缺口修复）：运行时环境值损坏时 _load_active_config 会
    #    fail-closed 抛 ValueError。运行时选模必须 fail-closed（不可用错模型），
    #    但健康快照若跟着 500，前端只会显示「加载失败」，用户既看不到真实
    #    原因（环境值损坏）也找不到恢复入口（PUT /env 重置为通用）。
    #    故此处捕获并把原因如实上报为独立状态 env_corrupt。
    env_error = ""
    try:
        row = await _load_active_config()
    except ValueError as e:
        row, env_error = None, redact_sensitive_text(str(e))

    # 熔断器状态（per-provider）
    breaker = {}
    try:
        for name, entry in getattr(circuit_breaker, "_per_provider", {}).items():
            breaker[name] = {"state": entry.get("state"), "failures": entry.get("failures", 0)}
    except Exception:
        breaker = {}

    # ✅ 2026-09-23：运行时开关状态也要可见 —— 否则「明明配了却一直不用它」
    #    只能靠翻审计日志才发现是被运行时开关禁用了。
    try:
        _disabled = sorted(await resolve_disabled_providers())
    except Exception:
        _disabled = []
    try:
        _env = await resolve_active_env()
    except Exception:
        _env = ""

    base = {
        "degraded_providers": breaker,
        "concurrency": getattr(concurrency_controller, "current", None),
        "max_concurrency": getattr(concurrency_controller, "max_c", None),
        # 被运行时开关禁用的厂商（空列表 = 未启用该开关）
        "disabled_providers": _disabled,
        # 当前生效环境（空串 = 通用，不做环境过滤）
        "active_env": _env,
        # 环境值损坏的原因描述（空串 = 正常）；前端据此给出「重置为通用」入口
        "env_error": env_error,
        # ✅ R175（BUG-04）：降级链生效上限，前端据此说明「仅前 N 条参与」
        "fallback_chain_max": int(getattr(settings, "ai_fallback_chain_max", 3) or 3),
    }

    if not row:
        if env_error:
            # 不把「环境值损坏」误报成「尚未配置模型」——两者提示与修法完全不同
            base.update(
                {
                    "status": "env_corrupt",
                    "fallback_count": 0,
                    "hint": f"{env_error}；请在「运行时设置」把当前生效环境清空" "（恢复通用环境）后重试",
                }
            )
        else:
            # ✅ 2026-10-06（B-7 · 多环境下的误导性提示）：多环境开启后，
            #    ``is_active=1`` 是**全局唯一**的（save/toggle 都会先把其它行清 0），
            #    于是「切到环境 B」而唯一的主配置属于环境 A 时，_load_active_config
            #    返回 None —— 状态确实是 not_configured，但旧提示恒写
            #    「尚未启用任何文本模型配置」，把用户指向「去新增一条配置」，
            #    而真因是「当前环境没有主配置 / 主配置属于别的环境」。
            #    这里按「库里是否还有配置」分流，保持 status 契约不变。
            hint = "尚未启用任何文本模型配置，所有 AI 生成能力将不可用"
            if _env:
                cur_any = await db.execute("SELECT COUNT(*) FROM ai_config")
                total_cfg = int((await cur_any.fetchone())[0] or 0)
                if total_cfg:
                    hint = (
                        f"当前生效环境「{_env}」下没有可用的「当前使用」配置"
                        "（该标记全局唯一，当前它属于其它环境或通用环境），"
                        "所有 AI 生成能力将不可用：请在列表中把目标环境的配置"
                        "点「设为当前使用」，或把生效环境切回通用（清空）"
                    )
            base.update({"status": "not_configured", "fallback_count": 0, "hint": hint})
        return base

    d = row
    enc = d.get("api_key_encrypted") or ""
    api_key = decrypt_api_key(enc) if enc else ""
    key_configured = bool(api_key)
    key_broken = bool(enc and not api_key)

    # ✅ 修复（本轮审查）：原实现是二元的 `configured / key_invalid`，
    #    「压根没填 Key」也被报成 key_invalid —— 前端据此提示
    #    「API Key 无法解密，常见于更换过加密密钥 FERNET_KEY」，
    #    用户会去反复排查加密密钥，而真实原因只是没填 Key。
    #    现区分三态，并各自给出可执行的 hint。
    if key_configured:
        status, hint = "configured", ""
    elif key_broken:
        status = "key_invalid"
        hint = (
            "已保存的 API Key 无法解密（常见于更换过加密密钥 FERNET_KEY "
            "或删除过 data/secret_key.key），请重新填写并保存该配置的 API Key"
        )
    else:
        status = "no_key"
        hint = "当前使用配置尚未填写 API Key，所有 AI 生成能力不可用，请补填后保存"

    supports_vision = False
    try:
        preset = PROVIDER_PRESETS.get(d["provider_name"], {})
        if preset.get("supports_vision"):
            supports_vision = True
        elif api_key and d["base_url"]:
            p = _build_provider(d["provider_name"], api_key, d["base_url"], d["model"])
            supports_vision = p.supports_vision()
    except Exception as e:
        # 视觉能力探测属可选增强，失败按不支持处理，不影响连通性判定主流程
        logger.debug("supports_vision 探测失败（provider=%s，按不支持处理）: %s", d.get("provider_name", ""), e)

    try:
        fallback_count = len(await _fallback_chain())
    except Exception:
        fallback_count = 0

    base.update(
        {
            # key 密文存在但解不开时状态是「未就绪」，否则前端会显示绿色却实际调不通
            "status": status,
            "hint": hint,
            "id": d["id"],
            "provider": d["provider_name"],
            "model": d["model"],
            "base_url": d["base_url"],
            "timeout": d["timeout"],
            # ✅ 增强：`concurrency` 为配置值（落库），`live_concurrency` 为并发控制器
            #    当前实际生效值（自适应调整会随时改动）。原实现只回一个 `concurrency`，
            #    前端标着「当前并发」显示的却是配置值 —— 与运行时脱节。
            "concurrency": d["concurrency"],
            "live_concurrency": getattr(concurrency_controller, "current", None),
            "supports_vision": supports_vision,
            "fallback_count": fallback_count,
            "key_configured": key_configured,
            "key_broken": key_broken,
            # ✅ 新增：请求方式（normal / stream）—— 排查「同一份配置，
            #    为什么这条链路首字节慢/长正文易断」时必须能看到当前生效值。
            "request_mode": normalize_request_mode(d.get("request_mode")),
            "request_mode_label": request_mode_label(d.get("request_mode")),
        }
    )
    return base
