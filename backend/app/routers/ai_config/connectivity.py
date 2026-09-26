"""AI 配置路由 · 连通性 / 健康（测试连接、预检、健康快照）。"""
from fastapi import APIRouter, Depends

from app.db import read_db
from app.models import AIConfigTest
from app.services.ai.provider_factory import (
    normalize_base_url, PROVIDER_PRESETS,
    normalize_request_mode, request_mode_label,
)
from app.services.crypto import decrypt_api_key

from ._common import _classify_error, _dns_precheck_async, _same_base_url

router = APIRouter(tags=["ai_config"])


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
    if config_id:
        cur = await db.execute(
            "SELECT base_url, api_key_encrypted, model, request_mode"
            " FROM ai_config WHERE id=?", (config_id,))
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "error": "配置不存在（可能已被删除）",
                    "suggestion": "请刷新页面后重新选择配置"}
        saved_base_url = (row["base_url"] or "").strip()
        # 已保存 Key 与其 Base URL 是一组不可拆分的凭据。地址被改写时若仍从
        # DB 取旧 Key，会把密钥发给任意请求方指定的外部地址，属于密钥外泄。
        if base_url and not _same_base_url(base_url, saved_base_url) and not api_key:
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
        return {"ok": False, "error": "base_url 不能为空", "suggestion": "请填写 API Base URL，如 https://api.openai.com/v1"}
    if not model:
        return {"ok": False, "error": "模型名称为空",
                "suggestion": "请先选择或填写模型名称，再测试连接"}
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
        return {"ok": False, "error": str(e), "suggestion": "请填写形如 https://api.example.com/v1 的地址"}

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
    probe_mode = raw_mode if raw_mode in ("normal", "stream") else "auto"
    # auto（未指定）沿用历史策略：流式优先（首字节快），错误归因按 chat 口径。
    prefer_stream = probe_mode != "normal"

    probe_max_tokens = int(getattr(data, "max_tokens", 0) or 0) or 8192
    try:
        provider = _build_provider(data.provider_name, api_key, base_url, model,
                                   max_tokens=probe_max_tokens, timeout=120)
    except ValueError as e:
        return {"ok": False, "error": str(e), "category": "config",
                "suggestion": "请检查供应商、Base URL 与模型名称是否填写完整"}

    import time as _t

    async def _probe_stream() -> str:
        total = ""
        async for chunk in provider.stream([{"role": "user", "content": "回复ok"}]):
            if chunk:
                total += chunk
                # 凑够一小段响应片段即可收工，不必等全部生成完
                if len(total) > 80:
                    break
        return total

    async def _probe_chat() -> str:
        return await provider.chat([{"role": "user", "content": "回复ok"}],
                                   temperature=provider.temperature,
                                   max_tokens=probe_max_tokens)

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
            "provider": data.provider_name,
            "model": provider.model,
            "base_url": base_url,
            "network": pre,
            "latency_ms": elapsed_ms,
            "mode": f"{used}_probe",
            "request_mode": "stream" if used == "stream" else "normal",
        }
        if used != preferred:
            first_err = probe_errors.get(preferred)
            if preferred == "stream":
                payload["warning"] = (
                    "该平台的流式接口（stream）未返回内容，已回退为普通对话探测："
                    "正文生成不受影响，但流式输出类功能可能不可用。"
                    + (f"（流式错误：{str(first_err)[:120]}）" if first_err else "")
                )
            else:
                payload["warning"] = (
                    "普通请求探测未返回内容，改用流式请求探测成功："
                    "该平台可能对非流式请求有限制，"
                    "建议把该配置的「请求方式」改为「流式请求」。"
                    + (f"（普通请求错误：{str(first_err)[:120]}）" if first_err else "")
                )
        return payload

    # 两种方式都没拿到内容：以「该配置实际会被使用的那种方式」的错误为准
    # （显式选了流式就用流式错误；normal / auto 时正文生成走 chat，以 chat 为准），
    # 同时附带另一条路径的错误便于对照差异。
    primary = "stream" if probe_mode == "stream" else "chat"
    err = (probe_errors.get(primary) or probe_errors.get("chat")
           or probe_errors.get("stream") or RuntimeError("厂商返回了空响应"))
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
            classified["raw"] = (f"非流式：{classified.get('raw', '')}"
                                 f" ｜ 流式：{str(_stream_err)[:150]}")
        else:
            classified["raw"] = (f"流式：{classified.get('raw', '')}"
                                 f" ｜ 非流式：{str(_chat_err)[:150]}")
    classified["network"] = pre
    classified["latency_ms"] = elapsed_ms
    return {
        "ok": False,
        "error": classified["message"],
        "suggestion": classified["suggestion"],
        "category": classified["category"],
        "label": classified.get("label", ""),
        "raw": classified.get("raw", str(err)),
        "model": model,
        "base_url": base_url,
        "network": pre,
        "latency_ms": elapsed_ms,
    }


@router.post("/config/precheck")
async def precheck_config(data: AIConfigTest, db=Depends(read_db)):
    """只做连通性预检（DNS + TCP），不发送任何需要认证的请求。

    ✅ 增强：支持只传 config_id（从库中取 base_url），与「测试连接」的能力对齐。
    """
    base_url = (data.base_url or "").strip()
    if not base_url and (data.config_id or "").strip():
        cur = await db.execute(
            "SELECT base_url FROM ai_config WHERE id=?", (data.config_id.strip(),))
        row = await cur.fetchone()
        if row:
            base_url = (row["base_url"] or "").strip()
    if not base_url:
        return {"ok": False, "step": "dns", "message": "Base URL 为空，请先填写地址"}
    try:
        base_url = normalize_base_url(base_url)
    except ValueError as e:
        return {"ok": False, "step": "url", "message": str(e)}
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
        "SELECT id, provider_name, model, base_url, request_mode FROM ai_config"
        " ORDER BY priority ASC, updated_at DESC")
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

    async def _one(item):
        r, url = item
        res = await _dns_precheck_async(url)
        return {
            "id": r["id"],
            "provider_name": r["provider_name"],
            "model": r["model"],
            "base_url": url,
            "request_mode": normalize_request_mode(r.get("request_mode")),
            "ok": bool(res.get("ok")),
            "message": res.get("message") or ("可达" if res.get("ok") else "不可达"),
            "ip": res.get("ip", ""),
            "step": res.get("step", ""),
        }

    results = await asyncio.gather(*[_one(t) for t in targets]) if targets else []
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "items": results,
        "total": len(results),
        "ok_count": ok_count,
        "fail_count": len(results) - ok_count,
        "skipped_no_url": len(rows) - len(results),
    }


@router.get("/health")
async def ai_health(db=Depends(read_db)):
    """当前 AI 配置健康快照。

    ✅ 增强：原实现只返回 status/provider/model/supports_vision，
      排查「明明配了却调不通」时看不到 Base URL、降级候选数、熔断状态与并发，
      只能靠翻库。现一次性给出运行时可观测信息。
    """
    from app.services.ai.provider_factory import (
        _build_provider, _fallback_chain, resolve_active_env, resolve_disabled_providers,
        _load_active_config,
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
        row, env_error = None, str(e)

    # 熔断器状态（per-provider）
    breaker = {}
    try:
        for name, entry in getattr(circuit_breaker, "_per_provider", {}).items():
            breaker[name] = {"state": entry.get("state"),
                             "failures": entry.get("failures", 0)}
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
    }

    if not row:
        if env_error:
            # 不把「环境值损坏」误报成「尚未配置模型」——两者提示与修法完全不同
            base.update({"status": "env_corrupt",
                         "fallback_count": 0,
                         "hint": f"{env_error}；请在「运行时设置」把当前生效环境清空"
                                 "（恢复通用环境）后重试"})
        else:
            base.update({"status": "not_configured",
                         "fallback_count": 0,
                         "hint": "尚未启用任何文本模型配置，所有 AI 生成能力将不可用"})
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
        hint = ("已保存的 API Key 无法解密（常见于更换过加密密钥 FERNET_KEY "
                "或删除过 data/secret_key.key），请重新填写并保存该配置的 API Key")
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
    except Exception:
        pass

    try:
        fallback_count = len(await _fallback_chain())
    except Exception:
        fallback_count = 0

    base.update({
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
    })
    return base
