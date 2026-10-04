"""AI 配置路由 · 运行时开关（✅ 2026-09-23 新增）。

背景（补齐「临时降级 / 灰度 / 合规下线」缺口）：
  「某厂商临时欠费、被限流、故障，或按合规要求下线」此前只有两条路 ——
  ① 删除配置（丢失配置与密钥，事后要重填）；② 等熔断器自己冷却。
  两者都不合适：需要一个**不删配置、立即生效、可随时恢复**的开关。

实现要点：
  - 落 ``ai_runtime_settings``（键 ``disabled_providers``），复用 provider_factory 的
    缓存 + 代际号机制，写入即失效缓存，**无需重启**；
  - 默认（未设置）= 空集 → ``chat_with_fallback`` 不跳过任何候选，
    **行为与引入该功能前完全一致**；
  - 被禁用的厂商在候选过滤阶段直接跳过（连探测请求都不发），记
    ``action="provider_disabled"`` 审计，且**不计入成功率** ——
    否则重新启用后会被「死配置剔除」继续排除，表现为「开了也没用」；
  - 写入时校验厂商名必须属于「已配置的配置 ∪ 内置厂商预设」，防止拼错后静默无效。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db import get_db, read_db
from app.models import DisabledProvidersIn
from app.services.ai.provider_factory import (
    PROVIDER_PRESETS,
    RUNTIME_DISABLED_PROVIDERS_KEY,
    invalidate_config_cache,
    normalize_provider_name,
    resolve_active_env,
    resolve_disabled_providers,
    upsert_runtime_setting,
)

from .audit import record_config_audit

router = APIRouter(tags=["ai_config"])


async def _configured_providers(db) -> list[str]:
    """库中已出现过的厂商名（去重排序）。"""
    cur = await db.execute("SELECT DISTINCT provider_name FROM ai_config")
    return sorted({str(dict(r).get("provider_name") or "").strip()
                   for r in await cur.fetchall()} - {""})


@router.get("/runtime")
async def get_runtime(db=Depends(read_db)):
    """运行时开关总览：当前生效环境 + 被禁用的厂商 + 可选厂商清单。"""
    disabled = await resolve_disabled_providers()
    configured = await _configured_providers(db)
    # 可选项 = 已配置 ∪ 内置预设 ∪ 已禁用（后者可能既非配置也非预设，需能取消勾选）
    known = sorted(set(configured) | set(PROVIDER_PRESETS) | disabled)
    # ✅ 2026-09-25：环境值损坏时本端点必须仍可用 —— 它正是用户「清空环境
    #    恢复通用」的操作入口所在页面，500 会让恢复路径彻底消失。
    env_error = ""
    try:
        active_env = await resolve_active_env()
    except ValueError as e:
        active_env, env_error = "", str(e)
    return {
        "active_env": active_env,
        "env_error": env_error,
        "disabled_providers": sorted(disabled),
        "providers": known,
        "configured_providers": configured,
        "effective_provider_count": len([p for p in configured if p not in disabled]),
        "all_configured_disabled": bool(configured) and all(p in disabled for p in configured),
    }


@router.put("/runtime/disabled-providers")
async def set_disabled_providers(body: DisabledProvidersIn, request: Request = None,
                                 db=Depends(get_db)):
    """整体覆盖「运行时禁用厂商」清单（传空数组 = 全部恢复）。

    ✅ 无需重启即时生效：写 ``ai_runtime_settings`` + 失效配置缓存。

    ✅ BUG 修复（2026-09-27 · 恢复路径自我锁死）：白名单此前取
    ``已配置 ∪ 内置预设``，**不含「当前已被禁用」的厂商**；而
    ``GET /runtime`` 的 ``providers`` 却**包含**禁用集（注释明确写着
    "后者可能既非配置也非预设，需能取消勾选"）。两侧口径分叉的后果：
    某厂商被禁用后其配置被删除（或内置预设改名/下架），该厂商就变成
    "既不在库、也不在预设、但仍在禁用清单里" —— 界面仍把它列为可选项，
    用户点掉它想恢复，PUT 却因 `未知厂商` 直接 400。
    即：**唯一能把厂商从禁用集里移出来的入口，恰好拒绝执行该操作**，
    禁用集变成不可逆的脏数据，只能手工改库或删 ai_runtime_settings 行。
    修复：白名单与展示端点**同口径**（都并入 ``disabled``），
    保证「界面能点 = 后端能存」。
    """
    configured = await _configured_providers(db)
    disabled_now = await resolve_disabled_providers()
    # 白名单 = 已配置 ∪ 内置预设 ∪ 当前已禁用（与 GET /runtime 的 providers 严格同口径）
    known = set(configured) | set(PROVIDER_PRESETS) | set(disabled_now)

    names: list[str] = []
    for raw in (body.providers or []):
        try:
            name = normalize_provider_name(raw)
        except ValueError as e:
            raise HTTPException(400, f"厂商名非法：{raw!r}（{e}）")
        if name:
            names.append(name)
    unknown = sorted({n for n in names} - known)
    if unknown:
        raise HTTPException(
            400, f"未知厂商：{', '.join(unknown)}"
                 "（可禁用的厂商 = 已配置的配置 ∪ 内置厂商预设）")

    saved = sorted(set(names))
    await upsert_runtime_setting(
        db, RUNTIME_DISABLED_PROVIDERS_KEY,
        json.dumps(saved, ensure_ascii=False))
    saved_set = set(saved)

    warning = ""
    if configured and all(p in saved_set for p in configured):
        warning = ("已禁用**全部**已配置厂商：所有 AI 生成能力将立即不可用，"
                   "请尽快恢复至少一个厂商。")
    else:
        hit = [p for p in configured if p in saved_set]
        if hit:
            warning = (f"已禁用 {', '.join(hit)}：若它们正被「当前使用」配置或场景路由使用，"
                       "相关调用会自动降级到其它候选（可在用量日志里看到 provider_disabled 记录）。")

    await record_config_audit(
        db, "runtime_switch",
        detail=("运行时厂商开关：已全部恢复" if not saved
                else "运行时厂商开关：禁用 " + ", ".join(saved)),
        request=request, commit=False)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(500, f"运行时厂商开关写入失败：{e}")
    # 事务提交后再让所有读路径看到新值，避免「开关已落库、候选链仍用旧缓存」。
    invalidate_config_cache()
    return {"ok": True, "disabled_providers": saved, "count": len(saved),
            "warning": warning}
