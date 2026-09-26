"""AI 配置路由（原单文件 routers/ai_config.py 拆分为子模块）。

对外仍暴露 ``router``（聚合各子模块路由，前缀 /api/v1/ai，tags=["ai_config"]），
``main.py`` 的 ``app.include_router(ai_config.router)`` 与既有测试无需改动。
子模块划分：
  - ``_common``      ：错误分类 + DNS/TCP 预检（被 connectivity / models 复用）
  - ``config``       ：配置增删改 / 激活 / 导入导出 / 降级链 / **多环境切换**
  - ``connectivity`` ：测试连接 / 预检 / 健康快照
  - ``models``       ：厂商模型列表拉取 / 预设 / 自定义供应商
  - ``usage``        ：用量统计 / 审计日志
  - ``audit``        ：配置变更审计 + 版本回滚（✅ 2026-09-23 新增）
  - ``scene_routes`` ：场景模型路由（✅ 2026-09-23 新增）
  - ``runtime``      ：运行时开关（多环境当前环境 / 厂商禁用，✅ 2026-09-23 新增）
"""
from fastapi import APIRouter

from . import config, connectivity, models, usage, audit, scene_routes, runtime

router = APIRouter(prefix="/api/v1/ai", tags=["ai_config"])
router.include_router(config.router)
router.include_router(connectivity.router)
router.include_router(models.router)
router.include_router(usage.router)
router.include_router(audit.router)
router.include_router(scene_routes.router)
router.include_router(runtime.router)

# 兼容历史直接导入（如 _diagnostics/_fallback_chain_test.py 的
# ``from app.routers.ai_config import update_fallback_chain``）
from ._common import (
    _http_status_of, _classify_error, _dns_precheck, _dns_precheck_async,
)
from .config import (
    get_config, save_config, delete_config, toggle_config, clear_config_key,
    update_fallback_chain, export_config, import_config,
    get_active_env, set_active_env,
)
from .connectivity import (test_config, precheck_config, precheck_all, ai_health)
from .models import (
    fetch_provider_models, list_models, fetch_custom_models,
    _resolve_conn_credentials, _MODEL_LIST_MAX, _ctx_tokens, _fmt_context, _fetch_models_list,
)
from .usage import (ai_stats, audit_logs, cleanup_audit_logs)
from .audit import (
    record_config_audit, config_audit_logs, CONFIG_ACTIONS, rollback_config,
    sanitize_config_snapshot, diff_snapshots,
)
from .scene_routes import (list_scene_routes, update_scene_route)
from .runtime import (get_runtime, set_disabled_providers)

__all__ = [
    "router",
    "_http_status_of", "_classify_error", "_dns_precheck", "_dns_precheck_async",
    "_resolve_conn_credentials", "_MODEL_LIST_MAX", "_ctx_tokens", "_fmt_context", "_fetch_models_list",
    "get_config", "save_config", "delete_config", "toggle_config", "clear_config_key",
    "update_fallback_chain", "export_config", "import_config",
    "get_active_env", "set_active_env",
    "test_config", "precheck_config", "precheck_all", "ai_health",
    "fetch_provider_models", "list_models", "fetch_custom_models",
    "ai_stats", "audit_logs", "cleanup_audit_logs",
    "record_config_audit", "config_audit_logs", "CONFIG_ACTIONS",
    "rollback_config", "sanitize_config_snapshot", "diff_snapshots",
    "list_scene_routes", "update_scene_route",
    "get_runtime", "set_disabled_providers",
]
